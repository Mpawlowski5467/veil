import inspect
import re
import typing
import warnings

import pytest

import veil as package
from veil import (
    Detector,
    MaskedEntity,
    MaskResult,
    MemoryVault,
    RegexDetector,
    RestoreResult,
    Shield,
    ShieldWarning,
    Span,
    Vault,
)

README_INPUT = "Email Jan Nowak at jan.n@example.com about the invoice."
README_MASKED = "Email [PERSON_1] at [EMAIL_1] about the invoice."
README_REPLY = "Hi [PERSON_1], following up on the invoice..."
README_RESTORED = "Hi Jan Nowak, following up on the invoice..."


class RecordingLLM:
    """A fake model: records prompts and answers using their placeholders."""

    def __init__(self, reply=None):
        self.prompts = []
        self.reply = reply

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.reply is not None:
            return self.reply
        placeholders = re.findall(r"\[[A-Z][A-Z0-9_]*_\d+\]", prompt)
        return "Noted: " + ", ".join(placeholders)


class NameDetector:
    """A stand-in for a future NER detector."""

    def detect(self, text):
        return [
            Span(m.start(), m.end(), m.group(0), "PERSON", source="ner")
            for m in re.finditer(r"\bDr\. [A-Z][a-z]+\b", text)
        ]


@pytest.fixture
def shield():
    return Shield()


def test_type_hints_resolve_at_runtime():
    # Tools like pydantic and typer evaluate annotations at runtime.
    for obj in (Shield.__init__, Shield.mask, Shield.wrap, RegexDetector.__init__):
        typing.get_type_hints(obj)


def test_public_api_exports():
    for name in package.__all__:
        assert hasattr(package, name)
    assert package.__version__ == "0.1.0"


def test_readme_example(shield):
    shield.add_entity("Jan Nowak", "PERSON")
    masked = shield.mask(README_INPUT)
    assert isinstance(masked, MaskResult)
    assert masked.text == README_MASKED
    restored = shield.restore(README_REPLY)
    assert isinstance(restored, RestoreResult)
    assert restored.text == README_RESTORED
    assert restored.restored_count == 1
    assert restored.warnings == []


def test_default_detects_builtin_types(shield):
    text = "a@example.com, 555-123-4567, +44 20 7946 0958, 192.0.2.1"
    assert shield.mask(text).text == "[EMAIL_1], [PHONE_1], [PHONE_2], [IPV4_1]"


def test_custom_patterns():
    shield = Shield(custom_patterns={"ORDER": r"#\d{5}"})
    masked = shield.mask("Where is order #12345? Reply to a@example.com")
    assert masked.text == "Where is order [ORDER_1]? Reply to [EMAIL_1]"
    assert shield.restore("Order [ORDER_1] ships today.").text == (
        "Order #12345 ships today."
    )


def test_invalid_custom_pattern():
    with pytest.raises(ValueError, match="Invalid regex"):
        Shield(custom_patterns={"ORDER": "("})


def test_add_entity_masks_future_calls(shield):
    assert shield.mask("Jan Nowak").text == "Jan Nowak"
    shield.add_entity("Jan Nowak", "PERSON")
    assert shield.mask("Jan Nowak").text == "[PERSON_1]"


def test_add_entity_validates(shield):
    with pytest.raises(ValueError, match="non-blank"):
        shield.add_entity("  ", "PERSON")
    with pytest.raises(ValueError, match="Invalid entity type"):
        shield.add_entity("Jan Nowak", "person")


def test_manual_entity_wins_tie_with_custom_pattern():
    shield = Shield(custom_patterns={"ORDER": r"#\d{5}"})
    shield.add_entity("#12345", "CASE")
    assert shield.mask("#12345").text == "[CASE_1]"


class TestConsistency:
    def test_same_value_same_placeholder_across_calls(self, shield):
        shield.add_entity("Jan Nowak", "PERSON")
        first = shield.mask("Jan Nowak wrote from jan.n@example.com")
        second = shield.mask("Ask jan.n@example.com whether Jan Nowak agrees")
        assert first.text == "[PERSON_1] wrote from [EMAIL_1]"
        assert second.text == "Ask [EMAIL_1] whether [PERSON_1] agrees"

    def test_new_values_continue_numbering(self, shield):
        shield.mask("a@example.com")
        assert shield.mask("b@example.com").text == "[EMAIL_2]"

    def test_multi_turn_conversation(self, shield):
        shield.add_entity("Jan Nowak", "PERSON")
        shield.add_entity("Anna Kowalska", "PERSON")
        turn1 = shield.mask("Jan Nowak (jan.n@example.com) needs help.")
        reply1 = shield.restore("Sure, I'll contact [PERSON_1] at [EMAIL_1].")
        turn2 = shield.mask("Also loop in Anna Kowalska, and keep Jan Nowak on cc.")
        reply2 = shield.restore("Added [PERSON_2]; [PERSON_1] stays on cc.")
        assert turn1.text == "[PERSON_1] ([EMAIL_1]) needs help."
        assert reply1.text == "Sure, I'll contact Jan Nowak at jan.n@example.com."
        assert turn2.text == "Also loop in [PERSON_2], and keep [PERSON_1] on cc."
        assert reply2.text == "Added Anna Kowalska; Jan Nowak stays on cc."

    def test_separate_shields_are_independent(self):
        a, b = Shield(), Shield()
        a.mask("first@example.com")
        assert b.mask("second@example.com").text == "[EMAIL_1]"
        assert b.restore("[EMAIL_1]").text == "second@example.com"


class TestPersonTen:
    def test_restore_person_1_vs_person_10(self, shield):
        names = [f"Person Name{chr(ord('A') + i)}" for i in range(12)]
        for name in names:
            shield.add_entity(name, "PERSON")
        masked = shield.mask(" / ".join(names))
        assert masked.text == " / ".join(f"[PERSON_{i}]" for i in range(1, 13))
        restored = shield.restore("[PERSON_1] [PERSON_10] [PERSON_11] [PERSON_1]")
        assert restored.text == "Person NameA Person NameJ Person NameK Person NameA"
        assert restored.restored_count == 4


class TestUnknownPlaceholders:
    def test_warns_instead_of_raising(self, shield):
        shield.mask("a@example.com")
        result = shield.restore("[EMAIL_1] and [EMAIL_9] and [PERSON_1]")
        assert result.text == "a@example.com and [EMAIL_9] and [PERSON_1]"
        assert result.restored_count == 1
        assert result.warnings == [
            "Unknown placeholder [EMAIL_9] was left unchanged.",
            "Unknown placeholder [PERSON_1] was left unchanged.",
        ]


class TestReset:
    def test_reset_clears_vault(self, shield):
        shield.mask("a@example.com")
        shield.reset()
        assert len(shield.vault) == 0
        result = shield.restore("[EMAIL_1]")
        assert result.text == "[EMAIL_1]"
        assert result.warnings == ["Unknown placeholder [EMAIL_1] was left unchanged."]

    def test_reset_restarts_numbering(self, shield):
        shield.mask("a@example.com b@example.com")
        shield.reset()
        assert shield.mask("b@example.com").text == "[EMAIL_1]"

    def test_reset_keeps_entities_and_patterns(self):
        shield = Shield(custom_patterns={"ORDER": r"#\d{5}"})
        shield.add_entity("Jan Nowak", "PERSON")
        shield.mask("Jan Nowak, #12345")
        shield.reset()
        assert shield.mask("#12345 for Jan Nowak").text == "[ORDER_1] for [PERSON_1]"


class TestEdgeCases:
    def test_empty_string(self, shield):
        shield.add_entity("Jan Nowak", "PERSON")
        assert shield.mask("") == MaskResult("")
        assert shield.restore("") == RestoreResult("")

    def test_text_without_pii(self, shield):
        text = "Summarize our Q3 roadmap in three bullet points, version 2.0."
        masked = shield.mask(text)
        assert masked.text == text
        assert masked.entities == []
        assert masked.warnings == []
        assert shield.restore(text) == RestoreResult(text)

    def test_mask_entities(self, shield):
        masked = shield.mask("Mail a@example.com")
        assert masked.entities == [
            MaskedEntity("[EMAIL_1]", "a@example.com", "EMAIL", 5, 18, "regex")
        ]


class TestWrap:
    def test_round_trip(self, shield):
        shield.add_entity("Jan Nowak", "PERSON")
        llm = RecordingLLM()
        safe_llm = shield.wrap(llm)
        answer = safe_llm(README_INPUT)
        assert llm.prompts == [README_MASKED]
        assert answer == "Noted: Jan Nowak, jan.n@example.com"

    def test_model_never_sees_real_values(self, shield):
        shield.add_entity("Jan Nowak", "PERSON")
        llm = RecordingLLM(reply=README_REPLY)
        safe_llm = shield.wrap(llm)
        assert safe_llm(README_INPUT) == README_RESTORED
        [prompt] = llm.prompts
        for secret in ("Jan Nowak", "jan.n@example.com"):
            assert secret not in prompt

    def test_multi_turn_through_wrap(self, shield):
        shield.add_entity("Jan Nowak", "PERSON")
        llm = RecordingLLM()
        safe_llm = shield.wrap(llm)
        safe_llm("Hi, I'm Jan Nowak.")
        answer = safe_llm("My email is jan.n@example.com; tell Jan Nowak.")
        assert llm.prompts == [
            "Hi, I'm [PERSON_1].",
            "My email is [EMAIL_1]; tell [PERSON_1].",
        ]
        assert answer == "Noted: jan.n@example.com, Jan Nowak"

    def test_accepts_lambda(self, shield):
        safe_llm = shield.wrap(lambda prompt: prompt.upper())
        # Upper-casing leaves placeholders intact, so restore still works.
        assert safe_llm("mail a@example.com") == "MAIL a@example.com"

    def test_preserves_function_metadata(self, shield):
        def ask_model(prompt: str) -> str:
            """Ask the model."""
            return prompt

        safe_llm = shield.wrap(ask_model)
        assert safe_llm.__name__ == "ask_model"
        assert safe_llm.__doc__ == "Ask the model."
        assert safe_llm.__wrapped__ is ask_model  # type: ignore[attr-defined]

    def test_signature_is_the_wrappers_own(self, shield):
        @shield.wrap
        def ask(prompt: str) -> str:
            return prompt

        assert str(inspect.signature(ask)) == "(text: str, /) -> str"
        with pytest.raises(TypeError):
            ask(prompt="hi")  # type: ignore[call-arg]

    def test_unknown_placeholder_in_reply_warns(self, shield):
        safe_llm = shield.wrap(RecordingLLM(reply="Hello [PERSON_3]"))
        with pytest.warns(ShieldWarning, match=r"Unknown placeholder \[PERSON_3\]"):
            answer = safe_llm("hi")
        assert answer == "Hello [PERSON_3]"

    def test_leak_warning_is_emitted(self, shield):
        shield.mask("Call 555-123-4567")
        safe_llm = shield.wrap(RecordingLLM(reply="ok"))
        with pytest.warns(ShieldWarning, match="Leak check") as record:
            safe_llm("Call 555-123-4567-2")
        assert record[0].filename == __file__

    def test_warnings_can_fail_closed(self, shield):
        shield.mask("Call 555-123-4567")
        llm = RecordingLLM(reply="ok")
        safe_llm = shield.wrap(llm)
        with warnings.catch_warnings():
            warnings.simplefilter("error", ShieldWarning)
            with pytest.raises(ShieldWarning, match="Leak check"):
                safe_llm("Call 555-123-4567-2")
        assert llm.prompts == []  # the model was never called

    def test_no_warnings_on_clean_round_trip(self, shield):
        safe_llm = shield.wrap(RecordingLLM())
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert safe_llm("mail a@example.com") == "Noted: a@example.com"

    def test_non_string_reply_raises(self, shield):
        safe_llm = shield.wrap(lambda prompt: None)
        with pytest.raises(TypeError, match="returned NoneType, expected str"):
            safe_llm("hi")

    def test_model_errors_propagate(self, shield):
        def broken(prompt):
            raise RuntimeError("model is down")

        with pytest.raises(RuntimeError, match="model is down"):
            shield.wrap(broken)("hi")

    def test_empty_input(self, shield):
        llm = RecordingLLM(reply="")
        assert shield.wrap(llm)("") == ""
        assert llm.prompts == [""]


class TestPluggableParts:
    def test_custom_detector(self):
        shield = Shield(detectors=[NameDetector()])
        assert shield.mask("Ask Dr. Nowak").text == "Ask [PERSON_1]"
        # The default RegexDetector is replaced, not added to.
        assert shield.mask("a@example.com").text == "a@example.com"

    def test_manual_entities_work_with_custom_detectors(self):
        shield = Shield(detectors=[NameDetector(), RegexDetector()])
        shield.add_entity("Anna Kowalska", "PERSON")
        masked = shield.mask("Dr. Nowak and Anna Kowalska, a@example.com")
        assert masked.text == "[PERSON_1] and [PERSON_2], [EMAIL_1]"

    def test_detectors_and_custom_patterns_conflict(self):
        with pytest.raises(ValueError, match="custom_patterns configures"):
            Shield(detectors=[RegexDetector()], custom_patterns={"ORDER": r"#\d+"})

    def test_detectors_from_a_generator(self):
        # Validating a one-shot iterator used to consume it, silently turning
        # off every detector.
        shield = Shield(detectors=(d for d in [RegexDetector()]))
        assert shield.mask("Mail a@example.com").text == "Mail [EMAIL_1]"
        shield = Shield(detectors=iter([RegexDetector()]))
        assert shield.mask("Mail a@example.com").text == "Mail [EMAIL_1]"

    def test_single_detector_not_in_a_list(self):
        with pytest.raises(TypeError, match="sequence of detectors"):
            Shield(detectors=RegexDetector())  # type: ignore[arg-type]

    def test_object_without_detect(self):
        with pytest.raises(TypeError, match="does not implement detect"):
            Shield(detectors=[object()])  # type: ignore[list-item]

    def test_empty_detector_list_uses_manual_only(self):
        shield = Shield(detectors=[])
        shield.add_entity("Jan Nowak", "PERSON")
        assert (
            shield.mask("Jan Nowak, a@example.com").text == "[PERSON_1], a@example.com"
        )

    def test_custom_vault_is_used_even_when_empty(self):
        vault = MemoryVault()
        shield = Shield(vault=vault)
        assert shield.vault is vault
        shield.mask("a@example.com")
        assert vault.get_value("[EMAIL_1]") == "a@example.com"

    def test_shared_vault_across_shields(self):
        vault = MemoryVault()
        Shield(vault=vault).mask("a@example.com")
        assert Shield(vault=vault).restore("[EMAIL_1]").text == "a@example.com"

    def test_invalid_vault(self):
        with pytest.raises(TypeError, match="does not implement Vault"):
            Shield(vault={})  # type: ignore[arg-type]

    def test_default_vault_type(self, shield):
        assert isinstance(shield.vault, MemoryVault)
        assert isinstance(shield.vault, Vault)

    def test_detector_protocol_is_structural(self):
        assert isinstance(NameDetector(), Detector)
