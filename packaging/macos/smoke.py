"""Exercise a copied standalone app with fictional data outside the checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def main() -> None:
    """Check a relocated bundle; never read or alter the user's live Veil data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--voice-model", type=Path)
    args = parser.parse_args()
    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="veil-clean-install-") as temporary:
        root = Path(temporary)
        app = root / "Applications with spaces" / "Veil.app"
        app.parent.mkdir()
        subprocess.run(
            ["/usr/bin/ditto", str(args.app.resolve()), str(app)], check=True
        )
        helper = app / "Contents/MacOS/veil-runtime"
        data = root / "private data"
        config = root / "codex/config.toml"
        manifest = json.loads(
            (app / "Contents/Resources/build-manifest.json").read_text()
        )
        hostile = root / "hostile"
        hostile.mkdir()
        (hostile / "veil.py").write_text(
            "raise RuntimeError('untrusted project import')"
        )
        environment = {
            **os.environ,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(hostile),
        }

        def run(*arguments: str, source: str = "", success: bool = True) -> str:
            result = subprocess.run(
                [str(helper), "--data-dir", str(data), *arguments],
                input=source,
                text=True,
                capture_output=True,
                cwd=hostile,
                env=environment,
                timeout=210,
            )
            if success and result.returncode:
                raise RuntimeError(f"{' '.join(arguments)} failed: {result.stderr}")
            return result.stdout.strip()

        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
            check=True,
        )
        for path in app.rglob("*"):
            if path.is_symlink():
                assert path.resolve(strict=True).is_relative_to(app.resolve()), path
        checks.append("relocated_bundle_signature_and_internal_links")
        assert "Mask personal data" in run("--help")
        assert not data.exists()
        checks.append("launch_without_system_python_or_checkout")
        run("entities", "add", "PERSON", "--stdin", source="Mira Quill")
        source = "Mira Quill: jane@example.com — José"
        masked = run("mask", "--session", "rc-text", source=source)
        original_text_masked = masked
        assert "Mira Quill" not in masked
        assert "jane@example.com" not in masked
        assert run("restore", "--session", "rc-text", source=masked) == source
        checks.append("unicode_registration_mask_restore")

        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 16 Tf 50 700 Td (jane@example.com) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        pdf = root / "fictional.pdf"
        writer.write(pdf)
        before = hashlib.sha256(pdf.read_bytes()).hexdigest()
        masked = run("mask", "--session", "rc-pdf", "--pdf", str(pdf))
        assert "EMAIL" in masked
        assert "jane@example.com" not in masked
        assert "jane@example.com" in run(
            "restore", "--session", "rc-pdf", source=masked
        )
        assert hashlib.sha256(pdf.read_bytes()).hexdigest() == before
        checks.append("pdf_worker_extract_mask_restore_original_unchanged")

        image = Image.new("RGB", (1400, 280), "white")
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 72)
        ImageDraw.Draw(image).text(
            (50, 70), "jane@example.com", fill="black", font=font
        )
        scan = root / "fictional.png"
        image.save(scan)
        masked = run("mask", "--session", "rc-image", "--ocr", str(scan))
        assert "EMAIL" in masked
        assert "jane@example.com" not in masked
        assert "jane@example.com" in run(
            "restore", "--session", "rc-image", source=masked
        )
        checks.append("apple_vision_image_worker_mask_restore")
        scanned_pdf = root / "scanned.pdf"
        image.save(scanned_pdf, format="PDF", resolution=150)
        masked = run("mask", "--session", "rc-scan", "--ocr", str(scanned_pdf))
        assert "EMAIL" in masked
        assert "jane@example.com" not in masked
        checks.append("scanned_pdf_render_and_ocr_worker")

        run("setup", "codex", "--config", str(config))
        assert config.exists()
        run("undo", "codex", "--config", str(config))
        assert not config.exists()
        checks.append("codex_setup_and_undo_in_disposable_config")
        skills = root / "skills"
        run("skill", "install", "codex", "--skills-dir", str(skills))
        runtime_text = (skills / "veil/runtime.md").read_text()
        pinned = json.loads(runtime_text.split("```json\n")[1].split("\n```", 1)[0])
        assert Path(pinned[0]).resolve() == helper.resolve()
        assert pinned[1:] == ["--veil-worker", "cli"]
        subprocess.run(
            [*pinned, "--help"],
            capture_output=True,
            env=environment,
            cwd=hostile,
            timeout=15,
            check=True,
        )
        run("skill", "uninstall", "codex", "--skills-dir", str(skills))
        assert not (skills / "veil").exists()
        checks.append("installed_skill_pins_relocated_runtime_and_uninstalls")

        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = str(port_socket.getsockname()[1])
        # Avoid Git identity lookup and all personal settings in lifecycle checks.
        settings = json.loads((data / "config.json").read_text())
        settings["identity"] = False
        (data / "config.json").write_text(json.dumps(settings))
        try:
            run(
                "start",
                "--port",
                port,
                "--api",
                "anthropic",
                "--auth",
                "api-key",
                "--cwd",
                str(root),
            )
            status = json.loads(run("status", "--service", "--json"))["service"]
            assert status["state"] == "running"
            run("restart")
            assert (
                json.loads(run("status", "--service", "--json"))["service"]["state"]
                == "running"
            )
        finally:
            run("stop", success=False)
        assert (
            json.loads(run("status", "--service", "--json", success=False))["service"][
                "state"
            ]
            == "stopped"
        )
        checks.append("gateway_helper_survives_launcher_restart_and_stops")

        if args.voice_model:
            from veil.voice_model import _FILES, validate_model

            model = data / "voice-model"
            model.mkdir()
            for name in _FILES:
                shutil.copyfile(args.voice_model / name, model / name)
            validate_model(data)
            assert "not installed" not in run("voice", "status").lower()
            assert run("voice", "status").endswith("installed")
            audio = root / "fictional.aiff"
            subprocess.run(
                [
                    "/usr/bin/say",
                    "-o",
                    str(audio),
                    "Please call Jane Example tomorrow about the meeting. "
                    "The project is called Veil.",
                ],
                check=True,
                timeout=30,
            )
            run("entities", "add", "PERSON", "--stdin", source="meeting")
            transcript = run("mask", "--session", "rc-voice", "--audio", str(audio))
            assert "meeting" not in transcript.lower()
            assert "PERSON" in transcript
            assert (
                "meeting"
                in run("restore", "--session", "rc-voice", source=transcript).lower()
            )
            checks.append(
                "offline_voice_worker_decodes_and_transcribes_fictional_audio"
            )
        else:
            assert "not installed" in run("voice", "status").lower()
            checks.append("voice_model_remains_explicit_optional_download")

        shutil.rmtree(app)
        assert data.exists()
        subprocess.run(
            ["/usr/bin/ditto", str(args.app.resolve()), str(app)], check=True
        )
        assert (
            run(
                "restore",
                "--session",
                "rc-text",
                source=original_text_masked,
            )
            == source
        )
        checks.append("remove_and_reinstall_preserves_separate_saved_mappings")
        report = {
            "version": manifest["version"],
            "architecture": manifest["architecture"],
            "source_sha256": manifest["source_sha256"],
            "scope": "fictional_relocated_bundle_only",
            "checks": checks,
            "microphone": "not_opened",
            "live_providers": "not_called",
            "login_reboot": "not_tested",
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
