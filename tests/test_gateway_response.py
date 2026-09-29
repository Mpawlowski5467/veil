"""Restoring streamed replies: text as it arrives, tool calls in one piece."""

import json
import random

import pytest

from veil import MemoryVault, Shield
from veil.gateway import (
    MemoryLedger,
    RequestMasker,
    ResponseRestorer,
    StreamError,
    restore_message,
)

EMAIL = "jane.doe@example.com"
NAME = "Jan Nowak"
ODD = 'Ada "Q" \\ Quill'  # a value that needs escaping inside JSON


def make_shield():
    shield = Shield(vault=MemoryVault())
    shield.add_entity(NAME, "PERSON")
    shield.add_entity(ODD, "PERSON")
    shield.mask(f"{NAME}, {ODD}, {EMAIL}")  # [PERSON_1], [PERSON_2], [EMAIL_1]
    return shield


def sse(data):
    return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"


def start(index, block):
    return sse({"type": "content_block_start", "index": index, "content_block": block})


def delta(index, body):
    return sse({"type": "content_block_delta", "index": index, "delta": body})


def stop(index):
    return sse({"type": "content_block_stop", "index": index})


def text_block(index, pieces):
    return (
        start(index, {"type": "text", "text": ""})
        + "".join(delta(index, {"type": "text_delta", "text": p}) for p in pieces)
        + stop(index)
    )


def tool_block(index, tool_id, pieces):
    return (
        start(index, {"type": "tool_use", "id": tool_id, "name": "Write", "input": {}})
        + "".join(
            delta(index, {"type": "input_json_delta", "partial_json": p})
            for p in pieces
        )
        + stop(index)
    )


THINKING = (
    start(0, {"type": "thinking", "thinking": "", "signature": ""})
    + delta(0, {"type": "thinking_delta", "thinking": "Use [EMAIL_1] as given."})
    + delta(0, {"type": "signature_delta", "signature": "c2lnbmF0dXJl"})
    + stop(0)
)
TOOL_INPUT = json.dumps(
    {
        "file_path": "/work/out.txt",
        "content": "To [EMAIL_1] from [PERSON_2], [person 1]",
    }
)


def reply():
    return (
        sse({"type": "message_start", "message": {"id": "msg_1", "content": []}})
        + THINKING
        + text_block(1, ["Writing to [EMA", "IL_1] for [pers", "on 1] now."])
        + tool_block(
            2, "toolu_1", [TOOL_INPUT[:30], TOOL_INPUT[30:45], TOOL_INPUT[45:]]
        )
        + sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
        + ": keep-alive comment\n\n"
        + sse({"type": "message_stop"})
    )


def parse(stream):
    out = []
    for raw in stream.split("\n\n"):
        data = [
            line[5:].strip() for line in raw.split("\n") if line.startswith("data:")
        ]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


def run(shield, ledger, stream, sizes=None):
    restorer = ResponseRestorer(shield, ledger)
    rng = random.Random(sizes)
    out, i = [], 0
    while i < len(stream):
        n = rng.randint(1, 60) if sizes is not None else len(stream)
        out.append(restorer.feed(stream[i : i + n]))
        i += n
    out.append(restorer.finish())
    return "".join(out), restorer


def blocks(events):
    found = {}
    for event in events:
        if event["type"] == "content_block_start":
            found[event["index"]] = {"start": event["content_block"], "deltas": []}
        elif event["type"] == "content_block_delta":
            found[event["index"]]["deltas"].append(event["delta"])
    return found


class TestStreaming:
    def test_text_is_restored_across_pieces(self):
        out, _ = run(make_shield(), MemoryLedger(), reply())
        text = "".join(d["text"] for d in blocks(parse(out))[1]["deltas"])
        assert text == f"Writing to {EMAIL} for {NAME} now."

    def test_a_tool_call_comes_in_one_piece_restored_exactly(self):
        out, _ = run(make_shield(), MemoryLedger(), reply())
        tool = blocks(parse(out))[2]
        assert tool["start"] == {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "Write",
            "input": {},
        }
        assert len(tool["deltas"]) == 1
        restored = json.loads(tool["deltas"][0]["partial_json"])
        # Exact placeholders only: a rewritten form in a tool input stays.
        assert restored == {
            "file_path": "/work/out.txt",
            "content": f"To {EMAIL} from {ODD}, [person 1]",
        }

    def test_placeholders_in_tool_input_keys_are_restored(self):
        shield, ledger = make_shield(), MemoryLedger()
        masked = {"[EMAIL_1]": {"type": "[PERSON_1]", "[person 1]": 2}, "limit": 5}
        stream = tool_block(0, "toolu_1", [json.dumps(masked)])
        out, _ = run(shield, ledger, stream)
        restored = json.loads(blocks(parse(out))[0]["deltas"][0]["partial_json"])
        # Exact placeholders only, as for values.
        assert restored == {EMAIL: {"type": NAME, "[person 1]": 2}, "limit": 5}
        assert ledger.masked_tool_input("toolu_1", restored) == masked

    def test_tool_input_keys_that_restore_alike_end_the_stream(self):
        shield, ledger = make_shield(), MemoryLedger()
        colliding = json.dumps({"to": {"[EMAIL_1]": 1, EMAIL: 2}})
        out, restorer = run(shield, ledger, tool_block(0, "toolu_1", [colliding]))
        assert restorer.failed == "a tool call's input could not be restored"
        assert parse(out)[-1] == {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": "a tool call's input could not be restored",
            },
        }
        assert EMAIL not in out
        assert ledger.masked_tool_input("toolu_1", {"to": {EMAIL: 2}}) is None

    def test_thinking_passes_through_byte_for_byte(self):
        out, _ = run(make_shield(), MemoryLedger(), reply())
        assert THINKING in out

    def test_other_events_pass_through(self):
        out, _ = run(make_shield(), MemoryLedger(), reply())
        assert out.startswith(
            sse({"type": "message_start", "message": {"id": "msg_1", "content": []}})
        )
        assert ": keep-alive comment\n\n" in out
        assert out.endswith(sse({"type": "message_stop"}))
        kinds = [e["type"] for e in parse(out) if e["type"] != "content_block_delta"]
        assert kinds == [
            "message_start",
            "content_block_start",
            "content_block_stop",
            "content_block_start",
            "content_block_stop",
            "content_block_start",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]

    def test_any_chunking_gives_the_same_reply(self):
        shield = make_shield()
        expected = blocks(parse(run(shield, MemoryLedger(), reply())[0]))
        for seed in range(200):
            out, restorer = run(shield, MemoryLedger(), reply(), sizes=seed)
            assert restorer.failed is None
            got = blocks(parse(out))
            assert got[0] == expected[0]
            assert got[2] == expected[2]
            text = "".join(d["text"] for d in got[1]["deltas"])
            assert text == "".join(d["text"] for d in expected[1]["deltas"])

    def test_crlf_line_endings(self):
        out, _ = run(make_shield(), MemoryLedger(), reply().replace("\n", "\r\n"))
        text = "".join(d["text"] for d in blocks(parse(out))[1]["deltas"])
        assert text == f"Writing to {EMAIL} for {NAME} now."

    def test_text_in_the_start_event_is_sent_as_a_delta(self):
        stream = start(0, {"type": "text", "text": "Hi [PERSON_1]"}) + stop(0)
        out, _ = run(make_shield(), MemoryLedger(), stream)
        found = blocks(parse(out))[0]
        assert found["start"] == {"type": "text", "text": ""}
        assert found["deltas"] == [{"type": "text_delta", "text": f"Hi {NAME}"}]

    def test_a_citation_stays_behind_the_text_before_it(self):
        citation = {"type": "citations_delta", "citation": {"cited_text": "x"}}
        stream = (
            start(0, {"type": "text", "text": ""})
            + delta(0, {"type": "text_delta", "text": "See [EMA"})
            + delta(0, citation)
            + delta(0, {"type": "text_delta", "text": "IL_1] ok"})
            + stop(0)
        )
        out, _ = run(make_shield(), MemoryLedger(), stream)
        deltas = blocks(parse(out))[0]["deltas"]
        assert deltas == [
            {"type": "text_delta", "text": "See "},
            {"type": "text_delta", "text": f"{EMAIL} ok"},
            citation,
        ]

    def test_an_unfinished_event_is_passed_on_at_the_end(self):
        restorer = ResponseRestorer(make_shield(), MemoryLedger())
        assert restorer.feed("event: ping\ndata: {") == ""
        assert restorer.finish() == "event: ping\ndata: {"

    def test_unknown_blocks_pass_through(self):
        stream = (
            start(0, {"type": "server_tool_use", "id": "s", "name": "web_search"})
            + delta(
                0, {"type": "input_json_delta", "partial_json": '{"q": "[EMAIL_1]"}'}
            )
            + stop(0)
        )
        out, restorer = run(make_shield(), MemoryLedger(), stream)
        assert out == stream
        assert restorer.failed is None


class TestFailures:
    def test_a_tool_call_cut_off_passes_through_as_written(self):
        # max_tokens ended the call mid-input: the client can't run it, and
        # handles the stop reason that follows.
        stream = (
            tool_block(0, "toolu_1", ['{"content": "[EMAIL_1]', " and more"])
            + sse({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}})
            + sse({"type": "message_stop"})
        )
        out, restorer = run(make_shield(), MemoryLedger(), stream)
        assert restorer.failed is None
        events = parse(out)
        deltas = [e["delta"] for e in events if e["type"] == "content_block_delta"]
        assert deltas == [
            {
                "type": "input_json_delta",
                "partial_json": '{"content": "[EMAIL_1] and more',
            }
        ]
        assert [e["type"] for e in events][-2:] == ["message_delta", "message_stop"]
        assert EMAIL not in out

    def test_a_crlf_split_across_reads_still_ends_an_event(self):
        stream = reply().replace("\n", "\r\n")
        for cut in range(1, len(stream)):
            if stream[cut - 1] != "\r":
                continue
            restorer = ResponseRestorer(make_shield(), MemoryLedger())
            out = (
                restorer.feed(stream[:cut])
                + restorer.feed(stream[cut:])
                + restorer.finish()
            )
            tool = blocks(parse(out))[2]
            assert json.loads(tool["deltas"][0]["partial_json"])["content"].startswith(
                f"To {EMAIL}"
            )

    def test_keepalive_is_an_event_while_a_tool_call_is_held(self):
        restorer = ResponseRestorer(make_shield(), MemoryLedger())
        assert restorer.keepalive() == ": keep-alive\n\n"
        restorer.feed(
            start(3, {"type": "tool_use", "id": "t", "name": "Bash", "input": {}})
        )
        assert parse(restorer.keepalive()) == [
            {
                "type": "content_block_delta",
                "index": 3,
                "delta": {"type": "input_json_delta", "partial_json": ""},
            }
        ]

    def test_an_unexpected_part_of_a_tool_call_ends_the_stream(self):
        stream = (
            start(0, {"type": "tool_use", "id": "t", "name": "Bash", "input": {}})
            + delta(0, {"type": "text_delta", "text": "x"})
            + stop(0)
        )
        _, restorer = run(make_shield(), MemoryLedger(), stream)
        assert restorer.failed == "a tool call had an unexpected part"

    def test_a_tool_call_is_announced_before_its_input_arrives(self):
        restorer = ResponseRestorer(make_shield(), MemoryLedger())
        begin = start(0, {"type": "tool_use", "id": "t", "name": "Bash", "input": {}})
        assert restorer.feed(begin) == begin
        part = delta(
            0, {"type": "input_json_delta", "partial_json": '{"command": "echo'}
        )
        assert restorer.feed(part) == ""

    def test_an_empty_tool_input_is_the_start_events_input(self):
        stream = start(
            0,
            {
                "type": "tool_use",
                "id": "t",
                "name": "Bash",
                "input": {"a": "[PERSON_1]"},
            },
        ) + stop(0)
        out, _ = run(make_shield(), MemoryLedger(), stream)
        found = blocks(parse(out))[0]
        # The start event never carries the unrestored input.
        assert found["start"]["input"] == {}
        assert json.loads(found["deltas"][0]["partial_json"]) == {"a": NAME}


class TestLedger:
    def test_the_reply_is_recorded_as_written(self):
        shield, ledger = make_shield(), MemoryLedger()
        run(shield, ledger, reply())
        text = f"Writing to {EMAIL} for {NAME} now."
        assert ledger.masked_text(text) == "Writing to [EMAIL_1] for [person 1] now."
        restored = {
            "file_path": "/work/out.txt",
            "content": f"To {EMAIL} from {ODD}, [person 1]",
        }
        assert ledger.masked_tool_input("toolu_1", restored) == json.loads(TOOL_INPUT)

    def test_the_next_request_sends_the_models_own_words(self):
        shield, ledger = make_shield(), MemoryLedger()
        glued = text_block(0, ["Rozmawiałem z [PERSON_1]", "em wczoraj."])
        out, _ = run(shield, ledger, glued)
        shown = "".join(d["text"] for d in blocks(parse(out))[0]["deltas"])
        assert shown == f"Rozmawiałem z {NAME}em wczoraj."
        # Masked again, "Jan Nowakem" would go out as it is; the ledger has
        # what the model wrote.
        assert NAME in shield.mask(shown).text
        body = {
            "messages": [
                {"role": "user", "content": f"Did you speak with {NAME}?"},
                {"role": "assistant", "content": [{"type": "text", "text": shown}]},
            ]
        }
        masked = RequestMasker(shield, ledger, note=None).mask(body)
        assert masked["messages"][1]["content"][0]["text"] == (
            "Rozmawiałem z [PERSON_1]em wczoraj."
        )
        assert NAME not in json.dumps(masked)


class TestWholeMessages:
    def test_restore_message(self):
        shield, ledger = make_shield(), MemoryLedger()
        message = {
            "id": "msg_1",
            "content": [
                {"type": "thinking", "thinking": "[EMAIL_1]", "signature": "s"},
                {"type": "text", "text": "Hi [person 1]"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "echo [PERSON_1] [person 1]"},
                },
            ],
        }
        restored = restore_message(shield, ledger, message)
        assert restored["content"][0] == message["content"][0]
        assert restored["content"][1]["text"] == f"Hi {NAME}"
        assert restored["content"][2]["input"] == {"command": f"echo {NAME} [person 1]"}
        assert ledger.masked_text(f"Hi {NAME}") == "Hi [person 1]"
        assert ledger.masked_tool_input("t1", restored["content"][2]["input"]) == {
            "command": "echo [PERSON_1] [person 1]"
        }

    def test_restore_message_restores_tool_input_keys(self):
        shield, ledger = make_shield(), MemoryLedger()
        masked = {"[PERSON_1]": {"type": "[EMAIL_1]"}}
        message = {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Log", "input": masked}
            ]
        }
        restored = restore_message(shield, ledger, message)["content"][0]["input"]
        assert restored == {NAME: {"type": EMAIL}}
        assert ledger.masked_tool_input("t1", restored) == masked

    def test_restore_message_refuses_keys_that_restore_alike(self):
        shield, ledger = make_shield(), MemoryLedger()
        message = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Log",
                    "input": {"[PERSON_1]": 1, NAME: 2},
                }
            ]
        }
        with pytest.raises(StreamError):
            restore_message(shield, ledger, message)
        assert ledger.masked_tool_input("t1", {NAME: 2}) is None

    @pytest.mark.parametrize("message", [None, "text", {"content": "plain"}, {"id": 1}])
    def test_other_bodies_pass_unchanged(self, message):
        assert restore_message(make_shield(), MemoryLedger(), message) == message
