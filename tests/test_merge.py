"""Masking the leftover part of overlapping matches."""

import ast
import itertools
import random
import re
import time

import pytest

import veil._search as search
from vaults import DictVault
from veil import ManualDetector, MemoryVault, Shield, ShieldError, Span
from veil._text import is_word_char
from veil.masker import (
    MERGED_SOURCE,
    Masker,
    merge_partial_overlaps,
    resolve_overlaps,
)

CARD = "4111 1111 1111 1111"

# "ORD-2024-0001" wins over "00017555", which sticks out, so they merge. The
# phone glued to the 7 isn't detected, and the merged span swallows its first
# group; before merging, all of it was visible.
CUT_PATTERNS = {"ORDNO": r"ORD-\d{4}-\d{4}", "DIGITS8": r"\d{8,}"}
CUT_TEXT = "ORD-2024-00017555-555-0123"


class FixedDetector:
    def __init__(self, *spans):
        self.spans = list(spans)

    def detect(self, text):
        return list(self.spans)


def merge(text, spans):
    return merge_partial_overlaps(text, spans, resolve_overlaps(spans))


def shield(*names, **kwargs):
    s = Shield(**kwargs)
    for value, entity_type in names:
        s.add_entity(value, entity_type)
    return s


def leaks(warnings):
    return [w for w in warnings if w.startswith("Leak check")]


class TestMergePartialOverlaps:
    def test_no_spans(self):
        assert merge("abc", []) == []

    def test_kept_spans_pass_through_unchanged(self):
        a, b = Span(0, 3, "abc", "X"), Span(4, 7, "efg", "X")
        result = merge("abc efg", [b, a])
        assert result == [(a, ()), (b, ())]
        assert result[0][0] is a

    def test_loser_inside_a_kept_span_changes_nothing(self):
        text = "id 555-555-0123"
        phone, local = Span(3, 15, text[3:], "PHONE"), Span(7, 15, text[7:], "LOCAL")
        assert merge(text, [phone, local]) == [(phone, ())]

    def test_punctuation_only_leftover_changes_nothing(self):
        text = "(abc) tail"
        x, y = Span(0, 5, "(abc)", "X"), Span(4, 6, ") ", "Y")
        assert merge(text, [x, y]) == [(x, ())]

    def test_adjacent_kept_spans_are_not_merged(self):
        text = "abcdefghij"
        a, b = Span(0, 5, "abcde", "X"), Span(5, 10, "fghij", "X")
        assert merge(text, [a, b, Span(3, 5, "de", "Y")]) == [(a, ()), (b, ())]

    def test_one_sided_leftover(self):
        text = "Jan Nowak Street"
        person = Span(0, 9, "Jan Nowak", "PERSON", "manual", 100)
        street = Span(4, 16, "Nowak Street", "ADDRESS", "manual", 100)
        [(span, members)] = merge(text, [person, street])
        assert span == Span(0, 16, text, "ADDRESS", MERGED_SOURCE, 100)
        assert members == (street,)

    def test_loser_bridging_two_kept_spans(self):
        text = "AAAAAAAAAAmiBBBBBBBBBB"
        a, b = Span(0, 10, "A" * 10, "X"), Span(12, 22, "B" * 10, "X")
        [(span, members)] = merge(text, [a, b, Span(8, 14, "AAmiBB", "Z")])
        assert (span.start, span.end, span.entity_type) == (0, 22, "X")
        assert members == (a, b)

    def test_chain_collapses_into_one_span(self):
        text = "Anna Maria Kowalska Nowak"
        spans = [
            Span(0, 10, "Anna Maria", "PERSON", "manual", 100),
            Span(5, 19, "Maria Kowalska", "PERSON", "manual", 100),
            Span(11, 25, "Kowalska Nowak", "PERSON", "manual", 100),
        ]
        [(span, _)] = merge(text, spans)
        assert (span.start, span.end, span.value) == (0, 25, text)

    def test_overlapping_losers_join_groups_without_a_shared_kept_span(self):
        # Each loser overlaps a different kept span, but the losers overlap
        # each other: two merged spans would overlap, so it is one group.
        text = "a" * 21
        k1, k2 = Span(0, 10, "a" * 10, "K"), Span(14, 21, "a" * 7, "M", priority=100)
        spans = [
            k1,
            Span(5, 14, "a" * 9, "L"),
            k2,
            Span(11, 15, "a" * 4, "N", priority=10),
        ]
        [(span, members)] = merge(text, spans)
        assert (span.start, span.end, span.entity_type) == (0, 21, "K")
        assert members == (k1, k2)

    def test_two_separate_groups(self):
        text = "Jan Nowak Street, Anna Maria Kowalska"
        spans = [
            Span(0, 9, "Jan Nowak", "PERSON"),
            Span(4, 16, "Nowak Street", "ADDRESS"),
            Span(18, 28, "Anna Maria", "PERSON"),
            Span(23, 37, "Maria Kowalska", "PERSON"),
        ]
        result = merge(text, spans)
        assert [(s.value, s.entity_type) for s, _ in result] == [
            ("Jan Nowak Street", "ADDRESS"),
            ("Anna Maria Kowalska", "PERSON"),
        ]

    @pytest.mark.parametrize(
        ("spans", "expected"),
        [
            # the longest kept span names the group, whatever the priorities
            (
                [
                    Span(0, 3, "AAA", "SHORT", priority=100),
                    Span(4, 12, "A" * 8, "LONG"),
                    Span(2, 5, "AxA", "Z"),
                ],
                ("LONG", 0),
            ),
            # equal length: higher priority
            (
                [
                    Span(0, 5, "AAAAA", "LOW"),
                    Span(6, 11, "AAAAA", "HIGH", priority=100),
                    Span(4, 7, "AxA", "Z"),
                ],
                ("HIGH", 100),
            ),
            # full tie: the earlier one
            (
                [
                    Span(0, 5, "AAAAA", "FIRST"),
                    Span(6, 11, "AAAAA", "SECOND"),
                    Span(4, 7, "AxA", "Z"),
                ],
                ("FIRST", 0),
            ),
        ],
    )
    def test_merged_type_is_the_winner_of_the_overlap(self, spans, expected):
        text = "".join("x" if i in (3, 5) else "A" for i in range(12))
        spans = [
            Span(
                s.start,
                s.end,
                text[s.start : s.end],
                s.entity_type,
                s.source,
                s.priority,
            )
            for s in spans
        ]
        [(span, _)] = merge(text, spans)
        assert (span.entity_type, span.priority, span.source) == (*expected, "merged")

    def test_nothing_detected_stays_partly_visible(self):
        rng = random.Random(7)
        for _ in range(3000):
            n = rng.randint(1, 30)
            text = "".join(rng.choice("ab -._1́中") for _ in range(n))
            spans = []
            for _ in range(rng.randint(0, 10)):
                a = rng.randrange(n)
                b = rng.randint(a + 1, min(n, a + rng.randint(1, 12)))
                spans.append(
                    Span(
                        a,
                        b,
                        text[a:b],
                        rng.choice("ABC"),
                        priority=rng.choice([0, 10, 100]),
                    )
                )
            kept = resolve_overlaps(spans)
            result = merge_partial_overlaps(text, spans, kept)
            final = [s for s, _ in result]
            covered = [False] * n
            for s in final:
                assert text[s.start : s.end] == s.value
                covered[s.start : s.end] = [True] * len(s)
            assert all(a.end <= b.start for a, b in itertools.pairwise(final))
            for s in spans:  # nothing detected is left partly visible
                assert not any(
                    text[i].isalnum() and not covered[i] for i in range(s.start, s.end)
                )
            for k in kept:  # every kept span is masked, by itself or merged
                assert any(f.start <= k.start and k.end <= f.end for f in final)

    def test_long_chain_is_linear(self):
        text = "1234x" * 10_000
        s = Shield(custom_patterns={"K": r"\d{4}", "L": r"\dx\d"})
        start = time.perf_counter()
        result = s.mask(text)
        assert time.perf_counter() - start < 5
        assert [e.value for e in result.entities] == [text[:-1]]
        assert s.restore(result.text).text == text


class TestMaskerMerges:
    def test_registered_names_overlapping(self):
        s = shield(("Anna Maria", "PERSON"), ("Maria Kowalska", "PERSON"))
        result = s.mask("Present: Anna Maria Kowalska")
        assert result.text == "Present: [PERSON_1]"
        assert result.warnings == []
        [entity] = result.entities
        assert (entity.value, entity.entity_type, entity.source) == (
            "Anna Maria Kowalska",
            "PERSON",
            "merged",
        )
        assert (entity.start, entity.end) == (9, 28)
        assert s.restore("Hi [PERSON_1]").text == "Hi Anna Maria Kowalska"

    @pytest.mark.parametrize(
        ("names", "custom", "text", "masked"),
        [
            (
                [("Anna Kowalska", "PERSON")],
                None,
                "Owner: Anna Kowalska/anna.k@example.com",
                "Owner: [EMAIL_1]",
            ),
            (
                [("Mary-Jane Watson", "PERSON")],
                None,
                "Mary-Jane Watson.noreply@example.com wrote:",
                "[EMAIL_1] wrote:",
            ),
            ([], None, "Pay +1 4111 1111 1111 1111 today", "Pay [CREDIT_CARD_1] today"),
            ([], None, "2001:db8::1=o'brien@example.com", "[EMAIL_1]"),
            (
                [],
                None,
                "電話555-555-0123 ext. 4354連絡先はmuller@example.com",
                "電話[EMAIL_1]",
            ),
            ([], {"TICKET": r"TKT-\d{4,6}"}, "jan.n@example.comTKT-4242", "[EMAIL_1]"),
            (
                [],
                {"ZIP": r"\b\d{5}\b", "ORDNO": r"ORD-\d{4}-\d{4}"},
                "ORD-2024-00017 shipped",
                "[ORDNO_1] shipped",
            ),
        ],
    )
    def test_real_detector_overlaps_are_fully_masked(self, names, custom, text, masked):
        s = shield(*names, custom_patterns=custom)
        result = s.mask(text)
        assert result.text == masked
        assert result.warnings == []
        assert s.restore(result.text).text == text

    def test_ip_followed_by_a_number_is_not_merged(self):
        s = Shield()
        result = s.mask("ssh 198.51.100.123 2222")
        assert result.text == "ssh [IPV4_1] 2222"
        assert result.warnings == []

    @pytest.mark.parametrize(
        "text", ["A.Kowalska@example.com", "order #4111111111111111"]
    )
    def test_contained_or_punctuation_overlaps_are_unchanged(self, text):
        s = shield(("Kowalska", "PERSON"), custom_patterns={"ORDER": r"#\d{5}"})
        assert all(e.source != "merged" for e in s.mask(text).entities)

    def test_redacted_warnings_have_nothing_to_report(self):
        s = shield(
            ("Anna Maria", "PERSON"),
            ("Maria Kowalska", "PERSON"),
            redact_warnings=True,
            detectors=[],
        )
        result = s.mask("Present: Anna Maria Kowalska")
        assert result.text == "Present: [PERSON_1]"
        assert result.warnings == []

    def test_strict_wrap_no_longer_raises(self):
        s = shield(("Anna Maria", "PERSON"), ("Maria Kowalska", "PERSON"))
        seen = []
        safe = s.wrap(
            lambda prompt: seen.append(prompt) or "Hi [PERSON_1]", strict=True
        )
        assert safe("Present: Anna Maria Kowalska") == "Hi Anna Maria Kowalska"
        assert seen == ["Present: [PERSON_1]"]

    def test_parts_are_stored_after_the_visible_placeholders(self):
        s = shield(
            ("Jan Nowak", "PERSON"),
            ("Nowak Street", "ADDRESS"),
            ("Main Street", "ADDRESS"),
            detectors=[],
        )
        result = s.mask("Jan Nowak Street and Main Street")
        assert result.text == "[ADDRESS_1] and [ADDRESS_2]"
        # The part that was merged gets no placeholder of its own (so none
        # the model never saw can be restored), but the vault remembers it.
        assert s.vault.items() == [
            ("[ADDRESS_1]", "Jan Nowak Street"),
            ("[ADDRESS_2]", "Main Street"),
        ]
        assert s.vault._remembered() == [("Nowak Street", "ADDRESS", "[ADDRESS_1]")]
        # Only what won the overlap is remembered; the loser was not, as in v0.2.
        assert s.vault.get_placeholder("Jan Nowak") is None
        assert s.mask("Nowak Street").text == "[ADDRESS_3]"

    @pytest.mark.parametrize("redact", [False, True])
    def test_later_leak_of_a_merged_part_is_reported(self, redact):
        s = Shield(redact_warnings=redact)
        assert (
            s.mask("Pay +1 4111 1111 1111 1111 today").text
            == "Pay [CREDIT_CARD_1] today"
        )
        # Glued to a letter, the card isn't detected; the leak check knows it.
        result = s.mask("card x4111 1111 1111 1111")
        assert len(result.warnings) == 1
        assert "[CREDIT_CARD_1]" in result.warnings[0]  # what it was masked as
        assert ("4111" in result.warnings[0]) is not redact
        with pytest.raises(ShieldError):
            s.wrap(lambda p: p, strict=True)("card x4111 1111 1111 1111")

    def test_union_already_stored_keeps_its_first_type(self):
        vault = MemoryVault()
        vault.get_or_create("Jan Nowak Street", "LOCATION")
        s = shield(
            ("Jan Nowak", "PERSON"),
            ("Nowak Street", "ADDRESS"),
            vault=vault,
            detectors=[],
        )
        result = s.mask("Meet at Jan Nowak Street")
        assert result.text == "Meet at [LOCATION_1]"
        assert result.entities[0].entity_type == "LOCATION"
        assert result.entities[0].source == "merged"

    def test_same_union_twice_gets_one_placeholder(self):
        s = shield(("Anna Maria", "PERSON"), ("Maria Kowalska", "PERSON"))
        result = s.mask("Anna Maria Kowalska and Anna Maria Kowalska")
        assert result.text == "[PERSON_1] and [PERSON_1]"

    def test_custom_detector_spans_are_merged_too(self):
        text = "AAAAAAAAAAmiBBBBBBBBBB"
        detector = FixedDetector(
            Span(0, 10, "A" * 10, "X"),
            Span(12, 22, "B" * 10, "X"),
            Span(8, 14, "AAmiBB", "Z"),
        )
        result = Masker([detector], MemoryVault()).mask(text)
        assert result.text == "[X_1]"
        assert result.warnings == []


class TestLeaksAroundMergedSpans:
    """A merged span must not hide a known value from the leak check."""

    @pytest.mark.parametrize("redact", [False, True])
    def test_value_cut_by_a_merged_span(self, redact):
        s = Shield(custom_patterns=CUT_PATTERNS, redact_warnings=redact)
        s.mask("Call 555-555-0123")
        result = s.mask(CUT_TEXT)
        assert result.text == "[ORDNO_1]-555-0123"
        if redact:
            assert leaks(result.warnings) == [
                "Leak check: a known PHONE value ([PHONE_1]) still appears in "
                "the masked text."
            ]
        else:
            assert leaks(result.warnings) == [
                "Leak check: known value '555-555-0123' ([PHONE_1]) still appears "
                "in the masked text."
            ]

    def test_strict_wrap_raises(self):
        s = Shield(custom_patterns=CUT_PATTERNS, redact_warnings=True)
        s.mask("Call 555-555-0123")
        llm = s.wrap(lambda p: pytest.fail("model called"), strict=True)
        with pytest.raises(ShieldError) as info:
            llm(CUT_TEXT)
        assert "0123" not in str(info.value)

    def test_fully_hidden_value_is_not_reported(self):
        s = shield(("Anna Maria", "PERSON"), ("Maria Kowalska", "PERSON"))
        assert s.mask("Present: Anna Maria Kowalska").warnings == []
        assert s.mask("Again: Anna Maria Kowalska").warnings == []

    def test_merged_names_are_matched_as_plain_text(self):
        # Registered names are whole tokens, but their union is an ordinary
        # vault value, so it is reported even when glued. Pinned.
        s = shield(("Anna Maria", "PERSON"), ("Maria Kowalska", "PERSON"), detectors=[])
        s.mask("Present: Anna Maria Kowalska")
        [warning] = s.mask("user id xAnna Maria Kowalska2 logged in").warnings
        assert "'Anna Maria Kowalska' ([PERSON_1])" in warning

    def test_same_warnings_with_and_without_the_index(self, monkeypatch):
        r = random.Random(5)
        filler = " ".join(f"note{r.randrange(10**6)}" for _ in range(600))
        emails = " ".join(f"u{i}@example.com" for i in range(200))

        def run():
            s = Shield(custom_patterns=CUT_PATTERNS)
            s.mask(emails + " Call 555-555-0123")
            return s.mask(f"{filler} {CUT_TEXT} {filler}").warnings

        with_index = run()
        monkeypatch.setattr(search, "_worth_indexing", lambda count, size: False)
        assert run() == with_index
        assert len(with_index) == 1


class TestMergedParts:
    """The kept spans a merged span replaced get no placeholder of their own."""

    def test_no_placeholder_the_model_never_saw_restores(self):
        s = Shield()
        replies = iter(["ok", "Refund sent to [CREDIT_CARD_1] and [CREDIT_CARD_3]."])
        llm = s.wrap(lambda prompt: next(replies), strict=True)
        llm(f"Pay +1 {CARD} today")
        assert s.mask("Also refund card 5555 5555 5555 4444").text == (
            "Also refund card [CREDIT_CARD_2]"
        )
        with pytest.raises(ShieldError) as info:
            llm("Also refund card 5555 5555 5555 4444")
        assert info.value.stage == "restore"

    def test_part_seen_alone_later_gets_the_next_number(self):
        s = Shield()
        assert s.mask(f"Pay +1 {CARD} today").text == "Pay [CREDIT_CARD_1] today"
        assert s.mask(f"card {CARD}").text == "card [CREDIT_CARD_2]"
        assert s.restore("[CREDIT_CARD_1] / [CREDIT_CARD_2]").text == (
            f"+1 {CARD} / {CARD}"
        )

    def test_later_leak_is_reported(self):
        s = Shield()
        s.mask(f"Pay +1 {CARD} today")
        assert len(leaks(s.mask(f"card x{CARD}").warnings)) == 1

    def test_reset_forgets_them(self):
        s = Shield()
        s.mask(f"Pay +1 {CARD} today")
        s.reset()
        assert s.mask(f"card x{CARD}").warnings == []

    def test_a_vault_that_cant_remember(self):
        s = Shield(vault=DictVault())
        s.mask(f"Pay +1 {CARD} today")
        assert len(leaks(s.mask(f"card x{CARD}").warnings)) == 1


class _Spans:
    def __init__(self, spans):
        self.spans = spans

    def detect(self, text):
        return list(self.spans)


def _whole_token(text, start, end, lo, hi):
    """find_token's rule, with the ends of text[lo:hi] counting as boundaries."""
    if start > lo and is_word_char(text[start]) and is_word_char(text[start - 1]):
        return False
    return not (end < hi and is_word_char(text[end - 1]) and is_word_char(text[end]))


def _occurrences(text, value, lo, hi):
    pos = text.find(value, lo, hi)
    while pos != -1:
        yield pos
        pos = text.find(value, pos + 1, hi)


def _gaps(text, spans):
    out, cursor = [], 0
    for span in spans:
        out.append((cursor, span.start))
        cursor = span.end
    out.append((cursor, len(text)))
    return out


_LEAK = re.compile(r"^Leak check: known value (.*?)(?: \(\[[A-Z0-9_]+\]\))? still")


@pytest.mark.parametrize("index", [False, True], ids=["loop", "index"])
def test_leak_rule_after_merging(monkeypatch, index):
    """The leak check against a brute-force statement of its rule.

    A known value is reported when it occurs between the final placeholders,
    or between the spans that won the overlaps (where it was visible before
    merging) without lying entirely inside one merged span.
    """
    if index:
        for name in ("_INDEX_MIN_VALUES", "_INDEX_MIN_TEXT", "_INDEX_MIN_WORK"):
            monkeypatch.setattr(search, name, 0)
    merges = 0
    for seed in range(1000):
        r = random.Random(seed)
        alphabet = r.choice(["ab ", "aB1 -", "a\u4e2d x", "ab_."])
        text = "".join(r.choice(alphabet) for _ in range(r.randint(5, 60)))
        vault = MemoryVault()
        manual = ManualDetector()
        starts = [r.randrange(len(text)) for _ in range(6)]
        words = [text[a : a + r.randint(1, 6)] for a in starts]
        words = [w for w in words if w.strip()]
        for word in words[:2]:
            manual.add(word, "M")
        for word in words[2:]:
            vault.get_or_create(word, "V")
        spans = []
        for _ in range(r.randint(0, 8)):
            a = r.randrange(len(text))
            b = min(len(text), a + r.randint(1, 8))
            spans.append(
                Span(a, b, text[a:b], r.choice("XY"), "regex", r.choice([0, 10]))
            )
        candidates = manual.detect(text) + spans
        kept = resolve_overlaps(candidates)
        final = merge_partial_overlaps(text, candidates, kept)
        merged = [span for span, members in final if members]
        merges += bool(merged)

        result = Masker([manual, _Spans(spans)], vault).mask(text)
        got = set()
        for warning in result.warnings:
            match = _LEAK.match(warning)
            if match:
                got.add(ast.literal_eval(match.group(1)))

        known = {v for _, v in vault.items()} | {v for v, _, _ in vault._remembered()}
        known |= {c.value for c in candidates}
        tokens = set(manual.entities)
        want = set()
        for value in known:
            for lo, hi in _gaps(text, [span for span, _ in final]):
                if any(
                    value not in tokens or _whole_token(text, p, p + len(value), lo, hi)
                    for p in _occurrences(text, value, lo, hi)
                ):
                    want.add(value)
            for lo, hi in _gaps(text, kept):
                for p in _occurrences(text, value, lo, hi):
                    end = p + len(value)
                    if value in tokens and not _whole_token(text, p, end, lo, hi):
                        continue
                    if not any(m.start <= p and end <= m.end for m in merged):
                        want.add(value)
        assert got == want, (seed, text, sorted(got ^ want))
    assert merges > 100
