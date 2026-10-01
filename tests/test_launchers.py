"""Launchers on every native platform.

Signals, exit codes, and a stub client through a live gateway.
"""

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from veil import cli

#: Every signal the launcher handles, on some platform.
NAMES = ("SIGINT", "SIGBREAK", "SIGTERM", "SIGHUP")

REAL_POPEN = subprocess.Popen


def handlers():
    return {
        name: signal.getsignal(getattr(signal, name))
        for name in NAMES
        if hasattr(signal, name)
    }


def run(tmp_path, source, **env):
    """Run ``source`` as the client, the way a launcher runs one."""
    script = tmp_path / "client.py"
    script.write_text(source, encoding="utf-8")
    return cli._run_child([sys.executable, str(script)], dict(os.environ, **env), None)


class Recorder:
    """Starts each client with the real Popen, keeping it for the test."""

    def __init__(self):
        self.children = []
        self.before = lambda: None
        self.after = lambda child: None

    def __call__(self, *args, **kwargs):
        self.before()
        child = REAL_POPEN(*args, **kwargs)
        self.children.append(child)
        self.after(child)
        return child


@pytest.fixture
def launches(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(cli.subprocess, "Popen", recorder)
    yield recorder
    for child in recorder.children:
        if child.poll() is None:
            child.kill()
        child.wait()


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (0, 0),
        (3, 3),
        (7, 7),
        (-2, 130),
        (-9, 137),
        (-15, 143),
        # A Windows NTSTATUS passes through.
        (3221225786, 3221225786),
    ],
)
def test_exit_codes_are_shell_style(returncode, expected):
    assert cli._exit_code(returncode) == expected


def test_a_client_exit_code_is_returned(tmp_path):
    assert run(tmp_path, "import sys; sys.exit(7)") == 7


def test_missing_posix_signals_do_not_break_the_launch(tmp_path, monkeypatch):
    # Windows has no SIGHUP; POSIX has no SIGBREAK.
    monkeypatch.delattr(signal, "SIGHUP", raising=False)
    monkeypatch.delattr(signal, "SIGBREAK", raising=False)
    before = handlers()
    assert run(tmp_path, "import sys; sys.exit(3)") == 3
    assert handlers() == before


def test_handlers_while_the_client_runs_and_after(tmp_path, launches):
    go = tmp_path / "go"
    before = handlers()
    seen = {}

    def sample():
        deadline = time.monotonic() + 30
        while not launches.children and time.monotonic() < deadline:
            time.sleep(0.01)
        seen.update(handlers())
        go.touch()

    client = (
        "import os, sys, time\n"
        "deadline = time.monotonic() + 30\n"
        "while not os.path.exists(os.environ['CLIENT_GO']):\n"
        "    if time.monotonic() > deadline:\n"
        "        sys.exit(9)\n"
        "    time.sleep(0.01)\n"
    )
    sampler = threading.Thread(target=sample)
    sampler.start()
    try:
        assert run(tmp_path, client, CLIENT_GO=str(go)) == 0
    finally:
        sampler.join()
    assert seen.keys() == before.keys()
    for name, handler in seen.items():
        assert callable(handler), name
        assert handler not in (signal.SIG_IGN, signal.SIG_DFL, before[name]), name
    assert handlers() == before


def test_the_client_keeps_ctrl_c(tmp_path):
    report = tmp_path / "sigint.txt"
    client = (
        "import os, signal\n"
        "with open(os.environ['CLIENT_REPORT'], 'w', encoding='utf-8') as f:\n"
        "    f.write(repr(signal.getsignal(signal.SIGINT)))\n"
    )
    assert run(tmp_path, client, CLIENT_REPORT=str(report)) == 0
    # Not SIG_IGN, which a child process would inherit.
    assert report.read_text(encoding="utf-8") == repr(signal.default_int_handler)


def test_a_signal_before_the_client_starts_still_stops_it(tmp_path, launches):
    def terminate():
        # With the default handler, the signal would end the test run.
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
        signal.raise_signal(signal.SIGTERM)

    launches.before = terminate
    started = time.monotonic()
    code = run(tmp_path, "import time; time.sleep(30)")
    assert time.monotonic() - started < 10
    # Windows passes SIGTERM on as TerminateProcess(handle, 1).
    assert code == (1 if os.name == "nt" else 143)


def test_ctrl_c_just_after_the_start_leaves_the_client_running(tmp_path, launches):
    launches.after = lambda child: signal.raise_signal(signal.SIGINT)
    try:
        code = run(tmp_path, "import sys, time; time.sleep(0.5); sys.exit(4)")
    except KeyboardInterrupt:
        pytest.fail("Ctrl-C reached the launcher instead of being ignored")
    assert code == 4
    # It ended by itself, not stopped by the launcher.
    assert launches.children[0].returncode == 4


def test_an_error_after_start_still_stops_the_client(tmp_path, launches):
    def fail_the_first_wait(child):
        wait = child.wait
        calls = []

        def failing(timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise RuntimeError("simulated")
            return wait(timeout)

        child.wait = failing

    launches.after = fail_the_first_wait
    before = handlers()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="simulated"):
        run(tmp_path, "import time; time.sleep(30)")
    assert time.monotonic() - started < 10
    assert launches.children[0].poll() is not None
    assert handlers() == before
