"""The live-test harness itself: probe hook, MCP server, settings, fixtures.

These run without Claude Code; the live tests that use the harness are in
test_claude_live.py.
"""

import http.client
import http.server
import json
import subprocess
import sys
import threading

import pytest

from live import harness, mcp_contacts, probe_hook, recorder

READ_PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Read",
    "tool_input": {"file_path": "/work/notes.txt"},
    "tool_response": {
        "type": "text",
        "file": {
            "filePath": "/work/notes.txt",
            "content": harness.NOTES,
            "numLines": 4,
            "startLine": 1,
            "totalLines": 4,
        },
    },
}

WRITE_PAYLOAD = {
    "hook_event_name": "PreToolUse",
    "tool_name": "Write",
    "tool_input": {"file_path": "/work/out.txt", "content": "Contact: [EMAIL_1]"},
}


def call_hook(tmp_path, rules, payload):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps(rules))
    log = tmp_path / "hooks.jsonl"
    completed = subprocess.run(
        [sys.executable, str(harness.PROBE_HOOK), str(rules_file), str(log)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    records = [json.loads(line) for line in log.read_text().splitlines()]
    return completed, records


class TestProbeHook:
    def test_logs_and_stays_silent_without_a_rule(self, tmp_path):
        completed, records = call_hook(tmp_path, [], READ_PAYLOAD)
        assert completed.returncode == 0
        assert completed.stdout == ""
        assert len(records) == 1
        assert records[0]["event"] == "PostToolUse"
        assert records[0]["tool"] == "Read"
        assert records[0]["payload"] == READ_PAYLOAD
        assert records[0]["response"] is None

    def test_replace_output_keeps_the_shape(self, tmp_path):
        rules = [{"event": "PostToolUse", "replace_output": {harness.EMAIL: "[E]"}}]
        completed, _ = call_hook(tmp_path, rules, READ_PAYLOAD)
        output = json.loads(completed.stdout)["hookSpecificOutput"]
        assert output["hookEventName"] == "PostToolUse"
        updated = output["updatedToolOutput"]
        assert updated["file"]["content"] == harness.NOTES.replace(harness.EMAIL, "[E]")
        del updated["file"]["content"]
        original = dict(READ_PAYLOAD["tool_response"]["file"])
        del original["content"]
        assert updated["file"] == original
        assert updated["type"] == "text"

    def test_replace_output_under_another_field(self, tmp_path):
        rules = [
            {
                "event": "PostToolUse",
                "replace_output": {harness.EMAIL: "[E]"},
                "field": "updatedMCPToolOutput",
            }
        ]
        completed, _ = call_hook(tmp_path, rules, READ_PAYLOAD)
        assert (
            "updatedMCPToolOutput" in json.loads(completed.stdout)["hookSpecificOutput"]
        )

    def test_replace_output_says_nothing_when_nothing_changes(self, tmp_path):
        rules = [{"event": "PostToolUse", "replace_output": {"absent": "x"}}]
        completed, records = call_hook(tmp_path, rules, READ_PAYLOAD)
        assert completed.stdout == ""
        assert records[0]["response"] is None

    def test_replace_input_with_a_decision(self, tmp_path):
        rules = [
            {
                "event": "PreToolUse",
                "tool": "Write|Edit",
                "replace_input": {"[EMAIL_1]": harness.EMAIL},
                "decision": "ask",
                "reason": "check it",
            }
        ]
        completed, _ = call_hook(tmp_path, rules, WRITE_PAYLOAD)
        output = json.loads(completed.stdout)["hookSpecificOutput"]
        assert output["updatedInput"] == {
            "file_path": "/work/out.txt",
            "content": f"Contact: {harness.EMAIL}",
        }
        assert output["permissionDecision"] == "ask"
        assert output["permissionDecisionReason"] == "check it"

    def test_set_input_merges_keys(self, tmp_path):
        rules = [{"event": "PreToolUse", "set_input": {"run_in_background": False}}]
        completed, _ = call_hook(tmp_path, rules, WRITE_PAYLOAD)
        updated = json.loads(completed.stdout)["hookSpecificOutput"]["updatedInput"]
        assert updated == {**WRITE_PAYLOAD["tool_input"], "run_in_background": False}

    def test_tool_must_match_fully(self, tmp_path):
        rules = [{"event": "PreToolUse", "tool": "Writ", "respond": {"x": 1}}]
        completed, _ = call_hook(tmp_path, rules, WRITE_PAYLOAD)
        assert completed.stdout == ""

    def test_first_matching_rule_wins(self, tmp_path):
        rules = [
            {"event": "PostToolUse", "respond": {"first": True}},
            {"event": "*", "respond": {"second": True}},
        ]
        completed, _ = call_hook(tmp_path, rules, READ_PAYLOAD)
        assert json.loads(completed.stdout) == {"first": True}

    def test_contains_filters_on_the_payload(self, tmp_path):
        rules = [
            {"event": "*", "contains": "absent text", "respond": {"a": 1}},
            {"event": "*", "contains": "notes.txt", "respond": {"b": 2}},
        ]
        completed, _ = call_hook(tmp_path, rules, READ_PAYLOAD)
        assert json.loads(completed.stdout) == {"b": 2}

    def test_exit_code_and_stderr(self, tmp_path):
        rules = [{"event": "*", "exit": 2, "stderr": "blocked"}]
        completed, records = call_hook(tmp_path, rules, READ_PAYLOAD)
        assert completed.returncode == 2
        assert completed.stderr == "blocked"
        assert records[0]["exit"] == 2

    def test_logs_environment_values_only_when_safe(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/work")
        monkeypatch.setenv("CLAUDE_CODE_SOME_TOKEN", "secret-value")
        _, records = call_hook(tmp_path, [], READ_PAYLOAD)
        env = records[0]["env"]
        assert env["CLAUDE_PROJECT_DIR"] == "/work"
        assert env["CLAUDE_CODE_SOME_TOKEN"] == "…"

    def test_answer_directly(self):
        response, code, stderr, delay = probe_hook.answer(
            [{"event": "PostToolUse", "sleep": 0.5}], READ_PAYLOAD, ""
        )
        assert (response, code, stderr, delay) == (None, 0, "", 0.5)


class TestMCPServer:
    def test_serves_initialize_list_and_call(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "find_contact", "arguments": {"name": "Jan"}},
            },
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
        ]
        completed = subprocess.run(
            [sys.executable, str(harness.MCP_SERVER)],
            input="".join(json.dumps(r) + "\n" for r in requests),
            capture_output=True,
            text=True,
            check=True,
        )
        replies = [json.loads(line) for line in completed.stdout.splitlines()]
        assert [r["id"] for r in replies] == [1, 2, 3, 4]
        assert replies[0]["result"]["capabilities"] == {"tools": {}}
        assert replies[1]["result"]["tools"][0]["name"] == "find_contact"
        assert replies[2]["result"]["content"][0]["text"] == mcp_contacts.CONTACT
        assert replies[3]["error"]["code"] == -32601

    def test_contact_is_the_shared_card(self):
        assert mcp_contacts.CONTACT == harness.NOTES


class TestSettings:
    def test_every_event_runs_the_command(self, tmp_path):
        command = harness.hook_command(tmp_path / "r.json", tmp_path / "l.jsonl")
        result = json.loads(json.dumps(harness.settings(command)))
        assert set(result["hooks"]) == set(harness.EVENTS)
        for groups in result["hooks"].values():
            assert groups == [
                {"hooks": [{"type": "command", "command": command, "timeout": 60}]}
            ]

    def test_extra_settings_add_hooks_and_replace_the_rest(self):
        base = harness.settings("probe")
        extra = {
            "env": {"A": "1"},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}], "Custom": []},
        }
        merged = harness.merge_settings(base, extra)
        assert merged["env"] == {"A": "1"}
        assert merged["hooks"]["PreToolUse"] == [
            *base["hooks"]["PreToolUse"],
            {"matcher": "Bash", "hooks": []},
        ]
        assert merged["hooks"]["Custom"] == []
        assert merged["hooks"]["Stop"] == base["hooks"]["Stop"]
        assert (
            base["hooks"]["PreToolUse"]
            == harness.settings("probe")["hooks"]["PreToolUse"]
        )

    def test_command_quotes_paths(self, tmp_path):
        spaced = tmp_path / "a b"
        command = harness.hook_command(spaced / "r.json", spaced / "l.jsonl")
        assert f"'{spaced}/r.json'" in command
        assert str(harness.PROBE_HOOK) in command

    def test_environment_is_clean(self, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
        env = harness.environment({"EXTRA": "x"})
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE_ENTRYPOINT" not in env
        assert env["EXTRA"] == "x"
        assert env["PATH"].startswith("/opt/homebrew/bin")


class TestRun:
    def make_run(self, tmp_path, hooks, stream=()):
        return harness.Run(
            dir=tmp_path,
            command=[],
            returncode=0,
            stream=list(stream),
            hooks=hooks,
            stderr="",
            debug="",
        )

    def test_result_and_session_id(self, tmp_path):
        stream = [
            {"type": "system", "session_id": "s1"},
            {"type": "result", "result": "done", "session_id": "s1"},
        ]
        run = self.make_run(tmp_path, [], stream)
        assert run.result["result"] == "done"
        assert run.session_id == "s1"

    def test_calls_filter_by_event_and_tool(self, tmp_path):
        hooks = [
            {"event": "PreToolUse", "tool": "Read", "payload": {}},
            {"event": "PreToolUse", "tool": "Bash", "payload": {}},
            {"event": "PostToolUse", "tool": "Read", "payload": {}},
        ]
        run = self.make_run(tmp_path, hooks)
        assert len(run.calls("PreToolUse")) == 2
        assert len(run.calls("PreToolUse", "Bash")) == 1

    def test_tool_results_from_the_transcript(self, tmp_path):
        records = [
            {"message": {"content": "plain prompt"}},
            {
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "one"},
                        {"type": "tool_result", "content": [{"text": "two"}]},
                    ]
                }
            },
            {"attachment": {"type": "file", "content": "attached"}},
        ]
        run = self.make_run(tmp_path, [])
        run.transcript_text = "".join(json.dumps(r) + "\n" for r in records) + "junk\n"
        assert run.tool_results() == ["one", "two"]
        assert run.attachments("file") == [{"type": "file", "content": "attached"}]
        assert "plain prompt" in run.everything()

    def test_transcript_path_from_the_hooks(self, tmp_path):
        hooks = [
            {"event": "SessionStart", "tool": None, "payload": {}},
            {"event": "Stop", "tool": None, "payload": {"transcript_path": "/t.jsonl"}},
        ]
        assert self.make_run(tmp_path, hooks).transcript_path == harness.Path(
            "/t.jsonl"
        )

    def test_missing_transcript(self, tmp_path):
        run = self.make_run(tmp_path, [])
        assert run.transcript_path is None
        assert run.transcript() == []
        assert run.tool_results() == []

    def test_batch_calls_and_notifications(self, tmp_path):
        read = {"tool_name": "Read", "tool_response": "1\tName"}
        bash = {"tool_name": "Bash", "tool_response": "Exit code 1"}
        hooks = [
            {"event": "PostToolBatch", "tool": None, "payload": {"tool_calls": [read]}},
            {"event": "PostToolBatch", "tool": None, "payload": {"tool_calls": [bash]}},
            {"event": "UserPromptSubmit", "tool": None, "payload": {"prompt": "hi"}},
            {
                "event": "UserPromptSubmit",
                "tool": None,
                "payload": {"prompt": "<task-notification>\n<status>completed"},
            },
        ]
        run = self.make_run(tmp_path, hooks)
        assert run.batch_calls() == [read, bash]
        assert run.batch_calls("Bash") == [bash]
        assert run.notifications() == ["<task-notification>\n<status>completed"]

    def test_api_bodies_from_the_raw_body_files(self, tmp_path):
        api = tmp_path / "api"
        api.mkdir()
        (api / "r1.request.json").write_text('{"messages": "one"}')
        (api / "index.jsonl").write_text(
            json.dumps({"query_source": "sdk", "request_file": "r1.request.json"})
            + "\n"
            + json.dumps({"query_source": "web_fetch_apply", "request_file": "gone"})
            + "\n"
        )
        run = self.make_run(tmp_path, [])
        assert run.api_bodies() == [
            {"query_source": "sdk", "request": '{"messages": "one"}'},
            {"query_source": "web_fetch_apply", "request": ""},
        ]
        assert run.sent_to_api() == '{"messages": "one"}\n'

    def test_no_api_bodies(self, tmp_path):
        run = self.make_run(tmp_path, [])
        assert run.api_bodies() == []
        assert run.sent_to_api() == ""

    def test_api_requests_from_the_debug_log(self, tmp_path):
        run = self.make_run(tmp_path, [])
        run.debug = "a\n[API REQUEST] /v1/messages source=sdk\nb\n"
        assert run.api_requests() == ["[API REQUEST] /v1/messages source=sdk"]


class TestSavedFixtures:
    FIXTURES = sorted(
        (harness.HERE.parent / "claude_code_payloads").glob("*.json"),
        key=lambda path: path.name,
    )

    def test_there_are_fixtures(self):
        assert len(self.FIXTURES) >= 40

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.name)
    def test_fixture_is_fictional_and_machine_free(self, path):
        text = path.read_text(encoding="utf-8")
        json.loads(text)
        harness.assert_fictional(text)
        assert str(harness.Path.home()) not in text
        assert "/private/" not in text


class TestFixtures:
    def test_sanitize_replaces_paths_and_ids(self, tmp_path):
        work = tmp_path / "work"
        value = {
            "cwd": str(work),
            "file": f"{work}/notes.txt",
            "home": f"{harness.Path.home()}/.claude/projects/x.jsonl",
            "session_id": "0b2abf6d-c725-417c-8f95-30f7aebb1688",
            "tool_use_id": "toolu_01ABCdef",
            "transcript_path": (
                f"{harness.Path.home()}/.claude/projects/-Users-x-proj-l6vn7p/s.jsonl"
            ),
            "outputFile": "/private/tmp/claude-501/-Users-x-proj/s/tasks/a1.output",
        }
        assert harness.sanitize(value, work=work) == {
            "cwd": "/work",
            "file": "/work/notes.txt",
            "home": "~/.claude/projects/x.jsonl",
            "session_id": "00000000-0000-4000-8000-000000000000",
            "tool_use_id": "toolu_fixture",
            "transcript_path": "~/.claude/projects/-work/s.jsonl",
            "outputFile": "<tmp>/tasks/a1.output",
        }

    def test_fictional_data_passes(self):
        harness.assert_fictional(harness.NOTES + " from 192.0.2.10 and 127.0.0.1")

    @pytest.mark.parametrize(
        "text",
        [
            "Email: jane.doe@mail.test",  # reserved domain, but not example.com
            "Server: 10.20.30.40",
            "Card: 4111 1111 1111 1111",
        ],
    )
    def test_anything_else_is_rejected(self, text):
        with pytest.raises(AssertionError, match="not obviously fictional"):
            harness.assert_fictional(text)

    def test_rejection_does_not_quote_the_value(self):
        with pytest.raises(AssertionError) as info:
            harness.assert_fictional("Email: jane.doe@mail.test")
        assert "jane.doe" not in str(info.value)

    def test_save_fixture_writes_clean_json(self, tmp_path):
        work = tmp_path / "work"
        path = tmp_path / "fixtures" / "read.json"
        harness.save_fixture(path, {"cwd": str(work), "text": harness.NOTES}, work=work)
        assert json.loads(path.read_text()) == {"cwd": "/work", "text": harness.NOTES}

    def test_save_fixture_refuses_real_looking_data(self, tmp_path):
        path = tmp_path / "bad.json"
        with pytest.raises(AssertionError):
            harness.save_fixture(path, {"ip": "10.20.30.40"}, work=tmp_path)
        assert not path.exists()


class TestShape:
    def test_paths_kinds_and_type_values(self):
        value = {
            "model": "m",
            "stream": True,
            "max_tokens": 5,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": "plain"},
            ],
            "metadata": None,
        }
        assert recorder.census([value]) == {
            "$": ["object"],
            "$.max_tokens": ["number"],
            "$.messages": ["array"],
            "$.messages[]": ["object"],
            "$.messages[].content": ["array", "string"],
            "$.messages[].content[]": ["object"],
            "$.messages[].content[].text": ["string"],
            "$.messages[].content[].type": ["type=text"],
            "$.messages[].role": ["string"],
            "$.metadata": ["null"],
            "$.model": ["string"],
            "$.stream": ["boolean"],
        }

    def test_id_keys_are_collapsed(self):
        value = {"tool_uses": {"toolu_01AbC": {"x": 1}, "srvtoolu_9z": {"x": 2}}}
        assert recorder.census([value])["$.tool_uses.<id>.x"] == ["number"]

    def test_keys_and_types_that_look_like_data_are_hidden(self):
        value = {
            "metadata": {"jane.doe@example.com": 1, "$ref": "x"},
            "block": {"type": "Jan Nowak"},
        }
        shape = recorder.census([value])
        assert shape["$.metadata.<key>"] == ["number"]
        assert shape["$.metadata.$ref"] == ["string"]
        assert shape["$.block.type"] == ["type=<value>"]

    def test_census_merges_values(self):
        merged = recorder.census([{"type": "a"}, {"type": "b"}, [1]])
        assert merged == {
            "$": ["array", "object"],
            "$.type": ["type=a", "type=b"],
            "$[]": ["number"],
        }


SEEN = []  # what the fake API received: (path, headers, body)


class FakeAPI(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        SEEN.append((self.path, dict(self.headers), body))
        stream = (
            b'event: message_start\ndata: {"type": "message_start"}\n\n'
            b'event: content_block_delta\ndata: {"type": "content_block_delta",'
            b' "delta": {"type": "text_delta", "text": "hi"}}\n\n'
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(stream)))
        self.end_headers()
        self.wfile.write(stream)

    def do_GET(self):
        data = b'{"data": [{"type": "model", "id": "m"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_api():
    SEEN.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class TestRecorder:
    def test_forwards_streams_and_records_shapes(self, tmp_path, fake_api):
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
        with recorder.Recorder(tmp_path, upstream=fake_api, secure=False) as gw:
            host = gw.url.removeprefix("http://")
            conn = http.client.HTTPConnection(host)
            conn.request(
                "POST",
                "/v1/messages?beta=true",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer secret-token",
                    "anthropic-beta": "oauth-2025-04-20",
                },
            )
            response = conn.getresponse()
            streamed = response.read()
            conn.close()
        assert b"text_delta" in streamed
        path, headers, forwarded = SEEN[0]
        assert path == "/v1/messages?beta=true"
        assert forwarded == body.encode()
        assert headers["Authorization"] == "Bearer secret-token"
        (record,) = gw.records
        assert record["status"] == 200
        assert record["headers"]["anthropic-beta"] == "oauth-2025-04-20"
        assert "authorization" in record["header_names"]
        assert "secret-token" not in json.dumps(record)
        assert record["body_shape"]["$.messages[].content"] == ["string"]
        assert record["response_shape"]["$.delta.type"] == ["type=text_delta"]
        saved = (tmp_path / "requests.jsonl").read_text()
        assert "secret-token" not in saved
        assert (tmp_path / "request-1.json").exists()
        assert (tmp_path / "response-1.sse").read_bytes().startswith(b"event:")

    def test_a_body_can_be_changed_on_the_way(self, tmp_path, fake_api):
        def plant(body):
            return {**body, "planted": True} if "messages" in body else None

        with recorder.Recorder(
            tmp_path, upstream=fake_api, secure=False, rewrite=plant
        ) as gw:
            conn = http.client.HTTPConnection(gw.url.removeprefix("http://"))
            conn.request("POST", "/v1/messages", body=json.dumps({"messages": []}))
            conn.getresponse().read()
            conn.close()
        assert json.loads(SEEN[0][2]) == {"messages": [], "planted": True}
        assert gw.records[0]["rewritten"] is True
        assert gw.records[0]["body_shape"]["$.planted"] == ["boolean"]

    def test_json_responses_are_relayed(self, tmp_path, fake_api):
        with recorder.Recorder(tmp_path, upstream=fake_api, secure=False) as gw:
            conn = http.client.HTTPConnection(gw.url.removeprefix("http://"))
            conn.request("GET", "/v1/models")
            data = json.loads(conn.getresponse().read())
            conn.close()
        assert data["data"][0]["id"] == "m"
        assert gw.records[0]["response_shape"]["$.data[].type"] == ["type=model"]
        assert json.loads((tmp_path / "response-1.json").read_text()) == data
