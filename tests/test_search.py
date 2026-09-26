import random
import time

import pytest

import veil._search as search
from veil._search import LiteralIndex
from veil._text import contains_token, find_token


def one_by_one(haystacks, values, tokens):
    """What the leak check did before the index: one scan per value."""
    return {
        v
        for v in values
        if any(contains_token(h, v) if v in tokens else v in h for h in haystacks)
    }


def find_token_all(text, values):
    pairs = [(start, v) for v in values for start in find_token(text, v)]
    pairs.sort(key=lambda p: (p[0], -len(p[1])))
    return pairs


def every_occurrence(index, text):
    return sorted(
        (start, v)
        for start, cands in index._candidates(text)
        for v in cands
        if text.startswith(v, start)
    )


class TestLiteralIndex:
    def test_overlapping_and_nested_occurrences(self):
        index = LiteralIndex(["a", "aa", "aaa", "b"])
        assert index.present(["aaaa"], set()) == {"a", "aa", "aaa"}
        assert every_occurrence(index, "aaa") == [
            (0, "a"), (0, "aa"), (0, "aaa"), (1, "a"), (1, "aa"), (2, "a"),
        ]  # fmt: skip

    def test_value_found_only_inside_a_longer_values_occurrence(self):
        index = LiteralIndex(["abcd", "bc", "cd"])
        assert index.present(["xabcdx"], set()) == {"abcd", "bc", "cd"}

    def test_value_found_only_overlapping_another(self):
        assert LiteralIndex(["abc", "cde"]).present(["abcde"], set()) == {"abc", "cde"}

    def test_tokens_use_find_tokens_rule(self):
        index = LiteralIndex(["Jan", "Jan!", "田中", "a-b"])
        tokens = {"Jan", "Jan!", "田中", "a-b"}
        assert index.present(["January"], tokens) == set()
        assert index.present(["(Jan) _Jan_ Jan's"], tokens) == {"Jan"}
        assert index.present(["xJan!!"], tokens) == set()  # glued on the left
        assert index.present(["Jan!!"], tokens) == {"Jan", "Jan!"}
        assert index.present(["田中さん"], tokens) == {"田中"}  # CJK never glues
        assert index.present(["xa-bx"], tokens) == set()
        assert index.present(["xa-bx"], set()) == {"a-b"}  # plain: anywhere

    def test_token_occurrences_equal_find_token(self):
        text = "Jan, January, Jan's (Jan) aaaa -- ---- Jan Nowak"
        values = ["Jan", "Jan Nowak", "Nowak", "aa", "--", "-"]
        assert LiteralIndex(values).token_occurrences(text) == find_token_all(
            text, values
        )

    @pytest.mark.parametrize(
        "value",
        ["a.b*c", "(x)", "[PERSON_1]", "\\d+", "a|b", "^$", "\x00", "\x1f\n", "-]^["],
    )
    def test_metacharacters_and_control_characters_are_literal(self, value):
        index = LiteralIndex([value, "zz"])
        assert index.present([f"<{value}>"], set()) == {value}
        assert index.present(["<ab>"], set()) == set()

    @pytest.mark.parametrize(
        "value",
        ["\U0001f600", "\U00020000x", "é", "\ud800", "राम", "\U0010ffff"],
    )
    def test_unicode_values(self, value):
        assert LiteralIndex([value, value + "!"]).present([f" {value} "], set()) == {
            value
        }

    def test_empty_and_duplicate_values(self):
        index = LiteralIndex(["", "a", "a"])
        assert len(index) == 1
        assert index.present(["a"], set()) == {"a"}

    def test_many_values_sharing_long_prefixes(self):
        base = "x" * 300
        values = [base + str(i) for i in range(100)] + ["a" * i for i in range(1, 60)]
        text = base + "42 " + "a" * 30
        assert LiteralIndex(values).present([text], set()) == one_by_one(
            [text], values, set()
        )

    def test_several_haystacks_never_match_across_them(self):
        assert LiteralIndex(["ab"]).present(["xa", "bx"], set()) == set()


ALPHABETS = [
    "ab",
    "ab -",
    "aA1 _.-'",
    "áe ा",
    "山田太郎 a",
    "\U00020000\U0001f600a ",
    "\x00\x01a\x1f ",
    ".*+?()[]{}|^$\\a",
    "ก ข",
]

TUNINGS = {
    "default": {},
    "tiny": {"_BUCKET": 1, "_MIN_PREFIX": 1, "_MIN_FANOUT": 1},
    "caps": {"_BUCKET": 1, "_MIN_PREFIX": 1, "_MAX_NESTING": 2, "_MAX_DEPTH": 2},
    "gives up": {"_BASE_WORK": 5, "_CHARS_PER_WORK": 10**12},
}


def random_case(r):
    alpha = r.choice(ALPHABETS)
    text = "".join(r.choice(alpha) for _ in range(r.choice([0, 10, 80, 400])))
    values = []
    for _ in range(r.randint(1, r.choice([4, 30, 100]))):
        k = r.random()
        if k < 0.3 and text:
            start = r.randrange(len(text))
            v = text[start : start + r.randint(1, 10)]
        elif k < 0.45:
            v = r.choice(alpha) * r.randint(1, 6)
        elif k < 0.65 and values:
            base = r.choice(values)
            v = base[: r.randint(1, len(base))] if r.random() < 0.5 else base + "a"
        else:
            v = "".join(r.choice(alpha) for _ in range(r.randint(1, 12)))
        values.append(v)
    tokens = {v for v in values if r.random() < 0.5}
    return text, values, tokens


@pytest.mark.parametrize("tuning", TUNINGS.values(), ids=TUNINGS.keys())
def test_same_results_as_one_scan_per_value(monkeypatch, tuning):
    for name, value in tuning.items():
        monkeypatch.setattr(search, name, value)
    for seed in range(300):
        r = random.Random(seed)
        text, values, tokens = random_case(r)
        index = LiteralIndex(values)
        assert index.present([text], tokens) == one_by_one([text], values, tokens)
        assert index.token_occurrences(text) == find_token_all(text, set(values))
        cut = r.randint(0, len(text))
        pieces = [text[:cut], text[cut:]]
        assert index.present(pieces, tokens) == one_by_one(pieces, values, tokens)
        want = sorted(
            (p, v)
            for v in set(values)
            for p in range(len(text))
            if text.startswith(v, p)
        )
        assert every_occurrence(index, text) == want


class TestPerformance:
    """Inputs that take quadratic time when each value is searched on its own."""

    def test_many_values(self):
        values = [f"user{i}@example.com" for i in range(15_000)]
        values += [f"+44 20 7946 {i:04d}" for i in range(15_000)]
        text = " ".join(f"line {i} of the report, nothing here" for i in range(12_000))
        start = time.perf_counter()
        assert LiteralIndex(values).present([text], set()) == set()
        assert time.perf_counter() - start < 1.0

    @pytest.mark.parametrize("use", ["present", "tokens"])
    def test_long_shared_prefix_repeated(self, use):
        # Every position of the text starts the same 64-character skeleton
        # path, so the index gives up and checks the values one by one.
        base = "x" * 5000
        index = LiteralIndex(base + str(i) for i in range(200))
        text = (base + "y") * 100
        start = time.perf_counter()
        if use == "present":
            assert index.present([text], set()) == set()
        else:
            assert index.token_occurrences(text) == []
        assert time.perf_counter() - start < 1.0

    def test_many_first_characters_in_dense_text(self):
        r = random.Random(7)
        alphabet = [chr(0x4E00 + i) for i in range(3000)]
        values = {"".join(r.choices(alphabet, k=r.randint(3, 6))) for _ in range(9000)}
        text = "".join(r.choices(alphabet, k=400_000))
        start = time.perf_counter()
        found = LiteralIndex(values).present([text], set())
        assert time.perf_counter() - start < 2.0
        sample = sorted(values)[:300]
        assert [v in found for v in sample] == [v in text for v in sample]

    @pytest.mark.parametrize("use", ["present", "tokens"])
    def test_wide_inner_node(self, use):
        # 3,000 values share "a" and then differ: re would try every one of
        # them wherever the text has an "a", unless the node is dispatched.
        index = LiteralIndex("a" + chr(0x4E00 + i) + "z" * 60 for i in range(3000))
        text = "ab" * 300_000 + "中"
        start = time.perf_counter()
        if use == "present":
            assert index.present([text], set()) == set()
        else:
            assert index.token_occurrences(text) == []
        assert time.perf_counter() - start < 2.0
