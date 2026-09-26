import pytest

from veil.restorer import Restorer
from veil.types import RepairedPlaceholder
from veil.vault import MemoryVault

FULLWIDTH = ("\N{FULLWIDTH LEFT SQUARE BRACKET}", "\N{FULLWIDTH RIGHT SQUARE BRACKET}")
LENTICULAR = ("\N{LEFT BLACK LENTICULAR BRACKET}", "\N{RIGHT BLACK LENTICULAR BRACKET}")


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


def test_leading_zero_is_repaired(vault, restorer):
    vault.get_or_create("Jan Nowak", "PERSON")
    result = restorer.restore("[PERSON_01]")
    assert result.text == "Jan Nowak"
    assert result.warnings == []
    assert result.repaired == [RepairedPlaceholder("[PERSON_01]", "[PERSON_1]")]


def test_leading_zero_of_an_unknown_number_is_reported(vault, restorer):
    vault.get_or_create("Jan Nowak", "PERSON")
    result = restorer.restore("[PERSON_02]")
    assert result.text == "[PERSON_02]"
    assert result.warnings == ["Unknown placeholder [PERSON_02] was left unchanged."]


@pytest.mark.parametrize(
    "text", ["PERSON_1", "(PERSON_1)", "[PERSON_1", "PERSON_1]", "{PERSON_1}"]
)
def test_unbracketed_near_misses_are_not_placeholders(vault, restorer, text):
    # Only bracketed forms are repaired: these are neither restored nor
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


class TestTolerantRestore:
    @pytest.mark.parametrize(
        "written",
        [
            "[person_1]",
            "[Person_1]",
            "[PERSON 1]",
            "[PERSON-1]",
            "[ PERSON_1 ]",
            "[PERSON _ 1]",
            "[PERSON\t1]",
            "\\[PERSON_1\\]",  # Markdown-escaped
            f"{FULLWIDTH[0]}PERSON_1{FULLWIDTH[1]}",
            f"{LENTICULAR[0]}PERSON_1{LENTICULAR[1]}",
        ],
    )
    def test_rewritten_placeholders_are_restored(self, vault, restorer, written):
        vault.get_or_create("Jan Nowak", "PERSON")
        result = restorer.restore(f"Hi {written}!")
        assert result.text == "Hi Jan Nowak!"
        assert result.restored_count == 1
        assert result.warnings == []
        assert result.repaired == [RepairedPlaceholder(written, "[PERSON_1]")]

    def test_exact_placeholders_are_not_listed_as_repaired(self, vault, restorer):
        vault.get_or_create("Jan Nowak", "PERSON")
        assert restorer.restore("[PERSON_1]").repaired == []

    def test_types_with_digits_and_underscores(self, vault, restorer):
        vault.get_or_create("192.0.2.1", "IPV4")
        vault.get_or_create("#12345", "ORDER_ID")
        result = restorer.restore("[ipv4 1] [order id 1] [ipv41]")
        assert result.text == "192.0.2.1 #12345 192.0.2.1"
        assert [r.placeholder for r in result.repaired] == [
            "[IPV4_1]",
            "[ORDER_ID_1]",
            "[IPV4_1]",
        ]

    def test_person_10_is_not_read_as_person_1(self, vault, restorer):
        for i in range(1, 11):
            vault.get_or_create(f"Person Number{i}", "PERSON")
        result = restorer.restore("[person 10] [person 1]")
        assert result.text == "Person Number10 Person Number1"

    @pytest.mark.parametrize(
        "text", ["[Figure 2]", "[see note 1]", "[Step 3]", "[v2]", "[1]", "[PERSON]"]
    )
    def test_ordinary_bracketed_text_is_left_alone(self, vault, restorer, text):
        vault.get_or_create("Jan Nowak", "PERSON")
        result = restorer.restore(text)
        assert result.text == text
        assert result.warnings == []
        assert result.repaired == []

    def test_rewritten_unknown_number_of_a_known_type_is_reported(
        self, vault, restorer
    ):
        vault.get_or_create("Jan Nowak", "PERSON")
        result = restorer.restore("[person 3]")
        assert result.text == "[person 3]"
        assert result.warnings == ["Unknown placeholder [person 3] was left unchanged."]

    def test_brackets_around_a_placeholder_are_kept(self, vault, restorer):
        vault.get_or_create("Jan Nowak", "PERSON")
        assert restorer.restore("[[PERSON_1]]").text == "[Jan Nowak]"

    def test_single_pass_even_for_repairs(self, vault, restorer):
        vault.get_or_create("[person 2]", "NOTE")
        vault.get_or_create("Anna", "PERSON")
        result = restorer.restore("[note 1]")
        assert result.text == "[person 2]"
        assert result.restored_count == 1

    def test_can_be_turned_off(self, vault):
        vault.get_or_create("Jan Nowak", "PERSON")
        result = Restorer(vault, tolerant=False).restore("[person 1] [PERSON_1]")
        assert result.text == "[person 1] Jan Nowak"
        assert result.repaired == []
