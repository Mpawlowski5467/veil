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

from ..placeholders import validate_entity_type
from ..types import Span

PatternLike: TypeAlias = "str | re.Pattern[str]"

# Pragmatic address matching, not full RFC 5322. The local part is dot-atom
# style (no leading, trailing, or doubled dots), so sentence punctuation and
# ellipses stay outside the match. It accepts Unicode letters (RFC 6531, e.g.
# "łucja@example.com"); the domain must be ASCII (use punycode for IDNs).
EMAIL_PATTERN = re.compile(
    r"""
    (?<![\w%+-])
    [\w%+-]+(?:\.[\w%+-]+)*
    @
    (?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+
    [A-Za-z]{2,63}
    (?![A-Za-z0-9])
    """,
    re.VERBOSE,
)

# North American numbers. Separators are required between the groups, so bare
# ten-digit runs (order numbers, IDs) are not matched; use a leading "+" for
# unseparated numbers.
US_PHONE_PATTERN = re.compile(
    r"""
    (?<![\w+])
    (?:\+?1[ .-]?)?                        # optional country code
    (?:\(\d{3}\)[ .-]?|\d{3}[ .-])         # area code: (555) or 555-
    \d{3}[ .-]\d{4}                        # exchange and line number
    (?:[ ]?(?i:ext\.?|x)[ ]?\d{1,6})?      # optional extension
    (?!\w|[.-]\d)
    """,
    re.VERBOSE,
)

# International numbers must start with "+" and a country code. Digit groups
# may be separated by spaces, dots, or dashes, and may be in parentheses, e.g.
# "+49 (0)30 1234567". The digit count is checked by `_trim_phone`.
INTL_PHONE_PATTERN = re.compile(
    r"""
    (?<![\w+])
    \+[1-9]\d{0,2}                         # country code
    (?:[ .-]?(?:\(\d{1,4}\)|\d{1,4})){1,8}   # digit groups
    (?!\w|[.-]\d)
    """,
    re.VERBOSE,
)

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"

# Dotted-quad IPv4 with octets 0-255 and no leading zeros. The lookarounds keep
# it from matching part of a longer dotted run such as "1.2.3.4.5".
IPV4_PATTERN = re.compile(rf"(?<!\w)(?<!\d\.)(?:{_OCTET}\.){{3}}{_OCTET}(?!\w|\.\d)")

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


@dataclass(frozen=True, slots=True)
class _Rule:
    entity_type: str
    pattern: re.Pattern[str]
    priority: int
    # Optionally shortens or rejects a raw match: returns a prefix of the
    # matched text to keep, or None to drop the match.
    refine: Callable[[str], str | None] | None = None


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
        found: dict[tuple[int, int, str], Span] = {}
        for rule in self._rules:
            for match in rule.pattern.finditer(text):
                value: str | None = match.group(0)
                if rule.refine is not None and value:
                    value = rule.refine(value)
                if not value:
                    continue
                start = match.start()
                key = (start, start + len(value), rule.entity_type)
                if key not in found:
                    found[key] = Span(
                        start=start,
                        end=start + len(value),
                        value=value,
                        entity_type=rule.entity_type,
                        source="regex",
                        priority=rule.priority,
                    )
        return sorted(found.values(), key=lambda s: (s.start, -len(s)))

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
    _Rule("PHONE", INTL_PHONE_PATTERN, RegexDetector.BUILTIN_PRIORITY, _trim_phone),
    _Rule("IPV4", IPV4_PATTERN, RegexDetector.BUILTIN_PRIORITY),
)
