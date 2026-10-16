"""Explicit setup of a pinned, data-only local speech model."""

from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import time
import urllib.request
from pathlib import Path

from .gateway.config import SettingsError, prepare_data_dir

MODEL_REVISION = "0d3d19a32d3338f10357c0889762bd8d64bbdeba"
MODEL_REPOSITORY = "Systran/faster-whisper-tiny.en"
_FILES = {
    "config.json": (
        2317,
        "14b1b421a90349bc551b881461426b561a874049cb9e4c4864f2ca384f6a7cc5",
    ),
    "model.bin": (
        75_537_502,
        "1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a",
    ),
    "tokenizer.json": (
        2_128_466,
        "929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df",
    ),
    "vocabulary.txt": (
        422_309,
        "ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf",
    ),
}


def require_voice() -> None:
    """Keep the base package independent of audio and model runtimes."""
    if any(importlib.util.find_spec(name) is None for name in ("faster_whisper", "av")):
        raise SettingsError(
            "Local voice support needs the optional 'veil[voice]' extra."
        )


def model_path(data_dir: Path) -> Path:
    """Use one predictable model folder without changing gateway settings."""
    return data_dir / "voice-model"


def model_ready(data_dir: Path) -> bool:
    """Check installed file structure without loading or hashing large weights."""
    folder = model_path(data_dir)
    try:
        return (
            folder.is_dir()
            and not folder.is_symlink()
            and all(
                (folder / name).is_file()
                and not (folder / name).is_symlink()
                and (folder / name).stat().st_size == size
                for name, (size, _digest) in _FILES.items()
            )
        )
    except OSError:
        return False


def validate_model(data_dir: Path) -> Path:
    """Verify pinned model contents before passing them to the native engine."""
    if not model_ready(data_dir):
        raise SettingsError(
            "Set up the local voice model in Settings or run veil voice setup."
        )
    folder = model_path(data_dir)
    try:
        for name, (_size, expected) in _FILES.items():
            digest = hashlib.sha256()
            with (folder / name).open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise SettingsError("The local voice model failed its integrity check.")
    except OSError:
        raise SettingsError("Could not read the local voice model.") from None
    return folder


def setup_model(data_dir: Path) -> str:
    """Download only pinned public model assets, never audio or private text."""
    require_voice()
    if model_ready(data_dir):
        validate_model(data_dir)
        return "Local voice model is ready (tiny.en)."
    prepare_data_dir(data_dir)
    target = model_path(data_dir)
    if target.exists() or target.is_symlink():
        raise SettingsError(
            "The voice-model folder is incomplete. Move it aside and run setup again."
        )
    deadline = time.monotonic() + 300
    try:
        with tempfile.TemporaryDirectory(prefix=".voice-setup-", dir=data_dir) as temp:
            staging = Path(temp) / "voice-model"
            staging.mkdir(mode=0o700)
            for name, (size, expected) in _FILES.items():
                url = (
                    f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/"
                    f"{MODEL_REVISION}/{name}"
                )
                digest = hashlib.sha256()
                count = 0
                path = staging / name
                path.touch(mode=0o600, exist_ok=False)
                with (
                    urllib.request.urlopen(url, timeout=30) as response,
                    path.open("wb") as output,
                ):
                    while block := response.read(1024 * 1024):
                        count += len(block)
                        if count > size or time.monotonic() >= deadline:
                            raise ValueError
                        digest.update(block)
                        output.write(block)
                if count != size or digest.hexdigest() != expected:
                    raise ValueError
            staging.rename(target)
    except KeyboardInterrupt:
        raise SettingsError("Voice model setup cancelled. No audio was sent.") from None
    except Exception:
        raise SettingsError(
            "Could not install the voice model. Check your connection and disk space, "
            "then retry. No audio was sent."
        ) from None
    return "Local voice model is ready (tiny.en). Transcription works offline."
