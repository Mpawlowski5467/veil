"""Scripted local providers exercise recovery; these are not live client tests."""

import http.client
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from test_gateway_server import FakeAPI, call, stream_reply, text_reply
from test_openai_gateway import reply_item, text_events
from veil.gateway import SECRET_HEADER, SESSION_HEADER, Gateway, Settings, open_sessions


@pytest.fixture(params=["anthropic", "openai"])
def protocol(request):
    return request.param


def request_body(protocol, text, history=()):
    key = "input" if protocol == "openai" else "messages"
    return {
        "model": "fictional-model",
        "max_output_tokens" if protocol == "openai" else "max_tokens": 100,
        key: [*history, {"role": "user", "content": text}],
    }


def path(protocol):
    return "/v1/responses" if protocol == "openai" else "/v1/messages"


def headers(session):
    return {"thread-id": session, SESSION_HEADER: session}


def response_body(protocol, text):
    if protocol == "openai":
        return {"status": "completed", "output": [reply_item(text)]}
    return {
        "type": "message",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
    }


def reply(fake, protocol, text):
    fake.routes[path(protocol)] = (
        200,
        "application/json",
        [json.dumps(response_body(protocol, text)).encode()],
    )


def send(gateway, text, *, session="resumed", history=()):
    return call(
        gateway,
        path=path(gateway.api),
        headers=headers(session),
        body=request_body(gateway.api, text, history),
    )


def finished_activity(gateway, requests):
    # A client may read the final HTTP chunk before the server's finally block
    # records completion. Await that observable condition, not a fixed delay.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        activity = gateway.activity.summary()["sessions"]
        if sum(item["completed"] + item["failed"] for item in activity) == requests:
            return activity
        time.sleep(0.01)
    raise AssertionError("gateway did not finish request accounting")


@contextmanager
def running(directory, protocol, fake, *, review=False):
    sessions = open_sessions(
        directory,
        Settings(identity=False, note=False, secret_review=review),
        {},
        api=protocol,
    )
    # Exercise eviction/reopening as well as whole-process restarts.
    sessions._max_open = 3
    with Gateway(sessions, api=protocol, upstream=fake.host, secure=False) as gateway:
        yield gateway


@pytest.fixture
def fake():
    provider = FakeAPI()
    yield provider
    provider.close()


def test_long_history_survives_eviction_and_restart(tmp_path, protocol, fake):
    history = []
    restored = ""
    for restart in range(3):
        with running(tmp_path, protocol, fake) as gateway:
            assert gateway.activity.summary()["sessions"] == []
            for turn in range(12):
                number = restart * 12 + turn + 1
                email = f"fictional{number}@example.org"
                masked_reply = f"Contact [EMAIL_{number}]suffix."
                reply(fake, protocol, masked_reply)
                status, raw = send(gateway, email, history=history)
                assert status.status == 200
                restored = f"Contact {email}suffix."
                assert restored.encode() in raw
                outbound = fake.received[-1][3]
                assert b"@example.org" not in outbound
                assert f"[EMAIL_{number}]".encode() in outbound
                if number > 1:
                    assert f"[EMAIL_{number - 1}]suffix".encode() in outbound
                history.extend(
                    [
                        {"role": "user", "content": email},
                        {"role": "assistant", "content": restored},
                    ]
                )
                # Evict the conversation without touching its persisted mappings.
                for other in range(4):
                    reply(fake, protocol, "[EMAIL_1]")
                    assert (
                        send(gateway, "other@example.net", session=f"other-{other}")[
                            0
                        ].status
                        == 200
                    )
    assert len(history) == 72


def test_concurrent_sessions_cannot_restore_each_others_values(
    tmp_path, protocol, fake
):
    reply(fake, protocol, "[EMAIL_1]")
    with running(tmp_path, protocol, fake) as gateway:

        def one(index):
            email = f"fictional-worker-{index}@example.org"
            status, raw = send(gateway, email, session=f"worker-{index}")
            assert status.status == 200
            result = json.loads(raw)
            text = (
                result["output"][0]["content"][0]["text"]
                if protocol == "openai"
                else result["content"][0]["text"]
            )
            assert text == email

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(one, range(32)))
        assert len(fake.received) == 32
        assert all(b"@example.org" not in item[3] for item in fake.received)
        activity = finished_activity(gateway, 32)
        assert sum(item["completed"] for item in activity) == 32
        assert sum(item["failed"] for item in activity) == 0


def test_confirmation_survives_restart_but_ignore_does_not(tmp_path, protocol, fake):
    phrase = "fictional winter meadow phrase"
    text = f'Use "{phrase}" to sign in.'
    for restart in range(2):
        with running(tmp_path, protocol, fake, review=True) as gateway:
            reply(fake, protocol, "[PASSWORD_1]")
            if not restart:
                assert send(gateway, text)[0].status == 403
                held = gateway.reviews.report()["reviews"][0]
                gateway.reviews.decide(held["id"], 0, "PASSWORD")
            status, raw = send(gateway, text)
            assert status.status == 200
            assert phrase.encode() in raw
            assert phrase.encode() not in fake.received[-1][3]
            assert send(gateway, text, session="ignored")[0].status == 403
            held = gateway.reviews.report()["reviews"][-1]
            gateway.reviews.decide(held["id"], 0, "IGNORE")
            assert send(gateway, text, session="ignored")[0].status == 200
            assert phrase.encode() in fake.received[-1][3]
            assert send(gateway, text, session="unrelated")[0].status == 403


def partial_stream(protocol):
    if protocol == "openai":
        # End inside a placeholder, before response.completed.
        parts = text_events().split("event: response.output_text.done")[0]
        return parts.replace("_1].", "").encode()
    return b"".join(text_reply("[EMAIL")[2][:-2])


def test_rate_limit_and_incomplete_stream_can_retry_without_losing_mappings(
    tmp_path, protocol, fake
):
    with running(tmp_path, protocol, fake) as gateway:
        fake.replies.append(
            (429, "application/json", [b'{"error":{"message":"rate limited"}}'])
        )
        assert send(gateway, "retry@example.org")[0].status == 429
        fake.replies.append(stream_reply(partial_stream(protocol)))
        status, raw = send(gateway, "retry@example.org")
        assert status.status == 200
        assert b"error" in raw
        activity = finished_activity(gateway, 2)[0]
        assert activity["completed"] == 0
        assert activity["failed"] == 2
        reply(fake, protocol, "[EMAIL_1]")
        assert b"retry@example.org" in send(gateway, "retry@example.org")[1]
        assert all(b"retry@example.org" not in item[3] for item in fake.received)
        activity = finished_activity(gateway, 3)[0]
        assert (activity["requests"], activity["completed"], activity["failed"]) == (
            3,
            1,
            2,
        )


def test_cancelled_client_does_not_block_new_request(tmp_path, protocol, fake):
    with running(tmp_path, protocol, fake) as gateway:
        # Delay after headers, so the client disconnects during the response.
        fake.replies.append(stream_reply(0.2, partial_stream(protocol)))
        conn = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=5)
        conn.request(
            "POST",
            path(protocol),
            json.dumps(request_body(protocol, "cancel@example.org")),
            {
                SECRET_HEADER: gateway.secret,
                "Authorization": "Bearer fictional",
                "Content-Type": "application/json",
                **headers("cancelled"),
            },
        )
        response = conn.getresponse()
        assert response.status == 200
        conn.sock.shutdown(socket.SHUT_RDWR)
        response.close()
        conn.close()
        reply(fake, protocol, "[EMAIL_1]")
        assert (
            b"next@example.org" in send(gateway, "next@example.org", session="next")[1]
        )
        activity = finished_activity(gateway, 2)
        assert sum(item["failed"] for item in activity) == 1
        assert sum(item["completed"] for item in activity) == 1
        assert all(b"@example.org" not in item[3] for item in fake.received)
