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
    """Serialize a JSON value the same way whatever its key order or spacing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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

    def record_text(self, restored: str, masked: str) -> None:
        """Remember that ``masked`` model text was restored as ``restored``."""
        self._texts[_digest(restored)] = masked

    def masked_text(self, restored: str) -> str | None:
        """Return the model's masked text for ``restored``, if recorded."""
        return self._texts.get(_digest(restored))

    def record_tool_input(self, tool_use_id: str, restored: Any, masked: Any) -> None:
        """Remember the masked input of a tool call restored as ``restored``."""
        self._tools[tool_use_id] = (_digest(canonical(restored)), masked)

    def masked_tool_input(self, tool_use_id: str, restored: Any) -> Any | None:
        """Return the masked input of a tool call, if it still matches."""
        entry = self._tools.get(tool_use_id)
        if entry is None or entry[0] != _digest(canonical(restored)):
            return None
        return entry[1]
