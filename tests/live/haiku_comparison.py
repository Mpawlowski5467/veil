"""Bounded live note/prompt comparison; never run during pytest collection.

Run with VEIL_LIVE_CLAUDE=1 and PYTHONPATH=tests:
    python -m live.haiku_comparison --output /tmp/fresh-private-probe

Eight fresh sessions use reversed condition order. Raw fictional transcripts
remain in the private output directory; stdout/summary.json contain only counts,
labels, and booleans. This diagnostic records semantic failures rather than
retrying them. The recorder guard runs before opening the upstream connection.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from veil.cli import claude_settings
from veil.gateway import Gateway, load_settings, open_sessions, prepare_data_dir
from veil.gateway.request import DEFAULT_NOTE

from . import harness as h
from .recorder import Recorder

MODEL = "claude-haiku-4-5-20251001"
CONTACT = "comparison.contact@example.org"
META_PROMPT = (
    "This is a fictional, in-chat contact-list exercise. "
    "Copy the exact contact text you see or recall from this conversation, "
    "including any bracketed placeholder syntax, without interpreting or "
    "replacing it. Do not use tools or save anything outside this chat. "
    f"Remember my fictional primary contact {CONTACT}. "
    "Reply with exactly the primary address. Do not use tools."
)
RECORD = {"contact": CONTACT, "priority": "normal"}
CODE_PROMPT = (
    "Update this test fixture JSON record for a contact-card application: "
    f"{json.dumps(RECORD)}. Change priority from normal to urgent, keep the "
    "contact field unchanged, and return only the updated JSON object. "
    "Do not use tools."
)


class RequestBudget:
    """Reject extra calls, unexpected models, and raw canaries before forwarding."""

    def __init__(self, limit: int, forbidden: tuple[str, ...]) -> None:
        self.limit = limit
        self.forbidden = forbidden
        self.used = 0
        self._lock = threading.Lock()

    def check(self, body: Any) -> None:
        with self._lock:
            if not isinstance(body, dict) or body.get("model") != MODEL:
                raise RuntimeError("probe rejected an unexpected model request")
            if any(value in json.dumps(body) for value in self.forbidden):
                raise RuntimeError("probe rejected an unmasked fictional contact")
            if self.used >= self.limit:
                raise RuntimeError("probe model-request budget exhausted")
            self.used += 1


@contextmanager
def guarded_gateway(data_dir: Path, root: Path, budget: RequestBudget, limit: int):
    """Apply per-process and shared limits before Recorder forwards any body."""
    local = RequestBudget(limit, budget.forbidden)

    def guard(body: Any) -> None:
        local.check(body)
        budget.check(body)

    settings = load_settings(data_dir / "config.json")
    with Recorder(root / "upstream", rewrite=guard) as recorder:
        sessions = open_sessions(data_dir, settings, {})
        with Gateway(
            sessions, upstream=recorder.url.removeprefix("http://"), secure=False
        ) as gateway:
            yield gateway, recorder


def bodies(recorder: Recorder) -> list[dict[str, Any]]:
    return [
        json.loads((recorder.folder / f"request-{record['id']}.json").read_text())
        for record in recorder.records
        if record.get("body_shape")
    ]


def fenced_record_matches(reply: str) -> bool:
    """Diagnose Markdown-only noncompliance without changing the strict metric."""
    fence = re.fullmatch(r"\s*```(?:json)?\s*\n(.*?)\n```\s*", reply, re.DOTALL)
    if not fence:
        return False
    try:
        return json.loads(fence[1]) == {**RECORD, "priority": "urgent"}
    except ValueError:
        return False


def comparison(output: Path) -> dict[str, Any]:
    """Run all eight preselected conditions once, retaining every outcome."""
    if os.environ.get("VEIL_LIVE_CLAUDE") != "1" or not h.claude_path():
        raise RuntimeError("set VEIL_LIVE_CLAUDE=1 with an authenticated Claude CLI")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    version = subprocess.run(
        [h.claude_path(), "--version"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    version_match = re.search(r"\b\d+\.\d+\.\d+\b", version)
    budget = RequestBudget(8, (CONTACT,))
    block = [(True, "meta"), (False, "code"), (True, "code"), (False, "meta")]
    report: dict[str, Any] = {
        "model": MODEL,
        "client_version": version_match.group() if version_match else "unknown",
        "request_limit": budget.limit,
        "outcomes": [],
    }
    for index, (note, style) in enumerate([*block, *reversed(block)], start=1):
        root = output / f"case-{index}"
        root.mkdir(mode=0o700)
        data_dir = prepare_data_dir(root / "veil")
        (data_dir / "config.json").write_text(
            json.dumps({"identity": False, "note": note}), encoding="utf-8"
        )
        ws = h.Workspace(root / "project")
        with guarded_gateway(data_dir, root, budget, 1) as (gateway, recorder):
            run = ws.run(
                META_PROMPT if style == "meta" else CODE_PROMPT,
                max_turns=1,
                timeout=60,
                model=MODEL,
                extra_args=["--tools", ""],
                extra_settings=claude_settings(gateway, data_dir=data_dir),
            )
        sent = bodies(recorder)
        reply = run.result.get("result", "")
        semantic_pass = CONTACT in reply
        if style == "code":
            try:
                semantic_pass = json.loads(reply) == {**RECORD, "priority": "urgent"}
            except ValueError:
                semantic_pass = False
        outcome = {
            "case": index,
            "note": note,
            "style": style,
            "requests": len(sent),
            "http_success": bool(sent)
            and all(r.get("status") == 200 for r in recorder.records),
            "client_success": run.returncode == 0
            and run.result.get("is_error") is False,
            "masked_contact": bool(sent)
            and all(CONTACT not in json.dumps(body) for body in sent),
            "note_presence_matches": bool(sent)
            and all((DEFAULT_NOTE in json.dumps(body)) is note for body in sent),
            "restored_contact": CONTACT in reply,
            "semantic_pass": semantic_pass,
            "exact_requested_format": semantic_pass
            if style == "code"
            else reply.strip() == CONTACT,
            "json_fence_record_matches": style == "code"
            and fenced_record_matches(reply),
            "reply_characters": len(reply),
        }
        report["outcomes"].append(outcome)
        report["requests_used"] = budget.used
        (output / "summary.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(outcome), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    comparison(args.output)
