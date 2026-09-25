import re
import time

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
            ("łucja.wiśniewska@example.com", "łucja.wiśniewska@example.com"),
            ("Contact jan.o'neil@example.com", "jan.o'neil@example.com"),
            ("cc jan&anna@example.com", "jan&anna@example.com"),
            ("billing/ops@example.com", "billing/ops@example.com"),
            (
                "Return-Path: <list-bounces+jan.nowak=example.com@lists.example.org>",
                "list-bounces+jan.nowak=example.com@lists.example.org",
            ),
            ("quoted 'jan@example.com'", "jan@example.com"),
            ("code `jan@example.com`", "jan@example.com"),
            ('{"email": "jan@example.com"}', "jan@example.com"),
            ("email='jan@example.com'", "jan@example.com"),
            ("[Jan](mailto:jan@example.com)", "jan@example.com"),
            ("Write to user@example.xn--p1ai", "user@example.xn--p1ai"),
            ("user@xn--mnchen-3ya.de", "user@xn--mnchen-3ya.de"),
            ("連絡先はjan@example.comです", "jan@example.com"),
            ("邮箱是jan@example.com", "jan@example.com"),
        ],
    )
    def test_matches(self, detector, text, expected):
        assert values(detector, text, "EMAIL") == [expected]

    def test_key_value_prefix_is_masked_with_the_address(self, detector):
        # "=" can be part of a local part (VERP bounce addresses), so a key
        # glued to the address is masked along with it. Over-masking is safe;
        # splitting the address would leak part of it.
        assert values(detector, "ADMIN_EMAIL=jan@example.com", "EMAIL") == [
            "ADMIN_EMAIL=jan@example.com"
        ]
        # Quoting or spacing keeps the key out of the match, as the README says.
        assert values(detector, 'ADMIN_EMAIL="jan@example.com"', "EMAIL") == [
            "jan@example.com"
        ]
        assert values(detector, "ADMIN_EMAIL= jan@example.com", "EMAIL") == [
            "jan@example.com"
        ]

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
            ("Tel. (+48) 123 456 789", "(+48) 123 456 789"),
            ("(+44) 20 7946 0958", "(+44) 20 7946 0958"),
            ("(+48 123 456 789)", "+48 123 456 789"),
            ("+44 20 7946 0958x12", "+44 20 7946 0958x12"),
            ("+49 30 1234 5678 ext. 9", "+49 30 1234 5678 ext. 9"),
            # No spaces around numbers in CJK text.
            ("電話は555-123-4567です", "555-123-4567"),
            ("電話番号は+81 3-1234-5678です。", "+81 3-1234-5678"),
            ("请拨打+86 10 5555 0100联系", "+86 10 5555 0100"),
            ("전화번호는+82 2-123-4567입니다", "+82 2-123-4567"),
            ("_555-123-4567_", "555-123-4567"),  # Markdown emphasis
            ("５５５-１２３-４５６７", "５５５-１２３-４５６７"),  # noqa: RUF001 (fullwidth)
        ],
    )
    def test_matches(self, detector, text, expected):
        assert values(detector, text, "PHONE") == [expected]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "Phones: +44 20 7946 0958 555-123-4567",
                ["+44 20 7946 0958", "555-123-4567"],
            ),
            ("Tel +1 555 123 4567 555-765-4321", ["+1 555 123 4567", "555-765-4321"]),
            (
                "Phones: +48 123 456 789 555-123-4567 ext. 89",
                ["+48 123 456 789", "555-123-4567 ext. 89"],
            ),
        ],
    )
    def test_greedy_number_stops_before_the_next_one(self, detector, text, expected):
        assert values(detector, text, "PHONE") == expected

    def test_parenthesized_country_code_contains_the_us_match(self, detector):
        # Both spans are reported; the masker keeps the longer one.
        assert values(detector, "(+1) 555-123-4567", "PHONE") == [
            "(+1) 555-123-4567",
            "555-123-4567",
        ]

    def test_greedy_number_stops_before_an_ip(self, detector):
        assert found(detector, "Caller +44 20 7946 0958 192.0.2.1") == [
            ("PHONE", "+44 20 7946 0958"),
            ("IPV4", "192.0.2.1"),
        ]

    @pytest.mark.parametrize(
        "text", ["+44 20 7946 0958abc", "+49 30 1234 5678xyz", "+44 20 7946 0958x"]
    )
    def test_glued_international_number_is_not_split(self, detector, text):
        # Backing off to a shorter prefix would leave the last group unmasked.
        assert values(detector, text, "PHONE") == []

    def test_us_and_international_patterns_do_not_duplicate(self, detector):
        assert detector.detect("+1 555 123 4567") == [
            Span(0, 15, "+1 555 123 4567", "PHONE", "regex", 0)
        ]

    def test_extension_is_part_of_both_phone_patterns(self, detector):
        assert values(detector, "+1 555 123 4567 ext. 89", "PHONE") == [
            "+1 555 123 4567 ext. 89"
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
            ("服务器地址是192.0.2.1。", "192.0.2.1"),
            ("サーバー192.0.2.1に接続", "192.0.2.1"),
            ("__192.0.2.1__", "192.0.2.1"),
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


class TestPerformance:
    """Inputs that used to trigger quadratic or catastrophic backtracking."""

    @pytest.mark.parametrize(
        "text",
        [
            "a." * 25_000,
            "a-" * 25_000,
            "a'" * 25_000,
            "a=" * 25_000,
            "a" * 50_000,
            "a@" * 25_000,
            "1." * 25_000,
            ("+" + "1" * 30 + "a ") * 640,
            "+1 " + "1 " * 25_000,
        ],
        ids=[
            "dots",
            "dashes",
            "quotes",
            "equals",
            "letters",
            "ats",
            "digits",
            "plus",
            "groups",
        ],
    )
    def test_linear_time(self, detector, text):
        start = time.perf_counter()
        detector.detect(text)
        assert time.perf_counter() - start < 1.0
