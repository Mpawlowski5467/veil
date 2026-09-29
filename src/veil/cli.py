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
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, cast

from . import __version__
from .gateway import (
    SECRET_HEADER,
    Gateway,
    SettingsError,
    SQLiteLedger,
    compat,
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
    "SendFile",
    "SendMessage",
    "SendUserFile",
    "ShareOnboardingGuide",
)

#: Claude Code options that would drop the gateway's settings or hooks.
REFUSED_OPTIONS = ("--settings", "--bare", "--safe-mode")

#: Variables that turn every hook off; they are unset for Claude Code.
HOOKS_OFF = ("CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SAFE_MODE")

#: Seconds ``claude --version`` may take; past that the version is unknown.
VERSION_TIMEOUT = 3.0

# The versions already warned about, one "<claude code> <this package>" line
# each, in the data folder.
_WARNED = "version-warnings"


def hook_command(data_dir: Path) -> list[str]:
    """The command Claude Code runs for the gateway's hooks.

    ``-I`` keeps a project's ``PYTHONPATH`` and folder from changing what
    runs; the package's own folder is put on the path explicitly, so the
    hooks find it however it was installed.
    """
    package_folder = str(Path(__file__).resolve().parent.parent)
    code = (
        f"import sys; sys.path.insert(0, {package_folder!r}); "
        f"from {APP}.cli import main; sys.exit(main())"
    )
    return [sys.executable, "-I", "-c", code, "--data-dir", str(data_dir), "hook"]


def claude_settings(
    gateway: Gateway, *, data_dir: Path | None = None, headers: str = ""
) -> dict[str, Any]:
    """Return the settings a Claude Code process gets, as for ``--settings``.

    Settings given on the command line outrank a project's settings, the
    user's, and the environment, so they pin everything that could send
    requests elsewhere or turn the hooks off. With ``data_dir``, they also
    run the gateway's hooks (see `gateway.hooks`).
    """
    custom = f"{SECRET_HEADER}: {gateway.secret}"
    env = {
        "ANTHROPIC_BASE_URL": gateway.url,
        "ANTHROPIC_CUSTOM_HEADERS": f"{headers}\n{custom}" if headers else custom,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "VEIL_GATEWAY_URL": gateway.url,
        "VEIL_GATEWAY_SECRET": gateway.secret,
    }
    # An empty value means unset: no other provider, hooks on.
    env.update(dict.fromkeys(hooks.OTHER_PROVIDERS, ""))
    env.update(dict.fromkeys(HOOKS_OFF, ""))
    settings: dict[str, Any] = {
        "env": env,
        "permissions": {"deny": list(DENIED_TOOLS)},
        "disableAllHooks": False,
        "remoteControlAtStartup": False,
    }
    if data_dir is not None:
        settings["hooks"] = hook_settings(hook_command(data_dir), gateway.url)
    return settings


def _claude_user_env() -> dict[str, str]:
    """The ``env`` of the user's own Claude Code settings, if readable."""
    try:
        raw = json.loads((Path.home() / ".claude" / "settings.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    env = raw.get("env") if isinstance(raw, dict) else None
    return (
        {k: v for k, v in env.items() if isinstance(v, str)}
        if isinstance(env, dict)
        else {}
    )


def _problems(args: Sequence[str], user_env: dict[str, str]) -> list[str]:
    """What would keep requests from going through the gateway, if anything."""
    found = []
    for option in REFUSED_OPTIONS:
        if any(a == option or a.startswith(f"{option}=") for a in args):
            found.append(f"{option} would drop the masking settings; leave it out")
    for source, env in (
        ("your environment", os.environ),
        ("~/.claude/settings.json", user_env),
    ):
        if env.get("ANTHROPIC_BASE_URL"):
            found.append(
                f"ANTHROPIC_BASE_URL is set in {source}; {APP} claude sends requests "
                "to the Anthropic API itself, so unset it"
            )
    return found


def claude_version(executable: str) -> str | None:
    """The version the claude command reports, or None if it can't be read."""
    try:
        done = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=VERSION_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    return compat.from_cli_output(done.stdout)


def version_warning(version: str | None, data_dir: Path) -> str | None:
    """The line to print about an untested Claude Code version, if any.

    Given once for each pair of Claude Code and this package's version:
    Claude Code updates often, and a line on every start would soon be
    ignored. Never raises.
    """
    line = compat.version_warning(version)
    if line is None:
        return None
    pair = f"{version} {__version__}"
    path = data_dir / _WARNED
    try:
        if pair in path.read_text(encoding="utf-8").splitlines():
            return None
    except (OSError, ValueError):
        pass
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(pair + "\n")
    except OSError:
        pass
    return line


def _open(data_dir: Path, cwd: Path) -> tuple[Settings, Any]:
    prepare_data_dir(data_dir)
    settings = load_settings(data_dir / "config.json")
    identity = git_identity(cwd) if settings.identity else {}
    return settings, open_sessions(data_dir, settings, identity)


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _check_hooks(command: list[str], env: dict[str, str], url: str) -> str | None:
    """Run both hooks once as Claude Code would; return a problem, or None."""
    probes = [
        (
            ["pre-tool-use"],
            {
                "session_id": "_check",
                "tool_name": "Bash",
                "tool_input": {"command": "true"},
            },
        ),
        (
            ["user-prompt-submit", "--expect-url", url],
            {"session_id": "_check", "prompt": "check"},
        ),
    ]
    for extra, payload in probes:
        try:
            done = subprocess.run(
                [*command, *extra],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return f"the {extra[0]} hook couldn't be started"
        if done.returncode != 0 or done.stdout.strip():
            return f"the {extra[0]} hook didn't work (exit code {done.returncode})"
    return None


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
    user_env = _claude_user_env()
    problems = _problems(args, user_env)
    if problems:
        for problem in problems:
            print(f"{APP}: {problem}", file=sys.stderr)
        return 2
    settings, sessions = _open(data_dir, cwd or Path.cwd())
    registered = sum(len(values) for values in settings.entities.values())
    headers = "\n".join(
        h
        for h in (
            user_env.get("ANTHROPIC_CUSTOM_HEADERS"),
            os.environ.get("ANTHROPIC_CUSTOM_HEADERS"),
        )
        if h
    )
    with Gateway(sessions) as gateway:
        custom = claude_settings(gateway, data_dir=data_dir, headers=headers)
        env = {k: v for k, v in os.environ.items() if k not in HOOKS_OFF}
        env.update({k: v for k, v in custom["env"].items() if v})
        for name in hooks.OTHER_PROVIDERS:
            env.pop(name, None)
        # A hook that can't run lets every call through: check them first.
        broken = _check_hooks(hook_command(data_dir), env, gateway.url)
        if broken is not None:
            print(f"{APP}: {broken}, so Claude Code wasn't started", file=sys.stderr)
            return 1
        # The settings go in an owner-only file: on the command line, other
        # users could read the gateway's secret with ps.
        settings_file = data_dir / f"claude-settings-{os.getpid()}.json"
        _write_private(settings_file, json.dumps(custom))
        print(
            f"{APP}: masking through a local gateway "
            f"({registered} registered values, {len(settings.patterns)} patterns)",
            file=sys.stderr,
        )
        warning = version_warning(claude_version(executable), data_dir)
        if warning is not None:
            print(warning, file=sys.stderr)
        try:
            code = _run_child(
                [executable, "--settings", str(settings_file), *args], env, cwd
            )
        finally:
            settings_file.unlink(missing_ok=True)
        # Claude Code has left the terminal: say what it couldn't send, since
        # a background request (a session title, say) fails without a word.
        for line in gateway.refusals.summary():
            print(line, file=sys.stderr)
        return code


def _run_child(command: list[str], env: dict[str, str], cwd: Path | None) -> int:
    """Run a client, passing on signals, and return its exit code."""
    child = subprocess.Popen(command, env=env, cwd=cwd)

    def pass_on(signum: int, _frame: object) -> None:
        child.send_signal(signum)

    # Ctrl-C reaches Claude Code on its own; a SIGTERM or SIGHUP sent to this
    # process is passed on, so Claude Code never outlives its gateway.
    previous = {
        signal.SIGINT: signal.signal(signal.SIGINT, signal.SIG_IGN),
        signal.SIGTERM: signal.signal(signal.SIGTERM, pass_on),
        signal.SIGHUP: signal.signal(signal.SIGHUP, pass_on),
    }
    try:
        while True:
            try:
                return child.wait()
            except KeyboardInterrupt:
                continue
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child.poll() is None:
            child.terminate()


def gateway_secret(data_dir: Path) -> str:
    """Return the long-running gateway's secret, creating it once (owner-only)."""
    path = data_dir / "gateway-secret"
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        pass
    secret = secrets.token_urlsafe(32)
    _write_private(path, secret + "\n")
    return secret


def run_gateway(
    *,
    data_dir: Path,
    port: int,
    stop: Any = None,
    api: Literal["anthropic", "openai"] = "anthropic",
    auth: Literal["api-key", "chatgpt"] = "api-key",
) -> int:
    """Run a gateway until interrupted, printing how to point clients at it."""
    prepare_data_dir(data_dir)
    settings = load_settings(data_dir / "config.json")
    identity = git_identity(Path.cwd()) if settings.identity else {}
    sessions = open_sessions(data_dir, settings, identity, api=api)
    with Gateway(
        sessions, port=port, secret=gateway_secret(data_dir), api=api, openai_auth=auth
    ) as gateway:
        if api == "openai":
            from .codex import write_configuration

            configuration = write_configuration(gateway, data_dir)
            _print_openai_gateway(gateway)
            print(f"Private desktop/CLI configuration fragment: {configuration}")
            sys.stdout.flush()
            try:
                while stop is None or not stop():
                    time.sleep(0.2)
            except KeyboardInterrupt:
                pass
            return 0
        custom = claude_settings(gateway, data_dir=data_dir)
        custom["env"] = {k: v for k, v in custom["env"].items() if v}
        print(f"{APP} gateway listening at {gateway.url}")
        print(
            "Point Claude Code at it with these settings, in ~/.claude/settings.json:"
        )
        print(json.dumps(custom, indent=2))
        print(
            "They hold the gateway's secret: keep that file readable by you only. "
            "Keep the gateway running while clients use it; if its port is taken "
            f"by something else, the prompt check holds prompts back. `{APP} claude` "
            "starts a private gateway for each session instead."
        )
        sys.stdout.flush()
        try:
            while stop is None or not stop():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
    return 0


def _print_openai_gateway(gateway: Gateway) -> None:
    """Print a private provider configuration for local Codex clients."""
    from .codex import provider, toml_value

    print(f"{APP} experimental OpenAI gateway listening at {gateway.url}")
    print("Add this provider to your user-level ~/.codex/config.toml:")
    print("[model_providers.veil]")
    for key, value in provider(gateway).items():
        print(f"{key} = {toml_value(value)}")
    print("Start a new Codex session with:")
    print("codex --no-daemon -c 'model_provider=\"veil\"' -c 'web_search=\"disabled\"'")
    print(
        "Keep the configuration private and this gateway running. This path "
        f"uses {gateway.openai_auth} authentication. Only supported text in model "
        "requests is masked. Returned tool inputs containing private values are "
        "blocked except direct local patches. Local execution and other client "
        "traffic remain outside the gateway. See docs/openai-integration.md."
    )


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
    except Exception:
        allowed = ()  # nothing extra allowed; the gateway reports the error
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
    parser.add_argument(
        "--forget-after-run",
        action="store_true",
        help="use temporary mappings for a fresh Claude/Codex launch; remove on exit",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    entities = commands.add_parser("entities", help="manage exact private values")
    entity_commands = entities.add_subparsers(dest="operation", required=True)
    for operation in ("add", "remove"):
        entity_command = entity_commands.add_parser(operation)
        entity_command.add_argument(
            "entity_type", help="type such as PERSON or ORGANIZATION"
        )
        entity_command.add_argument(
            "--stdin",
            action="store_true",
            help="read one private value from stdin instead of a hidden prompt",
        )
    entity_list = entity_commands.add_parser(
        "list", help="show counts, without private values"
    )
    entity_list.add_argument("entity_type", nargs="?")
    entity_list.add_argument("--json", action="store_true")
    verify = commands.add_parser(
        "verify", help="create or check a client verification prompt"
    )
    verify.add_argument("--check", help="verification ID to inspect")
    verify.add_argument("--config", type=Path, help="Codex config.toml path")
    verify.add_argument(
        "--gateway-url", help="explicit loopback gateway; secret from --data-dir"
    )
    verify.add_argument("--json", action="store_true")
    skill = commands.add_parser("skill", help="install or remove assistant skills")
    skill.add_argument("operation", choices=("install", "uninstall"))
    skill.add_argument(
        "client", choices=("codex", "claude", "all"), nargs="?", default="all"
    )
    skill.add_argument(
        "--skills-dir", type=Path, help="custom skills directory for one client"
    )
    commands.add_parser(
        "claude",
        help="run Claude Code through a private gateway",
        description="Run Claude Code through a private gateway; the arguments "
        "after 'claude' go to Claude Code as they are.",
        add_help=False,
    )
    codex = commands.add_parser("codex", help="run Codex CLI through a private gateway")
    codex.add_argument("--auth", choices=("chatgpt", "api-key"), default="chatgpt")
    codex.add_argument(
        "args", nargs=argparse.REMAINDER, help="Codex arguments after --"
    )
    gateway = commands.add_parser("gateway", help="run a long-lived gateway")
    for operation in ("start", "stop", "restart"):
        service = commands.add_parser(
            operation, help=f"{operation} a background gateway"
        )
        service.add_argument(
            "--config", type=Path, help="Codex setup configuration path"
        )
        if operation == "stop":
            service.add_argument(
                "--remove",
                action="store_true",
                help="also remove saved service options",
            )
        else:
            service.add_argument(
                "--port", type=int, help="gateway port (saved setup or 8485)"
            )
            service.add_argument("--api", choices=("anthropic", "openai"))
            service.add_argument("--auth", choices=("api-key", "chatgpt"))
            service.add_argument(
                "--cwd", type=Path, help="folder for git identity (saved on restart)"
            )
    for operation in ("setup", "undo"):
        configuration = commands.add_parser(
            operation, help=f"{operation} a backed-up Codex desktop configuration"
        )
        configuration.add_argument("client", choices=("codex",))
        configuration.add_argument("--config", type=Path, help="Codex config.toml path")
        if operation == "setup":
            configuration.add_argument("--port", type=int, default=8485)
            configuration.add_argument(
                "--auth", choices=("chatgpt", "api-key"), default="chatgpt"
            )
    for operation in ("status", "doctor"):
        diagnostics = commands.add_parser(
            operation, help="check saved Codex routing and local gateway readiness"
        )
        diagnostics.add_argument("--config", type=Path, help="Codex config.toml path")
        diagnostics.add_argument(
            "--json", action="store_true", help="machine-readable report"
        )
        if operation == "status":
            diagnostics.add_argument(
                "--activity",
                action="store_true",
                help="inspect recent gateway activity",
            )
            diagnostics.add_argument(
                "--verification", help="check evidence for a verification ID"
            )
            diagnostics.add_argument(
                "--gateway-url",
                help="explicit loopback gateway; secret from --data-dir",
            )
            diagnostics.add_argument(
                "--service",
                action="store_true",
                help="check the background worker only",
            )
    for operation in ("mask", "restore"):
        text_command = commands.add_parser(
            operation, help=f"{operation} local text for a chat"
        )
        text_command.add_argument(
            "--session", required=True, help="one label per conversation"
        )
        text_command.add_argument(
            "--clipboard", action="store_true", help="read and replace clipboard text"
        )
        if operation == "restore":
            text_command.add_argument(
                "--exact", action="store_true", help="restore exact placeholders only"
            )
    gateway.add_argument("--port", type=int, default=8484)
    gateway.add_argument("--api", choices=("anthropic", "openai"), default="anthropic")
    gateway.add_argument("--auth", choices=("api-key", "chatgpt"), default="api-key")
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
    if os.name == "nt":
        # Windows pipes otherwise use the legacy ANSI code page, even though
        # the local text/file workflow promises UTF-8. Keep interactive console
        # handling with Python; only reconfigure redirected real streams.
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            configure = getattr(stream, "reconfigure", None)
            if not stream.isatty() and callable(configure):
                configure(encoding="utf-8", errors="strict")
    own, passed_on = _split_claude(list(sys.argv[1:] if argv is None else argv))
    options = _parser().parse_args(own)
    # Absolute, since the hooks run from wherever Claude Code is working.
    data_dir = (options.data_dir or default_data_dir()).absolute()
    try:
        if options.forget_after_run:
            if options.command not in {"claude", "codex"}:
                raise SettingsError(
                    "--forget-after-run is only for claude or codex launches"
                )
            settings = load_settings(data_dir / "config.json")
            with tempfile.TemporaryDirectory(prefix="veil-run-") as temporary:
                private = prepare_data_dir(Path(temporary) / "data")
                _write_private(private / "config.json", json.dumps(asdict(settings)))
                print(
                    f"{APP}: temporary mappings; removed when this launch exits. "
                    "Client transcripts remain. Interrupted cleanup may leave files.",
                    file=sys.stderr,
                )
                if options.command == "claude":
                    return run_claude(passed_on, data_dir=private)
                from .codex import run_codex

                args = options.args[1:] if options.args[:1] == ["--"] else options.args
                return run_codex(args, data_dir=private, auth=options.auth)
        if options.command == "entities":
            from .entities_cli import run_entities

            return run_entities(
                options.operation,
                data_dir=data_dir,
                entity_type=options.entity_type,
                stdin=getattr(options, "stdin", False),
                json_output=getattr(options, "json", False),
            )
        if options.command == "verify":
            from .verification import run_verify

            return run_verify(
                check=options.check,
                json_output=options.json,
                config=options.config,
                data_dir=options.data_dir,
                gateway_url=options.gateway_url,
            )
        if options.command == "status" and (
            options.activity or options.verification or options.gateway_url
        ):
            from .verification import run_activity

            if options.service:
                raise SettingsError(
                    "choose --service or activity/verification status, not both"
                )
            return run_activity(
                verification=options.verification,
                json_output=options.json,
                config=options.config,
                data_dir=options.data_dir,
                gateway_url=options.gateway_url,
            )
        if options.command == "skill":
            from .skill import run_skill

            return run_skill(
                options.operation, options.client, skills_dir=options.skills_dir
            )
        if options.command in {"start", "stop", "restart"}:
            from .service import run_service

            return run_service(
                cast("Literal['start', 'stop', 'restart']", options.command),
                data_dir=options.data_dir,
                config=options.config,
                port=getattr(options, "port", None),
                api=getattr(options, "api", None),
                auth=getattr(options, "auth", None),
                cwd=getattr(options, "cwd", None),
                remove=getattr(options, "remove", False),
            )
        if options.command in {"setup", "undo"}:
            from .codex_setup import config_path, setup_codex, undo_codex

            target = config_path(options.config)
            if options.command == "undo":
                return undo_codex(target)
            return setup_codex(data_dir, target, options.port, options.auth)
        if options.command in {"status", "doctor"}:
            if getattr(options, "service", False):
                from .service import run_service_status

                return run_service_status(
                    options.data_dir, options.config, options.json
                )
            from .diagnostics import run_diagnostics

            return run_diagnostics(
                config=options.config,
                data_dir=data_dir if options.data_dir is not None else None,
                doctor=options.command == "doctor",
                json_output=options.json,
            )
        if options.command == "claude":
            return run_claude(passed_on, data_dir=data_dir)
        if options.command == "codex":
            from .codex import run_codex

            args = options.args[1:] if options.args[:1] == ["--"] else options.args
            return run_codex(args, data_dir=data_dir, auth=options.auth)
        if options.command == "gateway":
            if options.api != "openai" and options.auth != "api-key":
                raise SettingsError("--auth chatgpt requires --api openai")
            return run_gateway(
                data_dir=data_dir, port=options.port, api=options.api, auth=options.auth
            )
        if options.command in {"mask", "restore"}:
            from .text_cli import run_text

            return run_text(
                options.command,
                data_dir=data_dir,
                session=options.session,
                clipboard=options.clipboard,
                exact=getattr(options, "exact", False),
            )
        if options.command == "hook":
            return _hook(options.event, data_dir, options.expect_url)
        prepare_data_dir(data_dir)
        return forget(
            data_dir=data_dir, session=None if options.all else options.session
        )
    except SettingsError as error:
        print(f"{APP}: {error}", file=sys.stderr)
        return 2
    except OSError:
        if options.command == "entities":
            print(
                f"{APP}: could not access private input or configuration; "
                "check permissions and available disk space",
                file=sys.stderr,
            )
            return 2
        if options.command == "skill":
            print(
                f"{APP}: could not access skill files; check permissions and "
                "available disk space. Preserve any .veil-skill-previous-* folder.",
                file=sys.stderr,
            )
            return 2
        if options.command not in {
            "setup",
            "undo",
            "status",
            "doctor",
            "start",
            "stop",
            "restart",
            "verify",
        }:
            raise
        print(
            f"{APP}: could not access setup, service, or diagnostic files; "
            "check permissions "
            "and available disk space. Preserve any existing backup and receipt.",
            file=sys.stderr,
        )
        return 2
