"""Blocks the API defines: sent back as the API made them, or masked by rule.

A block the gateway saw the API send (a web search result, a server tool's
call, a compaction) goes back exactly as it came, unless it holds a
registered value; otherwise each block type has a rule. None lets a
fictional personal value out.
"""

import json

import pytest

from veil import LiteralPlaceholderDetector, MemoryVault, RegexDetector, Shield
from veil.gateway import (
    MemoryLedger,
    RequestMasker,
    ResponseRestorer,
    SQLiteLedger,
    UnsupportedRequestError,
    restore_message,
)

EMAIL = "jane.doe@example.com"
NAME = "Jan Nowak"
HANDLE = "ada_quill"
USER = {"role": "user", "content": "hi"}
SEARCH_ID = "srvtoolu_01AbCdEfGhIjKlMnOp"


def make(ledger=None, extra=None):
    registered = {NAME: "PERSON", HANDLE: "USER", **(extra or {})}
    types = {*RegexDetector().entity_types, *registered.values()}
    shield = Shield(
        detectors=[LiteralPlaceholderDetector(types), RegexDetector()],
        vault=MemoryVault(),
        redact_warnings=True,
    )
    for value, kind in registered.items():
        shield.add_entity(value, kind)
    shield.mask(f"{NAME} {EMAIL}")  # [PERSON_1], [EMAIL_1]
    ledger = ledger if ledger is not None else MemoryLedger()
    return (
        RequestMasker(shield, ledger, note=None, registered=registered),
        shield,
        ledger,
    )


def search_call():
    return {
        "type": "server_tool_use",
        "id": SEARCH_ID,
        "name": "web_search",
        "input": {"query": "notes of [PERSON_1]"},
    }


def search_result(title="Notes of [PERSON_1] - example.com"):
    return {
        "type": "web_search_tool_result",
        "tool_use_id": SEARCH_ID,
        "content": [
            {
                "type": "web_search_result",
                "url": "https://example.com/notes",
                "title": title,
                "encrypted_content": "EqYBCkgIBxABGAIqQJ1fixture==",
                "page_age": None,
            }
        ],
    }


def history(*blocks):
    return {"messages": [USER, {"role": "assistant", "content": list(blocks)}]}


def seen(shield, ledger, *blocks):
    """Run a reply holding ``blocks`` through the restorer, as the API sent it."""
    restore_message(shield, ledger, {"content": list(blocks)})


class TestAsItCame:
    def test_a_block_the_api_sent_goes_back_exactly(self):
        masker, shield, ledger = make()
        seen(shield, ledger, search_call(), search_result())
        out = masker.mask(history(search_call(), search_result()))
        # Not escaped as literal text: these are the model's own placeholders.
        assert out["messages"][1]["content"] == [search_call(), search_result()]

    def test_a_block_the_api_sent_as_a_stream_goes_back_exactly(self):
        masker, shield, ledger = make()
        call = search_call()
        events = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {**call, "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(call["input"]),
                },
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": search_result(),
            },
            {"type": "content_block_stop", "index": 1},
        ]
        stream = "".join(
            f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
        )
        restorer = ResponseRestorer(shield, ledger)
        restorer.feed(stream)
        out = masker.mask(history(call, search_result()))
        assert out["messages"][1]["content"] == [call, search_result()]

    def test_a_cache_breakpoint_added_later_still_matches(self):
        masker, shield, ledger = make()
        seen(shield, ledger, search_result())
        marked = {**search_result(), "cache_control": {"type": "ephemeral"}}
        assert masker.mask(history(marked))["messages"][1]["content"] == [marked]
        noted = {
            **search_result(),
            "cache_control": {"type": "ephemeral", "note": EMAIL},
        }
        out = masker.mask(history(noted))["messages"][1]["content"][0]
        assert out["cache_control"] == {"type": "ephemeral", "note": "[EMAIL_1]"}

    def test_an_edited_block_is_masked_by_rule(self):
        masker, shield, ledger = make()
        seen(shield, ledger, search_result())
        edited = search_result(title=f"Notes of {NAME}, {EMAIL}")
        out = masker.mask(history(edited))["messages"][1]["content"][0]
        assert out["content"][0]["title"] == "Notes of [PERSON_1], [EMAIL_1]"
        assert out["content"][0]["encrypted_content"] == "EqYBCkgIBxABGAIqQJ1fixture=="

    def test_a_registered_value_is_masked_even_in_a_block_the_api_sent(self):
        masker, shield, ledger = make()
        block = search_result(title=f"About {HANDLE}")
        seen(shield, ledger, block)
        out = masker.mask(history(block))["messages"][1]["content"][0]
        assert HANDLE not in json.dumps(out)
        assert (
            out["content"][0]["encrypted_content"]
            == block["content"][0]["encrypted_content"]
        )

    def test_opaque_bytes_arent_searched_for_registered_values(self):
        # "Ada" inside encrypted content is chance, and the bytes can't change.
        masker, shield, ledger = make(extra={"Ada": "PERSON"})
        block = {
            **search_result(),
            "content": [
                {
                    **search_result()["content"][0],
                    "encrypted_content": "QUdhAdaYWJjZGVm" * 5,
                }
            ],
        }
        seen(shield, ledger, block)
        assert masker.mask(history(block))["messages"][1]["content"] == [block]

    def test_after_a_restart_on_the_same_file(self, tmp_path):
        _, shield, ledger = make(SQLiteLedger(tmp_path / "ledger.db", "s"))
        seen(shield, ledger, search_result())
        ledger.close()
        again = SQLiteLedger(tmp_path / "ledger.db", "s")
        masker = RequestMasker(shield, again, note=None)
        assert masker.mask(history(search_result()))["messages"][1]["content"] == [
            search_result()
        ]
        again.close()

    def test_a_block_type_without_a_rule_the_api_sent_goes_back(self):
        masker, shield, ledger = make()
        block = {
            "type": "future_result",
            "id": SEARCH_ID,
            "encrypted_content": "QWJj" * 20,
        }
        with pytest.raises(UnsupportedRequestError, match="opaque"):
            masker.mask(history(block))  # not seen: its bytes can't be checked
        seen(shield, ledger, block)
        assert masker.mask(history(block))["messages"][1]["content"] == [block]

    def test_in_a_users_message_only_the_apis_bytes_are_trusted(self):
        masker, shield, ledger = make()
        seen(shield, ledger, search_result())
        # The block isn't taken as it came: its text is the user's, masked.
        block = search_result(title=f"Notes of {NAME}")
        body = {"messages": [{"role": "user", "content": [block]}]}
        out = masker.mask(body)["messages"][0]["content"][0]["content"][0]
        assert out["title"] == "Notes of [PERSON_1]"
        # The encrypted bytes are the API's own, so they tell it nothing.
        assert out["encrypted_content"] == block["content"][0]["encrypted_content"]
        other = {**block["content"][0], "encrypted_content": "QWJj" * 20}
        body["messages"][0]["content"][0]["content"] = [other]
        with pytest.raises(UnsupportedRequestError, match="opaque"):
            masker.mask(body)


class TestRules:
    def test_a_server_call_not_seen_is_masked_like_a_tool_input(self):
        masker, _, _ = make()
        call = {**search_call(), "input": {"query": f"notes of {NAME}"}}
        out = masker.mask(history(call))["messages"][1]["content"][0]
        assert out["input"] == {"query": "notes of [PERSON_1]"}

    def test_web_results_mask_their_text_and_keep_their_bytes(self):
        masker, _, _ = make()
        result = search_result(title=f"{NAME} - {EMAIL}")
        out = masker.mask(history(result))["messages"][1]["content"][0]["content"][0]
        assert out["title"] == "[PERSON_1] - [EMAIL_1]"
        assert out["encrypted_content"] == "EqYBCkgIBxABGAIqQJ1fixture=="
        fetched = {
            "type": "web_fetch_tool_result",
            "tool_use_id": SEARCH_ID,
            "content": {
                "type": "web_fetch_result",
                "url": f"https://example.com/?u={EMAIL}",
                "retrieved_at": "2026-09-27T12:00:00Z",
                "content": {
                    "type": "document",
                    "title": f"Page of {NAME}",
                    "source": {
                        "type": "text",
                        "media_type": "text/plain",
                        "data": f"Mail {EMAIL}",
                    },
                },
            },
        }
        out = masker.mask(history(fetched))["messages"][1]["content"][0]["content"]
        assert EMAIL not in json.dumps(out)
        assert NAME not in json.dumps(out)

    def test_mcp_and_search_results_mask_their_text(self):
        masker, _, _ = make()
        mcp = {
            "type": "mcp_tool_result",
            "tool_use_id": "mcptoolu_01AbCdEfGhIjKl",
            "is_error": False,
            "content": [{"type": "text", "text": f"Found {NAME}"}],
        }
        out = masker.mask(history(mcp))["messages"][1]["content"][0]
        assert out["content"] == [{"type": "text", "text": "Found [PERSON_1]"}]
        found = {
            "type": "search_result",
            "source": f"https://example.com/{EMAIL}",
            "title": f"About {NAME}",
            "content": [{"type": "text", "text": f"{NAME} wrote"}],
        }
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        found,
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": [found],
                        },
                    ],
                }
            ]
        }
        assert EMAIL not in json.dumps(masker.mask(body))
        assert NAME not in json.dumps(masker.mask(body))

    def test_a_tool_search_result_names_tools_exactly(self):
        masker, _, _ = make(extra={"ada": "USER"})
        tools = [
            {"name": "Read", "description": "d", "input_schema": {}},
            {"name": "mcp__ada-crm__lookup", "description": "d", "input_schema": {}},
        ]
        refs = [
            {"type": "tool_reference", "tool_name": "Read"},
            {"type": "tool_reference", "tool_name": "mcp__ada-crm__lookup"},
        ]
        body = {
            "tools": tools,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": refs,
                        }
                    ],
                }
            ],
        }
        assert masker.mask(body)["messages"][0]["content"][0]["content"] == refs
        bad = [{"type": "tool_reference", "tool_name": EMAIL}]
        body["messages"][0]["content"][0]["content"] = bad
        with pytest.raises(UnsupportedRequestError, match="a tool name"):
            masker.mask(body)

    def test_tools_added_and_removed_mid_conversation(self):
        masker, _, _ = make()
        definition = {
            "name": "mcp__notes__save",
            "description": f"Saves for {NAME}",
            "input_schema": {},
        }
        changes = {
            "role": "system",
            "content": [
                {"type": "text", "text": "Tools changed."},
                {
                    "type": "tool_removal",
                    "tool": {"type": "tool_reference", "name": "Read"},
                },
                {
                    "type": "tool_addition",
                    "tool": {"type": "tool_definition", "definition": definition},
                },
                {
                    "type": "tool_addition",
                    "tool": {"type": "tool_reference", "name": "Grep"},
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        }
        call = {
            "type": "tool_use",
            "id": "toolu_01",
            "name": "mcp__notes__save",
            "input": {},
        }
        body = {"messages": [USER, changes, {"role": "assistant", "content": [call]}]}
        out = masker.mask(body)["messages"]
        # A tool's definition goes out as it is, as in the request's tools.
        assert out[1]["content"][1:] == changes["content"][1:]
        assert out[2]["content"] == [call]
        bad = {
            "type": "tool_removal",
            "tool": {"type": "tool_reference", "name": EMAIL},
        }
        with pytest.raises(UnsupportedRequestError):
            masker.mask({"messages": [USER, {"role": "system", "content": [bad]}]})

    def test_a_compaction_goes_back_as_the_model_wrote_it(self):
        masker, shield, ledger = make()
        block = {
            "type": "compaction",
            "content": "So far: [PERSON_1] asked to mail [EMAIL_1].",
            "signature": "c2lnbmVk",
        }
        restored = restore_message(shield, ledger, {"content": [block]})["content"][0]
        assert restored["content"] == f"So far: {NAME} asked to mail {EMAIL}."
        assert restored["signature"] == "c2lnbmVk"
        out = masker.mask(history(restored))["messages"][1]["content"][0]
        assert out == block

    def test_a_streamed_compaction_keeps_its_encrypted_part(self):
        _, shield, ledger = make()
        events = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "compaction", "content": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "compaction_delta", "content": "Asked about [PER"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "compaction_delta",
                    "content": "SON_1].",
                    "encrypted_content": "ZW5j",
                },
            },
            {"type": "content_block_stop", "index": 0},
        ]
        stream = "".join(
            f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
        )
        restorer = ResponseRestorer(shield, ledger)
        out = restorer.feed(stream)
        deltas = [
            json.loads(line[6:])["delta"]
            for line in out.splitlines()
            if line.startswith("data: ") and '"compaction_delta"' in line
        ]
        # One event out for each in; the last keeps its encrypted content.
        assert len(deltas) == 2
        assert "".join(d["content"] for d in deltas) == f"Asked about {NAME}."
        assert deltas[-1]["encrypted_content"] == "ZW5j"
        assert ledger.masked_text(f"Asked about {NAME}.") == "Asked about [PERSON_1]."

    def test_citations_mask_their_text(self):
        masker, _, _ = make()
        citation = {
            "type": "web_search_result_location",
            "cited_text": f"{NAME} lives at",
            "url": "https://example.com",
            "title": f"{EMAIL}",
            "encrypted_index": "Eo8BCioIAhgB",
        }
        text = {"type": "text", "text": "Found it.", "citations": [citation]}
        out = masker.mask(history(text))["messages"][1]["content"][0]["citations"][0]
        assert out["cited_text"] == "[PERSON_1] lives at"
        assert out["title"] == "[EMAIL_1]"
        assert out["encrypted_index"] == "Eo8BCioIAhgB"
        user = {"messages": [{"role": "user", "content": [text]}]}
        with pytest.raises(UnsupportedRequestError, match="opaque"):
            masker.mask(user)  # an encrypted index only in the model's messages

    @pytest.mark.parametrize(
        ("file_id", "ok"), [("file_011CNha8iCJcU1wXNR6q4V8w", True), (EMAIL, False)]
    )
    def test_file_sources_are_ids(self, file_id, ok):
        masker, _, _ = make()
        blocks = [
            {"type": "image", "source": {"type": "file", "file_id": file_id}},
            {"type": "document", "source": {"type": "file", "file_id": file_id}},
            {"type": "container_upload", "file_id": file_id},
        ]
        body = {"messages": [{"role": "user", "content": blocks}]}
        if ok:
            assert masker.mask(body)["messages"][0]["content"] == blocks
        else:
            with pytest.raises(UnsupportedRequestError, match="an id"):
                masker.mask(body)

    def test_a_document_of_blocks_is_masked(self):
        masker, _, _ = make()
        document = {
            "type": "document",
            "source": {
                "type": "content",
                "content": [{"type": "text", "text": f"Dear {NAME}"}],
            },
        }
        out = masker.mask({"messages": [{"role": "user", "content": [document]}]})
        assert out["messages"][0]["content"][0]["source"]["content"] == [
            {"type": "text", "text": "Dear [PERSON_1]"}
        ]

    def test_an_advisor_result_and_a_fallback(self):
        masker, _, _ = make()
        advice = {
            "type": "advisor_tool_result",
            "tool_use_id": SEARCH_ID,
            "content": {
                "type": "advisor_result",
                "text": f"Ask {NAME}",
                "stop_reason": "end_turn",
            },
        }
        fallback = {
            "type": "fallback",
            "from": {"model": "claude-opus-5-5"},
            "to": {"model": "claude-sonnet-5"},
            "trigger": {"type": "refusal", "category": None},
        }
        out = masker.mask(history(advice, fallback))["messages"][1]["content"]
        assert out[0]["content"]["text"] == "Ask [PERSON_1]"
        assert out[1] == fallback
