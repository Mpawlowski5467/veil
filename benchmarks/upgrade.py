"""Exercise previous -> current -> previous -> current with installed wheels.

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
        # Simulate a newly confirmed credential; older versions must retain
        # its mapping even though they cannot detect this class automatically.
        shield.add_entity("fictional upgrade phrase", "PASSWORD")
        assert shield.mask("fictional upgrade phrase").text == "[PASSWORD_1]"
    if phase in ("rollback", "return"):
        assert shield.restore("[PERSON_1]").text == "Mira Quill"
        assert shield.restore("[PASSWORD_1]").text == "fictional upgrade phrase"
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

BETA_PROGRAM = """
import json
import sys
from pathlib import Path
from veil import __version__
from veil.gateway import Settings, open_sessions

folder, phase = Path(sys.argv[1]), sys.argv[2]
fixture = json.loads(Path(sys.argv[3]).read_text(encoding='utf-8'))
source, old_mappings, probe, expected = (
    fixture[key] for key in ('source', 'old_mappings', 'probe', 'expected')
)
for api in ('anthropic', 'openai'):
    data = folder / ('beta-' + api)
    data.mkdir(exist_ok=True)
    def mask(session, text):
        if api == 'openai':
            request = {'model': 'test', 'input': text}
            return session.masker.mask(request)['input']
        request = {
            'model': 'test', 'max_tokens': 10,
            'messages': [{'role': 'user', 'content': text}],
        }
        return session.masker.mask(request)['messages'][0]['content']
    settings = Settings(identity=False, note=False)
    with open_sessions(data, settings, {}, api=api) as sessions:
        session = sessions.get('upgrade')
        if phase == 'seed':
            assert __version__ == '0.6.0b1', __version__
            masked = mask(session, source)
            assert dict(session.shield.vault.items()) == old_mappings
            assert session.shield.restore(masked).text == source
            (data / 'seed.json').write_text(json.dumps(masked))
        # Preserve every old mapping, including false positives, for restoration.
        for placeholder, value in old_mappings.items():
            assert session.shield.restore(placeholder).text == value
        masked = json.loads((data / 'seed.json').read_text())
        assert session.shield.restore(masked).text == source
        if phase in ('upgrade', 'return'):
            assert mask(session, probe) == expected
            # Quoting still masks code-shaped passwords explicitly.
            assert mask(session, 'PASSWORD="password"') == 'PASSWORD="[PASSWORD_1]"'
        if phase == 'upgrade':
            assert mask(session, 'PASSWORD="maple"') == 'PASSWORD="[PASSWORD_4]"'
        if phase in ('rollback', 'return'):
            assert session.shield.restore('[PASSWORD_4]').text == 'maple'
        if phase == 'rollback':
            assert __version__ == '0.6.0b1', __version__
            # Rollback retains mappings but restores the old detector behavior.
            assert mask(session, 'ToString letmein_old') == (
                'To[API_KEY_1] [PASSWORD_3]_old'
            )
        if phase == 'return':
            assert mask(session, 'maple maples') == '[PASSWORD_4] maples'
            session.shield.vault.clear()
            assert len(session.shield.vault) == 0
    print(api + ': persisted beta mappings passed')
"""


def main():
    """Check two installed interpreters against the same fictional databases."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-python", required=True, type=Path)
    parser.add_argument("--current-python", required=True, type=Path)
    args = parser.parse_args()
    fixture = Path(__file__).with_name("upgrade_fixture.json").resolve()
    previous_version = subprocess.check_output(
        [
            str(args.previous_python),
            "-I",
            "-c",
            "from veil import __version__; print(__version__)",
        ],
        text=True,
        timeout=30,
    ).strip()
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
            if previous_version == "0.6.0b1":
                subprocess.run(
                    [
                        str(python),
                        "-I",
                        "-c",
                        BETA_PROGRAM,
                        directory,
                        phase,
                        str(fixture),
                    ],
                    check=True,
                    timeout=30,
                )
            print(f"{phase}: passed")


if __name__ == "__main__":
    main()
