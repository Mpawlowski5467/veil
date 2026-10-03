"""Conservative local review cues for labelled privacy data and encoded values.

These rules propose values for a person's decision; they never assert that
arbitrary names, addresses, encodings, or prose are always private.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterator

from .detectors._secrets import _WEB_URL

PII_REVIEW_TYPES = ("PERSON", "ADDRESS", "DOB", "PASSPORT", "DRIVERS_LICENSE")
_LABELS = (
    (
        re.compile(
            r"(?i:\b(?:hasło|haslo)[ \t]+(?:to|jest)|"
            r"\bcontrase(?:ñ|n)a[ \t]+es)\b[ \t:]*"
        ),
        "PASSWORD",
    ),
    (
        re.compile(
            r"(?i:\b(?:recovery|backup|one[- ]time recovery)[ -]+code)[ \t]*[:=][ \t]*"
        ),
        "CREDENTIAL",
    ),
    (
        re.compile(r"(?i:\b(?:passport(?:[ -]+number)?|paszport))[ \t]*[:=]?[ \t]+"),
        "PASSPORT",
    ),
    (
        re.compile(
            r"(?i:\bdriver(?:['\u2019]s)?[ -]+licen[cs]e(?:[ -]+number)?)"
            r"[ \t]*[:=]?[ \t]+"
        ),
        "DRIVERS_LICENSE",
    ),
    (
        re.compile(
            r"(?i:\b(?:born(?:[ \t]+on)?|date[ \t]+of[ \t]+birth|dob))[ \t]*[:=]?[ \t]+"
        ),
        "DOB",
    ),
    (
        re.compile(
            r"(?i:\b(?:lives[ \t]+at|(?:home|street|postal|customer|patient|"
            r"employee|billing|shipping)[ \t]+address))"
            r"[ \t]*[:=]?[ \t]+"
        ),
        "ADDRESS",
    ),
    (
        re.compile(
            r"(?i:\b(?:customer|patient|employee|full[ \t]+name))[ \t]*[:=]?[ \t]+"
        ),
        "PERSON",
    ),
)
_SIGN_IN_MARK = re.compile(
    r"(?P<start>(?i:\b(?:use|enter))[ \t]+)|"
    r"(?P<end>[ \t]+(?i:to[ \t]+(?:sign[ \t]*in|log[ \t]*in|authenticate))\b)"
)
_ENCODING = re.compile(r'(?i:"encoding"[ \t]*:[ \t]*"base64(?:url)?")')
_ENCODED_VALUE = re.compile(
    r'"(?:data|value|payload|content)"[ \t]*:[ \t]*"(?P<value>[A-Za-z0-9_+/=-]+)"'
)
_WEBHOOK_CONTEXT = re.compile(
    r"(?i:\bwebhook(?:[ \t]+(?:endpoint|url))?(?:[ \t]+(?:is|was))?)"
    r"[ \t]*[:=]?[ \t]*$"
)
_SPLIT = re.compile(r"(?i:\b(?:fragments?|pieces|parts|split|concatenat\w*)\b)")
_CREDENTIAL_WORD = re.compile(
    r"(?i:\b(?:password|passphrase|api[ _-]?key|access[ _-]?token|"
    r"secret|credentials?)\b)"
)
_TOKEN_LINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./+~-]{3,}={0,2}\Z")
_FRAGMENT_LABEL = re.compile(
    r"(?i:\b(?:(?:first|second|third|fourth|last|next)[ \t]+(?:part|piece|fragment)|"
    r"(?:part|piece|fragment)(?:[ \t]+(?:[0-9]{1,3}|[a-z]))?))"
    r"[ \t]*[:=][ \t]*"
)
# Dotted expressions can be code references; quote them to establish a value.
_FRAGMENT_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_/+~-]{3,}={0,2}\Z")
_NAME = re.compile(r"[^\W\d_][\w'\u2019.-]*(?:[ \t]+[^\W\d_][\w'\u2019.-]*){1,4}\Z")
_DATE = re.compile(r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4})\Z")


def _phrase(text: str, *, start: int = 0, address: bool = False) -> str:
    """Use quoted boundaries or a sentence/clause boundary for labelled prose."""
    while start < len(text) and text[start].isspace():
        start += 1
    if text[start : start + 1] in {'"', "'", "`"}:
        quote = text[start]
        cursor = start + 1
        while cursor < len(text):
            if text[cursor] == "\\":
                cursor += 2
            elif text[cursor] == quote:
                return text[start + 1 : cursor]
            else:
                cursor += 1
        return text[start + 1 :]
    separators = r"[;\r\n]|[.!?](?=\s|$)" if address else r"[,;\r\n]|[.!?](?=\s|$)"
    for boundary in re.compile(separators).finditer(text, start):
        if boundary[0] == ".":
            word = re.search(
                r"([^\W\d_]+)$",
                text[max(start, boundary.start() - 16) : boundary.start()],
            )
            # Initials and common address abbreviations are not sentence ends.
            if word and (
                len(word[0]) == 1
                or (
                    address
                    and word[0].lower()
                    in {
                        "st",
                        "ave",
                        "rd",
                        "blvd",
                        "dr",
                        "ln",
                        "apt",
                        "ste",
                        "no",
                    }
                )
            ):
                continue
        return text[start : boundary.start()].strip()
    return text[start:].strip().rstrip(".")


def _sign_in_values(text: str) -> Iterator[str]:
    # Pair cues in one pass instead of retrying a lazy full-line match at every
    # occurrence of 'use' in a long prompt with no terminating sign-in phrase.
    for line in text.splitlines():
        start = None
        for marker in _SIGN_IN_MARK.finditer(line):
            if marker["start"] and start is None:
                start = marker.end()
            elif marker["end"] and start is not None:
                value = line[start : marker.start()].strip()
                # The explicit 'to sign in' delimiter bounds an unquoted
                # credential too; punctuation inside that value is private.
                yield _phrase(value) if value[:1] in {'"', "'", "`"} else value
                start = None


def fragment_context(text: str) -> bool:
    """Require both a fragmentation cue and a credential cue on the same line."""
    return any(
        _SPLIT.search(line) and _CREDENTIAL_WORD.search(line)
        for line in text.splitlines()
    )


def _labelled_fragments(text: str) -> Iterator[str]:
    """Read explicit fragment labels, preserving source escapes and boundaries."""
    for line in text.splitlines():
        cursor = 0
        while label := _FRAGMENT_LABEL.search(line, cursor):
            start = cursor = label.end()
            value = _phrase(line, start=start)
            # Consume a proposed value once. Repeated label-like words in an
            # unbounded value must not rescan the remaining line quadratically.
            cursor = max(cursor, start + len(value))
            if value and (
                line[start : start + 1] in {'"', "'", "`"}
                or _FRAGMENT_TOKEN.fullmatch(value)
            ):
                yield value


def contextual_values(
    text: str, *, fragments: bool = False
) -> Iterator[tuple[str, str, str]]:
    """Yield original source spellings and safe, fixed reasons for local review."""
    for label, kind in _LABELS:
        for match in label.finditer(text):
            value = _phrase(text, start=match.end(), address=kind == "ADDRESS")
            if not value:
                continue
            if kind == "PERSON" and not (
                _NAME.fullmatch(value)
                and all(word[0].isupper() for word in value.split())
            ):
                continue
            if kind == "DOB" and not _DATE.fullmatch(value):
                continue
            if kind == "ADDRESS" and not re.match(r"\d+[A-Za-z]?[ \t]+\S", value):
                continue
            if kind in {"PASSPORT", "DRIVERS_LICENSE"} and not (
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{3,63}", value)
                and any(char.isdigit() for char in value)
            ):
                continue
            yield value, kind, "value described by a privacy or credential label"
    for value in _sign_in_values(text):
        yield value, "PASSWORD", "value offered for signing in"
    for match in _WEB_URL.finditer(text):
        prefix = text[max(0, match.start() - 160) : match.start()]
        if _WEBHOOK_CONTEXT.search(prefix):
            yield (
                match[0].rstrip(".,;"),
                "TOKEN",
                "webhook endpoint may contain a credential",
            )
    if _ENCODING.search(text):
        for match in _ENCODED_VALUE.finditer(text):
            value = match["value"]
            if len(value) < 4:
                continue
            # Bound decoding; large values are still proposed and the review
            # queue's shared value limit refuses oversized input safely.
            if len(value) > 16_384:
                yield value, "CREDENTIAL", "explicitly encoded payload needs review"
                continue
            try:
                decoded = base64.b64decode(
                    value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
                )
            except (ValueError, binascii.Error):
                continue
            if decoded:
                yield value, "CREDENTIAL", "explicitly encoded payload needs review"
    if fragments:
        for value in _labelled_fragments(text):
            yield value, "CREDENTIAL", "labelled fragment of a described credential"
        for line in text.splitlines():
            value = line.strip().strip("\"'`")
            if _TOKEN_LINE.fullmatch(value):
                yield value, "CREDENTIAL", "possible fragment of a described credential"
