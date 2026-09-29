"""The census script's own logic, without Claude Code or a model.

The census (``tests/live/census.py``) runs Claude Code through the gateway
and compares what it sends with ``tests/gateway_payloads/``. These tests
check its parts: shapes and what counts as new, merging the census files,
moving the tested version, the leak and refusal checks, the settle rule, and
a whole chain against a fake API.
"""

import http.client
import http.server
import json
import re
import threading

import pytest

from live import census as c
from live.recorder import census as shape_of
from live.terminal import chunks, fold

EMAIL = "jane.doe@example.com"


# --- Shapes and what is new ----------------------------------------------------


def test_tool_schemas_are_not_listed():
    shape = shape_of(
        [{"tools": [{"name": "Read", "input_schema": {"properties": {"x": {}}}}]}]
    )
    collapsed = c.collapse(shape)
    assert "$.tools[].input_schema" in collapsed
    assert not [p for p in collapsed if p.startswith("$.tools[].input_schema.")]


def test_new_paths_kinds_and_types():
    known = {
        "$": {"kinds": ["object"]},
        "$.messages[].content[].type": {"kinds": ["type=text"]},
    }
    shape = {
        "$": ["object"],
        "$.brand_new": ["string"],
        "$.messages[].content[].type": ["type=text", "type=future_block"],
        "$.messages[].content[].input.anything": ["string"],
    }
    assert c.new_paths(shape, known) == {
        "$.brand_new": ["string"],
        "$.messages[].content[].type": ["type=future_block"],
    }
    assert c.new_paths({"$": ["object"]}, known) == {}
    reply = {"$.content_block.input.q": ["string"]}
    assert c.new_paths(reply, {}, reply=True) == {}


def observed(path, kinds, label):
    into = {}
    c.observe(into, c.MESSAGES, {path: kinds}, label)
    return into


def test_merging_adds_scenarios_and_only_accepted_paths():
    old = {c.MESSAGES: {"$": {"kinds": ["object"], "scenarios": ["tools/run-1"]}}}
    seen = observed("$", ["object", "array"], "haiku/print")
    c.observe(seen, c.MESSAGES, {"$.new": ["string"]}, "haiku/print")
    kept = c.merge_census(old, seen, accept_new=False)
    assert kept == {
        c.MESSAGES: {
            "$": {"kinds": ["object"], "scenarios": ["haiku/print", "tools/run-1"]}
        }
    }
    accepted = c.merge_census(old, seen, accept_new=True)
    assert accepted[c.MESSAGES]["$"]["kinds"] == ["array", "object"]
    assert accepted[c.MESSAGES]["$.new"] == {
        "kinds": ["string"],
        "scenarios": ["haiku/print"],
    }
    assert c.merge_census(accepted, seen, accept_new=True) == accepted


@pytest.mark.parametrize("name", ["request_census.json", "response_census.json"])
def test_merging_nothing_keeps_the_committed_census_byte_for_byte(name):
    text = (c.PAYLOADS / name).read_text()
    merged = c.merge_census(json.loads(text), {}, accept_new=True)
    assert c.dump(merged) == text


def test_merging_nothing_keeps_the_endpoints_byte_for_byte():
    text = (c.PAYLOADS / "endpoints.json").read_text()
    merged = c.merge_endpoints(json.loads(text), c.Endpoints(), accept_new=True)
    assert c.dump(merged) == text


def front_record(**extra):
    return {
        "method": "POST",
        "path": "/v1/messages?beta=true",
        "header_names": ["anthropic-beta", c.SECRET, "x-new-header"],
        "headers": {"anthropic-beta": "oauth-2025-04-20, new-beta-2026-10-01"},
        "status": 200,
        "response_type": "text/event-stream; charset=utf-8",
        **extra,
    }


def test_endpoints_headers_betas_and_tools():
    known = json.loads((c.PAYLOADS / "endpoints.json").read_text())
    seen = c.Endpoints()
    body = {
        "tools": [
            {"name": "Read"},
            {"name": "BrandNewTool"},
            {"name": "mcp__contacts__find_contact"},
            {"name": "StructuredOutput"},
        ]
    }
    seen.add(front_record(), body, "haiku/print")
    seen.add(
        {**front_record(), "method": "GET", "path": "/_gateway/proof?n=1"}, None, "x"
    )
    new = c.new_endpoint_things(seen, known)
    assert not [line for line in new if "_gateway" in line]
    assert "request header x-new-header" in new
    assert "beta new-beta-2026-10-01" in new
    assert f"request header {c.SECRET}" not in new
    assert "tool BrandNewTool" in new
    assert not [line for line in new if "mcp__" in line or "Structured" in line]
    merged = c.merge_endpoints(known, seen, accept_new=True)
    assert (
        merged["endpoints"][c.MESSAGES]["count"]
        == known["endpoints"][c.MESSAGES]["count"]
    )
    assert "new-beta-2026-10-01" in merged["anthropic_beta"]
    assert c.SECRET not in merged["request_headers"]
    assert {"BrandNewTool", "Read"} <= set(merged["tools"])
    unchanged = c.merge_endpoints(known, seen, accept_new=False)
    assert "new-beta-2026-10-01" not in unchanged["anthropic_beta"]
    assert "x-new-header" not in unchanged["request_headers"]


# --- Protocol words ------------------------------------------------------------


def test_words_leave_out_what_others_wrote():
    body = {
        "model": "m",
        "context_management": {"edits": [{"type": "clear_thinking_20251015"}]},
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "input": {"user_supplied_key": 1},
                    }
                ],
            }
        ],
        "tools": [
            {"name": "Read", "input_schema": {"properties": {"file_path": {}}}},
            {"name": "mcp__x__y", "input_schema": {"properties": {"server_key": {}}}},
            {"name": "StructuredOutput", "input_schema": {"properties": {"email": {}}}},
        ],
        "metadata": {"jane_notes": 1, "path/like": 2},
    }
    found = c.words(body)
    assert {"context_management", "clear_thinking_20251015", "file_path"} <= found
    assert not found & {"user_supplied_key", "server_key", "email", "jane_notes"}
    assert "path/like" not in found


def test_the_protocol_words_file_round_trips():
    text = c.VOCAB.read_text()
    assert c.vocab_source(text, []) == text
    added = c.vocab_source(text, ["zzz_new_word"])
    assert '        "zzz_new_word",\n' in added
    assert c.vocab_source(added, ["zzz_new_word"]) == added


# --- Versions ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "moved"),
    [
        ("2.1.283", "2.1.290", True),
        ("2.1.290", "2.1.1000", True),
        ("2.1.283", "2.1.99", False),
        ("2.1.283", "2.1.283", False),
    ],
)
def test_the_tested_version_only_moves_forward(old, new, moved):
    text = f'x = 1\nTESTED_CLAUDE_CODE = "{old}"\n'
    out, why = c.move_version(text, c._TESTED, new)
    expected = f'x = 1\nTESTED_CLAUDE_CODE = "{new if moved else old}"\n'
    assert out == expected
    assert bool(why) is not moved


@pytest.mark.parametrize(
    "text", ["nothing here\n", 'TESTED_CLAUDE_CODE = "1.0.0"\n' * 2]
)
def test_the_tested_version_must_be_found_once(text):
    with pytest.raises(ValueError, match="expected one"):
        c.move_version(text, c._TESTED, "9.9.9")


def test_the_real_files_hold_the_tested_version_once():
    from veil.gateway.compat import TESTED_CLAUDE_CODE

    (found,) = c._TESTED.findall(c.COMPAT.read_text())
    assert found == TESTED_CLAUDE_CODE
    assert c._README_TESTED.findall(c.README.read_text()) == [TESTED_CLAUDE_CODE]


def test_billing_versions():
    line = "x-anthropic-billing-header: cc_version=2.1.283.abc; cc_entrypoint=cli;"
    assert c.billing_versions({"system": [{"type": "text", "text": line}]}) == {
        "2.1.283"
    }
    assert c.billing_versions({"system": line}) == {"2.1.283"}
    assert c.billing_versions({"system": "cc_version=2.1.283"}) == set()


# --- Checks --------------------------------------------------------------------


def test_leaks_are_reported_by_path_only():
    body = {
        "messages": [{"content": f"Hi {EMAIL}"}],
        "metadata": {"user": "someone@corp.test"},
        "ok": "[EMAIL_1] and ada@example.com",
        EMAIL: 1,
    }
    found = list(c.leaks(body))
    assert found == ["$.messages[0].content", "$.metadata.user", "$.<key>"]
    assert not [p for p in found if "@" in p]


def test_report_lines_never_show_a_value():
    assert c.shown(f"refused: {EMAIL}") == "<line withheld>"
    assert c.shown("refused: messages[1].x") == "refused: messages[1].x"


def test_the_settle_rule():
    ready = {"posts": 3, "before": 1, "open_requests": 0}
    assert c.settled(10.0, changed=6.0, **ready)
    assert not c.settled(10.0, changed=8.0, **ready)  # changed too recently
    assert not c.settled(10.0, changed=0.0, **{**ready, "open_requests": 1})
    assert not c.settled(10.0, changed=0.0, **{**ready, "posts": 1})  # no request


def test_the_refusal_is_planted_once():
    plant = c.planter()
    assert plant({"messages": []}) is None
    first = plant({"safeguards": [{"type": "x"}]})
    assert first[c.PLANTED] == {"signature": c.PLANTED_VALUE}
    assert plant({"safeguards": [{"type": "x"}]}) is None


def test_the_fictional_files_are_well_formed():
    assert c.png().startswith(b"\x89PNG\r\n\x1a\n")
    document = c.pdf()
    offset = int(re.search(rb"startxref\n(\d+)", document)[1])
    assert document[offset:].startswith(b"xref")
    assert b"(Invoice 1001) Tj" in document


def test_terminal_text_is_folded_and_sent_in_pieces():
    assert fold("\u276f 1. Yes") == "o 1. Yes"
    assert fold("╭─╮") == "m\x00n"
    pieces = chunks("é" * 600, 512)
    assert all(len(p.encode()) <= 512 for p in pieces)
    assert "".join(pieces) == "é" * 600


def test_what_to_run():
    args = c.parse(["--models", "haiku,opus", "--scenarios", "print,web,refusal"])
    # Auto mode's scenarios run on Sonnet: Claude Code has no auto mode on Haiku.
    assert c.plan(args) == [
        ("haiku", "print"),
        ("opus", "print"),
        ("haiku", "web"),
        ("sonnet", "refusal"),
    ]
    assert c.parse(["--accept-new"]).update


@pytest.mark.parametrize(
    ("new", "failures", "harness", "code"),
    [([], [], [], 0), (["x"], [], [], 1), (["x"], ["y"], [], 2), ([], [], ["z"], 2)],
)
def test_exit_codes(new, failures, harness, code):
    assert c.Report(new=new, failures=failures, harness=harness).exit_code() == code


# --- A whole chain, against a fake API -------------------------------------------

SEEN = []  # the bodies the fake API got


class FakeAPI(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        SEEN.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        stream = (
            b'event: message_start\ndata: {"type": "message_start"}\n\n'
            b'event: content_block_delta\ndata: {"type": "content_block_delta",'
            b' "index": 0, "delta": {"type": "text_delta", "text": "hi"},'
            b' "new_key": 1}\n\n'
            b'event: message_stop\ndata: {"type": "message_stop"}\n\n'
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(stream)))
        self.end_headers()
        self.wfile.write(stream)


@pytest.fixture
def fake_api():
    SEEN.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def send(link, body, session="s1"):
    from veil.gateway import SECRET_HEADER, SESSION_HEADER

    connection = http.client.HTTPConnection(link.front.url.removeprefix("http://"))
    connection.request(
        "POST",
        "/v1/messages?beta=true",
        body=json.dumps(body),
        headers={
            "Content-Type": "application/json",
            SECRET_HEADER: link.gateway.secret,
            SESSION_HEADER: session,
            "user-agent": "claude-cli/2.1.283 (external, cli)",
        },
    )
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def test_a_whole_chain(tmp_path, fake_api):
    run_root = tmp_path / "run"
    folder = run_root / "haiku" / "print"
    data = run_root / "data"
    data.mkdir(parents=True, mode=0o700)
    (data / "config.json").write_text(json.dumps(c.CONFIG))
    user = {"role": "user", "content": f"Mail {EMAIL}"}
    with c.chain(folder, data, api=fake_api) as link:
        ok, _ = send(link, {"model": "m", "max_tokens": 5, "messages": [user]})
        new, _ = send(
            link,
            {"model": "m", "max_tokens": 5, "messages": [user], "brand_new": "x"},
        )
        refused, _ = send(
            link,
            {
                "model": "m",
                "max_tokens": 5,
                "messages": [user],
                "odd": {"signature": "Q" * 80},
            },
        )
    assert (ok, new, refused) == (200, 200, 400)
    assert EMAIL not in json.dumps(SEEN)
    c.Outcome("haiku/print", api_requests=3).save(folder)
    report = c.analyze(run_root, c.known_census())
    everything = json.dumps(report.__dict__, default=str)
    assert EMAIL not in everything
    assert "handled generically: haiku/print: brand_new" in report.new
    assert any(
        line.startswith("refused live: haiku/print: veil:") for line in report.failures
    )
    assert any(
        "refused on replay: haiku/print: odd.signature" in line
        for line in report.failures
    )
    # The reply's key of its own reached Claude Code: nothing was dropped.
    assert not [line for line in report.failures if "dropped reply path" in line]
    assert "request POST /v1/messages $.brand_new: string" in report.new
    assert report.exit_code() == 2


STOP = 'event: message_stop\ndata: {"type": "message_stop"}\n\n'


def synthetic(tmp_path, front, upstream, *, label="haiku/print", api_requests=None):
    """A run folder with one capture: (record, body, stream) triples per side."""
    folder = tmp_path.joinpath(*label.split("/"))
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "config.json").write_text(json.dumps(c.CONFIG))
    for side, items in (("front", front), ("upstream", upstream)):
        (folder / side).mkdir(parents=True)
        lines = []
        for number, (record, body, stream) in enumerate(items, 1):
            lines.append(json.dumps({"id": number, **record}))
            (folder / side / f"request-{number}.json").write_text(json.dumps(body))
            (folder / side / f"response-{number}.sse").write_text(stream)
        (folder / side / "requests.jsonl").write_text("".join(f"{x}\n" for x in lines))
    count = len(front) if api_requests is None else api_requests
    c.Outcome(label, api_requests=count).save(folder)
    return c.analyze(tmp_path, c.known_census())


def request(agent=None, **shape):
    headers = {"x-claude-code-session-id": "s1"}
    if agent:
        headers["x-claude-code-agent-id"] = agent
    record = {
        **front_record(),
        "headers": headers,
        "response_shape": shape or {"$": ["object"]},
    }
    return record


def test_a_reply_kind_the_gateway_dropped_is_found_per_request(tmp_path):
    body = {"model": "m", "messages": []}
    kinds = {"$.type": ["type=message_start", "type=message_stop"]}
    front = [(request(**{"$.type": ["type=message_start"]}), body, STOP)]
    report = synthetic(tmp_path, front, [(request(**kinds), body, STOP)])
    assert (
        "haiku/print: request 1: the gateway dropped reply $.type type=message_stop"
        in report.failures
    )
    assert "haiku/print" not in report.clean


def test_requests_that_crossed_are_paired_by_what_they_are(tmp_path):
    # A subagent's request reached the API first; nothing was dropped.
    main = {"model": "m", "messages": [{"role": "user", "content": "a"}]}
    sub = {"model": "m", "messages": []}
    main_reply = {"$.delta.text": ["string"]}
    sub_reply = {"$.delta.thinking": ["string"]}
    front = [
        (request(**main_reply), main, STOP),
        (request("agent-1", **sub_reply), sub, STOP),
    ]
    upstream = [
        (request("agent-1", **sub_reply), sub, STOP),
        (request(**main_reply), main, STOP),
    ]
    report = synthetic(tmp_path, front, upstream)
    assert report.failures == []
    assert "haiku/print" in report.clean


def test_a_stream_that_never_stopped_never_completed(tmp_path):
    body = {"model": "m", "messages": []}
    report = synthetic(tmp_path, [(request(), body, "")], [(request(), body, STOP)])
    assert (
        "haiku/print: request 1 (POST /v1/messages) never completed" in report.failures
    )


def test_no_debug_log_count_is_a_failure(tmp_path):
    body = {"model": "m", "messages": []}
    front = [(request(), body, STOP)]
    report = synthetic(tmp_path, front, [(request(), body, STOP)], api_requests=0)
    assert any("logged no [API REQUEST] lines" in line for line in report.failures)


def test_endpoint_paths_are_shapes():
    def ep(path):
        return c.endpoint({"method": "GET", "path": path})

    assert ep("/v1/messages?beta=true") == "GET /v1/messages"
    assert ep("/v1/messages/count_tokens") == "GET /v1/messages/count_tokens"
    assert ep("/api/organizations/0f3e8a2c-uuid/settings") == (
        "GET /api/organizations/<id>/settings"
    )
    assert ep("/v1/files/file_011CNha8iCJcU1wXNR6q4V8w") == "GET /v1/files/<id>"


def test_nothing_of_this_machines_is_written(monkeypatch):
    monkeypatch.setenv("USER", "adaquill")
    monkeypatch.setenv("LOGNAME", "adaquill")
    assert c.endpoint({"method": "GET", "path": "/u/adaquill/x"}) == "GET /u/<id>/x"
    seen = c.Endpoints()
    seen.add({**front_record(), "header_names": ["x-adaquill-id", "accept"]}, None, "l")
    assert set(seen.headers) == {"accept"}
    assert c.collapse({"$.metadata.adaquill": ["number"], "$": ["object"]}) == {
        "$": ["object"]
    }
    with pytest.raises(AssertionError, match="user name"):
        c._check_committable('{"x": "AdaQuill"}', "endpoints.json")


def test_words_leave_out_keys_of_maps():
    body = {
        "safeguards": [
            {"classifier_context": {"by_slug": {"acme-widgets": 1, "notes_app": 2}}}
        ]
    }
    found = c.words(body)
    assert "by_slug" in found
    assert not found & {"acme-widgets", "notes_app"}


def test_new_paths_keep_the_protocol_words_as_they_are(tmp_path, monkeypatch):
    for name in ("request_census.json", "response_census.json", "endpoints.json"):
        (tmp_path / name).write_text((c.PAYLOADS / name).read_text())
    vocab = tmp_path / "vocab.py"
    vocab.write_text(c.VOCAB.read_text())
    monkeypatch.setattr(c, "PAYLOADS", tmp_path)
    monkeypatch.setattr(c, "VOCAB", vocab)
    report = c.Report(new=["request POST /v1/messages $.brand_new: object"])
    report.words = {"brand_new"}
    done = c.update(report, c.known_census(), accept_new=False, version=None)
    assert "brand_new" not in vocab.read_text()
    assert any(line.startswith("vocab.py stays") for line in done)
    c.update(report, c.known_census(), accept_new=True, version=None)
    assert '"brand_new",' in vocab.read_text()


def test_a_kept_folder_must_be_a_census_run(tmp_path, capsys):
    assert c.main(["--from", str(tmp_path)]) == 3
    assert "not a census run folder" in capsys.readouterr().err
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text(json.dumps(c.CONFIG))
    assert c.main(["--from", str(tmp_path)]) == 2  # nothing ran in it


def test_the_sweep_ends_only_what_names_the_run_folder(tmp_path, monkeypatch):
    import subprocess
    import time

    ended = []
    monkeypatch.setattr(c, "end_processes", ended.extend)
    root = tmp_path / "census-run"
    root.mkdir()
    near = f"{root}0/settings.json"
    started = [
        subprocess.Popen(["/bin/sh", "-c", "sleep 30; true", path])
        for path in (f"{root}/settings.json", near, str(root))
    ]
    try:
        time.sleep(0.3)
        c.sweep(root)
        assert sorted(ended) == sorted([started[0].pid, started[2].pid])
        ended.clear()
        c.sweep(c.Path("/"))
        c.sweep(c.Path("/Users"))
        assert ended == []  # never a run folder
    finally:
        for process in started:
            process.kill()
            process.wait()


def test_the_planted_field_is_never_taken_for_claude_codes(tmp_path):
    folder = tmp_path / "sonnet" / "refusal"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text(json.dumps(c.CONFIG))
    body = {"model": "m", "messages": [], c.PLANTED: {"signature": c.PLANTED_VALUE}}
    for side in ("front", "upstream"):
        (folder / side).mkdir(parents=True)
    record = {
        "id": 1,
        **front_record(),
        "rewritten": True,
        "response_shape": {"$": ["object"]},
    }
    (folder / "front" / "requests.jsonl").write_text(json.dumps(record) + "\n")
    (folder / "front" / "request-1.json").write_text(json.dumps(body))
    (folder / "upstream" / "requests.jsonl").write_text("")
    note = f"claude -p reported an error: veil: ... Not handled: {c.PLANTED}.signature"
    c.Outcome("sonnet/refusal", api_requests=1, notes=[note]).save(folder)
    report = c.analyze(tmp_path, c.known_census())
    assert not [p for p in report.requests.get(c.MESSAGES, {}) if c.PLANTED in p]
    assert c.PLANTED not in report.words
    assert not [w for w in report.warnings if c.PLANTED in w]


def test_the_inputs_of_tools_others_wrote_arent_listed():
    call = {"type": "tool_use", "id": "toolu_01", "input": {"email": "x"}}
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {**call, "name": "StructuredOutput"},
                    {**call, "name": "mcp__contacts__find_contact"},
                    {**call, "name": "Read", "input": {"file_path": "x"}},
                ],
            }
        ]
    }
    shape = c.collapse(shape_of([c.own_inputs(body)]))
    assert "$.messages[].content[].input.file_path" in shape
    assert "$.messages[].content[].input.email" not in shape
    assert body["messages"][0]["content"][0]["input"] == {"email": "x"}  # unchanged
