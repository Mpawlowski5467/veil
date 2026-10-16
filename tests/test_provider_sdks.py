"""Opt-in official SDK contracts against fictional providers on loopback only."""

import asyncio
import json
import os

import pytest

from test_chat_gateway import chunk, frame, reply
from test_gateway_server import FakeAPI, sse, stream_reply
from test_openai_gateway import EMAIL, shield_for
from veil.gateway import Gateway, Sessions

pytestmark = pytest.mark.skipif(
    os.environ.get("VEIL_PROVIDER_SDKS") != "1",
    reason="opt-in installed OpenAI/Anthropic SDK contracts (local only)",
)


@pytest.fixture(params=["openai", "anthropic"])
def sdk_gateway(request):
    api = FakeAPI()
    with Gateway(
        Sessions(shield_for, api=request.param),
        api=request.param,
        upstream=api.host,
        secure=False,
    ) as gateway:
        yield api, gateway
    api.close()


def client_for(gateway, *, asynchronous=False):
    if gateway.api == "openai":
        sdk = pytest.importorskip("openai")
        client = sdk.AsyncOpenAI if asynchronous else sdk.OpenAI
        return client(
            api_key="fictional-api-key",
            base_url=gateway.url + "/v1",
            max_retries=0,
            default_headers={
                "x-gateway-secret": gateway.secret,
                "thread-id": "sdk-fixture",
            },
        )
    sdk = pytest.importorskip("anthropic")
    client = sdk.AsyncAnthropic if asynchronous else sdk.Anthropic
    return client(
        api_key="fictional-api-key",
        base_url=gateway.url,
        max_retries=0,
        default_headers={
            "x-gateway-secret": gateway.secret,
            "x-claude-code-session-id": "sdk-fixture",
        },
    )


def api_reply(api, gateway, *, stream=False):
    if gateway.api == "openai":
        if stream:
            source = (
                frame(chunk("[EMAIL"))
                + frame(chunk("_1]", reason="stop"))
                + "data: [DONE]\n\n"
            )
            api.replies.append((200, "text/event-stream", [source.encode()]))
        else:
            api.replies.append(
                (200, "application/json", [json.dumps(reply("[EMAIL_1]")).encode()])
            )
        return
    message = {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": "fictional-model",
        "content": [{"type": "text", "text": "[EMAIL_1]"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    if stream:
        api.replies.append(
            stream_reply(
                sse(
                    {
                        "type": "message_start",
                        "message": {**message, "content": [], "stop_reason": None},
                    }
                ),
                sse(
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    }
                ),
                sse(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "[EMAIL"},
                    }
                ),
                sse(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "_1]"},
                    }
                ),
                sse({"type": "content_block_stop", "index": 0}),
                sse(
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 1},
                    }
                ),
                sse({"type": "message_stop"}),
            )
        )
    else:
        api.replies.append((200, "application/json", [json.dumps(message).encode()]))


@pytest.mark.parametrize("streaming", [False, True])
def test_sync_official_sdk_text_round_trip(sdk_gateway, streaming):
    api, gateway = sdk_gateway
    api_reply(api, gateway, stream=streaming)
    with client_for(gateway) as client:
        arguments = {
            "model": "fictional-model",
            "messages": [{"role": "user", "content": EMAIL}],
            "stream": streaming,
        }
        if gateway.api == "openai":
            result = client.chat.completions.create(**arguments)
            text = (
                "".join(part.choices[0].delta.content or "" for part in result)
                if streaming
                else result.choices[0].message.content
            )
        else:
            result = client.messages.create(**arguments, max_tokens=32)
            text = (
                "".join(
                    part.delta.text
                    for part in result
                    if part.type == "content_block_delta"
                )
                if streaming
                else result.content[0].text
            )
    assert text == EMAIL
    assert EMAIL not in api.received[0][3].decode()
    headers = {key.lower(): value for key, value in api.received[0][2].items()}
    assert "x-gateway-secret" not in headers
    assert "fictional-api-key" in (
        headers.get("authorization", "") + headers.get("x-api-key", "")
    )


def test_async_official_sdk_text_round_trip(sdk_gateway):
    api, gateway = sdk_gateway
    api_reply(api, gateway)

    async def run():
        async with client_for(gateway, asynchronous=True) as client:
            arguments = {
                "model": "fictional-model",
                "messages": [{"role": "user", "content": EMAIL}],
            }
            if gateway.api == "openai":
                result = await client.chat.completions.create(**arguments)
                return result.choices[0].message.content
            result = await client.messages.create(**arguments, max_tokens=32)
            return result.content[0].text

    assert asyncio.run(run()) == EMAIL
    assert EMAIL not in api.received[0][3].decode()
