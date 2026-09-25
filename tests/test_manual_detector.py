import unicodedata

import pytest

from veil.detectors import Detector, ManualDetector
from veil.types import Span


@pytest.fixture
def detector():
    return ManualDetector()


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
