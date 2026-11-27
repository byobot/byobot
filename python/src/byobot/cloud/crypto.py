"""Keys for cloud protocol 1: device identities, keys sealed to a device, signed documents and recovery codes."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..util import ByobotError

FORMAT = 1
KEY_BYTES = 32
RECOVERY_BYTES = 20
SEAL_INFO = b"byobot-cloud seal 1"
RECOVERY_INFO = b"byobot-cloud recovery 1"
DOCUMENT_DOMAIN = b"byobot-cloud document 1\n"
JOIN_DOMAIN = "byobot-cloud join 1"
_RAW = serialization.Encoding.Raw, serialization.PublicFormat.Raw
_RAW_PRIVATE = serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()


class CloudError(ByobotError):
    """A cloud file that can't be trusted or read."""


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def unb64(text: Any, size: int | None = None) -> bytes:
    try:
        data = base64.b64decode(text, validate=True) if isinstance(text, str) else None
    except binascii.Error:
        data = None
    if data is None or (size is not None and len(data) != size):
        raise CloudError("not a valid key or ciphertext")
    return data


def dumps(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def new_key() -> bytes:
    return os.urandom(KEY_BYTES)


@dataclass(frozen=True)
class Identity:
    """A device's two key pairs: one to receive sealed keys (X25519), one to sign what it writes (Ed25519)."""

    box: X25519PrivateKey
    sign: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> Identity:
        return cls(X25519PrivateKey.generate(), Ed25519PrivateKey.generate())

    @classmethod
    def from_private(cls, private: dict) -> Identity:
        return cls(X25519PrivateKey.from_private_bytes(unb64(private["box"], KEY_BYTES)),
                   Ed25519PrivateKey.from_private_bytes(unb64(private["sign"], KEY_BYTES)))

    def private(self) -> dict:
        return {"box": b64(self.box.private_bytes(*_RAW_PRIVATE)), "sign": b64(self.sign.private_bytes(*_RAW_PRIVATE))}

    def public(self) -> dict:
        return {"box": b64(self.box.public_key().public_bytes(*_RAW)),
                "sign": b64(self.sign.public_key().public_bytes(*_RAW))}

    def signature(self, domain: bytes, data: bytes) -> bytes:
        return self.sign.sign(domain + data)


def verify(sign_public: str, domain: bytes, data: bytes, signature: bytes) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(unb64(sign_public, KEY_BYTES)).verify(signature, domain + data)
    except InvalidSignature:
        raise CloudError("changed or cut short since its writer signed it, or signed by another device") from None


# -- sealing a key to a device -------------------------------------------------

def _seal_key(shared: bytes, ephemeral: bytes, recipient: bytes) -> bytes:
    return HKDF(hashes.SHA256(), KEY_BYTES, salt=ephemeral + recipient, info=SEAL_INFO).derive(shared)


def seal(secret: bytes, to_box: str, context: bytes, ephemeral: X25519PrivateKey | None = None) -> dict:
    """``secret`` sealed to a device's public box key, bound to ``context``. Only that device can open it."""
    ephemeral = ephemeral or X25519PrivateKey.generate()
    recipient = unb64(to_box, KEY_BYTES)
    eph = ephemeral.public_key().public_bytes(*_RAW)
    key = _seal_key(ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient)), eph, recipient)
    return {"ephemeral": b64(eph), "ciphertext": b64(ChaCha20Poly1305(key).encrypt(bytes(12), secret, context))}


def unseal(identity: Identity, sealed: dict, context: bytes) -> bytes:
    eph = unb64(sealed.get("ephemeral"), KEY_BYTES)
    mine = identity.box.public_key().public_bytes(*_RAW)
    key = _seal_key(identity.box.exchange(X25519PublicKey.from_public_bytes(eph)), eph, mine)
    try:
        return ChaCha20Poly1305(key).decrypt(bytes(12), unb64(sealed.get("ciphertext")), context)
    except InvalidTag:
        raise CloudError("sealed to another device, or changed") from None


# -- signed documents ------------------------------------------------------------

def sign_document(identity: Identity, body: dict) -> bytes:
    """A small JSON file its writer signed: the exact body bytes, and the signature over them."""
    raw = dumps(body)
    return dumps({"format": FORMAT, "body": b64(raw), "sig": b64(identity.signature(DOCUMENT_DOMAIN, raw))})


def _document(data: bytes) -> tuple[bytes, bytes]:
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise CloudError("not a byobot cloud file") from None
    if not isinstance(doc, dict) or "body" not in doc:
        raise CloudError("not a byobot cloud file")
    if doc.get("format") != FORMAT:
        raise CloudError("written by a newer byobot; upgrade byobot")
    return unb64(doc["body"]), unb64(doc.get("sig"), 64)


def _body(raw: bytes) -> dict:
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        raise CloudError("not a byobot cloud file")
    return body


def peek_document(data: bytes) -> dict:
    """A signed document's body, not yet verified: only to find who should have signed it."""
    return _body(_document(data)[0])


def read_document(data: bytes, sign_public: str) -> dict:
    raw, sig = _document(data)
    verify(sign_public, DOCUMENT_DOMAIN, raw, sig)
    return _body(raw)


# -- recovery codes ----------------------------------------------------------------

RECOVERY_VERSION = "A"  # the code's first character; a newer format takes the next letter
RECOVERY_CHECK = 3  # base32 characters of SHA-256 at the end, so a mistyped code is told apart from a wrong one
RECOVERY_LOOKALIKES = str.maketrans("018", "OIB")


def recovery_code(raw: bytes | None = None) -> str:
    """160 random bits as a code shown once at setup: a version letter, 32 base32 characters and three check
    characters, in nine groups of four."""
    secret = base64.b32encode(raw or os.urandom(RECOVERY_BYTES)).decode()
    text = RECOVERY_VERSION + secret + _recovery_check(RECOVERY_VERSION + secret)
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def _recovery_check(text: str) -> str:
    return base64.b32encode(hashlib.sha256(text.encode()).digest()).decode()[:RECOVERY_CHECK]


def recovery_key(code: str, store_id: str) -> bytes:
    """The key a recovery code opens. Case, spaces and dashes don't matter, and 0, 1 and 8 read as O, I and B."""
    text = "".join(code.split()).replace("-", "").upper().translate(RECOVERY_LOOKALIKES)
    if text[:1] != RECOVERY_VERSION and len(text) == 36 and text[:1].isalpha():
        raise CloudError("that recovery code is from a newer byobot; upgrade byobot")
    if len(text) != 1 + 32 + RECOVERY_CHECK or text[:1] != RECOVERY_VERSION:
        raise CloudError("that isn't a byobot recovery code")
    if not hmac.compare_digest(_recovery_check(text[:33]), text[33:]):
        raise CloudError("that recovery code has a typo; check it against where you kept it")
    try:
        raw = base64.b32decode(text[1:33])
    except (binascii.Error, ValueError):
        raise CloudError("that isn't a byobot recovery code") from None
    return HKDF(hashes.SHA256(), KEY_BYTES, salt=store_id.encode(), info=RECOVERY_INFO).derive(raw)


def encrypt(key: bytes, plaintext: bytes, context: bytes, nonce: bytes | None = None) -> dict:
    nonce = nonce or os.urandom(12)
    return {"nonce": b64(nonce), "ciphertext": b64(ChaCha20Poly1305(key).encrypt(nonce, plaintext, context))}


def decrypt(key: bytes, sealed: dict, context: bytes) -> bytes:
    try:
        return ChaCha20Poly1305(key).decrypt(unb64(sealed.get("nonce"), 12), unb64(sealed.get("ciphertext")), context)
    except InvalidTag:
        raise CloudError("can't be opened with this key, or changed") from None


# -- joining ---------------------------------------------------------------------

def join_proof(secret: bytes, store_id: str, device_id: str, public: dict) -> str:
    """What a joining device shows it holds the one-time join secret: an HMAC over who it says it is."""
    message = "\n".join((JOIN_DOMAIN, store_id, device_id, public["box"], public["sign"])).encode()
    return b64(hmac.new(secret, message, hashlib.sha256).digest())


def proof_matches(secret: bytes, store_id: str, device_id: str, public: dict, proof: Any) -> bool:
    return isinstance(proof, str) and hmac.compare_digest(join_proof(secret, store_id, device_id, public), proof)
