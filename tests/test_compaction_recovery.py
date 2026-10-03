"""Compaction stops locally; a fresh protected conversation can continue."""

import json
import os

import pytest

from test_codex_desktop import app_round_trip, binary_path
from test_gateway_server import FakeAPI, call, stream_reply
from test_openai_gateway import reply_item, text_events
from veil import Shield
from veil.gateway import Gateway, Sessions, Settings, open_sessions


@pytest.fixture(params=["api-key", "chatgpt"])
def world(request, tmp_path):
    api = FakeAPI()
    auth = request.param
    route = "/backend-api/codex/responses" if auth == "chatgpt" else "/v1/responses"
    api.routes[route] = (
        200,
        "application/json",
        [json.dumps({"output": [reply_item("Continue with [EMAIL_1].")]}).encode()],
    )
    headers = {"chatgpt-account-id": "fictional-account"} if auth == "chatgpt" else {}
    try:
        with Gateway(
            open_sessions(
                tmp_path, Settings(identity=False, note=False), {}, api="openai"
            ),
            api="openai",
            openai_auth=auth,
            upstream=api.host,
            secure=False,
        ) as gateway:
            yield api, gateway, headers, route
    finally:
        api.close()


def send(gateway, headers, session, text):
    return call(
        gateway,
        path="/v1/responses",
        headers={**headers, "thread-id": session},
        body={"input": text},
    )


def test_refusal_preserves_old_mappings_and_a_new_chat_can_continue(world):
    api, gateway, headers, route = world
    old = "before-compaction@example.com"
    new = "after-compaction@example.com"
    response, payload = send(gateway, headers, "full-conversation", old)
    assert response.status == 200
    assert f"Continue with {old}." in payload.decode()

    response, payload = call(
        gateway,
        path="/v1/responses/compact",
        headers={**headers, "thread-id": "full-conversation"},
        body={"input": [{"role": "user", "content": old}]},
    )
    assert response.status == 400
    assert response.getheader("x-should-retry") == "false"
    assert response.getheader("Connection") == "close"
    error = json.loads(payload)["error"]
    assert error["type"] == "invalid_request_error"
    assert "remote compaction" in error["message"]
    assert "/new" in error["message"]
    assert "keeping Veil selected" in error["message"]
    assert "reviewed locally" in error["message"]
    assert old.encode() not in payload
    assert len(api.received) == 1

    # This is a reviewed handoff in a new conversation, not a forged compacted
    # response or an automatic replay of the old transcript.
    response, payload = send(gateway, headers, "new-conversation", new)
    assert response.status == 200
    assert f"Continue with {new}." in payload.decode()
    assert old.encode() not in payload

    # Refusing compaction did not erase or cross-wire the old session's vault.
    response, payload = send(gateway, headers, "full-conversation", old)
    assert response.status == 200
    assert f"Continue with {old}." in payload.decode()
    assert new.encode() not in payload
    assert len(api.received) == 3
    for _, path, _, body in api.received:
        assert path == route
        assert old.encode() not in body
        assert new.encode() not in body
        assert b"[EMAIL_1]" in body


def test_compaction_refusal_does_not_echo_body_or_query(world):
    api, gateway, headers, _ = world
    private = "compaction-canary@example.com"
    response, payload = call(
        gateway,
        path="/v1/responses/compact?private=" + private,
        headers={**headers, "thread-id": "unopened-conversation"},
        body=("not even valid JSON: " + private).encode(),
    )
    assert response.status == 400
    assert private.encode() not in payload
    assert "remote compaction" in json.loads(payload)["error"]["message"]
    assert api.received == []


@pytest.mark.skipif(
    os.environ.get("VEIL_LOCAL_CODEX") != "1", reason="opt-in local app-server test"
)
def test_installed_app_server_masks_local_summary_and_continues(tmp_path):
    api = FakeAPI()
    api.routes["/v1/models"] = (200, "application/json", [b'{"models":[]}'])
    checkpoint = "Fictional handoff checkpoint c4. "
    summary = text_events().replace("Hi ", checkpoint + "Hi ")
    api.replies.extend(
        [
            stream_reply(text.encode())
            for text in [text_events(), summary, text_events()]
        ]
    )
    try:
        with Gateway(
            Sessions(lambda _: Shield(redact_warnings=True), api="openai"),
            api="openai",
            upstream=api.host,
            secure=False,
        ) as gateway:
            email = "compaction-recovery@example.com"
            app_round_trip(binary_path(), gateway, tmp_path, email, compact=True)
        posts = [row for row in api.received if row[0] == "POST"]
        assert len(posts) == 3  # ordinary turn, local summary, continuation
        assert all(b"[EMAIL_1]" in row[3] for row in posts)
        assert checkpoint.encode() not in posts[1][3]
        assert checkpoint.encode() in posts[2][3]
        for _, path, _, body in api.received:
            assert path != "/v1/responses/compact"
            assert email.encode() not in body
    finally:
        api.close()
