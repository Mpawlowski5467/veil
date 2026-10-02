"""Launch a standalone Codex CLI through Veil without editing user settings."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from .gateway import (
    SECRET_HEADER,
    Gateway,
    git_identity,
    load_settings,
    open_sessions,
    prepare_data_dir,
)
from .launches import published, review_command

Auth = Literal["api-key", "chatgpt"]


def toml_value(value: Any) -> str:
    """Encode the small JSON-compatible subset needed for Codex -c options."""
    if isinstance(value, Mapping):
        return (
            "{"
            + ",".join(
                f"{json.dumps(key)}={toml_value(item)}" for key, item in value.items()
            )
            + "}"
        )
    return json.dumps(value, ensure_ascii=True)


def provider(gateway: Gateway, *, environment_secret: bool = False) -> dict[str, Any]:
    """Return a provider table for the gateway's selected authentication mode."""
    return provider_configuration(
        gateway.url,
        gateway.secret,
        gateway.openai_auth,
        environment_secret=environment_secret,
    )


def provider_configuration(
    url: str, secret: str, auth: Auth, *, environment_secret: bool = False
) -> dict[str, Any]:
    """Build settings without starting a gateway or changing a user's files."""
    result: dict[str, Any] = {
        "name": "Veil",
        "base_url": url + "/v1",
        "wire_api": "responses",
        "supports_websockets": False,
        "supports_standalone_web_search": False,
        "requires_openai_auth": auth == "chatgpt",
    }
    if auth == "api-key":
        result["env_key"] = "OPENAI_API_KEY"
    if environment_secret:
        result["env_http_headers"] = {SECRET_HEADER: "VEIL_GATEWAY_SECRET"}
    else:
        result["http_headers"] = {SECRET_HEADER: secret}
    return result


def write_configuration(gateway: Gateway, data_dir: Path) -> Path:
    """Write a private configuration fragment for desktop or manual CLI setup."""
    lines = [
        "# Merge into user-level Codex settings; do not replace unrelated settings.",
        'model_provider = "veil"',
        'web_search = "disabled"',
        "[features]",
        "apps = false",
        "multi_agent = false",
        "[model_providers.veil]",
        *[f"{key} = {toml_value(value)}" for key, value in provider(gateway).items()],
        "",
    ]
    path = data_dir / "codex-provider.toml"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=data_dir, delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write("\n".join(lines))
            file.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def _problem(args: Sequence[str]) -> str | None:
    # These can select a different transport or replace the managed settings.
    refused = {
        "--config",
        "-c",
        "--profile",
        "-p",
        "--oss",
        "--local-provider",
        "--remote",
        "--remote-auth-token-env",
        "--search",
        "--enable",
        "--disable",
    }
    for arg in args:
        if arg.split("=", 1)[0] in refused or (
            arg.startswith(("-c", "-p")) and not arg.startswith("--") and len(arg) > 2
        ):
            return "routing and feature overrides are not accepted by veil codex"
    if any(
        arg
        in {
            "app",
            "app-server",
            "cloud",
            "login",
            "logout",
            "mcp",
            "plugin",
            "remote-control",
            "agents",
            "queue",
            "exec-server",
        }
        for arg in args
    ):
        return "veil codex supports local CLI conversations, not this command"
    return None


def run_codex(
    args: Sequence[str],
    *,
    data_dir: Path,
    auth: Auth = "chatgpt",
    codex: str | None = None,
    cwd: Path | None = None,
    launch_dir: Path | None = None,
) -> int:
    """Keep a gateway alive for one local Codex process and forward its exit code.

    The gateway's launch record goes in ``launch_dir`` (by default
    ``data_dir``), where review and verification in another terminal look.
    """
    from .cli import _run_child

    executable = codex or shutil.which("codex")
    if executable is None:
        print("veil: the codex command wasn't found on PATH", file=sys.stderr)
        return 1
    problem = _problem(args)
    if problem is not None:
        print("veil: " + problem, file=sys.stderr)
        return 2
    if auth == "api-key" and not os.environ.get("OPENAI_API_KEY"):
        print(
            "veil: set OPENAI_API_KEY locally before using --auth api-key",
            file=sys.stderr,
        )
        return 2
    prepare_data_dir(data_dir)
    settings = load_settings(data_dir / "config.json")
    identity = git_identity(cwd or Path.cwd()) if settings.identity else {}
    sessions = open_sessions(data_dir, settings, identity, api="openai")
    record_dir = launch_dir or data_dir
    with Gateway(sessions, api="openai", openai_auth=auth) as gateway:
        gateway.review_command = review_command(gateway.url, record_dir)
        overrides: dict[str, Any] = {
            "model_provider": "veil",
            "model_providers.veil": provider(gateway, environment_secret=True),
            "web_search": "disabled",
            "features.apps": False,
            "features.multi_agent": False,
            "analytics.enabled": False,
        }
        command = [executable, "--no-daemon"]
        for key, value in overrides.items():
            command.extend(["-c", f"{key}={toml_value(value)}"])
        command.extend(args)
        env = {
            **os.environ,
            "VEIL_GATEWAY_SECRET": gateway.secret,
            "VEIL_GATEWAY_URL": gateway.url,
        }
        print(
            f"veil: Codex through a local masking gateway ({auth}); "
            "private values in returned tools are limited to direct local patches",
            file=sys.stderr,
        )
        with published(record_dir, "codex", gateway.url, gateway.secret):
            return _run_child(command, env, cwd)
