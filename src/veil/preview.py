"""Ephemeral local masking preview: no gateway, persisted vault, or model call."""

from __future__ import annotations

import time
import webbrowser
from pathlib import Path
from typing import Any

from .detectors.literal import LiteralPlaceholderDetector
from .detectors.regex import RegexDetector
from .review_cli import ReviewServer
from .secret_review import CHOICES, REVIEW_TYPES, candidates
from .shield import Shield

MAX_TEXT = 32_768
_HTML = Path(__file__).with_name("preview.html").read_text(encoding="utf-8")
_REASONS = {
    "secret": "Recognized credential syntax or key format",
    "manual": "Your local preview choice",
    "literal": "Escaped literal placeholder for lossless restoration",
    "merged": "Overlapping detections protected together",
}


def validate_input(payload: dict[str, Any]) -> None:
    """Accept bounded text and explicit choices, never arbitrary registrations."""
    if set(payload) != {"text", "choices"}:
        raise ValueError
    text, choices = payload["text"], payload["choices"]
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError
    if not isinstance(choices, dict) or len(choices) > 100:
        raise ValueError
    for key, value in choices.items():
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not key.isdecimal()
            or len(key) > 3
            or str(int(key)) != key
            or not isinstance(value, str)
            or value not in CHOICES
        ):
            raise ValueError


def preview(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Run built-in rules in a fresh memory vault for this one input only."""
    if payload is None:
        return {"choices": list(CHOICES), "max_text": MAX_TEXT}
    validate_input(payload)
    text, choices = payload["text"], payload["choices"]
    detector = RegexDetector()
    shield = Shield(
        detectors=[
            LiteralPlaceholderDetector({*detector.entity_types, *REVIEW_TYPES}),
            detector,
        ],
        redact_warnings=True,
    )
    masked = shield.mask(text)
    visible = list(text)
    for entity in masked.entities:
        visible[entity.start : entity.end] = " " * (entity.end - entity.start)
    findings = candidates(text, visible="".join(visible))
    if any(int(index) >= len(findings) for index in choices):
        raise ValueError
    for index, finding in enumerate(findings):
        choice = choices.get(str(index))
        if choice and choice != "IGNORE":
            shield.add_entity(finding.value, choice)
    masked = shield.mask(text)
    restored = shield.restore(masked.text, tolerant=False)
    if restored.text != text or masked.warnings or restored.warnings:
        # Never present an unreliable result as a successful preview.
        raise ValueError
    return {
        "masked": masked.text,
        "restored": restored.text,
        "masks": [
            {
                "placeholder": entity.placeholder,
                "kind": entity.entity_type,
                "reason": _REASONS.get(
                    entity.source, "Matched a built-in privacy pattern"
                ),
            }
            for entity in masked.entities
        ],
        "findings": [
            {
                "index": index,
                "value": finding.value,
                "kind": finding.kind,
                "reason": finding.reason,
                "choice": choices.get(str(index)),
            }
            for index, finding in enumerate(findings)
        ],
        "unresolved": sum(str(index) not in choices for index in range(len(findings))),
    }


def preview_server() -> ReviewServer:
    """Reuse the review page's authenticated loopback transport and CSP."""
    return ReviewServer(
        preview,
        html=_HTML,
        max_body=262_144,
        validate=validate_input,
        error="Preview unavailable: shorten the input or reopen veil preview",
    )


def run_preview(*, no_browser: bool = False) -> int:
    """Serve a private ten-minute preview; never read personal configuration."""
    with preview_server() as server:
        print("Local preview: built-in rules only; no gateway or model call.")
        print("This does not verify routing for any conversation.")
        print("Private page expires in ten minutes. Ctrl-C stops it.")
        print(server.url, flush=True)
        if not no_browser:
            webbrowser.open(server.url)
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline:
                server.handle_request()
        except KeyboardInterrupt:
            pass
    return 0
