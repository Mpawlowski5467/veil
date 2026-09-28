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
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from ..placeholders import placeholder_type
from ..shield import Shield
from ..vault.base import Vault
from .ledger import Ledger

_T = TypeVar("_T")

# An index in a path, e.g. the [3] in messages[3].content.
_INDEX = re.compile(r"\[[0-9]+\]")


class UnsupportedRequestError(ValueError):
    """The request has content this gateway has no rule for.

    Paths and problems never contain a value from the request: a key or a
    type is named only when it looks like a field name and holds nothing
    masking would change.

    Attributes:
        path: Where the first problem is, e.g. ``messages[3].content[0].source``.
        problem: What is wrong there.
        problems: Every distinct problem, in the order found, as ``(path,
            problem, count)``. Problems at the same place in different
            messages or blocks (the path without its indices) count as one,
            under the path where it was first found.
        total: How many places have a problem.
    """

    def __init__(
        self, path: str, problem: str, *, more: Sequence[tuple[str, str]] = ()
    ) -> None:
        """Describe the unsupported parts by their paths, never by value."""
        found = [(path, problem), *more]
        groups: dict[tuple[str, str], list[Any]] = {}
        for where, what in found:
            key = (_INDEX.sub("[]", where), what)
            if key in groups:
                groups[key][2] += 1
            else:
                groups[key] = [where, what, 1]
        self.found = tuple(found)
        self.problems = tuple((w, p, n) for w, p, n in groups.values())
        self.total = len(found)
        self.path = path
        self.problem = problem
        super().__init__(
            "; ".join(
                f"{where}: {what}" + (f" ({count} times)" if count > 1 else "")
                for where, what, count in self.problems
            )
        )


#: What replaces a line that still holds a known value after masking.
WITHHELD_LINE = "[withheld: this line holds personal data that could not be masked]"

#: Told to the model once, in the system prompt.
DEFAULT_NOTE = (
    "Personal data in this conversation has been replaced with placeholders: "
    "a type and a number in square brackets, in the form [TYPE_N]. The real "
    "values are put back on the user's computer, including in tool calls, so "
    "write each placeholder exactly as it appears and never guess the value "
    "behind it. [LITERAL_n] stands for text that itself looks like a "
    "placeholder: copy it unchanged."
)

# The client's billing line. The API recognizes it by its version, so the
# line's name and version are kept as they are, and only when the version is
# the one in the client's User-Agent: then they tell nothing the User-Agent
# doesn't. The rest of the line is masked like any text.
_BILLING_LINE = re.compile(
    r"^x-anthropic-billing-header:[ \t]*cc_version="
    r"(?P<version>[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6})(?P<hash>\.[0-9a-f]{3})?"
    r"(?=;|[ \t]*$)",
    re.ASCII | re.IGNORECASE | re.MULTILINE,
)

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
_MESSAGE_KEYS = frozenset({"role", "content", "output_config"})
# The effort a system message can set for the turns after it (its
# output_config). The top-level output_config is still passed as it is.
_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

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


# An exact placeholder, which the known-value pass leaves as it is.
_EXACT = r"\[[A-Z][A-Z0-9_]{0,63}_[0-9]{1,9}\]"


class _KnownValues:
    """Masks every known value wherever it is, even glued to other text.

    Masking finds a registered value only as a whole word, so "Jan Nowakem"
    (a restored "[PERSON_1]em") would go out as it is. This pass runs after
    masking, over everything that leaves: any value in the vault, or any
    registered value, still present becomes its placeholder.
    """

    #: Shorter values would be masked inside too many ordinary words.
    MIN_LENGTH = 3

    def __init__(self, vault: Vault, registered: Mapping[str, str]) -> None:
        self._vault = vault
        self._registered = {
            value: kind
            for value, kind in registered.items()
            if len(value) >= self.MIN_LENGTH
        }
        self._size = -1
        self._types: dict[str, str] = {}
        self._pattern: re.Pattern[str] | None = None

    def reset(self) -> None:
        self._size = -1

    def _current(self) -> re.Pattern[str] | None:
        size = len(self._vault)
        if size != self._size:
            types = dict(self._registered)
            for placeholder, value in self._vault.items():
                kind = placeholder_type(placeholder)
                if kind not in (None, "LITERAL") and len(value) >= self.MIN_LENGTH:
                    types.setdefault(value, kind)
            self._types = types
            values = sorted(types, key=len, reverse=True)
            self._pattern = (
                re.compile(
                    f"(?P<ph>{_EXACT})|(?P<v>{'|'.join(map(re.escape, values))})"
                )
                if values
                else None
            )
            self._size = size
        return self._pattern

    def mask(self, text: str) -> str:
        """Replace every known value left in ``text`` with its placeholder."""
        pattern = self._current()
        if pattern is None or not text:
            return text

        def replace(match: re.Match[str]) -> str:
            value = match["v"]
            if value is None:
                return match.group(0)
            found = self._vault.get_placeholder(value)
            return found or self._vault.get_or_create(value, self._types[value])

        return pattern.sub(replace, text)

    def found_in(self, text: str) -> bool:
        """Whether ``text`` holds a known value outside a placeholder."""
        pattern = self._current()
        return pattern is not None and any(
            m["v"] is not None for m in pattern.finditer(text)
        )


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
        registered: The values registered with the shield (``{value:
            type}``), so they are masked even glued to other text before the
            vault has them.
        memo_limit: Roughly how many characters of masked text to keep for
            reuse (see above).
    """

    def __init__(
        self,
        shield: Shield,
        ledger: Ledger,
        *,
        note: str | None = DEFAULT_NOTE,
        registered: Mapping[str, str] | None = None,
        memo_limit: int = 64_000_000,
    ) -> None:
        """Create a masker for one conversation."""
        self._shield = shield
        self._ledger = ledger
        self._note = note
        self._memo_limit = memo_limit
        self._memo = _Memo(memo_limit)
        self._known = _KnownValues(shield.vault, registered or {})
        self._vault_state: tuple[int, tuple[str, str] | None] = (0, None)
        self._problems: list[tuple[str, str]] = []
        self._client_version: str | None = None

    def mask(
        self, body: dict[str, Any], *, client_version: str | None = None
    ) -> dict[str, Any]:
        """Return a copy of a request body with every model-read text masked.

        Every part of the body is checked, even after a problem is found, so
        a refusal names all of them. ``client_version`` is the client's
        version from its User-Agent (``claude-cli/2.1.283`` gives
        ``"2.1.283"``), if known: a billing line with that version keeps it.

        Raises:
            UnsupportedRequestError: If the body has a field, role, or block type
                without a rule, or a field of the wrong kind.
        """
        if not isinstance(body, dict):
            raise UnsupportedRequestError("$", "the body is not a JSON object")
        self._notice_a_cleared_vault()
        self._problems = []
        self._client_version = client_version
        try:
            out = self._body(body)
            # Named only now: by the end of the body, the vault knows every
            # value in it, however early a name holding one came.
            problems = [self._safe(*problem) for problem in self._problems]
        finally:
            self._problems = []
        if problems:
            raise UnsupportedRequestError(*problems[0], more=problems[1:])
        self._vault_state = self._state()
        return out

    def _body(self, body: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in body.items():
            path = _key(key)
            # The conversation is checked message by message, so a problem
            # is named where it is.
            if key not in _WALKED and not self._guard(_check_depth, value, path):
                continue
            if key in _TOP_LEVEL_PASS:
                out[key] = value
            elif key == "system":
                out[key] = self._guard(self._system, value)
            elif key == "messages":
                out[key] = self._guard(self._messages, value)
            elif key == "stop_sequences":
                out[key] = self._guard(self._strings, value, "stop_sequences")
            elif key == "safeguards":
                out[key] = self._guard(self._anything, value, "safeguards")
            else:
                self._problem(path, "unknown field")
        if self._note is not None and isinstance(out.get("messages"), list):
            out["system"] = self._with_note(out.get("system"))
        return out

    # --- collecting problems -------------------------------------------------

    def _guard(self, part: Callable[..., _T], *args: Any) -> _T | None:
        """Run one part of the masking; on a problem, note it and go on.

        Returns None for a part that failed. Nothing masked after a problem
        is sent, since the request is refused, but the rest is still checked
        so the refusal names every problem.
        """
        try:
            return part(*args)
        except UnsupportedRequestError as error:
            for path, problem in error.found:
                self._problem(path, problem)
            return None

    def _problem(self, path: str, problem: str) -> None:
        """Note a problem; its names are checked for data when it is shown."""
        self._problems.append((path, problem))

    def _safe(self, path: str, problem: str) -> tuple[str, str]:
        """Return a problem as it may be shown.

        A name taken from the request is shown only if it holds no data;
        otherwise it becomes ``<key>``, or a quoted name is left out.
        """

        def segment(match: re.Match[str]) -> str:
            return match[0] if self._nameable(match[0]) else "<key>"

        def quoted(match: re.Match[str]) -> str:
            return match[0] if self._nameable(match[1]) else ""

        return _SEGMENT.sub(segment, path), _QUOTED.sub(quoted, problem)

    def _nameable(self, name: str) -> bool:
        """Whether a name may appear in a refusal.

        It may if it is one of the gateway's own words, or if masking leaves
        it as it is. The memo isn't used: it may hold a verdict from before
        the vault knew a value.
        """
        if name in _OWN_WORDS:
            return True
        return _mask_text(self._shield, self._known, name) == name

    def _state(self) -> tuple[int, tuple[str, str] | None]:
        items = self._shield.vault.items()
        return len(items), (items[0] if items else None)

    def _notice_a_cleared_vault(self) -> None:
        """Start afresh if the vault was cleared since the last request.

        After ``forget`` or a purge, numbering starts again, so masked text
        kept from before would name the wrong values.
        """
        size, first = self._state()
        old_size, old_first = self._vault_state
        if size < old_size or (old_first is not None and first != old_first):
            self._memo = _Memo(self._memo_limit)
            self._known.reset()
            forget = getattr(self._ledger, "forget", None)
            if callable(forget):
                forget()

    # --- the parts of a request ---------------------------------------------

    def _system(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._billed(value, first=True)
        blocks = _list(value, "system")
        out = []
        for i, block in enumerate(blocks):
            path = f"system[{i}]"
            if not self._guard(_check_depth, block, path):
                continue
            masked = self._guard(self._system_block, block, path, i == 0)
            if masked is not None:
                out.append(masked)
        return out

    def _system_block(self, block: Any, path: str, first: bool) -> Any:
        _check_block(block, path, allowed={"text"})
        _no_citations(block, path)
        text = _str(block["text"], f"{path}.text")
        return {**block, "text": self._billed(text, first=first)}

    def _billed(self, text: str, *, first: bool) -> str:
        """Mask system text, keeping a billing line's name and version."""
        line = self._billing_line(text, first=first)
        if line is None:
            return self._text(text)
        before, after = text[: line.start()], text[line.end() :]
        return self._text(before) + line[0] + self._text(after)

    def _billing_line(self, text: str, *, first: bool) -> re.Match[str] | None:
        """The billing line whose name and version may be kept, if any.

        Its version must be the client's. Without a User-Agent version, only
        a line that starts the first system block, with the version's short
        hash, is kept, as Claude Code writes it.
        """
        for line in _BILLING_LINE.finditer(text):
            if self._client_version is not None:
                if line["version"] == self._client_version:
                    return line
            elif first and line.start() == 0 and line["hash"] is not None:
                return line
        return None

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
            if not self._guard(_check_depth, message, path):
                continue
            masked = self._guard(self._message, message, path)
            if masked is not None:
                out.append(masked)
        return out

    def _message(self, message: Any, path: str) -> Any:
        if not isinstance(message, dict):
            raise UnsupportedRequestError(path, "not an object")
        for key in sorted(set(message) - _MESSAGE_KEYS):
            self._problem(f"{path}.{_key(key)}", "unknown field")
        role = message.get("role")
        if not isinstance(role, str) or role not in _ROLES:
            # Checked on as a user message, so every other problem is named.
            self._problem(f"{path}.role", f"unknown role{_named(role)}")
            role = "user"
        elif "output_config" in message:
            self._guard(
                _effort_only, message["output_config"], f"{path}.output_config", role
            )
        content = message.get("content")
        if isinstance(content, str):
            masked: Any = (
                self._reply_text(content)
                if role == "assistant"
                else self._text(content)
            )
        else:
            blocks = [
                self._guard(self._block, block, f"{path}.content[{j}]", role)
                for j, block in enumerate(_list(content, f"{path}.content"))
            ]
            masked = [block for block in blocks if block is not None]
        return {**message, "content": masked}

    def _block(self, block: Any, path: str, role: str) -> Any:
        kind = _check_block(block, path, allowed=set(_BLOCK_KEYS))
        if kind == "text":
            _no_citations(block, path)
            text = _str(block["text"], f"{path}.text")
            masked = self._reply_text(text) if role == "assistant" else self._text(text)
            return {**block, "text": masked}
        if kind == "redacted_thinking":
            return block
        if kind == "thinking":
            # Signed by the API, so sent back exactly as the model wrote it,
            # or not at all: thinking about data seen before it was masked
            # (a value registered later, a session begun without the
            # gateway) is dropped.
            thinking = block.get("thinking")
            if isinstance(thinking, str) and self._known.found_in(thinking):
                return None
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
        tool_id = block.get("id")
        if (
            role == "assistant"
            and isinstance(tool_id, str)
            and _TOOL_ID.fullmatch(tool_id)
        ):
            masked = self._ledger.masked_tool_input(tool_id, tool_input)
            if masked is not None:
                return {**block, "input": self._known_everywhere(masked)}
        return {**block, "input": self._anything(tool_input, f"{path}.input")}

    def _tool_result(self, block: dict[str, Any], path: str) -> Any:
        content = block.get("content")
        if content is None or isinstance(content, str):
            masked = None if content is None else self._text(content)
            return {**block, "content": masked} if "content" in block else block
        nested = []
        for i, item in enumerate(_list(content, f"{path}.content")):
            masked = self._guard(self._nested, item, f"{path}.content[{i}]")
            if masked is not None:
                nested.append(masked)
        return {**block, "content": nested}

    def _nested(self, item: Any, path: str) -> Any:
        kind = _check_block(item, path, allowed=_NESTED_BLOCKS)
        if kind == "text":
            _no_citations(item, path)
            return {**item, "text": self._text(_str(item["text"], f"{path}.text"))}
        if kind == "image":
            _media_source(item.get("source"), f"{path}.source")
            return item
        return self._document(item, path)

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
            _no_extra_keys(source, {"type", "media_type", "data"}, source_path)
            data = _str(source.get("data"), f"{source_path}.data")
            out["source"] = {**source, "data": self._text(data)}
            return out
        raise UnsupportedRequestError(
            f"{source_path}.type", f"unknown document source{_named(kind)}"
        )

    # --- masking text --------------------------------------------------------

    def _reply_text(self, text: str) -> str:
        """Mask assistant text: the model's own words if the gateway has them."""
        masked = self._ledger.masked_text(text)
        return self._known.mask(masked) if masked is not None else self._text(text)

    def _text(self, text: str) -> str:
        if not text:
            return text
        key = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        masked = _mask_text(self._shield, self._known, text)
        self._memo.put(key, masked)
        return masked

    def _known_everywhere(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._known.mask(value)
        if isinstance(value, list):
            return [self._known_everywhere(item) for item in value]
        if isinstance(value, dict):
            return {key: self._known_everywhere(item) for key, item in value.items()}
        return value

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


def _mask_text(shield: Shield, known: _KnownValues, text: str) -> str:
    """Mask ``text``, then any known value left in it, even glued to a word.

    A line that still holds a known value after that is withheld.
    """
    masked = known.mask(shield.mask(text).text)
    if not known.found_in(masked):
        return masked
    # Withhold, from the masked text, each line where a known value remains
    # (never mask lines alone: a value may span a line break).
    lines = masked.split("\n")
    return "\n".join(WITHHELD_LINE if known.found_in(line) else line for line in lines)


# --- checking shapes -----------------------------------------------------------


_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_TYPE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Top-level parts walked item by item, each item checked for depth on its own.
_WALKED = frozenset({"messages", "system"})
# The gateway's own words in paths and problems: never data, always shown.
_OWN_WORDS = frozenset(
    {
        *_TOP_LEVEL_PASS,
        *_WALKED,
        *_MESSAGE_KEYS,
        *_BLOCK_KEYS,
        *(key for keys in _BLOCK_KEYS.values() for key in keys),
        "stop_sequences",
        "safeguards",
        "type",
        "effort",
        "media_type",
        "data",
        "url",
    }
)
# The names in a path, and a name quoted in a problem, which a masker checks
# for data before a refusal shows them.
_SEGMENT = re.compile(r"(?<![^.])[A-Za-z_][A-Za-z0-9_]{0,63}(?![A-Za-z0-9_])")
_QUOTED = re.compile(r" '([a-z][a-z0-9_]{0,63})'")
# A tool call's id, as the API makes them; anything else is never looked up.
_TOOL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: How deeply a request's JSON may nest. Claude Code's requests nest about ten
#: levels, and tool inputs a few more; deeper values are refused rather than
#: walked (a recursion that deep would fail).
MAX_DEPTH = 100


def billing_version(body: Any) -> str | None:
    """The Claude Code version in a body's billing line, if there is one.

    Only for naming the client in a message; never raises.
    """
    system = body.get("system") if isinstance(body, dict) else None
    texts = [system] if isinstance(system, str) else system
    if not isinstance(texts, list):
        return None
    for block in texts:
        text = block.get("text") if isinstance(block, dict) else block
        if isinstance(text, str):
            found = _BILLING_LINE.search(text)
            if found:
                return found["version"]
    return None


def _key(key: str) -> str:
    """Name a key in an error only if it looks like a field name, not data."""
    return key if _FIELD_NAME.fullmatch(key) else "<key>"


def _named(value: Any) -> str:
    """`` 'name'`` for a problem, if ``value`` looks like a type name, else ``''``.

    The masker still checks the name for data before showing it.
    """
    if isinstance(value, str) and _TYPE_NAME.fullmatch(value):
        return f" '{value}'"
    return ""


def _check_depth(value: Any, path: str) -> bool:
    """Refuse a value nested deeper than `MAX_DEPTH`, without recursing."""
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise UnsupportedRequestError(path, "nested too deeply")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return True


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
        raise UnsupportedRequestError(
            f"{path}.type", f"unknown block type{_named(kind)}"
        )
    _no_extra_keys(block, _BLOCK_KEYS[kind] | {"type"}, path)
    if kind == "text":
        _str(block.get("text"), f"{path}.text")
    return kind


def _no_extra_keys(value: dict[str, Any], allowed: set[str], path: str) -> None:
    """Refuse every key of ``value`` not in ``allowed``, naming each one."""
    found = [
        (f"{path}.{_key(key)}", "unknown field") for key in sorted(set(value) - allowed)
    ]
    if found:
        raise UnsupportedRequestError(*found[0], more=found[1:])


def _effort_only(value: Any, path: str, role: Any) -> None:
    """Check a message's output_config: a system message's effort, and no more.

    It is passed on as it is. Every refusal here names output_config:
    Claude Code (2.1.283) then sends the conversation again without its
    per-turn settings (for output_config.timing, only without the time), so
    the session goes on.
    """
    if role != "system":
        raise UnsupportedRequestError(path, "only a system message has output_config")
    if not isinstance(value, dict):
        raise UnsupportedRequestError(path, "not an object")
    _no_extra_keys(value, {"effort"}, path)
    effort = value.get("effort")
    if not isinstance(effort, str) or effort not in _EFFORT_LEVELS:
        raise UnsupportedRequestError(f"{path}.effort", "not an effort level")


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
    if not isinstance(kind, str) or kind not in keys:
        raise UnsupportedRequestError(
            f"{path}.type", f"unknown source type{_named(kind)}"
        )
    _no_extra_keys(source, keys[kind], path)
