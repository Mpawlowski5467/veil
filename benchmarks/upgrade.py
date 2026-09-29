"""Exercise v0.4.1 -> current -> v0.4.1 -> current with installed wheels.

Run from the repository root. Uses only fictional values in a temporary folder.
No running gateway or personal configuration is touched. The old release runs
on the host platform; Windows support is new, so old Windows storage behavior
is not claimed to be secure by this check.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

PROGRAM = """
import sys
from pathlib import Path
from veil import Shield, SQLiteVault
from veil.gateway.store import SQLiteLedger
folder, phase = Path(sys.argv[1]), sys.argv[2]
with SQLiteVault(folder / "vault.db", session="upgrade") as vault:
    shield = Shield(vault=vault)
    if phase == "seed":
        assert shield.mask("upgrade@example.com").text == "[EMAIL_1]"
    else:
        assert shield.restore("[EMAIL_1]").text == "upgrade@example.com"
    if phase == "upgrade":
        shield.add_entity("Mira Quill", "PERSON")
        assert shield.mask("Mira Quill").text == "[PERSON_1]"
    if phase in ("rollback", "return"):
        assert shield.restore("[PERSON_1]").text == "Mira Quill"
ledger = SQLiteLedger(folder / "ledger.db", "upgrade")
try:
    if phase == "seed":
        ledger.record_text("upgrade@example.com", "[EMAIL_1]")
    else:
        assert ledger.masked_text("upgrade@example.com") == "[EMAIL_1]"
    if phase == "upgrade":
        ledger.record_seen({"type": "future_result", "text": "fictional"})
    if phase == "return":
        assert ledger.was_seen({"type": "future_result", "text": "fictional"})
        ledger.forget()
        assert not ledger.was_seen({"type": "future_result", "text": "fictional"})
        assert ledger.masked_text("upgrade@example.com") is None
finally:
    ledger.close()
if phase == "return":
    with SQLiteVault(folder / "vault.db", session="upgrade") as vault:
        vault.clear()
        assert len(vault) == 0
"""


def main():
    """Check two installed interpreters against the same fictional databases."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-python", required=True, type=Path)
    parser.add_argument("--current-python", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="veil-upgrade-") as directory:
        for python, phase in (
            (args.previous_python, "seed"),
            (args.current_python, "upgrade"),
            (args.previous_python, "rollback"),
            (args.current_python, "return"),
        ):
            subprocess.run(
                [str(python), "-I", "-c", PROGRAM, directory, phase],
                check=True,
                timeout=30,
            )
            print(f"{phase}: passed")


if __name__ == "__main__":
    main()
