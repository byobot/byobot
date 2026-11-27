"""Talk to WHOOP's API directly, with your own WHOOP developer app. No server sits in between.

`byobot connect whoop` signs you in once. The client secret and the tokens stay in
your Keychain (see credentials.py), and the import refreshes them itself. Every page
WHOOP sends is checked against the pinned API (whoop.schema.json) before anything is
written.
"""

from __future__ import annotations

import http.server
import json
import random
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any, Callable, Iterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from byobot import __version__, credentials
from byobot.util import ByobotError

API = "https://api.prod.whoop.com/developer"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
PORT = 8766
REDIRECT = f"http://localhost:{PORT}/whoop/callback"
SCOPES = "offline read:recovery read:cycles read:sleep read:workout"
NAME = "whoop"
# Firewalls in front of APIs often turn away urllib's default User-Agent.
HEADERS = {"User-Agent": f"byobot/{__version__} (+https://github.com/byobot/byobot)", "Accept": "application/json"}
SNAPSHOT = Path(__file__).with_name("whoop.schema.json")
PAGE = 25
TRIES = 5
BUSY = {429, 500, 502, 503, 504}
LONGEST_WAIT = 120  # WHOOP's limit is per minute; a longer Retry-After means try again later


class UpstreamError(ByobotError):
    code = "upstream"


class NotConnected(ByobotError):
    code = "not_connected"


def _why(e: urllib.error.HTTPError) -> str:
    """WHOOP's own reason for an error, short and on one line."""
    try:
        text = (e.read() or b"")[:4000].decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict):
        text = " ".join(str(body[k]) for k in ("error", "error_description", "message") if body.get(k))
    elif re.search(r"cloudflare|<html", text, re.I):
        code = re.search(r"error code:?\s*(\d{4})", text, re.I)
        return "turned away by WHOOP's firewall" + (f" (Cloudflare {code.group(1)})" if code else "")
    text = " ".join(text.split())
    return text[:200]


def _post(url: str, form: dict, opener: Callable) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        why = _why(e)
        raise UpstreamError(f"WHOOP refused the sign-in ({e.code}{': ' + why if why else ''}). "
                            "Run `byobot connect whoop` again.") from None
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
    state = secrets.token_urlsafe(32)
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


def version() -> str:
    return _snapshot()["x-whoop-api-version"]


@cache
def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text())


def check(body: Any, name: str) -> None:
    """Refuse a page that doesn't match the pinned WHOOP API."""
    schema = {**_snapshot(), "$ref": f"#/components/schemas/{name}"}
    error = best_match(Draft202012Validator(schema).iter_errors(body))
    if error:
        while error.context:
            error = best_match(error.context)
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        raise UpstreamError(f"WHOOP sent a {name} byobot doesn't recognize (at {where}: {error.message}). "
                            f"byobot reads WHOOP API {version()}.")


def _retry_after(e: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait before trying again: WHOOP's Retry-After, else a growing backoff."""
    try:
        return max(0.0, float((e.headers or {}).get("Retry-After")))
    except (TypeError, ValueError):
        return min(2.0 ** attempt, 30.0) + random.random()


class Client:
    def __init__(self, opener: Callable | None = None):
        self.opener = opener or urllib.request.urlopen
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
        """One call's JSON. Refreshes the access token once on a 401, and waits and tries again when WHOOP is busy
        or the network drops."""
        refreshed = False
        for attempt in range(TRIES):
            if self.stored["expires_at"] - 60 < time.time():
                self._refresh()
            query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
            req = urllib.request.Request(f"{API}{path}" + (f"?{query}" if query else ""), method=method,
                                         headers={**HEADERS, "Authorization": f"Bearer {self.stored['access_token']}"})
            try:
                with self.opener(req) as resp:
                    body = resp.read()
                    return json.loads(body.decode("utf-8")) if body else None
            except urllib.error.HTTPError as e:
                if e.code == 401 and not refreshed:
                    self._refresh()
                    refreshed = True
                    continue
                last = attempt == TRIES - 1
                wait = _retry_after(e, attempt)
                if e.code == 429 and (last or wait > LONGEST_WAIT):
                    raise UpstreamError("WHOOP says too many requests; try again in a few minutes") from None
                if e.code not in BUSY or last:
                    why = _why(e)
                    raise UpstreamError(f"WHOOP answered {e.code} for {path}{': ' + why if why else ''}") from None
            except (urllib.error.URLError, OSError) as e:
                if attempt >= 1:
                    raise UpstreamError(f"can't reach WHOOP: {getattr(e, 'reason', e)}") from None
                wait = 2.0
            except json.JSONDecodeError:
                raise UpstreamError(f"WHOOP sent something other than JSON for {path}") from None
            time.sleep(wait)
        raise UpstreamError("WHOOP didn't answer; try again")

    def pages(self, path: str, name: str, start: str) -> Iterator[dict]:
        """Every record since `start`, page by page, each page checked before any record is used."""
        params = {"limit": PAGE, "start": start}
        while True:
            body = self.request("GET", path, params)
            check(body, name)
            yield from body.get("records") or []
            if not body.get("next_token"):
                return
            params["nextToken"] = body["next_token"]


def utc(ts: str) -> str:
    """An instant in UTC, to the millisecond."""
    at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# source_type -> (path, response schema): each record is kept as WHOOP sent it; byobot reads it on the way out.
ENDPOINTS = {"sleep": ("/v2/activity/sleep", "PaginatedSleepResponse"),
             "recovery": ("/v2/recovery", "RecoveryCollection"),
             "cycle": ("/v2/cycle", "PaginatedCycleResponse"),
             "workout": ("/v2/activity/workout", "WorkoutCollection")}


def record_key(source_type: str, record: dict) -> str:
    """A recovery has no id of its own: it's its cycle's."""
    return str(record["cycle_id"] if source_type == "recovery" else record["id"])


def collect(client: Client, start: str) -> list[tuple[str, str, dict]]:
    """Every sleep, recovery, cycle and workout since `start`, as (source_type, source_id, raw)."""
    return [(t, record_key(t, r), r) for t, (path, name) in ENDPOINTS.items() for r in client.pages(path, name, start)]


def import_into(store, client: Client, days: int | None = None) -> dict:
    """Fetch since the last import (a week back, so rescores are picked up), or `days` back, or 90 days."""
    from byobot.core import sources

    last = store.conn.execute("SELECT max(json_extract(raw, '$.start')) FROM source_records WHERE source = ?",
                              (NAME,)).fetchone()[0]
    if days is None and last:
        since = datetime.fromisoformat(last.replace("Z", "+00:00")) - timedelta(days=7)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=days or 90)
    found = collect(client, utc(since.isoformat()))
    written = sources.ingest(store, NAME, found)
    return {"since": utc(since.isoformat()), "fetched": len(found), "written": len(written),
            "whoop_api": version()}
