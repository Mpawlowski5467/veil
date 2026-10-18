"""Local OCR input, rendering, masking, and failure boundaries."""

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from veil import cli, ocr
from veil.desktop import DesktopController
from veil.gateway.config import SettingsError, prepare_data_dir


@pytest.fixture
def images():
    return pytest.importorskip("PIL.Image")


@pytest.fixture
def scan_file(tmp_path, images):
    path = tmp_path / "private-image.png"
    with images.new("RGB", (900, 400), "white") as image:
        image.save(path)
    return path


@pytest.fixture
def renderer():
    pytest.importorskip("pypdf")
    return pytest.importorskip("pypdfium2")


@pytest.fixture
def recognize(monkeypatch):
    calls = []

    def read(image, deadline):
        calls.append((image.size, image.mode, image.getpixel((0, 0))))
        return "jane@example.com"

    monkeypatch.setattr(ocr, "_recognize", read)
    return calls


def test_image_preparation_applies_orientation_and_white_background(images, recognize):
    with images.new("RGBA", (120, 80), (0, 0, 0, 0)) as image:
        exif = image.getexif()
        exif[274] = 6
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", exif=exif)
    assert ocr._scan(buffer.getvalue())["text"] == "jane@example.com"
    assert recognize == [((80, 120), "RGB", (255, 255, 255))]


@pytest.mark.parametrize("format_name", ["PNG", "JPEG", "TIFF", "WEBP", "BMP"])
def test_supported_image_formats(images, recognize, format_name):
    buffer = io.BytesIO()
    with images.new("RGB", (100, 100), "white") as image:
        image.save(buffer, format=format_name)
    assert ocr._scan(buffer.getvalue())["pages"] == 1
    assert len(recognize) == 1


def test_multiframe_tiff_keeps_order_and_reports_unread_pages(images, monkeypatch):
    buffer = io.BytesIO()
    with (
        images.new("RGB", (100, 100), "white") as first,
        images.new("RGB", (120, 100), "white") as second,
    ):
        first.save(buffer, format="TIFF", save_all=True, append_images=[second])
    texts = iter(["jane@example.com", ""])
    monkeypatch.setattr(ocr, "_recognize", lambda *a: next(texts))
    result = ocr._scan(buffer.getvalue())
    assert result == {"text": "jane@example.com\n\n", "pages": 2, "empty_pages": 1}
    assert "1 page had no recognized text" in ocr.OCRText(**result).summary


def test_unsupported_image_is_refused_before_recognition(images, recognize):
    buffer = io.BytesIO()
    with images.new("RGB", (100, 100), "white") as image:
        image.save(buffer, format="GIF")
    assert ocr._scan(buffer.getvalue()) == {"error": "invalid"}
    assert not recognize


def test_pixel_limit_precedes_decoding(images, recognize, monkeypatch):
    buffer = io.BytesIO()
    with images.new("RGB", (101, 100), "white") as image:
        image.save(buffer, format="PNG")
    monkeypatch.setattr(ocr, "MAX_PIXELS", 10_000)
    with pytest.raises(RuntimeError, match="pixels"):
        ocr._scan(buffer.getvalue())
    assert not recognize


def test_frame_limit_precedes_recognition(images, recognize, monkeypatch):
    buffer = io.BytesIO()
    with images.new("RGB", (100, 100), "white") as image:
        image.save(buffer, format="TIFF", save_all=True, append_images=[image])
    monkeypatch.setattr(ocr, "MAX_PAGES", 1)
    assert ocr._scan(buffer.getvalue()) == {"error": "pages"}
    assert not recognize


def test_pdf_renders_every_page_at_200_dpi(tmp_path, images, renderer, recognize):
    path = tmp_path / "mixed.pdf"
    pdf = pytest.importorskip("pypdf")
    # The first page has only pixels. The second has no recognized content.
    with images.new("RGB", (600, 300), "white") as image:
        image.save(path, format="PDF", resolution=100)
    writer = pdf.PdfWriter(path)
    writer.add_blank_page(width=432, height=216)
    writer.add_metadata({"/Title": "private-metadata@example.com"})
    writer.write(path)
    result = ocr._scan(path.read_bytes())
    assert result["pages"] == 2
    assert result["text"] == "jane@example.com\n\njane@example.com"
    assert [call[0] for call in recognize] == [(1200, 600), (1200, 600)]


@pytest.mark.parametrize("password", ["", "fictional-password"])
def test_encrypted_pdf_is_refused_before_rendering(
    tmp_path, renderer, recognize, password
):
    pdf = pytest.importorskip("pypdf")
    writer = pdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(password)
    path = tmp_path / "encrypted.pdf"
    writer.write(path)
    assert ocr._scan(path.read_bytes()) == {"error": "encrypted"}
    assert not recognize


@pytest.mark.parametrize("count", [0, 26])
def test_pdf_page_limit_precedes_rendering(renderer, recognize, count):
    pdf = pytest.importorskip("pypdf")
    writer = pdf.PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    assert ocr._scan(buffer.getvalue()) == {"error": "pages"}
    assert not recognize


def test_oversized_pdf_page_is_refused_before_rendering(renderer, recognize):
    pdf = pytest.importorskip("pypdf")
    writer = pdf.PdfWriter()
    writer.add_blank_page(width=100_000, height=100_000)
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(RuntimeError, match="pixels"):
        ocr._scan(buffer.getvalue())
    assert not recognize


def test_all_blank_and_excessive_text_are_not_successes(scan_file, monkeypatch):
    source = scan_file.read_bytes()
    monkeypatch.setattr(ocr, "_recognize", lambda *a: " ")
    assert ocr._scan(source) == {"error": "empty"}
    monkeypatch.setattr(ocr, "_recognize", lambda *a: "a" * (ocr.MAX_TEXT + 1))
    with pytest.raises(RuntimeError, match="text"):
        ocr._scan(source)


def test_no_partial_output_if_a_later_page_fails(images, monkeypatch):
    buffer = io.BytesIO()
    with images.new("RGB", (100, 100), "white") as image:
        image.save(buffer, format="TIFF", save_all=True, append_images=[image])
    calls = []

    def read(*a):
        if calls:
            raise TimeoutError
        calls.append(True)
        return "private@example.com"

    monkeypatch.setattr(ocr, "_recognize", read)
    with pytest.raises(TimeoutError):
        ocr._scan(buffer.getvalue())


def test_source_size_and_regular_file_limits(tmp_path):
    with pytest.raises(SettingsError, match=r"regular|permissions"):
        ocr._source(tmp_path)
    path = tmp_path / "private.png"
    with path.open("wb") as stream:
        stream.truncate(ocr.MAX_BYTES + 1)
    with pytest.raises(SettingsError, match="20 MiB"):
        ocr._source(path)


def fake_dependencies(monkeypatch):
    monkeypatch.setattr(ocr.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(ocr.shutil, "which", lambda _: "/local/tesseract")


@pytest.mark.parametrize("payload", [b"not JSON", b"[]", b"{}", b'{"text":"private"}'])
def test_invalid_worker_output_is_private(tmp_path, monkeypatch, payload):
    fake_dependencies(monkeypatch)
    path = tmp_path / "private.png"
    path.write_bytes(b"fictional")
    monkeypatch.setattr(
        ocr.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, payload),
    )
    with pytest.raises(SettingsError, match="Could not scan") as error:
        ocr.extract_ocr(path)
    assert "private" not in str(error.value)


def test_timeout_is_private_and_does_not_create_session(tmp_path, monkeypatch, capsys):
    fake_dependencies(monkeypatch)
    path = tmp_path / "private.png"
    path.write_bytes(b"fictional")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("private.png", 120, output=b"private text")

    monkeypatch.setattr(ocr.subprocess, "run", timeout)
    data = tmp_path / "absent"
    assert (
        cli.main(
            ["--data-dir", str(data), "mask", "--session", "ocr", "--ocr", str(path)]
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "timed out" in output.err
    assert "private" not in output.err
    assert not data.exists()


def test_dependency_errors_are_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr.importlib.util, "find_spec", lambda _: None)
    with pytest.raises(SettingsError, match=r"veil\[ocr\]"):
        ocr.extract_ocr(tmp_path / "absent")
    fake_dependencies(monkeypatch)
    monkeypatch.setattr(ocr.sys, "platform", "linux")
    monkeypatch.setattr(ocr.shutil, "which", lambda _: None)
    with pytest.raises(SettingsError, match="Tesseract with English"):
        ocr.extract_ocr(tmp_path / "absent")


def test_tesseract_receives_pixels_over_stdin_and_no_private_paths(images, monkeypatch):
    monkeypatch.setattr(ocr.sys, "platform", "linux")
    monkeypatch.setattr(ocr.shutil, "which", lambda _: "/local/tesseract")

    def run(args, **kwargs):
        assert args == [
            "/local/tesseract",
            "stdin",
            "stdout",
            "-l",
            "eng",
            "--psm",
            "3",
        ]
        assert kwargs["input"].startswith(b"\x89PNG")
        assert 0 < kwargs["timeout"] <= 10
        return subprocess.CompletedProcess(args, 0, b"jane@example.com\n")

    monkeypatch.setattr(ocr.subprocess, "run", run)
    with images.new("RGB", (100, 100), "white") as image:
        assert ocr._recognize(image, float("inf")) == "jane@example.com"


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_tesseract_failure_does_not_expose_diagnostics(images, monkeypatch, failure):
    monkeypatch.setattr(ocr.sys, "platform", "linux")
    monkeypatch.setattr(ocr.shutil, "which", lambda _: "/local/tesseract")

    def run(*a, **k):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("private", 10, output=b"private")
        return subprocess.CompletedProcess([], 1, b"private", b"private")

    monkeypatch.setattr(ocr.subprocess, "run", run)
    with (
        images.new("RGB", (100, 100), "white") as image,
        pytest.raises((RuntimeError, TimeoutError)) as error,
    ):
        ocr._recognize(image, float("inf"))
    assert "private" not in str(error.value)


def test_ocr_uses_registered_values_and_persistent_restoration(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        ocr,
        "extract_ocr",
        lambda _: ocr.OCRText("Jane Example at jane@example.com", 1, 0),
    )
    data = prepare_data_dir(tmp_path / "data")
    (data / "config.json").write_text(
        json.dumps({"identity": False, "entities": {"PERSON": ["Jane Example"]}})
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("must not read"))
    options = ["--data-dir", str(data)]
    assert cli.main([*options, "mask", "--session", "ocr", "--ocr", "private.png"]) == 0
    output = capsys.readouterr()
    assert output.out == "[PERSON_1] at [EMAIL_1]"
    assert "review before sharing" in output.err
    assert "original file is unchanged" in output.err
    assert "private" not in output.err
    monkeypatch.setattr(sys, "stdin", io.StringIO("[PERSON_1]: [EMAIL_1]"))
    assert cli.main([*options, "restore", "--session", "ocr"]) == 0
    assert capsys.readouterr().out == "Jane Example: jane@example.com"


@pytest.mark.parametrize("other", [["--clipboard"], ["--pdf", "file.pdf"]])
def test_ocr_inputs_are_mutually_exclusive(other):
    with pytest.raises(SystemExit) as error:
        cli.main(["mask", "--session", "ocr", "--ocr", "scan.png", *other])
    assert error.value.code == 2


def test_restore_does_not_accept_ocr():
    with pytest.raises(SystemExit) as error:
        cli.main(["restore", "--session", "ocr", "--ocr", "scan.png"])
    assert error.value.code == 2


def test_import_does_not_load_optional_libraries():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import veil.ocr; "
            "assert not {'PIL','pypdf','pypdfium2','Vision'} & sys.modules.keys()",
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    os.environ.get("VEIL_NATIVE_OCR") != "1", reason="opt-in local OCR engine"
)
@pytest.mark.parametrize("kind", ["png", "pdf", "tiff"])
def test_native_scans_are_read_only_and_maskable(tmp_path, images, renderer, kind):
    if sys.platform == "darwin":
        pytest.importorskip("Vision")
    elif shutil.which("tesseract") is None:
        pytest.skip("Tesseract is not installed")
    from PIL import ImageDraw, ImageFont

    fonts = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    font_path = next((path for path in fonts if Path(path).is_file()), None)
    assert font_path, "a native test font is required"
    path = tmp_path / f"private.{kind}"
    with images.new("RGB", (1600, 650), "white") as image:
        ImageDraw.Draw(image).multiline_text(
            (100, 100),
            "Jane Example\njane@example.com",
            fill="black",
            font=ImageFont.truetype(font_path, 56),
            spacing=30,
        )
        if kind == "tiff":
            image.save(path, save_all=True, append_images=[image])
        else:
            image.save(path, resolution=150)
    if kind == "pdf":
        pdf = pytest.importorskip("pypdf")
        assert not pdf.PdfReader(path).pages[0].extract_text().strip()
    before = path.read_bytes()
    data = tmp_path / "absent"
    document = DesktopController(data).import_scan(path)
    assert "Jane Example" in document.text
    assert "jane@example.com" in document.text
    assert document.pages == (2 if kind == "tiff" else 1)
    assert path.read_bytes() == before
    assert not data.exists()
    from veil import Shield

    shield = Shield()
    shield.add_entity("Jane Example", "PERSON")
    masked = shield.mask(document.text)
    assert "jane@example.com" not in masked.text
    assert "Jane Example" not in masked.text
    assert shield.restore(masked.text).text == document.text


@pytest.mark.skipif(
    os.environ.get("VEIL_NATIVE_OCR") != "1", reason="opt-in local OCR engine"
)
@pytest.mark.parametrize("kind", ["blank", "malformed"])
def test_native_failure_writes_no_partial_text_or_session(
    tmp_path, images, renderer, capsys, kind
):
    if sys.platform == "darwin":
        pytest.importorskip("Vision")
    elif shutil.which("tesseract") is None:
        pytest.skip("Tesseract is not installed")
    path = tmp_path / "private.png"
    if kind == "blank":
        with images.new("RGB", (900, 400), "white") as image:
            image.save(path)
    else:
        path.write_bytes(b"%PDF-1.7\nprivate malformed text")
    data = tmp_path / "absent"
    assert (
        cli.main(
            ["--data-dir", str(data), "mask", "--session", "ocr", "--ocr", str(path)]
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "private" not in output.err
    assert str(path) not in output.err
    assert not data.exists()
