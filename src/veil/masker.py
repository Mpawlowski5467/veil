"""Masking: replace detected spans with placeholders from the vault."""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence

from ._search import CachedIndex
from .detectors.base import Detector
from .detectors.manual import ManualDetector
from .placeholders import (
    LOOSE_PLACEHOLDER_RE,
    PLACEHOLDER_RE,
    loose_match_body,
    placeholder_candidates,
    placeholder_type,
)
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
    if len(ranked) < _FEW_SPANS:
        return _resolve_sparse(ranked)
    size = max(span.end for span in ranked)
    if size > _DENSE_LIMIT and size > 8 * sum(map(len, ranked)):
        return _resolve_sparse(ranked)
    # One flag per character already covered by a kept span: checking and
    # marking a span costs its length, so the whole pass is linear.
    taken = bytearray(size)
    kept: list[Span] = []
    for span in ranked:
        if taken.find(1, span.start, span.end) == -1:
            taken[span.start : span.end] = b"\x01" * (span.end - span.start)
            kept.append(span)
    kept.sort(key=lambda s: s.start)
    return kept


# resolve_overlaps() uses a bisect-and-insert loop, O(spans^2) but fastest for
# a handful of spans, below this many; and when the spans are spread so thinly
# (far past _DENSE_LIMIT characters) that a byte per character is wasteful.
_FEW_SPANS = 64
_DENSE_LIMIT = 1 << 26


def _resolve_sparse(ranked: list[Span]) -> list[Span]:
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

    def __init__(
        self,
        detectors: Sequence[Detector],
        vault: Vault,
        *,
        redact_warnings: bool = False,
        tolerant_restore: bool = True,
    ) -> None:
        """Create a masker.

        Args:
            detectors: Detectors to run, in order. Their spans are pooled.
            vault: Where placeholders are created and looked up.
            redact_warnings: Describe leaked values by type, placeholder, and
                length instead of quoting them, so warnings are safe to log.
            tolerant_restore: Whether the matching `Restorer` also restores
                rewritten placeholders (``[person 1]``). Input that it would
                treat as a placeholder is reported.
        """
        self._detectors = tuple(detectors)
        self._vault = vault
        self._redact = redact_warnings
        self._tolerant = tolerant_restore
        self._leak_index = CachedIndex()

    def forget(self) -> None:
        """Drop what is cached about the conversation so far.

        `Shield.reset` calls this after clearing the vault.
        """
        self._leak_index.clear()

    def __getstate__(self) -> dict[str, object]:
        """Pickle without the leak check's index; it is rebuilt on demand."""
        state = dict(self.__dict__)
        state.pop("_leak_index", None)
        return state

    def __setstate__(self, state: dict[str, object]) -> None:
        """Unpickle, including a masker pickled by an older version."""
        self.__dict__.update(state)
        self._leak_index = CachedIndex()

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

        warnings = self._placeholder_like_input(text)
        warnings.extend(_partial_masks(text, candidates, kept, redact=self._redact))
        warnings.extend(self._leaks(list(reversed(unmasked)), candidates))
        return MaskResult(text=masked, entities=entities, warnings=warnings)

    def _placeholder_like_input(self, text: str) -> list[str]:
        """Warn about input that restore() would treat as a placeholder.

        That is every exact placeholder, and, with tolerant restore, every
        rewritten form (``[Person 1]``) of a type this vault uses.
        """
        known_types = {placeholder_type(stored) for stored, _ in self._vault.items()}
        tokens: dict[str, None] = {}
        if self._tolerant:
            for match in LOOSE_PLACEHOLDER_RE.finditer(text):
                candidates = placeholder_candidates(loose_match_body(match))
                if match["exact"] is not None or any(
                    placeholder_type(c) in known_types for c in candidates
                ):
                    tokens[match.group(0)] = None
        else:
            tokens.update(
                dict.fromkeys(m.group(0) for m in PLACEHOLDER_RE.finditer(text))
            )
        warnings = []
        for token in tokens:
            label = f"({len(token)} characters)" if self._redact else token
            warnings.append(
                f"Input already contains placeholder-like text {label}; "
                "restore() will treat it as a placeholder."
            )
        return warnings

    def _leaks(self, unmasked: list[str], candidates: Iterable[Span]) -> list[str]:
        """Warn about every known value that still appears between placeholders.

        Known values are everything in the vault (from this call and earlier
        ones) plus anything detected in this call, including spans that lost
        an overlap. Registered manual entities are matched with the same
        whole-token rule used to detect them (so "Jan" isn't reported inside
        "January"); every other value is matched as a plain substring, because
        a phone number glued to letters is still a phone number. All of them
        are searched for together, with an index kept between calls (see
        `CachedIndex`).
        """
        known: dict[str, str] = {}  # value -> entity type
        for stored, value in self._vault.items():
            match = PLACEHOLDER_RE.fullmatch(stored)
            known[value] = match["type"] if match else "?"
        for span in candidates:
            known.setdefault(span.value, span.entity_type)
        manual: set[str] = set()
        for detector in self._detectors:
            if isinstance(detector, ManualDetector):
                manual.update(detector.entities)

        # The pieces are joined with a character that is in neither the text
        # nor any value. It stands in for each placeholder: like "[", it is not
        # a word character, and no value can match across it.
        separator = _unused_char(unmasked, known)
        haystacks = unmasked if separator is None else [separator.join(unmasked)]

        found = self._leak_index.present(haystacks, known.keys(), manual)

        warnings = []
        for value, entity_type in known.items():
            if value not in found:
                continue
            placeholder = self._vault.get_placeholder(value)
            if self._redact:
                where = f" ({placeholder})" if placeholder else ""
                label = f"a known {entity_type} value{where}"
            else:
                label = f"known value {value!r}"
                label += f" ({placeholder})" if placeholder else ""
            warnings.append(f"Leak check: {label} still appears in the masked text.")
        return warnings


def _partial_masks(
    text: str, candidates: Iterable[Span], kept: list[Span], *, redact: bool
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
            if redact:
                size = sum(len(piece) for piece in leftovers)
                message = (
                    f"Partial mask: a detected {span.entity_type} value "
                    f"({len(span.value)} characters) overlapped a match that was "
                    f"kept, so {size} of its characters are still in the masked text."
                )
            else:
                shown = ", ".join(repr(piece) for piece in leftovers)
                message = (
                    f"Partial mask: detected {span.entity_type} value "
                    f"{span.value!r} overlapped a match that was kept, so {shown} "
                    "is still in the masked text."
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
