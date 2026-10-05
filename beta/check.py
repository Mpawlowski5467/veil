"""Check an installed Veil with fictional data and write an allowlisted report.

Run with the Python interpreter from the environment where Veil was installed.
This uses temporary storage, never changes client settings or the clipboard,
and makes no model calls. It is not a substitute for the manual beta journey.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ORIGINAL = (
    'Contact Mira Quill at mira.quill@example.org.\nPASSWORD="orchard-demo-724"\n'
)
MASKED = 'Contact [PERSON_1] at [EMAIL_1].\nPASSWORD="[PASSWORD_1]"\n'
STEP_LABELS = {
    "installation": "Load the installed Veil package",
    "storage": "Create private temporary storage",
    "registration": "Register a fictional name",
    "mask": "Mask the fictional name, email, and password",
    "restore": "Restore the original fictional text",
    "forget": "Forget the test conversation mappings",
    "registration_removal": "Remove the fictional name registration",
}


class CheckError(Exception):
    """A check failed; never include captured commands, paths, or output."""


def command(root: Path, *args: str, text: str = "") -> str:
    """Run a public CLI command against disposable storage."""
    result = subprocess.run(
        [sys.executable, "-I", "-m", "veil", "--data-dir", str(root), *args],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=root,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise CheckError("command_failed")
    return result.stdout


def check() -> dict:
    """Return only software versions and fixed outcome codes."""
    report = {
        "schema": 1,
        "veil_version": None,
        "python_version": ".".join(str(v) for v in sys.version_info[:3]),
        "platform": platform.system()
        if platform.system() in {"Darwin", "Linux", "Windows"}
        else "other",
        "scope": "fictional_local_cli_only",
        "client_journey": "not_run",
        "checks": [],
    }
    try:
        from veil import __version__
        from veil.gateway import prepare_data_dir
    except ImportError:
        report["checks"].append({"code": "installation", "state": "fail"})
        return report
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?", __version__):
        report["veil_version"] = __version__
    with tempfile.TemporaryDirectory(prefix="veil-beta-") as directory:
        # Python 3.14's Windows temporary-folder ACL uses OWNER RIGHTS. Let
        # Veil create its own child with the same explicit ACL as normal use.
        # Do not relax production permission validation for this exercise.
        try:
            root = prepare_data_dir(Path(directory) / "data")
        except (OSError, ValueError):
            report["checks"].append({"code": "storage", "state": "fail"})
            return report
        (root / "config.json").write_text(
            '{"identity":false,"secret_review":false}', encoding="utf-8"
        )
        steps = (
            (
                "registration",
                ("entities", "add", "PERSON", "--stdin"),
                "Mira Quill",
                None,
            ),
            ("mask", ("mask", "--session", "beta-local"), ORIGINAL, MASKED),
            (
                "restore",
                ("restore", "--session", "beta-local", "--exact"),
                MASKED,
                ORIGINAL,
            ),
            ("forget", ("forget", "--session", "beta-local"), "", None),
            (
                "registration_removal",
                ("entities", "remove", "PERSON", "--stdin"),
                "Mira Quill",
                None,
            ),
        )
        failed = False
        for name, args, source, expected in steps:
            if failed:
                report["checks"].append({"code": name, "state": "not_run"})
                continue
            try:
                result = command(root, *args, text=source)
                if expected is not None and result != expected:
                    raise CheckError("unexpected_result")
            except (CheckError, OSError, UnicodeError, subprocess.SubprocessError):
                report["checks"].append({"code": name, "state": "fail"})
                failed = True
            else:
                report["checks"].append({"code": name, "state": "pass"})
    return report


def main() -> int:
    """Save a report only to a new file; share it manually if desired."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("beta-check.json"))
    args = parser.parse_args()
    try:
        # Exclusive creation prevents accidentally replacing an existing report.
        with args.output.open("x", encoding="utf-8") as output:
            report = check()
            json.dump(report, output, indent=2)
            output.write("\n")
    except OSError:
        print("Cannot create report. Choose a writable, unused --output filename.")
        return 2
    for step in report["checks"]:
        state = step["state"].upper().replace("_", " ")
        print(f"{state}: {STEP_LABELS[step['code']]}")
    print("Report saved. No report was sent.")
    passed = all(step["state"] == "pass" for step in report["checks"])
    if passed:
        print(
            "All five local checks passed. Continue with the optional client "
            "exercises in README.md."
        )
    else:
        print(
            "Stop here: a local check failed. Report the failed step before "
            "continuing to the client exercises. You can attach the saved "
            "report after reviewing it."
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
