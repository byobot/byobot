"""``byobot remote``: the MCP tools over HTTPS, for assistants that run somewhere else (a phone app, a chat service).

One profile per process. It listens on 127.0.0.1 and sits behind a tunnel you run
(Tailscale Funnel, Cloudflare Tunnel), which gives it a public HTTPS address. Your
data stays on this computer; the tunnel only carries requests.

* **MCP.** Streamable HTTP at ``/mcp``: the same tools and prompts as ``byobot mcp``,
  answered as JSON, without sessions: the token says who calls and the header which protocol.
* **Sign-in.** OAuth 2.1, as the MCP authorization spec asks: protected resource
  and authorization server metadata, dynamic client registration, the
  authorization code flow with PKCE (S256), and rotating refresh tokens. A client
  is approved by typing the one-time code ``byobot remote serve`` shows. Tokens are kept
  only as hashes, in this profile's own folder, so they never open another profile.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote_plus, urlencode, urlsplit

from byobot import __version__, commands, mcp, oauth, util, web
from byobot.lock import held
from byobot.core.store import interrupted, limit_time, stamp
from byobot.util import PROFILE_NAME, ByobotError, NotFound, ValidationFailed, db_path, home, profile
from byobot.web import PAIRING_TTL_S, HTTPError, Pairing, printable, timed_out

READ, FULL = "read", "read write"  # a client with only read may call read-only tools
SCOPES = ("read", "write")
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 90 * 86400
CODE_TTL_S = 300
MAX_PENDING_CLIENTS = 50
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def remote_dir() -> Path:
    return home() / "remote"


def check_url(url: str) -> str:
    """The public base URL, without a trailing slash: https, or http only for this computer."""
    parts = urlsplit(url.strip())
    local = parts.hostname in LOOPBACK
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ValidationFailed("--url must be the https address your tunnel gives this profile")
    if not parts.hostname or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise ValidationFailed("--url must be just the address, such as https://example-laptop.example.ts.net")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _redirect_ok(uri: Any) -> bool:
    if not isinstance(uri, str) or len(uri) > 2000:
        return False
    parts = urlsplit(uri)
    if parts.fragment or not parts.hostname:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK)


def requested_scope(scope: Any) -> str:
    """What a client asked for: read alone when it names read and not write, else what it could always do."""
    words = set(scope.split()) if isinstance(scope, str) else set()
    return READ if "read" in words and "write" not in words else FULL


def scope_of(client: dict) -> str:
    """A client's scope. Clients approved before scopes existed hold "byobot", which is everything."""
    return READ if client.get("scope") == READ else FULL


def actor_id(client: dict) -> str:
    """The author of a client's writes: its name and its own id, so two clients that call themselves alike differ."""
    return f"{client['client_name'][:100]}#{client['client_id']}"


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
    generation: int  # removing an assistant moves oauth.json past this, and the code with it
    named: bool = True  # whether the request named redirect_uri, so the token request must too (RFC 6749 4.1.3)
    scope: str = FULL
    expires_at: float = field(default_factory=lambda: time.monotonic() + CODE_TTL_S)


class Auth:
    """The authorization server's state for one profile, kept in ``oauth.json`` (mode 0600).

    Approved clients and token hashes live in the file, read on every check, so
    ``byobot device remove`` takes effect while a server runs. Clients that have
    registered but not been approved yet, and authorization codes, stay in memory.
    A change is written only if no other process wrote the file since it was read.
    """

    def __init__(self, path: Path, resource: str | None = None):
        self.path = path
        self.resource = resource
        self.lock = threading.Lock()
        self.pending: dict[str, dict] = {}
        self.grants: dict[str, Grant] = {}

    # -- the file --

    def _raw(self) -> bytes:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return b""

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        return {"clients": data.get("clients", {}), "access": data.get("access", {}), "refresh": data.get("refresh", {}),
                "used": data.get("used", {}), "keys": data.get("keys", {}), "generation": data.get("generation", 0)}

    def _save(self, data: dict) -> None:
        now = time.time()
        for kind in ("access", "refresh", "used"):
            data[kind] = {h: t for h, t in data[kind].items() if t["expires_at"] > now}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")  # mode 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _change(self, fn: Callable[[dict], Any]) -> Any:
        """Apply ``fn`` to the file's data and write it, again from a fresh read if another process wrote it meanwhile.
        ``fn`` returns its result, or an OAuthError to raise once the change is written."""
        with self.lock:
            while True:
                seen = self._raw()
                data = self._load()
                out = fn(data)
                with held(self.path.with_name(self.path.name + ".lock")):
                    if self._raw() == seen:
                        self._save(data)
                        break
        if isinstance(out, OAuthError):
            raise out
        return out

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
        grants = meta.get("grant_types", ["authorization_code"])
        if not isinstance(grants, list) or not set(map(str, grants)) <= {"authorization_code", "refresh_token"}:
            raise OAuthError("invalid_client_metadata", "only authorization_code and refresh_token grants are supported")
        name = meta.get("client_name")
        name = name.strip()[:100] if isinstance(name, str) and name.strip() else "MCP client"
        client = {"client_id": secrets.token_hex(16), "client_name": name, "redirect_uris": uris,
                  "token_endpoint_auth_method": method, "client_id_issued_at": int(time.time()),
                  "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": FULL}
        out = dict(client)
        if method != "none":
            out["client_secret"] = secrets.token_urlsafe(32)
            out["client_secret_expires_at"] = 0
            client["secret_sha256"] = web.token_hash(out["client_secret"])
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

    def keys(self) -> list[dict]:
        with self.lock:
            return [{"name": k["name"]} for k in self._load()["keys"].values()]

    def authenticate(self, client_id: Any, secret: str | None) -> dict:
        client = self.client(client_id)
        if client is None:
            raise OAuthError("invalid_client", "unknown client", 401)
        if client["token_endpoint_auth_method"] != "none":
            if not secret or not secrets.compare_digest(web.token_hash(secret), client["secret_sha256"]):
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

    def grant(self, client_id: str, redirect_uri: str, challenge: str, named: bool = True, scope: str = FULL) -> str:
        code = secrets.token_urlsafe(32)
        with self.lock:
            generation = self._load()["generation"]
            now = time.monotonic()
            self.grants = {k: g for k, g in self.grants.items() if g.expires_at > now}
            self.grants[web.token_hash(code)] = Grant(client_id, redirect_uri, challenge, generation, named, scope)
        return code

    def exchange(self, client: dict, code: Any, redirect_uri: Any, verifier: Any) -> dict:
        with self.lock:
            grant = self.grants.pop(web.token_hash(code), None) if isinstance(code, str) else None
        if grant is None or grant.expires_at < time.monotonic() or grant.client_id != client["client_id"]:
            raise OAuthError("invalid_grant", "the code is unknown, used or expired")
        if redirect_uri != grant.redirect_uri and (grant.named or redirect_uri is not None):
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
        if not isinstance(verifier, str) or not 43 <= len(verifier) <= 128:
            raise OAuthError("invalid_grant", "code_verifier is required")
        if not secrets.compare_digest(oauth.pkce_challenge(verifier), grant.challenge):
            raise OAuthError("invalid_grant", "code_verifier does not match")
        return self._issue({**client, "scope": grant.scope}, grant.generation)

    def refresh(self, client: dict, token: Any) -> dict:
        h, cid = web.token_hash(token) if isinstance(token, str) else None, client["client_id"]

        def rotate(data: dict) -> OAuthError | None:
            if h in data["used"] and data["used"][h]["client_id"] == cid:  # a replay, so the token was copied
                for kind in ("access", "refresh"):
                    data[kind] = {k: t for k, t in data[kind].items() if t["client_id"] != cid}
                return OAuthError("invalid_grant", "the refresh token was already used; connect this assistant again")
            held = data["refresh"].pop(h, None)
            if held is None or held["expires_at"] < time.time() or held["client_id"] != cid:
                raise OAuthError("invalid_grant", "the refresh token is unknown, used or expired")
            data["used"][h] = held
            return None

        self._change(rotate)
        return self._issue(client)

    def _issue(self, client: dict, generation: int | None = None) -> dict:
        """Tokens for an approved client, or, with its code's generation, for one approved since the last removal."""
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        cid, now = client["client_id"], time.time()

        def add(data: dict) -> None:
            if (cid not in data["clients"]) if generation is None else (generation != data["generation"]):
                raise OAuthError("invalid_grant", "this assistant was disconnected; connect it again")
            data["clients"][cid] = dict(client)
            data["access"][web.token_hash(access)] = {"client_id": cid, "expires_at": now + ACCESS_TTL_S}
            data["refresh"][web.token_hash(refresh)] = {"client_id": cid, "expires_at": now + REFRESH_TTL_S}

        self._change(add)
        with self.lock:
            self.pending.pop(cid, None)
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL_S,
                "refresh_token": refresh, "scope": scope_of(client)}

    def client_for_token(self, token: str) -> dict | None:
        """The client an access token or API key belongs to."""
        if not token:
            return None
        with self.lock:
            data = self._load()
        h = web.token_hash(token)
        if h in data["keys"]:
            key = data["keys"][h]
            return {"client_id": "key:" + h[:16], "client_name": key["name"], "scope": key.get("scope", FULL)}
        held = data["access"].get(h)
        if held is None or held["expires_at"] < time.time():
            return None
        return data["clients"].get(held["client_id"])

    def create_key(self, name: str, scope: str = FULL) -> str:
        """A long-lived API key, for assistants that call an API with a fixed key instead of signing in."""
        if not PROFILE_NAME.fullmatch(name):
            raise ValidationFailed(f"API key names are lowercase letters, digits and _, not {name!r}")
        key = "byobot_" + secrets.token_urlsafe(32)

        def add(data: dict) -> None:
            data["keys"][web.token_hash(key)] = {"name": name, "created_at": int(time.time()), "scope": scope}

        self._change(add)
        return key

    def assistants(self) -> list[dict]:
        """Every assistant that can reach the store: a signed-in client by its client_id, an API key as key:<hash>."""
        with self.lock:
            data = self._load()
        def since(at: int | None) -> str | None:
            return at and stamp(datetime.fromtimestamp(at, timezone.utc))

        signed_in = [{"id": c["client_id"], "name": c["client_name"], "route": "sign_in",
                      "since": since(c.get("client_id_issued_at")), "read_only": scope_of(c) == READ}
                     for c in data["clients"].values()]
        keys = [{"id": "key:" + h[:16], "name": k["name"], "route": "api_key", "since": since(k.get("created_at")),
                 "read_only": k.get("scope") == READ} for h, k in data["keys"].items()]
        return signed_in + keys

    def remove(self, assistant_id: str) -> dict:
        """Disconnect one assistant. A sign-in part way through, this one's or another's, has to start again."""
        def drop(data: dict) -> dict | None:
            key = next((h for h in data["keys"] if "key:" + h[:16] == assistant_id), None)
            if key is not None:
                found = {"id": assistant_id, "name": data["keys"].pop(key)["name"], "route": "api_key"}
            elif assistant_id in data["clients"]:
                name = data["clients"].pop(assistant_id)["client_name"]
                found = {"id": assistant_id, "name": name, "route": "sign_in"}
                for kind in ("access", "refresh", "used"):
                    data[kind] = {h: t for h, t in data[kind].items() if t["client_id"] != assistant_id}
            else:
                return None
            data["generation"] += 1
            return found

        found = self._change(drop)
        if found is None:
            raise NotFound(f"no assistant {assistant_id}")
        with self.lock:
            self.pending.pop(assistant_id, None)
            self.grants = {k: g for k, g in self.grants.items() if g.client_id != assistant_id}
        return found



# -- HTTP --------------------------------------------------------------------

MAX_FORM = 8192
API = "/api/v1"
# a tool's error code and the HTTP status the API answers it with; any other code is 400
STATUS = {"invalid": HTTPStatus.BAD_REQUEST, "forbidden": HTTPStatus.FORBIDDEN, "not_found": HTTPStatus.NOT_FOUND,
          "conflict": HTTPStatus.CONFLICT, "not_connected": HTTPStatus.CONFLICT, "upstream": HTTPStatus.BAD_GATEWAY}
QUERY_TIMEOUT_S = 20


class RemoteServer(web.Server):
    def __init__(self, address: tuple[str, int], base: str, db: Path, auth: Auth, pairing: Pairing | None,
                 name: str, log: Callable[[str], None] | None = None):
        self.base, self.auth = base, auth
        super().__init__(address, Handler, db, pairing, name, log)

    @property
    def resource(self) -> str:
        return self.base + "/mcp"


class Handler(web.Handler):
    server: RemoteServer

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
        return web.last_values(parse_qs(raw))

    def _route(self) -> str:
        return urlsplit(self.path).path.rstrip("/") or "/"

    def _guard(self, fn: Callable[[], None]) -> None:
        try:
            origin = self.headers.get("Origin")
            if origin is not None and origin.rstrip("/").lower() != self.server.base.lower():  # DNS rebinding
                raise HTTPError(HTTPStatus.FORBIDDEN, "forbidden", "requests from other sites' pages are refused")
            fn()
        except OAuthError as exc:
            self._oauth_fail(exc)
        except HTTPError as exc:
            self._fail(exc)

    def do_GET(self) -> None:  # noqa: N802
        routes = {"/.well-known/oauth-protected-resource": self.resource_metadata,
                  "/.well-known/oauth-protected-resource/mcp": self.resource_metadata,
                  "/.well-known/oauth-authorization-server": self.server_metadata,
                  "/oauth/authorize": self.authorize_page, "/mcp": self.mcp_not_allowed,
                  f"{API}/openapi.json": self.api_spec, f"{API}/tools": self.api_tools}
        self._guard(routes.get(self._route(), self.not_found))

    def do_POST(self) -> None:  # noqa: N802
        routes = {"/oauth/register": self.register, "/oauth/authorize": self.approve, "/oauth/token": self.token,
                  "/mcp": self.mcp}
        route = self._route()
        if route.startswith(f"{API}/tools/"):
            self._guard(lambda: self.api_call(route[len(f"{API}/tools/"):]))
            return
        self._guard(routes.get(route, self.not_found))

    def do_DELETE(self) -> None:  # noqa: N802
        self._guard(self.mcp_not_allowed if self._route() == "/mcp" else self.not_found)

    def not_found(self) -> None:
        raise HTTPError(HTTPStatus.NOT_FOUND, "not_found", f"no such endpoint {self._route()}")

    # -- metadata --

    def resource_metadata(self) -> None:
        """RFC 9728."""
        self._send(HTTPStatus.OK, {"resource": self.server.resource, "authorization_servers": [self.server.base],
                                   "scopes_supported": list(SCOPES), "bearer_methods_supported": ["header"],
                                   "resource_name": f"byobot ({self.server.name})"})

    def server_metadata(self) -> None:
        """RFC 8414."""
        base = self.server.base
        self._send(HTTPStatus.OK, {
            "issuer": base, "authorization_endpoint": base + "/oauth/authorize", "token_endpoint": base + "/oauth/token",
            "registration_endpoint": base + "/oauth/register", "scopes_supported": list(SCOPES),
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
        self._authorize(self._query())

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
                scope = READ if q.get("read_only") else requested_scope(q.get("scope"))
                code = self.server.auth.grant(client["client_id"], uri, q["code_challenge"],
                                              bool(q.get("redirect_uri")), scope)
                self.server.log(printable(f"approved {client['client_name']}"))
                self._redirect(uri, {"code": code, "state": q.get("state"), "iss": self.server.base})
                return
            message = "That code isn't right." if is_open and tries else ""
        if self.server.pairing is None or not self.server.pairing.open:
            self._html(HTTPStatus.FORBIDDEN, "Approval is closed",
                       "<p>Run <code>byobot remote serve --pair</code> on the computer that holds your byobot, "
                       "then connect again.</p>")
            return
        self._approval_page(client, uri, q, message)

    def _approval_page(self, client: dict, uri: str, q: dict[str, str], message: str) -> None:
        keep = "".join(f"<input type=hidden name='{escape(k)}' value='{escape(v)}'>" for k, v in q.items()
                       if k in ("response_type", "client_id", "redirect_uri", "code_challenge",
                                "code_challenge_method", "state", "scope", "resource"))
        writes = requested_scope(q.get("scope")) == FULL
        choice = ("<p><label><input type=checkbox name=read_only value=1> Only let it read</label></p>"
                  if writes else "")
        self._html(HTTPStatus.FORBIDDEN if message else HTTPStatus.OK, "Connect to byobot",
                   f"<p>An app calling itself <b>{escape(client['client_name'])}</b> wants to "
                   f"{'read and write' if writes else 'read'} your byobot ({escape(self.server.name)}). "
                   f"After you approve, it goes to <b>{escape(urlsplit(uri).hostname or '')}</b>.</p>"
                   f"<p>Enter the code <code>byobot remote serve</code> shows only if you just started connecting this "
                   f"app yourself and that address is your assistant's.</p>"
                   f"<form method=post action='/oauth/authorize'>{keep}{choice}"
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
        token = self._bearer() or self.headers.get("X-API-Key", "").strip()[:web.MAX_TOKEN]
        client = self.server.auth.client_for_token(token) if token else None
        if client is None:
            challenge = f'Bearer resource_metadata="{self.server.base}/.well-known/oauth-protected-resource/mcp"'
            if token:
                challenge += ', error="invalid_token"'
            raise HTTPError(HTTPStatus.UNAUTHORIZED, "unauthorized", "connect this assistant to byobot first",
                            headers={"WWW-Authenticate": challenge}, content_type="application/json")
        self.peer = {"name": client["client_name"]}
        return client

    def mcp(self) -> None:
        client = self._client()
        version = self.headers.get("MCP-Protocol-Version")
        if version is not None and version not in mcp.SUPPORTED_VERSIONS:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", f"unsupported MCP-Protocol-Version {version}")
        message = self._body()
        server = mcp.Server(actor_id(client), read_only=scope_of(client) == READ, path=self.server.db,
                            protocol=version or mcp.LATEST, quiet_errors=True)
        try:
            if not _writes(message):
                limit_time(server.ctx.store().conn, QUERY_TIMEOUT_S)
            reply = server.handle(message)
        finally:
            server.close()
        if reply is None:
            self._send_bytes(HTTPStatus.ACCEPTED, b"", None)
        else:
            self._send_bytes(HTTPStatus.OK, json.dumps(reply, ensure_ascii=False).encode(),
                             "application/json; charset=utf-8")

    def mcp_not_allowed(self) -> None:
        self._send_bytes(HTTPStatus.METHOD_NOT_ALLOWED, b"", None, {"Allow": "POST"})

    # -- plain JSON API --

    def api_spec(self) -> None:
        self._send(HTTPStatus.OK, openapi(self.server.base))

    def api_tools(self) -> None:
        client = self._client()
        self._send(HTTPStatus.OK, {"tools": mcp.tools(read_only=scope_of(client) == READ)})

    def api_call(self, name: str) -> None:
        client = self._client()
        args = self._body()
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid", "send the tool's arguments as a JSON object")
        cmd = commands.tool(name)
        if cmd is None:
            raise HTTPError(HTTPStatus.NOT_FOUND, "not_found", f"unknown tool: {name}")
        ctx = commands.Context(actor={}, path=self.server.db)
        try:
            if cmd.read_only:
                limit_time(ctx.store().conn, QUERY_TIMEOUT_S)
            result = commands.call(ctx, cmd, args, actor_id(client), scope_of(client) == READ)
        except ByobotError as exc:
            self._send(STATUS.get(exc.code, HTTPStatus.BAD_REQUEST), exc.to_dict())
            return
        except Exception as exc:  # its text goes to the log, not to the caller
            if interrupted(exc):
                timed_out(exc)
            self.server.log(printable(f"{name} failed: {exc!r}"))
            raise HTTPError(HTTPStatus.INTERNAL_SERVER_ERROR, "error",
                            "internal error; the byobot log has details") from None
        finally:
            ctx.close()
        self._send(HTTPStatus.OK, result)


def _writes(message: Any) -> bool:
    """Whether a message calls a tool that writes. Those aren't under the read deadline: imports wait on the network."""
    if isinstance(message, list):
        return any(_writes(m) for m in message)
    params = message.get("params") if isinstance(message, dict) and message.get("method") == "tools/call" else None
    name = params.get("name") if isinstance(params, dict) else None
    cmd = commands.tool(name)
    return cmd is not None and not cmd.read_only


ERROR_SCHEMA = {
    "type": "object", "required": ["error", "message"],
    "properties": {
        "error": {"type": "string", "description": (
            "invalid (400: the arguments or body), unauthorized (401: no or a wrong key), forbidden (403: this "
            "assistant may only read), not_found (404: no such tool or record), conflict (409), not_connected "
            "(409: a source isn't signed in), upstream (502: a source's service failed), timeout (503: a read "
            "ran too long); error for anything else. New codes may be added.")},
        "message": {"type": "string"}, "details": {}}}


def openapi(base: str) -> dict:
    """An OpenAPI 3.1 description of the JSON API: one POST per tool, its body the tool's arguments."""
    error = {"description": "The body's `error` is a stable code and `message` says why.",
             "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
    responses = {"200": {"description": "The result, as JSON."}, "400": error, "401": error, "403": error,
                 "404": error, "409": error, "502": error, "503": error}
    paths = {}
    for tool in mcp.tools():
        paths[f"{API}/tools/{tool['name']}"] = {"post": {
            "operationId": tool["name"], "summary": tool["title"],
            "description": tool["description"][:300],
            "requestBody": {"required": True, "content": {"application/json": {"schema": tool["inputSchema"]}}},
            "responses": responses}}
    return {"openapi": "3.1.0",
            "info": {"title": "byobot", "version": __version__, "description": mcp.INSTRUCTIONS},
            "servers": [{"url": base}], "paths": paths,
            "components": {"schemas": {"Error": ERROR_SCHEMA},
                           "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"},
                                                              "apiKey": {"type": "apiKey", "in": "header",
                                                                         "name": "X-API-Key"}}},
            "security": [{"bearer": []}, {"apiKey": []}]}


# -- running it --------------------------------------------------------------

DEFAULT_PORT = 8780


def choose_port() -> int:
    """The port this profile used last, else 8780 for the default profile, else a free one from 8781 to 8879."""
    return util.choose_port("remote", DEFAULT_PORT, range(DEFAULT_PORT + 1, DEFAULT_PORT + 100))


def make_server(base: str, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *, pair: bool | None = None,
                db: Path | None = None, log: Callable[[str], None] | None = None) -> RemoteServer:
    """Build a server (not yet serving). ``pair=None`` opens approval only when no client or API key is connected yet."""
    saved = auth(base + "/mcp")
    if pair is None:
        pair = not saved.clients() and not saved.keys()
    name = profile() or "default profile"
    return RemoteServer((host, port), base, db or db_path(), saved, Pairing() if pair else None, name, log)


CLIPBOARD = (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"])


def clipboard() -> list[str]:
    for cmd in CLIPBOARD:
        if shutil.which(cmd[0]):
            return cmd
    raise ValidationFailed("no clipboard tool found (pbcopy, wl-copy, xclip or xsel); or pass --show to print the key once")


def make_key(name: str, show: bool = False, read_only: bool = False) -> dict:
    """The ``byobot remote key`` command."""
    saved = remote_dir() / "url"
    cmd = None if show else clipboard()
    key = auth().create_key(name, READ if read_only else FULL)
    base = saved.read_text().strip() if saved.is_file() else "https://YOUR-TUNNEL-ADDRESS"
    out = {"name": name, "openapi": base + API + "/openapi.json", "mcp": base + "/mcp"}
    if cmd:
        subprocess.run(cmd, input=key.encode(), check=True)
        out["copied"] = True
    else:
        out["key"] = key
    sys.stderr.write(("Key copied to the clipboard, not shown. " if cmd else "Shown once. ")
                     + "Send it as Authorization: Bearer KEY or X-API-Key: KEY.\n")
    return out


def auth(resource: str | None = None) -> Auth:
    """This profile's saved assistants and keys."""
    return Auth(remote_dir() / "oauth.json", resource)


def run(url: str | None, host: str, port: int | None, pair: bool | None) -> int:
    """The ``byobot remote serve`` command."""
    saved = remote_dir() / "url"
    if url is None and not saved.is_file():
        raise ValidationFailed("pass --url with the https address your tunnel gives this profile (saved after that)")
    base = check_url(url if url is not None else saved.read_text())
    remote_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    saved.write_text(base + "\n")
    port = port or choose_port()
    try:
        server = make_server(base, host, port, pair=pair, log=web.log_line)
    except OSError as exc:
        raise ByobotError(f"could not listen on {host}:{port}: {exc}") from None
    (remote_dir() / "port").write_text(f"{server.server_address[1]}\n")
    err = sys.stderr
    err.write(f"byobot remote · {server.name} · http://{host}:{server.server_address[1]} · store {server.db}\n")
    err.write(f"Point your tunnel's {base} at http://{host}:{server.server_address[1]}.\n")
    err.write(f"\nIn your assistant, add a custom connector with the URL {server.resource}\n")
    if server.pairing:
        err.write(f"When it asks for a code, enter {server.pairing.code} (good for {PAIRING_TTL_S // 60} minutes).\n")
    else:
        names = ", ".join([c["client_name"] for c in server.auth.clients()]
                          + [f"API key {k['name']}" for k in server.auth.keys()])
        err.write(f"Connected: {names}. Run with --pair to connect another, or `byobot device remove` to disconnect one.\n")
    return web.serve_until_stopped(server)


class _Shown(Exception):
    """An authorization error shown on the page, because redirecting to an unchecked address is unsafe."""
