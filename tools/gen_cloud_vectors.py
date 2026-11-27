#!/usr/bin/env python3
"""Write schema/fixtures/v1/cloud.json: cloud protocol 1 files made from fixed keys, and broken copies of them.

    python tools/gen_cloud_vectors.py           # write the fixtures
    python tools/gen_cloud_vectors.py --check   # fail if they differ from what this writes

Every implementation must open the valid files to the expected values and refuse the broken ones.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402

from byobot.cloud import files, segment  # noqa: E402
from byobot.cloud.crypto import (Identity, b64, join_proof, recovery_code, recovery_key,  # noqa: E402
                                 sign_document)

OUT = ROOT / "schema" / "fixtures" / "v1" / "cloud.json"
STORE = "0193a0b0-0000-7000-8000-00000000c001"
OTHER_STORE = "0193a0b0-0000-7000-8000-00000000c002"
KEEPER = "0193a0b0-0000-7000-8000-00000000d001"
RELAY = "0193a0b0-0000-7000-8000-00000000d002"


def fixed(label: str, n: int = 32) -> bytes:
    return hashlib.sha256(f"byobot example {label}".encode()).digest()[:n]


def identity(label: str) -> Identity:
    return Identity.from_private({"box": b64(fixed(f"{label} box")), "sign": b64(fixed(f"{label} sign"))})


def build() -> dict:
    keeper, relay, rotated = identity("keeper"), identity("relay"), identity("rotated keeper")
    keeper_dev = {"device_id": KEEPER, "device_class": "keeper", **keeper.public()}
    relay_dev = {"device_id": RELAY, "device_class": "relay", **relay.public()}
    epoch_key, other_key, secret = fixed("epoch key"), fixed("other epoch key"), fixed("join secret", 16)
    code = recovery_code(fixed("recovery code", 20))
    rkey = recovery_key(code, STORE)
    event = {"id": "0193a0b0-0000-7000-8000-00000000e001", "schema_version": 1,
             "record": {"kind": "effort", "id": "0193a0b0-0000-7000-8000-00000000f001"}, "op": "put",
             "kind_version": 1, "hlc": "1795131000000-000000", "ts": "2026-11-21T07:10:30-05:00", "device_id": RELAY,
             "actor": {"kind": "human", "id": "me"}, "prev": [],
             "payload": {"activity_id": "barbell_back_squat", "time": {"start": "2026-11-21T07:10:00-05:00"},
                         "metrics": {"reps": {"value": 5}, "rpe": {"value": 7.5}}}}
    samples = {"samples": [{"source": "apple_health", "source_id": "8f2e1c3a-0000-4000-8000-000000000001",
                            "type": "HKQuantityTypeIdentifierBodyMass", "start": "2026-11-22T07:00:00-05:00",
                            "end": "2026-11-22T07:00:00-05:00", "value": 82.4, "unit": "kg"}]}
    plan = {"from": "2026-11-22", "to": "2026-12-05", "since": "2026-11-22", "activities": [], "locations": [],
            "settings": [], "plans": [], "planned_sessions": [], "overlays": [], "sessions": [], "efforts": [],
            "store_id": STORE, "hlc": "1795131000000-000001", "removed": [], "erased": [],
            "kind_versions": {"effort": 1}, "received": {RELAY: "1795131000000-000000"}, "rejected": []}
    relay_messages = [{"events": {"events": [event], "kind_versions": {"effort": 1}}}, {"samples": samples}]
    seg_path = segment.name("segment", 1, 1, RELAY)
    seg = segment.write("segment", STORE, RELAY, 1, 1, relay_messages, epoch_key, relay, fixed("nonce 1", 12))
    plan_path = segment.name("plan", 1, 2)
    plan_messages = [{"plan": plan}, {"receipts": {RELAY: {"read": [1], "rejected": [
        {"seq": 1, "line": 1, "reason": "a phone sends only apple_health samples"}]}}}]
    plan_seg = segment.write("plan", STORE, KEEPER, 1, 2, plan_messages, epoch_key, keeper, fixed("nonce 2", 12))
    cp_path = segment.name("checkpoint", 1, 0)
    cp = segment.write("checkpoint", STORE, KEEPER, 1, 0, [{"events": {"events": [event]}}], epoch_key, keeper,
                       fixed("nonce 3", 12))
    recovery_content = {"epoch_key": b64(epoch_key), "store_key": fixed("store key").hex(),
                        "keeper_device_id": KEEPER, "keeper_private": keeper.private()}

    def tampered(data: bytes, old: bytes, new: bytes) -> bytes:
        assert data.count(old) == 1
        return data.replace(old, new)

    rotated_dev = {"device_id": KEEPER, "device_class": "keeper", **rotated.public()}
    handover = files.epoch_file(rotated, STORE, 2, KEEPER, [rotated_dev, relay_dev], 1, previous=keeper)
    replayed = files.epoch_file(rotated, STORE, 3, KEEPER, [rotated_dev, relay_dev], 1)
    replayed = json.loads(base64.b64decode(json.loads(replayed)["body"]))
    replayed["handover"] = json.loads(base64.b64decode(json.loads(handover)["body"]))["handover"]
    replayed = sign_document(rotated, replayed)
    newer = segment.write("segment", STORE, RELAY, 1, 1, relay_messages, epoch_key, relay, fixed("nonce 1", 12))
    newer = tampered(newer, b'"format":1', b'"format":2')
    valid = [
        {"name": "relay device file", "path": files.device_path(RELAY), "def": "device_file",
         "data": files.device_file(STORE, relay_dev, join_proof(secret, STORE, RELAY, relay.public())),
         "expect": {**relay_dev, "capabilities": [], "proof": join_proof(secret, STORE, RELAY, relay.public())}},
        {"name": "epoch file", "path": files.epoch_path(1), "def": "signed",
         "data": files.epoch_file(keeper, STORE, 1, KEEPER, [keeper_dev, relay_dev], 1),
         "expect": {"format": 1, "store_id": STORE, "epoch": 1, "keeper_device_id": KEEPER,
                    "keeper_key": keeper.public()["sign"], "devices": [keeper_dev, relay_dev], "checkpoint": 1,
                    "capabilities": []}},
        {"name": "epoch file handed over to a new keeper key", "path": files.epoch_path(2), "def": "signed",
         "data": handover, "expect": json.loads(base64.b64decode(json.loads(handover)["body"]))},
        {"name": "epoch key sealed to the relay by the new keeper key", "path": files.key_path(2, RELAY),
         "def": "signed", "signer": rotated.public()["sign"],
         "data": files.key_file(rotated, STORE, 2, relay_dev, fixed("second epoch key"),
                                X25519PrivateKey.from_private_bytes(fixed("second ephemeral"))),
         "expect": {"epoch_key": b64(fixed("second epoch key"))}},
        {"name": "epoch key sealed to the relay", "path": files.key_path(1, RELAY), "def": "signed",
         "data": files.key_file(keeper, STORE, 1, relay_dev, epoch_key,
                                X25519PrivateKey.from_private_bytes(fixed("ephemeral"))),
         "expect": {"epoch_key": b64(epoch_key)}},
        {"name": "recovery key", "path": files.recovery_path(1), "def": "recovery_file",
         "data": files.recovery_file(rkey, STORE, 1, recovery_content, fixed("nonce 4", 12)),
         "expect": recovery_content},
        {"name": "relay segment", "path": seg_path, "def": "header", "data": seg,
         "expect": {"header": segment.header(seg), "messages": relay_messages}},
        {"name": "plan", "path": plan_path, "def": "header", "data": plan_seg,
         "expect": {"header": segment.header(plan_seg), "messages": plan_messages}},
        {"name": "checkpoint part", "path": cp_path, "def": "header", "data": cp,
         "expect": {"header": segment.header(cp), "messages": [{"events": {"events": [event]}}]}},
    ]
    invalid = [
        {"name": "header changed", "path": seg_path, "data": tampered(seg, b'"seq":1', b'"seq":2'),
         "error": "moved or renamed"},
        {"name": "header and path changed together", "path": segment.name("segment", 1, 2, RELAY),
         "data": tampered(seg, b'"seq":1', b'"seq":2'), "error": "signed"},
        {"name": "moved to another device's folder", "path": segment.name("segment", 1, 1, KEEPER), "data": seg,
         "error": "moved or renamed"},
        {"name": "body cut short", "path": seg_path, "data": seg[:-100], "error": "cut short"},
        {"name": "body changed", "path": seg_path, "data": seg[:-80] + bytes([seg[-80] ^ 1]) + seg[-79:],
         "error": "signed"},
        {"name": "another store", "path": seg_path, "store_id": OTHER_STORE, "data": seg, "error": "another store"},
        {"name": "another epoch key", "path": seg_path, "key": b64(other_key), "data": seg, "error": "key"},
        {"name": "signed by another device", "path": plan_path, "data": plan_seg, "signer": relay.public()["sign"],
         "error": "signed"},
        {"name": "newer format", "path": seg_path, "data": newer, "error": "upgrade byobot"},
        {"name": "epoch file signed by a relay", "path": files.epoch_path(1),
         "data": files.epoch_file(relay, STORE, 1, KEEPER, [keeper_dev, relay_dev], 1), "error": "signed"},
        {"name": "epoch file handed over by a relay", "path": files.epoch_path(2),
         "data": files.epoch_file(rotated, STORE, 2, KEEPER, [rotated_dev, relay_dev], 1, previous=relay),
         "error": "no handover"},
        {"name": "handover from another epoch", "path": files.epoch_path(3), "data": replayed, "error": "signed"},
        {"name": "recovery key opened with another code", "path": files.recovery_path(1),
         "data": files.recovery_file(recovery_key(recovery_code(fixed("another code", 20)), STORE), STORE, 1,
                                     recovery_content, fixed("nonce 4", 12)), "error": "recovery code"},
    ]
    for case in valid + invalid:
        case["data"] = b64(case["data"])
    follow = following(keeper, rotated, keeper_dev, relay_dev, rotated_dev, epoch_key)
    return {
        "description": "Conformance files for cloud.schema.json, made from fixed keys. Every implementation opens "
                       "the valid ones to `expect` and refuses the invalid ones with an error containing `error`. "
                       "A case's own `store_id`, `key` or `signer` replaces the inputs'. In `follow`, a relay that just "
                       "joined, trusting the keeper key, reads `first` if there is one, then `folder`, and holds "
                       "`expect`.",
        "schema_version": 1,
        "inputs": {"store_id": STORE, "keeper": {"id": KEEPER, "private": keeper.private(), **keeper.public()},
                   "relay": {"id": RELAY, "private": relay.private(), **relay.public()}, "epoch_key": b64(epoch_key),
                   "join_secret": b64(secret), "seal_ephemeral": b64(fixed("ephemeral")), "recovery_code": code, "recovery_key": b64(rkey)},
        "valid": valid,
        "invalid": invalid,
        "follow": follow,
    }


def following(keeper: Identity, rotated: Identity, keeper_dev: dict, relay_dev: dict, rotated_dev: dict,
              epoch_key: bytes) -> list[dict]:
    """Folders a relay reads: which epoch, key and keeper key it takes from each, or that it was removed."""
    again, stranger = identity("keeper rotated again"), identity("stranger")
    again_dev = {"device_id": KEEPER, "device_class": "keeper", **again.public()}
    keys = {n: fixed(f"follow epoch key {n}") for n in (2, 3)}
    keys[1] = epoch_key

    def epoch(n, signer, devices, previous=None):
        return files.epoch_path(n), files.epoch_file(signer, STORE, n, KEEPER, devices, 1, previous=previous)

    def key(n, signer):
        return files.key_path(n, RELAY), files.key_file(signer, STORE, n, relay_dev, keys[n],
                                                        X25519PrivateKey.from_private_bytes(fixed(f"follow seal {n}")))

    one = [epoch(1, keeper, [keeper_dev, relay_dev]), key(1, keeper)]
    two = one + [epoch(2, rotated, [rotated_dev, relay_dev], keeper), key(2, rotated)]
    at_one = {"epoch": 1, "keeper_key": keeper.public()["sign"], "key": b64(keys[1])}
    at_two = {"epoch": 2, "keeper_key": rotated.public()["sign"], "key": b64(keys[2])}
    first = {"at_one": one, "at_two": two}
    cases = [
        ("a new relay takes the epoch its key file is in", None, one, {**at_one, "removed": False}),
        ("admitted mid-epoch: no epoch file lists it yet, its key file does", None,
         [epoch(1, keeper, [keeper_dev]), key(1, keeper)], {**at_one, "removed": False}),
        ("a handover moves trust to the keeper's new key", "at_one", two, {**at_two, "removed": False}),
        ("two handovers in a row are followed in order", "at_one",
         two + [epoch(3, again, [again_dev, relay_dev], rotated), key(3, again)],
         {"epoch": 3, "keeper_key": again.public()["sign"], "key": b64(keys[3]), "removed": False}),
        ("an epoch file from a key with no handover is skipped", "at_one",
         one + [epoch(2, stranger, [relay_dev]), key(2, stranger)], {**at_one, "removed": False}),
        ("the old keys can't take trust back", "at_two",
         two + [epoch(3, keeper, [keeper_dev, relay_dev]), key(3, keeper)], {**at_two, "removed": False}),
        ("a newer epoch with no key file yet is waited for", "at_one",
         one + [epoch(2, keeper, [keeper_dev, relay_dev])], {**at_one, "removed": False}),
        ("a newer epoch that leaves the relay out removes it", "at_one",
         one + [epoch(2, keeper, [keeper_dev])], {"epoch": 1, "keeper_key": keeper.public()["sign"], "key": None,
                                                  "removed": True}),
    ]
    def listed(files_: list) -> dict:
        return {path: b64(data) for path, data in files_}

    return [{"name": name, "first": listed(first[start]) if start else None, "folder": listed(folder),
             "expect": expect} for name, start, folder, expect in cases]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if parser.parse_args().check:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.relative_to(ROOT)} is out of date; run python tools/gen_cloud_vectors.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
