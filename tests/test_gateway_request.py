"""Masking Messages API requests: every field has a rule, unknown ones are refused."""

import copy
import json

import pytest

from veil import LiteralPlaceholderDetector, MemoryVault, RegexDetector, Shield
from veil.gateway import (
    DEFAULT_NOTE,
    WITHHELD_LINE,
    MemoryLedger,
    RequestMasker,
    UnsupportedRequestError,
)
from veil.gateway.ledger import canonical

EMAIL = "jane.doe@example.com"
ACCOUNT = "account.owner@example.com"
NAME = "Jan Nowak"
BILLING = "x-anthropic-billing-header: cc_version=2.1.99.146; cc_entrypoint=cli"


def make_shield():
    shield = Shield(
        detectors=[
            LiteralPlaceholderDetector({"EMAIL", "PHONE", "PERSON"}),
            RegexDetector(),
        ],
        vault=MemoryVault(),
        redact_warnings=True,
    )
    shield.add_entity(NAME, "PERSON")
    return shield


def make_masker(**kwargs):
    shield = make_shield()
    ledger = MemoryLedger()
    return RequestMasker(shield, ledger, **kwargs), shield, ledger


def claude_code_body():
    """A request shaped like Claude Code's (see tests/gateway_payloads)."""
    return {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 32000,
        "stream": True,
        "metadata": {"user_id": json.dumps({"session_id": "s-1", "device_id": "d"})},
        "thinking": {"type": "adaptive"},
        "context_management": {
            "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
        },
        "tools": [
            {
                "name": "Read",
                "description": "Reads a file, e.g. ada.q@example.com.txt",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                },
            }
        ],
        "system": [
            {"type": "text", "text": BILLING},
            {
                "type": "text",
                "text": f"You are a helpful agent. The user is {ACCOUNT}.",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<system-reminder>{ACCOUNT}</system-reminder>",
                    },
                    {"type": "text", "text": f"Email {NAME} at {EMAIL} please."},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": f"The user wants {EMAIL}.",
                        "signature": "sig==",
                    },
                    {"type": "redacted_thinking", "data": "opaque"},
                    {"type": "text", "text": f"I'll read the notes for {NAME}."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "Read",
                        "input": {
                            "file_path": "/work/notes.txt",
                            "note": f"for {EMAIL}",
                            "limit": 5,
                        },
                        "caller": {"type": "direct"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": f"Exit code 1\nName: {NAME}\nEmail: {EMAIL}",
                        "is_error": True,
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_02",
                        "content": [
                            {"type": "text", "text": f"Card for {NAME}"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "iVBOR",
                                },
                            },
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": "JVBER",
                                },
                            },
                        ],
                    },
                ],
            },
            {"role": "system", "content": f"Reminder: {EMAIL} is the contact."},
        ],
    }


class TestMasking:
    def test_every_text_the_model_reads_is_masked(self):
        masker, _, _ = make_masker(note=None)
        out = json.dumps(masker.mask(claude_code_body())["messages"])
        for value in (EMAIL, ACCOUNT, NAME):
            # Only the signed thinking block may still hold one (see below).
            assert out.count(value) == (1 if value == EMAIL else 0), value
        assert "[EMAIL_2]" in out
        assert "[PERSON_1]" in out

    def test_system_text_is_masked_but_the_billing_header_is_not(self):
        masker, _, _ = make_masker(note=None)
        system = masker.mask(claude_code_body())["system"]
        assert system[0] == {"type": "text", "text": BILLING}
        assert system[1]["text"] == "You are a helpful agent. The user is [EMAIL_1]."
        assert system[1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_settings_tools_and_media_pass_unchanged(self):
        masker, _, _ = make_masker(note=None)
        body = claude_code_body()
        out = masker.mask(body)
        for key in (
            "model",
            "max_tokens",
            "stream",
            "metadata",
            "thinking",
            "context_management",
            "tools",
        ):
            assert out[key] == body[key]
        nested = out["messages"][2]["content"][1]["content"]
        assert nested[1] == body["messages"][2]["content"][1]["content"][1]
        assert nested[2] == body["messages"][2]["content"][1]["content"][2]

    def test_signed_thinking_is_sent_exactly_as_written(self):
        masker, _, _ = make_masker(note=None)
        body = claude_code_body()
        blocks = masker.mask(body)["messages"][1]["content"]
        assert blocks[0] == body["messages"][1]["content"][0]
        assert blocks[1] == {"type": "redacted_thinking", "data": "opaque"}

    def test_tool_inputs_and_results_are_masked(self):
        masker, _, _ = make_masker(note=None)
        messages = masker.mask(claude_code_body())["messages"]
        tool_use = messages[1]["content"][3]
        assert tool_use["input"] == {
            "file_path": "/work/notes.txt",
            "note": "for [EMAIL_2]",
            "limit": 5,
        }
        assert tool_use["caller"] == {"type": "direct"}
        result = messages[2]["content"][0]
        assert result["content"] == "Exit code 1\nName: [PERSON_1]\nEmail: [EMAIL_2]"
        assert result["is_error"] is True
        assert messages[2]["content"][1]["content"][0]["text"] == "Card for [PERSON_1]"
        assert messages[3]["content"] == "Reminder: [EMAIL_2] is the contact."

    def test_the_body_is_not_changed_in_place(self):
        masker, _, _ = make_masker()
        body = claude_code_body()
        before = copy.deepcopy(body)
        masker.mask(body)
        assert body == before

    def test_the_same_body_masks_the_same_way(self):
        masker, _, _ = make_masker()
        first = masker.mask(claude_code_body())
        assert masker.mask(claude_code_body()) == first
        # A fresh masker on the same vault gives the same result too.
        shield = masker._shield
        again = RequestMasker(shield, MemoryLedger()).mask(claude_code_body())
        assert again == first

    def test_the_note_names_no_placeholder_that_could_exist(self):
        from veil.placeholders import LOOSE_PLACEHOLDER_RE

        assert LOOSE_PLACEHOLDER_RE.search(DEFAULT_NOTE) is None

    def test_the_note_is_the_last_system_block(self):
        masker, _, _ = make_masker()
        system = masker.mask(claude_code_body())["system"]
        assert system[-1] == {"type": "text", "text": DEFAULT_NOTE}
        assert len(system) == 3
        plain = make_masker()[0].mask({"messages": [], "system": "Hi"})["system"]
        assert plain == [
            {"type": "text", "text": "Hi"},
            {"type": "text", "text": DEFAULT_NOTE},
        ]
        assert make_masker()[0].mask({"messages": []})["system"] == [
            {"type": "text", "text": DEFAULT_NOTE}
        ]

    def test_documents_with_text_are_masked(self):
        masker, _, _ = make_masker(note=None)
        block = {
            "type": "document",
            "title": f"Notes of {NAME}",
            "context": f"From {EMAIL}",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": f"Hi {NAME}",
            },
        }
        out = masker.mask({"messages": [{"role": "user", "content": [block]}]})
        masked = out["messages"][0]["content"][0]
        assert masked["title"] == "Notes of [PERSON_1]"
        assert masked["context"] == "From [EMAIL_1]"
        assert masked["source"]["data"] == "Hi [PERSON_1]"

    def test_stop_sequences_and_safeguards_are_masked(self):
        masker, _, _ = make_masker(note=None)
        out = masker.mask(
            {
                "messages": [],
                "stop_sequences": [f"Bye {NAME}"],
                "safeguards": [
                    {
                        "type": "dangerous_tool_use",
                        "classifier_context": {
                            "user_identity": ACCOUNT,
                            "home_dir": "/Users/example",
                            "v": 1,
                            "rules": {"allow": ["Bash(git:*)"]},
                        },
                    }
                ],
            }
        )
        assert out["stop_sequences"] == ["Bye [PERSON_1]"]
        context = out["safeguards"][0]["classifier_context"]
        assert out["safeguards"][0]["type"] == "dangerous_tool_use"
        assert context["user_identity"] == "[EMAIL_1]"
        assert context["home_dir"] == "/Users/example"
        assert context["v"] == 1
        assert context["rules"] == {"allow": ["Bash(git:*)"]}

    def test_placeholder_like_text_is_masked_as_literal(self):
        masker, shield, _ = make_masker(note=None)
        body = {
            "messages": [{"role": "user", "content": f"Template [EMAIL_1] for {EMAIL}"}]
        }
        out = masker.mask(body)["messages"][0]["content"]
        assert out == "Template [LITERAL_1] for [EMAIL_1]"
        assert shield.restore(out).text == f"Template [EMAIL_1] for {EMAIL}"

    def test_a_line_that_still_leaks_is_withheld(self):
        masker, shield, _ = make_masker(note=None)
        shield.mask("Call 555-123-4567")  # now a known value
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "Keep this line\nCall 555-123-4567-2\nand this",
                }
            ]
        }
        out = masker.mask(body)["messages"][0]["content"]
        assert out == f"Keep this line\n{WITHHELD_LINE}\nand this"
        assert "555-123-4567" not in json.dumps(masker.mask(body))

    def test_masked_text_is_reused(self):
        masker, shield, _ = make_masker(note=None)
        calls = []
        real_mask = shield.mask
        shield.mask = lambda text: calls.append(text) or real_mask(text)
        body = {"messages": [{"role": "user", "content": f"Hi {NAME}"}]}
        first = masker.mask(body)
        second = masker.mask(body)
        assert first == second
        assert calls == [f"Hi {NAME}"]

    def test_the_memo_drops_old_entries_past_its_limit(self):
        masker, _, _ = make_masker(note=None, memo_limit=50)
        for i in range(20):
            masker.mask({"messages": [{"role": "user", "content": f"text number {i}"}]})
        assert masker._memo._size <= 50


class TestLedger:
    def test_a_restored_reply_goes_back_as_the_model_wrote_it(self):
        masker, shield, ledger = make_masker(note=None)
        shield.mask(f"{NAME}")
        # The model wrote a placeholder glued to a word ending; restored, it
        # would no longer mask back (not a whole-word match).
        model_text = "Spoke with [PERSON_1]em today"
        restored = shield.restore(model_text).text
        assert restored == f"Spoke with {NAME}em today"
        ledger.record_text(restored, model_text)
        body = {
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": restored}]}
            ]
        }
        assert masker.mask(body)["messages"][0]["content"][0]["text"] == model_text
        as_string = {"messages": [{"role": "assistant", "content": restored}]}
        assert masker.mask(as_string)["messages"][0]["content"] == model_text

    def test_only_assistant_text_is_replayed(self):
        masker, _, ledger = make_masker(note=None)
        ledger.record_text(f"Hi {NAME}", "Hi [PERSON_7]")
        body = {"messages": [{"role": "user", "content": f"Hi {NAME}"}]}
        assert masker.mask(body)["messages"][0]["content"] == "Hi [PERSON_1]"

    def test_tool_inputs_are_replayed_while_unchanged(self):
        masker, _, ledger = make_masker(note=None)
        restored = {"command": f"echo {NAME}", "description": "Say hi"}
        ledger.record_tool_input(
            "toolu_9", restored, {"command": "echo [PERSON_1]", "description": "Say hi"}
        )

        def body(tool_input):
            block = {
                "type": "tool_use",
                "id": "toolu_9",
                "name": "Bash",
                "input": tool_input,
            }
            return {"messages": [{"role": "assistant", "content": [block]}]}

        # Key order and spacing don't matter; the content does.
        reordered = {"description": "Say hi", "command": f"echo {NAME}"}
        replayed = masker.mask(body(reordered))["messages"][0]["content"][0]["input"]
        assert replayed == {"command": "echo [PERSON_1]", "description": "Say hi"}
        changed = {"command": f"echo {NAME}!", "description": "Say hi"}
        remasked = masker.mask(body(changed))["messages"][0]["content"][0]["input"]
        assert remasked == {"command": "echo [PERSON_1]!", "description": "Say hi"}

    def test_memory_ledger_keeps_no_restored_text(self):
        ledger = MemoryLedger()
        ledger.record_text(f"Hi {NAME}", "Hi [PERSON_1]")
        ledger.record_tool_input("t", {"a": NAME}, {"a": "[PERSON_1]"})
        held = json.dumps([ledger._texts, {k: v[0] for k, v in ledger._tools.items()}])
        assert NAME not in held
        assert ledger.masked_text(f"Hi {NAME}") == "Hi [PERSON_1]"
        assert ledger.masked_text("other") is None
        assert ledger.masked_tool_input("missing", {}) is None

    def test_canonical_ignores_key_order_and_spacing(self):
        assert canonical({"b": 1, "a": [1, 2]}) == canonical({"a": [1, 2], "b": 1})


BAD_BODIES = [
    ({"messages": [], "prompt": "x"}, "prompt: unknown field"),
    ({"messages": [], "a b": "x"}, "<key>: unknown field"),
    ("not an object", "$: the body is not a JSON object"),
    ({"messages": {"role": "user"}}, "messages: not a list"),
    (
        {"messages": [{"role": "tool", "content": "x"}]},
        "messages[0].role: unknown role",
    ),
    (
        {"messages": [{"role": "user", "content": "x", "name": "n"}]},
        "messages[0]: a message has only role and content",
    ),
    (
        {
            "messages": [
                {"role": "user", "content": [{"type": "server_tool_use", "id": "s"}]}
            ]
        },
        "messages[0].content[0].type: unknown block type",
    ),
    (
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "x", "extra": 1}]}
            ]
        },
        "messages[0].content[0].extra: unknown field",
    ),
    (
        {"messages": [{"role": "user", "content": [{"type": "text", "text": 5}]}]},
        "messages[0].content[0].text: not a string",
    ),
    (
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "x",
                            "citations": [{"cited_text": "y"}],
                        }
                    ],
                }
            ]
        },
        "messages[0].content[0].citations: citations aren't supported",
    ),
    (
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "file", "file_id": "f"}}
                    ],
                }
            ]
        },
        "messages[0].content[0].source.type: unknown source type",
    ),
    (
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "content", "content": []},
                        }
                    ],
                }
            ]
        },
        "messages[0].content[0].source.type: unknown document source",
    ),
    (
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": [{"type": "tool_use"}],
                        }
                    ],
                }
            ]
        },
        "messages[0].content[0].content[0].type: unknown block type",
    ),
    (
        {"system": [{"type": "image", "source": {}}], "messages": []},
        "system[0].type: unknown block type",
    ),
    ({"messages": [], "stop_sequences": [1]}, "stop_sequences[0]: not a string"),
]


@pytest.mark.parametrize(("body", "message"), BAD_BODIES)
def test_anything_without_a_rule_is_refused(body, message):
    masker, _, _ = make_masker()
    with pytest.raises(UnsupportedRequestError) as info:
        masker.mask(body)
    assert str(info.value) == message
    assert info.value.path == message.split(": ")[0]


def test_refusals_never_quote_a_value():
    masker, _, _ = make_masker()
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": EMAIL, EMAIL: 1}]}
        ]
    }
    with pytest.raises(UnsupportedRequestError) as info:
        masker.mask(body)
    # Keys are named only when they look like field names.
    assert str(info.value) == "messages[0].content[0].<key>: unknown field"
    body = {"messages": [{"role": EMAIL, "content": "x"}]}
    with pytest.raises(UnsupportedRequestError) as info:
        masker.mask(body)
    assert EMAIL not in str(info.value)


def test_every_recorded_field_and_block_type_has_a_rule():
    from pathlib import Path

    from veil.gateway import request

    census_file = Path(__file__).parent / "gateway_payloads" / "request_census.json"
    census = json.loads(census_file.read_text())["POST /v1/messages"]
    handled = request._TOP_LEVEL_PASS | {
        "system",
        "messages",
        "stop_sequences",
        "safeguards",
    }
    top_level = {
        path[2:] for path in census if path.count(".") == 1 and "[" not in path
    }
    assert top_level <= handled

    def types_at(path):
        return {kind[5:] for kind in census[path]["kinds"] if kind.startswith("type=")}

    assert types_at("$.messages[].content[].type") <= set(request._BLOCK_KEYS)
    assert types_at("$.messages[].content[].content[].type") <= request._NESTED_BLOCKS
    assert types_at("$.system[].type") == {"text"}
    assert types_at("$.messages[].content[].content[].source.type") <= {"base64", "url"}
    for path in census:
        if path.startswith("$.messages[].content[].") and path.count(".") == 3:
            key = path.rsplit(".", 1)[1].removesuffix("[]")
            assert (
                any(key in keys for keys in request._BLOCK_KEYS.values())
                or key == "type"
            )
