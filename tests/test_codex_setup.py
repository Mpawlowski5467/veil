"""Setup must preserve user settings and make rollback safe under later edits."""

import json
import stat

import pytest
import tomlkit

from veil import cli
from veil.codex_setup import (
    config_path,
    read_receipt,
    receipt_path,
    setup_codex,
    undo_codex,
)
from veil.gateway import SettingsError


@pytest.fixture
def paths(tmp_path):
    directory = tmp_path / "veil"
    directory.mkdir(mode=0o700)
    return directory, tmp_path / "codex" / "config.toml"


def test_setup_and_exact_undo_preserve_comments_settings_and_permissions(paths):
    directory, config = paths
    config.parent.mkdir()
    original = (
        '# My settings\nmodel = "some-model" # keep me\n'
        'model_provider = "other"\nweb_search = "cached"\n'
        "[features]\napps = true # original\nexperimental = true\n"
        '[model_providers.other]\nbase_url = "https://example.com/v1"\n'
        '[plugins."a@b"]\nenabled = true\n'
    )
    config.write_text(original)
    assert setup_codex(directory, config, 8485, "chatgpt") == 0
    installed = config.read_text()
    value = tomlkit.parse(installed).unwrap()
    assert "# keep me" in installed
    assert value["model"] == "some-model"
    assert value["features"]["experimental"] is True
    assert value["model_providers"]["other"]["base_url"] == "https://example.com/v1"
    assert value["plugins"]["a@b"]["enabled"] is True
    assert value["model_provider"] == "veil"
    assert value["features"]["apps"] is False
    assert value["analytics"]["enabled"] is False
    state = read_receipt(config)
    backup = config.parent / state["backup"]
    assert backup.read_text() == original
    for path in (config, backup, receipt_path(config)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert setup_codex(directory, config, 8485, "chatgpt") == 0
    assert read_receipt(config) == state
    assert len(list(config.parent.glob("*.veil-backup-*"))) == 1
    assert undo_codex(config) == 0
    assert config.read_text() == original
    assert backup.exists()
    assert not receipt_path(config).exists()


@pytest.mark.parametrize(
    "original",
    [
        'features = { apps = true, unrelated = "yes" }\n',
        'features.apps = true\nfeatures.unrelated = "yes"\n',
        '[features]\napps = true\nunrelated = "yes"\n',
    ],
)
def test_supported_toml_table_layouts(paths, original):
    directory, config = paths
    config.parent.mkdir()
    config.write_text(original)
    setup_codex(directory, config, 12345, "api-key")
    value = tomlkit.parse(config.read_text()).unwrap()
    assert value["features"]["unrelated"] == "yes"
    assert value["model_providers"]["veil"]["env_key"] == "OPENAI_API_KEY"
    assert not value["model_providers"]["veil"]["requires_openai_auth"]
    undo_codex(config)
    assert config.read_text() == original


def test_undo_preserves_unrelated_later_edits(paths):
    directory, config = paths
    setup_codex(directory, config, 8485, "chatgpt")
    updated = tomlkit.parse(config.read_text())
    updated["model"] = "new-model"
    updated["features"]["my_feature"] = True
    updated["model_providers"]["other"] = {"base_url": "https://example.com"}
    config.write_text(updated.as_string())
    undo_codex(config)
    assert tomlkit.parse(config.read_text()).unwrap() == {
        "model": "new-model",
        "features": {"my_feature": True},
        "model_providers": {"other": {"base_url": "https://example.com"}},
    }


def test_conflicting_edit_is_never_overwritten_by_setup_or_undo(paths):
    directory, config = paths
    setup_codex(directory, config, 8485, "chatgpt")
    changed = config.read_text().replace(
        'model_provider = "veil"', 'model_provider = "other"'
    )
    config.write_text(changed)
    for action in (
        lambda: setup_codex(directory, config, 8485, "chatgpt"),
        lambda: undo_codex(config),
    ):
        with pytest.raises(SettingsError, match="changed after setup"):
            action()
        assert config.read_text() == changed
        assert receipt_path(config).exists()


def test_new_configuration_removed_on_undo(paths):
    directory, config = paths
    setup_codex(directory, config, 8485, "chatgpt")
    undo_codex(config)
    assert not config.exists()


@pytest.mark.parametrize("text", ['SECRET = "do-not-show\n', 'profile = "work"\n'])
def test_invalid_or_profile_configuration_is_not_changed(paths, text, capsys):
    directory, config = paths
    config.parent.mkdir()
    config.write_text(text)
    assert (
        cli.main(
            ["--data-dir", str(directory), "setup", "codex", "--config", str(config)]
        )
        == 2
    )
    assert config.read_text() == text
    assert not receipt_path(config).exists()
    assert "do-not-show" not in capsys.readouterr().err


def test_symlink_lock_and_backup_tampering_are_refused(paths):
    directory, config = paths
    config.parent.mkdir()
    target = config.parent / "real.toml"
    target.write_text('model = "keep"\n')
    config.symlink_to(target)
    with pytest.raises(SettingsError, match="symlink"):
        setup_codex(directory, config, 8485, "chatgpt")
    assert target.read_text() == 'model = "keep"\n'
    config.unlink()
    lock = config.with_name(config.name + ".veil-setup.lock")
    lock.touch()
    with pytest.raises(SettingsError, match="setup lock"):
        setup_codex(directory, config, 8485, "chatgpt")
    lock.unlink()
    setup_codex(directory, config, 8485, "chatgpt")
    original = config.read_text()
    state = read_receipt(config)
    (config.parent / state["backup"]).write_text("tampered")
    with pytest.raises(SettingsError, match="backup changed"):
        undo_codex(config)
    assert config.read_text() == original


def test_receipt_cannot_reference_a_file_outside_backup_directory(paths):
    directory, config = paths
    setup_codex(directory, config, 8485, "chatgpt")
    state = read_receipt(config)
    state["backup"] = "../outside"
    receipt_path(config).write_text(json.dumps(state))
    with pytest.raises(SettingsError, match="receipt is invalid"):
        undo_codex(config)


def test_custom_codex_home_is_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert config_path() == tmp_path / "config.toml"


def test_undo_preserves_original_crlf_bytes(paths):
    directory, config = paths
    config.parent.mkdir()
    original = b'# Windows layout\r\nmodel = "keep"\r\n'
    config.write_bytes(original)
    setup_codex(directory, config, 8485, "chatgpt")
    state = read_receipt(config)
    assert (config.parent / state["backup"]).read_bytes() == original
    undo_codex(config)
    assert config.read_bytes() == original


def test_interrupted_install_can_be_undone(paths, monkeypatch, capsys):
    from veil import codex_setup

    directory, config = paths
    config.parent.mkdir()
    original = '# keep this\nmodel = "before"\n'
    config.write_text(original)
    write = codex_setup._write

    def fail_configuration(path, text):
        if path == config:
            raise OSError("disk full")
        write(path, text)

    monkeypatch.setattr(codex_setup, "_write", fail_configuration)
    assert (
        cli.main(
            ["--data-dir", str(directory), "setup", "codex", "--config", str(config)]
        )
        == 2
    )
    assert config.read_text() == original
    assert receipt_path(config).exists()
    assert "disk full" not in capsys.readouterr().err
    monkeypatch.setattr(codex_setup, "_write", write)
    assert undo_codex(config) == 0
    assert config.read_text() == original


def test_empty_receipt_does_not_overwrite_previous_backup(paths):
    directory, config = paths
    setup_codex(directory, config, 8485, "chatgpt")
    original = config.read_text()
    receipt_path(config).write_text("")
    with pytest.raises(SettingsError, match="receipt is empty"):
        setup_codex(directory, config, 8485, "chatgpt")
    assert config.read_text() == original


@pytest.mark.parametrize("secret", ["", "secret\ninjection", "nonascii-\u00e9"])
def test_invalid_existing_secret_is_not_used(paths, secret):
    directory, config = paths
    (directory / "gateway-secret").write_text(secret)
    with pytest.raises(SettingsError, match="secret file is invalid"):
        setup_codex(directory, config, 8485, "chatgpt")
    assert not config.exists()
