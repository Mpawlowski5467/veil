"""A pass-through gateway for live tests that records what Claude Code sends.

It listens on 127.0.0.1, forwards every request unchanged to the Anthropic API
(the same headers, so the user's own login works), and streams the reply back
unchanged. For each request it records the method, path, header names, a few
header values that carry no secret, and the *shape* of the JSON body and of
the reply: every key path with its value types, and every ``type`` value
seen. Full bodies are written only to the run's own directory, never to the
repository. Authorization values are never recorded.

Standard library only.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

UPSTREAM = "api.anthropic.com"

# Header values that are safe to record; any other header is recorded by name.
SHOWN_HEADERS = frozenset(
    {
        "accept",
        "anthropic-beta",
        "anthropic-version",
        "content-type",
        "user-agent",
        "x-app",
        "x-claude-code-agent-id",
        "x-claude-code-session-id",
        "x-stainless-lang",
        "x-stainless-retry-count",
    }
)
# Headers that describe one hop of the connection and aren't forwarded.
HOP_HEADERS = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-connection",
        "te",
        "transfer-encoding",
        "upgrade",
    }
)


# Keys that are ids rather than field names, such as the tool-use ids that
# key auto mode's review results; they are all written as ``<id>``.
_ID_KEY = re.compile(r"^(?:srv)?toolu_[A-Za-z0-9_-]+$")


def shape(value: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    """Yield ``(path, kind)`` for every node of a JSON value.

    Lists are written ``[]``, so every item shares a path. ``kind`` is the
    JSON type, or ``type=<value>`` for the string in a ``type`` key, which is
    how the Messages API names its block kinds.
    """
    if isinstance(value, dict):
        yield path, "object"
        for key, item in value.items():
            child = f"{path}.{'<id>' if _ID_KEY.match(key) else key}"
            if key == "type" and isinstance(item, str):
                yield child, f"type={item}"
            else:
                yield from shape(item, child)
    elif isinstance(value, list):
        yield path, "array"
        for item in value:
            yield from shape(item, f"{path}[]")
    elif isinstance(value, str):
        yield path, "string"
    elif isinstance(value, bool):
        yield path, "boolean"
    elif isinstance(value, (int, float)):
        yield path, "number"
    else:
        yield path, "null"


def census(values: list[Any]) -> dict[str, list[str]]:
    """Merge the shapes of several JSON values: ``{path: sorted kinds}``."""
    merged: dict[str, set[str]] = {}
    for value in values:
        for path, kind in shape(value):
            merged.setdefault(path, set()).add(kind)
    return {path: sorted(kinds) for path, kinds in sorted(merged.items())}


class Recorder:
    """A running pass-through gateway; use as a context manager."""

    def __init__(
        self, folder: Path, *, upstream: str = UPSTREAM, secure: bool = True
    ) -> None:
        """Start listening on a free port; bodies go under ``folder``.

        ``upstream`` and ``secure`` exist for tests, which use a local
        plain-HTTP server in place of the API.
        """
        self.folder = folder
        self.upstream = upstream
        self.secure = secure
        self.folder.mkdir(parents=True, exist_ok=True)
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """The base URL to give Claude Code as ``ANTHROPIC_BASE_URL``."""
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        """Stop the server and write ``requests.jsonl``."""
        self._server.shutdown()
        self._server.server_close()
        with open(self.folder / "requests.jsonl", "w", encoding="utf-8") as f:
            for record in self.records:
                f.write(json.dumps(record) + "\n")

    def __enter__(self) -> Recorder:
        """Return the running recorder."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop the recorder."""
        self.close()

    def _add(self, record: dict[str, Any]) -> int:
        with self._lock:
            record["id"] = len(self.records) + 1
            self.records.append(record)
            return int(record["id"])


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    length = int(handler.headers.get("Content-Length") or 0)
    if length:
        return handler.rfile.read(length)
    if handler.headers.get("Transfer-Encoding", "").lower() != "chunked":
        return b""
    chunks = []
    while True:
        size = int(handler.rfile.readline().strip() or b"0", 16)
        if size == 0:
            handler.rfile.readline()
            return b"".join(chunks)
        chunks.append(handler.rfile.read(size))
        handler.rfile.readline()


def _sse_events(response: http.client.HTTPResponse) -> Iterator[bytes]:
    """Yield each raw server-sent event, blank-line terminator included."""
    event = b""
    while True:
        line = response.readline()
        if not line:
            if event:
                yield event
            return
        event += line
        if line in (b"\n", b"\r\n"):
            yield event
            event = b""


def _event_json(raw: bytes) -> Any:
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                return None
    return None


def _handler(recorder: Recorder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _forward(self) -> None:
            body = _read_body(self)
            record: dict[str, Any] = {
                "method": self.command,
                "path": self.path,
                "header_names": sorted(name.lower() for name in self.headers),
                "headers": {
                    name.lower(): value
                    for name, value in self.headers.items()
                    if name.lower() in SHOWN_HEADERS
                },
                "body_bytes": len(body),
            }
            parsed: Any = None
            if body:
                try:
                    parsed = json.loads(body)
                except ValueError:
                    record["body_kind"] = "not json"
            if parsed is not None:
                record["body_shape"] = census([parsed])
            rid = recorder._add(record)
            if parsed is not None:
                (recorder.folder / f"request-{rid}.json").write_text(
                    json.dumps(parsed, indent=1, ensure_ascii=False), encoding="utf-8"
                )
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in HOP_HEADERS
            }
            headers["Host"] = recorder.upstream
            headers["Accept-Encoding"] = "identity"
            headers["Content-Length"] = str(len(body))
            connection = (
                http.client.HTTPSConnection
                if recorder.secure
                else http.client.HTTPConnection
            )
            upstream = connection(recorder.upstream, timeout=600)
            try:
                upstream.request(self.command, self.path, body=body, headers=headers)
                response = upstream.getresponse()
                self._relay(response, record, rid)
            finally:
                upstream.close()

        def _relay(
            self, response: http.client.HTTPResponse, record: dict[str, Any], rid: int
        ) -> None:
            content_type = response.getheader("Content-Type") or ""
            record["status"] = response.status
            record["response_type"] = content_type
            record["response_header_names"] = sorted(
                name.lower() for name, _ in response.getheaders()
            )
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in HOP_HEADERS:
                    self.send_header(name, value)
            if "text/event-stream" in content_type:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                events = []
                with open(
                    recorder.folder / f"response-{rid}.sse", "wb"
                ) as copy:  # full stream, for local inspection only
                    for raw in _sse_events(response):
                        copy.write(raw)
                        self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                        self.wfile.flush()
                        payload = _event_json(raw)
                        if payload is not None:
                            events.append(payload)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                record["response_shape"] = census(events)
            else:
                data = response.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                try:
                    record["response_shape"] = census([json.loads(data)])
                except ValueError:
                    record["response_kind"] = "not json"

        # http.server dispatches on these names.
        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _forward  # noqa: N815

    return Handler
