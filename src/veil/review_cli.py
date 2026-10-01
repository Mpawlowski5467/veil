"""Explicit human review in a local browser or interactive terminal."""

from __future__ import annotations

import contextlib
import hmac
import http.client
import json
import os
import secrets
import socket
import sys
import time
import webbrowser
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .gateway import SECRET_HEADER
from .gateway.config import SettingsError
from .gateway.hooks import _gateway_answers
from .secret_review import CHOICES, REVIEW_PATH
from .verification import Endpoint, endpoint

_HTML = Path(__file__).with_name("review.html").read_text(encoding="utf-8")


def _validate_review_choice(choice: dict[str, Any]) -> None:
    if set(choice) != {"id", "index", "choice"} or (
        not isinstance(choice["id"], str)
        or type(choice["index"]) is not int
        or choice["choice"] not in CHOICES
    ):
        raise ValueError


def _gateway_request(target: Endpoint, choice: dict[str, Any] | None) -> dict[str, Any]:
    if not _gateway_answers(target.url, target.secret):
        raise SettingsError("gateway identity could not be verified")
    connection = http.client.HTTPConnection(urlsplit(target.url).netloc, timeout=5)
    try:
        connection.request(
            "GET" if choice is None else "POST",
            REVIEW_PATH,
            body=None if choice is None else json.dumps(choice).encode(),
            headers={SECRET_HEADER: target.secret, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read(20 * 1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 20 * 1024 * 1024:
            raise ValueError
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get("reviews"), list):
            raise ValueError
        return result
    except (OSError, ValueError, http.client.HTTPException):
        raise SettingsError(
            "review unavailable; check gateway version and review expiry"
        ) from None
    finally:
        connection.close()


def _socket_owner(
    tables: Iterable[str], client: tuple[str, int], server_port: int
) -> int | None:
    """Return the uid of an established loopback client socket in tcp tables."""

    def encode(raw: bytes, port: int) -> str:
        # Each 32-bit address word is printed in host byte order.
        words = (raw[i : i + 4] for i in range(0, len(raw), 4))
        address = "".join(f"{int.from_bytes(w, sys.byteorder):08X}" for w in words)
        return f"{address}:{port:04X}"

    host, server = socket.inet_aton(client[0]), socket.inet_aton("127.0.0.1")
    mapped = bytes(10) + b"\xff\xff"
    wanted = {
        (encode(host, client[1]), encode(server, server_port)),
        (encode(mapped + host, client[1]), encode(mapped + server, server_port)),
    }
    for table in tables:
        for line in table.splitlines()[1:]:
            fields = line.split()
            # Match the client's side of the pair while established: the
            # server's side is this process, and closing rows report uid 0.
            if (
                len(fields) > 7
                and (fields[1], fields[2]) in wanted
                and fields[3] == "01"
                and fields[7].isdecimal()
            ):
                return int(fields[7])
    return None


def _same_account(client: tuple[str, int], server_port: int) -> bool:
    """On Linux, accept only loopback clients run by this OS account."""
    if sys.platform != "linux":
        return True
    tables = []
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        with contextlib.suppress(OSError):
            tables.append(Path(name).read_text(encoding="ascii"))
    # Fail closed: an unreadable table or a missing row refuses the request.
    return _socket_owner(tables, client, server_port) == os.getuid()


class ReviewServer(HTTPServer):
    """Short-lived browser bridge: a one-time launch code opens one page session.

    The session token never grants model or gateway access, and it is never
    printed or put in a URL.
    """

    def __init__(
        self,
        request: Callable[[dict[str, Any] | None], dict[str, Any]],
        *,
        html: str = _HTML,
        max_body: int = 4096,
        validate: Callable[[dict[str, Any]], None] = _validate_review_choice,
        error: str = "Review unavailable or expired",
    ) -> None:
        """Bind only to loopback with a new capability for this review window."""
        self.token = secrets.token_urlsafe(32)
        self.launch: str | None = secrets.token_urlsafe(32)
        self.review_request = request
        self.html = html
        self.max_body = max_body
        self.validate = validate
        self.error = error
        self.same_account: Callable[[tuple[str, int], int], bool] = _same_account
        super().__init__(("127.0.0.1", 0), _ReviewHandler)
        self.timeout = 1
        # The launch code sits in the fragment, never the HTTP path, and the
        # URL stays printable after the page has used the code.
        self.url = f"http://127.0.0.1:{self.server_port}/#{self.launch}"

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Keep input and callback exceptions out of terminal logs."""


class _ReviewHandler(BaseHTTPRequestHandler):
    timeout = 5

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def _serve(self) -> None:
        import base64
        import hashlib

        server = self.server
        assert isinstance(server, ReviewServer)
        origin = f"http://127.0.0.1:{server.server_port}"
        host, port = self.client_address
        # Loopback is shared by every account on this computer. Refuse other
        # accounts before any route, so they cannot use the launch code.
        if (
            self.headers.get("Host") != f"127.0.0.1:{server.server_port}"
            or self.headers.get("Origin", origin) != origin
            or self.headers.get("Sec-Fetch-Site", "none") not in {"none", "same-origin"}
            or not server.same_account((str(host), int(port)), server.server_port)
        ):
            self._reply(403, b"Refused")
            return
        if self.path == "/session" and self.command == "POST":
            # The first page load exchanges the launch code for the session
            # token. A wrong code leaves it unused; a used code is refused.
            launch = server.launch
            if launch is None or not hmac.compare_digest(
                self.headers.get("X-Veil-Review", "").encode(), launch.encode()
            ):
                self._reply(403, b"Refused")
                return
            server.launch = None
            self._reply(200, json.dumps({"token": server.token}).encode())
            return
        if self.path == "/" and self.command == "GET":
            script = server.html.split("<script>")[1].split("</script>")[0]
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            self._reply(200, server.html.encode(), html=True, script_hash=digest)
            return
        if self.path != "/data" or not hmac.compare_digest(
            self.headers.get("X-Veil-Review", "").encode(), server.token.encode()
        ):
            self._reply(403, b"Refused")
            return
        try:
            choice = None
            if self.command == "POST":
                length = self.headers.get("Content-Length", "")
                if (
                    self.headers.get("Transfer-Encoding")
                    or self.headers.get("Content-Encoding")
                    or not length.isdecimal()
                    or not 0 < int(length) <= server.max_body
                ):
                    raise ValueError
                choice = json.loads(self.rfile.read(int(length)))
                if not isinstance(choice, dict):
                    raise ValueError
                server.validate(choice)
            result = server.review_request(choice)
            self._reply(200, json.dumps(result).encode())
        except Exception:
            self._reply(400, server.error.encode())

    def _reply(
        self, status: int, body: bytes, *, html: bool = False, script_hash: str = ""
    ) -> None:
        self.send_response(status)
        self.send_header(
            "Content-Type", "text/html; charset=utf-8" if html else "application/json"
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; connect-src 'self'; "
            f"script-src 'sha256-{script_hash}'; style-src 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_review(
    *,
    config: Path | None = None,
    data_dir: Path | None = None,
    gateway_url: str | None = None,
    terminal: bool = False,
) -> int:
    """Open a private review surface only from an explicit interactive terminal."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SettingsError("open veil review yourself in a local interactive terminal")
    target = endpoint(config=config, data_dir=data_dir, gateway_url=gateway_url)
    report = _gateway_request(target, None)
    if terminal:
        for review in report["reviews"]:
            for finding in review["findings"]:
                if finding["choice"]:
                    continue
                choice = ask_terminal(finding["value"], finding["reason"])
                _gateway_request(
                    target,
                    {"id": review["id"], "index": finding["index"], "choice": choice},
                )
        print("Choices saved. Retry the original request in your client.")
        return 0
    with ReviewServer(lambda choice: _gateway_request(target, choice)) as server:
        print(
            "Private review is open for ten minutes. Ctrl-C closes this page's access."
        )
        print(server.url)
        webbrowser.open(server.url)
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline:
                server.handle_request()
        except KeyboardInterrupt:
            pass
    return 0


def ask_terminal(value: str, reason: str) -> str:
    """Ask on the controlling terminal, never a pipe or an assistant transcript."""
    # A redirected stdin can contain the prompt being masked. The controlling
    # terminal must be separate, and values are escaped to prevent ANSI attacks.
    try:
        with (
            open(
                "CONOUT$" if sys.platform == "win32" else "/dev/tty",
                "w",
                encoding="utf-8",
            ) as output,
            open(
                "CONIN$" if sys.platform == "win32" else "/dev/tty", encoding="utf-8"
            ) as source,
        ):
            if not source.isatty() or not output.isatty():
                raise OSError
            output.write(f"\nPossible private value ({reason}): {value!a}\n")
            output.write(
                " / ".join(f"{i + 1}={kind}" for i, kind in enumerate(CHOICES)) + "\n"
            )
            while True:
                output.write("Classify (blank cancels): ")
                output.flush()
                answer = source.readline().strip()
                if not answer:
                    raise SettingsError("review cancelled; no output written")
                if answer.isdigit() and 1 <= int(answer) <= len(CHOICES):
                    return CHOICES[int(answer) - 1]
    except OSError:
        raise SettingsError(
            "review needs a local controlling terminal; no output written"
        ) from None
