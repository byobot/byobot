"""``byobot remote``: the MCP tools over HTTPS, for assistants that run somewhere else (a phone app, a chat service).

One profile per process. It listens on 127.0.0.1 and sits behind a tunnel you run
(Tailscale Funnel, Cloudflare Tunnel), which gives it a public HTTPS address. Your
data stays on this computer; the tunnel only carries requests.

* **MCP.** Streamable HTTP at ``/mcp``: the same tools and prompts as ``byobot mcp``,
  answered as JSON. A session id ties later requests to the client's ``initialize``.
* **Sign-in.** OAuth 2.1, as the MCP authorization spec asks: protected resource
  and authorization server metadata, dynamic client registration, the
  authorization code flow with PKCE (S256), and rotating refresh tokens. A client
  is approved by typing the one-time code ``byobot remote`` shows. Tokens are kept
  only as hashes, in this profile's own folder, so they never open another profile.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import sys
import time
from dataclasses import dataclass, field
from html import escape
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote_plus, urlencode, urlsplit

from byobot import mcp, serve
from byobot.commands import Context
from byobot.serve import Pairing
from byobot.util import PROFILE_NAME, ByobotError, ValidationFailed, db_path, home, profile

SCOPE = "byobot"
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 90 * 86400
CODE_TTL_S = 300
MAX_PENDING_CLIENTS = 50
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")


def remote_dir() -> Path:
    return home() / "remote"


def check_url(url: str) -> str:
    """The public base URL, without a trailing slash: https, or http only for this computer."""
    parts = urlsplit(url.strip())
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ValidationFailed("--url must be the https address your tunnel gives this profile")
    if not parts.hostname or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise ValidationFailed("--url must be just the address, such as https://example-laptop.example.ts.net")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _redirect_ok(uri: Any) -> bool:
    if not isinstance(uri, str) or len(uri) > 2000:
        return False
    parts = urlsplit(uri)
    if parts.fragment or not parts.hostname:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"))


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status

    def to_dict(self) -> dict:
        return {"error": self.error, "error_description": self.description}


@dataclass
class Grant:
    """An authorization code waiting to be exchanged."""

    client_id: str
    redirect_uri: str
    challenge: str
    expires_at: float = field(default_factory=lambda: time.monotonic() + CODE_TTL_S)


class Auth:
    """The authorization server's state for one profile, kept in ``oauth.json`` (mode 0600).

    Approved clients and token hashes live in the file, read on every check, so
    ``byobot remote --revoke`` takes effect while a server runs. Clients that have
    registered but not been approved yet, and authorization codes, stay in memory.
    """

    def __init__(self, path: Path, resource: str | None = None):
        self.path = path
        self.resource = resource
        self.lock = threading.Lock()
        self.pending: dict[str, dict] = {}
        self.grants: dict[str, Grant] = {}

    # -- the file --

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        return {"clients": data.get("clients", {}), "access": data.get("access", {}), "refresh": data.get("refresh", {}),
                "keys": data.get("keys", {})}

    def _save(self, data: dict) -> None:
        now = time.time()
        for kind in ("access", "refresh"):
            data[kind] = {h: t for h, t in data[kind].items() if t["expires_at"] > now}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    # -- clients --

    def register(self, meta: Any) -> dict:
        """RFC 7591 dynamic registration. The client is usable only once someone approves it."""
        if not isinstance(meta, dict):
            raise OAuthError("invalid_client_metadata", "send the client metadata as a JSON object")
        uris = meta.get("redirect_uris")
        if not isinstance(uris, list) or not 0 < len(uris) <= 10 or not all(_redirect_ok(u) for u in uris):
            raise OAuthError("invalid_redirect_uri", "redirect_uris must be 1 to 10 https (or localhost) addresses")
        method = meta.get("token_endpoint_auth_method", "client_secret_basic")
        if method not in AUTH_METHODS:
            raise OAuthError("invalid_client_metadata", f"token_endpoint_auth_method must be one of {', '.join(AUTH_METHODS)}")
        if not set(meta.get("grant_types", ["authorization_code"])) <= {"authorization_code", "refresh_token"}:
            raise OAuthError("invalid_client_metadata", "only authorization_code and refresh_token grants are supported")
        name = meta.get("client_name")
        name = name.strip()[:100] if isinstance(name, str) and name.strip() else "MCP client"
        client = {"client_id": secrets.token_urlsafe(16), "client_name": name, "redirect_uris": uris,
                  "token_endpoint_auth_method": method, "client_id_issued_at": int(time.time()),
                  "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": SCOPE}
        out = dict(client)
        if method != "none":
            out["client_secret"] = secrets.token_urlsafe(32)
            out["client_secret_expires_at"] = 0
            client["secret_sha256"] = _hash(out["client_secret"])
        with self.lock:
            while len(self.pending) >= MAX_PENDING_CLIENTS:
                self.pending.pop(next(iter(self.pending)))
            self.pending[client["client_id"]] = client
        return out

    def client(self, client_id: Any) -> dict | None:
        if not isinstance(client_id, str):
            return None
        with self.lock:
            return self._load()["clients"].get(client_id) or self.pending.get(client_id)

    def clients(self) -> list[dict]:
        with self.lock:
            return [{"client_id": c["client_id"], "client_name": c["client_name"]} for c in self._load()["clients"].values()]

    def authenticate(self, client_id: Any, secret: str | None) -> dict:
        client = self.client(client_id)
        if client is None:
            raise OAuthError("invalid_client", "unknown client", 401)
        if client["token_endpoint_auth_method"] != "none":
            if not secret or not secrets.compare_digest(_hash(secret), client["secret_sha256"]):
                raise OAuthError("invalid_client", "wrong client secret", 401)
        return client

    def check_resource(self, resource: Any) -> None:
        """RFC 8707: a client that names a resource must name this server."""
        if resource is None or self.resource is None:
            return
        base = self.resource.rsplit("/mcp", 1)[0]
        if not isinstance(resource, str) or resource.rstrip("/") not in (self.resource, base):
            raise OAuthError("invalid_target", f"this server only issues tokens for {self.resource}")

    # -- codes and tokens --

    def grant(self, client_id: str, redirect_uri: str, challenge: str) -> str:
        code = secrets.token_urlsafe(32)
        with self.lock:
            now = time.monotonic()
            self.grants = {k: g for k, g in self.grants.items() if g.expires_at > now}
            self.grants[_hash(code)] = Grant(client_id, redirect_uri, challenge)
        return code

    def exchange(self, client: dict, code: Any, redirect_uri: Any, verifier: Any) -> dict:
        with self.lock:
            grant = self.grants.pop(_hash(code), None) if isinstance(code, str) else None
        if grant is None or grant.expires_at < time.monotonic() or grant.client_id != client["client_id"]:
            raise OAuthError("invalid_grant", "the code is unknown, used or expired")
        if redirect_uri != grant.redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
        if not isinstance(verifier, str) or not 43 <= len(verifier) <= 128:
            raise OAuthError("invalid_grant", "code_verifier is required")
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if not secrets.compare_digest(expected, grant.challenge):
            raise OAuthError("invalid_grant", "code_verifier does not match")
        return self._issue(client)

    def refresh(self, client: dict, token: Any) -> dict:
        with self.lock:
            data = self._load()
            held = data["refresh"].pop(_hash(token), None) if isinstance(token, str) else None
            if held is None or held["expires_at"] < time.time() or held["client_id"] != client["client_id"]:
                raise OAuthError("invalid_grant", "the refresh token is unknown, used or expired")
            self._save(data)
        return self._issue(client)

    def _issue(self, client: dict) -> dict:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.pending.pop(client["client_id"], None)
            data = self._load()
            data["clients"][client["client_id"]] = dict(client)
            data["access"][_hash(access)] = {"client_id": client["client_id"], "expires_at": now + ACCESS_TTL_S}
            data["refresh"][_hash(refresh)] = {"client_id": client["client_id"], "expires_at": now + REFRESH_TTL_S}
            self._save(data)
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL_S,
                "refresh_token": refresh, "scope": SCOPE}

    def client_for_token(self, token: str) -> dict | None:
        """The client an access token or API key belongs to."""
        if not token:
            return None
        with self.lock:
            data = self._load()
        h = _hash(token)
        if h in data["keys"]:
            return {"client_id": "key:" + h[:16], "client_name": data["keys"][h]["name"]}
        held = data["access"].get(h)
        if held is None or held["expires_at"] < time.time():
            return None
        return data["clients"].get(held["client_id"])

    def create_key(self, name: str) -> str:
        """A long-lived API key, for assistants that call an API with a fixed key instead of signing in."""
        if not PROFILE_NAME.fullmatch(name):
            raise ValidationFailed(f"API key names are lowercase letters, digits and _, not {name!r}")
        key = "byobot_" + secrets.token_urlsafe(32)
        with self.lock:
            data = self._load()
            data["keys"][_hash(key)] = {"name": name, "created_at": int(time.time())}
            self._save(data)
        return key

    def revoke_all(self) -> int:
        with self.lock:
            data = self._load()
            count = len(data["clients"]) + len(data["keys"])
            self.pending.clear()
            self.grants.clear()
            self._save({"clients": {}, "access": {}, "refresh": {}, "keys": {}})
        return count


# -- HTTP --------------------------------------------------------------------

MAX_FORM = 8192
MAX_SESSIONS = 1000
QUERY_TIMEOUT_S = 20


class RemoteServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], base: str, db: Path, auth: Auth, pairing: Pairing | None,
                 name: str, log: Callable[[str], None] | None = None):
        self.base, self.db, self.auth, self.pairing, self.name = base, db, auth, pairing, name
        self.pairing_lock = threading.Lock()
        self.sessions: dict[str, dict] = {}
        self.sessions_lock = threading.Lock()
        self.log = log or (lambda line: None)
        super().__init__(address, Handler)

    @property
    def resource(self) -> str:
        return self.base + "/mcp"

    def finish_request(self, request: Any, client_address: Any) -> None:
        request.settimeout(serve.SOCKET_TIMEOUT_S)
        super().finish_request(request, client_address)

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(serve.Handler):
    server: RemoteServer  # type: ignore[assignment]

    # -- plumbing --

    def _html(self, status: HTTPStatus, title: str, body: str) -> None:
        page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<title>{escape(title)}</title><style>body{{font:16px/1.5 system-ui;max-width:28em;margin:2em auto;"
                f"padding:0 1em}}input,button{{font:inherit;padding:.4em}}</style><h1>{escape(title)}</h1>{body}")
        self._send_bytes(status, page.encode(), "text/html; charset=utf-8",
                         {"X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                          "frame-ancestors 'none'"})

    def _oauth_fail(self, exc: OAuthError) -> None:
        headers = {"WWW-Authenticate": 'Basic realm="byobot"'} if exc.status == 401 else None
        self._send_bytes(HTTPStatus(exc.status), json.dumps(exc.to_dict()).encode(), "application/json", headers)

    def _form(self) -> dict[str, str]:
        length = self._content_length(MAX_FORM)
        self._continue_if_expected()
        raw = self._read_exact(length).decode("utf-8", "replace")
        return {k: v[-1] for k, v in parse_qs(raw).items()}

    def _route(self) -> str:
        return urlsplit(self.path).path.rstrip("/") or "/"

    def _guard(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except OAuthError as exc:
            self._oauth_fail(exc)
        except serve.HTTPError as exc:
            self._fail(exc)

    def do_GET(self) -> None:  # noqa: N802
        routes = {"/.well-known/oauth-protected-resource": self.resource_metadata,
                  "/.well-known/oauth-protected-resource/mcp": self.resource_metadata,
                  "/.well-known/oauth-authorization-server": self.server_metadata,
                  "/oauth/authorize": self.authorize_page, "/mcp": self.mcp_not_allowed}
        self._guard(routes.get(self._route(), self.not_found))

    def do_POST(self) -> None:  # noqa: N802
        routes = {"/oauth/register": self.register, "/oauth/authorize": self.approve, "/oauth/token": self.token,
                  "/mcp": self.mcp}
        self._guard(routes.get(self._route(), self.not_found))

    def do_DELETE(self) -> None:  # noqa: N802
        self._guard(self.end_session if self._route() == "/mcp" else self.not_found)

    def not_found(self) -> None:
        raise serve.HTTPError(HTTPStatus.NOT_FOUND, "not_found", f"no such endpoint {self._route()}")

    # -- metadata --

    def resource_metadata(self) -> None:
        """RFC 9728."""
        self._send(HTTPStatus.OK, {"resource": self.server.resource, "authorization_servers": [self.server.base],
                                   "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"],
                                   "resource_name": f"byobot ({self.server.name})"})

    def server_metadata(self) -> None:
        """RFC 8414."""
        base = self.server.base
        self._send(HTTPStatus.OK, {
            "issuer": base, "authorization_endpoint": base + "/oauth/authorize", "token_endpoint": base + "/oauth/token",
            "registration_endpoint": base + "/oauth/register", "scopes_supported": [SCOPE],
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "authorization_response_iss_parameter_supported": True})

    # -- registration, approval, tokens --

    def register(self) -> None:
        self._send(HTTPStatus.CREATED, self.server.auth.register(self._body(MAX_FORM)))

    def _authorization(self, q: dict[str, str]) -> tuple[dict, str]:
        """The client and redirect URI of an authorization request. Errors here are shown, not redirected."""
        client = self.server.auth.client(q.get("client_id"))
        if client is None:
            raise _Shown("This connection request names an app byobot doesn't know. Start again from your assistant.")
        uris = client["redirect_uris"]
        uri = q.get("redirect_uri") or (uris[0] if len(uris) == 1 else None)
        if uri not in uris:
            raise _Shown("This connection request's return address doesn't match the app. Start again from your assistant.")
        return client, uri

    def _check_request(self, q: dict[str, str]) -> None:
        if q.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "response_type must be code")
        if q.get("code_challenge_method") != "S256" or not 43 <= len(q.get("code_challenge", "")) <= 128:
            raise OAuthError("invalid_request", "PKCE with code_challenge_method S256 is required")
        self.server.auth.check_resource(q.get("resource"))

    def authorize_page(self) -> None:
        self._authorize({k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()})

    def approve(self) -> None:
        self._authorize(self._form(), submitted=True)

    def _authorize(self, q: dict[str, str], submitted: bool = False) -> None:
        try:
            client, uri = self._authorization(q)
        except _Shown as exc:
            self._html(HTTPStatus.BAD_REQUEST, "Can't connect", f"<p>{escape(str(exc))}</p>")
            return
        try:
            self._check_request(q)
        except OAuthError as exc:
            self._redirect(uri, {"error": exc.error, "error_description": exc.description, "state": q.get("state")})
            return
        message = ""
        if submitted:
            with self.server.pairing_lock:
                pairing = self.server.pairing
                is_open = pairing is not None and pairing.open
                ok = is_open and pairing.check(q.get("approval_code", ""))
                tries = pairing.tries_left if pairing else 0
            if ok:
                code = self.server.auth.grant(client["client_id"], uri, q["code_challenge"])
                self.server.log(f"approved {client['client_name']}")
                self._redirect(uri, {"code": code, "state": q.get("state"), "iss": self.server.base})
                return
            message = "That code isn't right." if is_open and tries else ""
        if self.server.pairing is None or not self.server.pairing.open:
            self._html(HTTPStatus.FORBIDDEN, "Approval is closed",
                       "<p>Run <code>byobot remote --pair</code> on the computer that holds your byobot, "
                       "then connect again.</p>")
            return
        keep = "".join(f"<input type=hidden name='{escape(k)}' value='{escape(v)}'>" for k, v in q.items()
                       if k in ("response_type", "client_id", "redirect_uri", "code_challenge",
                                "code_challenge_method", "state", "scope", "resource"))
        self._html(HTTPStatus.FORBIDDEN if message else HTTPStatus.OK, "Connect to byobot",
                   f"<p><b>{escape(client['client_name'])}</b> wants to read and write your byobot "
                   f"({escape(self.server.name)}). It will return to {escape(urlsplit(uri).hostname or '')}.</p>"
                   f"<p>Enter the code <code>byobot remote</code> shows.</p>"
                   f"<form method=post action='/oauth/authorize'>{keep}"
                   f"<p><input name=approval_code inputmode=numeric autocomplete=one-time-code size=8 autofocus>"
                   f" <button>Connect</button></p></form><p>{escape(message)}</p>")

    def _redirect(self, uri: str, params: dict[str, Any]) -> None:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        self._send_bytes(HTTPStatus.FOUND, b"", None, {"Location": uri + ("&" if urlsplit(uri).query else "?") + query})

    def token(self) -> None:
        form = self._form()
        client_id, secret = form.get("client_id"), form.get("client_secret")
        basic = self.headers.get("Authorization", "")
        if basic.lower().startswith("basic "):
            try:
                pair = base64.b64decode(basic[6:].strip(), validate=True).decode()
                client_id, secret = (unquote_plus(x) for x in pair.split(":", 1))
            except (ValueError, UnicodeDecodeError):
                raise OAuthError("invalid_client", "malformed Basic credentials", 401) from None
        client = self.server.auth.authenticate(client_id, secret)
        self.server.auth.check_resource(form.get("resource"))
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            tokens = self.server.auth.exchange(client, form.get("code"), form.get("redirect_uri"),
                                               form.get("code_verifier"))
        elif grant_type == "refresh_token":
            tokens = self.server.auth.refresh(client, form.get("refresh_token"))
        else:
            raise OAuthError("unsupported_grant_type", "grant_type must be authorization_code or refresh_token")
        self._send(HTTPStatus.OK, tokens)

    # -- MCP --

    def _client(self) -> dict:
        token = self._bearer()
        client = self.server.auth.client_for_token(token) if token else None
        if client is None:
            challenge = f'Bearer resource_metadata="{self.server.base}/.well-known/oauth-protected-resource/mcp"'
            if token:
                challenge += ', error="invalid_token"'
            body = json.dumps({"error": "unauthorized", "message": "connect this assistant to byobot first"}).encode()
            self._send_bytes(HTTPStatus.UNAUTHORIZED, body, "application/json", {"WWW-Authenticate": challenge})
            raise _Sent
        self.peer = {"name": client["client_name"]}
        return client

    def _session(self, client: dict) -> dict | None:
        sid = self.headers.get("Mcp-Session-Id")
        if sid is None:
            return None
        with self.server.sessions_lock:
            session = self.server.sessions.get(sid)
        if session is None or session["client_id"] != client["client_id"]:
            raise serve.HTTPError(HTTPStatus.NOT_FOUND, "no_session", "unknown session; initialize again")
        return session

    def mcp(self) -> None:
        try:
            client = self._client()
        except _Sent:
            return
        version = self.headers.get("MCP-Protocol-Version")
        if version is not None and version not in mcp.SUPPORTED_VERSIONS:
            raise serve.HTTPError(HTTPStatus.BAD_REQUEST, "invalid", f"unsupported MCP-Protocol-Version {version}")
        message = self._body()
        initializing = isinstance(message, dict) and message.get("method") == "initialize"
        session = None if initializing else self._session(client)
        server = mcp.Server()
        server.client = session["client"] if session else {"name": client["client_name"]}
        if session:
            server.protocol = session["protocol"]
        server.ctx = Context(actor={}, path=self.server.db)
        try:
            serve.limit_query_time(server.ctx.store().conn, QUERY_TIMEOUT_S)
            reply = server.handle(message)
        finally:
            server.close()
        headers = {}
        if initializing and isinstance(reply, dict) and "result" in reply:
            sid = secrets.token_urlsafe(24)
            with self.server.sessions_lock:
                while len(self.server.sessions) >= MAX_SESSIONS:
                    self.server.sessions.pop(next(iter(self.server.sessions)))
                self.server.sessions[sid] = {"client_id": client["client_id"], "client": server.client,
                                             "protocol": server.protocol}
            headers["Mcp-Session-Id"] = sid
        if reply is None:
            self._send_bytes(HTTPStatus.ACCEPTED, b"", None, headers)
        else:
            self._send_bytes(HTTPStatus.OK, json.dumps(reply, ensure_ascii=False).encode(),
                             "application/json; charset=utf-8", headers)

    def mcp_not_allowed(self) -> None:
        self._send_bytes(HTTPStatus.METHOD_NOT_ALLOWED, b"", None, {"Allow": "POST, DELETE"})

    def end_session(self) -> None:
        try:
            client = self._client()
        except _Sent:
            return
        sid = self.headers.get("Mcp-Session-Id")
        if self._session(client) is not None:
            with self.server.sessions_lock:
                self.server.sessions.pop(sid, None)
        self._send_bytes(HTTPStatus.NO_CONTENT, b"", None)


# -- running it --------------------------------------------------------------

DEFAULT_PORT = 8780


def choose_port(host: str) -> int:
    """The port this profile used last, else 8780 for the default profile, else a free one from 8781 up."""
    saved = remote_dir() / "port"
    if saved.is_file():
        return int(saved.read_text().strip())
    if profile() is None:
        return DEFAULT_PORT
    for port in range(DEFAULT_PORT + 1, DEFAULT_PORT + 100):
        with socket.socket() as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise ByobotError("no free port for byobot remote; pass --port")


def make_server(base: str, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *, pair: bool | None = None,
                db: Path | None = None, log: Callable[[str], None] | None = None) -> RemoteServer:
    """Build a server (not yet serving). ``pair=None`` opens approval only when no client is connected yet."""
    auth = Auth(remote_dir() / "oauth.json", base + "/mcp")
    if pair is None:
        pair = not auth.clients()
    name = profile() or "default profile"
    return RemoteServer((host, port), base, db or db_path(), auth, Pairing() if pair else None, name, log)


def run(url: str | None, host: str, port: int | None, pair: bool | None, revoke: bool) -> int:
    """The ``byobot remote`` command."""
    saved = remote_dir() / "url"
    if revoke:
        count = Auth(remote_dir() / "oauth.json").revoke_all()
        sys.stdout.write(json.dumps({"disconnected": count}) + "\n")
        return 0
    if url is None and not saved.is_file():
        raise ValidationFailed("pass --url with the https address your tunnel gives this profile (saved after that)")
    base = check_url(url if url is not None else saved.read_text())
    remote_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    saved.write_text(base + "\n")

    def log(line: str) -> None:
        sys.stderr.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        sys.stderr.flush()

    port = port or choose_port(host)
    try:
        server = make_server(base, host, port, pair=pair, log=log)
    except OSError as exc:
        raise ByobotError(f"could not listen on {host}:{port}: {exc}") from None
    (remote_dir() / "port").write_text(f"{server.server_address[1]}\n")
    err = sys.stderr
    err.write(f"byobot remote · {server.name} · http://{host}:{server.server_address[1]} · store {server.db}\n")
    err.write(f"Point your tunnel's {base} at http://{host}:{server.server_address[1]}.\n")
    err.write(f"\nIn your assistant, add a custom connector with the URL {server.resource}\n")
    if server.pairing:
        err.write(f"When it asks for a code, enter {server.pairing.code} (good for {serve.PAIRING_TTL_S // 60} minutes).\n")
    else:
        names = ", ".join(c["client_name"] for c in server.auth.clients())
        err.write(f"Connected: {names}. Run with --pair to connect another, --revoke to disconnect all.\n")
    err.write("\nServing. Press Ctrl-C to stop.\n")
    err.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


class _Shown(Exception):
    """An authorization error shown on the page, because redirecting to an unchecked address is unsafe."""


class _Sent(Exception):
    """The response is already sent."""
