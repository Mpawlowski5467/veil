"""Coding-secret detection by explicit syntax, never by entropy alone.

These rules recognize supported shapes, not whether a credential is active.
Values retain their exact source spelling so restoration is lossless.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse
from collections.abc import Iterator

from ..types import Span

SECRET_TYPES = ("API_KEY", "TOKEN", "PASSWORD", "PRIVATE_KEY", "CREDENTIAL")
_CONCAT = re.compile(r"[ \t]*\+[ \t]*")

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
    r"(?P<quote>[\"']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]{0,127})(?P=quote)"
    # Go typed declarations such as: apiKey string = "..."
    r"(?:[ \t]+(?:string|\[\]byte))?"
    r"(?P<before>[ \t]*)(?P<separator>:?=(?![=>])|:)(?P<after>[ \t]*)"
)
# A type between ':' and the value: str =, Optional[str] =, &'static str =,
# String :=, str="...". Bounded and lazy, so the first such '=' ends it.
_ANNOTATION = re.compile(
    r"(?P<type>[A-Za-z_&*(\[][\w.:&*?!<>\[\](), |']{0,127}?)"
    r"(?:[ \t]+:?=(?![=>])[ \t]*|:?=(?![=>])(?=[ \t\"'`])[ \t]*)"
)
_QUOTES = frozenset("\"'`")
_BARE = re.compile(r"[^\s\"'`<>{}\[\]()]+")
# YAML/INI/prose values run to the end of their clause, never stopping mid-phrase.
_LINE_REST = re.compile(r"[^\r\n]*")
_CLAUSE_END = re.compile(r"[ \t]+(?:#|//)|[.!?;,](?=[ \t]|$|\r|\n)")
# The quote that closes a string around a label, as in -H "X-Api-Key: ...".
_CLOSING = {q: re.compile(re.escape(q) + r"(?![^\s,;)\]}])") for q in _QUOTES}
_REFERENCE = re.compile(
    r"(?:\$\{[^}\r\n]+\}|\$[A-Za-z_]\w*|%[A-Za-z_]\w*%"
    r"|\{\{[^\r\n]+\}\}|<[^>\r\n]+>|\[[A-Z][A-Z0-9_]*_[0-9]+\])\Z"
)
_CODE_REFERENCE = re.compile(
    r"(?:os\.(?:environ|getenv)|(?:process\.env|import\.meta\.env)"
    r"(?:\.[A-Za-z_]\w*)?)\Z"
)
_PROPERTY_REFERENCE = re.compile(r"(?:settings|config|self|this)(?:\.[A-Za-z_]\w*)+\Z")
_AUTH = re.compile(
    r"(?i:\b(?P<scheme>Bearer|Basic))[ \t]+(?P<value>[A-Za-z0-9._~+/-]{8,}=*)"
    r"(?![\w.~+/=-])"
)
_URL = re.compile(
    r"(?i:\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?|amqps?|https?)://)"
    r"(?P<value>[^\s/@:\"'<>]*:[^\s/@\"'<>]+)@"
)
_WEB_URL = re.compile(r"(?i:\b(?:https?|otpauth)://)[^\s<>\"'`]+")
_QUERY_PARAMETER = re.compile(r"(?:\?|&)(?P<name>[^?&#=\s]+)=(?P<value>[^&#]*)")
_COOKIE_HEADER = re.compile(
    r"(?im)^[ \t]*(?:set-cookie|cookie):[ \t]*(?P<pairs>[^\r\n]+)"
)
_COOKIE_PAIR = re.compile(r"(?:^|;)[ \t]*(?P<name>[^=;\s]+)=(?P<value>[^;]*)")
_COOKIE_NAMES = frozenset(
    {
        "sid",
        "session",
        "sessionid",
        "session_id",
        "session-id",
        "connect.sid",
        "jsessionid",
        "phpsessid",
        "auth",
        "auth_token",
        "access_token",
        "refresh_token",
        "csrftoken",
        "xsrf-token",
        "csrf_token",
    }
)
_JSON_KEY = re.compile(r'"(?P<name>(?:[^"\\\r\n]|\\[^\r\n]){1,512})"[ \t]*:[ \t]*')
_PROSE_LABEL = re.compile(
    r"(?i:\b(?P<name>api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"password|passphrase|client[ _-]?secret|private[ _-]?key))"
    r"[ \t]+(?i:is|was|equals)[ \t]+"
)
_LOCAL_PASSWORD = re.compile(
    r"(?i:\b(?:hasło|haslo)[ \t]+(?:to|jest)|"
    r"\bcontrase(?:ñ|n)a[ \t]+es)\b[ \t:]*"
)
_RECOVERY_CODE = re.compile(
    r"(?i:\b(?:recovery|backup|one[- ]time recovery)[ -]+code)[ \t]*[:=][ \t]*"
)
# Unquoted prose needs a token-like value AND an explicit end to the clause.
# Ordinary explanations ("password is stored in ...") remain review candidates.
_LABELLED_TOKEN = re.compile(
    r"(?:[^\W_]+(?:[-_/@+][^\W_]+)+|[A-Za-z_]*[0-9][A-Za-z0-9_]*)"
    r"(?=[ \t]*(?:[.,;!?](?=\s|$)|\r?\n|$))"
)
_FLAG_LABEL = re.compile(
    r"(?<![\w-])--(?P<name>password|passwd|passphrase|api-key|access-token|"
    r"refresh-token|token|secret|client-secret|private-key)(?:[ \t]+|=)"
)
_BLOCK_HEADER = re.compile(
    r"(?m)^(?P<indent> *)(?P<item>-[ ]+)?"
    r"(?P<quote>[\"']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]{0,127})(?P=quote)"
    r":[ \t]*(?P<marker>[|>](?:[1-9][+-]?|[+-][1-9]?)?)"
    r"[ \t]*(?:#[^\r\n]*)?\r?\n"
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


def _assignment_value(
    text: str, start: int, *, clause: bool = False, closer: str = ""
) -> tuple[int, int] | None:
    """Offsets of an assigned value, or None for references and code.

    Quoted values end at their quote; plain values after ':' or a spaced '='
    end at their clause; compact NAME=value reads one shell word.
    """
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
    value = match[0].rstrip(",;")
    next_char = match.end()
    while next_char < len(text) and text[next_char] in " \t":
        next_char += 1
    if (
        (next_char < len(text) and text[next_char] in "([")
        or _CODE_REFERENCE.fullmatch(value)
        # Only known configuration/object roots and a credential attribute.
        # Quoted lookalikes are literals and were handled above. Arbitrary bare
        # identifiers remain protected: they may be weak passwords.
        or (_PROPERTY_REFERENCE.fullmatch(value) and credential_type(value))
        or value in {"None", "null", "true", "false", "str", "string", "int", "bytes"}
    ):
        return None
    # A trailing source-code terminator is not part of a bare assignment.
    end = match.end()
    while end > start and text[end - 1] in ",;":
        end -= 1
    if (
        clause
        and end == match.end()
        and text[end - 1] not in ".!?"
        and (line := _LINE_REST.match(text, end))
        and (tail := line[0].lstrip(" \t"))
        and not tail.startswith(("#", "//", ")", "]", "}", ",", ";"))
    ):
        limit = line.end()
        if closer and (close := _CLOSING[closer].search(text, end, limit)):
            limit = close.start()
        stop = _CLAUSE_END.search(text, end, limit)
        end = stop.start() if stop else limit
        while text[end - 1] in " \t":
            end -= 1
    return start, end


def _annotated(text: str, start: int) -> int | None:
    """Where a value starts after a balanced type annotation, if there is one.

    Parameter lists are not annotations: ``f(token: str, retries: int = 3)``
    has a ',' and a single ':' outside any brackets, so it is rejected.
    """
    match = _ANNOTATION.match(text, start)
    if match is None:
        return None
    annotation = match["type"]
    depth = 0
    for index, char in enumerate(annotation):
        if char in "([<":
            depth += 1
        elif char in ")]>":
            depth -= 1
        # A path such as std::string is a type; a ',' or one ':' starts another
        # parameter. The first character is never ':', so index - 1 is valid.
        next_parameter = char == "," or (
            char == ":" and "::" not in annotation[index - 1 : index + 2]
        )
        if depth < 0 or (depth == 0 and next_parameter):
            return None
    return match.end() if depth == 0 else None


def _ends_string(text: str, match: re.Match[str]) -> bool:
    """Whether the quote after a label closes the string that holds the label.

    In input("Password: ") or read -p "Password: " pw that quote is no value;
    reading it as an unterminated one would hide the rest of the text.
    """
    start = match.end()
    quote = text[start]
    following = text.find(quote, start + 1)
    # Only the last such quote on its line, after an odd number of them, so
    # each line is counted at most once.
    if _CLOSING[quote].match(text, start) is None or (
        following >= 0 and text.find("\n", start, following) < 0
    ):
        return False
    line_start = text.rfind("\n", 0, match.start()) + 1
    return text.count(quote, line_start, match.start()) % 2 == 1


def _assigned_value(text: str, match: re.Match[str]) -> tuple[int, int, bool] | None:
    """Value offsets after an assignment, past any type annotation; flag quotes."""
    start = match.end()
    clause = match["separator"] != "=" or bool(match["before"] or match["after"])
    # A label that opens a string, as in curl -H "X-Api-Key: ...", ends with it.
    name = match.start("name")
    opener = "" if match["quote"] else text[name - 1 : name]
    closer = opener if opener in _QUOTES else ""
    if match["separator"] == ":" and (value := _annotated(text, start)) is not None:
        offsets = _assignment_value(text, value, clause=True, closer=closer)
        if offsets is None:
            return None
        if text[value : value + 1] in _QUOTES:
            return (*offsets, True)
        if offsets[1] <= offsets[0]:  # Nothing assigned: password: str = ;
            return None
        # A misread annotation must not leave part of a bare value visible.
        return start, offsets[1], False
    if text[start : start + 1] in _QUOTES and _ends_string(text, match):
        return None
    offsets = _assignment_value(text, start, clause=clause, closer=closer)
    return None if offsets is None else (*offsets, text[start : start + 1] in _QUOTES)


def _private_keys(text: str) -> Iterator[Span]:
    cursor = 0
    while match := _BEGIN_KEY.search(text, cursor):
        ending = f"-----END {match['kind']}-----"
        end = text.find(ending, match.end())
        # With no closing delimiter, withholding the rest avoids leaking a
        # truncated key. Never validate key material or call a remote service.
        cursor = len(text) if end < 0 else end + len(ending)
        yield _span(text, match.start(), cursor, "PRIVATE_KEY")


def _labelled_values(text: str) -> Iterator[Span]:
    """Promote explicit credential labels only when value boundaries are clear."""
    for pattern, fixed_kind in (
        (_PROSE_LABEL, None),
        (_LOCAL_PASSWORD, "PASSWORD"),
        (_RECOVERY_CODE, "CREDENTIAL"),
    ):
        cursor = 0
        while match := pattern.search(text, cursor):
            cursor = match.end()
            kind = fixed_kind or credential_type(match["name"].replace(" ", "_"))
            if text[cursor : cursor + 1] in {'"', "'", "`"}:
                offsets = _assignment_value(text, cursor)
                # An unfinished prose quote has uncertain boundaries: review it.
                if offsets is None or offsets[1] == len(text):
                    continue
            else:
                token = _LABELLED_TOKEN.match(text, cursor)
                if token is None:
                    continue
                offsets = token.span()
            start, end = offsets
            cursor = end
            if kind and _literal(text[start:end]):
                yield _span(text, start, end, kind)


def _flag_values(text: str) -> Iterator[Span]:
    """Read one shell word without evaluating references, escapes, or commands."""
    cursor = 0
    while match := _FLAG_LABEL.search(text, cursor):
        start = cursor = match.end()
        if text.startswith(("--", "$", "`"), start):
            continue
        quote = ""
        first_close = None
        while cursor < len(text):
            char = text[cursor]
            if char == "\\" and quote != "'":
                cursor = min(cursor + 2, len(text))
            elif quote:
                if char == quote:
                    quote = ""
                    if first_close is None:
                        first_close = cursor
                cursor += 1
            elif char in "\"'":
                quote = char
                cursor += 1
            elif char.isspace() or char in ";|&()<>":
                break
            else:
                cursor += 1
        end = cursor
        if quote:  # Incomplete commands remain for review, not a guessed token.
            continue
        # Keep a single pair of surrounding quotes visible. Mixed shell words
        # such as 'first'"second" are replaced together with their source quotes.
        if start < end and text[start] in "\"'" and first_close == end - 1:
            start, end = start + 1, end - 1
        kind = credential_type(match["name"])
        value = text[start:end]
        if (value.startswith("$(") and value.endswith(")")) or (
            value.startswith("`") and value.endswith("`")
        ):
            continue
        if kind and end > start and _literal(value):
            yield _span(text, start, end, kind)


def _block_values(text: str) -> tuple[list[Span], set[int]]:
    """Protect raw YAML credential blocks through the last indented content line."""
    found, handled = [], set()
    cursor = 0
    while header := _BLOCK_HEADER.search(text, cursor):
        cursor = header.end()
        kind = credential_type(header["name"])
        if kind is None:
            continue
        handled.add(header.start("marker"))
        parent_indent = len(header["indent"]) + len(header["item"] or "")
        start = cursor
        end = cursor
        while cursor < len(text):
            newline = text.find("\n", cursor)
            line_end = len(text) if newline < 0 else newline
            line = text[cursor:line_end].rstrip("\r")
            if line.strip():
                if len(line) - len(line.lstrip(" ")) <= parent_indent:
                    break
                end = cursor + len(line)
            cursor = line_end + 1
        if end > start and _literal(text[start:end]):
            found.append(_span(text, start, end, kind))
    return found, handled


def _url_parameters(text: str) -> tuple[list[Span], set[int]]:
    """Mask only credential query values, retaining their original URL spelling."""
    found, handled = [], set()
    for url in _WEB_URL.finditer(text):
        # A fragment is not part of the server-side query string.
        raw = url[0].split("#", 1)[0]
        if "?" not in raw:
            continue
        for parameter in _QUERY_PARAMETER.finditer(raw, raw.index("?")):
            name = urllib.parse.unquote_plus(parameter["name"]).lower()
            kind = credential_type(name)
            if name in {"sig", "signature", "x-amz-signature", "x-goog-signature"}:
                kind = "TOKEN"
            if kind is None:
                continue
            start = url.start() + parameter.start("value")
            end = url.start() + parameter.end("value")
            # Do not let the generic assignment rule swallow '&next=...'.
            if start != end or text[start : start + 1] not in {'"', "'", "`"}:
                handled.add(start)
            if end > start and _literal(text[start:end]):
                found.append(_span(text, start, end, kind))
    return found, handled


def _cookies(text: str) -> tuple[list[Span], set[int]]:
    """Recognize session/auth cookies in pasted HTTP headers, not theme cookies."""
    found, handled = [], set()
    for header in _COOKIE_HEADER.finditer(text):
        for pair in _COOKIE_PAIR.finditer(header["pairs"]):
            name = pair["name"].lower()
            for prefix in ("__host-", "__secure-"):
                if name.startswith(prefix):
                    name = name[len(prefix) :]
            if name not in _COOKIE_NAMES:
                continue
            start = header.start("pairs") + pair.start("value")
            end = header.start("pairs") + pair.end("value")
            handled.add(start)
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if end - start >= 2 and text[start] == text[end - 1] == '"':
                start, end = start + 1, end - 1
            if end > start and _literal(text[start:end]):
                found.append(_span(text, start, end, "TOKEN"))
    return found, handled


def _escaped_fields(text: str) -> Iterator[Span]:
    """Decode JSON field names only; mask the original source value losslessly."""
    if "\\" not in text:
        return
    for field in _JSON_KEY.finditer(text):
        if "\\" not in field["name"]:
            continue
        try:
            name = json.loads('"' + field["name"] + '"')
        except ValueError:
            continue
        kind = credential_type(name)
        offsets = _assignment_value(text, field.end()) if kind else None
        if kind and offsets:
            start, end = offsets
            if _literal(text[start:end]):
                yield _span(text, start, end, kind)


def _concatenated_literals(text: str, end: int, kind: str) -> Iterator[Span]:
    """Protect each literal in a simple quoted credential concatenation."""
    cursor = end + 1  # end is just before the closing quote of the first literal
    while cursor < len(text):
        operator = _CONCAT.match(text, cursor)
        if operator is None:
            return
        cursor = operator.end()
        if text[cursor : cursor + 1] not in {'"', "'", "`"}:
            return
        offsets = _assignment_value(text, cursor)
        if offsets is None:
            return
        start, end = offsets
        if end > start and _literal(text[start:end]):
            yield _span(text, start, end, kind)
        cursor = end + 1


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
    query_spans, query_starts = _url_parameters(text)
    found.extend(query_spans)
    cookie_spans, cookie_starts = _cookies(text)
    found.extend(cookie_spans)
    query_starts.update(cookie_starts)
    block_spans, block_starts = _block_values(text)
    found.extend(block_spans)
    query_starts.update(block_starts)
    found.extend(_labelled_values(text))
    found.extend(_flag_values(text))
    found.extend(_escaped_fields(text))
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
        if assigned_type is None or cursor in query_starts:
            continue
        value = _assigned_value(text, assignment)
        if value is not None:
            start, end, quoted = value
            cursor = max(cursor, end)
            if _literal(text[start:end]):
                found.append(_span(text, start, end, assigned_type))
                if quoted:
                    for fragment in _concatenated_literals(text, end, assigned_type):
                        found.append(fragment)
                        cursor = max(cursor, fragment.end + 1)
    return found
