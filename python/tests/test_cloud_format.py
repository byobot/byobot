"""Cloud protocol 1's files: the golden vectors in schema/fixtures/v1/cloud.json, and what a reader refuses."""

import base64
import gzip
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from byobot.cloud import files, segment
from byobot.cloud.crypto import (CloudError, Identity, b64, dumps, join_proof, new_key, proof_matches, read_document,
                                 recovery_code, recovery_key)
from byobot.core.contract import cloud_errors

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "schema" / "v1").is_dir())
VECTORS = json.loads((ROOT / "schema" / "fixtures" / "v1" / "cloud.json").read_text(encoding="utf-8"))
INPUTS = VECTORS["inputs"]
KEEPER, RELAY = INPUTS["keeper"], INPUTS["relay"]
STORE = INPUTS["store_id"]


def unb64(text):
    return base64.b64decode(text)


def open_file(path, data, case=None):
    case = case or {}
    store = case.get("store_id", STORE)
    if path.endswith(".seg"):
        writer = RELAY if path.startswith("segments/") and RELAY["id"] in path else KEEPER
        found, messages = segment.read(data, path=path, store_id=store, key=unb64(case.get("key", INPUTS["epoch_key"])),
                                       sign_public=case.get("signer", writer["sign"]))
        return {"header": found, "messages": messages}
    if path.startswith("devices/"):
        return files.read_device(data, store, path[len("devices/"):-len(".json")])
    if path.startswith("epochs/"):
        return files.read_epoch(data, case.get("signer", KEEPER["sign"]), store, int(path[7:11]))
    epoch = int(path[5:9])
    if path.endswith("recovery.key"):
        return files.open_recovery(data, unb64(INPUTS["recovery_key"]), store, epoch)
    key = files.open_key(data, Identity.from_private(RELAY["private"]), RELAY["id"],
                         case.get("signer", KEEPER["sign"]), store, epoch)
    return {"epoch_key": b64(key)}


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_each_valid_file_opens_to_what_it_should(case):
    data = unb64(case["data"])
    assert open_file(case["path"], data) == case["expect"]
    if case["def"] == "header":
        assert cloud_errors("header", segment.header(data)) == []
        for m in case["expect"]["messages"]:
            assert cloud_errors("message", m) == []
    else:
        assert cloud_errors(case["def"], json.loads(data)) == []
    if case["def"] == "signed":
        body = json.loads(unb64(json.loads(data)["body"]))
        assert cloud_errors("epoch_body" if case["path"].startswith("epochs/") else "key_body", body) == []


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=lambda c: c["name"])
def test_each_broken_file_is_refused(case):
    with pytest.raises(CloudError, match=case["error"]):
        open_file(case["path"], unb64(case["data"]), case)


def test_the_inputs_match_their_public_keys_and_code():
    for who in (KEEPER, RELAY):
        assert Identity.from_private(who["private"]).public() == {"box": who["box"], "sign": who["sign"]}
    assert b64(recovery_key(INPUTS["recovery_code"], STORE)) == INPUTS["recovery_key"]


def test_the_deterministic_files_match_byte_for_byte():
    """Sealing, signing and the recovery file don't involve gzip, so every implementation writes the same bytes."""
    keeper = Identity.from_private(KEEPER["private"])
    epoch = next(c for c in VECTORS["valid"] if c["name"] == "epoch file")
    devices = epoch["expect"]["devices"]
    assert b64(files.epoch_file(keeper, STORE, 1, KEEPER["id"], devices, 1)) == epoch["data"]
    device = next(c for c in VECTORS["valid"] if c["name"] == "relay device file")
    public = {"box": RELAY["box"], "sign": RELAY["sign"]}
    proof = join_proof(unb64(INPUTS["join_secret"]), STORE, RELAY["id"], public)
    assert b64(files.device_file(STORE, {"device_id": RELAY["id"], "device_class": "relay", **public}, proof)) == device["data"]
    sealed = next(c for c in VECTORS["valid"] if c["name"] == "epoch key sealed to the relay")
    ephemeral = X25519PrivateKey.from_private_bytes(unb64(INPUTS["seal_ephemeral"]))
    relay = {"device_id": RELAY["id"], "device_class": "relay", **public}
    assert b64(files.key_file(keeper, STORE, 1, relay, unb64(INPUTS["epoch_key"]), ephemeral)) == sealed["data"]
    recovery = next(c for c in VECTORS["valid"] if c["name"] == "recovery key")
    nonce = unb64(json.loads(unb64(recovery["data"]))["nonce"])
    again = files.recovery_file(unb64(INPUTS["recovery_key"]), STORE, 1, recovery["expect"], nonce)
    assert b64(again) == recovery["data"]


def test_a_segment_round_trips_and_its_lines_end_with_a_count():
    me, key = Identity.generate(), new_key()
    messages = [{"events": {"events": []}}, {"samples": {"samples": []}}]
    data = segment.write("segment", STORE, "dev-1", 3, 42, messages, key, me)
    path = segment.name("segment", 3, 42, "dev-1")
    assert path == "segments/dev-1/0003-0000000042.seg"
    assert segment.read(data, path=path, store_id=STORE, key=key, sign_public=me.public()["sign"])[1] == messages
    assert segment.parse_name(path) == {"kind": "segment", "epoch": 3, "seq": 42, "device_id": "dev-1"}
    assert segment.parse_name("segments/dev-1/3-42.seg") is None


def _forge(body: bytes, me: Identity, key: bytes, path="segments/dev-1/0001-0000000001.seg"):
    """A properly signed and encrypted segment around an arbitrary body."""
    from byobot.cloud.crypto import encrypt
    nonce = bytes(12)
    head = dumps({"magic": segment.MAGIC, "format": 1, "kind": "segment", "store_id": STORE, "device_id": "dev-1",
                  "epoch": 1, "seq": 1, "nonce": b64(nonce)}) + b"\n"
    ct = unb64(encrypt(key, body, head, nonce)["ciphertext"])
    return segment.read(head + ct + me.signature(segment.DOMAIN, head + ct), path=path, store_id=STORE, key=key,
                        sign_public=me.public()["sign"])


@pytest.mark.parametrize("body, error", [
    (gzip.compress(b'{"events":{"events":[]}}\n'), "cut short"),
    (gzip.compress(b'{"events":{"events":[]}}\n{"end":{"lines":2}}\n'), "cut short"),
    (gzip.compress(b'{"meal":{}}\n{"end":{"lines":1}}\n'), "upgrade byobot"),
    (gzip.compress(b'{"end":{"lines":0}}\n') + gzip.compress(b'{"end":{"lines":0}}\n'), "cut short"),
    (gzip.compress(b"\0" * (segment.MAX_BODY + 1)), "too large"),
    (b"not gzip", "readable"),
])
def test_a_signed_segment_with_a_bad_body_is_still_refused(body, error):
    me, key = Identity.generate(), new_key()
    with pytest.raises(CloudError, match=error):
        _forge(body, me, key)


def test_a_key_sealed_to_one_device_opens_only_for_it():
    keeper, phone, other = Identity.generate(), Identity.generate(), Identity.generate()
    device = {"device_id": "dev-1", "device_class": "relay", **phone.public()}
    key = new_key()
    data = files.key_file(keeper, STORE, 2, device, key)
    assert files.open_key(data, phone, "dev-1", keeper.public()["sign"], STORE, 2) == key
    assert files.key_device(data, keeper.public()["sign"], STORE, 2) == device
    with pytest.raises(CloudError, match="another device"):
        files.open_key(data, other, "dev-1", keeper.public()["sign"], STORE, 2)
    with pytest.raises(CloudError, match="renamed"):
        files.open_key(data, phone, "dev-1", keeper.public()["sign"], STORE, 3)
    with pytest.raises(CloudError, match="signed"):
        files.open_key(files.key_file(other, STORE, 2, device, key), phone, "dev-1", keeper.public()["sign"],
                       STORE, 2)


def test_a_signed_document_can_t_be_edited():
    me = Identity.generate()
    doc = json.loads(files.epoch_file(me, STORE, 1, "k", [], 0))
    body = json.loads(unb64(doc["body"]))
    doc["body"] = b64(dumps({**body, "devices": [{"device_id": "x", "device_class": "relay", **me.public()}]}))
    with pytest.raises(CloudError, match="signed"):
        read_document(dumps(doc), me.public()["sign"])


def test_recovery_codes_forgive_case_spaces_and_dashes():
    code = recovery_code()
    assert len(code.replace("-", "")) == 32 and code.count("-") == 7
    assert recovery_key(code.lower().replace("-", " "), STORE) == recovery_key(code, STORE)
    assert recovery_key(code, STORE) != recovery_key(code, "0193a0b0-0000-7000-8000-00000000c002")
    for bad in ("", "A" * 31, "1" * 32):
        with pytest.raises(CloudError, match="recovery code"):
            recovery_key(bad, STORE)


def test_a_join_proof_binds_the_device_and_its_keys():
    secret, me = b"\x01" * 16, Identity.generate()
    proof = join_proof(secret, STORE, "dev-1", me.public())
    assert proof_matches(secret, STORE, "dev-1", me.public(), proof)
    assert not proof_matches(secret, STORE, "dev-2", me.public(), proof)
    assert not proof_matches(b"\x02" * 16, STORE, "dev-1", me.public(), proof)
    assert not proof_matches(secret, STORE, "dev-1", Identity.generate().public(), proof)
    assert not proof_matches(secret, STORE, "dev-1", me.public(), None)
