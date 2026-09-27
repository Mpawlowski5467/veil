"""Claude Code hooks that go with the gateway.

The gateway puts real values back into the model's tool calls, so a tool can
act on them. That is what the user wants for a file edit, but a web request or
an MCP call would send them off the machine, and a shell command could too. A
``PreToolUse`` hook checks those calls against the conversation's vault:
a shell command that holds a real value asks the user first, and a web or MCP
call is refused (unless the MCP tool is allowed in the settings). A
``UserPromptSubmit`` hook holds a prompt back if Claude Code isn't routed
through the gateway after all.

Both fail closed: any error refuses the call or holds the prompt back.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, TextIO

from ..placeholders import placeholder_type
from ..vault.sqlite import SQLiteVault

LITERAL = "LITERAL"

#: Tools whose calls are checked, as a hook matcher (a regex on tool names).
MATCHER = "Bash|WebFetch|WebSearch|mcp__.*"

# Variables that would send Claude Code's requests to another provider.
_OTHER_PROVIDERS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)


def count_values(tool_input: Any, values: Iterable[str]) -> int:
    """Count how many of ``values`` appear anywhere in a tool call's input."""
    texts = list(_strings(tool_input))
    return sum(1 for value in set(values) if any(value in text for text in texts))


def _decision(decision: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def pre_tool_use(
    payload: dict[str, Any], vault_path: Path, allowed_mcp_tools: Iterable[str] = ()
) -> dict[str, Any] | None:
    """Decide on one tool call; return the hook's answer, or None to let it be."""
    tool = payload.get("tool_name")
    session = payload.get("session_id")
    if not isinstance(tool, str) or not isinstance(session, str):
        return _decision("deny", "The call couldn't be checked for personal data.")
    if tool in set(allowed_mcp_tools):
        return None
    vault = SQLiteVault(vault_path, session=session)
    try:
        # Literal placeholder-shaped text isn't personal data.
        values = [v for p, v in vault.items() if placeholder_type(p) != LITERAL]
    finally:
        vault.close()
    found = count_values(payload.get("tool_input"), values)
    if not found:
        return None
    what = "a value" if found == 1 else f"{found} values"
    if tool == "Bash":
        return _decision(
            "ask",
            f"This command contains {what} of personal data that was masked from "
            "the model and put back for the command. Run it only if you expect it.",
        )
    return _decision(
        "deny",
        f"This call would send {what} of personal data off the machine, so it was "
        "refused. Use placeholders only in web requests and MCP calls, or ask the "
        "user to send the data themselves.",
    )


def user_prompt_submit(
    payload: dict[str, Any], expected_url: str
) -> dict[str, Any] | None:
    """Hold the prompt back unless requests still go through the gateway."""
    problem = None
    if os.environ.get("ANTHROPIC_BASE_URL") != expected_url:
        problem = "Claude Code isn't sending its requests through the masking gateway"
    elif any(os.environ.get(name) for name in _OTHER_PROVIDERS):
        problem = "Claude Code is set to use another model provider"
    if problem is None:
        return None
    return {
        "decision": "block",
        "reason": f"{problem}, so this prompt was held back and nothing was sent. "
        "Check the settings that set ANTHROPIC_BASE_URL.",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "suppressOriginalPrompt": True,
        },
    }


def run(
    event: str,
    *,
    stdin: TextIO,
    stdout: TextIO,
    vault_path: Path,
    expected_url: str | None = None,
    allowed_mcp_tools: Iterable[str] = (),
) -> int:
    """Answer one hook call read from ``stdin``; return the exit code (0).

    On any error the answer refuses the call (or holds the prompt back): a
    hook that crashed would let the call through.
    """
    try:
        payload = json.load(stdin)
        if not isinstance(payload, dict):
            raise TypeError("the hook input isn't a JSON object")
        if event == "pre-tool-use":
            answer = pre_tool_use(payload, vault_path, allowed_mcp_tools)
        elif event == "user-prompt-submit" and expected_url is not None:
            answer = user_prompt_submit(payload, expected_url)
        else:
            raise ValueError("unknown hook")
    except Exception:
        if event == "user-prompt-submit":
            answer = {
                "decision": "block",
                "reason": "The masking check failed, so this prompt was held back.",
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "suppressOriginalPrompt": True,
                },
            }
        else:
            answer = _decision(
                "deny", "The call couldn't be checked for personal data."
            )
    if answer is not None:
        stdout.write(json.dumps(answer))
    return 0


def hook_settings(command: list[str], expected_url: str) -> dict[str, Any]:
    """Return the ``hooks`` settings that run these hooks with ``command``."""

    def entry(*extra: str) -> dict[str, Any]:
        return {
            "type": "command",
            "command": shlex.join([*command, *extra]),
            "timeout": 30,
        }

    return {
        "PreToolUse": [{"matcher": MATCHER, "hooks": [entry("pre-tool-use")]}],
        "UserPromptSubmit": [
            {"hooks": [entry("user-prompt-submit", "--expect-url", expected_url)]}
        ],
    }
