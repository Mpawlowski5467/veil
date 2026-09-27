"""The hooks that go with the gateway: real values in outbound calls, routing."""

import io
import json
import subprocess
import sys

import pytest

from veil import LiteralPlaceholderDetector, RegexDetector, Shield, SQLiteVault
from veil.cli import claude_settings, hook_command
from veil.gateway import hooks

EMAIL = "jane.doe@example.com"
NAME = "Jan Nowak"


@pytest.fixture
def vault_path(tmp_path):
    path = tmp_path / "vault.db"
    shield = Shield(
        detectors=[LiteralPlaceholderDetector({"EMAIL"}), RegexDetector()],
        vault=SQLiteVault(path, session="s1"),
    )
    shield.add_entity(NAME, "PERSON")
    shield.mask(f"{NAME} {EMAIL} and the literal [EMAIL_9]")
    shield.vault.close()
    return path


def call(tool, tool_input, session="s1"):
    return {"session_id": session, "tool_name": tool, "tool_input": tool_input}


class TestPreToolUse:
    def test_a_command_with_a_real_value_asks(self, vault_path):
        answer = hooks.pre_tool_use(
            call("Bash", {"command": f"git log --author='{NAME}'"}), vault_path
        )
        output = answer["hookSpecificOutput"]
        assert output["hookEventName"] == "PreToolUse"
        assert output["permissionDecision"] == "ask"
        assert "a value of personal data" in output["permissionDecisionReason"]
        assert NAME not in json.dumps(answer)

    def test_a_command_without_one_is_left_alone(self, vault_path):
        assert (
            hooks.pre_tool_use(call("Bash", {"command": "ls -la"}), vault_path) is None
        )

    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            ("WebFetch", {"url": f"https://example.com/?q={EMAIL}", "prompt": "x"}),
            ("WebSearch", {"query": f"who is {NAME}"}),
            ("mcp__crm__lookup", {"contact": {"email": EMAIL}}),
            ("mcp__crm__lookup", {EMAIL: "as a key"}),
        ],
    )
    def test_web_and_mcp_calls_with_a_real_value_are_refused(
        self, vault_path, tool, tool_input
    ):
        answer = hooks.pre_tool_use(call(tool, tool_input), vault_path)
        output = answer["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "refused" in output["permissionDecisionReason"]
        assert EMAIL not in json.dumps(answer)
        assert NAME not in json.dumps(answer)

    def test_the_count_is_given(self, vault_path):
        answer = hooks.pre_tool_use(
            call("WebSearch", {"query": f"{NAME} {EMAIL}"}), vault_path
        )
        assert "2 values" in answer["hookSpecificOutput"]["permissionDecisionReason"]

    def test_an_allowed_mcp_tool_may_receive_real_values(self, vault_path):
        payload = call("mcp__crm__lookup", {"email": EMAIL})
        assert hooks.pre_tool_use(payload, vault_path, ["mcp__crm__lookup"]) is None

    def test_literal_text_isnt_personal_data(self, vault_path):
        payload = call("WebFetch", {"url": "https://example.com/[EMAIL_9]"})
        assert hooks.pre_tool_use(payload, vault_path) is None

    def test_another_conversation_has_its_own_values(self, vault_path):
        payload = call("WebSearch", {"query": EMAIL}, session="s2")
        assert hooks.pre_tool_use(payload, vault_path) is None

    def test_a_call_it_cant_read_is_refused(self, vault_path):
        answer = hooks.pre_tool_use({"tool_input": {}}, vault_path)
        assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_count_values(self):
        assert (
            hooks.count_values(
                {"a": ["x Jan y", {"b": "Ada"}]}, ["Jan", "Ada", "Bo", "Jan"]
            )
            == 2
        )


class TestUserPromptSubmit:
    def test_routed_through_the_gateway(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:5555")
        assert hooks.user_prompt_submit({}, "http://127.0.0.1:5555") is None

    @pytest.mark.parametrize(
        "env",
        [
            {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"},
            {"ANTHROPIC_BASE_URL": None},
            {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:5555",
                "CLAUDE_CODE_USE_BEDROCK": "1",
            },
        ],
    )
    def test_anything_else_holds_the_prompt_back(self, monkeypatch, env):
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        answer = hooks.user_prompt_submit({"prompt": EMAIL}, "http://127.0.0.1:5555")
        assert answer["decision"] == "block"
        assert answer["hookSpecificOutput"] == {
            "hookEventName": "UserPromptSubmit",
            "suppressOriginalPrompt": True,
        }
        assert EMAIL not in json.dumps(answer)


class TestRun:
    def run(self, event, payload, vault_path, **kwargs):
        out = io.StringIO()
        text = payload if isinstance(payload, str) else json.dumps(payload)
        code = hooks.run(
            event, stdin=io.StringIO(text), stdout=out, vault_path=vault_path, **kwargs
        )
        assert code == 0
        return json.loads(out.getvalue()) if out.getvalue() else None

    def test_answers_and_silence(self, vault_path):
        answer = self.run(
            "pre-tool-use", call("Bash", {"command": f"echo {EMAIL}"}), vault_path
        )
        assert answer["hookSpecificOutput"]["permissionDecision"] == "ask"
        assert (
            self.run("pre-tool-use", call("Bash", {"command": "ls"}), vault_path)
            is None
        )

    def test_garbage_in_refuses_the_call(self, vault_path):
        answer = self.run("pre-tool-use", "{not json", vault_path)
        assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_garbage_in_holds_the_prompt_back(self, vault_path):
        answer = self.run(
            "user-prompt-submit", "[1]", vault_path, expected_url="http://x"
        )
        assert answer["decision"] == "block"

    def test_a_broken_vault_refuses_the_call(self, tmp_path):
        folder = tmp_path / "not-a-file.db"
        folder.mkdir()
        answer = self.run("pre-tool-use", call("Bash", {"command": "ls"}), folder)
        assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestSettings:
    def test_hook_settings(self, tmp_path):
        command = hook_command(tmp_path / "data dir")
        settings = hooks.hook_settings(command, "http://127.0.0.1:5555")
        (pre,) = settings["PreToolUse"]
        assert pre["matcher"] == hooks.MATCHER
        assert pre["hooks"][0]["command"].endswith("hook pre-tool-use")
        assert "'" + str(tmp_path / "data dir") + "'" in pre["hooks"][0]["command"]
        (prompt,) = settings["UserPromptSubmit"]
        assert prompt["hooks"][0]["command"].endswith(
            "hook user-prompt-submit --expect-url http://127.0.0.1:5555"
        )

    def test_the_matcher_covers_the_outbound_tools(self):
        import re

        for tool in ("Bash", "WebFetch", "WebSearch", "mcp__crm__lookup"):
            assert re.fullmatch(hooks.MATCHER, tool)
        for tool in ("Read", "Write", "Edit", "Grep", "Agent"):
            assert not re.fullmatch(hooks.MATCHER, tool)

    def test_claude_settings_run_the_hooks(self, tmp_path):
        class FakeGateway:
            url = "http://127.0.0.1:5555"
            secret = "s"

        settings = claude_settings(FakeGateway(), data_dir=tmp_path)
        assert set(settings["hooks"]) == {"PreToolUse", "UserPromptSubmit"}
        assert "hooks" not in claude_settings(FakeGateway())


def test_the_hook_command_runs_isolated(vault_path, tmp_path):
    data_dir = vault_path.parent
    command = hook_command(data_dir)
    assert command[:3] == [sys.executable, "-I", "-m"]
    completed = subprocess.run(
        [*command, "pre-tool-use"],
        input=json.dumps(call("WebFetch", {"url": f"https://example.com/{EMAIL}"})),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    answer = json.loads(completed.stdout)
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert completed.stderr == ""
