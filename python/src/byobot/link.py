"""The one ``byobot://pair`` link a phone scans to pair with a hub.

``v`` is the link's version. Fields: ``host`` (repeated), ``port``, ``code`` and ``pin``, the SHA-256 of the hub's
public key (SubjectPublicKeyInfo DER), as unpadded base64url.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, quote, urlencode, urlsplit

VERSION = 1


def _enc(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _dec(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pairing_link(*, hosts: list[str], port: int, code: str, pin: str) -> str:
    """``pin`` is hex."""
    query = [("v", str(VERSION))] + [("host", h) for h in hosts]
    query += [("port", str(port)), ("code", code), ("pin", _enc(bytes.fromhex(pin)))]
    return "byobot://pair?" + urlencode(query, quote_via=quote)


def parse_pairing_link(link: str) -> dict:
    """The fields of a link: ``v``, ``hosts``, ``port``, ``code`` and ``pin`` (hex)."""
    parts = urlsplit(link)
    if parts.scheme != "byobot" or parts.netloc != "pair":
        raise ValueError("not a byobot pairing link")
    q = parse_qs(parts.query)
    one = {k: v[0] for k, v in q.items()}
    found: dict = {"v": int(one["v"])}
    if found["v"] != VERSION:
        raise ValueError("a link from a newer byobot; update the app")
    if "host" not in q:
        raise ValueError("a link with no hub")
    pin = _dec(one["pin"])
    if len(pin) != 32:
        raise ValueError("pin is not a SHA-256")
    found.update(hosts=q["host"], port=int(one["port"]), code=one["code"], pin=pin.hex())
    return found
