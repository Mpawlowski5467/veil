"""Seeded fuzz test: realistic mixed text must never leak PII silently.

Every generated text mixes fictional PII with ordinary prose, Markdown, URLs,
and several scripts. For each one the test checks the library's core promises:
every seeded value is fully masked (or `mask()` warns), restoring gives back
the exact input, and the reported entities line up with the text.
"""

import random
import unicodedata

import pytest

from veil import Shield
from veil.placeholders import PLACEHOLDER_RE

EMAILS = [
    "jan.n@example.com",
    "first.last+tag@sub.example.co.uk",
    "sean.o'brien@example.com",
    "łucja@example.com",
    "bounce+jan=example.com@lists.example.org",
    "anna_k@example.net",
    "zoë@example.com",
    "jan@example.xn--p1ai",
    "USER123@MAIL.EXAMPLE.COM",
    "山田太郎@example.co.jp",
]
US_PHONES = [
    "555-123-4567",
    "(555) 123-4567",
    "+1 555 123 4567",
    "555.123.4567",
    "555-123-4567 ext. 89",
    "1-555-123-4567",
    "(555)\u00a0123-4567",
]
INTL_PHONES = [
    "+44 20 7946 0958",
    "(+48) 123 456 789",
    "+49 (0)30 1234567",
    "+81 3 1234 5678",
    "+33 1 23 45 67 89",
    "+44 20 7946 0958 ext. 12",
    "+61-2-5550-1234",
    "+4912345678",
    "+49 (0)711 1234567-890",
]
IPS = ["192.0.2.1", "198.51.100.23", "203.0.113.254", "10.0.0.1"]
NAMES = [
    "Jan Nowak",
    "Anna Kowalska",
    "Zoë Müller",
    "王小明",
    "राम शर्मा",
    unicodedata.normalize("NFD", "José Núñez"),
    "Ольга Иванова",
    "สมชาย",
]
ORDERS = ["#12345", "#99999", "TKT-4242"]
CONTEXT = [
    "Hello",
    "please call",
    "連絡先は",
    "です",
    "谢谢",
    "**note**",
    "`code`",
    "- item",
    "> quote",
    "https://example.org/path?q=1",
    '{"k": "v"}',
    "the 24 hours",
    "v1.2",
    "2024-01-15",
    "안녕하세요",
    "ครับ",
    "नमस्ते",
    "price 1.50",
    "Q3 report",
    "¿Qué tal?",
]
# Always contain a non-word character, so no seeded value is glued to an ASCII
# letter or digit (a documented limitation, not what this test is about).
SEPARATORS = [" ", ", ", "\n", " (", ") ", ": ", "; ", " - ", "、", "。", " | ", "\t"]
KINDS = [
    (EMAILS, 0.18),
    (US_PHONES, 0.12),
    (INTL_PHONES, 0.14),
    (IPS, 0.08),
    (NAMES, 0.10),
    (ORDERS, 0.06),
]


def generate(rng):
    """Return (text, seeded) where seeded lists (start, end, value) for PII."""
    text, seeded = "", []
    for index in range(rng.randint(1, 8)):
        if index:
            text += rng.choice(SEPARATORS)
        roll, pool = rng.random(), CONTEXT
        for values, weight in KINDS:
            if roll < weight:
                pool = values
                break
            roll -= weight
        value = rng.choice(pool)
        if pool is not CONTEXT:
            seeded.append((len(text), len(text) + len(value), value))
        text += value
    return text, seeded


def new_shield():
    shield = Shield(custom_patterns={"ORDER": r"#\d{5}\b", "TICKET": r"\bTKT-\d+\b"})
    for name in NAMES:
        shield.add_entity(name, "PERSON")
    return shield


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_silent_leaks(seed):
    rng = random.Random(seed)
    shield = new_shield()
    for turn in range(1500):
        if turn % 25 == 0:  # a new conversation every 25 turns
            shield = new_shield()
        text, seeded = generate(rng)
        if PLACEHOLDER_RE.search(text):
            continue
        masked = shield.mask(text)
        restored = shield.restore(masked.text)
        context = f"seed={seed} turn={turn} text={text!r} masked={masked.text!r}"

        assert restored.text == text, context
        assert restored.restored_count == len(masked.entities), context
        assert restored.warnings == [], context
        covered = [False] * len(text)
        for entity in masked.entities:
            assert text[entity.start : entity.end] == entity.value, context
            covered[entity.start : entity.end] = [True] * (entity.end - entity.start)
        for start, end, value in seeded:
            visible = [
                text[i]
                for i in range(start, end)
                if not covered[i] and text[i].isalnum()
            ]
            assert not visible or masked.warnings, f"{context} leaked {value!r}"
