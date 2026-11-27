"""The cloud folder in Google Drive's hidden app folder, reached with your own Google Cloud project's Desktop client.

byobot asks only for the ``drive.appdata`` scope, so it sees no other file in your Drive. Every file is written once
with an id asked for up front, so a retried upload can't make a second copy, and deleted outright, since files in the
hidden folder can't be trashed. ``list`` reads the whole folder once, then only Drive's change feed.
"""

from __future__ import annotations

import http.server
import json
import os
import random
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable

from byobot import __version__, credentials, oauth
from byobot.util import ByobotError, home

from .folder import Exists, _safe, checksum

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive.appdata"
NAME = "google"
STORE = "byobot_store"  # the appProperties key that marks a file as one store's
FIELDS = "id,name,size,sha256Checksum,appProperties,trashed"
RELIST = 600  # seconds between full listings, so a change feed that lags or misses a file never hides it for long
HEADERS = {"User-Agent": f"byobot/{__version__} (+https://github.com/byobot/byobot)"}
TRIES = 6
TIMEOUT = 60
RETRY = {429, 500, 502, 503, 504}
RATE = {"rateLimitExceeded", "userRateLimitExceeded"}


PROVIDER = oauth.Provider(NAME, "Google Drive", TOKEN_URL, "`byobot cloud setup`", rotates=False)
UpstreamError, NotConnected = oauth.UpstreamError, oauth.NotConnected


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _why(e: urllib.error.HTTPError) -> tuple[str, str]:
    """Google's message and first reason for an error."""
    try:
        body = json.loads((e.read() or b"")[:8000])
    except (ValueError, OSError):
        return "", ""
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return "", ""
    reasons = [x.get("reason", "") for x in err.get("errors") or [] if isinstance(x, dict)]
    return str(err.get("message", ""))[:200], reasons[0] if reasons else ""


# -- sign-in -------------------------------------------------------------------------------------------------------


def read_client(path: str | Path) -> dict:
    """The id and secret from the Desktop client's JSON that Google Cloud's console downloads."""
    try:
        raw = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError) as e:
        raise ByobotError(f"can't read the client file: {e}") from None
    client = raw.get("installed") or raw
    if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
        raise ByobotError("that isn't a Desktop app client file; make a Desktop app OAuth client and download its JSON")
    return {"client_id": client["client_id"], "client_secret": client["client_secret"]}


def wait_for_code(server: http.server.HTTPServer, state: str, timeout: float = 300) -> str:
    """Catch Google's redirect to this machine and return the authorization code."""
    return oauth.wait_for_code(server, state, PROVIDER, timeout=timeout)


def sign_in(client: dict, opener: Callable = urllib.request.urlopen,
            open_browser: Callable[[str], Any] = webbrowser.open,
            wait: Callable[[http.server.HTTPServer, str], str] = wait_for_code) -> dict:
    """Sign in in the browser with PKCE and a loopback redirect, check the project is fit for daily use, and keep
    the refresh token in the Keychain."""
    verifier, challenge = oauth.pkce()
    state = secrets.token_urlsafe(16)
    with http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler) as server:
        redirect = f"http://127.0.0.1:{server.server_address[1]}"
        open_browser(AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
            "access_type": "offline", "prompt": "consent"}))
        code = wait(server, state)
    answer = oauth.post_token(PROVIDER, {
        "client_id": client["client_id"], "client_secret": client["client_secret"], "code": code,
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect}, opener)
    if "refresh_token" not in answer:
        raise UpstreamError("Google gave no refresh token; run `byobot cloud setup` again")
    if "refresh_token_expires_in" in answer:
        raise ByobotError("your Google Cloud project's consent screen is in Testing, which signs byobot out every "
                          "seven days; publish it so its status is In production, then run `byobot cloud setup` again")
    granted = set(answer.get("scope", SCOPE).split())
    if granted != {SCOPE}:
        raise ByobotError(f"Google granted {' '.join(sorted(granted))}; byobot wants only {SCOPE}. "
                          "Give the consent screen only that scope, then run `byobot cloud setup` again")
    with credentials.locked(NAME):
        credentials.save(NAME, oauth.tokens(client, answer))
    return {"signed_in": True, "scope": SCOPE}


# -- the API -------------------------------------------------------------------------------------------------------


class Drive(oauth.Session):
    """Drive API v3 calls with the saved sign-in: refreshes the access token, and backs off and retries when Drive
    is busy or the network drops."""

    provider = PROVIDER

    def __init__(self, opener: Callable | None = None, sleep: Callable[[float], None] = time.sleep,
                 stored: dict | None = None, tries: int = TRIES, timeout: float = TIMEOUT):
        super().__init__(opener or (lambda req: urllib.request.urlopen(req, timeout=timeout)), stored)
        self.sleep = sleep
        self.tries = tries

    def call(self, method: str, url: str, params: dict | None = None, data: bytes | None = None,
             headers: dict | None = None) -> bytes:
        """One call's body. Raises NotFound on 404 and Conflict on 409."""
        if params:
            url += "?" + urllib.parse.urlencode(params)
        refreshed = False
        for attempt in range(self.tries):
            self._fresh()
            req = urllib.request.Request(url, data=data, method=method, headers={
                **HEADERS, **(headers or {}), "Authorization": f"Bearer {self.stored['access_token']}"})
            try:
                with self.opener(req) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                message, reason = _why(e)
                if e.code == 401 and not refreshed:
                    self._refresh()
                    refreshed = True
                    continue
                if e.code == 404:
                    raise NotFound(url) from None
                if e.code == 409:
                    raise Conflict(url) from None
                if e.code == 403 and reason == "storageQuotaExceeded":
                    raise UpstreamError("your Google Drive is full, so byobot can't upload; free some space") from None
                if not (e.code in RETRY or (e.code == 403 and reason in RATE)) or attempt == self.tries - 1:
                    raise UpstreamError(f"Google Drive answered {e.code}{': ' + message if message else ''}") from None
            except (urllib.error.URLError, OSError) as e:
                if attempt == self.tries - 1:
                    raise UpstreamError(f"can't reach Google Drive: {getattr(e, 'reason', e)}") from None
            self.sleep(min(2 ** attempt, 32) + random.random())
        raise UpstreamError("Google Drive didn't answer; try again")

    def json(self, method: str, path: str, params: dict | None = None) -> dict:
        return json.loads(self.call(method, API + path, params) or b"{}")


def stores(drive: Drive) -> set[str]:
    """Every store with a byobot logbook in this Google account's hidden app folder. A folder `cloud off --delete
    yes` closed holds only epoch files, and isn't one."""
    found: set[str] = set()
    other: set[str] = set()
    params = {"spaces": "appDataFolder", "pageSize": 1000, "fields": "nextPageToken,files(name,appProperties)"}
    while True:
        page = drive.json("GET", "/files", params)
        for f in page.get("files", []):
            store = (f.get("appProperties") or {}).get(STORE)
            if store and f.get("name", "").startswith(f"{store}/epochs/"):
                found.add(store)
            elif store:
                other.add(store)
        if not page.get("nextPageToken"):
            return found & other
        params["pageToken"] = page["nextPageToken"]


# -- the folder ----------------------------------------------------------------------------------------------------


class DriveFolder:
    """One store's cloud folder: files named ``<store_id>/<path>`` in the hidden app folder, each marked with the
    store in its appProperties. Ids asked for before an upload are kept in a small journal beside the store, so an
    upload cut short by a crash is retried with the same id and never lands twice."""

    def __init__(self, store_id: str, drive: Drive, journal: Path | None = None):
        self.store_id, self.drive = _safe(store_id), drive
        self.journal = journal or home() / "cloud" / f"drive-{store_id}.json"
        self._index: dict[str, list[tuple[str, str]]] | None = None  # path -> [(file id, checksum)], oldest first
        self._token: str | None = None
        self._loaded_at = 0.0
        self._sizes: dict[str, int] = {}

    # The journal: path -> the id asked for before its upload.

    def _ids(self) -> dict[str, str]:
        try:
            return json.loads(self.journal.read_text())
        except (OSError, ValueError):
            return {}

    def _keep(self, path: str, file_id: str | None) -> None:
        ids = self._ids()
        if file_id is None:
            if ids.pop(path, None) is None:
                return
        else:
            ids[path] = file_id
        self.journal.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.journal.with_suffix(".tmp")
        tmp.write_text(json.dumps(ids))
        os.replace(tmp, self.journal)

    # The index, from one full listing, then the change feed.

    def _path(self, f: dict) -> str | None:
        name, props = f.get("name", ""), f.get("appProperties") or {}
        if f.get("trashed") or props.get(STORE) != self.store_id or not name.startswith(self.store_id + "/"):
            return None
        try:
            return _safe(name[len(self.store_id) + 1:])
        except ValueError:  # a name byobot never writes; leave it alone
            return None

    def _add(self, f: dict) -> None:
        path = self._path(f)
        if path is not None:
            self._index.setdefault(path, []).append((f["id"], f.get("sha256Checksum") or f"id:{f['id']}"))
            self._sizes[f["id"]] = int(f.get("size") or 0)

    def _drop_id(self, file_id: str) -> None:
        self._sizes.pop(file_id, None)
        for path, entries in list(self._index.items()):
            kept = [e for e in entries if e[0] != file_id]
            if kept != entries:
                if kept:
                    self._index[path] = kept
                else:
                    del self._index[path]

    def _load(self) -> None:
        self._token = self.drive.json("GET", "/changes/startPageToken")["startPageToken"]
        self._index, self._sizes, self._loaded_at = {}, {}, time.monotonic()
        params = {"spaces": "appDataFolder", "pageSize": 1000, "orderBy": "createdTime",
                  "q": f"appProperties has {{ key='{STORE}' and value='{self.store_id}' }}",
                  "fields": f"nextPageToken,files({FIELDS})"}
        while True:
            page = self.drive.json("GET", "/files", params)
            for f in page.get("files", []):
                self._add(f)
            if not page.get("nextPageToken"):
                return
            params["pageToken"] = page["nextPageToken"]

    def changed(self) -> bool:
        """Read Drive's change feed; True if any of this store's files came or went since the last look."""
        if self._index is None or time.monotonic() - self._loaded_at >= RELIST:
            self._load()
            return True
        seen = False
        params = {"pageToken": self._token, "spaces": "appDataFolder", "pageSize": 1000,
                  "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FIELDS}))"}
        while True:
            page = self.drive.json("GET", "/changes", params)
            for c in page.get("changes", []):
                ours = any(e[0] == c["fileId"] for entries in self._index.values() for e in entries)
                self._drop_id(c["fileId"])
                if not c.get("removed") and c.get("file"):
                    ours = ours or self._path(c["file"]) is not None
                    self._add(c["file"])
                seen = seen or ours
            if page.get("newStartPageToken"):
                self._token = page["newStartPageToken"]
                return seen
            params["pageToken"] = page["nextPageToken"]

    # The Folder interface.

    def list(self) -> dict[str, str]:
        self.changed()
        return {p: entries[-1][1] for p, entries in sorted(self._index.items())}

    def refresh(self) -> None:
        """List the folder afresh rather than through the change feed."""
        self._load()

    def size(self) -> int:
        """Bytes the folder takes in Drive, as of the last listing."""
        if self._index is None:
            self._load()
        return sum(self._sizes.get(e[0], 0) for entries in self._index.values() for e in entries)

    def read(self, path: str) -> bytes | None:
        if self._index is None:
            self._load()
        for file_id, _ in reversed(self._index.get(_safe(path), [])):  # a run that retried wrote the newest
            try:
                return self.drive.call("GET", f"{API}/files/{file_id}", {"alt": "media"})
            except NotFound:
                self._drop_id(file_id)
        return None

    def create(self, path: str, data: bytes) -> None:
        self.changed()
        if _safe(path) in self._index:
            self._keep(path, None)
            raise Exists(path)
        file_id = self._ids().get(path)
        for _ in range(2):
            if file_id is None:
                file_id = self.drive.json("GET", "/files/generateIds",
                                          {"count": 1, "space": "appDataFolder", "type": "files"})["ids"][0]
                self._keep(path, file_id)
            try:
                made = self._upload(file_id, path, data)
            except Conflict:  # an earlier try with this id landed, or the id was used and deleted since
                try:
                    found = self.drive.json("GET", f"/files/{file_id}", {"fields": FIELDS})
                except NotFound:
                    found = None
                self._keep(path, None)
                if found is None or self._path(found) != path:
                    file_id = None
                    continue
                self._add(found)
                if found.get("sha256Checksum") not in (None, checksum(data)):
                    raise Exists(path) from None
                return
            self._add(made)
            self._keep(path, None)
            if made.get("sha256Checksum") not in (None, checksum(data)):
                self.delete(path)
                raise UpstreamError(f"{path} reached Google Drive damaged and was deleted; it is sent again next sync")
            return
        raise UpstreamError(f"Google Drive wouldn't take {path}; try again")

    def _upload(self, file_id: str, path: str, data: bytes) -> dict:
        meta = {"id": file_id, "name": f"{self.store_id}/{path}", "parents": ["appDataFolder"],
                "appProperties": {STORE: self.store_id}}
        boundary = secrets.token_hex(16)
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + data + \
            f"\r\n--{boundary}--\r\n".encode()
        return json.loads(self.drive.call("POST", UPLOAD, {"uploadType": "multipart", "fields": FIELDS}, body,
                                          {"Content-Type": f"multipart/related; boundary={boundary}"}))

    def delete(self, path: str) -> None:
        if self._index is None:
            self._load()
        for file_id, _ in self._index.pop(_safe(path), []):
            self._sizes.pop(file_id, None)
            try:
                self.drive.call("DELETE", f"{API}/files/{file_id}")
            except NotFound:
                pass
        self._keep(path, None)
