"""Masking a Messages API request before it leaves the machine.

Every field of the request body is handled by an explicit rule: masked (text
the model reads), passed through (settings, tool definitions, signed thinking,
image and PDF data), or refused. A field or block type without a rule is
refused, never forwarded, so a new kind of content can't slip through unmasked
after a client update.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from ..detectors._secrets import SECRET_TYPES, code_value, credential_type
from ..detectors.manual import ManualDetector
from ..placeholders import placeholder_type
from ..secret_review import Candidate, request_candidates
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
# A value of letters only, such as a dictionary-word password.
_LETTERS = re.compile(r"[^\W\d_]+")


class _KnownValues:
    r"""Masks every known value wherever it is, even glued to other text.

    Masking finds a registered value only as a whole word, so "Jan Nowakem"
    (a restored "[PERSON_1]em") would go out as it is. This pass runs after
    masking, over everything that leaves: any value in the vault, or any
    registered value, still present becomes its placeholder. A value is also
    found as it is spelled inside a JSON string (``Ada \"Q\" Quill``), the
    way a JSON reply gives it back restored.

    Two kinds of detected secret are not matched this way. A stored secret
    that reads as code (a type such as ``String``, or a credential word on
    its own such as ``password``) is not matched at all. A secret that is a
    single word of letters is matched only as a whole word, so words and
    identifiers that contain it stay readable: a stored ``postgres`` leaves
    ``postgresql://`` alone.
    """

    #: Shorter values would be masked inside too many ordinary words.
    MIN_LENGTH = 3

    def __init__(self, vault: Vault, registered: Mapping[str, str]) -> None:
        self._vault = vault
        self._short_registered = ManualDetector()
        for value, kind in registered.items():
            if len(value) < self.MIN_LENGTH:
                self._short_registered.add(value, kind)
        self._registered = {
            value: kind
            for value, kind in registered.items()
            if len(value) >= self.MIN_LENGTH
        }
        self._registered_pattern: re.Pattern[str] | None = None
        self._size = -1
        self._types: dict[str, str] = {}
        self._spelled: dict[str, str] = {}  # each spelling, to its value
        self._pattern: re.Pattern[str] | None = None
        # Known values folded (see `folded_in`), for the vault size and
        # shortest length they were made for.
        self._folded: tuple[tuple[int, int], re.Pattern[str] | None] | None = None

    def reset(self) -> None:
        self._size = -1

    def _current(self) -> re.Pattern[str] | None:
        size = len(self._vault)
        if size != self._size:
            types = dict(self._registered)
            for placeholder, value in self._vault.items():
                kind = placeholder_type(placeholder)
                if kind not in (None, "LITERAL") and len(value) >= self.MIN_LENGTH:
                    if kind in SECRET_TYPES and code_value(value):
                        continue
                    types.setdefault(value, kind)
            self._types = types
            self._spelled = {
                spelling: value for value in types for spelling in _spellings(value)
            }

            def alternative(spelling: str) -> str:
                value = self._spelled[spelling]
                if (
                    value not in self._registered
                    and types[value] in SECRET_TYPES
                    and _LETTERS.fullmatch(value)
                ):
                    return rf"(?<!\w){re.escape(spelling)}(?!\w)"
                return re.escape(spelling)

            values = sorted(self._spelled, key=len, reverse=True)
            self._pattern = (
                re.compile(
                    f"(?P<ph>{_EXACT})|(?P<v>{'|'.join(map(alternative, values))})"
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
            if match["v"] is None:
                return match.group(0)
            value = self._spelled[match["v"]]
            found = self._vault.get_placeholder(value)
            return found or self._vault.get_or_create(value, self._types[value])

        return pattern.sub(replace, text)

    def registered_in(self, text: str) -> bool:
        """Whether ``text`` holds a registered value.

        Registered values are fixed by the settings, unlike the vault, which
        learns values as a conversation goes on.
        """
        if self._registered_pattern is None and self._registered:
            spelled = {s for value in self._registered for s in _spellings(value)}
            values = sorted(spelled, key=len, reverse=True)
            self._registered_pattern = re.compile("|".join(map(re.escape, values)))
        pattern = self._registered_pattern
        return pattern is not None and pattern.search(text) is not None

    def unprotected(self, text: str) -> str:
        """Blank known values without moving offsets, for partial-secret review."""
        pattern = self._current()
        visible = (
            text
            if pattern is None
            else pattern.sub(
                lambda match: " " * len(match[0]) if match["v"] else match[0], text
            )
        )
        for span in reversed(self._short_registered.detect(text)):
            visible = (
                visible[: span.start] + " " * len(span.value) + visible[span.end :]
            )
        return visible

    def folded_in(self, text: str, *, min_length: int) -> bool:
        """Whether ``text`` holds a known value, however it is spelled.

        Values of ``min_length`` or more are compared without letter case,
        spaces, ``_`` or ``-``. So ``jan_nowak``, ``JanNowak`` and
        ``jan-nowak`` all hold "Jan Nowak": how a name that can't be masked
        (a setting's, a property's) would spell a value.
        """
        self._current()
        key = (self._size, min_length)
        if self._folded is None or self._folded[0] != key:
            values = {_fold(value) for value in self._types}
            long = sorted(
                (v for v in values if len(v) >= min_length), key=len, reverse=True
            )
            pattern = re.compile("|".join(map(re.escape, long))) if long else None
            self._folded = (key, pattern)
        pattern = self._folded[1]
        return pattern is not None and pattern.search(_fold(text)) is not None

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
        secret_review: Collect uncertain findings for the gateway's local
            review gate. Direct users must call `review_findings` before sending.
    """

    def __init__(
        self,
        shield: Shield,
        ledger: Ledger,
        *,
        note: str | None = DEFAULT_NOTE,
        registered: Mapping[str, str] | None = None,
        memo_limit: int = 64_000_000,
        secret_review: bool = False,
    ) -> None:
        """Create a masker for one conversation."""
        self._shield = shield
        self._ledger = ledger
        self._note = note
        self._memo_limit = memo_limit
        self._memo = _Memo(memo_limit)
        self._known = _KnownValues(shield.vault, registered or {})
        self._registered = dict(registered or {})
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
        self.secret_review = secret_review
        self._review_texts: set[str] = set()

    def confirm_secret(self, value: str, kind: str) -> None:
        """Remember an explicitly reviewed secret and invalidate earlier text."""
        if self._registered.get(value) == kind and self._shield.vault.get_placeholder(
            value
        ):
            return
        self._shield.add_entity(value, kind)
        self._shield.vault.get_or_create(value, kind)
        self._registered[value] = kind
        self._known = _KnownValues(self._shield.vault, self._registered)
        self._memo = _Memo(self._memo_limit)

    def mask(
        self, body: dict[str, Any], *, client_version: str | None = None
    ) -> dict[str, Any]:
        """Learn values across the request, then mask with that complete context.

        Repeat the adapter's own traversal, never a blind rewrite of protocol
        fields. Memoized detection keeps the second traversal inexpensive.
        """
        self._review_texts.clear()
        try:
            before = len(self._shield.vault)
            out = self._mask_once(body, client_version=client_version)
            if len(self._shield.vault) != before:
                self._review_texts.clear()
                out = self._mask_once(body, client_version=client_version)
            return out
        except Exception:
            self._review_texts.clear()
            raise

    def review_findings(self) -> tuple[Candidate, ...]:
        """Scan supported model text locally, after automatic masking finishes."""
        try:
            return request_candidates(
                [
                    (text, self._known.unprotected(text))
                    for text in sorted(self._review_texts)
                ]
            )
        finally:
            self._review_texts.clear()

    def _mask_once(
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
            if key in _SETTINGS:
                out[key] = self._guard(_SETTINGS[key], self, value, path)
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
        known = self._block_settings(known, path)
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
            if role == "assistant" and self._as_it_came(block):
                return self._block_settings(block, path)
            rule = _API_BLOCKS.get((role, kind))
            if rule is not None:
                return rule(self, block, path)
            return self._unknown_block(block, path)
        known, extra = _split(block, _BLOCK_KEYS[kind])
        known = self._block_settings(known, path)
        if kind in ("thinking", "redacted_thinking"):
            return self._signed({**block, **known}, extra, path, role, kind)
        if kind == "text":
            text = _str(known.get("text"), f"{path}.text")
            masked = self._reply_text(text) if role == "assistant" else self._text(text)
            out = {**known, "text": masked}
            citations = known.get("citations")
            if citations:
                rule = _CITATIONS if role == "assistant" else _USER_CITATIONS
                out["citations"] = rule(self, citations, f"{path}.citations")
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
            rule = _NESTED_API_BLOCKS.get(kind)
            if rule is not None:
                return rule(self, item, path)
            return self._unknown_block(item, path)
        known, extra = _split(item, _BLOCK_KEYS[kind])
        known = self._block_settings(known, path)
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
        if kind in ("base64", "url", "file"):
            self._media_source(source, source_path, _DOCUMENT_TYPES)
            return out
        if kind == "content":
            out["source"] = _CONTENT_SOURCE(self, source, source_path)
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
            "file": {"type", "file_id"},
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
        if kind == "file":
            self._tool_id(source.get("file_id"), f"{path}.file_id")
            return
        url = _str(source.get("url"), f"{path}.url")
        if not _WEB_URL.match(url) or _opaque(None, url) or not self._clean(url):
            raise UnsupportedRequestError(
                f"{path}.url", "a URL that may hold personal data"
            )

    def _block_settings(self, block: dict[str, Any], path: str) -> dict[str, Any]:
        """Check a block's settings: its cache breakpoint, and a call's caller."""
        out = block
        for key, rule in (("cache_control", _CACHE_CONTROL), ("caller", _CALLER)):
            if key in block:
                out = {**out, key: rule(self, block[key], f"{path}.{key}")}
        return out

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

    def _name_ok(self, name: str) -> bool:
        """Whether a name that can't be masked may go out as it is.

        It is read like an id the API makes: nothing a detector finds, no
        registered value as a whole word, no known value of five or more
        characters inside it, however it is spelled (``jan_nowak``, lower
        case), and no run of nine or more digits (a phone or card number
        glued to a letter; a date is eight). A shorter known value inside a
        longer word (``ted`` in ``selected_memories``) is there by chance.
        """
        if name in WORDS:
            return self._word_clean(name)
        return (
            self._shield.mask(name).text == name
            and not self._known.folded_in(name, min_length=_CHANCE)
            and _LONG_NUMBER.search(name) is None
        )

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
        if (
            key
            and credential_type(key)
            and any(
                self._shield.mask(text, field_name=key).text != text for text in texts
            )
        ):
            return False
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

    def _sent_by_api(self, value: Any) -> bool:
        """Whether the API itself sent this block or opaque value in a reply."""
        was_seen = getattr(self._ledger, "was_seen", None)
        return callable(was_seen) and bool(was_seen(value))

    def _unreadable(self, value: str) -> bool:
        """Whether no personal data can be read in an opaque value.

        The API's opaque values are base64 of bytes that aren't text. One is
        read like an id the API makes: a short known value may be in it by
        chance, a longer one or anything a detector finds may not. If it
        decodes to text, that text must hold nothing to mask and nothing
        long enough to be encoded data.
        """
        compact = re.sub(r"[\r\n]+", "", value)
        if not _BASE64.fullmatch(compact):
            return False
        # Not the cached word check: these can be large, and seen only once.
        if self._shield.mask(compact).text != compact or self._known.found_in(
            compact, min_length=_CHANCE
        ):
            return False
        decoded = _decoded_text(compact)
        return decoded is None or (
            self._clean(decoded) and _ENCODED_RUN.search(decoded) is None
        )

    def _as_it_came(self, block: dict[str, Any]) -> bool:
        """Whether a block goes back exactly as the API sent it.

        It does if the gateway saw the API send it so, and nothing readable
        in it holds a registered value. (Values the vault learned since are
        not looked for: bytes the API wrote tell it nothing new, and changing
        them would break what it binds to them, such as signed thinking.)
        """
        if not self._sent_by_api(block):
            return False
        return not any(self._known.registered_in(text) for text in _readable(block))

    # --- data: tool inputs, the classifier's context ---------------------------

    def _safeguards(self, value: Any) -> Any:
        """Auto mode's context for its safety check: local paths, rules, state."""
        return self._data(value, "safeguards", opaque="marked")

    def _data(
        self, value: Any, path: str, key: str | None = None, *, opaque: str = "mask"
    ) -> Any:
        """Mask a value that is data, such as a tool call's input.

        Every string is masked, keys too (unless they are protocol words or
        hold nothing to mask), and ``type`` values like any other string.
        Two keys that would mask to the same text are refused. ``opaque``
        says what becomes of opaque strings: ``"mask"`` them as text (a
        tool's input, the model's own writing); refuse ``"marked"`` ones, a
        ``data:`` URI or a value under a key such as ``signature`` (the
        classifier's context, where a long path is ordinary); or refuse
        ``"all"`` of them (what the user wrote, such as a schema's values).
        """
        if isinstance(value, str):
            if opaque == "all":
                refuse = _opaque(key, value)
            elif opaque == "marked":
                refuse = bool(
                    _DATA_URI.match(value) or (key and _OPAQUE_KEY.fullmatch(key))
                )
            else:
                refuse = False
            if refuse and not self._sent_by_api(value):
                raise UnsupportedRequestError(path, "opaque data that can't be masked")
            if key == "type" and self._ident_ok(value):
                return value
            if (
                key is not None
                and credential_type(key) is None
                and _ID_KEY.fullmatch(key)
                and self._id_ok(value)
            ):
                return value
            return self._text(value, field_name=key)
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
                    # Merging them would change the data; keeping one, lose some.
                    raise UnsupportedRequestError(
                        path, "two keys mask to the same text"
                    )
                # Paths name a key only as masked, so a refusal never quotes one.
                out[masked] = self._data(
                    item, f"{path}.{_key(masked)}", name, opaque=opaque
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

    # --- JSON schemas --------------------------------------------------------

    def _schema(self, schema: Any, path: str) -> Any:
        """Mask the text of a JSON schema, keeping what makes it a schema.

        Descriptions, titles and example values are masked; the model then
        writes a placeholder where the schema names a value, and the reply
        restores it. What can't be masked without breaking the schema
        (property names, ``required``, ``pattern``, ``$ref``) goes out only
        if it holds nothing masking would change.
        """
        if isinstance(schema, bool):
            return schema
        if not isinstance(schema, dict):
            raise UnsupportedRequestError(path, "not a schema")
        out: dict[str, Any] = {}
        for key, value in schema.items():
            where = f"{path}.{_key(key)}"
            if key in _SCHEMA_TEXT:
                out[key] = self._text(_str(value, where))
            elif key in _SCHEMA_VALUES:
                out[key] = self._data(value, where, opaque="all")
            elif key in _SCHEMA_NAMED:
                if not isinstance(value, dict):
                    raise UnsupportedRequestError(where, "not an object")
                for name in value:
                    self._schema_name(
                        name, f"{where}.<key>", key == "patternProperties"
                    )
                out[key] = {
                    name: self._schema(item, f"{where}.{_key(name)}")
                    for name, item in value.items()
                }
            elif key == "required":
                for i, name in enumerate(_list(value, where)):
                    self._schema_name(_str(name, f"{where}[{i}]"), f"{where}[{i}]")
                out[key] = value
            elif key == "pattern":
                self._schema_name(_str(value, where), where, pattern=True)
                out[key] = value
            elif key == "type":
                kinds = value if isinstance(value, list) else [value]
                if not kinds or not all(
                    isinstance(kind, str) and kind in _JSON_TYPES for kind in kinds
                ):
                    raise UnsupportedRequestError(where, "not a JSON type")
                out[key] = value
            elif key in _SCHEMA_REFERENCES:
                self._schema_name(_str(value, where), where)
                out[key] = value
            elif key in _SCHEMA_NUMBERS:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise UnsupportedRequestError(where, "not a number")
                out[key] = value
            elif key in _SCHEMA_FLAGS:
                if not isinstance(value, bool):
                    raise UnsupportedRequestError(where, "not true or false")
                out[key] = value
            elif key in _SUBSCHEMAS:
                out[key] = (
                    [
                        self._schema(item, f"{where}[{i}]")
                        for i, item in enumerate(value)
                    ]
                    if isinstance(value, list)
                    else self._schema(value, where)
                )
            else:
                self._generic.append(where)
                if not self._key_ok(key):
                    raise UnsupportedRequestError(
                        where, "a field name that may hold personal data"
                    )
                out[key] = self._data(value, where, opaque="all")
        return out

    def _schema_name(self, name: str, path: str, pattern: bool = False) -> None:
        """Check a name in a schema, which can't be masked without breaking it."""
        if pattern:
            # Also as the texts the pattern matches: jan\.n@example\.com,
            # ^Jan[ ]Nowak$. Counts like {1,4096} are limits, not data.
            texts = {_QUANTIFIER.sub("", name), *_readings(name)}
            ok = all(self._name_ok(text) for text in texts)
        else:
            # A reference is a URI: jane.doe%40example.com is an address.
            ok = self._name_ok(name) and (
                "%" not in name or self._name_ok(urllib.parse.unquote(name))
            )
        if not ok:
            raise UnsupportedRequestError(path, "a name that may hold personal data")

    # --- masking text --------------------------------------------------------

    def _reply_text(self, text: str) -> str:
        """Mask assistant text: the model's own words if the gateway has them."""
        masked = self._ledger.masked_text(text)
        return self._known.mask(masked) if masked is not None else self._text(text)

    def _text(self, text: str, *, field_name: str | None = None) -> str:
        if not text:
            return text
        # Always encode both parts: a literal JSON-looking prompt must not
        # collide with the cache entry for a parsed credential field.
        context = json.dumps([field_name, text])
        key = hashlib.sha256(context.encode("utf-8", "surrogatepass")).hexdigest()
        cached = self._memo.get(key)
        if cached is not None:
            masked = self._known.mask(cached)
        else:
            masked = _mask_text(self._shield, self._known, text, field_name=field_name)
            self._memo.put(key, masked)
        if self.secret_review:
            self._review_texts.add(text)
        return masked

    def _known_everywhere(self, value: Any, path: str) -> Any:
        """Mask any known value left in a replayed tool input, keys included."""
        if isinstance(value, str):
            return self._known.mask(value)
        if isinstance(value, list):
            return [
                self._known_everywhere(item, f"{path}[{i}]")
                for i, item in enumerate(value)
            ]
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                masked = key if self._ident_ok(key) else self._known.mask(key)
                if masked in out:
                    raise UnsupportedRequestError(
                        path, "two keys mask to the same text"
                    )
                out[masked] = self._known_everywhere(item, f"{path}.{_key(masked)}")
            return out
        return value

    def _strings(self, value: Any, path: str) -> list[str]:
        return [
            self._text(_str(item, f"{path}[{i}]"))
            for i, item in enumerate(_list(value, path))
        ]


# --- settings: the request's fields with a shape of their own ---------------------

_Rule = Callable[["RequestMasker", Any, str], Any]


def _enum(masker: RequestMasker, value: Any, path: str) -> Any:
    """A name from a set the API may extend: an identifier holding no data."""
    if (
        isinstance(value, str)
        and _TYPE_NAME.fullmatch(value)
        and masker._name_ok(value)
    ):
        return value
    raise UnsupportedRequestError(path, "not one of the setting's names")


def _integer(masker: RequestMasker, value: Any, path: str) -> Any:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedRequestError(path, "not a whole number")
    return value


def _number(masker: RequestMasker, value: Any, path: str) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnsupportedRequestError(path, "not a number")
    return value


def _boolean(masker: RequestMasker, value: Any, path: str) -> Any:
    if not isinstance(value, bool):
        raise UnsupportedRequestError(path, "not true or false")
    return value


def _masked_text(masker: RequestMasker, value: Any, path: str) -> Any:
    return masker._text(_str(value, path))


def _optional(rule: _Rule) -> _Rule:
    def check(masker: RequestMasker, value: Any, path: str) -> Any:
        return None if value is None else rule(masker, value, path)

    return check


def _list_of(rule: _Rule) -> _Rule:
    def check(masker: RequestMasker, value: Any, path: str) -> Any:
        return [
            rule(masker, item, f"{path}[{i}]")
            for i, item in enumerate(_list(value, path))
        ]

    return check


def _fields(**rules: _Rule) -> _Rule:
    """An object whose fields follow ``rules``; others are masked generically."""

    def check(masker: RequestMasker, value: Any, path: str) -> Any:
        if not isinstance(value, dict):
            raise UnsupportedRequestError(path, "not an object")
        known, extra = _split(value, frozenset(rules), keep_type=False)
        out = {
            key: rules[key](masker, item, f"{path}.{key}")
            for key, item in known.items()
        }
        return masker._merged(value, out, extra, path)

    return check


def _model(masker: RequestMasker, value: Any, path: str) -> Any:
    model = _str(value, path)
    if _MODEL.fullmatch(model) and masker._word_clean(model):
        return model
    raise UnsupportedRequestError(path, "not a model's name")


def _tool_reference(masker: RequestMasker, value: Any, path: str) -> Any:
    """A tool's name referred to: it can't be masked, so it must hold no data.

    One the request defines goes as it is, like its definition; any other
    must be a tool name that masking would leave as it is.
    """
    name = _str(value, path)
    if name in masker._exposed or (
        _TOOL_REFERENCE_NAME.fullmatch(name) and masker._name_ok(name)
    ):
        return name
    raise UnsupportedRequestError(path, "a tool name that may hold personal data")


def _user_id(masker: RequestMasker, value: Any, path: str) -> Any:
    """Claude Code's user id: JSON of random ids, kept as they are, and the rest.

    A short registered value can be in a random id by chance, so the ids are
    read as ids (see `RequestMasker._id_ok`), not masked as text; the text
    around them is masked. The bytes stay Claude Code's.
    """
    text = _str(value, path)
    parts, at = [], 0
    for match in _USER_ID_PART.finditer(text):
        if masker._id_ok(match[1]):
            parts += [masker._text(text[at : match.start(1)]), match[1]]
            at = match.end(1)
    parts.append(masker._text(text[at:]))
    return "".join(parts)


def _ttl(masker: RequestMasker, value: Any, path: str) -> Any:
    if isinstance(value, str) and _TTL.fullmatch(value):
        return value
    raise UnsupportedRequestError(path, "not a time to live")


def _amount(masker: RequestMasker, value: Any, path: str) -> Any:
    """How much a context edit keeps or clears: ``"all"`` or a counted amount."""
    if isinstance(value, str):
        return _enum(masker, value, path)
    return _fields(type=_enum, value=_integer)(masker, value, path)


def _tool_inputs_cleared(masker: RequestMasker, value: Any, path: str) -> Any:
    if isinstance(value, bool):
        return value
    return _list_of(_tool_reference)(masker, value, path)


def _schema_rule(masker: RequestMasker, value: Any, path: str) -> Any:
    return masker._schema(value, path)


def _examples(masker: RequestMasker, value: Any, path: str) -> Any:
    return masker._data(value, path)


_CACHE_CONTROL = _fields(type=_enum, ttl=_ttl, scope=_enum, evict_on_complete=_boolean)


def _call_id(masker: RequestMasker, value: Any, path: str) -> Any:
    return masker._tool_id(value, path)


_CALLER = _fields(type=_enum, tool_id=_call_id)

# The name of the tool Claude Code makes from `claude -p --json-schema`: its
# schema is the user's own, so its text is masked.
STRUCTURED_OUTPUT = "StructuredOutput"


def _tool(masker: RequestMasker, value: Any, path: str) -> Any:
    """A tool the model may call.

    Its name, description and schema go out as they are (they describe the
    tool, not the user), except the schema of StructuredOutput, which the
    user wrote.
    """
    if not isinstance(value, dict):
        raise UnsupportedRequestError(path, "not an object")
    rules: dict[str, _Rule] = {
        "name": _string,
        "description": _optional(_string),
        "input_schema": _as_is,
        "type": _enum,
        "cache_control": _CACHE_CONTROL,
        "strict": _boolean,
        "defer_loading": _boolean,
        "eager_input_streaming": _optional(_boolean),
        "input_examples": _examples,
        "max_uses": _integer,
        "max_content_tokens": _integer,
        "max_tokens": _integer,
        "display_width_px": _integer,
        "display_height_px": _integer,
        "display_number": _optional(_integer),
        "allowed_domains": _list_of(_masked_text),
        "blocked_domains": _list_of(_masked_text),
        "user_location": _fields(
            type=_enum,
            city=_optional(_masked_text),
            region=_optional(_masked_text),
            country=_optional(_masked_text),
            timezone=_optional(_masked_text),
        ),
        "citations": _fields(enabled=_boolean),
        "allowed_callers": _list_of(_enum),
        "model": _model,
    }
    if value.get("name") == STRUCTURED_OUTPUT:
        rules["input_schema"] = _schema_rule
    return _fields(**rules)(masker, value, path)


def _string(masker: RequestMasker, value: Any, path: str) -> Any:
    return _str(value, path)


def _as_is(masker: RequestMasker, value: Any, path: str) -> Any:
    return value


_EDIT = _fields(
    type=_enum,
    keep=_amount,
    trigger=_amount,
    clear_at_least=_amount,
    exclude_tools=_list_of(_tool_reference),
    clear_tool_inputs=_tool_inputs_cleared,
    pause_after_compaction=_boolean,
    instructions=_masked_text,
)

#: The request's settings, by field; anything they hold without a rule is
#: masked generically.
_SETTINGS: dict[str, _Rule] = {
    "model": _model,
    "max_tokens": _integer,
    "top_k": _integer,
    "temperature": _number,
    "top_p": _number,
    "stream": _boolean,
    "service_tier": _enum,
    # Fast mode (beta fast-mode-2026-02-01): "fast", or "standard".
    "speed": _enum,
    "metadata": _fields(user_id=_user_id),
    "thinking": _fields(type=_enum, budget_tokens=_integer, display=_enum),
    "context_management": _fields(edits=_list_of(_EDIT)),
    "output_config": _fields(
        effort=_enum,
        format=_fields(type=_enum, schema=_schema_rule),
        task_budget=_fields(type=_enum, total=_integer, remaining=_optional(_integer)),
        timing=_fields(type=_enum, now=_masked_text),
    ),
    "tool_choice": _fields(
        type=_enum, name=_tool_reference, disable_parallel_tool_use=_boolean
    ),
    "tools": _list_of(_tool),
    "cache_control": _CACHE_CONTROL,
}

# The request's top-level fields that are settings.
_TOP_LEVEL_PASS = frozenset(_SETTINGS)


# --- blocks the API makes, and the client sends back ------------------------------
#
# A block the gateway saw the API send goes back exactly as it came (see
# `RequestMasker._as_it_came`). These rules are for the rest: a block it
# didn't see (a conversation begun without it, a ledger since cleared), or one
# holding a registered value. Their opaque parts (encrypted results, a
# signature) go on as they are only in the model's own messages, where the API
# put them.


def _kept_opaque(masker: RequestMasker, value: Any, path: str) -> Any:
    """An opaque value the API wrote into the model's own message.

    It goes back as it is if the gateway saw the API send it. One it didn't
    see (a conversation from before an update) is kept only if nothing in it
    can be read (see `RequestMasker._unreadable`), since it can't be masked.
    """
    text = _str(value, path)
    if masker._sent_by_api(text) or masker._unreadable(text):
        return text
    raise UnsupportedRequestError(path, "opaque data that can't be masked")


def _call_ids(masker: RequestMasker, value: Any, path: str) -> Any:
    return masker._tool_id(value, path)


def _exact(**rules: _Rule) -> _Rule:
    """An object with exactly these fields: a reference, which can't be masked."""

    def check(masker: RequestMasker, value: Any, path: str) -> Any:
        if not isinstance(value, dict):
            raise UnsupportedRequestError(path, "not an object")
        _no_extra_keys(value, set(rules), path)
        return {
            key: rules[key](masker, item, f"{path}.{key}")
            for key, item in value.items()
        }

    return check


def _by_type(rules: Mapping[str, _Rule], default: _Rule) -> _Rule:
    """An object whose rule depends on its type."""

    def check(masker: RequestMasker, value: Any, path: str) -> Any:
        kind = value.get("type") if isinstance(value, dict) else None
        rule = rules.get(kind) if isinstance(kind, str) else None
        return (rule or default)(masker, value, path)

    return check


def _server_call(masker: RequestMasker, value: Any, path: str) -> Any:
    """A call the API ran itself (web search, an MCP tool on its side).

    Its input goes back as the API ran it if the gateway saw it, else it is
    masked like any tool input.
    """
    out = _fields(
        type=_enum,
        id=_call_ids,
        name=_tool_reference,
        server_name=_tool_reference,
        input=_as_is,
        caller=_CALLER,
        cache_control=_CACHE_CONTROL,
    )(masker, value, path)
    if "input" in out:
        tool_id = out.get("id")
        tool_input = value["input"]
        replayed = (
            masker._ledger.masked_tool_input(tool_id, tool_input)
            if isinstance(tool_id, str)
            else None
        )
        out["input"] = (
            masker._known_everywhere(replayed, f"{path}.input")
            if replayed is not None
            else masker._data(tool_input, f"{path}.input")
        )
    return out


_ERROR = _fields(type=_enum, error_code=_enum, error_message=_optional(_masked_text))


def _media(masker: RequestMasker, value: Any, path: str) -> Any:
    if value in ("text/plain", "application/pdf"):
        return value
    raise UnsupportedRequestError(path, "a kind of file that isn't sent")


_FETCHED_SOURCE = _by_type(
    {
        "text": _fields(type=_enum, media_type=_media, data=_masked_text),
        "base64": _fields(type=_enum, media_type=_media, data=_kept_opaque),
    },
    _fields(type=_enum),
)
_CITATION_CONFIG = _fields(enabled=_boolean)
_WEB_SEARCH_TOOL_RESULT = _fields(
    type=_enum,
    tool_use_id=_call_ids,
    content=lambda m, v, p: (
        _list_of(
            _fields(
                type=_enum,
                url=_masked_text,
                title=_optional(_masked_text),
                encrypted_content=_kept_opaque,
                page_age=_optional(_masked_text),
            )
        )(m, v, p)
        if isinstance(v, list)
        else _ERROR(m, v, p)
    ),
    caller=_CALLER,
    cache_control=_CACHE_CONTROL,
)
_WEB_FETCH_TOOL_RESULT = _fields(
    type=_enum,
    tool_use_id=_call_ids,
    content=_by_type(
        {
            "web_fetch_result": _fields(
                type=_enum,
                url=_masked_text,
                retrieved_at=_optional(_masked_text),
                content=_fields(
                    type=_enum,
                    title=_optional(_masked_text),
                    context=_optional(_masked_text),
                    source=_FETCHED_SOURCE,
                    citations=_optional(_CITATION_CONFIG),
                ),
            )
        },
        _ERROR,
    ),
    caller=_CALLER,
    cache_control=_CACHE_CONTROL,
)


def _result_content(masker: RequestMasker, value: Any, path: str) -> Any:
    """A tool result's content: text, or blocks as in a tool_result."""
    if isinstance(value, str):
        return masker._text(value)
    return [
        masker._nested(item, f"{path}[{i}]")
        for i, item in enumerate(_list(value, path))
    ]


_MCP_TOOL_RESULT = _fields(
    type=_enum,
    tool_use_id=_call_ids,
    is_error=_optional(_boolean),
    content=_result_content,
    cache_control=_CACHE_CONTROL,
)
_ADVISOR_TOOL_RESULT = _fields(
    type=_enum,
    tool_use_id=_call_ids,
    content=_by_type(
        {
            "advisor_result": _fields(
                type=_enum, text=_masked_text, stop_reason=_optional(_enum)
            ),
            "advisor_redacted_result": _fields(
                type=_enum, encrypted_content=_kept_opaque, stop_reason=_optional(_enum)
            ),
        },
        _ERROR,
    ),
    caller=_CALLER,
    cache_control=_CACHE_CONTROL,
)
# A tool named by reference: ToolSearch's results, and a tool added or
# removed mid-conversation. The name must be exact, so it isn't masked.
_TOOL_REFERENCE = _exact(
    type=_enum, tool_name=_tool_reference, cache_control=_CACHE_CONTROL
)
_TOOL_SEARCH_TOOL_RESULT = _fields(
    type=_enum,
    tool_use_id=_call_ids,
    content=_by_type(
        {
            "tool_search_tool_search_result": _fields(
                type=_enum, tool_references=_list_of(_TOOL_REFERENCE)
            )
        },
        _ERROR,
    ),
    cache_control=_CACHE_CONTROL,
)
_TOOL_CHANGE = _exact(
    type=_enum,
    tool=_by_type(
        {
            "tool_reference": _exact(type=_enum, name=_tool_reference),
            "tool_definition": _exact(type=_enum, definition=_tool),
        },
        lambda m, v, p: _exact(type=_enum)(m, v, p),
    ),
    cache_control=_CACHE_CONTROL,
)


def _compacted(masker: RequestMasker, value: Any, path: str) -> Any:
    """A compaction's summary: the model's text, sent back as it wrote it.

    The API may have signed it, so it can't be masked again; a summary that
    holds a registered value is refused.
    """
    if value is None:
        return None
    text = _str(value, path)
    masked = masker._ledger.masked_text(text)
    if masked is None:
        return masker._text(text)
    if masker._known.registered_in(masked):
        raise UnsupportedRequestError(
            path, "a summary holding a registered value, which can't be masked"
        )
    return masked


_COMPACTION = _fields(
    type=_enum,
    content=_compacted,
    encrypted_content=_optional(_kept_opaque),
    signature=_optional(_kept_opaque),
    tool_changes=_optional(_list_of(_TOOL_CHANGE)),
    cache_control=_CACHE_CONTROL,
)
_MODEL_REFERENCE = _fields(model=_model)
_FALLBACK = _fields(
    **{
        "type": _enum,
        "from": _MODEL_REFERENCE,
        "to": _MODEL_REFERENCE,
        "trigger": _fields(type=_enum, category=_optional(_enum)),
        "cache_control": _CACHE_CONTROL,
    }
)


def _text_block(masker: RequestMasker, value: Any, path: str) -> Any:
    return masker._nested(value, path)


_SEARCH_RESULT = _fields(
    type=_enum,
    source=_masked_text,
    title=_optional(_masked_text),
    content=_list_of(_text_block),
    citations=_optional(_CITATION_CONFIG),
    cache_control=_CACHE_CONTROL,
)
_CONTAINER_UPLOAD = _fields(type=_enum, file_id=_call_ids, cache_control=_CACHE_CONTROL)
_CONTENT_SOURCE = _fields(type=_enum, content=_result_content)


def _refused_opaque(masker: RequestMasker, value: Any, path: str) -> Any:
    raise UnsupportedRequestError(path, "opaque data that can't be masked")


def _citation(index_rule: _Rule) -> _Rule:
    """Where a reply's text came from; ``index_rule`` for its encrypted index."""
    return _fields(
        type=_enum,
        cited_text=_masked_text,
        document_index=_integer,
        document_title=_optional(_masked_text),
        start_char_index=_integer,
        end_char_index=_integer,
        start_page_number=_integer,
        end_page_number=_integer,
        start_block_index=_integer,
        end_block_index=_integer,
        search_result_index=_integer,
        url=_masked_text,
        title=_optional(_masked_text),
        source=_masked_text,
        encrypted_index=index_rule,
        file_id=_optional(_call_ids),
    )


_CITATIONS = _list_of(_citation(_kept_opaque))
_USER_CITATIONS = _list_of(_citation(_refused_opaque))

#: Rules for block types the API defines, by the role that may send them.
_API_BLOCKS: dict[tuple[str, str], _Rule] = {
    ("assistant", "server_tool_use"): _server_call,
    ("assistant", "mcp_tool_use"): _server_call,
    ("assistant", "web_search_tool_result"): _WEB_SEARCH_TOOL_RESULT,
    ("assistant", "web_fetch_tool_result"): _WEB_FETCH_TOOL_RESULT,
    ("assistant", "mcp_tool_result"): _MCP_TOOL_RESULT,
    ("assistant", "advisor_tool_result"): _ADVISOR_TOOL_RESULT,
    ("assistant", "tool_search_tool_result"): _TOOL_SEARCH_TOOL_RESULT,
    ("assistant", "compaction"): _COMPACTION,
    ("assistant", "fallback"): _FALLBACK,
    ("user", "search_result"): _SEARCH_RESULT,
    ("user", "container_upload"): _CONTAINER_UPLOAD,
    ("system", "tool_addition"): _TOOL_CHANGE,
    ("system", "tool_removal"): _TOOL_CHANGE,
}
#: Rules for block types the API defines inside a tool result's content.
_NESTED_API_BLOCKS: dict[str, _Rule] = {
    "tool_reference": _TOOL_REFERENCE,
    "search_result": _SEARCH_RESULT,
}


def _readable(value: Any, key: str | None = None) -> Any:
    """Every string of a value a model would read: keys and text, not opaque data."""
    if isinstance(value, str):
        if not _opaque(key, value):
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from _readable(item, key)
    elif isinstance(value, dict):
        for name, item in value.items():
            yield name
            yield from _readable(item, name)


_SCHEMA_TEXT = frozenset({"description", "title", "$comment"})
_SCHEMA_VALUES = frozenset({"enum", "const", "default", "examples"})
_SCHEMA_NAMED = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
_SCHEMA_REFERENCES = frozenset(
    {
        "format",
        "$ref",
        "$id",
        "$schema",
        "$anchor",
        "$dynamicRef",
        "$dynamicAnchor",
        "contentEncoding",
        "contentMediaType",
    }
)
_SCHEMA_NUMBERS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "minContains",
        "maxContains",
    }
)
_SCHEMA_FLAGS = frozenset(
    {"uniqueItems", "readOnly", "writeOnly", "deprecated", "nullable"}
)
_SUBSCHEMAS = frozenset(
    {
        "items",
        "prefixItems",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "additionalItems",
        "contains",
        "propertyNames",
        "contentSchema",
    }
)
# The parts of a regular expression that spell no text of their own.
_REGEX_CLASS = re.compile(r"\[(\^?)((?:\\.|[^\]\\])*)\]")
_REGEX_GROUP = re.compile(
    r"\((?:\?(?:[:=!>|]|<[=!]|P?<[A-Za-z_][A-Za-z0-9_]*>|[a-zA-Z-]+[:)]))?"
    r"|(?<!\\)[)^$]"
)
_REGEX_REPEAT = re.compile(r"(?<!\\)(?:[?*+]|\{[0-9]+(?:,[0-9]*)?\})[?+]?")
# A wildcard repeated (.+, .*, .{1,64}) stands for no one text.
_REGEX_ANY = re.compile(r"(?<!\\)\.(?:[?*+]|\{[0-9]+(?:,[0-9]*)?\})[?+]?")
_JSON_TYPES = frozenset(
    {"string", "number", "integer", "boolean", "object", "array", "null"}
)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}", re.ASCII)
_TTL = re.compile(r"[0-9]{1,5}[smh]", re.ASCII)
_QUANTIFIER = re.compile(r"\{[0-9]+(?:,[0-9]*)?\}")


def _unregex(pattern: str) -> str:
    r"""The text a regular expression spells out, roughly: its escapes undone.

    ``jan\.n@example\.com`` gives ``jan.n@example.com``; a class such as
    ``\s`` or ``\d`` becomes a space.
    """
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), pattern)
    text = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), text)
    text = re.sub(r"\\[bBAzZG]", "", text)
    text = re.sub(r"\\[sSdDwW](?:[*+?]|\{[0-9]+(?:,[0-9]*)?\})?", " ", text)
    return re.sub(r"\\(.)", r"\1", text)


def _class_text(match: re.Match[str]) -> str:
    """A character class as the character it matches first: ``[.]`` as ``.``.

    One with a range or negated (``[a-z]``, ``[^@]``) stands for no one
    character, so it reads as a space.
    """
    negated, members = match[1], match[2]
    if negated or not members or re.search(r"[^\\]-.", members):
        return " "
    return _unregex(members[:2]) if members[0] == "\\" else members[0]


def _readings(pattern: str) -> set[str]:
    r"""The texts a regular expression spells, read a few ways.

    Classes are read as their first character, and as a space; a group's
    parentheses and the quantifiers are dropped, ``|`` reads as a space,
    ``.+`` as a space and ``.`` as itself or a space. So ``^Jan[ ]Nowak$``,
    ``^(Jan)\s(Nowak)$`` and ``^jane[.]doe@example\.(com|org)$`` read as
    what they match.
    """
    texts: set[str] = set()
    pattern = _REGEX_ANY.sub(" ", pattern)
    for text in (
        _REGEX_CLASS.sub(_class_text, pattern),
        _REGEX_CLASS.sub(" ", pattern),
    ):
        text = _REGEX_GROUP.sub("", text)
        text = re.sub(r"(?<!\\)\|", " ", text)
        text = _REGEX_REPEAT.sub("", text)
        texts.update(_unregex(re.sub(r"(?<!\\)\.", dot, text)) for dot in (".", " "))
    return texts


def _fold(text: str) -> str:
    """``text`` in lower case, without spaces, ``_`` or ``-``."""
    return re.sub(r"[\s_-]+", "", text.lower())


def _spellings(value: str) -> set[str]:
    """A value as written, and as written inside a JSON string."""
    return {
        value,
        json.dumps(value, ensure_ascii=False)[1:-1],
        json.dumps(value)[1:-1],
    }


def _mask_text(
    shield: Shield, known: _KnownValues, text: str, *, field_name: str | None = None
) -> str:
    """Mask ``text``, then any known value left in it, even glued to a word.

    A line that still holds a known value after that is withheld.
    """
    result = (
        shield.mask(text)
        if field_name is None
        else shield.mask(text, field_name=field_name)
    )
    masked = known.mask(result.text)
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
# Digits enough for a phone or card number; a date (20251015) has eight.
_LONG_NUMBER = re.compile(r"[0-9]{9,}")
# A run of characters long enough to be encoded data (base64, hex, a URI's).
_ENCODED_RUN = re.compile(r"[A-Za-z0-9+/_=%-]{16,}")
# A URL a source may point to: on the web, not inline data.
_WEB_URL = re.compile(r"https?://", re.IGNORECASE)
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}", re.ASCII)
_TOOL_REFERENCE_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
# The ids in Claude Code's metadata.user_id, a JSON object.
_USER_ID_PART = re.compile(
    r'"(?:device_id|account_uuid|session_id|parent_session_id)"\s*:\s*"([^"\\]*)"'
)
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


def _decoded_text(value: str) -> str | None:
    """The text that base64 holds, or None if it holds bytes that aren't text.

    Encrypted bytes are almost never UTF-8, so text found this way was put
    there as text.
    """
    padded = value.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        return base64.b64decode(padded, validate=True).decode("utf-8")
    except ValueError:  # binascii.Error and UnicodeDecodeError both are
        return None


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
