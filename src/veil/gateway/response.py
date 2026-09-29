"""Restoring a Messages API reply, streamed or not, before the client sees it.

The model only ever saw masked text, so its reply holds placeholders. Text is
restored as it streams, holding back only what could still become a
placeholder; a tool call is held until it is complete, then its input is
restored (exact placeholders only, since the input will be acted on) and sent
on in one piece, which is how the client runs it anyway. Everything else,
thinking included, passes through unchanged. Each restored reply is recorded
in the ledger, so it can go back to the model exactly as written.
"""

from __future__ import annotations

import json
from typing import Any

from ..restorer import StreamRestorer
from ..shield import Shield
from .ledger import Ledger

_TEXT = "text"
_TOOL_USE = "tool_use"


def _event(name: str, data: dict[str, Any]) -> str:
    try:
        payload = json.dumps(data, ensure_ascii=False)
        payload.encode("utf-8")
    except UnicodeEncodeError:
        payload = json.dumps(data)
    return f"event: {name}\ndata: {payload}\n\n"


class _TextBlock:
    def __init__(self, stream: StreamRestorer) -> None:
        self.stream = stream
        self.masked: list[str] = []
        self.restored: list[str] = []
        self.received = 0  # characters of masked text fed so far
        self.queued: list[tuple[int, str]] = []  # (text offset, raw event)


class _ToolBlock:
    def __init__(self, block: dict[str, Any]) -> None:
        self.block = block
        self.parts: list[str] = []


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
    """

    def __init__(self, shield: Shield, ledger: Ledger) -> None:
        """Start restoring a reply."""
        self._shield = shield
        self._ledger = ledger
        self._buffer = ""
        self._carry = ""
        self._text: dict[int, _TextBlock] = {}
        self._tools: dict[int, _ToolBlock] = {}
        self.failed: str | None = None
        self.completed = False
        self.terminal_error = False

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
                out.append(
                    _event(
                        "error",
                        {
                            "type": "error",
                            "error": {"type": "api_error", "message": str(error)},
                        },
                    )
                )
                break
        return "".join(out)

    def finish(self) -> str:
        """End of the reply: pass on anything left, unchanged."""
        rest, self._buffer = self._buffer + self._carry, ""
        if rest.strip() or self._text or self._tools:
            self.completed = False
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
        data = _parse(raw)
        if not isinstance(data, dict):
            return raw
        kind = data.get("type")
        if self.completed and kind != "ping":
            self.terminal_error = True
        if kind == "message_stop":
            self.completed = not self._text and not self._tools
        elif kind == "error":
            self.terminal_error = True
        index = data.get("index")
        if kind == "content_block_start" and isinstance(index, int):
            return self._start(raw, index, data.get("content_block"))
        if kind == "content_block_delta" and isinstance(index, int):
            return self._delta(raw, index, data.get("delta"))
        if kind == "content_block_stop" and isinstance(index, int):
            return self._stop(raw, index)
        return raw

    def _start(self, raw: str, index: int, block: Any) -> str:
        if not isinstance(block, dict):
            return raw
        if block.get("type") == _TEXT:
            text = _TextBlock(self._shield.stream_restorer())
            self._text[index] = text
            initial = block.get("text")
            if isinstance(initial, str) and initial:
                # Rare: text in the start event. Send it on as a delta.
                start = _event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {**block, "text": ""},
                    },
                )
                return start + self._text_delta(index, text, initial)
            return raw
        if block.get("type") == _TOOL_USE:
            # Announce the call now; its input follows, restored, at the end.
            self._tools[index] = _ToolBlock(block)
            if block.get("input"):
                return _event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {**block, "input": {}},
                    },
                )
            return raw
        return raw

    def _delta(self, raw: str, index: int, delta: Any) -> str:
        if not isinstance(delta, dict):
            return raw
        text = self._text.get(index)
        if text is not None:
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return self._text_delta(index, text, delta["text"])
            # Another kind of delta (a citation): keep it behind held text.
            if text.stream.held:
                text.queued.append((text.received, raw))
                return ""
            return raw
        tool = self._tools.get(index)
        if tool is not None:
            if delta.get("type") == "input_json_delta":
                part = delta.get("partial_json")
                if not isinstance(part, str):
                    raise StreamError("a tool call's input could not be read")
                tool.parts.append(part)
                return ""
            raise StreamError("a tool call had an unexpected part")
        return raw

    def _text_delta(self, index: int, block: _TextBlock, piece: str) -> str:
        block.masked.append(piece)
        block.received += len(piece)
        restored = block.stream.feed(piece)
        return self._emit_text(index, block, restored)

    def _emit_text(self, index: int, block: _TextBlock, restored: str) -> str:
        out = []
        if restored:
            block.restored.append(restored)
            out.append(
                _event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": restored},
                    },
                )
            )
        released = block.received - block.stream.held
        while block.queued and block.queued[0][0] <= released:
            out.append(block.queued.pop(0)[1])
        return "".join(out)

    def _stop(self, raw: str, index: int) -> str:
        text = self._text.pop(index, None)
        if text is not None:
            out = self._emit_text(index, text, text.stream.finish())
            out += "".join(event for _, event in text.queued)
            self._ledger.record_text("".join(text.restored), "".join(text.masked))
            return out + raw
        tool = self._tools.pop(index, None)
        if tool is not None:
            return self._tool_stop(raw, index, tool)
        return raw

    def _tool_stop(self, raw: str, index: int, tool: _ToolBlock) -> str:
        source = "".join(tool.parts)
        if source.strip():
            try:
                masked = json.loads(source)
            except ValueError:
                # Cut off (by max_tokens, say): the client can't run it, so
                # it goes on as the model wrote it, with the stop reason
                # that follows, for the client to handle.
                return self._raw_delta(index, source) + raw
        else:
            masked = tool.block.get("input") or {}
        restored = self._restore_value(masked)
        tool_id = tool.block.get("id")
        if isinstance(tool_id, str):
            self._ledger.record_tool_input(tool_id, restored, masked)
        delta = _event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(restored, ensure_ascii=False),
                },
            },
        )
        return delta + raw

    def _raw_delta(self, index: int, partial_json: str) -> str:
        return _event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": partial_json},
            },
        )

    def _restore_value(self, value: Any) -> Any:
        """Restore exact placeholders in every string of a tool call's input.

        Keys are restored too, since the model saw them masked. Two keys that
        restore to the same text can't both be kept, so such a call fails.
        """
        if isinstance(value, str):
            return self._restore_text(value)
        if isinstance(value, list):
            return [self._restore_value(item) for item in value]
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                restored = self._restore_text(key)
                if restored in out:
                    raise StreamError("a tool call's input could not be restored")
                out[restored] = self._restore_value(item)
            return out
        return value

    def _restore_text(self, text: str) -> str:
        return self._shield.restore(text, tolerant=False).text


def restore_message(shield: Shield, ledger: Ledger, message: Any) -> Any:
    """Restore a whole (not streamed) Messages API reply the same way.

    Raises:
        StreamError: If a tool call's input can't be restored safely.
    """
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return message
    content = []
    restorer = ResponseRestorer(shield, ledger)
    for block in message["content"]:
        if isinstance(block, dict) and block.get("type") == _TEXT:
            masked = block.get("text")
            if isinstance(masked, str):
                restored = shield.restore(masked).text
                ledger.record_text(restored, masked)
                block = {**block, "text": restored}
        elif isinstance(block, dict) and block.get("type") == _TOOL_USE:
            masked_input = block.get("input")
            restored_input = restorer._restore_value(masked_input)
            if isinstance(block.get("id"), str):
                ledger.record_tool_input(block["id"], restored_input, masked_input)
            block = {**block, "input": restored_input}
        content.append(block)
    return {**message, "content": content}


def _parse(raw: str) -> Any:
    """Return an event's data parsed as JSON, or None."""
    data_lines = [
        line[5:].lstrip(" ") for line in raw.split("\n") if line.startswith("data:")
    ]
    if not data_lines:
        return None
    try:
        return json.loads("\n".join(data_lines))
    except ValueError:
        return None
