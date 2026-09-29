"""Opt-in installed Claude Code check against a scripted local provider only."""

import json
import os
import shutil
import subprocess

import pytest

from test_gateway_server import FakeAPI, sse, stream_reply
from veil import Shield
from veil.cli import claude_settings
from veil.gateway import Gateway, Sessions


@pytest.mark.skipif(
    os.environ.get("VEIL_LOCAL_CLAUDE") != "1",
    reason="opt-in installed Claude Code check",
)
def test_installed_claude_verification(tmp_path):
    executable = shutil.which("claude")
    if executable is None:
        pytest.skip("Claude Code is not installed")
    api = FakeAPI()
    api.routes["/v1/messages"] = stream_reply(
        sse(
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-4-5-20251001",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            }
        ),
        sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        ),
        sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "[EMAIL_1]"},
            }
        ),
        sse({"type": "content_block_stop", "index": 0}),
        sse(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            }
        ),
        sse({"type": "message_stop"}),
    )
    try:
        with Gateway(
            Sessions(lambda _: Shield()), upstream=api.host, secure=False
        ) as gateway:
            probe = gateway.activity.create_probe()
            settings = tmp_path / "settings.json"
            settings.write_text(json.dumps(claude_settings(gateway)))
            settings.chmod(0o600)
            env = {
                key: os.environ[key]
                for key in ("PATH", "TMPDIR", "LANG")
                if key in os.environ
            }
            env.update(
                HOME=str(tmp_path),
                CLAUDE_CONFIG_DIR=str(tmp_path / ".claude"),
                ANTHROPIC_API_KEY="fictional-offline-key",
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            )
            result = subprocess.run(
                [
                    executable,
                    "--settings",
                    str(settings),
                    "--setting-sources",
                    "project",
                    "--strict-mcp-config",
                    "--tools",
                    "",
                    "--model",
                    "claude-haiku-4-5-20251001",
                    "--max-turns",
                    "1",
                    "-p",
                    probe["prompt"],
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stderr[-2000:]
            assert probe["prompt"].splitlines()[-1] in result.stdout
            assert (
                gateway.activity.probe(probe["verification_id"])["state"] == "verified"
            )
            for _, _, _, body in api.received:
                assert probe["prompt"].splitlines()[-1].encode() not in body
    finally:
        api.close()
