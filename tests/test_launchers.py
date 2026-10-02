"""Launchers on every native platform.

Signals, exit codes, and a stub client through a live gateway.
"""

import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from veil import cli
from veil.gateway.compat import TESTED_CLAUDE_CODE
from veil.gateway.config import prepare_data_dir

#: Every signal the launcher handles, on some platform.
NAMES = ("SIGINT", "SIGBREAK", "SIGTERM", "SIGHUP")

REAL_POPEN = subprocess.Popen

# A stand-in for both clients: it finds the gateway the way each one is told
# about it, checks that it is up, and reports what it saw.
STUB_CLIENT = """\
import http.client, json, os, signal, sys
from urllib.parse import urlsplit
args = sys.argv[1:]
if args == ["--version"]:
    print(os.environ.get("FAKE_CLIENT_VERSION", ""))
    sys.exit(0)
if "--settings" in args:
    path = args[args.index("--settings") + 1]
    with open(path, encoding="utf-8") as f:
        url = json.load(f)["env"]["ANTHROPIC_BASE_URL"]
    data_dir = os.path.dirname(path)
else:
    url, data_dir = os.environ["VEIL_GATEWAY_URL"], None
conn = http.client.HTTPConnection(urlsplit(url).netloc, timeout=5)
conn.request("GET", "/not-served")
status = conn.getresponse().status
conn.close()
report = {
    "args": args,
    "url": url,
    "status": status,
    "data_dir": data_dir,
    "sigint": repr(signal.getsignal(signal.SIGINT)),
}
with open(os.environ["FAKE_CLIENT_REPORT"], "w", encoding="utf-8") as f:
    json.dump(report, f)
sys.exit(int(os.environ.get("FAKE_CLIENT_EXIT", "0")))
"""

CONFIG = b'{"identity":false,"entities":{"PERSON":["Ada Quill"]}}'


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


@pytest.fixture
def stub_clients(tmp_path, monkeypatch):
    """``claude`` and ``codex`` on PATH, as the stub; returns the report path.

    On Windows each is a ``.cmd`` shim, the shape npm installs for both.
    """
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "stub_client.py").write_text(STUB_CLIENT, encoding="utf-8")
    for name in ("claude", "codex"):
        if os.name == "nt":
            shim = f'@"{sys.executable}" "%~dp0stub_client.py" %*\r\n'
            (folder / f"{name}.cmd").write_bytes(shim.encode())
        else:
            path = folder / name
            path.write_text(f"#!{sys.executable}\n{STUB_CLIENT}", encoding="utf-8")
            path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    report = tmp_path / "report.json"
    monkeypatch.setenv("FAKE_CLIENT_REPORT", str(report))
    monkeypatch.setenv("FAKE_CLIENT_VERSION", f"{TESTED_CLAUDE_CODE} (Claude Code)")
    # Not this machine's own settings: a session running these tests may
    # have set these.
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "OPENAI_API_KEY",
        *cli.HOOKS_OFF,
    ):
        monkeypatch.delenv(name, raising=False)
    # Where --forget-after-run puts its temporary storage.
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    return report


def read(report):
    return json.loads(report.read_text(encoding="utf-8"))


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_launcher_runs_the_client_through_a_live_gateway(
    stub_clients, tmp_path, monkeypatch, client
):
    data = prepare_data_dir(tmp_path / "data")
    (data / "config.json").write_bytes(CONFIG)
    monkeypatch.setenv("FAKE_CLIENT_EXIT", "7")
    assert cli.main(["--data-dir", str(data), client]) == 7
    report = read(stub_clients)
    # The gateway was up, and refused a request without its secret.
    assert report["status"] == 401
    assert report["url"].startswith("http://127.0.0.1:")
    assert report["sigint"] == repr(signal.default_int_handler)
    if client == "claude":
        assert "--settings" in report["args"]
        assert not list(data.glob("claude-settings-*.json"))
    else:
        assert "--no-daemon" in report["args"]
    # The gateway is gone with the client.
    conn = http.client.HTTPConnection(urlsplit(report["url"]).netloc, timeout=5)
    # Windows retries a refused local connection for about two seconds.
    with pytest.raises((ConnectionRefusedError, TimeoutError)):
        conn.connect()
    conn.close()


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_forget_after_run_uses_and_removes_temporary_storage(
    stub_clients, tmp_path, client
):
    source = prepare_data_dir(tmp_path / "private")
    (source / "config.json").write_bytes(CONFIG)
    argv = ["--data-dir", str(source), "--forget-after-run", client]
    assert cli.main(argv) == 0
    assert (source / "config.json").read_bytes() == CONFIG
    assert os.listdir(source) == ["config.json"]
    assert os.listdir(tempfile.tempdir) == []
    report = read(stub_clients)
    assert report["status"] == 401
    if client == "claude":
        used = Path(report["data_dir"])
        assert used.is_relative_to(tempfile.tempdir)
        assert not used.exists()


def test_forget_after_run_returns_the_client_failure(
    stub_clients, tmp_path, monkeypatch
):
    source = prepare_data_dir(tmp_path / "private")
    (source / "config.json").write_bytes(CONFIG)
    monkeypatch.setenv("FAKE_CLIENT_EXIT", "5")
    argv = ["--data-dir", str(source), "--forget-after-run", "claude"]
    assert cli.main(argv) == 5
    assert os.listdir(tempfile.tempdir) == []
