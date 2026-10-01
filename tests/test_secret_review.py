"""Local secret triage, request isolation, transport boundaries, and consent."""

import http.client
import io
import json
import threading

import pytest

from test_gateway_server import FakeAPI, call
from veil import cli
from veil.gateway import Gateway, Settings, open_sessions
from veil.gateway.config import SettingsError, parse_settings, prepare_data_dir
from veil.review_cli import ReviewServer, _gateway_request, run_review
from veil.secret_review import (
    REVIEW_PATH,
    Candidate,
    ReviewError,
    ReviewQueue,
    candidates,
)
from veil.verification import Endpoint

OPAQUE = "Fictional7zV2wQ8nR4pL9xT6bC3dK1"
PHRASE = "fictional four word password"


def body(api, text):
    if api == "openai":
        return {"model": "test", "input": text}
    return {
        "model": "test",
        "max_tokens": 30,
        "messages": [{"role": "user", "content": text}],
    }


def send(gateway, request, *, session="test"):
    is_openai = gateway.api == "openai"
    return call(
        gateway,
        "POST",
        "/v1/responses" if is_openai else "/v1/messages",
        request,
        headers={"thread-id" if is_openai else "x-claude-code-session-id": session},
    )


@pytest.mark.parametrize(
    ("text", "value", "kind"),
    [
        (f"My password is '{PHRASE}'", PHRASE, "PASSWORD"),
        (f"My API key is {OPAQUE}", OPAQUE, "API_KEY"),
        (f"refresh token was `{OPAQUE}`", OPAQUE, "TOKEN"),
        (f"Example --password '{PHRASE}' --verbose", PHRASE, "PASSWORD"),
        (f"https://example.test/?api_key={OPAQUE}&x=1", OPAQUE, "API_KEY"),
        (f"Can you inspect {OPAQUE} please?", OPAQUE, "TOKEN"),
        ("password is x", "x", "PASSWORD"),
    ],
)
def test_local_reasons_and_values(text, value, kind):
    found = candidates(text)
    assert any(f.value == value and f.kind == kind for f in found)
    assert value not in repr(found)


@pytest.mark.parametrize(
    "text",
    [
        "A normal sentence about writing a function.",
        "max_tokens=8192; password_length=16; api_key_file='settings.txt'",
        "My password is [PASSWORD_1]",
        "api key is $OPENAI_API_KEY",
        "api key is ${OPENAI_API_KEY}",
        "x" * 100_000,
    ],
    ids=["prose", "identifiers", "placeholder", "environment", "braces", "large-input"],
)
def test_ordinary_code_references_and_masked_values_do_not_prompt(text):
    assert not candidates(text)


def test_conservative_prose_value_includes_whole_unquoted_line():
    text = "password is four fictional words here"
    assert candidates(text)[0].value == "four fictional words here"


def test_review_limits_fail_without_quoting_values():
    with pytest.raises(ReviewError) as error:
        candidates("password is " + "x" * 16_385)
    assert "xxxx" not in str(error.value)
    with pytest.raises(ReviewError):
        candidates("\n".join(f"password is fictional value {i}" for i in range(101)))


def test_queue_dedup_scope_and_partial_choices():
    queue = ReviewQueue()
    findings = (
        Candidate(PHRASE, "PASSWORD", "prose"),
        Candidate(OPAQUE, "TOKEN", "random"),
    )
    token = queue.hold("s", b"request", findings)
    assert queue.hold("s", b"request", findings) == token
    queue.decide(token, 0, "PASSWORD")
    assert queue.decisions("s", b"request") is None
    queue.decide(token, 1, "IGNORE")
    assert queue.decisions("s", b"request") == {PHRASE: "PASSWORD", OPAQUE: "IGNORE"}
    assert queue.decisions("s2", b"request") is None
    assert queue.decisions("s", b"changed") is None
    assert PHRASE not in repr(queue)


def test_expiry_eviction_and_invalid_decisions(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("veil.secret_review.time.monotonic", lambda: now[0])
    queue = ReviewQueue(ttl=10, limit=1)
    findings = (Candidate(PHRASE, "PASSWORD", "prose"),)
    first = queue.hold("s", b"one", findings)
    second = queue.hold("s", b"two", findings)
    with pytest.raises(ReviewError):
        queue.decide(first, 0, "IGNORE")
    for index, choice in [
        (True, "IGNORE"),
        (0, "password"),
        (-1, "IGNORE"),
        (3, "IGNORE"),
    ]:
        with pytest.raises(ReviewError):
            queue.decide(second, index, choice)
    now[0] += 11
    assert queue.report() == {"reviews": []}
    assert queue.decisions("s", b"two") is None
    with pytest.raises(ReviewError):
        queue.decide(second, 0, "IGNORE")


@pytest.mark.parametrize("api", ["anthropic", "openai"])
@pytest.mark.parametrize("value", [PHRASE, "x", "xx", OPAQUE])
def test_gateway_requires_review_then_masks_complete_value(tmp_path, api, value):
    fake = FakeAPI()
    try:
        with (
            open_sessions(
                tmp_path,
                Settings(identity=False, note=False, secret_review=True),
                {},
                api=api,
            ) as sessions,
            Gateway(sessions, api=api, upstream=fake.host, secure=False) as gateway,
        ):
            request = body(api, f'My password is "{value}"')
            response, raw = send(gateway, request)
            assert response.status == 403
            assert value.encode() not in raw if len(value) > 2 else True
            assert fake.received == []
            target = Endpoint(gateway.url, gateway.secret)
            report = _gateway_request(target, None)
            finding = report["reviews"][0]
            assert finding["findings"][0]["value"] == value
            _gateway_request(
                target, {"id": finding["id"], "index": 0, "choice": "PASSWORD"}
            )
            reply = (
                {
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_test",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "[PASSWORD_1]",
                                    "annotations": [],
                                }
                            ],
                        }
                    ]
                }
                if api == "openai"
                else {"content": [{"type": "text", "text": "[PASSWORD_1]"}]}
            )
            fake.replies.append((200, "application/json", [json.dumps(reply).encode()]))
            response, restored = send(gateway, request)
            assert response.status == 200
            assert value in restored.decode()
            assert len(fake.received) == 1
            sent = json.loads(fake.received[0][3])
            text = sent["input"] if api == "openai" else sent["messages"][0]["content"]
            assert text == 'My password is "[PASSWORD_1]"'
            assert sessions.get("test").shield.restore("[PASSWORD_1]").text == value
    finally:
        fake.close()


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_ignore_only_allows_exact_request_and_same_session(tmp_path, api):
    fake = FakeAPI()
    try:
        with (
            open_sessions(
                tmp_path,
                Settings(identity=False, note=False, secret_review=True),
                {},
                api=api,
            ) as sessions,
            Gateway(sessions, api=api, upstream=fake.host, secure=False) as gateway,
        ):
            request = body(api, f"Inspect this: {OPAQUE}")
            assert send(gateway, request)[0].status == 403
            report = gateway.reviews.report()["reviews"][0]
            gateway.reviews.decide(report["id"], 0, "IGNORE")
            send(gateway, request)
            assert len(fake.received) == 1
            assert OPAQUE.encode() in fake.received[0][3]
            assert (
                send(gateway, body(api, f"Changed prompt: {OPAQUE}"))[0].status == 403
            )
            assert send(gateway, request, session="other")[0].status == 403
            assert len(fake.received) == 1
    finally:
        fake.close()


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_full_request_context_masks_earlier_unlabelled_occurrence(tmp_path, api):
    with open_sessions(
        tmp_path, Settings(identity=False, note=False), {}, api=api
    ) as sessions:
        masker = sessions.get("s").masker
        request = body(api, "A fictional weak value")
        if api == "openai":
            request["instructions"] = 'password="A fictional weak value"'
        else:
            request["messages"].append(
                {"role": "user", "content": 'password="A fictional weak value"'}
            )
        masked = masker.mask(request)
        assert "A fictional weak value" not in json.dumps(masked)


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_clear_secrets_need_no_review_and_auth_headers_stay_intact(tmp_path, api):
    fake = FakeAPI()
    try:
        with (
            open_sessions(
                tmp_path,
                Settings(identity=False, note=False, secret_review=True),
                {},
                api=api,
            ) as sessions,
            Gateway(sessions, api=api, upstream=fake.host, secure=False) as gateway,
        ):
            send(gateway, body(api, f'password="{PHRASE}"'))
            assert len(fake.received) == 1
            assert PHRASE.encode() not in fake.received[0][3]
            assert fake.received[0][2]["Authorization"] == "Bearer user-token"
            assert gateway.reviews.report() == {"reviews": []}
    finally:
        fake.close()


@pytest.mark.parametrize(
    "headers",
    [
        {"x-gateway-secret": None},
        {"Origin": "https://evil.test"},
        {"Host": "evil.test"},
    ],
)
def test_gateway_review_endpoint_requires_local_authenticated_access(tmp_path, headers):
    with (
        open_sessions(tmp_path, Settings(), {}) as sessions,
        Gateway(sessions) as gateway,
    ):
        response, raw = call(gateway, "GET", REVIEW_PATH, headers=headers)
        assert response.status in {401, 403}
        assert b'"reviews"' not in raw


def test_disabled_mode_does_not_collect_review_values(tmp_path):
    with open_sessions(tmp_path, Settings(identity=False), {}) as sessions:
        masker = sessions.get("s").masker
        assert OPAQUE in json.dumps(masker.mask(body("anthropic", OPAQUE)))
        assert masker.review_findings() == ()


def test_settings_boolean_only(tmp_path):
    assert parse_settings('{"secret_review":true}', tmp_path).secret_review
    for value in (1, "yes", None, []):
        with pytest.raises(SettingsError, match="secret_review"):
            parse_settings(json.dumps({"secret_review": value}), tmp_path)


def test_cli_review_confirm_and_restore(tmp_path, monkeypatch, capsys):
    directory = prepare_data_dir(tmp_path / "private")
    (directory / "config.json").write_text('{"identity":false}')
    source = f'My password is "{PHRASE}"'
    monkeypatch.setattr("sys.stdin", io.StringIO(source))
    seen = []
    monkeypatch.setattr(
        "veil.review_cli.ask_terminal",
        lambda value, reason: seen.append(value) or "PASSWORD",
    )
    args = ["--data-dir", str(directory), "mask", "--session", "s", "--review"]
    assert cli.main(args) == 0
    masked = capsys.readouterr().out
    assert masked == 'My password is "[PASSWORD_1]"'
    assert seen == [PHRASE]
    monkeypatch.setattr("sys.stdin", io.StringIO(masked))
    assert cli.main(["--data-dir", str(directory), "restore", "--session", "s"]) == 0
    assert capsys.readouterr().out == source


def test_cancelled_review_never_writes_stdout_or_clipboard(
    tmp_path, monkeypatch, capsys
):
    directory = prepare_data_dir(tmp_path / "private")

    def cancel(*args):
        raise SettingsError("review cancelled; no output written")

    monkeypatch.setattr("veil.review_cli.ask_terminal", cancel)
    monkeypatch.setattr("veil.text_cli.clipboard_read", lambda: f"password is {PHRASE}")
    writes = []
    monkeypatch.setattr("veil.text_cli.clipboard_write", writes.append)
    assert (
        cli.main(
            [
                "--data-dir",
                str(directory),
                "mask",
                "--session",
                "s",
                "--clipboard",
                "--review",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert not captured.out
    assert not writes
    assert PHRASE not in captured.err
    assert "review cancelled" in captured.err


def test_review_command_refuses_pipes_before_reading_private_findings(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    with pytest.raises(SettingsError, match="local interactive terminal"):
        run_review()


def browser_call(
    server, method="GET", path="/data", *, auth=True, headers=None, data=None
):
    sent = {"X-Veil-Review": server.token} if auth else {}
    sent.update(headers or {})
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request(method, path, headers=sent, body=data)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response, raw


def test_browser_bridge_boundaries_and_private_rendering():
    calls = []

    def callback(choice):
        calls.append(choice)
        return {"reviews": [{"value": "<script>fictional</script>"}]}

    with ReviewServer(callback) as server:
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        worker.start()
        try:
            response, raw = browser_call(server, path="/", auth=False)
            assert response.status == 200
            assert b"<script>fictional</script>" not in raw
            assert b"value.textContent = finding.value" in raw
            assert response.getheader("Cache-Control") == "no-store"
            assert "frame-ancestors 'none'" in response.getheader(
                "Content-Security-Policy"
            )
            assert browser_call(server, auth=False)[0].status == 403
            for headers in (
                {"Origin": "https://evil.test"},
                {"Host": "evil.test"},
                {"Sec-Fetch-Site": "cross-site"},
            ):
                assert browser_call(server, headers=headers)[0].status == 403
            assert not calls
            assert browser_call(server)[0].status == 200
            assert calls == [None]
            assert (
                browser_call(
                    server, "POST", data='{"id":"x","index":0,"choice":"PASSWORD"}'
                )[0].status
                == 200
            )
            assert calls[-1]["choice"] == "PASSWORD"
            before = len(calls)
            assert (
                browser_call(server, "POST", data='{"choice":"IGNORE"}')[0].status
                == 400
            )
            assert len(calls) == before
            assert browser_call(server, path="/?secret=anything")[0].status == 403
        finally:
            server.shutdown()
            worker.join()


@pytest.mark.parametrize("api", ["anthropic", "openai"])
@pytest.mark.parametrize(
    "source",
    [
        "password=fictional four word phrase",
        "password: |\n  fictional four word phrase\n  another line\n",
        "My password is 'brackets[within]aSecret'",
        "FictionalABCdef123-45-6789Zyxwvut098",
    ],
)
def test_partial_masks_cannot_hide_remaining_candidate(tmp_path, api, source):
    with open_sessions(
        tmp_path, Settings(identity=False, note=False, secret_review=True), {}, api=api
    ) as sessions:
        masker = sessions.get("s").masker
        masker.mask(body(api, source))
        findings = masker.review_findings()
        assert findings
        for finding in findings:
            masker.confirm_secret(finding.value, finding.kind)
        masked = masker.mask(body(api, source))
        assert not masker.review_findings()
        for finding in findings:
            assert finding.value not in json.dumps(masked)


def test_recognized_pem_does_not_ask_about_tokens_inside_it(tmp_path):
    source = "-----BEGIN PRIVATE KEY-----\n" + OPAQUE + "\n-----END PRIVATE KEY-----"
    with open_sessions(
        tmp_path, Settings(identity=False, note=False, secret_review=True), {}
    ) as sessions:
        masker = sessions.get("s").masker
        assert OPAQUE not in json.dumps(masker.mask(body("anthropic", source)))
        assert not masker.review_findings()


def test_confirmed_cli_value_is_masked_again_in_same_session(
    tmp_path, monkeypatch, capsys
):
    directory = prepare_data_dir(tmp_path / "private")
    (directory / "config.json").write_text('{"identity": false}')
    monkeypatch.setattr("veil.review_cli.ask_terminal", lambda *_: "TOKEN")
    args = ["--data-dir", str(directory), "mask", "--session", "s", "--review"]
    monkeypatch.setattr("sys.stdin", io.StringIO(OPAQUE))
    assert cli.main(args) == 0
    assert capsys.readouterr().out == "[TOKEN_1]"

    def no_repeat(*_):
        pytest.fail("an already confirmed value was asked about again")

    monkeypatch.setattr("veil.review_cli.ask_terminal", no_repeat)
    monkeypatch.setattr("sys.stdin", io.StringIO(OPAQUE))
    assert cli.main(args) == 0
    assert capsys.readouterr().out == "[TOKEN_1]"


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_confirmed_secret_masks_changed_retry_but_does_not_cross_sessions(
    tmp_path, api
):
    fake = FakeAPI()
    try:
        with (
            open_sessions(
                tmp_path,
                Settings(identity=False, note=False, secret_review=True),
                {},
                api=api,
            ) as sessions,
            Gateway(sessions, api=api, upstream=fake.host, secure=False) as gateway,
        ):
            request = body(api, f'My password is "{PHRASE}"')
            assert send(gateway, request)[0].status == 403
            review = gateway.reviews.report()["reviews"][0]
            gateway.reviews.decide(review["id"], 0, "PASSWORD")
            changed = body(api, f'On retry, my password is "{PHRASE}"')
            send(gateway, changed)
            assert len(fake.received) == 1
            assert PHRASE.encode() not in fake.received[0][3]
            assert b"[PASSWORD_1]" in fake.received[0][3]
            assert send(gateway, changed, session="other")[0].status == 403
            assert len(fake.received) == 1
    finally:
        fake.close()
