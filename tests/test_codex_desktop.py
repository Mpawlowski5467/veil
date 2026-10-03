"""Exercise an ephemeral app-server session, without changing desktop settings."""

import json
import os
import queue
import shutil
import subprocess
import threading
import time

import pytest

from test_gateway_server import FakeAPI, stream_reply
from test_openai_gateway import text_events
from veil import Shield
from veil.codex import provider, toml_value
from veil.gateway import Gateway, Sessions


def app_round_trip(
    binary, gateway, tmp_path, email, *, prompt=None, turns=1, compact=False
):
    config = provider(gateway, environment_secret=True)
    config.update(request_max_retries=0, stream_max_retries=0)
    if gateway.openai_auth == "api-key":
        config["env_key"] = "VEIL_FIXTURE_KEY"
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("Reply as requested. Do not use tools.")
    overrides = {
        "model_provider": "veil",
        "model_providers.veil": config,
        "web_search": "disabled",
        "features.apps": False,
        "features.multi_agent": False,
        "mcp_servers": {},
        "plugins": {},
        "project_doc_max_bytes": 0,
        "model_instructions_file": str(instructions),
        "developer_instructions": "",
        "analytics.enabled": False,
    }
    command = [binary, "app-server", "--stdio"]
    for key, value in overrides.items():
        command.extend(["-c", f"{key}={toml_value(value)}"])
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "VEIL_GATEWAY_SECRET": gateway.secret,
            "VEIL_FIXTURE_KEY": "fictional",
        },
    )
    incoming = queue.Queue()

    def reader():
        for line in process.stdout:
            incoming.put(json.loads(line))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    def send(method, params, request_id=None):
        message = {"method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def response(request_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            message = incoming.get(timeout=max(0.01, deadline - time.monotonic()))
            if message.get("id") == request_id:
                assert "error" not in message, message.get("error")
                return message["result"]
        pytest.fail("app-server did not respond")

    try:
        send("initialize", {"clientInfo": {"name": "veil_test", "version": "0.1"}}, 0)
        response(0)
        send("initialized", {})
        send(
            "thread/start",
            {
                "model": "gpt-6-luna",
                "modelProvider": "veil",
                "cwd": str(tmp_path),
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "read-only",
            },
            1,
        )
        started = response(1)
        assert started["thread"]["ephemeral"] is True
        thread_id = started["thread"]["id"]
        for generation in range(2 if compact else 1):
            for _ in range(turns):
                send(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [
                            {
                                "type": "text",
                                "text": prompt
                                or f"Reply with exactly {email}. Do not use tools.",
                            }
                        ],
                    },
                    2,
                )
                response(2)
                output = []
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    message = incoming.get(
                        timeout=max(0.01, deadline - time.monotonic())
                    )
                    if message.get("method") == "item/completed":
                        item = message["params"]["item"]
                        if item.get("type") == "agentMessage":
                            output.append(item["text"])
                    if message.get("method") == "turn/completed":
                        turn = message["params"]["turn"]
                        assert turn["status"] == "completed", json.dumps(
                            turn.get("error")
                        )
                        assert email in "".join(output)
                        break
                else:
                    pytest.fail("app-server did not complete its turn")
            if compact and generation == 0:
                send("thread/compact/start", {"threadId": thread_id}, 3)
                response(3)
                deadline = time.monotonic() + 30
                compacted = False
                while time.monotonic() < deadline:
                    message = incoming.get(
                        timeout=max(0.01, deadline - time.monotonic())
                    )
                    if message.get("method") == "error":
                        pytest.fail(json.dumps(message["params"]["error"]))
                    if message.get("method") == "item/completed":
                        compacted |= (
                            message["params"]["item"]["type"] == "contextCompaction"
                        )
                    if message.get("method") == "turn/completed":
                        assert message["params"]["turn"]["status"] == "completed"
                        assert compacted
                        break
                else:
                    pytest.fail("app-server did not complete local compaction")
    finally:
        process.stdin.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        thread.join(timeout=2)
        process.stdout.close()


def binary_path():
    binary = os.environ.get("VEIL_CODEX_APP_BINARY") or shutil.which("codex")
    if not binary:
        pytest.skip("Codex app-server binary is not installed")
    return binary


@pytest.mark.skipif(
    os.environ.get("VEIL_LOCAL_CODEX") != "1", reason="opt-in local app-server test"
)
def test_desktop_runtime_against_local_fixture(tmp_path):
    api = FakeAPI()
    api.routes["/v1/models"] = (200, "application/json", [b'{"models":[]}'])
    api.replies.append(stream_reply(text_events().encode()))
    try:
        with Gateway(
            Sessions(lambda _: Shield(redact_warnings=True), api="openai"),
            api="openai",
            upstream=api.host,
            secure=False,
        ) as gateway:
            probe = gateway.activity.create_probe()
            email = probe["prompt"].splitlines()[-1]
            app_round_trip(
                binary_path(), gateway, tmp_path, email, prompt=probe["prompt"]
            )
            assert (
                gateway.activity.probe(probe["verification_id"])["state"] == "verified"
            )
        assert api.received
        for _, _, _, body in api.received:
            assert email.encode() not in body
    finally:
        api.close()


@pytest.mark.live
def test_desktop_runtime_live_chatgpt(tmp_path):
    if os.environ.get("VEIL_LIVE_CODEX_APP") != "1":
        pytest.skip("set VEIL_LIVE_CODEX_APP=1 for a live desktop-runtime model call")
    with Gateway(
        Sessions(lambda _: Shield(redact_warnings=True), api="openai"),
        api="openai",
        openai_auth="chatgpt",
    ) as gateway:
        probe = gateway.activity.create_probe()
        email = probe["prompt"].splitlines()[-1]
        connect = gateway._connect
        sent = []

        def checked_connect():
            connection = connect()
            request = connection.request

            def checked_request(method, url, body=None, headers=None, **kwargs):
                if method == "POST":
                    assert email.encode() not in body
                    assert b"[EMAIL_1]" in body
                    sent.append(url)
                return request(method, url, body=body, headers=headers, **kwargs)

            connection.request = checked_request
            return connection

        gateway._connect = checked_connect
        app_round_trip(
            binary_path(), gateway, tmp_path, email, prompt=probe["prompt"], turns=3
        )
        assert len(sent) >= 3
        deadline = time.monotonic() + 2
        while gateway.activity.probe(probe["verification_id"])["state"] != "verified":
            assert time.monotonic() < deadline, "completed request was not verified"
            time.sleep(0.01)
