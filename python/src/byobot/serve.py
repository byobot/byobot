"""``byobot serve``: sync with your phone over your own network.

The laptop running ``byobot serve`` is the hub. A phone pairs with it once,
then pulls the plan and pushes what it logged. Nothing goes through a cloud.

* **Transport.** HTTPS with a self-signed certificate that byobot creates on
  first run and keeps in ``$BYOBOT_HOME/serve``. The phone pins the
  certificate's SHA-256 fingerprint, which it learns from the pairing QR code
  (or confirms by eye when pairing by hand), so nobody on the network can read
  or change what is synced.
* **Pairing.** ``byobot serve`` shows a one-time six-digit code, valid for ten
  minutes and five tries, as a QR code holding a ``byobot://pair`` link. The
  phone trades the code for a token; the store keeps only the token's hash.
  ``byobot unpair`` revokes a device.
* **Protocol (v0).** JSON over HTTPS under ``/v0``:

  ``GET /v0/hello``        who this is (no token needed)
  ``POST /v0/pair``        trade a pairing code for a token
  ``GET /v0/status``       a cheap authenticated check
  ``GET /v0/plan``         activities, plans, planned sessions in a date window, and efforts already logged
                           against them, all in contract shape
  ``POST /v0/events``      push events authored on the phone; each is accepted, a duplicate, or rejected
  ``POST /v0/telemetry``   push telemetry samples (body mass from HealthKit)

  Pushed events must carry the paired device's id as their ``device_id`` and
  that device's user as their ``actor`` (``human`` / ``me``). Anything else is
  rejected. Pushing the same event twice is harmless, so a phone can retry freely.

  Calls that need a token are refused before the body is read. Plan and status
  reads are stopped if they run longer than a few seconds.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, NoReturn
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from byobot import __version__, contract
from byobot.store import Store
from byobot.util import ByobotError, Conflict, NotFound, ValidationFailed, db_path, home, profile

DEFAULT_PORT = 8765
PAIRING_TTL_S = 600
PAIRING_TRIES = 5
MAX_BODY = 8 * 1024 * 1024
MAX_PAIR_BODY = 4096  # pairing is unauthenticated, so its body stays small
MAX_BATCH = 2000
MAX_TOKEN = 200
# Bytes already in the socket buffer that we may drop after a refusal, so close() does not RST
# the response away. We never wait for the client to send more.
DISCARD_BUFFERED = 64 * 1024
PROTOCOL = 0
SOCKET_TIMEOUT_S = 15  # idle cap so a stalled handshake or header cannot hold a thread
REQUEST_DEADLINE_S = 30  # whole-request cap, so a slow body cannot refresh the idle timer forever
QUERY_TIMEOUT_S = 5
QUERY_PROGRESS_INTERVAL = 10_000
# A paired phone logs the person's training. The body does not get to name another actor.
PAIRED_ACTOR = {"kind": "human", "id": "me"}


# -- certificate -------------------------------------------------------------

def serve_dir() -> Path:
    return home() / "serve"


def ensure_certificate(device_id: str, directory: Path | None = None) -> tuple[Path, Path, str]:
    """The server's certificate and key, made on first use. Returns (cert, key, SHA-256 fingerprint)."""
    directory = directory or serve_dir()
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    if not (cert_path.is_file() and key_path.is_file()):
        _make_certificate(device_id, cert_path, key_path)
    return cert_path, key_path, fingerprint(cert_path.read_bytes())


def _make_certificate(device_id: str, cert_path: Path, key_path: Path) -> None:
    from datetime import datetime, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"byobot {device_id}"[:64])])
    start = datetime.now(timezone.utc) - timedelta(days=1)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_bytes = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key_bytes)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def fingerprint(cert_pem_or_der: bytes) -> str:
    """Lowercase hex SHA-256 of the certificate's DER bytes (what the phone pins)."""
    der = ssl.PEM_cert_to_DER_cert(cert_pem_or_der.decode()) if cert_pem_or_der.startswith(b"-----") else cert_pem_or_der
    return hashlib.sha256(der).hexdigest()


def short_fingerprint(fp: str) -> str:
    """The first 16 hex digits in groups of four, for comparing by eye."""
    return " ".join(fp[i:i + 4] for i in range(0, 16, 4))


# -- port --------------------------------------------------------------------

def choose_port(host: str) -> int:
    """The port this profile used last, else 8765 for the default profile, else a free one from 8767 up.

    Named profiles never take 8765, and the port is kept, so a paired phone keeps finding its laptop.
    """
    saved = serve_dir() / "port"
    if saved.is_file():
        return int(saved.read_text().strip())
    if profile() is None:
        return DEFAULT_PORT
    for port in range(DEFAULT_PORT + 2, DEFAULT_PORT + 100):  # 8766 is WHOOP's sign-in callback
        with socket.socket() as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise ByobotError("no free port for byobot serve; pass --port")


def server_name() -> str:
    """What the phone shows: the laptop's name, and the profile when it isn't the default."""
    name = socket.gethostname() or "byobot"
    return f"{name} ({profile()})" if profile() else name


# -- pairing -----------------------------------------------------------------

@dataclass
class Pairing:
    """One pairing code: single use, short-lived, a few tries."""

    code: str = field(default_factory=lambda: f"{secrets.randbelow(10**6):06d}")
    expires_at: float = field(default_factory=lambda: time.monotonic() + PAIRING_TTL_S)
    tries_left: int = PAIRING_TRIES
    used: bool = False

    @property
    def open(self) -> bool:
        return not self.used and self.tries_left > 0 and time.monotonic() < self.expires_at

    def check(self, code: Any) -> bool:
        if not self.open:
            return False
        if isinstance(code, str) and secrets.compare_digest(code.replace(" ", "").replace("-", ""), self.code):
            self.used = True
            return True
        self.tries_left -= 1
        return False


def pairing_link(hosts: list[str], port: int, code: str, fp: str) -> str:
    """``byobot://pair?v=0&host=...&port=...&code=...&fp=...``; ``fp`` is the fingerprint in unpadded base64url."""
    fp64 = base64.urlsafe_b64encode(bytes.fromhex(fp)).decode().rstrip("=")
    query = [("v", str(PROTOCOL))] + [("host", h) for h in hosts] + [("port", str(port)), ("code", code), ("fp", fp64)]
    return "byobot://pair?" + urlencode(query, quote_via=quote)


def parse_pairing_link(link: str) -> dict:
    """The inverse of :func:`pairing_link` (the app has its own; this one is for tests and tools)."""
    parts = urlsplit(link)
    if parts.scheme != "byobot" or parts.netloc != "pair":
        raise ValueError("not a byobot pairing link")
    q = parse_qs(parts.query)
    fp = base64.urlsafe_b64decode(q["fp"][0] + "=" * (-len(q["fp"][0]) % 4)).hex()
    return {"v": int(q["v"][0]), "hosts": q.get("host", []), "port": int(q["port"][0]), "code": q["code"][0], "fp": fp}


def lan_hosts() -> list[str]:
    """Addresses a phone on the same network can try: the mDNS name, then the main IPv4 address."""
    hosts = []
    name = socket.gethostname()
    if name and name != "localhost":
        hosts.append(name if name.endswith(".local") else f"{name.split('.')[0]}.local")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this picks the outgoing interface
            ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            hosts.append(ip)
    except OSError:
        pass
    return hosts or ["127.0.0.1"]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# -- what the phone pulls ----------------------------------------------------

def _payload_of(store: Store, event_id: str) -> dict:
    row = store.conn.execute("SELECT payload FROM events WHERE id = ?", (event_id,)).fetchone()
    return json.loads(row[0])


def plan_window(store: Store, start: str, end: str) -> dict:
    """Everything the phone needs to run the sessions scheduled from ``start`` to ``end`` (inclusive).

    Activities, locations, plans and planned sessions come back exactly as their
    current event wrote them, so the phone decodes them with the contract types.
    Efforts already logged against those sessions come back as performed efforts,
    each with its location (its session's when it has none of its own), and so do
    the last two times each planned activity was done at each location, so the
    phone can show last time at the gym you're in.
    """
    activities = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM activities ORDER BY id")]
    locations = [_payload_of(store, r["event_id"]) for r in store.rows("SELECT event_id FROM locations ORDER BY name, id")]
    sessions = [_payload_of(store, r["event_id"]) for r in store.rows(
        "SELECT event_id FROM planned_sessions WHERE scheduled_on >= ? AND scheduled_on <= ?"
        " ORDER BY scheduled_on, name, id", [start, end])]
    plan_ids = sorted({s["plan_id"] for s in sessions if "plan_id" in s})
    plans = [_payload_of(store, r["event_id"]) for r in store.rows(
        "SELECT event_id FROM plans WHERE id IN (SELECT value FROM json_each(?))"
        " OR (starts_on <= ? AND (ends_on IS NULL OR ends_on >= ?)) ORDER BY starts_on, id",
        [json.dumps(plan_ids), end, start])]
    efforts = [store.effort_payload(r) for r in store.rows(
        f"SELECT {_EFFORT_COLUMNS} FROM performed_efforts pf {_SESSION_JOIN}"
        " JOIN planned_efforts pe ON pe.id = pf.planned_effort_id JOIN planned_sessions s ON s.id = pe.planned_session_id"
        " WHERE s.scheduled_on >= ? AND s.scheduled_on <= ? ORDER BY pf.performed_at, pf.id", [start, end])]
    seen = {e["id"] for e in efforts}
    efforts += [e for e in _recent_efforts(store, start, end) if e["id"] not in seen]
    return {"from": start, "to": end, "activities": activities, "locations": locations, "plans": plans,
            "sessions": sessions, "efforts": efforts}


def _recent_efforts(store: Store, start: str, end: str, times: int = 2) -> list[dict]:
    """Efforts from the last `times` sessions each activity planned in the window was done in, per location."""
    rows = store.rows(f"""
        WITH planned AS (
          SELECT DISTINCT pe.activity_id FROM planned_efforts pe JOIN planned_sessions s ON s.id = pe.planned_session_id
          WHERE s.scheduled_on >= ? AND s.scheduled_on <= ?),
        done AS (
          SELECT {_EFFORT_COLUMNS}, coalesce(pf.session_id, pf.id) AS occasion
          FROM performed_efforts pf {_SESSION_JOIN} WHERE pf.activity_id IN (SELECT activity_id FROM planned)),
        occasions AS (
          SELECT activity_id, location_id, occasion, dense_rank() OVER (
            PARTITION BY activity_id, coalesce(location_id, '') ORDER BY max(performed_at) DESC, occasion DESC) AS n
          FROM done GROUP BY activity_id, location_id, occasion)
        SELECT d.* FROM done d JOIN occasions o ON o.activity_id = d.activity_id AND o.occasion = d.occasion
          AND coalesce(o.location_id, '') = coalesce(d.location_id, '')
        WHERE o.n <= ? ORDER BY d.performed_at, d.id""", [start, end, times])
    return [store.effort_payload(r) for r in rows]


# A performed effort's columns, with its session's location when it has none of its own.
_EFFORT_COLUMNS = ("pf.id, pf.session_id, pf.activity_id, pf.performed_at, pf.planned_effort_id, pf.round, pf.side,"
                   " pf.notes, coalesce(pf.location_id, ps.location_id) AS location_id")
_SESSION_JOIN = "LEFT JOIN performed_sessions ps ON ps.id = pf.session_id"


def actor_for_peer(peer: dict) -> dict:
    """The actor a pushed event must already name (``human`` / ``me``). A mismatch is rejected; the body is not rewritten."""
    if not peer.get("device_id"):
        raise HTTPError(HTTPStatus.UNAUTHORIZED, "unauthorized", "pair this device with `byobot serve` first")
    return dict(PAIRED_ACTOR)


def limit_query_time(conn: sqlite3.Connection, seconds: float | None = None,
                     interval: int | None = None) -> None:
    """Abort reads that run longer than ``seconds``. SQLite then raises ``OperationalError('interrupted')``.

    Defaults are read when a request runs, so tests can tighten them.
    """
    if seconds is None:
        seconds = QUERY_TIMEOUT_S
    if interval is None:
        interval = QUERY_PROGRESS_INTERVAL
    deadline = time.monotonic() + seconds

    def progress() -> int:
        return 1 if time.monotonic() >= deadline else 0

    conn.set_progress_handler(progress, interval)


def _query_interrupted(exc: sqlite3.OperationalError) -> NoReturn:
    if "interrupt" in str(exc).lower():
        raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "timeout",
                        "the query took too long and was stopped") from None
    raise exc


def accept_events(store: Store, device_id: str, events: list, actor: dict) -> list[dict]:
    """Accept pushed events one by one, in order. Retrying an event already accepted is harmless.

    ``device_id`` and ``actor`` are the authenticated peer's. An event that names
    another device or actor is rejected and not written.
    """
    results = []
    for i, event in enumerate(events):
        event_id = event.get("id") if isinstance(event, dict) else None
        result: dict[str, Any] = {"index": i, "id": event_id}
        if not isinstance(event, dict):
            results.append({**result, "status": "rejected", "errors": [{"path": "$", "message": "not an object"}]})
            continue
        if event.get("device_id") != device_id:
            results.append({**result, "status": "rejected", "errors": [
                {"path": "$.device_id", "message": "events must be authored by the paired device that pushes them"}]})
            continue
        if event.get("actor") != actor:
            results.append({**result, "status": "rejected", "errors": [
                {"path": "$.actor", "message": "events must be authored by the paired device's user"}]})
            continue
        existing = store.stored_event(event_id) if isinstance(event_id, str) else None
        if existing is not None:
            same = existing == event
            results.append({**result, "status": "duplicate" if same else "rejected", **({} if same else {
                "errors": [{"path": "$.id", "message": "a different event with this id already exists"}]})})
            continue
        try:
            store.accept(event)
        except ByobotError as exc:
            errors = exc.details if isinstance(exc.details, list) else [{"path": "$", "message": exc.message}]
            results.append({**result, "status": "rejected", "errors": errors})
            continue
        results.append({**result, "status": "accepted"})
    return results


def accept_samples(store: Store, samples: list) -> dict:
    """Ingest the valid samples; report the rest. One bad sample does not hold back the others."""
    good, rejected = [], []
    for i, sample in enumerate(samples):
        errors = contract.validate("telemetry", sample)
        if errors:
            rejected.append({"index": i, "errors": errors})
        else:
            good.append(sample)
    counts = store.ingest(good) if good else {"inserted": 0, "updated": 0, "unchanged": 0}
    return {**counts, "rejected": rejected}


# -- HTTP --------------------------------------------------------------------

class HTTPError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], db: Path, ssl_context: ssl.SSLContext | None,
                 pairing: Pairing | None, name: str, log: Callable[[str], None] | None = None):
        self.db = db
        self.ssl_context = ssl_context
        self.pairing = pairing
        self.pairing_lock = threading.Lock()
        self.name = name
        self.log = log or (lambda line: None)
        with Store(db) as store:
            self.device_id = store.device_id
        super().__init__(address, Handler)

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
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, ssl.SSLError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"
    server_version = f"byobot/{__version__}"
    peer: dict | None = None

    # Posts that require a bearer token. Checked before the body is read; /v0/pair is not one of them.
    _AUTH_POSTS = {"/v0/events", "/v0/telemetry"}

    # -- plumbing --

    def setup(self) -> None:
        super().setup()
        self.deadline = time.monotonic() + REQUEST_DEADLINE_S
        self._body_consumed = False
        self._continued = False
        self.connection.settimeout(min(SOCKET_TIMEOUT_S, REQUEST_DEADLINE_S))

    def handle_expect_100(self) -> bool:
        # Too early to invite the body. _body sends 100 Continue after the token and Content-Length pass.
        return True

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler's signature
        who = self.peer["name"] if self.peer else self.client_address[0]
        self.server.log(f"{self.command} {urlsplit(self.path).path} {format % args if args else format} ({who})")

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        self.log_message("%s", code)

    def _send(self, status: HTTPStatus, body: Any) -> None:
        self._send_bytes(status, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")

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
        unread = self.command == "POST" and not self._body_consumed
        if unread:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        if unread:
            self.wfile.flush()
            # The refusal is already on the wire. Drop only what has already arrived so the
            # kernel does not answer close() with a RST that hides this response.
            self._discard_buffered()

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
        self._send(exc.status, out)

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
            return json.loads(raw or b"null")
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
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

    def _auth(self, store: Store) -> dict:
        token = self._bearer()
        peer = store.peer_for_token(token_hash(token)) if token else None
        if peer is None:
            raise HTTPError(HTTPStatus.UNAUTHORIZED, "unauthorized", "pair this device with `byobot serve` first")
        self.peer = peer
        return peer

    def _guard_reads(self, store: Store) -> None:
        """Authenticate, then cap how long this connection's reads may run."""
        self._auth(store)
        limit_query_time(store.conn)

    def _dispatch(self, routes: dict[str, Callable[[], Any]]) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        try:
            route = routes.get(path)
            if route is None:
                raise HTTPError(HTTPStatus.NOT_FOUND, "not_found", f"no such endpoint {path}")
            self._send(HTTPStatus.OK, route())
        except HTTPError as exc:
            self._fail(exc)
        except NotFound as exc:
            self._send(HTTPStatus.NOT_FOUND, exc.to_dict())
        except (ValidationFailed, Conflict) as exc:
            self._send(HTTPStatus.BAD_REQUEST, exc.to_dict())

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch({"/v0/hello": self.hello, "/v0/status": self.status, "/v0/plan": self.plan})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/")
        if path in self._AUTH_POSTS:
            # Refuse a missing or unknown token before touching the body, however large it claims to be.
            try:
                with Store(self.server.db) as store:
                    self._auth(store)
            except HTTPError as exc:
                self._fail(exc)
                return
        self._dispatch({"/v0/pair": self.pair, "/v0/events": self.events, "/v0/telemetry": self.telemetry})

    def _query(self) -> dict[str, str]:
        return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

    # -- endpoints --

    def _server_info(self) -> dict:
        return {"device_id": self.server.device_id, "name": self.server.name}

    def hello(self) -> dict:
        pairing = self.server.pairing
        return {"byobot": __version__, "protocol": PROTOCOL, "schema_version": contract.SCHEMA_VERSION,
                "server": self._server_info(), "pairing_open": bool(pairing and pairing.open)}

    def pair(self) -> dict:
        body = self._body(MAX_PAIR_BODY)
        if not isinstance(body, dict):
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "send {code, device_id, name}")
        device_id, name = body.get("device_id"), body.get("name")
        id_errors = contract.validate_value("id", device_id)
        if id_errors:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "device_id is not a valid byobot id", id_errors)
        if not isinstance(name, str) or not 0 < len(name) <= 200:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "name must be 1 to 200 characters")
        with self.server.pairing_lock:
            pairing = self.server.pairing
            if pairing is None or not pairing.open:
                raise HTTPError(HTTPStatus.FORBIDDEN, "pairing_closed",
                                "pairing is closed; run `byobot serve --pair` for a new code")
            if not pairing.check(body.get("code")):
                raise HTTPError(HTTPStatus.FORBIDDEN, "wrong_code", "that code is not right",
                                {"tries_left": pairing.tries_left})
        token = secrets.token_urlsafe(32)
        with Store(self.server.db) as store:
            self.peer = store.pair_peer(device_id, name, token_hash(token))
        self.server.log(f"paired {name} ({device_id})")
        return {"token": token, "server": self._server_info(), "device": self.peer}

    def status(self) -> dict:
        with Store(self.server.db) as store:
            self._guard_reads(store)
            try:
                counts = store.counts()
            except sqlite3.OperationalError as exc:
                _query_interrupted(exc)
            return {"server": self._server_info(), "device": self.peer, "counts": counts,
                    "today": date.today().isoformat()}

    def plan(self) -> dict:
        q = self._query()
        today = date.today()
        start = q.get("from") or (today - timedelta(days=1)).isoformat()
        end = q.get("to") or (today + timedelta(days=13)).isoformat()
        with Store(self.server.db) as store:
            self._guard_reads(store)
            for label, value in (("from", start), ("to", end)):
                if contract.validate_value("date", value):
                    raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", f"{label} must be a YYYY-MM-DD date")
            try:
                return plan_window(store, start, end)
            except sqlite3.OperationalError as exc:
                _query_interrupted(exc)

    def events(self) -> dict:
        body = self._body()
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list) or len(events) > MAX_BATCH:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", f"send {{events: [...]}} with at most {MAX_BATCH} events")
        with Store(self.server.db) as store:
            peer = self._auth(store)
            results = accept_events(store, peer["device_id"], events, actor_for_peer(peer))
        counts = {s: sum(r["status"] == s for r in results) for s in ("accepted", "duplicate", "rejected")}
        return {"results": results, **counts}

    def telemetry(self) -> dict:
        body = self._body()
        samples = body.get("samples") if isinstance(body, dict) else None
        if not isinstance(samples, list) or len(samples) > MAX_BATCH:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", f"send {{samples: [...]}} with at most {MAX_BATCH} samples")
        with Store(self.server.db) as store:
            self._auth(store)
            return accept_samples(store, samples)


# -- running it --------------------------------------------------------------

def make_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT, *, pair: bool | None = None, tls: bool = True,
                db: Path | None = None, log: Callable[[str], None] | None = None) -> tuple[Server, dict]:
    """Build a server (not yet serving). ``pair=None`` opens pairing only when no device is paired yet."""
    db = db or db_path()
    with Store(db) as store:
        device_id = store.device_id
        if pair is None:
            pair = not any(p["revoked_at"] is None for p in store.peers())
    info: dict[str, Any] = {"device_id": device_id, "name": server_name()}
    context = None
    if tls:
        cert, key, fp = ensure_certificate(device_id)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        info.update(cert=str(cert), fingerprint=fp)
    pairing = Pairing() if pair else None
    server = Server((host, port), db, context, pairing, info["name"], log)
    info["port"] = server.server_address[1]
    if pairing:
        info["code"] = pairing.code
        if tls:
            info["hosts"] = lan_hosts()
            info["link"] = pairing_link(info["hosts"], info["port"], pairing.code, info["fingerprint"])
    return server, info


def run(host: str, port: int | None, pair: bool | None) -> int:
    """The ``byobot serve`` command: print how to pair, then serve until Ctrl-C."""
    def log(line: str) -> None:
        sys.stderr.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        sys.stderr.flush()

    port = port or choose_port(host)
    try:
        server, info = make_server(host, port, pair=pair, log=log)
    except OSError as exc:
        sys.stderr.write(json.dumps({"error": "serve", "message": f"could not listen on {host}:{port}: {exc}"}) + "\n")
        return 1
    (serve_dir() / "port").write_text(f"{info['port']}\n")
    err = sys.stderr
    err.write(f"byobot serve · {info['name']} · port {info['port']} · store {db_path()}\n")
    err.write(f"certificate fingerprint {short_fingerprint(info['fingerprint'])} (full: {info['fingerprint']})\n")
    if "link" in info:
        err.write("\nScan this with your iPhone camera to pair the byobot app:\n\n")
        try:
            import segno

            segno.make(info["link"], error="l").terminal(out=err, compact=True)
        except Exception:  # pragma: no cover - a terminal that cannot draw the code still gets the link
            pass
        err.write(f"\nOr enter it by hand: host {' or '.join(info['hosts'])}, port {info['port']}, code {info['code']}"
                  f" (good for {PAIRING_TTL_S // 60} minutes)\n")
        err.write(f"Link: {info['link']}\n")
    else:
        err.write("Pairing is closed (a device is already paired). Run `byobot serve --pair` to pair another.\n")
    err.write("\nServing. Press Ctrl-C to stop.\n")
    err.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
