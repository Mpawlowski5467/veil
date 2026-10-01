"""Install a built wheel alone, then its desktop extra, in a clean environment."""

from __future__ import annotations

import os
import subprocess
import tempfile
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
    print(
        "base wheel, preview, report, desktop setup/undo, and installed skill: passed"
    )


if __name__ == "__main__":
    main()
