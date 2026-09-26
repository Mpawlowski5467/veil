"""Regex detection for emails, phone numbers, IPv4 addresses, and custom patterns.

The built-in patterns are pragmatic, not exhaustive: they aim to catch the
formats people actually type while rejecting look-alikes such as dates, version
numbers, and long digit runs. See the README's "Limitations" section.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import ClassVar, TypeAlias

from .._text import MARK_CLASS, UNSPACED_CLASS
from ..placeholders import validate_entity_type
from ..types import Span

PatternLike: TypeAlias = str | re.Pattern[str]

# A character that can start or continue a run in an email's local part: any
# letter, digit, or combining mark (so "łucja@", "josé@" in decomposed form,
# and "राम@" work, per RFC 6531), plus % + - and underscore.
_LOCAL_CHAR = rf"[\w%+\-{MARK_CLASS}]"
# Separators allowed *between* runs: dots, plus RFC 5322 characters that show
# up in real addresses (o'brien@ with either apostrophe, VERP bounces with =,
# jan&anna@). Characters people glue addresses to (` { | } ? and Markdown's *)
# are left out: "note*jan@example.com" must not absorb "note".
_LOCAL_SEP = r"[.'\u2019=&/!#$^~]"

# Pragmatic address matching, not full RFC 5322. The local part has no
# leading, trailing, or doubled separators, so sentence punctuation, quotes,
# and ellipses stay outside the match. The lookahead is a cheap filter: a
# local part is at most 64 characters (RFC 5321; 128 here to allow for a glued
# key=), so a start with no "@" within reach is skipped at once, which keeps
# long dotted or dashed strings linear. The domain must be ASCII; punycode
# (xn--) labels and TLDs are accepted.
_EMAIL_BODY = rf"""
    (?=[^\s@]{{1,128}}@)
    {_LOCAL_CHAR}+(?:{_LOCAL_SEP}{_LOCAL_CHAR}+)*
    @
    (?:[A-Za-z0-9](?:[A-Za-z0-9-]{{0,61}}[A-Za-z0-9])?\.)+
    (?:(?i:xn--)[A-Za-z0-9-]{{0,58}}[A-Za-z0-9]|[A-Za-z]{{2,63}})
    (?![A-Za-z0-9])
"""

# A match starts at the beginning of a run of local-part characters (starting
# halfway through would leave the front of the address visible), or where a
# script written without spaces meets another one ("…ทางอีเมลjan@example.com":
# the Thai run may be too long for the lookahead, and there is no space to
# tell where the address begins).
EMAIL_PATTERN = re.compile(
    rf"(?:(?<!{_LOCAL_CHAR})|(?<=[{UNSPACED_CLASS}])(?![{UNSPACED_CLASS}])){_EMAIL_BODY}",
    re.VERBOSE,
)
# The same without the start rule, to continue right after an address that
# another one is glued to ("…com+anna@…", "…com%20anna@…").
_EMAIL_CONTINUATION = re.compile(_EMAIL_BODY, re.VERBOSE)
_ADDRESS_RUN = re.compile(rf"(?:{_LOCAL_CHAR}|{_LOCAL_SEP})*")
# An address ends at most this far past its "@" (253-character domain).
_DOMAIN_REACH = 256


def _run_end(text: str, pos: int) -> int:
    run = _ADDRESS_RUN.match(text, pos)  # always matches, possibly empty
    return run.end() if run else pos


def _find_emails(text: str) -> Iterator[re.Match[str]]:
    pos = 0
    while (match := EMAIL_PATTERN.search(text, pos)) is not None:
        yield match
        pos = match.end()
        # The next address may start inside the run of address characters
        # that this one ends, where the start rule never allows a match. If
        # no normal match starts in that run (as the tighter "anna@..." after
        # a CJK word does), continue right here instead.
        while (run_end := _run_end(text, pos)) > pos:
            reach = min(len(text), run_end + _DOMAIN_REACH)
            tight = EMAIL_PATTERN.search(text, pos, reach)
            if tight is not None and tight.start() < run_end:
                break
            glued = _EMAIL_CONTINUATION.match(text, pos)
            if glued is None:
                break
            yield glued
            pos = glued.end()


# Phone numbers and IPv4 addresses must not be glued to ASCII letters or to
# digits ("tel555-123-4567", "v1.2.3.4"), but may touch any other script:
# Chinese and Japanese put no spaces around numbers ("電話は555-123-4567です").
# Digit groups may be separated by a space (including the no-break spaces
# common in HTML and French text), a dot, or a dash (including en dash and
# the non-breaking hyphen).
_SPACES = " \u00a0\u2009\u202f"
_PHONE_SEPARATORS = _SPACES + ".-\u2011\u2013"
_SEP = r"[ \u00a0\u2009\u202f.\-\u2011\u2013]"
_DASH_DOT = r"[.\-\u2011\u2013]"
_EXTENSION = r"(?:[ \u00a0]?(?i:ext\.?|x)[ \u00a0]?\d{1,6})"

# North American numbers. Separators are required between the groups, so bare
# ten-digit runs (order numbers, IDs) are not matched; use a leading "+" for
# unseparated numbers.
US_PHONE_PATTERN = re.compile(
    rf"""
    (?:
        (?<![A-Za-z\d+])
        (?:(?<!\d\.)\+?1{_SEP}?)?           # country code, but not the last
                                            # octet of "192.0.2.1 555..."
        (?:\(\d{{3}}\){_SEP}?|\d{{3}}{_SEP})    # area code: (555) or 555-
      | (?<![\d+])\(\d{{3}}\){_SEP}?          # "(555)" glued to a label: Tel(555)
    )
    \d{{3}}{_SEP}\d{{4}}                     # exchange and line number
    {_EXTENSION}?
    (?![A-Za-z\d]|{_DASH_DOT}\d)
    """,
    re.VERBOSE,
)

# International numbers start with a "+" country code, optionally in
# parentheses: "+44 20 7946 0958", "(+48) 123 456 789". Digit groups may be in
# parentheses too, e.g. "+49 (0)30 1234567". The groups are captured inside a
# lookahead and consumed with a backreference, which makes the run atomic so a
# long digit run can't backtrack catastrophically. The capture is greedy and
# may run into the next token; `_intl_phone_end` decides where the number
# really ends.
INTL_PHONE_PATTERN = re.compile(
    rf"""
    (?=(?P<number>
        (?:(?<![\d+])\(\+[1-9]\d{{0,2}}\)|(?<![A-Za-z\d+])\+[1-9]\d{{0,2}})
        (?:{_SEP}?(?:\(\d{{1,4}}\)|\d{{1,4}})){{1,15}}
    ))(?P=number)
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

_CLEAN_END = re.compile(rf"(?![A-Za-z\d]|{_DASH_DOT}\d)")
_GLUED = re.compile(r"[A-Za-z\d]")
_EXTENSION_RE = re.compile(_EXTENSION)


def _plausible(number: str) -> bool:
    # A "(0)" trunk prefix ("+49 (0)711 ...") is not dialled from abroad, so it
    # doesn't count toward the E.164 limit.
    digits = sum(ch.isdecimal() for ch in number) - number.count("(0)")
    return MIN_PHONE_DIGITS <= digits <= MAX_PHONE_DIGITS


def _intl_phone_end(match: re.Match[str]) -> int | None:
    """Pick where an international number ends, or reject it.

    In order of preference:

    1. The whole captured run, if it has 8-15 digits and ends cleanly (an
       extension like "x12" may follow).
    2. If the run ends in a short group glued to a word, as in "0958 24h" or
       "0958 9am", the number stops before that word.
    3. Otherwise the longest group-aligned prefix with 8-15 digits, whatever
       follows it: "+49 (0)711 1234567-890" masks the base number. Masking a
       little too much (the "2024" of a date right after a number) is safer
       than leaving digits of the number visible.
    """
    text, start, number = match.string, match.start(), match["number"]
    end = start + len(number)
    if _plausible(number):
        extension = _EXTENSION_RE.match(text, end)
        if extension and _CLEAN_END.match(text, extension.end()):
            return extension.end()
        if _CLEAN_END.match(text, end):
            return end
    if _GLUED.match(text, end):
        space = max(number.rfind(ch) for ch in _SPACES)
        tail = number[space + 1 :]
        if (
            space > 0
            and len(tail) <= 2
            and tail.isdecimal()
            and _plausible(number[:space])
        ):
            return start + space
    for cut in range(len(number), 0, -1):
        at_boundary = cut == len(number) or (
            number[cut] in _PHONE_SEPARATORS + "("
            and number[cut - 1] not in _PHONE_SEPARATORS
        )
        if at_boundary and _plausible(number[:cut]):
            return start + cut
    return None


def _shrink_phone(value: str, limit: int) -> str | None:
    """Cut ``value`` back to end before offset ``limit``, at a group boundary.

    Only shrinks when ``limit`` is itself at a group boundary (just after a
    separator). If the other span starts mid-group, shrinking would strand the
    digits before it, so the span is left alone and the masker's overlap
    handling (and partial-mask warning) takes over.
    """
    head = value[:limit]
    kept = head.rstrip(_PHONE_SEPARATORS)
    if len(kept) == len(head):
        return None
    return kept if _plausible(kept) else None


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
    # Optionally replaces pattern.finditer for finding raw matches.
    finder: Callable[[str], Iterable[re.Match[str]]] | None = None


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
            matches = rule.finder(text) if rule.finder else rule.pattern.finditer(text)
            for match in matches:
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
        entries = sorted(found.values(), key=lambda entry: entry[0].start)
        starts = [span.start for span, _ in entries]
        result: dict[tuple[int, int, str], Span] = {}
        for span, rule in entries:
            if rule.shrink is not None:
                i = bisect.bisect_right(starts, span.start)
                while i < len(entries) and entries[i][0].start < span.end:
                    other = entries[i][0]
                    if other.end > span.end:
                        value = rule.shrink(span.value, other.start - span.start)
                        if value:
                            span = Span(
                                start=span.start,
                                end=span.start + len(value),
                                value=value,
                                entity_type=span.entity_type,
                                source=span.source,
                                priority=span.priority,
                            )
                        break
                    i += 1
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
    _Rule("EMAIL", EMAIL_PATTERN, RegexDetector.BUILTIN_PRIORITY, finder=_find_emails),
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
