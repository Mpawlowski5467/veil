"""Masking stays byte-identical for traffic an older release accepted.

``gateway_payloads/golden.json`` holds, for each body in ``gateway_bodies``
and each set of registered values, what the masker produced (or the refusal,
if it refused the body). It was first written by the 0.4.0 masker; a change a
release makes on purpose is recorded there by rewriting it, with the reason in
the commit. Rewrite it with ``VEIL_WRITE_GOLDEN=1 uv run pytest
tests/test_gateway_golden.py``.
"""

import json
import os
from pathlib import Path

import pytest

import gateway_bodies as bodies
from veil import LiteralPlaceholderDetector, MemoryVault, RegexDetector, Shield
from veil.gateway import MemoryLedger, RequestMasker, UnsupportedRequestError

GOLDEN = Path(__file__).parent / "gateway_payloads" / "golden.json"
WRITE = os.environ.get("VEIL_WRITE_GOLDEN") == "1"
CASES = [
    (body, registered) for body in bodies.BODIES for registered in bodies.REGISTERED
]


def masked(body_name, registered_name):
    """Mask a golden body as the gateway would: a fresh conversation."""
    registered = bodies.REGISTERED[registered_name]
    types = {*RegexDetector().entity_types, *registered.values()}
    shield = Shield(
        detectors=[LiteralPlaceholderDetector(types), RegexDetector()],
        vault=MemoryVault(),
        redact_warnings=True,
    )
    for value, kind in registered.items():
        shield.add_entity(value, kind)
    masker = RequestMasker(shield, MemoryLedger(), registered=registered)
    try:
        out = {"masked": masker.mask(bodies.BODIES[body_name]())}
    except UnsupportedRequestError as error:
        return {"refused": str(error)}
    # Claude Code's own shapes all have rules: nothing is masked generically.
    assert masker.last_generic == ()
    return out


def test_golden_file_covers_every_case():
    if WRITE:
        GOLDEN.write_text(
            json.dumps(
                {f"{b}/{r}": masked(b, r) for b, r in CASES},
                indent=1,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert sorted(golden) == sorted(f"{b}/{r}" for b, r in CASES)


@pytest.mark.parametrize(("body_name", "registered_name"), CASES)
def test_masking_matches_the_golden_file(body_name, registered_name):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert (
        masked(body_name, registered_name) == golden[f"{body_name}/{registered_name}"]
    )


def test_golden_file_is_fictional():
    from live.harness import assert_fictional

    assert_fictional(GOLDEN.read_text(encoding="utf-8"))
