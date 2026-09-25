import pytest

from veil.restorer import Restorer
from veil.vault import MemoryVault


@pytest.fixture
def vault():
    return MemoryVault()


@pytest.fixture
def restorer(vault):
    return Restorer(vault)


def test_readme_example(vault, restorer):
    vault.get_or_create("Jan Nowak", "PERSON")
    vault.get_or_create("jan.n@example.com", "EMAIL")
    result = restorer.restore("Hi [PERSON_1], following up on the invoice...")
    assert result.text == "Hi Jan Nowak, following up on the invoice..."
    assert result.restored_count == 1
    assert result.warnings == []


def test_every_occurrence_is_restored_and_counted(vault, restorer):
    vault.get_or_create("a@example.com", "EMAIL")
    vault.get_or_create("555-123-4567", "PHONE")
    result = restorer.restore("[EMAIL_1] / [PHONE_1] / [EMAIL_1]")
    assert result.text == "a@example.com / 555-123-4567 / a@example.com"
    assert result.restored_count == 3


def test_person_1_vs_person_10(vault, restorer):
    for i in range(1, 11):
        vault.get_or_create(f"Person Number{i}", "PERSON")
    result = restorer.restore("[PERSON_10] and [PERSON_1] and [PERSON_10][PERSON_1]")
    assert result.text == (
        "Person Number10 and Person Number1 and Person Number10Person Number1"
    )
    assert result.restored_count == 4


def test_person_1_does_not_match_inside_unknown_person_10(vault, restorer):
    vault.get_or_create("Jan Nowak", "PERSON")
    result = restorer.restore("[PERSON_10] vs [PERSON_1]")
    assert result.text == "[PERSON_10] vs Jan Nowak"
    assert result.restored_count == 1
    assert result.warnings == ["Unknown placeholder [PERSON_10] was left unchanged."]


def test_unknown_placeholders_are_kept_and_reported_once(vault, restorer):
    vault.get_or_create("Jan Nowak", "PERSON")
    result = restorer.restore("[PERSON_1], [PERSON_2], [ORDER_7], [PERSON_2]")
    assert result.text == "Jan Nowak, [PERSON_2], [ORDER_7], [PERSON_2]"
    assert result.restored_count == 1
    assert result.warnings == [
        "Unknown placeholder [PERSON_2] was left unchanged.",
        "Unknown placeholder [ORDER_7] was left unchanged.",
    ]


def test_unknown_placeholders_with_empty_vault_do_not_raise(restorer):
    result = restorer.restore("Hello [PERSON_1]")
    assert result.text == "Hello [PERSON_1]"
    assert result.restored_count == 0
    assert result.warnings == ["Unknown placeholder [PERSON_1] was left unchanged."]


def test_leading_zero_is_a_different_placeholder(vault, restorer):
    vault.get_or_create("Jan Nowak", "PERSON")
    result = restorer.restore("[PERSON_01]")
    assert result.text == "[PERSON_01]"
    assert result.warnings == ["Unknown placeholder [PERSON_01] was left unchanged."]


@pytest.mark.parametrize(
    "text",
    ["[person_1]", "PERSON_1", "[PERSON 1]", "(PERSON_1)", "[PERSON_1", "PERSON_1]"],
)
def test_near_misses_are_not_placeholders(vault, restorer, text):
    # Fuzzy matching is out of scope for v0.1: these are neither restored nor
    # reported.
    vault.get_or_create("Jan Nowak", "PERSON")
    result = restorer.restore(text)
    assert result.text == text
    assert result.restored_count == 0
    assert result.warnings == []


def test_single_pass_does_not_rescan_restored_values(vault, restorer):
    vault.get_or_create("Jan Nowak", "PERSON")
    vault.get_or_create("literally [PERSON_1]", "NOTE")
    result = restorer.restore("[NOTE_1]")
    assert result.text == "literally [PERSON_1]"
    assert result.restored_count == 1


def test_placeholders_inside_other_text(vault, restorer):
    vault.get_or_create("a@example.com", "EMAIL")
    result = restorer.restore("mailto:[EMAIL_1]?subject=hi")
    assert result.text == "mailto:a@example.com?subject=hi"


def test_type_names_with_underscores_and_digits(vault, restorer):
    vault.get_or_create("#12345", "ORDER_ID")
    vault.get_or_create("192.0.2.1", "IPV4")
    result = restorer.restore("[ORDER_ID_1] from [IPV4_1]")
    assert result.text == "#12345 from 192.0.2.1"


def test_empty_string(restorer):
    result = restorer.restore("")
    assert result.text == ""
    assert result.restored_count == 0
    assert result.warnings == []


def test_text_without_placeholders(vault, restorer):
    vault.get_or_create("Jan Nowak", "PERSON")
    text = "No placeholders here, just [brackets] and [A_B] and [1]."
    result = restorer.restore(text)
    assert result.text == text
    assert result.restored_count == 0
    assert result.warnings == []


def test_reflects_vault_changes(vault, restorer):
    assert restorer.restore("[EMAIL_1]").restored_count == 0
    vault.get_or_create("a@example.com", "EMAIL")
    assert restorer.restore("[EMAIL_1]").text == "a@example.com"


def test_non_string_input(restorer):
    with pytest.raises(TypeError, match="expects str"):
        restorer.restore(None)  # type: ignore[arg-type]
