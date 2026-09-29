"""Exercise real local background processes; never call an external model."""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from veil import Shield, cli, service
from veil.codex_setup import setup_codex
from veil.gateway import Gateway, Sessions, SettingsError

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" and not sys.platform.startswith("linux"),
    reason="background controls support macOS/Linux",
)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_stopped(directory):
    deadline = time.monotonic() + 5
    while service._running(directory) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not service._running(directory)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    (directory / "config.json").write_text('{"identity":false}')
    processes = []
    popen = subprocess.Popen

    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(service.subprocess, "Popen", spawn)
    yield directory, processes
    # Test cleanup owns the actual handles, never a PID from a state file.
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def start(directory, **kwargs):
    options = {"port": free_port(), **kwargs}
    assert service.run_service("start", data_dir=directory, **options) == 0
    return service._state(directory)


def test_real_start_is_idempotent_and_stop_keeps_private_data(worker, capsys):
    directory, processes = worker
    initial = start(directory)
    assert service.run_service("start", data_dir=directory) == 0
    assert len(processes) == 1
    assert os.getsid(initial["pid"]) == initial["pid"]
    assert service.service_status(directory)["state"] == "running"
    assert (
        cli.main(["--data-dir", str(directory), "status", "--service", "--json"]) == 0
    )
    out = capsys.readouterr().out
    assert (directory / "gateway-secret").read_text().strip() not in out
    assert initial["instance"] not in out
    assert "unverified" in out
    for name in (service._STATE, service._COMMAND, service._LEASE):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert service.run_service("stop", data_dir=directory) == 0
    assert service.service_status(directory)["state"] == "stopped"
    assert (directory / "vault.db").exists()
    assert (directory / "ledger.db").exists()
    assert (directory / "gateway-secret").exists()
    assert not (directory / service._STOP).exists()
    assert service.run_service("stop", data_dir=directory) == 0
    assert cli.main(["--data-dir", str(directory), "status", "--service"]) == 1


def test_restart_replaces_generation_and_uses_saved_options(worker):
    directory, _ = worker
    initial = start(directory, auth="api-key")
    assert service.run_service("restart", data_dir=directory) == 0
    current = service._state(directory)
    assert current["instance"] != initial["instance"]
    assert current["port"] == initial["port"]
    assert current["auth"] == "api-key"
    assert current["cwd"] == initial["cwd"]
    assert service.service_status(directory)["state"] == "running"
    assert service.run_service("restart", data_dir=directory, port=free_port()) == 0
    assert service._state(directory)["port"] != initial["port"]


def test_crash_releases_lease_and_recovers_without_trusting_pid(worker):
    directory, processes = worker
    initial = start(directory)
    processes[0].kill()
    processes[0].wait(timeout=5)
    wait_stopped(directory)
    assert service.service_status(directory)["state"] == "crashed"
    # A stale/reused PID could now name this test runner. It must be ignored.
    initial["pid"] = os.getpid()
    service._write(directory / service._STATE, initial)
    assert service.run_service("restart", data_dir=directory) == 0
    assert service.service_status(directory)["state"] == "running"
    assert service._state(directory)["instance"] != initial["instance"]


def test_stop_never_signals_a_stale_pid(worker, monkeypatch):
    directory, _ = worker
    initial = start(directory)
    service.run_service("stop", data_dir=directory)
    initial.update(pid=os.getpid(), phase="running")
    service._write(directory / service._STATE, initial)

    def forbidden(*args):
        pytest.fail("stop tried to signal a saved PID")

    with monkeypatch.context() as guarded:
        guarded.setattr(os, "kill", forbidden)
        assert service.run_service("stop", data_dir=directory, remove=True) == 0
    assert service.service_status(directory)["state"] == "unmanaged"
    assert not (directory / service._STATE).exists()


def test_occupied_port_leaves_foreground_gateway_alone(worker):
    directory, _ = worker
    with Gateway(
        Sessions(lambda _: Shield(), api="openai"), api="openai"
    ) as foreground:
        with pytest.raises(SettingsError, match="port already in use"):
            start(directory, port=foreground.port)
        assert service.service_status(directory)["state"] == "failed"
        assert service._gateway_answers(foreground.url, foreground.secret)
        service.run_service("stop", data_dir=directory)
        assert service._gateway_answers(foreground.url, foreground.secret)


def test_concurrent_commands_do_not_start_duplicate_workers(worker):
    directory, processes = worker
    port = free_port()

    def attempt():
        try:
            return service.run_service("start", data_dir=directory, port=port)
        except SettingsError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert 0 in results
    assert all(
        result == 0 or "another lifecycle command" in result for result in results
    )
    assert len(processes) == 1
    assert service.service_status(directory)["state"] == "running"


def test_old_stop_request_cannot_stop_a_new_generation(worker):
    directory, _ = worker
    initial = start(directory)
    service.run_service("restart", data_dir=directory)
    service._write(directory / service._STOP, {"instance": initial["instance"]})
    time.sleep(0.25)
    assert service.service_status(directory)["state"] == "running"
    service.run_service("stop", data_dir=directory)
    assert service.service_status(directory)["state"] == "stopped"


def test_invalid_restart_settings_preserve_running_worker(worker):
    directory, _ = worker
    initial = start(directory)
    (directory / "config.json").write_text('{"patterns":{"EMAIL":"["}}')
    with pytest.raises(SettingsError):
        service.run_service("restart", data_dir=directory)
    assert service._state(directory)["instance"] == initial["instance"]
    assert service.service_status(directory)["state"] == "running"
    assert service.run_service("stop", data_dir=directory) == 0


def test_setup_receipt_selects_directory_port_and_auth(worker, tmp_path, capsys):
    directory, _ = worker
    config = tmp_path / "codex" / "config.toml"
    port = free_port()
    setup_codex(directory, config, port, "api-key")
    assert service.run_service("start", config=config) == 0
    state = service._state(directory)
    assert state["port"] == port
    assert state["auth"] == "api-key"
    capsys.readouterr()
    assert cli.main(["status", "--config", str(config), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["service"]["state"] == "running"
    assert "unverified" in report["scope"]
    assert service.run_service("stop", config=config, remove=True) == 0
    assert config.exists()
    assert (directory / "vault.db").exists()


def test_daemon_imports_are_isolated_from_cwd_and_pythonpath(
    worker, tmp_path, monkeypatch
):
    directory, _ = worker
    poison = tmp_path / "project"
    poison.mkdir()
    (poison / "veil.py").write_text('raise RuntimeError("project code ran")')
    (poison / "sitecustomize.py").write_text('raise RuntimeError("project code ran")')
    monkeypatch.setenv("PYTHONPATH", str(poison))
    start(directory, cwd=poison)
    assert service.service_status(directory)["state"] == "running"


def test_start_survives_launcher_process_exit(worker):
    directory, _ = worker
    common = [sys.executable, "-m", "veil", "--data-dir", str(directory)]
    try:
        started = subprocess.run(
            [*common, "start", "--port", str(free_port())],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert started.returncode == 0, started.stderr
        assert service.service_status(directory)["state"] == "running"
        stopped = subprocess.run(
            [*common, "stop"], capture_output=True, text=True, timeout=15
        )
        assert stopped.returncode == 0, stopped.stderr
        assert service.service_status(directory)["state"] == "stopped"
    finally:
        service.run_service("stop", data_dir=directory)


@pytest.mark.parametrize(
    "name", [service._STATE, service._LEASE, service._COMMAND, service._STOP]
)
def test_symlink_service_files_never_overwrite_targets(worker, tmp_path, name, capsys):
    directory, processes = worker
    target = tmp_path / "keep"
    target.write_text('"private-value"')
    (directory / name).symlink_to(target)
    assert (
        cli.main(["--data-dir", str(directory), "start", "--port", str(free_port())])
        == 2
    )
    assert not processes
    assert target.read_text() == '"private-value"'
    assert "private-value" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "field", ["instance", "phase", "pid", "port", "api", "auth", "cwd", "error"]
)
def test_malformed_state_is_reported_without_values(worker, field, capsys):
    directory, _ = worker
    start(directory)
    service.run_service("stop", data_dir=directory)
    value = service._state(directory)
    value[field] = ["private-value"]
    service._write(directory / service._STATE, value)
    assert service.service_status(directory)["state"] == "unknown"
    assert cli.main(["--data-dir", str(directory), "start"]) == 2
    assert "private-value" not in capsys.readouterr().err


def test_read_only_status_and_stop_do_not_create_missing_directory(tmp_path):
    directory = tmp_path / "missing"
    assert service.service_status(directory)["state"] == "unmanaged"
    assert service.run_service("stop", data_dir=directory) == 0
    assert not directory.exists()


def test_unsupported_platform_fails_before_any_files_change(tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")
    with pytest.raises(SettingsError, match="macOS and Linux"):
        service.run_service("start", data_dir=tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


def test_signal_exit_records_stopped_state(worker):
    directory, processes = worker
    start(directory)
    processes[0].send_signal(signal.SIGTERM)
    processes[0].wait(timeout=5)
    assert service.service_status(directory)["state"] == "stopped"


def test_restart_follows_reconfigured_setup(worker, tmp_path):
    from veil.codex_setup import undo_codex

    directory, _ = worker
    config = tmp_path / "codex" / "config.toml"
    old_port, new_port = free_port(), free_port()
    setup_codex(directory, config, old_port, "chatgpt")
    service.run_service("start", config=config)
    undo_codex(config)
    setup_codex(directory, config, new_port, "api-key")
    service.run_service("restart", config=config)
    state = service._state(directory)
    assert state["port"] == new_port
    assert state["auth"] == "api-key"


def test_unresponsive_worker_is_distinct_from_stopped(worker, monkeypatch):
    directory, _ = worker
    start(directory)
    monkeypatch.setattr(service, "_gateway_answers", lambda *args: False)
    assert service.service_status(directory)["state"] == "unresponsive"
    with pytest.raises(SettingsError, match="already active"):
        service.run_service("start", data_dir=directory)
    assert service.run_service("stop", data_dir=directory) == 0


def test_startup_timeout_cleans_up_only_its_new_child(worker, monkeypatch):
    directory, processes = worker
    monkeypatch.setattr(service, "_gateway_answers", lambda *args: False)
    monkeypatch.setattr(service, "_TIMEOUT", 0.2)
    with pytest.raises(SettingsError, match="startup timed out"):
        start(directory)
    assert processes[0].poll() is not None
    assert not service._running(directory)
    assert service.service_status(directory)["state"] == "failed"
    assert "timed out" in service.service_status(directory)["detail"]


def test_stop_timeout_preserves_pending_request_without_signals(worker, monkeypatch):
    directory, processes = worker
    start(directory)
    process = processes[0]
    process.send_signal(signal.SIGSTOP)
    try:
        monkeypatch.setattr(service, "_TIMEOUT", 0.1)
        with pytest.raises(SettingsError, match="stop request remains pending"):
            service.run_service("stop", data_dir=directory)
        assert process.poll() is None
        assert (directory / service._STOP).exists()
    finally:
        process.send_signal(signal.SIGCONT)
        process.wait(timeout=5)
    assert service.service_status(directory)["state"] == "stopped"


def test_anthropic_background_gateway_uses_api_key_mode(worker):
    directory, _ = worker
    state = start(directory, api="anthropic")
    assert state["api"] == "anthropic"
    assert state["auth"] == "api-key"
    assert service.service_status(directory)["state"] == "running"


def test_status_does_not_modify_service_files(worker):
    directory, _ = worker
    start(directory)
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    assert service.service_status(directory)["state"] == "running"
    after = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    assert before == after
