"""Text-only OpenAI Chat Completions masking and JSON/SSE restoration."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from ..restorer import StreamRestorer
from .openai_request import ResponsesRequestMasker, _identifier, _object
from .openai_response import ResponsesRestorer
from .request import UnsupportedRequestError, _list, _str
from .response import ResponseRestorer, StreamError

_NUMBERS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "frequency_penalty",
    "presence_penalty",
    "seed",
}
_KEYS = {
    "model",
    "messages",
    "stream",
    "stream_options",
    "stop",
    "n",
    "store",
    "metadata",
    "user",
    "safety_identifier",
    "prompt_cache_key",
    "reasoning_effort",
    "service_tier",
    "response_format",
    *_NUMBERS,
}
_FINISH = {"stop", "length", "content_filter"}
_LIMIT = 8 * 1024 * 1024


class ChatRequestMasker(ResponsesRequestMasker):
    """Accept explicit text chat shapes; refuse media, tools, and unknown fields."""

    def _mask_once(
        self, body: dict[str, Any], *, client_version: str | None = None
    ) -> dict[str, Any]:
        self._notice_a_cleared_vault()
        try:
            _object(body, "$", _KEYS)
            _identifier(body.get("model"), "model")
            messages = _list(body.get("messages"), "messages")
            if not messages:
                raise UnsupportedRequestError("messages", "at least one is required")
            out = {key: self._chat_field(key, value) for key, value in body.items()}
            if self._note:
                out["messages"] = [
                    {"role": "developer", "content": self._note},
                    *out["messages"],
                ]
            out["store"] = False
            self._vault_state = self._state()
            return out
        except UnsupportedRequestError as error:
            found = [self._safe(path, problem) for path, problem in error.found]
            raise UnsupportedRequestError(*found[0], more=found[1:]) from None

    def _chat_field(self, key: str, value: Any) -> Any:
        if key == "messages":
            return [
                self._chat_message(item, f"messages[{i}]")
                for i, item in enumerate(value)
            ]
        if key in _NUMBERS:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise UnsupportedRequestError(key, "not a finite number")
            if (
                key in {"max_tokens", "max_completion_tokens", "seed"}
                and type(value) is not int
            ):
                raise UnsupportedRequestError(key, "not a whole number")
            return value
        if key == "n":
            if type(value) is not int or value != 1:
                raise UnsupportedRequestError(key, "only one reply is supported")
            return value
        if key == "stream_options":
            options = _object(value, key, {"include_usage", "include_obfuscation"})
            if any(type(item) is not bool for item in options.values()):
                raise UnsupportedRequestError(key, "options must be booleans")
            return dict(options)
        if key == "stop":
            if value is None:
                return None
            if isinstance(value, str):
                return self._mask_string(value)
            return [self._mask_string(_str(item, key)) for item in _list(value, key)]
        if key == "response_format":
            config = _object(value, key, {"type"})
            if config.get("type") != "text":
                raise UnsupportedRequestError(key, "only plain text is supported")
            return dict(config)
        if key == "reasoning_effort":
            if not isinstance(value, str) or value not in {
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
            }:
                raise UnsupportedRequestError(key, "unsupported effort")
            return value
        return self._field(key, value)

    def _chat_message(self, item: Any, path: str) -> dict[str, Any]:
        message = _object(item, path, {"role", "content", "name", "refusal"})
        role = message.get("role")
        if not isinstance(role, str) or role not in {
            "system",
            "developer",
            "user",
            "assistant",
        }:
            raise UnsupportedRequestError(
                path + ".role", "only text messages are supported"
            )
        content = message.get("content")

        def mask(text: str) -> str:
            return self._mask_string(text, assistant=role == "assistant")

        if (
            content is None
            and role == "assistant"
            and isinstance(message.get("refusal"), str)
        ):
            masked: Any = None
        elif isinstance(content, str):
            masked = mask(content)
        else:
            masked = []
            for i, part in enumerate(_list(content, path + ".content")):
                where = f"{path}.content[{i}]"
                _object(part, where, {"type", "text"})
                if part.get("type") != "text":
                    raise UnsupportedRequestError(
                        where, "only text parts are supported"
                    )
                masked.append(
                    {
                        "type": "text",
                        "text": mask(_str(part.get("text"), where + ".text")),
                    }
                )
        out = {"role": role, "content": masked}
        if "name" in message:
            name = _str(message["name"], path + ".name")
            out["name"] = "veil_" + hashlib.sha256(name.encode()).hexdigest()[:32]
        if "refusal" in message:
            if role != "assistant":
                raise UnsupportedRequestError(
                    path + ".refusal", "only assistant refusals are supported"
                )
            out["refusal"] = (
                None
                if message["refusal"] is None
                else mask(_str(message["refusal"], path + ".refusal"))
            )
        return out


def _choices(value: Any, *, stream: bool) -> list[Any]:
    if not isinstance(value, dict) or value.get("object") != (
        "chat.completion.chunk" if stream else "chat.completion"
    ):
        raise StreamError("unsupported chat reply")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) > 1:
        raise StreamError("expected one chat choice")
    if not choices and not (stream and isinstance(value.get("usage"), dict)):
        raise StreamError("missing chat choice")
    for choice in choices:
        if (
            not isinstance(choice, dict)
            or type(choice.get("index")) is not int
            or choice["index"] != 0
        ):
            raise StreamError("invalid chat choice")
        if choice.get("logprobs") is not None:
            raise StreamError("chat log probabilities are unsupported")
        reason = choice.get("finish_reason")
        if reason is not None and (
            not isinstance(reason, str) or reason not in _FINISH
        ):
            raise StreamError("unsupported chat completion reason")
        if not stream and reason is None:
            raise StreamError("incomplete chat reply")
    return choices


def _content(value: Any, *, stream: bool) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {
        "role",
        "content",
        "refusal",
        "annotations",
    }:
        raise StreamError("unsupported chat message content")
    if value.get("role", "assistant") != "assistant":
        raise StreamError("invalid chat reply role")
    if not stream and value.get("role") != "assistant":
        raise StreamError("missing chat reply role")
    if value.get("annotations") not in (None, []):
        raise StreamError("chat annotations are unsupported")
    for key in ("content", "refusal"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise StreamError("chat reply text is invalid")
    return value


class ChatRestorer(ResponsesRestorer):
    """Restore chat prose/refusals and record exact masked history for replay."""

    def response(self, response: Any) -> dict[str, Any]:
        """Validate a complete plain-text reply before returning restored text."""
        value = response
        choices = _choices(value, stream=False)
        choice = choices[0]
        message = _content(choice.get("message"), stream=False)
        restored = dict(message)
        for key in ("content", "refusal"):
            if isinstance(message.get(key), str):
                restored[key] = self.text(message[key])
        return {**value, "choices": [{**choice, "message": restored}]}


@dataclass
class _Channel:
    stream: StreamRestorer
    masked: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)


def _frame(value: dict[str, Any]) -> str:
    return "data: " + json.dumps(value, ensure_ascii=True) + "\n\n"


class ChatStreamRestorer(ResponseRestorer):
    """Restore fragmented text, requiring finish_reason followed by [DONE]."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Use the request session's shield and ledger for both text channels."""
        super().__init__(*args, **kwargs)
        self._channels: dict[str, _Channel] = {}
        self._finished = False
        self._received = 0
        self._wire_received = 0

    def feed(self, data: str) -> str:
        """Parse complete SSE frames while holding split placeholder text."""
        if self.failed is not None:
            return ""
        data = self._carry + data
        self._carry = "\r" if data.endswith("\r") else ""
        if self._carry:
            data = data[:-1]
        self._buffer += data.replace("\r\n", "\n").replace("\r", "\n")
        out: list[str] = []
        try:
            self._wire_received += len(data)
            if len(self._buffer) > _LIMIT or self._wire_received > 2 * _LIMIT:
                raise StreamError("chat event exceeds the limit")
            while "\n\n" in self._buffer:
                raw, self._buffer = self._buffer.split("\n\n", 1)
                out.append(self._handle_chat(raw))
        except (ValueError, TypeError, RecursionError):
            self.failed = (
                "unsupported or incomplete chat stream; the reply is cut off here"
            )
            self.completed = False
            out.append(
                _frame(
                    {"error": {"type": "veil_gateway_error", "message": self.failed}}
                )
            )
        return "".join(out)

    def _handle_chat(self, raw: str) -> str:
        lines = raw.split("\n")
        payloads = []
        for line in lines:
            if not line or line.startswith(":"):
                continue
            key, colon, value = line.partition(":")
            if key == "data" and colon:
                payloads.append(value.removeprefix(" "))
            elif key != "event" or value.strip() not in {"message", ""}:
                raise StreamError("unsupported chat event")
        if not payloads:
            return ": keep-alive\n\n"
        if self.completed:
            raise StreamError("chat data after completion")
        source = "\n".join(payloads)
        if source == "[DONE]":
            if not self._finished:
                raise StreamError("chat ended without a finish reason")
            self.completed = True
            return ""
        value = json.loads(source)
        choices = _choices(value, stream=True)
        if not choices:
            if not self._finished:
                raise StreamError("usage before chat completion")
            return _frame(value)
        if self._finished:
            raise StreamError("chat choice after finish reason")
        choice = choices[0]
        delta = _content(choice.get("delta"), stream=True)
        restored = dict(delta)
        for key in ("content", "refusal"):
            text = delta.get(key)
            if text is None:
                continue
            self._received += len(text)
            if self._received > _LIMIT:
                raise StreamError("chat reply exceeds the limit")
            channel = self._channels.setdefault(
                key, _Channel(self._shield.stream_restorer())
            )
            result = channel.stream.feed(text)
            channel.masked.append(text)
            channel.restored.append(result)
            restored[key] = result
        if choice.get("finish_reason") is not None:
            self._finished = True
            for key, channel in self._channels.items():
                tail = channel.stream.finish()
                channel.restored.append(tail)
                restored[key] = (restored.get(key) or "") + tail
        return _frame({**value, "choices": [{**choice, "delta": restored}]})

    def finish(self) -> str:
        """Record replay text only after a complete stream, never raw leftovers."""
        rest = self.feed("\n") if self._carry else ""
        if self._buffer.strip() or self._carry.strip():
            self.completed = False
        self._buffer = self._carry = ""
        if self.completed and self.failed is None:
            for channel in self._channels.values():
                self._ledger.record_text(
                    "".join(channel.restored), "".join(channel.masked)
                )
            return rest + "data: [DONE]\n\n"
        return rest

    def keepalive(self) -> str:
        """Send a comment while a placeholder is held back."""
        return ": keep-alive\n\n"
