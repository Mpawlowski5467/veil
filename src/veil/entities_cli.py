"""Register exact private values without putting them in command arguments."""

from __future__ import annotations

import getpass
import json
import os
import stat
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Literal

from .gateway.config import SettingsError, parse_settings, prepare_data_dir
from .placeholders import validate_entity_type

_MAX_CONFIG = 4 * 1024 * 1024
_MAX_VALUE = 16 * 1024


def _check_type(entity_type: str) -> None:
    try:
        validate_entity_type(entity_type)
        if entity_type == "LITERAL":
            raise ValueError
    except ValueError:
        raise SettingsError(
            "use a type such as PERSON or ORGANIZATION; LITERAL is reserved"
        ) from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _input_value(stdin: bool) -> str:
    if stdin:
        # A single record, not a bulk import. Bound reads before parsing, and
        # remove only one terminal line ending so internal newlines are refused.
        value = sys.stdin.read(_MAX_VALUE + 3)
        value = value.removesuffix("\n").removesuffix("\r")
    else:
        if not sys.stdin.isatty():
            raise SettingsError("use a local terminal prompt or --stdin")
        try:
            with warnings.catch_warnings():
                # getpass otherwise falls back to echoing input when it cannot
                # disable echo. Refuse that fallback before it reads anything.
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass("Private value (hidden): ")
        except getpass.GetPassWarning:
            raise SettingsError(
                "cannot hide input; use --stdin from a private file"
            ) from None
    if not value.strip() or len(value) > _MAX_VALUE:
        raise SettingsError("provide one non-blank value of at most 16384 characters")
    value.encode("utf-8")  # Reject undecodable bytes represented by surrogateescape.
    if any(
        ord(ch) < 32 or 127 <= ord(ch) <= 159 or ch in "\u2028\u2029" for ch in value
    ):
        raise SettingsError("the value must be one line without control characters")
    return value


def _read(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise SettingsError("config.json must be a regular file, not a link")
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            or opened.st_nlink != 1
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
        ):
            raise SettingsError("config.json must be owned by you and have no links")
        if opened.st_size > _MAX_CONFIG:
            raise SettingsError("config.json is too large to edit (maximum 4 MiB)")
        raw = stream.read(_MAX_CONFIG + 1)
    if len(raw) > _MAX_CONFIG:
        raise SettingsError("config.json is too large to edit (maximum 4 MiB)")
    try:
        text = raw.decode("utf-8")
        # Do not silently discard duplicate fields when rewriting the JSON.
        json.loads(text, object_pairs_hook=_unique_object)
        parse_settings(text, path)
    except (ValueError, RecursionError):
        raise SettingsError(
            "config.json is invalid; fix it before managing entities"
        ) from None
    return text


def _write(path: Path, text: str, original: str | None) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".veil-config-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)  # NamedTemporaryFile creates mode 0600.
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if _read(path) != original:
            raise SettingsError("config.json changed during the edit; try again")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _edit(data_dir: Path, operation: str, entity_type: str, value: str) -> bool:
    prepare_data_dir(data_dir)
    path = data_dir / "config.json"
    lock = data_dir / ".veil-entities.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SettingsError(
            "an entity edit is already running; if interrupted, remove "
            ".veil-entities.lock only after confirming no edit is running"
        ) from None
    try:
        os.close(fd)
        original = _read(path)
        raw = json.loads(original) if original is not None else {}
        entities = raw.setdefault("entities", {})
        values = entities.get(entity_type, [])
        if operation == "add":
            if any(
                value in items
                for kind, items in entities.items()
                if kind != entity_type
            ):
                raise SettingsError(
                    "this value is registered under another type; remove it there first"
                )
            if value in values:
                return False
            entities[entity_type] = [*values, value]
        else:
            if value not in values:
                return False
            remaining = [item for item in values if item != value]
            if remaining:
                entities[entity_type] = remaining
            else:
                del entities[entity_type]
        text = json.dumps(raw, indent=2, ensure_ascii=True) + "\n"
        if len(text.encode("utf-8")) > _MAX_CONFIG:
            raise SettingsError("the updated config.json would exceed 4 MiB")
        parse_settings(text, path)
        _write(path, text, original)
        return True
    finally:
        lock.unlink()


def run_entities(
    operation: Literal["add", "remove", "list"],
    *,
    data_dir: Path,
    entity_type: str | None,
    stdin: bool = False,
    json_output: bool = False,
) -> int:
    """Manage private registrations; reports never contain registered values."""
    if entity_type is not None:
        _check_type(entity_type)
    if operation == "list":
        # Listing an absent configuration does not create a directory or file.
        if data_dir.exists() or data_dir.is_symlink():
            prepare_data_dir(data_dir)
            text = _read(data_dir / "config.json")
        else:
            text = None
        settings = parse_settings(text or "{}", data_dir / "config.json")
        counts = {
            kind: len(set(values))
            for kind, values in sorted(settings.entities.items())
            if entity_type is None or kind == entity_type
        }
        if entity_type is not None:
            counts.setdefault(entity_type, 0)
        if json_output:
            print(json.dumps({"entities": counts, "total": sum(counts.values())}))
        elif counts:
            for kind, count in counts.items():
                print(f"{kind}: {count} registered")
        else:
            print("No registered values.")
        return 0
    assert entity_type is not None
    try:
        value = _input_value(stdin)
    except UnicodeError:
        raise SettingsError("private input could not be decoded as text") from None
    except (EOFError, KeyboardInterrupt):
        print("veil: registration cancelled; no changes made", file=sys.stderr)
        return 2
    if operation == "remove" and not data_dir.exists() and not data_dir.is_symlink():
        changed = False
    else:
        changed = _edit(data_dir, operation, entity_type, value)
    if not changed:
        print(
            "Already registered." if operation == "add" else "No matching registration."
        )
    else:
        print(
            "Registered one value."
            if operation == "add"
            else "Removed one registration."
        )
        print("Restart running gateways or relaunch Veil clients to apply the change.")
        if operation == "remove":
            print(
                "Existing conversation mappings are retained; "
                "this does not erase history."
            )
    return 0
