"""Sign-in with your own OAuth app at a provider (WHOOP): PKCE and state on a loopback redirect, the token
post, and a refresh that processes take turns at, so two never spend the same rotating refresh token."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from byobot import __version__, credentials
from byobot.util import NotConnected, UpstreamError

# Firewalls in front of APIs often turn away urllib's default User-Agent.
HEADERS = {"User-Agent": f"byobot/{__version__} (+https://github.com/byobot/byobot)", "Accept": "application/json"}


@dataclass(frozen=True)
class Provider:
    name: str  # the credentials entry
    label: str  # as the person knows it
    token_url: str
    sign_in_command: str
    refresh_form: dict = field(default_factory=dict)


def pkce() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def why(e: urllib.error.HTTPError, label: str) -> tuple[str, str]:
    """The provider's reason for an error, short and on one line, and its OAuth error code if it gave one."""
    try:
        text = (e.read() or b"")[:4000].decode("utf-8", "replace")
    except Exception:
        return "", ""
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = body.get("error") if isinstance(body.get("error"), str) else ""
        text = " ".join(str(body[k]) for k in ("error", "error_description", "message")
                        if isinstance(body.get(k), (str, int)) and body.get(k))
        return text[:200], code
    if re.search(r"cloudflare|<html", text, re.I):
        found = re.search(r"error code:?\s*(\d{4})", text, re.I)
        return f"turned away by {label}'s firewall" + (f" (Cloudflare {found.group(1)})" if found else ""), ""
    return " ".join(text.split())[:200], ""


def post_token(p: Provider, form: dict, opener: Callable) -> dict:
    req = urllib.request.Request(p.token_url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text, code = why(e, p.label)
        if code == "invalid_grant":
            raise NotConnected(f"{p.label} signed byobot out; run {p.sign_in_command} to sign in again") from None
        raise UpstreamError(f"{p.label} refused the sign-in ({e.code}{': ' + text if text else ''}); "
                            f"run {p.sign_in_command} again") from None
    except (urllib.error.URLError, OSError) as e:
        raise UpstreamError(f"can't reach {p.label}: {getattr(e, 'reason', e)}") from None
    except ValueError:
        raise UpstreamError(f"{p.label} sent something other than JSON for the sign-in") from None


def tokens(stored: dict, answer: dict) -> dict:
    """The saved sign-in with a token answer's new tokens; a refresh that sends no new refresh token keeps the old."""
    if not isinstance(answer.get("access_token"), str):
        raise UpstreamError("the sign-in answer had no access token")
    return {**stored, "access_token": answer["access_token"],
            "refresh_token": answer.get("refresh_token") or stored["refresh_token"],
            "expires_at": time.time() + answer.get("expires_in", 3600)}


def wait_for_code(server: http.server.HTTPServer, state: str, p: Provider, path: str, timeout: float = 300) -> str:
    """Catch the provider's redirect to this machine and return the authorization code."""
    found: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            if url.path != path or not ({"state", "code", "error"} & query.keys()):
                self.send_response(404)
                self.end_headers()
                return
            found.update(query)
            ok = query.get("state") == state and "code" in query
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"byobot is connected to {p.label}. You can close this tab.".encode() if ok
                             else f"byobot couldn't connect to {p.label}. Check the terminal.".encode())

        def log_message(self, *args: Any) -> None:
            pass

    server.RequestHandlerClass = Handler
    server.timeout = 1
    deadline = time.monotonic() + timeout
    while not found and time.monotonic() < deadline:
        server.handle_request()
    if not found:
        raise UpstreamError(f"no answer from {p.label}'s sign-in; run {p.sign_in_command} again")
    if found.get("state") != state:
        raise UpstreamError(f"the sign-in answer didn't match this request; run {p.sign_in_command} again")
    if "code" not in found:
        raise UpstreamError(f"{p.label} didn't sign you in: {found.get('error_description') or found.get('error')}")
    return found["code"]


class Session:
    """A provider's saved sign-in, refreshed when its access token runs out."""

    provider: Provider

    def __init__(self, opener: Callable):
        self.opener = opener
        self.stored = credentials.load(self.provider.name)
        if self.stored is None:
            raise NotConnected(f"byobot isn't signed in to {self.provider.label} yet; run {self.provider.sign_in_command} "
                               "in a terminal")

    def _fresh(self) -> None:
        if self.stored["expires_at"] - 60 < time.time():
            self._refresh()

    def _refresh(self) -> None:
        """New tokens, one process at a time. A sign-in saved meanwhile by another process is used instead: a newer
        refresh from the same app is taken as it is, and a new sign-in is never overwritten with this one."""
        p = self.provider
        with credentials.locked(p.name):
            latest = credentials.load(p.name) or self.stored
            same = latest.get("refresh_token") == self.stored.get("refresh_token")
            if latest.get("client_id") != self.stored.get("client_id"):
                self.stored = latest
                raise UpstreamError(f"the {p.label} sign-in changed; trying again with the new one")
            if not same:
                self.stored = latest
                if latest["expires_at"] - 60 >= time.time():
                    return
            s = self.stored
            answer = post_token(p, {"grant_type": "refresh_token", "refresh_token": s["refresh_token"],
                                    "client_id": s["client_id"], "client_secret": s["client_secret"],
                                    **p.refresh_form}, self.opener)
            self.stored = tokens(s, answer)
            credentials.save(p.name, self.stored)
