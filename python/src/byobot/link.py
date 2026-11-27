"""The one ``byobot://pair`` link a phone scans: a hub to pair with, a Drive store to join, or both.

``v`` is the link's version. Hub fields: ``host`` (repeated), ``port``, ``code`` and ``pin``, the SHA-256 of the
hub's public key (SubjectPublicKeyInfo DER). Drive fields: ``store_id``, ``secret``, ``keeper_key`` (the keeper's
signing key), ``provider`` and, optionally, ``client``. Binary values are unpadded base64url.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, quote, urlencode, urlsplit

VERSION = 1
PROVIDERS = ("google",)


def _enc(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _dec(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pairing_link(*, hosts: list[str] = (), port: int | None = None, code: str | None = None,
                 pin: str | None = None, store_id: str | None = None, secret: str | None = None,
                 keeper_key: str | None = None, client: str | None = None) -> str:
    """``pin`` is hex; ``secret`` and ``keeper_key`` are standard base64, as the cloud files hold them."""
    query = [("v", str(VERSION))]
    if hosts:
        query += [("host", h) for h in hosts] + [("port", str(port)), ("code", code), ("pin", _enc(bytes.fromhex(pin)))]
    if store_id:
        query += [("store_id", store_id), ("secret", _enc(base64.b64decode(secret))),
                  ("keeper_key", _enc(base64.b64decode(keeper_key))), ("provider", PROVIDERS[0])]
        query += [("client", client)] if client else []
    return "byobot://pair?" + urlencode(query, quote_via=quote)


def parse_pairing_link(link: str) -> dict:
    """The fields of a link: ``v``, then ``hosts``, ``port``, ``code`` and ``pin`` (hex) when it names a hub, and
    ``store_id``, ``secret``, ``keeper_key`` (standard base64), ``provider`` and ``client`` when it names a store."""
    parts = urlsplit(link)
    if parts.scheme != "byobot" or parts.netloc != "pair":
        raise ValueError("not a byobot pairing link")
    q = parse_qs(parts.query)
    one = {k: v[0] for k, v in q.items()}
    found: dict = {"v": int(one["v"])}
    if found["v"] != VERSION:
        raise ValueError("a link from a newer byobot; update the app")
    if "host" in q:
        pin = _dec(one["pin"])
        if len(pin) != 32:
            raise ValueError("pin is not a SHA-256")
        found.update(hosts=q["host"], port=int(one["port"]), code=one["code"], pin=pin.hex())
    if "store_id" in q:
        if one.get("provider") not in PROVIDERS:
            raise ValueError("a storage provider this byobot doesn't know")
        found.update(store_id=one["store_id"], secret=base64.b64encode(_dec(one["secret"])).decode(),
                     keeper_key=base64.b64encode(_dec(one["keeper_key"])).decode(), provider=one["provider"],
                     client=one.get("client"))
    if "hosts" not in found and "store_id" not in found:
        raise ValueError("a link with neither a hub nor a store")
    return found
