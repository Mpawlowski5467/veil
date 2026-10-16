"""PDF text import stays local, bounded, and separate from the original file."""

import io
import json
import subprocess
import sys

import pytest

from veil import cli
from veil.desktop import DesktopController
from veil.gateway.config import SettingsError, prepare_data_dir
from veil.pdf_text import MAX_BYTES, MAX_TEXT, extract_pdf


@pytest.fixture
def make_pdf(tmp_path):
    pdf = pytest.importorskip("pypdf")
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    def create(pages, *, password=None, extra_stream=b""):
        writer = pdf.PdfWriter()
        font = writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                    NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                }
            )
        )
        for text in pages:
            page = writer.add_blank_page(width=612, height=792)
            if text:
                page[NameObject("/Resources")] = DictionaryObject(
                    {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
                )
                stream = DecodedStreamObject()
                escaped = (
                    text.replace("\\", "\\\\")
                    .replace("(", "\\(")
                    .replace(")", "\\)")
                    .encode("cp1252")
                )
                stream.set_data(
                    b"BT /F1 12 Tf 50 700 Td (" + escaped + b") Tj ET\n" + extra_stream
                )
                page[NameObject("/Contents")] = writer._add_object(stream)
        writer.add_metadata({"/Title": "private-metadata@example.com"})
        if password is not None:
            writer.encrypt(password)
        path = tmp_path / "private-source.pdf"
        writer.write(path)
        return path

    return create


def test_desktop_import_is_read_only_and_excludes_pdf_metadata(tmp_path, make_pdf):
    path = make_pdf(["José Example", "Contact jane@example.com"])
    before = path.read_bytes()
    data = tmp_path / "absent"
    document = DesktopController(data).import_pdf(path)
    assert document.text == "José Example\n\nContact jane@example.com"
    assert document.pages == 2
    assert document.empty_pages == 0
    assert "private-metadata" not in document.text
    assert not data.exists()
    assert path.read_bytes() == before


def test_mixed_document_reports_pages_without_text(make_pdf):
    document = extract_pdf(make_pdf(["jane@example.com", None]))
    assert document.pages == 2
    assert document.empty_pages == 1
    assert "1 page had no text" in document.summary
    assert "scanned" in document.summary


@pytest.mark.parametrize("password", ["fictional-password", ""])
def test_encrypted_documents_are_refused_without_echoing_password(make_pdf, password):
    with pytest.raises(SettingsError, match="Unlock the PDF") as error:
        extract_pdf(make_pdf(["jane@example.com"], password=password))
    assert "fictional-password" not in str(error.value)


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ([None], "no extractable text"),
        ([], "1 to 100 pages"),
        (["text"] * 101, "1 to 100 pages"),
        (["a" * (MAX_TEXT + 1)], "32768 characters"),
    ],
)
def test_unusable_or_oversized_documents_are_refused(make_pdf, pages, message):
    with pytest.raises(SettingsError, match=message):
        extract_pdf(make_pdf(pages))


def test_large_decoded_page_is_refused(make_pdf):
    path = make_pdf(["text"], extra_stream=b"%" + b"a" * (2 * 1024 * 1024))
    with pytest.raises(SettingsError, match="too complex"):
        extract_pdf(path)


def test_size_limit_is_checked_before_starting_parser(tmp_path, monkeypatch):
    pytest.importorskip("pypdf")
    path = tmp_path / "large.pdf"
    with path.open("wb") as stream:
        stream.truncate(MAX_BYTES + 1)
    monkeypatch.setattr(
        "veil.pdf_text.subprocess.run", lambda *a, **k: pytest.fail("parser started")
    )
    with pytest.raises(SettingsError, match="20 MiB"):
        extract_pdf(path)


def test_timeout_is_private_and_does_not_return_partial_text(make_pdf, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            "private-file-name", 20, output=b"private PDF text"
        )

    path = make_pdf(["jane@example.com"])
    monkeypatch.setattr("veil.pdf_text.subprocess.run", timeout)
    with pytest.raises(SettingsError, match="timed out") as error:
        extract_pdf(path)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("source", [b"not a PDF", b"%PDF-1.7\nprivate PDF text"])
def test_bad_pdf_writes_no_output_or_session_data(tmp_path, capsys, source):
    pytest.importorskip("pypdf")
    path = tmp_path / "private.pdf"
    path.write_bytes(source)
    data = tmp_path / "absent"
    assert (
        cli.main(
            ["--data-dir", str(data), "mask", "--session", "pdf", "--pdf", str(path)]
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "private PDF text" not in output.err
    assert str(path) not in output.err
    assert not data.exists()


def test_optional_dependency_error_is_actionable_without_creating_data(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("veil.pdf_text.importlib.util.find_spec", lambda _: None)
    data = tmp_path / "absent"
    assert (
        cli.main(
            ["--data-dir", str(data), "mask", "--session", "pdf", "--pdf", "input.pdf"]
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "veil[pdf]" in output.err
    assert not data.exists()


def test_pdf_masking_uses_config_and_persistent_restoration(
    tmp_path, make_pdf, monkeypatch, capsys
):
    path = make_pdf(["José Example", "Contact jane@example.com"])
    data = prepare_data_dir(tmp_path / "data")
    (data / "config.json").write_text(
        json.dumps({"identity": False, "entities": {"PERSON": ["José Example"]}})
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("should not read stdin"))
    options = ["--data-dir", str(data)]
    assert cli.main([*options, "mask", "--session", "pdf", "--pdf", str(path)]) == 0
    output = capsys.readouterr()
    assert output.out == "[PERSON_1]\n\nContact [EMAIL_1]"
    assert "Read 2 PDF pages" in output.err
    assert "jane@example.com" not in output.err
    assert str(path) not in output.err
    monkeypatch.setattr("sys.stdin", io.StringIO("Email [PERSON_1] at [EMAIL_1]"))
    assert cli.main([*options, "restore", "--session", "pdf"]) == 0
    assert capsys.readouterr().out == "Email José Example at jane@example.com"


def test_pdf_and_clipboard_are_mutually_exclusive():
    with pytest.raises(SystemExit) as error:
        cli.main(["mask", "--session", "pdf", "--pdf", "input.pdf", "--clipboard"])
    assert error.value.code == 2


def test_pdf_input_is_not_available_for_restore():
    with pytest.raises(SystemExit) as error:
        cli.main(["restore", "--session", "pdf", "--pdf", "input.pdf"])
    assert error.value.code == 2


def test_import_module_does_not_load_parser():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import veil.pdf_text; assert 'pypdf' not in sys.modules",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
