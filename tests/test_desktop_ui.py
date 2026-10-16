"""Opt-in checks against a real Tk event loop, without changing gateway state."""

import gc
import os
import threading
import time

import pytest

from veil.desktop import DesktopController
from veil.desktop_ui import DesktopWindow
from veil.gateway.config import SettingsError

pytestmark = pytest.mark.skipif(
    os.environ.get("VEIL_NATIVE_UI") != "1",
    reason="set VEIL_NATIVE_UI=1 on a graphical desktop to exercise native Tk",
)


@pytest.fixture
def window(tmp_path):
    # Tcl objects from prior tests must be collected on this thread before a
    # new background action can trigger Python's cyclic garbage collector.
    gc.collect()
    app = DesktopWindow(DesktopController(tmp_path / "absent"))
    app.root.withdraw()
    yield app
    if not app.closed:
        app.close()
    app.executor.shutdown(wait=True, cancel_futures=True)


def pump(window, until):
    deadline = time.monotonic() + 3
    while not until():
        window.root.update()
        if time.monotonic() >= deadline:
            pytest.fail("native UI did not complete its background action")
        time.sleep(0.01)


def test_logo_animates_without_blocking_and_close_cancels_callbacks(window):
    release = threading.Event()

    def work():
        assert release.wait(3)
        return "Done"

    window.submit(work, "Working locally…")
    window._receive()
    phase = window.phase
    try:
        pump(window, lambda: window.phase != phase)
        assert window.loader.itemcget(window.arc, "extent") != "0.0"
        assert window.app_icon.width() >= 256
        assert window.app_icon.transparency_get(0, 0)
        assert window.mark.transparency_get(0, 0)
        assert window.mark.width() > 30
        window.close()
    finally:
        release.set()


def test_error_stops_loader_and_survives_quiet_status_refresh(window):
    def fail():
        raise SettingsError("Choose a trusted local model folder.")

    window.submit(fail, "Validating…")
    window._receive()
    pump(window, lambda: not window.pending)
    assert window.notice.get() == "Choose a trusted local model folder."
    assert float(window.loader.itemcget(window.arc, "extent")) == 0
    assert window.animation is None
    assert not window.controller.directory.exists()


def test_provider_selection_is_explicit_and_survives_status_refresh(
    window, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        window.controller, "provider", lambda mode: calls.append(mode) or "Applied"
    )
    snapshot = {
        "gateway": {
            "state": "running",
            "api": "openai",
            "auth": "chatgpt",
            "port": 8485,
        },
        "startup": {"state": "disabled"},
        "names": False,
        "registrations": 0,
    }
    window._update_snapshot(snapshot)
    assert window.provider_mode.get() == "OpenAI · ChatGPT sign-in (Codex)"
    window.provider_mode.set("Anthropic · API key / Claude Code")
    window.provider_choice.event_generate("<<ComboboxSelected>>")
    window._update_snapshot(snapshot)
    assert window.provider_mode.get().startswith("Anthropic")
    assert calls == []
    window._apply_provider()
    window._receive()
    pump(window, lambda: bool(calls) and not window.provider_dirty)
    assert calls == ["Anthropic · API key / Claude Code"]


def test_provider_apply_unavailable_when_startup_controls_mode(window):
    snapshot = {
        "gateway": {
            "state": "running",
            "api": "anthropic",
            "auth": "api-key",
            "port": 8484,
        },
        "startup": {"state": "enabled"},
        "names": False,
        "registrations": 0,
    }
    window._update_snapshot(snapshot)
    assert str(window.apply_provider["state"]) == "disabled"
    assert "http://127.0.0.1:8484" in window.provider_label["text"]


def test_only_visible_page_participates_in_keyboard_navigation(window):
    window.show_page("Masking preview")
    assert window.pages["Masking preview"].grid_info()
    assert not window.pages["Overview"].grid_info()
    assert not window.pages["Settings"].grid_info()
    window.source.insert("1.0", "Fictional input")
    window.show_page("Settings")
    window.show_page("Masking preview")
    assert window.source.get("1.0", "end-1c") == "Fictional input"
    window._clear_preview()
    with pytest.raises(window.tk.TclError, match="nothing to undo"):
        window.source.edit_undo()


def test_logo_fallback_works_without_pillow(tmp_path, monkeypatch):
    import importlib

    real_import = importlib.import_module

    def without_pillow(name, *args, **kwargs):
        if name == "PIL.Image":
            raise ImportError("optional dependency absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("veil.desktop_ui.importlib.import_module", without_pillow)
    app = DesktopWindow(DesktopController(tmp_path / "absent"))
    try:
        assert 32 <= app.mark.width() <= 64
        assert 256 <= app.app_icon.width() <= 512
        assert app.app_icon.transparency_get(0, 0)
        assert app.brand_name.cget("text") == "veil"
    finally:
        app.close()


def test_pdf_import_updates_preview_without_retaining_previous_undo(
    window, monkeypatch
):
    from veil.pdf_text import PDFText

    monkeypatch.setattr(window.filedialog, "askopenfilename", lambda **_: "/local.pdf")
    monkeypatch.setattr(
        window.controller,
        "import_pdf",
        lambda _: PDFText("jane@example.com", 2, 1),
    )
    window.source.insert("1.0", "previous private text")
    window._set_output("previous output")
    window._import_pdf()
    assert window.pending
    assert window.output.get("1.0", "end-1c") == ""
    window._receive()
    pump(window, lambda: window.source.get("1.0", "end-1c") == "jane@example.com")
    assert "1 page had no text" in window.pdf_detail.cget("text")
    assert "original PDF is unchanged" in window.notice.get()
    with pytest.raises(window.tk.TclError, match="nothing to undo"):
        window.source.edit_undo()
    window._clear_preview()
    assert window.source.get("1.0", "end-1c") == ""
    assert "Read 2 PDF pages" not in window.pdf_detail.cget("text")


def test_pdf_import_failure_preserves_existing_input(window, monkeypatch):
    def fail(_):
        raise SettingsError("Scanned pages need OCR support.")

    monkeypatch.setattr(window.filedialog, "askopenfilename", lambda **_: "/local.pdf")
    monkeypatch.setattr(window.controller, "import_pdf", fail)
    window.source.insert("1.0", "unfinished input")
    window._import_pdf()
    window._receive()
    pump(window, lambda: "OCR support" in window.notice.get())
    assert window.source.get("1.0", "end-1c") == "unfinished input"
    assert window.output.get("1.0", "end-1c") == ""


def test_cancelled_pdf_picker_preserves_preview(window, monkeypatch):
    monkeypatch.setattr(window.filedialog, "askopenfilename", lambda **_: "")
    window.source.insert("1.0", "unfinished input")
    window._set_output("previous output")
    window._import_pdf()
    assert not window.pending
    assert window.source.get("1.0", "end-1c") == "unfinished input"
    assert window.output.get("1.0", "end-1c") == "previous output"


def test_scan_import_explains_review_and_clears_previous_undo(window, monkeypatch):
    from veil.ocr import OCRText

    monkeypatch.setattr(window.filedialog, "askopenfilename", lambda **_: "/scan.png")
    monkeypatch.setattr(
        window.controller, "import_scan", lambda _: OCRText("jane@example.com", 2, 1)
    )
    window.source.insert("1.0", "previous private text")
    window._set_output("previous output")
    window._import_scan()
    assert window.pending
    assert window.output.get("1.0", "end-1c") == ""
    window._receive()
    pump(window, lambda: not window.pending)
    assert window.source.get("1.0", "end-1c") == "jane@example.com"
    assert "1 page had no recognized text" in window.pdf_detail.cget("text")
    assert "review before sharing" in window.pdf_detail.cget("text")
    assert "Correct any misread details" in window.notice.get()
    with pytest.raises(window.tk.TclError, match="nothing to undo"):
        window.source.edit_undo()


def test_scan_failure_preserves_input_and_cancel_preserves_preview(window, monkeypatch):
    def fail(_):
        raise SettingsError("OCR found no readable text.")

    monkeypatch.setattr(window.filedialog, "askopenfilename", lambda **_: "/scan.png")
    monkeypatch.setattr(window.controller, "import_scan", fail)
    window.source.insert("1.0", "unfinished input")
    window._set_output("previous output")
    window._import_scan()
    window._receive()
    pump(window, lambda: not window.pending)
    assert window.source.get("1.0", "end-1c") == "unfinished input"
    assert window.output.get("1.0", "end-1c") == ""
    monkeypatch.setattr(window.filedialog, "askopenfilename", lambda **_: "")
    window._set_output("current output")
    window._import_scan()
    assert not window.pending
    assert window.output.get("1.0", "end-1c") == "current output"


def test_audio_import_enters_editable_review_without_undo(window, monkeypatch):
    from veil.voice import VoiceText

    monkeypatch.setattr(window.filedialog, "askopenfilename", lambda **_: "/voice.wav")
    monkeypatch.setattr(
        window.controller, "import_audio", lambda _: VoiceText("Jane example", 2)
    )
    window.source.insert("1.0", "unfinished text")
    window._set_output("old output")
    window._import_audio()
    assert window.pending
    assert window.output.get("1.0", "end-1c") == ""
    window._receive()
    pump(window, lambda: window.source.get("1.0", "end-1c") == "Jane example")
    assert "review before sharing" in window.pdf_detail.cget("text")
    assert "Correct misheard" in window.notice.get()
    with pytest.raises(window.tk.TclError, match="nothing to undo"):
        window.source.edit_undo()


class FakeRecording:
    def __init__(self):
        self.finished = threading.Event()
        self.cancelled = threading.Event()
        self.stopped = False

    def cancel(self):
        self.cancelled.set()

    def stop(self):
        self.stopped = True
        return b"fictional audio"


def start_fake_recording(window, monkeypatch):
    recording = FakeRecording()
    monkeypatch.setattr(window.controller, "start_recording", lambda: recording)
    window._record_voice()
    window._receive()
    pump(window, lambda: window.record_button.cget("text") == "Stop & transcribe")
    return recording


def test_record_stop_transcribes_only_after_device_is_closed(window, monkeypatch):
    from veil.voice import VoiceText

    recording = start_fake_recording(window, monkeypatch)
    assert str(window.record_button.cget("state")) == "normal"
    assert str(window.discard_recording.cget("state")) == "normal"
    assert str(window.source.cget("state")) == "disabled"
    assert "Recording" in window.notice.get()

    def transcribe(value):
        assert value is recording
        value.stop()
        assert value.stopped
        return VoiceText("Jane Example", 2)

    monkeypatch.setattr(window.controller, "transcribe_recording", transcribe)
    window._record_voice()
    pump(window, lambda: window.source.get("1.0", "end-1c") == "Jane Example")
    assert window.recording is None
    assert window.record_timer is None
    assert recording.stopped


def test_discard_stops_microphone_and_preserves_previous_input(window, monkeypatch):
    window.source.insert("1.0", "unfinished text")
    recording = start_fake_recording(window, monkeypatch)
    window._discard_recording()
    pump(window, lambda: recording.cancelled.is_set())
    assert window.recording is None
    assert window.record_timer is None
    assert window.source.get("1.0", "end-1c") == "unfinished text"


def test_capture_limit_automatically_stops_and_transcribes(window, monkeypatch):
    from veil.voice import VoiceText

    recording = start_fake_recording(window, monkeypatch)

    def transcribe(value):
        value.stop()
        return VoiceText("Timed recording", 300)

    monkeypatch.setattr(window.controller, "transcribe_recording", transcribe)
    recording.finished.set()
    window._record_tick()
    pump(window, lambda: window.source.get("1.0", "end-1c") == "Timed recording")
    assert recording.stopped


def test_close_cancels_recording_even_when_microphone_is_still_opening(
    window, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    recording = FakeRecording()

    def start():
        entered.set()
        assert release.wait(3)
        return recording

    monkeypatch.setattr(window.controller, "start_recording", start)
    window._record_voice()
    assert entered.wait(1)
    window.close()
    release.set()
    assert recording.cancelled.wait(2)


def test_close_stops_active_microphone(window, monkeypatch):
    recording = start_fake_recording(window, monkeypatch)
    window.close()
    assert recording.cancelled.is_set()


def ready_snapshot():
    return {
        "gateway": {
            "state": "running",
            "api": "openai",
            "auth": "chatgpt",
            "port": 8485,
        },
        "startup": {"state": "disabled"},
        "names": False,
        "registrations": 0,
    }


def test_quiet_poll_never_locks_controls_or_drops_user_action(window):
    poll_started, release_poll = threading.Event(), threading.Event()
    action_started, release_action = threading.Event(), threading.Event()
    window._update_snapshot(ready_snapshot())
    window.notice.set("Previous action completed")

    def read():
        poll_started.set()
        assert release_poll.wait(3)
        return ready_snapshot()

    def action():
        action_started.set()
        assert release_action.wait(3)
        return "User action completed"

    window.submit(read, "Quiet read", window._update_snapshot, foreground=False)
    try:
        assert poll_started.wait(1)
        assert window.refreshing
        assert not window.pending
        assert str(window.stop_button["state"]) == "normal"
        assert str(window.provider_choice["state"]) == "readonly"
        assert window.notice.get() == "Previous action completed"
        assert window.animation is None
        window.submit(action, "User action")
        assert window.pending
        window._receive()
        release_poll.set()
        pump(window, lambda: not window.refreshing and action_started.is_set())
        assert window.pending
        assert window.animation is not None
        assert str(window.stop_button["state"]) == "disabled"
        assert window.notice.get() == "User action"
        release_action.set()
        pump(window, lambda: window.notice.get() == "User action completed")
    finally:
        release_poll.set()
        release_action.set()


def test_unchanged_snapshot_does_not_redraw_or_reset_pending_selection(
    window, monkeypatch
):
    snapshot = ready_snapshot()
    window._update_snapshot(snapshot)
    window.provider_mode.set("Anthropic · API key / Claude Code")
    window.provider_dirty = True

    def unexpected_redraw(**kwargs):
        pytest.fail("unchanged status repainted widgets")

    monkeypatch.setattr(window.badge, "configure", unexpected_redraw)
    monkeypatch.setattr(window.stop_button, "configure", unexpected_redraw)
    window._update_snapshot(dict(snapshot))
    assert window.provider_mode.get().startswith("Anthropic")


def test_late_poll_failure_preserves_action_message(window):
    window._update_snapshot(ready_snapshot())
    window.notice.set("Preview complete")
    window._complete(window._update_snapshot, None, "Status read failed", False)
    assert window.notice.get() == "Preview complete"
    assert window.gateway_state == "unknown"


def test_page_scroll_position_survives_navigation(window):
    window.root.geometry("840x640")
    window.root.deiconify()
    mapped_at = time.monotonic()
    window.reduce_motion.set(True)
    window.show_page("Settings")
    pump(
        window,
        lambda: (
            window.content.winfo_reqheight() > window.viewport.winfo_height()
            and window.layout_timer is None
            and time.monotonic() - mapped_at > 0.25
        ),
    )
    window.viewport.yview_moveto(0.5)
    position = window.viewport.yview()[0]
    assert position > 0.1, (
        window.root.geometry(),
        window.content.winfo_reqheight(),
        window.viewport.winfo_height(),
        window.viewport.cget("scrollregion"),
    )
    window.show_page("Overview")
    pump(window, lambda: window.layout_timer is None)
    window.show_page("Settings")
    pump(window, lambda: window.layout_timer is None)
    assert abs(window.viewport.yview()[0] - position) < 0.01


def test_rapid_navigation_settles_and_close_cancels_motion(window):
    errors = []
    window.root.report_callback_exception = lambda *args: errors.append(args)
    window.root.deiconify()
    window.source.insert("1.0", "Unfinished private draft")
    for _ in range(10):
        window.show_page("Settings")
        window.show_page("Masking preview")
        window.show_page("Overview")
    pump(window, lambda: window.transition is None and window.layout_timer is None)
    assert window.viewport.coords(window.content_window) == [0.0, 0.0]
    assert window.source.get("1.0", "end-1c") == "Unfinished private draft"
    assert errors == []
    window.show_page("Settings")
    assert window.transition is not None
    window.close()
    window.close()


def test_reduce_motion_keeps_loader_static_and_navigation_instant(window):
    release = threading.Event()
    window.reduce_motion.set(True)
    window._motion_changed()
    window.show_page("Settings")
    assert window.transition is None
    window.submit(lambda: release.wait(3), "Working")
    try:
        assert window.animation is None
        assert float(window.loader.itemcget(window.arc, "extent")) > 0
    finally:
        release.set()


def test_action_and_notice_do_not_move_sidebar_or_viewport(window):
    release = threading.Event()
    window.root.deiconify()
    mapped_at = time.monotonic()
    pump(
        window,
        lambda: time.monotonic() - mapped_at > 0.25 and window.layout_timer is None,
    )
    before = (window.viewport.winfo_height(), window.loader.winfo_y())
    window.submit(lambda: release.wait(3), "A long action label " * 12)
    try:
        window.root.update()
        after = (window.viewport.winfo_height(), window.loader.winfo_y())
        assert after == before
    finally:
        release.set()


def test_editing_source_invalidates_previous_masked_result(window):
    window.source.insert("1.0", "jane@example.com")
    window.root.update()
    window._set_output("[EMAIL_1]")
    window.source.insert("end", " and another detail")
    window.root.update()
    assert window.output.get("1.0", "end-1c") == ""


def test_every_page_fits_minimum_width_without_clipped_controls(window):
    window.root.deiconify()
    errors = []
    window.root.report_callback_exception = lambda *args: errors.append(args)
    for width, height in ((1160, 820), (840, 640), (980, 720)):
        window.root.geometry(f"{width}x{height}")
        for name in window.pages:
            window.show_page(name)
            pump(
                window,
                lambda: window.transition is None and window.layout_timer is None,
            )
            window.root.update()
            left = window.viewport.winfo_rootx()
            right = left + window.viewport.winfo_width()
            for button in [*window.buttons, window.provider_choice]:
                if button.winfo_ismapped():
                    assert button.winfo_rootx() >= left
                    assert button.winfo_rootx() + button.winfo_width() <= right
    assert errors == []


def test_example_masks_in_real_controller_and_stays_until_source_changes(window):
    window.root.deiconify()
    window.show_page("Masking preview")
    window._example()
    window.root.update()
    window._mask_preview()
    window._receive()
    pump(window, lambda: window.notice.get().startswith("Preview complete"))
    masked = window.output.get("1.0", "end-1c")
    assert "jane@example.com" not in masked
    assert "EMAIL" in masked
    window.root.update()
    assert window.output.get("1.0", "end-1c") == masked
    window.source.insert("end", " A new detail.")
    window.root.update()
    assert window.output.get("1.0", "end-1c") == ""
    assert not window.controller.directory.exists()


def test_native_view_menu_switches_pages_without_running_gateway(window):
    menu = window.root.nametowidget(window.root.cget("menu"))
    view = window.root.nametowidget(menu.entrycget("View", "menu"))
    view.invoke("Settings")
    assert window.current_page == "Settings"
    assert not window.pending
    assert not window.controller.directory.exists()
