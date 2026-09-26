"""Normalizers: when two spellings are the same value."""

import copy
import pickle
import random
import unicodedata

import pytest

from vaults import DictVault
from veil import MemoryVault, RegexDetector, Shield, ShieldError
from veil._normalize import (
    BUILTIN_NORMALIZERS,
    normalize_card,
    normalize_email,
    normalize_iban,
    normalize_ipv6,
    normalize_phone,
)
from veil.detectors import ManualDetector
from veil.masker import Masker
from veil.restorer import Restorer

FW = str.maketrans(
    "0123456789", "\uff10\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19"
)


def same_key(normalizer, forms):
    keys = {normalizer(form) for form in forms}
    assert None not in keys, forms
    return len(keys) == 1


class TestEmail:
    def test_case_merges(self):
        assert same_key(
            normalize_email,
            ["jan.n@example.com", "Jan.N@Example.com", "JAN.N@EXAMPLE.COM"],
        )

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("jan.n@example.com", "jan.n+news@example.com"),  # plus tag
            ("jan.n@example.com", "jann@example.com"),  # dots
            ("straße@example.com", "strasse@example.com"),  # lower(), not casefold()
            ("\uff4a\uff41\uff4e@example.com", "jan@example.com"),  # no NFKC
            ("jan@example.com", "連絡先はjan@example.com"),  # glued text stays
        ],
    )
    def test_different_addresses_stay_apart(self, a, b):
        assert normalize_email(a) != normalize_email(b)

    def test_canonically_equivalent_forms_merge(self):
        nfc = "josé@example.com"
        assert normalize_email(unicodedata.normalize("NFD", nfc)) == normalize_email(
            nfc
        )
        for upper, lower in [
            ("\u0141UCJA", "\u0142ucja"),
            ("\u03a3\u039f\u03a6\u0399\u0391", "\u03c3\u03bf\u03c6\u03b9\u03b1"),
            ("I\u015eIK", "i\u015fik"),
            (unicodedata.normalize("NFD", "JOS\u00c9"), "jos\u00e9"),
        ]:
            assert normalize_email(f"{upper}@example.com") == normalize_email(
                f"{lower}@example.com"
            ), upper

    @pytest.mark.parametrize(
        "value",
        [
            "ADMIN_EMAIL=jan@example.com",  # restore would re-case the label
            "unsubscribe?email=jan@example.com",
            "Users/Jan/jan@example.com",
            "unsubscribe?email%3DJan@example.com",  # URL-encoded glue
            "Hello%20Jan@example.com",
            "\u212arl@example.com",  # KELVIN SIGN, lowercases to k
            "\u212bngstrom@example.com",  # ANGSTROM SIGN
            "\u0130rem@example.com",  # I WITH DOT ABOVE
            "\u01c5ivko@example.com",  # titlecase DZ digraph
            "mailto:jan@example.com",
            "Jan <jan@example.com>",
            " jan@example.com",
            "jan@example",
            "<jan@example.com>",
        ],
    )
    def test_guard(self, value):
        assert normalize_email(value) is None


US_FORMS = [
    "555-555-0123",
    "(555) 555-0123",
    "(555)555-0123",
    "555.555.0123",
    "555 555 0123",
    "+1 555 555 0123",
    "+1-555-555-0123",
    "1-555-555-0123",
    "1 (555) 555-0123",
    "+1 (555) 555-0123",
    "+15555550123",
    "(+1) 555 555 0123",
    "555\u2011555\u20110123",
    "(555)\u00a0555-0123",
    "555-555-0123".translate(FW),
]
UK_FORMS = [
    "+44 20 7946 0958",
    "+44 (0)20 7946 0958",
    "+44 (0) 20 7946 0958",
    "+442079460958",
    "(+44) 20 7946 0958",
    "+44-20-7946-0958",
    "+44 20 79460958",
]


class TestPhone:
    def test_us_forms_merge(self):
        assert same_key(normalize_phone, US_FORMS)

    def test_intl_forms_merge(self):
        assert same_key(normalize_phone, UK_FORMS)

    def test_extension_spellings_merge_but_stay_apart_from_the_number(self):
        with_ext = ["555-555-0123 ext. 89", "555-555-0123 x89", "(555) 555-0123 EXT.89"]
        assert same_key(normalize_phone, with_ext)
        assert normalize_phone("555-555-0123 x89") != normalize_phone("555-555-0123")
        assert normalize_phone("555-555-0123 x89") != normalize_phone(
            "555-555-0123 x089"
        )

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("555-555-0123", "555-555-0124"),
            ("555-555-0123".translate(FW), "555-555-0124".translate(FW)),
            ("+44 20 7946 0958", "+44 020 7946 0958"),  # trunk 0 needs "(0)"
            ("+44 20 7946 0958", "+44 20 7946 0958 24"),  # digits swallowed
            ("+49 711 1234567-890", "+49 711 1234567"),
            ("+39 06 1234 5678", "+39 6 1234 5678"),
            ("555-555-0123", "+52 555 555 0123"),
            ("+44 20 7946 0958", "+44 20 7946 (0)0958"),  # "(0)" not a trunk
            ("+44 20 7946 0958", "+44 20 7946 0958 (0)"),
            ("+33 1 23 45 67 89", "+33 1 23 45 67 (0) 89"),
        ],
    )
    def test_different_numbers_stay_apart(self, a, b):
        assert normalize_phone(a) != normalize_phone(b)

    @pytest.mark.parametrize(
        "value",
        [
            "Tel: 555-555-0123",
            "tel:+1-555-555-0123",
            "5555550123",
            "555-0123",
            "555-555-0123 (mobile)",
            " 555-555-0123",
            "+44 20 7946 0958 or",
            "+44 20 7946 (0)0958",
        ],
    )
    def test_guard(self, value):
        assert normalize_phone(value) is None

    @pytest.mark.parametrize(
        "value",
        ["+44(0)20 7946 0958", "(+44) (0) 20 7946 0958", "+44-(0)-20-7946-0958"],
    )
    def test_trunk_prefix_spellings(self, value):
        assert normalize_phone(value) == normalize_phone("+44 20 7946 0958")


class TestOtherTypes:
    def test_ipv6(self):
        assert same_key(
            normalize_ipv6,
            ["2001:db8::1", "2001:DB8::1", "2001:0db8:0000:0000:0000:0000:0000:0001"],
        )
        assert normalize_ipv6("2001:db8::1") != normalize_ipv6("2001:db8::2")
        for value in ["[2001:db8::1]", "2001:db8::1%eth0", "2001:db8::1/64"]:
            assert normalize_ipv6(value) is None

    def test_card(self):
        assert same_key(
            normalize_card,
            ["4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111"],
        )
        assert same_key(normalize_card, ["3782 822463 10005", "378282246310005"])
        assert normalize_card("4111 1111 1111 1111 12/25") is None
        assert normalize_card("Card: 4111 1111 1111 1111") is None  # a label
        assert normalize_card("1234 5678 9012 3456 7890") is None  # 20 digits
        assert normalize_card("1234 5678 901") is None  # 11 digits

    def test_iban(self):
        assert same_key(
            normalize_iban,
            [
                "DE89 3704 0044 0532 0130 00",
                "DE89370400440532013000",
                "de89 3704 0044 0532 0130 00",
            ],
        )
        assert normalize_iban("IBAN DE89 3704 0044 0532 0130 00") is None
        assert normalize_iban("XX12 ABCD") is None  # shorter than 15
        assert normalize_iban("DE89" + " 0000" * 8) is None  # longer than 34
        assert normalize_iban("DE89-3704-0044-0532-0130-00") is None

    def test_ipv4_and_custom_types_have_no_normalizer(self):
        assert set(BUILTIN_NORMALIZERS) == {
            "EMAIL",
            "PHONE",
            "IPV6",
            "CREDIT_CARD",
            "IBAN",
        }

    @pytest.mark.parametrize(
        "value",
        [
            "jan.n@example.com",
            *US_FORMS,
            *UK_FORMS,
            "2001:db8::1",
            "4111 1111 1111 1111",
            "DE89 3704 0044 0532 0130 00",
        ],
    )
    def test_every_detected_form_is_one_whole_span(self, value):
        # The forms above are what the built-in detector emits as a whole span.
        assert [s.value for s in RegexDetector().detect(f"x {value} y")][:1] == [value]


class TestShield:
    def test_default_is_exact(self):
        shield = Shield()
        assert shield.mask("(555) 555-0123 555-555-0123").text == "[PHONE_1] [PHONE_2]"

    def test_normalize_must_be_bool(self):
        with pytest.raises(TypeError):
            Shield(normalize={"ORDER": str.upper})

    def test_one_placeholder_restores_the_first_spelling(self):
        shield = Shield(normalize=True)
        masked = shield.mask("Call 555-555-0123, or (555) 555-0123. Jan.N@Example.com")
        assert masked.text == "Call [PHONE_1], or [PHONE_1]. [EMAIL_1]"
        assert [(e.placeholder, e.value, e.entity_type) for e in masked.entities] == [
            ("[PHONE_1]", "555-555-0123", "PHONE"),
            ("[PHONE_1]", "(555) 555-0123", "PHONE"),
            ("[EMAIL_1]", "Jan.N@Example.com", "EMAIL"),
        ]
        assert shield.restore(masked.text).text == (
            "Call 555-555-0123, or 555-555-0123. Jan.N@Example.com"
        )
        assert masked.warnings == []

    def test_across_calls_the_vault_keeps_one_value(self):
        shield = Shield(normalize=True)
        shield.mask("jan.n@example.com")
        assert shield.mask("JAN.N@EXAMPLE.COM and Jan.N@example.com").text == (
            "[EMAIL_1] and [EMAIL_1]"
        )
        assert shield.vault.items() == [("[EMAIL_1]", "jan.n@example.com")]

    def test_tolerant_restore_uses_the_first_spelling(self):
        shield = Shield(normalize=True)
        shield.mask("+44 20 7946 0958 / +44 (0)20 7946 0958")
        assert shield.restore("[phone 1] 【PHONE_1】").text == (
            "+44 20 7946 0958 +44 20 7946 0958"
        )

    def test_leak_check_remembers_variant_spellings(self):
        shield = Shield(normalize=True)
        shield.mask("555-555-0123 (555) 555-0123")
        assert shield.mask("(555) 555-01234").warnings == [
            "Leak check: known value '(555) 555-0123' ([PHONE_1]) still appears "
            "in the masked text."
        ]

    def test_redacted_variant_leak(self):
        shield = Shield(normalize=True, redact_warnings=True)
        shield.mask("555-555-0123 (555) 555-0123")
        assert shield.mask("(555) 555-01234").warnings == [
            "Leak check: a known PHONE value ([PHONE_1]) still appears in the "
            "masked text."
        ]
        with pytest.raises(ShieldError) as error:
            shield.wrap(lambda prompt: prompt, strict=True)("(555) 555-01234")
        assert "555" not in str(error.value)

    def test_reset_forgets_variants(self):
        shield = Shield(normalize=True)
        shield.mask("555-555-0123 (555) 555-0123")
        shield.reset()
        assert shield.mask("(555) 555-01234").warnings == []
        assert shield.mask("(555) 555-0123 555-555-0123").text == "[PHONE_1] [PHONE_1]"
        assert shield.vault.items() == [("[PHONE_1]", "(555) 555-0123")]

    def test_variant_dropped_when_its_placeholder_changes_hands(self):
        shield = Shield(normalize=True)
        shield.mask("555-555-0123 (555) 555-0123")
        shield.vault.clear()  # directly, not through reset()
        shield.mask("555-555-0199")  # [PHONE_1] is now another number
        assert shield.mask("(555) 555-01234").warnings == []
        assert shield.mask("(555) 555-0123").text == "[PHONE_2]"

    def test_shared_vault(self):
        vault = MemoryVault()
        exact, norm = Shield(vault=vault), Shield(vault=vault, normalize=True)
        exact.mask("jan.n@example.com")
        assert norm.mask("JAN.N@EXAMPLE.COM").text == "[EMAIL_1]"
        assert exact.mask("JAN.N@EXAMPLE.COM").text == "[EMAIL_2]"
        assert norm.mask("Jan.N@example.com JAN.N@EXAMPLE.COM").text == (
            "[EMAIL_1] [EMAIL_2]"  # first stored spelling wins; exact hits win
        )

    def test_types_never_merge(self):
        shield = Shield(
            normalize=True, custom_patterns={"CONTACT": r"jan@example\.com"}
        )
        assert shield.mask("jan@example.com JAN@example.com").text == (
            "[CONTACT_1] [EMAIL_1]"
        )
        shield = Shield(normalize=True)
        shield.add_entity("555-555-0123", "PERSON")
        assert shield.mask("555-555-0123 (555) 555-0123").text == "[PERSON_1] [PHONE_1]"

    def test_manual_email_merges_with_detected_spellings(self):
        shield = Shield(normalize=True)
        shield.add_entity("Jan.N@Example.com", "EMAIL")
        assert shield.mask("Jan.N@Example.com jan.n@example.com").text == (
            "[EMAIL_1] [EMAIL_1]"
        )

    @pytest.mark.parametrize(
        "make",
        [
            lambda: Shield(
                normalize=True,
                custom_patterns={"PHONE": r"Tel: \d{3}-\d{3}-\d{4}|\d{3}-\d{3}-\d{4}"},
            ),
            lambda: _with_entity(Shield(normalize=True), "Tel: 555-555-0123", "PHONE"),
        ],
    )
    def test_value_with_a_label_is_not_merged(self, make):
        shield = make()
        assert shield.mask("Tel: 555-555-0123, 555-555-0123").text == (
            "[PHONE_1], [PHONE_2]"
        )

    def test_placeholder_like_warnings_unchanged(self):
        exact, norm = Shield(), Shield(normalize=True)
        for shield in (exact, norm):
            shield.mask("555-555-0123 (555) 555-0123")
        text = "see [PHONE_2] and [phone 3]"
        assert norm.mask(text).warnings == exact.mask(text).warnings

    def test_person_1_vs_person_10_style(self):
        shield = Shield(normalize=True)
        shield.mask(" ".join(f"555-555-01{i:02d}" for i in range(1, 12)))
        masked = shield.mask(" ".join(f"(555) 555-01{i:02d}" for i in range(1, 12)))
        assert masked.text.split() == [f"[PHONE_{i}]" for i in range(1, 12)]
        assert (
            shield.restore("[PHONE_1] [PHONE_10]").text == "555-555-0101 555-555-0110"
        )

    def test_masker_accepts_custom_normalizers(self):
        masker = Masker(
            [ManualDetector(), RegexDetector({"ORDER": r"(?i)ord-\d{4}"})],
            MemoryVault(),
            normalizers={"ORDER": str.upper},
        )
        assert masker.mask("ord-1234 ORD-1234 ord-9999").text == (
            "[ORDER_1] [ORDER_1] [ORDER_2]"
        )

    def test_equal_keys_of_different_types_never_merge(self):
        masker = Masker(
            [
                RegexDetector(
                    {"ORDER": r"ord-\d{4}", "TICKET": r"ORD-\d{4}"},
                    include_builtins=False,
                )
            ],
            MemoryVault(),
            normalizers={"ORDER": str.upper, "TICKET": str.upper},
        )
        assert masker.mask("ord-1234 ORD-1234 ord-1234").text == (
            "[ORDER_1] [TICKET_1] [ORDER_1]"
        )


def _with_entity(shield, value, entity_type):
    shield.add_entity(value, entity_type)
    return shield


PHONES = [*US_FORMS, *UK_FORMS, "555-555-0199", "+44 20 7946 0999", "555-555-0123 x12"]
EMAILS = [
    "jan.n@example.com",
    "Jan.N@Example.com",
    "JAN.N@EXAMPLE.COM",
    "anna.k@example.org",
    "Anna.K@Example.org",
    "jan.n+news@example.com",
]
OTHERS = [
    "2001:db8::1",
    "2001:DB8:0:0:0:0:0:1",
    "4111 1111 1111 1111",
    "4111111111111111",
    "DE89 3704 0044 0532 0130 00",
    "de89370400440532013000",
]
GLUE = [" ", ", ", "\n", " (", ") ", "; ", " | "]
BREAK = ["7", "x"]  # glued to a phone or card, the detector misses it


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_fuzz_matches_exact_mode(seed):
    """Same spans, round trip to first spellings, and the same leak reports."""
    rng = random.Random(seed)
    exact, norm = Shield(), Shield(normalize=True)
    for turn in range(600):
        if turn % 30 == 0:
            exact.reset()
            norm.reset()
        parts = []
        for _ in range(rng.randint(1, 6)):
            value = rng.choice(rng.choice([PHONES, EMAILS, OTHERS]))
            parts.append(value + (rng.choice(BREAK) if rng.random() < 0.1 else ""))
        text = rng.choice(GLUE).join(parts)
        m_exact, m_norm = exact.mask(text), norm.mask(text)
        context = f"seed={seed} turn={turn} text={text!r}"
        spans = [(e.start, e.end, e.value) for e in m_norm.entities]
        assert spans == [(e.start, e.end, e.value) for e in m_exact.entities], context
        expected, cursor = "", 0
        for e in m_norm.entities:
            expected += text[cursor : e.start] + norm.vault.get_value(e.placeholder)
            cursor = e.end
        restored = norm.restore(m_norm.text)
        assert restored.text == expected + text[cursor:], context
        assert restored.warnings == [], context
        assert len(m_norm.warnings) == len(m_exact.warnings), context
        assert len({e.placeholder for e in m_norm.entities}) <= len(
            {e.placeholder for e in m_exact.entities}
        ), context


CARD = "4111 1111 1111 1111"


def leaks(warnings):
    return [w for w in warnings if w.startswith("Leak check")]


class TestSharedVaults:
    VARIANTS = "Call 555-555-0123 or (555) 555-0123."
    LATER = "Old note: (555) 555-01234"

    def test_new_shield_per_request(self):
        vault = MemoryVault()
        Shield(vault=vault, normalize=True).mask(self.VARIANTS)
        assert leaks(Shield(vault=vault, normalize=True).mask(self.LATER).warnings) == [
            "Leak check: known value '(555) 555-0123' ([PHONE_1]) still appears "
            "in the masked text."
        ]

    def test_exact_shield_sees_the_other_shields_variants(self):
        vault = MemoryVault()
        Shield(vault=vault, normalize=True).mask(self.VARIANTS)
        assert len(leaks(Shield(vault=vault).mask(self.LATER).warnings)) == 1

    def test_strict_wrap_on_a_shared_vault(self):
        vault = MemoryVault()
        Shield(vault=vault, normalize=True).mask(self.VARIANTS)
        llm = Shield(vault=vault, normalize=True).wrap(lambda p: p, strict=True)
        with pytest.raises(ShieldError):
            llm(self.LATER)

    def test_other_shields_reset_forgets_variants(self):
        vault = MemoryVault()
        a = Shield(vault=vault, normalize=True)
        b = Shield(vault=vault)
        a.mask(self.VARIANTS)
        b.reset()
        a.mask("New chat: 555-555-0123")
        assert a.mask(self.LATER).warnings == []


class TestThirdPartyVault:
    def test_masker_remembers_variants_itself(self):
        s = Shield(vault=DictVault(), normalize=True)
        assert s.mask(TestSharedVaults.VARIANTS).text == "Call [PHONE_1] or [PHONE_1]."
        assert len(leaks(s.mask(TestSharedVaults.LATER).warnings)) == 1

    def test_reset_forgets(self):
        s = Shield(vault=DictVault(), normalize=True)
        s.mask(TestSharedVaults.VARIANTS)
        s.reset()
        s.mask("New chat: 555-555-0123")  # the same number gets [PHONE_1] again
        assert s.mask(TestSharedVaults.LATER).warnings == []

    def test_clearing_the_vault_elsewhere_drops_stale_entries(self):
        vault = DictVault()
        s = Shield(vault=vault, normalize=True)
        s.mask(TestSharedVaults.VARIANTS)
        vault.clear()
        s.mask("555-555-0199")  # [PHONE_1] now holds another number
        assert s.mask(TestSharedVaults.LATER).warnings == []


EXT = {"EXT": r"0123 [A-Z]+"}  # merges with a phone ending in 0123


class TestNormalizeAndMerge:
    @pytest.mark.parametrize(
        "turns",
        [
            ["Call 555-555-0123", "Call 555-555-0123 ABC", "Call (555) 555-0123"],
            ["Call 555-555-0123 ABC", "Call (555) 555-0123", "Call 555-555-0123"],
        ],
    )
    def test_union_is_never_normalized(self, turns):
        s = Shield(custom_patterns=EXT, normalize=True)
        for turn in turns:
            masked = s.mask(turn)
            restored = s.restore(masked.text).text
            if "ABC" in turn:
                assert [e.source for e in masked.entities] == ["merged"]
                assert restored == turn  # never merged with the plain number
            else:
                assert "ABC" not in restored

    def test_union_first_then_variant_gets_its_own_placeholder(self):
        s = Shield(custom_patterns=EXT, normalize=True)
        assert s.mask("Call 555-555-0123 ABC").text == "Call [PHONE_1]"
        assert s.mask("Call (555) 555-0123").text == "Call [PHONE_2]"
        assert s.mask("Call 555.555.0123").text == "Call [PHONE_2]"

    def test_merged_member_variant_is_known(self):
        s = Shield(normalize=True)
        s.mask(f"Pay +1 {CARD} today")
        [warning] = s.mask(f"card x{CARD}").warnings
        assert "[CREDIT_CARD_1]" in warning


class TestMergedValuesAreExact:
    """Through the internal seam, with a lossy normalizer (digits only)."""

    @staticmethod
    def masker():
        vault = MemoryVault()
        detectors = [RegexDetector({"EXT": r"0123 [A-Z]+"})]

        def digits(value: str) -> str:
            return "".join(ch for ch in value if ch.isdigit())

        return Masker(detectors, vault, normalizers={"PHONE": digits}), vault

    @pytest.mark.parametrize(
        "turns",
        [
            ["Call 555-555-0123", "Call 555-555-0123 ABC"],
            ["Call 555-555-0123 ABC", "Call 555-555-0123", "Call (555) 555-0123"],
        ],
    )
    def test_round_trip(self, turns):
        masker, vault = self.masker()
        for turn in turns:
            restored = Restorer(vault).restore(masker.mask(turn).text).text
            assert ("ABC" in restored) == ("ABC" in turn)


class TestNormalizeGuards:
    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("ADMIN_EMAIL=jan@example.com", "admin_email=jan@example.com"),
            ("unsubscribe?email=Jan@example.com", "unsubscribe?EMAIL=jan@example.com"),
            ("Users/Jan/jan@example.com", "users/jan/jan@example.com"),
            ("\u212arl@example.com", "karl@example.com"),  # KELVIN SIGN
        ],
    )
    def test_glued_labels_and_lookalike_letters_stay_exact(self, first, second):
        s = Shield(normalize=True)
        a, b = s.mask(first), s.mask(second)
        assert a.entities[0].placeholder != b.entities[0].placeholder
        assert s.restore(b.text).text == second


class TestKeyIndex:
    def test_vault_refilled_to_the_same_size(self):
        vault = MemoryVault()
        s = Shield(vault=vault, normalize=True)
        assert s.mask("555-555-0123").text == "[PHONE_1]"
        vault.clear()
        vault.get_or_create("555-555-0199", "PHONE")  # [PHONE_1] again, same size
        masked = s.mask("(555) 555-0123")
        assert masked.text == "[PHONE_2]"
        assert s.restore(masked.text).text == "(555) 555-0123"

    def test_growing_conversation_rarely_rebuilds(self, monkeypatch):
        import veil._normalize as normalize

        rebuilds = []
        real = normalize.KeyIndex._rebuild

        def counting(self, vault):
            rebuilds.append(1)
            return real(self, vault)

        monkeypatch.setattr(normalize.KeyIndex, "_rebuild", counting)
        s = Shield(normalize=True)
        for turn in range(1000):
            s.mask(f"Call 555-555-{turn:04d} or ({555}) 555-{turn:04d}")
        assert len(s.vault) == 1000
        assert len(rebuilds) <= 2

    def test_repeated_merged_value_keeps_the_index(self, monkeypatch):
        import veil._normalize as normalize

        rebuilds = []
        real = normalize.KeyIndex._rebuild

        def counting(self, vault):
            rebuilds.append(1)
            return real(self, vault)

        monkeypatch.setattr(normalize.KeyIndex, "_rebuild", counting)
        s = Shield(normalize=True)
        s.add_entity("Anna Maria", "PERSON")
        s.add_entity("Maria Kowalska", "PERSON")
        for turn in range(50):
            s.mask(f"Present: Anna Maria Kowalska, mail u{turn}@example.com")
        assert len(rebuilds) <= 2

    def test_another_writer_grows_the_shared_vault(self):
        vault = MemoryVault()
        a, b = Shield(vault=vault, normalize=True), Shield(vault=vault)
        a.mask("call (555) 555-0123")  # a's index is built and in sync
        b.mask("mail jan.n@example.com")  # b grows the vault behind a's back
        assert a.mask("mail JAN.N@example.com").text == "mail [EMAIL_1]"

    def test_another_writer_grows_the_vault_while_a_merged_value_repeats(self):
        vault = MemoryVault()
        a = Shield(vault=vault, normalize=True, custom_patterns={"TAIL": r"0130 00 12"})
        b = Shield(vault=vault)
        a.mask("call (555) 555-0123")
        a.mask("IBAN DE89 3704 0044 0532 0130 00 12 ok")  # a merged value
        b.mask("mail jan.n@example.com")  # a is now one value behind
        a.mask("IBAN DE89 3704 0044 0532 0130 00 12 ok")  # the vault doesn't grow
        assert a.mask("mail JAN.N@example.com").text == "mail [EMAIL_1]"

    @pytest.mark.parametrize("then", ["pickle", "mask"])
    @pytest.mark.parametrize("how", ["clear", "other shield's reset"])
    def test_values_from_a_cleared_vault_are_dropped(self, how, then):
        vault = MemoryVault()
        s, other = Shield(vault=vault, normalize=True), Shield(vault=vault)
        s.mask("Mail jan.n@example.com or JAN.N@EXAMPLE.COM, call 555-555-0123")
        if how == "clear":
            vault.clear()
        else:
            other.reset()
        if then == "pickle":
            assert b"jan.n@example.com" not in pickle.dumps(s)
        else:
            s.mask("Mail anna.k@example.com")
            cached = {value for value, _ in s._masker._keys._keys.values()}
            assert cached <= {value for _, value in vault.items()}

    def test_vault_cleared_and_refilled_to_the_same_size_by_another_shield(self):
        vault = MemoryVault()
        a = Shield(vault=vault, normalize=True)
        b = Shield(vault=vault, normalize=True)
        b.mask("Mail kai@example.net")  # b's index: one value
        a.reset()
        a.mask("Call (213) 555-0199")  # one value again, a different one
        assert b.mask("Call 213-555-0199").text == "Call [PHONE_1]"

    def test_stale_index_never_gives_another_types_placeholder(self):
        vault = MemoryVault()
        a, b = Shield(vault=vault, normalize=True), Shield(vault=vault)
        a.mask("x@example.com ; DE89 3704 0044 0532 0130 00")
        vault.clear()
        b.mask("y@example.com ; 555-555-0100")  # refilled to the same size
        text = "gb82west12345698765432 ; de89370400440532013000"
        masked = a.mask(text)
        assert masked.text == "[IBAN_1] ; [IBAN_2]"
        assert a.restore(masked.text).text == text

    def test_reset_clears_the_key_index(self):
        s = Shield(normalize=True)
        s.mask("Call 555-555-0101 or (555) 555-0101")
        s.reset()
        assert s._masker._keys._keys == {}
        assert s._masker._keys._index is None

    @pytest.mark.parametrize("make_vault", [MemoryVault, DictVault])
    def test_pickle_leaves_out_values_of_a_vault_another_shield_reset(self, make_vault):
        vault = make_vault()
        a, b = Shield(vault=vault, normalize=True), Shield(vault=vault)
        a.mask("Mail Jan.N@Example.com and jan.n@example.com")
        b.reset()
        b.mask("Mail anna@example.org")  # the vault holds one value again
        data = pickle.dumps(a)
        assert b"jan.n" not in data.lower()

    def test_pickle_round_trip(self):
        s = Shield(normalize=True)
        s.mask("Call 555-555-0123 or (555) 555-0123, jan.n@example.com")
        clone = pickle.loads(pickle.dumps(s))
        text = "Call 555.555.0123, JAN.N@example.com, x(555) 555-01234"
        assert clone.mask(text) == s.mask(text)
        assert copy.deepcopy(s).mask(text) == s.mask(text)

    def test_state_from_before_normalization(self):
        # What unpickling a v0.2 masker hands __setstate__: no _keys.
        s = Shield()
        masker = s._masker
        state = {k: v for k, v in masker.__dict__.items() if k != "_keys"}
        masker.__dict__.clear()
        masker.__setstate__(state)
        assert s.mask("555-555-0123 (555) 555-0123").text == "[PHONE_1] [PHONE_2]"
