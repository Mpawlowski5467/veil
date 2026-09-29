"""The gateway's HTTP server, against a fake API on a local port."""

import http.client
import http.server
import json
import threading
import time

import pytest

from veil import LiteralPlaceholderDetector, MemoryVault, RegexDetector, Shield
from veil.gateway import SECRET_HEADER, SESSION_HEADER, Gateway, Sessions

EMAIL = "jane.doe@example.com"
NAME = "Jan Nowak"


class FakeAPI:
    """Records what reaches it; answers with the next scripted reply."""

    def __init__(self):
        self.received = []
        self.replies = []
        self.routes = {}
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass

            def _any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                api.received.append((self.command, self.path, dict(self.headers), body))
                status, content_type, parts = (
                    api.routes[self.path.split("?", 1)[0]]
                    if self.path.split("?", 1)[0] in api.routes
                    else api.replies.pop(0)
                    if api.replies
                    else (200, "application/json", [b"{}"])
                )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                if content_type == "text/event-stream":
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for part in parts:
                        if isinstance(part, float):
                            time.sleep(part)
                            continue
                        self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    data = b"".join(parts)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(data)

            do_GET = do_POST = do_HEAD = _any  # noqa: N815

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        ).start()
        self.host = f"127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_shield(_session_id):
    shield = Shield(
        detectors=[LiteralPlaceholderDetector({"EMAIL", "PERSON"}), RegexDetector()],
        vault=MemoryVault(),
        redact_warnings=True,
    )
    shield.add_entity(NAME, "PERSON")
    return shield


@pytest.fixture
def api():
    fake = FakeAPI()
    yield fake
    fake.close()


@pytest.fixture
def gateway(api):
    with Gateway(
        Sessions(make_shield), upstream=api.host, secure=False, keepalive=0.05
    ) as gw:
        yield gw


def call(
    gateway, method="POST", path="/v1/messages?beta=true", body=None, headers=None
):
    conn = http.client.HTTPConnection(f"127.0.0.1:{gateway.port}", timeout=10)
    sent = {
        SECRET_HEADER: gateway.secret,
        SESSION_HEADER: "session-1",
        "Content-Type": "application/json",
        "Authorization": "Bearer user-token",
        "anthropic-beta": "oauth-2025-04-20",
    }
    sent.update(headers or {})
    sent = {k: v for k, v in sent.items() if v is not None}
    data = (
        None
        if body is None
        else (body if isinstance(body, bytes) else json.dumps(body).encode())
    )
    conn.request(method, path, body=data, headers=sent)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response, payload


def sse(data):
    return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n".encode()


def stream_reply(*parts):
    return (200, "text/event-stream", list(parts))


REQUEST = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 100,
    "stream": True,
    "messages": [{"role": "user", "content": f"Email {NAME} at {EMAIL}"}],
}


def text_reply(*pieces):
    parts = [
        sse({"type": "message_start", "message": {"id": "m", "content": []}}),
        sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        ),
    ]
    parts += [
        sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": p},
            }
        )
        for p in pieces
    ]
    parts += [
        sse({"type": "content_block_stop", "index": 0}),
        sse({"type": "message_stop"}),
    ]
    return stream_reply(*parts)


def events(payload):
    out = []
    for raw in payload.decode().split("\n\n"):
        data = [
            line[5:].strip() for line in raw.split("\n") if line.startswith("data:")
        ]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


class TestRoundTrip:
    def test_the_request_goes_out_masked_and_the_reply_comes_back_restored(
        self, api, gateway
    ):
        api.replies.append(text_reply("Sure, I'll email [PERS", "ON_1] at [EMAIL_1]."))
        response, payload = call(gateway, body=REQUEST)
        assert response.status == 200
        _, path, _, body = api.received[0]
        assert path == "/v1/messages?beta=true"
        sent = json.loads(body)
        assert sent["messages"][0]["content"] == "Email [PERSON_1] at [EMAIL_1]"
        assert EMAIL.encode() not in body
        assert NAME.encode() not in body
        text = "".join(
            e["delta"]["text"]
            for e in events(payload)
            if e["type"] == "content_block_delta"
        )
        assert text == f"Sure, I'll email {NAME} at {EMAIL}."

    def test_credentials_pass_through_and_the_secret_doesnt(self, api, gateway):
        api.replies.append(text_reply("ok"))
        call(gateway, body=REQUEST)
        _, _, headers, _ = api.received[0]
        lowered = {k.lower(): v for k, v in headers.items()}
        assert lowered["authorization"] == "Bearer user-token"
        assert lowered["anthropic-beta"] == "oauth-2025-04-20"
        assert lowered["host"] == api.host
        assert lowered["accept-encoding"] == "identity"
        assert SECRET_HEADER not in lowered
        assert gateway.secret not in json.dumps(headers)

    def test_cors_headers_from_the_api_are_dropped(self, api, gateway):
        api.replies.append(text_reply("ok"))
        response, _ = call(gateway, body=REQUEST)
        assert response.getheader("Access-Control-Allow-Origin") is None

    def test_the_next_request_sends_back_what_the_model_wrote(self, api, gateway):
        api.replies.append(text_reply("Spoke with [PERSON_1]em."))
        _, payload = call(gateway, body=REQUEST)
        shown = "".join(
            e["delta"]["text"]
            for e in events(payload)
            if e["type"] == "content_block_delta"
        )
        assert shown == f"Spoke with {NAME}em."
        api.replies.append(text_reply("ok"))
        follow_up = {
            **REQUEST,
            "messages": [
                *REQUEST["messages"],
                {"role": "assistant", "content": [{"type": "text", "text": shown}]},
                {"role": "user", "content": "Thanks"},
            ],
        }
        call(gateway, body=follow_up)
        sent = json.loads(api.received[1][3])
        assert sent["messages"][1]["content"][0]["text"] == "Spoke with [PERSON_1]em."
        assert NAME.encode() not in api.received[1][3]

    def test_a_tool_call_is_restored_and_kept_alive_while_held(self, api, gateway):
        tool = '{"command": "echo [PERSON_1]"}'
        api.replies.append(
            stream_reply(
                sse(
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {},
                        },
                    }
                ),
                sse(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": tool[:12],
                        },
                    }
                ),
                0.12,
                sse(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": tool[12:],
                        },
                    }
                ),
                sse({"type": "content_block_stop", "index": 0}),
            )
        )
        _, payload = call(gateway, body=REQUEST)
        deltas = [
            e["delta"] for e in events(payload) if e["type"] == "content_block_delta"
        ]
        # Keep-alives are empty input deltas: real events the client counts.
        assert {"type": "input_json_delta", "partial_json": ""} in deltas
        parts = [d["partial_json"] for d in deltas if d["partial_json"]]
        assert [json.loads(p) for p in parts] == [{"command": f"echo {NAME}"}]

    def test_a_reply_that_isnt_streamed_is_restored(self, api, gateway):
        message = {"id": "m", "content": [{"type": "text", "text": "Hi [PERSON_1]"}]}
        api.replies.append((200, "application/json", [json.dumps(message).encode()]))
        response, payload = call(gateway, body={**REQUEST, "stream": False})
        assert json.loads(payload)["content"][0]["text"] == f"Hi {NAME}"
        assert response.getheader("Content-Length") == str(len(payload))

    def test_count_tokens_is_masked_and_not_restored(self, api, gateway):
        api.replies.append((200, "application/json", [b'{"input_tokens": 12}']))
        _, payload = call(
            gateway, path="/v1/messages/count_tokens?beta=true", body=REQUEST
        )
        assert json.loads(payload) == {"input_tokens": 12}
        assert EMAIL.encode() not in api.received[0][3]

    def test_api_errors_pass_through(self, api, gateway):
        error = {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "slow down"},
        }
        api.replies.append((429, "application/json", [json.dumps(error).encode()]))
        response, payload = call(gateway, body=REQUEST)
        assert response.status == 429
        assert json.loads(payload) == error

    def test_the_connectivity_check_is_forwarded(self, api, gateway):
        response, _ = call(gateway, method="HEAD", path="/api/hello", body=None)
        assert response.status == 200
        assert api.received[0][:2] == ("HEAD", "/api/hello")

    def test_sessions_keep_separate_placeholders(self, api, gateway):
        api.replies += [text_reply("a"), text_reply("b")]
        call(gateway, body=REQUEST)
        other = {
            **REQUEST,
            "messages": [{"role": "user", "content": "Mail ada.q@example.com"}],
        }
        call(gateway, body=other, headers={SESSION_HEADER: "session-2"})
        assert (
            json.loads(api.received[1][3])["messages"][0]["content"] == "Mail [EMAIL_1]"
        )


REFUSALS = [
    ({"Host": "evil.example.com"}, "POST", "/v1/messages", 403, "wrong host"),
    (
        {"Origin": "https://evil.example.com"},
        "POST",
        "/v1/messages",
        403,
        "browser requests are refused",
    ),
    (
        {"Sec-Fetch-Site": "cross-site"},
        "POST",
        "/v1/messages",
        403,
        "browser requests are refused",
    ),
    ({SECRET_HEADER: None}, "POST", "/v1/messages", 401, "missing or wrong secret"),
    ({SECRET_HEADER: "guess"}, "POST", "/v1/messages", 401, "missing or wrong secret"),
    ({}, "POST", "/v1/complete", 404, "not served by the gateway"),
    ({}, "GET", "/v1/messages", 404, "not served by the gateway"),
    ({SESSION_HEADER: None}, "POST", "/v1/messages", 400, f"send {SESSION_HEADER}"),
    (
        {"Content-Encoding": "gzip"},
        "POST",
        "/v1/messages",
        415,
        "compressed bodies aren't read",
    ),
]


@pytest.mark.parametrize(("headers", "method", "path", "status", "message"), REFUSALS)
def test_refused_requests_never_reach_the_api(
    api, gateway, headers, method, path, status, message
):
    response, payload = call(
        gateway, method=method, path=path, body=REQUEST, headers=headers
    )
    assert response.status == status
    assert json.loads(payload) == {
        "type": "error",
        "error": {"type": json.loads(payload)["error"]["type"], "message": message},
    }
    assert response.getheader("x-should-retry") == "false"
    assert api.received == []


def test_options_is_refused(api, gateway):
    response, _ = call(gateway, method="OPTIONS", path="/v1/messages", body=None)
    assert response.status == 405
    assert api.received == []


def test_an_unmaskable_request_is_refused_by_path(api, gateway):
    body = {
        **REQUEST,
        "messages": [
            {"role": "user", "content": [{"type": "server_tool_use", "id": "x"}]}
        ],
    }
    response, payload = call(gateway, body=body)
    assert response.status == 400
    assert json.loads(payload)["error"]["message"] == (
        "the gateway can't mask messages[0].content[0].type: unknown block type"
    )
    assert api.received == []


@pytest.mark.parametrize(
    ("path", "reply"),
    [
        ("/v1/messages?beta=true", text_reply("OK")),
        (
            "/v1/messages/count_tokens?beta=true",
            (200, "application/json", [b'{"input_tokens": 12}']),
        ),
    ],
)
def test_a_per_turn_effort_goes_through_unchanged(api, gateway, path, reply):
    effort = {"role": "system", "content": [], "output_config": {"effort": "high"}}
    body = {**REQUEST, "model": "claude-opus-5-5", "messages": [*REQUEST["messages"]]}
    body["messages"].append(effort)
    api.replies.append(reply)
    response, _ = call(gateway, path=path, body=body)
    assert response.status == 200
    sent = json.loads(api.received[0][3])
    assert sent["messages"][0]["content"] == "Email [PERSON_1] at [EMAIL_1]"
    assert sent["messages"][1] == effort


def test_a_refused_effort_is_named_so_the_client_can_drop_it(api, gateway):
    effort = {"role": "system", "content": [], "output_config": {"effort": "ultra"}}
    body = {**REQUEST, "messages": [*REQUEST["messages"], effort]}
    response, payload = call(gateway, body=body)
    assert response.status == 400
    assert json.loads(payload)["error"] == {
        "type": "invalid_request_error",
        "message": (
            "the gateway can't mask messages[1].output_config.effort: "
            "not an effort level"
        ),
    }
    assert response.getheader("x-should-retry") == "false"
    assert api.received == []


def test_a_body_that_isnt_json_is_refused(api, gateway):
    response, _ = call(gateway, body=b"{not json")
    assert response.status == 400
    assert api.received == []


def test_a_failure_while_masking_is_refused(api):
    def broken(_session_id):
        raise RuntimeError(f"cannot open vault for {EMAIL}")

    with Gateway(Sessions(broken), upstream=api.host, secure=False) as gw:
        response, payload = call(gw, body=REQUEST)
    assert response.status == 500
    assert EMAIL.encode() not in payload
    assert api.received == []


def test_a_refusal_closes_the_connection(api, gateway):
    # The body of a refused request is left unread; it must never be read as
    # a request of its own.
    smuggled = b"POST /v1/messages HTTP/1.1\r\nHost: x\r\n\r\n"
    response, _ = call(gateway, body=smuggled, headers={"Host": "evil.example.com"})
    assert response.status == 403
    assert response.getheader("Connection") == "close"
    assert api.received == []


def test_errors_print_nothing(api, capfd):
    def broken(_session_id):
        raise RuntimeError(f"cannot open vault for {EMAIL}")

    with Gateway(Sessions(broken), upstream=api.host, secure=False) as gw:
        call(gw, body=REQUEST)
    captured = capfd.readouterr()
    assert EMAIL not in captured.err + captured.out


def test_a_masker_error_is_refused_without_details(api, gateway):
    session = gateway.sessions.get("session-1")

    def explode(_body):
        raise RuntimeError(f"boom {EMAIL}")

    session.masker.mask = explode
    response, payload = call(gateway, body=REQUEST)
    assert response.status == 500
    assert EMAIL.encode() not in payload
    assert api.received == []


def test_an_unreachable_api_is_a_502(gateway):
    gateway.upstream = "127.0.0.1:9"
    response, payload = call(gateway, body=REQUEST)
    assert response.status == 502
    assert json.loads(payload)["error"]["message"] == "the API couldn't be reached"


def test_the_gateway_listens_on_loopback_only(gateway):
    assert gateway._server.server_address[0] == "127.0.0.1"
    assert gateway.url == f"http://127.0.0.1:{gateway.port}"
    assert len(gateway.secret) >= 40


class BrokenAPI:
    """Sends a stream's first event, then resets the connection."""

    def __init__(self):
        import socket

        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.host = f"127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import struct

        conn, _ = self.sock.accept()
        self.sock.close()
        conn.recv(65536)
        first = sse({"type": "message_start", "message": {"id": "m", "content": []}})
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            + f"{len(first):x}\r\n".encode()
            + first
            + b"\r\n"
        )
        time.sleep(0.2)
        conn.setsockopt(
            __import__("socket").SOL_SOCKET,
            __import__("socket").SO_LINGER,
            struct.pack("ii", 1, 0),
        )
        conn.close()


def test_an_api_reset_mid_stream_ends_the_stream_with_an_error():
    broken = BrokenAPI()
    with Gateway(Sessions(make_shield), upstream=broken.host, secure=False) as gw:
        started = time.monotonic()
        response, payload = call(gw, body=REQUEST)
        assert time.monotonic() - started < 5
    assert response.status == 200
    assert events(payload)[-1] == {
        "type": "error",
        "error": {"type": "api_error", "message": "the API connection was lost"},
    }


def test_a_failure_while_restoring_a_stream_ends_it_cleanly(api, gateway, monkeypatch):
    import veil.gateway.server as server

    def broken_feed(self, text):
        raise RuntimeError(f"boom {EMAIL}")

    monkeypatch.setattr(server.ResponseRestorer, "feed", broken_feed)
    api.replies.append(text_reply("hi"))
    response, payload = call(gateway, body=REQUEST)
    assert response.status == 200
    assert b"HTTP/1.1" not in payload  # no second response inside the first
    assert events(payload)[-1]["type"] == "error"
    assert EMAIL.encode() not in payload


def test_a_failure_while_restoring_a_whole_reply_is_a_clean_500(
    api, gateway, monkeypatch
):
    import veil.gateway.server as server

    monkeypatch.setattr(server, "restore_message", lambda *a: 1 / 0)
    message = {"id": "m", "content": [{"type": "text", "text": "Hi"}]}
    api.replies.append((200, "application/json", [json.dumps(message).encode()]))
    response, payload = call(gateway, body={**REQUEST, "stream": False})
    assert response.status == 500
    assert json.loads(payload)["error"]["message"] == "the gateway failed"


def test_only_refusals_ask_the_client_not_to_retry(api, gateway):
    refused, _ = call(gateway, body=REQUEST, headers={SECRET_HEADER: "wrong"})
    assert refused.getheader("x-should-retry") == "false"
    gateway.upstream = "127.0.0.1:9"
    unreachable, _ = call(gateway, body=REQUEST)
    assert unreachable.status == 502
    assert unreachable.getheader("x-should-retry") is None


@pytest.mark.parametrize(
    ("length", "status"), [("-1", 400), ("12x", 400), (str(300 * 2**20), 413)]
)
def test_bad_lengths_are_refused(api, gateway, length, status):
    conn = http.client.HTTPConnection(f"127.0.0.1:{gateway.port}", timeout=5)
    conn.putrequest("POST", "/v1/messages")
    for name, value in {
        SECRET_HEADER: gateway.secret,
        SESSION_HEADER: "s",
        "Content-Length": length,
    }.items():
        conn.putheader(name, value)
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == status
    conn.close()
    assert api.received == []


class TestSessions:
    def make(self, max_open):
        closed = []

        class Vault(MemoryVault):
            def close(self):
                closed.append(self)

        def shield(_id):
            return Shield(vault=Vault())

        return Sessions(shield, max_open=max_open), closed

    def test_the_oldest_idle_session_is_closed(self):
        sessions, closed = self.make(2)
        first = sessions.get("a")
        sessions.get("b")
        sessions.get("c")
        assert closed == [first.shield.vault]
        assert sessions.get("a") is not first  # opened again when needed

    def test_a_session_in_use_is_never_closed(self):
        sessions, closed = self.make(1)
        with sessions.use("a") as busy:
            sessions.get("b")
            sessions.get("c")
            assert busy.shield.vault not in closed
        assert busy.users == 0
