"""Responses masking and restoration, with no external model requests."""

import copy
import json

import pytest

from test_gateway_server import FakeAPI, call, sse, stream_reply
from veil import LiteralPlaceholderDetector, RegexDetector, Shield
from veil.gateway import (
    Gateway,
    MemoryLedger,
    ResponsesRequestMasker,
    ResponsesRestorer,
    ResponsesStreamRestorer,
    Sessions,
    Settings,
    UnsupportedRequestError,
    open_sessions,
)
from veil.gateway.response import StreamError

EMAIL = "jane.doe@example.com"
NAME = 'Jan "JJ" Nowak'


def shield_for(_session="unused"):
    shield = Shield(
        detectors=[LiteralPlaceholderDetector({"EMAIL", "PERSON"}), RegexDetector()],
        redact_warnings=True,
    )
    shield.add_entity(NAME, "PERSON")
    return shield


@pytest.fixture
def adapters():
    shield = shield_for()
    ledger = MemoryLedger()
    return (
        shield,
        ledger,
        ResponsesRequestMasker(shield, ledger, registered={NAME: "PERSON"}),
        ResponsesRestorer(shield, ledger),
    )


def message(text, role="user"):
    return {
        "role": role,
        "type": "message",
        "content": [{"type": "input_text", "text": text}],
    }


def reply_item(text="Hello [PERSON_1]."):
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


class TestRequests:
    def test_masks_each_user_data_surface_without_changing_the_request(self, adapters):
        _, _, masker, _ = adapters
        body = {
            "model": "gpt-6-sol",
            "instructions": f"Write to {NAME}",
            "input": [
                message(f"Email {NAME} at {EMAIL}"),
                {
                    "type": "function_call",
                    "name": "contact",
                    "call_id": "c1",
                    "arguments": json.dumps({EMAIL: NAME}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": f"Found {NAME}",
                },
                {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "c2",
                    "input": f"+{NAME}",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "c2",
                    "output": [{"type": "input_text", "text": EMAIL}],
                },
            ],
            "metadata": {EMAIL: NAME},
            "client_metadata": {"cwd": f"/home/{NAME}"},
            "prompt_cache_key": EMAIL,
            "user": EMAIL,
            "safety_identifier": EMAIL,
            "stream": True,
            "parallel_tool_calls": True,
        }
        original = copy.deepcopy(body)
        masked = masker.mask(body)
        serialized = json.dumps(masked)
        assert EMAIL not in serialized
        assert NAME not in serialized
        assert json.dumps(NAME)[1:-1] not in serialized
        assert "[PERSON_1]" in serialized
        assert "[EMAIL_1]" in serialized
        assert masked["store"] is False
        assert len(masked["prompt_cache_key"]) == 64
        assert body == original

    def test_accepts_the_codex_additional_tools_shape(self, adapters):
        _, _, masker, _ = adapters
        tools = [
            {
                "type": "namespace",
                "name": "functions",
                "description": "Local tools",
                "tools": [
                    {
                        "type": "function",
                        "name": "exec_command",
                        "description": "Run a command",
                        "parameters": {"type": "object"},
                        "strict": False,
                    }
                ],
            }
        ]
        body = {
            "model": "gpt-6-sol",
            "input": [
                {
                    "type": "additional_tools",
                    "id": "tools_1",
                    "role": "system",
                    "tools": tools,
                },
                message(EMAIL),
            ],
        }
        masked = masker.mask(body)
        assert masked["input"][0]["tools"] == tools
        assert masked["input"][1]["content"][0]["text"] == "[EMAIL_1]"

    @pytest.mark.parametrize(
        "extra",
        [
            {"future_field": EMAIL},
            {"store": True},
            {"store": 0},
            {"background": True},
            {"previous_response_id": "resp_old"},
            {"conversation": "conv_old"},
            {"stream": EMAIL},
            {"temperature": EMAIL},
            {"reasoning": {"future": EMAIL}},
            {"reasoning": {"context": EMAIL}},
            {"text": {"format": {"type": "json_schema", "schema": {}}}},
            {"tools": [{"type": "web_search"}]},
            {"tools": [{"type": "mcp"}]},
            {"include": ["file_search_call.results"]},
            {"tool_choice": {"type": "web_search"}},
        ],
    )
    def test_refuses_unsupported_root_fields_and_capabilities(self, adapters, extra):
        with pytest.raises(UnsupportedRequestError) as error:
            adapters[2].mask({"input": "hello", **extra})
        assert EMAIL not in str(error.value)

    @pytest.mark.parametrize(
        "item",
        [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_file", "file_id": "file_old"}],
            },
            {"type": "message", "role": "user", "content": "hello", "future": EMAIL},
            {"type": "item_reference", "id": "old"},
            {
                "type": "function_call",
                "name": "foo",
                "call_id": "c1",
                "arguments": "not JSON",
            },
            {
                "type": "reasoning",
                "id": "r1",
                "summary": [],
                "encrypted_content": "opaque",
            },
        ],
    )
    def test_refuses_unknown_content_and_inherited_state(self, adapters, item):
        with pytest.raises(UnsupportedRequestError):
            adapters[2].mask({"input": [item]})

    def test_replays_only_reasoning_seen_in_this_session(self, adapters):
        _, _, masker, restorer = adapters
        item = {
            "type": "reasoning",
            "id": "r1",
            "summary": [],
            "encrypted_content": "opaque-signed-value",
        }
        restorer.item(item)
        assert masker.mask({"input": [item]})["input"][0] == item
        with pytest.raises(UnsupportedRequestError):
            masker.mask({"input": [{**item, "encrypted_content": "changed"}]})

    def test_history_replays_the_original_tool_arguments(self, adapters):
        _, _, masker, restorer = adapters
        masker.mask({"input": f"Write to {NAME} at {EMAIL}"})
        model_item = {
            "type": "function_call",
            "call_id": "c1",
            "name": "write",
            "arguments": json.dumps({"body": "[PERSON_1]em, [EMAIL_1]"}),
        }
        restored = restorer.item(model_item)
        replay = masker.mask({"input": [restored]})["input"][0]
        assert json.loads(replay["arguments"]) == json.loads(model_item["arguments"])
        assert "LITERAL" not in replay["arguments"]

    def test_refusal_history_reuses_the_model_text(self, adapters):
        _, _, masker, restorer = adapters
        masker.mask({"input": EMAIL})
        item = reply_item()
        item["content"] = [{"type": "refusal", "refusal": "Cannot email [EMAIL_1]."}]
        restored = restorer.item(item)
        assert restored["content"][0]["refusal"] == f"Cannot email {EMAIL}."
        assert masker.mask({"input": [restored]})["input"][0] == item

    def test_tool_replay_masks_newly_known_keys_without_remasking_placeholders(
        self, adapters
    ):
        shield, _, masker, restorer = adapters
        masker.mask({"input": NAME})
        item = {
            "type": "function_call",
            "call_id": "c1",
            "name": "write",
            "arguments": '{"Alice":"[PERSON_1]"}',
        }
        restored = restorer.item(item)
        shield.add_entity("Alice", "PERSON")
        shield.mask("Alice")
        replay = masker.mask({"input": [restored]})["input"][0]
        assert json.loads(replay["arguments"]) == {"[PERSON_2]": "[PERSON_1]"}

    def test_new_registration_is_applied_to_cached_text(self, adapters):
        shield, _, masker, _ = adapters
        assert masker.mask({"input": "Meet Alice"})["input"] == "Meet Alice"
        shield.add_entity("Alice", "PERSON")
        shield.mask("Alice")
        assert masker.mask({"input": "Meet Alice"})["input"] == "Meet [PERSON_1]"

    def test_persistent_reasoning_and_tools_survive_reopening(self, tmp_path):
        settings = Settings(entities={"PERSON": (NAME,)})
        with open_sessions(tmp_path, settings, {}, api="openai") as sessions:
            session = sessions.get("s1")
            session.masker.mask({"input": NAME})
            restorer = ResponsesRestorer(session.shield, session.ledger)
            reasoning = restorer.item(
                {
                    "type": "reasoning",
                    "id": "r1",
                    "summary": [],
                    "encrypted_content": "opaque",
                }
            )
            tool = restorer.item(
                {
                    "type": "custom_tool_call",
                    "call_id": "c1",
                    "name": "patch",
                    "input": "+[PERSON_1]em",
                }
            )
        with open_sessions(tmp_path, settings, {}, api="openai") as sessions:
            result = sessions.get("s1").masker.mask({"input": [reasoning, tool]})
            assert result["input"][1]["input"] == "+[PERSON_1]em"
            with pytest.raises(UnsupportedRequestError):
                sessions.get("s2").masker.mask({"input": [reasoning]})


def test_a_refusal_never_names_a_known_value():
    # A single-word registered name used as a key is shown as <key>.
    shield = shield_for()
    shield.add_entity("Quillsby", "PERSON")
    masker = ResponsesRequestMasker(
        shield, MemoryLedger(), registered={NAME: "PERSON", "Quillsby": "PERSON"}
    )
    for body in ({"input": "hi", "Quillsby": 1}, {"input": [{"Quillsby": 1}]}):
        with pytest.raises(UnsupportedRequestError) as info:
            masker.mask(body)
        assert "Quillsby" not in str(info.value)


class TestRestoration:
    @pytest.mark.parametrize(
        "name", ["exec_command", "functions.exec", "mcp__remote__write", None]
    )
    def test_guard_blocks_private_tool_arguments(self, adapters, name):
        shield, ledger, masker, _ = adapters
        masker.mask({"input": EMAIL})
        restorer = ResponsesRestorer(shield, ledger, guard_tools=True)
        with pytest.raises(StreamError, match="blocked a tool"):
            restorer.tool("c1", '{"cmd":"echo [EMAIL_1]"}', function=True, name=name)
        assert ledger.masked_tool_input("c1", {"cmd": f"echo {EMAIL}"}) is None

    def test_guard_allows_direct_local_patch_and_non_private_calls(self, adapters):
        shield, ledger, masker, _ = adapters
        masker.mask({"input": EMAIL})
        restorer = ResponsesRestorer(shield, ledger, guard_tools=True)
        assert (
            restorer.tool("c1", "+[EMAIL_1]", function=False, name="apply_patch")
            == f"+{EMAIL}"
        )
        assert json.loads(
            restorer.tool("c2", '{"cmd":"pwd"}', function=True, name="exec_command")
        ) == {"cmd": "pwd"}

    def test_function_values_and_keys_remain_valid_json(self, adapters):
        _, _, masker, restorer = adapters
        masker.mask({"input": f"{NAME} {EMAIL}"})
        restored = restorer.tool(
            "c1", '{"[EMAIL_1]":"[PERSON_1]","literal":"[Person 1]"}', function=True
        )
        assert json.loads(restored) == {EMAIL: NAME, "literal": "[Person 1]"}
        assert (
            restorer.tool("c2", "+[PERSON_1] [Person 1]", function=False)
            == f"+{NAME} [Person 1]"
        )

    def test_invalid_json_is_not_delivered_as_a_tool_call(self, adapters):
        with pytest.raises(StreamError):
            adapters[3].tool("c1", '{"name":"[PERSON_1]', function=True)


def events(*data):
    return b"".join(sse(item) for item in data).decode()


def parse_events(raw):
    return [
        json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: {")
    ]


def text_events():
    full = "Hi [PERSON_1], mail [EMAIL_1]."
    common = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    return events(
        {"type": "response.created", "response": {"id": "resp_1", "output": []}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": reply_item(""),
        },
        *[
            {"type": "response.output_text.delta", **common, "delta": piece}
            for piece in ["Hi [PER", "SON_1], mail [EMAIL", "_1]."]
        ],
        {"type": "response.output_text.done", **common, "text": full},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": reply_item(full),
        },
        {
            "type": "response.completed",
            "response": {"id": "resp_1", "output": [reply_item(full)]},
        },
    )


class TestStreams:
    def test_guard_emits_no_executable_private_input(self, adapters):
        shield, ledger, masker, _ = adapters
        masker.mask({"input": EMAIL})
        stream = ResponsesStreamRestorer(shield, ledger, guard_tools=True)
        source = '{"cmd":"echo [EMAIL_1]"}'
        output = stream.feed(
            events(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "name": "exec_command",
                        "id": "t1",
                        "call_id": "c1",
                        "arguments": "",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "t1",
                    "delta": source,
                },
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "t1",
                    "arguments": source,
                },
            )
        )
        parsed = parse_events(output)
        assert EMAIL not in output
        assert source not in output
        assert [e["type"] for e in parsed] == ["response.output_item.added", "error"]
        assert "blocked a tool" in parsed[-1]["message"]

    @pytest.mark.parametrize(
        ("family", "field"),
        [
            ("response.reasoning_summary_text", "text"),
            ("response.reasoning_text", "text"),
            ("response.refusal", "refusal"),
        ],
    )
    def test_other_visible_text_families_are_restored(self, adapters, family, field):
        shield, ledger, masker, _ = adapters
        masker.mask({"input": EMAIL})
        stream = ResponsesStreamRestorer(shield, ledger)
        identity = {"item_id": "r1", "summary_index": 0}
        out = stream.feed(
            events(
                {"type": family + ".delta", **identity, "delta": "Mail [EMA"},
                {"type": family + ".delta", **identity, "delta": "IL_1]."},
                {"type": family + ".done", **identity, field: "Mail [EMAIL_1]."},
                {"type": "response.completed", "response": {"output": []}},
            )
        )
        assert stream.finish() == ""
        parsed = parse_events(out)
        assert "".join(e["delta"] for e in parsed if "delta" in e) == f"Mail {EMAIL}."
        assert parsed[-2][field] == f"Mail {EMAIL}."

    @pytest.mark.parametrize("chunk_size", [1, 2, 7, 53, 100000])
    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_split_placeholders_and_frames(self, adapters, chunk_size, newline):
        shield, ledger, masker, _ = adapters
        masker.mask({"input": f"{NAME} {EMAIL}"})
        stream = ResponsesStreamRestorer(shield, ledger)
        source = text_events().replace("\n", newline)
        output = (
            "".join(
                stream.feed(source[i : i + chunk_size])
                for i in range(0, len(source), chunk_size)
            )
            + stream.finish()
        )
        parsed = parse_events(output)
        assert stream.failed is None
        text = "".join(
            e["delta"] for e in parsed if e["type"] == "response.output_text.delta"
        )
        assert text == f"Hi {NAME}, mail {EMAIL}."
        assert parsed[-1]["response"]["output"][0]["content"][0]["text"] == text
        assert [e["sequence_number"] for e in parsed] == list(range(len(parsed)))
        assert ledger.masked_text(text) == "Hi [PERSON_1], mail [EMAIL_1]."

    @pytest.mark.parametrize("function", [True, False])
    def test_holds_tool_input_until_complete(self, adapters, function):
        shield, ledger, masker, _ = adapters
        masker.mask({"input": NAME})
        stream = ResponsesStreamRestorer(shield, ledger)
        kind, key, family = (
            ("function_call", "arguments", "response.function_call_arguments")
            if function
            else ("custom_tool_call", "input", "response.custom_tool_call_input")
        )
        source = '{"name":"[PERSON_1]"}' if function else "Write [PERSON_1]"
        tool = {"type": kind, "id": "tool_1", "call_id": "c1", "name": "write", key: ""}
        stream.feed(
            events(
                {"type": "response.output_item.added", "output_index": 0, "item": tool}
            )
        )
        common = {"item_id": "tool_1", "output_index": 0}
        for piece in (source[:5], source[5:]):
            assert (
                stream.feed(
                    events({"type": family + ".delta", **common, "delta": piece})
                )
                == ""
            )
        out = stream.feed(events({"type": family + ".done", **common, key: source}))
        parsed = parse_events(out)
        restored = parsed[0]["delta"]
        assert (json.loads(restored)["name"] if function else restored) == (
            NAME if function else f"Write {NAME}"
        )
        assert parsed[1][key] == restored
        stream.feed(
            events(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {**tool, key: source},
                },
                {
                    "type": "response.completed",
                    "response": {"output": [{**tool, key: source}]},
                },
            )
        )
        assert stream.finish() == ""
        assert stream.failed is None

    def test_invalid_completed_tool_is_an_error_without_raw_arguments(self, adapters):
        shield, ledger, _, _ = adapters
        stream = ResponsesStreamRestorer(shield, ledger)
        stream.feed(
            events(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "id": "t1",
                        "call_id": "c1",
                        "name": "write",
                        "arguments": "",
                    },
                }
            )
        )
        output = stream.feed(
            events(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "t1",
                    "arguments": '{"x":"secret',
                }
            )
        )
        assert "secret" not in output
        assert parse_events(output)[0]["type"] == "error"
        assert stream.failed
        assert stream.feed(text_events()) == ""

    @pytest.mark.parametrize(
        "source",
        [
            'data: {"type":"response.output_text.delta"',
            "data: not-json\n\n",
            "data: [DONE]\n\n",
            events(
                {"type": "response.created", "response": {"id": "r1", "output": []}}
            ),
        ],
    )
    def test_truncated_or_malformed_stream_fails_closed(self, adapters, source):
        stream = ResponsesStreamRestorer(adapters[0], adapters[1])
        output = stream.feed(source) + stream.finish()
        assert parse_events(output)[-1]["type"] == "error"
        assert stream.failed


@pytest.fixture
def api():
    fake = FakeAPI()
    yield fake
    fake.close()


@pytest.fixture
def gateway(api):
    with Gateway(
        Sessions(shield_for, api="openai"),
        api="openai",
        upstream=api.host,
        secure=False,
    ) as gw:
        yield gw


class TestHTTP:
    def test_chatgpt_routes_auth_and_parses_sse_without_content_type(self, api):
        api.replies.append((200, "", [text_events().encode()]))
        with Gateway(
            Sessions(shield_for, api="openai"),
            api="openai",
            openai_auth="chatgpt",
            upstream=api.host,
            secure=False,
        ) as gateway:
            response, payload = call(
                gateway,
                path="/v1/responses",
                headers={
                    "thread-id": "s1",
                    "chatgpt-account-id": "fictional-account",
                    "x-openai-internal-codex-responses-lite": "true",
                },
                body={"input": f"{NAME} {EMAIL}", "stream": True},
            )
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream"
        assert EMAIL in payload.decode()
        _, path, headers, body = api.received[0]
        assert path == "/backend-api/codex/responses"
        assert EMAIL.encode() not in body
        lowered = {k.lower(): v for k, v in headers.items()}
        assert lowered["chatgpt-account-id"] == "fictional-account"
        assert "x-openai-internal-codex-responses-lite" not in lowered

    @pytest.mark.parametrize("auth", ["chatgpt", "api-key"])
    def test_authentication_modes_cannot_be_mixed(self, api, auth):
        with Gateway(
            Sessions(shield_for, api="openai"),
            api="openai",
            openai_auth=auth,
            upstream=api.host,
            secure=False,
        ) as gateway:
            headers = {"thread-id": "s1"}
            if auth == "api-key":
                headers["chatgpt-account-id"] = "wrong-mode"
            response, _ = call(
                gateway, path="/v1/responses", headers=headers, body={"input": EMAIL}
            )
        assert response.status == 400
        assert api.received == []

    def test_chatgpt_model_catalog(self, api):
        api.replies.append((200, "application/json", [b'{"models":[]}']))
        with Gateway(
            Sessions(shield_for, api="openai"),
            api="openai",
            openai_auth="chatgpt",
            upstream=api.host,
            secure=False,
        ) as gateway:
            response, _ = call(
                gateway,
                method="GET",
                path="/v1/models?client_version=0.156.1",
                headers={"chatgpt-account-id": "fictional"},
            )
        assert response.status == 200
        assert api.received[0][1] == "/backend-api/codex/models?client_version=0.156.1"

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/models?secret=private",
            "/v1/models?client_version=jane@example.com",
            "/v1/models?client_version=0.1.0&client_version=0.2.0",
        ],
    )
    def test_unknown_model_queries_are_not_forwarded(self, api, gateway, path):
        response, _ = call(gateway, method="GET", path=path)
        assert response.status == 400
        assert api.received == []

    def test_a_refusal_speaks_to_codex_not_claude_code(self, api, gateway):
        response, payload = call(
            gateway,
            path="/v1/responses",
            headers={"thread-id": "s1"},
            body={"input": EMAIL, "future_field": 1},
        )
        assert response.status == 400
        error = json.loads(payload)["error"]
        assert error["type"] == "invalid_request_error"
        assert "future_field" in error["message"]
        assert "Claude Code" not in error["message"]
        assert "/rewind" not in error["message"]
        assert EMAIL not in payload.decode()
        assert api.received == []

    @pytest.mark.parametrize(
        ("content_type", "body"),
        [("application/json", b"{invalid"), ("text/plain", b"not a Responses reply")],
    )
    def test_unreadable_successful_reply_is_an_error(
        self, api, gateway, content_type, body
    ):
        api.replies.append((200, content_type, [body]))
        response, payload = call(
            gateway,
            path="/v1/responses",
            headers={"thread-id": "s1"},
            body={"input": EMAIL},
        )
        assert response.status == 502
        assert json.loads(payload)["error"]["type"] == "api_error"
        assert body not in payload

    def test_json_request_and_reply(self, api, gateway):
        api.replies.append(
            (200, "application/json", [json.dumps({"output": [reply_item()]}).encode()])
        )
        response, body = call(
            gateway,
            path="/v1/responses",
            headers={"thread-id": "s1", "x-codex-turn-metadata": NAME},
            body={"model": "gpt-6-sol", "input": NAME},
        )
        assert response.status == 200
        assert json.loads(body)["output"][0]["content"][0]["text"] == f"Hello {NAME}."
        _, path, headers, request = api.received[0]
        assert path == "/v1/responses"
        assert json.loads(request)["input"] == "[PERSON_1]"
        lowered = {k.lower(): v for k, v in headers.items()}
        assert lowered["authorization"] == "Bearer user-token"
        assert "x-gateway-secret" not in lowered
        assert "thread-id" not in lowered
        assert "x-codex-turn-metadata" not in lowered

    def test_streamed_request_and_reply(self, api, gateway):
        api.replies.append(stream_reply(text_events().encode()))
        response, body = call(
            gateway,
            path="/v1/responses",
            headers={"thread-id": "s1"},
            body={"input": f"{NAME} {EMAIL}", "stream": True},
        )
        assert response.status == 200
        parsed = parse_events(body.decode())
        assert parsed[-1]["type"] == "response.completed"
        assert (
            parsed[-1]["response"]["output"][0]["content"][0]["text"]
            == f"Hi {NAME}, mail {EMAIL}."
        )

    @pytest.mark.parametrize(
        ("path", "headers", "body", "status"),
        [
            ("/v1/responses", {}, {"input": EMAIL}, 400),
            (
                "/v1/responses",
                {"thread-id": "s1", "x-gateway-secret": "wrong"},
                {"input": EMAIL},
                401,
            ),
            (
                "/v1/responses",
                {"thread-id": "s1", "Origin": "https://example.com"},
                {"input": EMAIL},
                403,
            ),
            ("/v1/responses?email=" + EMAIL, {"thread-id": "s1"}, {"input": "hi"}, 400),
            ("/v1/responses/compact", {"thread-id": "s1"}, {"input": EMAIL}, 404),
            (
                "/v1/responses",
                {"thread-id": "s1"},
                {"input": EMAIL, "future": EMAIL},
                400,
            ),
        ],
    )
    def test_rejected_requests_never_reach_upstream(
        self, api, gateway, path, headers, body, status
    ):
        response, payload = call(gateway, path=path, headers=headers, body=body)
        assert response.status == status
        assert EMAIL.encode() not in payload
        assert api.received == []

    def test_api_mismatch_is_rejected(self, api):
        with Gateway(
            Sessions(shield_for), api="openai", upstream=api.host, secure=False
        ) as gw:
            response, _ = call(
                gw,
                path="/v1/responses",
                headers={"thread-id": "s1"},
                body={"input": EMAIL},
            )
        assert response.status == 500
        assert api.received == []
