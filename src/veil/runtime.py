"""Pinned subprocess entry points for installed Python and standalone bundles."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Literal

from .gateway.config import SettingsError

Target = Literal["cli", "gateway", "pdf", "ocr", "voice"]
_TARGETS = {
    "cli": ("veil.cli", "main"),
    "gateway": ("veil.service", "daemon_main"),
    "pdf": ("veil.pdf_text", "_worker"),
    "ocr": ("veil.ocr", "_worker"),
    "voice": ("veil.voice", "_worker"),
}


def bundled() -> bool:
    """Return whether the executable carries its own interpreter and libraries."""
    return bool(getattr(sys, "frozen", False))


def executable() -> str:
    """Use the bundled console helper so workers never open extra Dock windows."""
    if not bundled():
        return sys.executable
    helper = Path(sys.executable).with_name("veil-runtime")
    if not helper.is_file():
        raise SettingsError("The Veil app is incomplete. Reinstall the complete app.")
    return str(helper)


def command(target: Target, *arguments: str) -> list[str]:
    """Pin a known entry point; never resolve Python or Veil from the shell PATH."""
    module, function = _TARGETS[target]
    if bundled():
        return [executable(), "--veil-worker", target, *arguments]
    package = str(Path(__file__).resolve().parent.parent)
    code = (
        f"import sys; sys.path.insert(0, {package!r}); "
        f"from {module} import {function}; sys.exit({function}())"
    )
    return [sys.executable, "-I", "-c", code, *arguments]


def main() -> int:
    """Dispatch the app or a fixed worker, without exposing Python's eval flags."""
    arguments = sys.argv[1:]
    if arguments[:1] == ["--veil-worker"]:
        if len(arguments) < 2 or arguments[1] not in _TARGETS:
            return 2
        module, function = _TARGETS[arguments[1]]
        sys.argv = [sys.argv[0], *arguments[2:]]
        return int(getattr(importlib.import_module(module), function)())
    from .cli import main as cli_main

    if not arguments and Path(sys.executable).name == "Veil":
        arguments = ["desktop"]
    return cli_main(arguments)
