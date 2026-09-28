"""Fictional request bodies shaped like Claude Code's, for golden masking tests.

Each body follows a shape recorded from Claude Code (see
``tests/gateway_payloads``), with made-up content. ``tests/test_gateway_golden.py``
masks them and compares the result with ``gateway_payloads/golden.json``, which
was first written by the 0.4.0 masker: masking must not change for traffic an
older release accepted, except where a change is intended and recorded there.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

EMAIL = "jane.doe@example.com"
ACCOUNT = "account.owner@example.com"
OTHER_EMAIL = "ada.q@example.com"
NAME = "Jan Nowak"
PHONE = "(555) 555-0100"
UA_VERSION = "2.1.283"
PROMPT = f"My name is {NAME}; mail {EMAIL} or call {PHONE}."
TITLE_PROMPT = f"Help {NAME} fix the build ({EMAIL})."


def fingerprint(prompt: str) -> str:
    """Claude Code's (2.1.283) short hash of the first prompt, for the billing line."""
    picked = "".join(prompt[i] if i < len(prompt) else "0" for i in (4, 7, 20))
    data = f"59cf53e54c78{picked}{UA_VERSION}".encode()
    return hashlib.sha256(data).hexdigest()[:3]


def billing(prompt: str) -> str:
    """Claude Code's billing line for a conversation that began with ``prompt``."""
    return (
        f"x-anthropic-billing-header: cc_version={UA_VERSION}.{fingerprint(prompt)}; "
        "cc_entrypoint=cli; cch=00000; "
        "cc_prompt_id=0f3c1a2b-1111-4222-8333-444455556666;"
    )


BILLING = billing(PROMPT)
DEVICE = "d3adb33f" * 8
USER_ID = json.dumps(
    {
        "device_id": DEVICE,
        "account_uuid": "11111111-2222-4333-8444-555555555555",
        "session_id": "66666666-7777-4888-9999-000000000000",
    }
)

#: Registered values for each case set: the usual name, and short lower-case
#: values (like a git user.name) that hit inside protocol words.
REGISTERED = {
    "name": {NAME: "PERSON"},
    "short": {NAME: "PERSON", "pat": "USER", "dan": "USER", "ada": "USER"},
}


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "Read",
            "description": "Reads a file from the local filesystem.",
            "input_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The absolute path to the file to read",
                    },
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "exclusiveMinimum": 0},
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "Bash",
            "description": "Executes a bash command. Use the timeout parameter.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number", "maximum": 600000},
                    "description": {"type": "string"},
                    "run_in_background": {"type": "boolean"},
                    "dangerouslyDisableSandbox": {"type": "boolean"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "Grep",
            "description": "Searches file contents with a regular expression.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                    },
                    "-A": {"type": "number"},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "mcp__contacts__find_contact",
            "description": f"Finds a contact, e.g. {OTHER_EMAIL}.",
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
    ]


def _system(billing: str = BILLING) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": billing},
        {
            "type": "text",
            "text": "You are Claude Code. Write each placeholder exactly.",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
        {
            "type": "text",
            "text": f"Working directory: /work\nThe user is {ACCOUNT}.",
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _history() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"<system-reminder>{ACCOUNT}</system-reminder>",
                },
                {
                    "type": "text",
                    "text": PROMPT,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "I should read the notes of [PERSON_1].",
                    "signature": "EqQBCkgIBxABGAIqQJ1fixtureSignature==",
                },
                {"type": "text", "text": f"I'll read the notes for {NAME}."},
                {
                    "type": "tool_use",
                    "id": "toolu_01FixtureRead00000000000",
                    "name": "Read",
                    "input": {"file_path": "/work/notes.txt", "limit": 2000},
                    "caller": {"type": "direct"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01FixtureRead00000000000",
                    "content": f"Name: {NAME}\nEmail: {EMAIL}\nPhone: {PHONE}\n",
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": "RWRhY3RlZEZpeHR1cmU="},
                {
                    "type": "tool_use",
                    "id": "toolu_01FixtureBash00000000000",
                    "name": "Bash",
                    "input": {
                        "command": "cat notes.txt; exit 1",
                        "description": "Show the notes",
                        "timeout": 120000,
                    },
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01FixtureBash00000000000",
                    "content": [
                        {"type": "text", "text": f"Exit code 1\n{NAME}, {EMAIL}"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                            },
                        },
                    ],
                    "is_error": True,
                },
                {"type": "text", "text": "Now summarize the file path and pattern."},
            ],
        },
        {"role": "system", "content": f"Reminder: {EMAIL} is the contact."},
    ]


def haiku_print() -> dict[str, Any]:
    """``claude -p`` on Haiku: no thinking budget, clear_thinking edits."""
    return {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 32000,
        "stream": True,
        "metadata": {"user_id": USER_ID},
        "thinking": {"type": "enabled", "budget_tokens": 31999},
        "context_management": {
            "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
        },
        "tools": _tools(),
        "system": _system(),
        "messages": _history(),
    }


def sonnet_interactive() -> dict[str, Any]:
    """An interactive turn: adaptive thinking, effort, temperature."""
    body = haiku_print()
    body["model"] = "claude-sonnet-5"
    body["thinking"] = {"type": "adaptive", "display": "omitted"}
    body["output_config"] = {"effort": "high"}
    body["temperature"] = 1
    return body


def title_generation() -> dict[str, Any]:
    """The session-title request interactive mode makes first."""
    return {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "stream": True,
        "metadata": {"user_id": USER_ID},
        "system": [
            {"type": "text", "text": billing(TITLE_PROMPT)},
            {"type": "text", "text": "Generate a short title for this session."},
        ],
        "messages": [{"role": "user", "content": TITLE_PROMPT}],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                    "additionalProperties": False,
                },
            }
        },
    }


def structured_output() -> dict[str, Any]:
    """``claude -p --json-schema``: the user's schema is a tool's input_schema."""
    body = haiku_print()
    body["tools"] = [
        *_tools()[:1],
        {
            "name": "StructuredOutput",
            "description": "Use this tool to return your final response.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": f"The contact email, for example {OTHER_EMAIL}",
                    }
                },
                "required": ["email"],
            },
        },
    ]
    body["tool_choice"] = {"type": "tool", "name": "StructuredOutput"}
    return body


def auto_mode() -> dict[str, Any]:
    """Auto mode adds safeguards with the classifier's context."""
    body = haiku_print()
    body["safeguards"] = [
        {
            "type": "dangerous_tool_use",
            "classifier_context": {
                "v": 1,
                "user_identity": ACCOUNT,
                "home_dir": "/Users/example",
                "live_cwd": "/work",
                "platform": "darwin",
                "permission_mode": "auto",
                "restricted": False,
                "classify_all_shell": False,
                "case_insensitive_paths": True,
                "artifact_consent_holdback": False,
                "is_remote_mode": False,
                "rules": {"allow": ["Bash(git:*)"], "ask": [], "deny": []},
                "rule_roots": {"userSettings": "/Users/example/.claude"},
                "trusted_directories": {
                    "primary": {"path": "/work", "resolved": ["/work"]},
                    "additional": [],
                    "network": [],
                    "block_reads_outside_working_directories": False,
                },
                "git_state": {
                    "cwd": "/work",
                    "branch": None,
                    "error": "not a git repository",
                    "visibility": {"origin": None, "remotes": []},
                },
                "prior_turn_context": [
                    {
                        "context": {"live_cwd": "/work", "platform": "darwin"},
                        "tool_use_ids": ["toolu_01FixtureBash00000000000"],
                    }
                ],
            },
        }
    ]
    return body


def opus_print() -> dict[str, Any]:
    """Opus 5.5 and Fable 5.1 put effort on a system-role message."""
    body = haiku_print()
    body["model"] = "claude-opus-5-5"
    body["thinking"] = {"type": "adaptive"}
    body["output_config"] = {"effort": "medium"}
    body["messages"] = [
        body["messages"][0],
        {
            "role": "system",
            "content": [{"type": "text", "text": "Effort for this turn."}],
            "output_config": {"effort": "medium"},
        },
        *body["messages"][1:],
    ]
    return body


#: Every golden case: a name, and a function returning its body.
BODIES = {
    "haiku_print": haiku_print,
    "sonnet_interactive": sonnet_interactive,
    "title_generation": title_generation,
    "structured_output": structured_output,
    "auto_mode": auto_mode,
    "opus_print": opus_print,
}
