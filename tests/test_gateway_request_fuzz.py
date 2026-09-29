"""Seeded fuzzing of the request masker: odd bodies are refused, never crash it.

Anything but `UnsupportedRequestError` would reach the client as a bug, so
every body built here must either mask or be refused, with no value in the
refusal. Set ``VEIL_FUZZ_CASES`` for a longer run.
"""

import json
import math
import os
import random

import pytest

from veil import LiteralPlaceholderDetector, MemoryVault, RegexDetector, Shield
from veil.gateway import (
    MemoryLedger,
    RequestMasker,
    SQLiteLedger,
    UnsupportedRequestError,
)
from veil.vault.sqlite import SQLiteVault

CASES = int(os.environ.get("VEIL_FUZZ_CASES", "300"))
EMAIL = "jan.n@example.com"
NAME = "Jan Nowak"

ODD_STRINGS = [
    "",
    " ",
    "\ud800",
    "a\udfffb",
    "\x00",
    "[EMAIL_1]",
    "[EMAIL_999999999]",
    "x" * 70,
    "Jan Nowakem",
    EMAIL,
    NAME,
    "type",
    "text",
    "tool_use",
    "base64",
    "url",
    "‮",
    "\\",
    "\U0001d49c",
    "a b",
    "toolu_\ud800",
]


def base_body():
    return {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 32000,
        "metadata": {"user_id": json.dumps({"session_id": "s-1"})},
        "thinking": {"type": "adaptive"},
        "tools": [{"name": "Read", "description": "Reads", "input_schema": {}}],
        "stop_sequences": ["END"],
        "safeguards": [
            {"type": "dangerous_tool_use", "classifier_context": {"who": EMAIL}}
        ],
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.1"},
            {"type": "text", "text": f"User is {EMAIL}"},
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Email {NAME} at {EMAIL}"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBOR",
                        },
                    },
                    {
                        "type": "document",
                        "title": f"Notes {NAME}",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": f"hi {EMAIL}",
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "[EMAIL_1]", "signature": "s"},
                    {"type": "redacted_thinking", "data": "opaque"},
                    {"type": "text", "text": f"Reading for {NAME}"},
                    {
                        "type": "tool_use",
                        "id": "toolu_rec",
                        "name": "Bash",
                        "input": {"command": f"echo {NAME}"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_rec",
                        "content": [{"type": "text", "text": f"Card {NAME}"}],
                    }
                ],
            },
            {"role": "system", "content": [], "output_config": {"effort": "high"}},
        ],
    }


def scalar(rng):
    return rng.choice(
        [
            *ODD_STRINGS,
            0,
            -1,
            2**53 + 1,
            10**300,
            0.5,
            -0.0,
            math.inf,
            math.nan,
            True,
            False,
            None,
        ]
    )


def value(rng, depth=0):
    roll = rng.random()
    if depth > 3 or roll < 0.5:
        return scalar(rng)
    if roll < 0.75:
        return [value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    keys = ["type", "text", "source", "id", "data", "content", rng.choice(ODD_STRINGS)]
    return {rng.choice(keys): value(rng, depth + 1) for _ in range(rng.randint(0, 3))}


def nested(rng, levels):
    item = rng.choice(["x", EMAIL, 1])
    for _ in range(levels):
        item = [item] if rng.random() < 0.5 else {"k": item}
    return item


def places(body):
    stack = [(body, ())]
    while stack:
        item, path = stack.pop()
        yield path
        if isinstance(item, dict):
            stack.extend((child, (*path, key)) for key, child in item.items())
        elif isinstance(item, list):
            stack.extend((child, (*path, i)) for i, child in enumerate(item))


def mutate(rng, body):
    path = rng.choice(list(places(body)))
    if not path:
        return
    parent = body
    for step in path[:-1]:
        parent = parent[step]
    last = path[-1]
    roll = rng.random()
    if roll < 0.4:
        parent[last] = value(rng)
    elif roll < 0.5:
        parent[last] = nested(rng, rng.choice([50, 120, 400]))
    elif roll < 0.65:
        del parent[last]
    elif roll < 0.85:
        target = parent[last]
        if isinstance(target, dict):
            target[rng.choice([*ODD_STRINGS, "citations", "cache_control"])] = value(
                rng
            )
        elif isinstance(target, list):
            target.append(value(rng))
    else:
        target = parent[last]
        if isinstance(target, dict) and "type" in target:
            target["type"] = rng.choice([["text"], {"a": 1}, "future_block", 5])
        elif isinstance(target, dict) and "role" in target:
            target["role"] = rng.choice([["user"], {"r": 1}, 1, None, "tool"])


def shield_on(vault):
    shield = Shield(
        detectors=[
            LiteralPlaceholderDetector({"EMAIL", "PHONE", "PERSON"}),
            RegexDetector(),
        ],
        vault=vault,
        redact_warnings=True,
    )
    shield.add_entity(NAME, "PERSON")
    return shield


@pytest.fixture(params=["memory", "sqlite"])
def make_masker(request, tmp_path):
    def make(case):
        if request.param == "memory":
            shield, ledger = shield_on(MemoryVault()), MemoryLedger()
        else:
            session = f"s{case}"
            shield = shield_on(SQLiteVault(tmp_path / "vault.db", session=session))
            ledger = SQLiteLedger(tmp_path / "ledger.db", session)
        ledger.record_tool_input(
            "toolu_rec", {"command": f"echo {NAME}"}, {"command": "echo [PERSON_1]"}
        )
        return RequestMasker(shield, ledger, registered={NAME: "PERSON"})

    return make


def refusal(masker, body):
    """The refusal's text, or None if the body was masked."""
    try:
        masker.mask(body)
    except UnsupportedRequestError as error:
        return str(error)
    return None


def test_odd_bodies_are_masked_or_refused_never_crash(make_masker):
    rng = random.Random(20260927)
    for case in range(CASES):
        body = base_body()
        for _ in range(rng.randint(1, 4)):
            mutate(rng, body)
        try:
            # As the server reads it: a body JSON can't hold isn't possible.
            body = json.loads(json.dumps(body))
        except (ValueError, RecursionError):
            continue
        text = refusal(make_masker(case), body) or ""
        assert EMAIL not in text, case
        assert NAME not in text, case
