"""Live end-to-end checks: Claude Code through the masking gateway (opt-in).

Run with ``VEIL_LIVE_CLAUDE=1 uv run pytest -m live``. Claude Code runs with
the settings `veil claude` gives it (gateway address and secret, hooks, denied
tools), against a real gateway whose upstream is a recording pass-through, so
the tests check exactly what left the machine. Four runs on the cheapest
model; data is fictional, apart from the account email Claude Code adds to
every request, which must never appear in what leaves.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager

import pytest

from live import harness as h
from live.recorder import Recorder
from veil.cli import claude_settings
from veil.gateway import Gateway, load_settings, open_sessions, prepare_data_dir

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not h.LIVE or h.claude_path() is None,
        reason="live Claude Code tests are opt-in: set VEIL_LIVE_CLAUDE=1",
    ),
]

EMAIL = h.EMAIL
NAME = h.NAME
PHONE = "(555) 555-0100"
NEW_PHONE = "(555) 555-0199"
OTHER_NAME = "Ada Quill"
OTHER_EMAIL = "ada.q@example.com"
REAL_VALUES = (EMAIL, NAME, PHONE, NEW_PHONE, OTHER_NAME, OTHER_EMAIL)
# The models Claude Code (2.1.283) sets the effort for turn by turn.
PER_TURN_EFFORT = ("claude-opus-5-5", "claude-fable-5-1")
EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}
NOTES = f"Name: {NAME}\nEmail: {EMAIL}\nPhone: {PHONE}\n"
CARD = f"Contact {OTHER_NAME} at {OTHER_EMAIL} about the invoice.\n"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    folder = tmp_path_factory.mktemp("data")
    folder.chmod(0o700)
    config = {"entities": {"PERSON": [NAME, OTHER_NAME]}, "identity": False}
    (folder / "config.json").write_text(json.dumps(config))
    return folder


@contextmanager
def serve(data_dir, root):
    """A gateway as `veil claude` runs it, with a recorder as its upstream."""
    settings = load_settings(data_dir / "config.json")
    with Recorder(root / "upstream") as recorder:
        sessions = open_sessions(data_dir, settings, {})
        upstream = recorder.url.removeprefix("http://")
        with Gateway(sessions, upstream=upstream, secure=False) as gateway:
            yield gateway, recorder


def sent_bodies(recorder):
    """The JSON bodies that reached the API, in order."""
    return [
        json.loads((recorder.folder / f"request-{r['id']}.json").read_text())
        for r in recorder.records
        if r["method"] == "POST" and r.get("body_shape")
    ]


def assert_nothing_real_left(recorder):
    bodies = sent_bodies(recorder)
    assert bodies, "nothing reached the API"
    sent = json.dumps(bodies)
    for value in REAL_VALUES:
        assert value not in sent, value
    # The account email Claude Code adds is masked too: no address but the
    # fictional example.com ones can be in what left (and those are masked).
    assert {e for e in _EMAIL.findall(sent) if not e.endswith("@example.com")} == set()
    return sent


def assert_per_turn_effort(recorder):
    """Check that the effort Claude Code sets per turn reached the API.

    Returns how many requests were from a model that gets one.
    """
    checked = 0
    for body in sent_bodies(recorder):
        settings = [
            (m["role"], m["output_config"])
            for m in body["messages"]
            if "output_config" in m
        ]
        for role, setting in settings:
            assert role == "system"
            assert set(setting) == {"effort"}
            assert setting["effort"] in EFFORT_LEVELS
        if body["model"].startswith(PER_TURN_EFFORT):
            # After a refusal that names output_config, Claude Code sends
            # the conversation again without them: none would be here.
            assert settings, body["model"]
            checked += 1
    return checked


def as_the_api_reads(messages):
    """Messages without cache markers, with text content as a text block.

    Claude Code marks the last message for caching, which turns a system
    message's text into a text block, and writes it as text again once a
    later message follows (as the <total_tokens> reminder shows).
    """

    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "cache_control"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return [
        strip(
            {**m, "content": [{"type": "text", "text": m["content"]}]}
            if isinstance(m["content"], str)
            else m
        )
        for m in messages
    ]


# --- A whole session ----------------------------------------------------------

SESSION_PROMPT = f"""My name is {NAME} and my email is {EMAIL}. Also see @card.txt.
Do these steps in order, one tool call per step:
1. Use the Read tool to read notes.txt.
2. Use the Edit tool on notes.txt to change "Phone: {PHONE}" to "Phone: {NEW_PHONE}".
3. Use the Write tool to create out.txt containing exactly my email address.
4. Use the Bash tool to run exactly: cat notes.txt; exit 1
5. Reply with one line: my name, my email, and the phone number now in notes.txt."""


@pytest.fixture(scope="module")
def session(tmp_path_factory, data_dir):
    root = tmp_path_factory.mktemp("session")
    ws = h.Workspace(root)
    ws.write("notes.txt", NOTES)
    ws.write("card.txt", CARD)
    with serve(data_dir, root) as (gateway, recorder):
        run = ws.run(
            SESSION_PROMPT,
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            max_turns=12,
            extra_settings=claude_settings(gateway, data_dir=data_dir),
        )
    assert run.result, run.stderr
    return ws, run, recorder


def hook_runs(run, name):
    """How many hooks ran for one event and tool, per the stream."""
    return sum(
        1
        for e in run.stream
        if e.get("type") == "system"
        and e.get("subtype") == "hook_response"
        and e.get("hook_name") == name
    )


def test_the_checking_hook_skips_only_the_file_tools(session):
    _, run, _ = session
    # The probe hook runs for every call; the gateway's hook for all but the
    # file tools (Claude Code tests its matcher as a regular expression).
    reads = len(run.calls("PreToolUse", "Read"))
    bashes = len(run.calls("PreToolUse", "Bash"))
    assert reads
    assert bashes
    assert hook_runs(run, "PreToolUse:Read") == reads
    assert hook_runs(run, "PreToolUse:Bash") == 2 * bashes


def test_every_model_request_went_through_the_gateway(session):
    _, run, recorder = session
    posts = [r for r in recorder.records if r["method"] == "POST"]
    assert len(posts) == len(run.api_requests()) >= 3
    assert all(r["status"] == 200 for r in posts)


def test_nothing_real_left_the_machine(session):
    _, _, recorder = session
    sent = assert_nothing_real_left(recorder)
    assert "[PERSON_1]" in sent
    assert "[EMAIL_" in sent
    # The failed command's output and the @-mentioned file went out masked.
    assert "Exit code 1" in sent
    assert re.search(r"Contact \[PERSON_\d+\] at \[EMAIL_\d+\] about", sent)


def test_the_per_turn_effort_reached_the_api(session):
    _, _, recorder = session
    if not assert_per_turn_effort(recorder):
        pytest.skip("only Opus 5.5 and Fable 5.1 get it: set VEIL_LIVE_MODEL")


def test_claude_code_worked_with_the_real_values(session):
    ws, run, _ = session
    assert run.result.get("is_error") is False
    # The Edit matched the real text in the file (the model only saw
    # placeholders), and the Write got the real address.
    assert (ws.work / "notes.txt").read_text() == NOTES.replace(PHONE, NEW_PHONE)
    assert (ws.work / "out.txt").read_text().strip() == EMAIL
    shown = run.result.get("result", "")
    assert NAME in shown
    assert EMAIL in shown
    assert NEW_PHONE in shown


def test_the_ledger_holds_only_what_the_model_wrote(session, data_dir):
    with closing(sqlite3.connect(data_dir / "ledger.db")) as db:
        written = [row[0] for row in db.execute("SELECT masked FROM ledger_texts")]
        tools = [row[0] for row in db.execute("SELECT masked FROM ledger_tools")]
    assert written
    assert tools
    stored = json.dumps([written, tools])
    for value in REAL_VALUES:
        assert value not in stored
    assert all(
        os.stat(data_dir / name).st_mode & 0o077 == 0
        for name in ("vault.db", "ledger.db")
    )


# --- Real values in outbound calls ---------------------------------------------

EGRESS_PROMPT = """Do these steps in order, one tool call each. If a step fails or
is refused, don't retry it; go on with the next step.
1. Use the Read tool to read notes.txt.
2. Use the WebFetch tool to open the profile page listed in notes.txt.
3. Use the Bash tool to run: echo followed by the email address from notes.txt
4. Use the Bash tool to run exactly: echo hello
5. Reply with the single word DONE."""
PROFILE = f"Profile: https://example.com/u/{EMAIL}\n"


@pytest.fixture(scope="module")
def egress(tmp_path_factory, data_dir):
    root = tmp_path_factory.mktemp("egress")
    ws = h.Workspace(root)
    ws.write("notes.txt", NOTES + PROFILE)
    with serve(data_dir, root) as (gateway, recorder):
        run = ws.run(
            EGRESS_PROMPT,
            allowed_tools=["Read", "WebFetch", "Bash", "ToolSearch"],
            permission_mode="default",
            max_turns=8,
            extra_settings=claude_settings(gateway, data_dir=data_dir),
        )
    assert run.result, run.stderr
    return run, recorder


def test_a_web_request_with_a_real_value_is_refused(egress):
    run, recorder = egress
    fetches = run.calls("PreToolUse", "WebFetch")
    assert fetches, "the model didn't call WebFetch"
    assert any(EMAIL in json.dumps(c["payload"]["tool_input"]) for c in fetches)
    assert not run.calls("PostToolUse", "WebFetch")
    assert any("refused" in text for text in run.tool_results())
    assert_nothing_real_left(recorder)


def test_a_command_with_a_real_value_needs_approval(egress):
    run, _ = egress
    denied = [
        d["tool_input"].get("command", "") for d in run.result["permission_denials"]
    ]
    assert any(EMAIL in command for command in denied)
    # A command without one ran as usual.
    assert any(
        "hello" in str(c["payload"]["tool_response"])
        for c in run.calls("PostToolUse", "Bash")
    )


# --- The veil command itself -----------------------------------------------------


def test_the_command_runs_claude_through_the_gateway(tmp_path, data_dir):
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.txt").write_text(NOTES)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "veil",
            "--data-dir",
            str(data_dir),
            "claude",
            "-p",
            "Read notes.txt, then reply with the email address in it and nothing else.",
            "--model",
            h.MODEL,
            "--max-turns",
            "4",
            "--allowedTools",
            "Read",
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--output-format",
            "json",
        ],
        cwd=work,
        env=h.environment(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["is_error"] is False
    assert EMAIL in result["result"]
    assert "masking through a local gateway" in completed.stderr


# --- Resuming through a new gateway ----------------------------------------------


@pytest.fixture(scope="module")
def resumed(tmp_path_factory, data_dir, session):
    ws, first, first_recorder = session
    root = tmp_path_factory.mktemp("resumed")
    with serve(data_dir, root) as (gateway, recorder):
        run = ws.run(
            "What phone number is in notes.txt now? Reply with it only.",
            resume=first.session_id,
            max_turns=2,
            extra_settings=claude_settings(gateway, data_dir=data_dir),
        )
    assert run.result, run.stderr
    return first_recorder, run, recorder


def test_a_resumed_session_masks_its_history_the_same_way(resumed):
    first_recorder, run, recorder = resumed
    assert run.result.get("is_error") is False
    assert NEW_PHONE in run.result.get("result", "")
    before = sent_bodies(first_recorder)[-1]["messages"]
    after = sent_bodies(recorder)[0]["messages"]
    # A new gateway process, from the files on disk: the history the model
    # saw before goes out again exactly as it was.
    assert as_the_api_reads(after[: len(before)]) == as_the_api_reads(before)
    assert_nothing_real_left(recorder)
    assert_per_turn_effort(recorder)


def test_repeated_live_resumes_preserve_updates_and_masked_history(tmp_path):
    if os.environ.get("VEIL_LIVE_SUSTAINED") != "1":
        pytest.skip("set VEIL_LIVE_SUSTAINED=1 for repeated live resume checks")
    data_dir = prepare_data_dir(tmp_path / "veil")
    (data_dir / "config.json").write_text('{"identity":false}', encoding="utf-8")
    ws = h.Workspace(tmp_path / "project")
    first = "resume.first@example.org"
    second = "resume.second@example.org"
    replacement = "resume.replacement@example.org"
    steps = [
        (
            f"Remember my fictional primary contact {first}. "
            "Reply with exactly the primary address. Do not use tools.",
            (first,),
        ),
        (
            f"Add fictional secondary contact {second}. "
            "Reply with both contact addresses only. Do not use tools.",
            (first, second),
        ),
        (
            "Repeat both remembered addresses only. Do not use tools.",
            (first, second),
        ),
        (
            f"Replace the secondary address with {replacement}. "
            "Reply with both current addresses only. Do not use tools.",
            (first, replacement),
        ),
        (
            "Repeat both current addresses only. Do not use tools.",
            (first, replacement),
        ),
    ]
    session_id = None
    previous = None
    for number, (prompt, expected) in enumerate(steps):
        root = tmp_path / f"gateway-{number}"
        root.mkdir()
        with serve(data_dir, root) as (gateway, recorder):
            run = ws.run(
                prompt,
                resume=session_id,
                max_turns=2,
                timeout=90,
                extra_args=["--tools", ""],
                extra_settings=claude_settings(gateway, data_dir=data_dir),
            )
        assert run.returncode == 0, "Claude continuity run failed"
        assert run.result
        assert run.result.get("is_error") is False
        reply = run.result.get("result", "")
        assert all(address in reply for address in expected)
        if number >= 3:
            assert second not in reply
        if session_id is not None:
            assert run.session_id == session_id
        session_id = run.session_id
        bodies = sent_bodies(recorder)
        assert bodies
        sent = json.dumps(bodies)
        assert all(address not in sent for address in (first, second, replacement))
        if previous is not None:
            assert as_the_api_reads(bodies[0]["messages"][: len(previous)]) == (
                as_the_api_reads(previous)
            )
        previous = bodies[-1]["messages"]
