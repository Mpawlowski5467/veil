"""Offline checks for the bounded live diagnostic's safety controls."""

import json

import pytest

from live.haiku_comparison import MODEL, RECORD, RequestBudget, fenced_record_matches
from live.work_file_guard import decision


def test_live_probe_rejects_unmasked_values_and_extra_requests():
    budget = RequestBudget(1, ("probe@example.org",))
    with pytest.raises(RuntimeError, match="unexpected model"):
        budget.check({"model": "other"})
    with pytest.raises(RuntimeError, match="unmasked"):
        budget.check({"model": MODEL, "text": "probe@example.org"})
    assert budget.used == 0
    body = {"model": MODEL, "text": "[EMAIL_1]"}
    budget.check(body)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        budget.check(body)
    assert budget.used == 1
    assert body == {"model": MODEL, "text": "[EMAIL_1]"}


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_live_file_guard_rejects_other_paths_with_target_in_content(tmp_path, tool):
    target = tmp_path / "contact.json"
    allowed = {"tool_name": tool, "tool_input": {"file_path": str(target)}}
    assert decision(allowed, target) == "allow"
    rejected = {
        "tool_name": tool,
        "tool_input": {
            "file_path": str(tmp_path / "memory.json"),
            "content": str(target),
        },
    }
    assert decision(rejected, target) == "deny"
    rejected["tool_input"]["file_path"] = "contact.json"
    assert decision(rejected, target) == "deny"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"tool_name": [], "tool_input": {"file_path": "/invalid"}},
        {"tool_name": "Write", "tool_input": "invalid"},
        {"tool_name": "Write", "tool_input": {"file_path": "/invalid\0path"}},
    ],
)
def test_live_file_guard_denies_malformed_inputs(tmp_path, payload):
    assert decision(payload, tmp_path / "contact.json") == "deny"


def test_markdown_diagnostic_requires_the_complete_correct_record():
    correct = json.dumps({**RECORD, "priority": "urgent"})
    assert fenced_record_matches(f"```json\n{correct}\n```")
    assert not fenced_record_matches(correct)
    assert not fenced_record_matches(f"```json\n{json.dumps(RECORD)}\n```")
    assert not fenced_record_matches(f"prose\n```json\n{correct}\n```")
