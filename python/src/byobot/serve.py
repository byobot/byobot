"""``byobot serve``: sync with your phone over your own network.

The laptop running ``byobot serve`` is the hub. A phone pairs with it once,
then pulls the plan and pushes what it logged. Nothing goes through a cloud.

* **Transport.** HTTPS with a self-signed certificate that byobot creates on
  first run and keeps in ``$BYOBOT_HOME/serve``. The phone pins the SHA-256
  fingerprint of its public key, which it learns from the pairing QR code
  (or confirms by eye when pairing by hand), so nobody on the network can read
  or change what is synced.
* **Pairing.** ``byobot serve`` shows a one-time six-digit code, valid for ten
  minutes and five tries, as a QR code holding a ``byobot://pair`` link. The
  phone trades the code for a token; the store keeps only the token's hash.
  ``byobot device remove`` revokes a device.
* **Protocol (v1).** JSON over HTTPS under ``/v1``:

  ``GET /v1/hello``        who this is (no token needed)
  ``POST /v1/pair``        trade a pairing code for a token
  ``GET /v1/status``       a cheap authenticated check
  ``GET /v1/plan``         what the phone needs to train from a date window: each record with its payload and
                           the versions a change to it names as ``prev``
  ``POST /v1/events``      push events authored on the phone; each is accepted, a duplicate, or rejected
  ``POST /v1/samples``     push samples read from Apple Health, and the ones it says were deleted

  Pushed events must carry the paired device's id as their ``device_id`` and
  that device's user as their ``actor`` (``human`` / ``me``), and not write what only a connector writes.
  Anything else is rejected; a kind this byobot doesn't know yet is stored for a newer one. Pushing the same event twice is harmless, so a phone can retry freely.

  Calls that need a token are refused before the body is read. Plan and status
  reads are stopped if they run longer than a few seconds.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import socket
import sqlite3
import ssl
import sys
from contextlib import closing
from datetime import date, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from byobot import __version__, util, web
from byobot.core.contract import SCHEMA_VERSION, Contract, sync_errors
from byobot.core.relay import Cut, accept_samples, plan_window, push_events
from byobot.core.store import Store, busy, limit_time
from byobot.link import pairing_link
from byobot.util import ByobotError, Conflict, NotFound, ValidationFailed, db_path, home, profile
from byobot.web import PAIRING_TTL_S, HTTPError, Pairing, printable, timed_out

DEFAULT_PORT = 8765
MAX_PAIR_BODY = 4096  # pairing is unauthenticated, so its body stays small
PROTOCOL = 1
CAPABILITIES: tuple[str, ...] = ()  # features beyond protocol 1 this hub offers
QUERY_TIMEOUT_S = 5
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
DATE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])")


# -- certificate -------------------------------------------------------------

def serve_dir() -> Path:
    return home() / "serve"


def ensure_certificate(device_id: str, directory: Path | None = None) -> tuple[Path, Path, str]:
    """The server's certificate and key, made on first use. Returns (cert, key, public key fingerprint)."""
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

    cert_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    """Lowercase hex SHA-256 of the certificate's public key, as SubjectPublicKeyInfo DER (what the phone pins), so
    a certificate renewed with the same key keeps its phones."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    pem = cert_pem_or_der.startswith(b"-----")
    cert = x509.load_pem_x509_certificate(cert_pem_or_der) if pem else x509.load_der_x509_certificate(cert_pem_or_der)
    return hashlib.sha256(cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def short_fingerprint(fp: str) -> str:
    """The first 16 hex digits in groups of four, for comparing by eye."""
    return " ".join(fp[i:i + 4] for i in range(0, 16, 4))


# -- port --------------------------------------------------------------------

def choose_port() -> int:
    """The port this profile used last, else 8765 for the default profile, else a free one from 8767 to 8779 or
    8880 to 8979 (8780 to 8879 are remote's). The port is kept, so a paired phone keeps finding its laptop."""
    return util.choose_port("serve", DEFAULT_PORT, [*range(8767, 8780), *range(8880, 8980)])


def server_name() -> str:
    """What the phone shows: the laptop's name, and the profile when it isn't the default."""
    name = socket.gethostname() or "byobot"
    return f"{name} ({profile()})" if profile() else name


# -- pairing -----------------------------------------------------------------

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


# -- HTTP --------------------------------------------------------------------

BUSY = HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "busy", "the laptop's store is busy with a long write; try again")


class Server(web.Server):
    def __init__(self, address: tuple[str, int], db: Path, ssl_context: ssl.SSLContext | None,
                 pairing: Pairing | None, name: str, log: Callable[[str], None] | None = None):
        self.ssl_context = ssl_context
        with Store(db):
            pass
        super().__init__(address, Handler, db, pairing, name, log)

    def ids(self) -> tuple[str, str]:
        """This store's device and store ids, read now: a restore while serving takes on the backup's store id."""
        with closing(sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)) as conn:
            meta = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('device_id', 'store_id')"))
        return meta["device_id"], meta["store_id"]


class Handler(web.Handler):
    server: Server

    # Posts that require a bearer token. Checked before the body is read; /v1/pair is not one of them.
    _AUTH_POSTS = {"/v1/events", "/v1/samples"}

    def _auth(self, store: Store) -> dict:
        token = self._bearer()
        peer = store.peer_for_token(web.token_hash(token)) if token else None
        if peer is None:
            raise HTTPError(HTTPStatus.UNAUTHORIZED, "unauthorized", "pair this device with `byobot serve` first")
        self.peer = peer
        return peer

    def _guard_reads(self, store: Store) -> None:
        """Authenticate, then cap how long this connection's reads may run."""
        self._auth(store)
        limit_time(store.conn, QUERY_TIMEOUT_S)

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
        except sqlite3.OperationalError as exc:
            if not busy(exc):
                raise
            self._fail(BUSY)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch({"/v1/hello": self.hello, "/v1/status": self.status, "/v1/plan": self.plan})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/")
        if path in self._AUTH_POSTS:
            # Refuse a missing or unknown token before touching the body, however large it claims to be.
            try:
                with Store(self.server.db, migrate=False) as store:
                    self._auth(store)
            except HTTPError as exc:
                self._fail(exc)
                return
            except sqlite3.OperationalError as exc:
                if not busy(exc):
                    raise
                self._fail(BUSY)
                return
        self._dispatch({"/v1/pair": self.pair, "/v1/events": self.events, "/v1/samples": self.samples})

    # -- endpoints --

    def _server_info(self) -> dict:
        device_id, store_id = self.server.ids()
        return {"device_id": device_id, "store_id": store_id, "name": self.server.name}

    def hello(self) -> dict:
        pairing = self.server.pairing
        open_ = bool(pairing and pairing.open)
        server = self._server_info() if open_ else {**self._server_info(), "name": "byobot"}  # the name is often a person's
        return {"byobot": __version__, "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION,
                "server": server, "pairing_open": open_,
                "kind_versions": Contract.shipped().kind_versions(), "capabilities": list(CAPABILITIES)}

    def pair(self) -> dict:
        body = self._body(MAX_PAIR_BODY)
        if not isinstance(body, dict):
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "send {code, device_id, name}")
        device_id, name = body.get("device_id"), body.get("name")
        if not isinstance(device_id, str) or not ID.fullmatch(device_id):
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "device_id is not a valid byobot id")
        if not isinstance(name, str) or not 0 < len(name) <= 200:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "name must be 1 to 200 characters")
        if sync_errors("capabilities", body.get("capabilities", [])):
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "capabilities must be a list of names")
        with self.server.pairing_lock:
            pairing = self.server.pairing
            if pairing is not None and not pairing.open and self._removed(device_id):
                pairing = None
            if pairing is None or not pairing.admits(device_id):
                raise HTTPError(HTTPStatus.FORBIDDEN, "pairing_closed",
                                "pairing is closed; run `byobot serve --pair` for a new code")
            if not pairing.check(body.get("code"), device_id):
                raise HTTPError(HTTPStatus.FORBIDDEN, "wrong_code", "that code is not right",
                                {"tries_left": pairing.tries_left})
        token = secrets.token_urlsafe(32)
        with Store(self.server.db, migrate=False) as store:
            self.peer = store.pair(device_id, name, web.token_hash(token))
        self.server.log(printable(f"paired {name} ({device_id})"))
        return {"token": token, "server": self._server_info(), "device": self.peer}

    def _removed(self, device_id: str) -> bool:
        """A device removed after it spent the code can't send it again to pair back."""
        with Store(self.server.db, migrate=False) as store:
            peer = store.peer(device_id)
        return bool(peer and peer["revoked_at"])

    def status(self) -> dict:
        with Store(self.server.db, migrate=False) as store:
            self._guard_reads(store)
            try:
                counts = store.counts()
            except sqlite3.OperationalError as exc:
                timed_out(exc)
            return {"server": self._server_info(), "device": self.peer, "counts": counts,
                    "today": date.today().isoformat()}

    def plan(self) -> dict:
        q = self._query()
        today = date.today()
        start = q.get("from") or (today - timedelta(days=1)).isoformat()
        end = q.get("to") or (today + timedelta(days=13)).isoformat()
        with Store(self.server.db, migrate=False) as store:
            self._guard_reads(store)
            for label, value in (("from", start), ("to", end)):
                if not DATE.fullmatch(value):
                    raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", f"{label} must be a YYYY-MM-DD date")
            try:
                return plan_window(store, start, end, self.peer["device_id"])
            except sqlite3.OperationalError as exc:
                timed_out(exc)

    def events(self) -> dict:
        body = self._body()
        with Store(self.server.db, migrate=False) as store:
            peer = self._auth(store)
            try:
                return push_events(store, peer["device_id"], body)
            except Cut as exc:
                raise HTTPError(BUSY.status, BUSY.code, BUSY.message, {"results": exc.results}) from None

    def samples(self) -> dict:
        body = self._body()
        with Store(self.server.db, migrate=False) as store:
            self._auth(store)
            return accept_samples(store, body)


# -- running it --------------------------------------------------------------

def make_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT, *, pair: bool | None = None, tls: bool = True,
                db: Path | None = None, log: Callable[[str], None] | None = None) -> tuple[Server, dict]:
    """Build a server (not yet serving). ``pair=None`` opens pairing only when no device is paired yet. It listens on
    every interface by default, so a phone on the same network can reach it."""
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
            info["link"] = pairing_link(hosts=info["hosts"], port=info["port"], code=pairing.code,
                                        pin=info["fingerprint"])
    return server, info


def run(host: str, port: int | None, pair: bool | None) -> int:
    """The ``byobot serve`` command: print how to pair, then serve until Ctrl-C."""
    port = port or choose_port()
    try:
        server, info = make_server(host, port, pair=pair, log=web.log_line)
    except OSError as exc:
        raise ByobotError(f"could not listen on {host}:{port}: {exc}") from None
    (serve_dir() / "port").write_text(f"{info['port']}\n")
    err = sys.stderr
    err.write(f"byobot serve · {info['name']} · port {info['port']} · store {db_path()}\n")
    err.write(f"key fingerprint {short_fingerprint(info['fingerprint'])} (full: {info['fingerprint']})\n")
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
    return web.serve_until_stopped(server)
