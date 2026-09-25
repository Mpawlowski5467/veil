"""The `Vault` protocol: storage for value <-> placeholder mappings."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Vault(Protocol):
    """Stores the two-way mapping between original values and placeholders.

    A vault owns placeholder numbering, so a persistent implementation (e.g. a
    future SQLite vault) keeps numbering stable across processes. Values are
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
