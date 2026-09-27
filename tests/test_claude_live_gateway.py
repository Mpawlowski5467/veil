"""Live checks of what Claude Code sends through a gateway (opt-in).

Run with ``VEIL_LIVE_CLAUDE=1 uv run pytest -m live``. These pin the facts a
masking gateway set with ``ANTHROPIC_BASE_URL`` relies on: that every model
request goes through it, with the full conversation each time; which
endpoints, headers, and body fields it must understand; and that settings
passed on the command line decide where requests go.

``tests/gateway_payloads/`` holds the census of body fields and reply events
recorded from Claude Code 2.1.283 (shapes only). A test here fails when a
live run uses a field or event type the census doesn't list, which means the
gateway's list of known fields needs updating.
"""

import json
from pathlib import Path

import pytest

from live import harness as h
from live.recorder import Recorder, census

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not h.LIVE or h.claude_path() is None,
        reason="live Claude Code tests are opt-in: set VEIL_LIVE_CLAUDE=1",
    ),
]

PAYLOADS = Path(__file__).parent / "gateway_payloads"
KNOWN_REQUEST = json.loads((PAYLOADS / "request_census.json").read_text())
KNOWN_RESPONSE = json.loads((PAYLOADS / "response_census.json").read_text())
MESSAGES = "POST /v1/messages"


def model_requests(run):
    return [
        r for r in run.gateway if r["method"] == "POST" and "/v1/messages" in r["path"]
    ]


def endpoint(record):
    return f"{record['method']} {record['path'].split('?')[0]}"


def request_bodies(run):
    """The JSON bodies Claude Code sent, in order, as the gateway saved them."""
    folder = run.dir / "gateway"
    return [
        json.loads((folder / f"request-{r['id']}.json").read_text(encoding="utf-8"))
        for r in model_requests(run)
    ]


def unknown(shape, known):
    """Paths or kinds in ``shape`` that the recorded census doesn't list."""
    missing = {}
    for path, kinds in shape.items():
        if path.startswith("$.tools[].input_schema."):
            continue  # tool schemas are Claude Code's own definitions
        if ".input." in path and path.startswith("$.messages[]"):
            continue  # tool inputs vary by tool
        new = set(kinds) - set(known.get(path, {}).get("kinds", []))
        if new:
            missing[path] = sorted(new)
    return missing


# --- One session through a recording gateway ---------------------------------

SESSION_PROMPT = """Do these steps in order, one tool call per step:
1. Use the Read tool to read notes.txt.
2. Use the Bash tool to run exactly: cat notes.txt; exit 1
3. Use the Agent tool with the general-purpose subagent, run_in_background
   false, description "fg read", prompt "Read notes.txt and reply with its
   Email line only".
4. Reply with the single word DONE."""


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    ws = h.Workspace(tmp_path_factory.mktemp("gateway_session"))
    ws.write("notes.txt", h.NOTES)
    run = ws.run(
        SESSION_PROMPT,
        allowed_tools=["Read", "Bash", "Agent"],
        max_turns=10,
        gateway=True,
    )
    assert run.result, run.stderr
    assert model_requests(run), "no request went through the gateway"
    return run


def test_every_model_request_goes_through_the_gateway(session):
    # The debug log counts every model request the CLI made; the gateway saw
    # each of them, so nothing reached the API another way.
    assert len(model_requests(session)) == len(session.api_requests())
    assert {endpoint(r) for r in session.gateway} <= {MESSAGES, "HEAD /api/hello"}


def test_each_request_carries_the_whole_conversation(session):
    bodies = [b for b in request_bodies(session) if "Do these steps" in json.dumps(b)]
    assert len(bodies) >= 3
    counts = [len(b["messages"]) for b in bodies]
    assert counts == sorted(counts)
    assert counts[-1] > counts[0]
    # No server-side thread: the gateway sees, and must mask, every message.
    assert all("thread" not in b for b in bodies)
    # The failed command's output is in the next request, like any result.
    assert any("Exit code 1" in json.dumps(b) for b in bodies)


def test_requests_name_their_session_and_subagent(session):
    requests = model_requests(session)
    assert {r["headers"].get("x-claude-code-session-id") for r in requests} == {
        session.session_id
    }
    assert any("x-claude-code-agent-id" in r["headers"] for r in requests)
    for body in request_bodies(session):
        user = json.loads(body["metadata"]["user_id"])
        assert user["session_id"] == session.session_id


def test_a_claude_ai_login_passes_through_as_a_bearer_token(session):
    requests = model_requests(session)
    if any("x-api-key" in r["header_names"] for r in requests):
        pytest.skip("logged in with an API key, not a claude.ai account")
    assert all("authorization" in r["header_names"] for r in requests)
    assert all("oauth-" in r["headers"].get("anthropic-beta", "") for r in requests)


def test_bodies_and_replies_use_only_known_fields(session):
    assert unknown(census(request_bodies(session)), KNOWN_REQUEST[MESSAGES]) == {}
    for record in model_requests(session):
        shape = record.get("response_shape", {})
        assert unknown(shape, KNOWN_RESPONSE[MESSAGES]) == {}, record["id"]


# --- Which settings decide where requests go ---------------------------------


@pytest.fixture(scope="module")
def precedence(tmp_path_factory):
    root = tmp_path_factory.mktemp("gateway_precedence")
    ws = h.Workspace(root)
    # A project's settings and the process environment both name a dead port.
    ws.write(
        ".claude/settings.json",
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}}),
    )
    with Recorder(root / "gateway") as gateway:
        run = ws.run(
            "Reply with the single word OK.",
            max_turns=2,
            extra_env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:7"},
            extra_settings={"env": {"ANTHROPIC_BASE_URL": gateway.url}},
            timeout=180,
        )
    run.gateway = gateway.records
    return run


def test_command_line_settings_decide_the_base_url(precedence):
    # So `--settings` can route a session through the gateway, whatever the
    # project's settings or the shell say.
    assert precedence.result.get("is_error") is False
    assert len(model_requests(precedence)) == len(precedence.api_requests()) >= 1
