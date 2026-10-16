"""Render the project trailer as a 1080p MP4 and a looping README GIF.

Every masked and restored line is produced by this checkout's Veil when the
trailer renders, using fictional text. Model replies are simulated and nothing
is sent over the network. Pillow and ffmpeg are needed by maintainers only:

    uv run --with "pillow>=10,<13" python docs/demo/trailer.py

Add ``--still 7.5 30`` to write single frames for review instead of video.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from multiprocessing import Pool
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from veil import Shield, __version__

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
MARK = HERE.parents[1] / "src" / "veil" / "assets" / "veil-mark.png"

W, H = 1280, 720  # design units; every position below uses them
OUTPUT = 1.5  # 1920 x 1080 video
SUPERSAMPLE = 2  # draw at twice the size, then reduce, for smooth edges
FPS, GIF_FPS, GIF_WIDTH = 30, 15, 960
FADE = 0.35

BG, PANEL, RAISED, LINE = "#0e1613", "#16211d", "#1c2a25", "#2b3c35"
TEXT, MUTED, AMBER = "#f9fcfa", "#9db1a8", "#f4b860"
TEAL, DEEP, MINT, WELL = "#18745c", "#0e4b3c", "#6fdcb2", "#173a30"
# The desktop window's own light-layout colors.
PAGE, WHITE, INK, SOFT = "#f5f7f6", "#ffffff", "#213c33", "#64776e"
RULE, TINT, FIELD = "#e3e9e6", "#e7f2ed", "#fbfcfb"
TRAFFIC = ("#ff5f57", "#febc2e", "#28c840")

Color = str | tuple[int, int, int]
Style = tuple[str, float, Color]  # face, size, color
Run = tuple[str, str | None]  # text, and the placeholder Veil gave it if private
Box = tuple[float, float, float, float]

CAPTION: Style = ("display", 38, TEXT)
LABEL: Style = ("bold", 13, MUTED)

AVENIR, MENLO = (
    "/System/Library/Fonts/Avenir Next.ttc",
    "/System/Library/Fonts/Menlo.ttc",
)
FACES = {
    "display": [(AVENIR, 2), ("DejaVuSans-Bold", 0)],
    "bold": [(AVENIR, 0), ("DejaVuSans-Bold", 0)],
    "medium": [(AVENIR, 5), ("DejaVuSans", 0)],
    "regular": [(AVENIR, 7), ("DejaVuSans", 0)],
    "mono": [(MENLO, 0), ("DejaVuSansMono", 0)],
    "monobold": [(MENLO, 1), ("DejaVuSansMono-Bold", 0)],
}
DEJAVU = "/usr/share/fonts/truetype/dejavu/{}.ttf"


@cache
def font(face: str, pixels: int) -> ImageFont.FreeTypeFont:
    """Find a system font; the renderer does not bundle font files."""
    for name, index in FACES[face]:
        path = name if name.startswith("/") else DEJAVU.format(name)
        if Path(path).is_file():
            return ImageFont.truetype(path, pixels, index=index)
    raise RuntimeError("Install Avenir Next and Menlo, or DejaVu Sans and Mono.")


@cache
def rgb(color: str) -> tuple[int, int, int]:
    """Turn #rrggbb into a tuple."""
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def clamp(value: float) -> float:
    """Limit a value to 0..1."""
    return max(0.0, min(1.0, value))


def mix(base: Color, color: Color, amount: float) -> tuple[int, int, int]:
    """Blend toward a color; scenes use flat backgrounds, so this is a fade."""
    start = rgb(base) if isinstance(base, str) else base
    end = rgb(color) if isinstance(color, str) else color
    t = clamp(amount)
    r, g, b = (round(a + (z - a) * t) for a, z in zip(start, end, strict=True))
    return r, g, b


def span(t: float, start: float, length: float) -> float:
    """Return linear progress through an interval, from 0 to 1."""
    return clamp((t - start) / length)


def ease(value: float) -> float:
    """Ease in and out (cubic)."""
    v = clamp(value)
    return 4 * v**3 if v < 0.5 else 1 - (-2 * v + 2) ** 3 / 2


def appear(t: float, start: float, length: float = 0.45) -> float:
    """Return eased-out entrance progress for an element."""
    return 1 - (1 - span(t, start, length)) ** 3


def lerp(a: float, b: float, t: float) -> float:
    """Interpolate between two numbers."""
    return a + (b - a) * t


@cache
def mark_alpha(height: int) -> Image.Image:
    """Return the ribbon symbol's coverage, trimmed and resized."""
    with Image.open(MARK) as source:
        alpha = source.convert("RGBA").getchannel("A")
    alpha = alpha.crop(alpha.getbbox())
    width = round(alpha.width * height / alpha.height)
    return alpha.resize((width, height), Image.Resampling.LANCZOS)


class Frame:
    """One frame, drawn in design units at any pixel scale.

    Drawing calls take ``a`` (opacity) and ``on`` (the flat color beneath), so
    an element fades in by blending toward its color.
    """

    def __init__(self, scale: float) -> None:
        """Start a blank frame."""
        self.k = scale
        size = (round(W * scale), round(H * scale))
        self.image = Image.new("RGB", size, rgb(BG))
        self.draw = ImageDraw.Draw(self.image)

    def px(self, value: float) -> int:
        """Convert design units to pixels."""
        return round(value * self.k)

    def face(self, face: str, size: float) -> ImageFont.FreeTypeFont:
        """Return a font at a design-unit size."""
        return font(face, max(1, self.px(size)))

    def width(self, value: str, face: str, size: float) -> float:
        """Measure text in design units."""
        return self.face(face, size).getlength(value) / self.k

    def text(
        self,
        x: float,
        y: float,
        value: str,
        style: Style,
        *,
        a: float = 1.0,
        on: Color = BG,
        anchor: str = "ls",
    ) -> None:
        """Draw text; the default anchor is the left end of the baseline."""
        face, size, color = style
        if a > 0 and value:
            fill = mix(on, color, a)
            point = (self.px(x), self.px(y))
            self.draw.text(point, value, fill, self.face(face, size), anchor)

    def rect(
        self,
        box: Box,
        radius: float,
        fill: Color | None = None,
        *,
        outline: Color | None = None,
        width: float = 1.5,
        a: float = 1.0,
        on: Color = BG,
        corners: tuple[bool, bool, bool, bool] | None = None,
    ) -> None:
        """Draw a rounded rectangle."""
        if a <= 0:
            return
        x0, y0, x1, y1 = (self.px(v) for v in box)
        self.draw.rounded_rectangle(
            (x0, y0, max(x0, x1), max(y0, y1)),
            self.px(radius),
            None if fill is None else mix(on, fill, a),
            None if outline is None else mix(on, outline, a),
            max(1, self.px(width)),
            corners=corners,
        )

    def line(
        self,
        points: list[tuple[float, float]],
        color: Color,
        width: float,
        *,
        a: float = 1.0,
        on: Color = BG,
    ) -> None:
        """Draw a polyline."""
        if a > 0:
            xy = [(self.px(x), self.px(y)) for x, y in points]
            fill = mix(on, color, a)
            self.draw.line(xy, fill, max(1, self.px(width)), joint="curve")

    def polygon(self, points: list[tuple[float, float]], color: Color) -> None:
        """Draw a filled polygon."""
        xy = [(self.px(x), self.px(y)) for x, y in points]
        self.draw.polygon(xy, mix(BG, color, 1))

    def circle(
        self,
        x: float,
        y: float,
        r: float,
        fill: Color | None = None,
        *,
        outline: Color | None = None,
        width: float = 1.5,
        a: float = 1.0,
        on: Color = BG,
    ) -> None:
        """Draw a circle around a center point."""
        if a > 0:
            self.draw.ellipse(
                (self.px(x - r), self.px(y - r), self.px(x + r), self.px(y + r)),
                None if fill is None else mix(on, fill, a),
                None if outline is None else mix(on, outline, a),
                max(1, self.px(width)),
            )

    def arc(
        self,
        box: Box,
        angles: tuple[float, float],
        color: Color,
        width: float,
        *,
        a: float = 1.0,
        on: Color = BG,
    ) -> None:
        """Draw part of an ellipse outline; angles run clockwise from 3 o'clock."""
        if a > 0:
            x0, y0, x1, y1 = (self.px(v) for v in box)
            fill = mix(on, color, a)
            self.draw.arc((x0, y0, x1, y1), *angles, fill, max(1, self.px(width)))

    def mark(
        self,
        x: float,
        y: float,
        height: float,
        color: Color,
        *,
        a: float = 1.0,
        on: Color = BG,
    ) -> None:
        """Paste the ribbon symbol centered on a point."""
        coverage = mark_alpha(self.px(height))
        if a > 0:
            solid = Image.new("RGB", coverage.size, mix(on, color, a))
            corner = (
                self.px(x) - coverage.width // 2,
                self.px(y) - coverage.height // 2,
            )
            self.image.paste(solid, corner, coverage)


# --- Real Veil output -------------------------------------------------------

PLACEHOLDER = re.compile(r"\[[A-Z0-9_]+_\d+\]")


def masked_lines(shield: Shield, marked: str) -> list[list[Run]]:
    """Mask text with Veil and pair each {private} part with its placeholder.

    Braces mark the parts the trailer expects Veil to mask. Fails if Veil's
    actual output differs from what the trailer would show.
    """
    lines = []
    for line in marked.split("\n"):
        pieces = re.split(r"\{(.*?)\}", line)
        lines.append([(piece, i % 2 == 1) for i, piece in enumerate(pieces) if piece])
    text = "\n".join("".join(part for part, _ in line) for line in lines)
    result = shield.mask(text)
    if result.warnings:
        raise RuntimeError(f"Veil warned while masking {text!r}")
    found = iter(PLACEHOLDER.findall(result.text))
    runs = [
        [(part, next(found, "?") if private else None) for part, private in line]
        for line in lines
    ]
    shown = "\n".join("".join(p or part for part, p in line) for line in runs)
    if shown != result.text:
        raise RuntimeError(f"Veil masked {result.text!r}; trailer shows {shown!r}")
    return runs


def restored_line(shield: Shield, runs: list[Run]) -> list[Run]:
    """Check that Veil restores a simulated reply exactly as the trailer shows."""
    result = shield.restore("".join(p or text for text, p in runs))
    expected = "".join(text for text, _ in runs)
    if result.warnings or result.text != expected:
        raise RuntimeError(f"Veil restored {result.text!r}; expected {expected!r}")
    return runs


@dataclass(frozen=True)
class Story:
    """Every line of real Veil output the trailer shows."""

    hero: list[list[Run]]
    message: list[Run]
    reply: list[Run]
    code: list[list[Run]]
    preview: list[list[Run]]


@cache
def story() -> Story:
    """Run Veil on the fictional text; each worker process does this once."""
    shield = Shield(redact_warnings=True)
    shield.add_entity("Jan Nowak", "PERSON")
    hero = masked_lines(
        shield,
        "Reply to {Jan Nowak} at {jan.n@example.com}\n"
        "and confirm the number {+1 202-555-0147}.",
    )
    shield = Shield(redact_warnings=True)  # the README's round trip, from the top
    shield.add_entity("Jan Nowak", "PERSON")
    message = masked_lines(shield, "Email {Jan Nowak} at {jan.n@example.com}.")[0]
    invoice = ", following up on the invoice..."
    reply = restored_line(shield, [("Hi ", None), message[1], (invoice, None)])
    code = masked_lines(
        Shield(redact_warnings=True),
        'export DB_PASSWORD="{fictional-orchard-42}"\n'
        'API_KEY="{fictional-example-key}"\n'
        'curl -H "Authorization: Bearer {fictional-token-0001}" api.example.com',
    )
    preview = masked_lines(
        Shield(redact_warnings=True),
        "Please send the draft to {jane@example.com}\nbefore Friday.",
    )
    return Story(hero, message, reply, code, preview)


# --- Building blocks --------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    """Colors for text holding private values or placeholder chips."""

    plain: str
    private: str
    chip: str
    chip_text: str


DARK = Palette(TEXT, AMBER, TEAL, "#effff8")
LIGHT = Palette(INK, "#b4691f", "#d4ebe0", TEAL)


def runs_line(
    f: Frame,
    x: float,
    baseline: float,
    runs: list[Run],
    size: float,
    *,
    morph: Callable[[float, float], float] | float = 0.0,
    highlight: float = 0.0,
    a: float = 1.0,
    on: Color = BG,
    face: str = "medium",
    palette: Palette = DARK,
) -> float:
    """Draw a line whose private parts can turn into placeholder chips.

    ``morph`` is a fixed progress, or a function of a part's left and right
    edges so a sweep transforms each part as it passes; later parts reflow.
    Returns the line's width.
    """
    chip_style = ("mono", size * 0.76, palette.chip_text)
    pad = size * 0.26
    cursor = x
    for text, placeholder in runs:
        original = f.width(text, face, size)
        if placeholder is None:
            f.text(cursor, baseline, text, (face, size, palette.plain), a=a, on=on)
            cursor += original
            continue
        chip = f.width(placeholder, "mono", size * 0.76) + 2 * pad
        p = ease(morph(cursor, cursor + original) if callable(morph) else morph)
        current = lerp(original, chip, p)
        top, bottom = baseline - size * 0.8, baseline + size * 0.24
        fill = mix(on, palette.chip, p * a)
        if p > 0:
            f.rect((cursor, top, cursor + current, bottom), size * 0.2, fill)
            middle = (cursor + current / 2, (top + bottom) / 2)
            f.text(*middle, placeholder, chip_style, a=p * a, on=fill, anchor="mm")
        if p < 1:
            style = (face, size, mix(palette.plain, palette.private, highlight))
            f.text(cursor, baseline, text, style, a=(1 - p) * a, on=fill)
            if highlight > 0:
                y = baseline + size * 0.18
                under = [(cursor, y), (cursor + original * highlight, y)]
                f.line(under, palette.private, size * 0.06, a=(1 - p) * a, on=on)
        cursor += current
    return cursor - x


class Sweep:
    """The slanted teal ribbon that passes over text, like the logo's fold."""

    skew = 34.0

    def __init__(self, t: float, start: float, length: float, x0: float, x1: float):
        """Describe a sweep from x0 to x1 during [start, start + length]."""
        self.progress = span(t, start, length)
        self.x = lerp(x0, x1, ease(self.progress))

    def morph(
        self, y: float, top: float, bottom: float
    ) -> Callable[[float, float], float]:
        """Return a morph function: a part changes while the ribbon crosses it."""
        center = self.x + self.skew - 2 * self.skew * (y - top) / (bottom - top)
        if self.progress >= 1:
            return lambda left, right: 1.0
        if self.progress <= 0:
            return lambda left, right: 0.0
        return lambda left, right: clamp((center - left) / max(right - left, 1.0))

    def draw(self, f: Frame, top: float, bottom: float, width: float = 60) -> None:
        """Draw the ribbon while it is moving."""
        if not 0 < self.progress < 1:
            return
        x, s, h = self.x, self.skew, width / 2
        band = [(x - h + s, top), (x + h + s, top), (x + h - s, bottom)]
        f.polygon([*band, (x - h - s, bottom)], TEAL)
        fold = [(x - h + s, top), (x - h + s + 9, top), (x - h - s + 9, bottom)]
        f.polygon([*fold, (x - h - s, bottom)], DEEP)
        f.line([(x + h + s, top), (x + h - s, bottom)], MINT, 3)


def caption(f: Frame, t: float, lines: list[tuple[float, float, str]]) -> None:
    """Show scene titles one after another, rising slightly as they appear."""
    for start, end, value in lines:
        if f.width(value, "display", 38) > W - 100:
            raise RuntimeError(f"Caption too wide: {value!r}")
        a = ease(span(t, start, 0.4)) * (1 - ease(span(t, end - 0.3, 0.3)))
        rise = (1 - appear(t, start, 0.5)) * 12
        f.text(W / 2, 118 + rise, value, CAPTION, a=a, anchor="ms")


def note(
    f: Frame, t: float, start: float, y: float, value: str, size: float = 21
) -> None:
    """Show one muted, centered line."""
    if f.width(value, "regular", size) > W - 100:
        raise RuntimeError(f"Line too wide: {value!r}")
    style = ("regular", size, MUTED)
    f.text(W / 2, y, value, style, a=appear(t, start), anchor="ms")


def dashed(f: Frame, box: Box, color: str, a: float) -> None:
    """Draw a dashed rectangle outline."""
    x0, y0, x1, y1 = box
    edges = [((x0, y0), (1, 0), x1 - x0), ((x0, y1), (1, 0), x1 - x0)]
    edges += [((x0, y0), (0, 1), y1 - y0), ((x1, y0), (0, 1), y1 - y0)]
    for (x, y), (dx, dy), length in edges:
        for start in range(0, round(length), 17):
            end = min(start + 10, length)
            points = [(x + dx * start, y + dy * start), (x + dx * end, y + dy * end)]
            f.line(points, color, 1.5, a=a)


# Line icons in units of their size, centered on (0, 0); drawn here, so no
# artwork needs licensing. Arc angles run clockwise from 3 o'clock.
ICONS: dict[str, list[tuple[str | float, ...]]] = {
    "person": [("ring", 0, -0.17, 0.17), ("arc", -0.34, 0.06, 0.34, 0.66, 180, 360)],
    "terminal": [
        ("box", -0.44, -0.34, 0.44, 0.34, 0.08),
        ("line", -0.25, -0.12, -0.09, 0.01, -0.25, 0.14),
        ("line", -0.01, 0.16, 0.22, 0.16),
    ],
    "braces": [("text", "{ }")],
    "chat": [
        ("box", -0.42, -0.34, 0.42, 0.2, 0.12),
        ("line", -0.18, 0.2, -0.26, 0.38, 0, 0.2),
    ],
    "window": [
        ("box", -0.44, -0.34, 0.44, 0.34, 0.08),
        ("line", -0.44, -0.14, 0.44, -0.14),
        ("dot", -0.32, -0.24, 0.035),
        ("dot", -0.21, -0.24, 0.035),
        ("dot", -0.1, -0.24, 0.035),
    ],
    "mic": [
        ("box", -0.13, -0.42, 0.13, 0.06, 0.13),
        ("arc", -0.28, -0.22, 0.28, 0.24, 0, 180),
        ("line", 0, 0.24, 0, 0.4),
        ("line", -0.14, 0.4, 0.14, 0.4),
    ],
    "scan": [
        ("line", -0.4, -0.18, -0.4, -0.4, -0.18, -0.4),
        ("line", 0.18, -0.4, 0.4, -0.4, 0.4, -0.18),
        ("line", 0.4, 0.18, 0.4, 0.4, 0.18, 0.4),
        ("line", -0.18, 0.4, -0.4, 0.4, -0.4, 0.18),
        ("line", -0.26, 0, 0.26, 0),
    ],
    "pdf": [
        ("line", -0.3, -0.4, 0.12, -0.4, 0.3, -0.22, 0.3, 0.4, -0.3, 0.4, -0.3, -0.4),
        ("line", -0.16, -0.08, 0.16, -0.08),
        ("line", -0.16, 0.06, 0.16, 0.06),
        ("line", -0.16, 0.2, 0.16, 0.2),
    ],
    "plug": [
        ("ring", -0.28, 0, 0.13),
        ("ring", 0.28, 0, 0.13),
        ("line", -0.15, 0, 0.15, 0),
    ],
    "power": [("arc", -0.34, -0.3, 0.34, 0.38, 300, 240), ("line", 0, -0.42, 0, -0.02)],
    "cloud": [
        ("dot", -0.2, 0.05, 0.2),
        ("dot", 0.05, -0.1, 0.26),
        ("dot", 0.27, 0.07, 0.18),
        ("fill", -0.38, 0.02, 0.44, 0.25, 0.11),
    ],
    "check": [("line", -0.3, 0, -0.08, 0.22, 0.32, -0.22)],
}


def icon(
    f: Frame,
    name: str,
    x: float,
    y: float,
    s: float,
    color: str,
    *,
    a: float = 1.0,
    on: Color = BG,
) -> None:
    """Draw a named line icon of size s centered on (x, y)."""
    width = max(1.6, s * 0.075)
    for kind, *values in ICONS[name]:
        if kind == "text":
            style = ("monobold", s * 0.62, color)
            f.text(x, y, str(values[0]), style, a=a, on=on, anchor="mm")
            continue
        v = [
            x + float(n) * s if i % 2 == 0 else y + float(n) * s
            for i, n in enumerate(values)
        ]
        if kind == "line":
            f.line(list(zip(v[::2], v[1::2], strict=True)), color, width, a=a, on=on)
        elif kind == "box":
            radius = float(values[4]) * s
            f.rect(
                (v[0], v[1], v[2], v[3]), radius, outline=color, width=width, a=a, on=on
            )
        elif kind == "fill":
            f.rect((v[0], v[1], v[2], v[3]), float(values[4]) * s, color, a=a, on=on)
        elif kind == "ring":
            r = float(values[2]) * s
            f.circle(v[0], v[1], r, outline=color, width=width, a=a, on=on)
        elif kind == "dot":
            f.circle(v[0], v[1], float(values[2]) * s, color, a=a, on=on)
        elif kind == "arc":
            angles = (float(values[4]), float(values[5]))
            f.arc((v[0], v[1], v[2], v[3]), angles, color, width, a=a, on=on)


def chrome(f: Frame, box: Box, bar: str, height: float, a: float, on: Color) -> None:
    """Draw a window title bar with its three buttons."""
    x0, y0, x1, _ = box
    top = (True, True, False, False)
    f.rect((x0, y0, x1, y0 + height), 14, bar, a=a, on=on, corners=top)
    for index, color in enumerate(TRAFFIC):
        f.circle(x0 + 22 + index * 19, y0 + height / 2, 5.5, color, a=a * 0.9, on=bar)


# --- Scenes -----------------------------------------------------------------


def hero(f: Frame, t: float) -> None:
    """A prompt with private details; the ribbon passes and masks them."""
    first = "Every AI prompt can carry private details."
    second = "Veil masks them locally, before the request is sent."
    caption(f, t, [(3.3, 5.6, first), (5.7, 99, second)])
    shown = appear(t, 0.15, 0.6)
    top = 236 + (1 - shown) * 18
    bottom = top + 214
    on = mix(BG, PANEL, shown)
    f.rect((190, top, 1090, bottom), 22, PANEL, outline=LINE, a=shown)
    f.text(226, top + 42, "NEW MESSAGE", LABEL, a=shown, on=on)

    size, x, baselines = 34.0, 226.0, [top + 100, top + 152]
    lines = story().hero
    total = sum(len(text) for line in lines for text, _ in line)
    typed = round(total * span(t, 0.7, 2.3))
    sweep = Sweep(t, 6.0, 1.6, 150, 1150)
    highlight = ease(span(t, 3.4, 0.6))
    caret = (x, baselines[0])
    for line, baseline in zip(lines, baselines, strict=True):
        full = "".join(text for text, _ in line)
        if typed < len(full):
            f.text(x, baseline, full[:typed], ("medium", size, TEXT), a=shown, on=on)
            caret = (x + f.width(full[:typed], "medium", size), baseline)
            break
        morph = sweep.morph(baseline - 12, top - 14, bottom + 14)
        width = runs_line(
            f, x, baseline, line, size, morph=morph, highlight=highlight, on=on
        )
        caret = (x + width, baseline)
        typed -= len(full)
    if t < 3.4 and (t < 3.0 or (t * 2) % 1 < 0.5):
        cx, cy = caret[0] + 3, caret[1]
        f.line([(cx, cy - 30), (cx, cy + 6)], MINT, 2.5)
    sweep.draw(f, top - 14, bottom + 14)

    # The send button pulses once the text is masked.
    bx, by = 1046.0, bottom - 38
    f.circle(bx, by, 21, TEAL, a=shown, on=on)
    arrow = [(bx - 8, by - 1), (bx, by - 9), (bx + 8, by - 1)]
    f.line([(bx, by + 9), (bx, by - 9)], TEXT, 2.5, a=shown, on=TEAL)
    f.line(arrow, TEXT, 2.5, a=shown, on=TEAL)
    pulse = span(t, 8.3, 0.9)
    if 0 < pulse < 1:
        ring = 21 + 22 * appear(pulse, 0, 1)
        f.circle(bx, by, ring, outline=MINT, width=2, a=1 - pulse, on=on)
    mapping = (
        "Detected values become placeholders. The originals stay in a local mapping."
    )
    note(f, t, 8.0, bottom + 62, mapping)


def node(
    f: Frame, x: float, y: float, kind: str, title: str, detail: str, a: float
) -> None:
    """Draw one station in the round-trip diagram."""
    y += (1 - a) * 14
    on = mix(BG, RAISED, a)
    edge = TEAL if kind == "veil" else LINE
    f.rect((x - 104, y - 52, x + 104, y + 52), 16, RAISED, outline=edge, width=2, a=a)
    if kind == "veil":
        f.mark(x - 66, y, 34, TEXT, a=a, on=on)
    else:
        icon(f, kind, x - 66, y, 40, MINT, a=a, on=on)
    f.text(x - 34, y - 3, title, ("bold", 22, TEXT), a=a, on=on)
    f.text(x - 34, y + 22, detail, ("regular", 15, MUTED), a=a, on=on)


def round_trip(f: Frame, t: float) -> None:
    """Follow one message to the provider and back."""
    first = "The model works with placeholders."
    second = "Veil restores the originals in the reply."
    caption(f, t, [(0.3, 4.1, first), (4.2, 99, second)])
    region = appear(t, 0.1, 0.5)
    dashed(f, (130, 214, 742, 386), LINE, region)
    label = "ON YOUR COMPUTER"
    f.rect((152, 206, 170 + f.width(label, "bold", 13), 222), 0, BG)
    f.text(161, 214, label, ("bold", 13, MINT), a=region, anchor="lm")

    you, veil, cloud, y = 270.0, 602.0, 1010.0, 300.0
    links = [(you + 104, veil - 104), (veil + 104, cloud - 104)]
    for start, end in links:
        f.line([(start, y), (end, y)], LINE, 2, a=appear(t, 0.5))
    node(f, you, y, "person", "You", "your text", appear(t, 0.15))
    node(f, veil, y, "veil", "Veil", "local gateway", appear(t, 0.3))
    node(f, cloud, y, "cloud", "AI provider", "model API", appear(t, 0.45))
    sees, style = "receives placeholders, not originals", ("medium", 16, MINT)
    f.text(cloud, y + 84, sees, style, a=appear(t, 3.0), anchor="ms")

    out, back = links, [link[::-1] for link in links]
    trips = [(1.0, *out[0], AMBER), (2.1, *out[1], MINT)]
    trips += [(4.2, *back[1], MINT), (5.3, *back[0], AMBER)]
    for start, a, b, color in trips:
        p = span(t, start, 0.8)
        if 0 < p < 1:
            x = lerp(a, b, ease(p))
            f.circle(x, y, 13, color, a=0.25)
            f.circle(x, y, 7, color)
    for start, x in ((1.8, veil), (2.9, cloud), (5.0, veil), (6.1, you)):
        pulse = span(t, start, 0.6)
        if 0 < pulse < 1:
            gx, gy = 104 + 10 * pulse, 52 + 10 * pulse
            f.rect(
                (x - gx, y - gy, x + gx, y + gy), 18, outline=MINT, width=2, a=1 - pulse
            )

    s = story()
    stages = [
        (0.4, "YOUR TEXT", AMBER, s.message, 0.0),
        (1.85, "SENT TO THE MODEL", MINT, s.message, 1.0),
        (3.95, "SIMULATED MODEL REPLY", MINT, s.reply, 1.0),
        (5.05, "RESTORED ON YOUR COMPUTER", AMBER, s.reply, 0.0),
    ]
    f.rect((150, 430, 1130, 574), 20, PANEL, outline=LINE, a=appear(t, 0.4))
    current = [stage for stage in stages if t >= stage[0]]
    if current:
        start, eyebrow, color, runs, morph = current[-1]
        a = ease(span(t, start, 0.3))
        f.text(186, 474, eyebrow, ("bold", 14, color), a=a, on=PANEL)
        runs_line(f, 186, 532, runs, 32, morph=morph, highlight=1.0, a=a, on=PANEL)


def secrets(f: Frame, t: float) -> None:
    """Credentials in a script are masked as the ribbon passes."""
    caption(f, t, [(0.2, 99, "Coding secrets, too.")])
    kinds = "API keys, tokens, labelled passwords, private keys and URL credentials"
    note(f, t, 0.45, 166, f"Supported {kinds}.")
    shown = appear(t, 0.3, 0.5)
    rise = (1 - shown) * 16
    box = (150, 214 + rise, 1130, 484 + rise)
    top, bottom = box[1], box[3]
    on = mix(BG, PANEL, shown)
    f.rect(box, 18, PANEL, outline=LINE, a=shown)
    chrome(f, box, RAISED, 40, shown, on)
    name = ("medium", 15, MUTED)
    f.text(640, top + 21, "deploy.sh", name, a=shown, on=RAISED, anchor="mm")

    sweep = Sweep(t, 2.4, 1.4, 130, 1150)
    highlight = ease(span(t, 1.5, 0.6))
    for index, line in enumerate(story().code):
        baseline = top + 100 + index * 54
        a = appear(t, 0.6 + index * 0.25)
        number = ("mono", 19, MUTED)
        f.text(196, baseline, str(index + 1), number, a=a * 0.7, on=on, anchor="rs")
        morph = sweep.morph(baseline - 8, top + 46, bottom - 6)
        kw = {"morph": morph, "highlight": highlight, "a": a, "on": on}
        runs_line(f, 222, baseline, line, 21, face="mono", **kw)
    sweep.draw(f, top + 46, bottom - 6)
    restored = "Placeholders go to the model. Exact values are restored locally."
    note(f, t, 4.0, 546, restored)


def workflows(f: Frame, t: float) -> None:
    """Where Veil fits: two launchers, the library, and any chat."""
    caption(f, t, [(0.2, 99, "Works with the tools you already use.")])
    tiles = [
        ("terminal", "Claude Code", "veil claude", ""),
        ("terminal", "Codex CLI", "veil codex", "EXPERIMENTAL"),
        ("braces", "Python", "Shield().mask(text)", ""),
        ("chat", "Any chat app", "veil mask / restore", ""),
    ]
    for index, (kind, title, detail, tag) in enumerate(tiles):
        a = appear(t, 0.4 + index * 0.12)
        x, y = 101 + index * 276, 176 + (1 - a) * 16
        on = mix(BG, PANEL, a)
        f.rect((x, y, x + 250, y + 192), 18, PANEL, outline=LINE, a=a)
        f.rect((x + 24, y + 24, x + 78, y + 78), 14, WELL, a=a, on=on)
        icon(f, kind, x + 51, y + 51, 34, MINT, a=a, on=mix(on, WELL, a))
        f.text(x + 24, y + 124, title, ("bold", 23, TEXT), a=a, on=on)
        f.text(x + 24, y + 160, detail, ("mono", 16, MINT), a=a, on=on)
        if tag:
            pill = (x + 210 - f.width(tag, "bold", 11), y + 36, x + 226, y + 60)
            f.rect(pill, 12, outline=AMBER, width=1.2, a=a, on=on)
            style = ("bold", 11, AMBER)
            f.text(x + 218, y + 48, tag, style, a=a, on=on, anchor="rm")

    strip = appear(t, 1.2)
    f.rect((101, 400, 1179, 494), 18, PANEL, outline=LINE, a=strip)
    command = "$ veil verify"
    typed = command[: round(len(command) * span(t, 1.5, 0.6))]
    f.text(132, 456, typed, ("mono", 22, TEXT), a=strip, on=PANEL)
    arrow = appear(t, 2.4)
    f.line([(340, 448), (382, 448)], MUTED, 2, a=arrow, on=PANEL)
    f.line([(372, 440), (382, 448), (372, 456)], MUTED, 2, a=arrow, on=PANEL)
    chip = appear(t, 2.6)
    chip_on = mix(PANEL, MINT, chip)
    f.rect((404, 428, 548, 468), 20, MINT, a=chip, on=PANEL)
    icon(f, "check", 428, 448, 22, BG, a=chip, on=chip_on)
    f.text(446, 449, "verified", ("bold", 19, BG), a=chip, on=chip_on, anchor="lm")
    result = "One exchange was masked, forwarded and restored."
    f.text(576, 455, result, ("regular", 20, MUTED), a=appear(t, 2.9), on=PANEL)
    scope = "Verification covers that exchange, not every future message."
    note(f, t, 3.4, 548, scope)


def desktop_window(f: Frame, t: float) -> None:
    """A drawing of the desktop masking preview, showing real Veil output."""
    a = appear(t, 0.3, 0.6)
    if a <= 0:
        return
    dy = (1 - a) * 26
    x0, y0, x1, y1 = 70, 196 + dy, 640, 604 + dy
    f.rect((x0 + 6, y0 + 10, x1 + 6, y1 + 10), 16, "#080d0b", a=a)
    f.rect((x0, y0, x1, y1), 14, PAGE, a=a)
    chrome(f, (x0, y0, x1, y1), "#e9eeeb", 30, a, PAGE)
    bottom_left = (False, False, False, True)
    f.rect((x0, y0 + 30, x0 + 150, y1), 14, WHITE, a=a, corners=bottom_left)
    f.line([(x0 + 150, y0 + 30), (x0 + 150, y1)], RULE, 1, a=a, on=PAGE)
    f.mark(x0 + 30, y0 + 62, 22, INK, a=a, on=WHITE)
    f.text(x0 + 46, y0 + 70, "veil", ("bold", 22, INK), a=a, on=WHITE)
    tagline = ("regular", 10, SOFT)
    f.text(x0 + 18, y0 + 92, "Your privacy workspace", tagline, a=a, on=WHITE)
    for index, page in enumerate(("Overview", "Masking preview", "Settings")):
        y = y0 + 128 + index * 32
        style, on = ("medium", 12, INK), WHITE
        if page == "Masking preview":
            style, on = ("bold", 12, TEAL), TINT
            f.rect((x0 + 10, y - 13, x0 + 140, y + 13), 8, TINT, a=a, on=WHITE)
        f.text(x0 + 20, y, page, style, a=a, on=on, anchor="lm")
    f.text(x0 + 18, y1 - 42, "LOCAL CONNECTION", ("bold", 8.5, SOFT), a=a, on=WHITE)
    f.circle(x0 + 22, y1 - 26, 3.5, TEAL, a=a, on=WHITE)
    f.text(x0 + 32, y1 - 22, "Gateway ready", ("medium", 11, TEAL), a=a, on=WHITE)

    cx = x0 + 168
    about = "Try your configured rules before sharing. Nothing sent to a provider."
    f.text(cx, y0 + 58, "LOCAL TOOLS", ("bold", 9.5, TEAL), a=a, on=PAGE)
    f.text(cx, y0 + 84, "Masking preview", ("bold", 20, INK), a=a, on=PAGE)
    f.text(cx, y0 + 102, about, ("regular", 10.5, SOFT), a=a, on=PAGE)
    result = appear(t, 2.6, 0.4)
    cards = [
        (116, 128, "01  Original text", 0.0, a),
        (258, 128, "02  Masked preview", 1.0, result),
    ]
    for top, height, heading, morph, shown in cards:
        f.rect(
            (cx, y0 + top, x1 - 18, y0 + top + height),
            10,
            WHITE,
            outline=RULE,
            a=a,
            on=PAGE,
        )
        f.text(cx + 14, y0 + top + 22, heading, ("bold", 12, INK), a=a, on=WHITE)
        field = (cx + 14, y0 + top + 34, x1 - 32, y0 + top + 84)
        f.rect(field, 6, FIELD, outline=RULE, a=a, on=WHITE)
        for index, line in enumerate(story().preview):
            baseline = y0 + top + 54 + index * 19
            kw = {"morph": morph, "a": shown, "on": FIELD, "palette": LIGHT}
            runs_line(f, cx + 24, baseline, line, 12, **kw)
    button = DEEP if 2.3 <= t < 2.5 else TEAL
    by = y0 + 222
    f.rect((cx + 14, by - 12, cx + 152, by + 12), 7, button, a=a, on=WHITE)
    label = ("bold", 10.5, WHITE)
    f.text(cx + 26, by, "Preview masking", label, a=a, on=button, anchor="lm")
    f.line([(cx + 124, by), (cx + 137, by)], WHITE, 1.4, a=a, on=button)
    f.line(
        [(cx + 133, by - 4), (cx + 137, by), (cx + 133, by + 4)],
        WHITE,
        1.4,
        a=a,
        on=button,
    )
    footer = "Temporary placeholders · Text stays in memory · Exact restoration checked"
    f.text(cx + 14, y0 + 362, footer, ("regular", 9, SOFT), a=a, on=WHITE)


def candidate(f: Frame, t: float) -> None:
    """What the 1.0 release candidate adds."""
    caption(f, t, [(0.2, 99, "New in the 1.0 release candidate")])
    tag = "UNRELEASED  ·  IN TESTING"
    half = f.width(tag, "bold", 12) / 2 + 14
    a = appear(t, 0.45)
    f.rect((W / 2 - half, 140, W / 2 + half, 166), 13, outline=AMBER, width=1.2, a=a)
    f.text(W / 2, 153, tag, ("bold", 12, AMBER), a=a, anchor="mm")
    desktop_window(f, t)
    features = [
        (
            "window",
            "Desktop and menu-bar controls",
            "Start, stop and preview in one window",
        ),
        ("mic", "Local voice notes", "Transcribed offline, reviewed, then masked"),
        ("scan", "Scanned PDFs and images", "On-device English OCR before masking"),
        ("pdf", "PDF text import", "Selectable text, extracted locally"),
        ("person", "Local name recognition", "Optional spaCy model on your machine"),
        ("plug", "OpenAI and Anthropic modes", "Provider controls in Settings"),
        ("power", "Login startup and recovery", "Opt-in; restarts a crashed gateway"),
    ]
    for index, (kind, title, detail) in enumerate(features):
        a = appear(t, 0.9 + index * 0.2)
        x, y = 690 + (1 - a) * 18, 200 + index * 57
        f.circle(x + 22, y + 22, 21, WELL, a=a)
        icon(f, kind, x + 22, y + 22, 24, MINT, a=a, on=mix(BG, WELL, a))
        f.text(x + 56, y + 18, title, ("bold", 19, TEXT), a=a)
        f.text(x + 56, y + 40, detail, ("regular", 15, MUTED), a=a)
    signing = (
        "The standalone Apple Silicon Mac app is ad-hoc signed, not yet notarized."
    )
    note(f, t, 3.0, 650, signing, size=16)


def proof(f: Frame, t: float) -> None:
    """Evidence, and the limits stated just as plainly."""
    caption(f, t, [(0.2, 99, "Tested in the open. Honest about limits.")])
    count = round(8006 * appear(t, 0.5, 1.3))
    stats = [
        (f"{count:,}", "non-live tests passing", "on Apple Silicon macOS"),
        ("3", "CI platforms:", "macOS, Linux, Windows"),
        ("0", "required dependencies", "in the base library"),
        ("MIT", "open-source", "license"),
    ]
    for index, (value, first, second) in enumerate(stats):
        a = appear(t, 0.4 + index * 0.15)
        x, rise = 220 + index * 280, (1 - a) * 12
        f.text(x, 268 + rise, value, ("display", 60, MINT), a=a, anchor="ms")
        f.text(x, 304, first, ("regular", 17, MUTED), a=a, anchor="ms")
        f.text(x, 326, second, ("regular", 17, MUTED), a=a, anchor="ms")
    a = appear(t, 1.8)
    on = mix(BG, PANEL, a)
    f.rect((200, 392, 1080, 530), 18, PANEL, outline=mix(PANEL, AMBER, 0.55), a=a)
    f.text(236, 430, "KNOW THE LIMITS", ("bold", 13, AMBER), a=a, on=on)
    limits = [
        "Detection uses patterns and registrations, so it can miss values.",
        "Names need registration. Veil is not a sandbox or a firewall.",
    ]
    for index, value in enumerate(limits):
        f.text(236, 468 + index * 32, value, ("medium", 20, TEXT), a=a, on=on)


def finale(f: Frame, t: float) -> None:
    """The ribbon reveals the logo; then where to try it."""
    height, size, gap, y = 116.0, 128.0, 34.0, 262.0
    word = f.width("veil", "display", size)
    mark = mark_alpha(f.px(height)).width / f.k
    left = W / 2 - (mark + gap + word) / 2
    f.mark(left + mark / 2, y, height, TEXT)
    f.text(left + mark + gap, y + 4, "veil", ("display", size, TEXT), anchor="lm")
    sweep = Sweep(t, 0.15, 1.1, left - 80, left + mark + gap + word + 120)
    if sweep.progress < 1:
        f.rect((sweep.x, 170, W, 360), 0, BG)  # not revealed yet
    sweep.draw(f, 168, 362, width=70)

    tagline = "Mask private values before an AI call. Restore them in the reply."
    note(f, t, 1.3, 410, tagline, 27)
    a = appear(t, 1.7)
    url = "github.com/Mpawlowski5467/veil"
    half = f.width(url, "mono", 22) / 2 + 26
    pill = (W / 2 - half, 446, W / 2 + half, 498)
    f.rect(pill, 26, PANEL, outline=TEAL, width=2, a=a)
    f.text(W / 2, 473, url, ("mono", 22, MINT), a=a, on=mix(BG, PANEL, a), anchor="mm")
    status = "Try the 0.6.0b2 beta today  ·  1.0.0rc1 in testing  ·  Python 3.10+"
    note(f, t, 2.1, 548, status, 18)


TIMELINE: list[tuple[float, Callable[[Frame, float], None]]] = [
    (11.0, hero),
    (8.6, round_trip),
    (6.6, secrets),
    (6.6, workflows),
    (9.4, candidate),
    (5.6, proof),
    (6.6, finale),
]
DURATION = sum(length for length, _ in TIMELINE)


def scene_at(seconds: float) -> tuple[Callable[[Frame, float], None], float, float]:
    """Return the scene playing at a moment, its local time, and its length."""
    start = 0.0
    for length, scene in TIMELINE:
        if seconds < start + length:
            return scene, seconds - start, length
        start += length
    length, scene = TIMELINE[-1]
    return scene, length, length


def render(seconds: float) -> Image.Image:
    """Draw the frame at a moment in the trailer."""
    scene, elapsed, length = scene_at(seconds)
    f = Frame(OUTPUT * SUPERSAMPLE)
    scene(f, elapsed)
    fade = ease(min(clamp(elapsed / FADE), clamp((length - elapsed) / FADE)))
    if fade < 1:
        empty = Image.new("RGB", f.image.size, rgb(BG))
        f.image = Image.blend(empty, f.image, fade)
        f.draw = ImageDraw.Draw(f.image)
    # Keep the demo's boundaries on screen the whole time.
    label = f"FICTIONAL DATA  ·  REAL VEIL {__version__} MASKING"
    f.text(40, 694, f"{label}  ·  SIMULATED MODEL REPLY", ("bold", 11, MUTED), a=0.75)
    return f.image.reduce(SUPERSAMPLE)


def frame_bytes(index: int) -> bytes:
    """Render one video frame as raw RGB."""
    return render(index / FPS).tobytes()


def ffmpeg(*arguments: str, feed: bool = False) -> subprocess.Popen[bytes]:
    """Start ffmpeg quietly, optionally reading raw frames from stdin."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments]
    return subprocess.Popen(command, stdin=subprocess.PIPE if feed else None)


def encode(video: Path, gif: Path) -> None:
    """Stream frames into an MP4 and a lossless GIF source, then build the GIF."""
    size = f"{round(W * OUTPUT)}x{round(H * OUTPUT)}"
    raw = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", size]
    with tempfile.TemporaryDirectory(prefix="veil-trailer-") as temporary:
        source = Path(temporary) / "gif.mkv"
        palette = Path(temporary) / "palette.png"
        mp4 = ffmpeg(
            *raw, "-r", str(FPS), "-i", "-",
            "-c:v", "libx264", "-preset", "slow", "-tune", "animation", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(video),
            feed=True,
        )  # fmt: skip
        small = ffmpeg(
            *raw, "-r", str(GIF_FPS), "-i", "-",
            "-vf", f"scale={GIF_WIDTH}:-2:flags=lanczos", "-c:v", "ffv1", str(source),
            feed=True,
        )  # fmt: skip
        if mp4.stdin is None or small.stdin is None:
            raise RuntimeError("ffmpeg did not open its input.")
        with Pool() as pool:
            count = round(DURATION * FPS)
            for index, data in enumerate(pool.imap(frame_bytes, range(count), 4)):
                mp4.stdin.write(data)
                if index % (FPS // GIF_FPS) == 0:
                    small.stdin.write(data)
        mp4.stdin.close()
        small.stdin.close()
        dither = "paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
        steps = [
            ["-i", str(source), "-vf", "palettegen=max_colors=128", str(palette)],
            ["-i", str(source), "-i", str(palette), "-lavfi", dither, str(gif)],
        ]
        if mp4.wait() or small.wait() or any(ffmpeg(*step).wait() for step in steps):
            raise SystemExit("ffmpeg could not encode the trailer.")


def main() -> None:
    """Render stills for review, or the MP4 and GIF into docs/assets."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--still", type=float, nargs="+", metavar="SECONDS")
    parser.add_argument("--out", type=Path, default=ASSETS)
    args = parser.parse_args()
    story()  # stop before rendering if Veil's output no longer matches
    args.out.mkdir(parents=True, exist_ok=True)
    if args.still:
        for seconds in args.still:
            path = args.out / f"trailer-{seconds:05.2f}s.png"
            render(seconds).save(path)
            print(path)
        return
    if shutil.which("ffmpeg") is None:
        raise SystemExit("Install ffmpeg to encode the trailer.")
    video, gif = args.out / "veil-trailer.mp4", args.out / "veil-trailer.gif"
    encode(video, gif)
    for path in (video, gif):
        print(f"{path.name}: {path.stat().st_size:,} bytes; {DURATION:.1f} seconds")


if __name__ == "__main__":
    main()
