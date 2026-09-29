"""Restoring streamed replies: text as it arrives, tool calls in one piece."""

import json
import random
import sqlite3

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


class BrokenLedger(MemoryLedger):
    """A ledger whose file is locked: nothing can be recorded."""

    def record_text(self, restored, masked):
        raise sqlite3.OperationalError("database is locked")

    def record_tool_input(self, tool_id, restored, masked):
        raise sqlite3.OperationalError("database is locked")


def sse(data, name=None):
    return f"event: {name or data['type']}\ndata: {json.dumps(data)}\n\n"


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
        from veil.gateway.config import APP

        shield, ledger = make_shield(), MemoryLedger()
        colliding = json.dumps({"to": {"[EMAIL_1]": 1, EMAIL: 2}})
        out, restorer = run(shield, ledger, tool_block(0, "toolu_1", [colliding]))
        why = "two keys of a tool call's input stand for the same text"
        assert restorer.failed == why
        # A failure of the gateway's own: it says why, and is final.
        assert parse(out)[-1] == {
            "type": "error",
            "error": {
                "type": "policy_blocked",
                "message": f"{APP}: {why}; the reply is cut off here",
                "details": {"error_code": "dlp_request_denied"},
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

    def test_an_unknown_part_of_a_tool_call_follows_its_input(self):
        # A kind of part without a rule is held, then sent after the input.
        ledger = MemoryLedger()
        stream = (
            start(0, {"type": "tool_use", "id": "t", "name": "Bash", "input": {}})
            + delta(0, {"type": "input_json_delta", "partial_json": '{"c": "[PER'})
            + delta(0, {"type": "tool_note_delta", "note": "checked"})
            + delta(0, {"type": "text_delta", "text": "x"})
            + delta(0, {"type": "input_json_delta", "partial_json": 'SON_1]"}'})
            + stop(0)
        )
        text, restorer = run(make_shield(), ledger, stream)
        assert restorer.failed is None
        deltas = [e["delta"] for e in parse(text) if e["type"] == "content_block_delta"]
        assert deltas == [
            {"type": "input_json_delta", "partial_json": json.dumps({"c": NAME})},
            {"type": "tool_note_delta", "note": "checked"},
            {"type": "text_delta", "text": "x"},
        ]
        assert ledger.masked_tool_input("t", {"c": NAME}) == {"c": "[PERSON_1]"}

    def test_an_unknown_part_holding_a_placeholder_ends_the_stream(self):
        # A newer client might act on that part as the call's input: a
        # placeholder there would be used as is, so it isn't passed on.
        from veil.gateway.config import APP

        stream = (
            start(0, {"type": "tool_use", "id": "t", "name": "Bash", "input": {}})
            + delta(0, {"type": "input_text_delta", "text": "echo [PERSON_1]"})
            + stop(0)
        )
        text, restorer = run(make_shield(), MemoryLedger(), stream)
        assert restorer.failed is not None
        error = parse(text)[-1]
        assert error["type"] == "error"
        assert error["error"]["type"] == "policy_blocked"
        assert error["error"]["details"] == {"error_code": "dlp_request_denied"}
        assert error["error"]["message"].startswith(f"{APP}: a tool call came with")
        assert NAME not in text

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


class TestToolInputKeys:
    """Keys of a tool call's input are restored, as the gateway masks them."""

    def test_a_masked_key_is_restored(self):
        shield = make_shield()
        placeholder = shield.mask(EMAIL).text
        tool = {"type": "tool_use", "id": "t1", "name": "save", "input": {}}
        masked = {placeholder: "x"}
        message = {"content": [{**tool, "input": masked}]}
        restored = restore_message(shield, MemoryLedger(), message)
        assert restored["content"][0]["input"] == {EMAIL: "x"}

    def test_two_keys_that_restore_alike_are_refused(self):
        shield = make_shield()
        placeholder = shield.mask(EMAIL).text
        message = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "save",
                    "input": {placeholder: 1, EMAIL: 2},
                }
            ]
        }
        with pytest.raises(StreamError, match="two keys"):
            restore_message(shield, MemoryLedger(), message)


class TestKeysOfTheirOwn:
    """Keys an event or a delta carries without a rule are kept."""

    def test_a_text_delta_keeps_its_keys(self):
        event = {
            "type": "content_block_delta",
            "index": 0,
            "seq": 7,
            "delta": {"type": "text_delta", "text": "Hi [PERSON_1]", "lang": "pl"},
        }
        stream = start(0, {"type": "text", "text": ""}) + sse(event) + stop(0)
        text, _ = run(make_shield(), MemoryLedger(), stream)
        deltas = [e for e in parse(text) if e["type"] == "content_block_delta"]
        assert deltas == [{**event, "delta": {**event["delta"], "text": f"Hi {NAME}"}}]

    def test_held_pieces_keep_their_keys_in_order(self):
        pieces = [("[EMA", {"k": 1}), ("IL_1]", {"k": 2}), (" ok", {})]
        stream = start(0, {"type": "text", "text": ""})
        for piece, keys in pieces:
            stream += delta(0, {"type": "text_delta", "text": piece, **keys})
        stream += stop(0)
        for seed in range(30):
            text, _ = run(make_shield(), MemoryLedger(), stream, sizes=seed)
            deltas = [
                e["delta"] for e in parse(text) if e["type"] == "content_block_delta"
            ]
            assert [d["k"] for d in deltas if "k" in d] == [1, 2]
            assert "".join(d["text"] for d in deltas) == f"{EMAIL} ok"

    def test_start_events_keep_their_keys(self):
        text_start = sse(
            {
                "type": "content_block_start",
                "index": 0,
                "x": 1,
                "content_block": {"type": "text", "text": "Hi [PERSON_1]"},
            }
        )
        tool_start = sse(
            {
                "type": "content_block_start",
                "index": 1,
                "y": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "t",
                    "name": "Bash",
                    "input": {"c": "[PERSON_1]"},
                    "caller": {"type": "direct"},
                },
            }
        )
        text, _ = run(
            make_shield(), MemoryLedger(), text_start + stop(0) + tool_start + stop(1)
        )
        starts = [e for e in parse(text) if e["type"] == "content_block_start"]
        assert starts[0]["x"] == 1
        assert starts[0]["content_block"]["text"] == ""
        assert starts[1]["y"] == 2
        assert starts[1]["content_block"]["caller"] == {"type": "direct"}

    def test_a_tool_calls_input_keeps_its_parts_keys(self):
        stream = (
            start(0, {"type": "tool_use", "id": "t", "name": "Bash", "input": {}})
            + delta(0, {"type": "input_json_delta", "partial_json": '{"c": ', "a": 1})
            + delta(0, {"type": "input_json_delta", "partial_json": '"x"'})
            + delta(0, {"type": "input_json_delta", "partial_json": "}", "b": 2})
            + stop(0)
        )
        text, _ = run(make_shield(), MemoryLedger(), stream)
        deltas = [e["delta"] for e in parse(text) if e["type"] == "content_block_delta"]
        assert deltas == [
            {"type": "input_json_delta", "partial_json": '{"c": "x"}', "a": 1},
            {"type": "input_json_delta", "partial_json": "", "b": 2},
        ]

    def test_an_events_name_is_kept(self):
        raw = (
            "event: content_block_delta_v2\ndata: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "x", "k": 1},
                }
            )
            + "\n\n"
        )
        stream = start(0, {"type": "text", "text": ""}) + raw + stop(0)
        text, _ = run(make_shield(), MemoryLedger(), stream)
        assert "event: content_block_delta_v2\n" in text


class TestEventNames:
    """An event rebuilt keeps the name it came under (clients act on names)."""

    NAME = "content_block_delta_v2"

    def names(self, stream, json_text=False):
        restorer = ResponseRestorer(make_shield(), MemoryLedger(), json_text=json_text)
        out = restorer.feed(stream) + restorer.finish()
        return [line[7:] for line in out.split("\n") if line.startswith("event: ")]

    @pytest.mark.parametrize("json_text", [False, True])
    def test_text(self, json_text):
        body = {"type": "text_delta", "text": '"Hi [PERSON_1]"'}
        data = {"type": "content_block_delta", "index": 0, "delta": body}
        stream = start(0, {"type": "text", "text": ""}) + sse(data, self.NAME) + stop(0)
        names = self.names(stream, json_text)
        assert names == ["content_block_start", self.NAME, "content_block_stop"]

    def test_a_tool_calls_input(self):
        body = {"type": "input_json_delta", "partial_json": '{"a": "[PERSON_1]"}'}
        data = {"type": "content_block_delta", "index": 0, "delta": body}
        call = {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}
        names = self.names(start(0, call) + sse(data, self.NAME) + stop(0))
        assert names == ["content_block_start", self.NAME, "content_block_stop"]


class TestJsonReplies:
    """A reply whose text is JSON (a title, say) stays JSON when restored."""

    def title(self):
        return json.dumps({"title": "Fix the build for [PERSON_2]"})

    def test_a_streamed_reply(self):
        masked = self.title()
        stream = text_block(0, [masked[:10], masked[10:25], masked[25:]])
        restorer = ResponseRestorer(make_shield(), MemoryLedger(), json_text=True)
        text = restorer.feed(stream) + restorer.finish()
        restored = "".join(
            e["delta"]["text"]
            for e in parse(text)
            if e["type"] == "content_block_delta"
        )
        # The name has a quote and a backslash in it: escaped, it stays JSON.
        assert json.loads(restored) == {"title": f"Fix the build for {ODD}"}

    def test_a_whole_reply(self):
        message = {"content": [{"type": "text", "text": self.title()}]}
        out = restore_message(make_shield(), MemoryLedger(), message, json_text=True)
        assert json.loads(out["content"][0]["text"]) == {
            "title": f"Fix the build for {ODD}"
        }

    def restored(self, masked, pieces=None):
        """The text of a JSON reply as the client gets it, streamed and whole."""
        pieces = pieces or [masked[:7], masked[7:]]
        restorer = ResponseRestorer(make_shield(), MemoryLedger(), json_text=True)
        streamed = restorer.feed(text_block(0, pieces)) + restorer.finish()
        assert restorer.failed is None
        message = {"content": [{"type": "text", "text": masked}]}
        whole = restore_message(make_shield(), MemoryLedger(), message, json_text=True)
        text = "".join(
            e["delta"]["text"]
            for e in parse(streamed)
            if e["type"] == "content_block_delta"
        )
        assert whole["content"][0]["text"] == text
        return text

    @pytest.mark.parametrize(
        "masked",
        [
            '{"title":"Fix the build"}',
            '{"title": "x", "n": 1e400, "score": 1.50, "u": "caf\\u00e9"}',
            '{"a": ' + "[" * 3000 + "]" * 3000 + "}",
        ],
        ids=["title", "numbers", "deep"],
    )
    def test_nothing_to_restore_keeps_every_byte(self, masked):
        assert self.restored(masked) == masked

    def test_only_a_string_with_a_placeholder_changes(self):
        masked = '{"title":"Notes for [PERSON_2]","n":1e400,"tags":["a"]}'
        assert self.restored(masked) == (
            '{"title":"Notes for Ada \\"Q\\" \\\\ Quill","n":1e400,"tags":["a"]}'
        )

    def test_deltas_with_keys_of_their_own_keep_them(self):
        stream = (
            start(0, {"type": "text", "text": ""})
            + delta(0, {"type": "text_delta", "text": '{"title": '})
            + delta(0, {"type": "text_delta", "text": '"Notes for', "k": 1})
            + delta(0, {"type": "text_delta", "text": ' [PERSON_1]"}', "k": 2})
            + stop(0)
        )
        restorer = ResponseRestorer(make_shield(), MemoryLedger(), json_text=True)
        deltas = [
            e["delta"]
            for e in parse(restorer.feed(stream))
            if e["type"] == "content_block_delta"
        ]
        # One whole text, on the first delta with keys; then the others' keys.
        assert deltas == [
            {"type": "text_delta", "text": f'{{"title": "Notes for {NAME}"}}', "k": 1},
            {"type": "text_delta", "text": "", "k": 2},
        ]
        empty = (
            start(0, {"type": "text", "text": ""})
            + delta(0, {"type": "text_delta", "text": "", "k": 3})
            + stop(0)
        )
        restorer = ResponseRestorer(make_shield(), MemoryLedger(), json_text=True)
        assert [e.get("delta") for e in parse(restorer.feed(empty))][1] == {
            "type": "text_delta",
            "text": "",
            "k": 3,
        }


class TestFailingSafely:
    def test_a_ledger_that_cant_write_doesnt_end_the_reply(self):
        text, restorer = run(make_shield(), BrokenLedger(), reply())
        assert restorer.failed is None
        assert parse(text)[-1]["type"] == "message_stop"

    def test_a_json_reply_sent_back_without_its_record_is_masked(self):
        # Restored values are escaped inside the JSON; with no record of the
        # reply, masking it afresh must still find them.
        shield = make_shield()
        address = "Quill Lane 5\n\tExample Town"
        shield.add_entity(address, "ADDRESS")
        shield.mask(address)  # [ADDRESS_1]
        ledger = BrokenLedger()
        masked = json.dumps({"title": "Notes for [PERSON_2]", "at": "[ADDRESS_1]"})
        reply = {"content": [{"type": "text", "text": masked}]}
        out = restore_message(shield, ledger, reply, json_text=True)
        text = out["content"][0]["text"]
        assert json.loads(text) == {"title": f"Notes for {ODD}", "at": address}
        masker = RequestMasker(shield, ledger, note=None)
        body = {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": text},
            ]
        }
        sent = masker.mask(body)["messages"][1]["content"]
        assert "Quill" not in sent
        assert "Example Town" not in sent
        assert json.loads(sent) == json.loads(masked)

    def test_input_nested_too_deeply_ends_the_stream_by_name(self):
        deep = "[" * 5000 + "]" * 5000
        stream = (
            start(0, {"type": "tool_use", "id": "t", "name": "Bash", "input": {}})
            + delta(0, {"type": "input_json_delta", "partial_json": deep})
            + stop(0)
        )
        text, restorer = run(make_shield(), MemoryLedger(), stream)
        assert restorer.failed is not None
        assert parse(text)[-1]["error"]["details"] == {
            "error_code": "dlp_request_denied"
        }


@pytest.mark.parametrize(
    "kind",
    ["text", "tool_use", "server_tool_use", "compaction", "thinking", "future_block"],
)
def test_verification_requires_every_content_block_to_finish(kind):
    restorer = ResponseRestorer(make_shield(), MemoryLedger())
    restorer.feed(start(0, {"type": kind}))
    restorer.feed(sse({"type": "message_stop"}))
    restorer.finish()
    assert not restorer.completed


def test_observing_verification_preserves_provider_replay():
    from veil.gateway.activity import Observation, ObservedLedger

    ledger = MemoryLedger()
    observation = Observation(session="session", counts={}, generation="generation")
    observed = ObservedLedger(ledger, observation)
    block = {"type": "future_result", "content": "provider result"}
    restore_message(make_shield(), observed, {"content": [block]})
    assert ledger.was_seen(block)
    assert observed.was_seen(block)
    assert not observed.was_seen({**block, "content": "changed"})
