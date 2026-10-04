"""The beta pack reports clear next steps without exposing local diagnostics."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "beta_check", Path(__file__).resolve().parents[1] / "beta" / "check.py"
)
assert SPEC is not None
assert SPEC.loader is not None
beta_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(beta_check)

STEP_CODES = ("registration", "mask", "restore", "forget", "registration_removal")


def test_successful_local_check_keeps_report_schema_and_offers_client_exercise(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["check.py", "--output", str(output)])
    assert beta_check.main() == 0
    report = json.loads(output.read_text())
    assert set(report) == {
        "schema",
        "veil_version",
        "python_version",
        "platform",
        "scope",
        "client_journey",
        "checks",
    }
    assert report["scope"] == "fictional_local_cli_only"
    assert report["client_journey"] == "not_run"
    assert report["checks"] == [{"code": code, "state": "pass"} for code in STEP_CODES]
    text = capsys.readouterr().out
    assert "PASS: Register a fictional name" in text
    assert "PASS: Restore the original fictional text" in text
    assert "All five local checks passed." in text
    assert "optional client exercises in README.md" in text
    assert "No report was sent." in text


def test_failed_step_stops_later_checks_and_guidance_without_raw_error(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["check.py", "--output", str(output)])
    calls = []

    def fail_mask(root, *args, text=""):
        calls.append(args[0])
        if args[0] == "mask":
            raise beta_check.CheckError("private diagnostic must not be printed")
        return ""

    monkeypatch.setattr(beta_check, "command", fail_mask)
    assert beta_check.main() == 1
    assert calls == ["entities", "mask"]
    report_text = output.read_text()
    assert json.loads(report_text)["checks"] == [
        {"code": code, "state": state}
        for code, state in zip(
            STEP_CODES, ("pass", "fail", "not_run", "not_run", "not_run"), strict=True
        )
    ]
    text = capsys.readouterr().out
    assert "FAIL: Mask the fictional name, email, and password" in text
    assert "NOT RUN: Restore the original fictional text" in text
    assert "Stop here: a local check failed." in text
    assert "Report the failed step" in text
    assert "All five local checks passed" not in text
    assert "Continue with" not in text
    assert "private diagnostic" not in text + report_text


def test_existing_report_is_preserved_and_no_checks_run(tmp_path, monkeypatch, capsys):
    output = tmp_path / "result.json"
    output.write_text("existing report")
    monkeypatch.setattr(sys, "argv", ["check.py", "--output", str(output)])

    def forbidden():
        pytest.fail("checks ran despite an existing report")

    monkeypatch.setattr(beta_check, "check", forbidden)
    assert beta_check.main() == 2
    assert output.read_text() == "existing report"
    assert "Choose a writable, unused --output filename." in capsys.readouterr().out
