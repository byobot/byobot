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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from byobot.util import ValidationFailed, home

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
        return {"clients": data.get("clients", {}), "access": data.get("access", {}), "refresh": data.get("refresh", {})}

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
        with self.lock:
            data = self._load()
        held = data["access"].get(_hash(token)) if token else None
        if held is None or held["expires_at"] < time.time():
            return None
        return data["clients"].get(held["client_id"])

    def revoke_all(self) -> int:
        with self.lock:
            data = self._load()
            count = len(data["clients"])
            self.pending.clear()
            self.grants.clear()
            self._save({"clients": {}, "access": {}, "refresh": {}})
        return count
