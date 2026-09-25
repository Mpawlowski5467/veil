"""Word-boundary helpers shared by the detectors and the leak check."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator

# Scripts normally written without spaces between words. Word boundaries can't
# be seen in them, so a character from one of these ranges never counts as
# "part of the same word" as its neighbour.
UNSPACED_RANGES: tuple[tuple[int, int], ...] = (
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x1000, 0x109F),  # Myanmar
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x1780, 0x17FF),  # Khmer
    (0x2E80, 0x2FDF),  # CJK radicals, Kangxi radicals
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x3130, 0x318F),  # Hangul compatibility Jamo
    (0x31F0, 0x31FF),  # Katakana phonetic extensions
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xA960, 0xA97F),  # Hangul Jamo Extended-A
    (0xAC00, 0xD7FF),  # Hangul syllables, Hangul Jamo Extended-B
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF66, 0xFFDC),  # Halfwidth Katakana and Hangul
    (0x20000, 0x3134F),  # CJK Unified Ideographs Extensions B-G
)


def _ranges(codepoints: Iterable[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for code in codepoints:
        if ranges and ranges[-1][1] == code - 1:
            ranges[-1] = (ranges[-1][0], code)
        else:
            ranges.append((code, code))
    return ranges


def _class_body(ranges: Iterable[tuple[int, int]]) -> str:
    return "".join(
        re.escape(chr(lo)) + (f"-{re.escape(chr(hi))}" if hi > lo else "")
        for lo, hi in ranges
    )


#: The unspaced-script ranges as the body of a regex character class.
UNSPACED_CLASS = _class_body(UNSPACED_RANGES)

#: Combining marks (accents in decomposed text, Indic vowel signs, ...) as the
#: body of a regex character class. Python's ``\w`` does not include them, but
#: they belong to the letter they follow. Built from the running Python's
#: Unicode database; takes a few milliseconds at import.
MARK_CLASS = _class_body(
    _ranges(c for c in range(0x0300, 0x20000) if unicodedata.category(chr(c))[0] == "M")
)

#: Regex for one character that continues a word in a space-separated script:
#: a letter, digit, or combining mark, outside unspaced scripts. Underscore
#: doesn't count, so a name in Markdown emphasis ("_Jan_") is still found.
WORD_CHAR = rf"(?![{UNSPACED_CLASS}_])[\w{MARK_CLASS}]"

_WORD_CHAR_RE = re.compile(WORD_CHAR)
_NON_WORD_CHAR_RE = re.compile(rf"(?!{WORD_CHAR})[\s\S]")


def is_word_char(ch: str) -> bool:
    """Return whether ``ch`` can continue a word in a space-separated script.

    Letters, digits, and combining marks (so an accent or a Devanagari vowel
    sign stays attached to its letter) count. Underscore and characters from
    scripts written without spaces (CJK, Thai, and others) do not.
    """
    return _WORD_CHAR_RE.fullmatch(ch) is not None


def find_token(text: str, value: str) -> Iterator[int]:
    """Yield the start of each occurrence of ``value`` as a whole token.

    Matching is exact and case-sensitive, and occurrences don't overlap. An
    occurrence is skipped when it is glued to a word character (see
    `is_word_char`) on a side where ``value`` itself starts or ends with one,
    so ``"Jan"`` is found in ``"Jan's"`` and ``"(Jan)"`` but not in
    ``"January"``.
    """
    if not value:
        return
    guard_start = is_word_char(value[0])
    guard_end = is_word_char(value[-1])
    size = len(value)
    pos = text.find(value)
    while pos != -1:
        if guard_start and pos > 0 and _WORD_CHAR_RE.match(text, pos - 1):
            # Every later start inside this word is glued as well: skip to the
            # first position after the next non-word character.
            gap = _NON_WORD_CHAR_RE.search(text, pos)
            if gap is None:
                return
            pos = text.find(value, gap.end())
            continue
        end = pos + size
        if guard_end and _WORD_CHAR_RE.match(text, end):
            pos = text.find(value, pos + 1)
            continue
        yield pos
        pos = text.find(value, end)


def contains_token(text: str, value: str) -> bool:
    """Return whether ``value`` occurs in ``text`` as a whole token."""
    return next(find_token(text, value), None) is not None
