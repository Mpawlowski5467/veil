"""The tested Claude Code version and the messages built around it."""

import re
from pathlib import Path

import pytest

from veil import __version__
from veil.gateway import compat
from veil.gateway.compat import TESTED_CLAUDE_CODE
from veil.gateway.config import APP


class TestVersions:
    @pytest.mark.parametrize(
        ("agent", "version"),
        [
            ("claude-cli/2.1.283 (external, cli)", "2.1.283"),
            ("claude-cli/2.1.283 (external, cli, client-app/app 1.0)", "2.1.283"),
            ("claude-cli/12.0.1000", "12.0.1000"),
            ("claude-cli/2.1.283x (external, cli)", None),
            ("claude-cli/2.1", None),
            ("my-claude-cli/2.1.283", None),
            ("claude-cli/٢.1.283", None),
            ("Anthropic/JS 0.60.0", None),
            ("", None),
            (None, None),
        ],
    )
    def test_the_version_in_a_user_agent(self, agent, version):
        assert compat.from_user_agent(agent) == version

    @pytest.mark.parametrize(
        ("output", "version"),
        [
            ("2.1.283 (Claude Code)\n", "2.1.283"),
            ("  2.1.290 (Claude Code)", "2.1.290"),
            ("2.2.0-beta.1 (Claude Code)", "2.2.0"),
            ("Claude Code", None),
            ("2.1", None),
            ("", None),
        ],
    )
    def test_the_version_claude_prints(self, output, version):
        assert compat.from_cli_output(output) == version

    def test_versions_compare_as_numbers(self):
        assert compat.compare("2.1.1000", "2.1.283") == 1
        assert compat.compare("2.1.99", "2.1.283") == -1
        assert compat.compare("2.1.283", "2.1.283") == 0
        assert compat.compare(None) is None

    def test_the_tested_version_is_written_once_in_its_module(self):
        # The census script rewrites this line; it must stay findable.
        source = Path(compat.__file__).read_text(encoding="utf-8")
        found = re.findall(
            r'^TESTED_CLAUDE_CODE = "([0-9]+\.[0-9]+\.[0-9]+)"$', source, re.M
        )
        assert found == [TESTED_CLAUDE_CODE]


PROBLEMS = [
    ("messages[4].content[1].type", "unknown block type 'future_block'", 30),
    ("messages[6].output_config.x", "unknown field", 1),
]


class TestMessages:
    @pytest.mark.parametrize(
        ("client", "advice"),
        [
            (
                "9.0.0",
                f"This is Claude Code 9.0.0, and {APP} {__version__} was "
                f"tested with {TESTED_CLAUDE_CODE}: update {APP}.",
            ),
            ("0.0.1", "update Claude Code, or"),
            (TESTED_CLAUDE_CODE, "report this if it is up to date"),
            (None, f"was tested with Claude Code {TESTED_CLAUDE_CODE}: update {APP}."),
        ],
    )
    def test_the_advice_depends_on_the_version(self, client, advice):
        message = compat.refusal_message(PROBLEMS, client)
        assert advice in message
        assert "update" in compat.version_advice(client)

    def test_a_refusal_is_one_line_with_the_advice_first(self):
        message = compat.refusal_message(PROBLEMS, "9.0.0")
        assert message.startswith(
            f"{APP}: can't mask this request, so nothing was sent."
        )
        assert "\n" not in message
        assert message.index("update") < message.index("Not handled")
        assert message.endswith(
            "Not handled: messages[4].content[1].type (unknown block type "
            "'future_block') 30 times; messages[6].output_config.x (unknown field)"
        )

    def test_rewind_is_suggested_only_for_the_conversation(self):
        assert "/rewind to before the prompt" in compat.refusal_message(PROBLEMS, None)
        everywhere = [*PROBLEMS, ("tools[0].x", "unknown field", 1)]
        message = compat.refusal_message(everywhere, None)
        assert "/rewind won't help" in message
        assert "/rewind to before" not in message

    def test_a_long_list_is_cut(self):
        many = [(f"field_{i}", "unknown field", 1) for i in range(200)]
        message = compat.refusal_message(many, None)
        assert f"field_{compat.MAX_LISTED - 1} (unknown field)" in message
        assert f"field_{compat.MAX_LISTED} " not in message
        assert message.endswith(f"and {200 - compat.MAX_LISTED} more")

    def test_a_failure_says_whether_anything_was_sent(self):
        before = compat.failure_message("failed while masking", None)
        assert before.startswith(f"{APP}: failed while masking (a bug), so nothing")
        after = compat.failure_message("failed", None, sent=True)
        assert "the masked request reached the API" in after

    def test_the_summary_lists_every_problem(self):
        lines = compat.summary(2, PROBLEMS, None)
        assert lines[0] == (
            f"{APP}: 2 requests couldn't be masked, so they weren't sent. Not handled:"
        )
        assert lines[1] == (
            "  messages[4].content[1].type (unknown block type 'future_block'), "
            "30 times"
        )
        assert lines[-1].startswith(f"{APP}: {APP} {__version__} was tested")
