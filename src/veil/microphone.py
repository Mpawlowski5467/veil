"""Explicit, bounded microphone capture held in memory until transcribed."""

from __future__ import annotations

import contextlib
import importlib
import io
import math
import threading
import wave
from typing import Any

from .gateway.config import SettingsError
from .voice import MAX_SECONDS, SAMPLE_RATE


class MicrophoneRecording:
    """Own one microphone stream; stop/cancel always close it and clear buffers."""

    def __init__(self) -> None:
        """Start capture only when called by an explicit recording action."""
        self.finished = threading.Event()
        self._data = bytearray()
        self._error = False
        self._stream: Any = None
        try:
            device = importlib.import_module("sounddevice")

            def receive(data: Any, frames: int, _time: Any, status: Any) -> None:
                if status or len(data) != frames * 2:
                    self._error = True
                    self.finished.set()
                    raise device.CallbackStop
                limit = MAX_SECONDS * SAMPLE_RATE * 2
                remaining = limit - len(self._data)
                self._data.extend(bytes(data)[:remaining])
                if len(self._data) >= limit:
                    self.finished.set()
                    raise device.CallbackStop

            self._stream = device.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                callback=receive,
                finished_callback=self.finished.set,
            )
            self._stream.start()
        except BaseException as error:
            with contextlib.suppress(Exception):
                self.cancel()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SettingsError(
                "Could not open the microphone. Check microphone permission, "
                "the input device, and the optional 'veil[voice]' extra."
            ) from None

    @property
    def seconds(self) -> float:
        """Return captured duration without exposing audio samples."""
        return len(self._data) / (SAMPLE_RATE * 2)

    def _close(self, *, discard: bool) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort() if discard else stream.stop()
            finally:
                stream.close()

    def cancel(self) -> None:
        """Stop immediately and discard audio without writing any file."""
        try:
            self._close(discard=True)
        finally:
            self._data.clear()
            self.finished.set()

    def stop(self) -> bytes:
        """Close capture and return an in-memory mono PCM WAV for transcription."""
        try:
            self._close(discard=False)
            if self._error:
                raise SettingsError(
                    "Microphone audio was interrupted or lost. Record again; "
                    "no partial transcript was returned."
                )
            if self.seconds < 0.2:
                raise SettingsError("The recording is too short. Record again.")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(SAMPLE_RATE)
                output.writeframes(self._data)
            return buffer.getvalue()
        except SettingsError:
            raise
        except Exception:
            raise SettingsError(
                "Could not finish microphone capture. Record again."
            ) from None
        finally:
            self._data.clear()
            self.finished.set()


def record_audio(seconds: float) -> bytes:
    """Record for an explicitly requested duration; Ctrl-C discards capture."""
    if not math.isfinite(seconds) or not 1 <= seconds <= MAX_SECONDS:
        raise SettingsError("Choose a recording duration from 1 to 300 seconds.")
    recording = MicrophoneRecording()
    try:
        recording.finished.wait(seconds)
        return recording.stop()
    except KeyboardInterrupt:
        raise SettingsError("Recording cancelled. No audio was saved.") from None
    finally:
        recording.cancel()
