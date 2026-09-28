"""The gateway's HTTP server: local only, one secret per launch, fail closed.

Standard library only. It accepts the Messages API calls a client such as
Claude Code makes, masks each request for its conversation, forwards it to
the API over HTTPS with the client's own credentials, and restores the reply
on the way back. A request it can't mask is answered with an error in the
API's format and never forwarded.
"""

from __future__ import annotations

import codecs
import contextlib
import hashlib
import hmac
import http.client
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..shield import Shield
from . import compat
from .config import APP
from .hooks import PROOF_PATH, proof
from .ledger import Ledger, MemoryLedger
from .request import RequestMasker, UnsupportedRequestError, billing_version
from .response import FINAL, ResponseRestorer, error_event, restore_message

#: The API the gateway forwards to.
UPSTREAM = "api.anthropic.com"

#: The header that must carry the gateway's secret. It is never forwarded.
SECRET_HEADER = "x-gateway-secret"

#: The header naming the client's conversation (sent by Claude Code).
SESSION_HEADER = "x-claude-code-session-id"

# (method, path) pairs the gateway masks and forwards; everything else gets a
# 404, but for the connection check, which is answered here.
_MESSAGES = "/v1/messages"
_COUNT_TOKENS = "/v1/messages/count_tokens"
_ROUTES = {("POST", _MESSAGES), ("POST", _COUNT_TOKENS)}
_HELLO = "/api/hello"
# Headers only a browser sends: a page can't leave them out. (Node's fetch
# sends Sec-Fetch-Mode alone, so that one is let in.)
_BROWSER_HEADERS = frozenset(
    {"origin", "sec-fetch-site", "sec-fetch-dest", "sec-fetch-user"}
)
# The largest request body read (a long conversation with images is a few MB).
_MAX_BODY = 256 * 1024 * 1024

# Claude Code (2.1.283) treats an error with this code as final for the
# request: it doesn't send it again, on another model or without a feature.
_FINAL = FINAL
# Problems Claude Code can get past by sending the request again without the
# feature they are in: its per-turn effort, or auto mode's safeguards. A
# plain 400 that names the feature lets it do that; any other refusal is
# final, so it doesn't drop an unrelated feature for nothing.
_DROPPABLE = re.compile(
    r"(?:safeguards|messages\[[0-9]+\]\.output_config)(?![A-Za-z0-9_])"
)
_INDEX = re.compile(r"\[[0-9]+\]")
# A path shown in a 404: an API path's shape, nothing that could hold data.
_SHOWN_PATH = re.compile(r"/v1/[a-z_]{1,32}(?:/[a-z_]{1,32}){0,2}")

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
    users: int = 0  # requests using it now; kept by `Sessions`

    def close(self) -> None:
        """Close the vault and ledger, if they hold open files."""
        for part in (self.ledger, self.shield.vault):
            close = getattr(part, "close", None)
            if callable(close):
                close()


class Sessions:
    """Makes and keeps one `Session` per conversation id.

    At most ``max_open`` sessions stay open (each holds open files); the one
    used longest ago, if no request is using it, is closed to make room, and
    opened again from disk when it is needed.

    Args:
        make_shield: Builds the shield for a new conversation id, with the
            vault that holds that conversation's placeholders. Its ledger is
            a `MemoryLedger`.
        make_session: Builds the whole session instead, for a ledger or
            masker of your own.
        max_open: How many sessions to keep open.
    """

    def __init__(
        self,
        make_shield: Callable[[str], Shield] | None = None,
        *,
        make_session: Callable[[str], Session] | None = None,
        max_open: int = 32,
    ) -> None:
        """Create an empty set of sessions."""
        if (make_shield is None) == (make_session is None):
            raise TypeError("pass exactly one of make_shield and make_session")
        if make_session is not None:
            self._make_session = make_session
        else:
            assert make_shield is not None
            shield_for = make_shield

            def in_memory(session_id: str) -> Session:
                shield = shield_for(session_id)
                ledger = MemoryLedger()
                return Session(shield, ledger, RequestMasker(shield, ledger))

            self._make_session = in_memory
        self._max_open = max_open
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Session:
        """Return the session for ``session_id``, making it if needed."""
        with self._lock:
            return self._open(session_id)

    @contextmanager
    def use(self, session_id: str) -> Iterator[Session]:
        """Use a session for one request; it stays open until the request ends."""
        with self._lock:
            session = self._open(session_id)
            session.users += 1
        try:
            yield session
        finally:
            with self._lock:
                session.users -= 1
                self._close_extra()

    def _open(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = self._make_session(session_id)
            self._sessions[session_id] = session
        self._sessions.move_to_end(session_id)
        self._close_extra()
        return session

    def _close_extra(self) -> None:
        idle = [key for key, s in self._sessions.items() if s.users == 0]
        while len(self._sessions) > self._max_open and idle:
            session = self._sessions.pop(idle.pop(0))
            with session.lock:
                session.close()

    def close(self) -> None:
        """Close every session's files."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions = OrderedDict()
        for session in sessions:
            with session.lock:
                session.close()

    def __enter__(self) -> Sessions:
        """Return the sessions."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close every session's files."""
        self.close()


class Refusals:
    """What the gateway refused, for a summary when the client exits.

    Only paths and problems, never a value; a request sent again counts once.

    Attributes:
        client: The Claude Code version of the last refused request, if known.
    """

    def __init__(self, limit: int = 200) -> None:
        """Start with nothing refused; keep at most ``limit`` problems."""
        self._lock = threading.Lock()
        self._limit = limit
        self._bodies: set[str] = set()
        self._groups: dict[tuple[str, str], list[Any]] = {}
        self.client: str | None = None

    def add(
        self, error: UnsupportedRequestError, body: bytes, client: str | None
    ) -> None:
        """Count a refused request and its problems."""
        digest = hashlib.sha256(body).hexdigest()
        with self._lock:
            self.client = client or self.client
            if digest in self._bodies:
                return
            self._bodies.add(digest)
            for path, problem, count in error.problems:
                key = (_INDEX.sub("[]", path), problem)
                if key in self._groups:
                    self._groups[key][2] += count
                elif len(self._groups) < self._limit:
                    self._groups[key] = [path, problem, count]

    @property
    def requests(self) -> int:
        """How many different requests were refused."""
        with self._lock:
            return len(self._bodies)

    def summary(self) -> list[str]:
        """Lines to print once the client has exited; none if nothing was refused."""
        with self._lock:
            if not self._bodies:
                return []
            problems = [(p, w, n) for p, w, n in self._groups.values()]
            return compat.summary(len(self._bodies), problems, self.client)


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

    Attributes:
        refusals: The requests refused so far, for a summary.
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
        self.refusals = Refusals()
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
        """Stop serving, and close the sessions' files."""
        self._server.shutdown()
        self._server.server_close()
        self.sessions.close()

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
    """A request answered with an error.

    ``retry`` leaves it to the client's usual retries (a failure on the way);
    otherwise it is ``final``: Claude Code is told not to send it again, on
    another model or without some feature, unless ``final`` is off for a
    refusal it can get past by dropping a feature.
    """

    def __init__(
        self,
        status: int,
        kind: str,
        message: str,
        *,
        retry: bool = False,
        final: bool = True,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message
        self.retry = retry
        self.final = final and not retry


def _handler(gateway: Gateway) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "gateway"
        sys_version = ""
        # A socket timeout, so a half-sent request can't hold a thread forever.
        timeout = 120

        def log_message(self, format: str, *args: Any) -> None:
            pass  # no access log: paths and headers stay out of the terminal

        def _serve(self) -> None:
            self._started = False  # whether a response has begun
            self._chunked = False  # whether it is a chunked stream
            self._forwarded = False  # whether the request went to the API
            self._json_text = False  # whether the reply's text is JSON
            self._client = compat.from_user_agent(self.headers.get("User-Agent"))
            try:
                address = urlsplit(self.path)
                path = address.path
                if self.command == "GET" and path == PROOF_PATH:
                    self._check_access(secret=False)
                    self._prove(address.query)
                    return
                if self.command in ("GET", "HEAD") and path == _HELLO:
                    self._check_access(secret=False)
                    self._hello()
                    return
                self._check_access()
                if (self.command, path) not in _ROUTES:
                    shown = path if _SHOWN_PATH.fullmatch(path) else "this path"
                    raise _RefusedError(
                        404,
                        "not_found_error",
                        f"{self.command} {shown} isn't served by the gateway, so "
                        f"nothing was sent. {compat.version_advice(self._client)}",
                    )
                self._masked(path, self._read_body())
            except _RefusedError as refusal:
                self._fail(refusal)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # the client went away
            except Exception:
                # Never forwarded and never printed: the details may hold data.
                # A bug fails the same way every time: not worth a retry.
                message = compat.failure_message(
                    "failed", self._client, sent=self._forwarded
                )
                self._fail(_RefusedError(500, "api_error", message))

        def _prove(self, query: str) -> None:
            """Answer the hooks' check that this is the right gateway.

            The secret isn't needed here: the answer is a keyed hash of the
            caller's nonce, which only a holder of the secret can give, so a
            process that took this port can't pass for the gateway.
            """
            nonce = parse_qs(query).get("nonce", [""])[0]
            if not re.fullmatch(r"[0-9a-f]{16,64}", nonce):
                raise _RefusedError(400, "invalid_request_error", "bad nonce")
            data = json.dumps({"proof": proof(gateway.secret, nonce)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._started = True
            self.wfile.write(data)

        def _hello(self) -> None:
            """Answer Claude Code's connection check at startup, here.

            It carries no secret and nothing of the user's, and its answer is
            ignored; forwarding it would let anything on this machine send
            requests through the gateway without the secret.
            """
            data = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._started = True
            if self.command == "GET":
                self.wfile.write(data)

        def _check_access(self, *, secret: bool = True) -> None:
            port = gateway.port
            if self.headers.get("Host", "") not in (
                f"127.0.0.1:{port}",
                f"localhost:{port}",
            ):
                raise _RefusedError(403, "permission_error", "wrong host")
            if any(name.lower() in _BROWSER_HEADERS for name in self.headers):
                raise _RefusedError(
                    403, "permission_error", "browser requests are refused"
                )
            sent = self.headers.get(SECRET_HEADER, "")
            if secret and not hmac.compare_digest(
                sent.encode(), gateway.secret.encode()
            ):
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
            if not length.isdigit():
                raise _RefusedError(400, "invalid_request_error", "bad Content-Length")
            size = int(length)
            if size > _MAX_BODY:
                raise _RefusedError(
                    413, "request_too_large", "the request is too large"
                )
            return self.rfile.read(size)

        def _masked(self, path: str, body: bytes) -> None:
            session_id = self.headers.get(SESSION_HEADER)
            if not session_id:
                raise _RefusedError(
                    400,
                    "invalid_request_error",
                    f"the request has no {SESSION_HEADER}, so nothing was sent. "
                    + compat.version_advice(self._client),
                )
            try:
                request = json.loads(
                    body, parse_constant=_no_constant, parse_float=_finite
                )
            except (ValueError, RecursionError):
                raise _RefusedError(
                    400,
                    "invalid_request_error",
                    "the body isn't JSON, or is nested too deeply",
                ) from None
            client = self._client or billing_version(request)
            with contextlib.ExitStack() as stack:
                try:
                    session = stack.enter_context(gateway.sessions.use(session_id))
                    with session.lock:
                        masked = session.masker.mask(
                            request, client_version=self._client
                        )
                except UnsupportedRequestError as error:
                    refusal = _refusal(error, client)
                    if refusal.final:
                        # Only these are lost: the rest are sent again,
                        # without the feature, by the client.
                        gateway.refusals.add(error, body, client)
                    raise refusal from None
                except Exception as error:
                    if _busy(error):
                        # Another process holds the data folder: worth a retry.
                        raise _RefusedError(
                            500,
                            "api_error",
                            "its data folder is busy, so nothing was sent",
                            retry=True,
                        ) from None
                    raise _RefusedError(
                        500,
                        "api_error",
                        compat.failure_message("failed while masking", client),
                    ) from None
                self._json_text = _asks_for_json(request)
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
            self._forwarded = True
            try:
                try:
                    upstream.request(
                        self.command, self.path, body=body, headers=headers
                    )
                    response = upstream.getresponse()
                except (OSError, http.client.HTTPException):
                    raise _RefusedError(
                        502, "api_error", "the API couldn't be reached", retry=True
                    ) from None
                self._relay(response, session)
            finally:
                upstream.close()

        def _send_head(
            self, response: http.client.HTTPResponse, *, chunked: bool, length: int = 0
        ) -> None:
            self.send_response(response.status)
            for name, value in response.getheaders():
                lowered = name.lower()
                if lowered in _HOP_HEADERS or lowered.startswith("access-control-"):
                    continue
                self.send_header(name, value)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(length))
            self.end_headers()
            self._started = True
            self._chunked = chunked

        def _relay(
            self, response: http.client.HTTPResponse, session: Session | None
        ) -> None:
            content_type = response.getheader("Content-Type") or ""
            restore = session is not None and response.status == 200
            if "text/event-stream" in content_type:
                self._send_head(response, chunked=True)
                self._stream(response, session if restore else None)
                return
            try:
                data = response.read()
            except (OSError, http.client.HTTPException):
                raise _RefusedError(
                    502, "api_error", "the API connection was lost", retry=True
                ) from None
            if restore and session is not None and "json" in content_type:
                try:
                    message = json.loads(data)
                except ValueError:
                    message = None
                if message is not None:
                    try:
                        with session.lock:
                            restored = restore_message(
                                session.shield,
                                session.ledger,
                                message,
                                json_text=self._json_text,
                            )
                    except Exception:
                        # The model already ran: don't have it run again.
                        text = compat.failure_message(
                            "failed while restoring the reply", self._client, sent=True
                        )
                        raise _RefusedError(500, "api_error", text) from None
                    data = json.dumps(restored).encode()
            # Restored before anything is sent, so a failure is a clean error.
            self._send_head(response, chunked=False, length=len(data))
            if self.command != "HEAD":
                self.wfile.write(data)

        def _stream(
            self, response: http.client.HTTPResponse, session: Session | None
        ) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            restorer = (
                ResponseRestorer(
                    session.shield, session.ledger, json_text=self._json_text
                )
                if session
                else None
            )
            last_write = time.monotonic()
            while True:
                try:
                    chunk = response.read1(65536)
                except (OSError, http.client.HTTPException):
                    self._end_stream_with_error("the API connection was lost")
                    return
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if restorer is None or session is None:
                    out = text
                else:
                    with session.lock:
                        out = restorer.feed(text)
                if not out and time.monotonic() - last_write >= gateway.keepalive:
                    out = (
                        restorer.keepalive()
                        if restorer is not None
                        else ": keep-alive\n\n"
                    )
                if out:
                    self._chunk(out)
                    last_write = time.monotonic()
                if restorer is not None and restorer.failed is not None:
                    self._end_chunks()
                    return
            rest = decoder.decode(b"", final=True)
            if restorer is not None and session is not None:
                with session.lock:
                    rest = restorer.feed(rest) + restorer.finish()
            if rest:
                self._chunk(rest)
            self._end_chunks()

        def _chunk(self, text: str) -> None:
            data = text.encode("utf-8")
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        def _end_chunks(self) -> None:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def _end_stream_with_error(self, message: str, *, final: bool = False) -> None:
            self._chunk(error_event(message, final=final))
            self._end_chunks()
            self.close_connection = True

        def _fail(self, refusal: _RefusedError) -> None:
            """Report a failure: as a response, or, once one has begun, ending it."""
            self.close_connection = True
            if not self._started:
                self._error(
                    refusal.status,
                    refusal.kind,
                    refusal.message,
                    retry=refusal.retry,
                    final=refusal.final,
                )
                return
            if self._chunked:
                with contextlib.suppress(OSError):
                    self._end_stream_with_error(refusal.message, final=refusal.final)
            # A response with a length already on its way can't be changed:
            # closing the connection makes the client see it as cut off.

        def _error(
            self,
            status: int,
            kind: str,
            message: str,
            *,
            retry: bool = False,
            final: bool = True,
        ) -> None:
            # The request's body may be unread: never read what follows as a
            # request of its own.
            self.close_connection = True
            error: dict[str, Any] = {"type": kind, "message": _prefixed(message)}
            if final and not retry:
                error["details"] = _FINAL
            data = json.dumps({"type": "error", "error": error}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if not retry:
                # Asking again gets the same answer; a failure on the way
                # (retry=True) is left to the client's usual retries.
                self.send_header("x-should-retry", "false")
            self.send_header("Connection", "close")
            self.end_headers()
            self._started = True
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_OPTIONS(self) -> None:
            self._started = False
            self._chunked = False
            self._error(405, "invalid_request_error", "method not allowed")

        # http.server dispatches on these names.
        do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = _serve  # noqa: N815

    return Handler


def _prefixed(message: str) -> str:
    """Every message the gateway writes starts with the package's name."""
    return message if message.startswith(f"{APP}:") else f"{APP}: {message}"


def _refusal(error: UnsupportedRequestError, client: str | None) -> _RefusedError:
    """The response for a request that can't be masked."""
    message = compat.refusal_message(error.problems, client)
    if all(_DROPPABLE.match(path) for path, _, _ in error.problems):
        # Claude Code sends it again without that feature.
        return _RefusedError(400, "invalid_request_error", message, final=False)
    return _RefusedError(400, "policy_blocked", message)


def _busy(error: Exception) -> bool:
    """Whether a failure is another process holding the data folder."""
    text = str(error).lower()
    return isinstance(error, sqlite3.OperationalError) and (
        "locked" in text or "busy" in text
    )


def _asks_for_json(request: Any) -> bool:
    """Whether a request asked for its reply's text to be JSON (a format)."""
    config = request.get("output_config") if isinstance(request, dict) else None
    legacy = request.get("output_format") if isinstance(request, dict) else None
    return (isinstance(config, dict) and "format" in config) or isinstance(legacy, dict)


def _no_constant(name: str) -> Any:
    """Refuse NaN and Infinity, which aren't JSON and can't be sent on."""
    raise ValueError(f"{name} isn't a JSON number")


def _finite(text: str) -> float:
    """Read a JSON number, refusing one too large to be sent back as JSON."""
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("the number is too large")
    return number
