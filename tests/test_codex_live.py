"""Opt-in real Codex model call through the managed Veil launcher."""

import os
import shutil
import subprocess

import pytest

from veil import cli, codex
from veil.gateway import Gateway

pytestmark = pytest.mark.live


@pytest.mark.parametrize("auth", ["chatgpt", "api-key"])
def test_real_codex_round_trip(tmp_path, monkeypatch, auth):
    flag = "VEIL_LIVE_CODEX_CHATGPT" if auth == "chatgpt" else "VEIL_LIVE_OPENAI"
    if os.environ.get(flag) != "1":
        pytest.skip(f"set {flag}=1 to make a real model call")
    if shutil.which("codex") is None:
        pytest.skip("Codex CLI is not installed")
    if auth == "api-key" and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("set OPENAI_API_KEY locally")
    email = "veil.canary@example.com"
    captured = []
    output = []
    metadata = []
    original_provider = codex.provider

    def no_retries(*args, **kwargs):
        return {
            **original_provider(*args, **kwargs),
            "request_max_retries": 0,
            "stream_max_retries": 0,
        }

    monkeypatch.setattr(codex, "provider", no_retries)

    def monitored_gateway(*args, **kwargs):
        gateway = Gateway(*args, **kwargs)
        connect = gateway._connect

        def monitored_connect():
            connection = connect()
            request = connection.request
            getresponse = connection.getresponse

            def observed_response():
                response = getresponse()
                metadata.append(
                    (
                        response.status,
                        response.getheader("Content-Type"),
                        response.getheader("Content-Encoding"),
                    )
                )
                return response

            connection.getresponse = observed_response

            def checked_request(method, url, body=None, headers=None, **options):
                assert email.encode() not in body
                if method == "POST":
                    assert b"[EMAIL_1]" in body
                    captured.append(url)
                assert "x-gateway-secret" not in {key.lower() for key in headers}
                return request(method, url, body=body, headers=headers, **options)

            connection.request = checked_request
            return connection

        gateway._connect = monitored_connect
        return gateway

    def child(command, env, cwd):
        result = subprocess.run(
            command,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        output.append(result.stdout)
        assert result.returncode == 0, (metadata, result.stderr[-4000:])
        return result.returncode

    monkeypatch.setattr(codex, "Gateway", monitored_gateway)
    monkeypatch.setattr(cli, "_run_child", child)
    data_dir = tmp_path / "veil"
    data_dir.mkdir(mode=0o700)
    (data_dir / "config.json").write_text('{"identity":false}')
    assert (
        codex.run_codex(
            [
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--skip-git-repo-check",
                "-C",
                str(tmp_path),
                "--model",
                "gpt-6-luna",
                f"Reply with exactly this email and nothing else: {email}. "
                "Do not use tools.",
            ],
            data_dir=data_dir,
            auth=auth,
            cwd=tmp_path,
        )
        == 0
    )
    assert email in "".join(output)
    assert captured
    expected = "/backend-api/codex/responses" if auth == "chatgpt" else "/v1/responses"
    assert set(captured) == {expected}
