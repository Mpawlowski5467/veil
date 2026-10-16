"""Build a self-contained Apple Silicon app from an installed candidate wheel."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import sysconfig
from pathlib import Path

from PIL import Image

import veil

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    """Build and verify the bundle; never copy user configuration into artifacts."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("This build recipe currently targets Apple Silicon macOS.")
    if Path(veil.__file__).resolve().is_relative_to(ROOT):
        raise SystemExit(
            "Install the candidate wheel in the isolated build environment first."
        )
    output = ROOT / "dist" / "macos"
    generated = ROOT / "build" / "macos-resources"
    generated.mkdir(parents=True, exist_ok=True)
    with Image.open(ROOT / "src/veil/assets/veil-app-icon.png") as image:
        image.save(generated / "Veil.icns", format="ICNS")
    (generated / "LICENSE.txt").write_bytes((ROOT / "LICENSE").read_bytes())
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    (generated / "PYTHON-LICENSE.txt").write_bytes(python_license.read_bytes())
    distributions = sorted(
        importlib.metadata.distributions(), key=lambda dist: dist.metadata["Name"]
    )
    notices = []
    for dist in distributions:
        name = dist.metadata["Name"]
        notices.append(f"\n{'=' * 72}\n{name} {dist.version}\n")
        for item in dist.files or ():
            if any(
                word in item.name.lower() for word in ("license", "copying", "notice")
            ):
                path = Path(dist.locate_file(item))
                if path.is_file():
                    notices.append(path.read_text(encoding="utf-8", errors="replace"))
    (generated / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(notices))
    source_hash = hashlib.sha256()
    package_root = Path(veil.__file__).resolve().parent
    for path in sorted(package_root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            source_hash.update(str(path.relative_to(package_root)).encode())
            source_hash.update(path.read_bytes())
    manifest = {
        "version": veil.__version__,
        "architecture": "arm64",
        "build_os": platform.mac_ver()[0],
        "python": platform.python_version(),
        "source_state": "working-tree candidate; not a published release",
        "source_sha256": source_hash.hexdigest(),
        "signing": "Developer ID requested"
        if os.environ.get("VEIL_CODESIGN_IDENTITY")
        else "ad-hoc; not notarized",
        "dependencies": {d.metadata["Name"]: d.version for d in distributions},
    }
    (generated / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    environment = {
        **os.environ,
        "VEIL_BUNDLE_RESOURCES": str(generated),
        "PYINSTALLER_CONFIG_DIR": str(ROOT / "build" / "pyinstaller-cache"),
        "PYINSTALLER_STRICT_BUNDLE_CODESIGN_ERROR": "1",
    }
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(output),
            "--workpath",
            str(ROOT / "build" / "macos"),
            str(Path(__file__).with_name("Veil.spec")),
        ],
        env=environment,
        check=True,
    )
    subprocess.run(
        [
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            str(output / "Veil.app"),
        ],
        check=True,
    )
    print(f"Built {output / 'Veil.app'}")


if __name__ == "__main__":
    main()
