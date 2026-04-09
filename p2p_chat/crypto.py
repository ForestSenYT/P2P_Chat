"""
Message encryption for P2P chat.

Scheme:
    - Each room can optionally have a passphrase.
    - A 256-bit AES key is derived via PBKDF2-HMAC-SHA256
      (salt = room name, 100 000 iterations).
    - Messages are encrypted with AES-GCM (authenticated encryption):
      a random 12-byte nonce is prepended to the ciphertext.
    - The encrypted payload is base64-encoded and placed in the
      message's "encrypted_msg" field; the plaintext "msg" field
      is replaced with a placeholder so nodes without the key
      can still route the message.

Only the message *body* is encrypted.  Routing metadata (id, type,
from, room, ttl) stays in plaintext so that gossip forwarding
works for all nodes, even those without the key.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# PBKDF2 parameters.
_KDF_ITERATIONS = 100_000
_KEY_LEN = 32  # 256 bits


def derive_key(passphrase: str, room: str) -> bytes:
    """Derive a 256-bit AES key from a passphrase + room name."""
    salt = room.encode("utf-8")
    return hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, _KDF_ITERATIONS, dklen=_KEY_LEN
    )


def encrypt(plaintext: str, key: bytes) -> str:
    """Encrypt *plaintext* with AES-256-GCM.  Returns a base64 string."""
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(token: str, key: bytes) -> str | None:
    """Decrypt a base64 AES-GCM token.  Returns None on failure."""
    try:
        raw = base64.b64decode(token)
        if len(raw) < 13:  # 12-byte nonce + at least 1 byte
            return None
        nonce, ct = raw[:12], raw[12:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
    except Exception:
        return None


# ── Binary-level encryption (for voice UDP) ────────────────────────

def encrypt_bytes(data: bytes, key: bytes) -> bytes:
    """Encrypt raw bytes with AES-256-GCM.  Returns nonce + ciphertext."""
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt_bytes(data: bytes, key: bytes) -> bytes | None:
    """Decrypt raw bytes.  Returns None on failure."""
    try:
        if len(data) < 13:
            return None
        return AESGCM(key).decrypt(data[:12], data[12:], None)
    except Exception:
        return None
