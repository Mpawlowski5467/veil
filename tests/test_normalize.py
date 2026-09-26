"""Normalizers: when two spellings are the same value."""

import unicodedata

import pytest

from veil import RegexDetector
from veil._normalize import (
    BUILTIN_NORMALIZERS,
    normalize_card,
    normalize_email,
    normalize_iban,
    normalize_ipv6,
    normalize_phone,
)

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
        ],
    )
    def test_guard(self, value):
        assert normalize_phone(value) is None


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
