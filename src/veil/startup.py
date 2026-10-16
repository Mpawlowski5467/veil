"""Opt-in user login services and a supervisor for the existing gateway worker."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import service
from .gateway.config import SettingsError, prepare_data_dir
from .runtime import command

_RECEIPT = "startup.json"


def isolated_command(directory: Path, *arguments: str) -> list[str]:
    """Pin the interpreter and package instead of trusting a login shell's PATH."""
    return command("cli", "--data-dir", str(directory), *arguments)


def _identity(directory: Path) -> str:
    digest = hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    return f"org.veil.gateway.{digest}"


def _target(directory: Path) -> Path:
    name = _identity(directory)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{name}.plist"
    if sys.platform.startswith("linux"):
        return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"
    raise SettingsError("login startup currently supports macOS and Linux")


def _unit_quote(argument: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in argument):
        raise SettingsError("startup paths must not contain control characters")
    escaped = argument.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("%", "%%").replace("$", "$$") + '"'


def descriptor(directory: Path, options: dict[str, Any]) -> bytes:
    """Render an OS service without writing files or registering anything."""
    command = isolated_command(
        directory,
        "supervise",
        "--port",
        str(options["port"]),
        "--api",
        options["api"],
        "--auth",
        options["auth"],
        "--cwd",
        options["cwd"],
    )
    if sys.platform == "darwin":
        return plistlib.dumps(
            {
                "Label": _identity(directory),
                "ProgramArguments": command,
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 10,
                "ProcessType": "Background",
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            }
        )
    if not sys.platform.startswith("linux"):
        raise SettingsError("login startup currently supports macOS and Linux")
    return (
        "[Unit]\nDescription=Veil local gateway supervisor\n\n"
        "[Service]\nType=simple\nExecStart="
        + " ".join(_unit_quote(arg) for arg in command)
        + "\nRestart=always\nRestartSec=10\nKillMode=process\n"
        "StandardOutput=null\nStandardError=null\n\n"
        "[Install]\nWantedBy=default.target\n"
    ).encode("utf-8")


def _manager(arguments: list[str], *, check: bool = True) -> bool:
    try:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        if check:
            raise SettingsError(
                "could not contact the user login-service manager"
            ) from None
        return False
    if check and result.returncode:
        raise SettingsError(
            "the user login-service manager rejected the change; "
            "registration files were retained for recovery with veil startup disable"
        )
    return result.returncode == 0


def _active(directory: Path) -> bool:
    if sys.platform == "darwin":
        return _manager(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{_identity(directory)}"],
            check=False,
        )
    return _manager(
        ["systemctl", "--user", "is-active", _target(directory).name], check=False
    )


def _checked_registration(directory: Path) -> tuple[Path, dict[str, Any] | None]:
    target = _target(directory)
    if target.parent.exists():
        _check_parents(target)
    receipt = service._read(directory / _RECEIPT)
    if receipt is not None:
        if receipt.get("target") != str(target) or not isinstance(
            receipt.get("sha256"), str
        ):
            raise SettingsError("startup registration receipt is invalid")
        try:
            # The same private-file checks used by lifecycle state refuse
            # symlinks, non-owner files, and unexpectedly large descriptors.
            fd = service._open_file(target)
        except FileNotFoundError:
            return target, receipt
        with os.fdopen(fd, "rb") as stream:
            content = stream.read()
        if hashlib.sha256(content).hexdigest() != receipt["sha256"]:
            raise SettingsError(
                "startup registration was edited; preserve it before removal"
            )
    elif target.exists() or target.is_symlink():
        raise SettingsError("startup target already exists without a Veil receipt")
    return target, receipt


def startup_status(directory: Path) -> dict[str, Any]:
    """Report registration separately from gateway readiness or task coverage."""
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        return {"state": "unsupported", "active": False}
    try:
        if not directory.exists():
            return {"state": "disabled", "active": False}
        service._directory(directory)
        target, receipt = _checked_registration(directory)
        desired = service._read(directory / service._DESIRED)
        return {
            "state": "enabled" if receipt and target.exists() else "disabled",
            "active": _active(directory) if receipt else False,
            "gateway_requested": desired is None or desired.get("running") is True,
        }
    except (SettingsError, OSError):
        return {"state": "unknown", "active": False}


def _check_parents(target: Path) -> None:
    for parent in (target.parent, *target.parent.parents):
        if parent == Path.home().parent:
            break
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise SettingsError(
                "login-service folders must be user-owned without writable links"
            )


def _prepare_target(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _check_parents(target)


def run_startup(
    operation: str,
    *,
    data_dir: Path | None = None,
    config: Path | None = None,
    json_output: bool = False,
) -> int:
    """Register or remove only this data folder's user login service."""
    directory = service.service_directory(data_dir, config)
    if operation == "status":
        result = startup_status(directory)
        print(
            json.dumps(result)
            if json_output
            else "Veil login startup: " + result["state"]
        )
        return 0 if result["state"] in {"enabled", "disabled"} else 1
    service._supported()
    prepare_data_dir(directory)
    try:
        with service._lock(directory / service._COMMAND):
            target, receipt = _checked_registration(directory)
            if operation == "disable":
                # Pause first, so an unload failure cannot keep respawning it.
                service._write(directory / service._DESIRED, {"running": False})
                if receipt:
                    if sys.platform == "darwin":
                        if _active(directory):
                            _manager(
                                [
                                    "/bin/launchctl",
                                    "bootout",
                                    f"gui/{os.getuid()}/{_identity(directory)}",
                                ]
                            )
                    else:
                        _manager(
                            ["systemctl", "--user", "disable", "--now", target.name]
                        )
                    target.unlink(missing_ok=True)
                    if sys.platform.startswith("linux"):
                        _manager(["systemctl", "--user", "daemon-reload"])
                    (directory / _RECEIPT).unlink()
                service._stop_locked(directory)
                print("Login startup disabled; gateway stopped. Mappings kept.")
                return 0
            options = service._options(
                directory, service._state(directory), config, None, None, None, None
            )
            content = descriptor(directory, options)
            digest = hashlib.sha256(content).hexdigest()
            if receipt and receipt["sha256"] != digest:
                raise SettingsError(
                    "disable startup before changing its environment or options"
                )
            if not receipt or not target.exists():
                _prepare_target(target)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                service._write(
                    directory / _RECEIPT, {"target": str(target), "sha256": digest}
                )
            service._write(directory / service._DESIRED, {"running": True})
            if sys.platform == "darwin":
                if not _active(directory):
                    _manager(
                        [
                            "/bin/launchctl",
                            "bootstrap",
                            f"gui/{os.getuid()}",
                            str(target),
                        ]
                    )
            else:
                _manager(["systemctl", "--user", "daemon-reload"])
                _manager(["systemctl", "--user", "enable", "--now", target.name])
            print("Login startup enabled. Check veil status --service for readiness.")
    except service._BusyError:
        raise SettingsError(
            "another lifecycle command is running; retry after it finishes"
        ) from None
    return 0


def supervise(directory: Path, options: dict[str, Any]) -> int:
    """Recover crashed workers; honor explicit stops until the next start.

    The supervisor never signals a saved PID. Its kernel lease prevents two
    supervisors; existing lifecycle locks prevent duplicate gateway workers.
    """
    service._supported()
    prepare_data_dir(directory)
    stopped = threading.Event()
    previous: dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[signum] = signal.signal(signum, lambda *_: stopped.set())
    delay = 1.0
    stable_since = time.monotonic()
    try:
        with service._lock(directory / "supervisor.lock"):
            while not stopped.is_set():
                try:
                    with service._lock(directory / service._COMMAND):
                        desired = service._read(directory / service._DESIRED)
                        if (
                            desired is not None
                            and type(desired.get("running")) is not bool
                        ):
                            raise SettingsError("invalid desired worker state")
                        if (
                            desired is None or desired["running"]
                        ) and not service._running(directory):
                            # Explicit restarts can change saved ports/auth/cwd.
                            saved = service._state(directory)
                            selected = (
                                {key: saved[key] for key in options}
                                if saved
                                else options
                            )
                            service._options(
                                directory,
                                saved,
                                None,
                                selected["port"],
                                selected["api"],
                                selected["auth"],
                                Path(selected["cwd"]),
                            )
                            service._start_locked(directory, selected)
                            # Repeated short-lived workers must not cause a
                            # tight crash loop even when each startup succeeds.
                            delay = min(delay * 2, 60.0)
                            stable_since = time.monotonic()
                        elif time.monotonic() - stable_since >= 60:
                            delay = 1.0
                except service._BusyError:
                    pass
                except (SettingsError, OSError):
                    delay = min(delay * 2, 60.0)
                stopped.wait(delay)
    except service._BusyError:
        return 2
    finally:
        for saved_signum, handler in previous.items():
            signal.signal(saved_signum, handler)
    return 0
