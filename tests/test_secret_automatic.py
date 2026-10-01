"""Automatic credential syntax with unrelated examples and lossless restoration."""

import json

import pytest

from test_gateway_server import FakeAPI, call
from veil import RegexDetector, Shield
from veil.gateway import Gateway, Settings, open_sessions


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("My password is", "PASSWORD"),
        ("The passphrase was", "PASSWORD"),
        ("API key equals", "API_KEY"),
        ("client secret is", "API_KEY"),
        ("Refresh token was", "TOKEN"),
        ("access_token is", "TOKEN"),
        ("private key is", "PRIVATE_KEY"),
        ("Moje HASŁO jest", "PASSWORD"),
        ("Haslo to", "PASSWORD"),
        ("Mi CONTRASEÑA es", "PASSWORD"),
        ("La contrasena es", "PASSWORD"),
        ("Backup code:", "CREDENTIAL"),
    ],
)
@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_explicit_quoted_labels_mask_the_entire_value(label, kind, quote):
    value = "fictional żółty meadow 🦊 plus demo@example.org"
    source = f"{label} {quote}{value}{quote}. Help me update it."
    shield = Shield()
    masked = shield.mask(source).text
    assert masked == source.replace(value, f"[{kind}_1]")
    assert shield.restore(masked).text == source


@pytest.mark.parametrize("label", ["Password is", "Hasło to", "Contraseña es"])
@pytest.mark.parametrize("value", ["fikcyjny-meadow-94", "żółty_fiction_94", "abc94"])
@pytest.mark.parametrize("ending", ["", ". Next sentence.", "; next item", "\r\n"])
def test_structured_prose_tokens_require_a_clause_boundary(label, value, ending):
    source = f"{label} {value}{ending}"
    shield = Shield()
    masked = shield.mask(source).text
    assert masked == source.replace(value, "[PASSWORD_1]")
    assert shield.restore(masked).text == source


@pytest.mark.parametrize(
    "label", ["Backup code:", "Recovery code=", "One-time recovery code:"]
)
def test_recovery_code_masks_only_the_delimited_token(label):
    source = f"{label} meadow-fern-cobalt. Keep this for later."
    shield = Shield()
    masked = shield.mask(source).text
    assert masked == source.replace("meadow-fern-cobalt", "[CREDENTIAL_1]")
    assert shield.restore(masked).text == source


@pytest.mark.parametrize(
    ("option", "kind"),
    [
        ("password", "PASSWORD"),
        ("passwd", "PASSWORD"),
        ("passphrase", "PASSWORD"),
        ("api-key", "API_KEY"),
        ("secret", "API_KEY"),
        ("client-secret", "API_KEY"),
        ("token", "TOKEN"),
        ("access-token", "TOKEN"),
        ("refresh-token", "TOKEN"),
        ("private-key", "PRIVATE_KEY"),
    ],
)
@pytest.mark.parametrize("separator", [" ", "=", "\t"])
def test_shell_credential_options_preserve_neighboring_flags(option, kind, separator):
    source = f"client --{option}{separator}'fictional fern 94' --verbose; echo done"
    shield = Shield()
    masked = shield.mask(source).text
    assert masked == source.replace("fictional fern 94", f"[{kind}_1]")
    assert shield.restore(masked).text == source


@pytest.mark.parametrize(
    ("word", "value"),
    [
        ("fictional-fern-94", "fictional-fern-94"),
        (r"fictional\ fern\ 94", r"fictional\ fern\ 94"),
        (r"'fictional\fern\'", "fictional\\fern\\"),
        ('"fictional \\"fern\\" 94"', 'fictional \\"fern\\" 94'),
        ("'fictional'\"fern\"94", "'fictional'\"fern\"94"),
    ],
)
def test_shell_word_boundaries_include_escapes_and_adjacent_quoted_parts(word, value):
    source = f"client --password {word} --verbose"
    shield = Shield()
    masked = shield.mask(source).text
    assert masked == source.replace(value, "[PASSWORD_1]")
    assert shield.restore(masked).text == source


@pytest.mark.parametrize("marker", ["|", ">", "|-", ">+", "|2-", ">-2"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_yaml_blocks_end_at_dedent_and_keep_the_complete_raw_value(marker, newline):
    value = f"    fictional meadow{newline}{newline}    fern 94"
    source = f"database:{newline}  password: {marker} # private{newline}{value}"
    source += f"{newline}  retries: 3{newline}public: yes{newline}"
    shield = Shield()
    masked = shield.mask(source).text
    assert masked == source.replace(value, "[PASSWORD_1]")
    assert shield.restore(masked).text == source


def test_nested_yaml_list_with_two_different_credential_types():
    source = (
        "servers:\n  - 'password': |\n      fictional fern\n"
        '    "api_key": >\n      fictional meadow\n    port: 8080\n'
    )
    shield = Shield()
    masked = shield.mask(source).text
    assert masked == source.replace("      fictional fern", "[PASSWORD_1]").replace(
        "      fictional meadow", "[API_KEY_1]"
    )
    assert shield.restore(masked).text == source


@pytest.mark.parametrize(
    "source",
    [
        "The password is stored in the operating system keychain.",
        "The password is fictional-meadow followed by more words.",
        "Hasło to ustawienie aplikacji.",
        "La contraseña es necesaria para entrar.",
        "Recovery code: generated by the application.",
        "Password is 'unfinished fictional quote",
        "Use fictional-fern-94 to sign in.",  # ambiguous sign-in methods need review
        "client --password --verbose",
        "client --password-file /run/secrets/password",
        "client --password ${DATABASE_PASSWORD}",
        "client --password '$DATABASE_PASSWORD'",
        "client --password %DATABASE_PASSWORD%",
        'client --password "$(read-password)"',
        "client --password `read-password`",
        "client --password '[PASSWORD_1]'",
        "password: |\nnext: value\n",
        "password: |-\n    ${DATABASE_PASSWORD}\nnext: value\n",
        "description: |\n    fictional meadow\nnext: value\n",
    ],
)
def test_new_automatic_rules_leave_references_and_uncertainty_alone(source):
    assert Shield().mask(source).text == source


def test_custom_types_and_disabled_builtins_apply_to_new_syntax():
    source = "My password is 'fictional meadow 94'"
    assert not RegexDetector(include_builtins=False).detect(source)
    assert not RegexDetector({"PASSWORD": "ONLY_CUSTOM"}).detect(source)


def test_repeated_labels_and_long_nonmatching_tokens_stay_bounded():
    source = ("Password is " + "a_" * 2000 + " ordinary words\n") * 20
    assert Shield().mask(source).text == source


@pytest.mark.parametrize("api", ["anthropic", "openai"])
@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize(
    ("source", "value", "kind"),
    [
        ("My password is 'fictional fern 94'", "fictional fern 94", "PASSWORD"),
        ("client --api-key 'fictional fern 94'", "fictional fern 94", "API_KEY"),
        (
            "password: |\n  fictional fern 94\nnext: 3",
            "  fictional fern 94",
            "PASSWORD",
        ),
        ("Hasło to fikcyjny-fern-94.", "fikcyjny-fern-94", "PASSWORD"),
        ("Recovery code: fictional-fern-94.", "fictional-fern-94", "CREDENTIAL"),
    ],
)
def test_new_syntax_is_masked_before_the_http_provider_and_restored(
    tmp_path, api, review, source, value, kind
):
    upstream = FakeAPI()
    route = "/v1/responses" if api == "openai" else "/v1/messages"
    expected = source.replace(value, f"[{kind}_1]")
    reply = (
        {
            "output": [
                {
                    "type": "message",
                    "id": "msg_fictional",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": expected, "annotations": []}
                    ],
                }
            ]
        }
        if api == "openai"
        else {"content": [{"type": "text", "text": expected}]}
    )
    upstream.routes[route] = (200, "application/json", [json.dumps(reply).encode()])
    try:
        with (
            open_sessions(
                tmp_path,
                Settings(identity=False, note=False, secret_review=review),
                {},
                api=api,
            ) as sessions,
            Gateway(sessions, api=api, upstream=upstream.host, secure=False) as gateway,
        ):
            body = (
                {"model": "test", "input": source}
                if api == "openai"
                else {
                    "model": "test",
                    "max_tokens": 30,
                    "messages": [{"role": "user", "content": source}],
                }
            )
            response, restored = call(
                gateway,
                "POST",
                route,
                body,
                headers={
                    "thread-id"
                    if api == "openai"
                    else "x-claude-code-session-id": "automatic"
                },
            )
            assert response.status == 200
            assert len(upstream.received) == 1
            sent = json.loads(upstream.received[0][3])
            assert (
                sent["input"] if api == "openai" else sent["messages"][0]["content"]
            ) == expected
            decoded = json.loads(restored)
            assert (
                decoded["output"][0]["content"][0]["text"]
                if api == "openai"
                else decoded["content"][0]["text"]
            ) == source
            assert gateway.reviews.report() == {"reviews": []}
    finally:
        upstream.close()
