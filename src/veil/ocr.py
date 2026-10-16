"""Recognize document text locally in a disposable, bounded worker process."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import json
import logging
import math
import os
import shutil
import stat
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gateway.config import SettingsError
from .runtime import command

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 25
MAX_PIXELS = 20_000_000
MAX_TEXT = 32_768
TIMEOUT = 120
_WORK_SECONDS = 90
_FORMATS = ("PNG", "JPEG", "TIFF", "WEBP", "BMP")
_ERRORS = {
    "invalid": (
        "Could not scan this file. Choose a valid PDF, PNG, JPEG, TIFF, WebP, or BMP."
    ),
    "encrypted": "Unlock the PDF locally and choose an unencrypted copy.",
    "pages": "OCR accepts 1 to 25 pages or image frames. Choose a smaller document.",
    "pixels": "An image or PDF page exceeds 20 megapixels. Use a smaller copy.",
    "text": "OCR text exceeds 32768 characters. Choose a shorter document.",
    "empty": "OCR found no readable text. Try a clearer, upright scan.",
    "engine": "Local text recognition failed. Check the engine and try a clearer scan.",
    "timeout": "OCR timed out. Choose a smaller or simpler document.",
    "tesseract": "OCR needs Tesseract with English language data installed locally.",
}


@dataclass(frozen=True)
class OCRText:
    """Recognized text and page counts; no original path, images, or metadata."""

    text: str
    pages: int
    empty_pages: int

    @property
    def summary(self) -> str:
        """Keep recognition limitations visible even when scanning succeeds."""
        unit = "page" if self.pages == 1 else "pages"
        message = f"Scanned {self.pages} {unit} locally (English OCR)."
        if self.empty_pages:
            unit = "page" if self.empty_pages == 1 else "pages"
            message += f" {self.empty_pages} {unit} had no recognized text."
        return message + " OCR can miss or misread details; review before sharing."


def _source(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise SettingsError("Choose a regular PDF or image file.")
            if info.st_size > MAX_BYTES:
                raise SettingsError("OCR accepts files up to 20 MiB.")
            source = stream.read(MAX_BYTES + 1)
    except OSError:
        raise SettingsError("Could not read the file. Check its permissions.") from None
    if len(source) > MAX_BYTES:
        raise SettingsError("OCR accepts files up to 20 MiB.")
    return source


def extract_ocr(path: Path) -> OCRText:
    """Read PDF/image pixels without changing the source or calling a provider."""
    required = ["PIL", "pypdf", "pypdfium2"]
    if sys.platform == "darwin":
        required.append("Vision")
    if any(importlib.util.find_spec(name) is None for name in required):
        raise SettingsError("Local OCR needs the optional 'veil[ocr]' extra.")
    if sys.platform != "darwin" and shutil.which("tesseract") is None:
        raise SettingsError(_ERRORS["tesseract"])
    source = _source(path)
    try:
        result = subprocess.run(
            command("ocr"),
            input=source,
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SettingsError(_ERRORS["timeout"]) from None
    except OSError:
        raise SettingsError("Could not start local OCR.") from None
    try:
        payload = json.loads(result.stdout)
        if result.returncode != 0:
            raise SettingsError(_ERRORS.get(payload.get("error"), _ERRORS["invalid"]))
        text, pages, empty = payload["text"], payload["pages"], payload["empty_pages"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_TEXT
            or type(pages) is not int
            or not 1 <= pages <= MAX_PAGES
            or type(empty) is not int
            or not 0 <= empty < pages
        ):
            raise ValueError
    except SettingsError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SettingsError(_ERRORS["invalid"]) from None
    return OCRText(text, pages, empty)


def _recognize(image: Any, deadline: float) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    if time.monotonic() >= deadline:
        raise TimeoutError
    if sys.platform == "darwin":
        vision = importlib.import_module("Vision")
        objc = importlib.import_module("objc")
        with objc.autorelease_pool():
            request = vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
            request.setRecognitionLanguages_(["en-US"])
            # Correcting words can alter the private values we need to detect.
            request.setUsesLanguageCorrection_(False)
            handler = vision.VNImageRequestHandler.alloc().initWithData_options_(
                buffer.getvalue(), {}
            )
            success, _error = handler.performRequests_error_([request], None)
            if not success:
                raise RuntimeError("engine")
            lines = []
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if candidates:
                    lines.append(str(candidates[0].string()))
            return "\n".join(lines).strip()
    executable = shutil.which("tesseract")
    if executable is None:
        raise RuntimeError("tesseract")
    try:
        result = subprocess.run(
            [executable, "stdin", "stdout", "-l", "eng", "--psm", "3"],
            input=buffer.getvalue(),
            capture_output=True,
            timeout=min(10, max(0.1, deadline - time.monotonic())),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    if result.returncode:
        raise RuntimeError("tesseract")
    return result.stdout.decode("utf-8").strip()


def _pixels(width: float, height: float) -> None:
    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or width < 1
        or height < 1
        or math.ceil(width) * math.ceil(height) > MAX_PIXELS
    ):
        raise RuntimeError("pixels")


def _scan(source: bytes) -> dict[str, str | int]:
    image_module: Any = importlib.import_module("PIL.Image")
    image_ops = importlib.import_module("PIL.ImageOps")
    image_module.MAX_IMAGE_PIXELS = MAX_PIXELS
    deadline = time.monotonic() + _WORK_SECONDS
    texts: list[str] = []
    length = 0

    def read(image: Any) -> None:
        nonlocal length
        _pixels(*image.size)
        if time.monotonic() >= deadline:
            raise TimeoutError
        # Composite transparency on white, matching a document viewer.
        with (
            image_ops.exif_transpose(image) as oriented,
            oriented.convert("RGBA") as rgba,
            image_module.new("RGB", rgba.size, "white") as rgb,
        ):
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            text = _recognize(rgb, deadline)
        length += len(text) + (2 if texts else 0)
        if length > MAX_TEXT:
            raise RuntimeError("text")
        texts.append(text)

    if source.startswith(b"%PDF-"):
        pdf = importlib.import_module("pypdf")
        reader = pdf.PdfReader(io.BytesIO(source), strict=True)
        if reader.is_encrypted:
            return {"error": "encrypted"}
        if not 1 <= len(reader.pages) <= MAX_PAGES:
            return {"error": "pages"}
        renderer = importlib.import_module("pypdfium2")
        with renderer.PdfDocument(source) as document:
            document.init_forms()
            if len(document) != len(reader.pages):
                return {"error": "invalid"}
            for page in document:
                with contextlib.closing(page):
                    # 200 DPI; validate the allocation before PDFium renders.
                    scale = 200 / 72
                    width, height = page.get_size()
                    _pixels(width * scale, height * scale)
                    with (
                        contextlib.closing(
                            page.render(scale=scale, draw_annots=True)
                        ) as bitmap,
                        bitmap.to_pil() as image,
                    ):
                        read(image)
    else:
        try:
            with image_module.open(io.BytesIO(source)) as image:
                if image.format not in _FORMATS:
                    return {"error": "invalid"}
                frames = getattr(image, "n_frames", 1)
                if not 1 <= frames <= MAX_PAGES:
                    return {"error": "pages"}
                for frame in range(frames):
                    image.seek(frame)
                    read(image)
        except (
            image_module.DecompressionBombWarning,
            image_module.DecompressionBombError,
        ):
            raise RuntimeError("pixels") from None
    text = "\n\n".join(texts)
    if not text.strip():
        return {"error": "empty"}
    return {
        "text": text,
        "pages": len(texts),
        "empty_pages": sum(not text.strip() for text in texts),
    }


def _worker() -> int:
    """Keep parser and native engine diagnostics out of public output."""
    logging.disable(logging.CRITICAL)
    try:
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("error")
            source = sys.stdin.buffer.read(MAX_BYTES + 1)
            payload = (
                _scan(source) if len(source) <= MAX_BYTES else {"error": "invalid"}
            )
    except TimeoutError:
        payload = {"error": "timeout"}
    except RuntimeError as error:
        code = str(error)
        payload = {"error": code if code in _ERRORS else "engine"}
    except Exception:
        payload = {"error": "invalid"}
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    return 2 if "error" in payload else 0
