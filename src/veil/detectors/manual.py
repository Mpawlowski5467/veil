"""Detection of values the user registered by hand, such as names."""

from __future__ import annotations

import re
from typing import ClassVar

from ..placeholders import validate_entity_type
from ..types import Span

_WORD_CHAR = re.compile(r"\w")


def literal_pattern(value: str) -> re.Pattern[str]:
    """Compile a case-sensitive pattern that finds ``value`` as a whole token.

    The value must not be glued to a word character on a side where the value
    itself starts or ends with one. So ``"Jan"`` matches in ``"Jan's"`` and
    ``"(Jan)"`` but not in ``"January"``, while ``"#123"`` still matches in
    ``"order#123"`` because it starts with a non-word character.

    Args:
        value: A non-empty literal string.

    Returns:
        A compiled pattern matching the value.
    """
    prefix = r"(?<!\w)" if _WORD_CHAR.match(value[0]) else ""
    suffix = r"(?!\w)" if _WORD_CHAR.match(value[-1]) else ""
    return re.compile(prefix + re.escape(value) + suffix)


class ManualDetector:
    """Finds manually registered values as exact, case-sensitive tokens.

    Use it for values a pattern cannot find, such as people's names. Matching
    follows `literal_pattern`: registering ``"Jan"`` will not mask the start of
    ``"January"``.

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
        self._entities: dict[str, tuple[str, re.Pattern[str]]] = {}

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
        self._entities[value] = (entity_type, literal_pattern(value))

    @property
    def entities(self) -> dict[str, str]:
        """A copy of the registered ``{value: entity_type}`` mapping."""
        return {value: etype for value, (etype, _) in self._entities.items()}

    def detect(self, text: str) -> list[Span]:
        """Return every occurrence of every registered value, sorted by position.

        Occurrences of different values may overlap (``"Jan Nowak"`` and
        ``"Nowak"``); the masker keeps the longest.
        """
        spans: list[Span] = []
        for value, (entity_type, pattern) in self._entities.items():
            spans.extend(
                Span(
                    start=match.start(),
                    end=match.end(),
                    value=value,
                    entity_type=entity_type,
                    source="manual",
                    priority=self.PRIORITY,
                )
                for match in pattern.finditer(text)
            )
        spans.sort(key=lambda s: (s.start, -len(s)))
        return spans

    def __len__(self) -> int:
        """Return the number of registered values."""
        return len(self._entities)

    def __repr__(self) -> str:
        """Summarize the detector without revealing registered values."""
        return f"{type(self).__name__}(<{len(self)} entities>)"
