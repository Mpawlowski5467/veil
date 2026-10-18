"""Offline audio decoding, model setup, CLI masking, and private failure paths."""

import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import types
import wave
from pathlib import Path

import pytest

from veil import cli, voice, voice_model
from veil.desktop import DesktopController
from veil.gateway.config import SettingsError, prepare_data_dir


@pytest.fixture
def wav_bytes():
    def create(*, rate=16000, channels=1, seconds=1, value=1000):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as stream:
            stream.setnchannels(channels)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(
                struct.pack("<h", value) * int(rate * seconds) * channels
            )
        return buffer.getvalue()

    return create


@pytest.fixture
def audio_runtime():
    pytest.importorskip("av")
    return pytest.importorskip("numpy")


@pytest.mark.parametrize("rate", [8000, 16000, 44100, 48000])
@pytest.mark.parametrize("channels", [1, 2])
def test_decode_resamples_all_samples_and_downmixes(
    audio_runtime, wav_bytes, rate, channels
):
    audio = voice._decode(wav_bytes(rate=rate, channels=channels))
    assert len(audio) == 16000
    assert audio.dtype == audio_runtime.float32
    assert audio.min() >= -1
    assert audio.max() <= 1


@pytest.mark.parametrize(
    ("format_name", "codec", "rate"),
    [
        ("mp3", "libmp3lame", 16000),
        ("mp4", "aac", 16000),
        ("flac", "flac", 16000),
        ("ogg", "libopus", 48000),
        ("adts", "aac", 16000),
        ("aiff", "pcm_s16be", 16000),
    ],
)
def test_native_audio_formats_decode(audio_runtime, format_name, codec, rate):
    import av

    buffer = io.BytesIO()
    with av.open(buffer, "w", format=format_name) as output:
        stream = output.add_stream(codec, rate=rate)
        stream.layout = "mono"
        samples = audio_runtime.ones((1, rate), dtype=audio_runtime.int16) * 1000
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)
    audio = voice._decode(buffer.getvalue())
    # Lossy codecs may include a little padding; no source samples are dropped.
    assert 16000 <= len(audio) < 18000


def test_multiple_audio_tracks_are_refused(audio_runtime):
    import av

    buffer = io.BytesIO()
    with av.open(buffer, "w", format="matroska") as output:
        streams = [output.add_stream("flac", rate=16000) for _ in range(2)]
        for stream in streams:
            stream.layout = "mono"
        for stream in streams:
            frame = av.AudioFrame.from_ndarray(
                audio_runtime.ones((1, 16000), dtype=audio_runtime.int16),
                format="s16",
                layout="mono",
            )
            frame.sample_rate = 16000
            frame.pts = 0
            for packet in stream.encode(frame):
                output.mux(packet)
            for packet in stream.encode(None):
                output.mux(packet)
    with pytest.raises(RuntimeError, match="streams"):
        voice._decode(buffer.getvalue())


def test_decoded_duration_is_enforced_without_truncating(
    audio_runtime, wav_bytes, monkeypatch
):
    monkeypatch.setattr(voice, "MAX_SECONDS", 1)
    with pytest.raises(RuntimeError, match="duration"):
        voice._decode(wav_bytes(seconds=1.1))


def test_decode_refuses_playlists_before_external_reads(audio_runtime, monkeypatch):
    import av

    with pytest.raises(av.error.FFmpegError):
        voice._decode(
            b"#EXTM3U\n#EXT-X-TARGETDURATION:10\nhttps://example.invalid/private.mp3\n"
        )


def test_silence_never_loads_model(audio_runtime, wav_bytes, monkeypatch):
    monkeypatch.setattr(voice, "validate_model", lambda _: pytest.fail("loaded model"))
    assert voice._transcribe(wav_bytes(value=0), Path("unused")) == {"error": "empty"}


@pytest.fixture
def speech_engine(audio_runtime, wav_bytes, monkeypatch, tmp_path):
    real_import = voice.importlib.import_module
    calls = []
    segments = [
        types.SimpleNamespace(text=" Jane Example"),
        types.SimpleNamespace(text="jane@example.com "),
    ]

    class Model:
        def __init__(self, path, **kwargs):
            assert path == str(tmp_path)
            assert kwargs["local_files_only"] is True
            assert kwargs["device"] == "cpu"
            calls.append(kwargs)

        def transcribe(self, audio, **kwargs):
            assert kwargs["language"] == "en"
            assert kwargs["task"] == "transcribe"
            assert kwargs["vad_filter"] is True
            return iter(segments), None

    monkeypatch.setattr(voice, "validate_model", lambda _: tmp_path)
    monkeypatch.setattr(
        voice.importlib,
        "import_module",
        lambda name: (
            types.SimpleNamespace(WhisperModel=Model)
            if name == "faster_whisper"
            else real_import(name)
        ),
    )
    return segments, calls


def test_engine_is_local_and_complete_transcript_is_bounded(
    speech_engine, wav_bytes, tmp_path
):
    result = voice._transcribe(wav_bytes(), tmp_path)
    assert result == {"text": "Jane Example\njane@example.com", "seconds": 1.0}
    assert len(speech_engine[1]) == 1


def test_oversized_transcript_returns_no_partial_text(
    speech_engine, wav_bytes, tmp_path
):
    speech_engine[0].append(types.SimpleNamespace(text="a" * (voice.MAX_TEXT + 1)))
    assert voice._transcribe(wav_bytes(), tmp_path) == {"error": "text"}


def test_segment_failure_propagates_instead_of_returning_previous_text(
    speech_engine, wav_bytes, tmp_path
):
    class BadSegment:
        @property
        def text(self):
            raise RuntimeError("private transcript")

    speech_engine[0].append(BadSegment())
    with pytest.raises(RuntimeError, match="private transcript"):
        voice._transcribe(wav_bytes(), tmp_path)


def fake_ready(monkeypatch):
    monkeypatch.setattr(voice, "require_voice", lambda: None)
    monkeypatch.setattr(voice, "model_ready", lambda _: True)


def test_worker_receives_audio_only_in_stdin_and_offline_environment(
    tmp_path, monkeypatch
):
    fake_ready(monkeypatch)

    def run(args, **kwargs):
        assert "private-audio" not in repr(args)
        assert kwargs["input"] == b"private-audio"
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert kwargs["env"]["TRANSFORMERS_OFFLINE"] == "1"
        return subprocess.CompletedProcess(
            args, 0, b'{"text":"Jane Example","seconds":1}'
        )

    monkeypatch.setattr(voice.subprocess, "run", run)
    assert voice.transcribe_audio(b"private-audio", tmp_path).text == "Jane Example"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "payload",
    [
        b"not JSON",
        b"[]",
        b"{}",
        b'{"text":"private","seconds":NaN}',
        b'{"text":"private","seconds":301}',
        b'{"text":"private","seconds":true}',
        b'{"text":"","seconds":1}',
    ],
)
def test_bad_worker_output_is_not_exposed(tmp_path, monkeypatch, payload):
    fake_ready(monkeypatch)
    monkeypatch.setattr(
        voice.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, payload),
    )
    with pytest.raises(SettingsError, match="Local transcription failed") as error:
        voice.transcribe_audio(b"audio", tmp_path)
    assert "private" not in str(error.value)


def test_timeout_and_source_errors_are_private(tmp_path, monkeypatch):
    fake_ready(monkeypatch)

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("private.wav", 180, output=b"private text")

    monkeypatch.setattr(voice.subprocess, "run", timeout)
    with pytest.raises(SettingsError, match="timed out") as error:
        voice.transcribe_audio(b"audio", tmp_path)
    assert "private" not in str(error.value)
    with pytest.raises(SettingsError, match="permissions") as error:
        voice.transcribe_file(tmp_path / "private.wav", tmp_path)
    assert "private.wav" not in str(error.value)


def test_size_limit_and_regular_files(tmp_path):
    path = tmp_path / "large.wav"
    with path.open("wb") as stream:
        stream.truncate(voice.MAX_BYTES + 1)
    with pytest.raises(SettingsError, match="50 MiB"):
        voice._source(path)
    with pytest.raises(SettingsError, match=r"regular|permissions"):
        voice._source(tmp_path)


def test_missing_voice_runtime_and_model_are_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_model.importlib.util, "find_spec", lambda _: None)
    with pytest.raises(SettingsError, match=r"veil\[voice\]"):
        voice.transcribe_audio(b"audio", tmp_path)
    monkeypatch.setattr(voice, "require_voice", lambda: None)
    with pytest.raises(SettingsError, match="veil voice setup"):
        voice.transcribe_audio(b"audio", tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.fixture
def model_assets(monkeypatch):
    assets = {"model.bin": b"fake-weights", "tokenizer.json": b"fake-tokenizer"}
    files = {
        name: (len(value), hashlib.sha256(value).hexdigest())
        for name, value in assets.items()
    }
    monkeypatch.setattr(voice_model, "_FILES", files)
    monkeypatch.setattr(voice_model, "require_voice", lambda: None)

    def download(url, timeout):
        assert voice_model.MODEL_REVISION in url
        assert url.startswith(
            "https://huggingface.co/Systran/faster-whisper-tiny.en/resolve/"
        )
        return io.BytesIO(assets[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(voice_model.urllib.request, "urlopen", download)
    return assets


def test_model_setup_verifies_pinned_files_and_is_idempotent(
    tmp_path, model_assets, monkeypatch
):
    data = tmp_path / "data"
    assert "works offline" in voice_model.setup_model(data)
    assert voice_model.model_ready(data)
    assert voice_model.validate_model(data) == data / "voice-model"
    assert {
        p.name: p.read_bytes() for p in (data / "voice-model").iterdir()
    } == model_assets
    if os.name == "posix":
        assert (data.stat().st_mode & 0o777) == 0o700
        assert all(
            (p.stat().st_mode & 0o777) == 0o600
            for p in (data / "voice-model").iterdir()
        )
    monkeypatch.setattr(
        voice_model.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("downloaded again"),
    )
    assert "ready" in voice_model.setup_model(data)


def test_failed_download_cleans_partial_files(tmp_path, model_assets):
    model_assets["tokenizer.json"] = b"corrupt-private-error"
    data = tmp_path / "data"
    with pytest.raises(SettingsError, match="Could not install") as error:
        voice_model.setup_model(data)
    assert "private" not in str(error.value)
    assert not list(data.iterdir())


def test_corrupt_and_existing_models_are_not_overwritten(tmp_path, model_assets):
    data = tmp_path / "data"
    voice_model.setup_model(data)
    path = data / "voice-model" / "model.bin"
    path.write_bytes(b"x" * len(model_assets["model.bin"]))
    with pytest.raises(SettingsError, match="integrity"):
        voice_model.validate_model(data)
    with pytest.raises(SettingsError, match="integrity"):
        voice_model.setup_model(data)
    path.write_bytes(b"short")
    with pytest.raises(SettingsError, match="Move it aside"):
        voice_model.setup_model(data)
    assert path.read_bytes() == b"short"


@pytest.mark.skipif(os.name != "posix", reason="native symlink test")
def test_model_links_are_refused(tmp_path, model_assets):
    data = tmp_path / "data"
    voice_model.setup_model(data)
    path = data / "voice-model" / "model.bin"
    source = tmp_path / "weights"
    path.rename(source)
    path.symlink_to(source)
    assert not voice_model.model_ready(data)


@pytest.mark.parametrize(
    "input_option", [["--audio", "private.wav"], ["--record", "2"]]
)
def test_voice_masking_uses_registrations_and_saved_restoration(
    tmp_path, monkeypatch, capsys, input_option
):
    transcript = voice.VoiceText("Jane Example at jane@example.com", 2)
    monkeypatch.setattr(voice, "transcribe_file", lambda *a: transcript)
    monkeypatch.setattr(voice, "transcribe_audio", lambda *a: transcript)
    monkeypatch.setattr(voice_model, "require_voice", lambda: None)
    monkeypatch.setattr(voice_model, "validate_model", lambda _: tmp_path)
    monkeypatch.setattr("veil.microphone.record_audio", lambda _: b"fictional audio")
    data = prepare_data_dir(tmp_path / "data")
    (data / "config.json").write_text(
        json.dumps({"identity": False, "entities": {"PERSON": ["Jane Example"]}})
    )
    options = ["--data-dir", str(data)]
    monkeypatch.setattr(sys, "stdin", io.StringIO("must not read"))
    assert cli.main([*options, "mask", "--session", "voice", *input_option]) == 0
    output = capsys.readouterr()
    assert output.out == "[PERSON_1] at [EMAIL_1]"
    assert output.err.count("Transcribed") == 1
    assert "review before sharing" in output.err
    assert "private.wav" not in output.err
    monkeypatch.setattr(sys, "stdin", io.StringIO("[PERSON_1]: [EMAIL_1]"))
    assert cli.main([*options, "restore", "--session", "voice"]) == 0
    assert capsys.readouterr().out == "Jane Example: jane@example.com"


def test_audio_failure_writes_no_text_or_session(tmp_path, capsys, monkeypatch):
    def fail(*a):
        raise SettingsError("No speech was recognized.")

    monkeypatch.setattr(voice, "transcribe_file", fail)
    data = tmp_path / "absent"
    assert (
        cli.main(
            [
                "--data-dir",
                str(data),
                "mask",
                "--session",
                "voice",
                "--audio",
                "private.wav",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert not data.exists()


@pytest.mark.parametrize(
    "options",
    [
        ["--audio", "audio.wav", "--clipboard"],
        ["--audio", "audio.wav", "--pdf", "file.pdf"],
        ["--audio", "audio.wav", "--ocr", "image.png"],
        ["--audio", "audio.wav", "--record", "2"],
    ],
)
def test_voice_input_is_exclusive(options):
    with pytest.raises(SystemExit) as error:
        cli.main(["mask", "--session", "voice", *options])
    assert error.value.code == 2


def test_voice_status_does_not_download_or_create_data(tmp_path, capsys):
    data = tmp_path / "absent"
    assert cli.main(["--data-dir", str(data), "voice", "status"]) == 0
    assert "not installed" in capsys.readouterr().out
    assert not data.exists()


def test_import_is_lightweight_and_never_starts_a_microphone():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import veil.voice; import veil.microphone; "
            "assert not {'faster_whisper','av','numpy','sounddevice'} "
            "& sys.modules.keys()",
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    os.environ.get("VEIL_NATIVE_VOICE") != "1", reason="opt-in real local speech model"
)
def test_real_offline_speech_decode_transcribe_mask_restore(tmp_path, monkeypatch):
    pytest.importorskip("faster_whisper")
    data = Path(os.environ["VEIL_VOICE_TEST_DATA"])
    assert voice_model.model_ready(data), "install the explicit local test model first"
    path = tmp_path / "fictional.aiff"
    phrase = (
        "Please call Jane Example tomorrow about the meeting. "
        "The project is called Veil."
    )
    if sys.platform == "darwin":
        subprocess.run(
            ["/usr/bin/say", "-o", str(path), phrase], check=True, timeout=30
        )
    elif shutil.which("flite"):
        # espeak-ng's formant voice runs "Jane Example" together for tiny.en.
        path = tmp_path / "fictional.wav"
        subprocess.run(
            ["flite", "-voice", "slt", "-t", phrase, "-o", str(path)],
            check=True,
            timeout=30,
        )
    else:
        pytest.skip("native test speech synthesizer unavailable")
    original = path.read_bytes()
    document = DesktopController(data).import_audio(path)
    # Depending on the voice, tiny.en may run the name together ("Jane an
    # example"). Register the first name as heard; transcription quality is
    # separate from masking.
    found = re.search(r"\bJane\b", document.text, flags=re.IGNORECASE)
    assert found, document.text
    assert 1 < document.seconds < 30
    assert path.read_bytes() == original
    name = found.group()
    from veil import Shield

    shield = Shield()
    shield.add_entity(name, "PERSON")
    masked = shield.mask(document.text)
    assert "[PERSON_1]" in masked.text
    assert name not in masked.text
    assert shield.restore(masked.text).text == document.text
