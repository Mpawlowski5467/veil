"""Native presentation for the desktop controller; no gateway work on the UI thread."""

from __future__ import annotations

import base64
import contextlib
import importlib
import io
import queue
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .desktop import PROVIDER_MODES
from .gateway.config import SettingsError

if TYPE_CHECKING:
    from .desktop import DesktopController
    from .microphone import MicrophoneRecording
    from .ocr import OCRText
    from .pdf_text import PDFText
    from .voice import VoiceText

_BG = "#f5f7f6"
_WHITE = "#ffffff"
_INK = "#213c33"
_MUTED = "#64776e"
_LINE = "#e3e9e6"
_TEAL = "#18745c"
_TINT = "#e7f2ed"
_ERROR = "#a34435"


class DesktopWindow:
    """Keep native widgets, background actions, and logo animation in one lifetime."""

    def __init__(self, controller: DesktopController) -> None:
        """Build an idle window; only status reads run automatically."""
        try:
            import tkinter as tk
            from tkinter import filedialog, ttk
        except ImportError:
            raise SettingsError(
                "desktop controls need Python with Tk support installed"
            ) from None
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError:
            raise SettingsError(
                "desktop controls need an available graphical desktop and Tk"
            ) from None
        self.tk, self.ttk, self.filedialog = tk, ttk, filedialog
        self.controller = controller
        self.closed = False
        self.recording: MicrophoneRecording | None = None
        self.record_lock = threading.Lock()
        self.record_timer: str | None = None
        self.record_started = 0.0
        self.pending = False
        self.refreshing = False
        self.foreground = False
        self.layout_timer: str | None = None
        self.layout_stable = 0
        self.content_size = (0, 0)
        self.transition: str | None = None
        self.current_page: str | None = None
        self.scroll_restore: float | None = None
        self.scroll_positions: dict[str, float] = {}
        self.reduce_motion = tk.BooleanVar(master=self.root, value=False)
        self.phase = 0.0
        self.animation: str | None = None
        self.poll: str | None = None
        self.receiver: str | None = None
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="veil-controls"
        )
        self.results: queue.Queue[
            tuple[Callable[[Any], None] | None, Any, str | None, bool]
        ] = queue.Queue()
        self.buttons: list[Any] = []
        self.gateway_state = "checking"
        self.startup_state = "checking"
        self.snapshot: dict[str, Any] | None = None
        self.provider_dirty = False
        self.pages: dict[str, Any] = {}
        self.navigation: dict[str, Any] = {}
        self.root.title("Veil — Local privacy controls")
        width = min(1160, self.root.winfo_screenwidth() - 80)
        height = min(820, self.root.winfo_screenheight() - 110)
        left = max(0, (self.root.winfo_screenwidth() - width) // 2)
        top = max(30, (self.root.winfo_screenheight() - height) // 2 - 20)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.minsize(840, 640)
        self.root.configure(background=_BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.font = "SF Pro Text" if sys.platform == "darwin" else "Segoe UI"
        if sys.platform.startswith("linux"):
            self.font = "DejaVu Sans"
        self._styles()
        self._images()
        self._layout()
        self._overview()
        self._preview()
        self._settings()
        self.show_page("Overview")
        self._button_states()
        self._menus()
        self.root.bind("<Command-1>", lambda _: self.show_page("Overview"))
        self.root.bind("<Command-2>", lambda _: self.show_page("Masking preview"))
        self.root.bind("<Command-3>", lambda _: self.show_page("Settings"))
        self.root.bind("<Control-1>", lambda _: self.show_page("Overview"))
        self.root.bind("<Control-2>", lambda _: self.show_page("Masking preview"))
        self.root.bind("<Control-3>", lambda _: self.show_page("Settings"))

    def _menus(self) -> None:
        menu = self.tk.Menu(self.root)
        file_menu = self.tk.Menu(menu, tearoff=False)
        modifier = "Command" if sys.platform == "darwin" else "Control"
        shortcut = "⌘" if sys.platform == "darwin" else "Ctrl+"
        file_menu.add_command(
            label="Close window", command=self.close, accelerator=shortcut + "W"
        )
        menu.add_cascade(label="File", menu=file_menu)
        edit = self.tk.Menu(menu, tearoff=False)

        def edit_event(name: str) -> None:
            focus = self.root.focus_get()
            if focus is not None:
                focus.event_generate(name)

        for label, event, key in (
            ("Cut", "<<Cut>>", "X"),
            ("Copy", "<<Copy>>", "C"),
            ("Paste", "<<Paste>>", "V"),
            ("Select all", "<<SelectAll>>", "A"),
        ):
            edit.add_command(
                label=label,
                command=partial(edit_event, event),
                accelerator=shortcut + key,
            )
        menu.add_cascade(label="Edit", menu=edit)
        view = self.tk.Menu(menu, tearoff=False)
        for index, name in enumerate(self.pages, start=1):
            view.add_command(
                label=name,
                command=self._navigate(name),
                accelerator=shortcut + str(index),
            )
        menu.add_cascade(label="View", menu=view)
        help_menu = self.tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="Local diagnostics", command=self._diagnostics)
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menu)
        self.root.bind(f"<{modifier}-w>", lambda _: self.close())
        self.root.bind(f"<{modifier}-comma>", lambda _: self.show_page("Settings"))
        if sys.platform == "darwin":
            self.root.createcommand("tk::mac::Quit", self.close)

    def _styles(self) -> None:
        style = self.ttk.Style(self.root)
        style.theme_use("clam")
        # Remove clam's dotted focus rectangle; retain an explicit focus border.
        style.layout(
            "TButton",
            [
                (
                    "Button.border",
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Button.padding",
                                {
                                    "sticky": "nswe",
                                    "children": [("Button.label", {"sticky": "nswe"})],
                                },
                            )
                        ],
                    },
                )
            ],
        )
        style.configure("TButton", font=(self.font, 11), padding=(14, 9))
        style.configure(
            "Veil.TButton",
            background=_WHITE,
            foreground=_INK,
            bordercolor=_LINE,
            lightcolor=_WHITE,
            darkcolor=_WHITE,
            focuscolor=_TEAL,
            borderwidth=1,
        )
        style.map(
            "Veil.TButton",
            background=[("pressed", _TINT), ("active", _BG)],
            bordercolor=[("focus", _TEAL)],
            foreground=[("disabled", "#9ba9a2")],
        )
        style.configure(
            "Primary.TButton",
            background=_TEAL,
            foreground=_WHITE,
            bordercolor=_TEAL,
            lightcolor=_TEAL,
            darkcolor=_TEAL,
            focuscolor=_WHITE,
            borderwidth=1,
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", "#9cb8ad"),
                ("pressed", "#0e4b3c"),
                ("active", "#115d49"),
            ],
            bordercolor=[("focus", _INK)],
            foreground=[("disabled", _WHITE)],
        )
        style.configure(
            "Nav.TButton",
            background=_WHITE,
            foreground=_MUTED,
            anchor="w",
            borderwidth=0,
            padding=(16, 12),
            focuscolor=_TEAL,
        )
        style.map(
            "Nav.TButton", background=[("active", _BG)], foreground=[("focus", _TEAL)]
        )
        style.configure(
            "Selected.Nav.TButton",
            background=_TINT,
            foreground=_TEAL,
            font=(self.font, 11, "bold"),
        )
        style.map("Selected.Nav.TButton", background=[("active", _TINT)])
        style.configure(
            "Veil.TMenubutton",
            font=(self.font, 11),
            background=_WHITE,
            foreground=_INK,
            bordercolor=_LINE,
            lightcolor=_WHITE,
            darkcolor=_WHITE,
            borderwidth=1,
            relief="flat",
            padding=(14, 9),
        )
        style.map(
            "Veil.TMenubutton",
            background=[("active", _BG)],
            bordercolor=[("focus", _TEAL)],
        )
        style.configure(
            "TCheckbutton",
            background=_WHITE,
            foreground=_INK,
            font=(self.font, 11),
            padding=4,
        )
        style.map("TCheckbutton", background=[("active", _WHITE)])
        style.configure(
            "TEntry",
            fieldbackground=_BG,
            foreground=_INK,
            bordercolor=_LINE,
            lightcolor=_LINE,
            darkcolor=_LINE,
            padding=10,
            insertcolor=_INK,
        )
        style.map("TEntry", bordercolor=[("focus", _TEAL)])
        style.configure(
            "TCombobox",
            fieldbackground=_BG,
            foreground=_INK,
            background=_WHITE,
            bordercolor=_LINE,
            padding=9,
            arrowcolor=_MUTED,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", _BG)],
            foreground=[("readonly", _INK)],
        )
        style.configure(
            "Vertical.TScrollbar",
            background=_LINE,
            troughcolor=_WHITE,
            borderwidth=0,
            arrowsize=10,
            width=10,
        )

    def _images(self) -> None:
        # Keep the high-resolution Dock icon separate from the small UI mark.
        # Passing a 48px UI image to iconphoto makes magnified Dock icons blurry.
        try:
            pillow = importlib.import_module("PIL.Image")
        except ImportError:
            pillow = None

        def render(name: str, size: int) -> Any:
            resource = files("veil").joinpath("assets", name)
            if pillow is not None:
                with pillow.open(io.BytesIO(resource.read_bytes())) as original:
                    resized = original.resize((size, size), pillow.Resampling.LANCZOS)
                    output = io.BytesIO()
                    resized.save(output, format="PNG")
                    return self.tk.PhotoImage(
                        master=self.root,
                        data=base64.b64encode(output.getvalue()),
                        format="png",
                    )
            # Keep basic controls usable without optional desktop dependencies.
            with as_file(resource) as path:
                source = self.tk.PhotoImage(
                    master=self.root, file=str(path), format="png"
                )
            factor = max(1, (source.width() + size - 1) // size)
            return source.subsample(factor, factor)

        self.mark = render("veil-mark.png", 44)
        self.app_icons = [
            render("veil-app-icon.png", size) for size in (512, 256, 128, 64, 32)
        ]
        self.app_icon = self.app_icons[0]
        self.root.iconphoto(True, *self.app_icons)

    def _frame(self, parent: Any, *, color: str = _WHITE, **kwargs: Any) -> Any:
        return self.tk.Frame(parent, background=color, **kwargs)

    def _label(
        self,
        parent: Any,
        text: str = "",
        *,
        size: int = 11,
        color: str = _INK,
        bold: bool = False,
        bg: str = _WHITE,
        **kwargs: Any,
    ) -> Any:
        label = self.tk.Label(
            parent,
            text=text,
            background=bg,
            foreground=color,
            font=(self.font, size, "bold" if bold else "normal"),
            anchor="w",
            justify="left",
            borderwidth=0,
            **kwargs,
        )

        if "wraplength" in kwargs:

            def wrap(event: Any) -> None:
                padding = parent.winfo_pixels(parent.cget("padx")) * 2
                width = max(100, event.width - padding - 4)
                if int(label.cget("wraplength")) != width:
                    label.configure(wraplength=width)

            parent.bind("<Configure>", wrap, add="+")
        return label

    def _card(self, parent: Any, *, pady: int = 14) -> Any:
        surface = self.tk.Canvas(
            parent, background=_BG, highlightthickness=0, borderwidth=0, height=1
        )
        surface.pack(fill="x", pady=(0, pady))
        body = self._frame(surface)
        item = surface.create_window(24, 22, window=body, anchor="nw")
        shape = surface.create_polygon(
            0, 0, 0, 0, smooth=True, splinesteps=24, fill=_WHITE, outline=_LINE
        )
        surface.tag_lower(shape)
        last_width = 0

        def resize(_: Any = None) -> None:
            nonlocal last_width
            width = max(1, surface.winfo_width())
            height = body.winfo_reqheight() + 44
            if last_width != max(1, width - 48):
                last_width = max(1, width - 48)
                surface.itemconfigure(item, width=last_width)
            if int(surface.cget("height")) != height:
                surface.configure(height=height)
                self._resize_content()
            radius = 14
            surface.coords(
                shape,
                radius,
                1,
                width - radius,
                1,
                width - 1,
                1,
                width - 1,
                radius,
                width - 1,
                height - radius,
                width - 1,
                height - 1,
                width - radius,
                height - 1,
                radius,
                height - 1,
                1,
                height - 1,
                1,
                height - radius,
                1,
                radius,
                1,
                1,
            )

        surface.bind("<Configure>", resize)
        body.bind("<Configure>", resize)
        return body

    def _button(
        self,
        parent: Any,
        text: str,
        command: Callable[[], Any],
        *,
        primary: bool = False,
        tracked: bool = True,
    ) -> Any:
        button = self.ttk.Button(
            parent,
            text=text,
            command=command,
            width=0,
            style="Primary.TButton" if primary else "Veil.TButton",
        )
        if tracked:
            self.buttons.append(button)
        return button

    def _layout(self) -> None:
        sidebar = self._frame(self.root, width=208)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._frame(self.root, color=_LINE, width=1).pack(side="left", fill="y")
        brand = self._frame(sidebar)
        brand.pack(anchor="w", padx=24, pady=(28, 4))
        self._label(brand, image=self.mark).pack(side="left")
        self.brand_name = self._label(brand, "veil", size=28, bold=True)
        self.brand_name.pack(side="left", padx=(3, 0))
        self._label(sidebar, "Your privacy workspace", size=9, color=_MUTED).pack(
            anchor="w", padx=27, pady=(0, 32)
        )
        for page in ("Overview", "Masking preview", "Settings"):
            button = self.ttk.Button(
                sidebar,
                text=page,
                style="Nav.TButton",
                command=self._navigate(page),
            )
            button.pack(fill="x", padx=14, pady=3)
            self.navigation[page] = button
        bottom = self._frame(sidebar, padx=24, pady=22)
        bottom.pack(side="bottom", fill="x")
        self.loader = self.tk.Canvas(
            bottom,
            width=48,
            height=48,
            background=_WHITE,
            highlightthickness=0,
            borderwidth=0,
        )
        self.loader.pack(anchor="w", pady=(0, 10))
        self.arc = self.loader.create_arc(
            2,
            2,
            46,
            46,
            start=90,
            extent=0,
            outline=_TEAL,
            width=2,
            style="arc",
        )
        self.loader.create_image(24, 24, image=self.mark)
        # A fixed slot prevents long action labels from moving the sidebar.
        self.busy_text = self._label(
            bottom, "", size=9, color=_TEAL, width=21, height=2, wraplength=156
        )
        self.busy_text.pack(anchor="w", pady=(0, 10))
        self._frame(bottom, color=_LINE, height=1).pack(fill="x", pady=(0, 16))
        self._label(bottom, "LOCAL CONNECTION", size=8, color=_MUTED).pack(anchor="w")
        self.side_status = self._label(bottom, "●  Checking gateway", size=10)
        self.side_status.pack(anchor="w", pady=(9, 7))
        self._label(
            bottom,
            "Your text stays local\nuntil you choose to share.",
            size=10,
            color=_MUTED,
        ).pack(anchor="w")

        workspace = self._frame(self.root, color=_BG)
        workspace.pack(fill="both", expand=True)
        footer = self._frame(workspace, color=_BG, padx=30, pady=12, height=76)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        self.notice = self.tk.StringVar(
            value="Ready. Your client settings are unchanged."
        )
        self.notice_label = self._label(
            footer,
            textvariable=self.notice,
            size=10,
            color=_MUTED,
            bg=_BG,
            wraplength=700,
            height=2,
        )
        self.notice_label.pack(anchor="w")
        self._label(
            footer,
            "On this device · Closing the window keeps the gateway running",
            size=9,
            color=_MUTED,
            bg=_BG,
        ).pack(anchor="w", pady=(6, 0))
        self.viewport = self.tk.Canvas(
            workspace,
            background=_BG,
            borderwidth=0,
            highlightthickness=0,
            yscrollincrement=24,
        )
        scrollbar = self.ttk.Scrollbar(
            workspace, command=self.viewport.yview, takefocus=False
        )
        scrollbar.pack(side="right", fill="y")
        self.viewport.configure(yscrollcommand=scrollbar.set)
        self.viewport.pack(fill="both", expand=True)
        self.content = self._frame(self.viewport, color=_BG, padx=30, pady=28)
        self.content_window = self.viewport.create_window(
            0,
            0,
            window=self.content,
            anchor="nw",
        )
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self.viewport.bind("<Configure>", self._resize_content)
        self.content.bind("<Configure>", self._resize_content)
        self.root.bind("<MouseWheel>", self._scroll)
        self.root.bind("<Button-4>", self._scroll)
        self.root.bind("<Button-5>", self._scroll)

    def _resize_content(self, event: Any = None) -> None:
        if self.closed:
            return
        self.layout_stable = 0
        if self.layout_timer is None:
            # Geometry requests propagate through several nested frames. Wait
            # for the next frame, rather than reading a half-settled idle pass.
            self.layout_timer = self.root.after(16, self._fit_content)

    def _fit_content(self) -> None:
        self.layout_timer = None
        if self.closed:
            return
        width = self.viewport.winfo_width()
        height = max(self.content.winfo_reqheight(), self.viewport.winfo_height())
        changed = False
        if self.content_size != (width, height):
            self.viewport.itemconfigure(self.content_window, width=width, height=height)
            self.content_size = (width, height)
            changed = True
        self.viewport.configure(scrollregion=(0, 0, width, height))
        self.layout_stable = 0 if changed else self.layout_stable + 1
        if self.scroll_restore is not None:
            self.viewport.yview_moveto(self.scroll_restore)
            if self.layout_stable >= 2:
                self.scroll_restore = None
        if self.layout_stable < 2 and self.layout_timer is None:
            self.layout_timer = self.root.after(16, self._fit_content)

    def _scroll(self, event: Any) -> None:
        if event.widget.winfo_class() in {"Text", "TCombobox", "Listbox"}:
            return
        self.scroll_restore = None
        if event.num in {4, 5}:
            amount = -1 if event.num == 4 else 1
        else:
            amount = (
                -int(event.delta / 120)
                if abs(event.delta) >= 120
                else -int(event.delta)
            )
        self.viewport.yview_scroll(amount, "units")

    def _page(self, name: str, eyebrow: str, title: str, description: str) -> Any:
        page = self._frame(self.content, color=_BG)
        page.grid(row=0, column=0, sticky="nsew")
        # The canvas pins the content frame's actual height. A page can request
        # more space without changing that frame, so observe the page as well.
        page.bind("<Configure>", self._resize_content)
        self.pages[name] = page
        self._label(page, eyebrow, size=9, color=_TEAL, bold=True, bg=_BG).pack(
            anchor="w"
        )
        self._label(page, title, size=25, bold=True, bg=_BG, wraplength=690).pack(
            anchor="w", pady=(8, 6)
        )
        self._label(
            page,
            description,
            color=_MUTED,
            bg=_BG,
            wraplength=690,
        ).pack(anchor="w", pady=(0, 22))
        return page

    def _overview(self) -> None:
        page = self._page(
            "Overview",
            "ON THIS DEVICE",
            "Your privacy workspace.",
            "Manage your gateway and check text before you share.",
        )
        hero = self._card(page, pady=16)
        top = self._frame(hero)
        top.pack(fill="x")
        self._label(top, "LOCAL GATEWAY", size=9, color=_MUTED, bold=True).pack(
            side="left"
        )
        self.badge = self._label(
            top, "Checking", size=10, bg=_TINT, color=_TEAL, padx=10, pady=5
        )
        self.badge.pack(side="right")
        middle = self._frame(hero)
        middle.pack(fill="x", pady=(14, 16))
        copy = self._frame(middle)
        copy.pack(fill="x", expand=True)
        self.gateway_title = self._label(
            copy, "Finding your gateway…", size=21, bold=True, wraplength=600
        )
        self.gateway_title.pack(anchor="w", pady=(3, 7))
        self.gateway_detail = self._label(
            copy,
            "Reading local status. This only takes a moment.",
            color=_MUTED,
            wraplength=480,
        )
        self.gateway_detail.pack(anchor="w")
        actions = self._frame(hero)
        actions.pack(fill="x")
        self.start_button = self._button(
            actions,
            "Start gateway",
            lambda: self._gateway("start"),
            primary=True,
        )
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = self._button(actions, "Stop", lambda: self._gateway("stop"))
        self.stop_button.pack(side="left", padx=(0, 8))
        self.restart_button = self._button(
            actions,
            "Restart",
            lambda: self._gateway("restart"),
        )
        self.restart_button.pack(side="left")
        self._button(actions, "Diagnostics", self._diagnostics).pack(side="right")

        settings = self._card(page, pady=16)
        self._label(settings, "Your privacy setup", size=14, bold=True).pack(anchor="w")
        self.startup_summary = self._summary_row(settings, "Login startup", "Checking…")
        self.names_summary = self._summary_row(
            settings, "Local name recognition", "Checking…"
        )
        self.registrations_summary = self._summary_row(
            settings, "Private registrations", "Checking…"
        )
        self._button(
            settings,
            "Manage settings  →",
            lambda: self.show_page("Settings"),
            tracked=False,
        ).pack(anchor="w", pady=(15, 0))

        preview = self._card(page, pady=16)
        self._label(preview, "Check before you share", size=14, bold=True).pack(
            anchor="w"
        )
        self._label(
            preview,
            "Paste text or import a document, image, or recording. "
            "Review the masked result locally.",
            color=_MUTED,
            size=10,
            wraplength=650,
        ).pack(anchor="w", pady=(8, 14))
        self._button(
            preview,
            "Open masking preview  →",
            lambda: self.show_page("Masking preview"),
            tracked=False,
        ).pack(anchor="w")

        self._label(
            page,
            "Gateway readiness does not confirm that a chat used Veil.\n"
            "Use veil verify for evidence about a specific request.",
            color=_MUTED,
            bg=_BG,
            size=10,
            wraplength=670,
        ).pack(anchor="w", pady=(2, 0))

    def _summary_row(self, parent: Any, title: str, value: str) -> Any:
        row = self._frame(parent)
        row.pack(fill="x", pady=(14, 0))
        self._label(row, title, color=_MUTED).pack(side="left")
        label = self._label(row, value, size=10, bold=True)
        label.pack(side="right")
        return label

    def _text_area(self, parent: Any, *, readonly: bool = False) -> Any:
        frame = self._frame(
            parent, color=_BG, highlightbackground=_LINE, highlightthickness=1
        )
        frame.pack(fill="both", expand=True, pady=(8, 12))
        text = self.tk.Text(
            frame,
            height=5,
            width=20,
            wrap="word",
            background=_BG,
            foreground=_TEAL if readonly else _INK,
            insertbackground=_TEAL,
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=14,
            font=(self.font, 12),
            spacing1=3,
            spacing3=3,
            highlightthickness=0,
            undo=not readonly,
            state="disabled" if readonly else "normal",
        )
        scrollbar = self.ttk.Scrollbar(frame, command=text.yview)
        scrollbar.pack(side="right", fill="y")
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(fill="both", expand=True)
        return text

    def _preview(self) -> None:
        page = self._page(
            "Masking preview",
            "LOCAL TOOLS",
            "Masking preview",
            "Try your configured rules before sharing. Nothing sent to a provider.",
        )
        card = self._card(page)
        heading = self._frame(card)
        heading.pack(fill="x")
        self._label(heading, "01  Original text", size=13, bold=True).pack(side="left")
        self._button(heading, "Use an example", self._example).pack(side="right")
        tools = self._frame(card)
        tools.pack(fill="x", pady=(10, 0))
        imports = self.ttk.Menubutton(
            tools, text="Import file…", style="Veil.TMenubutton"
        )
        menu = self.tk.Menu(imports, tearoff=False)
        menu.add_command(label="PDF text…", command=self._import_pdf)
        menu.add_command(label="Scan PDF or image…", command=self._import_scan)
        menu.add_separator()
        menu.add_command(label="Audio recording…", command=self._import_audio)
        imports.configure(menu=menu)
        imports.pack(side="left", padx=(0, 8))
        self.buttons.append(imports)
        self.record_button = self._button(tools, "Record voice…", self._record_voice)
        self.record_button.pack(side="left", padx=(0, 8))
        self.discard_recording = self._button(tools, "Discard", self._discard_recording)
        self.discard_recording.pack(side="left")
        self.pdf_detail = self._label(
            card,
            "Import documents or English audio. Set up voice in Settings.",
            color=_MUTED,
            size=9,
            wraplength=650,
        )
        self.pdf_detail.pack(anchor="w", pady=(8, 0))
        self.source = self._text_area(card)
        self.source.bind("<<Modified>>", self._source_changed)
        actions = self._frame(card)
        actions.pack(fill="x", pady=(0, 16))
        self._button(
            actions,
            "Preview masking  →",
            self._mask_preview,
            primary=True,
        ).pack(side="left")
        self._button(actions, "Clear", self._clear_preview).pack(side="right")
        result = self._card(page)
        self._label(result, "02  Masked preview", size=13, bold=True).pack(anchor="w")
        self.output = self._text_area(result, readonly=True)
        self._label(
            result,
            "Temporary placeholders · Text stays in memory · Exact restoration checked",
            color=_MUTED,
            size=9,
            wraplength=650,
        ).pack(anchor="w")

    def _settings(self) -> None:
        page = self._page(
            "Settings",
            "PREFERENCES",
            "Settings",
            "Choose how Veil starts and which private details it recognizes.",
        )
        providers = self._card(page, pady=12)
        self._label(providers, "AI provider", size=14, bold=True).pack(anchor="w")
        self.provider_label = self._label(
            providers, "Checking provider…", color=_TEAL, size=10
        )
        self.provider_label.pack(anchor="w", pady=(6, 5))
        self._label(
            providers,
            "Use your client's existing key or sign-in. Applying restarts the gateway "
            "and may interrupt requests. Connect your client separately. "
            "Disable login startup before changing modes, then re-enable it.",
            color=_MUTED,
            size=10,
            wraplength=660,
        ).pack(anchor="w")
        self.provider_mode = self.tk.StringVar()
        self.provider_choice = self.ttk.Combobox(
            providers,
            textvariable=self.provider_mode,
            values=tuple(PROVIDER_MODES),
            state="readonly",
            width=36,
            font=(self.font, 11),
        )
        self.provider_choice.pack(anchor="w", pady=(12, 10))
        self.provider_choice.bind(
            "<<ComboboxSelected>>", lambda _: setattr(self, "provider_dirty", True)
        )
        self.apply_provider = self._button(
            providers, "Apply & restart gateway", self._apply_provider
        )
        self.apply_provider.pack(anchor="w")

        startup = self._card(page, pady=12)
        self._label(startup, "Start when you sign in", size=14, bold=True).pack(
            anchor="w"
        )
        self.startup_label = self._label(
            startup, "Checking startup…", color=_TEAL, size=10
        )
        self.startup_label.pack(anchor="w", pady=(6, 5))
        self._label(
            startup,
            "Recover crashed gateways. An explicit Stop keeps the gateway stopped.",
            color=_MUTED,
            size=10,
            wraplength=660,
        ).pack(anchor="w")
        actions = self._frame(startup)
        actions.pack(fill="x", pady=(12, 0))
        self.enable_startup = self._button(
            actions,
            "Enable login startup",
            lambda: self.submit(
                lambda: self.controller.startup(True), "Enabling login startup…"
            ),
        )
        self.enable_startup.pack(side="left", padx=(0, 8))
        self.disable_startup = self._button(
            actions,
            "Disable and stop",
            lambda: self.submit(
                lambda: self.controller.startup(False), "Disabling startup…"
            ),
        )
        self.disable_startup.pack(side="left")

        names = self._card(page, pady=12)
        self._label(names, "Recognize names on this device", size=14, bold=True).pack(
            anchor="w"
        )
        self.names_label = self._label(
            names, "Checking recognition…", color=_TEAL, size=10
        )
        self.names_label.pack(anchor="w", pady=(6, 5))
        self._label(
            names,
            "Choose a trusted spaCy model folder. "
            "Runs locally; no automatic downloads.",
            size=10,
            color=_MUTED,
            wraplength=660,
        ).pack(anchor="w")
        actions = self._frame(names)
        actions.pack(fill="x", pady=(12, 0))
        self._button(actions, "Choose local model…", self._choose_model).pack(
            side="left", padx=(0, 8)
        )
        self.disable_names = self._button(
            actions,
            "Disable recognition",
            lambda: self.submit(
                lambda: self.controller.names(None), "Saving recognition settings…"
            ),
        )
        self.disable_names.pack(side="left")

        voice = self._card(page, pady=12)
        self._label(
            voice, "Voice transcription on this device", size=14, bold=True
        ).pack(anchor="w")
        self.voice_label = self._label(
            voice, "Checking voice model…", color=_TEAL, size=10
        )
        self.voice_label.pack(anchor="w", pady=(6, 5))
        self._label(
            voice,
            "Download the English model once (about 75 MB from Hugging Face). "
            "Audio stays local; transcription then works offline. "
            "Recordings start only when you press Record and stop after 5 minutes.",
            color=_MUTED,
            size=10,
            wraplength=660,
        ).pack(anchor="w")
        self._button(
            voice,
            "Download voice model",
            lambda: self.submit(
                self.controller.setup_voice,
                "Downloading the public voice model. No audio is sent…",
            ),
        ).pack(anchor="w", pady=(12, 0))

        appearance = self._card(page)
        self._label(appearance, "Appearance", size=14, bold=True).pack(anchor="w")
        self.ttk.Checkbutton(
            appearance,
            text="Reduce motion",
            variable=self.reduce_motion,
            command=self._motion_changed,
        ).pack(anchor="w", pady=(10, 4))
        self._label(
            appearance,
            "Use a still logo and instant navigation for this window.",
            size=10,
            color=_MUTED,
            wraplength=650,
        ).pack(anchor="w")

        registration = self._card(page)
        self._label(registration, "Add a private detail", size=14, bold=True).pack(
            anchor="w"
        )
        self._label(
            registration,
            "Exact matches help with names the model may miss. Restart after changes.",
            color=_MUTED,
            size=10,
            wraplength=660,
        ).pack(anchor="w", pady=(7, 14))
        labels = self._frame(registration)
        labels.pack(fill="x")
        labels.grid_columnconfigure(1, weight=1)
        self._label(labels, "TYPE", size=9, color=_MUTED).grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        self._label(labels, "PRIVATE VALUE", size=9, color=_MUTED).grid(
            row=0, column=1, sticky="w", pady=(0, 6)
        )
        self.kind = self.tk.StringVar(value="PERSON")
        self.ttk.Combobox(
            labels,
            textvariable=self.kind,
            values=("PERSON", "ORGANIZATION", "LOCATION"),
            state="readonly",
            width=16,
            font=(self.font, 10),
        ).grid(row=1, column=0, sticky="ew", padx=(0, 10))
        self.private = self.tk.StringVar()
        self.ttk.Entry(
            labels,
            textvariable=self.private,
            show="•",
            width=20,
            font=(self.font, 11),
        ).grid(row=1, column=1, sticky="ew", padx=(0, 10))
        self._button(
            labels,
            "Register",
            self._register,
            primary=True,
        ).grid(row=1, column=2)
        self._label(
            registration,
            "Hidden as you type. Saved only in your local Veil configuration.",
            size=9,
            color=_MUTED,
            wraplength=650,
        ).pack(anchor="w", pady=(12, 0))

    def _navigate(self, name: str) -> Callable[[], None]:
        return lambda: self.show_page(name)

    def show_page(self, name: str) -> None:
        """Switch views without rebuilding widgets or losing preview input."""
        if self.closed or name == self.current_page:
            return
        previous = self.current_page
        if previous is not None:
            self.scroll_positions[previous] = self.viewport.yview()[0]
            focus = self.root.focus_get()
            if focus is not None and str(focus).startswith(
                str(self.pages[previous]) + "."
            ):
                self.navigation[name].focus_set()
        self.current_page = name
        for title, page in self.pages.items():
            if title == name:
                page.grid()
            else:
                page.grid_remove()
        # Restore again after layout settles: the previous scroll region may
        # initially clamp the saved position to zero.
        self.scroll_restore = self.scroll_positions.get(name, 0)
        self._resize_content()
        self.viewport.yview_moveto(self.scroll_restore)
        for title, button in self.navigation.items():
            button.configure(
                style="Selected.Nav.TButton" if title == name else "Nav.TButton"
            )
        self._stop_transition()
        if previous is not None and not self.reduce_motion.get():
            started = time.monotonic()

            def step() -> None:
                self.transition = None
                if self.closed:
                    return
                progress = min(1.0, (time.monotonic() - started) / 0.14)
                self.viewport.coords(
                    self.content_window, round(10 * (1 - progress) ** 3), 0
                )
                if progress < 1:
                    self.transition = self.root.after(16, step)

            step()

    def _stop_transition(self) -> None:
        if self.transition is not None:
            self.root.after_cancel(self.transition)
            self.transition = None
        self.viewport.coords(self.content_window, 0, 0)

    def _motion_changed(self) -> None:
        self._stop_transition()
        if self.animation is not None:
            self.root.after_cancel(self.animation)
            self.animation = None
        self.loader.itemconfigure(self.arc, extent=0)
        if self.pending and self.foreground:
            self._animate()

    def _animate(self) -> None:
        if self.closed or not self.pending or not self.foreground:
            self.animation = None
            self.loader.itemconfigure(self.arc, extent=0)
            return
        if self.reduce_motion.get():
            self.loader.itemconfigure(self.arc, start=90, extent=85)
            self.animation = None
            return
        self.phase = (time.monotonic() * 240) % 360
        self.loader.itemconfigure(self.arc, start=self.phase, extent=85)
        self.animation = self.root.after(16, self._animate)

    def submit(
        self,
        work: Callable[[], Any],
        label: str,
        done: Callable[[Any], None] | None = None,
        *,
        foreground: bool = True,
    ) -> None:
        """Serialize work and keep status polling visually quiet."""
        if (
            self.closed
            or self.pending
            or self.recording is not None
            or (not foreground and self.refreshing)
        ):
            return
        if foreground:
            self.pending = True
            self.foreground = True
            self._button_states()
        else:
            self.refreshing = True
        if foreground:
            self.notice_label.configure(foreground=_TEAL)
            self.notice.set(label)
            self.busy_text.configure(text="Working on this device…")
            if self.animation is None:
                self._animate()

        def execute() -> None:
            try:
                self.results.put((done, work(), None, foreground))
            except SettingsError as error:
                self.results.put((done, None, str(error), foreground))
            except Exception:
                self.results.put(
                    (
                        done,
                        None,
                        "The action failed. Check diagnostics and local configuration.",
                        foreground,
                    )
                )

        self.executor.submit(execute)

    def _button_states(self) -> None:
        self.provider_choice.configure(
            state="disabled"
            if self.pending or self.recording is not None
            else "readonly"
        )
        for button in self.buttons:
            button.configure(
                state="disabled"
                if self.pending or self.recording is not None
                else "normal"
            )
        self.discard_recording.configure(
            state="normal"
            if self.recording is not None and not self.pending
            else "disabled"
        )
        if self.recording is not None and not self.pending:
            self.record_button.configure(state="normal")
        self.source.configure(
            state="disabled"
            if self.recording is not None or (self.pending and self.foreground)
            else "normal"
        )
        if self.pending:
            return
        running = self.gateway_state in {
            "running",
            "starting",
            "stopping",
            "unresponsive",
        }
        self.start_button.configure(
            style="Veil.TButton"
            if running or self.gateway_state in {"unsupported", "checking", "unknown"}
            else "Primary.TButton"
        )
        if running or self.gateway_state in {"unsupported", "checking", "unknown"}:
            self.start_button.configure(state="disabled")
        if not running:
            self.stop_button.configure(state="disabled")
            self.restart_button.configure(state="disabled")
        if self.startup_state in {"enabled", "checking", "unknown", "unsupported"}:
            self.enable_startup.configure(state="disabled")
        if self.startup_state in {"disabled", "checking", "unknown", "unsupported"}:
            self.disable_startup.configure(state="disabled")
        if (
            self.gateway_state in {"unsupported", "unknown", "checking"}
            or self.startup_state != "disabled"
        ):
            self.apply_provider.configure(state="disabled")
        if self.snapshot is None or not self.snapshot["names"]:
            self.disable_names.configure(state="disabled")

    def _update_snapshot(self, value: dict[str, Any]) -> None:
        if value == self.snapshot:
            return
        self.snapshot = value
        gateway = value["gateway"]
        current = next(
            (
                label
                for label, (api, auth) in PROVIDER_MODES.items()
                if (api, auth) == (gateway.get("api"), gateway.get("auth"))
            ),
            None,
        )
        self.provider_label.configure(
            text=(
                "Current: "
                + current
                + f"\nClient base URL: http://127.0.0.1:{gateway['port']}"
                + ("/v1" if gateway.get("api") == "openai" else "")
            )
            if current and gateway.get("port")
            else "No managed provider selected"
        )
        if not self.provider_dirty:
            self.provider_mode.set(current or next(iter(PROVIDER_MODES)))
        self.voice_label.configure(
            text="Ready · English tiny.en · Runs offline"
            if value.get("voice")
            else "Model not installed · Download once to enable voice"
        )
        state = self.gateway_state = str(value["gateway"]["state"])
        running = state == "running"
        titles = {
            "running": "Your gateway is ready.",
            "unmanaged": "Ready when you are.",
            "stopped": "Your gateway is stopped.",
            "crashed": "Your gateway needs a restart.",
            "starting": "Your gateway is starting…",
            "stopping": "Your gateway is stopping…",
            "unresponsive": "Your gateway is not responding.",
            "unsupported": "Gateway controls unavailable.",
            "unknown": "Status unavailable.",
        }
        details = {
            "running": "Available for clients configured to route through Veil.",
            "unmanaged": "Start the local gateway when you are ready to connect.",
            "stopped": "Start it again whenever you need it.",
            "crashed": "Run diagnostics or start the gateway to recover.",
            "unsupported": "Use the foreground gateway on this platform.",
            "unknown": "Run diagnostics to check your local configuration.",
            "unresponsive": (
                "Check diagnostics before restarting. "
                "Active requests may be interrupted."
            ),
        }
        tone = _TEAL if running else _MUTED
        if state in {"unknown", "crashed", "unresponsive"}:
            tone = _ERROR
        self.gateway_title.configure(text=titles.get(state, "Gateway · " + state))
        self.gateway_detail.configure(
            text=details.get(state, "Checking the local gateway process.")
        )
        readable = {"running": "Ready", "unmanaged": "Not started"}.get(
            state, state.title()
        )
        self.badge.configure(text="●  " + readable, foreground=tone)
        self.side_status.configure(
            text="●  " + ("Gateway ready" if running else readable), foreground=tone
        )
        self.startup_state = value["startup"]["state"]
        self.startup_summary.configure(text=self.startup_state.title())
        self.startup_label.configure(text="Login startup is " + self.startup_state)
        self.names_summary.configure(
            text="Unknown"
            if value["names"] is None
            else "Configured"
            if value["names"]
            else "Off"
        )
        self.names_label.configure(
            text="Recognition status unavailable"
            if value["names"] is None
            else "Model configured · Restart after changes"
            if value["names"]
            else "Off · Exact registrations still apply"
        )
        count = value["registrations"]
        self.registrations_summary.configure(
            text="Unknown" if count is None else f"{count} saved"
        )
        self._button_states()

    def _refresh(self) -> None:
        if self.closed:
            return
        self.submit(
            self.controller.snapshot,
            "Checking local status…",
            self._update_snapshot,
            foreground=False,
        )
        self.poll = self.root.after(5000, self._refresh)

    def _receive(self) -> None:
        if self.closed:
            return
        try:
            done, value, error, foreground = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._complete(done, value, error, foreground)
        self.receiver = self.root.after(40, self._receive)

    def _complete(
        self,
        done: Callable[[Any], None] | None,
        value: Any,
        error: str | None,
        foreground: bool,
    ) -> None:
        if foreground:
            self.pending = False
            self.foreground = False
            if self.animation is not None:
                self.root.after_cancel(self.animation)
                self.animation = None
            self.loader.itemconfigure(self.arc, extent=0)
            self.busy_text.configure(text="")
            self._button_states()
        else:
            self.refreshing = False
            # The queued read predates the action. Its result must not reset an
            # active loader, unlock controls, or overwrite a recording timer.
            if self.pending or self.recording is not None:
                return
        if error:
            if foreground or self.snapshot is None:
                self.notice_label.configure(foreground=_ERROR)
                self.notice.set(error)
            if done == self._update_snapshot:
                self._update_snapshot(
                    {
                        "gateway": {"state": "unknown"},
                        "startup": {"state": "unknown"},
                        "names": None,
                        "registrations": None,
                    }
                )
        elif done:
            done(value)
        elif isinstance(value, str):
            self.notice_label.configure(foreground=_TEAL)
            self.notice.set(value)
        if foreground and done != self._update_snapshot:
            self.submit(
                self.controller.snapshot,
                "Refreshing status…",
                self._update_snapshot,
                foreground=False,
            )

    def _gateway(self, operation: Literal["start", "stop", "restart"]) -> None:
        labels = {"start": "Starting", "stop": "Stopping", "restart": "Restarting"}
        self.submit(
            lambda: self.controller.gateway(operation),
            f"{labels[operation]} gateway…",
        )

    def _apply_provider(self) -> None:
        mode = self.provider_mode.get()

        def applied(message: str) -> None:
            self.provider_dirty = False
            self.notice.set(message)

        self.submit(
            lambda: self.controller.provider(mode),
            "Applying provider and restarting gateway…",
            applied,
        )

    def _choose_model(self) -> None:
        if self.pending:
            return
        selected = self.filedialog.askdirectory(
            parent=self.root, title="Choose a trusted local spaCy model"
        )
        if selected:
            self.submit(
                lambda: self.controller.names(Path(selected)), "Validating local model…"
            )

    def _register(self) -> None:
        if self.pending:
            return
        kind, value = self.kind.get(), self.private.get()
        self.private.set("")
        self.submit(
            lambda: self.controller.register(kind, value),
            "Saving private registration…",
        )

    def _example(self) -> None:
        self._clear_preview()
        self.source.insert(
            "1.0", "Please send the draft to jane@example.com before Friday."
        )
        self._set_output("")
        self.source.focus_set()

    def _import_pdf(self) -> None:
        self._import_document(scan=False)

    def _import_scan(self) -> None:
        self._import_document(scan=True)

    def _import_document(self, *, scan: bool) -> None:
        if self.pending or self.recording is not None:
            return
        selected = self.filedialog.askopenfilename(
            parent=self.root,
            title="Scan PDF or image locally" if scan else "Import PDF text locally",
            filetypes=(
                [
                    (
                        "PDF and images",
                        "*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.webp *.bmp",
                    )
                ]
                if scan
                else [("PDF documents", "*.pdf")]
            ),
        )
        if not selected:
            return
        self._set_output("")

        def show(document: PDFText | OCRText) -> None:
            self.source.delete("1.0", "end")
            self.source.insert("1.0", document.text)
            self.source.edit_reset()
            self.pdf_detail.configure(text=document.summary)
            self.notice_label.configure(foreground=_TEAL)
            self.notice.set(
                "OCR text is ready. Correct any misread details, then preview masking. "
                "The original file is unchanged."
                if scan
                else "PDF text is ready. Review it, then choose Preview masking. "
                "The original PDF is unchanged."
            )
            self.source.focus_set()

        self.submit(
            lambda: (
                self.controller.import_scan(Path(selected))
                if scan
                else self.controller.import_pdf(Path(selected))
            ),
            "Recognizing text on this device…"
            if scan
            else "Reading PDF text on this device…",
            show,
        )

    def _show_voice(self, document: VoiceText) -> None:
        self.source.configure(state="normal")
        self.source.delete("1.0", "end")
        self.source.insert("1.0", document.text)
        self.source.edit_reset()
        self.pdf_detail.configure(text=document.summary)
        self.notice_label.configure(foreground=_TEAL)
        self.notice.set(
            "Voice text is ready. Correct misheard details, then preview masking. "
            "The original audio has not been uploaded or changed."
        )
        self.source.focus_set()

    def _import_audio(self) -> None:
        if self.pending or self.recording is not None:
            return
        selected = self.filedialog.askopenfilename(
            parent=self.root,
            title="Transcribe local audio",
            filetypes=[
                (
                    "Audio recordings",
                    "*.wav *.mp3 *.m4a *.flac *.ogg *.aac *.aiff *.webm",
                )
            ],
        )
        if selected:
            self._set_output("")
            self.submit(
                lambda: self.controller.import_audio(Path(selected)),
                "Transcribing audio on this device…",
                self._show_voice,
            )

    def _record_voice(self) -> None:
        if self.pending:
            return
        if self.recording is not None:
            recording = self._detach_recording()
            assert recording is not None
            self.submit(
                lambda: self.controller.transcribe_recording(recording),
                "Microphone stopped. Transcribing on this device…",
                self._show_voice,
            )
            return
        self._set_output("")

        def start() -> None:
            recording = self.controller.start_recording()
            # Closing during a permission prompt must not leave capture running.
            with self.record_lock:
                if self.closed:
                    recording.cancel()
                else:
                    self.recording = recording

        def started(_: Any) -> None:
            self.record_button.configure(text="Stop & transcribe")
            self.record_started = time.monotonic()
            self._button_states()
            self._record_tick()

        self.submit(start, "Opening microphone. Check permission if prompted…", started)

    def _record_tick(self) -> None:
        self.record_timer = None
        recording = self.recording
        if self.closed or recording is None:
            return
        elapsed = int(time.monotonic() - self.record_started)
        if recording.finished.is_set() or elapsed >= 300:
            self._record_voice()
            return
        self.notice_label.configure(foreground=_ERROR)
        self.notice.set(
            f"● Recording {elapsed // 60:02}:{elapsed % 60:02} / 05:00 · "
            "Stop & transcribe, or Discard. Audio stays in memory."
        )
        self.busy_text.configure(text="● Microphone recording")
        self.record_timer = self.root.after(200, self._record_tick)

    def _detach_recording(self) -> MicrophoneRecording | None:
        with self.record_lock:
            recording, self.recording = self.recording, None
        if self.record_timer is not None:
            self.root.after_cancel(self.record_timer)
            self.record_timer = None
        self.record_button.configure(text="Record voice…")
        self.busy_text.configure(text="")
        self._button_states()
        return recording

    def _discard_recording(self) -> None:
        recording = self._detach_recording()
        if recording is not None:

            def discard() -> str:
                recording.cancel()
                return "Recording discarded. No audio was saved."

            self.submit(discard, "Stopping microphone and discarding audio…")

    def _set_output(self, value: str) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", value)
        self.output.configure(state="disabled")

    def _source_changed(self, event: Any = None) -> None:
        if self.source.edit_modified():
            self.source.edit_modified(False)
            # A preview only describes the exact text that was checked.
            if self.output.get("1.0", "end-1c"):
                self._set_output("")

    def _clear_preview(self) -> None:
        self.source.delete("1.0", "end")
        self.source.edit_reset()
        self._set_output("")
        self.pdf_detail.configure(
            text="Import documents or English audio. Set up voice in Settings."
        )

    def _mask_preview(self) -> None:
        if self.pending:
            return
        value = self.source.get("1.0", "end-1c")
        self._set_output("")

        def show(masked: str) -> None:
            self._set_output(masked)
            self.notice_label.configure(foreground=_TEAL)
            self.notice.set(
                "Preview complete. Use veil mask with a session "
                "for restorable chat text."
            )

        self.submit(
            lambda: self.controller.preview(value), "Checking your text locally…", show
        )

    def _diagnostics(self) -> None:
        def show(value: str) -> None:
            window = self.tk.Toplevel(self.root, background=_WHITE)
            window.title("Veil — Local diagnostics")
            window.geometry("760x600")
            body = self._frame(window, padx=24, pady=24)
            body.pack(fill="both", expand=True)
            self._label(body, "Local diagnostics", size=20, bold=True).pack(anchor="w")
            output = self._text_area(body, readonly=True)
            output.configure(state="normal")
            output.insert("1.0", value)
            output.configure(state="disabled")
            self.notice.set("Local diagnostics are ready.")

        self.submit(self.controller.diagnostics, "Running local diagnostics…", show)

    def close(self) -> None:
        """Cancel timers and UI callbacks without stopping the gateway."""
        if self.closed:
            return
        self.closed = True
        with self.record_lock:
            recording, self.recording = self.recording, None
        if recording is not None:
            with contextlib.suppress(Exception):
                recording.cancel()
        for callback in (
            self.animation,
            self.poll,
            self.receiver,
            self.record_timer,
            self.layout_timer,
            self.transition,
        ):
            if callback is not None:
                self.root.after_cancel(callback)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def run(self) -> int:
        """Read status, then run the native event loop."""
        self.root.update_idletasks()
        self.root.deiconify()
        self._refresh()
        self._receive()
        self.root.mainloop()
        return 0
