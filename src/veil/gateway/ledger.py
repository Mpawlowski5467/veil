"""What the model actually wrote, for replies that come back in a later request.

A gateway restores placeholders in each reply before the client sees it, and
the client sends that reply back as history in its next request. Masking the
restored text again would not always give back what the model wrote: a
placeholder glued to letters (``[PERSON_1]em`` restored as ``Jan Nowakem``)
isn't a whole-word match any more. The ledger remembers, for each restored
reply, the masked text the model produced, so the gateway can send exactly
that instead.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol, runtime_checkable


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def canonical(value: Any) -> str:
    """Serialize a JSON value the same way whatever its key order or spacing.

    Numbers compare as JavaScript sees them (the client parses and resends
    tool inputs in JavaScript): ``1.0`` and ``1`` are the same, and so are
    ``-0.0`` and ``0``.
    """
    return json.dumps(_as_javascript(value), sort_keys=True, separators=(",", ":"))


def seen_digest(value: Any) -> str:
    """The digest a ledger keeps for a block or value the API sent.

    A block is compared as the client sends it back: without the cache
    breakpoint or the caller it adds or drops.
    """
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k not in ("cache_control", "caller")}
    return _digest(canonical(value))


def _as_javascript(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            return value
        return 0.0 if number == 0 else number
    if isinstance(value, list):
        return [_as_javascript(item) for item in value]
    if isinstance(value, dict):
        return {key: _as_javascript(item) for key, item in value.items()}
    return value


@runtime_checkable
class Ledger(Protocol):
    """Remembers the masked form of each reply the gateway restored."""

    def record_text(self, restored: str, masked: str) -> None:
        """Remember that ``masked`` model text was restored as ``restored``."""
        ...

    def masked_text(self, restored: str) -> str | None:
        """Return the model's masked text for ``restored``, if recorded."""
        ...

    def record_tool_input(self, tool_use_id: str, restored: Any, masked: Any) -> None:
        """Remember the masked input of a tool call restored as ``restored``."""
        ...

    def masked_tool_input(self, tool_use_id: str, restored: Any) -> Any | None:
        """Return the masked input of a tool call, if it still matches.

        Returns None when the call wasn't recorded, or when the input the
        client sent back differs from what the gateway restored.
        """
        ...


class MemoryLedger:
    """A `Ledger` held in memory, for one gateway process.

    Texts are keyed by a hash of the restored text, so the ledger never holds
    the restored (real) text itself, only the masked text the model wrote.
    """

    def __init__(self) -> None:
        """Create an empty ledger."""
        self._texts: dict[str, str] = {}
        self._tools: dict[str, tuple[str, Any]] = {}
        self._seen: set[str] = set()

    def record_text(self, restored: str, masked: str) -> None:
        """Remember that ``masked`` model text was restored as ``restored``."""
        self._texts[_digest(restored)] = masked

    def masked_text(self, restored: str) -> str | None:
        """Return the model's masked text for ``restored``, if recorded."""
        return self._texts.get(_digest(restored))

    def record_tool_input(self, tool_use_id: str, restored: Any, masked: Any) -> None:
        """Remember the masked input of a tool call restored as ``restored``."""
        self._tools[tool_use_id] = (_digest(canonical(restored)), masked)

    def record_seen(self, value: Any) -> None:
        """Remember a block or an opaque value the API sent, by its digest."""
        self._seen.add(seen_digest(value))

    def was_seen(self, value: Any) -> bool:
        """Whether the API sent exactly this block or value (see `seen_digest`)."""
        return seen_digest(value) in self._seen

    def forget(self) -> None:
        """Forget every entry."""
        self._texts.clear()
        self._tools.clear()
        self._seen.clear()

    def masked_tool_input(self, tool_use_id: str, restored: Any) -> Any | None:
        """Return the masked input of a tool call, if it still matches."""
        entry = self._tools.get(tool_use_id)
        if entry is None or entry[0] != _digest(canonical(restored)):
            return None
        return entry[1]
