"""Masking: replace detected spans with placeholders from the vault."""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence

from ._text import contains_token
from .detectors.base import Detector
from .detectors.manual import ManualDetector
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
    calls), and then checks the output: it warns when a detected value was only
    partly replaced, and when any known value still appears in it.
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

        kept = resolve_overlaps(candidates)
        entities: list[MaskedEntity] = []
        for span in kept:
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
        unmasked: list[str] = []  # the original text left between placeholders
        cursor = len(text)
        for entity in reversed(entities):
            unmasked.append(text[entity.end : cursor])
            pieces.append(unmasked[-1])
            pieces.append(entity.placeholder)
            cursor = entity.start
        unmasked.append(text[:cursor])
        pieces.append(unmasked[-1])
        masked = "".join(reversed(pieces))

        warnings = _placeholder_like_input(text)
        warnings.extend(_partial_masks(text, candidates, kept))
        warnings.extend(self._leaks(list(reversed(unmasked)), candidates))
        return MaskResult(text=masked, entities=entities, warnings=warnings)

    def _leaks(self, unmasked: list[str], candidates: Iterable[Span]) -> list[str]:
        """Warn about every known value that still appears between placeholders.

        Known values are everything in the vault (from this call and earlier
        ones) plus anything detected in this call, including spans that lost
        an overlap. Registered manual entities are matched with the same
        whole-token rule used to detect them (so "Jan" isn't reported inside
        "January"); every other value is matched as a plain substring, because
        a phone number glued to letters is still a phone number.
        """
        known = dict.fromkeys(value for _, value in self._vault.items())
        known.update(dict.fromkeys(span.value for span in candidates))
        manual: set[str] = set()
        for detector in self._detectors:
            if isinstance(detector, ManualDetector):
                manual.update(detector.entities)

        # The pieces are joined with a character that is in neither the text
        # nor any value. It stands in for each placeholder: like "[", it is not
        # a word character, and no value can match across it.
        separator = _unused_char(unmasked, known)
        haystacks = unmasked if separator is None else [separator.join(unmasked)]

        warnings = []
        for value in known:
            if value in manual:
                found = any(contains_token(piece, value) for piece in haystacks)
            else:
                found = any(value in piece for piece in haystacks)
            if found:
                placeholder = self._vault.get_placeholder(value)
                label = f"{value!r} ({placeholder})" if placeholder else repr(value)
                warnings.append(
                    f"Leak check: known value {label} still appears in the masked text."
                )
        return warnings


def _partial_masks(
    text: str, candidates: Iterable[Span], kept: list[Span]
) -> list[str]:
    """Warn about dropped spans that only partly overlapped a kept span.

    The longest span wins an overlap, but when the loser sticks out past the
    winner, the part outside it stays in the masked text. That part may be
    sensitive (the tail of a phone number, a first name), so it is reported.
    """
    kept_set = set(kept)
    starts = [span.start for span in kept]
    warnings: dict[str, None] = {}
    for span in candidates:
        if span in kept_set:
            continue
        leftovers = []
        pos = span.start
        i = max(bisect.bisect_right(starts, span.start) - 1, 0)
        while i < len(kept) and kept[i].start < span.end:
            if kept[i].end > pos:
                if kept[i].start > pos:
                    leftovers.append(text[pos : kept[i].start])
                pos = kept[i].end
            i += 1
        if pos < span.end:
            leftovers.append(text[pos : span.end])
        if any(ch.isalnum() for piece in leftovers for ch in piece):
            shown = ", ".join(repr(piece) for piece in leftovers)
            message = (
                f"Partial mask: detected {span.entity_type} value {span.value!r} "
                f"overlapped a match that was kept, so {shown} is still in the "
                "masked text."
            )
            warnings[message] = None
    return list(warnings)


def _unused_char(pieces: list[str], values: Iterable[str]) -> str | None:
    """Return a control character that appears in no piece and no value."""
    used = "".join(pieces) + "".join(values)
    for code in (*range(0x00, 0x09), *range(0x0E, 0x20)):
        if chr(code) not in used:
            return chr(code)
    return None


def _placeholder_like_input(text: str) -> list[str]:
    tokens = dict.fromkeys(match.group(0) for match in PLACEHOLDER_RE.finditer(text))
    return [
        f"Input already contains placeholder-like text {token}; "
        "restore() will treat it as a placeholder."
        for token in tokens
    ]
