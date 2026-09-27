"""Seeded fuzz: PII glued together so that matches overlap.

Values are joined by characters that addresses, phone extensions, and custom
patterns can absorb ("/", "&", "=", "." or nothing), so detectors return
overlapping spans. Whatever wins each overlap, no letter or digit of any
detected span may stay visible, and restoring gives back the exact input.
"""

import random

import pytest

from veil import ManualDetector, RegexDetector, Shield

VALUES = [
    "jan.n@example.com",
    "anna.k@example.org",
    "o'brien@example.com",
    "EMP123456@example.com",
    "555-555-0123",
    "555-555-0123 ext. 89",
    "+44 20 7946 0958",
    "+1 555 555 0199",
    "192.0.2.1",
    "203.0.113.254",
    "2001:db8::1",
    "4111 1111 1111 1111",
    "5555 5555 5555 4444",
    "3782 822463 10005",
    "DE89 3704 0044 0532 0130 00",
    "GB82 WEST 1234 5698 7654 32",
    "Jan Nowak",
    "Anna Maria",
    "Maria Kowalska",
    "Nowak Street",
    "山田太郎",
    "EMP42",
    "#12345",
    "TKT-4242",
    "ORD-2024-0001",
]
NAMES = {
    "Jan Nowak": "PERSON",
    "Anna Maria": "PERSON",
    "Maria Kowalska": "PERSON",
    "Nowak Street": "ADDRESS",
    "Kowalska": "PERSON",
    "山田太郎": "PERSON",
}
CUSTOM = {
    "EMPID": r"\bEMP\d+\b",
    "ORDER": r"#\d{5}",
    "TICKET": r"TKT-\d{4,6}",
    "ORDNO": r"ORD-\d{4}-\d{4}",
    "ZIP": r"\b\d{5}\b",
}
GLUES = [
    "",
    "",
    " ",
    "/",
    "&",
    "=",
    ".",
    "'",
    "+",
    "#",
    "-",
    ":",
    "x",
    "7",
    " 2024",
    "の",
    "และ",
]


def new_shield():
    shield = Shield(custom_patterns=CUSTOM)
    for value, entity_type in NAMES.items():
        shield.add_entity(value, entity_type)
    return shield


def detected(text):
    manual = ManualDetector()
    for value, entity_type in NAMES.items():
        manual.add(value, entity_type)
    return manual.detect(text) + RegexDetector(CUSTOM).detect(text)


@pytest.mark.parametrize("seed", [1, 2])
def test_overlapping_matches_never_stay_partly_visible(seed):
    rng = random.Random(seed)
    shield = new_shield()
    for turn in range(1500):
        if turn % 25 == 0:
            shield = new_shield()
        parts = [rng.choice(VALUES) for _ in range(rng.randint(2, 4))]
        text = rng.choice(["", " ", "(", "Tel "]) + "".join(
            part + rng.choice(GLUES) for part in parts
        )
        masked = shield.mask(text)
        context = f"seed={seed} turn={turn} text={text!r} masked={masked.text!r}"
        assert shield.restore(masked.text).text == text, context
        covered = [False] * len(text)
        for entity in masked.entities:
            assert text[entity.start : entity.end] == entity.value, context
            covered[entity.start : entity.end] = [True] * (entity.end - entity.start)
        for found in detected(text):
            visible = [
                text[i]
                for i in range(found.start, found.end)
                if text[i].isalnum() and not covered[i]
            ]
            assert not visible, (
                f"{context} left {''.join(visible)!r} of {found.value!r}"
            )
