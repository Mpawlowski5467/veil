"""Allowlisted, locally generated support reports with no request or path data."""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .diagnostics import collect_diagnostics
from .gateway.activity import ACTIVITY_PATH, VERIFY_PATH
from .gateway.config import SettingsError
from .verification import endpoint, local_request

_CHECKS = frozenset(
    {
        "configuration",
        "routing",
        "profile",
        "features",
        "transport",
        "authentication",
        "gateway",
        "storage",
        "local_round_trip",
        "data_folder",
        "codex_cli",
        "login",
        "api_key",
        "rollback",
    }
)
_STATES = frozenset({"pass", "warn", "fail"})
_WORKERS = frozenset(
    {"running", "stopped", "unmanaged", "unsupported", "unresponsive", "stale"}
)
_PROBES = frozenset(
    {"pending", "in_progress", "verified", "incomplete", "expired", "unknown"}
)
_VERSION = re.compile(
    r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}(?:(?:a|b|rc|\.dev)[0-9]{1,5}|-(?:alpha|beta|rc|dev)\.?[0-9]{1,5})?\Z"
)


def _version(value: str) -> str | None:
    return value if _VERSION.fullmatch(value) else None


def _client_version(client: str) -> str | None:
    executable = shutil.which(client)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 128:
            return None
        value = result.stdout.strip()
        if client == "codex":
            value = value.removeprefix("codex-cli ")
        else:
            value = value.removesuffix(" (Claude Code)")
        return _version(value)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def safe_checks(report: dict[str, Any]) -> list[dict[str, str]]:
    """Copy only known check names/states, never details, remedies, or new fields."""
    out = []
    checks = report.get("checks", [])
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        name, state = check.get("name"), check.get("state")
        if (
            isinstance(name, str)
            and name in _CHECKS
            and isinstance(state, str)
            and state in _STATES
        ):
            out.append({"code": name + "." + state, "state": state})
    return out


def support_report(
    *,
    client: str = "codex",
    config: Path | None = None,
    data_dir: Path | None = None,
    gateway_url: str | None = None,
    verification: str | None = None,
) -> dict[str, Any]:
    """Build a report from local checks; neither collect prompts nor create a probe."""
    if client not in {"codex", "claude"}:
        raise ValueError("unsupported client")
    system = platform.system()
    report: dict[str, Any] = {
        "schema": 1,
        "veil_version": _version(__version__),
        "python_version": ".".join(str(v) for v in sys.version_info[:3]),
        "platform": system if system in {"Darwin", "Linux", "Windows"} else "other",
        "client": {"name": client, "cli_version": _client_version(client)},
        "checks": [],
        "worker_state": "unknown",
        "gateway_state": "unavailable",
        "activity": None,
        "verification": {"state": "not_requested"},
        "errors": [],
        "scope": (
            "Local readiness and selected probe only; "
            "active task routing is unverified."
        ),
        "sharing": "Generated locally. Review this report before sharing it yourself.",
    }
    if client == "codex":
        try:
            local = collect_diagnostics(config=config, data_dir=data_dir, doctor=True)
            report["checks"] = safe_checks(local)
            state = local.get("service", {}).get("state")
            if isinstance(state, str) and state in _WORKERS:
                report["worker_state"] = state
        except (OSError, ValueError, SettingsError, subprocess.SubprocessError):
            report["errors"].append("diagnostics_unavailable")
    try:
        target = endpoint(config=config, data_dir=data_dir, gateway_url=gateway_url)
        activity = local_request(target, ACTIVITY_PATH)
        report["gateway_state"] = "reachable"
        totals = dict.fromkeys(("requests", "forwarded", "completed", "failed"), 0)
        sessions = activity.get("sessions", [])
        if isinstance(sessions, list):
            for session in sessions[:128]:
                if not isinstance(session, dict):
                    continue
                for name in totals:
                    count = session.get(name)
                    if type(count) is int and 0 <= count <= 1_000_000_000:
                        totals[name] += count
        report["activity"] = totals
        if verification is not None:
            if not re.fullmatch(r"[0-9a-f]{32}", verification):
                report["verification"] = {"state": "invalid"}
                report["errors"].append("verification_id_invalid")
            else:
                result = local_request(target, VERIFY_PATH + "?id=" + verification)
                state = result.get("state")
                report["verification"] = {
                    "state": state
                    if isinstance(state, str) and state in _PROBES
                    else "unknown"
                }
                for name in ("masked", "forwarded", "restored", "completed"):
                    if type(result.get(name)) is bool:
                        report["verification"][name] = result[name]
    except (OSError, ValueError, SettingsError):
        report["errors"].append("gateway_report_unavailable")
        if verification is not None:
            report["verification"] = {"state": "unavailable"}
    return report


def run_report(**options: Any) -> int:
    """Print a shareable JSON report, including safe failure codes when unhealthy."""
    report = support_report(**options)
    print(json.dumps(report, indent=2))
    passed = report["gateway_state"] == "reachable" and not report["errors"]
    return (
        0 if passed and not any(c["state"] == "fail" for c in report["checks"]) else 1
    )
