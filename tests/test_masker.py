import time

import pytest

from veil.detectors import ManualDetector, RegexDetector
from veil.masker import Masker, resolve_overlaps
from veil.types import MaskedEntity, Span
from veil.vault import MemoryVault


class FixedDetector:
    """Returns the same spans for any text; lets tests build exact overlaps."""

    def __init__(self, *spans):
        self.spans = list(spans)

    def detect(self, text):
        return list(self.spans)


def span(text, value, entity_type, *, source="regex", priority=0, occurrence=0):
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(value, start + 1)
    return Span(start, start + len(value), value, entity_type, source, priority)


@pytest.fixture
def vault():
    return MemoryVault()


@pytest.fixture
def manual():
    return ManualDetector()


@pytest.fixture
def masker(manual, vault):
    return Masker([manual, RegexDetector(custom_patterns={"ORDER": r"#\d{5}"})], vault)


class TestResolveOverlaps:
    def test_no_spans(self):
        assert resolve_overlaps([]) == []

    def test_disjoint_spans_all_kept_in_order(self):
        a = Span(10, 15, "bbbbb", "X")
        b = Span(0, 3, "aaa", "X")
        assert resolve_overlaps([a, b]) == [b, a]

    def test_adjacent_spans_do_not_overlap(self):
        a = Span(0, 3, "aaa", "X")
        b = Span(3, 6, "bbb", "X")
        assert resolve_overlaps([a, b]) == [a, b]

    def test_longest_wins(self):
        long = Span(0, 10, "a" * 10, "LONG")
        short = Span(5, 8, "aaa", "SHORT")
        assert resolve_overlaps([short, long]) == [long]

    def test_longest_wins_even_with_lower_priority(self):
        long = Span(0, 10, "a" * 10, "LONG", priority=0)
        short = Span(0, 5, "aaaaa", "SHORT", priority=100)
        assert resolve_overlaps([short, long]) == [long]

    def test_tie_prefers_higher_priority(self):
        regex = Span(0, 5, "aaaaa", "REGEX", "regex", 0)
        manual = Span(0, 5, "aaaaa", "MANUAL", "manual", 100)
        assert resolve_overlaps([regex, manual]) == [manual]
        assert resolve_overlaps([manual, regex]) == [manual]

    def test_tie_with_partial_overlap_prefers_priority(self):
        regex = Span(0, 5, "aaaaa", "REGEX", priority=0)
        manual = Span(3, 8, "aaaaa", "MANUAL", priority=100)
        assert resolve_overlaps([regex, manual]) == [manual]

    def test_full_tie_prefers_earlier_start(self):
        first = Span(0, 5, "aaaaa", "X")
        second = Span(3, 8, "aaaaa", "X")
        assert resolve_overlaps([second, first]) == [first]

    def test_chain_of_overlaps(self):
        # B is longest; A and C each overlap only B, so both lose.
        a = Span(0, 4, "aaaa", "A")
        b = Span(3, 9, "bbbbbb", "B")
        c = Span(8, 12, "cccc", "C")
        assert resolve_overlaps([a, b, c]) == [b]

    def test_losers_do_not_block_others(self):
        # A (longest) beats B; C only overlaps B, so C survives.
        a = Span(0, 6, "aaaaaa", "A")
        b = Span(5, 10, "bbbbb", "B")
        c = Span(9, 12, "ccc", "C")
        assert resolve_overlaps([c, b, a]) == [a, c]

    def test_identical_spans_collapse(self):
        a = Span(0, 3, "aaa", "X")
        assert resolve_overlaps([a, a]) == [a]


class TestMask:
    def test_readme_example(self, masker, manual):
        manual.add("Jan Nowak", "PERSON")
        result = masker.mask("Email Jan Nowak at jan.n@example.com about the invoice.")
        assert result.text == "Email [PERSON_1] at [EMAIL_1] about the invoice."
        assert result.warnings == []
        assert result.entities == [
            MaskedEntity("[PERSON_1]", "Jan Nowak", "PERSON", 6, 15, "manual"),
            MaskedEntity("[EMAIL_1]", "jan.n@example.com", "EMAIL", 19, 36, "regex"),
        ]

    def test_empty_string(self, masker):
        result = masker.mask("")
        assert result.text == ""
        assert result.entities == []
        assert result.warnings == []

    def test_text_without_pii_is_unchanged(self, masker, vault):
        text = "Please summarize the attached quarterly report in three bullets."
        result = masker.mask(text)
        assert result.text == text
        assert result.entities == []
        assert result.warnings == []
        assert len(vault) == 0

    def test_whole_text_is_pii(self, masker):
        assert masker.mask("jan@example.com").text == "[EMAIL_1]"

    def test_all_builtin_types(self, masker):
        text = "Mail a@example.com, call 555-123-4567, server 192.0.2.10."
        assert (
            masker.mask(text).text == "Mail [EMAIL_1], call [PHONE_1], server [IPV4_1]."
        )

    def test_custom_pattern(self, masker):
        assert masker.mask("Order #12345 shipped").text == "Order [ORDER_1] shipped"

    def test_numbering_follows_reading_order(self, masker):
        text = "b@example.com, then a@example.com"
        result = masker.mask(text)
        assert result.text == "[EMAIL_1], then [EMAIL_2]"

    def test_repeated_value_reuses_placeholder(self, masker):
        text = "a@example.com wrote to b@example.com and a@example.com again"
        result = masker.mask(text)
        assert result.text == "[EMAIL_1] wrote to [EMAIL_2] and [EMAIL_1] again"
        assert [e.placeholder for e in result.entities] == [
            "[EMAIL_1]",
            "[EMAIL_2]",
            "[EMAIL_1]",
        ]

    def test_consistent_across_calls(self, masker):
        first = masker.mask("Contact a@example.com")
        second = masker.mask("Also b@example.com, and again a@example.com")
        assert first.text == "Contact [EMAIL_1]"
        assert second.text == "Also [EMAIL_2], and again [EMAIL_1]"

    def test_entities_offsets_refer_to_original_text(self, masker):
        text = "x 555-123-4567 y a@example.com"
        result = masker.mask(text)
        for entity in result.entities:
            assert text[entity.start : entity.end] == entity.value

    def test_many_spans_replaced_back_to_front(self, masker):
        emails = [f"user{i}@example.com" for i in range(15)]
        text = " | ".join(emails)
        result = masker.mask(text)
        assert result.text == " | ".join(f"[EMAIL_{i}]" for i in range(1, 16))

    def test_unicode_around_spans(self, masker, manual):
        manual.add("Łucja", "PERSON")
        result = masker.mask("Cześć Łucja — napisz na łucja@example.com 🙂")
        assert result.text == "Cześć [PERSON_1] — napisz na [EMAIL_1] 🙂"

    def test_first_type_wins_and_entity_reports_it(self, vault):
        orders = FixedDetector(Span(0, 5, "12345", "ORDER"))
        zips = FixedDetector(Span(0, 5, "12345", "ZIP"))
        Masker([orders], vault).mask("12345")
        result = Masker([zips], vault).mask("12345")
        assert result.text == "[ORDER_1]"
        assert result.entities[0].entity_type == "ORDER"


class TestOverlapsInMask:
    def test_longest_span_wins(self, vault):
        text = "id 555-123-4567"
        detector = FixedDetector(
            span(text, "555-123-4567", "PHONE"),
            span(text, "123-4567", "LOCAL"),
        )
        assert Masker([detector], vault).mask(text).text == "id [PHONE_1]"

    def test_email_beats_custom_domain_pattern(self, vault):
        masker = Masker(
            [RegexDetector(custom_patterns={"DOMAIN": r"example\.com"})], vault
        )
        assert masker.mask("jan@example.com").text == "[EMAIL_1]"

    def test_manual_wins_tie_with_regex(self, vault, manual):
        manual.add("#12345", "CASE")
        masker = Masker(
            [RegexDetector(custom_patterns={"ORDER": r"#\d{5}"}), manual], vault
        )
        result = masker.mask("Ref #12345")
        assert result.text == "Ref [CASE_1]"
        assert result.entities[0].source == "manual"

    def test_manual_wins_tie_regardless_of_detector_order(self, vault, manual):
        manual.add("#12345", "CASE")
        regex = RegexDetector(custom_patterns={"ORDER": r"#\d{5}"})
        assert Masker([manual, regex], vault).mask("#12345").text == "[CASE_1]"
        vault.clear()
        assert Masker([regex, manual], vault).mask("#12345").text == "[CASE_1]"

    def test_longer_regex_beats_shorter_manual(self, masker, manual):
        manual.add("nowak", "PERSON")
        result = masker.mask("Write to nowak@example.com")
        assert result.text == "Write to [EMAIL_1]"
        assert result.warnings == []

    def test_overlapping_manual_entities(self, masker, manual):
        manual.add("Jan Nowak", "PERSON")
        manual.add("Nowak", "PERSON")
        result = masker.mask("Jan Nowak and Mr. Nowak")
        assert result.text == "[PERSON_1] and Mr. [PERSON_2]"
        assert [e.value for e in result.entities] == ["Jan Nowak", "Nowak"]

    def test_phone_extension_variant_wins(self, masker):
        result = masker.mask("Call +1 555 123 4567 ext. 89 today")
        assert result.text == "Call [PHONE_1] today"
        assert result.entities[0].value == "+1 555 123 4567 ext. 89"


class TestWarnings:
    def test_leak_check_reports_known_value_left_in_text(self, masker):
        masker.mask("Call 555-123-4567")
        # The regex refuses this form (a digit group follows), so the number
        # from the first turn would reach the model unmasked.
        result = masker.mask("Call 555-123-4567-2")
        assert result.text == "Call 555-123-4567-2"
        assert result.warnings == [
            "Leak check: known value '555-123-4567' ([PHONE_1]) still appears "
            "in the masked text."
        ]

    def test_partial_overlap_reports_the_part_left_in_text(self, vault):
        # The longer span wins, and the part of the shorter one outside it
        # stays in the text.
        text = "Jan Nowak Street"
        detector = FixedDetector(
            span(text, "Jan Nowak", "PERSON", source="manual", priority=100),
            span(text, "Nowak Street", "ADDRESS", source="manual", priority=100),
        )
        result = Masker([detector], vault).mask(text)
        assert result.text == "Jan [ADDRESS_1]"
        assert result.warnings == [
            "Partial mask: detected PERSON value 'Jan Nowak' overlapped a longer "
            "match, so 'Jan ' is still in the masked text."
        ]

    def test_partial_overlap_on_both_sides(self, vault):
        text = "aa BBBB cc DDDD ee"
        detector = FixedDetector(
            span(text, "BBBB", "X", priority=0),
            span(text, "DDDD", "X", priority=0),
            span(text, "aa BBBB cc DDDD ee"[1:17], "Y", priority=0),
        )
        # The long Y span wins; nothing of the X spans sticks out of it.
        result = Masker([detector], vault).mask(text)
        assert result.text == "a[Y_1]e"
        assert result.warnings == []

    def test_partial_overlap_with_two_winners_reports_the_gap(self, vault):
        # The loser is shorter than both winners but bridges the gap between
        # them, so only the gap is left over.
        text = "AAAAAAAAAA" + "mi" + "BBBBBBBBBB"
        detector = FixedDetector(
            Span(0, 10, "A" * 10, "X"),
            Span(12, 22, "B" * 10, "X"),
            Span(8, 14, "AAmiBB", "Z"),
        )
        result = Masker([detector], vault).mask(text)
        assert result.text == "[X_1]mi[X_2]"
        assert result.warnings == [
            "Partial mask: detected Z value 'AAmiBB' overlapped a longer match, "
            "so 'mi' is still in the masked text."
        ]

    def test_contained_loser_is_not_a_partial_mask(self, vault):
        text = "id 555-123-4567"
        detector = FixedDetector(
            span(text, "555-123-4567", "PHONE"),
            span(text, "123-4567", "LOCAL"),
        )
        assert Masker([detector], vault).mask(text).warnings == []

    def test_punctuation_only_leftover_is_not_reported(self, vault):
        text = "(abc) tail"
        detector = FixedDetector(span(text, "(abc)", "X"), span(text, ") ", "Y"))
        result = Masker([detector], vault).mask(text)
        assert result.text == "[X_1] tail"
        assert result.warnings == []

    def test_builtin_phone_followed_by_phone_masks_both(self, masker):
        result = masker.mask("Phones: +44 20 7946 0958 555-123-4567")
        assert result.text == "Phones: [PHONE_1] [PHONE_2]"
        assert result.warnings == []

    def test_leak_check_uses_word_boundaries(self, masker, manual):
        manual.add("Jan", "PERSON")
        masker.mask("Hi Jan")
        result = masker.mask("See you in January")
        assert result.text == "See you in January"
        assert result.warnings == []

    def test_no_leak_warning_when_value_masked(self, masker):
        masker.mask("a@example.com")
        assert masker.mask("again a@example.com").warnings == []

    def test_placeholder_like_input_is_reported(self, masker):
        result = masker.mask("Template: Dear [PERSON_1], see [PERSON_1] and [X_2].")
        assert result.text == "Template: Dear [PERSON_1], see [PERSON_1] and [X_2]."
        assert result.warnings == [
            "Input already contains placeholder-like text [PERSON_1]; "
            "restore() will treat it as a placeholder.",
            "Input already contains placeholder-like text [X_2]; "
            "restore() will treat it as a placeholder.",
        ]

    def test_value_shaped_like_a_placeholder_body_is_not_a_leak(self, vault):
        masker = Masker(
            [RegexDetector(custom_patterns={"USER": r"\bUSER_\d+\b"})], vault
        )
        result = masker.mask("USER_7 logged in, then USER_1 logged out.")
        assert result.text == "[USER_1] logged in, then [USER_2] logged out."
        assert result.warnings == []

    def test_registered_placeholder_lookalike_is_not_a_leak(self, vault, manual):
        manual.add("[EMAIL_1]", "SECRET")
        masker = Masker([manual, RegexDetector()], vault)
        result = masker.mask("a@example.com and [EMAIL_1]")
        assert result.text == "[EMAIL_1] and [SECRET_1]"
        assert result.warnings == [
            "Input already contains placeholder-like text [EMAIL_1]; "
            "restore() will treat it as a placeholder."
        ]

    def test_leak_check_scales_to_many_values(self, masker):
        rows = [
            f"{i},user{i}@example.com,555-{i % 900 + 100:03d}-{i:04d}"
            for i in range(4000)
        ]
        start = time.perf_counter()
        result = masker.mask("\n".join(rows))
        assert time.perf_counter() - start < 5  # was ~12 s before the fast path
        assert len(result.entities) == 8000
        assert result.warnings == []

    def test_placeholder_text_inside_output_is_not_a_leak(self, vault, manual):
        # A value that looks like part of a placeholder must not trip the check.
        manual.add("EMAIL", "WORD")
        masker = Masker([manual, RegexDetector()], vault)
        result = masker.mask("a@example.com")
        assert result.text == "[EMAIL_1]"
        assert result.warnings == []


class TestErrors:
    def test_non_string_input(self, masker):
        with pytest.raises(TypeError, match="expects str"):
            masker.mask(None)  # type: ignore[arg-type]

    def test_span_value_mismatch_raises_without_leaking(self, vault):
        detector = FixedDetector(Span(0, 3, "abc", "X"))
        with pytest.raises(
            ValueError, match=r"FixedDetector returned a span at 0:3"
        ) as exc:
            Masker([detector], vault).mask("xyz secret")
        assert "abc" not in str(exc.value)
        assert len(vault) == 0

    def test_span_past_end_raises(self, vault):
        detector = FixedDetector(Span(5, 8, "abc", "X"))
        with pytest.raises(ValueError, match="does not match the text"):
            Masker([detector], vault).mask("short")

    def test_no_detectors(self, vault):
        assert Masker([], vault).mask("a@example.com").text == "a@example.com"
