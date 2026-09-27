"""A Claude Code hook for live tests: logs every call and answers from rules.

Usage, as a hook command: ``python probe_hook.py RULES.json LOG.jsonl``.

Each call appends ``{"event", "tool", "payload", "response", "exit", "pid",
"t0", "t1"}`` as one JSON line to the log, then answers with the first rule
that matches. A rule is a JSON object:

- ``event``: the hook event name, or ``"*"``.
- ``tool`` (optional): a regex that must fully match ``tool_name``.
- ``contains`` (optional): text the payload's JSON must contain.
- Then one action:
  - ``respond``: print this JSON object as it is.
  - ``replace_output``: a ``{old: new}`` map applied to every string in
    ``tool_response``; prints ``updatedToolOutput`` (same shape) if anything
    changed. ``field`` renames the output key, e.g. to
    ``updatedMCPToolOutput``.
  - ``replace_input``: the same for ``tool_input``; prints ``updatedInput``,
    plus ``permissionDecision``/``permissionDecisionReason`` when the rule
    has ``decision``/``reason``.
  - ``set_input``: a ``{key: value}`` map merged into ``tool_input``; prints
    ``updatedInput``.
- ``exit`` and ``stderr`` (optional): the exit code and text for stderr.
- ``sleep`` (optional): seconds to wait before answering.

Standard library only, so it runs on any Python 3.10+ without veil.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

T0 = time.time()


def _walk(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_walk(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _walk(item, replacements) for key, item in value.items()}
    return value


def _matches(rule: dict[str, Any], payload: dict[str, Any], raw: str) -> bool:
    event = rule.get("event", "*")
    if event not in ("*", payload.get("hook_event_name")):
        return False
    tool = rule.get("tool")
    if tool is not None and not re.fullmatch(tool, str(payload.get("tool_name"))):
        return False
    contains = rule.get("contains")
    return contains is None or contains in raw


def answer(
    rules: list[dict[str, Any]], payload: dict[str, Any], raw: str
) -> tuple[dict[str, Any] | None, int, str, float]:
    """Return (JSON response or None, exit code, stderr, sleep) for a payload."""
    for rule in rules:
        if not _matches(rule, payload, raw):
            continue
        event = payload.get("hook_event_name")
        response: dict[str, Any] | None = None
        if "respond" in rule:
            response = rule["respond"]
        elif "replace_output" in rule:
            original = payload.get("tool_response")
            updated = _walk(original, rule["replace_output"])
            if updated != original:
                field = rule.get("field", "updatedToolOutput")
                response = {
                    "hookSpecificOutput": {"hookEventName": event, field: updated}
                }
        elif "replace_input" in rule or "set_input" in rule:
            original = payload.get("tool_input") or {}
            updated = _walk(original, rule.get("replace_input", {}))
            updated = {**updated, **rule.get("set_input", {})}
            specific: dict[str, Any] = {"hookEventName": event}
            if updated != original:
                specific["updatedInput"] = updated
            if "decision" in rule:
                specific["permissionDecision"] = rule["decision"]
                specific["permissionDecisionReason"] = rule.get("reason", "probe")
            if len(specific) > 1:
                response = {"hookSpecificOutput": specific}
        return (
            response,
            int(rule.get("exit", 0)),
            str(rule.get("stderr", "")),
            float(rule.get("sleep", 0)),
        )
    return None, 0, "", 0.0


# Environment variables whose values are logged; any other CLAUDE* variable is
# logged by name only, since it could hold a token.
_SHOWN = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_ENV_FILE",
        "CLAUDE_PLUGIN_DATA",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_PROJECT_DIR",
        "HOME",
        "PWD",
    }
)


def _environment() -> dict[str, str]:
    return {
        key: os.environ[key] if key in _SHOWN or key.startswith("VEIL_PROBE") else "…"
        for key in sorted(os.environ)
        if key in _SHOWN or key.startswith(("CLAUDE", "VEIL_PROBE"))
    }


def main(argv: list[str]) -> int:
    """Run one hook call: read stdin, log it, answer from the rules file."""
    rules_path, log_path = argv
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {"_unparsed": raw}
    with open(rules_path, encoding="utf-8") as f:
        rules = json.load(f)
    response, code, stderr, delay = answer(rules, payload, raw)
    if delay:
        time.sleep(delay)
    record = {
        "event": payload.get("hook_event_name"),
        "tool": payload.get("tool_name"),
        "payload": payload,
        "response": response,
        "exit": code,
        "pid": os.getpid(),
        "t0": T0,
        "t1": time.time(),
        "env": _environment(),
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if response is not None:
        sys.stdout.write(json.dumps(response))
    if stderr:
        sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
