"""A bundled runtime stays pinned and dispatches only known entry points."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from veil import cli, runtime, startup
from veil.gateway.config import SettingsError


def test_python_worker_is_isolated_from_cwd_and_pythonpath(tmp_path):
    (tmp_path / "veil.py").write_text("raise RuntimeError('untrusted module')")
    result = subprocess.run(
        runtime.command("cli", "--help"),
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "Mask personal data" in result.stdout


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    folder = tmp_path / "Veil app with spaces.app" / "Contents" / "MacOS"
    folder.mkdir(parents=True)
    helper = folder / "veil-runtime"
    helper.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(folder / "Veil"))
    return helper


@pytest.mark.parametrize("target", ["cli", "gateway", "pdf", "ocr", "voice"])
def test_frozen_workers_use_console_helper_and_literal_arguments(frozen, target):
    assert runtime.command(target, "a path with $quotes'", "plain") == [
        str(frozen),
        "--veil-worker",
        target,
        "a path with $quotes'",
        "plain",
    ]


def test_missing_bundle_helper_refuses_instead_of_using_system_python(frozen):
    frozen.unlink()
    with pytest.raises(SettingsError, match="incomplete"):
        runtime.command("pdf")


def test_frozen_startup_hook_and_skill_pin_same_helper(frozen, tmp_path):
    from veil.skill import _bundle

    expected = [str(frozen), "--veil-worker", "cli"]
    assert startup.isolated_command(tmp_path, "status") == [
        *expected,
        "--data-dir",
        str(tmp_path),
        "status",
    ]
    assert cli.hook_command(tmp_path) == [
        *expected,
        "--data-dir",
        str(tmp_path),
        "hook",
    ]
    text = _bundle()["runtime.md"].decode()
    argv = json.loads(text.split("```json\n")[1].split("\n```", 1)[0])
    assert argv == expected


@pytest.mark.parametrize(
    "arguments", [["--veil-worker"], ["--veil-worker", "os.system"]]
)
def test_unknown_or_missing_worker_target_never_imports_code(arguments, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["Veil", *arguments])
    monkeypatch.setattr(
        runtime.importlib, "import_module", lambda _: pytest.fail("unexpected import")
    )
    assert runtime.main() == 2


def test_worker_receives_only_its_own_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["Veil", "--veil-worker", "voice", "/data"])
    observed = []

    def worker():
        observed.append(list(sys.argv))
        return 7

    monkeypatch.setattr(
        runtime.importlib, "import_module", lambda name: SimpleNamespace(_worker=worker)
    )
    assert runtime.main() == 7
    assert observed == [["Veil", "/data"]]


def test_finder_launch_opens_desktop_and_cli_keeps_arguments(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda arguments: arguments)
    monkeypatch.setattr(sys, "executable", "/Applications/Veil.app/Contents/MacOS/Veil")
    monkeypatch.setattr(sys, "argv", ["Veil"])
    assert runtime.main() == ["desktop"]
    monkeypatch.setattr(sys, "argv", ["Veil", "--help"])
    assert runtime.main() == ["--help"]
