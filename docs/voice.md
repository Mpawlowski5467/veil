# Local voice transcription (unreleased)

This checkout adds audio-file import and microphone recording to the 1.0 plan.
These features are not in the published 0.6.0b2 package. Audio becomes editable
English text, which you review and mask using your existing Veil rules.

## Install and set up

```bash
python -m pip install '.[desktop,voice]'
veil voice setup
veil desktop
```

Alternatively, open **Settings → Voice transcription on this device → Download
voice model**. This explicit action downloads about 75 MB of public model assets
from [Systran's faster-whisper-tiny.en repository](https://huggingface.co/Systran/faster-whisper-tiny.en).
Veil pins revision `0d3d19a32d3338f10357c0889762bd8d64bbdeba` and checks each file's
size and SHA-256 hash. Partial downloads are discarded. Existing incomplete or
modified installations are refused rather than overwritten.

The files live in your selected Veil data folder's `voice-model` directory.
`veil voice status` reports whether the expected model files are installed;
transcription verifies their hashes before loading them. The global
`--data-dir PATH` selects the same model, settings, and sessions for CLI commands.
For CLI-only installation, use `'.[voice]'`.

Setup needs an Internet connection. **Transcription runs locally on the CPU and
does not download models or send audio to a provider.** It uses
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) with an existing local
model and offline mode. The optional runtime includes audio decoding through
PyAV and microphone capture through sounddevice. The base package remains free
of runtime dependencies; ordinary text masking does not import these libraries.
Linux microphone capture also needs the system PortAudio library (for example,
`libportaudio2` on Debian/Ubuntu). The OS may request microphone permission when
you first press Record. Permit the Python/Veil launcher you are actually using.

## Desktop audio and microphone

In **Masking preview**:

1. Choose **Import file… → Audio recording…** for a local recording, or **Record voice…** to start
   the microphone. A timer and recording notice remain visible while recording.
2. Choose **Stop & transcribe** when finished, or **Discard** to stop and forget
   the capture. Recording stops automatically after five minutes. Closing the
   window stops and discards an active capture, including one still opening.
3. Review the editable transcript in **Your original text**. Correct names,
   spelling, capitalization, numbers, and dictated email addresses.
4. Choose **Preview masking**, then review the masked output before sharing it.

The logo loader animates during transcription; microphone capture stops before
transcription begins. Capture stays in memory and is never written as a raw
recording by Veil. Discard, errors, and completion release its buffers; this is
not a guarantee of secure memory erasure or control over OS swap/crash dumps.
The preview and temporary mappings stay in memory. **Clear** removes text and
undo history. Cancelling a file picker preserves the current preview. Errors
preserve the previous input and clear the output. Imported source files remain
unchanged. Saved-session CLI workflows below support later reply restoration.

## CLI

```bash
veil mask --session voice-notes --audio recording.m4a > masked-notes.txt
veil mask --session dictated-notes --record 15 > masked-dictation.txt
veil restore --session voice-notes < copied-reply.txt > restored-reply.txt
```

`--record SECONDS` explicitly opens the default microphone for 1–300 seconds.
Ctrl-C during capture discards it. `--audio` and `--record` replace stdin for
`mask`; they cannot be combined with each other, `--pdf`, `--ocr`, or
`--clipboard`. No microphone opens during startup, status, setup, ordinary text
masking, or audio-file import.

stdout contains only successfully masked text. Recognition summaries and review
reminders go to stderr. Transcription failures, interrupted microphone audio,
and exceeded limits return no partial transcript. Shell redirection can still
create/truncate an output file before Veil runs; choose a separate output path.

Configured patterns, exact registrations, local name recognition, and optional
secret review apply to the **recognized text**. Persistent sessions store detected
original text values in the existing owner-only plaintext vault for restoration;
they do not store raw audio or a full voice transcript. Restoration returns the
recognized spelling, not the original sound. Use one label per conversation and
`veil forget --session voice-notes` to remove its mappings.

## Limits and review

- English initially, using the small `tiny.en` model for local CPU operation.
  It can omit, invent, mishear, or change capitalization of words. Background
  noise, overlapping speakers, accents, music, and silence can cause errors.
- Exact registrations remain case sensitive. A registered `Jane Example` will
  not match a transcript written as `Jane example`; correct it or register that
  recognized variant. Dictated `jane at example dot com` may remain words and
  evade email patterns. Check sensitive details explicitly before sharing.
- Audio-only WAV, MP3, M4A, FLAC, OGG, AAC, AIFF, and WebM through the bundled
  decoder. One audio track is required; video and multi-track inputs are refused.
  No playlists or external references are followed. File extensions are picker
  hints; the decoder checks the content.
- Up to 50 MiB, five minutes of decoded audio, and 32768 transcript characters.
  Stereo is downmixed and audio is resampled to 16 kHz mono. A disposable worker
  has a 180-second outer timeout and a 150-second checked work budget. Limits
  refuse imports instead of silently truncating them; the recording timer's
  explicit five-minute cap ends microphone capture. These are operation limits,
  not a parser sandbox or universal memory ceiling.
- Output is plain masked text. It does not redact the original audio, intercept
  voice chats, synthesize replies, or add automatic gateway audio processing.

Fictional native macOS checks cover synthesized speech, offline transcription,
unchanged files, masking, and restoration of the recognized text. Audio format
decoding and simulated-device tests cover capture, stop, discard, overflow,
duration limits, failed starts, Ctrl-C, and window closure. CI includes optional
runtime checks on all three target OSes and native model tests on macOS/Linux.
Real microphone permission/device journeys on each OS and independent speech
recognition quality measurements remain release work.
