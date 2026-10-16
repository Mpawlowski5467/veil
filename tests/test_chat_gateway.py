"""OpenAI text chat against fictional loopback providers; no account calls."""

import copy
import json

import pytest

from test_gateway_server import FakeAPI, call
from test_openai_gateway import EMAIL, NAME, shield_for
from veil.gateway import (
    ChatRequestMasker,
    ChatRestorer,
    ChatStreamRestorer,
    Gateway,
    MemoryLedger,
    Sessions,
    Settings,
    UnsupportedRequestError,
    open_sessions,
)
from veil.gateway.activity import ACTIVITY_PATH, VERIFY_PATH
from veil.gateway.response import StreamError


def test_refusal_text_can_be_restored_and_replayed(adapters):
    shield, ledger, masker = adapters
    source = reply(None)
    source["choices"][0]["message"]["refusal"] = "Cannot contact [PERSON_1]."
    result = ChatRestorer(shield, ledger).response(source)
    message = result["choices"][0]["message"]
    assert message["refusal"] == f"Cannot contact {NAME}."
    body = request()
    body["messages"].append(message)
    assert masker.mask(body)["messages"][-1]["refusal"] == "Cannot contact [PERSON_1]."


def test_participant_names_use_valid_stable_pseudonyms(adapters):
    body = request()
    body["messages"][0]["name"] = NAME
    first = adapters[2].mask(body)["messages"][-1]["name"]
    assert first.startswith("veil_")
    assert first.replace("_", "").isalnum()
    assert adapters[2].mask(body)["messages"][-1]["name"] == first


def test_split_refusal_stream_restores_and_records(adapters):
    stream = ChatStreamRestorer(*adapters[:2])
    source = frame(chunk(refusal="Cannot contact [PER"))
    source += frame(chunk(refusal="SON_1].", reason="content_filter"))
    source += "data: [DONE]\n\n"
    result = stream.feed(source) + stream.finish()
    assert stream.completed
    text = "".join(
        event["choices"][0]["delta"].get("refusal", "") for event in events(result)
    )
    assert text == f"Cannot contact {NAME}."
    assert adapters[1].masked_text(text) == "Cannot contact [PERSON_1]."


def test_chat_secret_review_has_the_same_local_gate(tmp_path):
    settings = Settings(identity=False, secret_review=True)
    sessions = open_sessions(tmp_path, settings, {}, api="openai")
    try:
        masker = sessions.get("fictional").chat_masker
        masker.mask(request("password is four fictional words here"))
        assert masker.review_findings()
    finally:
        sessions.close()


def request(text=EMAIL, **extra):
    return {
        "model": "fictional-model",
        "messages": [{"role": "user", "content": text}],
        **extra,
    }


def reply(text="Hi [PERSON_1] at [EMAIL_1].", **extra):
    return {
        "id": "chatcmpl_fixture",
        "object": "chat.completion",
        "model": "fictional-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        **extra,
    }


def chunk(text=None, *, reason=None, **delta):
    if text is not None:
        delta["content"] = text
    return {
        "id": "chatcmpl_fixture",
        "object": "chat.completion.chunk",
        "model": "fictional-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
    }


def frame(value):
    return "data: " + json.dumps(value) + "\n\n"


def events(source):
    return [
        json.loads(part.removeprefix("data: "))
        for part in source.split("\n\n")
        if part.startswith("data: {")
    ]


@pytest.fixture
def adapters():
    shield = shield_for()
    ledger = MemoryLedger()
    masker = ChatRequestMasker(shield, ledger, registered={NAME: "PERSON"})
    masker.mask(request(f"{NAME} {EMAIL}"))
    return shield, ledger, masker


def test_masks_all_supported_text_and_metadata_without_mutating(adapters):
    _, _, masker = adapters
    body = request(
        f"{NAME}: {EMAIL}",
        metadata={EMAIL: NAME},
        stop=[EMAIL],
        user=EMAIL,
        prompt_cache_key=EMAIL,
        stream=True,
        stream_options={"include_usage": True},
        n=1,
        store=False,
    )
    body["messages"] += [
        {"role": "system", "content": EMAIL},
        {"role": "developer", "content": NAME},
        {
            "role": "assistant",
            "name": NAME,
            "content": [{"type": "text", "text": EMAIL}],
        },
    ]
    before = copy.deepcopy(body)
    masked = masker.mask(body)
    assert EMAIL not in json.dumps(masked)
    assert NAME not in json.dumps(masked)
    assert "[EMAIL_1]" in json.dumps(masked)
    assert "[PERSON_1]" in json.dumps(masked)
    assert masked["store"] is False
    assert len(masked["user"]) == 64
    assert before == body


@pytest.mark.parametrize(
    "extra",
    [
        {"tools": []},
        {"functions": []},
        {"tool_choice": "auto"},
        {"web_search_options": {}},
        {"modalities": ["audio"]},
        {"audio": {}},
        {"prediction": {"content": EMAIL}},
        {"future": EMAIL},
        {"store": True},
        {"store": 0},
        {"n": 2},
        {"n": True},
        {"stream": 1},
        {"stream_options": {"include_usage": 1}},
        {"temperature": "private"},
        {"temperature": float("inf")},
        {"max_tokens": 1.5},
        {"response_format": {"type": "json_object"}},
        {"stop": [1]},
        {"reasoning_effort": EMAIL},
    ],
)
def test_unsupported_capabilities_refuse_without_values(adapters, extra):
    with pytest.raises(UnsupportedRequestError) as caught:
        adapters[2].mask(request(**extra))
    assert EMAIL not in str(caught.value)


@pytest.mark.parametrize(
    "message",
    [
        {"role": "tool", "content": EMAIL, "tool_call_id": "call_1"},
        {"role": "assistant", "tool_calls": [], "content": EMAIL},
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": EMAIL}}],
        },
        {
            "role": "user",
            "content": [{"type": "input_audio", "input_audio": {"data": EMAIL}}],
        },
        {"role": "user", "content": [{"type": "file", "file": {"file_id": EMAIL}}]},
        {"role": "user", "content": None},
        {"role": [], "content": EMAIL},
        {"role": "user", "content": [{"type": "text", "text": EMAIL, "future": EMAIL}]},
    ],
)
def test_unsupported_messages_refuse(adapters, message):
    body = request()
    body["messages"] = [message]
    with pytest.raises(UnsupportedRequestError):
        adapters[2].mask(body)


@pytest.mark.parametrize(
    "body", [None, [], {}, request(messages=[]), request(model=None)]
)
def test_bad_root_shape_refuses(adapters, body):
    with pytest.raises(UnsupportedRequestError):
        adapters[2].mask(body)


def test_json_restores_and_replays_exact_model_text(adapters):
    shield, ledger, masker = adapters
    source = reply()
    before = copy.deepcopy(source)
    result = ChatRestorer(shield, ledger).response(source)
    text = result["choices"][0]["message"]["content"]
    assert text == f"Hi {NAME} at {EMAIL}."
    assert source == before
    assert ledger.masked_text(text) == "Hi [PERSON_1] at [EMAIL_1]."
    history = request()
    history["messages"].append(result["choices"][0]["message"])
    assert (
        masker.mask(history)["messages"][-1]["content"]
        == source["choices"][0]["message"]["content"]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"object": "other"},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"index": 1, "finish_reason": "stop", "message": {}}]},
        {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {}}]},
        {"choices": [{"index": 0, "finish_reason": None, "message": {}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "tool_calls": []},
                }
            ]
        },
    ],
)
def test_json_reply_refuses_unrestorable_shapes(adapters, changes):
    with pytest.raises(StreamError):
        ChatRestorer(*adapters[:2]).response(reply(**changes))


@pytest.mark.parametrize("width", [1, 2, 7, 19, 4096])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_stream_split_placeholders_and_frames_replay(adapters, width, newline):
    shield, ledger, masker = adapters
    stream = ChatStreamRestorer(shield, ledger)
    source = ": comment\n\n" + frame(chunk(role="assistant"))
    for part in ["Hi [PER", "SON_1] at [EMAIL", "_1]."]:
        source += frame(chunk(part))
    source += frame(chunk(reason="stop"))
    source += frame(
        {
            "object": "chat.completion.chunk",
            "choices": [],
            "usage": {"total_tokens": 12},
        }
    )
    source += "data: [DONE]\n\n"
    source = source.replace("\n", newline)
    result = (
        "".join(
            stream.feed(source[i : i + width]) for i in range(0, len(source), width)
        )
        + stream.finish()
    )
    assert stream.completed
    assert stream.failed is None
    text = "".join(
        event["choices"][0]["delta"].get("content") or ""
        for event in events(result)
        if event.get("choices")
    )
    assert text == f"Hi {NAME} at {EMAIL}."
    assert ledger.masked_text(text) == "Hi [PERSON_1] at [EMAIL_1]."
    assert result.endswith("data: [DONE]\n\n")
    assert EMAIL not in json.dumps(masker.mask(request(text)))


@pytest.mark.parametrize(
    "source",
    [
        "data: [DONE]\n\n",
        "data: not json\n\n",
        "data: null\n\n",
        frame(chunk("[EMAIL_1]")),
        frame(chunk("[EMAIL_1]", reason="stop")),
        frame(chunk(reason="tool_calls", tool_calls=[])),
        frame(chunk("ok", reason="stop")) + frame(chunk("later")),
        frame(chunk("ok", reason="stop")) + "data: [DONE]\n\n" + frame(chunk("later")),
        frame(chunk("ok", reason="stop")) + "data: [DONE]\n\ntrailing",
    ],
)
def test_incomplete_or_invalid_stream_never_records_success(adapters, source):
    stream = ChatStreamRestorer(*adapters[:2])
    output = stream.feed(source) + stream.finish()
    assert not stream.completed or stream.failed
    assert adapters[1].masked_text(EMAIL) is None
    assert "not json" not in output
    assert "trailing" not in output


@pytest.fixture
def chat_gateway():
    api = FakeAPI()
    with Gateway(
        Sessions(shield_for, api="openai"),
        api="openai",
        upstream=api.host,
        secure=False,
    ) as gateway:
        yield api, gateway
    api.close()


def chat_call(gateway, body, **kwargs):
    headers = {"thread-id": "chat-test", "x-client-private": EMAIL, "Cookie": EMAIL}
    headers.update(kwargs.pop("headers", {}))
    return call(
        gateway, path="/v1/chat/completions", body=body, headers=headers, **kwargs
    )


def test_http_masks_and_restores_strips_headers_and_keeps_credentials(chat_gateway):
    api, gateway = chat_gateway
    api.replies.append((200, "application/json", [json.dumps(reply()).encode()]))
    response, data = chat_call(gateway, request(f"{NAME}: {EMAIL}"))
    assert response.status == 200
    assert (
        json.loads(data)["choices"][0]["message"]["content"] == f"Hi {NAME} at {EMAIL}."
    )
    method, path, headers, body = api.received[0]
    assert (method, path) == ("POST", "/v1/chat/completions")
    assert EMAIL not in body.decode()
    assert NAME not in body.decode()
    assert headers["Authorization"] == "Bearer user-token"
    lowered = {key.lower() for key in headers}
    assert not {"thread-id", "x-client-private", "cookie", "x-gateway-secret"} & lowered


def test_http_stream_and_incomplete_stream(chat_gateway):
    api, gateway = chat_gateway
    stream = (
        frame(chunk("[EMAIL")) + frame(chunk("_1]", reason="stop")) + "data: [DONE]\n\n"
    )
    api.replies.append((200, "text/event-stream", [part.encode() for part in stream]))
    response, data = chat_call(gateway, request(stream=True))
    assert response.status == 200
    assert EMAIL in data.decode()
    assert "data: [DONE]" in data.decode()
    api.replies.append((200, "text/event-stream", [frame(chunk("[EMAIL")).encode()]))
    _, data = chat_call(gateway, request(stream=True))
    assert "error" in data.decode()
    assert "[EMAIL" not in data.decode()


def test_http_review_gate_prevents_forwarding_until_local_decision(tmp_path):
    api = FakeAPI()
    try:
        sessions = open_sessions(
            tmp_path, Settings(identity=False, secret_review=True), {}, api="openai"
        )
        with Gateway(
            sessions, api="openai", upstream=api.host, secure=False
        ) as gateway:
            phrase = "four fictional words here"
            body = request("password is " + phrase)
            response, data = chat_call(gateway, body)
            assert response.status == 403
            assert phrase not in data.decode()
            assert api.received == []
            review = gateway.reviews.report()["reviews"][0]
            gateway.reviews.decide(review["id"], 0, "PASSWORD")
            api.replies.append(
                (200, "application/json", [json.dumps(reply("[PASSWORD_1]")).encode()])
            )
            response, data = chat_call(gateway, body)
            assert response.status == 200
            assert phrase not in api.received[0][3].decode()
            assert json.loads(data)["choices"][0]["message"]["content"] == phrase
    finally:
        api.close()


def test_chat_reply_size_limit_is_enforced_before_sending(chat_gateway, monkeypatch):
    from veil.gateway import server

    api, gateway = chat_gateway
    monkeypatch.setattr(server, "_MAX_CHAT_REPLY", 100)
    api.replies.append(
        (200, "application/json", [json.dumps(reply("x" * 200)).encode()])
    )
    response, data = chat_call(gateway, request())
    assert response.status == 502
    assert "exceeds the limit" in data.decode()
    assert "x" * 100 not in data.decode()


def test_stream_size_limit_refuses_without_echoing_payload(adapters, monkeypatch):
    from veil.gateway import chat

    monkeypatch.setattr(chat, "_LIMIT", 20)
    stream = ChatStreamRestorer(*adapters[:2])
    output = stream.feed("x" * 21)
    assert stream.failed
    assert "x" * 21 not in output
    assert not stream.completed


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (request(tools=[]), {}),
        (request(), {"thread-id": None}),
        (request(), {"x-gateway-secret": "wrong"}),
        (request(), {"Origin": "https://example.com"}),
    ],
)
def test_http_refusals_do_not_forward(chat_gateway, body, headers):
    api, gateway = chat_gateway
    response, _ = chat_call(gateway, body, headers=headers)
    assert response.status in {400, 401, 403}
    assert api.received == []


def test_subscription_auth_cannot_send_chat_completions():
    api = FakeAPI()
    try:
        with Gateway(
            Sessions(shield_for, api="openai"),
            api="openai",
            openai_auth="chatgpt",
            upstream=api.host,
            secure=False,
        ) as gateway:
            response, _ = chat_call(
                gateway, request(), headers={"chatgpt-account-id": "fictional"}
            )
            assert response.status == 400
            assert api.received == []
    finally:
        api.close()


def test_activity_verification_counts_chat_restoration(chat_gateway):
    api, gateway = chat_gateway
    _, data = call(gateway, path=VERIFY_PATH, body={})
    probe = json.loads(data)
    text = probe["prompt"]
    placeholder = "[EMAIL_1]"
    api.replies.append(
        (200, "application/json", [json.dumps(reply(placeholder)).encode()])
    )
    response, _ = chat_call(gateway, request(text))
    assert response.status == 200
    _, data = call(
        gateway, method="GET", path=VERIFY_PATH + "?id=" + probe["verification_id"]
    )
    assert json.loads(data)["state"] == "verified"
    _, data = call(gateway, method="GET", path=ACTIVITY_PATH)
    assert EMAIL not in data.decode()


def test_persistent_sessions_replay_after_gateway_restart(tmp_path):
    api = FakeAPI()
    settings = Settings(identity=False, entities={"PERSON": (NAME,)}, note=False)
    try:
        history = request(f"{NAME} {EMAIL}")
        for turn in range(2):
            sessions = open_sessions(tmp_path, settings, {}, api="openai")
            with Gateway(
                sessions, api="openai", upstream=api.host, secure=False
            ) as gateway:
                api.replies.append(
                    (200, "application/json", [json.dumps(reply()).encode()])
                )
                response, data = chat_call(gateway, history)
                assert response.status == 200
                assert EMAIL not in api.received[-1][3].decode()
                if turn == 0:
                    history["messages"].append(
                        json.loads(data)["choices"][0]["message"]
                    )
                    history["messages"].append({"role": "user", "content": "Continue"})
        assert (
            json.loads(api.received[-1][3])["messages"][1]["content"]
            == "Hi [PERSON_1] at [EMAIL_1]."
        )
    finally:
        api.close()
