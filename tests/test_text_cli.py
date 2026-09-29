"""Local chat masking must preserve sessions and avoid output on failure."""

import io
import json

import pytest

from veil import cli, text_cli


@pytest.fixture
def data_dir(tmp_path):
    directory = tmp_path / "veil"
    directory.mkdir(mode=0o700)
    (directory / "config.json").write_text(
        json.dumps({"identity": False, "entities": {"PERSON": ["Jan Nowak"]}})
    )
    return directory


def run(operation, source, data_dir, monkeypatch, session="chat", extra=()):
    monkeypatch.setattr("sys.stdin", io.StringIO(source))
    return cli.main(
        ["--data-dir", str(data_dir), operation, "--session", session, *extra]
    )


def test_mask_and_restore_across_process_style_calls(data_dir, monkeypatch, capsys):
    original = "Hello Jan Nowak at jane@example.com.\n"
    assert run("mask", original, data_dir, monkeypatch) == 0
    masked = capsys.readouterr().out
    assert masked == "Hello [PERSON_1] at [EMAIL_1].\n"
    assert run("restore", masked, data_dir, monkeypatch) == 0
    assert capsys.readouterr().out == original


def test_wrong_session_and_unknown_placeholder_write_nothing(
    data_dir, monkeypatch, capsys
):
    run("mask", "jane@example.com", data_dir, monkeypatch)
    capsys.readouterr()
    assert run("restore", "Hi [EMAIL_1]", data_dir, monkeypatch, session="another") == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert "no output written" in result.err
    assert "jane@example.com" not in result.err


def test_literal_placeholder_text_round_trips(data_dir, monkeypatch, capsys):
    original = "Template [EMAIL_1]; real jane@example.com"
    assert run("mask", original, data_dir, monkeypatch) == 0
    masked = capsys.readouterr().out
    assert "[LITERAL_1]" in masked
    assert run("restore", masked, data_dir, monkeypatch) == 0
    assert capsys.readouterr().out == original


def test_clipboard_updates_only_after_success(data_dir, monkeypatch, capsys):
    clipboard = ["jane@example.com"]
    writes = []
    monkeypatch.setattr(text_cli, "clipboard_read", lambda: clipboard[0])
    monkeypatch.setattr(text_cli, "clipboard_write", writes.append)
    args = ["--data-dir", str(data_dir), "mask", "--session", "chat", "--clipboard"]
    assert cli.main(args) == 0
    assert writes == ["[EMAIL_1]"]
    assert capsys.readouterr().out == ""
    clipboard[0] = "Hi [EMAIL_99]"
    args[2] = "restore"
    assert cli.main(args) == 2
    assert writes == ["[EMAIL_1]"]
    assert capsys.readouterr().out == ""


def test_clipboard_backend_failure_does_not_quote_contents(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private clipboard contents")

    monkeypatch.setattr(text_cli.subprocess, "run", fail)
    monkeypatch.setattr(text_cli, "_clipboard_command", lambda **_: ["fake"])
    with pytest.raises(
        cli.SettingsError, match="could not read clipboard text"
    ) as error:
        text_cli.clipboard_read()
    assert "private clipboard" not in str(error.value)


def test_session_label_cannot_be_a_path(data_dir, monkeypatch, capsys):
    assert run("mask", "hello", data_dir, monkeypatch, session="../other") == 2
    assert capsys.readouterr().out == ""
