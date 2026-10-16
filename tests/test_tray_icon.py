"""Shared tray artwork and opt-in native macOS rendering checks."""

import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from veil.tray_icon import create_tray_icon


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_system_tray_uses_transparent_teal_app_artwork(monkeypatch, platform):
    pytest.importorskip("PIL.Image")
    monkeypatch.setattr("veil.tray_icon.sys.platform", platform)
    backend = SimpleNamespace(
        Icon=lambda name, image, title, menu: SimpleNamespace(
            name=name, image=image, title=title, menu=menu
        )
    )
    menu = object()
    icon = create_tray_icon(backend, "Veil · ready", menu)
    assert icon.name == "veil"
    assert icon.title == "Veil · ready"
    assert icon.menu is menu
    assert icon.image.mode == "RGBA"
    assert icon.image.getpixel((0, 0))[3] == 0
    red, green, blue, alpha = icon.image.getpixel((64, 12))
    assert green > red
    assert green > blue
    assert alpha > 240


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("VEIL_NATIVE_UI") != "1",
    reason="set VEIL_NATIVE_UI=1 on a macOS desktop for native menu-bar checks",
)
def test_mac_template_preserves_retina_backing_and_native_size():
    # Cocoa and Tk each own their NSApplication initialization. As in production,
    # run the tray in its own process so desktop tests can still initialize Tk.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""\
                import pystray
                from veil.tray_icon import create_tray_icon

                icon = create_tray_icon(pystray, "Veil · ready", None)
                try:
                    icon._assert_image()
                    native = icon._icon_image
                    assert native.isTemplate()
                    assert native.size().width == icon._status_bar.thickness()
                    assert native.size().height == icon._status_bar.thickness()
                    assert native.representations()[0].pixelsWide() == 128
                    assert icon._status_item.button().image() is native
                    icon._assert_image()
                    assert icon._icon_image is native
                finally:
                    icon._status_bar.removeStatusItem_(icon._status_item)
                """),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
