"""Credential formats and local privacy cues beyond the original leak corpus."""

import io
import json

import pytest

from test_gateway_server import FakeAPI, call
from test_leak_evaluation import leaks
from test_openai_gateway import reply_item
from veil import Shield, cli
from veil.gateway import Gateway, Settings, open_sessions, prepare_data_dir
from veil.secret_review import (
    CHOICES,
    REVIEW_TYPES,
    ReviewError,
    candidates,
    request_candidates,
)


@pytest.mark.parametrize(
    "name",
    [
        "sid",
        "SESSIONID",
        "__Host-session",
        "__Secure-auth_token",
        "connect.sid",
        "JSESSIONID",
    ],
)
@pytest.mark.parametrize("header", ["Cookie", "Set-Cookie"])
def test_auth_cookie_values_preserve_other_cookies_and_attributes(name, header):
    text = f"{header}: {name}=fictional-cobalt-92;theme=dark; SameSite=Lax\r\n"
    shield = Shield()
    result = shield.mask(text)
    assert result.text == text.replace("fictional-cobalt-92", "[TOKEN_1]")
    assert shield.restore(result.text).text == text


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("pass%77ord", "PASSWORD"),
        ("API%5fKEY", "API_KEY"),
        ("access%5Ftoken", "TOKEN"),
        ("X-Amz-Signature", "TOKEN"),
        ("X-Goog-Signature", "TOKEN"),
        ("sig", "TOKEN"),
    ],
)
def test_url_query_value_boundaries_and_source_escapes(name, kind):
    text = f"https://files.example.org/download?{name}=fictional%2Fcobalt%2B92&next=home#details"
    shield = Shield()
    result = shield.mask(text)
    assert result.text == text.replace("fictional%2Fcobalt%2B92", f"[{kind}_1]")
    assert shield.restore(result.text).text == text


@pytest.mark.parametrize("name", [r"pass\u0077ord", r"\u0070assword", r"p\u0061ssword"])
def test_json_encoded_field_name_preserves_json_and_escaped_value(name):
    text = '{"' + name + '":"fictional \\"cobalt\\" phrase","retry":2}'
    shield = Shield()
    result = shield.mask(text)
    assert json.loads(result.text) == {"password": "[PASSWORD_1]", "retry": 2}
    assert shield.restore(result.text).text == text


@pytest.mark.parametrize(
    "text",
    [
        "Cookie: theme=dark;lang=en\r\n",
        "Set-Cookie: language=pl; Secure; SameSite=Lax\n",
        "cookie_name=sessionid",
        "https://example.org/search?page=2&sort=recent",
        r'{"pass\u0077ord_length":24}',
        r'{"pass\uZZZZord":"literal"}',
        'prefix = "cobalt" + "meadow"',
    ],
)
def test_new_formats_leave_unrelated_code_and_metadata_alone(text):
    assert Shield().mask(text).text == text
    assert not candidates(text)


def test_simple_credential_concatenation_masks_each_literal_and_restores():
    text = 'password = "cobalt-" + "meadow-" + "92"\nretries = 3'
    shield = Shield()
    result = shield.mask(text)
    assert (
        result.text
        == 'password = "[PASSWORD_1]" + "[PASSWORD_2]" + "[PASSWORD_3]"\nretries = 3'
    )
    assert shield.restore(result.text).text == text


@pytest.mark.parametrize(
    ("text", "value", "kind"),
    [
        ("Enter 'cobalt meadow 92' to log in.", "cobalt meadow 92", "PASSWORD"),
        ("Use cobalt,meadow.92 to sign in.", "cobalt,meadow.92", "PASSWORD"),
        ("Hasło jest 'błękitny fikcyjny sad'.", "błękitny fikcyjny sad", "PASSWORD"),
        ("La contraseña es 'prado azul ficticio'.", "prado azul ficticio", "PASSWORD"),
        ("Backup code: cobalt-moon-92. Save it.", "cobalt-moon-92", "CREDENTIAL"),
        ("Patient: Élodie Martin, please call tomorrow.", "Élodie Martin", "PERSON"),
        ("Patient: Mira J. Quill, please call tomorrow.", "Mira J. Quill", "PERSON"),
        ("Date of birth: 1988/11/23;", "1988/11/23", "DOB"),
        ("Home address: 26 Cobalt Meadow Road.", "26 Cobalt Meadow Road", "ADDRESS"),
        (
            "Home address: 26 Cobalt St., Apt. 5. Call tomorrow.",
            "26 Cobalt St., Apt. 5",
            "ADDRESS",
        ),
        ("Passport number: P7DEMO992.", "P7DEMO992", "PASSPORT"),
        ("Driver\u2019s licence number: D-79-DEMO.", "D-79-DEMO", "DRIVERS_LICENSE"),
        (
            '{"encoding":"base64","payload":"Y29iYWx0LXByYWRv"}',
            "Y29iYWx0LXByYWRv",
            "CREDENTIAL",
        ),
        (
            "Webhook URL: https://notify.example.net/hooks/cobalt-92",
            "https://notify.example.net/hooks/cobalt-92",
            "TOKEN",
        ),
    ],
)
def test_context_variants_ask_locally_without_asserting_secrecy(text, value, kind):
    assert any(f.value == value and f.kind == kind for f in candidates(text))
    assert value not in repr(candidates(text))
    assert kind in CHOICES


@pytest.mark.parametrize(
    "label", ["Customer", "PATIENT", "Employee", "Billing", "Shipping"]
)
def test_explicit_address_labels_require_numeric_street_values(label):
    value = "84 Fictional Birch St., Apt. 2"
    text = f"{label} address: {value}. Delivery is tomorrow."
    findings = candidates(text)
    assert [(f.value, f.kind) for f in findings] == [(value, "ADDRESS")]
    assert not candidates(f"{label} address fields should be optional.")
    assert not candidates(f"{label} address: unspecified")


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_new_address_label_holds_before_forwarding_and_confirms_on_retry(tmp_path, api):
    upstream = FakeAPI()
    source = "Customer address: 84 Fictional Birch Lane\nOrder: shipped"
    expected = source.replace("84 Fictional Birch Lane", "[ADDRESS_1]")
    route = "/v1/responses" if api == "openai" else "/v1/messages"
    reply = (
        {"output": [reply_item(expected)]}
        if api == "openai"
        else {"content": [{"type": "text", "text": expected}]}
    )
    upstream.routes[route] = (200, "application/json", [json.dumps(reply).encode()])
    body = (
        {"model": "test", "input": source}
        if api == "openai"
        else {
            "model": "test",
            "max_tokens": 20,
            "messages": [{"role": "user", "content": source}],
        }
    )
    headers = {
        "thread-id" if api == "openai" else "x-claude-code-session-id": "address"
    }
    try:
        with (
            open_sessions(
                tmp_path,
                Settings(identity=False, note=False, secret_review=True),
                {},
                api=api,
            ) as sessions,
            Gateway(sessions, api=api, upstream=upstream.host, secure=False) as gateway,
        ):
            response, _ = call(gateway, "POST", route, body, headers=headers)
            assert response.status == 403
            assert not upstream.received
            review = gateway.reviews.report()["reviews"][0]
            finding = review["findings"][0]
            assert finding["kind"] == "ADDRESS"
            gateway.reviews.decide(review["id"], finding["index"], "ADDRESS")
            response, restored = call(gateway, "POST", route, body, headers=headers)
            assert response.status == 200
            sent = json.loads(upstream.received[0][3])
            masked = (
                sent["input"] if api == "openai" else sent["messages"][0]["content"]
            )
            assert masked == expected
            decoded = json.loads(restored)
            assert (
                decoded["output"][0]["content"][0]["text"]
                if api == "openai"
                else decoded["content"][0]["text"]
            ) == source
            assert sessions.get("address").shield.restore(masked).text == source
    finally:
        upstream.close()


@pytest.mark.parametrize(
    "text",
    [
        "Customer support can explain the billing process.",
        "The employee handbook is public.",
        "Date of birth is collected by the form.",
        "Passport renewal instructions are on the website.",
        "A driver's license number is required.",
        "Our home address fields need better validation.",
        "Webhook documentation: https://docs.example.org/webhooks",
        "WEBHOOK_SECRET=fictional-value\nWEBHOOK_URL=https://notify.example.org/events",
        "Split the file into pieces before uploading it.",
        '{"encoding":"utf8","data":"ordinary text"}',
        '{"encoding":"base64","data":"%%%invalid%%%"}',
    ],
)
def test_unrelated_instructions_and_invalid_encodings_do_not_add_review(text):
    assert not candidates(text)


def test_fragments_need_credential_context_and_stay_request_scoped():
    fragment = "cobalt-part-92"
    assert not candidates(fragment)
    cue = "The access token is split across the next two pieces."
    findings = request_candidates([(cue, cue), (fragment, fragment)])
    assert any(f.value == fragment for f in findings)
    assert not request_candidates([(fragment, fragment)])
    protected = request_candidates([(cue, cue), (fragment, " " * len(fragment))])
    assert not any(f.value == fragment for f in protected)
    assert not request_candidates([(cue, " " * len(cue)), (fragment, fragment)])


def test_quoted_session_cookie_and_environment_reference():
    text = 'Cookie: access_token="fictional-cobalt-92";theme=dark\n'
    shield = Shield()
    masked = shield.mask(text).text
    assert masked == text.replace("fictional-cobalt-92", "[TOKEN_1]")
    assert shield.restore(masked).text == text
    reference = 'Cookie: access_token="${SESSION_TOKEN}";theme=dark\n'
    assert Shield().mask(reference).text == reference


def test_quoted_query_assignment_does_not_get_skipped_as_an_empty_parameter():
    text = 'https://example.org/?password="fictional-cobalt-92"&next=home'
    shield = Shield()
    masked = shield.mask(text).text
    assert masked == text.replace("fictional-cobalt-92", "[PASSWORD_1]")
    assert shield.restore(masked).text == text


@pytest.mark.parametrize(
    "kind", ["PERSON", "ADDRESS", "DOB", "PASSPORT", "DRIVERS_LICENSE"]
)
def test_reviewed_personal_value_masks_on_later_cli_calls(
    tmp_path, monkeypatch, capsys, kind
):
    directory = prepare_data_dir(tmp_path / "private")
    (directory / "config.json").write_text('{"identity":false}', encoding="utf-8")
    source = "Enter 'cobalt meadow 92' to sign in."
    monkeypatch.setattr("veil.review_cli.ask_terminal", lambda *args: kind)
    for _ in range(2):
        monkeypatch.setattr("sys.stdin", io.StringIO(source))
        assert (
            cli.main(
                ["--data-dir", str(directory), "mask", "--session", "s", "--review"]
            )
            == 0
        )
        assert capsys.readouterr().out == f"Enter '[{kind}_1]' to sign in."
        monkeypatch.setattr(
            "veil.review_cli.ask_terminal", lambda *args: pytest.fail("asked again")
        )


def test_review_ui_supports_every_masking_choice():
    from veil.review_cli import _HTML

    assert all(f"  {kind}:" in _HTML for kind in REVIEW_TYPES)
    assert CHOICES[5] == "IGNORE"  # preserve original terminal numbering


def test_multiple_fields_share_the_existing_review_bound():
    texts = [
        (f"Enter cobalt-{i} to log in.", f"Enter cobalt-{i} to log in.")
        for i in range(101)
    ]
    with pytest.raises(ReviewError, match="too many or oversized"):
        request_candidates(texts)


def test_oversized_encoded_payload_fails_without_reporting_its_value():
    text = '{"encoding":"base64","data":"' + "Y2Fi" * 5000 + '"}'
    with pytest.raises(ReviewError, match="too many or oversized") as error:
        candidates(text)
    assert "Y2Fi" not in str(error.value)


def test_repeated_incomplete_context_labels_do_not_produce_partial_candidates():
    assert not candidates("Use a public function. " * 10_000)
    assert not candidates("Date of birth: unspecified\n" * 10_000)
    assert not candidates("Customer: support\n" * 10_000)


@pytest.mark.parametrize("kind", ["DOB", "ADDRESS", "PASSPORT", "DRIVERS_LICENSE"])
def test_literal_personal_placeholders_stay_literal_when_a_value_is_confirmed(
    tmp_path, kind
):
    with open_sessions(
        prepare_data_dir(tmp_path / "private"),
        Settings(identity=False, secret_review=True),
        {},
    ) as sessions:
        session = sessions.get("s")
        session.masker.confirm_secret("fictional-cobalt", kind)
        text = f"Example [{kind}_1], real fictional-cobalt"
        request = {"messages": [{"role": "user", "content": text}]}
        masked = session.masker.mask(request)["messages"][0]["content"]
        assert session.shield.restore(masked).text == text


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_every_original_case_masks_after_review_with_real_gateway_retry(tmp_path, api):
    """No corpus labels are used to find or approve values; use actual suggestions."""
    upstream = FakeAPI()
    route = "/v1/responses" if api == "openai" else "/v1/messages"
    reply = {"output": []} if api == "openai" else {"content": []}
    upstream.routes[route] = (200, "application/json", [json.dumps(reply).encode()])
    try:
        with (
            open_sessions(
                prepare_data_dir(tmp_path / "private"),
                Settings(identity=False, note=False, secret_review=True),
                {},
                api=api,
            ) as sessions,
            Gateway(
                sessions, api=api, upstream=upstream.host, secure=False, keepalive=0.05
            ) as gateway,
        ):
            for doc in leaks.load_corpus():
                body, paths = leaks.request(doc, api)
                headers = {
                    "thread-id"
                    if api == "openai"
                    else "x-claude-code-session-id": doc.id
                }
                count = len(upstream.received)
                response, _ = call(gateway, "POST", route, body, headers=headers)
                if response.status == 403:
                    assert len(upstream.received) == count, doc.id
                    review = gateway.reviews.report()["reviews"][-1]
                    for finding in review["findings"]:
                        gateway.reviews.decide(
                            review["id"], finding["index"], finding["kind"]
                        )
                    response, _ = call(gateway, "POST", route, body, headers=headers)
                assert response.status == 200, doc.id
                assert len(upstream.received) == count + 1, doc.id
                outgoing = json.loads(upstream.received[-1][3])
                shield = sessions.get(doc.id).shield
                for section, path in zip(doc.sections, paths, strict=True):
                    masked = leaks.at_path(outgoing, path)
                    hidden = leaks.positions(
                        leaks.trace_masks(section.text, masked, shield)
                    )
                    assert all(
                        set(range(m.start, m.end)) <= hidden for m in section.marks
                    ), doc.id
                    assert shield.restore(masked).text == section.text, doc.id
    finally:
        upstream.close()
