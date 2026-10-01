"""Shareable reports use positive allowlists, even for malformed/private inputs."""

import json
import subprocess

import pytest

from veil import Shield, cli
from veil.codex_setup import setup_codex
from veil.gateway import Gateway, Sessions
from veil.gateway.config import SettingsError, prepare_data_dir
from veil.support_report import _client_version, support_report
from veil.verification import Endpoint

PRIVATE = "fictional-private-person@example.org"


@pytest.fixture
def configured(tmp_path):
    directory = prepare_data_dir(tmp_path / "private")
    secret = cli.gateway_secret(directory)
    config = tmp_path / "codex" / "config.toml"
    with Gateway(
        Sessions(lambda _: Shield(), api="openai"),
        secret=secret,
        api="openai",
        openai_auth="chatgpt",
    ) as gateway:
        setup_codex(directory, config, gateway.port, "chatgpt")
        yield directory, config, gateway


@pytest.fixture
def reports(monkeypatch):
    monkeypatch.setattr("veil.support_report._client_version", lambda _: "1.2.3")
    monkeypatch.setattr(
        "veil.support_report.endpoint",
        lambda **_: Endpoint("http://127.0.0.1:1234", PRIVATE),
    )
    local = {
        "config": "/Users/" + PRIVATE,
        "data_dir": PRIVATE,
        "gateway_command": PRIVATE,
        "service": {"state": "running", "path": PRIVATE},
        "checks": [
            {"name": "routing", "state": "pass", "detail": PRIVATE, "remedy": PRIVATE}
        ],
    }
    monkeypatch.setattr("veil.support_report.collect_diagnostics", lambda **_: local)
    activity = {
        "sessions": [
            {
                "requests": 5,
                "forwarded": 3,
                "completed": 2,
                "failed": 1,
                "session_ref": PRIVATE,
                "last_request_at": PRIVATE,
                "placeholder_occurrences": {PRIVATE: 5},
            }
        ],
        "scope": PRIVATE,
    }
    probe = {
        "state": "verified",
        "verification_id": PRIVATE,
        "session_ref": PRIVATE,
        "prompt": PRIVATE,
        "masked": True,
        "forwarded": True,
        "restored": True,
        "completed": True,
    }
    monkeypatch.setattr(
        "veil.support_report.local_request",
        lambda _, p: probe if "?id=" in p else activity,
    )
    return local, activity, probe


def test_report_never_copies_details_paths_ids_or_arbitrary_types(reports):
    report = support_report(verification="a" * 32)
    assert report["checks"] == [{"code": "routing.pass", "state": "pass"}]
    assert report["activity"] == {
        "requests": 5,
        "forwarded": 3,
        "completed": 2,
        "failed": 1,
    }
    assert report["verification"]["state"] == "verified"
    encoded = json.dumps(report)
    assert PRIVATE not in encoded
    assert "/Users/" not in encoded
    assert "a" * 32 not in encoded
    assert "unverified" in report["scope"]


@pytest.mark.parametrize("value", [PRIVATE, {}, [], None, True, -1, 10**30])
def test_untrusted_dynamic_report_values_do_not_escape_allowlists(reports, value):
    local, activity, probe = reports
    local["checks"].extend(
        [{"name": value, "state": "pass"}, {"name": "storage", "state": value}]
    )
    local["service"]["state"] = value
    activity["sessions"][0].update(
        requests=value, forwarded=value, completed=value, failed=value
    )
    probe.update(state=value, masked=value, completed=value)
    report = support_report(verification="a" * 32)
    assert report["checks"] == [{"code": "routing.pass", "state": "pass"}]
    assert report["worker_state"] == "unknown"
    assert report["activity"] == dict.fromkeys(
        ("requests", "forwarded", "completed", "failed"), 0
    )
    assert report["verification"]["state"] == "unknown"
    assert PRIVATE not in json.dumps(report)


def test_errors_cannot_echo_values_and_claude_skips_codex_configuration(
    reports, monkeypatch
):
    def fail(**_):
        raise SettingsError(PRIVATE)

    monkeypatch.setattr("veil.support_report.collect_diagnostics", fail)
    monkeypatch.setattr("veil.support_report.endpoint", fail)
    report = support_report(verification=PRIVATE)
    assert report["errors"] == ["diagnostics_unavailable", "gateway_report_unavailable"]
    assert PRIVATE not in json.dumps(report)
    assert support_report(client="claude")["errors"] == ["gateway_report_unavailable"]


def test_cli_report_is_json_even_when_gateway_is_unavailable(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("veil.support_report._client_version", lambda _: None)
    monkeypatch.delenv("VEIL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("VEIL_GATEWAY_SECRET", raising=False)
    code = cli.main(
        [
            "--data-dir",
            str(tmp_path / "private"),
            "report",
            "--config",
            str(tmp_path / "missing"),
        ]
    )
    assert code == 1
    output = capsys.readouterr()
    assert str(tmp_path) not in output.out + output.err
    assert json.loads(output.out)["gateway_state"] == "unavailable"
    assert not (tmp_path / "private").exists()


def test_report_contacts_only_real_authenticated_local_gateway(
    configured, monkeypatch, capsys
):
    directory, config, gateway = configured
    monkeypatch.setattr("veil.support_report._client_version", lambda _: None)
    monkeypatch.setattr("veil.diagnostics.shutil.which", lambda _: None)
    capsys.readouterr()
    report = support_report(config=config, data_dir=directory)
    assert report["gateway_state"] == "reachable"
    assert report["verification"] == {"state": "not_requested"}
    encoded = json.dumps(report)
    assert gateway.secret not in encoded
    assert str(directory) not in encoded
    assert report["activity"]["requests"] == 0
    assert not (directory / "vault.db").exists()


@pytest.mark.parametrize(
    ("client", "output", "expected"),
    [
        ("codex", "codex-cli 0.156.1\n", "0.156.1"),
        ("claude", "2.1.283 (Claude Code)\n", "2.1.283"),
        ("codex", "0.156.1 " + PRIVATE, None),
        ("claude", "1.2.3+" + PRIVATE, None),
        ("codex", "0.156.1-beta.3", "0.156.1-beta.3"),
    ],
)
def test_version_commands_export_only_strict_version_numbers(
    monkeypatch, client, output, expected
):
    monkeypatch.setattr(
        "veil.support_report.shutil.which", lambda _: "/private/" + client
    )
    monkeypatch.setattr(
        "veil.support_report.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, output, PRIVATE),
    )
    assert _client_version(client) == expected


def test_bad_verification_id_is_not_forwarded(reports):
    report = support_report(verification=PRIVATE)
    assert report["verification"] == {"state": "invalid"}
    assert report["errors"] == ["verification_id_invalid"]
    assert PRIVATE not in json.dumps(report)
