"""Read-only local readiness checks; never claim an active task is protected."""

from __future__ import annotations

import http.client
import json
import os
import shlex
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from . import Shield
from .codex_setup import config_path, parse_toml, read_private_file, read_receipt
from .gateway import SECRET_HEADER, SettingsError, default_data_dir, load_settings
from .gateway.hooks import STATUS_PATH, _gateway_answers


@dataclass(frozen=True)
class Check:
    """A result contains fixed explanations, never raw configuration values."""

    name: str
    state: Literal["pass", "warn", "fail"]
    detail: str
    remedy: str = ""


def _local_endpoint(provider: dict[str, Any]) -> str | None:
    try:
        url = urlsplit(provider.get("base_url", ""))
        if (
            url.scheme == "http"
            and url.hostname == "127.0.0.1"
            and url.port
            and url.username is None
            and url.password is None
            and url.path == "/v1"
            and not url.query
            and not url.fragment
        ):
            return f"http://127.0.0.1:{url.port}"
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _gateway_check(provider: dict[str, Any]) -> Check:
    url = _local_endpoint(provider)
    if url is None:
        return Check(
            "gateway",
            "fail",
            "The saved Veil endpoint is not a loopback /v1 URL.",
            "Run veil setup codex with the intended port. External hosts are ignored.",
        )
    headers = provider.get("http_headers", {})
    secret = headers.get(SECRET_HEADER) if isinstance(headers, dict) else None
    if not isinstance(secret, str) or not secret:
        return Check(
            "gateway",
            "fail",
            "The local gateway header is missing.",
            "Run veil setup codex; keep its secret private.",
        )
    if not _gateway_answers(url, secret):
        return Check(
            "gateway",
            "fail",
            "The gateway is stopped or its identity does not match.",
            "Start the gateway with the setup data folder and port; check conflicts.",
        )
    connection = http.client.HTTPConnection(urlsplit(url).netloc, timeout=3)
    try:
        connection.request("GET", STATUS_PATH, headers={SECRET_HEADER: secret})
        response = connection.getresponse()
        mode = json.loads(response.read(4097)) if response.status == 200 else {}
        expected = (
            "chatgpt" if provider.get("requires_openai_auth") is True else "api-key"
        )
        if not isinstance(mode, dict) or not mode:
            return Check(
                "gateway",
                "warn",
                "Gateway identity verified; runtime mode is unavailable.",
                "Restart the gateway with this Veil version, then run veil doctor.",
            )
        if mode.get("api") != "openai" or mode.get("auth") != expected:
            return Check(
                "gateway",
                "fail",
                "Gateway API/auth mode differs from Codex settings.",
                f"Restart it with --api openai --auth {expected}.",
            )
    except (OSError, http.client.HTTPException, ValueError):
        return Check(
            "gateway",
            "fail",
            "Gateway identity passed, but its status failed.",
            "Restart the gateway and retry.",
        )
    finally:
        connection.close()
    return Check(
        "gateway", "pass", "Loopback gateway identity and API/auth mode verified."
    )


def inspect_configuration(config: Path) -> tuple[list[Check], dict[str, Any]]:
    """Check saved routing and the live gateway without making a model call."""
    checks: list[Check] = []
    if not config.exists():
        return [
            Check(
                "configuration",
                "fail",
                "No Codex configuration file found.",
                "Run veil setup codex.",
            )
        ], {}
    try:
        document = parse_toml(read_private_file(config))
        data = document.unwrap()
    except SettingsError as error:
        return [
            Check(
                "configuration",
                "fail",
                str(error),
                "Fix the configuration or install Veil's desktop extra.",
            )
        ], {}
    if data.get("model_provider") != "veil":
        checks.append(
            Check(
                "routing",
                "fail",
                "Saved settings do not select Veil.",
                "Run veil setup codex; restart Codex and start a fresh local task.",
            )
        )
    else:
        checks.append(
            Check("routing", "pass", "Saved settings select the Veil provider.")
        )
    if data.get("profile"):
        checks.append(
            Check(
                "profile",
                "fail",
                "A selected profile can override these settings.",
                "Review the profile; this check does not resolve profile overrides.",
            )
        )
    features = data.get("features", {})
    if (
        not isinstance(features, dict)
        or features.get("apps") is not False
        or features.get("multi_agent") is not False
        or data.get("web_search") != "disabled"
    ):
        checks.append(
            Check(
                "features",
                "fail",
                "Unsupported hosted features are not disabled.",
                "Run veil setup codex to disable unsupported hosted features.",
            )
        )
    else:
        checks.append(
            Check(
                "features", "pass", "Hosted search, apps, and subagents are disabled."
            )
        )
    providers = data.get("model_providers", {})
    provider = providers.get("veil", {}) if isinstance(providers, dict) else {}
    if not isinstance(provider, dict):
        provider = {}
    if (
        provider.get("wire_api") != "responses"
        or provider.get("supports_websockets") is not False
    ):
        checks.append(
            Check(
                "transport",
                "fail",
                "Veil requires Responses over HTTP/SSE.",
                "Run veil setup codex to restore supported transport settings.",
            )
        )
    else:
        checks.append(Check("transport", "pass", "Responses HTTP/SSE selected."))
    if provider.get("auth") or provider.get("experimental_bearer_token"):
        checks.append(
            Check(
                "authentication",
                "fail",
                "An unsupported authentication override is present.",
                "Use the auth configuration generated by veil setup codex.",
            )
        )
    elif provider.get("requires_openai_auth") is True and provider.get("env_key"):
        checks.append(
            Check(
                "authentication",
                "fail",
                "ChatGPT and API-key settings are mixed.",
                "Run veil setup codex with one authentication mode.",
            )
        )
    elif (
        provider.get("requires_openai_auth") is not True
        and provider.get("env_key") != "OPENAI_API_KEY"
    ):
        checks.append(
            Check(
                "authentication",
                "fail",
                "The API-key environment setting is missing.",
                "Use --auth api-key in veil setup codex.",
            )
        )
    checks.append(_gateway_check(provider))
    return checks, provider


def _doctor_checks(
    config: Path, directory: Path, provider: dict[str, Any]
) -> list[Check]:
    checks: list[Check] = []
    if not directory.exists():
        checks.append(
            Check(
                "storage",
                "fail",
                "The selected Veil data folder does not exist.",
                "Select the --data-dir used by your gateway, or run setup with it.",
            )
        )
    elif (
        directory.is_symlink()
        or not directory.is_dir()
        or directory.stat().st_mode & 0o077
    ):
        checks.append(
            Check(
                "storage",
                "fail",
                "Veil data storage is not a private directory.",
                "Use an owner-only directory (chmod 700 on Unix).",
            )
        )
    else:
        files = [
            directory / name
            for name in ("config.json", "gateway-secret", "vault.db", "ledger.db")
        ]
        files.append(config)
        unsafe = [
            path.name
            for path in files
            if path.is_symlink()
            or (path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077)
        ]
        checks.append(
            Check(
                "storage",
                "warn" if unsafe else "pass",
                "Review permissions/symlinks for: " + ", ".join(unsafe)
                if unsafe
                else "Selected storage and configuration have private permissions.",
                "Use chmod 600 for regular files on Unix; review symlinks separately."
                if unsafe
                else "",
            )
        )
    try:
        settings = load_settings(directory / "config.json")
        shield = Shield(custom_patterns=settings.patterns, redact_warnings=True)
        for kind, values in settings.entities.items():
            for value in values:
                shield.add_entity(value, kind)
        source = "Email veil.check@example.com."
        masked = shield.mask(source)
        restored = shield.restore(masked.text)
        passed = (
            not masked.warnings
            and not restored.warnings
            and "veil.check@example.com" not in masked.text
            and restored.text == source
        )
        checks.append(
            Check(
                "local_round_trip",
                "pass" if passed else "fail",
                "Fictional email masked and restored locally."
                if passed
                else "Local email masking/restoration did not pass.",
                "Review detector overrides in config.json." if not passed else "",
            )
        )
    except (SettingsError, ValueError):
        checks.append(
            Check(
                "local_round_trip",
                "fail",
                "Veil's detector configuration could not be loaded.",
                "Check config.json. Its values are omitted from diagnostics.",
            )
        )
    secret_path = directory / "gateway-secret"
    try:
        secret = read_private_file(secret_path).strip()
        headers = provider.get("http_headers", {})
        same = isinstance(headers, dict) and secret == headers.get(SECRET_HEADER)
        checks.append(
            Check(
                "data_folder",
                "pass" if same else "fail",
                "Data folder secret matches Codex's saved gateway."
                if same
                else "Data folder and Codex gateway secrets differ.",
                "Use the --data-dir used during setup. Keep secrets private."
                if not same
                else "",
            )
        )
    except SettingsError:
        checks.append(
            Check(
                "data_folder",
                "fail",
                "No readable secret for this data folder.",
                "Select the gateway's --data-dir or run veil setup codex.",
            )
        )
    executable = shutil.which("codex")
    if executable is None:
        checks.append(
            Check(
                "codex_cli",
                "warn",
                "Codex CLI was not found on PATH.",
                "Install it for CLI use; desktop may use its own bundled runtime.",
            )
        )
    elif provider.get("requires_openai_auth") is True:
        try:
            result = subprocess.run(
                [executable, "login", "status"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            logged_in = (
                result.returncode == 0
                and "chatgpt" in (result.stdout + result.stderr).lower()
            )
            checks.append(
                Check(
                    "login",
                    "pass" if logged_in else "warn",
                    "Codex CLI reports ChatGPT sign-in."
                    if logged_in
                    else "Codex CLI ChatGPT sign-in was not confirmed.",
                    "Run codex login. Desktop may use a different environment."
                    if not logged_in
                    else "",
                )
            )
        except (OSError, subprocess.SubprocessError):
            checks.append(
                Check(
                    "login",
                    "warn",
                    "Codex login status could not be checked.",
                    "Run codex login status manually.",
                )
            )
    else:
        present = bool(os.environ.get("OPENAI_API_KEY"))
        checks.append(
            Check(
                "api_key",
                "pass" if present else "warn",
                "OPENAI_API_KEY is present in this shell; its value was not tested."
                if present
                else "OPENAI_API_KEY is absent from this shell.",
                "Ensure Codex has the key; this shell does not prove desktop state.",
            )
        )
    return checks


def run_diagnostics(
    *,
    config: Path | None = None,
    data_dir: Path | None = None,
    doctor: bool = False,
    json_output: bool = False,
) -> int:
    """Print readiness plus explicit limits, without printing credentials or PII."""
    target = config_path(config)
    checks, provider = inspect_configuration(target)
    directory = data_dir
    receipt = None
    try:
        receipt = read_receipt(target)
        if directory is None and receipt is not None:
            directory = Path(receipt["data_dir"])
    except SettingsError:
        checks.append(
            Check(
                "rollback",
                "warn",
                "The Veil setup receipt needs repair.",
                "Keep the private backup; setup/undo will refuse an invalid receipt.",
            )
        )
    directory = directory or default_data_dir()
    if doctor:
        checks.extend(_doctor_checks(target, directory, provider))
    ready = all(check.state == "pass" for check in checks)
    endpoint = _local_endpoint(provider)
    command = None
    if endpoint and (data_dir is not None or receipt is not None):
        auth = "chatgpt" if provider.get("requires_openai_auth") is True else "api-key"
        command = shlex.join(
            [
                "veil",
                "--data-dir",
                str(directory),
                "gateway",
                "--api",
                "openai",
                "--auth",
                auth,
                "--port",
                str(urlsplit(endpoint).port),
            ]
        )
    report = {
        "ready": ready,
        "config": str(target),
        "data_dir": str(directory),
        "gateway_url": endpoint,
        "gateway_command": command,
        "checks": [asdict(check) for check in checks],
        "scope": (
            "Saved user settings and local gateway only. "
            "Active task routing is unverified."
        ),
        "next_step": (
            "Restart Codex after changes and start a fresh local task. "
            "No model call was made."
        ),
    }
    if json_output:
        print(json.dumps(report, indent=2))
    else:
        print("Veil readiness: " + ("checks passed" if ready else "needs attention"))
        print(f"Codex settings: {target}")
        if doctor:
            print(f"Veil data folder: {directory}")
        for check in checks:
            print(f"  [{check.state.upper()}] {check.name}: {check.detail}")
            if check.remedy:
                print(f"    {check.remedy}")
        if command and any(
            check.name == "gateway" and check.state != "pass" for check in checks
        ):
            print(f"Gateway start/restart command:\n  {command}")
        print(report["scope"])
        print(report["next_step"])
    return 0 if ready else 1
