"""Login registration and real crash/stop recovery; no external model calls."""

import json
import os
import plistlib
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from veil import cli, service, startup
from veil.gateway.config import SettingsError, prepare_data_dir

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" and not sys.platform.startswith("linux"),
    reason="user login services currently support macOS/Linux",
)


def options(root, port=8485):
    return {"port": port, "api": "openai", "auth": "chatgpt", "cwd": str(root)}


@pytest.fixture
def registration(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    data = prepare_data_dir(tmp_path / "data")
    calls = []
    monkeypatch.setattr(startup, "_active", lambda _: False)
    monkeypatch.setattr(
        startup, "_manager", lambda args, **kw: calls.append(args) or True
    )
    monkeypatch.setattr(service, "_options", lambda *args: options(tmp_path))
    return data, calls


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_enable_idempotent_disable_preserves_mappings(
    registration, monkeypatch, platform
):
    data, calls = registration
    monkeypatch.setattr(sys, "platform", platform)
    (data / "private-mappings").write_text("keep")
    assert startup.run_startup("enable", data_dir=data) == 0
    target = startup._target(data)
    original = target.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    assert startup.startup_status(data)["state"] == "enabled"
    assert startup.run_startup("enable", data_dir=data) == 0
    assert target.read_bytes() == original
    assert any("bootstrap" in call or "enable" in call for call in calls)
    assert startup.run_startup("disable", data_dir=data) == 0
    assert not target.exists()
    assert not (data / "startup.json").exists()
    assert (data / "private-mappings").read_text() == "keep"
    assert startup.run_startup("disable", data_dir=data) == 0
    assert json.loads((data / service._DESIRED).read_text()) == {"running": False}


def test_launchd_descriptor_is_structured_and_contains_no_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "darwin")
    data = tmp_path / "folder with 'quotes' and $dollars"
    result = plistlib.loads(startup.descriptor(data, options(tmp_path)))
    assert result["KeepAlive"] is True
    assert result["RunAtLoad"] is True
    assert result["ProgramArguments"][0] == sys.executable
    assert str(data) in result["ProgramArguments"]
    assert "supervise" in result["ProgramArguments"]
    assert "-I" in result["ProgramArguments"]
    assert "EnvironmentVariables" not in result
    assert result["StandardErrorPath"] == "/dev/null"


def test_systemd_quotes_specifiers_and_environment_expansion(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    data = tmp_path / 'folder %h $HOME "quoted" \\backslash'
    text = startup.descriptor(data, options(tmp_path)).decode()
    assert "%%h" in text
    assert "$$HOME" in text
    assert '\\"quoted\\"' in text
    assert "\\\\backslash" in text
    assert "Restart=always" in text
    assert "KillMode=process" in text
    with pytest.raises(SettingsError, match="control characters"):
        startup.descriptor(tmp_path / "bad\nname", options(tmp_path))


def test_edited_registration_is_preserved(registration):
    data, _ = registration
    startup.run_startup("enable", data_dir=data)
    target = startup._target(data)
    target.write_bytes(b"user's later edit")
    with pytest.raises(SettingsError, match="edited"):
        startup.run_startup("disable", data_dir=data)
    assert target.read_bytes() == b"user's later edit"
    assert startup.startup_status(data)["state"] == "unknown"


def test_symlink_registration_never_touches_target(registration, tmp_path):
    data, _ = registration
    target = startup._target(data)
    target.parent.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    target.symlink_to(victim)
    with pytest.raises(SettingsError):
        startup.run_startup("enable", data_dir=data)
    assert victim.read_text() == "keep"


def test_manager_failure_retains_recoverable_receipt(registration, monkeypatch):
    data, _ = registration

    def fail(*args, **kwargs):
        raise SettingsError("manager unavailable")

    monkeypatch.setattr(startup, "_manager", fail)
    with pytest.raises(SettingsError):
        startup.run_startup("enable", data_dir=data)
    assert (data / "startup.json").exists()
    assert startup._target(data).exists()


def test_startup_status_does_not_create_files(tmp_path, capsys):
    data = tmp_path / "missing"
    assert cli.main(["--data-dir", str(data), "startup", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "disabled"
    assert not data.exists()


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("supervised gateway did not reach expected state")


def test_real_supervisor_recovers_crash_honors_stop_and_resumes(tmp_path):
    data = prepare_data_dir(tmp_path / "data")
    (data / "config.json").write_text('{"identity":false}')
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = startup.isolated_command(
        data,
        "supervise",
        "--port",
        str(port),
        "--api",
        "openai",
        "--auth",
        "api-key",
        "--cwd",
        str(tmp_path),
    )
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    def command_when_idle(operation, **kwargs):
        def attempt():
            try:
                service.run_service(operation, data_dir=data, **kwargs)
            except SettingsError as error:
                if str(error) != (
                    "another lifecycle command is running; retry after it finishes"
                ):
                    raise
                return False
            return True

        # A worker can report ready before its supervisor releases the command
        # lock. The public lifecycle contract asks competing callers to retry.
        wait_for(attempt)

    try:
        wait_for(lambda: service.service_status(data)["state"] == "running")
        first = service._state(data)
        assert first is not None
        # This live PID belongs to the worker just observed in our own private
        # test directory. Production control never signals a saved PID.
        os.kill(first["pid"], signal.SIGKILL)
        wait_for(
            lambda: (service._state(data) or {}).get("instance") != first["instance"]
        )
        wait_for(lambda: service.service_status(data)["state"] == "running")
        command_when_idle("stop")
        stopped = service._state(data)
        time.sleep(1.5)
        assert service.service_status(data)["state"] == "stopped"
        assert service._state(data)["instance"] == stopped["instance"]
        command_when_idle("start")
        wait_for(lambda: service.service_status(data)["state"] == "running")
        process.terminate()
        process.wait(timeout=5)
        assert service.service_status(data)["state"] == "running"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        command_when_idle("stop", remove=True)
