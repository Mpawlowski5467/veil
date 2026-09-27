"""Detection of text that is already shaped like a placeholder."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from ..placeholders import PLACEHOLDER_RE, validate_entity_type
from ..types import Span


class LiteralPlaceholderDetector:
    """Finds text that already looks like a placeholder, so it is masked too.

    A document that contains ``[EMAIL_1]`` as plain text, such as a template
    or these docs, would otherwise pass through masking unchanged, and a
    later `restore` would turn that text into whatever value ``[EMAIL_1]``
    stands for. This detector reports each exact placeholder in the text as a
    span of type ``LITERAL``, so it gets a placeholder of its own
    (``[LITERAL_1]``) and restores to the original text.

    Only placeholders of the given ``types`` are reported (plus the literal
    type itself, so ``[LITERAL_1]`` written in a document round-trips too);
    other bracketed text, such as the code subscript ``row[COL_1]``, is left
    alone. With ``types=None``, every exact placeholder is reported. Keep the
    set fixed for a conversation: masking the same text must always give the
    same result.

    Example:
        >>> detector = LiteralPlaceholderDetector({"EMAIL"})
        >>> [s.value for s in detector.detect("See [EMAIL_1], row[COL_1]")]
        ['[EMAIL_1]']
    """

    #: Tie-break priority; higher than manual entities, so a registered value
    #: that happens to look like a placeholder is still treated as literal.
    PRIORITY: ClassVar[int] = 200

    def __init__(
        self, types: Iterable[str] | None = None, *, entity_type: str = "LITERAL"
    ) -> None:
        """Create a detector.

        Args:
            types: The entity types whose placeholders count as literal text,
                e.g. ``{"EMAIL", "PERSON"}``, or None for every type.
            entity_type: The type literal text is masked as.

        Raises:
            ValueError: If a type name is invalid.
        """
        self._entity_type = validate_entity_type(entity_type)
        self._types: frozenset[str] | None = None
        if types is not None:
            if isinstance(types, str):
                raise TypeError("types must be a collection of type names, not a str")
            self._types = frozenset(validate_entity_type(t) for t in types) | {
                self._entity_type
            }

    @property
    def types(self) -> frozenset[str] | None:
        """The types whose placeholders are reported, or None for every type."""
        return self._types

    def detect(self, text: str) -> list[Span]:
        """Return every exact placeholder of a reported type, in order."""
        return [
            Span(
                start=match.start(),
                end=match.end(),
                value=match.group(0),
                entity_type=self._entity_type,
                source="literal",
                priority=self.PRIORITY,
            )
            for match in PLACEHOLDER_RE.finditer(text)
            if self._types is None or match["type"] in self._types
        ]
