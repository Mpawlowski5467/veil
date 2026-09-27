"""Live checks of what Claude Code does with a failing command (opt-in).

Run with ``VEIL_LIVE_CLAUDE=1 uv run pytest -m live``. Four headless
``claude -p`` runs on the cheapest model, about 15 cents in all: a run whose
Bash commands fail and whose PostToolBatch hook stops it, a fork of that
session, a resume of it, and a run whose PreToolUse hook wraps commands.

What veil relies on, or has to work around:

- A Bash command that exits non-zero skips PostToolUse, so veil's mask never
  runs on its output; PostToolUseFailure can't replace it either, and the
  model gets the real values.
- PostToolBatch sees that output and can stop the run before the next model
  request, but the stream and the transcript already hold it, and a resume or
  a fork sends it to the model. Before that request only SessionStart and
  UserPromptSubmit run.
- Rewriting a command into an exit-code wrapper, which would turn a failure
  into a success, makes it fail the user's own allow rules.

Checked against Claude Code 2.1.283.
"""

import json
from datetime import datetime

import pytest

from live import harness as h

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not h.LIVE or h.claude_path() is None,
        reason="live Claude Code tests are opt-in: set VEIL_LIVE_CLAUDE=1",
    ),
]

MASK = {h.EMAIL: "[EMAIL_1]", h.NAME: "[NAME_1]", h.PHONE: "[PHONE_1]"}
MARKS = ("MARK-A", "MARK-B", "MARK-C")


def bash_call(records, text):
    """The first of these hook records whose Bash command contains ``text``."""
    for record in records:
        if text in str((record["payload"].get("tool_input") or {}).get("command")):
            return record
    pytest.fail(f"no hook record for a Bash command with {text!r}")


def failure(run, mark):
    """The PostToolUseFailure record of the Bash command with ``mark``."""
    return bash_call(run.calls("PostToolUseFailure", "Bash"), mark)


def tool_use_ids(run):
    """The tool_use_id of each marked failing command, by mark."""
    return {mark: failure(run, mark)["payload"]["tool_use_id"] for mark in MARKS}


def text_of(content):
    """The text of a tool result's content, a string or a list of blocks."""
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")) for block in content if isinstance(block, dict)
        )
    return str(content)


def transcript_results(run):
    """The text of each tool result in the transcript, by tool_use_id."""
    results = {}
    for record in run.transcript():
        content = (record.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results[block.get("tool_use_id")] = text_of(block.get("content"))
    return results


def sent_results(run):
    """The text of each tool result sent to the API, by tool_use_id."""
    results = {}
    for body in run.api_bodies():
        try:
            request = json.loads(body["request"])
        except ValueError:
            continue
        for message in request.get("messages") or []:
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results[block.get("tool_use_id")] = text_of(block.get("content"))
    return results


def hooks_before_the_first_request(run):
    """The events of the hooks that finished before the first model request."""
    requests = run.api_requests()
    assert requests, "the run made no model request"
    stamp = requests[0].split(" ", 1)[0].replace("Z", "+00:00")
    start = datetime.fromisoformat(stamp).timestamp()
    return [record["event"] for record in run.hooks if record["t1"] <= start]


# --- Failing commands, a PostToolUseFailure rewrite, a PostToolBatch stop -----

FAIL_PROMPT = "\n".join(
    [
        "This is a test of shell error handling. The commands below are EXPECTED"
        " to fail with a non-zero exit code; that is intended and not a problem.",
        "Use the Bash tool. Make exactly ONE Bash call per message and wait for its"
        " result before the next step. Do not combine steps, do not fix, change or"
        " retry any command.",
        "Step 1: run exactly: cat notes.txt; echo MARK-A; exit 1",
        "Step 2: run exactly: cat notes.txt; echo MARK-B >&2; exit 2",
        "Step 3: run exactly: cat notes.txt; echo MARK-C; exit 3",
        "Step 4: reply with the word DONE, then for each step copy the tool result"
        " text you received, verbatim.",
    ]
)

STOP_REASON = "STOP-REASON-7X1 veil tripwire: unmasked output reached the model"

FAIL_RULES = [
    {
        "event": "PostToolUseFailure",
        "tool": "Bash",
        "contains": "MARK-A",
        "respond": {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUseFailure",
                "updatedToolOutput": "SENTINEL-FAILSTR-A1 Email: [EMAIL_1]",
                "additionalContext": "CTX-FAIL-A1 context from PostToolUseFailure",
            }
        },
    },
    {
        "event": "PostToolUseFailure",
        "tool": "Bash",
        "contains": "MARK-B",
        "respond": {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUseFailure",
                "updatedToolOutput": {
                    "stdout": "SENTINEL-FAILOBJ-B2 Email: [EMAIL_1]",
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                    "noOutputExpected": False,
                },
                "additionalContext": "CTX-FAIL-B2 context from PostToolUseFailure",
            }
        },
    },
    {
        "event": "PostToolBatch",
        "contains": "MARK-C",
        "respond": {"continue": False, "stopReason": STOP_REASON},
    },
]


@pytest.fixture(scope="module")
def failed_bash(tmp_path_factory):
    ws = h.Workspace(tmp_path_factory.mktemp("failed_bash"))
    ws.write("notes.txt", h.NOTES)
    run = ws.run(FAIL_PROMPT, rules=FAIL_RULES, allowed_tools=["Bash"], max_turns=8)
    # A run the hook stops still ends with a result event; its exit code
    # hasn't been checked, so don't depend on it.
    assert run.result, run.stderr
    return ws, run


def test_a_failed_command_fires_post_tool_use_failure_instead(failed_bash):
    _, run = failed_bash
    ids = tool_use_ids(run)
    # PostToolUse is registered but never runs, so veil's mask never sees a
    # failed command's output.
    assert run.calls("PostToolUse") == []
    assert set(ids.values()) <= {
        record["payload"]["tool_use_id"] for record in run.calls("PreToolUse", "Bash")
    }
    # The output has no tool_response: it is one "error" string, exit code first.
    payload = failure(run, "MARK-C")["payload"]
    assert "tool_response" not in payload
    assert payload["error"].startswith("Exit code 3\n")
    assert h.EMAIL in payload["error"]
    # stderr is in the same string.
    error = failure(run, "MARK-B")["payload"]["error"]
    assert error.startswith("Exit code 2\n")
    assert "MARK-B" in error


def test_a_failure_hook_cannot_replace_the_output(failed_bash):
    _, run = failed_bash
    given = transcript_results(run)
    ids = tool_use_ids(run)
    for mark in ("MARK-A", "MARK-B"):
        # The hook did answer with a replacement: a string for A, Bash's own
        # output shape for B ...
        record = failure(run, mark)
        assert "updatedToolOutput" in record["response"]["hookSpecificOutput"]
        # ... and the model was given the original anyway.
        assert h.EMAIL in given[ids[mark]]
        assert "SENTINEL-" not in given[ids[mark]]
    assert "unrecognized keys (ignored): hookSpecificOutput.updatedToolOutput" in (
        run.debug
    )
    sent = sent_results(run)
    for mark in ("MARK-A", "MARK-B"):
        assert h.EMAIL in sent[ids[mark]]
    assert "SENTINEL-" not in run.sent_to_api()


def test_a_failure_hook_can_still_add_context(failed_bash):
    _, run = failed_bash
    # additionalContext from the same answers is kept, and sent with the next
    # request: veil can warn the model, but not take the value back.
    contexts = [
        text
        for attachment in run.attachments("hook_additional_context")
        for text in attachment.get("content") or []
    ]
    assert any("CTX-FAIL-A1" in text for text in contexts)
    assert any("CTX-FAIL-B2" in text for text in contexts)
    sent = run.sent_to_api()
    assert "CTX-FAIL-A1" in sent
    assert "CTX-FAIL-B2" in sent


def test_post_tool_batch_sees_the_failed_output(failed_bash):
    _, run = failed_bash
    batch = {call["tool_use_id"]: call for call in run.batch_calls("Bash")}
    for mark in MARKS:
        record = failure(run, mark)
        call = batch[record["payload"]["tool_use_id"]]
        # The same text the failure hook saw, as a plain string: this is the
        # one place a tripwire can find the real values.
        assert call["tool_response"] == record["payload"]["error"]
        assert h.EMAIL in call["tool_response"]


def test_continue_false_stops_before_the_next_model_request(failed_bash):
    _, run = failed_bash
    ids = tool_use_ids(run)
    stopped = [record for record in run.calls("PostToolBatch") if record["response"]]
    assert len(stopped) == 1
    assert ids["MARK-C"] in {
        call["tool_use_id"] for call in stopped[0]["payload"]["tool_calls"]
    }
    assert run.result["terminal_reason"] == "hook_stopped"
    # Every model request was made before the hook answered, and the model
    # wrote nothing after the stopped batch's result.
    lines = run.debug.splitlines()
    stop = next((i for i, line in enumerate(lines) if "STOP-REASON-7X1" in line), None)
    assert stop is not None
    requests = [i for i, line in enumerate(lines) if "[API REQUEST]" in line]
    assert requests
    assert max(requests) < stop
    last_result = max(i for i, e in enumerate(run.stream) if e.get("type") == "user")
    assert not any(e.get("type") == "assistant" for e in run.stream[last_result:])
    # So the stopped batch's result never left the machine; the earlier,
    # unstopped ones did.
    sent = run.sent_to_api()
    assert ids["MARK-A"] in sent
    assert ids["MARK-B"] in sent
    assert ids["MARK-C"] not in sent


def test_a_stopped_result_is_still_printed_and_kept(failed_bash):
    _, run = failed_bash
    stopped = tool_use_ids(run)["MARK-C"]
    # The stream (``-p`` stdout) gets the result before PostToolBatch runs, so
    # the tripwire can't keep it out of logs of the output.
    printed = [
        event
        for event in run.stream
        if event.get("type") == "user" and stopped in json.dumps(event)
    ]
    assert printed
    assert h.EMAIL in json.dumps(printed)
    # The transcript keeps it too, next to the stop reason as a marker a later
    # hook can look for.
    assert h.EMAIL in transcript_results(run)[stopped]
    stops = run.attachments("hook_stopped_continuation")
    assert [attachment["message"] for attachment in stops] == [STOP_REASON]
    # ``-p`` itself reports a success with an empty result: the user never
    # sees the stop reason there.
    assert run.result["is_error"] is False
    assert run.result["result"] == ""


def test_no_stop_hook_runs_after_continue_false(failed_bash):
    _, run = failed_bash
    # Stop is registered and the session ended, but anything veil does in a
    # Stop hook doesn't run for a stopped turn.
    assert run.calls("SessionEnd")
    assert run.calls("Stop") == []


# --- A fork and a resume of the stopped session ------------------------------

FORK_PROMPT = (
    "Do not use any tools. Reply with exactly one line: the Email line from the"
    " output of the Bash command containing MARK-C, copied verbatim."
)

RESUME_PROMPT = "\n".join(
    [
        "Do not use any tools. Earlier in this conversation you ran three Bash"
        " commands, containing MARK-A, MARK-B and MARK-C.",
        "For each of them, copy the exact tool result text you received, verbatim"
        " and complete, in a code block labelled MARK-A, MARK-B or MARK-C.",
        "Then copy verbatim any other text you saw that came from hooks or from the"
        " system after those commands (additional context, stop messages,"
        " warnings), or write NONE.",
    ]
)


@pytest.fixture(scope="module")
def forked(failed_bash):
    ws, first = failed_bash
    # Fork before resuming, so the fork's history is only the stopped run's.
    run = ws.run(FORK_PROMPT, max_turns=2, resume=first.session_id, fork=True)
    assert run.result, run.stderr
    return first, run


@pytest.fixture(scope="module")
def resumed(failed_bash, forked):
    # ``forked`` is requested only so that the fork always runs first.
    ws, first = failed_bash
    run = ws.run(RESUME_PROMPT, max_turns=2, resume=first.session_id)
    assert run.result, run.stderr
    return first, run


def test_a_fork_copies_the_unmasked_results_into_a_new_session(forked):
    first, run = forked
    assert run.calls("SessionStart")[0]["payload"]["source"] == "fork"
    assert run.session_id not in (None, first.session_id)
    assert run.transcript_path not in (None, first.transcript_path)
    # A second file on disk holds every unmasked result, under the original
    # tool_use_ids, and the stop marker.
    copied = transcript_results(run)
    for tool_use_id in tool_use_ids(first).values():
        assert h.EMAIL in copied[tool_use_id]
    stops = run.attachments("hook_stopped_continuation")
    assert STOP_REASON in [attachment["message"] for attachment in stops]
    # Nothing a hook can read names the parent session, so veil can't look up
    # the parent's mappings by session id.
    assert first.session_id not in run.transcript_text
    assert first.session_id not in json.dumps([r["payload"] for r in run.hooks])


def test_a_fork_sends_the_stopped_result_to_the_model(forked):
    first, run = forked
    stopped = tool_use_ids(first)["MARK-C"]
    assert stopped not in first.sent_to_api()
    assert h.EMAIL in sent_results(run).get(stopped, "")


def test_resume_keeps_the_session_and_its_transcript(resumed):
    first, run = resumed
    assert run.calls("SessionStart")[0]["payload"]["source"] == "resume"
    assert run.session_id == first.session_id
    assert run.transcript_path == first.transcript_path
    # The file a resumed session's hooks are pointed at still holds the
    # unmasked result and the stop marker.
    stopped = tool_use_ids(first)["MARK-C"]
    assert h.EMAIL in transcript_results(run)[stopped]
    stops = run.attachments("hook_stopped_continuation")
    assert STOP_REASON in [attachment["message"] for attachment in stops]


def test_resume_sends_the_stopped_result_and_the_stop_reason(resumed):
    first, run = resumed
    stopped = tool_use_ids(first)["MARK-C"]
    assert stopped not in first.sent_to_api()
    assert h.EMAIL in sent_results(run).get(stopped, "")
    # The model is sent the stop reason too, so it must hold no real values.
    assert STOP_REASON in run.sent_to_api()


def test_no_tool_hook_runs_before_a_resumed_or_forked_request(forked, resumed):
    # The history goes out with the first request, and no hook that sees tool
    # data runs first, so the tripwire can't catch it. UserPromptSubmit runs,
    # and can hold the prompt back (see test_claude_live).
    for _, run in (forked, resumed):
        assert hooks_before_the_first_request(run) == [
            "SessionStart",
            "UserPromptSubmit",
        ]


# --- An exit-code wrapper set through PreToolUse updatedInput ----------------

SHOW_SH = '#!/bin/sh\ncat notes.txt\necho "show.sh arg=$1" >&2\nexit 1\n'


def wrap_printf(cmd):
    return "{ " + cmd + " ; } 2>&1; printf '\\n[exit code %s]\\n' \"$?\""


def wrap_echo(cmd):
    return "{ " + cmd + ' ; } 2>&1; echo "[exit code $?]"'


def wrap_subshell(cmd):
    return "( " + cmd + " ) 2>&1; printf '\\n[exit code %s]\\n' \"$?\""


WRAP_PROMPT = "\n".join(
    [
        "This is a test of shell error handling. Some commands below are EXPECTED"
        " to fail; that is intended and not a problem.",
        "Use the Bash tool. Make exactly ONE Bash call per message and wait for its"
        " result before the next step. Run each command exactly as written; do not"
        " combine steps, do not fix, change or retry any command. If a command is"
        " denied, do not retry it; go on to the next step.",
        "Step 1: run exactly: cat notes.txt missing1.txt",
        "Step 2: run exactly: cat missing2.txt notes.txt",
        "Step 3: run exactly: ./show.sh two",
        "Step 4: run exactly: ./show.sh one",
        "Step 5: run exactly: ./show.sh three",
        "Step 6: run exactly: cat notes.txt; echo MARK-D; exit 4",
        "Step 7: run exactly: cat notes.txt; echo MARK-E; exit 5",
        'Step 8: reply with one line per step in the form "Step N: exit code X"'
        " (write DENIED instead if permission was denied, or UNKNOWN if you could"
        " not tell), then copy the tool result text of steps 1 and 4 verbatim.",
    ]
)

# "./show.sh two" is the unwrapped control for "./show.sh one".
WRAPPED = {
    "missing1.txt": wrap_printf("cat notes.txt missing1.txt"),
    "missing2.txt": wrap_echo("cat missing2.txt notes.txt"),
    "show.sh one": wrap_printf("./show.sh one"),
    "show.sh three": wrap_echo("./show.sh three"),
    "MARK-D": wrap_printf("cat notes.txt; echo MARK-D; exit 4"),
    "MARK-E": wrap_subshell("cat notes.txt; echo MARK-E; exit 5"),
}

WRAP_RULES = [
    *(
        {
            "event": "PreToolUse",
            "tool": "Bash",
            "contains": key,
            "set_input": {"command": cmd},
        }
        for key, cmd in WRAPPED.items()
    ),
    {"event": "PostToolUse", "tool": "Bash", "replace_output": MASK},
]

WRAP_ALLOWED = ["Bash(cat:*)", "Bash(./show.sh:*)", "Bash(./show.sh *)"]


@pytest.fixture(scope="module")
def wrapped(tmp_path_factory):
    ws = h.Workspace(tmp_path_factory.mktemp("wrapped"))
    ws.write("notes.txt", h.NOTES)
    ws.write("show.sh", SHOW_SH).chmod(0o755)
    run = ws.run(
        WRAP_PROMPT,
        rules=WRAP_RULES,
        allowed_tools=WRAP_ALLOWED,
        permission_mode="default",
        max_turns=14,
    )
    assert run.result, run.stderr
    return run


def denied_commands(run):
    """The commands the result lists as denied, as checked (after any rewrite)."""
    return {
        denial["tool_input"]["command"] for denial in run.result["permission_denials"]
    }


def test_a_rewritten_command_fails_the_allow_rule_its_original_passes(wrapped):
    run = wrapped
    denied = denied_commands(run)
    # The control: "./show.sh two" as written matches the user's allow rules
    # and runs.
    bash_call(run.calls("PostToolUseFailure", "Bash"), "./show.sh two")
    assert not any("show.sh two" in command for command in denied)
    # "./show.sh one" is checked as PreToolUse rewrote it; the wrapper needs a
    # prompt ("Contains compound_statement"), which -p denies.
    one = bash_call(run.calls("PreToolUse", "Bash"), "./show.sh one")
    rewrite = one["response"]["hookSpecificOutput"]["updatedInput"]["command"]
    assert rewrite == WRAPPED["show.sh one"]
    assert rewrite in denied
    assert one["payload"]["tool_use_id"] in {
        event.get("tool_use_id")
        for event in run.stream
        if event.get("subtype") == "permission_denied"
    }
    # Only rewritten commands were denied.
    assert denied <= set(WRAPPED.values())


def test_permission_request_sees_the_rewrite_and_post_tool_batch_the_original(
    wrapped,
):
    run = wrapped
    one = bash_call(run.calls("PreToolUse", "Bash"), "./show.sh one")
    # The rewritten command goes to PermissionRequest: in veil, the command
    # with the real values restored.
    asked = [
        record["payload"]["tool_input"]["command"]
        for record in run.calls("PermissionRequest", "Bash")
    ]
    assert WRAPPED["show.sh one"] in asked
    # PostToolBatch reports the model's own command, and a denial with no output.
    batch = {call["tool_use_id"]: call for call in run.batch_calls("Bash")}
    call = batch[one["payload"]["tool_use_id"]]
    assert call["tool_input"]["command"] == "./show.sh one"
    assert h.EMAIL not in str(call["tool_response"])


def test_no_rewritten_command_ever_runs(wrapped):
    run = wrapped
    # In -p nothing can answer the permission prompt, so every rewrite that
    # needed one was denied, whatever its form (brace group or subshell,
    # printf or echo).
    asked = {
        record["payload"]["tool_input"]["command"]
        for record in run.calls("PermissionRequest", "Bash")
    }
    assert asked
    assert asked <= denied_commands(run)
    rewritten = {
        record["payload"]["tool_use_id"]
        for record in run.calls("PreToolUse", "Bash")
        if record["response"]
    }
    ran = {
        record["payload"]["tool_use_id"]
        for event in ("PostToolUse", "PostToolUseFailure")
        for record in run.calls(event, "Bash")
    }
    assert rewritten
    assert ran
    assert not rewritten & ran
    # And a denial like this doesn't fire PermissionDenied.
    assert run.calls("PermissionDenied") == []


def test_an_allowed_failure_skips_the_post_tool_use_mask(wrapped):
    run = wrapped
    two = bash_call(run.calls("PostToolUseFailure", "Bash"), "./show.sh two")
    error = two["payload"]["error"]
    assert error.startswith("Exit code 1\n")
    assert h.EMAIL in error
    assert "show.sh arg=two" in error
    # The PostToolUse mask rule never ran for it, so the model got the real
    # value, even in a normal permission mode with the command allow-listed.
    tool_use_id = two["payload"]["tool_use_id"]
    assert tool_use_id not in {
        record["payload"]["tool_use_id"] for record in run.calls("PostToolUse")
    }
    assert h.EMAIL in transcript_results(run)[tool_use_id]
    assert h.EMAIL in sent_results(run).get(tool_use_id, "")
