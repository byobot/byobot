"""Helpers for the hub deep review: a real `byobot serve` on 127.0.0.1 and raw HTTPS exchanges with it."""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import threading
from contextlib import contextmanager
from datetime import datetime

from byobot import serve
from byobot.core import hlc
from byobot.util import uuid7

PHONE = "0193a0b0-0000-7000-8000-00000000ab31"
ME = {"kind": "human", "id": "me"}
CLOCK = [1795600000000]


@contextmanager
def hub(**kw):
    """A running hub with pairing open, and a client pinned to its certificate."""
    logs: list[str] = []
    srv, info = serve.make_server("127.0.0.1", 0, pair=True, log=logs.append, **kw)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, info, Client(info["port"], info["fingerprint"]), logs
    finally:
        srv.shutdown()
        srv.server_close()


class Client:
    def __init__(self, port, fp, token=None):
        self.port, self.fp, self.token = port, fp, token

    def raw(self, method, path, data: bytes | None = None, token=None, timeout: float = 15):
        """(status, body) or raises ConnectionError when the server closes without answering."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=timeout)
        try:
            conn.connect()
            assert hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest() == self.fp
            headers = {"Content-Type": "application/json"}
            tok = token if token is not None else self.token
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            conn.request(method, path, body=data, headers=headers)
            try:
                resp = conn.getresponse()
            except (http.client.RemoteDisconnected, ConnectionResetError, ssl.SSLError) as exc:
                raise ConnectionError(f"no answer: {exc!r}") from None
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
        finally:
            conn.close()

    def call(self, method, path, body=None, token=None, timeout: float = 15):
        return self.raw(method, path, None if body is None else json.dumps(body).encode(), token, timeout)


def pair(client, info, device_id=PHONE):
    status, body = client.call("POST", "/v1/pair", {"code": info["code"], "device_id": device_id,
                                                    "name": "Example iPhone"})
    assert status == 200, body
    client.token = body["token"]
    return body


def event(kind, rid, payload=None, op="put", prev=(), device_id=PHONE, actor=ME):
    """An event as the app authors it, each with a later clock reading than the one before."""
    CLOCK[0] += 1000
    e = {"id": uuid7(), "schema_version": 1, "record": {"kind": kind, "id": rid}, "op": op,
         "hlc": hlc.format((CLOCK[0], 0)), "ts": "2026-11-23T08:00:00-05:00", "device_id": device_id,
         "actor": actor, "prev": list(prev)}
    return {**e, "kind_version": 1, "payload": payload} if op == "put" else {**e, "payload": payload or {}}


def location(slug, name):
    return event("location", slug, {"name": name})


def no_answer(fn) -> bool:
    try:
        fn()
    except (ConnectionError, socket.timeout, TimeoutError):
        return True
    return False


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()
