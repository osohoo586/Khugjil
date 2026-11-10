"""Field encryption, keyed HMACs and token helpers.

* Subkeys are derived from the 32-byte master key with keyed BLAKE2b (the same construction as
  libsodium's ``crypto_kdf``), one per purpose, so a leak of one subkey does not expose others.
* Field encryption is XChaCha20-Poly1305 (libsodium via PyNaCl). Associated data binds a
  ciphertext to its tenant, table and column: copying it to another row fails to decrypt.
* HMAC inputs are length-prefixed so different tuples can never collide by concatenation.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import os
import secrets
from uuid import UUID

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError

_FORMAT_VERSION = 1
_NONCE_BYTES = 24
_KDF_PERSON = b"khugjil-kdf-v1"


class Purpose(enum.IntEnum):
    FIELD_ENC = 1
    ACCOUNT_HMAC = 2
    JOURNAL_HMAC = 3
    IDEMPOTENCY = 4
    INBOUND_HMAC = 5
    CHAT_HMAC = 6
    CSRF = 7
    SHORT_CODE = 8


class DecryptError(Exception):
    """Ciphertext was tampered with, moved to another row, or uses an unknown key."""


Part = str | bytes | UUID | int


def _to_bytes(part: Part) -> bytes:
    if isinstance(part, bytes):
        return part
    if isinstance(part, UUID):
        return part.bytes
    if isinstance(part, int):
        return str(part).encode()
    return part.encode("utf-8")


def length_prefixed(*parts: Part) -> bytes:
    out = bytearray()
    for part in parts:
        b = _to_bytes(part)
        out += len(b).to_bytes(4, "big")
        out += b
    return bytes(out)


def derive_subkey(master: bytes, purpose: Purpose, version: int = 1) -> bytes:
    if len(master) != 32:
        raise ValueError("master key must be 32 bytes")
    salt = purpose.value.to_bytes(8, "little") + version.to_bytes(8, "little")
    return hashlib.blake2b(b"", key=master, salt=salt, person=_KDF_PERSON, digest_size=32).digest()


def field_aad(tenant_id: UUID | None, table: str, column: str) -> bytes:
    return length_prefixed(tenant_id.bytes if tenant_id else b"", table, column)


class Crypto:
    key_version = 1

    def __init__(self, master_key: bytes) -> None:
        self._keys = {p: derive_subkey(master_key, p, self.key_version) for p in Purpose}

    def __repr__(self) -> str:
        return "<Crypto redacted>"

    def hmac(self, purpose: Purpose, *parts: Part) -> bytes:
        if purpose is Purpose.FIELD_ENC:
            raise ValueError("FIELD_ENC key is not an HMAC key")
        return hmac.new(self._keys[purpose], length_prefixed(*parts), hashlib.sha256).digest()

    def encrypt(self, plaintext: str | bytes, aad: bytes) -> bytes:
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        nonce = os.urandom(_NONCE_BYTES)
        ct = crypto_aead_xchacha20poly1305_ietf_encrypt(
            data, aad, nonce, self._keys[Purpose.FIELD_ENC]
        )
        return bytes([_FORMAT_VERSION]) + nonce + ct

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        if len(blob) < 1 + _NONCE_BYTES + 16 or blob[0] != _FORMAT_VERSION:
            raise DecryptError("unknown ciphertext format")
        nonce, ct = blob[1 : 1 + _NONCE_BYTES], blob[1 + _NONCE_BYTES :]
        try:
            return bytes(
                crypto_aead_xchacha20poly1305_ietf_decrypt(
                    ct, aad, nonce, self._keys[Purpose.FIELD_ENC]
                )
            )
        except CryptoError as exc:
            raise DecryptError("authentication failed") from exc

    def decrypt_str(self, blob: bytes, aad: bytes) -> str:
        return self.decrypt(blob, aad).decode("utf-8")


def new_token(nbytes: int = 32) -> str:
    """URL-safe random token (default 256 bits)."""
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str | bytes) -> bytes:
    """Tokens are high-entropy, so a plain SHA-256 is enough to store them."""
    data = token.encode("utf-8") if isinstance(token, str) else token
    return hashlib.sha256(data).digest()


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def constant_time_eq(a: str | bytes, b: str | bytes) -> bool:
    return hmac.compare_digest(_to_bytes(a), _to_bytes(b))
