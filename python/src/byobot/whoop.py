"""Talk to WHOOP's API directly, with your own WHOOP developer app. No server sits in between.

`byobot connect whoop` signs you in once. The client secret and the tokens stay in
your Keychain (see credentials.py), and the import refreshes them itself. Every page
WHOOP sends is checked against the pinned API (whoop.schema.json) before anything is
written.
"""

from __future__ import annotations

import http.client
import http.server
import json
import random
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

from byobot import credentials, oauth
from byobot.core.contract import instant, stamp
from byobot.util import ByobotError

API = "https://api.prod.whoop.com/developer"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
PORT = 8766
REDIRECT = f"http://localhost:{PORT}/whoop/callback"
SCOPES = "offline read:recovery read:cycles read:sleep read:workout"
NAME = "whoop"
SNAPSHOT = Path(__file__).with_name("whoop.schema.json")
PAGE = 25
TIMEOUT = 60
TRIES = 5
BUSY = {429, 500, 502, 503, 504}
LONGEST_WAIT = 120  # WHOOP's limit is per minute; a longer Retry-After means try again later


PROVIDER = oauth.Provider(NAME, "WHOOP", TOKEN_URL, "`byobot connect whoop`", rotates=True,
                          refresh_form={"scope": "offline"})
UpstreamError, NotConnected = oauth.UpstreamError, oauth.NotConnected


def authorize_url(client_id: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "scope": SCOPES, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})


def wait_for_code(state: str, timeout: float = 300) -> str:
    """Catch WHOOP's redirect on this machine and return the authorization code."""
    with http.server.HTTPServer(("127.0.0.1", PORT), http.server.BaseHTTPRequestHandler) as server:
        return oauth.wait_for_code(server, state, PROVIDER, "/whoop/callback", timeout)


def connect(client_id: str, client_secret: str, opener: Callable = urllib.request.urlopen,
            open_browser: Callable[[str], Any] = webbrowser.open, wait: Callable[[str], str] = wait_for_code) -> dict:
    verifier, challenge = oauth.pkce()
    state = secrets.token_urlsafe(32)
    open_browser(authorize_url(client_id, state, challenge))
    code = wait(state)
    answer = oauth.post_token(PROVIDER, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                         "client_id": client_id, "client_secret": client_secret,
                                         "code_verifier": verifier}, opener)
    if "refresh_token" not in answer:
        raise UpstreamError("WHOOP gave no refresh token; check that your app has the offline scope")
    with credentials.locked(NAME):
        credentials.save(NAME, oauth.tokens({"client_id": client_id, "client_secret": client_secret}, answer))
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


_urlopen = urllib.request.urlopen


class Client(oauth.Session):
    provider = PROVIDER

    def __init__(self, opener: Callable | None = None):
        opener = opener or urllib.request.urlopen
        # Test fakes stand in for urlopen and take no timeout.
        super().__init__((lambda req: opener(req, timeout=TIMEOUT)) if opener is _urlopen else opener)

    def request(self, method: str, path: str, params: dict | None = None) -> Any:
        """One call's JSON. Refreshes the access token once on a 401, and waits and tries again when WHOOP is busy
        or the network drops."""
        refreshed = False
        for attempt in range(TRIES):
            self._fresh()
            query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
            req = urllib.request.Request(
                f"{API}{path}" + (f"?{query}" if query else ""), method=method,
                headers={**oauth.HEADERS, "Authorization": f"Bearer {self.stored['access_token']}"})
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
                    why = oauth.why(e, "WHOOP")[0]
                    raise UpstreamError(f"WHOOP answered {e.code} for {path}{': ' + why if why else ''}") from None
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                if attempt >= 1:
                    raise UpstreamError(f"can't reach WHOOP: {getattr(e, 'reason', e)}") from None
                wait = 2.0
            except json.JSONDecodeError:
                raise UpstreamError(f"WHOOP sent something other than JSON for {path}") from None
            time.sleep(wait)
        raise UpstreamError("WHOOP didn't answer; try again")

    def pages(self, path: str, name: str, start: str) -> Iterator[dict]:
        """Every record since `start`, page by page, each page checked before any record is used."""
        params, seen = {"limit": PAGE, "start": start}, set()
        while True:
            body = self.request("GET", path, params)
            check(body, name)
            yield from body.get("records") or []
            if not body.get("next_token"):
                return
            if body["next_token"] in seen:
                raise UpstreamError(f"WHOOP sent the same page twice for {path}; try again")
            seen.add(body["next_token"])
            params["nextToken"] = body["next_token"]


def utc(at: datetime) -> str:
    """An instant in UTC, to the millisecond; a naive one reads as UTC."""
    return stamp((at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc))


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
        since = instant(last) - timedelta(days=7)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=days or 90)
    start = utc(since)
    found = collect(client, start)
    written = sources.ingest(store, NAME, found)
    return {"since": start, "fetched": len(found), "written": len(written),
            "whoop_api": version()}
