"""Native desktop controls and an optional menu-bar/system-tray entry point."""

from __future__ import annotations

import importlib
import json
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from . import service
from .detectors.literal import LiteralPlaceholderDetector
from .detectors.regex import RegexDetector
from .entities_cli import _check_type, _edit
from .gateway.config import SettingsError, load_settings, name_detector
from .gateway.store import literal_types, registered_values
from .names_cli import configure_names
from .shield import Shield
from .startup import isolated_command, run_startup, startup_status

if TYPE_CHECKING:
    from .microphone import MicrophoneRecording
    from .ocr import OCRText
    from .pdf_text import PDFText
    from .voice import VoiceText

PROVIDER_MODES = {
    "OpenAI · ChatGPT sign-in (Codex)": ("openai", "chatgpt"),
    "OpenAI · API key": ("openai", "api-key"),
    "Anthropic · API key / Claude Code": ("anthropic", "api-key"),
}


class DesktopController:
    """Share the existing CLI operations without a second gateway implementation."""

    def __init__(self, data_dir: Path | None, config: Path | None = None) -> None:
        """Resolve the same data folder and optional Codex setup as the CLI."""
        self.directory = service.service_directory(data_dir, config)
        self.config = config

    def snapshot(self) -> dict[str, Any]:
        """Return lifecycle facts, never claim coverage for an active task."""
        settings = load_settings(self.directory / "config.json")
        from .voice_model import model_ready

        return {
            "gateway": service.service_status(self.directory),
            "startup": startup_status(self.directory),
            "names": settings.local_names_model is not None,
            "voice": model_ready(self.directory),
            "registrations": sum(len(values) for values in settings.entities.values()),
        }

    def gateway(self, operation: Literal["start", "stop", "restart"]) -> str:
        """Start, stop, or restart through the existing lifecycle locks."""
        service.run_service(operation, data_dir=self.directory, config=self.config)
        return f"Gateway: {service.service_status(self.directory)['state']}"

    def provider(self, mode: str) -> str:
        """Apply an explicit provider choice through the normal lifecycle lock."""
        if mode not in PROVIDER_MODES:
            raise SettingsError("choose an available provider mode")
        if startup_status(self.directory)["state"] != "disabled":
            raise SettingsError(
                "Disable login startup before changing provider mode, "
                "then enable it again."
            )
        api, auth = PROVIDER_MODES[mode]
        service.run_service(
            "restart", data_dir=self.directory, config=self.config, api=api, auth=auth
        )
        return (
            "Provider applied. Connect your client to this gateway; "
            "client settings are unchanged."
        )

    def startup(self, enabled: bool) -> str:
        """Change user login startup only after an explicit UI action."""
        run_startup(
            "enable" if enabled else "disable",
            data_dir=self.directory,
            config=self.config,
        )
        return (
            "Login startup enabled."
            if enabled
            else "Login startup disabled; gateway stopped."
        )

    def names(self, model: Path | None) -> str:
        """Validate and save optional recognition without downloading models."""
        configure_names(self.directory, model)
        return "Name recognition settings saved. Restart the gateway to apply."

    def register(self, kind: str, value: str) -> str:
        """Register one private value without command arguments or logging it."""
        _check_type(kind)
        if (
            not value.strip()
            or len(value) > 16_384
            or any(
                ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029"
                for char in value
            )
        ):
            raise SettingsError(
                "enter one non-blank private value without control characters"
            )
        _edit(self.directory, "add", kind, value)
        return "Value registered. Restart the gateway to apply."

    def preview(self, text: str) -> str:
        """Preview configured rules in memory; do not persist text or mappings."""
        if len(text) > 32_768:
            raise SettingsError("preview accepts up to 32768 characters")
        settings = load_settings(self.directory / "config.json")
        from .gateway.config import git_identity

        identity = git_identity() if settings.identity else {}
        shield = Shield(
            detectors=[
                LiteralPlaceholderDetector(literal_types(settings, identity)),
                RegexDetector(settings.patterns),
                *name_detector(settings),
            ],
            redact_warnings=True,
        )
        for value, kind in registered_values(settings, identity).items():
            shield.add_entity(value, kind)
        masked = shield.mask(text)
        if settings.secret_review:
            from .secret_review import candidates

            visible = list(text)
            for entity in masked.entities:
                visible[entity.start : entity.end] = " " * (entity.end - entity.start)
            if candidates(text, visible="".join(visible)):
                raise SettingsError(
                    "Uncertain values need local review. Use veil mask --review "
                    "with a session before sharing this text."
                )
        restored = shield.restore(masked.text, tolerant=False)
        if masked.warnings or restored.warnings or restored.text != text:
            raise SettingsError(
                "preview did not pass exact restoration; no output produced"
            )
        return masked.text

    def diagnostics(self) -> str:
        """Run diagnostics separately so CLI output cannot block the UI thread."""
        arguments = ["doctor", "--json"]
        if self.config is not None:
            arguments += ["--config", str(self.config)]
        done = subprocess.run(
            isolated_command(self.directory, *arguments),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        try:
            return json.dumps(json.loads(done.stdout), indent=2)
        except ValueError:
            raise SettingsError(
                "diagnostics could not be loaded; run veil doctor in a terminal"
            ) from None

    def import_pdf(self, path: Path) -> PDFText:
        """Read a local PDF into the ephemeral preview, without saving its text."""
        from .pdf_text import extract_pdf

        return extract_pdf(path)

    def import_scan(self, path: Path) -> OCRText:
        """Recognize PDF/image pixels locally without persisting the source."""
        from .ocr import extract_ocr

        return extract_ocr(path)

    def setup_voice(self) -> str:
        """Install the local speech model only after an explicit setup action."""
        from .voice_model import setup_model

        return setup_model(self.directory)

    def import_audio(self, path: Path) -> VoiceText:
        """Transcribe a local audio file into the editable preview."""
        from .voice import transcribe_file

        return transcribe_file(path, self.directory)

    def start_recording(self) -> MicrophoneRecording:
        """Check model readiness before opening the microphone."""
        from .microphone import MicrophoneRecording
        from .voice_model import require_voice, validate_model

        require_voice()
        validate_model(self.directory)
        return MicrophoneRecording()

    def transcribe_recording(self, recording: MicrophoneRecording) -> VoiceText:
        """Close microphone capture before sending its in-memory WAV to the worker."""
        from .voice import transcribe_audio

        return transcribe_audio(recording.stop(), self.directory)


def run_desktop(data_dir: Path | None = None, config: Path | None = None) -> int:
    """Open native controls without changing gateway or client settings."""
    from .desktop_ui import DesktopWindow

    return DesktopWindow(DesktopController(data_dir, config)).run()


def run_tray(data_dir: Path | None = None, config: Path | None = None) -> int:
    """Run the tray on the main thread, including the macOS application runloop."""
    try:
        pystray = importlib.import_module("pystray")
        importlib.import_module("PIL.Image")
    except ImportError:
        raise SettingsError(
            "menu-bar controls need the optional 'veil[tray]' extra"
        ) from None
    except Exception:
        raise SettingsError(
            "the tray backend could not connect to a graphical desktop"
        ) from None
    controller = DesktopController(data_dir, config)
    status = {"text": "Veil · checking gateway"}
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="veil-tray")
    stopped = threading.Event()

    def action(work: Callable[[], Any]) -> Callable[..., None]:
        def callback(*_: Any) -> None:
            def execute() -> None:
                try:
                    work()
                    status["text"] = (
                        "Veil · "
                        + service.service_status(controller.directory)["state"]
                    )
                except Exception:
                    status["text"] = "Veil · action failed; open controls"
                icon.title = status["text"]
                icon.update_menu()

            executor.submit(execute)

        return callback

    def open_controls() -> None:
        arguments = ["desktop"]
        if config:
            arguments += ["--config", str(config)]
        process = subprocess.Popen(
            isolated_command(controller.directory, *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=process.wait, daemon=True).start()

    def quit_tray(*_: Any) -> None:
        stopped.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: status["text"], None, enabled=False),
        pystray.MenuItem("Open Veil controls", action(open_controls), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start gateway", action(lambda: controller.gateway("start"))),
        pystray.MenuItem("Stop gateway", action(lambda: controller.gateway("stop"))),
        pystray.MenuItem(
            "Restart gateway", action(lambda: controller.gateway("restart"))
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit menu (gateway stays running)", quit_tray),
    )
    from .tray_icon import create_tray_icon

    icon = create_tray_icon(pystray, status["text"], menu)

    def setup(_: Any) -> None:
        icon.visible = True
        while not stopped.wait(5):
            status["text"] = (
                "Veil · " + service.service_status(controller.directory)["state"]
            )
            icon.title = status["text"]
            icon.update_menu()

    try:
        icon.run(setup=setup)
    finally:
        stopped.set()
        executor.shutdown(wait=False, cancel_futures=True)
    return 0
