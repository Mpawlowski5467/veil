"""Anthropic query limits and shared gateway request-target validation."""

import http.client
import json

import pytest

from test_gateway_server import FakeAPI, call, make_shield
from veil.gateway import SECRET_HEADER, SESSION_HEADER, Gateway, Sessions

PRIVATE = "fictional-audit@example.org"
ROUTES = ("/v1/messages", "/v1/messages/count_tokens")
BODY = {"messages": [{"role": "user", "content": PRIVATE}]}
TARGET_ERROR = (
    "unsupported request target; only an optional beta=true query is supported"
)


@pytest.fixture
def api():
    fake = FakeAPI()
    yield fake
    fake.close()


@pytest.fixture
def gateway(api):
    with Gateway(Sessions(make_shield), upstream=api.host, secure=False) as gateway:
        yield gateway


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("query", ["", "?beta=true"])
def test_supported_targets_preserve_query_and_mask_body(api, gateway, route, query):
    response, _ = call(gateway, path=route + query, body=BODY)

    assert response.status == 200
    assert len(api.received) == 1
    _, target, _, body = api.received[0]
    assert target == route + query
    assert PRIVATE.encode() not in body
    assert json.loads(body)["messages"][0]["content"] == "[EMAIL_1]"


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "suffix",
    [
        f"?private={PRIVATE}",
        f"?beta=true&private={PRIVATE}",
        f"?beta={PRIVATE}",
        f"?{PRIVATE}=true",
        "?beta=true&beta=true",
        "?beta=false",
        "?beta=",
        "?beta",
        "?Beta=true",
        "?beta=%74rue",
        "?%62eta=true",
        "?beta=true&",
        "?",
        "#",
        "?beta=true#",
        f"#private={PRIVATE}",
        f"?beta=true#private={PRIVATE}",
    ],
)
def test_unsupported_targets_never_reach_upstream(api, gateway, capsys, route, suffix):
    response, payload = call(gateway, path=route + suffix, body=BODY)

    assert response.status == 400
    assert response.getheader("x-should-retry") == "false"
    assert response.getheader("Connection") == "close"
    error = json.loads(payload)["error"]
    assert error["type"] == "invalid_request_error"
    expected = "unsupported request target" if "#" in suffix else TARGET_ERROR
    assert error["message"].endswith(expected)
    assert PRIVATE.encode() not in payload
    assert api.received == []
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out + captured.err


@pytest.mark.parametrize("route", ROUTES)
def test_absolute_form_target_is_refused(api, gateway, route):
    target = f"http://{PRIVATE}{route}?beta=true"
    response, payload = call(
        gateway,
        path=target,
        body=BODY,
        headers={"Host": f"127.0.0.1:{gateway.port}"},
    )

    assert response.status == 400
    assert json.loads(payload)["error"]["message"].endswith(
        "unsupported request target"
    )
    assert PRIVATE.encode() not in payload
    assert response.getheader("x-should-retry") == "false"
    assert api.received == []


@pytest.mark.parametrize("auth", ["api-key", "chatgpt"])
@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("POST", "/v1/responses#"),
        ("POST", f"/v1/responses#private={PRIVATE}"),
        ("POST", f"http://{PRIVATE}/v1/responses"),
        ("GET", "/v1/models?client_version=0.156.1#"),
        ("GET", f"/v1/models?client_version=0.156.1#private={PRIVATE}"),
        ("GET", f"http://{PRIVATE}/v1/models?client_version=0.156.1"),
    ],
)
def test_openai_non_origin_targets_are_refused_before_body(api, auth, method, target):
    with Gateway(
        Sessions(make_shield, api="openai"),
        api="openai",
        openai_auth=auth,
        upstream=api.host,
        secure=False,
    ) as gateway:
        conn = http.client.HTTPConnection(f"127.0.0.1:{gateway.port}", timeout=2)
        try:
            conn.putrequest(method, target, skip_host=True)
            conn.putheader("Host", f"127.0.0.1:{gateway.port}")
            conn.putheader(SECRET_HEADER, gateway.secret)
            conn.putheader("thread-id", "fictional-audit")
            if auth == "chatgpt":
                conn.putheader("chatgpt-account-id", "fictional-account")
            conn.putheader("Content-Length", "100")
            conn.endheaders()  # A target refusal must precede any body handling.
            response = conn.getresponse()
            payload = response.read()
        finally:
            conn.close()

    assert response.status == 400
    assert json.loads(payload)["error"]["message"].endswith(
        "unsupported request target"
    )
    assert PRIVATE.encode() not in payload
    assert response.getheader("x-should-retry") == "false"
    assert api.received == []


@pytest.mark.parametrize("auth", ["api-key", "chatgpt"])
@pytest.mark.parametrize("query", ["", "?", "?client_version=0.156.1"])
def test_openai_model_catalog_queries_remain_supported(api, auth, query):
    api.replies.append((200, "application/json", [b'{"models":[]}']))
    with Gateway(
        Sessions(make_shield, api="openai"),
        api="openai",
        openai_auth=auth,
        upstream=api.host,
        secure=False,
    ) as gateway:
        response, _ = call(
            gateway,
            method="GET",
            path="/v1/models" + query,
            headers={"chatgpt-account-id": "fictional"} if auth == "chatgpt" else {},
        )

    assert response.status == 200
    prefix = "/backend-api/codex" if auth == "chatgpt" else "/v1"
    assert api.received[0][1] == f"{prefix}/models{query}"


@pytest.mark.parametrize("route", ROUTES)
def test_unsupported_query_is_refused_before_reading_body(api, gateway, route):
    conn = http.client.HTTPConnection(f"127.0.0.1:{gateway.port}", timeout=2)
    try:
        conn.putrequest("POST", f"{route}?private={PRIVATE}")
        conn.putheader(SECRET_HEADER, gateway.secret)
        conn.putheader(SESSION_HEADER, "fictional-audit")
        conn.putheader("Content-Length", "100")
        conn.endheaders()  # Deliberately send no body: refusal must not wait for it.
        response = conn.getresponse()
        payload = response.read()
    finally:
        conn.close()

    assert response.status == 400
    assert json.loads(payload)["error"]["message"].endswith(TARGET_ERROR)
    assert PRIVATE.encode() not in payload
    assert response.getheader("x-should-retry") == "false"
    assert api.received == []
