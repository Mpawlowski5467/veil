"""Packaging, lifecycle, and local file workflows for assistant skills."""

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from veil import cli, skill


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def installed_runtime(directory):
    text = (directory / "runtime.md").read_text()
    return json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])


def test_install_both_repeat_and_remove_preserves_client_files(home):
    for folder in (".codex", ".claude", ".veil"):
        (home / folder).mkdir()
        (home / folder / "existing").write_text("keep user settings and data")
    before = snapshot(home)
    assert cli.main(["skill", "install"]) == 0
    installed = snapshot(home)
    for client in ("codex", "claude"):
        target = skill.skill_directory(client)
        assert (target / "SKILL.md").is_file()
        assert installed_runtime(target)[1:] == ["-I", "-m", "veil"]
    assert cli.main(["skill", "install", "all"]) == 0
    assert snapshot(home) == installed
    assert cli.main(["skill", "uninstall"]) == 0
    assert snapshot(home) == before
    assert cli.main(["skill", "uninstall"]) == 0
    assert snapshot(home) == before


def test_uninstall_missing_skills_has_no_side_effects(home):
    assert cli.main(["skill", "uninstall"]) == 0
    assert list(home.iterdir()) == []


def test_custom_directory_and_single_client(home):
    custom = home / "project" / "skills"
    assert cli.main(["skill", "install", "claude", "--skills-dir", str(custom)]) == 0
    assert (custom / "veil" / "SKILL.md").is_file()
    assert not (home / ".agents").exists()
    assert not (home / ".claude").exists()
    assert cli.main(["skill", "uninstall", "claude", "--skills-dir", str(custom)]) == 0
    assert not (custom / "veil").exists()


def test_custom_directory_rejects_all_clients(home):
    assert cli.main(["skill", "install", "--skills-dir", str(home)]) == 2
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("change", ["edited", "extra", "receipt", "symlink"])
def test_modified_skills_are_preserved(home, operation, change):
    assert cli.main(["skill", "install", "codex"]) == 0
    target = skill.skill_directory("codex")
    if change == "edited":
        (target / "SKILL.md").write_text("my custom instructions")
    elif change == "extra":
        (target / "notes.md").write_text("keep these notes")
    elif change == "receipt":
        (target / ".veil-skill.json").write_text("broken receipt")
    else:
        (target / "runtime.md").unlink()
        (target / "runtime.md").symlink_to(target / "SKILL.md")
    before = snapshot(home)
    assert cli.main(["skill", operation, "codex"]) == 2
    assert snapshot(home) == before


def test_collision_in_second_client_does_not_install_first(home):
    target = skill.skill_directory("claude")
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("a different veil skill")
    before = snapshot(home)
    assert cli.main(["skill", "install"]) == 2
    assert snapshot(home) == before
    assert not skill.skill_directory("codex").parent.exists()


def test_existing_target_symlink_is_not_followed(home):
    target = skill.skill_directory("codex")
    target.parent.mkdir(parents=True)
    other = home / "other-skill"
    other.mkdir()
    target.symlink_to(other, target_is_directory=True)
    assert cli.main(["skill", "install", "codex"]) == 2
    assert target.is_symlink()
    assert list(other.iterdir()) == []


def test_update_replaces_only_an_unchanged_owned_install(home, monkeypatch):
    assert cli.main(["skill", "install", "codex"]) == 0
    bundle = skill._bundle()
    bundle["SKILL.md"] += b"\nUpdated instructions.\n"
    monkeypatch.setattr(skill, "_bundle", lambda: bundle)
    assert cli.main(["skill", "install", "codex"]) == 0
    assert (skill.skill_directory("codex") / "SKILL.md").read_bytes() == bundle[
        "SKILL.md"
    ]
    assert cli.main(["skill", "uninstall", "codex"]) == 0


def test_failed_swap_restores_previous_install(home, monkeypatch):
    assert cli.main(["skill", "install", "codex"]) == 0
    before = snapshot(home)
    bundle = skill._bundle()
    bundle["SKILL.md"] += b"\nUpdated instructions.\n"
    monkeypatch.setattr(skill, "_bundle", lambda: bundle)
    rename = Path.rename

    def fail_new_folder(path, target):
        if path.name.startswith(".veil-skill-new-"):
            raise OSError("synthetic failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_new_folder)
    assert cli.main(["skill", "install", "codex"]) == 2
    assert snapshot(home) == before
    assert list(skill.skill_directory("codex").parent.iterdir()) == [
        skill.skill_directory("codex")
    ]


def test_locked_install_is_preserved(home):
    target = skill.skill_directory("codex")
    target.parent.mkdir(parents=True)
    lock = target.parent / ".veil-skill.lock"
    lock.write_text("another install")
    assert cli.main(["skill", "install", "codex"]) == 2
    assert lock.read_text() == "another install"
    assert not target.exists()


def test_installed_runtime_works_outside_checkout_and_ignores_pythonpath(home):
    assert cli.main(["skill", "install", "codex"]) == 0
    (home / "veil.py").write_text("raise RuntimeError('wrong module')")
    command = installed_runtime(skill.skill_directory("codex"))
    done = subprocess.run(
        [*command, "--help"],
        cwd=home,
        env={**os.environ, "PYTHONPATH": str(home)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert done.returncode == 0, done.stderr
    assert "Mask personal data" in done.stdout


@pytest.mark.skipif(os.name != "posix", reason="documented POSIX redirection workflow")
def test_skill_file_workflow_keeps_originals_out_of_tool_output(home):
    assert cli.main(["skill", "install", "claude"]) == 0
    command = installed_runtime(skill.skill_directory("claude"))
    data = home / "data"
    data.mkdir(mode=0o700)
    (data / "config.json").write_text('{"identity": false}')
    source, masked, restored = (
        home / name for name in ("email draft.txt", "masked.txt", "restored.txt")
    )
    original = "Contact jane.doe@example.com about the invoice.\n"
    source.write_text(original)

    def run(operation, input_path, output_path):
        shell = (
            "(umask 077; set -C; "
            + shlex.join(
                [
                    *command,
                    "--data-dir",
                    str(data),
                    operation,
                    "--session",
                    "email-demo",
                ]
            )
            + f" < {shlex.quote(str(input_path))} > {shlex.quote(str(output_path))})"
        )
        done = subprocess.run(
            ["/bin/sh", "-c", shell],
            cwd=home,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert original.strip() not in done.stdout + done.stderr
        assert done.stdout == ""
        return done.returncode

    assert run("mask", source, masked) == 0
    assert masked.read_text() == "Contact [EMAIL_1] about the invoice.\n"
    assert run("restore", masked, restored) == 0
    assert restored.read_text() == original
    assert source.read_text() == original
    assert restored.stat().st_mode & 0o777 == 0o600
    assert run("mask", source, restored) != 0
    assert restored.read_text() == original
