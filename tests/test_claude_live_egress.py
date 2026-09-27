"""Live checks of what reaches the API, "ask", and other tools' shapes (opt-in).

Run with ``VEIL_LIVE_CLAUDE=1 uv run pytest -m live``. Two headless runs on the
cheapest model, each shared by the tests that check it. The first restores
placeholders into Bash commands, twice with ``permissionDecision: "ask"``, in a
project whose settings set ``env``; the second fetches a web page, calls an MCP
tool, and @-mentions a file. The harness saves every model request body as it
was sent, which is the ground truth for what left the machine.

Checked against Claude Code 2.1.283.
"""

import json
import re
from datetime import datetime
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

MASK = {h.EMAIL: "[EMAIL_1]", h.PHONE: "[PHONE_1]", h.NAME: "[NAME_1]"}
REAL = tuple(MASK)


def answer_of(record):
    """The ``hookSpecificOutput`` the probe answered a call with (or {})."""
    return (record.get("response") or {}).get("hookSpecificOutput") or {}


def text_of(content):
    """The text of a tool result's content: a string or a list of blocks."""
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")) for block in content if isinstance(block, dict)
        )
    return content if isinstance(content, str) else ""


def tool_result_blocks(content):
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def results_sent(run):
    """Map each tool_use_id to the text of every tool_result the API was sent."""
    sent = {}
    for body in run.api_bodies():
        try:
            request = json.loads(body["request"])
        except ValueError:
            continue
        for message in request.get("messages") or []:
            for block in tool_result_blocks(message.get("content")):
                text = text_of(block.get("content"))
                sent.setdefault(block.get("tool_use_id"), []).append(text)
    return sent


def results_kept(run):
    """Map each tool_use_id to the transcript's (record, tool_result) pairs."""
    kept = {}
    for record in run.transcript():
        content = (record.get("message") or {}).get("content")
        for block in tool_result_blocks(content):
            kept.setdefault(block.get("tool_use_id"), []).append((record, block))
    return kept


def request_sources(run):
    """The query source of each model request in the debug log, in order."""
    sources = []
    for line in run.api_requests():
        match = re.search(r"\bsource=(\S+)", line)
        sources.append(match.group(1) if match else "")
    return sources


def request_times(run, source):
    """When each model request from ``source`` was sent (epoch seconds)."""
    times = []
    for line in run.api_requests():
        if re.search(rf"\bsource={re.escape(source)}\b", line):
            stamp = line.split(" ", 1)[0].replace("Z", "+00:00")
            times.append(datetime.fromisoformat(stamp).timestamp())
    return times


# --- Restored Bash commands, "ask", and project settings env -----------------

RESTORE_PROMPT = (
    "Do these steps in order. Use exactly one Bash tool call per step and run"
    " each command exactly as written: the bracketed tokens such as [EMAIL_1] are"
    " literal text, keep them unchanged. Do not combine steps, and go on to the"
    " next step even if a step fails or is denied.\n"
    "Step 1: Bash: echo 'contact [EMAIL_1]'\n"
    "Step 2: Bash: printf 'ask [PHONE_1]'\n"
    "Step 3: Bash: echo 'allowed-ask [NAME_1]'\n"
    "Then reply with one line per step: the step number and the exact output or"
    " error text you got."
)

ASK_REASON = "veil-probe: restored values need approval"
RESTORE_RULES = [
    {
        "event": "PreToolUse",
        "tool": "Bash",
        "contains": "allowed-ask",
        "replace_input": {"[NAME_1]": h.NAME},
        "decision": "ask",
        "reason": ASK_REASON,
    },
    {
        "event": "PreToolUse",
        "tool": "Bash",
        "contains": "printf",
        "replace_input": {"[PHONE_1]": h.PHONE},
        "decision": "ask",
        "reason": ASK_REASON,
    },
    {"event": "PreToolUse", "tool": "Bash", "replace_input": {"[EMAIL_1]": h.EMAIL}},
    {"event": "PostToolUse", "tool": "Bash", "replace_output": MASK},
]

# Written by a sitecustomize.py that the project's PYTHONPATH points at.
MARKER = "sitecustomize-ran.jsonl"
FAKE_HOME = "fake-home"


@pytest.fixture(scope="module")
def restored(tmp_path_factory):
    ws = h.Workspace(tmp_path_factory.mktemp("restored"))
    ws.write("notes.txt", h.NOTES)
    inject = ws.root / "pyinject"
    inject.mkdir()
    (inject / "sitecustomize.py").write_text(
        "import json, os\n"
        "try:\n"
        f"    with open({str(ws.root / MARKER)!r}, 'a') as f:\n"
        "        f.write(json.dumps({'pid': os.getpid()}) + '\\n')\n"
        "except OSError:\n"
        "    pass\n",
        encoding="utf-8",
    )
    env = {
        "VEIL_PROBE_FROM_PROJECT": "yes",
        "HOME": str(ws.root / FAKE_HOME),
        "PYTHONPATH": str(inject),
    }
    ws.write(".claude/settings.json", json.dumps({"env": env}))
    run = ws.run(
        RESTORE_PROMPT,
        rules=RESTORE_RULES,
        allowed_tools=["Bash(echo:*)"],
        permission_mode="default",
        max_turns=8,
    )
    assert run.returncode == 0, run.stderr
    return ws, run


def calls_restoring(run, value):
    """The PreToolUse Bash calls whose answer put ``value`` into the command."""
    return [
        record
        for record in run.calls("PreToolUse", "Bash")
        if value in str((answer_of(record).get("updatedInput") or {}).get("command"))
    ]


def test_a_restored_command_makes_no_extra_model_request(restored):
    _, run = restored
    ids = {record["payload"]["tool_use_id"] for record in calls_restoring(run, h.EMAIL)}
    assert ids, "the model didn't run step 1"
    # The restored command is what ran: its real output reached PostToolUse.
    ran = [
        record
        for record in run.calls("PostToolUse", "Bash")
        if record["payload"]["tool_use_id"] in ids
    ]
    assert any(h.EMAIL in json.dumps(r["payload"]["tool_response"]) for r in ran)
    # Every request was a main-loop turn (no classifier or side query saw the
    # restored command), and the saved bodies cover all of them, so the body
    # checks in these tests miss nothing.
    sources = request_sources(run)
    assert set(sources) == {"sdk"}
    assert [body["query_source"] for body in run.api_bodies()] == sources
    # One of them came after the restore and carried its result.
    assert ids & set(results_sent(run))


def test_restored_values_reach_no_request_body(restored):
    _, run = restored
    sent = run.sent_to_api()
    assert sent, "no request bodies were saved"
    for value in REAL:
        assert calls_restoring(run, value), f"no command had {value!r} restored"
        assert value not in sent
    # The API gets the restored command's masked output, not the real one.
    results = results_sent(run)
    texts = [
        text
        for record in calls_restoring(run, h.EMAIL)
        for text in results.get(record["payload"]["tool_use_id"], [])
    ]
    assert texts
    assert all("[EMAIL_1]" in text for text in texts)


def test_restored_values_are_kept_in_local_records(restored):
    _, run = restored
    ids = {record["payload"]["tool_use_id"] for record in calls_restoring(run, h.EMAIL)}
    assert ids, "the model didn't run step 1"
    # The hook's answer, restored value and all, is copied to the stream (the
    # harness passes --include-hook-events), the transcript's hook_success
    # attachments, and the debug log: local copies veil has to document.
    echoed = [
        event
        for event in run.stream
        if event.get("subtype") == "hook_response"
        and event.get("hook_event") == "PreToolUse"
    ]
    assert any(h.EMAIL in str(event.get("stdout")) for event in echoed)
    kept = [a for a in run.attachments("hook_success") if a.get("toolUseID") in ids]
    assert any(h.EMAIL in str(attachment.get("stdout")) for attachment in kept)
    assert h.EMAIL in run.debug
    # The transcript's copy of the result is the masked one: the command's raw
    # output is never stored.
    results = results_kept(run)
    pairs = [pair for tool_use_id in ids for pair in results.get(tool_use_id, [])]
    assert pairs
    for record, block in pairs:
        assert "[EMAIL_1]" in text_of(block.get("content"))
        assert h.EMAIL not in json.dumps(block)
        assert h.EMAIL not in json.dumps(record.get("toolUseResult"))


def test_ask_with_updated_input_is_denied_in_print_mode(restored):
    _, run = restored
    asked = {
        record["payload"]["tool_use_id"]: str(
            (answer_of(record).get("updatedInput") or {}).get("command")
        )
        for record in run.calls("PreToolUse", "Bash")
        if answer_of(record).get("permissionDecision") == "ask"
    }
    # Each "ask" came with a restored input.
    assert any(h.PHONE in c for c in asked.values()), "the model didn't run step 2"
    # Step 3 is an echo, which Bash(echo:*) allows: "ask" still wins.
    assert any(h.NAME in c for c in asked.values()), "the model didn't run step 3"
    # -p can't show a prompt, and neither permission hook runs.
    assert run.calls("PermissionRequest") == []
    assert run.calls("PermissionDenied") == []
    denied = {
        event.get("tool_use_id"): event
        for event in run.stream
        if event.get("type") == "system" and event.get("subtype") == "permission_denied"
    }
    denials = {
        denial.get("tool_use_id"): denial
        for denial in run.result.get("permission_denials") or []
    }
    ran = {record["payload"]["tool_use_id"] for record in run.calls("PostToolUse")}
    kept = results_kept(run)
    sent = results_sent(run)
    for tool_use_id, command in asked.items():
        event = denied.get(tool_use_id) or {}
        assert event.get("decision_reason_type") == "hook"
        assert event.get("decision_reason") == ASK_REASON
        assert tool_use_id not in ran
        # The result JSON records the restored command, real value and all.
        denial = denials.get(tool_use_id) or {}
        assert (denial.get("tool_input") or {}).get("command") == command
        # The model is told only the hook's reason.
        blocks = [block for _, block in kept.get(tool_use_id, [])]
        assert blocks
        assert all(block.get("content") == ASK_REASON for block in blocks)
        assert all(block.get("is_error") is True for block in blocks)
        texts = sent.get(tool_use_id)
        assert texts
        assert all(ASK_REASON in text and command not in text for text in texts)


def test_project_env_reaches_hook_processes_but_not_home(restored):
    ws, run = restored
    # A repository's settings can set any variable in veil's hook process, so
    # the hook must not take a config or vault path from its environment.
    # HOME is the exception: the CLI ignores it, with a warning.
    assert run.hooks
    for record in run.hooks:
        env = record.get("env") or {}
        assert env.get("VEIL_PROBE_FROM_PROJECT") == "yes", record["event"]
        assert env.get("HOME") == str(Path.home()), record["event"]
    assert re.search(r"HOME in \S*settings\S* is ignored", run.debug)
    assert not (ws.root / FAKE_HOME).exists()


def test_project_pythonpath_runs_code_in_every_hook(restored):
    ws, run = restored
    # A repository's settings can inject a sitecustomize.py into veil's hook
    # process, so the hook must not trust the Python environment it inherits.
    marker = ws.root / MARKER
    assert marker.exists(), "the planted sitecustomize.py never ran"
    lines = marker.read_text(encoding="utf-8").splitlines()
    started = {json.loads(line)["pid"] for line in lines if line.strip()}
    pids = {record["pid"] for record in run.hooks}
    assert pids
    assert pids <= started


# --- WebFetch, an MCP tool, and an @-mentioned file ---------------------------

TOOLS_PROMPT = (
    "@notes.txt\n"
    "\n"
    "Do these steps in order, one tool call per step:\n"
    "Step 1: Use the WebFetch tool with url https://example.com and prompt"
    ' "Quote the page title and its first sentence verbatim."\n'
    'Step 2: Call the mcp__contacts__find_contact tool with name "Jan".\n'
    "Then reply with exactly three lines and nothing else:\n"
    "WEB: <the page title exactly as the WebFetch result gave it>\n"
    "MCP: <the Email line exactly as the find_contact result gave it>\n"
    "FILE: <the Email line of the attached file notes.txt exactly as it appears"
    " there>"
)
MCP_TOOL = "mcp__contacts__find_contact"

# The @-mentioned file holds a different card from the MCP server's (h.NOTES),
# so where each value came from can be told apart.
FILE_CARD = "Name: Ada Quill\nEmail: ada.q@example.com\nPhone: 555-0142\n"
FILE_VALUES = ("ada.q@example.com", "555-0142", "Ada Quill")

# A fixed replacement in WebFetch's own shape, so the check doesn't depend on
# how the model summarized the page.
WEB_SENTINEL = "SENTINEL-WEB"
WEB_OUTPUT = {
    "bytes": 559,
    "code": 200,
    "codeText": "OK",
    "result": f"Page title: {WEB_SENTINEL}",
    "durationMs": 783,
    "url": "https://example.com",
}
# The page's own title: in the fetched HTML, whatever the model says about it.
PAGE_TEXT = "Example Domain"

TOOLS_RULES = [
    # If the model reads the file itself, the content must not reach it through
    # a tool, or the @-mention check below would prove nothing.
    {
        "event": "PreToolUse",
        "tool": "Read",
        "respond": {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "veil-probe: use the attached file",
            }
        },
    },
    {
        "event": "PostToolUse",
        "tool": "WebFetch",
        "respond": {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": WEB_OUTPUT,
            }
        },
    },
    {"event": "PostToolUse", "tool": MCP_TOOL, "replace_output": MASK},
]

# Events whose payload carries the model's own reply, which may quote the file.
REPLY_EVENTS = frozenset({"MessageDisplay", "Stop"})


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    ws = h.Workspace(tmp_path_factory.mktemp("tools"))
    ws.write("notes.txt", FILE_CARD)
    run = ws.run(
        TOOLS_PROMPT,
        rules=TOOLS_RULES,
        allowed_tools=["WebFetch", MCP_TOOL],
        permission_mode="default",
        mcp=True,
        max_turns=6,
    )
    assert run.returncode == 0, run.stderr
    return run


def fetches(run):
    """WebFetch's PostToolUse calls; skips the test if every fetch failed."""
    calls = run.calls("PostToolUse", "WebFetch")
    if not calls and run.calls("PostToolUseFailure", "WebFetch"):
        pytest.skip("WebFetch failed (network or domain check), nothing to check")
    assert calls, "the model didn't call WebFetch"
    return calls


def test_webfetch_output_is_replaced_with_the_same_shape(tools):
    run = tools
    batch = {call.get("tool_use_id"): call for call in run.batch_calls("WebFetch")}
    sent = results_sent(run)
    for record in fetches(run):
        payload = record["payload"]
        output = payload["tool_response"]
        assert {"result", "url", "code"} <= set(output)
        raw = output["result"]
        assert raw
        assert raw != WEB_OUTPUT["result"]
        # The model gets the replacement's result text, and so does the API.
        assert payload["tool_use_id"] in batch
        seen = text_of(batch[payload["tool_use_id"]]["tool_response"])
        texts = sent.get(payload["tool_use_id"])
        assert texts
        for text in [seen, *texts]:
            assert WEB_SENTINEL in text
            assert raw not in text
            assert PAGE_TEXT not in text


def test_webfetch_sends_the_raw_page_to_its_own_model_request(tools):
    run = tools
    calls = fetches(run)
    # WebFetch asks a model about the page itself, with the raw page in the
    # request, so no hook can mask a fetched page before a model reads it.
    pages = [
        body["request"]
        for body in run.api_bodies()
        if body["query_source"] == "web_fetch_apply"
    ]
    assert any(PAGE_TEXT in page for page in pages)
    # That request goes out between PreToolUse, which sees only the url and
    # prompt, and PostToolUse, the first hook that sees the page's content.
    sent_at = request_times(run, "web_fetch_apply")
    assert sent_at
    for post in calls:
        tool_use_id = post["payload"]["tool_use_id"]
        pres = [
            record
            for record in run.calls("PreToolUse", "WebFetch")
            if record["payload"]["tool_use_id"] == tool_use_id
        ]
        assert pres
        assert any(pre["t1"] <= t <= post["t0"] for pre in pres for t in sent_at)


def test_mcp_output_is_replaced_as_a_list_of_text_blocks(tools):
    run = tools
    calls = run.calls("PostToolUse", MCP_TOOL)
    assert calls, "the model didn't call the MCP tool"
    batch = {call.get("tool_use_id"): call for call in run.batch_calls(MCP_TOOL)}
    sent = results_sent(run)
    for record in calls:
        payload = record["payload"]
        assert (payload.get("mcp_server") or {}).get("name") == "contacts"
        blocks = payload["tool_response"]
        assert isinstance(blocks, list)
        assert blocks
        assert all(block.get("type") == "text" for block in blocks)
        # The hook gets the server's real card...
        assert all(value in text_of(blocks) for value in REAL)
        # ...and the model and the API get the list the hook sent back.
        assert payload["tool_use_id"] in batch
        seen = text_of(batch[payload["tool_use_id"]]["tool_response"])
        texts = sent.get(payload["tool_use_id"])
        assert texts
        for text in [seen, *texts]:
            assert "[EMAIL_1]" in text
            assert not any(value in text for value in REAL)
    # The file holds another card, so the MCP card's values reach no request.
    sent_all = run.sent_to_api()
    assert not any(value in sent_all for value in REAL)


def test_an_at_mentioned_file_reaches_the_api_without_any_tool_hook(tools):
    run = tools
    prompts = run.calls("UserPromptSubmit")
    assert prompts
    # UserPromptSubmit gets the literal @notes.txt, not the file's content.
    assert prompts[0]["payload"]["prompt"] == TOOLS_PROMPT
    # The CLI reads the file itself, keeps it as a transcript attachment...
    files = [json.dumps(a, ensure_ascii=False) for a in run.attachments("file")]
    assert any(all(value in text for value in FILE_VALUES) for text in files)
    # ...and sends it in the same request as the prompt.
    quoted = json.dumps(TOOLS_PROMPT)[1:-1]
    carriers = [
        body["request"] for body in run.api_bodies() if quoted in body["request"]
    ]
    assert carriers
    assert all(value in body for body in carriers for value in FILE_VALUES)
    # No hook that could mask it ever saw the content.
    checked = [record for record in run.hooks if record["event"] not in REPLY_EVENTS]
    events = {record["event"] for record in checked}
    assert {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolBatch"} <= events
    for record in checked:
        text = json.dumps(record["payload"], ensure_ascii=False)
        assert not any(value in text for value in FILE_VALUES), record["event"]
