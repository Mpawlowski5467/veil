"""Fictional credentials in typed declarations, Go assignments and clause values."""

import json
import time

import pytest

from veil import RegexDetector, Shield
from veil.gateway import Settings, open_sessions
from veil.secret_review import candidates

K = "fictional-orchard-42"


def body(api, text):
    if api == "openai":
        return {"model": "test", "input": text}
    return {
        "model": "test",
        "max_tokens": 30,
        "messages": [{"role": "user", "content": text}],
    }


def tool_result(api, text):
    if api == "openai":
        return {
            "model": "test",
            "input": [
                {
                    "type": "function_call",
                    "name": "Read",
                    "call_id": "c1",
                    "arguments": json.dumps({"file_path": "config.txt"}),
                },
                {"type": "function_call_output", "call_id": "c1", "output": text},
            ],
        }
    return {
        "model": "test",
        "max_tokens": 30,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "Read",
                        "input": {"file_path": "config.txt"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_01", "content": text}
                ],
            },
        ],
    }


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        # Python
        (f'API_KEY: Final = "{K}"', "API_KEY"),
        (f'DB_PASSWORD: str = "{K}"', "PASSWORD"),
        (f"password: str = '{K}'", "PASSWORD"),
        (f'password: Optional[str] = "{K}"', "PASSWORD"),
        (f'token: str | None = "{K}"', "TOKEN"),
        (f'api_key: ClassVar[str] = "{K}"', "API_KEY"),
        (f'secret_token: dict[str, str] = "{K}"', "TOKEN"),
        (f'    password: str = "{K}"  # dataclass default', "PASSWORD"),
        (f'password: str="{K}"', "PASSWORD"),
        (f'def login(password: str = "{K}"):', "PASSWORD"),
        (f'def connect(user: str, token: str = "{K}"):', "TOKEN"),
        (f'if (token := "{K}"):', "TOKEN"),
        # TypeScript
        (f'const password: string = "{K}";', "PASSWORD"),
        (f'let apiKey: string = "{K}"', "API_KEY"),
        (f'private readonly apiKey: string = "{K}";', "API_KEY"),
        (f'const token: string | undefined = "{K}";', "TOKEN"),
        # Go
        (f'apiKey := "{K}"', "API_KEY"),
        (f'dbPassword := "{K}"', "PASSWORD"),
        (f'var apiKey string = "{K}"', "API_KEY"),
        (f'const apiKey string = "{K}"', "API_KEY"),
        (f'\tapiKey string = "{K}"', "API_KEY"),
        (f'var apiKey = "{K}"', "API_KEY"),
        (f'apiKey :="{K}"', "API_KEY"),
        # Kotlin and Swift
        (f'val apiKey: String = "{K}"', "API_KEY"),
        (f'var password: String? = "{K}"', "PASSWORD"),
        (f'let apiKey: String = "{K}"', "API_KEY"),
        (f'private let apiKey: String = "{K}"', "API_KEY"),
        # Rust
        (f'let api_key: &str = "{K}";', "API_KEY"),
        (f'const API_KEY: &str = "{K}";', "API_KEY"),
        (f'static API_KEY: &\'static str = "{K}";', "API_KEY"),
        # Pascal
        (f"password : String := '{K}';", "PASSWORD"),
        (f"Password := '{K}';", "PASSWORD"),
        (f"var Password: string = '{K}';", "PASSWORD"),
        (f"const Password = '{K}';", "PASSWORD"),
        # Diff lines
        (f'-API_KEY: Final = "{K}"', "API_KEY"),
        (f'+\tapiKey string = "{K}"', "API_KEY"),
        # Forms that already worked
        (f'"password": "{K}"', "PASSWORD"),
        (f"DB_PASSWORD={K}", "PASSWORD"),
        (f'export DB_PASSWORD="{K}"', "PASSWORD"),
        (f"    export DB_PASSWORD={K}", "PASSWORD"),
    ],
)
def test_typed_declarations_mask_only_the_value(source, kind):
    shield = Shield()
    masked = shield.mask(source).text
    assert masked == source.replace(K, f"[{kind}_1]")
    assert K not in masked
    assert shield.restore(masked).text == source


def test_concatenation_after_annotation():
    shield = Shield()
    source = 'password: str = "fict-" + "orchard-42"'
    masked = shield.mask(source).text
    assert masked == 'password: str = "[PASSWORD_1]" + "[PASSWORD_2]"'
    assert shield.restore(masked).text == source


def test_bare_value_after_annotation_masks_type_and_value():
    shield = Shield()
    source = "password: str = hunter2"
    masked = shield.mask(source).text
    assert masked == "password: [PASSWORD_1]"
    assert shield.restore(masked).text == source


@pytest.mark.parametrize(
    "source",
    [
        "password: str",
        "token: str | None",
        "password: str = None",
        "password: Optional[str] = None",
        'api_key: str = os.environ["API_KEY"]',
        "password: str = settings.db_password",
        "def login(password: str) -> None:",
        "def login(password: str = None):",
        "if password == x:",
        "interface A { password: string; token: string }",
        "password: string;",
        "const password: string = process.env.PASSWORD;",
        "let token: string | undefined;",
        "function f(password: string, token?: string) {}",
        'apiKey := os.Getenv("API_KEY")',
        "var apiKey string",
        'if password == "" {',
        "token, err := getToken()",
        "if (password := getpass()):",
        f'let api_key: String = String::from("{K}");',
        "password string == x",
        f'var key []byte = []byte("{K}")',
        # Parameter lists are not one annotation.
        'def connect(password: str, user: str = "jan"):',
        "def f(token: str, retries: int = 3):",
        'def f(password: str, *, mode: str = "fast"):',
        "function f(password: string, retries: number = 3) {}",
        "password: str = ;",
    ],
)
def test_type_only_and_reference_declarations_stay_readable(source):
    assert Shield().mask(source).text == source
    assert [s for s in RegexDetector().detect(source) if s.source == "secret"] == []
    assert not candidates(source)


GO_AND_TYPED = {
    'apiKey := "fictionalGoKey0123"': "fictionalGoKey0123",
    'API_KEY: Final = "Fict-Meadow-42"': "Fict-Meadow-42",
    'DB_PASSWORD: str = "fict-river-77"': "fict-river-77",
    'const token: string = "fict-harbor-19";': "fict-harbor-19",
    'val apiKey: String = "fict-lantern-55"': "fict-lantern-55",
    'let api_key: &str = "fict-quarry-31";': "fict-quarry-31",
    'var dbPassword string = "fict-meadow-63"': "fict-meadow-63",
}


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_go_and_typed_lines_in_a_tool_result_never_reach_the_provider(tmp_path, api):
    text = "\n".join(GO_AND_TYPED)
    with open_sessions(
        tmp_path, Settings(identity=False, note=False), {}, api=api
    ) as sessions:
        sent = json.dumps(sessions.get("s").masker.mask(tool_result(api, text)))
    for value in GO_AND_TYPED.values():
        assert value not in sent
    for visible in (
        "apiKey := ",
        "API_KEY: Final = ",
        "DB_PASSWORD: str = ",
        "const token: string = ",
        "val apiKey: String = ",
        "let api_key: &str = ",
        "var dbPassword string = ",
    ):
        assert visible in sent


@pytest.mark.parametrize("api", ["anthropic", "openai"])
@pytest.mark.parametrize("source", [f'API_KEY: Final = "{K}"', f'apiKey := "{K}"'])
def test_review_does_not_ask_about_masked_typed_declarations(tmp_path, api, source):
    with open_sessions(
        tmp_path, Settings(identity=False, note=False, secret_review=True), {}, api=api
    ) as sessions:
        masker = sessions.get("s").masker
        assert K not in json.dumps(masker.mask(body(api, source)))
        assert not masker.review_findings()


@pytest.mark.parametrize(
    ("source", "masked"),
    [
        ("Password: fictional orchard 72", "Password: [PASSWORD_1]"),
        ("Password: fictional orchard 72.", "Password: [PASSWORD_1]."),
        ("password: fictional orchard 72", "password: [PASSWORD_1]"),
        ("  - password: fictional orchard 72", "  - password: [PASSWORD_1]"),
        ("password: fictional orchard 72 # old", "password: [PASSWORD_1] # old"),
        ("password = fictional orchard 72", "password = [PASSWORD_1]"),
        (
            "password: fict orchard, then sign in",
            "password: [PASSWORD_1], then sign in",
        ),
        (
            "Remember the password: it is in the vault",
            "Remember the password: [PASSWORD_1]",
        ),
        ("password = fict orchard or default", "password = [PASSWORD_1]"),
        ("token: Bearer fictional orchard", "token: [TOKEN_1]"),
        ("password: abc= def", "password: [PASSWORD_1]"),
        (
            "db:\n  password: fictional orchard 72\n  user: jan\n",
            "db:\n  password: [PASSWORD_1]\n  user: jan\n",
        ),
    ],
)
def test_unquoted_clause_is_masked_whole(source, masked):
    shield = Shield()
    assert shield.mask(source).text == masked
    assert shield.restore(masked).text == source


@pytest.mark.parametrize(
    ("source", "masked"),
    [
        (
            'curl -H "X-Api-Key: fict-key-123" https://api.example.com/v1',
            'curl -H "X-Api-Key: [API_KEY_1]" https://api.example.com/v1',
        ),
        (
            "curl -H 'X-Auth-Token: fict-key-123' https://api.example.com/v1",
            "curl -H 'X-Auth-Token: [TOKEN_1]' https://api.example.com/v1",
        ),
        (
            'echo "password: fict orchard 72" >> notes.txt',
            'echo "password: [PASSWORD_1]" >> notes.txt',
        ),
        (
            "echo 'Password: don't panic 42' >> notes.txt",
            "echo 'Password: [PASSWORD_1]' >> notes.txt",
        ),
        ('log("token: fict orchard 72", level)', 'log("token: [TOKEN_1]", level)'),
    ],
)
def test_label_that_opens_a_string_ends_with_it(source, masked):
    shield = Shield()
    assert shield.mask(source).text == masked
    assert shield.restore(masked).text == source


@pytest.mark.parametrize(
    ("source", "masked"),
    [
        ("password: hunter2", "password: [PASSWORD_1]"),
        ("password: abc=def", "password: [PASSWORD_1]"),
        ("password = hunter2 # note", "password = [PASSWORD_1] # note"),
        ("password = hunter2  // note", "password = [PASSWORD_1]  // note"),
        ("f(password = pw)", "f(password = [PASSWORD_1])"),
        ('"token": 123, "user": "x"', '"token": [TOKEN_1], "user": "x"'),
        ("DB_PASSWORD=fict-42 uv run app.py", "DB_PASSWORD=[PASSWORD_1] uv run app.py"),
        ("password=fict user=jan", "password=[PASSWORD_1] user=jan"),
        # A compact NAME=value reads one shell word; quote multiword values.
        ("PASSWORD=fictional orchard 72", "PASSWORD=[PASSWORD_1] orchard 72"),
        # A first word that ends a sentence ends the value.
        ("password: fict. orchard 72", "password: [PASSWORD_1] orchard 72"),
    ],
)
def test_single_word_forms_are_unchanged(source, masked):
    shield = Shield()
    assert shield.mask(source).text == masked
    assert shield.restore(masked).text == source


@pytest.mark.parametrize("api", ["anthropic", "openai"])
@pytest.mark.parametrize(
    "source",
    ["Password: fictional orchard 72.", "password: fictional orchard 72 # old"],
)
def test_clause_values_do_not_ask_again(tmp_path, api, source):
    with open_sessions(
        tmp_path, Settings(identity=False, note=False, secret_review=True), {}, api=api
    ) as sessions:
        masker = sessions.get("s").masker
        assert "orchard" not in json.dumps(masker.mask(body(api, source)))
        assert not masker.review_findings()


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("PASSWORD=fictional orchard 72", "fictional orchard 72"),
        ("password: fict. orchard 72", "fict. orchard 72"),
    ],
)
def test_compact_multiword_and_sentence_cut_still_ask(source, value):
    assert [(c.value, c.kind) for c in candidates(source)] == [(value, "PASSWORD")]


@pytest.mark.parametrize(
    "text",
    [
        "password: " + "a " * 50000,
        ("password: " + "A" * 120 + " ") * 2000,
        ('"password: ' + "A" * 120 + " x ") * 2000,
        "password=" + "a=password=" * 20000,
    ],
)
def test_long_clause_stays_bounded(text):
    start = time.monotonic()
    Shield().mask(text)
    candidates(text)
    assert time.monotonic() - start < 5
