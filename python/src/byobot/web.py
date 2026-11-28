"""The HTTP plumbing ``byobot serve`` and ``byobot remote`` share: bounded bodies, JSON answers, errors, pairing codes.

Each server builds its own handler on this one, not on the other's: they face different callers (a paired phone on
the local network, an assistant anywhere on the internet behind a tunnel).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qs, urlsplit

from byobot import __version__
from byobot.core.query import interrupted

PAIRING_TTL_S = 600
PAIRING_TRIES = 5
MAX_BODY = 8 * 1024 * 1024
MAX_TOKEN = 200
# Bytes already in the socket buffer that we may drop after a refusal, so close() does not RST
# the response away. We never wait for the client to send more.
DISCARD_BUFFERED = 64 * 1024
SOCKET_TIMEOUT_S = 15  # idle cap so a stalled handshake or header cannot hold a thread
REQUEST_DEADLINE_S = 30  # whole-request cap, so a slow body cannot refresh the idle timer forever
JSON = "application/json; charset=utf-8"


@dataclass
class Pairing:
    """One pairing code: single use, short-lived, a few tries."""

    code: str = field(default_factory=lambda: f"{secrets.randbelow(10**6):06d}")
    expires_at: float = field(default_factory=lambda: time.monotonic() + PAIRING_TTL_S)
    tries_left: int = PAIRING_TRIES
    used: bool = False
    holder: str | None = None  # who the code was spent on: it may send it again until it expires, as after a lost answer

    @property
    def open(self) -> bool:
        return not self.used and self.tries_left > 0 and time.monotonic() < self.expires_at

    def admits(self, holder: str | None) -> bool:
        again = holder is not None and holder == self.holder and self.tries_left > 0
        return self.open or (again and time.monotonic() < self.expires_at)

    def check(self, code: Any, holder: str | None = None) -> bool:
        if not self.admits(holder):
            return False
        typed = code.replace(" ", "").replace("-", "").encode() if isinstance(code, str) else b""
        if secrets.compare_digest(typed, self.code.encode()):
            self.used, self.holder = True, holder
            return True
        self.tries_left -= 1
        return False


class HTTPError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str, details: Any = None,
                 headers: dict[str, str] | None = None, content_type: str = JSON):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details
        self.headers, self.content_type = headers, content_type


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def last_values(fields: dict[str, list[str]]) -> dict[str, str]:
    """A parsed query or form, each name with the last value given for it."""
    return {k: v[-1] for k, v in fields.items()}


def timed_out(exc: sqlite3.OperationalError) -> NoReturn:
    """Answer a read that ``limit_time`` stopped; re-raise any other database error."""
    if interrupted(exc):
        raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "timeout", "the query took too long and was stopped") from None
    raise exc


def printable(text: str) -> str:
    """Text for the log, with control characters escaped, so a stranger can't drive the terminal."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in text)


def _not_json(constant: str) -> NoReturn:
    raise ValueError(f"{constant} is not JSON")


def _finite(text: str) -> float:
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise ValueError(f"{text} is out of range")
    return value


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    ssl_context: ssl.SSLContext | None = None

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], db: Path,
                 pairing: Pairing | None, name: str, log: Callable[[str], None] | None = None):
        self.db, self.pairing, self.name = db, pairing, name
        self.pairing_lock = threading.Lock()
        self.log = log or (lambda line: None)
        super().__init__(address, handler)

    def finish_request(self, request: Any, client_address: Any) -> None:
        # Handshake and headers run in this request's thread. An idle cap means a client that
        # never finishes cannot hold the thread; the handler applies the same cap to the body.
        request.settimeout(SOCKET_TIMEOUT_S)
        if self.ssl_context is not None:
            try:
                request = self.ssl_context.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError):
                return
            request.settimeout(SOCKET_TIMEOUT_S)
        super().finish_request(request, client_address)

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def log_line(line: str) -> None:
    """A server's log: the line on stderr, with the time."""
    sys.stderr.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    sys.stderr.flush()


def serve_until_stopped(server: Server) -> int:
    sys.stderr.write("\nServing. Press Ctrl-C to stop.\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"
    server_version = f"byobot/{__version__}"
    peer: dict | None = None  # who is calling, once known, for the log

    def setup(self) -> None:
        super().setup()
        self._start()

    def parse_request(self) -> bool:
        self._start()  # setup() runs once per connection; each request on a kept-alive one starts afresh
        return super().parse_request()

    def _start(self) -> None:
        self.deadline = time.monotonic() + REQUEST_DEADLINE_S
        self._body_consumed = False
        self._continued = False
        self.peer = None
        self.connection.settimeout(min(SOCKET_TIMEOUT_S, REQUEST_DEADLINE_S))

    def handle_expect_100(self) -> bool:
        # Too early to invite the body. _body sends 100 Continue after the token and Content-Length pass.
        return True

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler's signature
        who = self.peer["name"] if self.peer else self.client_address[0]
        self.server.log(printable(f"{self.command} {urlsplit(self.path).path} {format % args if args else format} "
                                  f"({who})"))

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        self.log_message("%s", code)

    def _send(self, status: HTTPStatus, body: Any, content_type: str = JSON,
              headers: dict[str, str] | None = None) -> None:
        self._send_bytes(status, json.dumps(body, ensure_ascii=False).encode(), content_type, headers)

    def _send_bytes(self, status: HTTPStatus, data: bytes, content_type: str | None,
                    headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        # Leaving an unread body on a keep-alive connection would desync the next request.
        unread = not self._body_consumed and (self.command == "POST" or self._has_body())
        if unread:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        if unread:
            self.wfile.flush()
            # The refusal is already on the wire. Drop only what has already arrived so the
            # kernel does not answer close() with a RST that hides this response.
            self._discard_buffered()

    def _has_body(self) -> bool:
        headers = getattr(self, "headers", None)
        if headers is None:
            return False
        return bool(headers.get_all("Transfer-Encoding")) or any(
            v.strip() != "0" for v in headers.get_all("Content-Length") or [])

    def _discard_buffered(self) -> None:
        previous = self.connection.gettimeout()
        self.connection.settimeout(0)
        dropped = 0
        try:
            while dropped < DISCARD_BUFFERED:
                chunk = self.rfile.read(min(8192, DISCARD_BUFFERED - dropped))
                if not chunk:
                    break
                dropped += len(chunk)
        except (TimeoutError, BlockingIOError, OSError):
            return
        finally:
            try:
                self.connection.settimeout(previous)
            except OSError:
                pass

    def _fail(self, exc: HTTPError) -> None:
        out = {"error": exc.code, "message": exc.message}
        if exc.details is not None:
            out["details"] = exc.details
        self._send(exc.status, out, exc.content_type, exc.headers)

    def _content_length(self, limit: int) -> int:
        """The declared length, or an error. Nothing is read from the socket here."""
        if self.headers.get_all("Transfer-Encoding"):
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "send a Content-Length and no Transfer-Encoding")
        values = self.headers.get_all("Content-Length") or []
        if not values:
            raise HTTPError(HTTPStatus.LENGTH_REQUIRED, "length_required", "Content-Length is required")
        if len(values) != 1:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "send a single Content-Length")
        raw = values[0].strip()
        if raw[:1] == "-":
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "Content-Length must not be negative")
        if re.fullmatch(r"[0-9]{1,10}", raw) is None:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "Content-Length must be a non-negative integer")
        length = int(raw)
        if length > limit:
            raise HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", f"bodies are limited to {limit} bytes")
        return length

    def _read_exact(self, length: int) -> bytes:
        """Read ``length`` bytes, giving up at the request deadline instead of whenever the client stops."""
        if length == 0:
            self._body_consumed = True
            return b""
        buf = bytearray()
        while len(buf) < length:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.close_connection = True
                raise HTTPError(HTTPStatus.REQUEST_TIMEOUT, "timeout", "the request took too long")
            self.connection.settimeout(min(SOCKET_TIMEOUT_S, remaining))
            try:
                chunk = self.rfile.read(min(64 * 1024, length - len(buf)))
            except TimeoutError:
                self.close_connection = True
                raise HTTPError(HTTPStatus.REQUEST_TIMEOUT, "timeout", "the request took too long") from None
            if not chunk:
                self.close_connection = True
                raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "the body ended before Content-Length bytes")
            buf += chunk
        self._body_consumed = True
        return bytes(buf)

    def _continue_if_expected(self) -> None:
        """Invite the body only once auth and a valid Content-Length have passed."""
        if self._continued:
            return
        if self.headers.get("Expect", "").strip().lower() != "100-continue":
            return
        self._continued = True
        self.send_response_only(HTTPStatus.CONTINUE)
        self.end_headers()
        self.wfile.flush()

    def _body(self, limit: int = MAX_BODY) -> Any:
        length = self._content_length(limit)
        self._continue_if_expected()
        raw = self._read_exact(length)
        try:
            return json.loads(raw or b"null", parse_constant=_not_json, parse_float=_finite)
        except (ValueError, RecursionError):  # not JSON or UTF-8, a number JSON can't hold, or nested too deep
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "the body is not JSON") from None

    def _bearer(self) -> str:
        headers = self.headers.get_all("Authorization") or []
        if len(headers) != 1:
            return ""
        header = headers[0]
        if len(header) > len("Bearer ") + MAX_TOKEN or not header.lower().startswith("bearer "):
            return ""
        token = header[7:].strip()
        if not token or len(token) > MAX_TOKEN or any(c.isspace() for c in token):
            return ""
        return token

    def _query(self) -> dict[str, str]:
        return last_values(parse_qs(urlsplit(self.path).query))
