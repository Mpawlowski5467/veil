"""The command line: `claude` runs Claude Code through a private gateway."""

import http.client
import json
import os
import stat
import sys
import threading
import time

import pytest

from veil import __version__, cli
from veil.gateway import SECRET_HEADER, Settings, open_sessions
from veil.gateway.compat import TESTED_CLAUDE_CODE

FAKE_CLAUDE = """#!{python}
import http.client, json, os, sys, time
from urllib.parse import urlsplit
args = sys.argv[1:]
if args == ["--version"]:
    time.sleep(float(os.environ.get("FAKE_CLAUDE_VERSION_SLEEP", "0")))
    print(os.environ.get("FAKE_CLAUDE_VERSION", ""))
    sys.exit(int(os.environ.get("FAKE_CLAUDE_VERSION_EXIT", "0")))
settings_path = args[args.index("--settings") + 1]
settings = json.load(open(settings_path))
mode = os.stat(settings_path).st_mode & 0o777
env = settings["env"]
host = urlsplit(env["ANTHROPIC_BASE_URL"]).netloc
name, _, secret = env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()[-1].partition(": ")

def status(headers):
    conn = http.client.HTTPConnection(host, timeout=5)
    conn.request("GET", "/not-served", headers=headers)
    code = conn.getresponse().status
    conn.close()
    return code

report = {{
    "args": args,
    "settings": settings,
    "process_env": {{k: os.environ.get(k) for k in env}},
    "with_secret": status({{name: secret}}),
    "without_secret": status({{}}),
    "cwd": os.getcwd(),
    "settings_mode": mode,
    "secret_in_argv": secret in " ".join(sys.argv),
}}
report["pid"] = os.getpid()
post = os.environ.get("FAKE_CLAUDE_POST")
if post:
    conn = http.client.HTTPConnection(host, timeout=5)
    conn.request(
        "POST",
        "/v1/messages",
        body=post,
        headers={{
            name: secret,
            "x-claude-code-session-id": "s-1",
            "User-Agent": "claude-cli/2.1.290 (external, cli)",
            "Content-Type": "application/json",
        }},
    )
    report["post_status"] = conn.getresponse().status
    conn.close()
with open(os.environ["FAKE_CLAUDE_REPORT"], "w") as f:
    json.dump(report, f)
import time
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE_CLAUDE.format(python=sys.executable))
    path.chmod(0o755)
    report = tmp_path / "report.json"
    monkeypatch.setenv("FAKE_CLAUDE_REPORT", str(report))
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", f"{TESTED_CLAUDE_CODE} (Claude Code)")
    monkeypatch.setenv("PATH", f"{path.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    # Not this machine's own settings: a Claude Code session running these
    # tests may have set these.
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return path, report


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


class TestClaude:
    def test_runs_claude_through_a_live_gateway(
        self, fake_claude, data_dir, tmp_path, capsys
    ):
        _, report_file = fake_claude
        code = cli.main(
            ["--data-dir", str(data_dir), "claude", "--resume", "abc", "-p", "hi"]
        )
        assert code == 0
        report = json.loads(report_file.read_text())
        assert report["args"][2:] == ["--resume", "abc", "-p", "hi"]
        # The secret is in an owner-only file, never on the command line
        # (other users can read command lines with ps).
        assert report["secret_in_argv"] is False
        assert report["settings_mode"] == 0o600
        assert not list(data_dir.glob("claude-settings-*.json"))  # removed after
        # The gateway was up, and let in only requests with its secret.
        assert report["with_secret"] == 404
        assert report["without_secret"] == 401
        env = report["settings"]["env"]
        assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
        assert env["ANTHROPIC_CUSTOM_HEADERS"].startswith(f"{SECRET_HEADER}: ")
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert report["process_env"] == {k: v or None for k, v in env.items()}
        assert report["settings"]["disableAllHooks"] is False
        assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
        assert set(cli.DENIED_TOOLS) == set(report["settings"]["permissions"]["deny"])
        hook_events = report["settings"]["hooks"]
        assert set(hook_events) == {"PreToolUse", "UserPromptSubmit"}
        prompt_hook = hook_events["UserPromptSubmit"][0]["hooks"][0]["command"]
        assert prompt_hook.endswith(f"--expect-url {env['ANTHROPIC_BASE_URL']}")
        assert "masking through a local gateway" in capsys.readouterr().err

    def test_the_gateway_stops_with_claude(self, fake_claude, data_dir, tmp_path):
        _, report_file = fake_claude
        cli.main(["--data-dir", str(data_dir), "claude"])
        url = json.loads(report_file.read_text())["settings"]["env"][
            "ANTHROPIC_BASE_URL"
        ]
        conn = http.client.HTTPConnection(url.removeprefix("http://"), timeout=2)
        with pytest.raises(ConnectionRefusedError):
            conn.connect()

    def test_claudes_exit_code_is_returned(self, fake_claude, data_dir, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_EXIT", "3")
        assert cli.main(["--data-dir", str(data_dir), "claude"]) == 3

    def test_refused_requests_are_listed_when_claude_exits(
        self, fake_claude, data_dir, monkeypatch, capsys
    ):
        _, report_file = fake_claude
        body = {"messages": [{"role": "user", "content": "hi"}], "prompt": "x"}
        monkeypatch.setenv("FAKE_CLAUDE_POST", json.dumps(body))
        monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
        assert cli.main(["--data-dir", str(data_dir), "claude"]) == 1
        assert json.loads(report_file.read_text())["post_status"] == 400
        err = capsys.readouterr().err.splitlines()
        start = err.index(
            "veil: 1 request couldn't be masked, so it wasn't sent. Not handled:"
        )
        assert err[start + 1] == "  prompt (unknown field)"
        assert err[start + 2].startswith("veil: This is Claude Code 2.1.290")

    def test_an_untested_claude_code_is_named_once(
        self, fake_claude, data_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("FAKE_CLAUDE_VERSION", "9.0.0 (Claude Code)")
        assert cli.main(["--data-dir", str(data_dir), "claude"]) == 0
        err = capsys.readouterr().err
        assert (
            f"veil: Claude Code 9.0.0 is newer than {TESTED_CLAUDE_CODE}, the version "
            f"veil {__version__} was tested with; if requests are refused, update veil"
        ) in err.splitlines()
        # Claude Code started anyway.
        assert json.loads(fake_claude[1].read_text())["args"]
        cli.main(["--data-dir", str(data_dir), "claude"])
        assert "is newer than" not in capsys.readouterr().err
        assert (data_dir / "version-warnings").stat().st_mode & 0o777 == 0o600

    def test_an_older_claude_code_is_told_to_update(
        self, fake_claude, data_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("FAKE_CLAUDE_VERSION", "0.1.0 (Claude Code)")
        cli.main(["--data-dir", str(data_dir), "claude"])
        err = capsys.readouterr().err
        assert "veil: Claude Code 0.1.0 is older than" in err
        assert "if requests are refused, update Claude Code" in err

    @pytest.mark.parametrize(
        ("output", "code", "sleep"),
        [
            (f"{TESTED_CLAUDE_CODE} (Claude Code)", "0", "0"),  # the tested one
            ("not a version", "0", "0"),
            ("9.0.0 (Claude Code)", "1", "0"),  # it failed
            ("9.0.0 (Claude Code)", "0", "2"),  # too slow
        ],
    )
    def test_nothing_is_said_when_the_version_isnt_new(
        self, fake_claude, data_dir, monkeypatch, capsys, output, code, sleep
    ):
        monkeypatch.setattr(cli, "VERSION_TIMEOUT", 0.3)
        monkeypatch.setenv("FAKE_CLAUDE_VERSION", output)
        monkeypatch.setenv("FAKE_CLAUDE_VERSION_EXIT", code)
        monkeypatch.setenv("FAKE_CLAUDE_VERSION_SLEEP", sleep)
        started = time.monotonic()
        assert cli.main(["--data-dir", str(data_dir), "claude"]) == 0
        assert time.monotonic() - started < 2
        assert "was tested with" not in capsys.readouterr().err

    def test_nothing_is_listed_when_nothing_was_refused(
        self, fake_claude, data_dir, capsys
    ):
        cli.main(["--data-dir", str(data_dir), "claude"])
        assert "couldn't be masked" not in capsys.readouterr().err

    def test_existing_custom_headers_are_kept(self, fake_claude, data_dir, monkeypatch):
        _, report_file = fake_claude
        monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-team: blue")
        cli.main(["--data-dir", str(data_dir), "claude"])
        headers = json.loads(report_file.read_text())["settings"]["env"][
            "ANTHROPIC_CUSTOM_HEADERS"
        ]
        lines = headers.splitlines()
        assert lines[0] == "x-team: blue"
        assert lines[1].startswith(f"{SECRET_HEADER}: ")

    def test_a_bad_config_stops_before_claude_starts(
        self, fake_claude, data_dir, capsys
    ):
        _, report_file = fake_claude
        data_dir.mkdir(mode=0o700)
        (data_dir / "config.json").write_text('{"entites": {}}')
        assert cli.main(["--data-dir", str(data_dir), "claude"]) == 2
        assert "unknown key 'entites'" in capsys.readouterr().err
        assert not report_file.exists()

    def test_a_missing_claude_is_reported(
        self, data_dir, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("PATH", str(tmp_path))
        assert cli.main(["--data-dir", str(data_dir), "claude"]) == 1
        assert "claude command wasn't found" in capsys.readouterr().err

    def test_the_data_folder_is_private(self, fake_claude, data_dir):
        cli.main(["--data-dir", str(data_dir), "claude"])
        assert stat.S_IMODE(os.stat(data_dir).st_mode) == 0o700


def test_a_sigterm_is_passed_on_to_claude(fake_claude, data_dir, monkeypatch):
    import signal
    import subprocess

    _, report_file = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    veil = subprocess.Popen(
        [sys.executable, "-m", "veil", "--data-dir", str(data_dir), "claude"],
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while not report_file.exists() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    child = json.loads(report_file.read_text())["pid"]
    veil.send_signal(signal.SIGTERM)
    veil.wait(10)
    # Claude Code never outlives its gateway.
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("claude is still running after veil got SIGTERM")


class TestRefusals:
    @pytest.mark.parametrize(
        "option", ["--settings", "--settings=x.json", "--bare", "--safe-mode"]
    )
    def test_options_that_drop_the_masking_settings(
        self, fake_claude, data_dir, option, capsys
    ):
        _, report_file = fake_claude
        assert cli.main(["--data-dir", str(data_dir), "claude", option]) == 2
        assert "would drop the masking settings" in capsys.readouterr().err
        assert not report_file.exists()

    def test_a_base_url_of_the_users_own(
        self, fake_claude, data_dir, monkeypatch, capsys
    ):
        _, report_file = fake_claude
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://llm-proxy.example.com")
        assert cli.main(["--data-dir", str(data_dir), "claude"]) == 2
        assert (
            "ANTHROPIC_BASE_URL is set in your environment" in capsys.readouterr().err
        )
        assert not report_file.exists()

    def test_a_base_url_in_the_users_claude_settings(
        self, fake_claude, data_dir, tmp_path, capsys
    ):
        settings = tmp_path / "home" / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://p.example.com"}})
        )
        assert cli.main(["--data-dir", str(data_dir), "claude"]) == 2
        assert "~/.claude/settings.json" in capsys.readouterr().err

    def test_hooks_that_cant_run_stop_it(
        self, fake_claude, data_dir, monkeypatch, capsys
    ):
        _, report_file = fake_claude
        monkeypatch.setattr(
            cli,
            "hook_command",
            lambda _dir: [sys.executable, "-c", "raise SystemExit(1)"],
        )
        assert cli.main(["--data-dir", str(data_dir), "claude"]) == 1
        assert "hook didn't work" in capsys.readouterr().err
        assert not report_file.exists()

    def test_variables_that_turn_hooks_off_are_unset(
        self, fake_claude, data_dir, monkeypatch
    ):
        _, report_file = fake_claude
        monkeypatch.setenv("CLAUDE_CODE_SAFE_MODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_USE_MANTLE", "1")
        cli.main(["--data-dir", str(data_dir), "claude"])
        report = json.loads(report_file.read_text())
        assert report["process_env"]["CLAUDE_CODE_SAFE_MODE"] is None
        assert report["process_env"]["CLAUDE_CODE_USE_MANTLE"] is None


class TestGateway:
    def test_prints_the_settings_and_keeps_its_secret(self, data_dir, capsys):
        data_dir.mkdir(mode=0o700)
        stop = threading.Event()
        thread = threading.Thread(
            target=cli.run_gateway,
            kwargs={"data_dir": data_dir, "port": 0, "stop": stop.is_set},
        )
        thread.start()
        deadline = time.time() + 5
        while "listening at" not in capsys.readouterr().out and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        thread.join(5)
        secret = cli.gateway_secret(data_dir)
        assert stat.S_IMODE(os.stat(data_dir / "gateway-secret").st_mode) == 0o600
        assert cli.gateway_secret(data_dir) == secret  # created once

    def test_the_printed_settings_include_the_hooks(self, data_dir, capsys):
        data_dir.mkdir(mode=0o700)
        done = threading.Event()
        thread = threading.Thread(
            target=cli.run_gateway,
            kwargs={"data_dir": data_dir, "port": 0, "stop": done.is_set},
        )
        thread.start()
        deadline = time.time() + 5
        out = ""
        while "keep that file" not in out and time.time() < deadline:
            out += capsys.readouterr().out
            time.sleep(0.05)
        done.set()
        thread.join(5)
        printed = json.loads(out[out.index("{") : out.rindex("}") + 1])
        assert set(printed["hooks"]) == {"PreToolUse", "UserPromptSubmit"}
        assert "PushNotification" in printed["permissions"]["deny"]
        assert printed["disableAllHooks"] is False

    def test_main_runs_it(self, data_dir, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "run_gateway", lambda **kw: calls.append(kw) or 0)
        assert cli.main(["--data-dir", str(data_dir), "gateway", "--port", "0"]) == 0
        assert calls == [{"data_dir": data_dir.absolute(), "port": 0}]


class TestForget:
    def fill(self, data_dir):
        data_dir.mkdir(mode=0o700, exist_ok=True)
        with open_sessions(data_dir, Settings(), {}) as sessions:
            for name in ("s1", "s2"):
                session = sessions.get(name)
                session.shield.mask("jane.doe@example.com")
                session.ledger.record_text("x", "y")

    def restored(self, data_dir, name):
        with open_sessions(data_dir, Settings(), {}) as sessions:
            session = sessions.get(name)
            return session.shield.restore("[EMAIL_1]").text, session.ledger.masked_text(
                "x"
            )

    def test_one_session(self, data_dir):
        self.fill(data_dir)
        assert cli.main(["--data-dir", str(data_dir), "forget", "--session", "s1"]) == 0
        assert self.restored(data_dir, "s1") == ("[EMAIL_1]", None)
        assert self.restored(data_dir, "s2") == ("jane.doe@example.com", "y")

    def test_everything(self, data_dir):
        self.fill(data_dir)
        assert cli.main(["--data-dir", str(data_dir), "forget", "--all"]) == 0
        assert self.restored(data_dir, "s1") == ("[EMAIL_1]", None)
        assert self.restored(data_dir, "s2") == ("[EMAIL_1]", None)


def test_python_dash_m_runs_the_command_line():
    import subprocess

    completed = subprocess.run(
        [sys.executable, "-m", "veil", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "claude" in completed.stdout
    assert "gateway" in completed.stdout
