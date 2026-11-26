"""Google Drive's hidden app folder and token endpoint, faked at the HTTP level for the Drive transport's tests.

``faults`` holds what the next matching calls do: an HTTP status, ``"pass"`` (answer as usual), ``"lost"`` (the call lands,
then its answer is lost), ``"kill"`` (the call lands, then the process dies) or ``"damage"`` (an upload lands with
one byte changed).
"""

import io
import json
import re
import urllib.error
import urllib.parse
from email.message import Message

from byobot.cloud.folder import checksum


class Killed(BaseException):
    """The process died. Not an Exception, so nothing in byobot catches it."""


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class FakeDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.taken: set[str] = set()  # every id ever used, so a deleted one can't be used again
        self.changes: list[str] = []
        self.counter = 0
        self.token = "access-1"
        self.refreshes = 0
        self.faults: list[tuple[str, object]] = []  # (method and path prefix, what happens)
        self.calls: list[str] = []
        self.lagging: set[str] = set()  # files Drive holds that its listings don't show yet

    def fail(self, where: str, what: object, times: int = 1) -> None:
        self.faults += [(where, what)] * times

    def names(self) -> list[str]:
        return sorted(f["name"] for f in self.files.values())

    def _meta(self, f: dict) -> dict:
        return {"id": f["id"], "name": f["name"], "appProperties": f["appProperties"], "trashed": False,
                "size": str(len(f["data"])), "sha256Checksum": checksum(f["data"])}

    def _id(self) -> str:
        self.counter += 1
        return f"f{self.counter:06d}"

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        params = dict(urllib.parse.parse_qsl(url.query))
        where = f"{req.get_method()} {url.path}"
        self.calls.append(where)
        fault = next((f for f in self.faults if where.startswith(f[0])), None)
        if fault:
            self.faults.remove(fault)
            if isinstance(fault[1], int):
                raise self._error(req, fault[1], "backendError" if fault[1] >= 500 else "rateLimitExceeded")
        answer = self._answer(req, url.path, params)
        if fault and fault[1] == "damage":
            f = self.files[self.changes[-1]]
            f["data"] = bytes([f["data"][0] ^ 1]) + f["data"][1:]
            return self._json(self._meta(f))
        if fault and fault[1] == "lost":
            raise ConnectionResetError("connection reset by peer")
        if fault and fault[1] == "kill":
            raise Killed(where)
        return answer

    def _error(self, req, code, reason, message="fake"):
        body = json.dumps({"error": {"code": code, "message": message, "errors": [{"reason": reason}]}}).encode()
        return urllib.error.HTTPError(req.full_url, code, message, Message(), io.BytesIO(body))

    def _json(self, body) -> Response:
        return Response(json.dumps(body).encode())

    def _answer(self, req, path, params):
        method = req.get_method()
        if path == "/token":
            form = dict(urllib.parse.parse_qsl(req.data.decode()))
            assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "refresh-1"
            self.refreshes += 1
            self.token = f"access-{self.refreshes + 1}"
            return self._json({"access_token": self.token, "expires_in": 3600})
        if req.get_header("Authorization") != f"Bearer {self.token}":
            raise self._error(req, 401, "authError")
        if path == "/drive/v3/changes/startPageToken":
            return self._json({"startPageToken": str(len(self.changes))})
        if path == "/drive/v3/changes":
            start = int(params["pageToken"])
            page = [i for i in self.changes[start:start + 2] if i not in self.lagging]
            out = [{"fileId": i, "removed": i not in self.files,
                    **({"file": self._meta(self.files[i])} if i in self.files else {})} for i in page]
            more = start + 2 < len(self.changes)
            return self._json({"changes": out, **({"nextPageToken": str(start + 2)} if more
                                                  else {"newStartPageToken": str(len(self.changes))})})
        if path == "/drive/v3/files/generateIds":
            assert params["space"] == "appDataFolder"
            return self._json({"ids": [self._id() for _ in range(int(params["count"]))]})
        if path == "/drive/v3/files" and method == "GET":
            assert params["spaces"] == "appDataFolder"
            query = re.fullmatch(r"appProperties has \{ key='byobot_store' and value='([^']+)' \}", params.get("q", ""))
            mine = [self._meta(f) for f in self.files.values() if f["id"] not in self.lagging
                    and (query is None or f["appProperties"].get("byobot_store") == query[1])]
            start = int(params.get("pageToken", 0))
            more = start + 3 < len(mine)
            return self._json({"files": mine[start:start + 3], **({"nextPageToken": str(start + 3)} if more else {})})
        if path == "/upload/drive/v3/files":
            boundary = req.get_header("Content-type").split("boundary=")[1]
            parts = req.data.split(f"--{boundary}".encode())
            meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1])
            data = parts[2].split(b"\r\n\r\n", 1)[1][:-2]
            assert meta["parents"] == ["appDataFolder"]
            if meta["id"] in self.taken:
                raise self._error(req, 409, "fileIdInUse")
            self.taken.add(meta["id"])
            self.files[meta["id"]] = {**meta, "data": data}
            self.changes.append(meta["id"])
            return self._json(self._meta(self.files[meta["id"]]))
        m = re.fullmatch(r"/drive/v3/files/([^/]+)", path)
        if m:
            f = self.files.get(m[1])
            if f is None:
                raise self._error(req, 404, "notFound")
            if method == "DELETE":
                del self.files[m[1]]
                self.changes.append(m[1])
                return Response(b"")
            return Response(f["data"]) if params.get("alt") == "media" else self._json(self._meta(f))
        raise AssertionError(f"the fake doesn't know {method} {path}")
