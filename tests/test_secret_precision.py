"""New positive/negative examples, independent of the frozen leak corpus."""

import pytest

from veil import Shield
from veil.secret_review import candidates


@pytest.mark.parametrize(
    "source",
    [
        "password = settings.database_password",
        "api_key = config.api_key;",
        "client_secret: this.client_secret,",
        "password = self.credentials.password",
        "const apiKey = settings.service.apiKey;",
        "The password must contain at least twelve characters.",
        "Store API keys in a password manager.",
        "Never paste your access token into a public issue.",
    ],
)
def test_code_references_and_ordinary_instructions_remain_readable(source):
    shield = Shield()
    masked = shield.mask(source)
    assert masked.text == source
    assert not candidates(source)
    assert shield.restore(masked.text).text == source


@pytest.mark.parametrize(
    "value",
    [
        "settings.database_password",
        "config.api_key",
        "this.client_secret",
        "self.credentials.password",
        "settings.service.apiKey",
        "password",
        "letmein",
    ],
)
def test_quoted_reference_lookalikes_remain_protected(value):
    source = f'password = "{value}"'
    shield = Shield()
    masked = shield.mask(source)
    assert masked.text == 'password = "[PASSWORD_1]"'
    assert shield.restore(masked.text).text == source


@pytest.mark.parametrize(
    "source", ["password=password", "PASSWORD=letmein", "api_key=fictional94"]
)
def test_weak_bare_secrets_are_not_mistaken_for_references(source):
    shield = Shield()
    masked = shield.mask(source)
    assert len(masked.entities) == 1
    assert shield.restore(masked.text).text == source


def test_ambiguous_explanatory_prose_still_needs_a_decision():
    # This may be an explanation OR someone's literal multiword passphrase.
    source = "The password is stored in the operating system keychain."
    assert Shield().mask(source).text == source
    assert candidates(source)[0].kind == "PASSWORD"
