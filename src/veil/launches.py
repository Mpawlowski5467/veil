"""Owner-only records that let local commands find a launcher's gateway.

A launcher's gateway listens on a free port with a random secret that only its
client is given. While the client runs, the launcher keeps a record of that
port and secret in the data folder's ``launches`` folder, readable by its
owner only, so review and verification commands started from another terminal
can reach the gateway. A record is trusted only after the gateway at its port
proves it holds the secret, and removed when the launch exits.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import _windows
from .gateway import SettingsError, default_data_dir, prepare_data_dir
from .gateway.config import APP
from .gateway.hooks import _gateway_answers

#: The data folder's subfolder that holds one record per running launch.
FOLDER = "launches"

#: The clients a launcher runs.
CLIENTS = ("claude", "codex")

_NAME = re.compile(r"[1-9][0-9]{0,4}\.json")
_KEYS = {"version", "client", "port", "secret", "started"}


@dataclass(frozen=True)
class Launch:
    """A running launcher's gateway, as its record says; never show the secret.

    Attributes:
        client: The client the launcher runs, ``claude`` or ``codex``.
        port: The gateway's local port.
        secret: The value clients must send in the gateway's secret header.
        started: When the launch began, in Unix seconds.
        path: The record's file.
    """

    client: str
    port: int
    secret: str = field(repr=False)
    started: int
    path: Path = field(repr=False, compare=False)

    @property
    def url(self) -> str:
        """The gateway's base URL."""
        return f"http://127.0.0.1:{self.port}"

    def describe(self) -> str:
        """A line naming the gateway for a choice between several; no secret."""
        started = time.strftime("%H:%M", time.localtime(self.started))
        return f"{self.url} ({self.client} launch, started {started})"


@contextmanager
def published(
    data_dir: Path, client: str, url: str, secret: str
) -> Iterator[Path | None]:
    """Keep a record of a launcher's gateway while the body runs.

    The record is removed afterwards, with the ``launches`` folder once it is
    empty and the data folder if this call created it. If the record can't be
    written, the launch goes on without it after one line on stderr, which
    names neither the folder nor the secret; the context then gives None.
    """
    created = not data_dir.exists()
    path: Path | None = None
    for attempt in range(2):
        try:
            path = _write(data_dir, client, url, secret)
            break
        except FileNotFoundError:
            # Another launch removed the empty folder just now: once more.
            if attempt:
                break
        except (OSError, SettingsError):
            break
    if path is None:
        print(
            f"{APP}: other terminals can't find this launch, so review and "
            "verification must name its gateway explicitly",
            file=sys.stderr,
        )
    try:
        yield path
    finally:
        if path is not None:
            with suppress(OSError):
                path.unlink(missing_ok=True)
        with suppress(OSError):
            (data_dir / FOLDER).rmdir()
        if created:
            with suppress(OSError):
                data_dir.rmdir()


def _write(data_dir: Path, client: str, url: str, secret: str) -> Path:
    folder = prepare_data_dir(prepare_data_dir(data_dir) / FOLDER)
    port = urlsplit(url).port
    path = folder / f"{port}.json"
    body = {
        "version": 1,
        "client": client,
        "port": port,
        "secret": secret,
        "started": int(time.time()),
    }
    # A new temporary file is owner-only (and inherits the folder's private
    # Windows ACL); replacing puts the whole record in place at once.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=folder, prefix=".", suffix=".tmp", delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(json.dumps(body))
            file.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    if _windows.is_windows():
        try:
            _windows.check_private(path)
        except OSError:
            path.unlink(missing_ok=True)
            raise
    return path


def _private(path: Path) -> bool:
    """Whether only the owner (and, on Windows, privileged accounts) can use it."""
    if _windows.is_windows():
        try:
            _windows.check_private(path)
        except OSError:
            return False
        return True
    try:
        return stat.S_IMODE(path.lstat().st_mode) & 0o077 == 0
    except OSError:
        return False


def _folder(data_dir: Path) -> Path | None:
    """The records folder, if it is a private folder; never creates it."""
    folder = data_dir / FOLDER
    try:
        info = folder.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return None
    return folder if _private(folder) else None


def _read(path: Path) -> Launch | None:
    """The record at ``path``, or None unless it is exactly valid."""
    from .codex_setup import read_private_file

    if not _NAME.fullmatch(path.name):
        return None
    try:
        text = read_private_file(path)
        if not _private(path):
            return None
        data = json.loads(text)
    except (SettingsError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != _KEYS:
        return None
    version, client, port = data["version"], data["client"], data["port"]
    secret, started = data["secret"], data["started"]
    if (
        type(version) is not int
        or version != 1
        or client not in CLIENTS
        or type(port) is not int
        or port != int(path.stem)
        or not 1 <= port <= 65535
        or not isinstance(secret, str)
        or not secret
        or len(secret) > 256
        or "\r" in secret
        or "\n" in secret
        or type(started) is not int
    ):
        return None
    return Launch(client, port, secret, started, path)


def records(data_dir: Path) -> list[Launch]:
    """The valid records in the data folder, live or not; never raises."""
    folder = _folder(data_dir)
    if folder is None:
        return []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    return [launch for entry in entries if (launch := _read(entry)) is not None]


def find_launch(data_dir: Path, port: int) -> Launch | None:
    """The valid record for a gateway port, live or not."""
    folder = _folder(data_dir)
    return None if folder is None else _read(folder / f"{port}.json")


def _refused(port: int) -> bool:
    """Whether nothing listens on the local port any more."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=3).close()
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
    return False


def running_launches(data_dir: Path) -> list[Launch]:
    """The launches whose gateways prove they hold the recorded secret.

    The proof sends only a nonce, never the secret. A record whose port
    refuses connections belongs to a stopped launch and is removed; one whose
    port answers without the proof is ignored but kept, since the gateway may
    be busy.
    """
    live: list[Launch] = []
    for launch in records(data_dir):
        if _gateway_answers(launch.url, launch.secret):
            live.append(launch)
        elif _refused(launch.port) and _read(launch.path) == launch:
            with suppress(OSError):
                launch.path.unlink(missing_ok=True)
    return sorted(live, key=lambda launch: launch.started)


def review_command(url: str, data_dir: Path) -> str:
    """The command that opens review for the gateway at ``url``.

    ``--data-dir`` is named only for a folder other than the default.
    """
    words = [APP]
    if data_dir.absolute() != default_data_dir().absolute():
        words += ["--data-dir", str(data_dir.absolute())]
    words += ["review", "--gateway-url", url]
    return subprocess.list2cmdline(words) if os.name == "nt" else shlex.join(words)
