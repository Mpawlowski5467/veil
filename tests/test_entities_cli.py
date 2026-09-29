"""Registration stays local, preserves settings, and survives failed edits."""

import io
import json
import os
import stat
import warnings

import pytest

from veil import cli, entities_cli
from veil.gateway import load_settings, open_sessions

PRIVATE = "Ada Quill"


@pytest.fixture
def directory(tmp_path):
    return tmp_path / "veil"


def invoke(directory, monkeypatch, *args, value=PRIVATE):
    monkeypatch.setattr("sys.stdin", io.StringIO(value))
    return cli.main(["--data-dir", str(directory), "entities", *args])


def write_config(directory, raw):
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_add_list_remove_preserve_settings_without_printing_values(
    directory, monkeypatch, capsys
):
    original = {
        "$schema": "https://example.com/veil.json",
        "version": 1,
        "identity": False,
        "note": False,
        "retention_days": 7,
        "patterns": {"ORDER": r"#\d{5}"},
        "allow_mcp_tools": ["mcp__local__example"],
        "entities": {"ORGANIZATION": ["Example Corp"]},
    }
    path = write_config(directory, original)
    assert (
        invoke(
            directory, monkeypatch, "add", "PERSON", "--stdin", value=PRIVATE + "\r\n"
        )
        == 0
    )
    added = json.loads(path.read_text())
    assert added == {
        **original,
        "entities": {**original["entities"], "PERSON": [PRIVATE]},
    }
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    output = capsys.readouterr()
    assert "Restart" in output.out
    assert PRIVATE not in output.out + output.err
    assert invoke(directory, monkeypatch, "list", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "entities": {"ORGANIZATION": 1, "PERSON": 1},
        "total": 2,
    }
    assert invoke(directory, monkeypatch, "remove", "PERSON", "--stdin") == 0
    assert json.loads(path.read_text()) == original
    output = capsys.readouterr()
    assert "does not erase history" in output.out
    assert PRIVATE not in output.out + output.err


def test_duplicates_and_missing_removals_do_not_rewrite(directory, monkeypatch, capsys):
    assert invoke(directory, monkeypatch, "add", "PERSON", "--stdin") == 0
    path = directory / "config.json"
    inode = path.stat().st_ino
    before = path.read_bytes()
    assert invoke(directory, monkeypatch, "add", "PERSON", "--stdin") == 0
    assert (
        invoke(
            directory, monkeypatch, "remove", "PERSON", "--stdin", value="Another Name"
        )
        == 0
    )
    assert path.read_bytes() == before
    assert path.stat().st_ino == inode
    assert PRIVATE not in capsys.readouterr().out


def test_different_types_cannot_silently_reclassify_a_value(
    directory, monkeypatch, capsys
):
    assert invoke(directory, monkeypatch, "add", "PERSON", "--stdin") == 0
    original = (directory / "config.json").read_bytes()
    assert invoke(directory, monkeypatch, "add", "CLIENT", "--stdin") == 2
    output = capsys.readouterr()
    assert "another type" in output.err
    assert PRIVATE not in output.out + output.err
    assert (directory / "config.json").read_bytes() == original


@pytest.mark.parametrize("operation", ["list", "remove"])
def test_absent_config_is_not_created(directory, monkeypatch, capsys, operation):
    args = (
        ("list", "--json") if operation == "list" else ("remove", "PERSON", "--stdin")
    )
    assert invoke(directory, monkeypatch, *args) == 0
    assert not directory.exists()
    assert PRIVATE not in capsys.readouterr().out


def test_filtered_list_reports_unique_values_not_raw_input(
    directory, monkeypatch, capsys
):
    write_config(
        directory,
        {"entities": {"PERSON": [PRIVATE, PRIVATE], "CLIENT": ["Example Corp"]}},
    )
    assert invoke(directory, monkeypatch, "list", "PERSON") == 0
    assert capsys.readouterr().out == "PERSON: 1 registered\n"
    assert invoke(directory, monkeypatch, "list", "ADDRESS", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "entities": {"ADDRESS": 0},
        "total": 0,
    }


def test_hidden_prompt_is_default(directory, monkeypatch, capsys):
    source = io.StringIO()
    monkeypatch.setattr(source, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", source)
    prompts = []

    def hidden(prompt):
        prompts.append(prompt)
        return PRIVATE

    monkeypatch.setattr(entities_cli.getpass, "getpass", hidden)
    assert cli.main(["--data-dir", str(directory), "entities", "add", "PERSON"]) == 0
    assert prompts == ["Private value (hidden): "]
    assert PRIVATE not in capsys.readouterr().out
    assert load_settings(directory / "config.json").entities == {"PERSON": (PRIVATE,)}


@pytest.mark.parametrize("failure", ["echo", "eof", "interrupt"])
def test_prompt_failures_do_not_create_config(directory, monkeypatch, capsys, failure):
    source = io.StringIO()
    monkeypatch.setattr(source, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", source)

    def fail(_prompt):
        if failure == "echo":
            warnings.warn(
                "unsafe echo fallback",
                entities_cli.getpass.GetPassWarning,
                stacklevel=2,
            )
            pytest.fail("must refuse before echo fallback reads input")
        raise EOFError if failure == "eof" else KeyboardInterrupt

    monkeypatch.setattr(entities_cli.getpass, "getpass", fail)
    assert cli.main(["--data-dir", str(directory), "entities", "add", "PERSON"]) == 2
    assert not directory.exists()
    assert "unsafe echo" not in capsys.readouterr().err


def test_noninteractive_input_requires_explicit_stdin(directory, monkeypatch, capsys):
    assert invoke(directory, monkeypatch, "add", "PERSON") == 2
    assert "--stdin" in capsys.readouterr().err
    assert not directory.exists()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "\n",
        " \t",
        "Ada\nQuill",
        "Ada\rQuill",
        "Ada\x00Quill",
        "Ada\x1bQuill",
        "Ada\u2028Quill",
        "Ada\n\n",
        "Ada\udcff",
        "x" * 16385,
    ],
)
def test_invalid_private_input_is_not_echoed_or_saved(
    directory, monkeypatch, capsys, value
):
    assert invoke(directory, monkeypatch, "add", "PERSON", "--stdin", value=value) == 2
    assert not directory.exists()
    output = capsys.readouterr()
    assert output.out == ""
    assert "Ada" not in output.err
    assert len(output.err) < 150


@pytest.mark.parametrize("kind", ["person", "LITERAL", "Private Value", "PERSON\n"])
def test_invalid_type_does_not_echo_user_input(directory, monkeypatch, capsys, kind):
    assert invoke(directory, monkeypatch, "add", kind, "--stdin") == 2
    assert not directory.exists()
    assert kind not in capsys.readouterr().err or kind == "LITERAL"


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        '"Ada Quill"',
        '{"entities":{"PERSON":"Ada Quill"}}',
        '{"entities":{"PERSON":["Ada\\nQuill"]}}',
        '{"Ada Quill":true}',
        '{"identity":true,"identity":false}',
        '{"entities":{"PERSON":["Ada Quill"],"PERSON":[]}}',
    ],
)
def test_invalid_existing_config_is_preserved(directory, monkeypatch, capsys, raw):
    directory.mkdir(mode=0o700)
    path = directory / "config.json"
    path.write_text(raw)
    assert invoke(directory, monkeypatch, "add", "PERSON", "--stdin") == 2
    assert path.read_text() == raw
    assert "Ada" not in capsys.readouterr().err
    assert list(directory.iterdir()) == [path]


@pytest.mark.parametrize("link", ["symlink", "dangling", "hardlink"])
def test_config_links_are_refused(directory, tmp_path, monkeypatch, capsys, link):
    directory.mkdir(mode=0o700)
    target = tmp_path / "other.json"
    if link != "dangling":
        target.write_text("{}")
    path = directory / "config.json"
    if link == "hardlink":
        os.link(target, path)
    else:
        path.symlink_to(target)
    assert invoke(directory, monkeypatch, "add", "PERSON", "--stdin") == 2
    if link != "dangling":
        assert target.read_text() == "{}"
    else:
        assert not target.exists()
    assert PRIVATE not in capsys.readouterr().err


def test_shared_or_symlinked_data_directory_is_refused(
    directory, tmp_path, monkeypatch
):
    directory.mkdir(mode=0o755)
    assert invoke(directory, monkeypatch, "add", "PERSON", "--stdin") == 2
    directory.rmdir()
    target = tmp_path / "private"
    target.mkdir(mode=0o700)
    directory.symlink_to(target)
    assert invoke(directory, monkeypatch, "list") == 2
    assert not list(target.iterdir())


def test_existing_lock_is_preserved_and_edit_refused(directory, monkeypatch, capsys):
    path = write_config(directory, {"identity": False})
    original = path.read_bytes()
    lock = directory / ".veil-entities.lock"
    lock.write_text("")
    assert invoke(directory, monkeypatch, "add", "PERSON", "--stdin") == 2
    assert path.read_bytes() == original
    assert lock.exists()
    assert "already running" in capsys.readouterr().err


def test_failed_replace_keeps_original_and_cleans_temporary_files(
    directory, monkeypatch, capsys
):
    path = write_config(directory, {"identity": False})
    original = path.read_bytes()

    def fail(*_args):
        raise OSError(PRIVATE)

    monkeypatch.setattr(entities_cli.os, "replace", fail)
    assert invoke(directory, monkeypatch, "add", "PERSON", "--stdin") == 2
    assert path.read_bytes() == original
    assert list(directory.iterdir()) == [path]
    assert PRIVATE not in capsys.readouterr().err


def test_conflicting_external_edit_is_preserved(directory, monkeypatch, capsys):
    path = write_config(directory, {"identity": False})
    real_write = entities_cli._write

    def external_edit(target, text, original):
        path.write_text('{"retention_days": 7}')
        real_write(target, text, original)

    monkeypatch.setattr(entities_cli, "_write", external_edit)
    assert invoke(directory, monkeypatch, "add", "PERSON", "--stdin") == 2
    assert json.loads(path.read_text()) == {"retention_days": 7}
    assert "changed during" in capsys.readouterr().err
    assert list(directory.iterdir()) == [path]


def test_reload_uses_case_sensitive_registrations_and_removal_retains_old_mapping(
    directory, monkeypatch
):
    write_config(directory, {"identity": False})
    for name in ("Jan", "Łucja Quill"):
        assert (
            invoke(directory, monkeypatch, "add", "PERSON", "--stdin", value=name) == 0
        )
    settings = load_settings(directory / "config.json")
    with open_sessions(directory, settings, {}) as sessions:
        shield = sessions.get("before").shield
        assert (
            shield.mask("Jan January jan Łucja Quill").text
            == "[PERSON_1] January jan [PERSON_2]"
        )
    assert (
        invoke(directory, monkeypatch, "remove", "PERSON", "--stdin", value="Jan") == 0
    )
    settings = load_settings(directory / "config.json")
    with open_sessions(directory, settings, {}) as sessions:
        assert (
            sessions.get("after").shield.mask("Jan Łucja Quill").text
            == "Jan [PERSON_1]"
        )
        assert sessions.get("before").shield.restore("[PERSON_1]").text == "Jan"
