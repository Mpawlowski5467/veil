"""Claude Code hooks that go with the gateway.

The gateway puts real values back into the model's tool calls, so a tool can
act on them. That is what the user wants for a file edit, but a web request,
an MCP call, or a remote agent would send them off the machine, and a shell
command could too. A ``PreToolUse`` hook checks every call except the plain
file tools against the conversation's vault: a shell command that holds a
real value asks the user first, a call to a tool that works on this machine
goes ahead, and any other call holding one is refused (unless it is an MCP
tool allowed in the settings). A ``UserPromptSubmit`` hook holds a prompt
back unless Claude Code is still routed through this very gateway.

Both fail closed: any error refuses the call or holds the prompt back.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import secrets
import shlex
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from ..placeholders import placeholder_type
from ..vault.sqlite import SQLiteVault

LITERAL = "LITERAL"

#: Tools the hook doesn't need to see: they only read and write local files.
FILE_TOOLS = ("Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep")

#: The hook matcher: every tool but the file tools (Claude Code tests it as a
#: regular expression against the tool's name).
MATCHER = f"^(?!(?:{'|'.join(FILE_TOOLS)})$)"

#: Tools that run a command on this machine: real values in them need the
#: user's approval, since the command itself may send them anywhere.
SHELL_TOOLS = frozenset({"Bash", "PowerShell", "Monitor"})

#: Tools that keep what they are given on this machine (or in a subagent,
#: whose requests go through the same gateway).
LOCAL_TOOLS = frozenset(
    {
        *FILE_TOOLS,
        "Agent",
        "Task",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
        "TodoWrite",
        "AskUserQuestion",
        "EnterPlanMode",
        "ExitPlanMode",
        "EnterWorktree",
        "ExitWorktree",
        "ListAgents",
        "Skill",
    }
)

#: Settings that would send Claude Code's requests to another provider,
#: never through the gateway.
OTHER_PROVIDERS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_MANTLE",
)

#: The gateway route that proves it holds the secret, without being told it.
PROOF_PATH = "/_gateway/proof"

#: Authenticated local metadata; never returns prompts, mappings, or credentials.
STATUS_PATH = "/_gateway/status"


def proof(secret: str, nonce: str) -> str:
    """The gateway's answer to ``nonce``: only a holder of ``secret`` knows it."""
    return hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()


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


def _stays_here(tool: str, tool_input: Any) -> bool:
    if tool in ("Agent", "Task"):
        # A remote agent runs in the cloud, without the gateway.
        return not (
            isinstance(tool_input, dict) and tool_input.get("isolation") == "remote"
        )
    return tool in LOCAL_TOOLS


def pre_tool_use(
    payload: dict[str, Any], vault_path: Path, allowed_mcp_tools: Iterable[str] = ()
) -> dict[str, Any] | None:
    """Decide on one tool call; return the hook's answer, or None to let it be."""
    tool = payload.get("tool_name")
    session = payload.get("session_id")
    if not isinstance(tool, str) or not isinstance(session, str):
        return _decision("deny", "The call couldn't be checked for personal data.")
    tool_input = payload.get("tool_input")
    if tool in set(allowed_mcp_tools) or _stays_here(tool, tool_input):
        return None
    vault = SQLiteVault(vault_path, session=session)
    try:
        # Literal placeholder-shaped text isn't personal data.
        values = [v for p, v in vault.items() if placeholder_type(p) != LITERAL]
    finally:
        vault.close()
    found = count_values(tool_input, values)
    if not found:
        return None
    what = "a value" if found == 1 else f"{found} values"
    if tool in SHELL_TOOLS:
        return _decision(
            "ask",
            f"This command contains {what} of personal data that was masked from "
            "the model and put back for the command. Run it only if you expect it.",
        )
    return _decision(
        "deny",
        f"This call would send {what} of personal data off the machine, so it was "
        "refused. Use placeholders only in web requests, MCP calls, and remote "
        "tools, or ask the user to send the data themselves.",
    )


def _secret_from_environment() -> str | None:
    from .server import SECRET_HEADER

    for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == SECRET_HEADER:
            return value.strip()
    return None


def _gateway_answers(expected_url: str, secret: str) -> bool:
    """Ask the gateway at ``expected_url`` to prove it holds ``secret``."""
    address = urlsplit(expected_url)
    if address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port:
        return False
    nonce = secrets.token_hex(16)
    conn = http.client.HTTPConnection("127.0.0.1", address.port, timeout=3)
    try:
        conn.request("GET", f"{PROOF_PATH}?nonce={nonce}")
        response = conn.getresponse()
        answer = json.loads(response.read()) if response.status == 200 else {}
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        conn.close()
    got = answer.get("proof") if isinstance(answer, dict) else None
    return isinstance(got, str) and hmac.compare_digest(got, proof(secret, nonce))


def user_prompt_submit(
    payload: dict[str, Any], expected_url: str
) -> dict[str, Any] | None:
    """Hold the prompt back unless requests still go through this gateway."""
    problem = None
    secret = _secret_from_environment()
    if os.environ.get("ANTHROPIC_BASE_URL") != expected_url:
        problem = "Claude Code isn't sending its requests through the masking gateway"
    elif any(os.environ.get(name) for name in OTHER_PROVIDERS):
        problem = "Claude Code is set to use another model provider"
    elif secret is None or not _gateway_answers(expected_url, secret):
        problem = "the masking gateway isn't running (or something else has its port)"
    if problem is None:
        return None
    return {
        "decision": "block",
        "reason": f"{problem}, so this prompt was held back and nothing was sent.",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "suppressOriginalPrompt": True,
        },
    }


def _refuse(event: str) -> dict[str, Any]:
    if event == "user-prompt-submit":
        return {
            "decision": "block",
            "reason": "The masking check failed, so this prompt was held back.",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "suppressOriginalPrompt": True,
            },
        }
    return _decision("deny", "The call couldn't be checked for personal data.")


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
        answer = _refuse(event)
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
