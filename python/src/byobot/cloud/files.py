"""The small files beside the segments: epochs, devices, sealed keys and the recovery key.

All names are numbers or random ids, never names of people or devices.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .crypto import (FORMAT, CloudError, Identity, decrypt, dumps, encrypt, peek_document, read_document, seal,
                     sign_document, unb64, unseal)
from .segment import ID, MAX_EPOCH

CLASSES = ("keeper", "relay")
RECOVERY = "recovery"
EPOCH_FILE = re.compile(r"epochs/([0-9]{10})\.json")


def epoch_path(epoch: int) -> str:
    return f"epochs/{epoch:010d}.json"


def epoch_number(path: str) -> int | None:
    """The epoch an epoch file is of, or None for any other path."""
    found = EPOCH_FILE.fullmatch(path)
    return int(found[1]) if found else None


def epoch_dir(epoch: int) -> str:
    """The digits that name an epoch in paths."""
    return f"{epoch:010d}"


def device_path(device_id: str) -> str:
    return f"devices/{device_id}.json"


def key_path(epoch: int, device_id: str) -> str:
    return f"keys/{epoch:010d}/{device_id}.key"


def recovery_path(epoch: int) -> str:
    return f"keys/{epoch:010d}/{RECOVERY}.key"


def _key_context(store_id: str, epoch: int, device_id: str) -> bytes:
    return f"byobot-cloud key 1\n{store_id}\n{epoch}\n{device_id}".encode()


def _recovery_context(store_id: str, epoch: int) -> bytes:
    return f"byobot-cloud recovery 1\n{store_id}\n{epoch}".encode()


DEVICE = ("device_id", "device_class", "box", "sign")
CAPABILITY = re.compile(r"[a-z0-9_]{1,64}")


def _capabilities(value: Any) -> list[str]:
    """What a device supports beyond this format, by name; names it doesn't know a reader ignores."""
    if not isinstance(value, list) or len(value) > 64 \
            or not all(isinstance(c, str) and CAPABILITY.fullmatch(c) for c in value):
        raise CloudError("not a byobot capability list")
    return value


def _check_device(device: Any) -> dict:
    if not isinstance(device, dict) or not isinstance(device.get("device_id"), str) \
            or not ID.fullmatch(device["device_id"]) or device["device_id"] == RECOVERY \
            or device.get("device_class") not in CLASSES:
        raise CloudError("not a byobot device")
    unb64(device.get("box"), 32)
    unb64(device.get("sign"), 32)
    return {k: device[k] for k in DEVICE}


def _matches(body: dict, store_id: str, epoch: int) -> None:
    if body.get("format") != FORMAT:
        raise CloudError("written by a newer byobot; upgrade byobot")
    if body.get("store_id") != store_id:
        raise CloudError("from another store")
    if body.get("epoch") != epoch:
        raise CloudError("moved or renamed")


# -- devices -------------------------------------------------------------------

def device_file(store_id: str, device: dict, proof: str | None = None, capabilities: list[str] = ()) -> bytes:
    """A device's public keys and capabilities, plain. A relay's carries its join proof."""
    return dumps({"format": FORMAT, "store_id": store_id, **_check_device(device),
                  "capabilities": _capabilities(list(capabilities)), **({"proof": proof} if proof else {})})


def read_device(data: bytes, store_id: str, device_id: str) -> dict:
    try:
        body = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise CloudError("not a byobot device") from None
    if not isinstance(body, dict) or body.get("format") != FORMAT:
        raise CloudError("not a byobot device, or written by a newer byobot")
    if body.get("store_id") != store_id:
        raise CloudError("from another store")
    device = _check_device(body)
    if device["device_id"] != device_id:
        raise CloudError("moved or renamed")
    return {**device, "capabilities": _capabilities(body.get("capabilities")),
            **({"proof": body["proof"]} if isinstance(body.get("proof"), str) else {})}


# -- epochs --------------------------------------------------------------------

def epoch_file(keeper: Identity, store_id: str, epoch: int, keeper_device_id: str, devices: list[dict],
               checkpoint: int, capabilities: list[str] = (), closed: bool = False) -> bytes:
    """The keeper's signed record of an epoch: the devices that hold its key, its checkpoint's parts, and what the
    keeper supports beyond this format."""
    if not 0 <= epoch <= MAX_EPOCH:
        raise ValueError("epoch out of range")
    return sign_document(keeper, {"format": FORMAT, "store_id": store_id, "epoch": epoch,
                                  "keeper_device_id": keeper_device_id,
                                  "devices": [_check_device(d) for d in devices], "checkpoint": checkpoint,
                                  "capabilities": _capabilities(list(capabilities)),
                                  **({"closed": True} if closed else {})})


def is_closed(data: bytes | None) -> bool:
    """Whether an epoch file says its folder was closed. Not verified, since a new keeper doesn't hold the old one's
    key; only a folder holding nothing but epoch files is taken over on its word."""
    try:
        return peek_document(data or b"").get("closed") is True
    except CloudError:
        return False


def read_epoch(data: bytes, keeper_sign: str, store_id: str, epoch: int) -> dict:
    body = read_document(data, keeper_sign)
    _matches(body, store_id, epoch)
    if not isinstance(body.get("devices"), list) or not isinstance(body.get("checkpoint"), int) \
            or not isinstance(body.get("keeper_device_id"), str):
        raise CloudError("not a byobot epoch")
    body["devices"] = [_check_device(d) for d in body["devices"]]
    _capabilities(body.get("capabilities"))
    return body


# -- sealed keys -----------------------------------------------------------------

def key_file(keeper: Identity, store_id: str, epoch: int, device: dict, epoch_key: bytes,
             ephemeral: Any = None) -> bytes:
    """An epoch key sealed to one device and signed by the keeper, which also vouches for the device's keys."""
    device = _check_device(device)
    sealed = seal(epoch_key, device["box"], _key_context(store_id, epoch, device["device_id"]), ephemeral)
    return sign_document(keeper, {"format": FORMAT, "store_id": store_id, "epoch": epoch, "device": device,
                                  "sealed": sealed})


def key_device(data: bytes, keeper_sign: str, store_id: str, epoch: int) -> dict:
    """The device a key file admits, once the keeper's signature checks out."""
    body = read_document(data, keeper_sign)
    _matches(body, store_id, epoch)
    return _check_device(body.get("device"))


def open_key(data: bytes, me: Identity, device_id: str, keeper_sign: str, store_id: str, epoch: int) -> bytes:
    body = read_document(data, keeper_sign)
    _matches(body, store_id, epoch)
    device = _check_device(body.get("device"))
    if device["device_id"] != device_id or device["box"] != me.public()["box"]:
        raise CloudError("sealed to another device")
    key = unseal(me, body.get("sealed") or {}, _key_context(store_id, epoch, device_id))
    if len(key) != 32:
        raise CloudError("not an epoch key")
    return key


# -- recovery --------------------------------------------------------------------

def recovery_file(recovery_key: bytes, store_id: str, epoch: int, content: dict, nonce: bytes | None = None) -> bytes:
    """What rebuilds a store from the folder alone (epoch key, store key, the keeper's keys), sealed to the code."""
    sealed = encrypt(recovery_key, dumps(content), _recovery_context(store_id, epoch), nonce)
    return dumps({"format": FORMAT, "store_id": store_id, "epoch": epoch, **sealed})


def open_recovery(data: bytes, recovery_key: bytes, store_id: str, epoch: int) -> dict:
    try:
        body = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise CloudError("not a byobot recovery key") from None
    if not isinstance(body, dict):
        raise CloudError("not a byobot recovery key")
    _matches(body, store_id, epoch)
    try:
        return json.loads(decrypt(recovery_key, body, _recovery_context(store_id, epoch)))
    except CloudError:
        raise CloudError("that recovery code doesn't open this store") from None
