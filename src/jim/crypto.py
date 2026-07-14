"""App-level encryption for credentials at rest (Garmin passwords/tokens,
Notion tokens in `user_credentials`). Postgres `pgcrypto` was rejected because
it needs the key passed in every SQL statement, which risks it landing in
query logs; encrypting in the app instead keeps the key only in process
memory, matching the existing trust model where secrets live in env vars and
never touch the database in plaintext.

AES-256-GCM: authenticated, so a tampered ciphertext raises instead of
decrypting to silent garbage. Nonce is random per call and stored alongside
the ciphertext (`nonce + ciphertext`) rather than derived, since nothing here
tracks a counter across process restarts.
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jim.config import settings

_NONCE_LEN = 12


def _key() -> bytes:
    raw = settings().credential_encryption_key
    if not raw:
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is not set — cannot encrypt/decrypt"
            " credentials. Generate a 32-byte base64 key and set it in the env."
        )
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY must decode to exactly 32 bytes"
            f" (got {len(key)}) — generate a fresh AES-256 key."
        )
    return key


def encrypt(plaintext: str) -> bytes:
    aesgcm = AESGCM(_key())
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def decrypt(blob: bytes) -> str:
    aesgcm = AESGCM(_key())
    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
