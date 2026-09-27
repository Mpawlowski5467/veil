"""Run the real Claude Code CLI headless, with probe hooks, for live tests.

Live tests are opt-in (``VEIL_LIVE_CLAUDE=1``): they need a logged-in
``claude`` CLI, and every run makes real model calls, on the cheapest model
unless ``VEIL_LIVE_MODEL`` says otherwise. They check the Claude Code
behavior veil's hooks rely on, so they can be run again after a Claude Code
update.

Each run is isolated from the user's own setup: a clean environment (no
``CLAUDECODE`` or other variables of a parent session), only the settings this
harness writes (``--setting-sources project`` in a fresh directory), only the
MCP servers it names (``--strict-mcp-config``), and a probe hook
(``probe_hook.py``) registered for every event, answering from rules.

Use only fictional data: `assert_fictional` rejects anything else before a
payload is saved as a fixture.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .recorder import Recorder

HERE = Path(__file__).resolve().parent
PROBE_HOOK = HERE / "probe_hook.py"
MCP_SERVER = HERE / "mcp_contacts.py"

LIVE = os.environ.get("VEIL_LIVE_CLAUDE") == "1"
MODEL = os.environ.get("VEIL_LIVE_MODEL", "haiku")

# The fictional contact card most live tests read.
NOTES = "Name: Jan Nowak\nEmail: jane.doe@example.com\nPhone: 555-0100\n"
EMAIL = "jane.doe@example.com"
PHONE = "555-0100"
NAME = "Jan Nowak"

# Hook events registered for the probe (Claude Code 2.1.283).
EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PreToolUse",
    "PermissionRequest",
    "PermissionDenied",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Notification",
    "MessageDisplay",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "StopFailure",
    "PreCompact",
    "PostCompact",
    "InstructionsLoaded",
)

# Only these variables reach the CLI; the rest of the parent environment,
# including a parent Claude Code session's variables, is left out.
_PASSED_ENV = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG")
_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def claude_path() -> str | None:
    """Return the ``claude`` executable, or None when it isn't installed."""
    return shutil.which("claude", path=f"{os.environ.get('PATH', '')}:{_PATH}")


def hook_command(rules: Path, log: Path) -> str:
    """Return the shell command that runs the probe hook with these files."""
    return shlex.join([sys.executable, str(PROBE_HOOK), str(rules), str(log)])


def settings(
    command: str, events: Iterable[str] = EVENTS, *, timeout: int = 60
) -> dict[str, Any]:
    """Return a settings object that runs ``command`` for every event."""
    entry = {"type": "command", "command": command, "timeout": timeout}
    return {"hooks": {event: [{"hooks": [entry]}] for event in events}}


def environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the clean environment the CLI runs in."""
    env = {key: os.environ[key] for key in _PASSED_ENV if key in os.environ}
    env["PATH"] = _PATH
    env["SHELL"] = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/sh"
    env.update(extra or {})
    return env


@dataclass
class Run:
    """What one ``claude -p`` run did.

    The transcript is read when the run ends, so a later ``--resume`` of the
    same session, which appends to the same file, doesn't change it.
    """

    dir: Path
    command: list[str]
    returncode: int
    stream: list[dict[str, Any]]
    hooks: list[dict[str, Any]]
    stderr: str
    debug: str
    transcript_text: str = ""
    gateway: list[dict[str, Any]] = field(default_factory=list)

    @property
    def result(self) -> dict[str, Any]:
        """The final ``result`` event of the stream (empty if there is none)."""
        for event in reversed(self.stream):
            if event.get("type") == "result":
                return event
        return {}

    @property
    def session_id(self) -> str | None:
        """The session id the CLI reported."""
        for event in self.stream:
            if isinstance(event.get("session_id"), str):
                return str(event["session_id"])
        return None

    def calls(self, event: str, tool: str | None = None) -> list[dict[str, Any]]:
        """Return the probe hook's log records for one event (and tool)."""
        return [
            record
            for record in self.hooks
            if record["event"] == event and (tool is None or record["tool"] == tool)
        ]

    def batch_calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        """Return every ``tool_calls`` entry PostToolBatch saw (for one tool).

        Each entry's ``tool_response`` is the text the model was given.
        """
        return [
            call
            for record in self.calls("PostToolBatch")
            for call in record["payload"].get("tool_calls") or []
            if tool is None or call.get("tool_name") == tool
        ]

    def notifications(self) -> list[str]:
        """Return the task notifications that arrived as UserPromptSubmit prompts."""
        return [
            str(record["payload"]["prompt"])
            for record in self.calls("UserPromptSubmit")
            if str(record["payload"].get("prompt", "")).startswith(
                "<task-notification>"
            )
        ]

    def api_requests(self) -> list[str]:
        """Return the debug log's line for each model request."""
        return [line for line in self.debug.splitlines() if "[API REQUEST]" in line]

    def api_bodies(self) -> list[dict[str, str]]:
        """Return each model request's body, as sent, with its query source.

        Claude Code writes them when ``OTEL_LOG_RAW_API_BODIES`` names a
        directory, which every run sets; this is the ground truth for what
        left the machine for the model.
        """
        folder = self.dir / "api"
        index = folder / "index.jsonl"
        if not index.exists():
            return []
        bodies = []
        for entry in _json_lines(index.read_text(encoding="utf-8")):
            request = folder / str(entry.get("request_file", ""))
            bodies.append(
                {
                    "query_source": str(entry.get("query_source", "")),
                    "request": (
                        request.read_text(encoding="utf-8") if request.is_file() else ""
                    ),
                }
            )
        return bodies

    def sent_to_api(self) -> str:
        """Return every request body of the run, joined."""
        return "\n".join(body["request"] for body in self.api_bodies())

    @property
    def transcript_path(self) -> Path | None:
        """The session transcript, as the hooks were told."""
        for record in self.hooks:
            path = record["payload"].get("transcript_path")
            if isinstance(path, str):
                return Path(path)
        return None

    def transcript(self) -> list[dict[str, Any]]:
        """Return the transcript's records as they were when the run ended."""
        return _json_lines(self.transcript_text)

    def attachments(self, kind: str) -> list[dict[str, Any]]:
        """Return the transcript's attachments of one type, e.g. ``"file"``."""
        return [
            record["attachment"]
            for record in self.transcript()
            if isinstance(record.get("attachment"), dict)
            and record["attachment"].get("type") == kind
        ]

    def tool_results(self) -> list[str]:
        """Return the text of every tool result the model was given."""
        texts = []
        for record in self.transcript():
            content = (record.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    texts.append(_text_of(block.get("content")))
        return texts

    def everything(self) -> str:
        """Return all the run's output, stream, transcript, and debug log."""
        stream = "\n".join(json.dumps(event) for event in self.stream)
        return "\n".join([stream, self.transcript_text, self.debug, self.stderr])


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")) for block in content if isinstance(block, dict)
        )
    return ""


def _json_lines(text: str) -> list[dict[str, Any]]:
    records = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


class Workspace:
    """A fresh project directory to run ``claude -p`` in, one run at a time.

    Files go in ``root / "work"`` (the CLI's working directory); each run
    keeps its settings, rules, hook log, stream, and debug log in its own
    ``root / "run-N"`` directory.
    """

    def __init__(self, root: Path) -> None:
        """Create the workspace under ``root``, which should be empty."""
        self.root = root
        self.work = root / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.runs: list[Run] = []

    def write(self, name: str, text: str) -> Path:
        """Write a file into the working directory and return its path."""
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run(
        self,
        prompt: str,
        *,
        rules: Sequence[Mapping[str, Any]] = (),
        allowed_tools: Sequence[str] = (),
        max_turns: int = 6,
        permission_mode: str | None = None,
        mcp: bool = False,
        resume: str | None = None,
        fork: bool = False,
        extra_args: Sequence[str] = (),
        extra_env: Mapping[str, str] | None = None,
        extra_settings: Mapping[str, Any] | None = None,
        gateway: bool = False,
        timeout: float = 300,
    ) -> Run:
        """Run ``claude -p prompt`` here with the probe hook answering ``rules``.

        ``extra_settings`` is merged into the settings file (e.g. ``env`` or
        ``permissions``). With ``gateway``, the CLI talks to the API through a
        recording pass-through gateway (see ``recorder.py``), whose records
        end up in ``Run.gateway``.
        """
        claude = claude_path()
        if claude is None:
            raise RuntimeError("the claude CLI isn't installed")
        run_dir = self.root / f"run-{len(self.runs) + 1}"
        run_dir.mkdir()
        rules_file = run_dir / "rules.json"
        rules_file.write_text(json.dumps(list(rules), indent=1), encoding="utf-8")
        log = run_dir / "hooks.jsonl"
        log.touch()
        settings_file = run_dir / "settings.json"
        run_settings = settings(hook_command(rules_file, log))
        run_settings.update(extra_settings or {})
        settings_file.write_text(json.dumps(run_settings, indent=1), encoding="utf-8")
        command = [
            claude,
            "-p",
            prompt,
            "--model",
            MODEL,
            "--settings",
            str(settings_file),
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-hook-events",
            "--max-turns",
            str(max_turns),
            "--debug-file",
            str(run_dir / "debug.log"),
        ]
        if allowed_tools:
            command += ["--allowedTools", ",".join(allowed_tools)]
        if permission_mode:
            command += ["--permission-mode", permission_mode]
        if mcp:
            config = {
                "mcpServers": {
                    "contacts": {"command": sys.executable, "args": [str(MCP_SERVER)]}
                }
            }
            mcp_file = run_dir / "mcp.json"
            mcp_file.write_text(json.dumps(config), encoding="utf-8")
            command += ["--mcp-config", str(mcp_file)]
        if resume:
            command += ["--resume", resume]
            if fork:
                command.append("--fork-session")
        command += list(extra_args)
        (run_dir / "command.txt").write_text(shlex.join(command), encoding="utf-8")
        env = environment({"OTEL_LOG_RAW_API_BODIES": f"file:{run_dir / 'api'}"})
        recorder = Recorder(run_dir / "gateway") if gateway else None
        if recorder is not None:
            env["ANTHROPIC_BASE_URL"] = recorder.url
        env.update(extra_env or {})
        try:
            completed = subprocess.run(
                command,
                cwd=self.work,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        finally:
            if recorder is not None:
                recorder.close()
        (run_dir / "stream.jsonl").write_text(completed.stdout, encoding="utf-8")
        debug_file = run_dir / "debug.log"
        run = Run(
            dir=run_dir,
            command=command,
            returncode=completed.returncode,
            stream=_json_lines(completed.stdout),
            hooks=_json_lines(log.read_text(encoding="utf-8")),
            stderr=completed.stderr,
            debug=debug_file.read_text(encoding="utf-8") if debug_file.exists() else "",
        )
        path = run.transcript_path
        if path is not None and path.exists():
            run.transcript_text = path.read_text(encoding="utf-8")
        if recorder is not None:
            run.gateway = recorder.records
        self.runs.append(run)
        return run


# --- Fixtures: saving real payloads without anything real in them ----------

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_TOOL_USE_ID = re.compile(r"\btoolu_[A-Za-z0-9]+\b")
_PROJECTS = re.compile(r"~/\.claude/projects/[^/\"]+/")
_TEMP = re.compile(r"(?:/private)?/(?:tmp|var/folders)/[^\"]*?/(?=tasks/|work/)")
_FICTIONAL_EMAIL = re.compile(r"@example\.(?:com|org|net)$", re.IGNORECASE)
_FICTIONAL_IPS = re.compile(
    r"^(?:192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|127\.)|^0\.0\.0\.0$|^::1?$"
)


def sanitize(value: Any, *, work: Path) -> Any:
    """Return a JSON value with machine-specific paths and ids replaced.

    The working directory becomes ``/work``, the home directory ``~``, and
    session and tool-use ids fixed stand-ins, so fixtures are stable.
    """
    text = json.dumps(value, ensure_ascii=False)
    for path in {str(work), str(work.resolve())}:
        text = text.replace(json.dumps(path)[1:-1], "/work")
    home = str(Path.home())
    text = text.replace(json.dumps(home)[1:-1], "~")
    # Claude Code names a project's transcript directory after its path.
    text = _PROJECTS.sub("~/.claude/projects/-work/", text)
    text = _TEMP.sub("<tmp>/", text)
    text = _UUID.sub("00000000-0000-4000-8000-000000000000", text)
    text = _TOOL_USE_ID.sub("toolu_fixture", text)
    return json.loads(text)


def assert_fictional(text: str) -> None:
    """Raise AssertionError if ``text`` holds PII that isn't obviously fictional.

    Uses veil's own detectors: every email must be at example.com, .org or
    .net, every phone number must use 555, and every IP address must be in a
    documentation or loopback range. Names can't be checked this way; keep
    them to the made-up ones this harness uses.
    """
    from veil import Shield

    for entity in Shield().mask(text).entities:
        kind, value = entity.entity_type, entity.value
        if kind == "EMAIL":
            ok = bool(_FICTIONAL_EMAIL.search(value))
        elif kind == "PHONE":
            ok = "555" in value
        elif kind in ("IPV4", "IPV6"):
            ok = bool(_FICTIONAL_IPS.search(value))
        else:
            ok = False
        assert ok, f"not obviously fictional: a {kind} value ({len(value)} characters)"


def save_fixture(path: Path, value: Any, *, work: Path) -> None:
    """Sanitize ``value``, check it is fictional, and write it as JSON."""
    clean = sanitize(value, work=work)
    text = json.dumps(clean, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    assert_fictional(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
