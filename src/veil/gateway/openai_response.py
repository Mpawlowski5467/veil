"""Restore Responses API text and complete local tool calls, including SSE.

Function arguments are parsed before restoring string values and then encoded
again, so a restored quote cannot corrupt JSON. Free-form custom tool inputs
use exact placeholders. The ledger preserves the model's version for replay.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..restorer import StreamRestorer
from ..shield import Shield
from .ledger import Ledger
from .openai_request import opaque_key
from .response import ResponseRestorer, StreamError, _event, _parse


class ResponsesRestorer:
    """Restore complete Responses output items and remember their masked form."""

    def __init__(self, shield: Shield, ledger: Ledger) -> None:
        """Use the same shield and ledger as the request masker."""
        self.shield = shield
        self.ledger = ledger

    def text(self, text: str) -> str:
        """Restore visible prose and record it for history replay."""
        restored = self.shield.restore(text).text
        self.ledger.record_text(restored, text)
        return restored

    def tool(self, call_id: str, source: str, *, function: bool) -> str:
        """Restore a complete tool input; reject invalid function JSON."""
        if function:
            try:
                masked = json.loads(source)
            except ValueError:
                raise StreamError(
                    "a function call's arguments are not valid JSON"
                ) from None
            restored = self._value(masked)
            self.ledger.record_tool_input(call_id, restored, masked)
            return json.dumps(restored, ensure_ascii=False)
        restored = self.shield.restore(source, tolerant=False).text
        self.ledger.record_tool_input(call_id, restored, source)
        return restored

    def _value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.shield.restore(value, tolerant=False).text
        if isinstance(value, list):
            return [self._value(item) for item in value]
        if isinstance(value, dict):
            out = {
                self.shield.restore(key, tolerant=False).text: self._value(item)
                for key, item in value.items()
            }
            if len(out) != len(value):
                raise StreamError("restored function argument keys collide")
            return out
        return value

    def content(self, value: Any) -> Any:
        """Restore visible text blocks, leaving annotations and opaque data intact."""
        if not isinstance(value, list):
            return value
        out = []
        for part in value:
            if isinstance(part, dict) and part.get("type") in {
                "output_text",
                "summary_text",
                "reasoning_text",
                "refusal",
            }:
                key = "refusal" if part["type"] == "refusal" else "text"
                if isinstance(part.get(key), str):
                    part = {**part, key: self.text(part[key])}
            out.append(part)
        return out

    def item(self, item: Any) -> Any:
        """Restore a message, reasoning summary, or complete local tool call."""
        if not isinstance(item, dict):
            return item
        out = dict(item)
        kind = item.get("type")
        if kind == "message":
            out["content"] = self.content(item.get("content"))
        elif kind == "reasoning":
            for field_name in ("summary", "content"):
                if field_name in item:
                    out[field_name] = self.content(item[field_name])
            if isinstance(item.get("id"), str) and isinstance(
                item.get("encrypted_content"), str
            ):
                encrypted = item["encrypted_content"]
                self.ledger.record_tool_input(
                    opaque_key(item["id"]), encrypted, encrypted
                )
        elif kind in {"function_call", "custom_tool_call"}:
            key = "arguments" if kind == "function_call" else "input"
            if not isinstance(item.get(key), str) or not isinstance(
                item.get("call_id"), str
            ):
                raise StreamError("a tool call's input could not be read")
            out[key] = self.tool(
                item["call_id"], item[key], function=kind == "function_call"
            )
        return out

    def response(self, response: Any) -> Any:
        """Restore the output array in a complete response object."""
        if not isinstance(response, dict) or not isinstance(
            response.get("output"), list
        ):
            raise StreamError("the API reply is not a Responses object")
        return {**response, "output": [self.item(item) for item in response["output"]]}


@dataclass
class _Text:
    stream: StreamRestorer
    template: dict[str, Any]
    masked: list[str] = field(default_factory=list)


@dataclass
class _Tool:
    call_id: str
    function: bool
    parts: list[str] = field(default_factory=list)
    emitted: bool = False


# Each family has delta and done events. The latter holds the full text.
_TEXT_EVENTS = {
    "response.output_text": "text",
    "response.reasoning_summary_text": "text",
    "response.reasoning_text": "text",
    "response.refusal": "refusal",
}
_TOOL_EVENTS = {
    "response.function_call_arguments": ("arguments", True),
    "response.custom_tool_call_input": ("input", False),
}
_TERMINAL = {"response.completed", "response.failed", "response.incomplete"}


class ResponsesStreamRestorer(ResponseRestorer):
    """Restore Responses SSE events, preserving arbitrary network chunk boundaries.

    Tool deltas stay buffered until a complete input is available. Output
    sequence numbers are reassigned because buffering can combine events.
    Incomplete frames and unfinished streams produce an error, never raw tails.
    """

    def __init__(self, shield: Shield, ledger: Ledger) -> None:
        """Start a Responses stream for the current session."""
        super().__init__(shield, ledger)
        self._restore = ResponsesRestorer(shield, ledger)
        self._visible: dict[tuple[str, str, int], _Text] = {}
        self._calls: dict[str, _Tool] = {}
        self._sequence = 0
        self._ended = False

    def feed(self, data: str) -> str:
        """Read SSE frames and return only events that can be restored safely."""
        if self.failed is not None:
            return ""
        data = self._carry + data
        self._carry = "\r" if data.endswith("\r") else ""
        if self._carry:
            data = data[:-1]
        self._buffer += data.replace("\r\n", "\n")
        out = []
        while "\n\n" in self._buffer:
            raw, self._buffer = self._buffer.split("\n\n", 1)
            try:
                out.append(self._handle(raw + "\n\n"))
            except (StreamError, ValueError, TypeError, KeyError):
                out.append(self._error("the Responses stream could not be restored"))
                break
        return "".join(out)

    def finish(self) -> str:
        """Reject truncated frames, pending placeholders, and missing termination."""
        if self.failed is not None:
            return ""
        if (
            self._buffer.strip()
            or self._carry
            or not self._ended
            or self._visible
            or any(not tool.emitted for tool in self._calls.values())
        ):
            return self._error("the Responses stream ended before it was complete")
        return ""

    def keepalive(self) -> str:
        """Emit an SSE comment while complete tool input is buffered."""
        return ": keep-alive\n\n"

    def _emit(self, data: dict[str, Any]) -> str:
        data = {**data, "sequence_number": self._sequence}
        self._sequence += 1
        return _event(data["type"], data)

    def _error(self, message: str) -> str:
        self.failed = message
        return self._emit(
            {
                "type": "error",
                "code": "veil_stream_error",
                "message": message,
                "param": None,
            }
        )

    def _handle(self, raw: str) -> str:
        if raw.strip() == "data: [DONE]":
            if not self._ended:
                raise StreamError("missing response completion")
            return raw
        if all(not line or line.startswith(":") for line in raw.splitlines()):
            return raw
        data = _parse(raw)
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            raise StreamError("invalid Responses event")
        kind = data["type"]
        for family, field_name in _TEXT_EVENTS.items():
            if kind == family + ".delta":
                return self._text_piece(data, family)
            if kind == family + ".done":
                return self._text_done(data, family, field_name)
        for family, (field_name, function) in _TOOL_EVENTS.items():
            if kind in {family + ".delta", family + ".done"}:
                return self._tool_piece(data, family, field_name, function)
        if kind == "response.output_item.added":
            item = data.get("item")
            if isinstance(item, dict) and item.get("type") in {
                "function_call",
                "custom_tool_call",
            }:
                item_id, call_id = item.get("id"), item.get("call_id")
                if not isinstance(item_id, str) or not isinstance(call_id, str):
                    raise StreamError("missing tool identity")
                function = item["type"] == "function_call"
                key = "arguments" if function else "input"
                initial = item.get(key, "")
                if not isinstance(initial, str):
                    raise StreamError("invalid initial tool input")
                self._calls[item_id] = _Tool(
                    call_id, function, [initial] if initial else []
                )
                data = {**data, "item": {**item, key: ""}}
            elif isinstance(item, dict):
                data = {**data, "item": self._restore.item(item)}
        elif kind == "response.output_item.done":
            item = data.get("item")
            if isinstance(item, dict) and item.get("id") in self._calls:
                tool = self._calls[item["id"]]
                if not tool.emitted:
                    # Some producers supply the whole tool at item.done only.
                    tool.emitted = True
            data = {**data, "item": self._restore.item(item)}
        elif kind in {
            "response.content_part.added",
            "response.content_part.done",
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
        }:
            data = {**data, "part": self._restore.content([data.get("part")])[0]}
        elif kind in _TERMINAL:
            if self._visible or any(not tool.emitted for tool in self._calls.values()):
                raise StreamError("response completed with unfinished output")
            self._ended = True
            response = data.get("response")
            if isinstance(response, dict) and "output" in response:
                data = {**data, "response": self._restore.response(response)}
        elif kind == "error":
            self._ended = True
            self._visible.clear()
            self._calls.clear()
        return self._emit(data)

    def _text_key(self, data: dict[str, Any], family: str) -> tuple[str, str, int]:
        item_id = data.get("item_id")
        index = data.get("content_index", data.get("summary_index", 0))
        if not isinstance(item_id, str) or not isinstance(index, int):
            raise StreamError("missing text identity")
        return family, item_id, index

    def _text_piece(self, data: dict[str, Any], family: str) -> str:
        key = self._text_key(data, family)
        text = self._visible.get(key)
        if text is None:
            text = _Text(self._shield.stream_restorer(), dict(data))
            self._visible[key] = text
        piece = data.get("delta")
        if not isinstance(piece, str):
            raise StreamError("invalid text delta")
        text.masked.append(piece)
        restored = text.stream.feed(piece)
        return self._emit({**data, "delta": restored}) if restored else ""

    def _text_done(self, data: dict[str, Any], family: str, field_name: str) -> str:
        key = self._text_key(data, family)
        text = self._visible.pop(key, None)
        source = data.get(field_name)
        if not isinstance(source, str):
            raise StreamError("invalid completed text")
        out = ""
        if text is not None:
            if "".join(text.masked) != source:
                raise StreamError("completed text differs from its deltas")
            tail = text.stream.finish()
            if tail:
                out += self._emit({**text.template, "delta": tail})
        return out + self._emit({**data, field_name: self._restore.text(source)})

    def _tool_piece(
        self, data: dict[str, Any], family: str, field_name: str, function: bool
    ) -> str:
        item_id = data.get("item_id")
        tool = self._calls.get(item_id) if isinstance(item_id, str) else None
        if tool is None or tool.function != function or tool.emitted:
            raise StreamError("tool delta has no matching call")
        if data["type"].endswith(".delta"):
            piece = data.get("delta")
            if not isinstance(piece, str):
                raise StreamError("invalid tool delta")
            tool.parts.append(piece)
            return ""
        source = data.get(field_name)
        if not isinstance(source, str) or (
            tool.parts and source != "".join(tool.parts)
        ):
            raise StreamError("completed tool differs from its deltas")
        restored = self._restore.tool(tool.call_id, source, function=function)
        tool.emitted = True
        delta = {key: value for key, value in data.items() if key != field_name}
        delta.update(type=family + ".delta", delta=restored)
        return self._emit(delta) + self._emit({**data, field_name: restored})
