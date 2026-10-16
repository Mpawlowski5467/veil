# PyInstaller spec: both executables share one read-only app runtime.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
from veil import __version__

root = Path(SPECPATH)
generated = Path(os.environ["VEIL_BUNDLE_RESOURCES"])
datas = collect_data_files("veil")
datas += collect_data_files("faster_whisper")
for package in ("veil", "spacy", "faster-whisper", "pypdfium2", "sounddevice"):
    datas += copy_metadata(package, recursive=True)
datas += [(str(generated / name), ".") for name in (
    "Veil.icns", "LICENSE.txt", "PYTHON-LICENSE.txt", "THIRD-PARTY-NOTICES.txt",
    "build-manifest.json",
)]

a = Analysis(
    [str(root / "entrypoint.py")],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("veil") + collect_submodules("av") + [
        "tkinter", "tkinter.ttk", "tkinter.filedialog", "PIL.Image", "PIL.ImageTk",
        "pystray._darwin", "spacy", "pypdf", "pypdfium2", "Vision", "Quartz",
        "Foundation", "AppKit", "faster_whisper", "sounddevice", "_sounddevice_data",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "IPython", "matplotlib", "torch", "tensorflow", "scipy", "pandas"],
    noarchive=False,
)
pyz = PYZ(a.pure)
identity = os.environ.get("VEIL_CODESIGN_IDENTITY") or None
gui = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="Veil", console=False,
    target_arch="arm64", codesign_identity=identity, argv_emulation=False,
)
helper = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="veil-runtime", console=True,
    target_arch="arm64", codesign_identity=identity,
)
app = BUNDLE(
    gui, helper, a.binaries, a.datas, name="Veil.app",
    icon=str(generated / "Veil.icns"), bundle_identifier="org.veil.desktop",
    version=__version__,
    info_plist={
        "CFBundleDisplayName": "Veil",
        "CFBundleName": "Veil",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0b1",
        "VeilReleaseVersion": __version__,
        "NSHighResolutionCapable": True,
        "LSBackgroundOnly": False,
        "LSMinimumSystemVersion": "14.0",
        "NSMicrophoneUsageDescription": "Record audio only when you choose Record voice. Audio is transcribed locally on your Mac.",
        "NSHumanReadableCopyright": "Veil contributors. MIT License.",
    },
)
