import pytest

from veil.detectors import Detector, ManualDetector
from veil.detectors.manual import literal_pattern
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
    ["January", "Janet", "DeJan", "Jan_", "Jan2", "JanNowak"],
)
def test_does_not_match_inside_words(detector, text):
    detector.add("Jan", "PERSON")
    assert detector.detect(text) == []


@pytest.mark.parametrize(
    "text",
    ["Jan", "Jan's", "(Jan)", "Jan, hi", "hi Jan.", "Jan-Nowak", '"Jan"', "@Jan"],
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


class TestLiteralPattern:
    def test_word_edges_get_boundaries(self):
        pattern = literal_pattern("Jan")
        assert pattern.search("January") is None
        assert pattern.search("Jan.") is not None

    def test_only_word_edges_get_boundaries(self):
        pattern = literal_pattern("Jan!")
        assert pattern.search("xJan!") is None  # starts with a word char
        assert pattern.search("Jan!!") is not None  # ends with a non-word char

    def test_case_sensitive(self):
        assert literal_pattern("Jan").search("jan") is None
