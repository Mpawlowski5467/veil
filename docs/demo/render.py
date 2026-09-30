"""Render the captured local demo as a 36-second GIF and shareable MP4.

Requires Pillow and ffmpeg for maintainers only; neither is a Veil dependency.
Run from any folder with: python docs/demo/render.py
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
WIDTH, HEIGHT = 1280, 720
BG = "#111816"
PANEL = "#1B2521"
LINE = "#34433B"
TEXT = "#F2F5EF"
MUTED = "#AEC0B3"
ACCENT = "#C7F391"


def font(size: int, *, bold: bool = False, mono: bool = False):
    """Find a readable system font without bundling someone else's font files."""
    if mono:
        candidates = [
            "/System/Library/Fonts/Menlo.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "C:/Windows/Fonts/consola.ttf",
        ]
    elif bold:
        candidates = [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    raise RuntimeError("Install Arial or DejaVu Sans, plus a monospace font.")


def frame(index: int, title: str, label: str, body: str, note: str, version: str):
    """Draw a fully captioned scene; keep the simulated-reply label visible."""
    canvas = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((64, 32), "veil", font=font(43, bold=True), fill=TEXT)
    draw.text((164, 47), "PRIVATE TEXT. USEFUL REPLIES.", font=font(17), fill=MUTED)
    draw.rounded_rectangle((991, 36, 1216, 76), radius=20, fill=PANEL, outline=LINE)
    draw.text((1010, 45), f"{version}  /  BETA PREVIEW", font=font(16), fill=ACCENT)
    draw.line((64, 101, 1216, 101), fill=LINE)

    title_font = font(48, bold=True)
    assert draw.textlength(title, font=title_font) <= 1152
    draw.text((64, 134), title, font=title_font, fill=TEXT)
    draw.text((66, 208), label, font=font(18, bold=True), fill=ACCENT)

    draw.rounded_rectangle((64, 248, 1216, 520), radius=18, fill=PANEL, outline=LINE)
    draw.rectangle((64, 269, 68, 499), fill=ACCENT)
    body_font = font(30, mono=True)
    lines = body.splitlines()
    top = 384 - len(lines) * 26
    for number, line in enumerate(lines):
        assert draw.textlength(line, font=body_font) <= 1060, line
        draw.text((108, top + number * 52), line, font=body_font, fill=TEXT)

    for number, line in enumerate(note.splitlines()):
        assert draw.textlength(line, font=font(23)) <= 1152, line
        draw.text((66, 551 + number * 32), line, font=font(23), fill=MUTED)

    draw.line((64, 642, 1216, 642), fill=LINE)
    draw.text(
        (64, 667),
        "LOCAL DEMO  /  SIMULATED REPLY  /  NO MODEL CALL",
        font=font(17),
        fill=MUTED,
    )
    for step in range(6):
        left = 967 + step * 34
        draw.rounded_rectangle(
            (left, 674, left + 25, 679),
            radius=2,
            fill=ACCENT if step <= index else LINE,
        )
    draw.text((1180, 661), f"{index + 1:02}", font=font(22), fill=ACCENT)
    return canvas


def main() -> None:
    """Render actual captured output; use ffmpeg only to encode the video."""
    data = json.loads((HERE / "transcript.json").read_text(encoding="utf-8"))
    assert data["network_calls"] == 0
    assert data["reply_source"] == "simulated"
    original, masked, reply, restored = [s["text"] for s in data["stages"]]
    scenes = [
        (
            "Keep the details local.",
            "MASK BEFORE THE CALL. RESTORE AFTER THE REPLY.",
            "jane.doe@example.com\n"
            "          -> [EMAIL_1]\n"
            "          -> jane.doe@example.com",
            "Real Veil masking and restoration, using fictional contact details.\n"
            "This walkthrough uses a simulated reply so anyone can try it.",
        ),
        (
            "Start with a fictional draft.",
            "01 / YOUR ORIGINAL TEXT",
            original,
            "Jane Doe is registered explicitly.\n"
            "Email and supported phone detection are built in.",
        ),
        (
            "Veil replaces detected values.",
            "02 / MASKED TEXT FOR A MODEL",
            masked,
            "A real integration sends this masked text to the provider.\n"
            "The original values stay in your local mapping.",
        ),
        (
            "The reply keeps the placeholders.",
            "03 / SIMULATED MODEL REPLY",
            reply,
            "This is a fixed example reply, not a live AI response.\n"
            "No API key or provider account is needed for this demo.",
        ),
        (
            "Get the original details back.",
            "04 / REPLY RESTORED LOCALLY",
            restored,
            "Veil restores the placeholders using the same local mapping.\n"
            "Seeing original values in the restored reply is expected.",
        ),
        (
            "Try it in five minutes.",
            "GITHUB.COM/MPAWLOWSKI5467/VEIL",
            "1. Install Veil 0.5.0\n"
            "2. Run the local demo\n"
            "3. Verify a real client request (optional)",
            "macOS / Linux / Windows  |  Python 3.10+\n"
            "Use fictional data. Detection has limits; names need registration.",
        ),
    ]
    frames = [frame(i, *scene, data["version"]) for i, scene in enumerate(scenes)]
    ASSETS.mkdir(exist_ok=True)
    frames[0].save(
        ASSETS / "veil-demo.gif",
        save_all=True,
        append_images=frames[1:],
        duration=6000,
        loop=0,
        optimize=True,
    )
    with tempfile.TemporaryDirectory(prefix="veil-demo-frames-") as temporary:
        directory = Path(temporary)
        for number, scene in enumerate(frames):
            scene.save(directory / f"frame-{number:02}.png")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                "1/6",
                "-i",
                str(directory / "frame-%02d.png"),
                "-t",
                "36",
                "-r",
                "24",
                "-c:v",
                "libx264",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(ASSETS / "veil-demo.mp4"),
            ],
            check=True,
        )
    for name in ("veil-demo.gif", "veil-demo.mp4"):
        print(f"{name}: {(ASSETS / name).stat().st_size:,} bytes; 36 seconds")


if __name__ == "__main__":
    main()
