"""Command-line entry point: run a client such as Claude Code through the gateway.

``<prog> claude [ARGS...]`` starts a gateway for one Claude Code process and
runs ``claude ARGS...`` through it: every request Claude Code makes to the
model is masked, and every reply restored. When Claude Code exits, so does
the gateway.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from .gateway import (
    SECRET_HEADER,
    Gateway,
    SettingsError,
    SQLiteLedger,
    default_data_dir,
    git_identity,
    hooks,
    load_settings,
    open_sessions,
    prepare_data_dir,
)
from .gateway.config import APP, Settings
from .gateway.hooks import hook_settings
from .vault.sqlite import SQLiteVault

#: Tools that send content through Anthropic-hosted services, or to another
#: session that may not use the gateway; real values there would leave the
#: machine unmasked, so they are turned off.
DENIED_TOOLS = (
    "Artifact",
    "ArtifactComments",
    "ArtifactData",
    "DesignSync",
    "PushNotification",
    "RemoteTrigger",
    "SendMessage",
    "SendUserFile",
    "ShareOnboardingGuide",
)


def hook_command(data_dir: Path) -> list[str]:
    """The command Claude Code runs for the gateway's hooks."""
    # -I: a project's PYTHONPATH or current folder can't change what runs.
    return [sys.executable, "-I", "-m", APP, "--data-dir", str(data_dir), "hook"]


def claude_settings(
    gateway: Gateway, *, data_dir: Path | None = None, headers: str = ""
) -> dict[str, Any]:
    """Return the settings a Claude Code process gets, as for ``--settings``.

    Settings given on the command line outrank a project's settings and the
    environment, so a project can't point Claude Code elsewhere. With
    ``data_dir``, they also run the gateway's hooks (see `gateway.hooks`).
    """
    custom = f"{SECRET_HEADER}: {gateway.secret}"
    settings: dict[str, Any] = {
        "env": {
            "ANTHROPIC_BASE_URL": gateway.url,
            "ANTHROPIC_CUSTOM_HEADERS": f"{headers}\n{custom}" if headers else custom,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        "permissions": {"deny": list(DENIED_TOOLS)},
    }
    if data_dir is not None:
        settings["hooks"] = hook_settings(hook_command(data_dir), gateway.url)
    return settings


def _open(data_dir: Path, cwd: Path) -> tuple[Settings, Any]:
    prepare_data_dir(data_dir)
    settings = load_settings(data_dir / "config.json")
    identity = git_identity(cwd) if settings.identity else {}
    return settings, open_sessions(data_dir, settings, identity)


def run_claude(
    args: Sequence[str],
    *,
    data_dir: Path,
    claude: str | None = None,
    cwd: Path | None = None,
) -> int:
    """Run Claude Code with ``args`` through a gateway; return its exit code."""
    executable = claude or shutil.which("claude")
    if executable is None:
        print(f"{APP}: the claude command wasn't found on PATH", file=sys.stderr)
        return 1
    settings, sessions = _open(data_dir, cwd or Path.cwd())
    registered = sum(len(values) for values in settings.entities.values())
    with Gateway(sessions) as gateway:
        custom = claude_settings(
            gateway,
            data_dir=data_dir,
            headers=os.environ.get("ANTHROPIC_CUSTOM_HEADERS", ""),
        )
        env = {**os.environ, **custom["env"]}
        print(
            f"{APP}: masking through a local gateway "
            f"({registered} registered values, {len(settings.patterns)} patterns)",
            file=sys.stderr,
        )
        command = [executable, "--settings", json.dumps(custom), *args]
        # Ctrl-C belongs to Claude Code; this process waits for it to exit.
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return subprocess.call(command, env=env, cwd=cwd)
        finally:
            signal.signal(signal.SIGINT, previous)


def gateway_secret(data_dir: Path) -> str:
    """Return the long-running gateway's secret, creating it once (owner-only)."""
    path = data_dir / "gateway-secret"
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        pass
    secret = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(secret + "\n")
    return secret


def run_gateway(*, data_dir: Path, port: int, stop: Any = None) -> int:
    """Run a gateway until interrupted, printing how to point clients at it."""
    _, sessions = _open(data_dir, Path.cwd())
    with Gateway(sessions, port=port, secret=gateway_secret(data_dir)) as gateway:
        env = claude_settings(gateway)["env"]
        print(f"{APP} gateway listening at {gateway.url}")
        print(
            "Point a client at it with these settings, e.g. in ~/.claude/settings.json:"
        )
        print(json.dumps({"env": env}, indent=2))
        print(
            "Keep it running while clients use it: if the port is taken by "
            f"something else, requests would reach that instead. `{APP} claude` "
            "avoids this by starting a private gateway for each session."
        )
        sys.stdout.flush()
        try:
            while stop is None or not stop():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
    return 0


def forget(*, data_dir: Path, session: str | None) -> int:
    """Delete one conversation's mappings, or every conversation's."""
    vault_path, ledger_path = data_dir / "vault.db", data_dir / "ledger.db"
    if session is None:
        vault = SQLiteVault(vault_path, session="_maintenance")
        ledger = SQLiteLedger(ledger_path, "_maintenance")
        try:
            vault.purge(timedelta(0))
            ledger.purge(timedelta(0))
        finally:
            vault.close()
            ledger.close()
        print(f"{APP}: forgot every conversation")
        return 0
    vault = SQLiteVault(vault_path, session=session)
    ledger = SQLiteLedger(ledger_path, session)
    try:
        vault.clear()
        ledger.forget()
    finally:
        vault.close()
        ledger.close()
    print(f"{APP}: forgot that conversation")
    return 0


def _hook(event: str, data_dir: Path, expected_url: str | None) -> int:
    try:
        allowed = load_settings(data_dir / "config.json").allow_mcp_tools
    except SettingsError:
        allowed = ()  # the hook still refuses; the gateway reports the error
    return hooks.run(
        event,
        stdin=sys.stdin,
        stdout=sys.stdout,
        vault_path=data_dir / "vault.db",
        expected_url=expected_url,
        allowed_mcp_tools=allowed,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP, description="Mask personal data before it reaches a model."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=f"where mappings and config.json live (default: ~/.{APP})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "claude",
        help="run Claude Code through a private gateway",
        description="Run Claude Code through a private gateway; the arguments "
        "after 'claude' go to Claude Code as they are.",
        add_help=False,
    )
    gateway = commands.add_parser("gateway", help="run a long-lived gateway")
    gateway.add_argument("--port", type=int, default=8484)
    hook = commands.add_parser("hook", help="answer a Claude Code hook (internal)")
    hook.add_argument("event", choices=["pre-tool-use", "user-prompt-submit"])
    hook.add_argument("--expect-url")
    forget_parser = commands.add_parser("forget", help="delete stored mappings")
    which = forget_parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--session", help="a Claude Code session id")
    which.add_argument("--all", action="store_true", help="every conversation")
    return parser


def _split_claude(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split off what follows the ``claude`` command, which goes to Claude Code."""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--data-dir":
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            if arg == "claude":
                return argv[: i + 1], argv[i + 1 :]
            break
    return argv, []


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; return the exit code."""
    own, passed_on = _split_claude(list(sys.argv[1:] if argv is None else argv))
    options = _parser().parse_args(own)
    data_dir = options.data_dir or default_data_dir()
    try:
        if options.command == "claude":
            return run_claude(passed_on, data_dir=data_dir)
        if options.command == "gateway":
            return run_gateway(data_dir=data_dir, port=options.port)
        if options.command == "hook":
            return _hook(options.event, data_dir, options.expect_url)
        prepare_data_dir(data_dir)
        return forget(
            data_dir=data_dir, session=None if options.all else options.session
        )
    except SettingsError as error:
        print(f"{APP}: {error}", file=sys.stderr)
        return 2
