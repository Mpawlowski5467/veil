import re
import time
import unicodedata

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
            ("連絡先: jan@example.com", "jan@example.com"),
            ("Write to 张伟@example.com today", "张伟@example.com"),
            ("連絡先: 山田太郎@example.co.jp", "山田太郎@example.co.jp"),
            ("mail: 김민수@example.kr", "김민수@example.kr"),
            ("mail: 山田.taro@example.com", "山田.taro@example.com"),
            ("mail: sean.o\u2019brien@example.com", "sean.o\u2019brien@example.com"),
            ("jan!x@example.com", "jan!x@example.com"),
            ("Write to राम@example.com", "राम@example.com"),
            ("mail: राम.शर्मा@example.in", "राम.शर्मा@example.in"),
        ],
    )
    def test_matches(self, detector, text, expected):
        assert values(detector, text, "EMAIL") == [expected]

    @pytest.mark.parametrize(
        "address",
        ["renée.dupont@example.fr", "josé.garcia@example.com", "mü@example.de"],
    )
    def test_decomposed_accents(self, detector, address):
        text = "Write to " + unicodedata.normalize("NFD", address) + " today"
        assert values(detector, text, "EMAIL") == [
            unicodedata.normalize("NFD", address)
        ]

    def test_cjk_written_right_before_an_address_is_masked_with_it(self, detector):
        # CJK may be part of a local part, and there is no space to tell where
        # the address starts. Over-masking is safe; guessing wrong would leak.
        assert values(detector, "連絡先はjan@example.comです", "EMAIL") == [
            "連絡先はjan@example.com"
        ]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "https://example.com/share?to=jan@example.com&cc=anna@example.com",
                ["to=jan@example.com", "cc=anna@example.com"],
            ),
            (
                "Contacts: jan@example.com/anna@example.com",
                ["jan@example.com", "anna@example.com"],
            ),
            ("a@example.com.b@example.com", ["a@example.com", "b@example.com"]),
        ],
    )
    def test_address_right_after_another(self, detector, text, expected):
        assert values(detector, text, "EMAIL") == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("note*jan@example.com", "jan@example.com"),
            ("555-123-4567 ext. 89*a/b@example.com", "a/b@example.com"),
            ("José Núñez*zoë@example.com", "zoë@example.com"),
        ],
    )
    def test_markdown_star_is_not_part_of_an_address(self, detector, text, expected):
        assert values(detector, text, "EMAIL") == [expected]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "请发邮件到jan@example.com或anna@example.com",
                ["请发邮件到jan@example.com", "anna@example.com"],
            ),
            (
                "请发送至zhang.wei@example.com或li.na@example.cn",
                ["请发送至zhang.wei@example.com", "li.na@example.cn"],
            ),
            (
                "ติดต่อ jan@example.comหรือanna@example.com",
                ["jan@example.com", "anna@example.com"],
            ),
            # Glued with ASCII: the second address absorbs the glue.
            (
                "GET /search?q=contact+jan@example.com+or+anna@example.com HTTP/1.1",
                ["q=contact+jan@example.com", "+or+anna@example.com"],
            ),
            (
                "q=jan@example.com%20anna@example.com",
                ["q=jan@example.com", "%20anna@example.com"],
            ),
        ],
    )
    def test_address_glued_to_the_previous_one(self, detector, text, expected):
        assert values(detector, text, "EMAIL") == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "zoë@example.net会议定于下午三点举行山田太郎@a1.example.dev",
                ["zoë@example.net", "会议定于下午三点举行山田太郎@a1.example.dev"],
            ),
            (
                "ivanov@example.org連絡先は张伟@lists.example.org連絡先は",
                ["ivanov@example.org", "連絡先は张伟@lists.example.org"],
            ),
        ],
    )
    def test_cjk_address_glued_to_the_previous_one(self, detector, text, expected):
        # No start position inside the CJK run is allowed, so the address is
        # continued from the end of the previous one (over-masking the prose).
        assert values(detector, text, "EMAIL") == expected

    def test_long_chain_of_glued_addresses_is_linear(self, detector):
        text = "a@b.co" + "+a@b.co" * 5000
        start = time.perf_counter()
        assert len(detector.detect(text)) == 5001
        assert time.perf_counter() - start < 1.0

    @pytest.mark.parametrize("local", ["jan", "jan.nowak"])
    def test_address_after_a_long_run_of_thai(self, detector, local):
        # Too long for the "@ within reach" filter from the start of the run.
        thai = "ผ่านทางอีเมล" * 15
        assert values(detector, thai + local + "@example.com", "EMAIL") == [
            local + "@example.com"
        ]

    @pytest.mark.parametrize(
        "address", ["USER@EXAMPLE.XN--P1AI", "ivan@shop.XN--P1AI", "a@b.Xn--P1ai"]
    )
    def test_upper_case_punycode_tld(self, detector, address):
        assert values(detector, f"Mail: {address} today", "EMAIL") == [address]

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

    @pytest.mark.parametrize("sep", [" ", "\n", "\t"])
    def test_ip_then_us_number_keeps_the_ip_whole(self, detector, sep):
        # The optional "1" country code must not take the IP's last octet.
        assert found(detector, f"192.0.2.1{sep}555.123.4567") == [
            ("IPV4", "192.0.2.1"),
            ("PHONE", "555.123.4567"),
        ]

    def test_greedy_number_stops_before_an_ip(self, detector):
        assert found(detector, "Caller +44 20 7946 0958 192.0.2.1") == [
            ("PHONE", "+44 20 7946 0958"),
            ("IPV4", "192.0.2.1"),
        ]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Call +44 20 7946 0958 24h a day", "+44 20 7946 0958"),
            ("Tel. +33 1 23 45 67 89 7j/7", "+33 1 23 45 67 89"),
            ("Office +44 20 7946 0958 9am-5pm", "+44 20 7946 0958"),
            ("Reception +48 22 123 45 67 2nd floor", "+48 22 123 45 67"),
            ("caller=+44 20 7946 0958 2024-01-15T10:00:00Z", "+44 20 7946 0958"),
            ("+49 30 1234 5678 2024-01-15 later", "+49 30 1234 5678"),
        ],
    )
    def test_number_followed_by_a_token_starting_with_digits(
        self, detector, text, expected
    ):
        # The greedy capture runs into "24h", "9am", a date...; backing off to
        # the last group boundary keeps the real number.
        assert values(detector, text, "PHONE") == [expected]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Caller +33 1 23 45 67 89 192.0.2.1", ["+33 1 23 45 67 89", "192.0.2.1"]),
            (
                "+48 123 456 789 198.51.100.23 192.0.2.7",
                ["+48 123 456 789", "198.51.100.23", "192.0.2.7"],
            ),
            (
                "+44 20 7946 0958 (555) 123-4567 555-765-4321",
                ["+44 20 7946 0958", "(555) 123-4567", "555-765-4321"],
            ),
            (
                "+33 1 23 45 67 89 1-555-123-4567",
                ["+33 1 23 45 67 89", "1-555-123-4567"],
            ),
        ],
    )
    def test_number_followed_by_ips_and_numbers(self, detector, text, expected):
        assert [s.value for s in detector.detect(text)] == expected

    @pytest.mark.parametrize(
        "text", ["Tel(+48) 123 456 789", "Phone(+44) 20 7946 0958"]
    )
    def test_parenthesized_country_code_after_a_label(self, detector, text):
        assert values(detector, text, "PHONE") == [text[text.index("(") :]]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # A long group glued to letters is part of the number: mask it all.
            ("+44 20 7946 0958abc", "+44 20 7946 0958"),
            ("Phone: +44-20-7946-0958Fax", "+44-20-7946-0958"),
            # Unsure whether "100" belongs to the number: over-mask it.
            ("+44 20 7946 0958 100km away", "+44 20 7946 0958 100"),
        ],
    )
    def test_number_glued_to_letters_is_still_masked(self, detector, text, expected):
        assert values(detector, text, "PHONE") == [expected]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # The "(0)" trunk prefix doesn't count, so the extension fits.
            ("Tel.: +49 (0)711 1234567-890", "+49 (0)711 1234567-890"),
            ("Tel.: +43 (0)662 123456-7890", "+43 (0)662 123456-7890"),
            # Over 15 digits with the extension: mask the base number.
            ("Tel.: +49 89 12345678-1234", "+49 89 12345678"),
            ("Tel. +44 20 7946 0958-0959", "+44 20 7946 0958"),
            # A date right after a number: its first group is over-masked.
            ("+48 123 456 789 2024-01-15", "+48 123 456 789 2024"),
        ],
    )
    def test_too_many_digits_keeps_the_longest_plausible_number(
        self, detector, text, expected
    ):
        assert values(detector, text, "PHONE") == [expected]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Call (555)\u00a0123-4567 today", "(555)\u00a0123-4567"),
            ("Call 555\u00a0123\u00a04567 today", "555\u00a0123\u00a04567"),
            (
                "T\u00e9l. : +33\u00a01\u00a023\u00a045\u00a067\u00a089",
                "+33\u00a01\u00a023\u00a045\u00a067\u00a089",
            ),
            (
                "Phone +44\u202f20\u202f7946\u202f0958",
                "+44\u202f20\u202f7946\u202f0958",
            ),
            ("555\u2013123\u20134567", "555\u2013123\u20134567"),
        ],
    )
    def test_no_break_spaces_and_dashes(self, detector, text, expected):
        assert values(detector, text, "PHONE") == [expected]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Call tel.1-800-555-0199 today", "1-800-555-0199"),
            ("Mob.1 (305) 555-0123", "1 (305) 555-0123"),
            ("Tel(212) 555-0142", "(212) 555-0142"),
        ],
    )
    def test_us_number_after_an_abbreviation_or_label(self, detector, text, expected):
        assert values(detector, text, "PHONE") == [expected]

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

    def test_shrink_only_at_group_boundaries(self):
        # A custom span starting mid-group must not strand the digits before
        # it; the phone keeps its full length and the masker reports the
        # overlap instead.
        detector = RegexDetector(custom_patterns={"CODE": r"\d{2}-[A-Z]{2}"})
        assert values(detector, "Call +44 20 7946 0958-AB today", "PHONE") == [
            "+44 20 7946 0958"
        ]


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

    def test_shrink_pass_with_a_busy_custom_pattern(self):
        detector = RegexDetector(custom_patterns={"NUM": r"\d{2}"})
        start = time.perf_counter()
        detector.detect(("+12345678 " * 5000)[:50_000])
        assert time.perf_counter() - start < 1.0
