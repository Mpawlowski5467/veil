"""Create and verify a drag-to-Applications disk image from the built app."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    """Stage only the signed app, an Applications link, and installation notes."""
    output = Path(__file__).resolve().parents[2] / "dist/macos"
    app = output / "Veil.app"
    manifest = json.loads((app / "Contents/Resources/build-manifest.json").read_text())
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True
    )
    version = manifest["version"]
    image = output / f"Veil-{version}-macos-arm64.dmg"
    with tempfile.TemporaryDirectory(prefix="veil-dmg-stage-") as temporary:
        staging = Path(temporary)
        subprocess.run(
            ["/usr/bin/ditto", str(app), str(staging / "Veil.app")], check=True
        )
        (staging / "Applications").symlink_to("/Applications")
        (staging / "INSTALL.txt").write_text(
            f"Veil {version} - Apple Silicon local release candidate\n\n"
            "Drag Veil.app into Applications, then open it from Applications.\n"
            "Python, Tk, OCR, and voice libraries are included. "
            "Models remain optional.\n"
            "Opening the app does not enable login startup "
            "or change client routing.\n\n"
            "This local candidate is ad-hoc signed and is NOT notarized by Apple.\n"
            "A downloaded copy may be blocked by macOS. Do not disable Gatekeeper.\n"
            "Public distribution requires Developer ID signing and notarization.\n\n"
            "Before replacing or moving an existing app, disable Veil login startup,\n"
            "stop its gateway, and close Veil. Reinstall its assistant skill after\n"
            "moving the app. Saved settings/mappings live separately in ~/.veil.\n"
            "To uninstall, disable startup, stop the gateway, undo any client routing\n"
            "you configured, then move the app to Trash. Keep private data for\n"
            "restoration; deleting it is a separate, irreversible decision.\n\n"
            "Tested locally on Apple Silicon macOS 26.5.1 only.\n"
        )
        subprocess.run(
            [
                "/usr/bin/hdiutil",
                "create",
                "-volname",
                f"Veil {version}",
                "-srcfolder",
                str(staging),
                "-ov",
                "-format",
                "UDZO",
                str(image),
            ],
            check=True,
        )
    subprocess.run(["/usr/bin/hdiutil", "verify", str(image)], check=True)
    checksum = hashlib.sha256(image.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{checksum}  {image.name}\n")
    print(image)


if __name__ == "__main__":
    main()
