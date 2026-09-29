"""Opt-in smoke test of the installed Codex CLI against loopback servers only.

Run with VEIL_LOCAL_CODEX=1. The provider key is fictional, every response is
scripted, and Codex starts with user configuration and repository rules disabled.
"""

import json
import os
import shutil
import subprocess

import pytest

from test_gateway_server import FakeAPI, sse, stream_reply
from veil import Shield
from veil.gateway import Gateway, Sessions


@pytest.mark.skipif(
    os.environ.get("VEIL_LOCAL_CODEX") != "1",
    reason="set VEIL_LOCAL_CODEX=1 to run the installed Codex against a local fixture",
)
def test_installed_codex_masks_a_prompt_and_restores_a_stream(tmp_path):
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("Codex CLI is not installed")
    email = "canary.person@example.com"
    masked = "Hello [EMAIL_1]."
    item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": masked, "annotations": []}],
    }
    identity = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    api = FakeAPI()
    api.replies.append(
        stream_reply(
            sse({"type": "response.created", "response": {"id": "resp_1"}}),
            sse(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "content": []},
                }
            ),
            sse({"type": "response.output_text.delta", **identity, "delta": masked}),
            sse({"type": "response.output_text.done", **identity, "text": masked}),
            sse({"type": "response.output_item.done", "output_index": 0, "item": item}),
            sse(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_1",
                        "status": "completed",
                        "output": [item],
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                }
            ),
        )
    )
    try:
        with Gateway(
            Sessions(lambda _: Shield(redact_warnings=True), api="openai"),
            api="openai",
            upstream=api.host,
            secure=False,
        ) as gateway:
            provider = (
                '{name="Veil local fixture",'
                f'base_url="{gateway.url}/v1",'
                'env_key="VEIL_CODEX_FIXTURE_KEY",wire_api="responses",'
                "supports_websockets=false,request_max_retries=0,"
                "stream_max_retries=0,"
                f'http_headers={{"x-gateway-secret"="{gateway.secret}"}}}}'
            )
            result = subprocess.run(
                [
                    codex,
                    "--no-daemon",
                    "exec",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "-C",
                    str(tmp_path),
                    "-c",
                    'model_provider="veil_fixture"',
                    "-c",
                    "model_providers.veil_fixture=" + provider,
                    "-c",
                    "features.apps=false",
                    "-c",
                    'web_search="disabled"',
                    "-m",
                    "gpt-6-sol",
                    f"Say hello to {email}. Do not use tools.",
                ],
                env={**os.environ, "VEIL_CODEX_FIXTURE_KEY": "fictional-offline-key"},
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            assert result.returncode == 0, result.stderr[-5000:]
            assert f"Hello {email}." in result.stdout
            assert len(api.received) == 1
            _, path, headers, body = api.received[0]
            assert path == "/v1/responses"
            assert email.encode() not in body
            assert "[EMAIL_1]" in body.decode()
            assert json.loads(body)["store"] is False
            assert "thread-id" not in {key.lower() for key in headers}
    finally:
        api.close()
