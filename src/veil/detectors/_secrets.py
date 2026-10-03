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
# Run-together names need an unambiguous ending: "pass", "pwd", "passwd", "key"
# and an arbitrary "...token" also finish ordinary identifiers (bypass, OLDPWD,
# htpasswd, monkey, ERRORTOKEN), so those still need a separator.
_QUALIFIED_TOKEN = re.compile(
    r"(?:api|auth|access|refresh|bearer|session|oauth|bot|app|deploy|personal"
    r"|service|github|gitlab|slack|npm|pypi|hf|vault)token\Z"
)

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
_FORM_HEADER = re.compile(
    r"(?im)^[ \t]*content-type:[ \t]*application/x-www-form-urlencoded"
    r"(?:[ \t]*;[^\r\n]*|[ \t]*)\r?\n"
)
_HTTP_HEADER_LINE = re.compile(
    r"[ \t]*[A-Za-z0-9!#$%&'*+.^_`|~-]+:[^\r\n]*(?:\r?\n|\Z)"
)
_FORM_PARAMETER = re.compile(r"(?:^|&)(?P<name>[^&=\s]+)=(?P<value>[^&]*)")
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
# Namespaced auth/session cookies have explicit semantics. A bare "access"
# suffix does not: feature_access and tenant_access both remain outside this rule.
_SESSION_COOKIE = re.compile(
    r"(?:[a-z0-9]+[_.-])+(?:auth|session|sessionid|session_id)\Z"
)
_BASE64_ENCODING = re.compile(r'(?i:"encoding"[ \t\r\n]*:[ \t\r\n]*"base64(?:url)?")')
_BASE64_TEXT = re.compile(r"[A-Za-z0-9_+/-]+={0,2}\Z")
_PAYLOAD_SUFFIX = re.compile(r"(?:[_.-](?i:payload)|Payload)\Z")
_JSON_VALUE_START = re.compile(r'[ \t\r\n]*"')
_JSON_KEY = re.compile(r'"(?P<name>(?:[^"\\\r\n]|\\[^\r\n]){1,512})"[ \t]*:[ \t]*')
# A key and string value inside stringified JSON: the same backslash run escapes
# each quote, e.g. {\"password\": \"...\"} in a Lambda event or HAR body. The
# leading literal backslash keeps the scan fast; the lookbehind after it makes
# the run start there.
_ESCAPED_KEY = re.compile(
    r'\\(?<!\\\\)(?P<escape>\\{0,14})"(?P<name>[A-Za-z_][A-Za-z0-9_.-]{0,127})'
    r'\\(?P=escape)"[ \t]*:[ \t]*\\(?P=escape)"'
)
# PHP/Perl hash keys, Perl -named arguments and Ruby symbols before '=>'; only
# quoted values follow. Arrow functions make '=>' common, so keys are read back
# from each arrow.
_HASH_ROCKET = re.compile(r"=>[ \t]*(?=[\"'])")
_ROCKET_KEY = re.compile(
    r"(?<![\w.$-])(?P<symbol>[:-]?)(?P<quote>[\"']?)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.-]{0,127})(?P=quote)\Z"
)
# A bare key's literal must end the item, unlike an arrow function's body.
_ROCKET_END = re.compile(r"[ \t]*(?:[,;)\]}#]|\r?\n|\Z)")
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
# Bare values after a credential name that are types or literals, not
# credentials. Never add ordinary words that are also weak passwords (admin,
# secret, postgres): those stay protected.
_CODE_WORD_LIST = (
    "None null nil NULL nullptr undefined true false ~ "
    "str string String Str int bytes bool boolean Boolean number Number "
    "any Any unknown object Object char byte float double long Integer Long "
    "rune u8 u16 u32 u64 u128 usize i8 i16 i32 i64 i128 isize f32 f64 "
    "int32 int64 uint float64 Option Optional Vec Box Arc Rc Cow "
    "SecretStr SecretBytes SecretString SecureString CharSequence"
)
_CODE_WORDS = frozenset(_CODE_WORD_LIST.split())
# Credential names on their own, compared without case, '_', '.' or '-'.
_CREDENTIAL_WORD_LIST = (
    "password passwd passphrase pwd pass secret secretkey apikey token privatekey"
)
_CREDENTIAL_WORDS = frozenset(_CREDENTIAL_WORD_LIST.split())
# Reference, pointer, slice and nullable marks around a type name.
_TYPE_DECORATION = re.compile(r"\A(?:&(?:mut)?|\*|\[\])+|(?:\[\]|\?)+\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", re.ASCII)
_COMPOUND = re.compile(r"[_.]|[a-z0-9][A-Z]")
# A capitalized type glued to '<': Option<String>, Secret<String>.
_GENERIC = re.compile(r"(?:\w+::)*[A-Z]\w*", re.ASCII)


def credential_type(name: str) -> str | None:
    """Classify an explicit credential field, retaining normal code identifiers."""
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    parts = re.split(r"[_.-]+", separated.lower())
    last = parts[-1]
    joined = "_".join(parts)
    if last in {"password", "passwd", "passphrase", "pwd", "pass"} or last.endswith(
        ("password", "passphrase")
    ):
        return "PASSWORD"
    if joined.endswith("private_key") or last.endswith("privatekey"):
        return "PRIVATE_KEY"
    if (
        joined.endswith(("api_key", "access_key_id", "secret_access_key"))
        or last in {"apikey", "secret", "secretkey"}
        or last.endswith(("apikey", "secretkey"))
        or joined.endswith("secret_key")
    ):
        return "API_KEY"
    if last == "token" or _QUALIFIED_TOKEN.search(last) is not None:
        return "TOKEN"
    return None


def _fold(value: str) -> str:
    return re.sub(r"[_.-]", "", value.lower())


def _name_words(name: str) -> set[str]:
    """Each word of a field name, and all of them run together."""
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    parts = [part for part in re.split(r"[_.-]+", separated.lower()) if part]
    return {*parts, "".join(parts)}


def code_value(value: str, name: str | None = None) -> bool:
    """Whether an unquoted value after a credential field reads as code.

    Code is a type (``String``, ``&str``, ``Option``), a repeat of the field
    name or one of its words (``api_key=API_KEY``, ``POSTGRES_PASSWORD:
    postgres``), or an identifier named after a credential
    (``password=db_password``). Without ``name``, only types and credential
    words on their own count, so a compound value such as ``Admin_Password``
    stays protected.
    """
    if value in {"&", "&mut"}:  # a reference before a lifetime or 'mut'
        return True
    if re.split(r"::|\.", _TYPE_DECORATION.sub("", value))[-1] in _CODE_WORDS:
        return True
    if not _IDENTIFIER.fullmatch(value):
        return False
    if name is None:
        return _fold(value) in _CREDENTIAL_WORDS
    if credential_type(value) and _COMPOUND.search(value):
        return True
    return _fold(value) in _name_words(name)


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
    text: str,
    start: int,
    *,
    clause: bool = False,
    closer: str = "",
    name: str | None = None,
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
        # TOML/Python multiline strings begin with three quotes. Reading the
        # second quote as an empty value would leave the entire secret visible.
        width = 3 if quote != "`" and text.startswith(quote * 3, start) else 1
        delimiter = quote * width
        end = start + width
        while end < len(text):
            if text[end] == "\\":
                end += 2
            elif text.startswith(delimiter, end):
                # TOML permits one or two literal quotes just before a triple
                # closing delimiter. Include them in the protected value.
                if width == 3:
                    run_end = end + width
                    while run_end < len(text) and text[run_end] == quote:
                        run_end += 1
                    if run_end - end in (4, 5):
                        end = run_end - width
                return start + width, end
            else:
                end += 1
        # Truncated quoted input: protect through the end, not a partial token.
        return start + width, len(text)
    match = _BARE.match(text, start)
    if match is None:
        return None
    value = match[0].rstrip(",;")
    next_char = match.end()
    while next_char < len(text) and text[next_char] in " \t":
        next_char += 1
    if (
        (next_char < len(text) and text[next_char] in "([")
        or (text[match.end() : match.end() + 1] == "<" and _GENERIC.fullmatch(value))
        or _CODE_REFERENCE.fullmatch(value)
        # Code is a type, an echo of the field name, or a credential-named
        # identifier. Quoted lookalikes are literals and were handled above.
        # Other bare identifiers stay protected: they may be weak passwords.
        or code_value(value, name)
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
        offsets = _assignment_value(
            text, value, clause=True, closer=closer, name=match["name"]
        )
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
    offsets = _assignment_value(
        text, start, clause=clause, closer=closer, name=match["name"]
    )
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


def _form_values(text: str) -> tuple[list[Span], set[int]]:
    """Read a single form-encoded body line after explicit pasted HTTP headers.

    Only field names are percent-decoded, once. Values keep their source bytes;
    an ampersand ends a field, while %26 and '+' remain inside its value.
    """
    found, handled = [], set()
    cursor = 0
    while header := _FORM_HEADER.search(text, cursor):
        cursor = header.end()
        # Consume each header block only once, including when its separator is
        # missing. Repeated Content-Type lines must not cause quadratic rescans.
        while extra := _HTTP_HEADER_LINE.match(text, cursor):
            cursor = extra.end()
        if text.startswith("\r\n", cursor):
            cursor += 2
        elif text.startswith("\n", cursor):
            cursor += 1
        else:
            continue
        end = text.find("\n", cursor)
        end = len(text) if end < 0 else end
        body = text[cursor:end].rstrip("\r")
        for parameter in _FORM_PARAMETER.finditer(body):
            name = urllib.parse.unquote_plus(parameter["name"])
            kind = credential_type(name) or credential_type(name.lower())
            if kind is None:
                continue
            start = cursor + parameter.start("value")
            value_end = cursor + parameter.end("value")
            handled.add(start)
            if value_end > start and _literal(text[start:value_end]):
                found.append(_span(text, start, value_end, kind))
        cursor = end
    return found, handled


def _encoded_credential_values(text: str) -> Iterator[Span]:
    """Mask base64-shaped JSON credential payloads only with an encoding marker.

    This is explicit naming/context, not recursive decoding or entropy guessing.
    JSON escapes are decoded to check the spelling; the entire original escaped
    value is protected and later restored exactly.
    """
    if _BASE64_ENCODING.search(text) is None:
        return
    for field in _JSON_KEY.finditer(text):
        try:
            name = json.loads('"' + field["name"] + '"')
        except ValueError:
            continue
        suffix = _PAYLOAD_SUFFIX.search(name)
        if suffix is None or not (kind := credential_type(name[: suffix.start()])):
            continue
        opening = _JSON_VALUE_START.match(text, field.end())
        if opening is None:
            continue
        offsets = _assignment_value(text, opening.end() - 1)
        if offsets is None:
            continue
        start, end = offsets
        if end == len(text):  # An unfinished JSON string is not validated here.
            continue
        try:
            value = json.loads('"' + text[start:end] + '"')
        except ValueError:
            continue
        # Accept padded and unpadded base64/base64url spellings without decoding
        # the payload. Bounds/padding reject short examples and malformed shapes.
        if (
            len(value) >= 8
            and _BASE64_TEXT.fullmatch(value)
            and len(value.rstrip("=")) % 4 != 1
            and ("=" not in value or len(value) % 4 == 0)
        ):
            yield _span(text, start, end, kind)


def _cookies(text: str) -> tuple[list[Span], set[int]]:
    """Recognize session/auth cookies in pasted HTTP headers, not theme cookies."""
    found, handled = [], set()
    for header in _COOKIE_HEADER.finditer(text):
        for pair in _COOKIE_PAIR.finditer(header["pairs"]):
            name = pair["name"].lower()
            for prefix in ("__host-", "__secure-"):
                if name.startswith(prefix):
                    name = name[len(prefix) :]
            kind = credential_type(name)
            if name in _COOKIE_NAMES or _SESSION_COOKIE.fullmatch(name):
                kind = "TOKEN"
            if kind is None:
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
                found.append(_span(text, start, end, kind))
    return found, handled


def _escaped_string_end(text: str, start: int, depth: int) -> int:
    """Find the end of a string value written at one JSON escape depth."""
    # At depth k = 2**n - 1, a run of r backslashes before '"' encodes
    # j = (r - k) / (k + 1) inner backslashes; the quote closes the value when
    # j is even. Encoded trailing backslashes stay inside the value.
    end = start
    while end < len(text):
        if text[end] in '"\r\n':
            return end
        if text[end] != "\\":
            end += 1
            continue
        run_end = end
        while run_end < len(text) and text[run_end] == "\\":
            run_end += 1
        if run_end < len(text) and text[run_end] == '"':
            run = run_end - end
            if run < depth:  # An enclosing string ended: malformed input.
                return end
            inner, remainder = divmod(run - depth, depth + 1)
            if remainder == 0 and inner % 2 == 0:
                return run_end - depth
            run_end += 1
        end = run_end
    # Truncated input: protect through the end, not a partial value.
    return len(text)


def _escaped_fields(text: str) -> Iterator[Span]:
    """Decode escaped JSON field names and stringified JSON keys.

    The original source value is masked, so restoration is lossless.
    """
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
        offsets = _assignment_value(text, field.end(), name=name) if kind else None
        if kind and offsets:
            start, end = offsets
            if _literal(text[start:end]):
                yield _span(text, start, end, kind)
    cursor = 0
    while key := _ESCAPED_KEY.search(text, cursor):
        start = cursor = key.end()
        depth = len(key["escape"]) + 1
        # Only 1, 3, 7 and 15 backslashes are JSON escape levels one to four.
        if depth not in {1, 3, 7, 15}:
            continue
        kind = credential_type(key["name"])
        if kind is None:
            continue
        end = _escaped_string_end(text, start, depth)
        cursor = max(start, end)
        if end > start and _literal(text[start:end]):
            yield _span(text, start, end, kind)


def _hash_rocket_values(text: str) -> Iterator[Span]:
    """Read quoted PHP, Ruby and Perl '=>' values; arrow functions stay readable."""
    cursor = 0
    while arrow := _HASH_ROCKET.search(text, cursor):
        key_end = arrow.start()
        while key_end > cursor and text[key_end - 1] in " \t":
            key_end -= 1
        # The longest key is a ':' or '-', two quotes and a 128-character name.
        key = _ROCKET_KEY.search(text, max(cursor, key_end - 131), key_end)
        cursor = arrow.end()
        if key is None or (kind := credential_type(key["name"])) is None:
            continue
        offsets = _assignment_value(text, cursor)
        if offsets is None:
            continue
        start, end = offsets
        cursor = end + 1
        # Truncated quoted input is protected through the end, as quoted '='
        # and ':' assignments are; nothing remains to scan after it.
        truncated = end == len(text)
        # A dash key (-password) is a Perl bareword too.
        bare = key["symbol"] != ":" and not key["quote"]
        if bare and not truncated and _ROCKET_END.match(text, cursor) is None:
            continue
        if _literal(text[start:end]):
            yield _span(text, start, end, kind)


def _concatenated_literals(
    text: str, start: int, end: int, kind: str
) -> Iterator[Span]:
    """Protect each literal in a simple quoted credential concatenation."""
    width = 3 if text[max(0, start - 3) : start] in {'"""', "'''"} else 1
    cursor = end + width
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
        width = 3 if text[max(0, start - 3) : start] in {'"""', "'''"} else 1
        cursor = end + width


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
    form_spans, form_starts = _form_values(text)
    found.extend(form_spans)
    query_starts.update(form_starts)
    cookie_spans, cookie_starts = _cookies(text)
    found.extend(cookie_spans)
    query_starts.update(cookie_starts)
    block_spans, block_starts = _block_values(text)
    found.extend(block_spans)
    query_starts.update(block_starts)
    found.extend(_labelled_values(text))
    found.extend(_flag_values(text))
    found.extend(_escaped_fields(text))
    found.extend(_encoded_credential_values(text))
    found.extend(_hash_rocket_values(text))
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
                # An empty/reference prefix can still be followed by a real
                # literal fragment. It must not exempt the rest of the value.
                for fragment in _concatenated_literals(text, start, end, assigned_type):
                    found.append(fragment)
                    cursor = max(cursor, fragment.end + 1)
    return found
