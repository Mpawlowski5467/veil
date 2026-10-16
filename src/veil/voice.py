"""Bounded offline speech transcription for explicit audio imports."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import logging
import math
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gateway.config import SettingsError
from .runtime import command
from .voice_model import model_ready, require_voice, validate_model

MAX_BYTES = 50 * 1024 * 1024
MAX_SECONDS = 300
MAX_TEXT = 32_768
SAMPLE_RATE = 16_000
TIMEOUT = 180
_FORMATS = "wav,mp3,mov,flac,ogg,aac,aiff,matroska,webm"
_ERRORS = {
    "invalid": (
        "Could not read this audio. Choose a valid WAV, MP3, M4A, FLAC, OGG, "
        "or AAC file."
    ),
    "duration": "Voice import accepts up to 5 minutes. Choose a shorter recording.",
    "streams": "Choose an audio-only file with one audio track.",
    "empty": "No speech was recognized. Try a clearer recording.",
    "text": "The transcript exceeds 32768 characters. Choose a shorter recording.",
    "timeout": "Local transcription timed out. Choose a shorter recording.",
    "model": "Could not load the local voice model. Check its installation.",
    "engine": "Local transcription failed. No partial transcript was returned.",
}


@dataclass(frozen=True)
class VoiceText:
    """A transcript and duration, with no audio, source path, or metadata."""

    text: str
    seconds: float

    @property
    def summary(self) -> str:
        """Remind users that speech recognition is not reliable PII detection."""
        return (
            f"Transcribed {math.ceil(self.seconds)} seconds locally (English). "
            "Speech recognition can miss, invent, or mishear details; "
            "review before sharing."
        )


def _source(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise SettingsError("Choose a regular audio file.")
            if info.st_size > MAX_BYTES:
                raise SettingsError("Voice import accepts files up to 50 MiB.")
            source = stream.read(MAX_BYTES + 1)
    except OSError:
        raise SettingsError(
            "Could not read the audio file. Check its permissions."
        ) from None
    if len(source) > MAX_BYTES:
        raise SettingsError("Voice import accepts files up to 50 MiB.")
    return source


def transcribe_file(path: Path, data_dir: Path) -> VoiceText:
    """Read a local audio file without modifying or uploading it."""
    return transcribe_audio(_source(path), data_dir)


def transcribe_audio(source: bytes, data_dir: Path) -> VoiceText:
    """Transcribe in a disposable process using an already installed model."""
    require_voice()
    if not model_ready(data_dir):
        raise SettingsError("Set up voice in Settings or run veil voice setup first.")
    if len(source) > MAX_BYTES:
        raise SettingsError("Voice import accepts files up to 50 MiB.")
    environment = dict(os.environ)
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    try:
        result = subprocess.run(
            command("voice", str(data_dir.absolute())),
            input=source,
            capture_output=True,
            timeout=TIMEOUT,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SettingsError(_ERRORS["timeout"]) from None
    except OSError:
        raise SettingsError("Could not start local speech transcription.") from None
    try:
        payload = json.loads(result.stdout)
        if result.returncode:
            raise SettingsError(_ERRORS.get(payload.get("error"), _ERRORS["engine"]))
        text, seconds = payload["text"], payload["seconds"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_TEXT
            or type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not 0 < seconds <= MAX_SECONDS
        ):
            raise ValueError
    except SettingsError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SettingsError(_ERRORS["engine"]) from None
    return VoiceText(text, float(seconds))


def _decode(source: bytes) -> Any:
    av = importlib.import_module("av")
    np = importlib.import_module("numpy")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    pcm = bytearray()

    def append(frame: Any) -> None:
        if len(pcm) + frame.samples * 2 > MAX_SECONDS * SAMPLE_RATE * 2:
            raise RuntimeError("duration")
        pcm.extend(frame.to_ndarray().tobytes())

    # BytesIO and the protocol/format allowlists prevent external playlist reads.
    with av.open(
        io.BytesIO(source),
        mode="r",
        options={
            "protocol_whitelist": "pipe",
            "format_whitelist": _FORMATS,
            "enable_drefs": "0",
            "use_absolute_path": "0",
        },
    ) as container:
        if len(container.streams.audio) != 1 or container.streams.video:
            raise RuntimeError("streams")
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                append(converted)
        for converted in resampler.resample(None):
            append(converted)
    if not pcm:
        raise RuntimeError("empty")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def _transcribe(source: bytes, data_dir: Path) -> dict[str, str | float]:
    deadline = time.monotonic() + 150
    audio = _decode(source)
    if not audio.any():
        return {"error": "empty"}
    try:
        folder = validate_model(data_dir)
        whisper = importlib.import_module("faster_whisper")
        model = whisper.WhisperModel(
            str(folder),
            device="cpu",
            compute_type="int8",
            cpu_threads=4,
            local_files_only=True,
        )
    except Exception:
        return {"error": "model"}
    segments, _info = model.transcribe(
        audio,
        language="en",
        task="transcribe",
        beam_size=5,
        condition_on_previous_text=False,
        vad_filter=True,
        temperature=0.0,
    )
    texts: list[str] = []
    length = 0
    for segment in segments:
        if time.monotonic() >= deadline:
            return {"error": "timeout"}
        text = segment.text.strip()
        length += len(text) + bool(texts)
        if length > MAX_TEXT:
            return {"error": "text"}
        if text:
            texts.append(text)
    if not texts:
        return {"error": "empty"}
    return {"text": "\n".join(texts), "seconds": len(audio) / SAMPLE_RATE}


def _worker() -> int:
    """Discard all engine diagnostics and return text only after full success."""
    logging.disable(logging.CRITICAL)
    try:
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            source = sys.stdin.buffer.read(MAX_BYTES + 1)
            payload = (
                _transcribe(source, Path(sys.argv[1]))
                if len(source) <= MAX_BYTES
                else {"error": "invalid"}
            )
    except RuntimeError as error:
        code = str(error)
        payload = {"error": code if code in _ERRORS else "engine"}
    except Exception:
        payload = {"error": "invalid"}
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    return 2 if "error" in payload else 0
