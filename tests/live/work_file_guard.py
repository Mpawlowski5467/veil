"""Allow a live probe to Write/Edit only its one declared temporary file."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def decision(payload: Any, target: Path) -> str:
    if not isinstance(payload, dict):
        return "deny"
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return "deny"
    supplied = tool_input.get("file_path")
    tool = payload.get("tool_name")
    try:
        if (
            isinstance(tool, str)
            and tool in {"Write", "Edit"}
            and isinstance(supplied, str)
            and Path(supplied).is_absolute()
            and Path(supplied).resolve() == target.resolve()
        ):
            return "allow"
    except (OSError, RuntimeError, ValueError):
        pass
    return "deny"


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = None
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision(payload, Path(sys.argv[1])),
                    "permissionDecisionReason": "Live probe temporary-file allowlist",
                }
            }
        )
    )
