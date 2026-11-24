"""Talk to WHOOP's API directly, with your own WHOOP developer app. No server sits in between.

`byobot connect whoop` signs you in once. The client secret and the tokens stay in
your Keychain (see credentials.py), and the import refreshes them itself.
"""

from __future__ import annotations

import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Callable

from byobot import credentials
from byobot.util import ByobotError

API = "https://api.prod.whoop.com/developer"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
PORT = 8766
REDIRECT = f"http://localhost:{PORT}/whoop/callback"
SCOPES = "offline read:recovery read:cycles read:sleep read:workout"
NAME = "whoop"


class UpstreamError(ByobotError):
    code = "upstream"


class NotConnected(ByobotError):
    code = "not_connected"


def _post(url: str, form: dict, opener: Callable) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    try:
        with opener(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise UpstreamError(f"WHOOP refused the sign-in ({e.code}). Run `byobot connect whoop` again.") from None
    except (urllib.error.URLError, OSError) as e:
        raise UpstreamError(f"can't reach WHOOP: {getattr(e, 'reason', e)}") from None


def _tokens(client_id: str, client_secret: str, answer: dict) -> dict:
    return {"client_id": client_id, "client_secret": client_secret, "access_token": answer["access_token"],
            "refresh_token": answer["refresh_token"], "expires_at": time.time() + answer.get("expires_in", 3600)}


def authorize_url(client_id: str, state: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({"response_type": "code", "client_id": client_id,
                                                    "redirect_uri": REDIRECT, "scope": SCOPES, "state": state})


def wait_for_code(state: str, timeout: float = 300) -> str:
    """Catch WHOOP's redirect on this machine and return the authorization code."""
    found: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            if url.path != "/whoop/callback":
                self.send_response(404)
                self.end_headers()
                return
            found.update(query)
            ok = query.get("state") == state and "code" in query
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"byobot is connected to WHOOP. You can close this tab." if ok
                             else b"byobot couldn't connect to WHOOP. Check the terminal.")

        def log_message(self, *args: Any) -> None:
            pass

    with http.server.HTTPServer(("127.0.0.1", PORT), Handler) as server:
        server.timeout = 1
        deadline = time.monotonic() + timeout
        while not found and time.monotonic() < deadline:
            server.handle_request()
    if not found:
        raise UpstreamError("no answer from WHOOP's sign-in; run `byobot connect whoop` again")
    if found.get("state") != state:
        raise UpstreamError("the sign-in answer didn't match this request; run `byobot connect whoop` again")
    if "code" not in found:
        raise UpstreamError(f"WHOOP didn't sign you in: {found.get('error_description') or found.get('error')}")
    return found["code"]


def connect(client_id: str, client_secret: str, opener: Callable = urllib.request.urlopen,
            open_browser: Callable[[str], Any] = webbrowser.open, wait: Callable[[str], str] = wait_for_code) -> dict:
    state = secrets.token_hex(4)
    url = authorize_url(client_id, state)
    open_browser(url)
    code = wait(state)
    answer = _post(TOKEN_URL, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                               "client_id": client_id, "client_secret": client_secret}, opener)
    if "refresh_token" not in answer:
        raise UpstreamError("WHOOP gave no refresh token; check that your app has the offline scope")
    credentials.save(NAME, _tokens(client_id, client_secret, answer))
    return {"connected": True, "scopes": answer.get("scope", SCOPES).split()}


def disconnect(opener: Callable = urllib.request.urlopen) -> dict:
    stored = credentials.load(NAME)
    if stored is None:
        return {"disconnected": False}
    revoked = True
    try:
        Client(opener).request("DELETE", "/v2/user/access")
    except ByobotError:
        revoked = False
    credentials.delete(NAME)
    return {"disconnected": True, "revoked_at_whoop": revoked}


class Client:
    def __init__(self, opener: Callable = urllib.request.urlopen):
        self.opener = opener
        self.stored = credentials.load(NAME)
        if self.stored is None:
            raise NotConnected("byobot isn't connected to WHOOP yet; run `byobot connect whoop` in a terminal")

    def _refresh(self) -> None:
        s = self.stored
        answer = _post(TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": s["refresh_token"],
                                   "client_id": s["client_id"], "client_secret": s["client_secret"],
                                   "scope": "offline"}, self.opener)
        # WHOOP rotates refresh tokens, so the new one is saved before anything else.
        self.stored = _tokens(s["client_id"], s["client_secret"], answer)
        credentials.save(NAME, self.stored)

    def request(self, method: str, path: str, params: dict | None = None) -> Any:
        if self.stored["expires_at"] - 60 < time.time():
            self._refresh()
        for attempt in (1, 2):
            query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
            req = urllib.request.Request(f"{API}{path}" + (f"?{query}" if query else ""), method=method,
                                         headers={"Authorization": f"Bearer {self.stored['access_token']}",
                                                  "Accept": "application/json"})
            try:
                with self.opener(req) as resp:
                    body = resp.read()
                    return json.loads(body.decode("utf-8")) if body else None
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    self._refresh()
                    continue
                if e.code == 429:
                    raise UpstreamError("WHOOP says too many requests; try again in a minute") from None
                raise UpstreamError(f"WHOOP answered {e.code} for {path}") from None
            except (urllib.error.URLError, OSError) as e:
                raise UpstreamError(f"can't reach WHOOP: {getattr(e, 'reason', e)}") from None
            except json.JSONDecodeError:
                raise UpstreamError(f"WHOOP sent something other than JSON for {path}") from None

