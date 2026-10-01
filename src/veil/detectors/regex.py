"""Regex detection for personal data and custom patterns.

The built-in patterns are pragmatic, not exhaustive: they aim to catch the
formats people actually type while rejecting look-alikes such as dates, version
numbers, and long digit runs. See the README's "Limitations" section.
"""

from __future__ import annotations

import bisect
import ipaddress
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import ClassVar, TypeAlias

from .._text import MARK_CLASS, UNSPACED_CLASS
from ..placeholders import validate_entity_type
from ..types import Span
from . import _secrets

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
# Not right after "::" or after a group of hex digits between colons: the "1"
# that follows ends an IPv6 address ("fe80::1", ":0:1"), but "Phone:1" doesn't.
_NOT_AFTER_IPV6_GROUP = "(?<!::)" + "".join(
    f"(?<!:[0-9A-Fa-f]{{{size}}}:)" for size in range(1, 5)
)

# North American numbers. Separators are required between the groups, so bare
# ten-digit runs (order numbers, IDs) are not matched; use a leading "+" for
# unseparated numbers.
US_PHONE_PATTERN = re.compile(
    rf"""
    (?:
        (?<![A-Za-z\d+])
        (?:(?<!\d\.){_NOT_AFTER_IPV6_GROUP}  # country code, but not the last
            \+?1{_SEP}?)?                   # group of "192.0.2.1 555..." or
                                            # of "::1 555..."
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
    digits before it, so the span is left alone and the masker masks the two
    overlapping matches together.
    """
    head = value[:limit]
    kept = head.rstrip(_PHONE_SEPARATORS)
    if len(kept) == len(head):
        return None
    return kept if _plausible(kept) else None


# IPv6 candidates are whole runs of hex digits, colons, and dots (for the
# IPv4-mapped form "::ffff:192.0.2.1"), captured atomically; `_ipv6_end`
# validates them with the standard library.
IPV6_PATTERN = re.compile(r"(?<![0-9A-Za-z:.])(?=(?P<run>[0-9A-Fa-f:.]{3,}))(?P=run)")
# An address written straight after a label and a colon ("[IPv6:2001:db8::1]"
# in mail headers, "ip:2001:db8::1" in logs) starts inside a colon run, where
# the pattern above never starts; this one steps over the label.
_IPV6_AFTER_LABEL = re.compile(
    r"(?<![0-9A-Za-z_.:-])[A-Za-z][A-Za-z0-9_-]{0,30}:(?=(?P<run>[0-9A-Fa-f:.]{3,}))"
)
# A port after an address without brackets: "2001:db8::1:54321", or the
# dotted form tcpdump and netstat print, "2001:db8::1.443".
_IPV6_PORT = re.compile(r"[.:][0-9]{1,5}[.:]*\Z")


# Anything shaped like an IPv4 address, for `_find_us_phones`: looser than
# IPV4_PATTERN, so a zero-padded octet, a glued letter, or a ".port" after it
# (tcpdump's "198.51.100.123.2222") still counts. A dot and a group with a
# leading zero is not a port, so "1.1.212.200.0123" is a list item and a number.
_PADDED_OCTET = r"(?:25[0-5]|2[0-4]\d|[01]?\d?\d)"
_DOTTED_QUAD = re.compile(
    rf"(?<![\d.]){_PADDED_OCTET}(?:\.{_PADDED_OCTET}){{3}}"
    r"(?=\.[1-9]\d{0,4}(?!\d|\.\d)|(?!\d|\.\d))"
)


def _find_us_phones(text: str) -> Iterator[re.Match[str]]:
    """Yield `US_PHONE_PATTERN` matches that don't start inside an IPv4 address.

    The end of an address followed by a number reads like a number with dots:
    "100.123 2222" in "198.51.100.123 2222", or "123 443 1024" in
    "198.51.100.123 443 1024". A match rejected that way is retried one
    character later, so a real number overlapping it is still found.
    """
    addresses: list[tuple[int, int]] | None = None
    pos = 0
    while (match := US_PHONE_PATTERN.search(text, pos)) is not None:
        start = match.start()
        # Inside an address, a number can only start right after a dot.
        if start > 0 and text[start - 1] == ".":
            if addresses is None:
                addresses = [m.span() for m in _DOTTED_QUAD.finditer(text)]
            i = bisect.bisect_right(addresses, (start, len(text))) - 1
            if i >= 0 and addresses[i][0] < start < addresses[i][1]:
                pos = start + 1
                continue
        yield match
        pos = match.end()


def _find_ipv6(text: str) -> Iterator[re.Match[str]]:
    yield from IPV6_PATTERN.finditer(text)
    yield from _IPV6_AFTER_LABEL.finditer(text)


def _ipv6_end(match: re.Match[str]) -> int | None:
    """Accept a real IPv6 address that isn't a look-alike from code or times.

    Trailing punctuation and an unbracketed port are dropped. "::1"
    (loopback), "a[1::2]" (a Python slice), "std::cout", and "12:30:45" are
    rejected: the address needs a digit and either three groups or a full
    four-digit group, as in "fe80::1" or "2001:db8::1".
    """
    text, start, run = match.string, match.start("run"), match["run"]
    if run.count(":") < 2:
        return None
    candidates = [run]
    trimmed = run
    while trimmed[-1:] in (".", ":"):
        trimmed = trimmed[:-1]
        candidates.append(trimmed)
    if port := _IPV6_PORT.search(run):
        candidates.append(run[: port.start()])
    for candidate in dict.fromkeys(candidates):
        try:
            ipaddress.IPv6Address(candidate)
        except ValueError:
            continue
        end = start + len(candidate)
        if _GLUED.match(text, end):
            return None
        groups = [group for group in candidate.split(":") if group]
        if not any(ch.isdigit() for ch in candidate):
            return None
        if len(groups) < 3 and not any(len(group) == 4 for group in groups):
            return None
        return end
    return None


# Payment card candidates: 13-19 digits, optionally split by spaces or dashes,
# captured with room to spare so `_card_end` can back off from whatever digits
# follow the card. The pattern only looks ahead and consumes nothing, so every
# digit group gets its own attempt: a card right after another number ("#99999
# 4111 ...") isn't hidden inside that number's rejected capture.
#
# Groups may be separated by dashes or by any space used in print: the plain
# space, the no-break spaces of HTML and French typography, and the ideographic
# space of Japanese text. A candidate never starts after the "." or "," of a
# decimal number, whose fractional digits can look like a card.
_GROUP_SPACES = _SPACES + "\N{IDEOGRAPHIC SPACE}"
_CARD_SEPARATORS = _GROUP_SPACES + "-"
_CARD_SEP = "[" + re.escape(_CARD_SEPARATORS) + "]"
CARD_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])(?<![0-9][.,])"
    rf"(?=(?P<card>[0-9](?:{_CARD_SEP}?[0-9]){{12,22}}))"
)
# Issuer prefixes of the major networks, with the lengths each one issues.
_CARD_NETWORKS: tuple[tuple[re.Pattern[str], range | tuple[int, ...]], ...] = (
    (re.compile(r"4"), (13, 16, 19)),  # Visa
    (
        re.compile(r"5[1-5]|2(?:22[1-9]|2[3-9][0-9]|[3-6][0-9]{2}|7[01][0-9]|720)"),
        (16,),
    ),
    (re.compile(r"3[47]"), (15,)),  # American Express
    (re.compile(r"6(?:011|4[4-9]|5)"), range(16, 20)),  # Discover
    (re.compile(r"35(?:2[89]|[3-8][0-9])"), range(16, 20)),  # JCB
    (re.compile(r"3(?:0[0-5]|[689])"), range(14, 20)),  # Diners Club
    (re.compile(r"62"), range(16, 20)),  # UnionPay
    (re.compile(r"5[0678]|6[37]"), range(13, 20)),  # Maestro
)


def _card_network_ok(digits: str) -> bool:
    return any(
        prefix.match(digits) and len(digits) in lengths
        for prefix, lengths in _CARD_NETWORKS
    )


def _luhn_ok(digits: str) -> bool:
    total = 0
    for position, ch in enumerate(reversed(digits)):
        digit = ord(ch) - 48
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


_CARD_GROUP_SPLIT = re.compile(_CARD_SEP)


def _card_layout_ok(candidate: str) -> bool:
    """Accept unseparated digits, or one separator in a printed card layout.

    Cards are printed in groups of four (the last group may be shorter), or
    4-6-5 and 4-6-4 for American Express and Diners Club. Requiring that
    keeps runs of other numbers ("555 123 4567 555-765") from passing.
    """
    if len({ch for ch in candidate if not ch.isdigit()}) > 1:
        return False
    sizes = [len(group) for group in _CARD_GROUP_SPLIT.split(candidate)]
    if len(sizes) == 1:
        return True
    if sizes in ([4, 6, 5], [4, 6, 4]):
        return True
    return all(size == 4 for size in sizes[:-1]) and 1 <= sizes[-1] <= 4


def _card_end(match: re.Match[str]) -> int | None:
    """Find the longest prefix that is a valid card number, or reject it.

    A card needs a printed card layout, a known issuer prefix, a length that
    network issues, and a valid Luhn check digit, which rules out almost all
    other digit runs (IDs, timestamps, phone numbers, an invalid IBAN).
    """
    text, start, run = match.string, match.start(), match["card"]
    # Every printed layout starts with a group of four, or has no separators
    # at all; anything else ("1 1 1 ...") can be rejected without more work.
    first = _CARD_GROUP_SPLIT.search(run)
    if first is not None and first.start() != 4:
        run = run[: first.start()]
    cuts = [
        len(run),
        *(i for i in range(len(run) - 1, 0, -1) if run[i] in _CARD_SEPARATORS),
    ]
    for cut in cuts:
        digits = "".join(ch for ch in run[:cut] if ch.isdigit())
        if len(digits) > 19:
            continue
        if len(digits) < 13:
            return None
        if _GLUED.match(text, start + cut):
            continue
        if _card_layout_ok(run[:cut]) and _card_network_ok(digits) and _luhn_ok(digits):
            return start + cut
    return None


# IBAN candidates: a country code and two check digits, then the rest either
# compact or printed in groups of four (the last group may be shorter), so a
# code like "BA115" or "IP67" can't pull in the words after it. `_iban_end`
# backs off group by group to the longest prefix with a valid ISO 7064 mod-97
# checksum. Like CARD_PATTERN, it consumes nothing, so an IBAN right after
# other IBAN-shaped text is still tried.
_IBAN_SEP = "[" + re.escape(_GROUP_SPACES) + "]"
IBAN_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])(?=(?P<iban>[A-Za-z]{{2}}[0-9]{{2}}(?:"
    rf"(?:{_IBAN_SEP}[A-Za-z0-9]{{4}}){{2,7}}(?:{_IBAN_SEP}[A-Za-z0-9]{{1,3}})?"
    rf"|[A-Za-z0-9]{{11,30}})))"
)
# Countries that issue IBANs (the SWIFT registry, plus countries that use the
# format without being registered).
_IBAN_COUNTRY_CODES = (
    "AD AE AL AO AT AZ BA BE BF BG BH BI BJ BR BY CF CG CH CI CM CR CV CY CZ "
    "DE DJ DK DO DZ EE EG ES FI FK FO FR GA GB GE GI GL GQ GR GT GW HN HR HU "
    "IE IL IQ IR IS IT JO KM KW KZ LB LC LI LT LU LV LY MA MC MD ME MG MK ML "
    "MN MR MT MU MZ NE NI NL NO OM PK PL PS PT QA RO RS RU SA SC SD SE SI SK "
    "SM SN SO ST SV TD TG TL TN TR UA VA VG XK YE"
)
_IBAN_COUNTRIES = frozenset(_IBAN_COUNTRY_CODES.split())


def _iban_end(match: re.Match[str]) -> int | None:
    """Find the longest prefix that is a valid IBAN, or reject it.

    An IBAN needs a country that issues IBANs, 15-34 characters, and a valid
    checksum. The checksum of every group-aligned prefix is computed in one
    pass: the rearranged number is the rest of the IBAN followed by the
    country code (as digits) and the check digits.
    """
    text, start, run = match.string, match.start(), match["iban"]
    country, check = run[:2].upper(), run[2:4]
    if country not in _IBAN_COUNTRIES or not 2 <= int(check) <= 98:
        return None
    tail = int(f"{int(country[0], 36)}{int(country[1], 36)}{check}")  # 6 digits
    remainder, length = 0, 4
    prefixes: list[tuple[int, int, int]] = []  # (end, remainder, length)
    for i in range(4, len(run)):
        ch = run[i]
        if ch in _GROUP_SPACES:
            prefixes.append((i, remainder, length))
            continue
        value = int(ch, 36)
        remainder = (remainder * (100 if value > 9 else 10) + value) % 97
        length += 1
    prefixes.append((len(run), remainder, length))
    for end, remainder, length in reversed(prefixes):
        if length > 34:
            continue
        if length < 15:
            return None
        if (remainder * 1_000_000 + tail) % 97 == 1 and not _GLUED.match(
            text, start + end
        ):
            return start + end
    return None


# SSNs use ASCII digits. Reject the area/group/serial ranges the SSA never
# assigns; this checks the shape, not whether a number was issued. Consistent
# hyphens stand alone; spaces and bare nine-digit runs need an explicit label
# so ordinary order numbers, routing numbers and dates aren't treated as SSNs.
_SSN_AREA = r"(?!000|666|9[0-9]{2})[0-9]{3}"
_SSN_GROUP = r"(?!00)[0-9]{2}"
_SSN_SERIAL = r"(?!0000)[0-9]{4}"
_SSN_LEFT = rf"(?<![A-Za-z\d+{MARK_CLASS}])(?<!\d[.\-\u2011\u2013])"
_SSN_RIGHT = rf"(?![A-Za-z\d{MARK_CLASS}]|[.\-\u2011\u2013]\d)"
SSN_PATTERN = re.compile(
    rf"{_SSN_LEFT}{_SSN_AREA}(?P<dash>[-\u2011\u2013])"
    rf"{_SSN_GROUP}(?P=dash){_SSN_SERIAL}{_SSN_RIGHT}"
)
_LABELLED_SSN_PATTERN = re.compile(
    rf"(?<![\w{MARK_CLASS}])(?i:ssn|social[ \t]+security(?:[ \t]+number)?)\b"
    rf"[ \t]*(?:[:=#][ \t]*)?"
    rf"(?P<number>{_SSN_AREA}(?P<space>[{_SPACES}]){_SSN_GROUP}"
    rf"(?P=space){_SSN_SERIAL}|{_SSN_AREA}{_SSN_GROUP}{_SSN_SERIAL})"
    rf"{_SSN_RIGHT}"
)


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
    # The match group where the span starts (a finder may match a label first).
    group: int | str = 0


class RegexDetector:
    r"""Detects personal data, supported coding secrets, and custom patterns.

    Built-in entity types are ``EMAIL``, ``PHONE``, ``IPV4``, ``IPV6``,
    ``CREDIT_CARD``, ``IBAN``, ``SSN``, ``API_KEY``, ``TOKEN``, ``PASSWORD``,
    ``PRIVATE_KEY``, and ``CREDENTIAL``. Custom
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
        self._secret_types = tuple(
            kind
            for kind in _secrets.SECRET_TYPES
            if include_builtins and kind not in custom
        )
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
        return tuple(
            dict.fromkeys(
                [rule.entity_type for rule in self._rules] + list(self._secret_types)
            )
        )

    def detect_field(self, text: str, name: str) -> list[Span]:
        """Detect a parsed credential string using its field name as context."""
        return [
            s
            for s in _secrets.detect_field(text, name)
            if s.entity_type in self._secret_types
        ]

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
                start = match.start(rule.group)
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
        secrets = (
            [s for s in _secrets.detect(text) if s.entity_type in self._secret_types]
            if self._secret_types
            else []
        )
        # URL userinfo can resemble an email (password@host). Keep the host
        # outside the credential placeholder, without suppressing custom rules.
        credentials = sorted(
            (s for s in secrets if s.entity_type == "CREDENTIAL"), key=lambda s: s.end
        )
        ends = [s.end for s in credentials]
        values = []
        for span in result.values():
            at = bisect.bisect_right(ends, span.start)
            if (
                span.entity_type == "EMAIL"
                and span.priority == self.BUILTIN_PRIORITY
                and at < len(credentials)
                and credentials[at].start <= span.start < credentials[at].end < span.end
            ):
                continue
            values.append(span)
        unique: dict[tuple[int, int, str], Span] = {}
        for span in [*values, *secrets]:
            unique.setdefault((span.start, span.end, span.entity_type), span)
        return sorted(unique.values(), key=lambda s: (s.start, -len(s)))

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
    _Rule(
        "PHONE",
        US_PHONE_PATTERN,
        RegexDetector.BUILTIN_PRIORITY,
        finder=_find_us_phones,
    ),
    _Rule(
        "PHONE",
        INTL_PHONE_PATTERN,
        RegexDetector.BUILTIN_PRIORITY,
        refine=_intl_phone_end,
        shrink=_shrink_phone,
    ),
    _Rule("IPV4", IPV4_PATTERN, RegexDetector.BUILTIN_PRIORITY),
    _Rule(
        "IPV6",
        IPV6_PATTERN,
        RegexDetector.BUILTIN_PRIORITY,
        refine=_ipv6_end,
        finder=_find_ipv6,
        group="run",
    ),
    _Rule(
        "CREDIT_CARD", CARD_PATTERN, RegexDetector.BUILTIN_PRIORITY, refine=_card_end
    ),
    _Rule("IBAN", IBAN_PATTERN, RegexDetector.BUILTIN_PRIORITY, refine=_iban_end),
    _Rule("SSN", SSN_PATTERN, RegexDetector.BUILTIN_PRIORITY),
    _Rule("SSN", _LABELLED_SSN_PATTERN, RegexDetector.BUILTIN_PRIORITY, group="number"),
)
