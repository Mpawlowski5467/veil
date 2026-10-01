"""The gateway's settings and data folder.

Settings come from one JSON file in the user's home folder, never from a
project: a repository someone cloned mustn't be able to change what gets
masked. The file is checked strictly; an unknown key or a value of the wrong
kind is an error that names the key, never the value.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import _windows
from ..detectors.regex import RegexDetector
from ..placeholders import validate_entity_type

#: The package's own name, used for its data folder (``~/.<name>``).
APP = __name__.split(".")[0]


class SettingsError(ValueError):
    """The settings file or data folder can't be used as it is."""


@dataclass(frozen=True)
class Settings:
    r"""What the gateway masks and how long it keeps mappings.

    Attributes:
        entities: Values to mask that patterns can't find, by type, e.g.
            ``{"PERSON": ("Jan Nowak",)}``.
        patterns: Extra regex patterns by type, e.g. ``{"ORDER": r"#\d{5}"}``.
        identity: Also mask the user's git name (and email).
        retention_days: Delete a conversation's mappings this many days after
            it was last used.
        note: Tell the model about placeholders in the system prompt.
        allow_mcp_tools: MCP tools that may receive real values.
        secret_review: Hold requests with uncertain secret candidates for local
            human review. Off by default; never calls a remote classifier.
    """

    entities: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    patterns: Mapping[str, str] = field(default_factory=dict)
    identity: bool = True
    retention_days: int = 30
    note: bool = True
    allow_mcp_tools: tuple[str, ...] = ()
    secret_review: bool = False


_KEYS = {
    "version",
    "entities",
    "patterns",
    "identity",
    "retention_days",
    "note",
    "allow_mcp_tools",
    "secret_review",
}


def default_data_dir() -> Path:
    """The data folder: ``.<package name>`` in the user's home folder."""
    return Path.home() / f".{APP}"


def load_settings(path: Path) -> Settings:
    """Read and check the settings file; a missing file means the defaults.

    Raises:
        SettingsError: If the file isn't valid JSON, has an unknown key, or a
            value of the wrong kind. The message names the file and key only.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    except OSError as error:
        raise SettingsError(f"{path}: can't be read ({error.strerror})") from None
    except UnicodeDecodeError:
        raise SettingsError(f"{path}: not UTF-8 text") from None
    return parse_settings(text, path)


def parse_settings(text: str, path: Path) -> Settings:
    """Validate a settings snapshot without rereading the file.

    The path is used only for diagnostics, which never include private values.
    """
    try:
        raw = json.loads(text)
    except ValueError:
        raise SettingsError(f"{path}: not valid JSON") from None
    if not isinstance(raw, dict):
        raise SettingsError(f"{path}: must be a JSON object")
    for key in raw:
        if key not in _KEYS and not key.startswith("$"):
            name = key if key.isidentifier() and len(key) <= 64 else "<key>"
            raise SettingsError(f"{path}: unknown key {name!r}")

    def fail(key: str, problem: str) -> SettingsError:
        return SettingsError(f"{path}: {key}: {problem}")

    if raw.get("version", 1) != 1:
        raise fail("version", "must be 1")
    entities: dict[str, tuple[str, ...]] = {}
    for entity_type, values in _mapping(raw, "entities", fail).items():
        _valid_type(entity_type, "entities", fail)
        if not isinstance(values, list) or not all(
            isinstance(v, str) and v.strip() for v in values
        ):
            raise fail(f"entities.{entity_type}", "must be a list of non-blank strings")
        if any("\n" in v or "\r" in v for v in values):
            # A file listing numbers its lines, so a value split over lines
            # would never be found there.
            raise fail(f"entities.{entity_type}", "values must be on one line")
        entities[entity_type] = tuple(values)
    patterns: dict[str, str] = {}
    for entity_type, pattern in _mapping(raw, "patterns", fail).items():
        _valid_type(entity_type, "patterns", fail)
        if not isinstance(pattern, str) or not pattern:
            raise fail(f"patterns.{entity_type}", "must be a non-empty string")
        try:
            RegexDetector({entity_type: pattern})
        except ValueError:
            raise fail(f"patterns.{entity_type}", "isn't a valid regex") from None
        patterns[entity_type] = pattern
    identity = raw.get("identity", True)
    note = raw.get("note", True)
    review = raw.get("secret_review", False)
    for key, value in (
        ("identity", identity),
        ("note", note),
        ("secret_review", review),
    ):
        if not isinstance(value, bool):
            raise fail(key, "must be true or false")
    retention = raw.get("retention_days", 30)
    if isinstance(retention, bool) or not isinstance(retention, int):
        raise fail("retention_days", "must be a whole number")
    if not 1 <= retention <= 3650:
        raise fail("retention_days", "must be from 1 to 3650")
    tools = raw.get("allow_mcp_tools", [])
    if not isinstance(tools, list) or not all(
        isinstance(t, str) and t.startswith("mcp__") for t in tools
    ):
        raise fail(
            "allow_mcp_tools", "must be a list of tool names like mcp__server__tool"
        )
    return Settings(
        entities=entities,
        patterns=patterns,
        identity=identity,
        retention_days=retention,
        note=note,
        allow_mcp_tools=tuple(tools),
        secret_review=review,
    )


def _mapping(raw: dict[str, Any], key: str, fail: Any) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise fail(key, "must be a JSON object")
    return value


def _valid_type(entity_type: str, key: str, fail: Any) -> None:
    try:
        validate_entity_type(entity_type)
    except ValueError:
        name = (
            entity_type
            if entity_type.isidentifier() and len(entity_type) <= 64
            else "<key>"
        )
        raise fail(f"{key}.{name}", "isn't a valid type name (like PERSON)") from None
    if entity_type == "LITERAL":
        raise fail(f"{key}.LITERAL", "is reserved")


def prepare_data_dir(path: Path) -> Path:
    """Create the data folder if needed, and check that only its owner can use it.

    Raises:
        SettingsError: If the folder is a symlink, isn't owned by this user,
            or other users can read or write it.
    """
    if _windows.is_windows():
        try:
            _windows.create_private(path, directory=True)
            if not path.is_dir():
                raise OSError("not a directory")
        except OSError:
            raise SettingsError(
                f"{path}: needs a private Windows folder owned by you; "
                "allow access only to your account, SYSTEM, and Administrators"
            ) from None
        return path
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(path)
    except OSError as error:
        raise SettingsError(f"{path}: can't be created ({error.strerror})") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SettingsError(f"{path}: must be a folder, not a link or file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise SettingsError(f"{path}: must be owned by you")
    if info.st_mode & 0o077:
        raise SettingsError(f"{path}: other users can use it; run chmod 700 on it")
    return path


def git_identity(cwd: Path | None = None) -> dict[str, str]:
    """Return the user's git name and email as ``{value: type}``, if set."""
    found: dict[str, str] = {}
    for key, entity_type in (("user.name", "PERSON"), ("user.email", "EMAIL")):
        try:
            completed = subprocess.run(
                ["git", "config", "--get", key],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        value = completed.stdout.strip()
        if completed.returncode == 0 and value:
            found[value] = entity_type
    return found
