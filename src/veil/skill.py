"""Install the bundled assistant skill without replacing user-owned skills."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from importlib.resources import files
from pathlib import Path

from .gateway import SettingsError

_RECEIPT = ".veil-skill.json"
_FILES = {"SKILL.md", "runtime.md"}


def skill_directory(client: str, skills_dir: Path | None = None) -> Path:
    """Return the personal skill location, or a selected skills directory."""
    if skills_dir is not None:
        return skills_dir.expanduser().absolute() / "veil"
    if client == "codex":
        return Path.home() / ".agents" / "skills" / "veil"
    if client == "claude":
        return Path.home() / ".claude" / "skills" / "veil"
    raise SettingsError("choose codex, claude, or all")


def _bundle() -> dict[str, bytes]:
    argv = [sys.executable, "-I", "-m", "veil"]
    runtime = (
        "# Installed Veil runtime\n\n"
        "Use this CLI argument array (JSON) with the host's shell quoting:\n\n"
        f"```json\n{json.dumps(argv)}\n```\n\n"
        "Equivalent POSIX shell command:\n\n"
        f"```sh\n{shlex.join(argv)}\n```\n\n"
        "Append Veil arguments to this command. It uses the Python environment "
        "that installed this skill, even when veil is not on PATH. Keep that "
        "environment available; reinstall the skill after moving or replacing "
        "it. If it is missing, report the failure instead of selecting an "
        "unrelated executable. No client routing or detector configuration "
        "is stored here.\n"
    )
    return {
        "SKILL.md": files("veil").joinpath("skills/veil/SKILL.md").read_bytes(),
        "runtime.md": runtime.encode("utf-8"),
    }


def _owned(path: Path) -> bool:
    """Recognize an unchanged installation; refuse unknown or modified content."""
    if not path.exists() and not path.is_symlink():
        return False
    message = (
        f"refusing to change {path}: existing skill is unrecognized or modified; "
        "preserve it and move it aside before retrying"
    )
    if path.is_symlink() or not path.is_dir():
        raise SettingsError(message)
    if {item.name for item in path.iterdir()} != _FILES | {_RECEIPT}:
        raise SettingsError(message)
    for name in _FILES | {_RECEIPT}:
        item = path / name
        if item.is_symlink() or not item.is_file() or item.stat().st_size > 100_000:
            raise SettingsError(message)
    try:
        receipt = json.loads((path / _RECEIPT).read_bytes())
    except (ValueError, UnicodeError):
        raise SettingsError(message) from None
    expected = {
        "format": 1,
        "files": {
            name: hashlib.sha256((path / name).read_bytes()).hexdigest()
            for name in _FILES
        },
    }
    if receipt != expected:
        raise SettingsError(message)
    return True


def _remove(path: Path) -> None:
    for name in _FILES | {_RECEIPT}:
        (path / name).unlink(missing_ok=True)
    path.rmdir()


@contextmanager
def _lock(parent: Path) -> Iterator[None]:
    path = parent / ".veil-skill.lock"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SettingsError(
            f"skill installation is locked at {path}; if an earlier operation "
            "was interrupted, confirm it has stopped before removing the lock"
        ) from None
    try:
        os.close(fd)
        yield
    finally:
        path.unlink()


def _install(path: Path, bundle: dict[str, bytes], exists: bool) -> None:
    staged = Path(tempfile.mkdtemp(prefix=".veil-skill-new-", dir=path.parent))
    previous: Path | None = None
    try:
        for name, content in bundle.items():
            (staged / name).write_bytes(content)
        (staged / _RECEIPT).write_text(
            json.dumps(
                {
                    "format": 1,
                    "files": {
                        name: hashlib.sha256(content).hexdigest()
                        for name, content in bundle.items()
                    },
                }
            ),
            encoding="utf-8",
        )
        if exists:
            previous = Path(
                tempfile.mkdtemp(prefix=".veil-skill-previous-", dir=path.parent)
            )
            previous.rmdir()
            path.rename(previous)
        try:
            staged.rename(path)
        except OSError:
            if previous is not None:
                previous.rename(path)
            raise
        if previous is not None:
            _remove(previous)
    finally:
        if staged.exists():
            _remove(staged)


def run_skill(
    operation: str, client: str = "all", *, skills_dir: Path | None = None
) -> int:
    """Install/update or remove skills, keeping client settings and data intact."""
    if operation not in {"install", "uninstall"}:
        raise SettingsError("choose install or uninstall")
    if client == "all" and skills_dir is not None:
        raise SettingsError("--skills-dir requires one client: codex or claude")
    clients = ("codex", "claude") if client == "all" else (client,)
    targets = [skill_directory(name, skills_dir) for name in clients]
    # Catch collisions in either client before changing the other one.
    for target in targets:
        _owned(target)
    bundle = _bundle() if operation == "install" else {}
    with ExitStack() as stack:
        for target in targets:
            if operation == "install":
                target.parent.mkdir(parents=True, exist_ok=True)
            if target.parent.exists():
                stack.enter_context(_lock(target.parent))
        for name, target in zip(clients, targets, strict=True):
            exists = _owned(target)
            if operation == "install":
                if exists and all(
                    (target / key).read_bytes() == value
                    for key, value in bundle.items()
                ):
                    action = "Already installed"
                else:
                    _install(target, bundle, exists)
                    action = "Installed"
                invocation = (
                    "$veil (or the skills picker)" if name == "codex" else "/veil"
                )
                print(f"{action}: {target}\nInvoke with {invocation}.")
            else:
                if exists:
                    _remove(target)
                print(f"Removed: {target}" if exists else f"Not installed: {target}")
    if operation == "install":
        print(
            "Start a new client session if the skill is not listed. "
            "Installing a skill does not enable masking or change client routing."
        )
    return 0
