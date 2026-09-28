"""Masking a Messages API request before it leaves the machine.

Every field of the request body is handled by an explicit rule: masked (text
the model reads), passed through (settings, tool definitions, signed thinking,
image and PDF data), or refused. A field or block type without a rule is
refused, never forwarded, so a new kind of content can't slip through unmasked
after a client update.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from ..placeholders import placeholder_type
from ..shield import Shield
from ..vault.base import Vault
from .ledger import Ledger
from .vocab import WORDS

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
    r"^(?i:x-anthropic-billing-header):[ \t]*cc_version="
    r"(?P<version>[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6})(?:\.(?P<hash>[0-9a-f]{3}))?"
    r"(?=;|[ \t]*$)",
    re.ASCII | re.MULTILINE,
)
# The short hash after the version is Claude Code's fingerprint of the first
# prompt: a few of its characters, salted and hashed. Made from the real
# prompt, it could tell a masked character, so it is made again from the
# masked prompt the API gets, or left out.
_FINGERPRINT_SALT = "59cf53e54c78"
_FINGERPRINT_AT = (4, 7, 20)

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

    def found_in(self, text: str, *, min_length: int = 0) -> bool:
        """Whether ``text`` holds a known value outside a placeholder.

        With ``min_length``, only values at least that long count.
        """
        pattern = self._current()
        return pattern is not None and any(
            m["v"] is not None and len(m["v"]) >= min_length
            for m in pattern.finditer(text)
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
        self._billing_hash: tuple[str, str] | None = None
        self._hash_mark = ""
        self._exposed: frozenset[str] = frozenset()
        self._generic: list[str] = []
        self._words: dict[str, bool] = {}
        #: Where the last `mask` call found content without a rule of its
        #: own, which it masked generically (paths with indices dropped).
        self.last_generic: tuple[str, ...] = ()

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
        self._generic = []
        self._client_version = client_version
        self._billing_hash = None
        # Stands in for a kept billing line's hash until it is made again;
        # random, so no request can hold it.
        self._hash_mark = f"\x00{secrets.token_hex(16)}\x00"
        try:
            out = self._body(body)
            self._fingerprinted(body, out)
            # Named only now: by the end of the body, the vault knows every
            # value in it, however early a name holding one came.
            problems = [self._safe(*problem) for problem in self._problems]
            self.last_generic = tuple(
                dict.fromkeys(
                    _INDEX.sub("[]", self._safe(path, "")[0]) for path in self._generic
                )
            )
        finally:
            self._problems = []
            self._generic = []
        if problems:
            raise UnsupportedRequestError(*problems[0], more=problems[1:])
        self._vault_state = self._state()
        return out

    def _body(self, body: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        self._exposed = _exposed_names(body)
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
                out[key] = self._guard(self._safeguards, value)
            else:
                out.update(self._unknown_fields({key: value}, ""))
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
        kind = _block_type(block, path)
        if kind != "text":
            return self._unknown_block(block, path)
        known, extra = _split(block, _BLOCK_KEYS["text"])
        _no_citations(known, path)
        text = _str(known.get("text"), f"{path}.text")
        return self._merged(
            block, {**known, "text": self._billed(text, first=first)}, extra, path
        )

    def _billed(self, text: str, *, first: bool) -> str:
        """Mask system text, keeping a billing line's name and version.

        The version's hash is only marked here: `_fingerprinted` makes it
        again once the whole body is masked.
        """
        line = self._billing_line(text, first=first)
        if line is None or self._known.found_in(line[0]):
            return self._text(text)
        kept = line[0]
        if line["hash"] is not None:
            kept = kept[: line.start("hash") - line.start() - 1]
            self._billing_hash = (line["version"], line["hash"])
            kept += self._hash_mark
        before, after = text[: line.start()], text[line.end() :]
        return self._text(before) + kept + self._text(after)

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
            elif (
                first
                and line.start() == 0
                and line["hash"] is not None
                and _mask_text(self._shield, self._known, line[0]) == line[0]
            ):
                return line
        return None

    def _fingerprinted(self, body: dict[str, Any], out: dict[str, Any]) -> None:
        """Make a kept billing line's hash again, from the masked first prompt.

        Claude Code hashes a few characters of the first prompt into it. The
        prompt it was made from is found by making the hash again from each
        of the user's texts; the new hash comes from that text as masked. If
        none gives the same hash, the hash is left out.
        """
        if self._billing_hash is None:
            return
        version, old = self._billing_hash
        new = ""
        for original, masked in _user_texts(body, out):
            if _fingerprint(original, version) == old:
                new = "." + _fingerprint(masked, version)
                break
        system = out.get("system")
        if isinstance(system, str):
            out["system"] = system.replace(self._hash_mark, new, 1)
        elif isinstance(system, list):
            out["system"] = [
                {**block, "text": block["text"].replace(self._hash_mark, new, 1)}
                if isinstance(block, dict) and isinstance(block.get("text"), str)
                else block
                for block in system
            ]

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
        # A message has no type of its own: a "type" here is an unknown field.
        known, extra = _split(message, _MESSAGE_KEYS, keep_type=False)
        role = known.get("role")
        if not isinstance(role, str) or role not in _ROLES:
            # Checked on as a user message, so every other problem is named.
            self._problem(f"{path}.role", f"unknown role{_named(role)}")
            role = "user"
        elif "output_config" in known:
            self._guard(
                _effort_only, known["output_config"], f"{path}.output_config", role
            )
        content = known.get("content")
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
        return self._merged(message, {**known, "content": masked}, extra, path)

    def _block(self, block: Any, path: str, role: str) -> Any:
        kind = _block_type(block, path)
        if kind not in _BLOCK_KEYS:
            return self._unknown_block(block, path)
        known, extra = _split(block, _BLOCK_KEYS[kind])
        if kind in ("thinking", "redacted_thinking"):
            return self._signed(block, extra, path, role, kind)
        if kind == "text":
            _no_citations(known, path)
            text = _str(known.get("text"), f"{path}.text")
            masked = self._reply_text(text) if role == "assistant" else self._text(text)
            out = {**known, "text": masked}
        elif kind == "image":
            self._media_source(known.get("source"), f"{path}.source", _IMAGE_TYPES)
            out = known
        elif kind == "document":
            out = self._document(known, path)
        elif kind == "tool_use":
            out = self._tool_use(known, path, role=role)
        else:
            out = self._tool_result(known, path)
        return self._merged(block, out, extra, path)

    def _signed(
        self,
        block: dict[str, Any],
        extra: dict[str, Any],
        path: str,
        role: str,
        kind: str,
    ) -> Any:
        """A thinking block, signed by the API: sent exactly as it came, or not at all.

        Thinking about data seen before it was masked (a value registered
        later, a session begun without the gateway) is dropped, and so is one
        with a field this gateway has no rule for that holds anything
        masking would change: nothing in a signed block can be changed.
        """
        if role != "assistant":
            raise UnsupportedRequestError(
                f"{path}.type", f"only the model's own messages have {kind}"
            )
        if kind == "thinking":
            thinking = _str(block.get("thinking"), f"{path}.thinking")
            _str(block.get("signature"), f"{path}.signature")
            if self._known.found_in(thinking):
                return None
        else:
            _str(block.get("data"), f"{path}.data")
        if extra:
            self._generic.extend(f"{path}.{_key(name)}" for name in extra)
            if not self._unchanged(extra):
                return None
        return block

    def _tool_use(self, block: dict[str, Any], path: str, *, role: str) -> Any:
        tool_id = self._tool_id(block.get("id"), f"{path}.id")
        tool_input = block.get("input")
        if role == "assistant":
            masked = self._ledger.masked_tool_input(tool_id, tool_input)
            if masked is not None:
                # The API made this call, name and all.
                _str(block.get("name"), f"{path}.name")
                replayed = self._known_everywhere(masked, f"{path}.input")
                return {**block, "input": replayed}
        self._tool_name(block.get("name"), f"{path}.name")
        return {**block, "input": self._data(tool_input, f"{path}.input")}

    def _tool_result(self, block: dict[str, Any], path: str) -> Any:
        self._tool_id(block.get("tool_use_id"), f"{path}.tool_use_id")
        if block.get("is_error") not in (None, True, False):
            raise UnsupportedRequestError(f"{path}.is_error", "not true or false")
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
        kind = _block_type(item, path)
        if kind not in _NESTED_BLOCKS:
            return self._unknown_block(item, path)
        known, extra = _split(item, _BLOCK_KEYS[kind])
        if kind == "text":
            _no_citations(known, path)
            text = self._text(_str(known.get("text"), f"{path}.text"))
            out = {**known, "text": text}
        elif kind == "image":
            self._media_source(known.get("source"), f"{path}.source", _IMAGE_TYPES)
            out = known
        else:
            out = self._document(known, path)
        return self._merged(item, out, extra, path)

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
            self._media_source(source, source_path, _DOCUMENT_TYPES)
            return out
        if kind == "text":
            _no_extra_keys(source, {"type", "media_type", "data"}, source_path)
            if source.get("media_type") != "text/plain":
                raise UnsupportedRequestError(
                    f"{source_path}.media_type", "not text/plain"
                )
            data = _str(source.get("data"), f"{source_path}.data")
            out["source"] = {**source, "data": self._text(data)}
            return out
        raise UnsupportedRequestError(
            f"{source_path}.type", f"unknown document source{_named(kind)}"
        )

    def _media_source(self, source: Any, path: str, types: frozenset[str]) -> None:
        """Check an image or PDF source, which is passed on as it is.

        Its bytes can't be masked, so only the media Claude Code sends are
        let through, and a URL only if it holds nothing masking would change.
        """
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
        if kind == "base64":
            media_type = source.get("media_type")
            if not isinstance(media_type, str) or media_type not in types:
                raise UnsupportedRequestError(
                    f"{path}.media_type", "a kind of file that isn't sent"
                )
            _str(source.get("data"), f"{path}.data")
            return
        url = _str(source.get("url"), f"{path}.url")
        if not _WEB_URL.match(url) or _opaque(None, url) or not self._clean(url):
            raise UnsupportedRequestError(
                f"{path}.url", "a URL that may hold personal data"
            )

    # --- names that can't be masked ------------------------------------------

    def _tool_id(self, value: Any, path: str) -> str:
        """A tool call's id: never masked (it pairs a call with its result)."""
        tool_id = _str(value, path)
        if _TOOL_ID.fullmatch(tool_id) and self._id_ok(tool_id):
            return tool_id
        raise UnsupportedRequestError(path, "an id the API couldn't have made")

    def _id_ok(self, value: str) -> bool:
        """Whether an id may go out as it is, never masked.

        One masking would leave as it is may. So may one the API makes at
        random, if the only known values inside it are short ones, there by
        chance (a registered ``ada`` inside ``toolu_01ada9...``).
        """
        if self._clean(value):
            return True
        return (
            _API_ID.fullmatch(value) is not None
            and self._word_clean(value)
            and not self._known.found_in(value, min_length=_CHANCE)
        )

    def _tool_name(self, value: Any, path: str) -> None:
        """A tool's name: never masked (the client runs the tool by it)."""
        name = _str(value, path)
        if name in self._exposed:
            return
        # Tool names go out as they are in the request's tools, by design; one
        # used earlier (an MCP tool still connecting, say) is checked as a word.
        if _TOOL_NAME.fullmatch(name) and self._word_clean(name):
            return
        raise UnsupportedRequestError(path, "a tool name that may hold personal data")

    def _key_ok(self, name: str) -> bool:
        """Whether a key of content without a rule may go out as it is."""
        if name in WORDS:
            return self._word_clean(name)
        return _FIELD_NAME.fullmatch(name) is not None and self._clean(name)

    def _ident_ok(self, name: str) -> bool:
        """Whether a name that can't be masked may go out as it is.

        A protocol word may (unless it is a registered value itself); any
        other name only if masking would leave it as it is.
        """
        if name in WORDS:
            return self._word_clean(name)
        return self._clean(name)

    def _word_clean(self, word: str) -> bool:
        """No detector and no registered value finds ``word`` as a whole word.

        Short registered values inside it don't count, so an id or a protocol
        word isn't taken for data by chance.
        """
        found = self._words.get(word)
        if found is None:
            found = self._shield.mask(word).text == word
            self._words[word] = found
        return found

    def _clean(self, text: str) -> bool:
        """Whether masking leaves ``text`` as it is."""
        return self._text(text) == text

    # --- content without a rule of its own -----------------------------------

    def _merged(
        self, whole: dict[str, Any], masked: Any, extra: dict[str, Any], path: str
    ) -> Any:
        """Put a part's unknown fields, masked, back with its masked known ones."""
        if not extra or masked is None:
            return masked
        fields = self._unknown_fields(extra, path)
        merged = {**masked, **fields}
        return {key: merged[key] for key in whole if key in merged}

    def _unknown_fields(self, fields: Mapping[str, Any], prefix: str) -> dict[str, Any]:
        """Mask fields this gateway has no rule for; note any it can't."""
        out: dict[str, Any] = {}
        for name, value in fields.items():
            path = f"{prefix}.{_key(name)}" if prefix else _key(name)
            self._generic.append(path)
            if not self._key_ok(name):
                self._problem(path, "a field name that may hold personal data")
                continue
            try:
                out[name] = self._unknown(value, path, name)
            except UnsupportedRequestError as error:
                for where, problem in error.found:
                    self._problem(where, problem)
        return out

    def _unknown_block(self, block: dict[str, Any], path: str) -> Any:
        """A block of a type this gateway has no rule for, masked generically."""
        self._generic.append(f"{path}.type")
        return self._unknown(block, path)

    def _unknown(self, value: Any, path: str, key: str | None = None) -> Any:
        """Mask content without a rule of its own, or refuse it.

        Text is masked as any text is. What can't be masked goes out only if
        it holds nothing masking would change: a key, a type, an id, a
        number. Bytes (base64, anything with a media type) and opaque values
        (signatures, encrypted data) are refused: they can't be checked.
        """
        if isinstance(value, dict):
            if _MEDIA_KEYS & value.keys() or value.get("type") == "base64":
                raise UnsupportedRequestError(path, "file data that can't be masked")
            out: dict[str, Any] = {}
            for name, item in value.items():
                where = f"{path}.{_key(name)}"
                if not self._key_ok(name):
                    raise UnsupportedRequestError(
                        where, "a field name that may hold personal data"
                    )
                out[name] = self._unknown(item, where, name)
            return out
        if isinstance(value, list):
            return [
                self._unknown(item, f"{path}[{i}]", key) for i, item in enumerate(value)
            ]
        if isinstance(value, str):
            return self._unknown_text(value, path, key)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            if not self._number_clean(value, key):
                raise UnsupportedRequestError(
                    path, "a number that may hold personal data"
                )
            return value
        return value

    def _unknown_text(self, text: str, path: str, key: str | None) -> str:
        if _opaque(key, text):
            if self._sent_by_api(text):
                return text
            raise UnsupportedRequestError(path, "opaque data that can't be masked")
        if key == "type":
            if _TYPE_NAME.fullmatch(text) and self._ident_ok(text):
                return text
            raise UnsupportedRequestError(path, "a type that may hold personal data")
        if key is not None and _ID_KEY.fullmatch(key):
            return text if self._id_ok(text) else self._text(text)
        if key is not None and _NAME_KEY.fullmatch(key) and text in self._exposed:
            return text
        return self._text(text)

    def _number_clean(self, number: float, key: str | None) -> bool:
        """Whether a number holds nothing masking would change in its digits.

        A number can't become a placeholder without changing its type, so
        one that would be masked as text (a card number, a registered value)
        is refused. Settings (token counts, limits) are only numbers.
        """
        texts = [json.dumps(number)]
        if isinstance(number, float) and number.is_integer() and abs(number) < 1e21:
            texts.append(str(int(number)))
        if key is not None and _SETTING_KEY.fullmatch(key):
            # A setting may hold a registered number by chance (64000 when
            # 4000 is registered), but not be one, or a card.
            return all(self._word_clean(text) for text in texts)
        return all(self._clean(text) for text in texts)

    def _unchanged(self, value: Any, key: str | None = None) -> bool:
        """Whether ``value`` holds nothing masking would change; nothing is masked."""
        if isinstance(value, dict):
            return all(
                self._key_ok(name) and self._unchanged(item, name)
                for name, item in value.items()
            )
        if isinstance(value, list):
            return all(self._unchanged(item, key) for item in value)
        if isinstance(value, str):
            if _opaque(key, value):
                return self._sent_by_api(value)
            if key == "type":
                return _TYPE_NAME.fullmatch(value) is not None and self._ident_ok(value)
            return self._clean(value)
        if isinstance(value, bool) or value is None:
            return True
        if isinstance(value, (int, float)):
            return self._number_clean(value, key)
        return False

    def _sent_by_api(self, value: str) -> bool:
        """Whether an opaque value is one the API itself sent in a reply."""
        return False

    # --- data: tool inputs, the classifier's context ---------------------------

    def _safeguards(self, value: Any) -> Any:
        """Auto mode's context for its safety check: local paths, rules, state."""
        return self._data(value, "safeguards", opaque=False)

    def _data(
        self, value: Any, path: str, key: str | None = None, *, opaque: bool = True
    ) -> Any:
        """Mask a value that is data, such as a tool call's input.

        Every string is masked, keys too (unless they are protocol words or
        hold nothing to mask), and ``type`` values like any other string.
        Two keys that would mask to the same text are refused. Without
        ``opaque``, a ``data:`` URI or a value under a key such as
        ``signature`` is refused; other strings, however long, are masked as
        text (a long path is ordinary in the classifier's context).
        """
        if isinstance(value, str):
            if (
                not opaque
                and (_DATA_URI.match(value) or (key and _OPAQUE_KEY.fullmatch(key)))
                and not self._sent_by_api(value)
            ):
                raise UnsupportedRequestError(path, "opaque data that can't be masked")
            if key == "type" and self._ident_ok(value):
                return value
            if key is not None and _ID_KEY.fullmatch(key) and self._id_ok(value):
                return value
            return self._text(value)
        if isinstance(value, list):
            return [
                self._data(item, f"{path}[{i}]", key, opaque=opaque)
                for i, item in enumerate(value)
            ]
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for name, item in value.items():
                masked = name if self._ident_ok(name) else self._text(name)
                if masked in out:
                    raise UnsupportedRequestError(
                        f"{path}.<key>", "two keys that mask to the same text"
                    )
                out[masked] = self._data(
                    item, f"{path}.{_key(name)}", name, opaque=opaque
                )
            return out
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            if not self._number_clean(value, key):
                raise UnsupportedRequestError(
                    path, "a number that may hold personal data"
                )
            return value
        return value

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

    def _known_everywhere(self, value: Any, path: str) -> Any:
        """Mask any known value left in a replayed tool input, keys included."""
        if isinstance(value, str):
            return self._known.mask(value)
        if isinstance(value, list):
            return [self._known_everywhere(item, path) for item in value]
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                masked = key if self._ident_ok(key) else self._known.mask(key)
                if masked in out:
                    raise UnsupportedRequestError(
                        f"{path}.<key>", "two keys that mask to the same text"
                    )
                out[masked] = self._known_everywhere(item, path)
            return out
        return value

    def _strings(self, value: Any, path: str) -> list[str]:
        return [
            self._text(_str(item, f"{path}[{i}]"))
            for i, item in enumerate(_list(value, path))
        ]


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
# Ids the API makes at random: a short known value inside one is chance
# (under _CHANCE characters). Hex ids need a letter: digits alone may be an
# account number.
_API_ID = re.compile(
    r"(?:srv|mcp)?toolu_[A-Za-z0-9_]{8,128}|(?:msg|req|file|container)_[A-Za-z0-9]{8,128}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?=[0-9]*[a-f])[0-9a-f]{16,128}",
    re.ASCII,
)
_CHANCE = 5
# A URL a source may point to: on the web, not inline data.
_WEB_URL = re.compile(r"https?://", re.IGNORECASE)
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}", re.ASCII)
# Keys whose values are ids, names, settings, opaque data or file bytes.
_ID_KEY = re.compile(r"id|.+_id|.+_ids")
_NAME_KEY = re.compile(r"name|.+_name")
# Only names that say so: a generic one like "value" could hold anything.
_SETTING_KEY = re.compile(r"max_uses|target_tokens_saved|[a-z_]*_tokens")
_OPAQUE_KEY = re.compile(
    r"signature|.+_signature|encrypted(?:_.+)?|.+_encrypted|ciphertext"
)
_BYTES_KEY = re.compile(r"data|bytes|blob|.+_b64|.+_base64")
# Base64 uses one alphabet or the other, never both.
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}|[A-Za-z0-9_-]+={0,2}")
_DATA_URI = re.compile(r"\s*data:[^,]{0,100};base64,", re.IGNORECASE)
_MEDIA_KEYS = frozenset({"media_type", "mime_type", "mimeType"})
# The files Claude Code sends as they are: images, and PDFs.
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_DOCUMENT_TYPES = frozenset({"application/pdf"})

#: How deeply a request's JSON may nest. Claude Code's requests nest about ten
#: levels, and tool inputs a few more; deeper values are refused rather than
#: walked (a recursion that deep would fail).
MAX_DEPTH = 100


def _fingerprint(text: str, version: str) -> str:
    """Claude Code's short hash of a prompt, as its billing line carries it.

    Characters are counted as JavaScript does (UTF-16 code units).
    """
    units = text.encode("utf-16-le")
    picked = []
    for at in _FINGERPRINT_AT:
        if 2 * at + 2 <= len(units):
            unit = int.from_bytes(units[2 * at : 2 * at + 2], "little")
            picked.append("\ufffd" if 0xD800 <= unit <= 0xDFFF else chr(unit))
        else:
            picked.append("0")
    data = f"{_FINGERPRINT_SALT}{''.join(picked)}{version}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:3]


def _user_texts(body: dict[str, Any], out: dict[str, Any]) -> Any:
    """Each text of the user's messages, with the same text as masked."""
    before, after = body.get("messages"), out.get("messages")
    if not isinstance(before, list) or not isinstance(after, list):
        return
    for message, masked in zip(before, after, strict=False):
        if not (isinstance(message, dict) and isinstance(masked, dict)):
            continue
        if message.get("role") != "user":
            continue
        content, done = message.get("content"), masked.get("content")
        if isinstance(content, str) and isinstance(done, str):
            yield content, done
        elif isinstance(content, list) and isinstance(done, list):
            for block, masked_block in zip(content, done, strict=False):
                if (
                    isinstance(block, dict)
                    and isinstance(masked_block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                    and isinstance(masked_block.get("text"), str)
                ):
                    yield block["text"], masked_block["text"]


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


def _block_type(block: Any, path: str) -> str:
    """A block's type, which may be one without a rule of its own."""
    if not isinstance(block, dict):
        raise UnsupportedRequestError(path, "not an object")
    kind = block.get("type")
    if not isinstance(kind, str):
        raise UnsupportedRequestError(f"{path}.type", "not a string")
    return kind


def _split(
    whole: dict[str, Any],
    known: set[str] | frozenset[str],
    *,
    keep_type: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a part into the fields with a rule (and a block's type) and the rest."""
    extra = {
        key: value
        for key, value in whole.items()
        if key not in known and not (keep_type and key == "type")
    }
    if not extra:
        return whole, {}
    return {key: value for key, value in whole.items() if key not in extra}, extra


def _exposed_names(body: dict[str, Any]) -> frozenset[str]:
    """The tool names a request itself defines, which go out as they are.

    Those of its tools, and of tools added in the conversation (a system
    message's ``tool_addition`` with a definition).
    """
    tools = body.get("tools")
    found = list(tools) if isinstance(tools, list) else []
    messages = body.get("messages")
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            tool = block.get("tool") if isinstance(block, dict) else None
            if isinstance(tool, dict) and block.get("type") == "tool_addition":
                found.append(tool.get("definition"))
    return frozenset(
        tool["name"]
        for tool in found
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    )


def _opaque(key: str | None, text: str) -> bool:
    """Whether a string is opaque: a signature, encrypted data, or file bytes.

    Masking can't look into it, so it goes out only if the API sent it.
    """
    if key is not None and _OPAQUE_KEY.fullmatch(key):
        return True
    if _DATA_URI.match(text):
        return True
    compact = re.sub(r"[\r\n]+", "", text)
    if not _BASE64.fullmatch(compact):
        return False
    mixed = (
        any(c.isupper() for c in compact)
        and any(c.islower() for c in compact)
        and any(c.isdigit() for c in compact)
    )
    if len(compact) >= 64 and mixed:
        return True
    return (
        key is not None
        and _BYTES_KEY.fullmatch(key) is not None
        and len(compact) >= 16
        and (mixed or compact.endswith("="))
    )


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
    """Text blocks carry no citations; a document may say whether to make them."""
    citations = block.get("citations")
    if citations is None or citations == []:
        return
    if (
        block.get("type") == "document"
        and isinstance(citations, dict)
        and set(citations) <= {"enabled"}
        and isinstance(citations.get("enabled", False), bool)
    ):
        return
    raise UnsupportedRequestError(f"{path}.citations", "citations aren't supported")
