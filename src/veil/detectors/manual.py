"""Detection of values the user registered by hand, such as names."""

from __future__ import annotations

from typing import ClassVar

from .._text import find_token
from ..placeholders import validate_entity_type
from ..types import Span


class ManualDetector:
    """Finds manually registered values as exact, case-sensitive tokens.

    Use it for values a pattern cannot find, such as people's names. A value
    is not matched when it is glued to a letter, digit, or combining mark on
    a side where the value itself starts or ends with one, so
    registering ``"Jan"`` does not mask the start of ``"January"``. Scripts
    written without spaces (Chinese, Japanese, Thai, ...) have no visible word
    boundaries, so there a registered value matches wherever it appears.

    Example:
        >>> detector = ManualDetector()
        >>> detector.add("Jan Nowak", "PERSON")
        >>> [s.value for s in detector.detect("Hi Jan Nowak!")]
        ['Jan Nowak']
    """

    #: Tie-break priority of manual spans; higher than any regex span.
    PRIORITY: ClassVar[int] = 100

    def __init__(self) -> None:
        """Create a detector with no registered values."""
        self._entities: dict[str, str] = {}

    def add(self, value: str, entity_type: str) -> None:
        """Register ``value`` so it is detected as ``entity_type``.

        Registering the same value again replaces its entity type.

        Raises:
            ValueError: If ``value`` is empty or only whitespace, or the entity
                type is invalid.
        """
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Manual entities must be non-empty, non-blank strings")
        validate_entity_type(entity_type)
        self._entities[value] = entity_type

    @property
    def entities(self) -> dict[str, str]:
        """A copy of the registered ``{value: entity_type}`` mapping."""
        return dict(self._entities)

    def detect(self, text: str) -> list[Span]:
        """Return every occurrence of every registered value, sorted by position.

        Occurrences of different values may overlap (``"Jan Nowak"`` and
        ``"Nowak"``); the masker keeps the longest.
        """
        spans = [
            Span(
                start=start,
                end=start + len(value),
                value=value,
                entity_type=entity_type,
                source="manual",
                priority=self.PRIORITY,
            )
            for value, entity_type in self._entities.items()
            for start in find_token(text, value)
        ]
        spans.sort(key=lambda s: (s.start, -len(s)))
        return spans

    def __len__(self) -> int:
        """Return the number of registered values."""
        return len(self._entities)

    def __repr__(self) -> str:
        """Summarize the detector without revealing registered values."""
        return f"{type(self).__name__}(<{len(self)} entities>)"
