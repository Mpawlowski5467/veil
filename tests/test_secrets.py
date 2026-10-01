"""Fictional coding secrets: detection, code lookalikes, and gateway round trips."""

import base64
import json
import pickle
import time

import pytest

from test_gateway_server import FakeAPI, call
from veil import RegexDetector, Shield, Span
from veil.detectors.base import Detector
from veil.gateway import Gateway, Settings, UnsupportedRequestError, open_sessions

# Assemble obvious synthetic payloads; no credentials or live provider calls.
OPENAI = "sk-proj-" + "A1b2" * 10
ANTHROPIC = "sk-ant-api03-" + "C3d4" * 15
GITHUB = "ghp_" + "E5f6" * 9
PASSWORD = 'fictional \\"quoted\\" phrase 🦊'
PEM = "-----BEGIN PRIVATE KEY-----\n" + "not-a-real-key\n" + "-----END PRIVATE KEY-----"


def b64(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


JWT = (
    b64({"alg": "HS256", "typ": "JWT"})
    + "."
    + b64({"sub": "fictional"})
    + "."
    + "xYz0" * 8
)


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (OPENAI, "API_KEY"),
        (ANTHROPIC, "API_KEY"),
        ("sk-" + "X1y2" * 12, "API_KEY"),
        ("sk-svcacct-" + "Z3y4" * 20, "API_KEY"),
        ("AIza" + "a" * 35, "API_KEY"),
        ("AKIA" + "A" * 16, "API_KEY"),
        ("ASIA" + "B" * 16, "API_KEY"),
        ("sk_live_" + "A1b2" * 6, "API_KEY"),
        ("rk_test_" + "C3d4" * 6, "API_KEY"),
        (GITHUB, "TOKEN"),
        ("github_pat_" + "A1_b2" * 10, "TOKEN"),
        ("glpat-" + "A1-b2" * 5, "TOKEN"),
        ("xoxb-" + "A1-b2" * 5, "TOKEN"),
        ("npm_" + "A1b2" * 9, "TOKEN"),
        ("hf_" + "A1b2" * 9, "TOKEN"),
        (JWT, "TOKEN"),
        (PEM, "PRIVATE_KEY"),
    ],
)
def test_standalone_formats_round_trip(value, kind):
    shield = Shield()
    text = f"Fictional sample:\n{value}\nEnd."
    masked = shield.mask(text)
    assert masked.text == f"Fictional sample:\n[{kind}_1]\nEnd."
    assert not masked.warnings
    assert shield.restore(masked.text).text == text
    assert "".join(shield.restore_stream(list(masked.text))) == text


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("API_KEY", "API_KEY"),
        ("OPENAI_API_KEY", "API_KEY"),
        ("apiKey", "API_KEY"),
        ("x-api-key", "API_KEY"),
        ("AWS_SECRET_ACCESS_KEY", "API_KEY"),
        ("aws_access_key_id", "API_KEY"),
        ("client_secret", "API_KEY"),
        ("signing_secret", "API_KEY"),
        ("secretKey", "API_KEY"),
        ("SECRET", "API_KEY"),
        ("password", "PASSWORD"),
        ("DB_PASSWORD", "PASSWORD"),
        ("mysql_pwd", "PASSWORD"),
        ("dbPass", "PASSWORD"),
        ("passphrase", "PASSWORD"),
        ("passwd", "PASSWORD"),
        ("access_token", "TOKEN"),
        ("refreshToken", "TOKEN"),
        ("session_token", "TOKEN"),
        ("token", "TOKEN"),
        ("PRIVATE_KEY", "PRIVATE_KEY"),
    ],
)
@pytest.mark.parametrize(
    "layout", ["{name}={value}", '"{name}": "{value}"', "'{name}': '{value}'"]
)
def test_named_literals_preserve_surrounding_code(name, kind, layout):
    text = layout.format(name=name, value="fictional-value-42")
    shield = Shield()
    masked = shield.mask(text)
    assert masked.text == text.replace("fictional-value-42", f"[{kind}_1]")
    assert shield.restore(masked.text).text == text


@pytest.mark.parametrize("value", ["x", "1234", "weak", "two words", "a\nb", PASSWORD])
def test_json_escaped_password_and_parsed_field_are_lossless(value):
    source = json.dumps({"password": value}, ensure_ascii=False)
    shield = Shield()
    masked = shield.mask(source)
    assert json.loads(masked.text) == {"password": "[PASSWORD_1]"}
    assert shield.restore(masked.text).text == source
    parsed = Shield()
    result = parsed.mask(value, field_name="password")
    assert result.text == "[PASSWORD_1]"
    assert parsed.restore(result.text).text == value


@pytest.mark.parametrize(
    "text",
    [
        "password = os.environ['PASSWORD']",
        "api_key = process.env.API_KEY",
        'api_key = getenv("API_KEY")',
        "password = config['password']",
        "password == supplied",
        "password: str",
        "token: int",
        "api_key: string",
        "max_tokens=8192",
        "token_count=42",
        "password_length=12",
        "api_key_file=/tmp/config",
        "secret_name=example",
        "password = None",
        "password = null",
        "password = # a comment",
        "API_KEY=$OPENAI_API_KEY",
        "API_KEY=${OPENAI_API_KEY}",
        "API_KEY={{ secrets.OPENAI_API_KEY }}",
        "API_KEY=%OPENAI_API_KEY%",
        'API_KEY="${OPENAI_API_KEY}"',
        "API_KEY=<your-key>",
        'password=""',
        "password=' '",
        "key=keyboard",
        "value=" + "a" * 64,
        "Basic authentication",
        "Bearer tokens",
        "a.b.c",
        "eyJnotjson.e30.signature",
        "-----BEGIN PUBLIC KEY-----\nfictional\n-----END PUBLIC KEY-----",
        "sk-short",
        "ghp_short",
        "password: [PASSWORD_1]",
    ],
)
def test_code_and_references_are_not_secret_values(text):
    assert not [s for s in RegexDetector().detect(text) if s.source == "secret"]


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "Basic"])
def test_authentication_values_inside_text(scheme):
    value = base64.b64encode(b"fictional:password").decode()
    text = f'curl -H "Authorization: {scheme} {value}" https://example.com'
    shield = Shield()
    masked = shield.mask(text)
    assert masked.text == text.replace(value, "[TOKEN_1]")
    assert shield.restore(masked.text).text == text


@pytest.mark.parametrize(
    "scheme", ["postgresql", "mysql", "mongodb+srv", "rediss", "amqp", "https"]
)
def test_url_userinfo_is_masked_without_changing_host_or_path(scheme):
    text = f"{scheme}://fictional:p%40ssword@example.com:5432/db?ssl=true"
    shield = Shield()
    masked = shield.mask(text)
    assert "fictional:p%40ssword" not in masked.text
    assert "[CREDENTIAL_1]" in masked.text
    assert shield.restore(masked.text).text == text


@pytest.mark.parametrize("kind", ["RSA ", "EC ", "DSA ", "OPENSSH ", "ENCRYPTED ", ""])
def test_pem_block_and_truncated_block(kind):
    text = (
        f"-----BEGIN {kind}PRIVATE KEY-----\nfictional\n-----END {kind}PRIVATE KEY-----"
    )
    for value in (text, text.partition("-----END")[0]):
        shield = Shield()
        assert shield.mask(value).text == "[PRIVATE_KEY_1]"
        assert shield.restore("[PRIVATE_KEY_1]").text == value


def test_custom_types_replace_secret_rules_including_field_context():
    detector = RegexDetector({"PASSWORD": r"CUSTOM-[a-z]+"})
    assert not detector.detect_field("fictional", "password")
    assert [
        (s.value, s.entity_type) for s in detector.detect('password="weak" CUSTOM-test')
    ] == [("CUSTOM-test", "PASSWORD")]
    assert not RegexDetector(include_builtins=False).detect(OPENAI)
    assert not RegexDetector(include_builtins=False).detect_field("weak", "password")


def test_explicit_registration_catches_an_unlabelled_unknown_secret():
    shield = Shield()
    shield.add_entity("fictional-unusual-secret", "TOKEN")
    assert shield.mask("fictional-unusual-secret").text == "[TOKEN_1]"


def test_field_context_preserves_legacy_detector_protocol_and_pickling():
    class LegacyDetector:
        def detect(self, text):
            return []

    assert isinstance(LegacyDetector(), Detector)
    assert (
        Shield(detectors=[LegacyDetector()]).mask("weak", field_name="password").text
        == "weak"
    )
    shield = pickle.loads(pickle.dumps(Shield()))
    assert shield.mask("weak", field_name="password").text == "[PASSWORD_1]"
    with pytest.raises(TypeError, match="field_name"):
        shield.mask("weak", field_name=123)


def test_field_detection_does_not_mutate_a_detectors_cached_list():
    class CachedDetector:
        def __init__(self):
            self.spans = []

        def detect(self, text):
            return self.spans

        def detect_field(self, text, name):
            return [Span(0, len(text), text, "PASSWORD")]

    detector = CachedDetector()
    result = Shield(detectors=[detector]).mask("fictional", field_name="password")
    assert result.text == "[PASSWORD_1]"
    assert detector.spans == []


def test_large_coding_text_and_unterminated_quotes_complete_promptly():
    text = "DB_PASSWORD=fictional-value\nmax_tokens=1024\n" * 10000
    detector = RegexDetector()
    start = time.monotonic()
    spans = detector.detect(text)
    assert len([s for s in spans if s.entity_type == "PASSWORD"]) == 10000
    assert time.monotonic() - start < 5
    text = 'password="' + "a=password=" * 20000
    start = time.monotonic()
    assert any(s.end == len(text) for s in detector.detect(text))
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("value", ["correct.horse", "abc,def", "abc#def", "abc;def"])
def test_bare_password_punctuation_does_not_leave_a_fragment(value):
    shield = Shield()
    source = f"DB_PASSWORD={value}\n"
    assert shield.mask(source).text == "DB_PASSWORD=[PASSWORD_1]\n"
    assert shield.restore("[PASSWORD_1]").text == value


def test_overlapping_bearer_and_prefix_rules_report_one_span():
    spans = RegexDetector().detect(f"Bearer {GITHUB}")
    assert len(spans) == 1
    assert spans[0].value == GITHUB


def test_url_without_username_and_adjacent_email():
    shield = Shield()
    source = "redis://:fictional@example.com/0 contact person@example.org"
    masked = shield.mask(source)
    assert masked.text == "redis://[CREDENTIAL_1]@example.com/0 contact [EMAIL_1]"
    assert shield.restore(masked.text).text == source


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_secret_placeholders_and_history_remain_distinct(tmp_path, api):
    with open_sessions(
        tmp_path, Settings(identity=False, note=False), {}, api=api
    ) as sessions:
        session = sessions.get("literal")
        source = f"{OPENAI}; template [API_KEY_1]"
        result = session.shield.mask(source)
        assert result.text == "[API_KEY_1]; template [LITERAL_1]"
        assert session.shield.restore(result.text).text == source
        session.ledger.record_text(source, result.text)
        assert session.masker._reply_text(source) == result.text


def test_cli_keeps_secret_mappings_for_later_restore(tmp_path):
    import subprocess
    import sys

    command = [sys.executable, "-m", "veil", "--data-dir", str(tmp_path / "data")]
    original = 'DB_PASSWORD="fictional passphrase"'
    masked = subprocess.run(
        [*command, "mask", "--session", "coding"],
        input=original,
        capture_output=True,
        text=True,
        check=True,
    )
    assert 'DB_PASSWORD="[PASSWORD_1]"' in masked.stdout
    restored = subprocess.run(
        [*command, "restore", "--session", "coding"],
        input=masked.stdout,
        capture_output=True,
        text=True,
        check=True,
    )
    assert restored.stdout.strip() == original
    assert "fictional passphrase" not in masked.stderr + restored.stderr


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_parsed_tool_input_preserves_credential_field_context(tmp_path, api):
    values = {
        "password": PASSWORD,
        "api_key": "fictional-short-key",
        "access_key_id": "fictional-id",
    }
    item = (
        {
            "type": "function_call",
            "name": "local",
            "call_id": "c1",
            "arguments": json.dumps(values),
        }
        if api == "openai"
        else {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "c1", "name": "local", "input": values}
            ],
        }
    )
    body = {"input" if api == "openai" else "messages": [item]}
    with open_sessions(
        tmp_path, Settings(identity=False, note=False), {}, api=api
    ) as sessions:
        session = sessions.get("credentials")
        # A prior context-free cache entry must not bypass field detection.
        session.masker._text(PASSWORD)
        masked = session.masker.mask(body)
        actual = (
            json.loads(masked["input"][0]["arguments"])
            if api == "openai"
            else masked["messages"][0]["content"][0]["input"]
        )
        assert actual == {
            "password": "[PASSWORD_1]",
            "api_key": "[API_KEY_1]",
            "access_key_id": "[API_KEY_2]",
        }
    with open_sessions(
        tmp_path, Settings(identity=False, note=False), {}, api=api
    ) as sessions:
        assert (
            sessions.get("credentials").shield.restore("[PASSWORD_1]").text == PASSWORD
        )


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_wire_round_trip_masks_body_and_keeps_provider_auth(tmp_path, api):
    fake = FakeAPI()
    placeholder = "[API_KEY_1]"
    reply = (
        {
            "output": [
                {
                    "type": "message",
                    "id": "m1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": placeholder, "annotations": []}
                    ],
                }
            ]
        }
        if api == "openai"
        else {"content": [{"type": "text", "text": placeholder}]}
    )
    fake.replies.append((200, "application/json", [json.dumps(reply).encode()]))
    settings = Settings(identity=False, note=False)
    try:
        with (
            open_sessions(tmp_path, settings, {}, api=api) as sessions,
            Gateway(sessions, api=api, upstream=fake.host, secure=False) as gateway,
        ):
            body = (
                {"input": OPENAI}
                if api == "openai"
                else {"messages": [{"role": "user", "content": OPENAI}]}
            )
            response, payload = call(
                gateway,
                path="/v1/responses" if api == "openai" else "/v1/messages",
                headers={"thread-id": "secret-test"},
                body=body,
            )
            assert response.status == 200
            assert OPENAI in payload.decode()
            request = fake.received[0]
            assert OPENAI.encode() not in request[3]
            assert placeholder.encode() in request[3]
            assert request[2]["Authorization"] == "Bearer user-token"
    finally:
        fake.close()


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_numeric_credential_is_refused_without_changing_its_type(tmp_path, api):
    values = {"password": 1234}
    body = (
        {
            "input": [
                {
                    "type": "function_call",
                    "name": "local",
                    "call_id": "c1",
                    "arguments": json.dumps(values),
                }
            ]
        }
        if api == "openai"
        else {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "c1",
                            "name": "local",
                            "input": values,
                        }
                    ],
                }
            ]
        }
    )
    with open_sessions(
        tmp_path, Settings(identity=False, note=False), {}, api=api
    ) as sessions:
        with pytest.raises(UnsupportedRequestError) as error:
            sessions.get("numeric").masker.mask(body)
        assert "1234" not in str(error.value)


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_context_cache_cannot_replay_unmasked_secret_or_collide_with_literal_json(
    tmp_path, api
):
    with open_sessions(
        tmp_path, Settings(identity=False, note=False), {}, api=api
    ) as sessions:
        masker = sessions.get("cache").masker
        assert masker._text("plain-secret") == "plain-secret"
        lookalike = json.dumps(["password", "plain-secret"])
        assert masker._text(lookalike) == lookalike
        assert masker._text("plain-secret", field_name="password") == "[PASSWORD_1]"
        assert masker._text("plain-secret") == "[PASSWORD_1]"
        assert "plain-secret" not in masker._text(lookalike)
