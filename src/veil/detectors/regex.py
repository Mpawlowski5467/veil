"""Regex detection for emails, phone numbers, IPv4 addresses, and custom patterns.

The built-in patterns are pragmatic, not exhaustive: they aim to catch the
formats people actually type while rejecting look-alikes such as dates, version
numbers, and long digit runs. See the README's "Limitations" section.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import ClassVar, TypeAlias

from .._text import UNSPACED_CLASS
from ..placeholders import validate_entity_type
from ..types import Span

PatternLike: TypeAlias = str | re.Pattern[str]

# A character that can start or continue a run in an email's local part: any
# letter or digit (so "łucja@example.com" works, per RFC 6531) except those
# from scripts written without spaces, plus % + - and underscore. Excluding
# CJK keeps "連絡先はjan@example.com" from pulling the Japanese into the match.
_LOCAL = rf"(?:[^\W{UNSPACED_CLASS}]|[%+-])"
# Separators allowed *between* runs: dots, plus the RFC 5322 characters that
# show up in real addresses (o'brien@, VERP bounces with =, jan&anna@).
_LOCAL_SEP = r"[.'=&/]"

# Pragmatic address matching, not full RFC 5322. The local part has no
# leading, trailing, or doubled separators, so sentence punctuation, quotes,
# and ellipses stay outside the match. The lookbehinds only let a match start
# at the beginning of a run of local-part characters: starting halfway through
# ("o'[EMAIL_1]") would leave part of the address unmasked, and retrying at
# every dot of a long dotted string would take quadratic time. The domain
# must be ASCII; punycode (xn--) labels and TLDs are accepted.
EMAIL_PATTERN = re.compile(
    rf"""
    (?<![^\W{UNSPACED_CLASS}])(?<![%+-])
    (?<![^\W{UNSPACED_CLASS}]{_LOCAL_SEP})(?<![%+-]{_LOCAL_SEP})
    {_LOCAL}+(?:{_LOCAL_SEP}{_LOCAL}+)*
    @
    (?:[A-Za-z0-9](?:[A-Za-z0-9-]{{0,61}}[A-Za-z0-9])?\.)+
    (?:xn--[A-Za-z0-9-]{{0,58}}[A-Za-z0-9]|[A-Za-z]{{2,63}})
    (?![A-Za-z0-9])
    """,
    re.VERBOSE,
)

# Phone numbers and IPv4 addresses must not be glued to ASCII letters or to
# digits ("tel555-123-4567", "v1.2.3.4"), but may touch any other script:
# Chinese and Japanese put no spaces around numbers ("電話は555-123-4567です").
_EXTENSION = r"(?:[ ]?(?i:ext\.?|x)[ ]?\d{1,6})"

# North American numbers. Separators are required between the groups, so bare
# ten-digit runs (order numbers, IDs) are not matched; use a leading "+" for
# unseparated numbers.
US_PHONE_PATTERN = re.compile(
    rf"""
    (?<![A-Za-z\d+])
    (?:\+?1[ .-]?)?                        # optional country code
    (?:\(\d{{3}}\)[ .-]?|\d{{3}}[ .-])         # area code: (555) or 555-
    \d{{3}}[ .-]\d{{4}}                        # exchange and line number
    {_EXTENSION}?
    (?![A-Za-z\d]|[.-]\d)
    """,
    re.VERBOSE,
)

# International numbers must start with a "+" country code, optionally in
# parentheses: "+44 20 7946 0958", "(+48) 123 456 789". Digit groups may be
# separated by spaces, dots, or dashes, and may be in parentheses, e.g.
# "+49 (0)30 1234567". The digit groups are captured inside a lookahead and
# then consumed with a backreference, which makes them atomic: if the number
# is followed by a letter, the engine can't back off to a shorter prefix and
# leave the last group unmasked, and it can't try every split of a long digit
# run (catastrophic backtracking). The digit count is checked by `_trim_phone`.
INTL_PHONE_PATTERN = re.compile(
    rf"""
    (?<![A-Za-z\d+])
    (?=(?P<number>
        (?:\(\+[1-9]\d{{0,2}}\)|\+[1-9]\d{{0,2}})    # country code
        (?:[ .-]?(?:\(\d{{1,4}}\)|\d{{1,4}})){{1,8}}  # digit groups
    ))(?P=number)
    {_EXTENSION}?
    (?![A-Za-z\d]|[.-]\d)
    """,
    re.VERBOSE,
)

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"

# Dotted-quad IPv4 with octets 0-255 and no leading zeros. The lookarounds keep
# it from matching part of a longer dotted run such as "1.2.3.4.5".
IPV4_PATTERN = re.compile(
    rf"(?<![A-Za-z\d])(?<!\d\.)(?:{_OCTET}\.){{3}}{_OCTET}(?![A-Za-z\d]|\.\d)"
)

#: E.164 allows at most 15 digits; fewer than 8 is almost never a full number.
MIN_PHONE_DIGITS = 8
MAX_PHONE_DIGITS = 15

_PHONE_SEPARATORS = " .-"


def _trim_phone(value: str) -> str | None:
    """Return the longest group-aligned prefix with a plausible digit count.

    The pattern is greedy, so a number followed by more digit groups (a table
    row, a date) can match too many digits. Dropping trailing groups keeps the
    real number instead of rejecting the whole match.
    """
    while True:
        digits = sum(ch.isdecimal() for ch in value)
        if digits <= MAX_PHONE_DIGITS:
            return value if digits >= MIN_PHONE_DIGITS else None
        cut = max(value.rfind(sep) for sep in _PHONE_SEPARATORS)
        if cut <= 0:
            return None
        value = value[:cut]


def _intl_phone_end(match: re.Match[str]) -> int | None:
    number = match["number"]
    kept = _trim_phone(number)
    if kept is None:
        return None
    if len(kept) < len(number):  # trimmed, so any extension belonged elsewhere
        return match.start() + len(kept)
    return match.end()


def _shrink_phone(value: str, limit: int) -> str | None:
    """Cut ``value`` back to a group boundary at or before offset ``limit``."""
    head = value[:limit]
    cut = max(head.rfind(sep) for sep in _PHONE_SEPARATORS)
    return _trim_phone(head[:cut]) if cut > 0 else None


@dataclass(frozen=True, slots=True)
class _Rule:
    entity_type: str
    pattern: re.Pattern[str]
    priority: int
    # Optionally shortens or rejects a raw match: returns the end offset to
    # keep, or None to drop the match.
    refine: Callable[[re.Match[str]], int | None] | None = None
    # Optionally shortens a span so it stops before a neighbouring span that
    # it runs into: returns the shortened value, or None if it can't be.
    shrink: Callable[[str, int], str | None] | None = None


class RegexDetector:
    r"""Detects emails, phone numbers, IPv4 addresses, and custom patterns.

    Built-in entity types are ``EMAIL``, ``PHONE``, and ``IPV4``. Custom
    patterns add new types, or replace a built-in type when they reuse its
    name. On a tie between spans of equal length, custom patterns win over
    built-in ones.

    Example:
        >>> detector = RegexDetector(custom_patterns={"ORDER": r"#\d{5}"})
        >>> [(s.value, s.entity_type) for s in detector.detect("Order #12345")]
        [('#12345', 'ORDER')]
    """

    #: Tie-break priority of spans from built-in patterns.
    BUILTIN_PRIORITY: ClassVar[int] = 0
    #: Tie-break priority of spans from custom patterns.
    CUSTOM_PRIORITY: ClassVar[int] = 10

    def __init__(
        self,
        custom_patterns: Mapping[str, PatternLike] | None = None,
        *,
        include_builtins: bool = True,
    ) -> None:
        """Create a detector.

        Args:
            custom_patterns: Maps an entity type to a regex (string or compiled
                pattern). The whole match (group 0) becomes the span. A type
                named like a built-in (e.g. ``"PHONE"``) replaces it.
            include_builtins: Set to ``False`` to use only ``custom_patterns``.

        Raises:
            ValueError: If an entity type name is invalid or a pattern string
                does not compile.
            TypeError: If a pattern is neither a string nor a compiled pattern.
        """
        custom = dict(custom_patterns or {})
        rules: list[_Rule] = []
        if include_builtins:
            rules.extend(r for r in _BUILTIN_RULES if r.entity_type not in custom)
        for entity_type, pattern in custom.items():
            validate_entity_type(entity_type)
            rules.append(
                _Rule(entity_type, _compile(entity_type, pattern), self.CUSTOM_PRIORITY)
            )
        self._rules = tuple(rules)

    @property
    def entity_types(self) -> tuple[str, ...]:
        """The entity types this detector can report, in rule order."""
        return tuple(dict.fromkeys(rule.entity_type for rule in self._rules))

    def detect(self, text: str) -> list[Span]:
        """Return every match of every pattern, sorted by position.

        Spans may overlap (for example when a custom pattern matches inside an
        email address). Empty matches are ignored, and identical spans of the
        same type are reported once.
        """
        # Keyed by (start, end, type) so two rules for the same type that match
        # the same text (e.g. US and international phone patterns on
        # "+1 555 123 4567") produce one span. The first rule wins.
        found: dict[tuple[int, int, str], tuple[Span, _Rule]] = {}
        for rule in self._rules:
            for match in rule.pattern.finditer(text):
                start = match.start()
                end = match.end() if rule.refine is None else rule.refine(match)
                if end is None or end <= start:
                    continue
                span = Span(
                    start=start,
                    end=end,
                    value=text[start:end],
                    entity_type=rule.entity_type,
                    source="regex",
                    priority=rule.priority,
                )
                found.setdefault((start, end, rule.entity_type), (span, rule))

        # A greedy international number can run into the next value, as in
        # "+44 20 7946 0958 555-123-4567". The longer span would win the
        # overlap and leave the rest of the second number unmasked, so cut the
        # first one back to end before the span it runs into.
        entries = list(found.values())
        result: dict[tuple[int, int, str], Span] = {}
        for span, rule in entries:
            if rule.shrink is not None:
                limit = min(
                    (
                        other.start
                        for other, _ in entries
                        if span.start < other.start < span.end < other.end
                    ),
                    default=None,
                )
                value = (
                    None
                    if limit is None
                    else rule.shrink(span.value, limit - span.start)
                )
                if value:
                    span = Span(
                        start=span.start,
                        end=span.start + len(value),
                        value=value,
                        entity_type=span.entity_type,
                        source=span.source,
                        priority=span.priority,
                    )
            result.setdefault((span.start, span.end, span.entity_type), span)
        return sorted(result.values(), key=lambda s: (s.start, -len(s)))

    def __repr__(self) -> str:
        """Show which entity types the detector reports."""
        return f"{type(self).__name__}(entity_types={self.entity_types!r})"


def _compile(entity_type: str, pattern: PatternLike) -> re.Pattern[str]:
    if isinstance(pattern, re.Pattern):
        if not isinstance(pattern.pattern, str):
            raise TypeError(f"Pattern for {entity_type} must match str, not bytes")
        return pattern
    if isinstance(pattern, str):
        try:
            return re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Invalid regex for {entity_type}: {exc}") from exc
    raise TypeError(
        f"Pattern for {entity_type} must be a str or re.Pattern, "
        f"got {type(pattern).__name__}"
    )


_BUILTIN_RULES: tuple[_Rule, ...] = (
    _Rule("EMAIL", EMAIL_PATTERN, RegexDetector.BUILTIN_PRIORITY),
    _Rule("PHONE", US_PHONE_PATTERN, RegexDetector.BUILTIN_PRIORITY),
    _Rule(
        "PHONE",
        INTL_PHONE_PATTERN,
        RegexDetector.BUILTIN_PRIORITY,
        refine=_intl_phone_end,
        shrink=_shrink_phone,
    ),
    _Rule("IPV4", IPV4_PATTERN, RegexDetector.BUILTIN_PRIORITY),
)
