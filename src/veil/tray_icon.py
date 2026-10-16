"""Render the shared Veil artwork for native menu bars and system trays."""

from __future__ import annotations

import importlib
import io
import sys
from importlib.resources import files
from typing import Any


def create_tray_icon(pystray: Any, title: str, menu: Any) -> Any:
    """Use a template ribbon on macOS and the teal application icon elsewhere."""
    pillow = importlib.import_module("PIL.Image")
    mac = sys.platform == "darwin"
    asset = "veil-mark.png" if mac else "veil-app-icon.png"
    with pillow.open(
        io.BytesIO(files("veil").joinpath("assets", asset).read_bytes())
    ) as source:
        image = source.convert("RGBA").resize((128, 128), pillow.Resampling.LANCZOS)
    if not mac:
        return pystray.Icon("veil", image, title, menu)

    appkit = importlib.import_module("AppKit")
    foundation = importlib.import_module("Foundation")

    def assert_image(icon: Any) -> None:
        thickness = icon._status_bar.thickness()
        size = (thickness, thickness)
        if icon._icon_image is not None and icon._icon_image.size() == size:
            return
        data = io.BytesIO()
        icon.icon.save(data, format="PNG")
        native = appkit.NSImage.alloc().initWithData_(
            foundation.NSData(data.getvalue())
        )
        # Retain the 128px backing image while sizing in logical menu-bar points.
        # A template lets macOS choose the foreground for appearance/selection.
        native.setSize_(size)
        native.setTemplate_(True)
        icon._icon_image = native
        icon._status_item.button().setImage_(native)

    # pystray 0.19's Darwin renderer has no template option and resamples to
    # logical pixels. Override only that rendering hook; keep its native loop,
    # menu handling, and status-item cleanup intact.
    template_icon = type(
        "VeilTemplateIcon", (pystray.Icon,), {"_assert_image": assert_image}
    )
    return template_icon("veil", image, title, menu)
