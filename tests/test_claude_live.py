"""Live checks of the Claude Code behavior veil's hooks rely on (opt-in).

Run with ``VEIL_LIVE_CLAUDE=1 uv run pytest -m live``. Each scenario is one
headless ``claude -p`` run on the cheapest model, shared by the tests that
check it, and the whole module costs a few cents. The probe hook answers from
fixed rules, so these tests check Claude Code, not veil: run them again after
a Claude Code update, and a failure means a design assumption changed.

Checked against Claude Code 2.1.283.
"""

import pytest

from live import harness as h

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not h.LIVE or h.claude_path() is None,
        reason="live Claude Code tests are opt-in: set VEIL_LIVE_CLAUDE=1",
    ),
]

MASK = {h.EMAIL: "[EMAIL_1]"}
RESTORE = {"[EMAIL_1]": h.EMAIL}


def batch_responses(run, tool):
    """The text each call of ``tool`` gave the model, as PostToolBatch saw it."""
    return [str(call.get("tool_response")) for call in run.batch_calls(tool)]


# --- Masking a result, restoring an input, and the Edit gap -------------------

ROUNDTRIP_PROMPT = """Do these steps in order, one tool call per step:
1. Use the Read tool to read notes.txt.
2. Use the Bash tool to run: cat notes.txt
3. Use the Write tool to create out.txt containing exactly: Contact: [EMAIL_1]
4. Use the Edit tool on notes.txt to replace the text "Email: [EMAIL_1]" with
   "Email: [EMAIL_1] (verified)". If it fails, do not retry and do not use
   another tool for it.
5. Reply with the single word DONE."""


@pytest.fixture(scope="module")
def roundtrip(tmp_path_factory):
    ws = h.Workspace(tmp_path_factory.mktemp("roundtrip"))
    ws.write("notes.txt", h.NOTES)
    run = ws.run(
        ROUNDTRIP_PROMPT,
        rules=[
            {"event": "PostToolUse", "tool": "Read|Bash", "replace_output": MASK},
            {
                "event": "PreToolUse",
                "tool": "Write|Edit|Bash",
                "replace_input": RESTORE,
            },
            {
                "event": "MessageDisplay",
                "respond": {
                    "hookSpecificOutput": {
                        "hookEventName": "MessageDisplay",
                        "displayContent": "SHOWN-ON-SCREEN",
                    }
                },
            },
        ],
        allowed_tools=["Read", "Bash", "Write", "Edit"],
        max_turns=12,
    )
    assert run.returncode == 0, run.stderr
    return ws, run


def test_replaced_results_are_what_the_model_sees(roundtrip):
    _, run = roundtrip
    results = run.tool_results()
    assert any("[EMAIL_1]" in text for text in results)
    assert not any(h.EMAIL in text for text in results)
    # PostToolBatch sees exactly the replaced text, so a tripwire there works.
    for tool in ("Read", "Bash"):
        seen = batch_responses(run, tool)
        assert seen, f"the model didn't call {tool}"
        assert all("[EMAIL_1]" in text and h.EMAIL not in text for text in seen)


def test_no_real_value_reaches_the_api(roundtrip):
    _, run = roundtrip
    sent = run.sent_to_api()
    assert "[EMAIL_1]" in sent
    # The restored Write input goes to disk, never back to the model.
    assert h.EMAIL not in sent


def test_the_transcript_keeps_the_replaced_result(roundtrip):
    _, run = roundtrip
    # The transcript's structured copy of each result is the replaced one too.
    # (Real values still reach it through the restored Write input: the hook's
    # own output and the Write tool's result.)
    copies = []
    for record in run.transcript():
        result = record.get("toolUseResult")
        if isinstance(result, dict) and isinstance(result.get("file"), dict):
            copies.append(result["file"].get("content", ""))
        elif isinstance(result, dict) and "stdout" in result:
            copies.append(result["stdout"])
    assert len(copies) >= 2
    assert all("[EMAIL_1]" in text and h.EMAIL not in text for text in copies)


def test_restored_write_input_reaches_the_disk(roundtrip):
    ws, run = roundtrip
    assert run.calls("PreToolUse", "Write"), "the model didn't call Write"
    assert (ws.work / "out.txt").read_text(encoding="utf-8").strip() == (
        f"Contact: {h.EMAIL}"
    )


def test_edit_with_a_placeholder_fails_before_any_hook(roundtrip):
    ws, run = roundtrip
    assert batch_responses(run, "Edit"), "the model didn't call Edit"
    # Edit checks old_string against the file before PreToolUse runs, so no
    # hook ever sees it, and the file is unchanged.
    for record in run.calls("PreToolUse", "Edit"):
        assert "[EMAIL_1]" not in record["payload"]["tool_input"]["old_string"]
    assert any("String to replace not found" in t for t in batch_responses(run, "Edit"))
    assert (ws.work / "notes.txt").read_text(encoding="utf-8") == h.NOTES


def test_display_content_changes_only_the_screen(roundtrip):
    _, run = roundtrip
    assert run.result.get("result") == "SHOWN-ON-SCREEN"
    assert "SHOWN-ON-SCREEN" not in run.transcript_text


# --- A replacement of the wrong shape is dropped (fails open) ----------------


@pytest.fixture(scope="module")
def wrong_shape(tmp_path_factory):
    ws = h.Workspace(tmp_path_factory.mktemp("wrong_shape"))
    ws.write("notes.txt", h.NOTES)
    run = ws.run(
        "Use the Read tool to read notes.txt, then reply with the single word DONE.",
        rules=[
            {
                "event": "PostToolUse",
                "tool": "Read",
                "respond": {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "updatedToolOutput": "a plain string instead of Read's shape",
                    }
                },
            }
        ],
        allowed_tools=["Read"],
        max_turns=4,
    )
    assert run.returncode == 0, run.stderr
    return run


def test_wrong_shape_replacement_leaves_the_original(wrong_shape):
    assert any(h.EMAIL in text for text in wrong_shape.tool_results())
    assert "does not match Read's output shape" in wrong_shape.everything()
    # PostToolBatch sees the original too, so the tripwire catches this case;
    # without one, the real value goes to the API.
    assert any(h.EMAIL in text for text in batch_responses(wrong_shape, "Read"))
    assert h.EMAIL in wrong_shape.sent_to_api()


# --- A held-back prompt never reaches the model ------------------------------


@pytest.fixture(scope="module")
def held_back(tmp_path_factory):
    ws = h.Workspace(tmp_path_factory.mktemp("held_back"))
    run = ws.run(
        f"My email is {h.EMAIL}. Reply with the single word HELLO.",
        rules=[
            {
                "event": "UserPromptSubmit",
                "respond": {
                    "decision": "block",
                    "reason": "HELD-BACK-REASON",
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "suppressOriginalPrompt": True,
                    },
                },
            }
        ],
        max_turns=2,
    )
    assert run.returncode == 0, run.stderr
    return run


def test_blocked_prompt_makes_no_model_call(held_back):
    assert held_back.result.get("num_turns") == 0
    assert not held_back.result.get("total_cost_usd")
    assert "HELD-BACK-REASON" in held_back.result.get("result", "")
    assert h.EMAIL not in held_back.result.get("result", "")
    assert held_back.api_bodies() == []


def test_blocked_prompt_is_still_written_to_the_transcript(held_back):
    # A known copy on disk that veil documents: the raw prompt is queued in
    # the transcript before hooks run, even though the model never sees it.
    assert held_back.transcript_text
    assert h.EMAIL in held_back.transcript_text
