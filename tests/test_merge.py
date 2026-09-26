"""Masking the leftover part of overlapping matches."""

import itertools
import random

import pytest

from veil import Span
from veil.masker import MERGED_SOURCE, merge_partial_overlaps, resolve_overlaps


def merge(text, spans):
    return merge_partial_overlaps(text, spans, resolve_overlaps(spans))


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
