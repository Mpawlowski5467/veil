"""Bounded in-memory request metadata and one-use client verification probes."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any

from ..placeholders import PLACEHOLDER_RE
from ..shield import Shield
from .ledger import Ledger

ACTIVITY_PATH = "/_gateway/activity"
VERIFY_PATH = "/_gateway/verify"
PROBE_TTL = 600
ACTIVITY_TTL = 3600
_TYPES = {
    "EMAIL",
    "PHONE",
    "IPV4",
    "IPV6",
    "CREDIT_CARD",
    "IBAN",
    "PERSON",
    "SSN",
    "API_KEY",
    "TOKEN",
    "PASSWORD",
    "PRIVATE_KEY",
    "CREDENTIAL",
}


def probe_prompt(token: str) -> str:
    """Build a fictional prompt; it contains no values from a conversation."""
    return (
        f"Veil verification {token}. Do not use tools. Reply with this email exactly:\n"
        f"veil-check-{token}@example.com"
    )


def _user_text(request: Any, api: str) -> str:
    if not isinstance(request, dict):
        return ""
    messages = request.get("input" if api == "openai" else "messages")
    if api == "openai" and isinstance(messages, str):
        return messages
    if not isinstance(messages, list) or not messages:
        return ""
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return ""
    content = last.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not all(
        isinstance(block, dict)
        and block.get("type") in {"text", "input_text"}
        and isinstance(block.get("text"), str)
        for block in content
    ):
        return ""
    return "\n".join(block["text"] for block in content)


@dataclass
class Observation:
    """Per-request facts, without prompt/reply text or original entity values."""

    session: str
    counts: dict[str, int]
    generation: str | None = None
    started_at: float = 0
    token: str | None = None
    placeholder: str | None = None
    masked: bool = False
    forwarded: bool = False
    restored: bool = False


class ObservedLedger:
    """Delegate replay storage and observe only the fictional probe round trip."""

    def __init__(self, ledger: Ledger, observation: Observation) -> None:
        """Bind the observer to one response, not to shared session state."""
        self.ledger = ledger
        self.observation = observation

    def record_text(self, restored: str, masked: str) -> None:
        """Notice an exact canary restoration while preserving normal replay."""
        self.ledger.record_text(restored, masked)
        item = self.observation
        if item.token and item.placeholder and item.placeholder in masked:
            canary = f"veil-check-{item.token}@example.com"
            if canary in restored and canary not in masked:
                item.restored = True

    def masked_text(self, restored: str) -> str | None:
        """Look up replay text through the original ledger."""
        return self.ledger.masked_text(restored)

    def record_tool_input(self, tool_use_id: str, restored: Any, masked: Any) -> None:
        """Preserve tool replay without counting tool inputs as visible replies."""
        self.ledger.record_tool_input(tool_use_id, restored, masked)

    def masked_tool_input(self, tool_use_id: str, restored: Any) -> Any | None:
        """Look up replay input through the original ledger."""
        return self.ledger.masked_tool_input(tool_use_id, restored)

    def record_seen(self, value: Any) -> None:
        """Preserve optional tracking of unchanged provider blocks."""
        record = getattr(self.ledger, "record_seen", None)
        if callable(record):
            record(value)

    def was_seen(self, value: Any) -> bool:
        """Look up provider blocks without observing their contents."""
        lookup = getattr(self.ledger, "was_seen", None)
        return bool(callable(lookup) and lookup(value))


class Activity:
    """Track at most 128 recent sessions and 64 ten-minute, one-use probes."""

    def __init__(self) -> None:
        """Start a new privacy boundary at every gateway process restart."""
        self.instance = secrets.token_hex(16)
        self._salt = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._probes: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _prune(self) -> None:
        now = time.monotonic()
        for key, item in list(self._sessions.items()):
            if now - item["updated"] > ACTIVITY_TTL:
                del self._sessions[key]
        while len(self._sessions) > 128:
            self._sessions.popitem(last=False)
        while len(self._probes) > 64:
            self._probes.popitem(last=False)

    def _session(self, alias: str) -> dict[str, Any]:
        item = self._sessions.setdefault(
            alias,
            {
                "session_ref": alias,
                "generation": secrets.token_hex(8),
                "requests": 0,
                "forwarded": 0,
                "completed": 0,
                "failed": 0,
                "placeholder_occurrences": {},
                "last_request_at": None,
                "last_verified_at": None,
                "verified_monotonic": None,
            },
        )
        item["updated"] = time.monotonic()
        self._sessions.move_to_end(alias)
        return item

    def create_probe(self) -> dict[str, Any]:
        """Register a challenge; this makes no provider request."""
        token = secrets.token_hex(16)
        with self._lock:
            self._probes[token] = {
                "verification_id": token,
                "state": "pending",
                "created": time.monotonic(),
                "expires_at": time.time() + PROBE_TTL,
                "session_ref": None,
                "masked": False,
                "forwarded": False,
                "restored": False,
                "completed": False,
                "verified_at": None,
            }
            self._prune()
            return {**self._probe(token), "prompt": probe_prompt(token)}

    def _probe(self, token: str) -> dict[str, Any]:
        item = self._probes.get(token)
        if item is None:
            return {"verification_id": token, "state": "unknown"}
        out = {key: value for key, value in item.items() if key != "created"}
        if time.monotonic() - item["created"] > PROBE_TTL:
            out["state"] = "expired"
        return out

    def probe(self, token: str) -> dict[str, Any]:
        """Return sanitized probe evidence, never an original session ID."""
        with self._lock:
            return {"gateway_instance": self.instance, **self._probe(token)}

    def begin(
        self, session_id: str, request: Any, masked: Any, shield: Shield, api: str
    ) -> Observation:
        """Observe a successfully masked request without retaining its contents."""
        alias = hmac.new(self._salt, session_id.encode(), hashlib.sha256).hexdigest()[
            :32
        ]
        encoded = json.dumps(masked)
        # Counts include replayed history and recognized placeholders in the body;
        # they are not a count of unique people or newly discovered values.
        counts: Counter[str] = Counter()
        occurrences = Counter(match[0] for match in PLACEHOLDER_RE.finditer(encoded))
        for placeholder, count in occurrences.items():
            match = PLACEHOLDER_RE.fullmatch(placeholder)
            assert match is not None
            kind = match["type"]
            if kind != "LITERAL" and shield.vault.get_value(placeholder) is not None:
                counts[kind if kind in _TYPES else "CUSTOM"] += count
        observed = Observation(alias, dict(counts))
        text = _user_text(request, api)
        with self._lock:
            self._prune()
            for token, probe in self._probes.items():
                if (
                    probe["state"] != "pending"
                    or time.monotonic() - probe["created"] > PROBE_TTL
                    or probe_prompt(token) not in text
                ):
                    continue
                observed.token = token
                observed.placeholder = shield.vault.get_placeholder(
                    f"veil-check-{token}@example.com"
                )
                observed.masked = bool(
                    observed.placeholder
                    and observed.placeholder in _user_text(masked, api)
                    and f"veil-check-{token}@example.com" not in encoded
                )
                probe.update(state="in_progress", session_ref=alias)
                break
            item = self._session(alias)
            observed.generation = item["generation"]
            observed.started_at = time.time()
            item["requests"] += 1
            item["last_request_at"] = observed.started_at
            self._prune()
        return observed

    def finish(self, observed: Observation, completed: bool) -> None:
        """Commit evidence only after relay and local restoration have finished."""
        with self._lock:
            self._prune()
            item = self._session(observed.session)
            # A bounded record may have been evicted while a request was running.
            if item["generation"] != observed.generation:
                item["requests"] += 1
                item["last_request_at"] = max(
                    item["last_request_at"] or 0, observed.started_at
                )
            item["forwarded"] += int(observed.forwarded)
            item["completed" if completed else "failed"] += 1
            if observed.forwarded:
                counts = Counter(item["placeholder_occurrences"])
                counts.update(observed.counts)
                item["placeholder_occurrences"] = dict(counts)
            probe = self._probes.get(observed.token or "")
            if probe is not None:
                verified = bool(
                    observed.masked
                    and observed.forwarded
                    and observed.restored
                    and completed
                )
                probe.update(
                    state="verified" if verified else "incomplete",
                    masked=observed.masked,
                    forwarded=observed.forwarded,
                    restored=observed.restored,
                    completed=completed,
                )
                if verified and time.monotonic() - probe["created"] <= PROBE_TTL:
                    probe["verified_at"] = item["last_verified_at"] = time.time()
                    item["verified_monotonic"] = time.monotonic()
            self._prune()

    def summary(self) -> dict[str, Any]:
        """Return bounded, volatile metadata only; no prompts, IDs, or values."""
        with self._lock:
            self._prune()
            sessions = []
            for item in reversed(self._sessions.values()):
                out = {
                    key: value
                    for key, value in item.items()
                    if key not in {"updated", "verified_monotonic", "generation"}
                }
                verified = item["verified_monotonic"]
                out["recently_verified"] = (
                    verified is not None and time.monotonic() - verified <= PROBE_TTL
                )
                sessions.append(out)
            return {
                "gateway_instance": self.instance,
                "sessions": sessions,
                "retention_seconds": ACTIVITY_TTL,
                "max_sessions": 128,
                "scope": "This gateway process only; session references are salted. "
                "Counts include repeated history, not unique values. "
                "Verification covers one request, not future requests "
                "or other traffic.",
            }
