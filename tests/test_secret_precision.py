"""New positive/negative examples, independent of the frozen leak corpus."""

import pytest

from veil import Shield
from veil.detectors._secrets import code_value
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
        # Types, echoes of the field name, and credential-named identifiers.
        "password=password",
        "pub struct Config { pub api_key: String }",
        "client = Client(api_key=api_key, password=password)",
        "token: Option<String>,",
        "secret: Secret<String>",
        "password: Vec<u8>",
        "password: SecretStr",
        "api_key: &str,",
        "password: &mut String",
        "password: &'a str",
        "val password: String?",
        "token: *string",
        "password: std::string",
        "    POSTGRES_PASSWORD: postgres",
        "- MYSQL_PASSWORD=mysql",
        "api_key = API_KEY",
        "self.password = password",
        "connect(password=db_password)",
        "api_key=args.api_key",
        "token: AuthToken",
        "password: Password",
        # The same, after a type annotation or before a comment or more words.
        "password: str = password",
        "api_key: Final = API_KEY",
        "password: str = settings.db_password",
        "POSTGRES_PASSWORD: postgres # default",
        "password = user.password or default",
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
    "source",
    [
        "PASSWORD=letmein",
        "PASSWORD=secret",
        "password=pass",
        "api_key=token",
        "api_key=fictional94",
        'POSTGRES_PASSWORD: "postgres"',
    ],
)
def test_weak_bare_secrets_are_not_mistaken_for_references(source):
    shield = Shield()
    masked = shield.mask(source)
    assert len(masked.entities) == 1
    assert shield.restore(masked.text).text == source


@pytest.mark.parametrize(
    ("value", "name", "expected"),
    [
        ("String", None, True),
        ("Option", None, True),
        ("&str", None, True),
        ("api_key", None, True),
        ("password", None, True),
        ("postgres", "POSTGRES_PASSWORD", True),
        ("API_KEY", "api_key", True),
        ("db_password", "password", True),
        ("letmein", "PASSWORD", False),
        ("secret", "PASSWORD", False),
        ("hunter2", "password", False),
        ("postgres", None, False),
        ("fictional-orchard-42", "api_key", False),
        # Without a field name, a compound identifier may be a quoted secret.
        ("Admin_Password", None, False),
    ],
)
def test_code_value(value, name, expected):
    assert code_value(value, name) is expected


def test_ambiguous_explanatory_prose_still_needs_a_decision():
    # This may be an explanation OR someone's literal multiword passphrase.
    source = "The password is stored in the operating system keychain."
    assert Shield().mask(source).text == source
    assert candidates(source)[0].kind == "PASSWORD"
