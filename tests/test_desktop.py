"""Desktop operations preserve the CLI's data and privacy boundaries."""

import json

import pytest

from veil import cli
from veil.desktop import PROVIDER_MODES, DesktopController
from veil.gateway.config import SettingsError


def test_preview_uses_registrations_without_persisting_input(tmp_path):
    data = tmp_path / "data"
    controller = DesktopController(data)
    controller.register("PERSON", "Jane Example")
    config = data / "config.json"
    raw = json.loads(config.read_text())
    raw["identity"] = False
    config.write_text(json.dumps(raw))
    before = {path.name: path.read_bytes() for path in data.iterdir()}
    output = controller.preview("Email Jane Example at jane@example.com")
    assert "Jane Example" not in output
    assert "jane@example.com" not in output
    assert "[PERSON_1]" in output
    assert "[EMAIL_1]" in output
    assert {path.name: path.read_bytes() for path in data.iterdir()} == before
    assert not (data / "vault.db").exists()


def test_snapshot_is_read_only_and_distinguishes_readiness(tmp_path):
    data = tmp_path / "absent"
    snapshot = DesktopController(data).snapshot()
    assert snapshot["gateway"]["state"] in {"unmanaged", "unsupported"}
    assert snapshot["startup"]["state"] in {"disabled", "unsupported"}
    assert snapshot["names"] is False
    assert not data.exists()


@pytest.mark.parametrize("mode", list(PROVIDER_MODES))
def test_provider_applies_only_explicit_mode_without_client_edits(
    tmp_path, monkeypatch, mode
):
    from veil import desktop

    calls = []
    monkeypatch.setattr(desktop, "startup_status", lambda _: {"state": "disabled"})
    monkeypatch.setattr(
        desktop.service,
        "run_service",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    controller = DesktopController(tmp_path / "absent")
    assert "client settings are unchanged" in controller.provider(mode)
    api, auth = PROVIDER_MODES[mode]
    assert calls == [
        (
            ("restart",),
            {
                "data_dir": controller.directory,
                "config": None,
                "api": api,
                "auth": auth,
            },
        )
    ]
    assert not controller.directory.exists()


@pytest.mark.parametrize("state", ["enabled", "unknown", "unsupported"])
def test_provider_refuses_to_override_login_startup(tmp_path, monkeypatch, state):
    from veil import desktop

    monkeypatch.setattr(desktop, "startup_status", lambda _: {"state": state})
    monkeypatch.setattr(
        desktop.service, "run_service", lambda *a, **kw: pytest.fail("must not restart")
    )
    with pytest.raises(SettingsError, match="Disable login startup"):
        DesktopController(tmp_path / "absent").provider(next(iter(PROVIDER_MODES)))
    assert not (tmp_path / "absent").exists()


def test_provider_rejects_unknown_mode_before_accessing_service(tmp_path):
    with pytest.raises(SettingsError, match="available provider"):
        DesktopController(tmp_path / "absent").provider("https://untrusted.example")
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("value", ["", "  ", "one\ntwo", "one\u0085two", "x" * 16385])
def test_registration_refuses_bad_input_without_writing(tmp_path, value):
    controller = DesktopController(tmp_path / "absent")
    with pytest.raises(SettingsError):
        controller.register("PERSON", value)
    assert not controller.directory.exists()


def test_preview_is_bounded(tmp_path):
    with pytest.raises(SettingsError, match="32768"):
        DesktopController(tmp_path).preview("x" * 32769)


def test_cli_dispatches_controls_with_explicit_context(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "veil.desktop.run_desktop", lambda *args: calls.append(args) or 0
    )
    config = tmp_path / "config.toml"
    assert (
        cli.main(["--data-dir", str(tmp_path), "desktop", "--config", str(config)]) == 0
    )
    assert calls == [(tmp_path, config)]
