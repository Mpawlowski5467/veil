"""The gateway's HTTP server: local only, one secret per launch, fail closed.

Standard library only. It accepts the Messages API calls a client such as
Claude Code makes, masks each request for its conversation, forwards it to
the API over HTTPS with the client's own credentials, and restores the reply
on the way back. A request it can't mask is answered with an error in the
API's format and never forwarded.
"""

from __future__ import annotations

import codecs
import hmac
import http.client
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from ..shield import Shield
from .ledger import Ledger, MemoryLedger
from .request import RequestMasker, UnsupportedRequestError
from .response import ResponseRestorer, restore_message

#: The API the gateway forwards to.
UPSTREAM = "api.anthropic.com"

#: The header that must carry the gateway's secret. It is never forwarded.
SECRET_HEADER = "x-gateway-secret"

#: The header naming the client's conversation (sent by Claude Code).
SESSION_HEADER = "x-claude-code-session-id"

# (method, path) pairs the gateway serves; everything else gets a 404.
_MESSAGES = "/v1/messages"
_COUNT_TOKENS = "/v1/messages/count_tokens"
_ROUTES = {
    ("POST", _MESSAGES),
    ("POST", _COUNT_TOKENS),
    ("HEAD", "/api/hello"),
    ("GET", "/api/hello"),
}
_MASKED_PATHS = {_MESSAGES, _COUNT_TOKENS}

# Headers about one hop of the connection, or that the gateway sets itself.
_HOP_HEADERS = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-encoding",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        SECRET_HEADER,
    }
)


@dataclass
class Session:
    """One conversation: its shield, ledger, and request masker."""

    shield: Shield
    ledger: Ledger
    masker: RequestMasker
    lock: threading.RLock = field(default_factory=threading.RLock)


class Sessions:
    """Makes and keeps one `Session` per conversation id.

    Args:
        make_shield: Builds the shield for a new conversation id, with the
            vault that holds that conversation's placeholders.
        make_ledger: Builds its ledger; a `MemoryLedger` by default.
    """

    def __init__(
        self,
        make_shield: Callable[[str], Shield],
        make_ledger: Callable[[str], Ledger] | None = None,
    ) -> None:
        """Create an empty set of sessions."""
        self._make_shield = make_shield
        self._make_ledger = make_ledger or (lambda _id: MemoryLedger())
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Session:
        """Return the session for ``session_id``, making it the first time."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                shield = self._make_shield(session_id)
                ledger = self._make_ledger(session_id)
                session = Session(shield, ledger, RequestMasker(shield, ledger))
                self._sessions[session_id] = session
            return session


class Gateway:
    """A running gateway; use as a context manager, or call `close`.

    Args:
        sessions: Where each conversation's shield and ledger come from.
        port: The local port, or 0 for a free one.
        secret: The value clients must send in `SECRET_HEADER`; a random one
            is made if not given.
        upstream: The API host. Only tests change it.
        secure: Use HTTPS to the upstream. Only tests turn it off.
        keepalive: Seconds of silence after which a comment line is sent to
            the client while a tool call is held back.
    """

    def __init__(
        self,
        sessions: Sessions,
        *,
        port: int = 0,
        secret: str | None = None,
        upstream: str = UPSTREAM,
        secure: bool = True,
        keepalive: float = 10.0,
        timeout: float = 600.0,
    ) -> None:
        """Start listening on 127.0.0.1."""
        self.sessions = sessions
        self.secret = secret or secrets.token_urlsafe(32)
        self.upstream = upstream
        self.secure = secure
        self.keepalive = keepalive
        self.timeout = timeout
        self._server = _QuietServer(("127.0.0.1", port), _handler(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        """The port the gateway listens on."""
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """The base URL to give the client, e.g. as ``ANTHROPIC_BASE_URL``."""
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> Gateway:
        """Return the running gateway."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop the gateway."""
        self.close()

    def _connect(self) -> http.client.HTTPConnection:
        if self.secure:
            return http.client.HTTPSConnection(self.upstream, timeout=self.timeout)
        return http.client.HTTPConnection(self.upstream, timeout=self.timeout)


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        """Print nothing: a traceback could quote a request's content."""


class _RefusedError(Exception):
    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message


def _handler(gateway: Gateway) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "gateway"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            pass  # no access log: paths and headers stay out of the terminal

        def _serve(self) -> None:
            try:
                self._check_access()
                path = urlsplit(self.path).path
                if (self.command, path) not in _ROUTES:
                    raise _RefusedError(
                        404, "not_found_error", "not served by the gateway"
                    )
                body = self._read_body()
                if path in _MASKED_PATHS:
                    self._masked(path, body)
                else:
                    self._forward(body, None)
            except _RefusedError as refusal:
                self._error(refusal.status, refusal.kind, refusal.message)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception:
                # Never forwarded and never printed: the details may hold data.
                try:
                    self._error(500, "api_error", "the gateway failed")
                except OSError:
                    self.close_connection = True

        def _check_access(self) -> None:
            port = gateway.port
            if self.headers.get("Host", "") not in (
                f"127.0.0.1:{port}",
                f"localhost:{port}",
            ):
                raise _RefusedError(403, "permission_error", "wrong host")
            if self.headers.get("Origin") is not None or any(
                name.lower().startswith("sec-fetch-") for name in self.headers
            ):
                raise _RefusedError(
                    403, "permission_error", "browser requests are refused"
                )
            sent = self.headers.get(SECRET_HEADER, "")
            if not hmac.compare_digest(sent.encode(), gateway.secret.encode()):
                raise _RefusedError(
                    401, "authentication_error", "missing or wrong secret"
                )

        def _read_body(self) -> bytes:
            if self.headers.get("Content-Encoding"):
                raise _RefusedError(
                    415, "invalid_request_error", "compressed bodies aren't read"
                )
            if self.headers.get("Transfer-Encoding"):
                raise _RefusedError(
                    411, "invalid_request_error", "send a Content-Length"
                )
            length = self.headers.get("Content-Length")
            if length is None:
                return b""
            try:
                size = int(length)
            except ValueError:
                raise _RefusedError(
                    400, "invalid_request_error", "bad Content-Length"
                ) from None
            return self.rfile.read(size)

        def _masked(self, path: str, body: bytes) -> None:
            session_id = self.headers.get(SESSION_HEADER)
            if not session_id:
                raise _RefusedError(
                    400, "invalid_request_error", f"send {SESSION_HEADER}"
                )
            try:
                request = json.loads(body)
            except ValueError:
                raise _RefusedError(
                    400, "invalid_request_error", "the body isn't JSON"
                ) from None
            try:
                session = gateway.sessions.get(session_id)
                with session.lock:
                    masked = session.masker.mask(request)
            except UnsupportedRequestError as error:
                raise _RefusedError(
                    400, "invalid_request_error", f"the gateway can't mask {error}"
                ) from None
            except Exception:
                raise _RefusedError(
                    500, "api_error", "the gateway failed to mask"
                ) from None
            self._forward(
                json.dumps(masked).encode(), session if path == _MESSAGES else None
            )

        def _forward(self, body: bytes, session: Session | None) -> None:
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in _HOP_HEADERS
            }
            headers["Host"] = gateway.upstream
            headers["Accept-Encoding"] = "identity"
            headers["Content-Length"] = str(len(body))
            upstream = gateway._connect()
            try:
                try:
                    upstream.request(
                        self.command, self.path, body=body, headers=headers
                    )
                    response = upstream.getresponse()
                except (OSError, http.client.HTTPException):
                    raise _RefusedError(
                        502, "api_error", "the API couldn't be reached"
                    ) from None
                self._relay(response, session)
            finally:
                upstream.close()

        def _relay(
            self, response: http.client.HTTPResponse, session: Session | None
        ) -> None:
            content_type = response.getheader("Content-Type") or ""
            restore = session is not None and response.status == 200
            self.send_response(response.status)
            for name, value in response.getheaders():
                lowered = name.lower()
                if lowered in _HOP_HEADERS or lowered.startswith("access-control-"):
                    continue
                self.send_header(name, value)
            if "text/event-stream" in content_type:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self._stream(response, session if restore else None)
                return
            data = response.read()
            if restore and session is not None and "json" in content_type:
                try:
                    message = json.loads(data)
                except ValueError:
                    message = None
                if message is not None:
                    with session.lock:
                        restored = restore_message(
                            session.shield, session.ledger, message
                        )
                    data = json.dumps(restored).encode()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _stream(
            self, response: http.client.HTTPResponse, session: Session | None
        ) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            restorer = (
                ResponseRestorer(session.shield, session.ledger) if session else None
            )
            last_write = time.monotonic()
            try:
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    if restorer is None or session is None:
                        out = text
                    else:
                        with session.lock:
                            out = restorer.feed(text)
                    if not out and time.monotonic() - last_write >= gateway.keepalive:
                        out = ": keep-alive\n\n"
                    if out:
                        self._chunk(out)
                        last_write = time.monotonic()
                    if restorer is not None and restorer.failed is not None:
                        break
                rest = decoder.decode(b"", final=True)
                if restorer is not None and session is not None:
                    with session.lock:
                        rest = restorer.feed(rest) + restorer.finish()
                if rest:
                    self._chunk(rest)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client went away; closing upstream ends the call

        def _chunk(self, text: str) -> None:
            data = text.encode("utf-8")
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        def _error(self, status: int, kind: str, message: str) -> None:
            # The request's body may be unread: never read what follows as a
            # request of its own.
            self.close_connection = True
            data = json.dumps(
                {"type": "error", "error": {"type": kind, "message": message}}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("x-should-retry", "false")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_OPTIONS(self) -> None:
            self._error(405, "invalid_request_error", "method not allowed")

        # http.server dispatches on these names.
        do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = _serve  # noqa: N815

    return Handler
