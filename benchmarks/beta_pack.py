"""Build the release beta pack from a fixed list of fictional exercise files."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from veil import __version__

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ("beta/README.md", "README.md"),
    ("beta/extra-tests.md", "extra-tests.md"),
    ("beta/check.py", "check.py"),
    ("beta/feedback.md", "feedback.md"),
    ("beta/workspace/contact.txt", "workspace/contact.txt"),
    ("beta/workspace/example.py", "workspace/example.py"),
    ("beta/workspace/example.env", "workspace/example.env"),
    ("LICENSE", "LICENSE"),
)


def build(output: Path) -> Path:
    """Write only allowlisted files, excluding local reports and beta data."""
    output.mkdir(parents=True, exist_ok=True)
    name = f"veil-{__version__}-beta"
    destination = output / f"{name}.zip"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as pack:
        for source, member in FILES:
            info = zipfile.ZipInfo(f"{name}/{member}", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            # Normalize checkout line endings for reproducible cross-platform packs.
            content = (ROOT / source).read_text(encoding="utf-8")
            pack.writestr(info, content.encode("utf-8"))
    return destination


def main() -> None:
    """Build a versioned archive for manual release publishing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    print(build(args.output_dir))


if __name__ == "__main__":
    main()
