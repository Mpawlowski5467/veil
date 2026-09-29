"""Manage a detached local gateway on macOS/Linux, without trusting saved PIDs.

A kernel lock lives as long as the worker does. Commands use a separate lock;
stop requests name one generation in a private file. No command sends a signal
to a PID read from disk. Lock files stay in place so concurrent commands always
lock the same inode, including after a crash or removal of saved state.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from . import __version__
from .codex_setup import config_path, parse_toml, read_private_file, read_receipt
from .gateway import (
    Gateway,
    SettingsError,
    default_data_dir,
    git_identity,
    load_settings,
    open_sessions,
    prepare_data_dir,
)
from .gateway.hooks import _gateway_answers

_STATE = "service.json"
_LEASE = "service-runtime.lock"
_COMMAND = "service-command.lock"
_STOP = "service-stop.json"
_TIMEOUT = 10.0
_ERRORS = {
    "port_in_use": "Gateway port already in use; no existing process was stopped.",
    "startup_failed": (
        "The background gateway could not start; check configuration and permissions."
    ),
    "worker_failed": "The background gateway stopped unexpectedly; run veil restart.",
    "startup_timeout": "Gateway startup timed out; the new worker was stopped.",
}


class _BusyError(Exception):
    pass


def _supported() -> None:
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise SettingsError(
            "background controls currently support macOS and Linux; "
            "use veil gateway in a terminal on this platform"
        )


def _directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise SettingsError("the service data folder must be private and owned by you")


def _open_file(path: Path, *, create: bool = False) -> int:
    flags = os.O_RDWR if create else os.O_RDONLY
    fd = os.open(
        path,
        flags | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0),
        0o600,
    )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            raise SettingsError(
                "service files must be small, private, user-owned files"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _lock(path: Path, *, create: bool = True) -> Iterator[int]:
    import fcntl

    fd = _open_file(path, create=create)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _BusyError from None
        yield fd
    finally:
        # Do not LOCK_UN: the worker inherits this open file description.
        os.close(fd)


def _running(directory: Path) -> bool:
    try:
        with _lock(directory / _LEASE, create=False):
            return False
    except FileNotFoundError:
        return False
    except _BusyError:
        return True


def _read(path: Path) -> dict[str, Any] | None:
    try:
        fd = _open_file(path)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as file:
            value = json.load(file)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise SettingsError(
            "service state is invalid; no process was signalled"
        ) from None


def _write(path: Path, value: dict[str, Any]) -> None:
    # Refuse unsafe existing files, including symlinks, before replacing them.
    _read(path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _state(directory: Path) -> dict[str, Any] | None:
    value = _read(directory / _STATE)
    if value is None:
        return None
    if (
        value.get("version") != 1
        or not isinstance(value.get("instance"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["instance"])
        or value.get("phase") not in ("starting", "running", "stopped", "failed")
        or type(value.get("pid")) is not int
        or value["pid"] < 0
        or type(value.get("port")) is not int
        or not 1 <= value["port"] <= 65535
        or value.get("api") not in ("openai", "anthropic")
        or value.get("auth") not in ("chatgpt", "api-key")
        or not isinstance(value.get("cwd"), str)
        or not Path(value["cwd"]).is_absolute()
        or not isinstance(value.get("veil_version"), str)
        or value.get("error") not in (None, *_ERRORS)
    ):
        raise SettingsError("service state is invalid; no process was signalled")
    return value


def service_directory(data_dir: Path | None, config: Path | None = None) -> Path:
    """Use an explicit folder, the Codex setup receipt, or the normal default."""
    if data_dir is not None:
        return data_dir.expanduser().absolute()
    receipt = read_receipt(config_path(config))
    return Path(receipt["data_dir"]).absolute() if receipt else default_data_dir()


def service_status(directory: Path) -> dict[str, Any]:
    """Inspect process state read-only; this does not prove client task routing."""
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        return {"state": "unsupported"}
    try:
        _directory(directory)
        value = _state(directory)
        running = _running(directory)
        if value is None:
            return {"state": "unknown" if running else "unmanaged"}
        phase = value["phase"]
        if running and phase not in {"running", "starting"}:
            phase = "stopping"
        elif not running and phase in {"running", "starting"}:
            phase = "crashed"
        if running and phase == "running":
            secret = read_private_file(directory / "gateway-secret").strip()
            if not _gateway_answers(f"http://127.0.0.1:{value['port']}", secret):
                phase = "unresponsive"
        result = {key: value[key] for key in ("port", "api", "auth", "veil_version")}
        result.update(state=phase, pid=value["pid"] if running else None)
        if value["error"]:
            result["detail"] = _ERRORS[value["error"]]
        return result
    except FileNotFoundError:
        return {"state": "unmanaged"}
    except (SettingsError, OSError):
        return {
            "state": "unknown",
            "detail": "Service files could not be checked safely.",
        }


def run_service_status(
    data_dir: Path | None, config: Path | None, json_output: bool
) -> int:
    """Report the worker independently of optional Codex configuration tools."""
    result = service_status(service_directory(data_dir, config))
    if json_output:
        print(
            json.dumps(
                {
                    "service": result,
                    "scope": (
                        "Background worker only. Active task routing is unverified."
                    ),
                },
                indent=2,
            )
        )
    else:
        print("Veil background gateway: " + result["state"])
        if "detail" in result:
            print(result["detail"])
        print("Active task routing is unverified.")
    return 0 if result["state"] == "running" else 1


def _secret(directory: Path) -> str:
    from .cli import gateway_secret

    path = directory / "gateway-secret"
    if path.exists() or path.is_symlink():
        secret = read_private_file(path).strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{16,256}", secret) is None:
            raise SettingsError(
                "invalid gateway secret; check the selected data folder"
            )
    return gateway_secret(directory)


def _options(
    directory: Path,
    previous: dict[str, Any] | None,
    config: Path | None,
    port: int | None,
    api: str | None,
    auth: str | None,
    cwd: Path | None,
) -> dict[str, Any]:
    defaults = previous or {
        "port": 8485,
        "api": "openai",
        "auth": "chatgpt",
        "cwd": str(Path.cwd()),
    }
    if api != "anthropic":
        target = config_path(config)
        receipt = read_receipt(target)
        if config is not None or (receipt and Path(receipt["data_dir"]) == directory):
            document = parse_toml(read_private_file(target)).unwrap()
            providers = document.get("model_providers", {})
            provider = providers.get("veil", {}) if isinstance(providers, dict) else {}
            try:
                endpoint = urlsplit(provider.get("base_url", ""))
                if (
                    endpoint.scheme != "http"
                    or endpoint.hostname != "127.0.0.1"
                    or not endpoint.port
                    or endpoint.path != "/v1"
                    or endpoint.username
                    or endpoint.password
                    or endpoint.query
                    or endpoint.fragment
                ):
                    raise ValueError
                defaults = {
                    **defaults,
                    "api": "openai",
                    "port": endpoint.port,
                    "auth": "chatgpt"
                    if provider.get("requires_openai_auth") is True
                    else "api-key",
                }
                if provider.get("http_headers", {}).get("x-gateway-secret") != _secret(
                    directory
                ):
                    raise SettingsError(
                        "Codex and this data folder use different gateway secrets"
                    )
            except SettingsError:
                raise
            except (ValueError, TypeError, AttributeError):
                raise SettingsError(
                    "Codex's Veil provider must use a loopback /v1 URL"
                ) from None
    selected_api = api or defaults["api"]
    selected_auth = auth or (
        "api-key" if selected_api == "anthropic" else defaults["auth"]
    )
    result: dict[str, Any] = {
        "port": port
        if port is not None
        else (8484 if api == "anthropic" and previous is None else defaults["port"]),
        "api": selected_api,
        "auth": selected_auth,
        "cwd": str(cwd.expanduser().absolute()) if cwd else defaults["cwd"],
    }
    if type(result["port"]) is not int or not 1 <= result["port"] <= 65535:
        raise SettingsError("use a gateway port from 1 through 65535")
    if selected_api not in {"openai", "anthropic"} or selected_auth not in {
        "chatgpt",
        "api-key",
    }:
        raise SettingsError("unsupported gateway API/auth mode")
    if selected_api == "anthropic" and selected_auth != "api-key":
        raise SettingsError("--auth chatgpt requires --api openai")
    if not Path(result["cwd"]).is_dir():
        raise SettingsError("the saved working folder is missing; select --cwd PATH")
    load_settings(directory / "config.json")
    _secret(directory)
    return result


def _stop_locked(directory: Path, *, remove: bool = False) -> None:
    state = _state(directory)
    if _running(directory):
        if state is None:
            raise SettingsError(
                "the worker is active but its state is missing; "
                "no process was signalled"
            )
        _write(directory / _STOP, {"instance": state["instance"]})
        deadline = time.monotonic() + _TIMEOUT
        while _running(directory):
            if time.monotonic() >= deadline:
                raise SettingsError(
                    "the worker has not stopped; its stop request remains pending. "
                    "No PID was killed"
                )
            time.sleep(0.05)
    if state is not None:
        state.update(phase="stopped", error=None)
        _write(directory / _STATE, state)
    _read(directory / _STOP)
    (directory / _STOP).unlink(missing_ok=True)
    if remove:
        (directory / _STATE).unlink(missing_ok=True)


def _start_locked(directory: Path, options: dict[str, Any]) -> bool:
    previous = _state(directory)
    if _running(directory):
        if (
            previous
            and all(previous[k] == v for k, v in options.items())
            and previous["phase"] == "running"
            and previous["veil_version"] == __version__
            and _gateway_answers(
                f"http://127.0.0.1:{options['port']}", _secret(directory)
            )
        ):
            return False
        raise SettingsError(
            "a managed worker is already active; "
            "use veil restart to update, change, or recover it"
        )
    with _lock(directory / _LEASE) as lease:
        _read(directory / _STOP)
        (directory / _STOP).unlink(missing_ok=True)
        instance = secrets.token_hex(16)
        state = {
            **options,
            "version": 1,
            "instance": instance,
            "phase": "starting",
            "pid": 0,
            "veil_version": __version__,
            "error": None,
        }
        _write(directory / _STATE, state)
        package = str(Path(__file__).resolve().parent.parent)
        code = (
            f"import sys; sys.path.insert(0, {package!r}); "
            "from veil.service import daemon_main; sys.exit(daemon_main())"
        )
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    code,
                    str(directory),
                    instance,
                    str(lease),
                ],
                cwd=options["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(lease,),
            )
        except OSError:
            state.update(phase="failed", error="startup_failed")
            _write(directory / _STATE, state)
            raise SettingsError(_ERRORS["startup_failed"]) from None
        # Reap while this CLI remains alive (e.g. a Python caller). The worker is
        # adopted by the OS when the CLI exits; no Popen object is abandoned.
        threading.Thread(target=process.wait, daemon=True).start()
        try:
            deadline = time.monotonic() + _TIMEOUT
            while process.poll() is None and time.monotonic() < deadline:
                current = _state(directory)
                if current and current["instance"] == instance:
                    if current["phase"] == "running" and _gateway_answers(
                        f"http://127.0.0.1:{options['port']}", _secret(directory)
                    ):
                        return True
                    if current["phase"] == "failed":
                        raise SettingsError(
                            _ERRORS[current["error"] or "startup_failed"]
                        )
                time.sleep(0.05)
            current = _state(directory)
            if process.poll() is None:
                state.update(phase="failed", error="startup_timeout")
                _write(directory / _STATE, state)
                raise SettingsError(_ERRORS["startup_timeout"])
            raise SettingsError(
                _ERRORS[(current or {}).get("error") or "startup_failed"]
            )
        except BaseException:
            # Only the newly launched child handle is eligible for signals.
            # Later stop/restart commands never use a saved PID for this.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if state["error"] == "startup_timeout":
                # A cooperative SIGTERM exit may have written "stopped".
                # Retain the actual reason this launch did not succeed.
                _write(directory / _STATE, state)
            raise


def run_service(
    operation: Literal["start", "stop", "restart"],
    *,
    data_dir: Path | None = None,
    config: Path | None = None,
    port: int | None = None,
    api: str | None = None,
    auth: str | None = None,
    cwd: Path | None = None,
    remove: bool = False,
) -> int:
    """Start/stop one worker per data folder; preserve client settings and mappings."""
    _supported()
    directory = service_directory(data_dir, config)
    if operation == "stop" and not directory.exists():
        print("Veil background gateway is already stopped.")
        return 0
    prepare_data_dir(directory)
    try:
        with _lock(directory / _COMMAND):
            if operation == "stop":
                _stop_locked(directory, remove=remove)
                print(
                    "Veil background gateway stopped."
                    + (" Saved service state removed." if remove else "")
                )
                return 0
            options = _options(
                directory, _state(directory), config, port, api, auth, cwd
            )
            if operation == "restart":
                _stop_locked(directory)
            started = _start_locked(directory, options)
            outcome = "started" if started else "already running"
            print(
                f"Veil background gateway {outcome} "
                f"at http://127.0.0.1:{options['port']} "
                f"({options['api']}, {options['auth']})."
            )
            status_command = ["veil", "--data-dir", str(directory), "status"]
            if config is not None:
                status_command.extend(["--config", str(config_path(config))])
            print(f"Readiness: {shlex.join(status_command)}")
            print("Active task routing remains unverified.")
    except _BusyError:
        raise SettingsError(
            "another lifecycle command is running; retry after it finishes"
        ) from None
    return 0


def daemon_main() -> int:
    """Private child entry point; the lease is inherited directly from its parent."""
    directory, instance, lease_text = sys.argv[1:]
    root = Path(directory)
    lease = int(lease_text)
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, lambda *_: stopped.set())
    state: dict[str, Any] | None = None
    try:
        _directory(root)
        state = _state(root)
        if state is None or state["instance"] != instance:
            return 2
        state["pid"] = os.getpid()
        settings = load_settings(root / "config.json")
        identity = git_identity(Path(state["cwd"])) if settings.identity else {}
        with (
            open_sessions(root, settings, identity, api=state["api"]) as sessions,
            Gateway(
                sessions,
                port=state["port"],
                secret=_secret(root),
                api=state["api"],
                openai_auth=state["auth"],
            ) as gateway,
        ):
            if state["api"] == "openai":
                from .codex import write_configuration

                write_configuration(gateway, root)
            state["phase"] = "running"
            _write(root / _STATE, state)
            while not stopped.wait(0.1):
                request = _read(root / _STOP)
                if request and request.get("instance") == instance:
                    break
        state.update(phase="stopped", error=None)
        _write(root / _STATE, state)
        return 0
    except Exception as error:
        if state is not None:
            reason = (
                "port_in_use"
                if isinstance(error, OSError) and error.errno == errno.EADDRINUSE
                else "startup_failed"
                if state["phase"] == "starting"
                else "worker_failed"
            )
            state.update(phase="failed", error=reason)
            with suppress(OSError, SettingsError):
                _write(root / _STATE, state)
        return 2
    finally:
        os.close(lease)
