"""Drive an interactive terminal program under GNU screen, for live tests.

The program runs detached in a screen session of its own, in a private
``SCREENDIR``, so the user's own screen sessions are never touched. Keys go
in with ``screen -X stuff`` and the screen is read with ``screen -X
hardcopy``. Checked with the screen macOS ships (4.00.03):

- ``stuff`` passes its argument through as it is (``^M`` is two characters),
  so keys are sent as real bytes, and an argument over about 1000 bytes is
  dropped without an error: text goes in pieces of at most 512 bytes.
- ``hardcopy`` writes each character past Latin-1 as its low byte (a
  pointer, U+276F, as ``o``). `fold` does the same to any text looked for on
  the screen.
- ``screen -X quit`` leaves a child that ignores SIGHUP running, so `close`
  ends the whole process tree first.

Standard library only.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

SCREEN = "/usr/bin/screen"
KEYS = {
    "enter": "\r",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "tab": "\t",
    "escape": "\x1b",
    "ctrl-c": "\x03",
}
#: The most UTF-8 bytes sent in one ``stuff``.
CHUNK = 512

# The program waits for the go file, so the window can be sized first; after
# it exits, its status is written and the window stays open (``read`` ends
# when the session quits, where ``sleep`` would be left running).
_WRAP = (
    'go=$1; status=$2; shift 2; while [ ! -e "$go" ]; do sleep 0.05; done; '
    '"$@"; echo $? > "$status"; read -r _'
)


def fold(text: str) -> str:
    r"""Text as screen's hardcopy writes it: past Latin-1, only the low byte.

    >>> fold("\u276f 1. Yes")
    'o 1. Yes'
    """
    return "".join(c if ord(c) < 256 else chr(ord(c) & 0xFF) for c in text)


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Split ``text`` into pieces of at most ``size`` UTF-8 bytes each.

    >>> chunks("abcé", 3)
    ['abc', 'é']
    """
    pieces: list[str] = []
    current: list[str] = []
    used = 0
    for char in text:
        n = len(char.encode("utf-8"))
        if used + n > size and current:
            pieces.append("".join(current))
            current, used = [], 0
        current.append(char)
        used += n
    if current:
        pieces.append("".join(current))
    return pieces


def decode(data: bytes) -> str:
    """A hardcopy's text: UTF-8 if it is (a newer screen), else Latin-1."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def descendants(pid: int) -> list[int]:
    """Every process under ``pid``, children first."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    todo = [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def end_processes(pids: Sequence[int], wait: float = 5.0) -> None:
    """Send TERM to each process, then KILL to any left after ``wait``."""
    for pid in pids:
        _signal(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and any(_alive(pid) for pid in pids):
        time.sleep(0.1)
    for pid in pids:
        _signal(pid, signal.SIGKILL)


def _signal(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class Terminal:
    """One program running in a detached screen window."""

    def __init__(
        self,
        folder: Path,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: Path,
        *,
        name: str = "live",
        width: int = 160,
        height: int = 50,
    ) -> None:
        """Set up (not start) ``argv`` to run in ``cwd`` with exactly ``env``.

        ``folder`` holds the screen's socket, the go and status files and
        the hardcopies.
        """
        self.folder = folder
        self.argv = list(argv)
        self.env = dict(env)
        self.cwd = cwd
        self.name = name
        self.width = width
        self.height = height
        self.screens = folder / "screens"
        self.go = folder / "go"
        self.status = folder / "status"
        self.pid: int | None = None
        self._copy = folder / "hardcopy.txt"

    def _screen(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        env = {
            "HOME": os.environ.get("HOME", "/"),
            "PATH": "/usr/bin:/bin",
            "SCREENDIR": str(self.screens),
        }
        return subprocess.run(
            [SCREEN, *args],
            env=env,
            cwd=self.cwd,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def start(self, timeout: float = 10.0) -> None:
        """Start the program in a window of the set size."""
        self.folder.mkdir(parents=True, exist_ok=True)
        self.screens.mkdir(mode=0o700, exist_ok=True)
        self.screens.chmod(0o700)  # screen refuses a folder others can use
        command = [
            "/usr/bin/env",
            "-i",
            *(f"{key}={value}" for key, value in self.env.items()),
            *self.argv,
        ]
        self._screen(
            "-U",
            "-dmS",
            self.name,
            "/bin/sh",
            "-c",
            _WRAP,
            "sh",
            str(self.go),
            str(self.status),
            *command,
        )
        deadline = time.monotonic() + timeout
        while self.pid is None:
            for socket in os.listdir(self.screens):
                pid, _, name = socket.partition(".")
                if name == self.name and pid.isdigit():
                    self.pid = int(pid)
            if self.pid is None:
                if time.monotonic() > deadline:
                    raise RuntimeError("screen didn't start")
                time.sleep(0.05)
        self._screen("-S", self.name, "-p", "0", "-X", "width", "-w", str(self.width))
        self._screen("-S", self.name, "-p", "0", "-X", "height", "-w", str(self.height))
        self.go.touch()

    def snapshot(self, timeout: float = 2.0) -> str:
        """The screen's text now, folded (see `fold`)."""
        self._copy.unlink(missing_ok=True)
        self._screen("-S", self.name, "-p", "0", "-X", "hardcopy", str(self._copy))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._copy.exists() and self._copy.stat().st_size:
                return fold(decode(self._copy.read_bytes()))
            time.sleep(0.05)
        return ""

    def type(self, text: str) -> None:
        """Type ``text`` (without Enter), in pieces screen passes on."""
        for piece in chunks(text):
            self._screen("-S", self.name, "-p", "0", "-X", "stuff", piece)
            time.sleep(0.05)

    def key(self, name: str) -> None:
        """Press one key of `KEYS`."""
        self._screen("-S", self.name, "-p", "0", "-X", "stuff", KEYS[name])

    def exited(self) -> int | None:
        """The program's exit status, or None while it runs."""
        try:
            text = self.status.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return int(text) if text.lstrip("-").isdigit() else -1

    def close(self) -> None:
        """End the program and everything it started, then the session."""
        if self.pid is not None:
            end_processes(descendants(self.pid))
        self._screen("-S", self.name, "-X", "quit")
        self._screen("-wipe")
        if self.pid is not None and _alive(self.pid):
            end_processes([self.pid])

    def __enter__(self) -> Terminal:
        """Start the program."""
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """End the program."""
        self.close()
