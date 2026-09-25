"""An in-memory `Vault` implementation."""

from __future__ import annotations

from ..placeholders import format_placeholder, validate_entity_type


class MemoryVault:
    """Keeps value <-> placeholder mappings in plain dictionaries.

    Mappings live as long as the object. Numbering is per entity type and
    starts at 1: the first email is ``[EMAIL_1]``, the second ``[EMAIL_2]``,
    independently of how many phone numbers have been seen.

    Not thread-safe. Use one vault (and one `Shield`) per conversation.
    """

    def __init__(self) -> None:
        """Create an empty vault."""
        self._placeholder_by_value: dict[str, str] = {}
        self._value_by_placeholder: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def get_or_create(self, value: str, entity_type: str) -> str:
        """Return the placeholder for ``value``, creating one if it is new.

        Args:
            value: The original text. Must be a non-empty string.
            entity_type: Type to use if the value is new. Ignored if the value
                is already stored, so the first type wins.

        Returns:
            The placeholder, e.g. ``"[EMAIL_1]"``.

        Raises:
            ValueError: If ``value`` is empty or ``entity_type`` is invalid.
        """
        if not isinstance(value, str) or not value:
            raise ValueError("Vault values must be non-empty strings")
        existing = self._placeholder_by_value.get(value)
        if existing is not None:
            return existing
        validate_entity_type(entity_type)
        number = self._counters.get(entity_type, 0) + 1
        placeholder = format_placeholder(entity_type, number)
        self._counters[entity_type] = number
        self._placeholder_by_value[value] = placeholder
        self._value_by_placeholder[placeholder] = value
        return placeholder

    def get_placeholder(self, value: str) -> str | None:
        """Return the placeholder for ``value``, or ``None`` if unknown."""
        return self._placeholder_by_value.get(value)

    def get_value(self, placeholder: str) -> str | None:
        """Return the original value for ``placeholder``, or ``None`` if unknown."""
        return self._value_by_placeholder.get(placeholder)

    def items(self) -> list[tuple[str, str]]:
        """Return every ``(placeholder, value)`` pair in creation order."""
        return list(self._value_by_placeholder.items())

    def clear(self) -> None:
        """Forget every mapping and restart numbering at 1 for every type."""
        self._placeholder_by_value.clear()
        self._value_by_placeholder.clear()
        self._counters.clear()

    def __len__(self) -> int:
        """Return the number of stored values."""
        return len(self._placeholder_by_value)

    def __repr__(self) -> str:
        """Summarize the vault without revealing any stored values."""
        return f"{type(self).__name__}(<{len(self)} values>)"
