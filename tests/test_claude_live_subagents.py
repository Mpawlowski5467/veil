"""Live checks: subagents and background work under veil-style hooks (opt-in).

Run with ``VEIL_LIVE_CLAUDE=1 uv run pytest -m live``. Three scenarios, one
headless ``claude -p`` run each, all reading the fictional notes file:

- ``defaults``: an Agent call and a background Bash command, left as the
  model makes them. Checks how a background report and background output
  reach the model.
- ``delegation``: PreToolUse forces one agent and a background command into
  the foreground and leaves a second agent in the background, while
  PostToolUse masks every Read and Bash result and the foreground report.
  Checks that subagents run the session's hooks, and where each result goes.
- ``steering``: SubagentStop blocks each subagent's first stop so that it
  rewrites its report, and UserPromptSubmit blocks the background agent's
  task notification.

The prompts ask haiku for exact calls. When it doesn't make them (the stream
shows the calls it asked for, before any hook), the scenario's tests skip:
such a run says nothing about Claude Code.

Checked against Claude Code 2.1.283.
"""

import json
import re
from pathlib import Path

import pytest

from live import harness as h

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not h.LIVE or h.claude_path() is None,
        reason="live Claude Code tests are opt-in: set VEIL_LIVE_CLAUDE=1",
    ),
]

# One stand-in per hook, so any text shows which hook produced it.
SUB_MASK = "SUBREADMASK-2P8"  # a subagent's Read
MAIN_MASK = "MAINREADMASK-5W1"  # a main-thread Read
BASH_MASK = "BASHMASK-3B6"
AGENT_MASK = "AGENTREPL-6T1"  # a foreground agent's report
ANY_MASK = "GENMASK-8K2"  # any other tool's result
SUBCTX = "SENTINEL-SUBCTX-4H7"
REWRITTEN = "REWRITTEN-7C3"
NOTIF_BLOCKED = "NOTIF-BLOCKED-5J2"

SUBAGENT_START = {
    "event": "SubagentStart",
    "respond": {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": (
                f"{SUBCTX} is the context sentinel from the SubagentStart hook."
            ),
        }
    },
}
# Subagent tool calls carry agent_id; main-thread ones don't.
SUB_READ = {
    "event": "PostToolUse",
    "tool": "Read",
    "contains": '"agent_id"',
    "replace_output": {h.EMAIL: SUB_MASK},
}
MAIN_READ = {
    "event": "PostToolUse",
    "tool": "Read",
    "replace_output": {h.EMAIL: MAIN_MASK},
}
BASH_OUTPUT = {
    "event": "PostToolUse",
    "tool": "Bash",
    "replace_output": {h.EMAIL: BASH_MASK},
}

# The subagent prompt of the first two scenarios: its report quotes the
# (masked) Read and whatever SubagentStart added to its context.
READ_AND_QUOTE = (
    "Use the Read tool to read the file notes.txt in the current working "
    "directory. Then reply with exactly two lines. Line 1: the Email line from "
    "the file, copied verbatim. Line 2: every piece of text containing the word "
    "SENTINEL followed by a dash that appears anywhere in your context, copied "
    "verbatim, or the word NONE if there is none."
)

TOOLS = ["Read", "Bash", "Agent"]


def agent_step(number, description, prompt):
    """One prompt step asking for a background Agent call."""
    return (
        f'Step {number}: Call the Agent tool with subagent_type "general-purpose", '
        f'description "{description}", run_in_background set to true, and this '
        f'exact prompt: "{prompt}"'
    )


def tool_uses(run):
    """Every tool call the stream shows, as (parent_tool_use_id, tool_use block).

    The parent is None for the main model's calls, and the Agent call's
    tool_use_id for a subagent's. This is what the model asked for, before
    any hook changed it.
    """
    return [
        (event.get("parent_tool_use_id"), block)
        for event in run.stream
        if event.get("type") == "assistant"
        for block in (event.get("message") or {}).get("content") or []
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]


def asked(run, tool, key, text):
    """The inputs of the main model's calls of ``tool`` with ``text`` in ``key``."""
    return [
        block.get("input") or {}
        for parent, block in tool_uses(run)
        if parent is None
        and re.fullmatch(tool, str(block.get("name")))
        and text in str((block.get("input") or {}).get(key, ""))
    ]


def skip_unless_asked(run, calls):
    """Skip a scenario's tests if the main model didn't make the calls it needs."""
    missing = [call for call in calls if not asked(run, *call)]
    if missing:
        pytest.skip(f"haiku didn't make the calls the prompt asks for: {missing}")


def run_scenario(tmp_path_factory, name, prompt, rules, calls):
    """Run one scenario on the fictional notes file and return the Run."""
    ws = h.Workspace(tmp_path_factory.mktemp(name))
    ws.write("notes.txt", h.NOTES)
    run = ws.run(prompt, rules=rules, allowed_tools=TOOLS, max_turns=14, timeout=600)
    assert run.returncode == 0, run.stderr
    skip_unless_asked(run, calls)
    return run


def main_thread(records):
    """The records of hook calls made outside any subagent."""
    return [record for record in records if "agent_id" not in record["payload"]]


def first_call(run, tool, key, text):
    """PreToolUse and PostToolUse records of the main thread's first such call.

    That is the first call of ``tool`` whose input has ``text`` in ``key``;
    the two records are paired by tool_use_id, so a retry can't mix them up.
    """
    pres = [
        record
        for record in main_thread(run.calls("PreToolUse", tool))
        if text in str(record["payload"]["tool_input"].get(key, ""))
    ]
    assert pres, f"no PreToolUse for a {tool} call with {text!r}"
    tool_use_id = pres[0]["payload"]["tool_use_id"]
    posts = [
        record
        for record in run.calls("PostToolUse", tool)
        if record["payload"]["tool_use_id"] == tool_use_id
    ]
    assert posts, f"no PostToolUse for the {tool} call with {text!r}"
    return pres[0], posts[0]


def launched(run):
    """The agentId each main-thread Agent call returned, by its tool_use_id."""
    return {
        record["payload"]["tool_use_id"]: record["payload"]["tool_response"]["agentId"]
        for record in main_thread(run.calls("PostToolUse", "Agent"))
    }


def stops(run, agent_id):
    """The SubagentStop records of one subagent, in order."""
    found = [
        record
        for record in run.calls("SubagentStop")
        if record["payload"].get("agent_id") == agent_id
    ]
    assert found, f"subagent {agent_id} never reached SubagentStop"
    return found


def subagent_records(stop):
    """The records of the subagent transcript a SubagentStop payload names."""
    text = Path(stop["agent_transcript_path"]).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def task_started(run, tool_use_id):
    """The stream's ``task_started`` event for this call, or None."""
    for event in run.stream:
        if (
            event.get("subtype") == "task_started"
            and event.get("tool_use_id") == tool_use_id
        ):
            return event
    return None


def notices(run, tool_use_id):
    """The task notifications (UserPromptSubmit prompts) about this call."""
    marker = f"<tool-use-id>{tool_use_id}</tool-use-id>"
    return [prompt for prompt in run.notifications() if marker in prompt]


def tag(text, name):
    """The text inside ``<name>...</name>`` in a task notification, or None."""
    found = re.search(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)
    return found.group(1) if found else None


def given_to_model(run, tool_use_id):
    """The main transcript's tool result for this call, as JSON text."""
    results = [
        json.dumps(block.get("content"))
        for record in run.transcript()
        for block in (record.get("message") or {}).get("content") or []
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and block.get("tool_use_id") == tool_use_id
    ]
    assert results, f"no tool result for {tool_use_id} in the transcript"
    return results[0]


def batch_entry(run, tool_use_id):
    """The PostToolBatch entry for this call (main thread or subagent)."""
    entries = [
        entry for entry in run.batch_calls() if entry.get("tool_use_id") == tool_use_id
    ]
    assert entries, f"PostToolBatch never listed {tool_use_id}"
    return entries[0]


def report_of(response):
    """The report text in a completed (foreground) Agent tool_response."""
    return "".join(block.get("text", "") for block in response["content"])


def delivered_report(run, post):
    """The report of an Agent call as the main thread received it."""
    response = post["payload"]["tool_response"]
    if response.get("status") == "completed":
        return report_of(response)
    found = notices(run, post["payload"]["tool_use_id"])
    assert found, "the background report never arrived as a task notification"
    return tag(found[0], "result") or ""


# --- Scenario 1: an agent and a background command, left as they are --------

DEFAULTS_PROMPT = "\n\n".join(
    [
        "Follow these steps exactly, in order.",
        'Step 1: Call the Agent tool once, with subagent_type "general-purpose", '
        'description "read notes", and this exact prompt for the subagent:\n'
        f'"{READ_AND_QUOTE}"\n'
        "Do not set run_in_background on the Agent call.",
        "Step 2: Call the Bash tool once, with run_in_background set to true, to "
        "run this exact command: sleep 2; cat notes.txt",
        "Step 3: Get the output of that background Bash command, and wait until "
        "the subagent has reported back.",
        'Step 4: Give your final answer with three parts: the line "SUBAGENT '
        "REPORT:\" followed by the subagent's report copied verbatim; the line "
        '"BACKGROUND OUTPUT:" followed by the background command\'s output '
        'copied verbatim; and the line "HOW:" followed by one sentence saying '
        "how you received each of them.",
    ]
)

DEFAULTS_RULES = [
    SUBAGENT_START,
    SUB_READ,
    MAIN_READ,
    BASH_OUTPUT,
    {
        "event": "PostToolUse",
        "tool": "Agent|Task",
        "replace_output": {SUB_MASK: AGENT_MASK, h.EMAIL: AGENT_MASK},
    },
    # A catch-all for any other tool that might carry the email.
    {"event": "PostToolUse", "replace_output": {h.EMAIL: ANY_MASK}},
]

DEFAULTS_CALLS = [
    ("Agent|Task", "description", "read notes"),
    ("Bash", "command", "cat notes.txt"),
]


@pytest.fixture(scope="module")
def defaults(tmp_path_factory):
    return run_scenario(
        tmp_path_factory, "defaults", DEFAULTS_PROMPT, DEFAULTS_RULES, DEFAULTS_CALLS
    )


def test_an_agent_runs_in_the_background_by_default(defaults):
    pre, post = first_call(defaults, "Agent", "description", "read notes")
    if "run_in_background" in pre["payload"]["tool_input"]:
        pytest.skip("haiku set run_in_background itself; the default wasn't used")
    # In -p mode the call returns at once, so PostToolUse never sees the
    # report: veil can't mask it there.
    response = post["payload"]["tool_response"]
    assert response["status"] == "async_launched"
    assert response["isAsync"] is True
    assert "content" not in response
    tool_use_id = post["payload"]["tool_use_id"]
    assert task_started(defaults, tool_use_id)["is_backgrounded"] is True
    assert notices(defaults, tool_use_id), "its report never came as a notification"


def test_background_output_arrives_only_through_its_output_file(defaults):
    pre, post = first_call(defaults, "Bash", "command", "cat notes.txt")
    if pre["payload"]["tool_input"].get("run_in_background") is not True:
        pytest.skip("haiku ran the command in the foreground")
    response = post["payload"]["tool_response"]
    tool_use_id = post["payload"]["tool_use_id"]
    task = response["backgroundTaskId"]
    assert task_started(defaults, tool_use_id)["is_backgrounded"] is True
    # The tool result has no output yet ...
    assert h.EMAIL not in json.dumps(response)
    # ... and the notification that the command finished doesn't carry it
    # either (the name is never masked, so it would show if it did).
    found = notices(defaults, tool_use_id)
    assert found, "the command's completion never came as a notification"
    notice = found[0]
    assert f"<task-id>{task}</task-id>" in notice
    assert tag(notice, "status") == "completed"
    assert tag(notice, "result") is None
    assert h.EMAIL not in notice
    assert h.NAME not in notice
    # The output sits unmasked in a file on disk ...
    output_file = Path(tag(notice, "output-file"))
    assert h.EMAIL in output_file.read_text(encoding="utf-8")
    # ... and reaches the model only through a tool call that reads it, whose
    # result the existing PostToolUse rules mask: no extra hook is needed.
    fetches = [
        record
        for record in main_thread(defaults.calls("PostToolUse"))
        if output_file.name in json.dumps(record["payload"]["tool_input"])
    ]
    if not fetches:
        pytest.skip("haiku never fetched the output file")
    assert any(
        h.EMAIL in json.dumps(record["payload"]["tool_response"]) for record in fetches
    )
    for record in fetches:
        given = json.dumps(batch_entry(defaults, record["payload"]["tool_use_id"]))
        assert h.EMAIL not in given
        assert MAIN_MASK in given or BASH_MASK in given


# --- Scenario 2: forced-foreground and background work, all masked -----------

DELEGATION_PROMPT = "\n\n".join(
    [
        "Follow these steps exactly, in order, with one tool call per step.",
        agent_step(1, "fg read", READ_AND_QUOTE),
        agent_step(2, "bg read", READ_AND_QUOTE),
        "Step 3: Call the Bash tool with run_in_background set to true, to run "
        "this exact command: sleep 2; cat notes.txt",
        "Step 4: Wait until both agents have reported and the command has "
        "finished. If the command's output was not in its tool result, use the "
        "Read tool on the output file it names.",
        'Step 5: Give your final answer with three parts: the line "FG REPORT:" '
        "followed by the fg read agent's report copied verbatim; the line "
        '"BG REPORT:" followed by the bg read agent\'s report copied verbatim, '
        'or MISSING; and the line "COMMAND OUTPUT:" followed by the command\'s '
        "output copied verbatim.",
    ]
)

DELEGATION_RULES = [
    {
        "event": "PreToolUse",
        "tool": "Agent|Task",
        "contains": "fg read",
        "set_input": {"run_in_background": False},
    },
    {"event": "PreToolUse", "tool": "Bash", "set_input": {"run_in_background": False}},
    SUBAGENT_START,
    SUB_READ,
    MAIN_READ,
    BASH_OUTPUT,
    {
        "event": "PostToolUse",
        "tool": "Agent|Task",
        "replace_output": {SUB_MASK: AGENT_MASK},
    },
]

DELEGATION_CALLS = [
    ("Agent|Task", "description", "fg read"),
    ("Agent|Task", "description", "bg read"),
    ("Bash", "command", "cat notes.txt"),
]


@pytest.fixture(scope="module")
def delegation(tmp_path_factory):
    return run_scenario(
        tmp_path_factory,
        "delegation",
        DELEGATION_PROMPT,
        DELEGATION_RULES,
        DELEGATION_CALLS,
    )


def test_subagent_tool_calls_run_the_session_hooks(delegation):
    agents = launched(delegation)
    started = {
        r["payload"]["agent_id"]: r["payload"]
        for r in delegation.calls("SubagentStart")
    }
    assert set(agents.values()) <= set(started)
    # Nested agents too, should a subagent start one.
    parents = {
        record["payload"]["tool_use_id"]: record["payload"]["tool_response"]["agentId"]
        for record in delegation.calls("PostToolUse", "Agent")
    }
    for event in ("PreToolUse", "PostToolUse"):
        payloads = {
            record["payload"]["tool_use_id"]: record["payload"]
            for record in delegation.calls(event)
        }
        seen = set()
        for parent, block in tool_uses(delegation):
            payload = payloads.get(block["id"])
            if payload is None:
                continue
            if parent is None:
                # The main thread's calls carry no agent_id ...
                assert "agent_id" not in payload
                continue
            # ... and a subagent's carry the id of the agent that made it,
            # under the parent's session and transcript: one vault per
            # session_id serves the parent and all its subagents.
            assert payload.get("agent_id") == parents.get(parent)
            assert payload["agent_type"] == started[payload["agent_id"]]["agent_type"]
            assert payload["session_id"] == delegation.session_id
            assert Path(payload["transcript_path"]) == delegation.transcript_path
            assert "agent_transcript_path" not in payload
            seen.add(payload["agent_id"])
        assert seen >= set(agents.values()), f"a subagent's calls skipped {event}"
    # Only SubagentStop names the subagent's own transcript, beside the parent's.
    folder = delegation.transcript_path.with_suffix("") / "subagents"
    for agent in agents.values():
        stop = stops(delegation, agent)[-1]["payload"]
        assert Path(stop["agent_transcript_path"]) == folder / f"agent-{agent}.jsonl"


def test_a_subagent_read_is_masked_by_posttooluse(delegation):
    reads = [
        record
        for record in delegation.calls("PostToolUse", "Read")
        if "agent_id" in record["payload"]
    ]
    assert reads, "no subagent called Read"
    assert any(h.EMAIL in json.dumps(r["payload"]["tool_response"]) for r in reads)
    for record in reads:
        # What the subagent was given (its own PostToolBatch shows it) is the
        # replacement, as for a main-thread call.
        given = json.dumps(batch_entry(delegation, record["payload"]["tool_use_id"]))
        assert h.EMAIL not in given
        if h.EMAIL in json.dumps(record["payload"]["tool_response"]):
            assert SUB_MASK in given
    # The subagents' transcripts on disk keep only the masked text.
    for agent in launched(delegation).values():
        stop = stops(delegation, agent)[-1]["payload"]
        text = Path(stop["agent_transcript_path"]).read_text(encoding="utf-8")
        assert SUB_MASK in text
        assert h.EMAIL not in text


def test_subagent_start_context_reaches_the_subagent(delegation):
    # SubagentStart's additionalContext is how veil can tell a subagent how
    # placeholders work.
    finals = []
    for agent in launched(delegation).values():
        stop = stops(delegation, agent)[-1]["payload"]
        context = [
            record["attachment"]
            for record in subagent_records(stop)
            if (record.get("attachment") or {}).get("type") == "hook_additional_context"
        ]
        assert any(
            item.get("hookEvent") == "SubagentStart" and SUBCTX in json.dumps(item)
            for item in context
        )
        finals.append(stop["last_assistant_message"])
    assert len(finals) >= 2
    # The subagents could quote it, as their prompt asks.
    assert any(SUBCTX in text for text in finals)


def test_set_input_forces_an_agent_into_the_foreground(delegation):
    pre, post = first_call(delegation, "Agent", "description", "fg read")
    if pre["payload"]["tool_input"].get("run_in_background") is False:
        pytest.skip("haiku ran the fg read agent in the foreground itself")
    updated = pre["response"]["hookSpecificOutput"]["updatedInput"]
    assert updated["run_in_background"] is False
    assert post["payload"]["tool_input"]["run_in_background"] is False
    # The call waited for the subagent, so its report is the tool result.
    assert post["payload"]["tool_response"]["status"] == "completed"
    started = task_started(delegation, post["payload"]["tool_use_id"])
    assert started["is_backgrounded"] is False


def test_a_foreground_report_is_a_result_posttooluse_can_replace(delegation):
    _, post = first_call(delegation, "Agent", "description", "fg read")
    response = post["payload"]["tool_response"]
    assert response["status"] == "completed"
    # The tool result is the subagent's final message ...
    report = report_of(response)
    final = stops(delegation, response["agentId"])[-1]["payload"]
    assert report.strip() == final["last_assistant_message"].strip()
    if SUB_MASK not in report:
        pytest.skip("the fg read agent didn't quote its Read; nothing to replace")
    # ... so updatedToolOutput replaces what the model gets, as for any tool.
    tool_use_id = post["payload"]["tool_use_id"]
    for text in (
        given_to_model(delegation, tool_use_id),
        json.dumps(batch_entry(delegation, tool_use_id)["tool_response"]),
    ):
        assert AGENT_MASK in text
        assert SUB_MASK not in text


def test_a_background_report_arrives_only_as_a_task_notification(delegation):
    pre, post = first_call(delegation, "Agent", "description", "bg read")
    if pre["payload"]["tool_input"].get("run_in_background") is False:
        pytest.skip("haiku ran the bg read agent in the foreground itself")
    response = post["payload"]["tool_response"]
    tool_use_id = post["payload"]["tool_use_id"]
    assert response["status"] == "async_launched"
    assert "content" not in response
    assert task_started(delegation, tool_use_id)["is_backgrounded"] is True
    stop = stops(delegation, response["agentId"])[-1]["payload"]
    report = stop["last_assistant_message"]
    assert report.strip()
    # The report comes later as a prompt, through UserPromptSubmit, and the
    # model gets it as a user message: veil's UserPromptSubmit hook must not
    # take these for something the user typed.
    found = notices(delegation, tool_use_id)
    assert found, "the report never came as a task notification"
    assert f"<task-id>{response['agentId']}</task-id>" in found[0]
    assert (tag(found[0], "result") or "").strip() == report.strip()
    marker = f"<tool-use-id>{tool_use_id}</tool-use-id>"
    assert any(
        record.get("type") == "user"
        and (record.get("origin") or {}).get("kind") == "task-notification"
        and marker in json.dumps(record)
        for record in delegation.transcript()
    )
    # PostToolUse and PostToolBatch only ever see the launch, so a tripwire
    # there never sees the report.
    escaped = json.dumps(report)[1:-1]
    assert escaped in json.dumps(found[0])
    seen = [
        json.dumps(post["payload"]),
        json.dumps(batch_entry(delegation, tool_use_id)),
    ]
    assert not any(escaped in text for text in seen)
    # Only background work notifies: the foreground agent and the command
    # don't.
    for tool, key, text in (
        ("Agent", "description", "fg read"),
        ("Bash", "command", "cat notes.txt"),
    ):
        other = first_call(delegation, tool, key, text)[1]["payload"]["tool_use_id"]
        assert not notices(delegation, other)
    # The launch names an output file the model may Read: it is the
    # subagent's own transcript, first report and all.
    output_file = Path(response["outputFile"]).resolve()
    assert output_file == Path(stop["agent_transcript_path"]).resolve()


def test_set_input_forces_bash_into_the_foreground(delegation):
    pre, post = first_call(delegation, "Bash", "command", "cat notes.txt")
    if pre["payload"]["tool_input"].get("run_in_background") is not True:
        pytest.skip("haiku didn't ask for a background command")
    updated = pre["response"]["hookSpecificOutput"]["updatedInput"]
    assert updated["run_in_background"] is False
    # The output is in the result itself, so PostToolUse masks it as usual.
    response = post["payload"]["tool_response"]
    assert "backgroundTaskId" not in response
    assert h.EMAIL in response["stdout"]
    given = given_to_model(delegation, post["payload"]["tool_use_id"])
    assert BASH_MASK in given
    assert h.EMAIL not in given
    # No background task was started for it (the agents' were).
    assert task_started(delegation, post["payload"]["tool_use_id"]) is None
    assert any(event.get("subtype") == "task_started" for event in delegation.stream)


def test_post_tool_batch_shows_the_input_before_pre_tool_use_changed_it(delegation):
    # A PostToolBatch hook that reads tool_input sees the model's own input,
    # not the one PreToolUse replaced it with.
    forced = [
        (pre, post)
        for pre, post in (
            first_call(delegation, "Agent", "description", "fg read"),
            first_call(delegation, "Bash", "command", "cat notes.txt"),
        )
        if pre["response"]
    ]
    if not forced:
        pytest.skip("haiku asked for the foreground itself; no input was changed")
    for pre, post in forced:
        entry = batch_entry(delegation, post["payload"]["tool_use_id"])
        assert post["payload"]["tool_input"]["run_in_background"] is False
        assert entry["tool_input"] == pre["payload"]["tool_input"]
        assert entry["tool_input"] != post["payload"]["tool_input"]


def test_the_email_never_reached_the_model(delegation):
    # The hooks were given the real value by the subagents' Reads and by the
    # command ...
    raw = {
        record["tool"]
        for record in delegation.calls("PostToolUse")
        if h.EMAIL in json.dumps(record["payload"].get("tool_response"))
    }
    assert {"Read", "Bash"} <= raw
    # ... and it is nowhere in the stream (every result included), the
    # transcripts, or the debug log, where the masks are.
    everything = delegation.everything()
    assert SUB_MASK in everything
    assert BASH_MASK in everything
    assert h.EMAIL not in everything
    for agent in launched(delegation).values():
        stop = stops(delegation, agent)[-1]["payload"]
        text = Path(stop["agent_transcript_path"]).read_text(encoding="utf-8")
        assert h.EMAIL not in text


def test_the_email_never_reached_the_api(delegation):
    sent = delegation.sent_to_api()
    # The command's masked output was sent, so the bodies were captured ...
    assert BASH_MASK in sent
    # ... and the real value never left the machine for the model.
    assert h.EMAIL not in sent


# --- Scenario 3: rewriting a report at SubagentStop, blocking a notification --

STEERING_SUB = (
    "Use the Read tool to read notes.txt in the current working directory, "
    "then reply with only the Email line from the file, copied verbatim."
)

STEERING_PROMPT = "\n\n".join(
    [
        "Follow these steps exactly, in order.",
        agent_step(1, "fg check", STEERING_SUB)
        + "\nWait for the result of this call before Step 2.",
        agent_step(2, "bg check", STEERING_SUB),
        'Step 3: Wait until the "bg check" agent has reported back.',
        'Step 4: Give your final answer with three parts: the line "FG REPORT:" '
        "followed by the fg check agent's report copied verbatim; the line "
        '"BG REPORT:" followed by the bg check agent\'s report copied verbatim, '
        'or the word MISSING if you never received it; and the line "HOW:" '
        "followed by one sentence saying how you received each report.",
    ]
)

REWRITE = {
    "decision": "block",
    "reason": (
        "VEIL-CHECK: output your previous final answer again, with every "
        f"occurrence of {SUB_MASK} replaced by {REWRITTEN}, and nothing else."
    ),
}
BLOCK_NOTIFICATION = {
    "decision": "block",
    "reason": f"{NOTIF_BLOCKED} held back by hook",
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "suppressOriginalPrompt": True,
    },
}

STEERING_RULES = [
    {
        "event": "PreToolUse",
        "tool": "Agent|Task",
        "contains": "fg check",
        "set_input": {"run_in_background": False},
    },
    SUBAGENT_START,
    # Block each subagent's first stop only (whichever spacing the JSON
    # uses): blocking a stop with stop_hook_active set would loop.
    {
        "event": "SubagentStop",
        "contains": '"stop_hook_active":false',
        "respond": REWRITE,
    },
    {
        "event": "SubagentStop",
        "contains": '"stop_hook_active": false',
        "respond": REWRITE,
    },
    {
        "event": "UserPromptSubmit",
        "contains": "<task-notification>",
        "respond": BLOCK_NOTIFICATION,
    },
    SUB_READ,
    MAIN_READ,
]

STEERING_CALLS = [
    ("Agent|Task", "description", "fg check"),
    ("Agent|Task", "description", "bg check"),
]


@pytest.fixture(scope="module")
def steering(tmp_path_factory):
    return run_scenario(
        tmp_path_factory, "steering", STEERING_PROMPT, STEERING_RULES, STEERING_CALLS
    )


def test_subagent_stop_block_makes_the_subagent_rewrite_its_report(steering):
    # SubagentStop sees a report before the parent does, in the foreground
    # and the background, so it is where veil can have a bad report redone.
    for description in ("fg check", "bg check"):
        _, post = first_call(steering, "Agent", "description", description)
        found = stops(steering, post["payload"]["tool_response"]["agentId"])
        # The block made the subagent go on, and stop once more.
        assert [s["payload"]["stop_hook_active"] for s in found] == [False, True]
        assert found[0]["response"]["decision"] == "block"
        first, final = (s["payload"]["last_assistant_message"] for s in found)
        # The reason reached the subagent as a message ...
        assert any(
            record.get("type") == "user"
            and REWRITE["reason"] in json.dumps(record.get("message"))
            for record in subagent_records(found[-1]["payload"])
        )
        # ... and it redid the report as told.
        assert REWRITTEN not in first
        assert REWRITTEN in final
        # The parent only ever got the redone report.
        assert delivered_report(steering, post).strip() == final.strip()


def test_blocking_a_task_notification_withholds_it_and_skips_the_turn(steering):
    pre, post = first_call(steering, "Agent", "description", "bg check")
    if pre["payload"]["tool_input"].get("run_in_background") is False:
        pytest.skip("haiku ran the bg check agent in the foreground itself")
    marker = f"<tool-use-id>{post['payload']['tool_use_id']}</tool-use-id>"
    blocked = [
        record
        for record in steering.calls("UserPromptSubmit")
        if marker in str(record["payload"].get("prompt"))
    ]
    assert blocked, "the notification never reached UserPromptSubmit"
    assert blocked[0]["response"]["decision"] == "block"
    records = steering.transcript()
    # It was queued, and its text stays on disk in the queue record ...
    assert any(
        record.get("type") == "queue-operation" and marker in str(record.get("content"))
        for record in records
    )
    # ... but it never became a message for the model,
    assert not any(
        record.get("type") == "user" and marker in json.dumps(record)
        for record in records
    )
    # and its turn made no model call: the last result is the block itself.
    # (Nothing else was pending, so this doesn't show whether a block ends a
    # run that still has work to come.)
    result = steering.result
    assert (result.get("origin") or {}).get("kind") == "task-notification"
    assert result.get("num_turns") == 0
    assert NOTIF_BLOCKED in str(result.get("result"))


def test_a_blocked_task_notification_is_never_sent_to_the_api(steering):
    pre, post = first_call(steering, "Agent", "description", "bg check")
    if pre["payload"]["tool_input"].get("run_in_background") is False:
        pytest.skip("haiku ran the bg check agent in the foreground itself")
    agent = post["payload"]["tool_response"]["agentId"]
    sent = steering.sent_to_api()
    # The launch result, which names the agent, was sent ...
    assert agent in sent
    # ... but never the notification with its report.
    assert f"<task-id>{agent}</task-id>" not in sent
