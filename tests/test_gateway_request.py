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
                        "thinking": "The user wants [EMAIL_2].",
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
            assert value not in out, value
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

    def test_a_known_value_glued_to_text_is_masked_in_place(self):
        masker, shield, _ = make_masker(note=None)
        shield.mask("Call 555-123-4567")  # now a known value
        body = {
            "messages": [{"role": "user", "content": "Keep\nCall 555-123-4567-2\nok"}]
        }
        out = masker.mask(body)["messages"][0]["content"]
        assert out == "Keep\nCall [PHONE_1]-2\nok"

    def test_a_value_across_a_line_break_is_masked(self):
        shield = make_shield()
        address = "ul. Fikcyjna 1\n00-950 Nibylandia"
        shield.add_entity(address, "ADDRESS")
        masker = RequestMasker(
            shield, MemoryLedger(), note=None, registered={address: "ADDRESS"}
        )
        body = {
            "messages": [
                {"role": "user", "content": f"Ship to {address}\nhost=a192.0.2.1"}
            ]
        }
        out = masker.mask(body)["messages"][0]["content"]
        assert "Fikcyjna" not in out
        assert "Nibylandia" not in out

    def test_a_line_that_still_leaks_is_withheld(self, monkeypatch):
        masker, shield, _ = make_masker(note=None)
        shield.mask("Call 555-123-4567")
        # If the known-value pass ever failed, the line is withheld.
        monkeypatch.setattr(masker._known, "mask", lambda text: text)
        body = {
            "messages": [{"role": "user", "content": "Keep\nCall 555-123-4567-2\nok"}]
        }
        out = masker.mask(body)["messages"][0]["content"]
        assert out == f"Keep\n{WITHHELD_LINE}\nok"

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


class TestGluedValues:
    """A registered value glued to a word must never go out as it is."""

    def make(self):
        shield = make_shield()
        return RequestMasker(
            shield, MemoryLedger(), note=None, registered={NAME: "PERSON"}
        ), shield

    def test_before_it_was_ever_seen_whole(self):
        masker, _ = self.make()
        body = {"messages": [{"role": "user", "content": f"Rozmawiałem z {NAME}em."}]}
        assert (
            masker.mask(body)["messages"][0]["content"] == "Rozmawiałem z [PERSON_1]em."
        )

    def test_in_a_reply_the_ledger_doesnt_have(self):
        # A fork, a purge, or a reply cut off before it was recorded.
        masker, _ = self.make()
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Das ist {NAME}s Auto."}],
                }
            ]
        }
        assert (
            masker.mask(body)["messages"][0]["content"][0]["text"]
            == "Das ist [PERSON_1]s Auto."
        )

    def test_in_a_summary_or_a_subagent_report(self):
        masker, _ = self.make()
        body = {
            "messages": [
                {"role": "user", "content": f"Summary: we spoke with {NAME}em."},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": f"Report: {NAME}s file",
                        }
                    ],
                },
            ]
        }
        out = json.dumps(masker.mask(body))
        assert NAME not in out
        assert "[PERSON_1]em" in out

    def test_in_replayed_text_after_it_was_registered(self):
        ledger = MemoryLedger()
        ledger.record_text("Ada Quill called", "Ada Quill called")  # before registering
        shield = make_shield()
        shield.add_entity("Ada Quill", "PERSON")
        masker = RequestMasker(
            shield, ledger, note=None, registered={"Ada Quill": "PERSON"}
        )
        body = {"messages": [{"role": "assistant", "content": "Ada Quill called"}]}
        assert masker.mask(body)["messages"][0]["content"] == "[PERSON_1] called"

    def test_thinking_that_holds_a_known_value_is_dropped(self):
        masker, _ = self.make()
        thinking = {
            "type": "thinking",
            "thinking": f"{NAME} wants this",
            "signature": "s",
        }
        clean = {
            "type": "thinking",
            "thinking": "[PERSON_1] wants this",
            "signature": "s",
        }
        text = {"type": "text", "text": "ok"}
        body = {
            "messages": [
                {"role": "assistant", "content": [thinking, text]},
                {"role": "assistant", "content": [clean, text]},
            ]
        }
        out = masker.mask(body)["messages"]
        assert out[0]["content"] == [text]
        assert out[1]["content"] == [clean, text]

    def test_short_values_are_left_inside_words(self):
        shield = make_shield()
        shield.add_entity("Al", "PERSON")
        masker = RequestMasker(
            shield, MemoryLedger(), note=None, registered={"Al": "PERSON"}
        )
        body = {"messages": [{"role": "user", "content": "Al asked about Alabama"}]}
        assert (
            masker.mask(body)["messages"][0]["content"]
            == "[PERSON_1] asked about Alabama"
        )


class TestClearedVault:
    def test_a_forget_between_requests_starts_afresh(self):
        masker, shield, ledger = make_masker(note=None)
        first = {
            "messages": [{"role": "user", "content": "Write to alice@example.com"}]
        }
        assert masker.mask(first)["messages"][0]["content"] == "Write to [EMAIL_1]"
        ledger.record_text("Sent to alice@example.com", "Sent to [EMAIL_1]")
        shield.vault.clear()  # veil forget, from another process
        shield.mask("bob.new@example.com")  # numbering starts again: [EMAIL_1]
        again = {
            "messages": [
                {"role": "user", "content": "Write to alice@example.com"},
                {"role": "assistant", "content": "Sent to alice@example.com"},
            ]
        }
        out = masker.mask(again)["messages"]
        # alice isn't sent under bob's placeholder, from the stale memo or ledger.
        assert out[0]["content"] == "Write to [EMAIL_2]"
        assert out[1]["content"] == "Sent to [EMAIL_2]"
        assert shield.restore("[EMAIL_2]").text == "alice@example.com"


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

    def test_canonical_compares_numbers_as_javascript_does(self):
        assert canonical({"p": 1.0, "q": -0.0, "r": 1e3}) == canonical(
            {"p": 1, "q": 0, "r": 1000}
        )
        assert canonical({"big": 2**60 + 1}) == canonical({"big": float(2**60)})
        assert canonical({"t": True}) != canonical({"t": 1})
        assert canonical(10**400) == str(10**400)


PHONE = "(555) 555-0100"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def opus_body():
    """A request shaped like Claude Code's on Opus 5.5 (Claude Code 2.1.283).

    Claude Code sets the effort per turn, in the output_config of a system
    message in messages[]: a reminder after a user turn, whose text is a
    text block with a cache marker while it is the last message and plain
    text after that, or a message of its own with no content.
    """
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 64000,
        "stream": True,
        "thinking": {"type": "adaptive", "display": "omitted"},
        "output_config": {"effort": "medium"},
        "context_management": {
            "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
        },
        "system": [{"type": "text", "text": "You are an agent."}],
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": f"Email {NAME} at {EMAIL}."}],
            },
            {
                "role": "system",
                "content": f"# Environment\nThe user is {NAME}, on {PHONE}.",
                "output_config": {"effort": "medium"},
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "c2lnbmVk"},
                    {"type": "text", "text": "Sent."},
                ],
            },
            {"role": "user", "content": f"Now call {PHONE}."},
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": f"{NAME} prefers mornings.",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    }
                ],
                "output_config": {"effort": "high"},
            },
        ],
    }


def with_output_config(value, role="system"):
    """A body whose second message has ``output_config`` set to ``value``."""
    return {
        "messages": [
            {"role": "user", "content": "x"},
            {"role": role, "content": [], "output_config": value},
        ]
    }


class TestPerTurnEffort:
    def test_the_effort_goes_out_as_it_is_and_the_text_masked(self):
        masker, _, _ = make_masker(note=None)
        body = opus_body()
        out = masker.mask(body)
        messages = out["messages"]
        assert messages[1] == {
            "role": "system",
            "content": "# Environment\nThe user is [PERSON_1], on [PHONE_1].",
            "output_config": {"effort": "medium"},
        }
        assert messages[3]["content"] == "Now call [PHONE_1]."
        assert messages[4] == {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "[PERSON_1] prefers mornings.",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            "output_config": {"effort": "high"},
        }
        for key in ("model", "thinking", "output_config"):
            assert out[key] == body[key]
        sent = json.dumps(out)
        for value in (NAME, EMAIL, PHONE):
            assert value not in sent, value

    def test_the_body_is_left_alone_and_masks_the_same_way_again(self):
        masker, _, _ = make_masker()
        body = opus_body()
        first = masker.mask(body)
        assert body == opus_body()
        assert masker.mask(opus_body()) == first

    @pytest.mark.parametrize("level", EFFORT_LEVELS)
    def test_every_effort_level_passes(self, level):
        masker, _, _ = make_masker(note=None)
        out = masker.mask(with_output_config({"effort": level}))
        assert out["messages"][1] == {
            "role": "system",
            "content": [],
            "output_config": {"effort": level},
        }

    @pytest.mark.parametrize(
        "effort",
        [
            "ultra",
            "HIGH",
            " high",
            "high\n",
            "jan_nowak",
            NAME,
            "",
            True,
            3,
            1.5,
            float("nan"),
            None,
            ["high"],
            {"level": "high"},
        ],
    )
    def test_only_an_effort_level_is_sent(self, effort):
        masker, _, _ = make_masker()
        with pytest.raises(UnsupportedRequestError) as info:
            masker.mask(with_output_config({"effort": effort}))
        assert str(info.value) == (
            "messages[1].output_config.effort: not an effort level"
        )

    def test_only_a_refused_output_config_is_named_as_one(self):
        # Claude Code (2.1.283) sends the conversation again without its
        # per-turn settings when a refusal names output_config, so the
        # session goes on. A refusal of anything else must not name it.
        masker, _, _ = make_masker()
        for body in (
            with_output_config({"effort": "ultra"}),
            with_output_config({"format": {"type": "json_schema"}}),
            with_output_config({"effort": "high"}, role="user"),
            with_output_config(None),
        ):
            with pytest.raises(UnsupportedRequestError) as info:
                masker.mask(body)
            assert "output_config" in str(info.value)
        clear_at = {"role": "system", "content": "x", "clear_at": "next_user_message"}
        with pytest.raises(UnsupportedRequestError) as info:
            masker.mask({"messages": [clear_at]})
        assert "output_config" not in str(info.value)


BAD_BODIES = [
    (
        {
            "system": [
                {"type": "text", "text": "x", "citations": [{"cited_text": "y"}]}
            ],
            "messages": [],
        },
        "system[0].citations: citations aren't supported",
    ),
    ({"messages": [], "prompt": "x"}, "prompt: unknown field"),
    ({"messages": [], "a b": "x"}, "<key>: unknown field"),
    ("not an object", "$: the body is not a JSON object"),
    ({"messages": {"role": "user"}}, "messages: not a list"),
    (
        {"messages": [{"role": "tool", "content": "x"}]},
        "messages[0].role: unknown role",
    ),
    ({"messages": ["x"]}, "messages[0]: not an object"),
    (
        {"messages": [{"role": "user", "content": "x", "name": "n"}]},
        "messages[0].name: unknown field",
    ),
    (
        {"messages": [{"role": "user", "content": "x", EMAIL: 1}]},
        "messages[0].<key>: unknown field",
    ),
    (
        {
            "messages": [
                {"role": "system", "content": "x", "clear_at": "next_user_message"}
            ]
        },
        "messages[0].clear_at: unknown field",
    ),
    (
        with_output_config({"effort": "high"}, role="user"),
        "messages[1].output_config: only a system message has output_config",
    ),
    (
        with_output_config({"effort": "high"}, role="assistant"),
        "messages[1].output_config: only a system message has output_config",
    ),
    (with_output_config(None), "messages[1].output_config: not an object"),
    (with_output_config(["high"]), "messages[1].output_config: not an object"),
    (with_output_config("high"), "messages[1].output_config: not an object"),
    (with_output_config({}), "messages[1].output_config.effort: not an effort level"),
    (
        with_output_config(
            {"effort": "high", "timing": {"type": "now", "now": "2026-09-27T12:45"}}
        ),
        "messages[1].output_config.timing: unknown field",
    ),
    (
        with_output_config(
            {"format": {"type": "json_schema", "schema": {"description": EMAIL}}}
        ),
        "messages[1].output_config.format: unknown field",
    ),
    (
        with_output_config({"effort": "high", EMAIL: 1}),
        "messages[1].output_config.<key>: unknown field",
    ),
    (
        {"messages": [{"content": [], "output_config": {"effort": "high"}}]},
        "messages[0].role: unknown role",
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
    message_keys = {
        path.split(".")[2].removesuffix("[]")
        for path in census
        if path.startswith("$.messages[].") and path.count(".") == 2
    }
    assert message_keys <= request._MESSAGE_KEYS
    assert {
        path.split(".")[3].removesuffix("[]")
        for path in census
        if path.startswith("$.messages[].output_config.")
    } <= {"effort"}

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
