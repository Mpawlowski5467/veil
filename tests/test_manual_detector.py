import copy
import pickle
import time
import unicodedata

import pytest

from veil.detectors import Detector, ManualDetector
from veil.detectors import manual as manual_module
from veil.types import Span


@pytest.fixture(params=["loop", "index"])
def detector(request):
    """A detector searching one value at a time, or all at once with an index."""
    detector = ManualDetector()
    if request.param == "index":
        detector._INDEX_MIN_VALUES = 1
    return detector


def found(detector, text):
    return [(s.entity_type, s.value, s.start) for s in detector.detect(text)]


def test_satisfies_protocol(detector):
    assert isinstance(detector, Detector)


def test_empty_detector_finds_nothing(detector):
    assert detector.detect("Jan Nowak") == []
    assert len(detector) == 0


def test_finds_registered_value(detector):
    detector.add("Jan Nowak", "PERSON")
    assert detector.detect("Email Jan Nowak today") == [
        Span(6, 15, "Jan Nowak", "PERSON", "manual", ManualDetector.PRIORITY)
    ]


def test_finds_every_occurrence(detector):
    detector.add("Jan Nowak", "PERSON")
    text = "Jan Nowak met Anna. Later, Jan Nowak left."
    assert found(detector, text) == [
        ("PERSON", "Jan Nowak", 0),
        ("PERSON", "Jan Nowak", 27),
    ]


def test_case_sensitive(detector):
    detector.add("Jan Nowak", "PERSON")
    assert detector.detect("jan nowak and JAN NOWAK") == []


def test_exact_substring_only(detector):
    detector.add("Jan Nowak", "PERSON")
    assert detector.detect("Jan  Nowak") == []  # two spaces
    assert detector.detect("Jan\nNowak") == []


@pytest.mark.parametrize(
    "text",
    ["January", "Janet", "DeJan", "Jan2", "JanNowak"],
)
def test_does_not_match_inside_words(detector, text):
    detector.add("Jan", "PERSON")
    assert detector.detect(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Jan",
        "Jan's",
        "(Jan)",
        "Jan, hi",
        "hi Jan.",
        "Jan-Nowak",
        '"Jan"',
        "@Jan",
        "_Jan_",  # Markdown emphasis
        "Jan_",
    ],
)
def test_matches_at_word_boundaries(detector, text):
    detector.add("Jan", "PERSON")
    assert [s.value for s in detector.detect(text)] == ["Jan"]


def test_non_word_edges_have_no_boundary_requirement(detector):
    detector.add("#A-17", "CASE")
    # Starts with "#", so a word character may precede it; ends with "7", so
    # a word character may not follow it.
    assert [s.value for s in detector.detect("ref#A-17.")] == ["#A-17"]
    assert detector.detect("ref#A-17x") == []


def test_unicode_names(detector):
    detector.add("Łucja Wiśniewska", "PERSON")
    detector.add("Zoë", "PERSON")
    assert [s.value for s in detector.detect("Łucja Wiśniewska and Zoë.")] == [
        "Łucja Wiśniewska",
        "Zoë",
    ]
    # Unicode letters count as word characters for the boundary check.
    assert detector.detect("Zoëy") == []


def test_regex_metacharacters_are_literal(detector):
    detector.add("a.b*c", "CODE")
    assert detector.detect("axb*c") == []
    assert [s.value for s in detector.detect("see a.b*c")] == ["a.b*c"]


def test_overlapping_values_are_all_reported(detector):
    detector.add("Jan Nowak", "PERSON")
    detector.add("Nowak", "PERSON")
    detector.add("Nowak Street", "ADDRESS")
    assert found(detector, "Jan Nowak Street") == [
        ("PERSON", "Jan Nowak", 0),
        ("ADDRESS", "Nowak Street", 4),
        ("PERSON", "Nowak", 4),
    ]


def test_re_registering_replaces_type(detector):
    detector.add("Acme", "PERSON")
    detector.add("Acme", "ORG")
    assert found(detector, "Acme") == [("ORG", "Acme", 0)]
    assert detector.entities == {"Acme": "ORG"}
    assert len(detector) == 1


def test_entities_is_a_copy(detector):
    detector.add("Jan Nowak", "PERSON")
    detector.entities.clear()
    assert len(detector) == 1


@pytest.mark.parametrize("value", ["", "   ", "\n", None])
def test_rejects_blank_values(detector, value):
    with pytest.raises(ValueError, match="non-blank"):
        detector.add(value, "PERSON")


def test_rejects_invalid_type(detector):
    with pytest.raises(ValueError, match="Invalid entity type"):
        detector.add("Jan Nowak", "person")


def test_repr_hides_values(detector):
    detector.add("Jan Nowak", "PERSON")
    assert "Jan" not in repr(detector)


def test_combining_marks_count_as_part_of_a_word(detector):
    # Devanagari vowel signs and decomposed accents are combining marks, not
    # letters, but they belong to the word they follow.
    detector.add("राम", "PERSON")
    assert [s.start for s in detector.detect("राम ने रामायण पढ़ी")] == [0]
    detector.add("Jose", "PERSON")
    decomposed = unicodedata.normalize("NFD", "José and Jose")
    assert [s.value for s in detector.detect(decomposed)] == ["Jose"]


def test_value_ending_in_a_combining_mark_is_guarded(detector):
    detector.add("सीता", "PERSON")  # ends with a vowel sign
    assert detector.detect("सीताराम") == []
    assert [s.value for s in detector.detect("सीता जी")] == ["सीता"]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("山田", "山田さんに連絡"),  # Japanese: no spaces around words
        ("王伟", "请联系王伟先生"),  # Chinese
        ("김철수", "김철수는 내일 온다"),  # Korean particle attached to the name
        ("สมชาย", "คุณสมชายมา"),  # Thai
        ("Jan", "Janさん"),  # Latin name next to Japanese
    ],
)
def test_matches_inside_scripts_without_spaces(detector, value, text):
    detector.add(value, "PERSON")
    assert [s.value for s in detector.detect(text)] == [value]


def test_overlapping_occurrences_of_one_value(detector):
    # A rejected occurrence must not hide a valid one that overlaps it.
    detector.add("ab-ab", "CODE")
    assert [s.start for s in detector.detect("xab-ab-ab")] == [4]


def registered(count, template="Name{} Example"):
    detector = ManualDetector()
    for i in range(count):
        detector.add(template.format(i), "PERSON")
    return detector


class TestIndex:
    """From _INDEX_MIN_VALUES registered values on, detect() uses a LiteralIndex."""

    def test_index_is_rebuilt_after_add(self):
        detector = registered(10, "Name{}")
        assert [s.value for s in detector.detect("Name3 Name99")] == ["Name3"]
        detector.add("Name99", "PERSON")
        assert [s.value for s in detector.detect("Name3 Name99")] == ["Name3", "Name99"]

    def test_reregistering_changes_the_type(self):
        detector = registered(10, "Name{}")
        detector.detect("Name1")
        detector.add("Name1", "ORG")
        assert detector.detect("Name1") == [
            Span(0, 5, "Name1", "ORG", "manual", ManualDetector.PRIORITY)
        ]

    def test_recursion_error_falls_back(self, monkeypatch):
        def deep(values):
            raise RecursionError

        monkeypatch.setattr(manual_module, "LiteralIndex", deep)
        detector = registered(10, "Name{}")
        assert [s.value for s in detector.detect("Name3, Name4")] == ["Name3", "Name4"]

    def test_value_added_while_the_index_is_built(self, monkeypatch):
        detector = registered(20)
        real = manual_module.LiteralIndex

        def racing(values):
            index = real(values)
            detector.add("Anna Example", "PERSON")  # lands after the snapshot
            return index

        monkeypatch.setattr(manual_module, "LiteralIndex", racing)
        detector.detect("x")
        monkeypatch.setattr(manual_module, "LiteralIndex", real)
        assert [s.value for s in detector.detect("Hi Anna Example")] == ["Anna Example"]

    def test_shallow_copy_shares_the_values(self):
        original = registered(20)
        original.detect("warm the index")
        clone = copy.copy(original)
        clone.add("Anna Example", "PERSON")
        assert [s.value for s in original.detect("Anna Example")] == ["Anna Example"]

    def test_pickle_leaves_the_index_out(self):
        detector = registered(20)
        detector.detect("warm the index")
        data = pickle.dumps(detector)
        assert b"_search" not in data
        assert [s.value for s in pickle.loads(data).detect("Name3 Example")] == [
            "Name3 Example"
        ]

    def test_state_from_before_the_index(self):
        # What unpickling a v0.2 detector hands __setstate__: no _index.
        detector = registered(20)
        state = {"_entities": dict(detector._entities)}
        clone = ManualDetector.__new__(ManualDetector)
        clone.__dict__.update(state)
        assert [s.value for s in clone.detect("Name3 Example")] == ["Name3 Example"]


class TestPerformance:
    def test_many_entities(self):
        detector = registered(5000, "Person{0} Surname{0}")
        text = " ".join(
            f"Person{i % 5000} Surname{i % 5000} wrote." for i in range(40_000)
        )
        start = time.perf_counter()
        assert len(detector.detect(text)) == 40_000
        assert time.perf_counter() - start < 1.5

    def test_long_shared_prefix_repeated(self):
        base = "x" * 5000
        detector = registered(200, base + "{}")
        start = time.perf_counter()
        assert detector.detect((base + "y") * 100) == []
        assert time.perf_counter() - start < 1.0
