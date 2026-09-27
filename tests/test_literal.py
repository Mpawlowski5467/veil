"""Masking text that already looks like a placeholder, and exact-only restore."""

import random

import pytest

from veil import (
    LiteralPlaceholderDetector,
    ManualDetector,
    MemoryVault,
    RegexDetector,
    Shield,
)


def literal_shield(types=("EMAIL", "PHONE", "PERSON")):
    shield = Shield(detectors=[LiteralPlaceholderDetector(types), RegexDetector()])
    shield.add_entity("Jan Nowak", "PERSON")
    return shield


class TestDetector:
    def test_reports_placeholders_of_the_given_types(self):
        detector = LiteralPlaceholderDetector({"EMAIL"})
        spans = detector.detect("[EMAIL_1] [PHONE_2] row[COL_1] [LITERAL_3]")
        assert [(s.value, s.entity_type, s.source) for s in spans] == [
            ("[EMAIL_1]", "LITERAL", "literal"),
            ("[LITERAL_3]", "LITERAL", "literal"),
        ]
        assert all(s.priority == LiteralPlaceholderDetector.PRIORITY for s in spans)

    def test_none_reports_every_exact_placeholder(self):
        spans = LiteralPlaceholderDetector().detect("[A_1] b[COL_2] [person 1]")
        assert [s.value for s in spans] == ["[A_1]", "[COL_2]"]

    def test_types_always_include_the_literal_type(self):
        assert LiteralPlaceholderDetector({"EMAIL"}).types == {"EMAIL", "LITERAL"}
        assert LiteralPlaceholderDetector(entity_type="QUOTED").types is None
        assert LiteralPlaceholderDetector((), entity_type="QUOTED").types == {"QUOTED"}

    def test_invalid_arguments(self):
        with pytest.raises(ValueError, match="Invalid entity type"):
            LiteralPlaceholderDetector({"email"})
        with pytest.raises(ValueError, match="Invalid entity type"):
            LiteralPlaceholderDetector(entity_type="literal")
        with pytest.raises(TypeError, match="not a str"):
            LiteralPlaceholderDetector("EMAIL")


class TestRoundTrip:
    TEXT = (
        "Template: Dear [EMAIL_1], call [PHONE_1]. Literal [LITERAL_1] too. "
        "Code arr[EMAIL_1] and row[COL_1]. Real: jane.doe@example.com, Jan Nowak."
    )

    def test_masks_and_restores_exactly(self):
        shield = literal_shield()
        result = shield.mask(self.TEXT)
        assert result.text == (
            "Template: Dear [LITERAL_1], call [LITERAL_2]. Literal [LITERAL_3] too. "
            "Code arr[LITERAL_1] and row[COL_1]. Real: [EMAIL_1], [PERSON_1]."
        )
        assert shield.restore(result.text).text == self.TEXT
        streamed = "".join(shield.restore_stream(list(result.text)))
        assert streamed == self.TEXT

    def test_only_unmasked_lookalikes_warn(self):
        result = literal_shield().mask(self.TEXT)
        assert result.warnings == [
            "Input already contains placeholder-like text [COL_1]; "
            "restore() will treat it as a placeholder."
        ]

    def test_masking_is_the_same_every_time(self):
        shield = literal_shield()
        first = shield.mask(self.TEXT).text
        assert shield.mask(self.TEXT).text == first
        # A new value later gets the next number; the literals keep theirs.
        later = shield.mask("New ada.q@example.com wrote [EMAIL_1].").text
        assert later == "New [EMAIL_2] wrote [LITERAL_1]."

    def test_a_restored_literal_is_never_restored_again(self):
        shield = literal_shield()
        masked = shield.mask("[LITERAL_1] then [EMAIL_1] then a@example.com").text
        assert masked == "[LITERAL_1] then [LITERAL_2] then [EMAIL_1]"
        assert shield.restore(masked).text == (
            "[LITERAL_1] then [EMAIL_1] then a@example.com"
        )

    def test_literal_wins_over_a_registered_lookalike(self):
        manual = ManualDetector()
        manual.add("[EMAIL_1]", "SECRET")
        shield = Shield(
            detectors=[manual, LiteralPlaceholderDetector({"EMAIL"})],
            vault=MemoryVault(),
        )
        assert shield.mask("x [EMAIL_1] y").text == "x [LITERAL_1] y"

    def test_random_documents_round_trip(self):
        rng = random.Random(11)
        pieces = [
            "[EMAIL_1]",
            "[EMAIL_2]",
            "[PERSON_1]",
            "[LITERAL_1]",
            "[LITERAL_4]",
            "[COL_1]",
            "arr[PHONE_1]",
            "jane.doe@example.com",
            "ada.q@example.com",
            "Jan Nowak",
            "[person 1]",
            " ",
            "x",
            "\n",
        ]
        for _ in range(500):
            shield = literal_shield()
            text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))
            masked = shield.mask(text).text
            assert shield.restore(masked, tolerant=False).text == text
            # No placeholder of a protected type is left as literal text.
            for kind in ("EMAIL", "PERSON", "PHONE"):
                for token in (f"[{kind}_{n}]" for n in range(1, 5)):
                    if token in masked:
                        assert shield.vault.get_value(token) is not None


class TestExactOnlyRestore:
    def make(self):
        shield = Shield()
        shield.add_entity("Jan Nowak", "PERSON")
        shield.mask("Jan Nowak")
        return shield

    def test_per_call_override(self):
        shield = self.make()
        text = "[person 1] and [PERSON_1]"
        assert shield.restore(text).text == "Jan Nowak and Jan Nowak"
        exact = shield.restore(text, tolerant=False)
        assert exact.text == "[person 1] and Jan Nowak"
        assert exact.repaired == []
        assert shield.restore(text).text == "Jan Nowak and Jan Nowak"  # unchanged

    def test_override_turns_tolerance_on(self):
        shield = Shield(tolerant_restore=False)
        shield.add_entity("Jan Nowak", "PERSON")
        shield.mask("Jan Nowak")
        assert shield.restore("[person 1]").text == "[person 1]"
        assert shield.restore("[person 1]", tolerant=True).text == "Jan Nowak"

    def test_streams_take_the_override(self):
        shield = self.make()
        stream = shield.stream_restorer(tolerant=False)
        assert stream.feed("[person 1] \\[PERS") == "[person 1] \\"
        assert stream.feed("ON_1]") == "Jan Nowak"
        assert stream.finish() == ""
        joined = "".join(shield.restore_stream(["[person", " 1]"], tolerant=False))
        assert joined == "[person 1]"

    def test_exact_streams_release_what_only_a_rewrite_could_match(self):
        shield = self.make()
        exact = shield.stream_restorer(tolerant=False)
        assert exact.feed("see [person 1") == "see [person 1"
        assert exact.feed(" 【PERSON_1") == " 【PERSON_1"
        tolerant = shield.stream_restorer()
        assert tolerant.feed("see [person 1") == "see "

    def test_override_must_be_a_bool(self):
        shield = self.make()
        with pytest.raises(TypeError, match="must be a bool"):
            shield.restore("x", tolerant="no")
        with pytest.raises(TypeError, match="must be a bool"):
            shield.stream_restorer(tolerant=1)
