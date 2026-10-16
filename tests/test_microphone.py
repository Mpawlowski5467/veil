"""Microphone capture owns its stream and never persists raw audio."""

import io
import types
import wave

import pytest

from veil import microphone
from veil.gateway.config import SettingsError


@pytest.fixture
def device(monkeypatch):
    streams = []

    class CallbackStopError(Exception):
        pass

    class Stream:
        def __init__(self, **kwargs):
            assert kwargs["samplerate"] == 16000
            assert kwargs["channels"] == 1
            assert kwargs["dtype"] == "int16"
            self.callback = kwargs["callback"]
            self.finished = kwargs["finished_callback"]
            self.actions = []
            streams.append(self)

        def start(self):
            self.actions.append("start")

        def stop(self):
            self.actions.append("stop")

        def abort(self):
            self.actions.append("abort")

        def close(self):
            self.actions.append("close")

        def emit(self, data, status=False, frames=None):
            try:
                self.callback(
                    data, len(data) // 2 if frames is None else frames, None, status
                )
            except CallbackStopError:
                self.finished()

    fake = types.SimpleNamespace(RawInputStream=Stream, CallbackStop=CallbackStopError)
    monkeypatch.setattr(microphone.importlib, "import_module", lambda _: fake)
    return streams


def test_stop_closes_capture_and_returns_only_in_memory_wav(device):
    recording = microphone.MicrophoneRecording()
    device[0].emit(b"\x01\x00" * 16000)
    assert recording.seconds == 1
    result = recording.stop()
    assert device[0].actions == ["start", "stop", "close"]
    assert recording.finished.is_set()
    assert recording._data == b""
    with wave.open(io.BytesIO(result), "rb") as stream:
        assert stream.getnchannels() == 1
        assert stream.getframerate() == 16000
        assert stream.getnframes() == 16000
    recording.cancel()
    assert device[0].actions.count("close") == 1


def test_cancel_aborts_and_discards_even_after_repeated_calls(device):
    recording = microphone.MicrophoneRecording()
    device[0].emit(b"\x01\x00" * 16000)
    recording.cancel()
    recording.cancel()
    assert device[0].actions == ["start", "abort", "close"]
    assert recording._data == b""
    assert recording.finished.is_set()


@pytest.mark.parametrize(("status", "frames"), [(True, None), (False, 2)])
def test_interrupted_or_bad_capture_refuses_partial_audio(device, status, frames):
    recording = microphone.MicrophoneRecording()
    device[0].emit(b"\x01\x00" * 16000)
    device[0].emit(b"\x01\x00" * 100, status=status, frames=frames)
    assert recording.finished.is_set()
    with pytest.raises(SettingsError, match="interrupted or lost"):
        recording.stop()
    assert device[0].actions[-1] == "close"
    assert not recording._data


def test_limit_stops_on_complete_samples(device, monkeypatch):
    monkeypatch.setattr(microphone, "MAX_SECONDS", 1)
    recording = microphone.MicrophoneRecording()
    device[0].emit(b"\x01\x00" * 17000)
    assert recording.finished.is_set()
    assert recording.seconds == 1
    with wave.open(io.BytesIO(recording.stop()), "rb") as stream:
        assert stream.getnframes() == 16000


def test_short_recordings_are_closed_and_discarded(device):
    recording = microphone.MicrophoneRecording()
    device[0].emit(b"\x01\x00" * 100)
    with pytest.raises(SettingsError, match="too short"):
        recording.stop()
    assert device[0].actions[-1] == "close"
    assert not recording._data


def test_failed_start_closes_created_stream(device, monkeypatch):
    # The stream constructor succeeds but starting the device fails.
    original_import = microphone.importlib.import_module
    fake = original_import("sounddevice")

    def fail(self):
        raise RuntimeError("private device name")

    monkeypatch.setattr(fake.RawInputStream, "start", fail)
    with pytest.raises(SettingsError, match="permission") as error:
        microphone.MicrophoneRecording()
    assert "private" not in str(error.value)
    assert device[0].actions == ["abort", "close"]


def test_stop_failure_still_closes_and_clears(device, monkeypatch):
    recording = microphone.MicrophoneRecording()
    device[0].emit(b"\x01\x00" * 16000)

    def fail():
        raise RuntimeError("private device name")

    monkeypatch.setattr(device[0], "stop", fail)
    with pytest.raises(SettingsError, match="Could not finish") as error:
        recording.stop()
    assert "private" not in str(error.value)
    assert device[0].actions[-1] == "close"
    assert not recording._data


@pytest.mark.parametrize("seconds", [0, -1, 301, float("nan"), float("inf")])
def test_timed_capture_rejects_bad_durations_before_opening(device, seconds):
    with pytest.raises(SettingsError, match="1 to 300"):
        microphone.record_audio(seconds)
    assert device == []


def test_ctrl_c_discards_timed_capture(device, monkeypatch):
    real = microphone.MicrophoneRecording
    recordings = []

    def create():
        recording = real()

        def interrupt(_):
            raise KeyboardInterrupt

        monkeypatch.setattr(recording.finished, "wait", interrupt)
        recordings.append(recording)
        return recording

    monkeypatch.setattr(microphone, "MicrophoneRecording", create)
    with pytest.raises(SettingsError, match="cancelled"):
        microphone.record_audio(2)
    assert device[0].actions[-2:] == ["abort", "close"]
    assert not recordings[0]._data
