"""Detection of values the user registered by hand, such as names."""

from __future__ import annotations

from typing import ClassVar

from .._search import LiteralIndex
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

    # From this many registered values on, detect() searches for all of them
    # in one pass (see LiteralIndex) instead of one value at a time.
    _INDEX_MIN_VALUES: ClassVar[int] = 8

    # Built on demand. A class-level default, so a detector unpickled from an
    # older version (whose __dict__ lacks it) still works.
    _index: LiteralIndex | None = None

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
        entities = self._entities
        index = self._current_index()
        if index is not None:
            return [
                Span(
                    start=start,
                    end=start + len(value),
                    value=value,
                    entity_type=entities[value],
                    source="manual",
                    priority=self.PRIORITY,
                )
                for start, value in index.token_occurrences(text)
            ]
        spans = [
            Span(
                start=start,
                end=start + len(value),
                value=value,
                entity_type=entity_type,
                source="manual",
                priority=self.PRIORITY,
            )
            for value, entity_type in entities.items()
            for start in find_token(text, value)
        ]
        spans.sort(key=lambda s: (s.start, -len(s)))
        return spans

    def _current_index(self) -> LiteralIndex | None:
        """Return an index of every registered value, or None to use the loop.

        Values are never removed, so an index with as many values as are
        registered has all of them. Checking that here, rather than dropping
        the index in add(), also catches an index built from a snapshot taken
        while another thread was adding a value, and a shallow copy that
        shares the entity dict.
        """
        entities = self._entities
        if len(entities) < self._INDEX_MIN_VALUES:
            return None
        index = self._index
        if index is None or len(index) != len(entities):
            try:
                index = LiteralIndex(tuple(entities))
            except RecursionError:  # the stack is nearly full: use the loop
                return None
            self._index = index
        return index

    def __getstate__(self) -> dict[str, object]:
        """Pickle the registered values only; the index is rebuilt on demand."""
        state = dict(self.__dict__)
        state.pop("_index", None)
        return state

    def __len__(self) -> int:
        """Return the number of registered values."""
        return len(self._entities)

    def __repr__(self) -> str:
        """Summarize the detector without revealing registered values."""
        return f"{type(self).__name__}(<{len(self)} entities>)"
