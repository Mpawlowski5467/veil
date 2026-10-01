import re
import time
import unicodedata

import pytest

from veil.detectors import Detector, RegexDetector
from veil.masker import resolve_overlaps
from veil.types import Span

# Spaces used to group numbers in print: plain, no-break (HTML), narrow
# no-break and thin (French typography), and ideographic (Japanese).
SPACES = [
    " ",
    "\N{NO-BREAK SPACE}",
    "\N{NARROW NO-BREAK SPACE}",
    "\N{THIN SPACE}",
    "\N{IDEOGRAPHIC SPACE}",
]


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
    assert detector.entity_types == (
        "EMAIL",
        "PHONE",
        "IPV4",
        "IPV6",
        "CREDIT_CARD",
        "IBAN",
        "SSN",
        "API_KEY",
        "TOKEN",
        "PASSWORD",
        "PRIVATE_KEY",
        "CREDENTIAL",
    )


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

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("ssh 198.51.100.123 2222", [("IPV4", "198.51.100.123")]),
            ("seen 203.0.113.187 2024 times", [("IPV4", "203.0.113.187")]),
            ("ssh 198.51.250.123 2222", [("IPV4", "198.51.250.123")]),
            (
                "hosts 10.0.0.1 and 198.51.100.123 2222",
                [("IPV4", "10.0.0.1"), ("IPV4", "198.51.100.123")],
            ),
            (
                "ssh 198.51.100.123 2222 then 10.0.0.1",
                [("IPV4", "198.51.100.123"), ("IPV4", "10.0.0.1")],
            ),
            ("IP 198.51.100.123.2222 > 203.0.113.5.443: Flags [S]", []),  # tcpdump
            ("peer 010.000.100.123 2222", []),  # zero-padded
            ("host 10.200.100.150.1234", []),
            ("198.51.100.123 443 1024 bytes", [("IPV4", "198.51.100.123")]),
            ("conn 10.0.0.200 443 1500", [("IPV4", "10.0.0.200")]),
            (
                "::ffff:198.51.100.123 2222",
                [("IPV6", "::ffff:198.51.100.123"), ("IPV4", "198.51.100.123")],
            ),
            (
                "2001:db8::1 555-123-4567",
                [("IPV6", "2001:db8::1"), ("PHONE", "555-123-4567")],
            ),
            ("fe80::1 555 123 4567", [("IPV6", "fe80::1"), ("PHONE", "555 123 4567")]),
            (
                "2001:db8:0:0:0:0:3:1 555 123 4567",
                [("IPV6", "2001:db8:0:0:0:0:3:1"), ("PHONE", "555 123 4567")],
            ),
            (
                "fe80:0:0:0:0:0:abcd:1 555-123-4567",
                [("IPV6", "fe80:0:0:0:0:0:abcd:1"), ("PHONE", "555-123-4567")],
            ),
            (
                "10.0.0.1 212 200 0123",
                [("IPV4", "10.0.0.1"), ("PHONE", "212 200 0123")],
            ),
        ],
    )
    def test_end_of_an_ip_address_is_not_a_phone_number(self, detector, text, expected):
        # "100.123 2222" and "123 443 1024" look like numbers, but they start
        # inside an IPv4 address; and "::1" is not a "1" country code.
        assert found(detector, text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Tel:555-555-0123", "555-555-0123"),
            ("Tel:1-555-555-0123", "1-555-555-0123"),
            ("Phone:1 (415) 555-0194", "1 (415) 555-0194"),
            ("ID:1 555-555-0123", "1 555-555-0123"),
            ("total 1.00.555-555-0123", "555-555-0123"),
            ("0123.555-555-0123", "555-555-0123"),
            ("id 12.555.555.0123", "555.555.0123"),  # 555 is not an octet
            ("Call 1.212.200.0123", "1.212.200.0123"),
            ("10.0.0.1.555.555.0123", "555.555.0123"),
            ("10.0.0.1.212-200-0123", "212-200-0123"),
            ("1.1.212.200.0123", "212.200.0123"),
            ("Note 60.61.415.200 0111", "415.200 0111"),  # 415 is not an octet
            # Area code and exchange that could be octets (100-255):
            ("Contacts: 2.212.225.0199", "212.225.0199"),
            ("dial 001.212.225.0199", "212.225.0199"),
            ("Ref 2024-01-15.212.200.0123", "212.200.0123"),
            ("Office 3.250.234-0131", "250.234-0131"),
        ],
    )
    def test_numbers_after_digits_and_dots_are_still_found(
        self, detector, text, expected
    ):
        assert values(detector, text, "PHONE") == [expected]

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


class TestIPv6:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Server 2001:db8::1 is up", "2001:db8::1"),
            ("fe80::1ff:fe23:4567:890a", "fe80::1ff:fe23:4567:890a"),
            ("fe80::1", "fe80::1"),
            (
                "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
                "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
            ),
            ("2001:DB8::ABCD:1", "2001:DB8::ABCD:1"),
            ("http://[2001:db8::1]:8080/", "2001:db8::1"),
            ("Blocked 2001:db8::1.", "2001:db8::1"),
            ("::ffff:192.0.2.1", "::ffff:192.0.2.1"),
            ("2001:db8::", "2001:db8::"),
            ("服务器2001:db8::1已上线", "2001:db8::1"),
        ],
    )
    def test_matches(self, detector, text, expected):
        assert values(detector, text, "IPV6") == [expected]

    @pytest.mark.parametrize(
        "text",
        [
            "::1",  # loopback, not personal
            "12:30:45",
            "std::vector<int>",
            "a[1::2]",
            "x[100::2]",
            "Foo::bar()",
            "00:1A:2B:3C:4D:5E",  # MAC address
            "cafe::babe",
            "2001:db8::1g",
            "1:2:3:4:5:6:7:8:9:10",
            "Genesis 1:2:3",
        ],
    )
    def test_does_not_match(self, detector, text):
        assert values(detector, text, "IPV6") == []

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Straight after a label and a colon.
            ("Received: from mx (unknown [IPv6:2001:db8::1])", "2001:db8::1"),
            ("EHLO [IPv6:2001:db8::1]", "2001:db8::1"),
            ("ip:2001:db8::1", "2001:db8::1"),
            ("X-Real-IP:2001:db8::1", "2001:db8::1"),
            ("id:2001:db8::1", "2001:db8::1"),
            # Followed by a port or punctuation.
            ("[client 2001:db8::1:54321] AH01630", "2001:db8::1"),
            (
                "connect to 2001:db8:85a3:0:0:8a2e:370:7334:443 failed",
                "2001:db8:85a3:0:0:8a2e:370:7334",
            ),
            ("Client 2001:db8:1::: denied", "2001:db8:1::"),
            ("Blocked 2001:db8::1.Next", "2001:db8::1"),
        ],
    )
    def test_labels_ports_and_punctuation(self, detector, text, expected):
        assert values(detector, text, "IPV6") == [expected]

    def test_tcpdump_dotted_ports(self, detector):
        text = "IP6 2001:db8::1.54321 > 2001:db8::2.443: Flags [S]"
        assert values(detector, text, "IPV6") == ["2001:db8::1", "2001:db8::2"]

    def test_ipv4_mapped_address_is_masked_whole(self, detector):
        spans = resolve_overlaps(detector.detect("from ::ffff:192.0.2.1"))
        assert [(s.entity_type, s.value) for s in spans] == [
            ("IPV6", "::ffff:192.0.2.1")
        ]


class TestCreditCard:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Networks' published test numbers.
            ("Card 4111 1111 1111 1111 exp 12/29", "4111 1111 1111 1111"),
            ("4111-1111-1111-1111", "4111-1111-1111-1111"),
            ("4111111111111111", "4111111111111111"),
            ("4222222222222", "4222222222222"),  # 13-digit Visa
            ("5555 5555 5555 4444", "5555 5555 5555 4444"),
            ("2223003122003222", "2223003122003222"),  # Mastercard 2-series
            ("Amex 3782 822463 10005", "3782 822463 10005"),
            ("6011111111111117", "6011111111111117"),  # Discover
            ("3530111333300000", "3530111333300000"),  # JCB
            ("30569309025904", "30569309025904"),  # Diners Club
            ("6200000000000005", "6200000000000005"),  # UnionPay
            ("卡号4111111111111111。", "4111111111111111"),
            ("5555 5555 5555 4444 1234", "5555 5555 5555 4444"),
        ],
    )
    def test_matches(self, detector, text, expected):
        assert values(detector, text, "CREDIT_CARD") == [expected]

    @pytest.mark.parametrize(
        "text",
        [
            "4111 1111 1111 1112",  # Luhn check fails
            "1234 5678 9012 3456",
            "9780306406157",  # ISBN-13
            "1700000000000",  # millisecond timestamp
            "41111111111111111111",  # 20 digits
            "3782 8224 6310 0051 2",  # Amex prefix, wrong length
            "DE89 3704 0044 0532 0130 01",  # an invalid IBAN's digits
            "order 4111111111111111x",
            "+1 555 123 4567 8900",
            "555 123 4567 555-765",  # mixed separators, passes Luhn by chance
            "5551 2345 6755 57 65",  # not a printed card layout
        ],
    )
    def test_does_not_match(self, detector, text):
        assert values(detector, text, "CREDIT_CARD") == []


class TestCreditCardSeparatorsAndDecimals:
    @pytest.mark.parametrize("space", SPACES)
    def test_grouped_with_other_spaces(self, detector, space):
        card = space.join(["4111", "1111", "1111", "1111"])
        assert values(detector, f"Card {card} exp 12/29", "CREDIT_CARD") == [card]

    @pytest.mark.parametrize(
        "text",
        [
            "print(6/11)  # 0.5454545454545454",
            '{"score": 0.3888888888888889, "loss": 0.35714285714285715}',
            "epoch 3 acc=0.5240722607865779",
            "x = 1,5454545454545454",
        ],
    )
    def test_decimal_digits_are_not_a_card(self, detector, text):
        assert values(detector, text, "CREDIT_CARD") == []


class TestIBAN:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Published example IBANs.
            ("IBAN DE89 3704 0044 0532 0130 00", "DE89 3704 0044 0532 0130 00"),
            ("DE89370400440532013000", "DE89370400440532013000"),
            ("GB82 WEST 1234 5698 7654 32", "GB82 WEST 1234 5698 7654 32"),
            ("FR14 2004 1010 0505 0001 3M02 606", "FR14 2004 1010 0505 0001 3M02 606"),
            (
                "PL61 1090 1014 0000 0712 1981 2874",
                "PL61 1090 1014 0000 0712 1981 2874",
            ),
            ("nl91 abna 0417 1643 00", "nl91 abna 0417 1643 00"),
            (
                "Pay DE89 3704 0044 0532 0130 00 please, thanks",
                "DE89 3704 0044 0532 0130 00",
            ),
            ("账户：DE89370400440532013000。", "DE89370400440532013000"),  # noqa: RUF001
        ],
    )
    def test_matches(self, detector, text, expected):
        assert values(detector, text, "IBAN") == [expected]

    @pytest.mark.parametrize(
        "text",
        [
            "DE89 3704 0044 0532 0130 01",  # checksum fails
            "AB12 3456 7890 1234 5678",
            "Q3 2024 revenue report for the team",
            "DE00 3704 0044 0532 0130 00",  # check digits out of range
            "DE89 3704",  # too short
            "xDE89370400440532013000",
        ],
    )
    def test_does_not_match(self, detector, text):
        assert values(detector, text, "IBAN") == []

    @pytest.mark.parametrize(
        "text",
        [
            "Flight BA115 departs from gate B12 at noon, boarding starts soon.",
            "Our IP67 targets include higher margins and lower churn",
            "fixed in cc47126 and cc4712638949",
            "00000020: 8e9f 5fcc fd31 80b8 1a6c 92b7 658d 3b3d",
            "md5 ed441bba3002e7c19815499c5ae1bf7a file.txt",
        ],
    )
    def test_common_text_is_not_an_iban(self, detector, text):
        assert values(detector, text, "IBAN") == []

    @pytest.mark.parametrize("space", SPACES)
    def test_grouped_with_other_spaces(self, detector, space):
        iban = space.join(["DE89", "3704", "0044", "0532", "0130", "00"])
        assert values(detector, f"IBAN {iban} bitte", "IBAN") == [iban]

    def test_shortest_iban(self, detector):
        assert values(detector, "NO93 8601 1117 947", "IBAN") == ["NO93 8601 1117 947"]

    def test_card_inside_a_valid_iban_loses_the_overlap(self, detector):
        spans = resolve_overlaps(detector.detect("DE89 3704 0044 0532 0130 00"))
        assert [s.entity_type for s in spans] == ["IBAN"]


class TestCustomPatterns:
    def test_string_pattern(self):
        detector = RegexDetector(custom_patterns={"ORDER": r"#\d{5}"})
        assert found(detector, "Order #12345 shipped") == [("ORDER", "#12345")]
        assert detector.entity_types == (
            "EMAIL",
            "PHONE",
            "IPV4",
            "IPV6",
            "CREDIT_CARD",
            "IBAN",
            "SSN",
            "ORDER",
            "API_KEY",
            "TOKEN",
            "PASSWORD",
            "PRIVATE_KEY",
            "CREDENTIAL",
        )

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
        assert detector.entity_types == (
            "EMAIL",
            "IPV4",
            "IPV6",
            "CREDIT_CARD",
            "IBAN",
            "SSN",
            "PHONE",
            "API_KEY",
            "TOKEN",
            "PASSWORD",
            "PRIVATE_KEY",
            "CREDENTIAL",
        )

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
        assert repr(detector) == (
            "RegexDetector(entity_types=('EMAIL', 'PHONE', 'IPV4', 'IPV6', "
            "'CREDIT_CARD', 'IBAN', 'SSN', 'API_KEY', 'TOKEN', 'PASSWORD', "
            "'PRIVATE_KEY', 'CREDENTIAL'))"
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
            "a/" * 25_000,
            "a!" * 25_000,
            ("+12345678 " * 5000)[:50_000],
            "".join(f"+4420{i:06d}\n" for i in range(4200))[:50_000],
            "1:" * 25_000,
            "abcd:" * 10_000,
            "4" * 50_000,
            "4111 " * 10_000,
            "DE89 " * 10_000,
            "DE89" + "A" * 50_000,
            "[a " * 16_000,
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
            "slashes",
            "bangs",
            "short-phones",
            "phone-list",
            "colons",
            "hex-groups",
            "long-digits",
            "card-groups",
            "iban-groups",
            "iban-letters",
            "brackets",
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
