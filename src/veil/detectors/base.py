"""The `Detector` protocol: anything that finds sensitive spans in text."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types import Span


@runtime_checkable
class Detector(Protocol):
    """Finds sensitive values in text.

    Detectors only report what they find. They may return overlapping spans;
    the masker decides which ones win. Implement this protocol to plug in a new
    detection strategy (for example an NER model) without touching the rest of
    the library.
    """

    def detect(self, text: str) -> list[Span]:
        """Return every sensitive span found in ``text``.

        Each span's ``value`` must equal ``text[span.start:span.end]``.
        """
        ...


@runtime_checkable
class _FieldDetector(Protocol):
    """Optional extension for detectors that understand a parsed field's name."""

    def detect_field(self, text: str, name: str) -> list[Span]:
        """Return spans in the value; the field name is context, not input text."""
        ...
