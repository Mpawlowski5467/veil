# Standalone Mac app — local 1.0.0rc1 candidate

The current feature set is frozen. This build packages the existing desktop,
menu-bar, gateway, PDF, Apple Vision OCR, and local voice libraries with Python
and Tk. An installed app needs neither a Python installation nor this checkout.
Name recognition still requires a selected trusted local spaCy model; voice
requires its explicit, pinned model download. No private data or voice weights
are copied into the installer.

The current candidate targets **Apple Silicon (arm64)** and was tested on
**macOS 26.5.1**. Intel and older macOS versions have not been validated. The
bundle's deployment floor is not evidence that every earlier OS works.

## Install and open

The local build produces `dist/macos/Veil-1.0.0rc1-macos-arm64.dmg` and
`dist/macos/SHA256SUMS`. Open the disk image, drag **Veil.app** onto
**Applications**, eject the image, and open **Veil** from Applications.
If this build is already installed, open `/Applications/Veil.app`.

This candidate is **ad-hoc signed, not Developer ID signed or notarized**.
The signature checks bundle integrity; it does not establish an Apple-verified
publisher. A copy downloaded to another Mac may be blocked by Gatekeeper.
Do not disable Gatekeeper or strip quarantine. Public distribution remains
blocked on signing, notarization, and testing that downloaded installation path.

Opening the window does not start a gateway, enable login startup, or configure
client routing. Configure those explicitly using the existing controls. Models
download only when requested. Follow the [desktop guide](desktop.md),
[provider guide](providers.md), and [feature limits](../README.md#understand-the-boundaries).

The app also includes a CLI, without adding anything to your shell PATH:

```bash
/Applications/Veil.app/Contents/MacOS/veil-runtime --help
/Applications/Veil.app/Contents/MacOS/veil-runtime tray
```

The second command opens the existing optional menu-bar controls. The desktop
and menu-bar processes are separate. Closing either leaves a gateway running.

## Replace, move, or remove

Before replacing or moving the app, disable Veil's login startup, stop its
gateway, and close the window/menu bar. Install the replacement at the same
path. If the path changed, reinstall the assistant skill and re-enable startup
from the new location so their pinned paths are updated. Do not run an app from
the disk image and then configure startup against that temporary location.

To remove the app, first disable startup and stop the gateway. Undo any client
routing and remove an installed assistant skill if you no longer want them.
Then move **Veil.app** to Trash. Configuration, models, and mappings in `~/.veil`
remain separate. Preserve them if you need to restore old placeholders. There
is no automatic private-data deletion during uninstall.

## Build from this checkout

Build on an Apple Silicon Mac with uv and a CPython 3.14 installation that
includes Tk (the uv-managed 3.14.6 runtime was used for this candidate). Keep the
build environment outside the checkout. All runtime versions come from `uv.lock`;
build-tool versions are pinned in `packaging/macos/requirements-build.txt`.

```bash
uv build
uv venv --python 3.14 /tmp/veil-macos-build-env
uv export --frozen --no-dev --extra tray --extra names --extra pdf --extra ocr --extra voice \
  --no-emit-project --no-hashes --format requirements-txt \
  --output-file /tmp/veil-macos-runtime-requirements.txt
uv pip sync --python /tmp/veil-macos-build-env/bin/python /tmp/veil-macos-runtime-requirements.txt
uv pip install --python /tmp/veil-macos-build-env/bin/python -r packaging/macos/requirements-build.txt
uv pip install --python /tmp/veil-macos-build-env/bin/python --no-deps dist/veil-1.0.0rc1-py3-none-any.whl
/tmp/veil-macos-build-env/bin/python packaging/macos/build.py
/tmp/veil-macos-build-env/bin/python packaging/macos/smoke.py dist/macos/Veil.app \
  --report dist/macos/clean-install-report.json
/tmp/veil-macos-build-env/bin/python packaging/macos/dmg.py
```

An optional `--voice-model /trusted/folder/voice-model` smoke argument copies
only four known public model assets, verifies their hashes, and transcribes
fictional synthesized speech offline. It never opens the microphone. The smoke
test relocates the app to a path containing spaces, uses disposable explicit
data/config paths and a hostile import directory, verifies workers, setup/undo,
skill pinning, gateway restart/stop, and reinstall preservation.

For a native startup/recovery check of the installed app, run
`python packaging/macos/startup_smoke.py /Applications/Veil.app --report dist/macos/startup-report.json`.
It temporarily registers a real user LaunchAgent, checks crash/stop/resume
behavior with fictional data, and removes the registration afterward. It does
not log out or reboot the Mac, or change the normal Veil startup setting.

The app carries project/Python/dependency notices and `build-manifest.json`
under `Contents/Resources`. The manifest records versions and a SHA-256 digest
of the installed Veil package contents; it is not a Git commit or reproducible
binary-build claim. The app uses a fixed console helper for known workers,
without exposing a general Python evaluation command.
Frozen gateway helpers allow up to 30 seconds for first-launch validation;
startup failures still clean up only the newly launched child.

For a future signed build, `VEIL_CODESIGN_IDENTITY` can select an installed
Developer ID identity. That setting alone does not notarize or staple the app.
Apple entitlements, signing, notarization, third-party license completeness,
and a fresh downloaded installation must be reviewed before publication. See
[PyInstaller's macOS notes](https://pyinstaller.org/en/stable/feature-notes.html).
