"""Data types shared by the detectors, vaults, masker, and restorer."""

from __future__ import annotations

from dataclasses import dataclass, field


class ShieldWarning(UserWarning):
    """Warning category for problems found while masking or restoring.

    `Shield.wrap()` emits every mask and restore warning with this category, so
    callers can filter them (``warnings.simplefilter("error", ShieldWarning)``)
    or capture them in tests.
    """


class ShieldError(Exception):
    """Raised by a strict `Shield.wrap()` call instead of emitting warnings.

    Attributes:
        stage: ``"mask"`` if masking warned, so the model was never called, or
            ``"restore"`` if restoring the model's reply warned.
        warnings: The warning messages. They quote detected values unless the
            `Shield` was created with ``redact_warnings=True``.
    """

    def __init__(self, stage: str, warnings: list[str]) -> None:
        """Create the error from the stage that failed and its warnings."""
        self.stage = stage
        self.warnings = list(warnings)
        super().__init__(f"{stage}() warned: " + " | ".join(self.warnings))


@dataclass(frozen=True, slots=True)
class Span:
    """A piece of sensitive text found by a detector.

    Attributes:
        start: Index of the first character of the match in the source text.
        end: Index one past the last character (so ``text[start:end] == value``).
        value: The matched text.
        entity_type: Entity type such as ``"EMAIL"``; becomes the placeholder's
            prefix.
        source: Short name of what produced the span, e.g. ``"regex"`` or
            ``"manual"``. Informational only.
        priority: Breaks ties between overlapping spans of equal length. Higher
            wins. Manual entities use the highest built-in priority.
    """

    start: int
    end: int
    value: str
    entity_type: str
    source: str = "regex"
    priority: int = 0

    def __post_init__(self) -> None:
        """Reject spans whose offsets and value disagree."""
        if not 0 <= self.start < self.end:
            raise ValueError(
                f"Span needs 0 <= start < end, got start={self.start}, end={self.end}"
            )
        if len(self.value) != self.end - self.start:
            raise ValueError(
                f"Span value length {len(self.value)} does not match "
                f"end - start = {self.end - self.start}"
            )

    def __len__(self) -> int:
        """Return the number of characters the span covers."""
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class MaskedEntity:
    """One occurrence of a value that was replaced by a placeholder.

    Attributes:
        placeholder: The placeholder written into the masked text, e.g.
            ``"[EMAIL_1]"``.
        value: The original text it replaced.
        entity_type: The entity type of the placeholder.
        start: Start offset of the value in the *original* text.
        end: End offset of the value in the *original* text.
        source: Which detector found it (see `Span.source`).
    """

    placeholder: str
    value: str
    entity_type: str
    start: int
    end: int
    source: str


@dataclass(frozen=True, slots=True)
class MaskResult:
    """Result of masking a piece of text.

    Attributes:
        text: The masked text, safe to send to a model.
        entities: Every replacement made, in the order they appear in the
            original text. A value that appears twice is listed twice with the
            same placeholder.
        warnings: Human-readable problems found while masking, such as a known
            value that still appears in ``text``.
    """

    text: str
    entities: list[MaskedEntity] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RepairedPlaceholder:
    """A placeholder the model wrote in a different form, restored anyway.

    Attributes:
        written: What the reply contained, e.g. ``"[person 1]"``.
        placeholder: The placeholder it was read as, e.g. ``"[PERSON_1]"``.
    """

    written: str
    placeholder: str


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Result of restoring placeholders in a model's reply.

    Attributes:
        text: The reply with every known placeholder replaced by its original
            value.
        restored_count: How many placeholder occurrences were replaced,
            including repaired ones.
        warnings: Human-readable problems, such as placeholders that are not in
            the vault. Unknown placeholders are left in ``text`` unchanged.
        repaired: Placeholders the model rewrote (``[person 1]``,
            ``【PERSON_1】``) that were restored anyway, in reply order.
    """

    text: str
    restored_count: int = 0
    warnings: list[str] = field(default_factory=list)
    repaired: list[RepairedPlaceholder] = field(default_factory=list)
