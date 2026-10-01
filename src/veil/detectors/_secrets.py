"""Coding-secret detection by explicit syntax, never by entropy alone.

These rules recognize supported shapes, not whether a credential is active.
Values retain their exact source spelling so restoration is lossless.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterator

from ..types import Span

SECRET_TYPES = ("API_KEY", "TOKEN", "PASSWORD", "PRIVATE_KEY", "CREDENTIAL")

_PREFIXED = re.compile(
    r"(?<![\w-])(?:"
    r"(?P<api>sk-(?:proj-|svcacct-|ant-api[0-9]+-)?[A-Za-z0-9_-]{20,}"
    r"|AIza[A-Za-z0-9_-]{35}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,})"
    r"|(?P<token>gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{20,}"
    r"|npm_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,})"
    r")(?![\w-])"
)
_JWT = re.compile(
    r"(?<![\w.-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![\w.-])"
)
_BEGIN_KEY = re.compile(
    r"-----BEGIN (?P<kind>(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY)-----"
)
_ASSIGNMENT = re.compile(
    # A removed diff line still contains a credential. Consume just its leading
    # '-' without relaxing boundaries inside ordinary identifiers.
    r"(?:(?<![\w.-])|(?m:^-))"
    r"(?P<quote>[\"']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]{0,127})"
    r"(?P=quote)[ \t]*(?::|=(?!=|>))[ \t]*"
)
_BARE = re.compile(r"[^\s\"'`<>{}\[\]()]+")
_REFERENCE = re.compile(
    r"(?:\$\{[^}\r\n]+\}|\$[A-Za-z_]\w*|%[A-Za-z_]\w*%"
    r"|\{\{[^\r\n]+\}\}|<[^>\r\n]+>|\[[A-Z][A-Z0-9_]*_[0-9]+\])\Z"
)
_CODE_REFERENCE = re.compile(
    r"(?:os\.(?:environ|getenv)|(?:process\.env|import\.meta\.env)"
    r"(?:\.[A-Za-z_]\w*)?)\Z"
)
_AUTH = re.compile(
    r"(?i:\b(?P<scheme>Bearer|Basic))[ \t]+(?P<value>[A-Za-z0-9._~+/-]{8,}=*)"
    r"(?![\w.~+/=-])"
)
_URL = re.compile(
    r"(?i:\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?|amqps?|https?)://)"
    r"(?P<value>[^\s/@:\"'<>]*:[^\s/@\"'<>]+)@"
)


def credential_type(name: str) -> str | None:
    """Classify an explicit credential field, retaining normal code identifiers."""
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    parts = re.split(r"[_.-]+", separated.lower())
    last = parts[-1]
    joined = "_".join(parts)
    if last in {"password", "passwd", "passphrase", "pwd", "pass"}:
        return "PASSWORD"
    if joined.endswith("private_key") or last == "privatekey":
        return "PRIVATE_KEY"
    if (
        joined.endswith(("api_key", "access_key_id", "secret_access_key"))
        or last in {"apikey", "secret", "secretkey"}
        or joined.endswith("secret_key")
    ):
        return "API_KEY"
    if last == "token":
        return "TOKEN"
    return None


def _span(text: str, start: int, end: int, kind: str) -> Span:
    return Span(start, end, text[start:end], kind, "secret", 1)


def _literal(value: str) -> bool:
    # Explicit placeholders are instructions/references, not actual values.
    return bool(value.strip()) and _REFERENCE.fullmatch(value.strip()) is None


def detect_field(text: str, name: str) -> list[Span]:
    """Find a secret in an already parsed string field, without losing context."""
    kind = credential_type(name)
    if kind and _literal(text):
        return [_span(text, 0, len(text), kind)]
    return []


def _assignment_value(text: str, start: int) -> tuple[int, int] | None:
    if start == len(text):
        return None
    if text.startswith(("${", "{{", "#"), start):
        return None
    quote = text[start]
    if quote in "\"'`":
        end = start + 1
        while end < len(text):
            if text[end] == "\\":
                end += 2
            elif text[end] == quote:
                return start + 1, end
            else:
                end += 1
        # Truncated quoted input: protect through the end, not a partial token.
        return start + 1, len(text)
    match = _BARE.match(text, start)
    if match is None:
        return None
    value = match[0]
    next_char = match.end()
    while next_char < len(text) and text[next_char] in " \t":
        next_char += 1
    if (
        (next_char < len(text) and text[next_char] in "([")
        or _CODE_REFERENCE.fullmatch(value)
        or value in {"None", "null", "true", "false", "str", "string", "int", "bytes"}
    ):
        return None
    # A trailing source-code terminator is not part of a bare assignment.
    end = match.end()
    while end > start and text[end - 1] in ",;":
        end -= 1
    return start, end


def _private_keys(text: str) -> Iterator[Span]:
    cursor = 0
    while match := _BEGIN_KEY.search(text, cursor):
        ending = f"-----END {match['kind']}-----"
        end = text.find(ending, match.end())
        # With no closing delimiter, withholding the rest avoids leaking a
        # truncated key. Never validate key material or call a remote service.
        cursor = len(text) if end < 0 else end + len(ending)
        yield _span(text, match.start(), cursor, "PRIVATE_KEY")


def _jwt_header(value: str) -> bool:
    header = value.partition(".")[0]
    if len(header) > 4096:
        return False
    try:
        decoded = base64.b64decode(
            header + "=" * (-len(header) % 4), altchars=b"-_", validate=True
        )
        fields = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, binascii.Error, RecursionError):
        return False
    return isinstance(fields, dict) and isinstance(fields.get("alg"), str)


def _basic_auth(value: str) -> bool:
    try:
        return b":" in base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return False


def detect(text: str) -> list[Span]:
    """Find supported prefixed keys, tokens, assignments, URLs, and PEM blocks."""
    found = list(_private_keys(text))
    for match in _PREFIXED.finditer(text):
        kind = "API_KEY" if match["api"] is not None else "TOKEN"
        found.append(_span(text, match.start(), match.end(), kind))
    for match in _JWT.finditer(text):
        if _jwt_header(match[0]):
            found.append(_span(text, match.start(), match.end(), "TOKEN"))
    for match in _AUTH.finditer(text):
        if match["scheme"].lower() == "bearer" or _basic_auth(match["value"]):
            found.append(_span(text, match.start("value"), match.end("value"), "TOKEN"))
    for match in _URL.finditer(text):
        found.append(
            _span(text, match.start("value"), match.end("value"), "CREDENTIAL")
        )
    # Skip consumed quoted values: apparent assignments inside one password
    # must not cause repeated scans of its remaining tail.
    cursor = 0
    while assignment := _ASSIGNMENT.search(text, cursor):
        cursor = assignment.end()
        assigned_type = credential_type(assignment["name"])
        if assigned_type is None:
            continue
        offsets = _assignment_value(text, cursor)
        if offsets is not None:
            start, end = offsets
            cursor = max(cursor, end)
            if _literal(text[start:end]):
                found.append(_span(text, start, end, assigned_type))
    return found
