import dataclasses

import pytest

from veil.placeholders import (
    PLACEHOLDER_RE,
    format_placeholder,
    validate_entity_type,
)
from veil.types import MaskedEntity, MaskResult, RestoreResult, ShieldWarning, Span


class TestSpan:
    def test_valid_span(self):
        span = Span(start=6, end=23, value="jan.n@example.com", entity_type="EMAIL")
        assert len(span) == 17
        assert span.source == "regex"
        assert span.priority == 0

    def test_is_frozen(self):
        span = Span(0, 3, "abc", "X")
        with pytest.raises(dataclasses.FrozenInstanceError):
            span.start = 1  # type: ignore[misc]

    @pytest.mark.parametrize(("start", "end"), [(-1, 2), (3, 3), (5, 2)])
    def test_rejects_bad_offsets(self, start, end):
        with pytest.raises(ValueError, match="start < end"):
            Span(start, end, "ab", "X")

    def test_rejects_value_length_mismatch(self):
        with pytest.raises(ValueError, match="does not match"):
            Span(0, 5, "abc", "X")

    def test_equality_and_hashing(self):
        a = Span(0, 3, "abc", "X")
        assert a == Span(0, 3, "abc", "X")
        assert len({a, Span(0, 3, "abc", "X")}) == 1


class TestResults:
    def test_mask_result_defaults(self):
        result = MaskResult("hello")
        assert result.entities == []
        assert result.warnings == []

    def test_defaults_are_not_shared(self):
        assert MaskResult("a").warnings is not MaskResult("b").warnings
        assert RestoreResult("a").warnings is not RestoreResult("b").warnings

    def test_restore_result_defaults(self):
        result = RestoreResult("hello")
        assert result.restored_count == 0
        assert result.warnings == []

    def test_masked_entity_fields(self):
        entity = MaskedEntity("[EMAIL_1]", "a@example.com", "EMAIL", 0, 13, "regex")
        assert entity.placeholder == "[EMAIL_1]"

    def test_shield_warning_is_user_warning(self):
        assert issubclass(ShieldWarning, UserWarning)


class TestEntityTypes:
    @pytest.mark.parametrize("name", ["PERSON", "EMAIL", "IPV4", "ORDER_ID", "X", "A1"])
    def test_valid(self, name):
        assert validate_entity_type(name) == name

    @pytest.mark.parametrize(
        "name",
        ["", "person", "Person", "1ABC", "_ABC", "ORDER-ID", "ORDER ID", "ÉMAIL"],
    )
    def test_invalid(self, name):
        with pytest.raises(ValueError, match="Invalid entity type"):
            validate_entity_type(name)

    def test_non_string_rejected(self):
        with pytest.raises(ValueError, match="Invalid entity type"):
            validate_entity_type(None)  # type: ignore[arg-type]


class TestPlaceholderFormat:
    def test_format(self):
        assert format_placeholder("PERSON", 1) == "[PERSON_1]"
        assert format_placeholder("ORDER_ID", 12) == "[ORDER_ID_12]"

    @pytest.mark.parametrize("number", [0, -1, True, 1.0, "1"])
    def test_rejects_bad_numbers(self, number):
        with pytest.raises(ValueError, match="number"):
            format_placeholder("PERSON", number)

    def test_rejects_bad_type(self):
        with pytest.raises(ValueError, match="Invalid entity type"):
            format_placeholder("person", 1)

    @pytest.mark.parametrize(
        ("placeholder", "entity_type", "number"),
        [
            ("[PERSON_1]", "PERSON", "1"),
            ("[PERSON_10]", "PERSON", "10"),
            ("[IPV4_3]", "IPV4", "3"),
            ("[ORDER_ID_12]", "ORDER_ID", "12"),
            ("[A_1_2]", "A_1", "2"),
        ],
    )
    def test_pattern_round_trips_format(self, placeholder, entity_type, number):
        match = PLACEHOLDER_RE.fullmatch(placeholder)
        assert match is not None
        assert match["type"] == entity_type
        assert match["number"] == number
        assert format_placeholder(entity_type, int(number)) == placeholder

    def test_pattern_does_not_match_prefix_of_longer_placeholder(self):
        matches = [m.group(0) for m in PLACEHOLDER_RE.finditer("[PERSON_10]")]
        assert matches == ["[PERSON_10]"]

    @pytest.mark.parametrize(
        "text", ["[person_1]", "PERSON_1", "[PERSON]", "[PERSON_]", "[_1]", "[1_1]"]
    )
    def test_pattern_rejects_non_placeholders(self, text):
        assert PLACEHOLDER_RE.search(text) is None
