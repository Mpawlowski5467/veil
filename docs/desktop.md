# Desktop and menu-bar controls (unreleased)

These additions are in the 1.0 plan and source checkout, not the published
0.6.0b2 wheel. They control the existing supported text gateways.

## Open the controls

Install the checkout into a persistent environment:

```bash
python -m pip install '.[desktop]'
veil desktop
```

The native window requires a Python installation with Tk support and a graphical
desktop. It offers gateway start/stop/restart, diagnostics, opt-in login startup,
private value registration, local name-model selection, and a masking preview.
Install `'.[desktop,pdf]'` to add **Import file… → PDF text…** in the preview. PDF extraction
runs locally and imports selectable text for review before masking; see
[PDF text import and limits](pdf-text.md).
Install `'.[desktop,ocr]'` for **Import file… → Scan PDF or image…**, which recognizes scanned
pages and images locally. Review and correct OCR text before masking; see
[OCR setup and limits](ocr.md).
Install `'.[desktop,voice]'` for **Import file… → Audio recording…** and **Record voice…**. In
Settings, **Download voice model** installs the public English model once;
transcription then works locally offline. Recording has a timer, stop/transcribe,
and discard controls. See [local voice setup, review, and limits](voice.md).
The window has Overview, Masking preview, and Settings views, a light interface
with teal accents, rounded cards, separate original/result panels, and a loader
built from the project logo. A brief slide eases navigation; the logo animates
while an action runs. **Settings → Appearance → Reduce motion** uses instant
navigation and a still progress indicator for the current window. Routine status
polling leaves controls usable and skips unchanged snapshots. Status and action
messages have fixed space so they do not move the rest of the layout. Layouts
wrap and scroll in smaller windows, and each view remembers its scroll position.
Native File/Edit/View/Help menus provide close, editing, navigation, and diagnostics;
use Command on macOS or Control elsewhere with 1/2/3 to switch views, comma for
Settings, and W to close. The desktop extra includes Pillow for smooth logo rendering;
basic controls also work with Tk's built-in image renderer.
The sidebar uses a transparent symbol with a native text wordmark, and the Dock
uses a separate rounded teal app icon with image sizes up to 512 pixels. See the
[logo assets and generation prompts](assets/desktop-branding.md).
Use `veil desktop --config PATH` for an alternate Codex configuration; the global
`--data-dir PATH` selects another private data folder.

Set up client routing first with the existing setup/launcher workflow. Opening
or closing the window does not edit Codex settings, start a gateway, enable
startup, or make a model call. Start/stop/restart affect the selected gateway and
can interrupt active requests. Closing controls leaves it running.

The status line describes gateway readiness. It does not verify that an active
chat used Veil; use `veil verify` for request-specific evidence.

## Menu bar / system tray

**Settings → AI provider** selects OpenAI API-key mode, Codex's OpenAI ChatGPT
sign-in mode, or Anthropic. **Apply & restart gateway** applies the mode through
the existing lifecycle controls; selecting a dropdown alone does not act. Your
client still supplies its credential and must connect to the displayed local
base URL. Disable login startup before changing modes, then enable it again.
See [provider setup](providers.md) for clients, routes, and limits.

```bash
python -m pip install '.[tray]'
veil tray
```

The icon provides start, stop, restart, readiness status, and Open Veil controls.
It uses the shared Veil ribbon artwork: a native monochrome template on macOS
that adapts to light/dark menu bars, and the teal application icon on other
platforms. The macOS renderer keeps a high-resolution backing image for Retina.
Quit menu closes the icon and leaves the gateway alone. The icon's native loop
runs on the main thread, as required by [pystray](https://pystray.readthedocs.io/en/latest/usage.html).
Linux tray visibility depends on the desktop and installed tray backend; a
desktop that hides legacy tray icons can still use `veil desktop`.

Neither the icon nor window currently has a signed standalone installer or
automatic icon launch. [Gateway login startup](background-gateway.md#automatic-login-startup-unreleased)
is a separate, explicit setting.

## Names and preview

Private registration input is hidden and stays out of process arguments. Restart
the gateway after adding registrations or changing a local model. Choosing a
model loads and validates it before saving the selection; install `[names]` first
and follow the [local model guide](local-names.md).

The desktop preview uses the selected data folder's patterns, registrations,
Git identity setting, and optional local name model. It checks exact restoration
in memory, without persisting preview text or mappings. With local secret review
enabled, unresolved candidates refuse preview output and direct you to the
explicit CLI review workflow. Preview placeholders are temporary: use
`veil mask --session LABEL` for text whose later reply must be restored.
Use **Use an example** to try a fictional email address. **Clear** removes preview
input, output, and the input widget's undo history. Switching views preserves
unfinished preview text in memory. Editing source text clears the old masked
result so it cannot be mistaken for a preview of the edited text.

The existing browser `veil preview` remains a separate built-in-rules playground
that does not load personal settings.

## Validation still needed

The macOS native views and a fictional email preview were exercised locally,
including keyboard traversal between views and preview controls at a narrow size.
A fictional two-page PDF was selected through the macOS file picker, imported,
and masked in the desktop preview with the gateway left running.
Controller regressions cover registration, in-memory preview, and CLI dispatch.
Opt-in native tests check logo animation, safe close during background work,
error handling, page focus isolation, and the renderer without Pillow. The
native regression suite also covers status reads overlapping user actions,
stale status failures, rapid navigation, remembered scrolling, minimum-width
controls, stable action/status layout, reduced motion, menu navigation, and an
actual in-memory example through the controller. These checks exercise macOS;
they do not replace longer manual journeys or other platform validation.
Linux/Windows native UI, screen readers, broader scaling/keyboard journeys, menu actions,
packaged installation/removal, and external-user testing remain release gates.
Windows gateway lifecycle/startup buttons currently report unsupported; the
foreground `veil gateway` and local text/name workflows remain available.
