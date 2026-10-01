"""Local heuristics and bounded, request-scoped review of uncertain secrets.

This is a triage system, not a claim that arbitrary secrets can be recognized.
Candidates and decisions never leave the process except to an authenticated
local reviewer. No classifier, model call, or credential validation is used.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ._review_context import PII_REVIEW_TYPES, contextual_values, fragment_context
from .detectors._secrets import (
    _ASSIGNMENT,
    SECRET_TYPES,
    _assigned_value,
    _literal,
    credential_type,
)

REVIEW_PATH = "/_veil/review"
REVIEW_TYPES = (*SECRET_TYPES, *PII_REVIEW_TYPES)
# Preserve the original terminal choices, including 6=IGNORE.
CHOICES = (*SECRET_TYPES, "IGNORE", *PII_REVIEW_TYPES)
_MAX_CANDIDATES = 100
_MAX_VALUE = 16_384
# A whole token, never a substring cut from an oversized one.
_TOKEN = re.compile(r"(?<![\w/+.~-])[A-Za-z0-9_+/~.-]{24,}={0,2}(?![\w/+.=~-])")
_PROSE = re.compile(
    r"(?i:\b(?P<name>api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"password|passphrase|client[ _-]?secret|private[ _-]?key))"
    r"[ \t]+(?i:is|was|equals)[ \t]+(?P<value>[^\r\n]+)"
)
_FLAG = re.compile(
    r"(?<!\w)--(?P<name>password|passwd|passphrase|api-key|access-token|token|secret)"
    r"[ \t]+(?P<value>[^\r\n]+)"
)
_QUERY = re.compile(
    r"[?&](?P<name>password|api_key|apikey|access_token|token|secret)="
    r"(?P<value>[^\s&#\"'<>]+)",
    re.IGNORECASE,
)
_MULTILINE = re.compile(
    r"(?m)^[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_.-]{0,127}):[ \t]*[|>][+-]?[ \t]*\r?\n"
    r"(?P<value>(?:[ \t]+[^\r\n]+(?:\r?\n|$))+)"
)


@dataclass(frozen=True)
class Candidate:
    """A private value with a fixed, non-private explanation and suggested type."""

    value: str = field(repr=False)
    kind: str
    reason: str


def _value(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in {'"', "'", "`"}:
        quote = raw[0]
        # Retain escaped source spelling, including embedded whitespace.
        end = 1
        while end < len(raw):
            if raw[end] == "\\":
                end += 2
            elif raw[end] == quote:
                return raw[1:end]
            else:
                end += 1
        return raw[1:]
    return raw


def candidates(
    text: str, *, visible: str | None = None, fragments: bool = False
) -> tuple[Candidate, ...]:
    """Find uncertainty in supported text, without returning values in reprs.

    Prose values are conservatively the rest of the line when unquoted. Token
    randomness is only a reason to ask, never proof that a value is a secret.
    Hashes can be flagged too; silently allowing them would miss opaque keys.
    """
    found: dict[str, Candidate] = {}

    def exposed(value: str) -> bool:
        if visible is None:
            return True
        start = text.find(value)
        while start >= 0:
            if visible[start : start + len(value)].strip():
                return True
            start = text.find(value, start + len(value))
        return False

    fragments = fragments or fragment_context(visible if visible is not None else text)
    for value, contextual_kind, reason in contextual_values(text, fragments=fragments):
        if value and _literal(value) and exposed(value):
            found[value] = Candidate(value, contextual_kind, reason)
            _bounded(found)

    for rule, reason in (
        (_PROSE, "credential described in prose"),
        (_FLAG, "credential command-line option"),
        (_QUERY, "credential URL parameter"),
    ):
        for match in rule.finditer(text):
            value = _value(match["value"])
            if value and _literal(value) and exposed(value):
                found[value] = Candidate(
                    value,
                    credential_type(match["name"].replace(" ", "_")) or "CREDENTIAL",
                    reason,
                )
                _bounded(found)
    for match in _MULTILINE.finditer(text):
        kind = credential_type(match["name"])
        value = match["value"].rstrip("\r\n")
        if kind and exposed(value):
            found[value] = Candidate(value, kind, "multiline credential value")
            _bounded(found)
    for match in _ASSIGNMENT.finditer(text):
        kind = credential_type(match["name"])
        if kind is None:
            continue
        offsets = _assigned_value(text, match)
        if offsets is None or offsets[2]:
            continue
        start, end, _ = offsets
        line_end = text.find("\n", end)
        line_end = len(text) if line_end < 0 else line_end
        tail = text[end:line_end].strip()
        value = text[start:line_end].strip()
        if (
            tail
            and tail[0] not in "#,;"
            and _literal(text[start:end])
            and exposed(value)
        ):
            found[value] = Candidate(
                value, kind, "unquoted credential with trailing words"
            )
            _bounded(found)
    for match in _TOKEN.finditer(text):
        value = match[0].rstrip(".,;")
        if not (any(c.isalpha() for c in value) and any(c.isdigit() for c in value)):
            continue
        counts = Counter(value)
        entropy = -sum(
            (n / len(value)) * math.log2(n / len(value)) for n in counts.values()
        )
        if (
            entropy >= 3.5
            and len(counts) >= 12
            and not any(value in existing for existing in found)
            and exposed(value)
        ):
            found[value] = Candidate(value, "TOKEN", "long, varied token")
            _bounded(found)
    return tuple(found.values())


def request_candidates(texts: list[tuple[str, str]]) -> tuple[Candidate, ...]:
    """Review supported fields together without joining unrelated text into values."""
    fragments = any(fragment_context(visible) for _, visible in texts)
    found: dict[str, Candidate] = {}
    for text, visible in texts:
        for candidate in candidates(text, visible=visible, fragments=fragments):
            found[candidate.value] = candidate
            _bounded(found)
    return tuple(found.values())


def _bounded(found: dict[str, Candidate]) -> None:
    if (
        len(found) > _MAX_CANDIDATES
        or any(len(v) > _MAX_VALUE for v in found)
        or sum(map(len, found)) > 256 * 1024
    ):
        raise ReviewError("too many or oversized findings; split the input and retry")


class ReviewError(ValueError):
    """A safe explanation that contains no prompt, candidate, or credential."""


@dataclass
class _Review:
    id: str
    expires: float
    session: bytes = field(repr=False)
    findings: tuple[Candidate, ...] = field(repr=False)
    choices: dict[int, str] = field(default_factory=dict)


class ReviewQueue:
    """Volatile, bounded decisions for exactly one session and request body.

    Expiry, eviction, or restart can only cause a new review, never permission
    to send. Ignore decisions cannot carry over to a changed prompt/session.
    """

    def __init__(self, *, ttl: float = 600, limit: int = 64) -> None:
        """Keep at most ``limit`` reviews for ``ttl`` seconds."""
        if ttl <= 0 or limit < 1:
            raise ValueError("review lifetime and capacity must be positive")
        self._ttl = ttl
        self._limit = limit
        self._salt = secrets.token_bytes(32)
        self._entries: OrderedDict[bytes, _Review] = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, session: str, body: bytes) -> bytes:
        digest = hmac.new(self._salt, digestmod=hashlib.sha256)
        session_bytes = session.encode("utf-8", "surrogatepass")
        digest.update(len(session_bytes).to_bytes(8, "big"))
        digest.update(session_bytes)
        digest.update(body)
        return digest.digest()

    def _purge(self) -> None:
        now = time.monotonic()
        for key, review in list(self._entries.items()):
            if review.expires <= now:
                del self._entries[key]

    def decisions(self, session: str, body: bytes) -> dict[str, str] | None:
        """Return complete decisions only; unresolved/expired entries return None."""
        with self._lock:
            self._purge()
            review = self._entries.get(self._key(session, body))
            if review is None or len(review.choices) != len(review.findings):
                return None
            return {f.value: review.choices[i] for i, f in enumerate(review.findings)}

    def confirmed(self, session: str) -> dict[str, str]:
        """Learn explicit masking choices within a session, even on changed retries.

        This never authorizes a release: ignore choices stay request-scoped.
        A client may add request metadata between the refusal and retry.
        """
        with self._lock:
            self._purge()
            session_key = self._key(session, b"")
            return {
                review.findings[i].value: choice
                for review in self._entries.values()
                if review.session == session_key
                for i, choice in review.choices.items()
                if choice != "IGNORE"
            }

    def hold(self, session: str, body: bytes, findings: tuple[Candidate, ...]) -> str:
        """Deduplicate an unresolved request and return its random review ID."""
        unique = {f.value: f for f in findings}
        _bounded(unique)
        if not unique:
            raise ReviewError("no findings to review")
        with self._lock:
            self._purge()
            key = self._key(session, body)
            if key in self._entries:
                return self._entries[key].id
            review = _Review(
                secrets.token_hex(16),
                time.monotonic() + self._ttl,
                self._key(session, b""),
                tuple(unique.values()),
            )
            self._entries[key] = review
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)
            return review.id

    def report(self) -> dict[str, Any]:
        """Return private findings only to the authenticated local review UI."""
        with self._lock:
            self._purge()
            return {
                "reviews": [
                    {
                        "id": r.id,
                        "remaining_seconds": max(0, int(r.expires - time.monotonic())),
                        "complete": len(r.choices) == len(r.findings),
                        "findings": [
                            {
                                "index": i,
                                "value": f.value,
                                "kind": f.kind,
                                "reason": f.reason,
                                "choice": r.choices.get(i),
                            }
                            for i, f in enumerate(r.findings)
                        ],
                    }
                    for r in self._entries.values()
                ]
            }

    def decide(self, review_id: str, index: int, choice: str) -> None:
        """Record one explicit choice, refusing unknown or expired IDs."""
        if choice not in CHOICES or type(index) is not int:
            raise ReviewError("invalid review choice")
        with self._lock:
            self._purge()
            for review in self._entries.values():
                if review.id == review_id and 0 <= index < len(review.findings):
                    review.choices[index] = choice
                    return
        raise ReviewError("review is missing or expired; retry the original request")
