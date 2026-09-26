"""The `Vault` protocol: storage for value <-> placeholder mappings."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Protocol, cast, runtime_checkable


@runtime_checkable
class Vault(Protocol):
    """Stores the two-way mapping between original values and placeholders.

    A vault owns placeholder numbering, so a persistent implementation (such
    as `SQLiteVault`) keeps numbering stable across processes. Values are
    keyed by their exact string: the same string always maps to the same
    placeholder, and the first entity type it was stored with wins.
    """

    def get_or_create(self, value: str, entity_type: str) -> str:
        """Return the placeholder for ``value``, creating one if it is new.

        Args:
            value: The original text, e.g. ``"jan.n@example.com"``.
            entity_type: Type to use if the value is new, e.g. ``"EMAIL"``.
                Ignored if the value is already stored.

        Returns:
            The placeholder, e.g. ``"[EMAIL_1]"``.
        """
        ...

    def get_placeholder(self, value: str) -> str | None:
        """Return the placeholder for ``value``, or ``None`` if unknown."""
        ...

    def get_value(self, placeholder: str) -> str | None:
        """Return the original value for ``placeholder``, or ``None`` if unknown."""
        ...

    def items(self) -> list[tuple[str, str]]:
        """Return every ``(placeholder, value)`` pair in creation order."""
        ...

    def clear(self) -> None:
        """Forget every mapping and restart numbering at 1 for every type."""
        ...

    def __len__(self) -> int:
        """Return the number of stored values."""
        ...


@runtime_checkable
class _Remembering(Protocol):
    """A vault that also keeps values the leak check should know.

    Private for now. These are spellings with no placeholder of their own: a
    variant spelling merged by ``Shield(normalize=True)``, and a match hidden
    inside a merged placeholder. Each is remembered with the placeholder it
    was masked as. They are not part of `Vault.items`, ``len()``,
    `Vault.get_placeholder` or `Vault.get_value`, so they never affect
    numbering or restoring, and `Vault.clear` forgets them. A vault without
    this capability gets a per-masker memory instead (see `Masker`).
    """

    def _remember(self, value: str, entity_type: str, placeholder: str) -> None:
        """Remember ``value``, masked as ``placeholder``, for the leak check."""
        ...

    def _remembered(self) -> list[tuple[str, str, str]]:
        """Return every ``(value, entity_type, placeholder)``, oldest first."""
        ...

    def _mark_merged(self, placeholder: str) -> None:
        """Record that ``placeholder`` covers several overlapping matches.

        Such a value is never normalized, and no later spelling merges into
        it; the mark lets a new `Shield` on the same vault know that too.
        """
        ...

    def _merged(self) -> set[str]:
        """Return the placeholders recorded with `_mark_merged`."""
        ...


def _batch(vault: object) -> AbstractContextManager[None]:
    """The vault's way of grouping one call's reads and writes, if it has one.

    `SQLiteVault` checks its file once per group and writes in one transaction.
    """
    batch = getattr(vault, "_batch", None)
    if callable(batch):
        return cast("AbstractContextManager[None]", batch())
    return nullcontext()
