"""Streaming restore: pieces joined must equal a one-shot restore."""

import random
import time

import pytest

from veil import MemoryVault, Shield, StreamRestorer
from veil.placeholders import (
    LOOSE_PLACEHOLDER_RE,
    MAX_PLACEHOLDER_LENGTH,
    PLACEHOLDER_OPENERS,
    PLACEHOLDER_RE,
    format_placeholder,
    validate_entity_type,
)
from veil.restorer import _EXACT_PREFIX_RE, _LOOSE_PREFIX_RE


def make_shield(*, tolerant=True):
    shield = Shield(tolerant_restore=tolerant)
    shield.add_entity("Jan Nowak", "PERSON")
    shield.add_entity("Ada Quill", "PERSON")
    shield.add_entity("Team [PERSON_2] Ltd", "ORG")  # a value that looks placeholdery
    shield.mask(
        "Jan Nowak, Ada Quill and Team [PERSON_2] Ltd: jane.doe@example.com, "
        "(555) 555-0100"
    )
    return shield


PIECES = [
    "[PERSON_1]",
    "[PERSON_2]",
    "[PERSON_10]",
    "[EMAIL_1]",
    "[PHONE_1]",
    "[ORG_1]",
    "[person 1]",
    "[Person-2]",
    "[ PERSON_1 ]",
    "[PERSON_01]",
    "\N{LEFT BLACK LENTICULAR BRACKET}PERSON_1\N{RIGHT BLACK LENTICULAR BRACKET}",
    "\N{FULLWIDTH LEFT SQUARE BRACKET}email 1\N{FULLWIDTH RIGHT SQUARE BRACKET}",
    "\\[PERSON_1\\]",
    "\\[EMAIL_9\\]",
    "[EMAIL_9]",
    "[person 7]",
    "[Figure 2]",
    "scores[email1]",
    "arr[EMAIL_1]",
    ")[person 1]",
    "[[PERSON_1]]",
    "[",
    "]",
    "\\",
    "\N{LEFT BLACK LENTICULAR BRACKET}",
    "\N{FULLWIDTH RIGHT SQUARE BRACKET}",
    "[PERSON_",
    "_1]",
    " ",
    "\n",
    "x",
    "Hello ",
    "42",
    "é",
    "\N{GRINNING FACE}",
]


def random_text(rng, n):
    return "".join(rng.choice(PIECES) for _ in range(n))


def split_randomly(rng, text):
    cuts = sorted(
        rng.sample(range(len(text) + 1), min(len(text) + 1, rng.randint(0, 8)))
    )
    pieces, last = [], 0
    for cut in cuts:
        pieces.append(text[last:cut])
        last = cut
    pieces.append(text[last:])
    return pieces


def stream_all(shield, pieces):
    stream = shield.stream_restorer()
    out = [stream.feed(piece) for piece in pieces]
    out.append(stream.finish())
    return "".join(out), stream.result()


def assert_same(batch, streamed_text, streamed):
    assert streamed_text == batch.text
    assert streamed.text == batch.text
    assert streamed.restored_count == batch.restored_count
    assert streamed.warnings == batch.warnings
    assert streamed.repaired == batch.repaired


@pytest.mark.parametrize("tolerant", [True, False])
def test_random_splits_match_one_shot_restore(tolerant):
    rng = random.Random(1234 + tolerant)
    shield = make_shield(tolerant=tolerant)
    for _ in range(3000):
        text = random_text(rng, rng.randint(0, 14))
        batch = shield.restore(text)
        streamed_text, streamed = stream_all(shield, split_randomly(rng, text))
        assert_same(batch, streamed_text, streamed)


TRICKY = [
    "Sent to [EMAIL_1] and [PERSON_1].",
    "\\[PERSON_1\\] and \\[EMAIL_9\\]",
    "scores[email1] vs [email 1]",
    "[[PERSON_1]] [ORG_1] [PERSON_2]",
    "\N{LEFT BLACK LENTICULAR BRACKET}PERSON_1\N{RIGHT BLACK LENTICULAR BRACKET}!",
    "a)[person 1] b [person 1]",
    "[PERSON_1",
    "\\[",
]


@pytest.mark.parametrize("text", TRICKY)
@pytest.mark.parametrize("tolerant", [True, False])
def test_every_single_split_matches(text, tolerant):
    shield = make_shield(tolerant=tolerant)
    batch = shield.restore(text)
    for cut in range(len(text) + 1):
        streamed_text, streamed = stream_all(shield, [text[:cut], text[cut:]])
        assert_same(batch, streamed_text, streamed)
    # One character at a time, too.
    streamed_text, streamed = stream_all(shield, list(text))
    assert_same(batch, streamed_text, streamed)


def test_only_a_possible_placeholder_is_held_back():
    shield = make_shield()
    stream = shield.stream_restorer()
    assert stream.feed("Hello, ") == "Hello, "
    assert stream.feed("mail [EMA") == "mail "
    assert stream.feed("IL_1] now") == "jane.doe@example.com now"
    assert stream.feed(" [Figure") == " "
    assert stream.feed(" 2] done") == "[Figure 2] done"
    assert stream.finish() == ""


def test_the_hold_back_is_bounded():
    rng = random.Random(7)
    shield = make_shield()
    for _ in range(300):
        stream = shield.stream_restorer()
        received = emitted = ""
        for piece in split_randomly(rng, random_text(rng, 30)):
            received += piece
            emitted_now = stream.feed(piece)
            emitted += emitted_now
            held = len(received) - len(stream._pending)
            assert held >= 0
            pending = stream._pending
            assert len(pending) < MAX_PLACEHOLDER_LENGTH
            assert pending == "" or pending[0] in PLACEHOLDER_OPENERS
        stream.finish()


def test_markdown_links_and_other_brackets_are_not_delayed():
    shield = make_shield()
    stream = shield.stream_restorer()
    assert stream.feed("See [the docs](https://example.com) ") == (
        "See [the docs](https://example.com) "
    )
    assert stream.feed("and [1, 2] or arr[i] ") == "and [1, 2] or arr[i] "
    assert stream.feed("or \\n") == "or \\n"
    assert stream.finish() == ""


def test_the_prefix_patterns_accept_every_start_of_a_match():
    rng = random.Random(5)
    for pattern, prefix in (
        (LOOSE_PLACEHOLDER_RE, _LOOSE_PREFIX_RE),
        (PLACEHOLDER_RE, _EXACT_PREFIX_RE),
    ):
        for _ in range(3000):
            text = random_text(rng, rng.randint(1, 6))
            for match in pattern.finditer(text):
                found = match.group(0)
                for end in range(1, len(found)):
                    assert prefix.fullmatch(found[:end]), found[:end]
        longest = "[" + "A" * 64 + "_" + "9" * 9
        assert prefix.fullmatch(longest)
        assert not prefix.fullmatch(longest + "9")
        for size in range(MAX_PLACEHOLDER_LENGTH, MAX_PLACEHOLDER_LENGTH + 3):
            for opener in "[\\\N{LEFT BLACK LENTICULAR BRACKET}":
                assert not prefix.fullmatch(opener + "A" * (size - 1))


def test_an_unfinished_placeholder_is_flushed_as_is():
    shield = make_shield()
    stream = shield.stream_restorer()
    assert stream.feed("see [PERSON_") == "see "
    assert stream.finish() == "[PERSON_"


def test_long_bracketed_text_is_released_once_it_cant_be_a_placeholder():
    shield = make_shield()
    stream = shield.stream_restorer()
    text = "[" + "a" * (MAX_PLACEHOLDER_LENGTH + 5)
    out = stream.feed(text)
    assert out.startswith("[")
    assert len(stream._pending) == 0


def test_a_restored_value_is_never_scanned_again():
    shield = make_shield()
    streamed_text, _ = stream_all(shield, ["from [OR", "G_1] with love"])
    assert streamed_text == "from Team [PERSON_2] Ltd with love"


def test_the_lookbehind_sees_the_previous_piece():
    shield = make_shield()
    # "scores" then "[email1]": a code subscript, left alone as in one shot.
    streamed_text, _ = stream_all(shield, ["scores", "[email1]"])
    assert streamed_text == shield.restore("scores[email1]").text == "scores[email1]"
    streamed_text, _ = stream_all(shield, ["see ", "[email 1]"])
    assert streamed_text == "see jane.doe@example.com"


def test_restore_stream_yields_non_empty_pieces():
    shield = make_shield()
    pieces = list(shield.restore_stream(["Hi [PER", "", "SON_1]", ", bye"]))
    assert pieces == ["Hi ", "Jan Nowak", ", bye"]
    assert list(shield.restore_stream([])) == []


def test_stream_restorer_type_and_misuse():
    shield = make_shield()
    stream = shield.stream_restorer()
    assert isinstance(stream, StreamRestorer)
    with pytest.raises(TypeError, match="expects str"):
        stream.feed(b"bytes")
    with pytest.raises(ValueError, match="before finish"):
        stream.result()
    stream.finish()
    with pytest.raises(ValueError, match="after finish"):
        stream.feed("x")
    with pytest.raises(ValueError, match="twice"):
        stream.finish()


def test_the_result_summarizes_the_whole_stream():
    shield = make_shield()
    _, result = stream_all(shield, ["[person 1] [EMAIL", "_9] [PERSON_1]"])
    assert result.restored_count == 2
    assert result.warnings == ["Unknown placeholder [EMAIL_9] was left unchanged."]
    assert [(r.written, r.placeholder) for r in result.repaired] == [
        ("[person 1]", "[PERSON_1]")
    ]


def test_streaming_a_large_reply_takes_linear_time():
    shield = make_shield()
    text = ("word [PERSON_1] [x] \\ " * 20000) + "[EMAIL_1]"
    started = time.perf_counter()
    streamed_text, _ = stream_all(
        shield, [text[i : i + 64] for i in range(0, len(text), 64)]
    )
    elapsed = time.perf_counter() - started
    assert streamed_text == shield.restore(text).text
    assert elapsed < 5


class TestPatternBounds:
    def test_the_longest_placeholder_is_the_bound(self):
        longest = format_placeholder("A" * 64, 999_999_999)
        assert len(longest) == MAX_PLACEHOLDER_LENGTH
        assert PLACEHOLDER_RE.fullmatch(longest)
        assert LOOSE_PLACEHOLDER_RE.fullmatch(longest)

    def test_no_match_is_longer_than_the_bound(self):
        rng = random.Random(3)
        alphabet = "[]\\ \tA_1a-" + "\N{LEFT BLACK LENTICULAR BRACKET}"
        for _ in range(5000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 120)))
            for match in LOOSE_PLACEHOLDER_RE.finditer(text):
                assert len(match.group(0)) <= MAX_PLACEHOLDER_LENGTH
        loose = "\\[" + " " * 8 + "a" + "b" * 40 + "123456" + " " * 8 + "\\]"
        assert LOOSE_PLACEHOLDER_RE.fullmatch(loose)
        assert len(loose) <= MAX_PLACEHOLDER_LENGTH

    def test_types_are_at_most_64_characters(self):
        assert validate_entity_type("A" * 64) == "A" * 64
        with pytest.raises(ValueError, match="up to 64"):
            validate_entity_type("A" * 65)

    def test_numbers_are_at_most_nine_digits(self):
        assert format_placeholder("X", 999_999_999) == "[X_999999999]"
        with pytest.raises(ValueError, match="1 to 999999999"):
            format_placeholder("X", 1_000_000_000)

    def test_a_64_character_type_round_trips(self):
        shield = Shield(vault=MemoryVault())
        kind = "T" * 64
        shield.add_entity("Example Corp", kind)
        masked = shield.mask("Ask Example Corp.").text
        assert masked == f"Ask [{kind}_1]."
        assert "".join(shield.restore_stream([masked[:10], masked[10:]])) == (
            "Ask Example Corp."
        )

    def test_padding_inside_brackets_is_at_most_eight(self):
        shield = make_shield()
        assert shield.restore("[" + " " * 8 + "person 1]").text == "Jan Nowak"
        assert shield.restore("[" + " " * 9 + "person 1]").text == (
            "[" + " " * 9 + "person 1]"
        )
