"""Install a built wheel alone, then its desktop extra, in a clean environment."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

from veil import __version__

BASE = """
import importlib.util
from veil import Shield
from veil.preview import _HTML, preview
assert "See what gets masked." in _HTML
result = preview({"text": "fictional@example.org", "choices": {}})
assert result["masked"] == "[EMAIL_1]"
from veil.support_report import safe_checks
assert safe_checks({"checks": []}) == []
assert importlib.util.find_spec("tomlkit") is None
shield = Shield()
text = "fictional@example.com"
masked = shield.mask(text).text
assert masked == "[EMAIL_1]"
assert shield.restore(masked).text == text
"""
DESKTOP = """
import json
import subprocess
import tempfile
from pathlib import Path
from veil.cli import main
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    config = root / "codex" / "config.toml"
    args = ["--data-dir", str(root / "data"), "setup", "codex"]
    assert main([*args, "--config", str(config)]) == 0
    assert main(["undo", "codex", "--config", str(config)]) == 0
    assert not config.exists()
    skills = root / "skills"
    assert main(["skill", "install", "codex", "--skills-dir", str(skills)]) == 0
    runtime = (skills / "veil" / "runtime.md").read_text(encoding="utf-8")
    argv = json.loads(runtime.split("```json\\n",1)[1].split("\\n```",1)[0])
    subprocess.run([*argv, "--help"], check=True, capture_output=True, timeout=15)
    assert main(["skill", "uninstall", "codex", "--skills-dir", str(skills)]) == 0
"""


def main():
    """Check the wheel rather than the source checkout on every target OS."""
    (wheel,) = Path("dist").glob(f"veil-{__version__}-*.whl")
    wheel = wheel.resolve()
    pack_builder = Path(__file__).with_name("beta_pack.py").resolve()
    with tempfile.TemporaryDirectory(prefix="veil-wheel-") as tmp:
        root = Path(tmp)
        env = root / "env"
        subprocess.run(["uv", "venv", str(env)], check=True)
        python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        for spec, program in ((str(wheel), BASE), (f"{wheel}[desktop]", DESKTOP)):
            subprocess.run(
                ["uv", "pip", "install", "--python", str(python), spec], check=True
            )
            subprocess.run(
                [str(python), "-I", "-c", program], cwd=root, check=True, timeout=60
            )
        subprocess.run(
            [str(python), "-I", str(pack_builder), "--output-dir", str(root)],
            cwd=root,
            check=True,
            timeout=30,
        )
        pack_name = f"veil-{__version__}-beta"
        with zipfile.ZipFile(root / f"{pack_name}.zip") as pack:
            pack.extractall(root)
        exercise = root / pack_name
        report_path = root / "beta-check.json"
        subprocess.run(
            [str(python), "-I", "check.py", "--output", str(report_path)],
            cwd=exercise,
            check=True,
            timeout=60,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["veil_version"] == __version__
        assert report["scope"] == "fictional_local_cli_only"
        assert report["client_journey"] == "not_run"
        assert report["checks"] == [
            {"code": code, "state": "pass"}
            for code in (
                "registration",
                "mask",
                "restore",
                "forget",
                "registration_removal",
            )
        ]
    print(
        "base wheel, preview, report, desktop setup/undo, installed skill, "
        "and packaged beta check: passed"
    )


if __name__ == "__main__":
    main()
