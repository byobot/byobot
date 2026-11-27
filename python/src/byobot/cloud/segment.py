"""Segment files: a plain header line, the encrypted body, and the writer's signature.

The body is gzip of JSON lines, each one message (``events``, ``samples`` or ``plan``, exactly as the hub's
``/v1/events`` and ``/v1/samples`` requests and ``/v1/plan`` answer, or the keeper's ``receipts``), then
``{"end": {"lines": n}}``. The header
is the associated data, so a file can't be renamed, moved or replayed into another store.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import zlib
from typing import Any

from .crypto import FORMAT, CloudError, Identity, b64, decrypt, dumps, encrypt, unb64, verify

MAGIC = "byobot-segment"
KINDS = ("segment", "checkpoint", "plan")
MESSAGES = ("events", "samples", "plan", "receipts")
DOMAIN = b"byobot-cloud segment 1\n"
MAX_HEADER = 1024
MAX_BODY = 32 * 1024 * 1024
MAX_SEQ = 10**10 - 1
MAX_EPOCH = 9999
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
NAMES = {
    "segment": re.compile(r"segments/(?P<device_id>[^/]+)/(?P<epoch>[0-9]{4})-(?P<seq>[0-9]{10})\.seg"),
    "checkpoint": re.compile(r"checkpoints/(?P<epoch>[0-9]{4})/(?P<part>[0-9]{10})\.seg"),
    "plan": re.compile(r"plan/(?P<epoch>[0-9]{4})-(?P<seq>[0-9]{10})\.seg"),
}


def counter(kind: str) -> str:
    """The header field that numbers a file: a writer's seq, or a checkpoint's part."""
    return "part" if kind == "checkpoint" else "seq"


def name(kind: str, epoch: int, seq: int, device_id: str | None = None) -> str:
    """Where a segment lives under the store's folder. A checkpoint's seq is its part number."""
    if kind == "segment":
        return f"segments/{device_id}/{epoch:04d}-{seq:010d}.seg"
    if kind == "checkpoint":
        return f"checkpoints/{epoch:04d}/{seq:010d}.seg"
    return f"plan/{epoch:04d}-{seq:010d}.seg"


def parse_name(path: str) -> dict | None:
    """What a segment's path says its header holds, or None if the path isn't a segment's."""
    for kind, pattern in NAMES.items():
        m = pattern.fullmatch(path)
        if m:
            found = {"kind": kind, "epoch": int(m["epoch"]), counter(kind): int(m[counter(kind)])}
            if "device_id" in m.groupdict():
                if not ID.fullmatch(m["device_id"]):
                    return None
                found["device_id"] = m["device_id"]
            return found
    return None


def write(kind: str, store_id: str, device_id: str, epoch: int, seq: int, messages: list[dict], key: bytes,
          signer: Identity, nonce: bytes | None = None) -> bytes:
    if kind not in KINDS or not (0 <= epoch <= MAX_EPOCH and 0 <= seq <= MAX_SEQ):
        raise ValueError("not a segment kind, epoch or seq")
    nonce = nonce or os.urandom(12)
    header = dumps({"magic": MAGIC, "format": FORMAT, "kind": kind, "store_id": store_id, "device_id": device_id,
                    "epoch": epoch, counter(kind): seq, "nonce": b64(nonce)}) + b"\n"
    lines = [*messages, {"end": {"lines": len(messages)}}]
    body = gzip.compress(b"".join(dumps(m) + b"\n" for m in lines), mtime=0)
    ciphertext = unb64(encrypt(key, body, header, nonce)["ciphertext"])
    return header + ciphertext + signer.signature(DOMAIN, header + ciphertext)


def header(data: bytes) -> dict:
    """A segment's header, not yet verified."""
    end = data.find(b"\n", 0, MAX_HEADER)
    try:
        found = json.loads(data[:end]) if end > 0 else None
    except (ValueError, UnicodeDecodeError):
        found = None
    if not isinstance(found, dict) or found.get("magic") != MAGIC:
        raise CloudError("not a byobot segment")
    if found.get("format") != FORMAT:
        raise CloudError("written by a newer byobot; upgrade byobot")
    if found.get("kind") not in KINDS or not all(isinstance(found.get(k), str) for k in ("store_id", "device_id")) \
            or not all(isinstance(found.get(k), int) and not isinstance(found.get(k), bool)
                       for k in ("epoch", counter(found["kind"]))) or "seq" in found and "part" in found:
        raise CloudError("not a byobot segment")
    return found


def read(data: bytes, *, path: str, store_id: str, key: bytes, sign_public: str,
         skip_unknown: bool = False) -> tuple[dict, list[dict]]:
    """A segment's header and messages, once its place, store, signature, key and length all check out. With
    ``skip_unknown``, a message a newer byobot added is left out instead of refusing the segment."""
    found = header(data)
    expected = parse_name(path)
    if expected is None or any(found[k] != v for k, v in expected.items()):
        raise CloudError("moved or renamed")
    if found["store_id"] != store_id:
        raise CloudError("from another store")
    split = data.index(b"\n") + 1
    if len(data) < split + 16 + 64:
        raise CloudError("cut short")
    head, ciphertext, sig = data[:split], data[split:-64], data[-64:]
    verify(sign_public, DOMAIN, head + ciphertext, sig)
    body = decrypt(key, {"nonce": found["nonce"], "ciphertext": b64(ciphertext)}, head)
    return found, _messages(body, skip_unknown)


def _messages(body: bytes, skip_unknown: bool = False) -> list[dict]:
    try:
        d = zlib.decompressobj(wbits=31)
        text = d.decompress(body, MAX_BODY)
        if d.unconsumed_tail or d.unused_data or not d.eof:
            raise CloudError("cut short, or too large")
        lines = [json.loads(line) for line in text.decode().split("\n")[:-1]]
    except (zlib.error, ValueError, UnicodeDecodeError):
        raise CloudError("not a readable segment body") from None
    if not lines or not isinstance(lines[-1], dict) or set(lines[-1]) != {"end"} \
            or lines[-1]["end"] != {"lines": len(lines) - 1}:
        raise CloudError("cut short")
    messages = lines[:-1]
    if any(not isinstance(m, dict) or len(m) != 1 for m in messages):
        raise CloudError("not a readable segment body")
    if skip_unknown:
        return [m for m in messages if next(iter(m)) in MESSAGES]
    if any(next(iter(m)) not in MESSAGES for m in messages):
        raise CloudError("holds a message a newer byobot wrote; upgrade byobot")
    return messages


def size(messages: list[dict]) -> int:
    """How many events, samples and deletions a segment's messages carry, against the hub's batch limit."""
    count = 0
    for m in messages:
        body: Any = next(iter(m.values()))
        if "events" in m:
            count += len(body.get("events", []))
        elif "samples" in m:
            count += len(body.get("samples", [])) + len(body.get("deleted", []))
        else:
            count += 1
    return count
