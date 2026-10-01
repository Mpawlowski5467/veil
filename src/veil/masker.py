"""Masking: replace detected spans with placeholders from the vault."""

from __future__ import annotations

import bisect
import re
import threading
from collections.abc import Iterable, Mapping, Sequence, Set

from ._normalize import KeyIndex, Normalizer
from ._search import CachedIndex
from ._text import is_word_char
from .detectors.base import Detector, _FieldDetector
from .detectors.manual import ManualDetector
from .placeholders import (
    LOOSE_PLACEHOLDER_RE,
    PLACEHOLDER_RE,
    loose_match_body,
    placeholder_candidates,
    placeholder_type,
)
from .types import MaskedEntity, MaskResult, Span
from .vault.base import Vault, _batch, _Remembering


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


#: `Span.source` (and `MaskedEntity.source`) of a placeholder that covers
#: several overlapping matches. Such a value is never normalized.
MERGED_SOURCE = "merged"

# A run of letters and digits: exactly the characters for which str.isalnum()
# is true (the two agree on every code point).
_ALNUM_RUN_RE = re.compile(r"[^\W_]+")


def merge_partial_overlaps(
    text: str, candidates: Sequence[Span], kept: list[Span]
) -> list[tuple[Span, tuple[Span, ...]]]:
    """Merge every partly masked span with the kept spans it overlaps.

    A candidate that lost an overlap is *partly masked* when a letter or digit
    of it lies outside every kept span. Kept spans and partly masked
    candidates are grouped by overlap, transitively. Each group that contains
    a partly masked candidate becomes one span covering the whole group, with
    the type and priority of the group's highest-ranked kept span and source
    `MERGED_SOURCE`.

    Returns:
        ``(span, members)`` pairs in text order, never overlapping. For a
        merged span, ``members`` holds the kept spans it replaces; for a kept
        span left as it was, ``members`` is empty.
    """
    if not kept:
        return []
    kept_set = set(kept)
    ends = [span.end for span in kept]
    runs: tuple[list[int], list[int]] | None = None
    partial: list[Span] = []
    for span in candidates:
        if span in kept_set:
            continue
        i = bisect.bisect_right(ends, span.start)  # first kept span ending after
        if i < len(kept) and kept[i].start <= span.start and span.end <= ends[i]:
            continue  # inside one kept span
        if runs is None:
            runs = _uncovered_alnum_runs(text, kept)
        run_starts, run_ends = runs
        r = bisect.bisect_right(run_ends, span.start)
        if r < len(run_starts) and run_starts[r] < span.end:
            partial.append(span)
    if not partial:
        return [(span, ()) for span in kept]

    items = sorted(
        [(span, True) for span in kept] + [(span, False) for span in partial],
        key=lambda item: item[0].start,
    )
    result: list[tuple[Span, tuple[Span, ...]]] = []
    members: list[Span] = []
    has_partial = False
    start = end = 0
    for index, (span, is_kept) in enumerate(items):
        if index and span.start >= end:
            result.append(_group_span(text, start, end, members, has_partial))
            members, has_partial = [], False
        if not members and not has_partial:
            start, end = span.start, span.end
        end = max(end, span.end)
        if is_kept:
            members.append(span)
        else:
            has_partial = True
    result.append(_group_span(text, start, end, members, has_partial))
    return result


def _group_span(
    text: str, start: int, end: int, members: list[Span], has_partial: bool
) -> tuple[Span, tuple[Span, ...]]:
    if not has_partial:
        return members[0], ()
    winner = min(members, key=lambda s: (-len(s), -s.priority, s.start))
    merged = Span(
        start=start,
        end=end,
        value=text[start:end],
        entity_type=winner.entity_type,
        source=MERGED_SOURCE,
        priority=winner.priority,
    )
    return merged, tuple(members)


def _uncovered_alnum_runs(text: str, kept: list[Span]) -> tuple[list[int], list[int]]:
    run_starts: list[int] = []
    run_ends: list[int] = []
    gap_start = 0
    for gap_end, next_start in [*((s.start, s.end) for s in kept), (len(text), 0)]:
        for match in _ALNUM_RUN_RE.finditer(text, gap_start, gap_end):
            run_starts.append(match.start())
            run_ends.append(match.end())
        gap_start = next_start
    return run_starts, run_ends


class _LocalMemory:
    """Remembered values for a vault that can't keep them itself.

    Each entry also records the value stored under its placeholder, and only
    counts while the vault still stores that value there, so clearing the
    vault some other way than `Masker.forget` drops it too.
    """

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        self._entries: dict[str, tuple[str, str, str]] = {}
        self._merged_entries: dict[str, str] = {}  # placeholder -> stored value

    def _remember(self, value: str, entity_type: str, placeholder: str) -> None:
        stored = self._vault.get_value(placeholder)
        if stored is not None and self._vault.get_placeholder(value) is None:
            self._entries.setdefault(value, (entity_type, placeholder, stored))

    def _remembered(self) -> list[tuple[str, str, str]]:
        live = []
        for value, (entity_type, placeholder, stored) in list(self._entries.items()):
            if self._vault.get_value(placeholder) != stored:
                del self._entries[value]
            else:
                live.append((value, entity_type, placeholder))
        return live

    def _mark_merged(self, placeholder: str) -> None:
        stored = self._vault.get_value(placeholder)
        if stored is not None:
            self._merged_entries[placeholder] = stored

    def _merged(self) -> set[str]:
        return {
            placeholder
            for placeholder, stored in self._merged_entries.items()
            if self._vault.get_value(placeholder) == stored
        }

    def clear(self) -> None:
        self._entries.clear()
        self._merged_entries.clear()


class Masker:
    """Replaces sensitive spans with placeholders.

    Runs every detector, resolves overlaps with `resolve_overlaps`, merges a
    match that would stay partly visible with the matches it overlaps (see
    `merge_partial_overlaps`), asks the vault for each value's placeholder (so
    a value keeps its placeholder across calls), and then warns when any known
    value still appears in the output. Calls from several threads take turns.
    """

    def __init__(
        self,
        detectors: Sequence[Detector],
        vault: Vault,
        *,
        redact_warnings: bool = False,
        tolerant_restore: bool = True,
        normalizers: Mapping[str, Normalizer] | None = None,
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
            normalizers: Internal and may change. Maps an entity type to a
                function giving a value's key (see ``_normalize``); a
                value whose key matches a stored value of its type shares that
                value's placeholder.
        """
        self._detectors = tuple(detectors)
        self._vault = vault
        self._redact = redact_warnings
        self._tolerant = tolerant_restore
        self._memory = _memory_for(vault)
        self._keys = KeyIndex(normalizers, self._memory) if normalizers else None
        self._leak_index = CachedIndex()
        self._lock = threading.Lock()

    def forget(self) -> None:
        """Drop what is cached about the conversation so far.

        `Shield.reset` calls this after clearing the vault.
        """
        with self._lock:
            if isinstance(self._memory, _LocalMemory):
                self._memory.clear()
            if self._keys is not None:
                self._keys.clear()
            self._leak_index.clear()

    def __getstate__(self) -> dict[str, object]:
        """Pickle without caches (they are rebuilt on demand) or the lock."""
        if isinstance(self._memory, _LocalMemory):
            self._memory._remembered()  # drops entries the vault no longer backs
        state = dict(self.__dict__)
        state.pop("_leak_index", None)
        state.pop("_lock", None)
        return state

    def __setstate__(self, state: dict[str, object]) -> None:
        """Unpickle, including a masker pickled by an older version."""
        self.__dict__.update(state)
        self.__dict__.setdefault("_keys", None)
        if "_memory" not in state:
            self._memory = _memory_for(self._vault)
        if self._keys is not None:
            self._keys.use_memory(self._memory)
        self._leak_index = CachedIndex()
        self._lock = threading.Lock()

    def mask(self, text: str, *, field_name: str | None = None) -> MaskResult:
        """Mask ``text``.

        Placeholders are assigned in reading order, and replacements are made
        from the end of the string backward so earlier offsets stay valid.

        Args:
            text: The text to mask.
            field_name: Optional parsed field name for contextual detectors.

        Returns:
            The masked text, every replacement made, and any warnings.

        Raises:
            TypeError: If ``text`` is not a string.
            ValueError: If a detector returns a span that does not match the
                text. Masking stops rather than risk sending partly masked text.
        """
        if not isinstance(text, str):
            raise TypeError(f"mask() expects str, got {type(text).__name__}")
        if field_name is not None and not isinstance(field_name, str):
            raise TypeError("field_name must be a string or None")
        if not text:
            return MaskResult(text=text)
        with self._lock:
            return self._mask(text, field_name)

    def _mask(self, text: str, field_name: str | None = None) -> MaskResult:
        candidates: list[Span] = []
        for detector in self._detectors:
            detected = detector.detect(text)
            if field_name is not None and isinstance(detector, _FieldDetector):
                detected = [*detected, *detector.detect_field(text, field_name)]
            for span in detected:
                if span.end > len(text) or text[span.start : span.end] != span.value:
                    raise ValueError(
                        f"{type(detector).__name__} returned a span at "
                        f"{span.start}:{span.end} that does not match the text"
                    )
                candidates.append(span)

        spans = merge_partial_overlaps(text, candidates, resolve_overlaps(candidates))
        with _batch(self._vault):  # one check of a shared vault, one transaction
            entities = self._store(spans)

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

        unmasked.reverse()
        # Only text left between placeholders can be mistaken for one later;
        # placeholder-shaped text that was itself masked restores as written.
        warnings = self._placeholder_like_input(unmasked)
        warnings.extend(self._leaks(text, spans, unmasked, candidates))
        return MaskResult(text=masked, entities=entities, warnings=warnings)

    def _store(self, spans: list[tuple[Span, tuple[Span, ...]]]) -> list[MaskedEntity]:
        """Give each span its placeholder, storing new values in the vault."""
        vault, keys, memory = self._vault, self._keys, self._memory
        entities: list[MaskedEntity] = []
        for span, members in spans:
            if keys is None:
                placeholder = vault.get_or_create(span.value, span.entity_type)
            elif members:
                # A merged value is not one value of its type: never normalize
                # it, and never let a later spelling merge into it.
                placeholder = keys.create(vault, span, None)
            else:
                placeholder = keys.placeholder_for(vault, span)
                if vault.get_value(placeholder) != span.value:
                    # A variant spelling: not in the vault, so remember it.
                    memory._remember(
                        span.value, placeholder_type(placeholder) or "?", placeholder
                    )
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
        # What a merged span replaced is remembered for later leak checks,
        # without a placeholder of its own (so restore never produces it). The
        # merged placeholder is marked, so no later spelling merges into it.
        for (_, members), entity in zip(spans, entities, strict=True):
            if members:
                memory._mark_merged(entity.placeholder)
            for member in members:
                memory._remember(member.value, member.entity_type, entity.placeholder)
        return entities

    def _placeholder_like_input(self, pieces: list[str]) -> list[str]:
        """Warn about input left unmasked that restore() would treat as a placeholder.

        That is every exact placeholder, and, with tolerant restore, every
        rewritten form (``[Person 1]``) of a type this vault uses.
        """
        known_types: set[str | None] | None = None  # built only if needed
        tokens: dict[str, None] = {}
        if self._tolerant:
            matches = (
                m for piece in pieces for m in LOOSE_PLACEHOLDER_RE.finditer(piece)
            )
            for match in matches:
                if match["exact"] is not None:
                    tokens[match.group(0)] = None
                    continue
                if known_types is None:
                    known_types = {
                        placeholder_type(stored) for stored, _ in self._vault.items()
                    }
                candidates = placeholder_candidates(loose_match_body(match))
                if any(placeholder_type(c) in known_types for c in candidates):
                    tokens[match.group(0)] = None
        else:
            tokens.update(
                dict.fromkeys(
                    m.group(0)
                    for piece in pieces
                    for m in PLACEHOLDER_RE.finditer(piece)
                )
            )
        warnings = []
        for token in tokens:
            label = f"({len(token)} characters)" if self._redact else token
            warnings.append(
                f"Input already contains placeholder-like text {label}; "
                "restore() will treat it as a placeholder."
            )
        return warnings

    def _leaks(
        self,
        text: str,
        spans: list[tuple[Span, tuple[Span, ...]]],
        unmasked: list[str],
        candidates: Iterable[Span],
    ) -> list[str]:
        """Warn about every known value that still appears between placeholders.

        Known values are everything in the vault (from this call and earlier
        ones), the values remembered with it (spellings that have no
        placeholder of their own), and anything detected in this call,
        including spans that lost an overlap. Registered manual entities are
        matched with the same whole-token rule used to detect them (so "Jan"
        isn't reported inside "January"); every other value is matched as a
        plain substring, because a phone number glued to letters is still a
        phone number. All of them are searched for together, with an index
        kept between calls (see `CachedIndex`).
        """
        # value -> entity type; None for a vault value, whose type is read from
        # its placeholder only if it is reported.
        known: dict[str, str | None] = dict.fromkeys(v for _, v in self._vault.items())
        masked_as: dict[str, str] = {}  # remembered value -> its placeholder
        for value, entity_type, masked in self._memory._remembered():
            if value not in known:
                known[value] = entity_type
                masked_as[value] = masked
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
        if any(members for _, members in spans):
            found |= self._cut_leaks(text, spans, known.keys(), manual, found)

        warnings = []
        for value, known_type in known.items():
            if value not in found:
                continue
            placeholder = self._vault.get_placeholder(value) or masked_as.get(value)
            entity_type = (
                known_type or (placeholder and placeholder_type(placeholder)) or "?"
            )
            if self._redact:
                where = f" ({placeholder})" if placeholder else ""
                label = f"a known {entity_type} value{where}"
            else:
                label = f"known value {value!r}"
                label += f" ({placeholder})" if placeholder else ""
            warnings.append(f"Leak check: {label} still appears in the masked text.")
        return warnings

    def _cut_leaks(
        self,
        text: str,
        spans: list[tuple[Span, tuple[Span, ...]]],
        known: Set[str],
        manual: Set[str],
        found: Set[str],
    ) -> set[str]:
        """Known values that a merged placeholder hides only part of.

        Without merging, the leak check would have seen these between the
        kept spans. A merged span can swallow the start or end of such an
        occurrence, so the part left over no longer matches. Only the gaps
        between kept spans that a merged span reaches into can hold one.
        """
        merged = [span for span, members in spans if members]
        merged_starts = [span.start for span in merged]
        kept: list[Span] = []
        for span, members in spans:
            kept.extend(members or (span,))
        gaps: list[tuple[int, int]] = []
        cursor = 0
        for start, end in [*((k.start, k.end) for k in kept), (len(text), len(text))]:
            if cursor < start:
                i = bisect.bisect_right(merged_starts, start - 1) - 1
                if i >= 0 and merged[i].end > cursor:
                    gaps.append((cursor, start))
            cursor = end
        if not gaps:
            return set()
        pieces = [text[a:b] for a, b in gaps]
        separator = _unused_char(pieces, known)
        haystacks = pieces if separator is None else [separator.join(pieces)]
        maybe = self._leak_index.present(haystacks, known, manual, update=False)
        maybe -= found
        if not maybe:
            return set()
        search = _GapSearch(text, gaps, merged, haystacks)
        return {value for value in maybe if search.visible(value, value in manual)}


class _GapSearch:
    """Finds where values occur in the gaps that merged spans reach into."""

    def __init__(
        self,
        text: str,
        gaps: list[tuple[int, int]],
        merged: list[Span],
        haystacks: list[str],
    ) -> None:
        self._text = text
        # Each haystack with the offset in it where each of its gaps starts,
        # and those gaps' bounds in the text.
        self._haystacks: list[tuple[str, list[int], list[tuple[int, int]]]] = []
        if len(haystacks) == len(gaps):
            for haystack, gap in zip(haystacks, gaps, strict=True):
                self._haystacks.append((haystack, [0], [gap]))
        else:  # one haystack, the gaps joined by a one-character separator
            offsets, cursor = [], 0
            for start, end in gaps:
                offsets.append(cursor)
                cursor += end - start + 1
            self._haystacks.append((haystacks[0], offsets, gaps))
        # Merged spans that touch hide an occurrence together.
        self._hidden_starts: list[int] = []
        self._hidden_ends: list[int] = []
        for span in merged:
            if self._hidden_ends and self._hidden_ends[-1] == span.start:
                self._hidden_ends[-1] = span.end
            else:
                self._hidden_starts.append(span.start)
                self._hidden_ends.append(span.end)

    def visible(self, value: str, token: bool) -> bool:
        """Whether ``value`` occurs in a gap and not entirely inside merged spans.

        The ends of a gap count as word boundaries, as the leak check's
        separator does. With ``token``, only whole-token occurrences count.
        """
        text, size = self._text, len(value)
        for haystack, offsets, bounds in self._haystacks:
            found = haystack.find(value)
            while found != -1:
                k = bisect.bisect_right(offsets, found) - 1
                gap_start, gap_end = bounds[k]
                start = gap_start + found - offsets[k]
                end = start + size
                if token and (
                    (
                        start > gap_start
                        and is_word_char(text[start])
                        and is_word_char(text[start - 1])
                    )
                    or (
                        end < gap_end
                        and is_word_char(text[end - 1])
                        and is_word_char(text[end])
                    )
                ):
                    found = haystack.find(value, found + 1)
                    continue
                i = bisect.bisect_right(self._hidden_starts, start) - 1
                if i < 0 or end > self._hidden_ends[i]:
                    return True
                # Hidden, and so is every later start up to the end of the
                # hidden stretch, less the value's length.
                skip = min(max(start + 1, self._hidden_ends[i] - size + 1), gap_end)
                found = haystack.find(value, found + skip - start)
        return False


def _memory_for(vault: Vault) -> _Remembering | _LocalMemory:
    """Where to remember values: the vault itself if it can, else the masker."""
    return vault if isinstance(vault, _Remembering) else _LocalMemory(vault)


def _unused_char(pieces: list[str], values: Iterable[str]) -> str | None:
    """Return a control character that appears in no piece and no value."""
    used = "".join(pieces) + "".join(values)
    for code in (*range(0x00, 0x09), *range(0x0E, 0x20)):
        if chr(code) not in used:
            return chr(code)
    return None
