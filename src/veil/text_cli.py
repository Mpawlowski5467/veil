"""Explicit local mask/restore workflows for ChatGPT and other chat interfaces."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import MaskResult, RestoreResult
from .gateway import git_identity, load_settings, open_sessions, prepare_data_dir
from .gateway.config import SettingsError


def _clipboard_command(*, write: bool) -> list[str]:
    if sys.platform == "darwin":
        return ["/usr/bin/pbcopy" if write else "/usr/bin/pbpaste"]
    if sys.platform == "win32":
        script = (
            "$veilText = [Console]::In.ReadToEnd(); "
            "if ($veilText.Length -eq 0) { "
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.Clipboard]::Clear() "
            "} else { Set-Clipboard -Value $veilText }"
            if write
            else "[Console]::Write((Get-Clipboard -Raw))"
        )
        return [
            "powershell.exe",
            "-NoProfile",
            "-STA",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference = 'Stop'; "
            "[Console]::InputEncoding = [Console]::OutputEncoding = "
            "[System.Text.UTF8Encoding]::new(); " + script,
        ]
    wayland = shutil.which("wl-copy" if write else "wl-paste")
    if wayland:
        return [wayland] if write else [wayland, "--no-newline"]
    xclip = shutil.which("xclip")
    if xclip:
        return [xclip, "-selection", "clipboard", "-i" if write else "-o"]
    raise SettingsError(
        "clipboard access needs wl-copy/wl-paste or xclip; use stdin instead"
    )


def clipboard_read() -> str:
    """Read text only when the user explicitly selected --clipboard."""
    try:
        result = subprocess.run(
            _clipboard_command(write=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        )
        return result.stdout
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise SettingsError("could not read clipboard text") from None


def clipboard_write(text: str) -> None:
    """Replace clipboard text after a successful transformation."""
    try:
        subprocess.run(
            _clipboard_command(write=True),
            input=text,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise SettingsError("could not write clipboard text") from None


def run_text(
    operation: str,
    *,
    data_dir: Path,
    session: str,
    clipboard: bool = False,
    exact: bool = False,
    review: bool = False,
) -> int:
    """Mask or restore stdin/clipboard text using a persistent local session."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", session):
        raise SettingsError(
            "use a session label with letters, numbers, dots, "
            "underscores, colons, or hyphens"
        )
    prepare_data_dir(data_dir)
    settings = load_settings(data_dir / "config.json")
    identity = git_identity(Path.cwd()) if settings.identity else {}
    source = clipboard_read() if clipboard else sys.stdin.read()
    with open_sessions(data_dir, settings, identity) as sessions:
        shield = sessions.get(session).shield
        result: MaskResult | RestoreResult
        if operation == "mask":
            if review or settings.secret_review:
                from .detectors._secrets import SECRET_TYPES
                from .placeholders import placeholder_type

                for placeholder, value in shield.vault.items():
                    kind = placeholder_type(placeholder)
                    if kind in SECRET_TYPES:
                        assert kind is not None
                        shield.add_entity(value, kind)
            result = shield.mask(source)
            if review or settings.secret_review:
                from .gateway.request import _KnownValues
                from .gateway.store import registered_values
                from .review_cli import ask_terminal
                from .secret_review import ReviewError, candidates

                try:
                    known = _KnownValues(
                        shield.vault, registered_values(settings, identity)
                    )
                    findings = candidates(source, visible=known.unprotected(source))
                except ReviewError as error:
                    raise SettingsError(f"{error}; no output written") from None
                for finding in findings:
                    kind = ask_terminal(finding.value, finding.reason)
                    if kind != "IGNORE":
                        shield.add_entity(finding.value, kind)
                result = shield.mask(source)
        elif operation == "restore":
            result = shield.restore(source, tolerant=not exact)
        else:
            raise SettingsError("unknown text operation")
        if result.warnings:
            raise SettingsError(
                f"{operation} reported {len(result.warnings)} warning(s); "
                "no output written. Check the session and registered values."
            )
        transformed = result.text
    if clipboard:
        clipboard_write(transformed)
        print(
            f"veil: {operation} completed on the clipboard (session {session})",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(transformed)
    return 0
