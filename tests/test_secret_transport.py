"""Bounded fictional form, cookie, and encoded credential-field regressions."""

import json
import time

import pytest

from test_gateway_server import FakeAPI, call
from veil import RegexDetector, Shield
from veil.gateway import Gateway, Settings, open_sessions, prepare_data_dir

FORM_TYPE = "Content-Type: application/x-www-form-urlencoded"
ENCODED = "ZmljdGlvbmFsLWJpcmNoLTYxMw=="


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("name", ["password", "pass%77ord", "%70ASSWORD"])
@pytest.mark.parametrize(
    "value", ["fictional%20birch%26phrase", "fictional+birch==613"]
)
def test_explicit_form_body_masks_only_credential_values(newline, name, value):
    source = newline.join(
        [
            "POST /login HTTP/1.1",
            FORM_TYPE + "; charset=UTF-8",
            "X-Request-Mode: fictional",
            "",
            f"username=demo&{name}={value}&remember=false",
        ]
    )
    shield = Shield()
    masked = shield.mask(source)
    assert masked.text == source.replace(value, "[PASSWORD_1]")
    assert shield.restore(masked.text).text == source


def test_form_parameters_repeat_and_preserve_empty_and_reference_values():
    source = (
        FORM_TYPE + "\n\n"
        "password=&password=fictional-birch-73&password=${PASSWORD}"
        "&api%5Fkey=fictional-alder-25&redirect=home"
    )
    shield = Shield()
    masked = shield.mask(source)
    assert masked.text == source.replace("fictional-birch-73", "[PASSWORD_1]").replace(
        "fictional-alder-25", "[API_KEY_1]"
    )
    assert shield.restore(masked.text).text == source


@pytest.mark.parametrize(
    "prefix", ["", "Content-Type: text/plain\n\n", FORM_TYPE + "\n"]
)
def test_form_boundary_rule_requires_type_and_blank_header_separator(prefix):
    # Generic assignment masking remains conservative without form framing;
    # the new rule must not reinterpret arbitrary shell/prose ampersands.
    source = prefix + "password=fictional-birch-73&next=home"
    shield = Shield()
    assert shield.mask(source).text == prefix + "password=[PASSWORD_1]"


@pytest.mark.parametrize(
    "name", ["tenant_session", "team-sessionid", "portal.auth", "org_session_id"]
)
@pytest.mark.parametrize("header", ["Cookie", "Set-Cookie"])
def test_namespaced_session_auth_cookies_are_explicit_values(name, header):
    source = f"{header}: __Host-{name}=fictional-birch-72; theme=light; Secure\r\n"
    shield = Shield()
    masked = shield.mask(source)
    assert masked.text == source.replace("fictional-birch-72", "[TOKEN_1]")
    assert shield.restore(masked.text).text == source


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("tenant_password", "PASSWORD"),
        ("api_key", "API_KEY"),
        ("org_access_token", "TOKEN"),
    ],
)
def test_cookie_credentials_keep_semantic_type_and_neighboring_attributes(name, kind):
    source = f'Cookie: {name}="fictional-birch-72"; feature_access=enabled\n'
    shield = Shield()
    masked = shield.mask(source)
    assert masked.text == source.replace("fictional-birch-72", f"[{kind}_1]")
    assert shield.restore(masked.text).text == source


@pytest.mark.parametrize(
    "source",
    [
        "Cookie: theme=light; preferences=compact; "
        "access_level=read; feature_access=enabled\n",
        "Cookie: tenant_access=fictional-unknown-73; account_id=demo\n",
        "Cookie: session_length=30; auth_enabled=true; nexttoken=page2\n",
        "Cookie: team_session=${SESSION_ID}\n",
        "team_session=fictional-birch-72\n",
        'api_key_payload = settings.api_key_payload\npassword_payload = "plain text"',
        "client = Client(api_key=api_key, password=settings.database_password)",
    ],
)
def test_unqualified_cookies_and_code_references_remain_readable(source):
    assert Shield().mask(source).text == source


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("password_payload", "PASSWORD"),
        ("API_KEY_PAYLOAD", "API_KEY"),
        ("clientSecretPayload", "API_KEY"),
        ("refresh_token.payload", "TOKEN"),
        (r"pass\u0077ord_payload", "PASSWORD"),
        ("private-key-payload", "PRIVATE_KEY"),
    ],
)
@pytest.mark.parametrize("encoding", ["base64", "base64url"])
def test_encoded_credential_fields_have_explicit_naming_and_encoding(
    name, kind, encoding
):
    source = (
        '{"encoding":"'
        + encoding
        + '","'
        + name
        + '":\n "'
        + ENCODED
        + '","retries":3}'
    )
    shield = Shield()
    masked = shield.mask(source)
    assert masked.text == source.replace(ENCODED, f"[{kind}_1]")
    assert json.loads(masked.text)["retries"] == 3
    assert shield.restore(masked.text).text == source


@pytest.mark.parametrize(
    "value",
    [
        "YWJjZA==",  # Minimum supported padded spelling: eight characters.
        "YWJjZGVm",  # Eight characters with no padding.
        "YWJjZGVmZw",  # Unpadded spelling.
        "__9maWN0aW9uYWw=",  # URL alphabet.
        r"\/\/9maWN0aW9uYWw=",  # JSON-escaped slash spelling.
        r"\u005amljdGlvbmFsLWJpcmNoLTYxMw==",  # Escaped alphabet character.
    ],
)
def test_encoded_values_preserve_the_original_escaped_source(value):
    source = '{"encoding":"base64","password_payload":"' + value + '"}'
    shield = Shield()
    masked = shield.mask(source)
    assert masked.text == source.replace(value, "[PASSWORD_1]")
    assert shield.restore(masked.text).text == source


@pytest.mark.parametrize(
    "value",
    [
        "YQ==",
        "YWJj",
        "abcde",
        "abcdabcd=",
        "abcd=abc",
        "abcdabcd===",
        "ordinary text",
        "${PASSWORD}",
    ],
)
def test_short_malformed_or_reference_payloads_do_not_get_a_new_automatic_mask(value):
    source = json.dumps({"encoding": "base64", "password_payload": value})
    assert Shield().mask(source).text == source


@pytest.mark.parametrize(
    "source",
    [
        '{"encoding":"base64","image_payload":"' + ENCODED + '"}',
        '{"encoding":"hex","password_payload":"' + ENCODED + '"}',
        '{"password_payload":"' + ENCODED + '"}',
        '{"encoding":"base64","password_payload":12345678}',
        '{"encoding":"base64","password_payload":"unterminated',
        '{"encoding":"base64","password_payload_length":24}',
    ],
)
def test_encoding_marker_alone_does_not_classify_unrelated_or_nonstring_fields(source):
    assert Shield().mask(source).text == source


def test_long_unterminated_http_header_runs_do_not_rescan_quadratically():
    text = (FORM_TYPE + "\n") * 12000
    started = time.monotonic()
    assert not RegexDetector().detect(text)
    assert time.monotonic() - started < 5


def test_long_unterminated_content_type_parameters_remain_bounded():
    # Avoid overlapping trailing-space scans between header parameters and a
    # missing newline. This is a different shape from repeated header lines.
    text = FORM_TYPE + "; charset=UTF-8" + " " * 50000
    started = time.monotonic()
    assert not RegexDetector().detect(text)
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("api", ["anthropic", "openai"])
@pytest.mark.parametrize("review", [False, True])
def test_new_transport_rules_match_real_gateway_egress_and_restoration(
    tmp_path, api, review
):
    source = (
        FORM_TYPE + "\r\n\r\npassword=fictional%20birch%26phrase&remember=false\r\n"
        "Cookie: tenant_session=fictional-session-613; feature_access=enabled\r\n"
        '{"encoding":"base64","password_payload":"' + ENCODED + '"}'
    )
    route = "/v1/responses" if api == "openai" else "/v1/messages"
    headers = {
        "thread-id" if api == "openai" else "x-claude-code-session-id": "transport"
    }
    body = (
        {"model": "test", "input": source}
        if api == "openai"
        else {
            "model": "test",
            "max_tokens": 20,
            "messages": [{"role": "user", "content": source}],
        }
    )
    upstream = FakeAPI()
    reply = {"output": []} if api == "openai" else {"content": []}
    upstream.replies.append((200, "application/json", [json.dumps(reply).encode()]))
    try:
        with (
            open_sessions(
                prepare_data_dir(tmp_path / "data"),
                Settings(identity=False, note=False, secret_review=review),
                {},
                api=api,
            ) as sessions,
            Gateway(sessions, api=api, upstream=upstream.host, secure=False) as gateway,
        ):
            response, _ = call(gateway, "POST", route, body, headers=headers)
            assert response.status == 200
            sent = json.loads(upstream.received[-1][3])
            masked = (
                sent["input"] if api == "openai" else sent["messages"][0]["content"]
            )
            assert "fictional%20birch%26phrase" not in masked
            assert "fictional-session-613" not in masked
            assert ENCODED not in masked
            assert "&remember=false" in masked
            assert "feature_access=enabled" in masked
            assert sessions.get("transport").shield.restore(masked).text == source
    finally:
        upstream.close()
