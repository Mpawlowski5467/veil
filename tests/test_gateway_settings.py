"""The request's settings: known fields keep their shape, text in them is masked.

Settings such as ``thinking``, ``output_config`` and ``tools`` used to be
passed on unchecked, so new text in them left unmasked. Each known field now
has a rule; anything else in them is masked generically.
"""

import json

import pytest

from veil import LiteralPlaceholderDetector, MemoryVault, RegexDetector, Shield
from veil.gateway import MemoryLedger, RequestMasker, UnsupportedRequestError
from veil.gateway.response import restore_message

EMAIL = "jane.doe@example.com"
NAME = "Jan Nowak"
HANDLE = "ada_quill"
USER = {"role": "user", "content": "hi"}


def make_masker(extra=None):
    registered = {NAME: "PERSON", HANDLE: "USER", **(extra or {})}
    types = {*RegexDetector().entity_types, *registered.values()}
    shield = Shield(
        detectors=[LiteralPlaceholderDetector(types), RegexDetector()],
        vault=MemoryVault(),
        redact_warnings=True,
    )
    for value, kind in registered.items():
        shield.add_entity(value, kind)
    masker = RequestMasker(shield, MemoryLedger(), note=None, registered=registered)
    return masker, shield


def mask(body, extra=None):
    return make_masker(extra)[0].mask({"messages": [USER], **body})


def structured(schema):
    return {
        "tools": [
            {
                "name": "StructuredOutput",
                "description": "Return your final response in this format.",
                "input_schema": schema,
            }
        ],
    }


class TestWhatLeftUnmasked:
    """Each case the brief found going out unmasked is masked now."""

    def test_a_new_thinking_type_with_text(self):
        out = mask({"thinking": {"type": "guided", "text": f"Focus on {NAME}"}})
        assert out["thinking"] == {"type": "guided", "text": "Focus on [PERSON_1]"}

    def test_compaction_instructions(self):
        edit = {"type": "compact_20260112", "instructions": f"Keep {EMAIL}"}
        out = mask({"context_management": {"edits": [edit]}})
        assert out["context_management"]["edits"][0]["instructions"] == "Keep [EMAIL_1]"

    def test_a_new_metadata_key(self):
        out = mask({"metadata": {"user_id": "u", "user_email": EMAIL}})
        assert out["metadata"] == {"user_id": "u", "user_email": "[EMAIL_1]"}

    def test_a_format_schema_description(self):
        schema = {"type": "object", "description": f"Answer for {NAME}"}
        out = mask(
            {"output_config": {"format": {"type": "json_schema", "schema": schema}}}
        )
        assert out["output_config"]["format"]["schema"] == {
            "type": "object",
            "description": "Answer for [PERSON_1]",
        }

    def test_the_json_schema_of_claude_p(self):
        schema = {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": f"e.g. {EMAIL}"},
                "who": {"enum": [NAME, "someone else"]},
            },
            "required": ["email"],
        }
        out = mask(structured(schema))
        assert out["tools"][0]["input_schema"] == {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "e.g. [EMAIL_1]"},
                "who": {"enum": ["[PERSON_1]", "someone else"]},
            },
            "required": ["email"],
        }

    def test_a_cache_breakpoint_on_signed_thinking(self):
        # Not part of what is signed, so masked like any other.
        thinking = {
            "type": "thinking",
            "thinking": "t",
            "signature": "s",
            "cache_control": {"type": "ephemeral", "note": EMAIL},
        }
        body = {
            "messages": [
                USER,
                {
                    "role": "assistant",
                    "content": [thinking, {"type": "text", "text": "ok"}],
                },
                USER,
            ]
        }
        out = make_masker()[0].mask(body)["messages"][1]["content"][0]
        assert out == {
            **thinking,
            "cache_control": {"type": "ephemeral", "note": "[EMAIL_1]"},
        }

    def test_the_user_id_stays_json(self):
        user_id = json.dumps({"device_id": "d" * 64, "team": EMAIL})
        out = mask({"metadata": {"user_id": user_id}})
        assert json.loads(out["metadata"]["user_id"]) == {
            "device_id": "d" * 64,
            "team": "[EMAIL_1]",
        }


class TestWhatGoesAsItIs:
    def test_claude_codes_settings_are_unchanged(self):
        body = {
            "model": "claude-opus-5-5",
            "max_tokens": 64000,
            "temperature": 1,
            "stream": True,
            "service_tier": "auto",
            "thinking": {
                "type": "enabled",
                "budget_tokens": 31999,
                "display": "omitted",
            },
            "context_management": {
                "edits": [
                    {"type": "clear_thinking_20251015", "keep": "all"},
                    {
                        "type": "clear_tool_uses_20250919",
                        "trigger": {"type": "input_tokens", "value": 100000},
                        "keep": {"type": "tool_uses", "value": 3},
                        "exclude_tools": ["Read"],
                        "clear_tool_inputs": True,
                    },
                ]
            },
            "output_config": {
                "effort": "xhigh",
                "task_budget": {"type": "tokens", "total": 1000, "remaining": None},
                "timing": {"type": "now", "now": "2026-09-27T12:45:00+02:00"},
            },
            "tools": [{"name": "Read", "description": "d", "input_schema": {}}],
            "tool_choice": {"type": "tool", "name": "Read"},
            "cache_control": {
                "type": "ephemeral",
                "ttl": "1h",
                "evict_on_complete": True,
            },
        }
        # A short registered value inside these words changes nothing.
        out = mask(body, {"eff": "USER", "tim": "USER"})
        assert {key: out[key] for key in body} == body

    def test_a_title_generation_format(self):
        output_config = {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                    "additionalProperties": False,
                },
            }
        }
        assert mask({"output_config": output_config})["output_config"] == output_config

    def test_tool_definitions_go_as_they_are_but_structured_output(self):
        # By design: they describe the tools, not the user.
        tool = {
            "name": "mcp__crm__lookup",
            "description": f"Looks up {EMAIL}",
            "input_schema": {"properties": {"q": {"description": f"like {NAME}"}}},
        }
        assert mask({"tools": [tool]})["tools"] == [tool]

    def test_server_tool_locations_are_masked(self):
        tool = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 8,
            "user_location": {"type": "approximate", "city": NAME, "country": "PL"},
        }
        out = mask({"tools": [tool]})["tools"][0]
        assert out["user_location"] == {
            "type": "approximate",
            "city": "[PERSON_1]",
            "country": "PL",
        }

    @pytest.mark.parametrize(
        "pattern",
        [
            "^[a-z]+$",
            "^\\d{3}-\\d{3}-\\d{4}$",
            "^[^@\\s]+@[^@\\s]+\\.[a-z]{2,}$",
            "^[A-Za-z0-9_=-]{1,4096}$",
            "^[\\w.+-]+@[\\w-]+\\.[\\w.]+$",
            "^[a-z]+@[a-z]+\\.com$",
            "^[A-Z]{2}\\d+$",
            "^(true|false)$",
            "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
            "^#[0-9a-fA-F]{6}$",
            # A domain alone is not an address.
            "^[\\w.+-]+@acme\\.com$",
            "^[a-z0-9._%+-]+@company\\.org$",
            ".+@example\\.com$",
            ".{1,64}@example\\.com$",
        ],
    )
    def test_a_pattern_without_data_is_kept(self, pattern):
        schema = {"type": "string", "pattern": pattern}
        out = mask(structured(schema), {"4096": "ACCOUNT"})
        assert out["tools"][0]["input_schema"] == schema

    @pytest.mark.parametrize(
        ("schema", "registered"),
        [
            # Claude Code's memory recall and prompt hooks.
            ({"properties": {"selected_memories": {"type": "array"}}}, "ted"),
            ({"properties": {"selected_memories": {"type": "array"}}}, "mem"),
            ({"properties": {"ok": {}, "reason": {}, "impossible": {}}}, "pos"),
            (
                {"properties": {"patch": {}, "dispatch": {}}, "required": ["patch"]},
                "pat",
            ),
            ({"properties": {"guidance": {}, "compatible": {}}}, "dan"),
            ({"type": "string", "pattern": "^[a-z]+_selected$"}, "ted"),
        ],
    )
    def test_a_short_value_inside_a_longer_name_is_chance(self, schema, registered):
        format_ = {"format": {"type": "json_schema", "schema": schema}}
        out = mask({"output_config": format_}, {registered: "USER"})
        assert out["output_config"] == format_
        tools = mask(structured(schema), {registered: "USER"})["tools"]
        assert tools[0]["input_schema"] == schema

    def test_the_user_ids_ids_are_kept(self):
        user_id = json.dumps(
            {
                "device_id": "3f9ada0e" + "0" * 56,
                "account_uuid": "",
                "session_id": "1b2c3d4e-0000-4000-8000-0000000ada00",
            },
            separators=(",", ":"),
        )
        out = mask({"metadata": {"user_id": user_id}}, {"ada": "USER"})
        assert out["metadata"]["user_id"] == user_id

    def test_schema_references_are_kept(self):
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {"Person": {"type": "object"}},
            "properties": {
                "who": {"$ref": "#/$defs/Person"},
                "at": {"format": "email"},
            },
            "additionalProperties": False,
            "minItems": 1,
        }
        assert mask(structured(schema))["tools"][0]["input_schema"] == schema


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "body",
        [
            {"model": f"claude {EMAIL}"},
            {"model": NAME},
            {"service_tier": NAME},
            {"thinking": {"type": EMAIL}},
            {"thinking": {"type": "enabled", "budget_tokens": "lots"}},
            {"stream": "yes"},
            {"temperature": "hot"},
            {"max_tokens": 1.5},
            {"tool_choice": {"type": "tool", "name": EMAIL}},
            {"tool_choice": {"type": "tool", "name": f"x_{HANDLE}"}},
            {"cache_control": {"type": "ephemeral", "ttl": "forever"}},
            {
                "context_management": {
                    "edits": [{"type": "x", "exclude_tools": [EMAIL]}]
                }
            },
            {"output_config": {"effort": NAME}},
            # A tool the request doesn't define, named with a value in it.
            {"tool_choice": {"type": "tool", "name": "Read-4821"}},
            {
                "context_management": {
                    "edits": [
                        {
                            "type": "clear_tool_uses_20250919",
                            "exclude_tools": ["xada_quill"],
                        }
                    ]
                }
            },
        ],
    )
    def test_a_setting_holding_data_or_the_wrong_kind(self, body):
        with pytest.raises(UnsupportedRequestError) as info:
            mask(body, {"4821": "ACCOUNT"})
        assert EMAIL not in str(info.value)
        assert "4821" not in str(info.value)
        assert NAME not in str(info.value)

    @pytest.mark.parametrize(
        "schema",
        [
            {"pattern": "^jan\\.n@example\\.com$"},
            {"pattern": "^Jan\\ Nowak$"},
            {"pattern": "^Jan\\sNowak$"},
            {"pattern": "^jan\\x2en@example\\.com$"},
            {"properties": {EMAIL: {"type": "string"}}},
            {"properties": {HANDLE: {"type": "string"}}},
            {"required": [HANDLE]},
            {"patternProperties": {"^jan\\.n@example\\.com$": {}}},
            # A value spelled the other ways a pattern can spell it.
            *(
                {"pattern": pattern}
                for pattern in (
                    "^jane[.]doe@example[.]com$",
                    "^jane\\.doe@example\\.(com|org)$",
                    "^(jane\\.doe|jd)@example\\.com$",
                    "^jane\\.doe[@]example\\.com$",
                    "^jane\\.doe@(example)\\.com$",
                    "^Jan[ ]Nowak$",
                    "^Jan.Nowak$",
                    "^Jan +Nowak$",
                    "^Jan[\\s]Nowak$",
                    "^(Jan)\\s(Nowak)$",
                    "^(?:Jan) (?:Nowak)$",
                    "^[Jj]an [Nn]owak$",
                    "^4111[ -]?1111[ -]?1111[ -]?1111$",
                    "^4111 ?1111 ?1111 ?1111$",
                    "^[(]555[)] 555-0100$",
                    "^555[-. ]555[-. ]0100$",
                    "^48[2]1$",
                    "^ada[_]quill$",
                    "^ada_qui(?:ll)$",
                )
            ),
            {"$ref": f"#/{EMAIL}"},
            {"$id": "https://example.com/s/jane.doe%40example.com"},
            {"$ref": "#/$defs/Jan%20Nowak"},
            {"type": "person"},
            {"enum": [4111111111111111]},
            {
                "const": "data:text/plain;base64,"
                "SmFuZSBEb2UgamFuZS5kb2VAZXhhbXBsZS5jb20="
            },
        ],
    )
    def test_a_schema_part_that_cant_be_masked(self, schema):
        with pytest.raises(UnsupportedRequestError):
            mask(structured(schema), {"4821": "ACCOUNT"})

    @pytest.mark.parametrize("kind", [{"a": 1}, [{"a": 1}], ["string", 1]])
    def test_a_type_that_isnt_a_name_is_refused_by_name(self, kind):
        with pytest.raises(UnsupportedRequestError, match=r"input_schema\.type"):
            mask(structured({"type": kind}))


def test_a_masked_enum_comes_back_as_the_users_value():
    # The model writes the placeholder the schema offers; the reply restores
    # it, so Claude Code's check against the real schema passes.
    masker, shield = make_masker()
    schema = {"properties": {"who": {"enum": [NAME]}}}
    out = masker.mask({"messages": [USER], **structured(schema)})
    placeholder = out["tools"][0]["input_schema"]["properties"]["who"]["enum"][0]
    reply = {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "StructuredOutput",
                "input": {"who": placeholder},
            }
        ]
    }
    restored = restore_message(shield, MemoryLedger(), reply)
    assert restored["content"][0]["input"] == {"who": NAME}
