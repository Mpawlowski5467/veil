"""Mask the text portions of stateless OpenAI Responses API requests.

This adapter accepts text messages and local function/custom tools. Images,
files, hosted tools, stored conversations, and unrecognized request shapes
are refused. Tool definitions are protocol configuration and stay unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .request import RequestMasker, UnsupportedRequestError, _key, _list, _str

_ITEM_KEYS = {
    "message": {"type", "id", "role", "content", "status", "phase"},
    "function_call": {"type", "id", "call_id", "name", "arguments", "status"},
    "custom_tool_call": {"type", "id", "call_id", "name", "input", "status"},
    "function_call_output": {"type", "id", "call_id", "output", "status"},
    "custom_tool_call_output": {"type", "id", "call_id", "output", "status"},
    "reasoning": {"type", "id", "summary", "content", "encrypted_content", "status"},
    "additional_tools": {"type", "id", "role", "tools"},
}
_BOOLEAN_FIELDS = {"stream", "parallel_tool_calls"}
_NUMBER_FIELDS = {"temperature", "top_p", "max_output_tokens", "max_tool_calls"}
_STRING_FIELDS = {"model", "service_tier", "truncation", "prompt_cache_retention"}
_ROOT_KEYS = {
    "input",
    "instructions",
    "tools",
    "tool_choice",
    "reasoning",
    "text",
    "include",
    "metadata",
    "client_metadata",
    "prompt_cache_key",
    "safety_identifier",
    "user",
    "store",
    "background",
    "previous_response_id",
    "conversation",
    *_BOOLEAN_FIELDS,
    *_NUMBER_FIELDS,
    *_STRING_FIELDS,
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/-]{1,256}\Z")


def opaque_key(item_id: str) -> str:
    """Namespace the provenance record for encrypted reasoning."""
    return "openai:reasoning:" + item_id


def _object(value: Any, path: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UnsupportedRequestError(path, "not an object")
    extra = set(value) - keys
    if extra:
        raise UnsupportedRequestError(
            f"{path}.{_key(sorted(extra)[0])}", "unknown field"
        )
    return value


def _identifier(value: Any, path: str) -> str:
    identifier = _str(value, path)
    if not _IDENTIFIER.fullmatch(identifier):
        raise UnsupportedRequestError(path, "not a protocol identifier")
    return identifier


def _json(value: Any, path: str) -> Any:
    try:
        return json.loads(_str(value, path))
    except ValueError:
        raise UnsupportedRequestError(path, "not valid JSON") from None


class ResponsesRequestMasker(RequestMasker):
    """Mask supported Responses API requests using the session's vault and ledger.

    Construction has the same options as `RequestMasker`. Only text input and
    locally executed function/custom tools are supported. No inherited server
    conversation state is accepted, and storage is always disabled.
    """

    def mask(self, body: dict[str, Any]) -> dict[str, Any]:
        """Return a masked copy; refuse anything without an explicit rule."""
        _object(body, "$", _ROOT_KEYS)
        self._notice_a_cleared_vault()
        out: dict[str, Any] = {}
        for key, value in body.items():
            out[key] = self._field(key, value)
        if "input" not in out:
            raise UnsupportedRequestError("input", "required")
        out["store"] = False
        if self._note:
            instructions = out.get("instructions") or ""
            out["instructions"] = f"{instructions}\n\n{self._note}".lstrip()
        self._vault_state = self._state()
        return out

    def _field(self, key: str, value: Any) -> Any:
        if key == "input":
            if isinstance(value, str):
                return self._mask_string(value)
            return [
                self._item(item, f"input[{i}]")
                for i, item in enumerate(_list(value, key))
            ]
        if key == "instructions":
            return None if value is None else self._mask_string(_str(value, key))
        if key in {"store", "background"}:
            if value not in (False, None) or (
                isinstance(value, (int, float)) and not isinstance(value, bool)
            ):
                raise UnsupportedRequestError(key, "only false is supported")
            return value
        if key in {"previous_response_id", "conversation"}:
            if value is not None:
                raise UnsupportedRequestError(
                    key, "replay masked history locally instead"
                )
            return None
        if key in _BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                raise UnsupportedRequestError(key, "not a boolean")
            return value
        if key in _NUMBER_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise UnsupportedRequestError(key, "not a number")
            return value
        if key in _STRING_FIELDS:
            return _identifier(value, key)
        if key in {"metadata", "client_metadata"}:
            if value is None:
                return None
            if not isinstance(value, dict):
                raise UnsupportedRequestError(key, "not an object")
            # Metadata is user data too, including its keys.
            return self._value(value)
        if key in {"prompt_cache_key", "safety_identifier", "user"}:
            if value is None:
                return None
            return hashlib.sha256(_str(value, key).encode()).hexdigest()
        if key == "include":
            values = _list(value, key)
            if any(
                v not in ("reasoning.encrypted_content", "message.output_text.logprobs")
                for v in values
            ):
                raise UnsupportedRequestError(key, "unsupported output inclusion")
            return values
        if key == "tools":
            return self._tools(value, key)
        if key == "tool_choice":
            if isinstance(value, str) and value in {"auto", "none", "required"}:
                return value
            choice = _object(value, key, {"type", "name"})
            if choice.get("type") not in {"function", "custom"}:
                raise UnsupportedRequestError(key, "unsupported tool choice")
            _identifier(choice.get("name"), key + ".name")
            return dict(choice)
        if key == "reasoning":
            choices = {
                "effort": {
                    "none",
                    "minimal",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                },
                "summary": {"auto", "concise", "detailed"},
                # Codex 0.156.1 sends this with a stateless history replay.
                "context": {"all_turns"},
            }
            config = _object(value, key, set(choices))
            for name, item in config.items():
                if not isinstance(item, str) or item not in choices[name]:
                    raise UnsupportedRequestError(
                        key + "." + name, "unsupported setting"
                    )
            return dict(config)
        if key == "text":
            config = _object(value, key, {"format", "verbosity"})
            if "verbosity" in config:
                _identifier(config["verbosity"], "text.verbosity")
            if "format" in config:
                fmt = _object(config["format"], "text.format", {"type"})
                if fmt.get("type") != "text":
                    raise UnsupportedRequestError(
                        "text.format", "only plain text is supported"
                    )
            return dict(config)
        raise UnsupportedRequestError(key, "unsupported field")

    def _mask_string(self, value: str, *, assistant: bool = False) -> str:
        masked = self._reply_text(value) if assistant else self._text(value)
        # A value registered since an earlier request must not evade a cached mask.
        return self._known.mask(masked)

    def _value(self, value: Any, *, known_only: bool = False) -> Any:
        mask = self._known.mask if known_only else self._mask_string
        if isinstance(value, str):
            return mask(value)
        if isinstance(value, list):
            return [self._value(item, known_only=known_only) for item in value]
        if isinstance(value, dict):
            out = {
                mask(key): self._value(item, known_only=known_only)
                for key, item in value.items()
            }
            if len(out) != len(value):
                raise UnsupportedRequestError("input", "masked object keys collide")
            return out
        return value

    def _item(self, value: Any, path: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise UnsupportedRequestError(path, "not an object")
        kind = value.get("type", "message")
        if not isinstance(kind, str) or kind not in _ITEM_KEYS:
            raise UnsupportedRequestError(path + ".type", "unsupported input item")
        item = _object(value, path, _ITEM_KEYS[kind])
        out = dict(item)
        for field in ("id", "call_id", "name", "status", "phase"):
            if item.get(field) is not None:
                _identifier(item[field], path + "." + field)
        if kind in {"message", "additional_tools"} and item.get("role") not in {
            "system",
            "developer",
            "user",
            "assistant",
        }:
            raise UnsupportedRequestError(path + ".role", "unsupported role")
        if kind == "message":
            out["content"] = self._content(
                item.get("content"),
                path + ".content",
                assistant=item["role"] == "assistant",
            )
        elif kind == "additional_tools":
            out["tools"] = self._tools(item.get("tools"), path + ".tools")
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            _identifier(item.get("call_id"), path + ".call_id")
            out["output"] = self._content(item.get("output"), path + ".output")
        elif kind in {"function_call", "custom_tool_call"}:
            call_id = _identifier(item.get("call_id"), path + ".call_id")
            _identifier(item.get("name"), path + ".name")
            field = "arguments" if kind == "function_call" else "input"
            original = (
                _json(item.get(field), path + "." + field)
                if field == "arguments"
                else _str(item.get(field), path + "." + field)
            )
            recorded = self._ledger.masked_tool_input(call_id, original)
            masked = (
                self._value(recorded, known_only=True)
                if recorded is not None
                else self._value(original)
            )
            out[field] = (
                json.dumps(masked, ensure_ascii=False)
                if field == "arguments"
                else masked
            )
        elif kind == "reasoning":
            if item.get("encrypted_content"):
                item_id = _identifier(item.get("id"), path + ".id")
                encrypted = _str(item["encrypted_content"], path + ".encrypted_content")
                if (
                    self._ledger.masked_tool_input(opaque_key(item_id), encrypted)
                    != encrypted
                ):
                    raise UnsupportedRequestError(
                        path + ".encrypted_content",
                        "reasoning was not received through this session",
                    )
            for field in ("summary", "content"):
                if field in item:
                    out[field] = self._content(
                        item[field], path + "." + field, assistant=True
                    )
        return out

    def _content(self, value: Any, path: str, *, assistant: bool = False) -> Any:
        if isinstance(value, str):
            return self._mask_string(value, assistant=assistant)
        out = []
        for i, block in enumerate(_list(value, path)):
            at = f"{path}[{i}]"
            if isinstance(block, dict) and block.get("type") == "refusal":
                _object(block, at, {"type", "refusal"})
                out.append(
                    {
                        **block,
                        "refusal": self._mask_string(
                            _str(block.get("refusal"), at + ".refusal"),
                            assistant=assistant,
                        ),
                    }
                )
                continue
            block = _object(block, at, {"type", "text", "annotations", "logprobs"})
            if block.get("type") not in {
                "input_text",
                "output_text",
                "summary_text",
                "reasoning_text",
            }:
                raise UnsupportedRequestError(
                    at + ".type", "only text content is supported"
                )
            if block.get("annotations") or block.get("logprobs"):
                raise UnsupportedRequestError(
                    at, "annotations and logprobs are unsupported"
                )
            out.append(
                {
                    **block,
                    "text": self._mask_string(
                        _str(block.get("text"), at + ".text"), assistant=assistant
                    ),
                }
            )
        return out

    def _tools(self, value: Any, path: str) -> list[Any]:
        out = []
        for i, tool in enumerate(_list(value, path)):
            at = f"{path}[{i}]"
            if not isinstance(tool, dict):
                raise UnsupportedRequestError(at, "not an object")
            kind = tool.get("type")
            if kind == "namespace":
                _object(tool, at, {"type", "name", "description", "tools"})
                _identifier(tool.get("name"), at + ".name")
                out.append(
                    {**tool, "tools": self._tools(tool.get("tools"), at + ".tools")}
                )
                continue
            keys = {
                "function": {
                    "type",
                    "name",
                    "description",
                    "parameters",
                    "strict",
                    "async",
                    "defer_loading",
                },
                "custom": {
                    "type",
                    "name",
                    "description",
                    "format",
                    "async",
                    "defer_loading",
                },
            }
            if not isinstance(kind, str) or kind not in keys:
                raise UnsupportedRequestError(
                    at + ".type", "only local function and custom tools are supported"
                )
            _object(tool, at, keys[kind])
            _identifier(tool.get("name"), at + ".name")
            out.append(dict(tool))
        return out
