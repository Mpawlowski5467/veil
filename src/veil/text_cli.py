"""Explicit local mask/restore workflows for ChatGPT and other chat interfaces."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import MaskResult, RestoreResult
from .gateway import git_identity, load_settings, open_sessions, prepare_data_dir
from .gateway.config import SettingsError

if TYPE_CHECKING:
    from .ocr import OCRText
    from .pdf_text import PDFText
    from .voice import VoiceText


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


def _clipboard_timeout() -> float:
    # A first Windows PowerShell start can take more than 10 s on a busy machine.
    return 30 if sys.platform == "win32" else 10


def clipboard_read() -> str:
    """Read text only when the user explicitly selected --clipboard."""
    try:
        result = subprocess.run(
            _clipboard_command(write=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_clipboard_timeout(),
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
            timeout=_clipboard_timeout(),
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
    pdf: Path | None = None,
    ocr: Path | None = None,
    audio: Path | None = None,
    record: float | None = None,
) -> int:
    """Mask or restore stdin/clipboard text using a persistent local session."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", session):
        raise SettingsError(
            "use a session label with letters, numbers, dots, "
            "underscores, colons, or hyphens"
        )
    document: PDFText | OCRText | VoiceText | None = None
    if audio is not None or record is not None:
        if (
            operation != "mask"
            or clipboard
            or pdf is not None
            or ocr is not None
            or (audio is not None and record is not None)
        ):
            raise SettingsError(
                "Voice input is available for mask without other inputs"
            )
        from .voice import transcribe_audio, transcribe_file

        if audio is not None:
            document = transcribe_file(audio, data_dir)
        else:
            from .microphone import record_audio
            from .voice_model import require_voice, validate_model

            require_voice()
            validate_model(data_dir)
            assert record is not None
            print(
                f"veil: Recording for {record:g} seconds. Ctrl-C discards.",
                file=sys.stderr,
            )
            document = transcribe_audio(record_audio(record), data_dir)
    if ocr is not None:
        if operation != "mask" or clipboard or pdf is not None:
            raise SettingsError("OCR input is available for mask without other inputs")
        from .ocr import extract_ocr

        document = extract_ocr(ocr)
    if pdf is not None:
        if operation != "mask" or clipboard:
            raise SettingsError("PDF input is available for mask without --clipboard")
        from .pdf_text import extract_pdf

        document = extract_pdf(pdf)
    prepare_data_dir(data_dir)
    settings = load_settings(data_dir / "config.json")
    identity = git_identity(Path.cwd()) if settings.identity else {}
    source = (
        document.text
        if document is not None
        else clipboard_read()
        if clipboard
        else sys.stdin.read()
    )
    with open_sessions(data_dir, settings, identity) as sessions:
        shield = sessions.get(session).shield
        result: MaskResult | RestoreResult
        if operation == "mask":
            if review or settings.secret_review:
                from .placeholders import placeholder_type
                from .secret_review import REVIEW_TYPES

                for placeholder, value in shield.vault.items():
                    kind = placeholder_type(placeholder)
                    if kind in REVIEW_TYPES:
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
    if document is not None:
        print(
            (
                f"veil: {document.summary} Output is masked text; no audio was saved."
                if record is not None
                else f"veil: {document.summary} Output is masked text; "
                f"the original {'PDF' if pdf is not None else 'file'} is unchanged."
            ),
            file=sys.stderr,
        )
    return 0
