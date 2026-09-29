"""Native public workflows used on Windows, macOS, and Linux CI runners."""

import json
import os
import subprocess
import sys
from contextlib import closing

import pytest

from veil import Shield, SQLiteVault, _windows
from veil.gateway.config import SettingsError, prepare_data_dir
from veil.gateway.store import SQLiteLedger


def command(*args, text=None):
    result = subprocess.run(
        [sys.executable, "-I", "-m", "veil", *map(str, args)],
        input=text,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_native_register_mask_restore_forget_and_skill(tmp_path):
    data = prepare_data_dir(tmp_path / "private")
    (data / "config.json").write_text('{"identity":false}', encoding="utf-8")
    options = ("--data-dir", data)
    command(*options, "entities", "add", "PERSON", "--stdin", text="Jan Łucja Quill\n")
    listed = command(*options, "entities", "list", "--json")
    assert "Jan" not in listed
    assert "PERSON" in listed
    original = "Email Jan Łucja Quill at jane.doe@example.com; SSN: 123-45-6789.\n"
    masked = command(*options, "mask", "--session", "native", text=original)
    assert "[PERSON_1]" in masked
    assert "[EMAIL_1]" in masked
    assert "[SSN_1]" in masked
    assert "jane.doe" not in masked
    assert command(*options, "restore", "--session", "native", text=masked) == original
    command(*options, "forget", "--all")
    with SQLiteVault(data / "vault.db", session="native") as vault:
        assert Shield(vault=vault).restore(masked).text == masked
    skills = tmp_path / "skills"
    command("skill", "install", "codex", "--skills-dir", skills)
    runtime = (skills / "veil/runtime.md").read_text(encoding="utf-8")
    argv = json.loads(runtime.split("```json\n", 1)[1].split("\n```", 1)[0])
    subprocess.run([*argv, "--help"], capture_output=True, check=True, timeout=15)
    command("skill", "uninstall", "codex", "--skills-dir", skills)
    assert not (skills / "veil").exists()


def test_native_codex_setup_undo_preserves_bytes(tmp_path):
    data = tmp_path / "private"
    folder = prepare_data_dir(tmp_path / "codex")
    config = folder / "config.toml"
    original = b'# User settings\r\nmodel = "keep"\r\n'
    config.write_bytes(original)
    command("--data-dir", data, "setup", "codex", "--config", config)
    assert 'model_provider = "veil"' in config.read_text(encoding="utf-8")
    command("undo", "codex", "--config", config)
    assert config.read_bytes() == original


def test_native_persistent_provider_replay_and_deletion(tmp_path):
    data = prepare_data_dir(tmp_path / "private")
    path = data / "ledger.db"
    block = {"type": "provider_result", "content": "fictional"}
    with closing(SQLiteLedger(path, "native")) as ledger:
        ledger.record_seen(block)
        ledger.record_text("fictional", "[PERSON_1]")
    with closing(SQLiteLedger(path, "native")) as ledger:
        assert ledger.was_seen(block)
        assert ledger.masked_text("fictional") == "[PERSON_1]"
        ledger.forget()
        assert not ledger.was_seen(block)
        assert ledger.masked_text("fictional") is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACLs")
def test_windows_private_acl_inheritance_and_reject_shared_storage(tmp_path):
    data = prepare_data_dir(tmp_path / "private")
    child = data / "inherited.txt"
    child.write_text("fictional", encoding="utf-8")
    _windows.check_private(data)
    _windows.check_private(child)
    vault_path = tmp_path / "private-vault.db"
    with SQLiteVault(vault_path):
        _windows.check_private(vault_path)
    subprocess.run(
        ["icacls", str(data), "/grant", "*S-1-1-0:(R)"],
        capture_output=True,
        check=True,
        timeout=15,
    )
    with pytest.raises(SettingsError, match="private Windows folder"):
        prepare_data_dir(data)


@pytest.mark.skipif(
    os.environ.get("VEIL_NATIVE_CLIPBOARD") != "1", reason="CI-only clipboard"
)
def test_native_clipboard_round_trip():
    # Only isolated CI desktops: never replace a user's current clipboard.
    from veil.text_cli import clipboard_read, clipboard_write

    text = "Fictional Łucja — jane.doe@example.com\nsecond line"
    clipboard_write(text)
    assert clipboard_read() == text
    clipboard_write("")
