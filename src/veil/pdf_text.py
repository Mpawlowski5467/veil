"""Read a PDF's text layer locally, without forwarding or rewriting the PDF."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import json
import logging
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .gateway.config import SettingsError
from .runtime import command

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100
MAX_TEXT = 32_768
MAX_PAGE_STREAM = 2 * 1024 * 1024
TIMEOUT = 20
_ERRORS = {
    "encrypted": "Unlock the PDF locally and choose an unencrypted copy.",
    "pages": "PDF import accepts 1 to 100 pages. Choose a smaller document.",
    "text": "PDF text exceeds 32768 characters. Choose a shorter document.",
    "stream": "A PDF page is too complex to import. Choose a simpler copy.",
    "empty": (
        "This PDF has no extractable text. Use Scan PDF / image or veil mask --ocr."
    ),
    "invalid": "Could not extract this PDF. Choose a valid PDF with selectable text.",
}


@dataclass(frozen=True)
class PDFText:
    """Extracted page text and page counts, with no source path or metadata."""

    text: str
    pages: int
    empty_pages: int

    @property
    def summary(self) -> str:
        """Describe extraction without claiming every page's content was read."""
        unit = "page" if self.pages == 1 else "pages"
        message = f"Read {self.pages} PDF {unit}."
        if self.empty_pages:
            unit = "page" if self.empty_pages == 1 else "pages"
            message += (
                f" {self.empty_pages} {unit} had no text; "
                "blank or scanned pages are not included."
            )
        return message


def _source(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise SettingsError("Choose a regular PDF file.")
            if info.st_size > MAX_BYTES:
                raise SettingsError("PDF import accepts files up to 20 MiB.")
            source = stream.read(MAX_BYTES + 1)
    except OSError:
        raise SettingsError("Could not read the PDF. Check its permissions.") from None
    if len(source) > MAX_BYTES:
        raise SettingsError("PDF import accepts files up to 20 MiB.")
    if not source.startswith(b"%PDF-"):
        raise SettingsError("Choose a valid PDF file.")
    return source


def extract_pdf(path: Path) -> PDFText:
    """Extract bounded text in a disposable worker; never log document contents."""
    if importlib.util.find_spec("pypdf") is None:
        raise SettingsError("PDF import needs the optional 'veil[pdf]' extra.")
    source = _source(path)
    try:
        result = subprocess.run(
            command("pdf"),
            input=source,
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SettingsError(
            "PDF extraction timed out. Choose a smaller or simpler document."
        ) from None
    except OSError:
        raise SettingsError("Could not start local PDF extraction.") from None
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
    return PDFText(text, pages, empty)


def _extract(source: bytes) -> dict[str, str | int]:
    pdf = importlib.import_module("pypdf")
    reader = pdf.PdfReader(io.BytesIO(source), strict=True)
    if reader.is_encrypted:
        return {"error": "encrypted"}
    count = len(reader.pages)
    if not 1 <= count <= MAX_PAGES:
        return {"error": "pages"}
    pages: list[str] = []
    length = 0
    empty = 0
    for page in reader.pages:
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > MAX_PAGE_STREAM:
            return {"error": "stream"}
        text = page.extract_text() or ""
        length += len(text) + (2 if pages else 0)
        if length > MAX_TEXT:
            return {"error": "text"}
        empty += not text.strip()
        pages.append(text)
    text = "\n\n".join(pages)
    if not text.strip():
        return {"error": "empty"}
    return {"text": text, "pages": count, "empty_pages": empty}


def _worker() -> int:
    """Parse stdin in isolation and emit only bounded JSON or a fixed error code."""
    logging.disable(logging.CRITICAL)
    try:
        # Parser diagnostics can contain private PDF strings; never pass them on.
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            source = sys.stdin.buffer.read(MAX_BYTES + 1)
            payload = (
                _extract(source) if len(source) <= MAX_BYTES else {"error": "invalid"}
            )
    except Exception:
        payload = {"error": "invalid"}
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    return 2 if "error" in payload else 0
