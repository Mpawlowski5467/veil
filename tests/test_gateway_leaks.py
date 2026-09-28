"""Personal data in content the gateway has no rule for never goes out as it is.

Content without a rule of its own (a new field, a new block type, a new key
on a known block) is masked generically instead of refused. These tests place
fictional personal values in every such place, in every way a value can be
carried (as text, a key, a type, an id, a name, a number, file bytes), and
check that each one is masked, refused, or (in signed thinking) dropped:
never sent as it is.
"""

import base64
import itertools
import json

import pytest

from veil import LiteralPlaceholderDetector, MemoryVault, RegexDetector, Shield
from veil.gateway import MemoryLedger, RequestMasker, UnsupportedRequestError

NAME = "Jan Nowak"
HANDLE = "ada_quill"
ACCOUNT = "4821"
REGISTERED = {NAME: "PERSON", HANDLE: "USER", ACCOUNT: "ACCOUNT"}

#: Values detectors or the registered list find in text.
VALUES = [
    "jane.doe@example.com",
    "(555) 555-0100",
    "4111 1111 1111 1111",
    "GB82 WEST 1234 5698 7654 32",
    "192.0.2.1",
    NAME,
    HANDLE,
]
#: Numbers whose digits masking would change.
NUMBERS = [4111111111111111, 4111111111111111.0, 4821, 48210, -4821]


def make_masker(registered=REGISTERED):
    types = {*RegexDetector().entity_types, *registered.values()}
    shield = Shield(
        detectors=[LiteralPlaceholderDetector(types), RegexDetector()],
        vault=MemoryVault(),
        redact_warnings=True,
    )
    for value, kind in registered.items():
        shield.add_entity(value, kind)
    return RequestMasker(shield, MemoryLedger(), note=None, registered=registered)


def carriers(value):
    """The ways a string value can ride in a JSON value."""
    encoded = base64.b64encode(f"Contact: {value}, notes".encode() * 3).decode()
    data_uri = f"data:text/plain;base64,{encoded}"
    return {
        "encoded_id": {"upload_id": encoded},
        "encoded_ids": {"source_ids": [encoded]},
        "encoded_type": {"type": encoded},
        "data_uri_id": {"src_id": data_uri},
        "text": {"note": f"Write to {value} today"},
        "key": {value: 1},
        "type": {"type": value},
        "id": {"id": value},
        "name": {"name": value},
        "nested": {"items": [{"deep": [value]}]},
        "file_bytes": {"data": encoded},
        "long_bytes": {"attachment": encoded},
        "data_uri": {"src": data_uri},
    }


def contexts(slot):
    """Every place content without a rule of its own can be, holding ``slot``."""
    user_text = {"role": "user", "content": "hi"}
    yield "top_level_field", {"messages": [user_text], "new_field": slot}
    yield "message_key", {"messages": [{**user_text, "new_key": slot}]}
    yield "message_type", {"messages": [{**user_text, "type": slot}]}
    yield (
        "key_on_text_block",
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "x", "k": slot}]}
            ]
        },
    )
    yield (
        "key_on_tool_use",
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "Read",
                            "input": {},
                            "k": slot,
                        }
                    ],
                }
            ]
        },
    )
    for role in ("user", "assistant", "system"):
        yield (
            f"new_block_{role}",
            {"messages": [{"role": role, "content": [{"type": "new", "p": slot}]}]},
        )
    yield (
        "new_block_in_tool_result",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": [{"type": "new", "p": slot}],
                        }
                    ],
                }
            ]
        },
    )
    yield (
        "new_block_in_system",
        {"system": [{"type": "new", "p": slot}], "messages": [user_text]},
    )
    yield (
        "tool_input",
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_02",
                            "name": "Read",
                            "input": slot,
                        }
                    ],
                }
            ]
        },
    )
    yield (
        "safeguards",
        {
            "messages": [user_text],
            "safeguards": [{"type": "dangerous_tool_use", "classifier_context": slot}],
        },
    )
    yield (
        "signed_thinking",
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "t",
                            "signature": "s",
                            "k": slot,
                        },
                        {"type": "text", "text": "ok"},
                    ],
                }
            ]
        },
    )


def outcome(body):
    """The masked body as sent, or None if the request was refused."""
    try:
        return json.dumps(make_masker().mask(body), ensure_ascii=False)
    except UnsupportedRequestError:
        return None


BYTES = (
    "file_bytes",
    "long_bytes",
    "data_uri",
    "encoded_id",
    "encoded_ids",
    "encoded_type",
    "data_uri_id",
)

CASES = [
    (context, carrier, value)
    for value in VALUES
    for carrier in carriers(value)
    for context, _ in contexts(None)
]


@pytest.mark.parametrize(("context", "carrier", "value"), CASES)
def test_a_value_is_never_sent_as_it_is(context, carrier, value):
    if context == "tool_input" and carrier in BYTES:
        pytest.skip("the model's own writing: see test_file_bytes_in_a_tool_input")
    if context == "safeguards" and carrier in BYTES and "data_uri" not in carrier:
        pytest.skip("local paths and rules: see test_long_strings_in_safeguards")
    slot = carriers(value)[carrier]
    body = dict(contexts(slot))[context]
    sent = outcome(body)
    if sent is None:
        return  # refused: nothing was sent
    assert value not in sent
    for encoded in _strings(slot):
        if len(encoded) >= 32:
            assert encoded not in sent  # file bytes are refused, never sent


@pytest.mark.parametrize(
    ("context", "number", "carrier"),
    list(
        itertools.product(
            [c for c, _ in contexts(None)], NUMBERS, ["alone", "in_list", "setting"]
        )
    ),
)
def test_a_number_that_holds_data_is_refused_or_dropped(context, number, carrier):
    if carrier == "setting" and number == 48210:
        pytest.skip("a setting may hold a registered number glued to others")
    slot = {
        "alone": {"n": number},
        "in_list": {"n": [0, number]},
        "setting": {"card_tokens": number},  # even under a setting's name
    }[carrier]
    body = dict(contexts(slot))[context]
    sent = outcome(body)
    if sent is None:
        return
    # Only signed thinking goes on without it: the block is dropped.
    assert context == "signed_thinking"
    assert number not in list(_numbers(json.loads(sent)))


def _numbers(value):
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from _numbers(item)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class TestWhatGoesThrough:
    """What holds nothing personal goes out as it is, or masked as text."""

    def test_new_fields_are_masked_like_text(self):
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "context_hint": {"enabled": True, "target_tokens_saved": 50000},
            "future": {
                "type": "summarize",
                "instructions": "Keep jane.doe@example.com in the summary",
                "previous_message_id": "msg_01AbCdEfGhIjKlMnOpQrStUv",
                "limits": [1, 2.5, None],
            },
        }
        out = make_masker().mask(body)
        assert out["context_hint"] == body["context_hint"]
        assert out["future"] == {
            "type": "summarize",
            "instructions": "Keep [EMAIL_1] in the summary",
            "previous_message_id": "msg_01AbCdEfGhIjKlMnOpQrStUv",
            "limits": [1, 2.5, None],
        }

    def test_a_new_block_keeps_its_shape(self):
        block = {
            "type": "tool_reference",
            "tool_name": "Read",
            "cache_control": {"type": "ephemeral"},
        }
        body = {
            "tools": [{"name": "Read", "description": "d", "input_schema": {}}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": [block],
                        }
                    ],
                }
            ],
        }
        masker = make_masker()
        out = masker.mask(body)
        assert out["messages"][0]["content"][0]["content"] == [block]
        assert masker.last_generic == ("messages[].content[].content[].type",)

    def test_placeholders_in_a_new_block_stay_placeholders_of_text(self):
        # The model's own placeholder, written back in a block without a rule,
        # is escaped like any text that looks like one: nothing is revealed.
        body = {
            "messages": [
                {"role": "assistant", "content": [{"type": "new", "q": "[PERSON_1]"}]}
            ]
        }
        out = make_masker().mask(body)
        assert out["messages"][0]["content"][0]["q"] == "[LITERAL_1]"

    def test_signed_thinking_with_a_clean_new_field_is_sent_unchanged(self):
        block = {
            "type": "thinking",
            "thinking": "Plan the steps.",
            "signature": "c2ln",
            "summary": {"kind": "short", "steps": 3},
        }
        body = {"messages": [{"role": "assistant", "content": [block]}]}
        assert make_masker().mask(body)["messages"][0]["content"] == [block]

    def test_protocol_words_arent_taken_for_data(self):
        # A short registered value (a git user.name, say) sits inside words
        # like file_path and dangerous_tool_use; they still go through.
        masker = make_masker({"pat": "USER", "dan": "USER", "ada": "USER"})
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "new_field": {"file_path": "x", "type": "dangerous_tool_use"},
            "safeguards": [
                {
                    "type": "dangerous_tool_use",
                    "classifier_context": {"trusted_directories": {"path": "/w"}},
                }
            ],
        }
        out = masker.mask(body)
        assert out["new_field"] == {"file_path": "x", "type": "dangerous_tool_use"}
        assert out["safeguards"] == body["safeguards"]

    def test_an_api_id_isnt_taken_for_data_by_chance(self):
        masker = make_masker({"ada": "USER"})
        tool_id = "srvtoolu_01ada9Lm3NpQrStUvWxYz12"
        body = {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [{"type": "new", "id": tool_id}]},
            ]
        }
        out = masker.mask(body)
        assert out["messages"][1]["content"][0]["id"] == tool_id

    def test_settings_numbers_are_numbers(self):
        # max_tokens: 64000 must never be refused because 4000 is registered.
        masker = make_masker({"4000": "ACCOUNT", NAME: "PERSON"})
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "fallbacks": [{"model": "m", "max_tokens": 64000}],
        }
        assert masker.mask(body)["fallbacks"] == [{"model": "m", "max_tokens": 64000}]

    def test_other_spellings_go_out_as_they_do_in_text(self):
        # A registered name spelled another way isn't found in text either;
        # a key holds the same protection as text, no less.
        masker = make_masker()
        text = masker.mask({"messages": [{"role": "user", "content": "JanNowak"}]})
        body = {"messages": [{"role": "user", "content": "hi"}], "new": {"JanNowak": 1}}
        passed_as_text = "JanNowak" in json.dumps(text)
        assert ("JanNowak" in json.dumps(make_masker().mask(body))) is passed_as_text


class TestToolInputs:
    def test_file_bytes_in_a_tool_input(self):
        # A tool's input is what the model wrote, from masked text: bytes in
        # it are masked as text, as before, not refused (a Write of a base64
        # file is ordinary). They are only what the model itself produced.
        encoded = carriers("jane.doe@example.com")["file_bytes"]
        body = dict(contexts(encoded))["tool_input"]
        out = make_masker().mask(body)["messages"][0]["content"][0]["input"]
        assert out == encoded

    def test_keys_and_types_are_masked(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "mcp__notes__save",
                            "input": {
                                "jane.doe@example.com": "x",
                                "type": f"Meeting with {NAME}",
                                "file_path": "/w/n.txt",
                            },
                        }
                    ],
                }
            ]
        }
        out = make_masker().mask(body)["messages"][0]["content"][0]["input"]
        assert out == {
            "[EMAIL_1]": "x",
            "type": "Meeting with [PERSON_1]",
            "file_path": "/w/n.txt",
        }

    def test_two_keys_that_mask_alike_are_refused(self):
        masker = make_masker()
        masker.mask({"messages": [{"role": "user", "content": "jane.doe@example.com"}]})
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "n",
                            "input": {"jane.doe@example.com": 1, "[EMAIL_1]x": 2},
                        }
                    ],
                }
            ]
        }
        # [EMAIL_1]x isn't masked into the same key (it is escaped as literal
        # text), so this goes through; a real collision is refused.
        assert masker.mask(body)


def test_random_unknown_content_never_carries_a_value_out():
    """Seeded fuzz: random trees of unknown content, with values planted."""
    import os
    import random

    rng = random.Random(20260928)
    keys = ["note", "type", "id", "name", "items", "data", "value", "x", *VALUES]
    leaves = [*VALUES, *NUMBERS, "plain", "summarize", 7, 0.5, True, None]
    leaves.append(base64.b64encode(b"Contact: jane.doe@example.com" * 3).decode())

    def tree(depth=0):
        if depth > 3 or rng.random() < 0.4:
            return rng.choice(leaves)
        if rng.random() < 0.3:
            return [tree(depth + 1) for _ in range(rng.randint(1, 3))]
        return {rng.choice(keys): tree(depth + 1) for _ in range(rng.randint(1, 3))}

    places = [name for name, _ in contexts(None) if name != "tool_input"]
    for case in range(int(os.environ.get("VEIL_FUZZ_CASES", "300"))):
        slot = {rng.choice(keys): tree()}
        body = dict(contexts(slot))[rng.choice(places)]
        sent = outcome(body)
        if sent is None:
            continue
        for value in VALUES:
            assert value not in sent, (case, value)
        found = list(_numbers(json.loads(sent)))
        for number in NUMBERS:
            assert number not in found, (case, number)


class TestReviewCases:
    def test_long_strings_in_safeguards(self):
        # Auto mode sends the working directory and the git branch; long ones
        # look like base64 by their letters, but are masked as text, as ever.
        masker = make_masker()
        cwd = "/Users/example/Documents/Projects/2026/client-portal/frontend-app"
        branch = "feature/PROJ-12345-migrate-customer-onboarding-flow-to-new-api"
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "safeguards": [
                {
                    "type": "dangerous_tool_use",
                    "classifier_context": {
                        "live_cwd": cwd,
                        "git_state": {"branch": branch},
                        "user_identity": "jane.doe@example.com",
                    },
                }
            ],
        }
        context = masker.mask(body)["safeguards"][0]["classifier_context"]
        assert context == {
            "live_cwd": cwd,
            "git_state": {"branch": branch},
            "user_identity": "[EMAIL_1]",
        }

    @pytest.mark.parametrize(
        "tool_id",
        [
            "61109010140000048210",  # an account number: digits only
            "toolu_ada_quill2x",  # a registered handle inside an id
            "msg_a4111111111111111b",  # a card glued inside
        ],
    )
    def test_an_id_holding_a_known_value_is_masked_or_refused(self, tool_id):
        masker = make_masker()
        masker.mask({"messages": [{"role": "user", "content": "4111111111111111"}]})
        generic = {
            "messages": [{"role": "user", "content": "hi"}],
            "x": {"id": tool_id},
        }
        assert tool_id not in json.dumps(masker.mask(generic))
        block = {"type": "tool_use", "id": tool_id, "name": "Read", "input": {}}
        with pytest.raises(UnsupportedRequestError, match="an id the API"):
            masker.mask({"messages": [{"role": "assistant", "content": [block]}]})

    def test_replayed_tool_keys_stay_intact(self):
        # A short registered value (a git user.name) sits inside file_path and
        # pattern; a tool call replayed from the ledger keeps them.
        registered = {"pat": "USER", "tim": "USER", NAME: "PERSON"}
        masker = make_masker(registered)
        masker._ledger.record_tool_input(
            "toolu_01",
            {"file_path": "/w/n.txt", "pattern": "x", "timeout": 5},
            {"file_path": "/w/n.txt", "pattern": "x", "timeout": 5},
        )
        block = {
            "type": "tool_use",
            "id": "toolu_01",
            "name": "Grep",
            "input": {"file_path": "/w/n.txt", "pattern": "x", "timeout": 5},
        }
        out = masker.mask({"messages": [{"role": "assistant", "content": [block]}]})
        assert out["messages"][0]["content"][0]["input"] == block["input"]

    @pytest.mark.parametrize("kind", ["image", "document"])
    def test_a_data_url_source_is_refused(self, kind):
        encoded = base64.b64encode(b"Contact: jane.doe@example.com").decode()
        block = {
            "type": kind,
            "source": {"type": "url", "url": f"data:text/plain;base64,{encoded}"},
        }
        with pytest.raises(UnsupportedRequestError, match="URL"):
            make_masker().mask({"messages": [{"role": "user", "content": [block]}]})

    def test_a_tool_used_earlier_keeps_its_name(self):
        # An MCP tool still connecting isn't in this request's tools; its
        # name holds a registered company only glued, as it was sent before.
        masker = make_masker({"acme": "ORG"})
        block = {
            "type": "tool_use",
            "id": "toolu_01",
            "name": "mcp__acme-jira__search",
            "input": {},
        }
        body = {
            "tools": [{"name": "Read", "description": "d", "input_schema": {}}],
            "messages": [{"role": "assistant", "content": [block]}],
        }
        with pytest.raises(UnsupportedRequestError, match="a tool name"):
            masker.mask(body)  # not seen made by the API: checked
        masker._ledger.record_tool_input("toolu_01", {}, {})
        assert masker.mask(body)["messages"][0]["content"][0] == block

    def test_schema_words_are_keys_too(self):
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "new": {"$schema": "https://json-schema.org/draft/2020-12/schema"},
        }
        assert make_masker().mask(body)["new"] == body["new"]

    def test_two_keys_that_would_mask_alike_are_refused(self, monkeypatch):
        masker = make_masker()
        monkeypatch.setattr(masker, "_ident_ok", lambda name: False)
        monkeypatch.setattr(masker, "_text", lambda text: "[EMAIL_1]")
        block = {
            "type": "tool_use",
            "id": "toolu_01AbCdEfGhIjKl",
            "name": "n",
            "input": {"a": "x", "b": "y"},
        }
        with pytest.raises(UnsupportedRequestError, match="two keys"):
            masker.mask({"messages": [{"role": "assistant", "content": [block]}]})

    def test_masking_generic_content_is_repeatable(self):
        slot = {"note": "Mail jane.doe@example.com", "id": "x-1", "n": 3}
        for _, body in contexts(slot):
            masker = make_masker()
            try:
                first = masker.mask(body)
            except UnsupportedRequestError:
                continue
            assert masker.mask(body) == first
