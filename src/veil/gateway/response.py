"""Restoring a Messages API reply, streamed or not, before the client sees it.

The model only ever saw masked text, so its reply holds placeholders. Text is
restored as it streams, holding back only what could still become a
placeholder; a tool call is held until it is complete, then its input is
restored (exact placeholders only, since the input will be acted on) and sent
on in one piece, which is how the client runs it anyway. Everything else,
thinking included, passes through unchanged, and so do fields and events
without a rule of their own. Each restored reply is recorded in the ledger,
so it can go back to the model exactly as written.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from typing import Any

from ..restorer import StreamRestorer
from ..shield import Shield
from .config import APP
from .ledger import Ledger
from .request import _opaque

_TEXT = "text"
_DELTA = "content_block_delta"
_TOOL_USE = "tool_use"
_COMPACTION = "compaction"
# Calls the API runs itself: it ran them with the masked input, which goes
# to the client as it is (and back to the API exactly so).
_SERVER_CALLS = frozenset({"server_tool_use", "mcp_tool_use"})
# Blocks with a rule of their own here; any other block the API sends is
# recorded as it came (see `Ledger.record_seen`), to go back exactly so.
_OWN_RULES = frozenset(
    {_TEXT, _TOOL_USE, _COMPACTION, "thinking", "redacted_thinking", *_SERVER_CALLS}
)
# Deltas that carry text, not opaque values: not searched for them.
_TEXT_DELTAS = frozenset({"text_delta", "input_json_delta", "thinking_delta"})
# The keys of an event and of a delta the restorer rewrites; any other key
# they carry is kept as it came.
_EVENT_KEYS = frozenset({"type", "index", "delta"})
# A string in JSON text.
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')

#: Tells Claude Code (2.1.283) a failure is final: it doesn't run the model
#: again, without streaming or on another model.
FINAL = {"error_code": "dlp_request_denied"}


def error_event(message: str, *, final: bool = True) -> str:
    """An ``error`` event ending a stream, saying why.

    A failure of the gateway's own is ``final``, and Claude Code shows its
    message; a lost connection to the API is not, and is left to the
    client's own handling.
    """
    text = message if message.startswith(f"{APP}:") else f"{APP}: {message}"
    error: dict[str, Any] = {
        "type": "policy_blocked" if final else "api_error",
        "message": text,
    }
    if final:
        error["details"] = FINAL
    return _event("error", {"type": "error", "error": error})


def _event(name: str, data: dict[str, Any]) -> str:
    try:
        payload = json.dumps(data, ensure_ascii=False)
        payload.encode("utf-8")
    except UnicodeEncodeError:
        payload = json.dumps(data)
    return f"event: {name}\ndata: {payload}\n\n"


def _text_event(name: str, index: int, text: str) -> str:
    """A text delta of our own, under the name the block's deltas came under."""
    return _event(
        name,
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    )


class _Event:
    """One event as it came: its name, its data, and its raw text."""

    def __init__(self, name: str, data: dict[str, Any], raw: str) -> None:
        self.name = name
        self.data = data
        self.raw = raw

    @property
    def delta(self) -> dict[str, Any]:
        delta = self.data.get("delta")
        return delta if isinstance(delta, dict) else {}

    def extra(self, payload: str) -> bool:
        """Whether it carries keys the restorer doesn't rewrite."""
        return bool(set(self.data) - _EVENT_KEYS) or bool(
            set(self.delta) - {"type", payload}
        )

    def with_delta(self, payload: str, value: str) -> str:
        """The event again, its payload replaced, every other key kept."""
        return _event(self.name, {**self.data, "delta": {**self.delta, payload: value}})


class _TextBlock:
    def __init__(self, stream: StreamRestorer) -> None:
        self.stream = stream
        self.masked: list[str] = []
        self.restored: list[str] = []
        self.received = 0  # characters of masked text fed so far
        self.queued: list[tuple[int, str]] = []  # (text offset, raw event)
        self.first: _Event | None = None  # its first text delta, as a template
        self.keyed: list[_Event] = []  # text deltas with keys of their own
        self.name = _DELTA  # the event name its text deltas came under


class _ToolBlock:
    def __init__(self, block: dict[str, Any]) -> None:
        self.block = block
        self.parts: list[str] = []
        self.template: _Event | None = None  # the first input delta with keys
        self.keyed: list[_Event] = []  # later input deltas with keys of their own
        self.held: list[_Event] = []  # deltas of kinds without a rule
        self.name = _DELTA  # the event name its input deltas came under


class _ServerCall:
    def __init__(self, block: dict[str, Any]) -> None:
        self.block = block
        self.parts: list[str] = []


class _Compaction:
    def __init__(self, stream: StreamRestorer, masked: str, restored: str) -> None:
        self.stream = stream
        self.masked = [masked]
        self.restored = [restored]
        self.events: list[_Event] = []  # held until the block ends


class _Other:
    def __init__(self, block: dict[str, Any]) -> None:
        self.block = block
        self.whole = True  # no part came after the start


class StreamError(ValueError):
    """The reply can't be restored safely; the stream is ended with an error."""


class ResponseRestorer:
    """Restores one streamed reply, server-sent event by event.

    Feed it the reply's text as it arrives; it returns the events to send on.
    A tool call's input is held until the call is complete, so a long one can
    leave the client waiting: send an SSE comment line now and then.

    Args:
        shield: The conversation's shield (its vault restores placeholders).
        ledger: Where each restored reply is recorded.
        json_text: The reply's text is JSON (the request asked for a format):
            text is restored once complete, with values escaped as JSON.
    """

    def __init__(
        self, shield: Shield, ledger: Ledger, *, json_text: bool = False
    ) -> None:
        """Start restoring a reply."""
        self._shield = shield
        self._ledger = ledger
        self._json_text = json_text
        self._buffer = ""
        self._carry = ""
        self._text: dict[int, _TextBlock] = {}
        self._tools: dict[int, _ToolBlock] = {}
        self._servers: dict[int, _ServerCall] = {}
        self._compactions: dict[int, _Compaction] = {}
        self._others: dict[int, _Other] = {}
        self.failed: str | None = None

    def feed(self, data: str) -> str:
        """Add the next part of the reply; return the events to send on."""
        if self.failed is not None:
            return ""
        data = self._carry + data
        # A "\r" at the end may be the first half of a "\r\n".
        self._carry = "\r" if data.endswith("\r") else ""
        if self._carry:
            data = data[:-1]
        self._buffer += data.replace("\r\n", "\n")
        out: list[str] = []
        while "\n\n" in self._buffer:
            raw, self._buffer = self._buffer.split("\n\n", 1)
            try:
                out.append(self._handle(raw + "\n\n"))
            except StreamError as error:
                self.failed = str(error)
            except (RecursionError, ValueError, TypeError):
                self.failed = "failed while restoring the reply (a bug)"
            if self.failed is not None:
                out.append(error_event(f"{self.failed}; the reply is cut off here"))
                break
        return "".join(out)

    def finish(self) -> str:
        """End of the reply: pass on anything left, unchanged."""
        rest, self._buffer = self._buffer + self._carry, ""
        self._carry = ""
        return "" if self.failed is not None else rest

    def keepalive(self) -> str:
        """Return something to send while a tool call is held back.

        An empty input delta for the held call is a real event, which the
        client counts as progress; a comment line is not.
        """
        for index in self._tools:
            return _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": ""},
                },
            )
        return ": keep-alive\n\n"

    def _handle(self, raw: str) -> str:
        name, data = _parse(raw)
        if not isinstance(data, dict):
            return raw
        event = _Event(name or str(data.get("type")), data, raw)
        if event.delta.get("type") not in _TEXT_DELTAS:
            self._record_opaque(data)
        kind = data.get("type")
        index = data.get("index")
        if kind == "content_block_start" and isinstance(index, int):
            return self._start(event, index, data.get("content_block"))
        if kind == "content_block_delta" and isinstance(index, int):
            return self._delta(event, index)
        if kind == "content_block_stop" and isinstance(index, int):
            return self._stop(raw, index)
        return raw

    def _start(self, event: _Event, index: int, block: Any) -> str:
        if not isinstance(block, dict):
            return event.raw
        if block.get("type") == _TEXT:
            text = _TextBlock(self._shield.stream_restorer())
            self._text[index] = text
            initial = block.get("text")
            if isinstance(initial, str) and initial:
                # Rare: text in the start event. Send it on as a delta.
                start = _event(
                    event.name, {**event.data, "content_block": {**block, "text": ""}}
                )
                return start + self._text_delta(index, text, initial, None)
            return event.raw
        if block.get("type") == _TOOL_USE:
            # Announce the call now; its input follows, restored, at the end.
            self._tools[index] = _ToolBlock(block)
            if block.get("input"):
                return _event(
                    event.name, {**event.data, "content_block": {**block, "input": {}}}
                )
            return event.raw
        if block.get("type") in _SERVER_CALLS:
            self._servers[index] = _ServerCall(block)
            return event.raw
        if block.get("type") == _COMPACTION:
            return self._compaction_start(event, index, block)
        if block.get("type") not in _OWN_RULES:
            self._others[index] = _Other(block)
        return event.raw

    def _delta(self, event: _Event, index: int) -> str:
        delta = event.delta
        if not delta:
            return event.raw
        text = self._text.get(index)
        if text is not None:
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return self._text_delta(index, text, delta["text"], event)
            # Another kind of delta (a citation): keep it behind held text.
            if text.stream.held or (self._json_text and text.masked):
                text.queued.append((text.received, event.raw))
                return ""
            return event.raw
        tool = self._tools.get(index)
        if tool is not None:
            if delta.get("type") == "input_json_delta":
                part = delta.get("partial_json")
                if not isinstance(part, str):
                    raise StreamError("a tool call's input could not be read")
                if not tool.parts:
                    tool.name = event.name
                tool.parts.append(part)
                if event.extra("partial_json"):
                    if tool.template is None:
                        tool.template = event
                    else:
                        tool.keyed.append(event)
                return ""
            # A kind of part without a rule: kept, and sent after the input.
            tool.held.append(event)
            return ""
        server = self._servers.get(index)
        if server is not None:
            part = delta.get("partial_json")
            if delta.get("type") == "input_json_delta" and isinstance(part, str):
                server.parts.append(part)
            return event.raw
        compaction = self._compactions.get(index)
        if compaction is not None:
            compaction.events.append(event)
            return ""
        other = self._others.get(index)
        if other is not None:
            other.whole = False
        return event.raw

    def _text_delta(
        self, index: int, block: _TextBlock, piece: str, event: _Event | None
    ) -> str:
        block.masked.append(piece)
        block.received += len(piece)
        keyed = event is not None and event.extra("text")
        if event is not None:
            block.name = event.name
            if block.first is None:
                block.first = event
            if keyed:
                block.keyed.append(event)
        if self._json_text:
            return ""  # restored once complete: see _stop
        restored = block.stream.feed(piece)
        return self._emit_text(index, block, restored, event if keyed else None)

    def _emit_text(
        self, index: int, block: _TextBlock, restored: str, event: _Event | None
    ) -> str:
        out = []
        if restored or event is not None:
            block.restored.append(restored)
            if event is not None:
                # Keys of its own stay on it, even with no text ready yet.
                out.append(event.with_delta("text", restored))
            else:
                out.append(_text_event(block.name, index, restored))
        released = block.received - block.stream.held
        while block.queued and block.queued[0][0] <= released:
            out.append(block.queued.pop(0)[1])
        return "".join(out)

    def _stop(self, raw: str, index: int) -> str:
        text = self._text.pop(index, None)
        if text is not None:
            return self._text_stop(raw, index, text)
        tool = self._tools.pop(index, None)
        if tool is not None:
            return self._tool_stop(raw, index, tool)
        server = self._servers.pop(index, None)
        if server is not None:
            self._server_stop(server)
            return raw
        compaction = self._compactions.pop(index, None)
        if compaction is not None:
            return self._compaction_stop(compaction) + raw
        other = self._others.pop(index, None)
        if other is not None and other.whole:
            self._record_seen(other.block)
        return raw

    def _server_stop(self, server: _ServerCall) -> None:
        """Record a call the API ran, input as it ran it, to go back so."""
        source = "".join(server.parts)
        try:
            tool_input = (
                json.loads(source) if source.strip() else server.block.get("input")
            )
        except ValueError:
            return
        tool_id = server.block.get("id")
        if isinstance(tool_id, str) and tool_input is not None:
            self._record(
                self._ledger.record_tool_input, tool_id, tool_input, tool_input
            )

    def _compaction_start(
        self, event: _Event, index: int, block: dict[str, Any]
    ) -> str:
        """A summary of the conversation so far: model text, restored as text.

        Its signed or encrypted parts go on as they came. Deltas are held to
        the end: the client keeps the last delta's encrypted content, so no
        delta may be added.
        """
        content = block.get("content")
        masked = content if isinstance(content, str) else ""
        restored = self._shield.restore(masked).text if masked else ""
        self._compactions[index] = _Compaction(
            self._shield.stream_restorer(), masked, restored
        )
        if restored == masked:
            return event.raw
        return _event(
            event.name, {**event.data, "content_block": {**block, "content": restored}}
        )

    def _compaction_stop(self, compaction: _Compaction) -> str:
        out: list[str] = []
        last = max(
            (
                i
                for i, e in enumerate(compaction.events)
                if isinstance(e.delta.get("content"), str)
            ),
            default=-1,
        )
        for i, event in enumerate(compaction.events):
            piece = event.delta.get("content")
            if not isinstance(piece, str):
                out.append(event.raw)
                continue
            compaction.masked.append(piece)
            restored = compaction.stream.feed(piece)
            if i == last:
                restored += compaction.stream.finish()
            compaction.restored.append(restored)
            out.append(event.with_delta("content", restored))
        masked = "".join(compaction.masked)
        if masked:
            self._record(self._ledger.record_text, "".join(compaction.restored), masked)
        return "".join(out)

    def _record_seen(self, value: Any) -> None:
        record = getattr(self._ledger, "record_seen", None)
        if callable(record):
            self._record(record, value)

    def _record_opaque(self, value: Any, key: str | None = None) -> None:
        """Record every opaque value the API sends (a signature, say).

        A request may then send it back, though it can't be looked into.
        """
        if isinstance(value, str):
            if _opaque(key, value):
                self._record_seen(value)
        elif isinstance(value, list):
            for item in value:
                self._record_opaque(item, key)
        elif isinstance(value, dict):
            for name, item in value.items():
                self._record_opaque(item, name)

    def _text_stop(self, raw: str, index: int, text: _TextBlock) -> str:
        masked = "".join(text.masked)
        if self._json_text:
            # The whole text in one delta, then each keyed delta's keys, as a
            # tool call's input goes.
            restored = self._restore_json_text(masked)
            text.restored = [restored]
            template = text.keyed[0] if text.keyed else text.first
            if template is not None and (restored or text.keyed):
                out = template.with_delta("text", restored)
            else:
                out = _text_event(text.name, index, restored) if restored else ""
            out += "".join(e.with_delta("text", "") for e in text.keyed[1:])
            out += "".join(e for _, e in text.queued)
        else:
            out = self._emit_text(index, text, text.stream.finish(), None)
            out += "".join(event for _, event in text.queued)
        self._record(self._ledger.record_text, "".join(text.restored), masked)
        return out + raw

    def _tool_stop(self, raw: str, index: int, tool: _ToolBlock) -> str:
        self._check_held(tool.held)
        held = "".join(event.raw for event in tool.held)
        source = "".join(tool.parts)
        if source.strip():
            try:
                masked = json.loads(source)
            except ValueError:
                # Cut off (by max_tokens, say): the client can't run it, so
                # it goes on as the model wrote it, with the stop reason
                # that follows, for the client to handle.
                return self._input_delta(index, tool, source) + held + raw
        else:
            masked = tool.block.get("input") or {}
        restored = self._restore_value(masked)
        tool_id = tool.block.get("id")
        if isinstance(tool_id, str):
            self._record(self._ledger.record_tool_input, tool_id, restored, masked)
        dumped = json.dumps(restored, ensure_ascii=False)
        return self._input_delta(index, tool, dumped) + held + raw

    def _input_delta(self, index: int, tool: _ToolBlock, partial_json: str) -> str:
        """The call's whole input in one delta, and its parts' own keys after."""
        if tool.template is not None:
            first = tool.template.with_delta("partial_json", partial_json)
        else:
            first = _event(
                tool.name,
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": partial_json},
                },
            )
        return first + "".join(e.with_delta("partial_json", "") for e in tool.keyed)

    def _check_held(self, held: list[_Event]) -> None:
        """Refuse to pass on a tool call's unknown part that holds a placeholder.

        A client that knows that kind of part may act on it as the call's
        input; a placeholder there would be used as is, silently.
        """
        for event in held:
            if any(
                self._shield.restore(text, tolerant=False).restored_count
                for text in _strings(event.delta)
            ):
                raise StreamError(
                    "a tool call came with a part this gateway doesn't know yet, "
                    "holding personal data, so it was not passed on; update "
                    f"{APP}"
                )

    def _record(self, record: Any, *args: Any) -> None:
        """Record in the ledger; a failure to write must not end the reply.

        Without the record, the reply is masked afresh when sent back.
        """
        with contextlib.suppress(sqlite3.Error, OSError):
            record(*args)

    def _restore_json_text(self, masked: str) -> str:
        """Restore text that is JSON: each string in it, escaped as JSON.

        Only a string that holds a placeholder changes; every other byte
        stays as the model wrote it. Text that isn't JSON is restored as
        text.
        """
        try:
            json.loads(masked)
        except (ValueError, RecursionError):
            return self._shield.restore(masked).text

        def restore(match: re.Match[str]) -> str:
            value = json.loads(match[0])
            restored = self._shield.restore(value).text
            if restored == value:
                return match[0]
            return json.dumps(restored, ensure_ascii=False)

        # In JSON, every quote outside a string starts one.
        return _JSON_STRING.sub(restore, masked)

    def _restore_value(self, value: Any) -> Any:
        """Restore exact placeholders in every string of a tool call's input.

        Keys too: the gateway masks them on the way out, like any string of
        a tool's input.
        """
        if isinstance(value, str):
            return self._shield.restore(value, tolerant=False).text
        if isinstance(value, list):
            return [self._restore_value(item) for item in value]
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                restored = self._shield.restore(key, tolerant=False).text
                if restored in out:
                    raise StreamError(
                        "two keys of a tool call's input stand for the same text"
                    )
                out[restored] = self._restore_value(item)
            return out
        return value


def restore_message(
    shield: Shield, ledger: Ledger, message: Any, *, json_text: bool = False
) -> Any:
    """Restore a whole (not streamed) Messages API reply the same way."""
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return message
    content = []
    restorer = ResponseRestorer(shield, ledger, json_text=json_text)
    for block in message["content"]:
        # What the API sent, before any placeholder in it is restored: a
        # restored value must never count as one the API sent. The model's
        # own writing (text, a call's input, thinking) is left out, as a
        # stream's deltas of it are.
        restorer._record_opaque(_without_writing(block))
        if isinstance(block, dict) and block.get("type") == _TEXT:
            masked = block.get("text")
            if isinstance(masked, str):
                restored = (
                    restorer._restore_json_text(masked)
                    if json_text
                    else shield.restore(masked).text
                )
                restorer._record(ledger.record_text, restored, masked)
                block = {**block, "text": restored}
        elif isinstance(block, dict) and block.get("type") == _TOOL_USE:
            masked_input = block.get("input")
            restored_input = restorer._restore_value(masked_input)
            if isinstance(block.get("id"), str):
                restorer._record(
                    ledger.record_tool_input, block["id"], restored_input, masked_input
                )
            block = {**block, "input": restored_input}
        elif isinstance(block, dict) and block.get("type") in _SERVER_CALLS:
            if isinstance(block.get("id"), str) and block.get("input") is not None:
                restorer._record(
                    ledger.record_tool_input,
                    block["id"],
                    block["input"],
                    block["input"],
                )
        elif isinstance(block, dict) and block.get("type") == _COMPACTION:
            masked = block.get("content")
            if isinstance(masked, str) and masked:
                restored = shield.restore(masked).text
                restorer._record(ledger.record_text, restored, masked)
                block = {**block, "content": restored}
        elif isinstance(block, dict) and block.get("type") not in _OWN_RULES:
            restorer._record_seen(block)
        content.append(block)
    return {**message, "content": content}


def _without_writing(block: Any) -> Any:
    """A block without the part the model writes (see `_TEXT_DELTAS`)."""
    if not isinstance(block, dict):
        return block
    kind = block.get("type")
    written = {
        _TEXT: "text",
        _TOOL_USE: "input",
        "thinking": "thinking",
        **dict.fromkeys(_SERVER_CALLS, "input"),
    }.get(kind if isinstance(kind, str) else "")
    return {k: v for k, v in block.items() if k != written} if written else block


def _strings(value: Any) -> list[str]:
    """Every string in a JSON value, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (key, *_strings(item))]
    return []


def _parse(raw: str) -> tuple[str | None, Any]:
    """Return an event's name and its data parsed as JSON (None if not JSON)."""
    name = None
    data_lines = []
    for line in raw.split("\n"):
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if not data_lines:
        return name, None
    try:
        return name, json.loads("\n".join(data_lines))
    except ValueError:
        return name, None
