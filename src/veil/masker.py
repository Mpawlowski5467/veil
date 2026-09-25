"""Masking: replace detected spans with placeholders from the vault."""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence

from .detectors.base import Detector
from .detectors.manual import literal_pattern
from .placeholders import PLACEHOLDER_RE
from .types import MaskedEntity, MaskResult, Span
from .vault.base import Vault


def resolve_overlaps(spans: Iterable[Span]) -> list[Span]:
    """Choose a non-overlapping subset of ``spans``, sorted by position.

    Spans are considered longest first, and each is kept unless it overlaps a
    span already kept. Among spans of equal length, higher ``priority`` wins
    (so manual entities beat regex matches), then the earlier start.

    Args:
        spans: Candidate spans from any number of detectors.

    Returns:
        The kept spans, ordered by ``start``.
    """
    ranked = sorted(spans, key=lambda s: (-len(s), -s.priority, s.start))
    kept: list[Span] = []  # sorted by start; never overlapping
    starts: list[int] = []
    for span in ranked:
        i = bisect.bisect_left(starts, span.start)
        if i > 0 and kept[i - 1].end > span.start:
            continue
        if i < len(kept) and kept[i].start < span.end:
            continue
        kept.insert(i, span)
        starts.insert(i, span.start)
    return kept


class Masker:
    """Replaces sensitive spans with placeholders.

    Runs every detector, resolves overlaps with `resolve_overlaps`, asks the
    vault for each value's placeholder (so a value keeps its placeholder across
    calls), and then checks that no known value survived in the output.
    """

    def __init__(self, detectors: Sequence[Detector], vault: Vault) -> None:
        """Create a masker.

        Args:
            detectors: Detectors to run, in order. Their spans are pooled.
            vault: Where placeholders are created and looked up.
        """
        self._detectors = tuple(detectors)
        self._vault = vault

    def mask(self, text: str) -> MaskResult:
        """Mask ``text``.

        Placeholders are assigned in reading order, and replacements are made
        from the end of the string backward so earlier offsets stay valid.

        Args:
            text: The text to mask.

        Returns:
            The masked text, every replacement made, and any warnings.

        Raises:
            TypeError: If ``text`` is not a string.
            ValueError: If a detector returns a span that does not match the
                text. Masking stops rather than risk sending partly masked text.
        """
        if not isinstance(text, str):
            raise TypeError(f"mask() expects str, got {type(text).__name__}")
        if not text:
            return MaskResult(text=text)

        candidates: list[Span] = []
        for detector in self._detectors:
            for span in detector.detect(text):
                if span.end > len(text) or text[span.start : span.end] != span.value:
                    raise ValueError(
                        f"{type(detector).__name__} returned a span at "
                        f"{span.start}:{span.end} that does not match the text"
                    )
                candidates.append(span)

        entities: list[MaskedEntity] = []
        for span in resolve_overlaps(candidates):
            placeholder = self._vault.get_or_create(span.value, span.entity_type)
            # A value keeps the type it was first stored with, which may differ
            # from what this detector called it; report the type actually used.
            match = PLACEHOLDER_RE.fullmatch(placeholder)
            entities.append(
                MaskedEntity(
                    placeholder=placeholder,
                    value=span.value,
                    entity_type=match["type"] if match else span.entity_type,
                    start=span.start,
                    end=span.end,
                    source=span.source,
                )
            )

        pieces: list[str] = []
        cursor = len(text)
        for entity in reversed(entities):
            pieces.append(text[entity.end : cursor])
            pieces.append(entity.placeholder)
            cursor = entity.start
        pieces.append(text[:cursor])
        masked = "".join(reversed(pieces))

        warnings = _placeholder_like_input(text)
        warnings.extend(self._leaks(masked, candidates))
        return MaskResult(text=masked, entities=entities, warnings=warnings)

    def _leaks(self, masked: str, candidates: Iterable[Span]) -> list[str]:
        """Warn about every known value that still appears in ``masked``.

        Known values are everything in the vault (from this call and earlier
        ones) plus anything detected in this call, including spans that lost
        an overlap. Matching follows `literal_pattern`, the same rule used for
        manual entities.
        """
        known = dict.fromkeys(value for _, value in self._vault.items())
        known.update(dict.fromkeys(span.value for span in candidates))
        warnings = []
        for value in known:
            if literal_pattern(value).search(masked):
                placeholder = self._vault.get_placeholder(value)
                label = f"{value!r} ({placeholder})" if placeholder else repr(value)
                warnings.append(
                    f"Leak check: known value {label} still appears in the masked text."
                )
        return warnings


def _placeholder_like_input(text: str) -> list[str]:
    tokens = dict.fromkeys(match.group(0) for match in PLACEHOLDER_RE.finditer(text))
    return [
        f"Input already contains placeholder-like text {token}; "
        "restore() will treat it as a placeholder."
        for token in tokens
    ]
