"""Readiness checks must detect wrong routing and never disclose sensitive data."""

import http.client
import json
import subprocess

import pytest

from veil import Shield, cli
from veil.codex_setup import setup_codex
from veil.diagnostics import run_diagnostics
from veil.gateway import Gateway, Sessions


@pytest.fixture
def configured(tmp_path):
    directory = tmp_path / "veil"
    directory.mkdir(mode=0o700)
    secret = "never-print-this-secret"
    (directory / "gateway-secret").write_text(secret)
    (directory / "gateway-secret").chmod(0o600)
    config = tmp_path / "codex" / "config.toml"
    with Gateway(
        Sessions(lambda _: Shield(), api="openai"),
        secret=secret,
        api="openai",
        openai_auth="chatgpt",
    ) as gateway:
        setup_codex(directory, config, gateway.port, "chatgpt")
        yield directory, config, gateway


def test_status_verifies_local_gateway_but_does_not_claim_task_coverage(
    configured, capsys
):
    directory, config, gateway = configured
    capsys.readouterr()
    before = {
        p: p.read_bytes()
        for root in (directory, config.parent)
        for p in root.iterdir()
        if p.is_file()
    }
    assert run_diagnostics(config=config, json_output=True) == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["ready"] is True
    assert report["data_dir"] == str(directory)
    assert "unverified" in report["scope"]
    assert "No model call" in report["next_step"]
    assert gateway.secret not in out
    assert before == {
        p: p.read_bytes()
        for root in (directory, config.parent)
        for p in root.iterdir()
        if p.is_file()
    }


def test_doctor_local_round_trip_and_credentials_are_private(
    configured, monkeypatch, capsys
):
    directory, config, gateway = configured
    (directory / "config.json").write_text('{"entities":{"PERSON":["Private Person"]}}')
    (directory / "config.json").chmod(0o600)
    monkeypatch.setattr("veil.diagnostics.shutil.which", lambda _: "/test/codex")
    monkeypatch.setattr(
        "veil.diagnostics.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, "Logged in using ChatGPT", "secret-email@example.com"
        ),
    )
    capsys.readouterr()
    assert run_diagnostics(config=config, doctor=True, json_output=True) == 0
    out = capsys.readouterr().out
    assert all(
        value not in out
        for value in (gateway.secret, "Private Person", "secret-email@example.com")
    )
    assert any(
        item["name"] == "local_round_trip" and item["state"] == "pass"
        for item in json.loads(out)["checks"]
    )
    assert not (directory / "vault.db").exists()


@pytest.mark.parametrize("mode", ["wrong-secret", "wrong-auth", "wrong-api"])
def test_unhealthy_gateway_never_reports_ready(configured, mode, capsys):
    _, config, gateway = configured
    if mode == "wrong-secret":
        gateway.secret = "different"
    elif mode == "wrong-auth":
        gateway.openai_auth = "api-key"
    else:
        gateway.api = "anthropic"
    capsys.readouterr()
    assert run_diagnostics(config=config, json_output=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is False
    assert any(
        c["name"] == "gateway" and c["state"] == "fail" for c in report["checks"]
    )


def test_status_metadata_requires_local_secret(configured):
    _, _, gateway = configured
    connection = http.client.HTTPConnection("127.0.0.1", gateway.port)
    try:
        connection.request("GET", "/_gateway/status")
        response = connection.getresponse()
        assert response.status == 401
        assert b'"auth":' not in response.read()
    finally:
        connection.close()


def test_external_endpoint_is_never_contacted(configured, monkeypatch, capsys):
    _, config, gateway = configured
    text = config.read_text().replace(
        f"http://127.0.0.1:{gateway.port}/v1", "https://example.com/v1"
    )
    config.write_text(text)

    def forbidden(*args, **kwargs):
        pytest.fail("an external network request was attempted")

    monkeypatch.setattr("veil.diagnostics._gateway_answers", forbidden)
    capsys.readouterr()
    assert run_diagnostics(config=config, json_output=True) == 1
    assert "loopback" in capsys.readouterr().out


def test_missing_config_and_data_are_not_created(tmp_path, capsys):
    config = tmp_path / "absent" / "config.toml"
    assert cli.main(["status", "--config", str(config), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False
    assert not config.parent.exists()


def test_doctor_does_not_confuse_masked_context_with_masked_email(
    configured, monkeypatch, capsys
):
    directory, config, _ = configured
    (directory / "config.json").write_text(
        json.dumps(
            {
                "entities": {"PERSON": ["Email"]},
                "patterns": {"EMAIL": "(?!)"},
            }
        )
    )
    monkeypatch.setattr("veil.diagnostics.shutil.which", lambda _: None)
    capsys.readouterr()
    assert run_diagnostics(config=config, doctor=True, json_output=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert any(
        c["name"] == "local_round_trip" and c["state"] == "fail"
        for c in report["checks"]
    )


def test_unsupported_platform_keeps_foreground_recovery_command(
    configured, monkeypatch, capsys
):
    _, config, gateway = configured
    gateway.secret = "wrong-secret"
    monkeypatch.setattr(
        "veil.service.service_status", lambda _: {"state": "unsupported"}
    )
    capsys.readouterr()
    assert run_diagnostics(config=config, json_output=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["service_command"] is None
    assert "gateway --api openai" in report["gateway_command"]
