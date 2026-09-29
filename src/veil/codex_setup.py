"""Backed-up, reversible edits to user-level Codex configuration."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .codex import Auth, provider_configuration
from .gateway.config import SettingsError, load_settings, prepare_data_dir

# These are the only settings setup owns; undo leaves every other setting alone.
OWNED = (
    ("model_provider",),
    ("web_search",),
    ("features", "apps"),
    ("features", "multi_agent"),
    ("analytics", "enabled"),
    ("model_providers", "veil"),
)
_MISSING = object()


def config_path(path: Path | None = None) -> Path:
    """Honor an explicit path or CODEX_HOME, otherwise use ~/.codex."""
    if path is not None:
        return path.expanduser().absolute()
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return home.expanduser().absolute() / "config.toml"


def parse_toml(text: str) -> Any:
    """Load the optional editor only for desktop setup and diagnostics."""
    try:
        import tomlkit
    except ImportError:
        raise SettingsError(
            "install the desktop extra: python -m pip install '.[desktop]' "
            "from the Veil checkout"
        ) from None
    try:
        return tomlkit.parse(text)
    except (ValueError, TypeError):
        # TOML parser errors can quote private values from the configuration.
        raise SettingsError("Codex configuration is not valid TOML") from None


def read_private_file(path: Path, *, missing: bool = False) -> str:
    """Read a regular, user-owned file without following its final symlink."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise SettingsError(f"{path}: must be a regular file, not a symlink")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise SettingsError(f"{path}: must be owned by you")
        if info.st_size > 4 * 1024 * 1024:
            raise SettingsError(f"{path}: file is too large for desktop setup")
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        if missing:
            return ""
        raise SettingsError(f"{path}: file is missing") from None
    except (OSError, UnicodeError):
        raise SettingsError(f"{path}: file could not be read") from None


def _write(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def receipt_path(config: Path) -> Path:
    """Keep setup state next to the selected configuration, independent of cwd."""
    return config.with_name(config.name + ".veil-setup.json")


@contextmanager
def _locked(config: Path) -> Iterator[None]:
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if config.parent.is_symlink():
        raise SettingsError("the Codex configuration folder must not be a symlink")
    info = config.parent.stat()
    if info.st_mode & 0o022 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise SettingsError(
            "the Codex configuration folder must be owned by you "
            "and not writable by others"
        )
    lock = config.with_name(config.name + ".veil-setup.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SettingsError(
            f"setup lock exists: {lock}; another setup/undo may be running. "
            "Remove it only after confirming that process has stopped"
        ) from None
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink()


def setting(document: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    """Look up a setting without confusing an absent value with false."""
    current: Any = document
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _put(document: Any, path: tuple[str, ...], value: Any) -> None:
    current = document
    for key in path[:-1]:
        if key not in current:
            current[key] = {}
        if not isinstance(current[key], Mapping):
            raise SettingsError(f"Codex {key} must be a TOML table")
        current = current[key]
    if value is _MISSING:
        current.pop(path[-1], None)
    else:
        current[path[-1]] = copy.deepcopy(value)


def read_receipt(config: Path) -> dict[str, Any] | None:
    """Read and validate the private rollback receipt without creating files."""
    path = receipt_path(config)
    raw = read_private_file(path, missing=True)
    if not raw:
        if path.exists():
            raise SettingsError(
                "Veil setup receipt is empty; preserve the private backup"
            )
        return None
    try:
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or value.get("config") != str(config)
            or not isinstance(value.get("installed"), str)
            or not isinstance(value.get("data_dir"), str)
            or not isinstance(value.get("existed"), bool)
            or not isinstance(value.get("backup"), str)
            or Path(value["backup"]).name != value["backup"]
            or not value["backup"].startswith(config.name + ".veil-backup-")
            or not isinstance(value.get("sha256"), str)
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise SettingsError(
            "Veil setup receipt is invalid; no settings changed"
        ) from None
    return value


def setup_codex(data_dir: Path, config: Path, port: int, auth: Auth) -> int:
    """Merge Veil settings with an atomic write and a private rollback backup."""
    from .cli import gateway_secret

    if not 1 <= port <= 65535:
        raise SettingsError("use a gateway port from 1 through 65535")
    config = config_path(config)
    with _locked(config):
        existed = config.exists()
        before = read_private_file(config, missing=True)
        document = parse_toml(before)
        if document.get("profile"):
            raise SettingsError(
                "Codex selects a profile; remove that selection before setup "
                "so it cannot override the Veil provider"
            )
        receipt = read_receipt(config)
        prepare_data_dir(data_dir)
        load_settings(data_dir / "config.json")
        secret_path = data_dir / "gateway-secret"
        if secret_path.exists() or secret_path.is_symlink():
            value = read_private_file(secret_path).strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{16,256}", value) is None:
                raise SettingsError(
                    "the gateway secret file is invalid; check the data folder"
                )
        secret = gateway_secret(data_dir)
        if not secret:
            raise SettingsError("the gateway secret is empty; restore its private file")
        desired = {
            "model_provider": "veil",
            "web_search": "disabled",
            "features": {"apps": False, "multi_agent": False},
            "analytics": {"enabled": False},
            "model_providers": {
                "veil": provider_configuration(f"http://127.0.0.1:{port}", secret, auth)
            },
        }
        if receipt is not None:
            installed = parse_toml(receipt["installed"])
            if any(setting(document, p) != setting(installed, p) for p in OWNED):
                raise SettingsError(
                    "settings managed by Veil changed after setup; review them "
                    "before undoing or reconfiguring"
                )
            if any(setting(desired, p) != setting(installed, p) for p in OWNED):
                raise SettingsError("undo the existing setup before changing port/auth")
            if receipt["data_dir"] != str(data_dir):
                raise SettingsError(
                    "undo the existing setup before changing data folders"
                )
            print("Veil is already configured. Existing rollback backup preserved.")
            return 0
        for path in OWNED:
            _put(document, path, setting(desired, path))
        after = document.as_string()
        checked = parse_toml(after)
        if any(setting(checked, p) != setting(desired, p) for p in OWNED):
            raise SettingsError("the merged configuration failed validation")
        backup = config.with_name(config.name + ".veil-backup-" + secrets.token_hex(8))
        # Exclusive backup creation and mode 0600, including on a permissive umask.
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(before)
            file.flush()
            os.fsync(file.fileno())
        state = {
            "version": 1,
            "config": str(config),
            "data_dir": str(data_dir),
            "existed": existed,
            "backup": backup.name,
            "sha256": hashlib.sha256(before.encode()).hexdigest(),
            "installed": after,
        }
        if read_private_file(config, missing=True) != before:
            raise SettingsError("Codex settings changed during setup; try again")
        _write(receipt_path(config), json.dumps(state))
        _write(config, after)
        print(f"Codex configured for Veil at 127.0.0.1:{port} ({auth}).")
        print(f"Private backup: {backup}")
        print("Web search, apps, subagents, and analytics disabled in these settings.")
        print("Start the background gateway (macOS/Linux):")
        print(
            f"  veil --data-dir {shlex.quote(str(data_dir))} start "
            f"--config {shlex.quote(str(config))}"
        )
        print("Or keep a foreground gateway running in a terminal:")
        print(
            f"  veil --data-dir {shlex.quote(str(data_dir))} gateway "
            f"--api openai --auth {auth} --port {port}"
        )
        print("Restart Codex and start a fresh local task.")
        print("Run `veil status` to check readiness; `veil undo codex` reverses setup.")
    return 0


def undo_codex(config: Path) -> int:
    """Restore managed settings while retaining unrelated edits made since setup."""
    config = config_path(config)
    with _locked(config):
        state = read_receipt(config)
        if state is None:
            raise SettingsError("no Veil setup receipt exists for this configuration")
        before = read_private_file(config.parent / state["backup"])
        if hashlib.sha256(before.encode()).hexdigest() != state["sha256"]:
            raise SettingsError("the rollback backup changed; no settings restored")
        original = parse_toml(before)
        installed = parse_toml(state["installed"])
        current_text = read_private_file(config, missing=True)
        current = parse_toml(current_text)
        for path in OWNED:
            now = setting(current, path)
            if now != setting(installed, path) and now != setting(original, path):
                raise SettingsError(
                    f"{'.'.join(path)} changed after setup; undo stopped "
                    "to preserve that edit. Review it against the private backup"
                )
        for path in OWNED:
            previous = setting(original, path)
            _put(current, path, previous)
            for length in range(len(path) - 1, 0, -1):
                parent = path[:length]
                if setting(original, parent) is _MISSING and not setting(
                    current, parent
                ):
                    _put(current, parent, _MISSING)
        after = (
            before
            if current_text in {state["installed"], before}
            else current.as_string()
        )
        parse_toml(after)
        if read_private_file(config, missing=True) != current_text:
            raise SettingsError("Codex settings changed during undo; try again")
        if not state["existed"] and not parse_toml(after):
            config.unlink(missing_ok=True)
        else:
            _write(config, after)
        receipt_path(config).unlink()
        print(
            "Veil's Codex settings were undone. Unrelated later edits were preserved."
        )
        print(f"Original backup retained: {config.parent / state['backup']}")
        print("Restart Codex for the restored settings to apply.")
    return 0
