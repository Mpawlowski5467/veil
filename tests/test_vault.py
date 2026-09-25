import pytest

from veil.vault import MemoryVault, Vault


@pytest.fixture
def vault():
    return MemoryVault()


def test_satisfies_protocol(vault):
    assert isinstance(vault, Vault)


def test_starts_empty(vault):
    assert len(vault) == 0
    assert vault.items() == []


def test_first_value_gets_number_one(vault):
    assert vault.get_or_create("jan.n@example.com", "EMAIL") == "[EMAIL_1]"


def test_numbering_is_per_type(vault):
    assert vault.get_or_create("a@example.com", "EMAIL") == "[EMAIL_1]"
    assert vault.get_or_create("555-123-4567", "PHONE") == "[PHONE_1]"
    assert vault.get_or_create("b@example.com", "EMAIL") == "[EMAIL_2]"
    assert vault.get_or_create("555-987-6543", "PHONE") == "[PHONE_2]"


def test_same_value_same_placeholder(vault):
    first = vault.get_or_create("a@example.com", "EMAIL")
    second = vault.get_or_create("a@example.com", "EMAIL")
    assert first == second == "[EMAIL_1]"
    assert len(vault) == 1


def test_first_type_wins_for_a_value(vault):
    assert vault.get_or_create("12345", "ORDER") == "[ORDER_1]"
    assert vault.get_or_create("12345", "ZIP") == "[ORDER_1]"
    assert vault.get_or_create("67890", "ZIP") == "[ZIP_1]"


def test_values_are_case_sensitive(vault):
    assert vault.get_or_create("Jan Nowak", "PERSON") == "[PERSON_1]"
    assert vault.get_or_create("jan nowak", "PERSON") == "[PERSON_2]"


def test_lookups_both_ways(vault):
    placeholder = vault.get_or_create("Jan Nowak", "PERSON")
    assert vault.get_placeholder("Jan Nowak") == placeholder
    assert vault.get_value(placeholder) == "Jan Nowak"


def test_unknown_lookups_return_none(vault):
    assert vault.get_placeholder("nobody@example.com") is None
    assert vault.get_value("[PERSON_1]") is None


def test_items_in_creation_order(vault):
    vault.get_or_create("b@example.com", "EMAIL")
    vault.get_or_create("Jan Nowak", "PERSON")
    vault.get_or_create("a@example.com", "EMAIL")
    assert vault.items() == [
        ("[EMAIL_1]", "b@example.com"),
        ("[PERSON_1]", "Jan Nowak"),
        ("[EMAIL_2]", "a@example.com"),
    ]


def test_items_is_a_copy(vault):
    vault.get_or_create("a@example.com", "EMAIL")
    vault.items().clear()
    assert len(vault) == 1


def test_clear_forgets_mappings_and_restarts_numbering(vault):
    vault.get_or_create("a@example.com", "EMAIL")
    vault.get_or_create("b@example.com", "EMAIL")
    vault.clear()
    assert len(vault) == 0
    assert vault.get_value("[EMAIL_1]") is None
    assert vault.get_or_create("c@example.com", "EMAIL") == "[EMAIL_1]"


def test_ten_or_more_per_type(vault):
    placeholders = [
        vault.get_or_create(f"user{i}@example.com", "EMAIL") for i in range(12)
    ]
    assert placeholders[0] == "[EMAIL_1]"
    assert placeholders[9] == "[EMAIL_10]"
    assert vault.get_value("[EMAIL_1]") == "user0@example.com"
    assert vault.get_value("[EMAIL_10]") == "user9@example.com"


@pytest.mark.parametrize("value", ["", None, 123])
def test_rejects_non_string_or_empty_values(vault, value):
    with pytest.raises(ValueError, match="non-empty"):
        vault.get_or_create(value, "EMAIL")


def test_rejects_invalid_entity_type(vault):
    with pytest.raises(ValueError, match="Invalid entity type"):
        vault.get_or_create("a@example.com", "email")
    assert len(vault) == 0


def test_invalid_type_ignored_for_known_value(vault):
    # The type is only used when creating, so a known value never raises.
    vault.get_or_create("a@example.com", "EMAIL")
    assert vault.get_or_create("a@example.com", "not valid") == "[EMAIL_1]"


def test_repr_hides_values(vault):
    vault.get_or_create("jan.n@example.com", "EMAIL")
    assert "jan" not in repr(vault)
    assert "1 values" in repr(vault)


def test_empty_vault_is_falsy_but_still_a_vault(vault):
    # Guards against `vault or MemoryVault()` style bugs in callers.
    assert not vault
    assert isinstance(vault, Vault)
