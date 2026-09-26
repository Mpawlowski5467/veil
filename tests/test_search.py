import random
import time

import pytest

import veil._search as search
from veil._search import CachedIndex, LiteralIndex
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


class TestCachedIndex:
    def test_small_inputs_check_values_one_by_one(self, monkeypatch):
        def fail(*args):
            raise AssertionError("built an index for a small input")

        monkeypatch.setattr(search, "LiteralIndex", fail)
        values = {f"v{i}" for i in range(1000)}
        cache = CachedIndex()
        assert cache.present(["v7 and v12"], values, set()) == {"v1", "v7", "v12"}
        assert cache.present(["x" * 100_000], set(sorted(values)[:10]), set()) == set()

    def test_large_inputs_use_the_index(self, monkeypatch):
        built = []
        real = search.LiteralIndex
        monkeypatch.setattr(
            search, "LiteralIndex", lambda v: built.append(1) or real(v)
        )
        values = {f"user{i}@example.com" for i in range(500)}
        text = "mail user42@example.com. " * 400
        cache = CachedIndex()
        assert cache.present([text], values, set()) == {"user42@example.com"}
        assert cache.present([text], values | {"new@example.com"}, set()) == {
            "user42@example.com"
        }
        assert built == [1]  # the new value was checked on its own

    def test_recursion_error_falls_back(self, monkeypatch):
        def deep(values):
            raise RecursionError

        monkeypatch.setattr(search, "LiteralIndex", deep)
        values = {f"user{i}@example.com" for i in range(500)}
        text = "mail user42@example.com. " * 200
        assert CachedIndex().present([text], values, set()) == {"user42@example.com"}

    def test_empty_value_counts_like_in(self, monkeypatch):
        monkeypatch.setattr(search, "_INDEX_MIN_VALUES", 0)
        monkeypatch.setattr(search, "_REBUILD_WORK", 0)
        assert CachedIndex().present(["abc"], {"", "b", "z"}, set()) == {"", "b"}
        assert CachedIndex().present(["abc"], {"", "b"}, {""}) == {"b"}

    def test_first_build_waits_until_it_pays_off(self, monkeypatch):
        # Building costs about _REBUILD_WORK characters of scanning per value;
        # until checking values one by one has cost that much, don't build.
        built = []
        real = search.LiteralIndex
        monkeypatch.setattr(
            search, "LiteralIndex", lambda v: built.append(1) or real(v)
        )
        values = {f"user{i}@example.com" for i in range(1000)}
        text = "no addresses here " * 170  # about 3,000 characters
        cache = CachedIndex()
        for expected in ([], [], [1], [1]):
            assert cache.present([text], values, set()) == set()
            assert built == expected

    def test_values_no_longer_asked_about_are_ignored(self):
        cache = CachedIndex()
        values = {f"user{i}@example.com" for i in range(100)}
        text = " ".join(sorted(values)) + " filler" * 2000  # big enough to index
        assert cache.present([text], values, set()) == values
        fewer = set(sorted(values)[:60])
        assert cache.present([text], fewer, set(), update=False) == fewer

    @pytest.mark.parametrize("seed", range(40))
    def test_same_results_as_one_scan_per_value_as_values_change(
        self, monkeypatch, seed
    ):
        r = random.Random(seed)
        tuning = {
            "_INDEX_MIN_VALUES": r.choice([0, 3, 64]),
            "_REBUILD_WORK": r.choice([1, 50, 1 << 13, 10**9]),
            "_VALUE_WORK": r.choice([0, 256]),
            "_MAX_WIDTH": r.choice([1, 2, 32]),
            "_BUCKET": r.choice([1, 16]),
            "_MIN_PREFIX": r.choice([1, 3]),
        }
        for name, value in tuning.items():
            monkeypatch.setattr(search, name, value)
        alpha = r.choice(
            ["ab", "abc -", "aB1_ ", "a\u00e9\u0301 \u4e2dx", "-.", "ab\x00\x01"]
        )

        def word():
            size = r.randint(0 if r.random() < 0.02 else 1, 6)
            return "".join(r.choice(alpha) for _ in range(size))

        cache = CachedIndex()
        values = {word() for _ in range(r.randint(0, 40))}
        for _ in range(r.randint(1, 25)):
            op = r.random()
            if op < 0.1:
                values = {word() for _ in range(r.randint(0, 5))}  # vault cleared
            elif op < 0.15:
                cache.clear()
            else:
                values |= {word() for _ in range(r.randint(0, 15))}
            haystacks = [
                "".join(r.choice(alpha) for _ in range(r.randint(0, 120)))
                for _ in range(r.randint(1, 3))
            ]
            tokens = {v for v in values if r.random() < 0.4}
            update = r.random() < 0.8
            assert cache.present(
                haystacks, set(values), tokens, update=update
            ) == one_by_one(haystacks, values, tokens)


def test_regexes_hold_at_most_max_depth_characters_of_a_value():
    # The patterns can outlive reset() in re's cache; the README says so.
    for count in (2, 20, 40):  # a bucket, a forced branch, a wide node
        values = ["k" * 100 + f"{i:02d}" for i in range(count)]
        index = LiteralIndex(values)
        for regex in index._regexes:
            assert "k" * (search._MAX_DEPTH + 1) not in regex.pattern
        assert index.present(["x" + values[-1]], set()) == {values[-1]}


def test_token_occurrences_falls_back_when_the_budget_runs_out(monkeypatch):
    calls = []
    real = search._tokens_one_by_one
    monkeypatch.setattr(
        search, "_tokens_one_by_one", lambda t, v: calls.append(1) or real(t, v)
    )
    index = LiteralIndex("-" * 100 + f"{i:04d}" for i in range(1000))
    assert index.token_occurrences("-" * 60_000) == []
    assert calls == [1]


class TestPerformance:
    """Inputs that take quadratic time when each value is searched on its own."""

    def test_values_starting_outside_the_bmp(self):
        # re tests astral first characters one by one at every position.
        index = LiteralIndex(chr(0x20000 + i) + "\u5c71" for i in range(1000))
        text = "plain English text, nothing to find here. " * 2400  # 100 KB
        start = time.perf_counter()
        assert index.present([text], set()) == set()
        assert index.token_occurrences(text) == []
        assert time.perf_counter() - start < 0.05  # 0.2 s before
        astral = chr(0x20000 + 7) + "\u5c71"
        assert index.present([f"x{astral}y"], set()) == {astral}
        assert index.token_occurrences(f"{chr(0x20000)}{astral}") == [(1, astral)]

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
