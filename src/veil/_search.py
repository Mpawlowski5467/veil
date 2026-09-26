"""Multi-string search: where do many literal values occur in a text?

The leak check and `ManualDetector` both ask this for many values at once.
Checking one value at a time costs O(values x text), which is what made large
inputs slow. `LiteralIndex` answers it in about one pass over the text.

It compiles only the top of the values' trie (a "skeleton") into regexes: a
branch stops once at most `_BUCKET` values share it and it is at least
`_MIN_PREFIX` characters long, or when a value ends. Where the text follows
the skeleton to the end of a branch, the values under that branch are checked
with `str.startswith`. The skeleton stays small, so compiling it is cheap even
for 100 000 values; `re` scans the text in C; and Python runs only where a
value may start. Every occurrence is found, including ones that overlap or
contain each other ("aa" and "aaa" in "aaaa").
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left
from collections.abc import Iterable, Iterator, Sequence, Set

from ._text import contains_token, find_token, is_word_char

# A skeleton branch stops when at most this many values share it...
_BUCKET = 16
# ...and it is at least this long (shorter values end their own branch).
_MIN_PREFIX = 3
# re's parser and compiler recurse once per nested group. Branching this deep
# (a chain of values that are prefixes of each other) stops the skeleton
# early; the values below are then checked with startswith instead.
_MAX_NESTING = 32
# At most this many characters of a value go into a regex, so text that
# repeats a long shared prefix can't make every position expensive.
_MAX_DEPTH = 64
# After a branch fails, re still tries every remaining alternative of the
# enclosing group (Python 3.10 has no atomic groups), so the values are split
# by first character over several regexes of about sqrt(4 x first chars),
# and at least this many, alternatives each.
_MIN_FANOUT = 16
# An inner node with more children than this is not put in the regex: re would
# try its alternatives one by one at every position where the path matches.
# The regex stops at the node instead, and the next character picks the
# child's values from a dict (see `_Wide`).
_MAX_WIDTH = 32
_LAST_CODE_POINT = 0x10FFFF

# A scan gives up and checks the values it hasn't settled one at a time once
# its work passes _BASE_WORK + values x characters / _CHARS_PER_WORK. A
# candidate check is one unit (~0.1 us) and a regex hit _HIT_WORK units. The
# plain loop costs ~0.3 ns per value per character, so a scan never spends
# much more than the plain loop would before switching to it.
_BASE_WORK = 1 << 14
_CHARS_PER_WORK = 256
_HIT_WORK = 8


class _WordChars(dict[str, bool]):
    """Memo of `is_word_char` for the characters one scan looks at."""

    def __missing__(self, ch: str) -> bool:
        self[ch] = result = is_word_char(ch)
        return result


def _whole_token(text: str, start: int, end: int, word: _WordChars) -> bool:
    """`find_token`'s rule for one occurrence ``text[start:end]``.

    It is glued, and doesn't count, when a word character touches it on a
    side where it starts or ends with a word character itself.
    """
    if start > 0 and word[text[start]] and word[text[start - 1]]:
        return False
    return not (end < len(text) and word[text[end - 1]] and word[text[end]])


def _common_prefix_len(a: str, b: str) -> int:
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


class _Skeleton:
    """Builds the skeleton regex and its branch -> values lookup."""

    def __init__(self, values: list[str]) -> None:
        self.values = values  # sorted, unique, non-empty
        self.lookup: dict[str, tuple[str, ...] | _Wide] = {}

    def alternatives(
        self,
        lo: int,
        hi: int,
        depth: int,
        above: tuple[str, ...],
        nesting: int,
        *,
        top: bool = False,
    ) -> list[str]:
        """One regex alternative per child of the node for values[lo:hi].

        Every value in the range starts with ``values[lo][:depth]`` and is
        longer than that. ``above`` holds the shorter values that the node's
        path starts with; they are candidates wherever the path matches.
        """
        values, lookup = self.values, self.lookup
        forced = nesting >= _MAX_NESTING or depth >= _MAX_DEPTH
        alts: list[str] = []
        leaves: list[str] = []  # one-character branch ends, merged into a class
        for i, j in self.children(lo, hi, depth):
            ch = values[i][depth]
            if forced or (
                j - i <= _BUCKET
                and len(values[i]) >= _MIN_PREFIX
                and values[i][:_MIN_PREFIX] == values[j - 1][:_MIN_PREFIX]
            ):
                stop = depth + 1 if forced else max(depth + 1, _MIN_PREFIX)
                lookup[values[i][:stop]] = above + tuple(values[i:j])
                if stop == depth + 1 and not top:
                    leaves.append(ch)
                else:
                    # Top-level alternatives must start with a literal so re
                    # can skip ahead to their first characters.
                    alts.append(re.escape(values[i][depth:stop]))
            else:
                end = _common_prefix_len(values[i], values[j - 1])
                if end > depth + 1:
                    end = min(end, _MAX_DEPTH)
                alts.append(
                    re.escape(values[i][depth:end])
                    + self.node(i, j, end, above, nesting + 1)
                )
        if len(leaves) == 1:
            alts.append(re.escape(leaves[0]))
        elif leaves:
            alts.append("[" + "".join(map(_class_escape, leaves)) + "]")
        return alts

    def children(self, lo: int, hi: int, depth: int) -> list[tuple[int, int]]:
        """Split values[lo:hi] into ranges sharing their character at ``depth``."""
        values = self.values
        path = values[lo][:depth]
        ranges: list[tuple[int, int]] = []
        i = lo
        while i < hi:
            code = ord(values[i][depth])
            j = (
                hi
                if code == _LAST_CODE_POINT
                else bisect_left(values, path + chr(code + 1), i, hi)
            )
            ranges.append((i, j))
            i = j
        return ranges

    def node(
        self, lo: int, hi: int, depth: int, above: tuple[str, ...], nesting: int
    ) -> str:
        """Regex for what may follow the node for values[lo:hi]."""
        values = self.values
        terminal = len(values[lo]) == depth  # a value ends at this node
        if terminal:
            above = (*above, values[lo])
            self.lookup[values[lo]] = above
            lo += 1
            if lo == hi:
                return ""
        children = self.children(lo, hi, depth)
        if len(children) > _MAX_WIDTH:
            table = {values[i][depth]: above + tuple(values[i:j]) for i, j in children}
            self.lookup[values[lo][:depth]] = _Wide(above, table)
            return ""
        alts = self.alternatives(lo, hi, depth, above, nesting)
        body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        return "(?:" + body + ")?" if terminal else body


class _Wide:
    """A skeleton branch end with many children, dispatched by the next character.

    ``above`` holds the values that end at or before the node; ``table`` maps
    each next character to ``above`` plus the values continuing with it.
    """

    __slots__ = ("above", "table")

    def __init__(
        self, above: tuple[str, ...], table: dict[str, tuple[str, ...]]
    ) -> None:
        self.above = above
        self.table = table


def _class_escape(ch: str) -> str:
    return "\\" + ch if ch in "\\]^-[" else ch


class LiteralIndex:
    """Finds every occurrence of many literal strings in a text.

    Build it once for a set of values, then scan any number of texts. The
    time to scan a text is about linear in its length, however many values
    there are. On text built to defeat the skeleton (a value's long prefix
    repeated over and over), a scan gives up once it has done about as much
    work as checking each value on its own would, and finishes that way, so it
    is never much slower than the plain loop.
    """

    __slots__ = ("_lookup", "_regexes", "_values")
    _lookup: dict[str, tuple[str, ...] | _Wide]

    def __init__(self, values: Iterable[str]) -> None:
        """Index ``values``. Empty strings are ignored.

        Raises:
            RecursionError: If the call stack is nearly used up (``re``
                compiles patterns recursively). Callers fall back to checking
                one value at a time.
        """
        ordered = sorted({value for value in values if value})
        skeleton = _Skeleton(ordered)
        top = (
            skeleton.alternatives(0, len(ordered), 0, (), 0, top=True)
            if ordered
            else []
        )
        fanout = max(_MIN_FANOUT, math.isqrt(4 * len(top)))
        self._regexes = tuple(
            re.compile("|".join(top[i : i + fanout]))
            for i in range(0, len(top), fanout)
        )
        self._lookup = skeleton.lookup
        self._values = tuple(ordered)

    def __len__(self) -> int:
        """Return the number of distinct non-empty values indexed."""
        return len(self._values)

    def _budget(self, size: int) -> int:
        """Work a scan of ``size`` characters may do before it gives up."""
        return _BASE_WORK + len(self._values) * size // _CHARS_PER_WORK

    def _candidates(self, text: str) -> Iterator[tuple[int, tuple[str, ...]]]:
        """Yield ``(start, values)`` where some of ``values`` may start.

        Every value that starts at ``start`` is in ``values``; each needs
        confirming with ``text.startswith(value, start)``. Starts ascend for
        values that begin with the same character.
        """
        lookup = self._lookup
        size = len(text)
        for regex in self._regexes:
            search = regex.search
            match = search(text)
            while match is not None:
                start = match.start()
                key = match.group()
                found = lookup[key]
                if isinstance(found, _Wide):
                    after = start + len(key)
                    found = (
                        found.table.get(text[after], found.above)
                        if after < size
                        else found.above
                    )
                yield start, found
                match = search(text, start + 1)

    def present(self, haystacks: Sequence[str], tokens: Set[str]) -> set[str]:
        """Return the values that occur in any of ``haystacks``.

        A value in ``tokens`` only counts where it is a whole token (see
        `find_token`); any other value counts wherever it occurs.
        """
        found: set[str] = set()
        total = len(self._values)
        budget = self._budget(sum(map(len, haystacks)))
        word = _WordChars()
        for text in haystacks:
            for start, values in self._candidates(text):
                budget -= _HIT_WORK + len(values)
                if budget < 0:
                    rest = [value for value in self._values if value not in found]
                    return found | _present_one_by_one(haystacks, rest, tokens)
                for value in values:
                    if value in found or not text.startswith(value, start):
                        continue
                    if value in tokens and not _whole_token(
                        text, start, start + len(value), word
                    ):
                        continue
                    found.add(value)
                if len(found) == total:
                    return found
        return found

    def token_occurrences(self, text: str) -> list[tuple[int, str]]:
        """Return ``(start, value)`` for each whole-token occurrence.

        For each value these are exactly the starts `find_token` yields: not
        overlapping each other. Different values may overlap. Sorted by start,
        longer values first.
        """
        word = _WordChars()
        next_start: dict[str, int] = {}
        found: list[tuple[int, str]] = []
        budget = self._budget(len(text))
        size = len(text)
        for start, values in self._candidates(text):
            budget -= _HIT_WORK
            if budget < 0:
                return _tokens_one_by_one(text, self._values)
            # Every value that starts here starts with text[start]: if that
            # is glued to the character before it, none of them count.
            if start > 0 and word[text[start]] and word[text[start - 1]]:
                continue
            for value in values:
                if start >= next_start.get(value, 0) and text.startswith(value, start):
                    end = start + len(value)
                    if not (end < size and word[text[end - 1]] and word[text[end]]):
                        next_start[value] = end
                        found.append((start, value))
                        continue
                budget -= 1  # only wasted checks count; found ones are output
        found.sort(key=_by_start_longest_first)
        return found


def _by_start_longest_first(pair: tuple[int, str]) -> tuple[int, int]:
    return pair[0], -len(pair[1])


def _present_one_by_one(
    haystacks: Sequence[str], values: Iterable[str], tokens: Set[str]
) -> set[str]:
    if len(haystacks) == 1:
        text = haystacks[0]
        return {
            value
            for value in values
            if (contains_token(text, value) if value in tokens else value in text)
        }
    return {
        value
        for value in values
        if any(
            contains_token(text, value) if value in tokens else value in text
            for text in haystacks
        )
    }


def _tokens_one_by_one(text: str, values: Iterable[str]) -> list[tuple[int, str]]:
    pairs = [(start, value) for value in values for start in find_token(text, value)]
    pairs.sort(key=_by_start_longest_first)
    return pairs


# CachedIndex builds an index only when it pays off; below these sizes
# (measured) checking each value on its own is as fast or faster.
_INDEX_MIN_VALUES = 64
_INDEX_MIN_TEXT = 2048
_INDEX_MIN_WORK = 1 << 20  # values x characters


def _worth_indexing(count: int, size: int) -> bool:
    """Whether building an index for one scan beats checking each value."""
    return (
        count >= _INDEX_MIN_VALUES
        and size >= _INDEX_MIN_TEXT
        and count * size >= _INDEX_MIN_WORK
    )


# CachedIndex rebuilds once checking the values missing from its index one at
# a time has cost about this many characters of scanning per indexed value
# (building costs roughly that much per value).
_REBUILD_WORK = 1 << 13
# What checking one value costs besides its scan, in characters.
_VALUE_WORK = 256


class CachedIndex:
    """A `LiteralIndex` kept from call to call for a set of values that grows.

    The leak check looks for every known value in every masked text, and the
    known values mostly carry over from one call to the next. This keeps one
    index over the values it last saw and checks newer values one at a time,
    until that has cost about as much as rebuilding the index ("ski rental").
    Results never depend on what is cached: indexed values that are no longer
    asked about are ignored, and values that aren't indexed are checked
    directly.
    """

    __slots__ = ("_debt", "_index", "_indexed")

    def __init__(self) -> None:
        """Create an empty cache."""
        self._index: LiteralIndex | None = None
        self._indexed: frozenset[str] = frozenset()
        self._debt = 0

    def clear(self) -> None:
        """Drop the index."""
        self._index = None
        self._indexed = frozenset()
        self._debt = 0

    def present(
        self,
        haystacks: Sequence[str],
        values: Set[str],
        tokens: Set[str],
        *,
        update: bool = True,
    ) -> set[str]:
        """Return the ``values`` that occur in any of ``haystacks``.

        Same rules as `find_present`. With ``update=False`` the cache is only
        read, never rebuilt.
        """
        size = sum(map(len, haystacks))
        if update and self._index is not None and 2 * len(values) < len(self._indexed):
            self.clear()  # most indexed values are gone: the vault was cleared
        index = self._index
        if index is None:
            if not (update and _worth_indexing(len(values), size)):
                return _present_one_by_one(haystacks, values, tokens)
            rest = self._rebuild(values)
        else:
            indexed = self._indexed
            rest = [value for value in values if value not in indexed]
            if update and rest:
                self._debt += len(rest) * (size + _VALUE_WORK)
                if self._debt > len(values) * _REBUILD_WORK:
                    rest = self._rebuild(values)
        index = self._index
        if index is None:  # the build failed (RecursionError)
            return _present_one_by_one(haystacks, values, tokens)
        found = {value for value in index.present(haystacks, tokens) if value in values}
        if rest:
            found |= _present_one_by_one(haystacks, rest, tokens)
        return found

    def _rebuild(self, values: Set[str]) -> list[str]:
        """Index ``values``; return the ones left to check one at a time."""
        self.clear()
        try:
            self._index = LiteralIndex(values)
        except RecursionError:
            return list(values)
        self._indexed = frozenset(value for value in values if value)
        return [""] if "" in values else []
