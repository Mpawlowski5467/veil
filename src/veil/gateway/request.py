"""Masking a Messages API request before it leaves the machine.

Every field of the request body is handled by an explicit rule: masked (text
the model reads), passed through (settings, tool definitions, signed thinking,
image and PDF data), or refused. A field or block type without a rule is
refused, never forwarded, so a new kind of content can't slip through unmasked
after a client update.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from typing import Any

from ..shield import Shield
from .ledger import Ledger


class UnsupportedRequestError(ValueError):
    """The request has a field or block this gateway has no rule for.

    Attributes:
        path: Where in the body, e.g. ``messages[3].content[0].source``.
            Never contains a value from the request.
    """

    def __init__(self, path: str, problem: str) -> None:
        """Describe the unsupported part by its path, never by its value."""
        super().__init__(f"{path}: {problem}")
        self.path = path
        self.problem = problem


#: What replaces a line that still holds a known value after masking.
WITHHELD_LINE = "[withheld: this line holds personal data that could not be masked]"

#: Told to the model once, in the system prompt.
DEFAULT_NOTE = (
    "Personal data in this conversation has been replaced with placeholders "
    "such as [EMAIL_1] or [PERSON_2]; the real values are put back on the "
    "user's computer. Write placeholders exactly as they appear, including in "
    "tool calls, and never guess the values behind them. [LITERAL_n] stands "
    "for text that itself looks like a placeholder: copy it unchanged."
)

# The client's billing header, which the API recognizes only if unchanged.
_BILLING_HEADER = re.compile(r"x-anthropic-billing-header: [^\n]*")

_TOP_LEVEL_PASS = frozenset(
    {
        "context_management",
        "max_tokens",
        "metadata",
        "model",
        "output_config",
        "service_tier",
        "stream",
        "temperature",
        "thinking",
        "tool_choice",
        "tools",
        "top_k",
        "top_p",
    }
)
_ROLES = frozenset({"user", "assistant", "system"})

# Allowed keys for each content block type, besides "type".
_BLOCK_KEYS = {
    "text": {"text", "cache_control", "citations"},
    "image": {"source", "cache_control"},
    "document": {"source", "title", "context", "citations", "cache_control"},
    "tool_use": {"id", "name", "input", "cache_control", "caller"},
    "tool_result": {"tool_use_id", "content", "is_error", "cache_control"},
    "thinking": {"thinking", "signature", "cache_control"},
    "redacted_thinking": {"data", "cache_control"},
}
# Blocks allowed inside a tool_result's content, and in a document's content.
_NESTED_BLOCKS = frozenset({"text", "image", "document"})


class _Memo:
    """Masked results by input text, oldest dropped past a size limit.

    Keeping a result makes masking repeatable: a block sent again in the next
    request (the whole history is resent each time) masks exactly as before,
    even if the vault learned new values in between.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._size = 0
        self._items: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        value = self._items.get(key)
        if value is not None:
            self._items.move_to_end(key)
        return value

    def put(self, key: str, value: str) -> None:
        if key in self._items:
            return
        self._items[key] = value
        self._size += len(key) + len(value)
        while self._size > self._limit and self._items:
            old_key, old_value = self._items.popitem(last=False)
            self._size -= len(old_key) + len(old_value)


class RequestMasker:
    """Masks every text a Messages API request would show the model.

    One masker serves one conversation: its shield's vault holds the
    conversation's placeholders, and its ledger the replies restored in it.

    Args:
        shield: Does the masking. Build it the same way for the whole
            conversation, with ``normalize`` off, so masking is repeatable.
        ledger: The replies the gateway restored; their masked form is sent
            back instead of masking them again.
        note: A system-prompt note telling the model about placeholders, or
            None for no note.
        memo_limit: Roughly how many characters of masked text to keep for
            reuse (see above).
    """

    def __init__(
        self,
        shield: Shield,
        ledger: Ledger,
        *,
        note: str | None = DEFAULT_NOTE,
        memo_limit: int = 64_000_000,
    ) -> None:
        """Create a masker for one conversation."""
        self._shield = shield
        self._ledger = ledger
        self._note = note
        self._memo = _Memo(memo_limit)

    def mask(self, body: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of a request body with every model-read text masked.

        Raises:
            UnsupportedRequestError: If the body has a field, role, or block type
                without a rule, or a field of the wrong kind.
        """
        if not isinstance(body, dict):
            raise UnsupportedRequestError("$", "the body is not a JSON object")
        out: dict[str, Any] = {}
        for key, value in body.items():
            if key in _TOP_LEVEL_PASS:
                out[key] = value
            elif key == "system":
                out[key] = self._system(value)
            elif key == "messages":
                out[key] = self._messages(value)
            elif key == "stop_sequences":
                out[key] = self._strings(value, "stop_sequences")
            elif key == "safeguards":
                out[key] = self._anything(value, "safeguards")
            else:
                raise UnsupportedRequestError(_key(key), "unknown field")
        if self._note is not None and "messages" in out:
            out["system"] = self._with_note(out.get("system"))
        return out

    # --- the parts of a request ---------------------------------------------

    def _system(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._text(value)
        blocks = _list(value, "system")
        out = []
        for i, block in enumerate(blocks):
            path = f"system[{i}]"
            _check_block(block, path, allowed={"text"})
            text = block["text"]
            if _BILLING_HEADER.fullmatch(text):
                out.append(block)
            else:
                out.append({**block, "text": self._text(_str(text, f"{path}.text"))})
        return out

    def _with_note(self, system: Any) -> Any:
        note = {"type": "text", "text": self._note}
        if system is None:
            return [note]
        if isinstance(system, str):
            return [{"type": "text", "text": system}, note]
        return [*system, note]

    def _messages(self, value: Any) -> list[Any]:
        out = []
        for i, message in enumerate(_list(value, "messages")):
            path = f"messages[{i}]"
            if not isinstance(message, dict) or set(message) - {"role", "content"}:
                raise UnsupportedRequestError(
                    path, "a message has only role and content"
                )
            role = message.get("role")
            if role not in _ROLES:
                raise UnsupportedRequestError(f"{path}.role", "unknown role")
            content = message.get("content")
            if isinstance(content, str):
                masked: Any = (
                    self._reply_text(content)
                    if role == "assistant"
                    else self._text(content)
                )
            else:
                masked = [
                    self._block(block, f"{path}.content[{j}]", role=role)
                    for j, block in enumerate(_list(content, f"{path}.content"))
                ]
            out.append({**message, "content": masked})
        return out

    def _block(self, block: Any, path: str, *, role: str) -> Any:
        kind = _check_block(block, path, allowed=set(_BLOCK_KEYS))
        if kind == "text":
            _no_citations(block, path)
            text = _str(block["text"], f"{path}.text")
            masked = self._reply_text(text) if role == "assistant" else self._text(text)
            return {**block, "text": masked}
        if kind in ("thinking", "redacted_thinking"):
            # Signed by the API: sent back exactly as the model wrote it.
            return block
        if kind == "image":
            _media_source(block.get("source"), f"{path}.source")
            return block
        if kind == "document":
            return self._document(block, path)
        if kind == "tool_use":
            return self._tool_use(block, path, role=role)
        return self._tool_result(block, path)

    def _tool_use(self, block: dict[str, Any], path: str, *, role: str) -> Any:
        tool_input = block.get("input")
        if role == "assistant" and isinstance(block.get("id"), str):
            masked = self._ledger.masked_tool_input(block["id"], tool_input)
            if masked is not None:
                return {**block, "input": masked}
        return {**block, "input": self._anything(tool_input, f"{path}.input")}

    def _tool_result(self, block: dict[str, Any], path: str) -> Any:
        content = block.get("content")
        if content is None or isinstance(content, str):
            masked = None if content is None else self._text(content)
            return {**block, "content": masked} if "content" in block else block
        nested = []
        for i, item in enumerate(_list(content, f"{path}.content")):
            item_path = f"{path}.content[{i}]"
            kind = _check_block(item, item_path, allowed=_NESTED_BLOCKS)
            if kind == "text":
                _no_citations(item, item_path)
                text = _str(item["text"], f"{item_path}.text")
                nested.append({**item, "text": self._text(text)})
            elif kind == "image":
                _media_source(item.get("source"), f"{item_path}.source")
                nested.append(item)
            else:
                nested.append(self._document(item, item_path))
        return {**block, "content": nested}

    def _document(self, block: dict[str, Any], path: str) -> Any:
        _no_citations(block, path)
        out = dict(block)
        for key in ("title", "context"):
            if block.get(key) is not None:
                out[key] = self._text(_str(block[key], f"{path}.{key}"))
        source = block.get("source")
        source_path = f"{path}.source"
        if not isinstance(source, dict):
            raise UnsupportedRequestError(source_path, "not an object")
        kind = source.get("type")
        if kind in ("base64", "url"):
            _media_source(source, source_path)
            return out
        if kind == "text":
            if set(source) - {"type", "media_type", "data"}:
                raise UnsupportedRequestError(source_path, "unknown field")
            data = _str(source.get("data"), f"{source_path}.data")
            out["source"] = {**source, "data": self._text(data)}
            return out
        raise UnsupportedRequestError(f"{source_path}.type", "unknown document source")

    # --- masking text --------------------------------------------------------

    def _reply_text(self, text: str) -> str:
        """Mask assistant text: the model's own words if the gateway has them."""
        masked = self._ledger.masked_text(text)
        return masked if masked is not None else self._text(text)

    def _text(self, text: str) -> str:
        if not text:
            return text
        key = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        masked = _mask_withholding_leaks(self._shield, text)
        self._memo.put(key, masked)
        return masked

    def _strings(self, value: Any, path: str) -> list[str]:
        return [
            self._text(_str(item, f"{path}[{i}]"))
            for i, item in enumerate(_list(value, path))
        ]

    def _anything(self, value: Any, path: str) -> Any:
        """Mask every string in a JSON value; keys and ``type`` values stay."""
        if isinstance(value, str):
            return self._text(value)
        if isinstance(value, list):
            return [
                self._anything(item, f"{path}[{i}]") for i, item in enumerate(value)
            ]
        if isinstance(value, dict):
            return {
                key: item
                if key == "type" and isinstance(item, str)
                else self._anything(item, f"{path}.{key}")
                for key, item in value.items()
            }
        return value


def _mask_withholding_leaks(shield: Shield, text: str) -> str:
    """Mask ``text``; replace any line that still holds a known value."""
    result = shield.mask(text)
    if not any(w.startswith("Leak check") for w in result.warnings):
        return result.text
    # Mask each line on its own (placeholders are the same: the values are in
    # the vault now) and withhold the lines that still leak.
    lines = text.split("\n")
    out = []
    for line in lines:
        masked = shield.mask(line)
        leaked = any(w.startswith("Leak check") for w in masked.warnings)
        out.append(WITHHELD_LINE if leaked else masked.text)
    return "\n".join(out)


# --- checking shapes -----------------------------------------------------------


_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _key(key: str) -> str:
    """Name a key in an error only if it looks like a field name, not data."""
    return key if _FIELD_NAME.fullmatch(key) else "<key>"


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise UnsupportedRequestError(path, "not a list")
    return value


def _str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise UnsupportedRequestError(path, "not a string")
    return value


def _check_block(block: Any, path: str, *, allowed: set[str] | frozenset[str]) -> str:
    """Return the block's type after checking it has a rule and known keys."""
    if not isinstance(block, dict):
        raise UnsupportedRequestError(path, "not an object")
    kind = block.get("type")
    if not isinstance(kind, str) or kind not in allowed:
        raise UnsupportedRequestError(f"{path}.type", "unknown block type")
    extra = set(block) - _BLOCK_KEYS[kind] - {"type"}
    if extra:
        raise UnsupportedRequestError(
            f"{path}.{_key(sorted(extra)[0])}", "unknown field"
        )
    if kind == "text":
        _str(block.get("text"), f"{path}.text")
    return kind


def _no_citations(block: dict[str, Any], path: str) -> None:
    if block.get("citations"):
        raise UnsupportedRequestError(f"{path}.citations", "citations aren't supported")


def _media_source(source: Any, path: str) -> None:
    """Check an image or PDF source, which is passed on as it is."""
    if not isinstance(source, dict):
        raise UnsupportedRequestError(path, "not an object")
    kind = source.get("type")
    keys: dict[str, set[str]] = {
        "base64": {"type", "media_type", "data"},
        "url": {"type", "url"},
    }
    if kind not in keys:
        raise UnsupportedRequestError(f"{path}.type", "unknown source type")
    if set(source) - keys[kind]:
        raise UnsupportedRequestError(path, "unknown field")
