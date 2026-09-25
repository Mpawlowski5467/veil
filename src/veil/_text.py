"""Word-boundary helpers shared by the detectors and the leak check."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator

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

#: The ranges above as the body of a regex character class, e.g. ``[^\W...]``.
UNSPACED_CLASS = "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in UNSPACED_RANGES)


def is_word_char(ch: str) -> bool:
    """Return whether ``ch`` can continue a word in a space-separated script.

    Letters, digits, combining marks (so an accent or a Devanagari vowel sign
    stays attached to its letter), and underscore count. Characters from
    scripts written without spaces (CJK, Thai, and others) do not.
    """
    if ch == "_":
        return True
    if unicodedata.category(ch)[0] not in "LMN":
        return False
    code = ord(ch)
    return not any(lo <= code <= hi for lo, hi in UNSPACED_RANGES)


def find_token(text: str, value: str) -> Iterator[int]:
    """Yield the start of every occurrence of ``value`` as a whole token.

    Matching is exact and case-sensitive. An occurrence is skipped when it is
    glued to a word character (see `is_word_char`) on a side where ``value``
    itself starts or ends with one, so ``"Jan"`` is found in ``"Jan's"`` and
    ``"(Jan)"`` but not in ``"January"``. Overlapping occurrences are all
    reported.
    """
    if not value:
        return
    guard_start = is_word_char(value[0])
    guard_end = is_word_char(value[-1])
    size = len(value)
    start = text.find(value)
    while start != -1:
        end = start + size
        glued_before = guard_start and start > 0 and is_word_char(text[start - 1])
        glued_after = guard_end and end < len(text) and is_word_char(text[end])
        if not glued_before and not glued_after:
            yield start
        start = text.find(value, start + 1)


def contains_token(text: str, value: str) -> bool:
    """Return whether ``value`` occurs in ``text`` as a whole token."""
    return next(find_token(text, value), None) is not None
