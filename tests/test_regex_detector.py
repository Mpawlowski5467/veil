import re

import pytest

from veil.detectors import Detector, RegexDetector
from veil.types import Span


@pytest.fixture(scope="module")
def detector():
    return RegexDetector()


def found(detector, text):
    return [(s.entity_type, s.value) for s in detector.detect(text)]


def values(detector, text, entity_type):
    return [s.value for s in detector.detect(text) if s.entity_type == entity_type]


def test_satisfies_protocol(detector):
    assert isinstance(detector, Detector)


def test_builtin_types(detector):
    assert detector.entity_types == ("EMAIL", "PHONE", "IPV4")


def test_spans_point_at_the_match(detector):
    text = "Email jan.n@example.com about the invoice."
    [span] = detector.detect(text)
    assert span == Span(6, 23, "jan.n@example.com", "EMAIL", "regex", 0)
    assert text[span.start : span.end] == span.value


class TestEmail:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Email jan.n@example.com about it.", "jan.n@example.com"),
            ("first.last+tag@sub.example.co.uk,", "first.last+tag@sub.example.co.uk"),
            ("(a@example.org)", "a@example.org"),
            ("<JAN@EXAMPLE.COM>", "JAN@EXAMPLE.COM"),
            ("mailto:jan@example.com?subject=hi", "jan@example.com"),
            ("wait...jan@example.com", "jan@example.com"),
            ("user_name%x@my-host.example.net", "user_name%x@my-host.example.net"),
            ("Is it jan@example.com?", "jan@example.com"),
        ],
    )
    def test_matches(self, detector, text, expected):
        assert values(detector, text, "EMAIL") == [expected]

    def test_multiple(self, detector):
        text = "cc a@example.com; b@example.org"
        assert values(detector, text, "EMAIL") == ["a@example.com", "b@example.org"]

    @pytest.mark.parametrize(
        "text",
        [
            "user@localhost",
            "@example.com",
            "a@b.c",
            "jan@@example.com",
            "name@example",
            "jan.@example.com",
            "follow @jan_nowak on social",
            "price is 5 @ 10 each",
            "jan@-example.com",
        ],
    )
    def test_does_not_match(self, detector, text):
        assert values(detector, text, "EMAIL") == []


class TestPhone:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # US / NANP
            ("Call 555-123-4567.", "555-123-4567"),
            ("(555) 123-4567", "(555) 123-4567"),
            ("(555)123-4567", "(555)123-4567"),
            ("555.123.4567", "555.123.4567"),
            ("555 123 4567", "555 123 4567"),
            ("1-555-123-4567", "1-555-123-4567"),
            ("+1 555 123 4567", "+1 555 123 4567"),
            ("+1 (555) 123-4567", "+1 (555) 123-4567"),
            ("+1.555.123.4567", "+1.555.123.4567"),
            ("office: 555-123-4567 ext. 89", "555-123-4567 ext. 89"),
            ("555-123-4567x12", "555-123-4567x12"),
            ("(call me at 555-123-4567)", "555-123-4567"),
            # International (leading +)
            ("+44 20 7946 0958", "+44 20 7946 0958"),
            ("+44 (0) 20 7946 0958", "+44 (0) 20 7946 0958"),
            ("+48 123 456 789", "+48 123 456 789"),
            ("+48-123-456-789", "+48-123-456-789"),
            ("+49 (0)30 1234567", "+49 (0)30 1234567"),
            ("+33 1 23 45 67 89", "+33 1 23 45 67 89"),
            ("+61 2 5550 1234", "+61 2 5550 1234"),
            ("+7 (495) 123-45-67", "+7 (495) 123-45-67"),
            ("+15551234567", "+15551234567"),
            ("Tel +48 123 456 789.", "+48 123 456 789"),
        ],
    )
    def test_matches(self, detector, text, expected):
        assert values(detector, text, "PHONE") == [expected]

    def test_us_and_international_patterns_do_not_duplicate(self, detector):
        assert detector.detect("+1 555 123 4567") == [
            Span(0, 15, "+1 555 123 4567", "PHONE", "regex", 0)
        ]

    def test_extension_variant_overlaps_plain_number(self, detector):
        # Both spans are reported; the masker keeps the longest.
        assert values(detector, "+1 555 123 4567 ext. 89", "PHONE") == [
            "+1 555 123 4567 ext. 89",
            "+1 555 123 4567",
        ]

    def test_trailing_digit_groups_are_trimmed_not_dropped(self, detector):
        # Greedy matching would take 21 digits and reject the whole thing.
        text = "+48 123 456 789 12345 67890"
        assert values(detector, text, "PHONE") == ["+48 123 456 789"]

    def test_two_numbers_in_a_row(self, detector):
        text = "Tel: +48 123 456 789, fax +48 123 456 780"
        assert values(detector, text, "PHONE") == ["+48 123 456 789", "+48 123 456 780"]

    @pytest.mark.parametrize(
        "text",
        [
            "5551234567",  # bare 10-digit run: could be an order number
            "Order 1234567890",
            "555-1234567",
            "555-123-45678",
            "2024-01-15",
            "15.01.2024",
            "123-45-6789",
            "ZIP 90210-1234",
            "4111 1111 1111 1111",
            "ISBN 978-0-306-40615-7",
            "12:30",
            "601 234 567",  # national format without a country code
            "+10%",
            "UTC+05:30",
            "+1-800-FLOWERS",
            "+12345678901234567890",  # too many digits
            "+1234567",  # too few digits
            "3+4=7",
            "Version 10.5.2024.3",
            "tel123-456-7890",
        ],
    )
    def test_does_not_match(self, detector, text):
        assert values(detector, text, "PHONE") == []


class TestIPv4:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Server 192.168.0.1 is down", "192.168.0.1"),
            ("10.0.0.1:8080", "10.0.0.1"),
            ("203.0.113.7/24", "203.0.113.7"),
            ("::ffff:192.0.2.1", "192.0.2.1"),
            ("IP 198.51.100.23.", "198.51.100.23"),
            ("0.0.0.0", "0.0.0.0"),
            ("255.255.255.255", "255.255.255.255"),
            ("(10.1.2.3)", "10.1.2.3"),
        ],
    )
    def test_matches(self, detector, text, expected):
        assert values(detector, text, "IPV4") == [expected]

    @pytest.mark.parametrize(
        "text",
        [
            "1.2.3.4.5",
            "999.1.1.1",
            "256.1.1.1",
            "1.2.3.256",
            "192.168.01.1",  # leading zeros are ambiguous (octal in some parsers)
            "v1.2.3.4",
            "10.0.0.1234",
            "10.0.0",
            "1.2.3.",
            "10..0.0.1",
            "abc10.0.0.1",
        ],
    )
    def test_does_not_match(self, detector, text):
        assert values(detector, text, "IPV4") == []


class TestCustomPatterns:
    def test_string_pattern(self):
        detector = RegexDetector(custom_patterns={"ORDER": r"#\d{5}"})
        assert found(detector, "Order #12345 shipped") == [("ORDER", "#12345")]
        assert detector.entity_types == ("EMAIL", "PHONE", "IPV4", "ORDER")

    def test_compiled_pattern(self):
        detector = RegexDetector(
            custom_patterns={"TICKET": re.compile(r"tkt-\d+", re.IGNORECASE)}
        )
        assert found(detector, "See TKT-42") == [("TICKET", "TKT-42")]

    def test_custom_and_builtin_together(self):
        detector = RegexDetector(custom_patterns={"ORDER": r"#\d{5}"})
        text = "Order #12345 for jan@example.com"
        assert found(detector, text) == [
            ("ORDER", "#12345"),
            ("EMAIL", "jan@example.com"),
        ]

    def test_custom_spans_have_higher_priority(self):
        detector = RegexDetector(custom_patterns={"ORDER": r"#\d{5}"})
        [span] = detector.detect("#12345")
        assert span.priority == RegexDetector.CUSTOM_PRIORITY
        assert span.priority > RegexDetector.BUILTIN_PRIORITY

    def test_same_name_replaces_builtin(self):
        detector = RegexDetector(custom_patterns={"PHONE": r"\d{3} \d{3} \d{3}"})
        assert found(detector, "601 234 567") == [("PHONE", "601 234 567")]
        # The built-in US pattern is gone.
        assert found(detector, "555-123-4567") == []
        assert detector.entity_types == ("EMAIL", "IPV4", "PHONE")

    def test_capture_groups_do_not_change_the_span(self):
        detector = RegexDetector(custom_patterns={"ORDER": r"#(\d{5})"})
        assert found(detector, "#12345") == [("ORDER", "#12345")]

    def test_empty_matches_are_ignored(self):
        detector = RegexDetector(
            custom_patterns={"DIGITS": r"\d*"}, include_builtins=False
        )
        assert found(detector, "ab 12 cd") == [("DIGITS", "12")]

    def test_without_builtins(self):
        detector = RegexDetector(
            custom_patterns={"ORDER": r"#\d{5}"}, include_builtins=False
        )
        assert detector.entity_types == ("ORDER",)
        assert found(detector, "jan@example.com #12345") == [("ORDER", "#12345")]

    def test_overlapping_matches_from_different_rules(self):
        detector = RegexDetector(custom_patterns={"DOMAIN": r"example\.com"})
        assert found(detector, "jan@example.com") == [
            ("EMAIL", "jan@example.com"),
            ("DOMAIN", "example.com"),
        ]

    def test_invalid_regex(self):
        with pytest.raises(ValueError, match="Invalid regex for ORDER"):
            RegexDetector(custom_patterns={"ORDER": r"#(\d{5}"})

    def test_invalid_entity_type(self):
        with pytest.raises(ValueError, match="Invalid entity type"):
            RegexDetector(custom_patterns={"order": r"#\d{5}"})

    def test_wrong_pattern_type(self):
        with pytest.raises(TypeError, match=r"must be a str or re\.Pattern"):
            RegexDetector(custom_patterns={"ORDER": 12345})  # type: ignore[dict-item]

    def test_bytes_pattern_rejected(self):
        with pytest.raises(TypeError, match="not bytes"):
            RegexDetector(custom_patterns={"ORDER": re.compile(rb"#\d{5}")})  # type: ignore[dict-item]


class TestGeneral:
    def test_empty_string(self, detector):
        assert detector.detect("") == []

    def test_text_without_pii(self, detector):
        text = "The quarterly report is due on 2024-03-31, version 2.0, see page 12."
        assert detector.detect(text) == []

    def test_results_sorted_by_position(self, detector):
        text = "10.0.0.1 then 555-123-4567 then jan@example.com"
        spans = detector.detect(text)
        assert [s.entity_type for s in spans] == ["IPV4", "PHONE", "EMAIL"]
        assert spans == sorted(spans, key=lambda s: s.start)

    def test_unicode_text_offsets(self, detector):
        text = "Zażółć gęślą jaźń: jan@example.com"
        [span] = detector.detect(text)
        assert text[span.start : span.end] == "jan@example.com"

    def test_repr(self, detector):
        assert (
            repr(detector) == "RegexDetector(entity_types=('EMAIL', 'PHONE', 'IPV4'))"
        )
