"""Credential-encryption tests — no live DB, no network. Security-adjacent:
verify the authenticated-encryption properties explicitly rather than assuming
the library does the right thing."""

import base64
import secrets
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag

import jim.crypto as crypto_mod

VALID_KEY = base64.b64encode(secrets.token_bytes(32)).decode()


def _patch_key(monkeypatch, key: str) -> None:
    monkeypatch.setattr(
        crypto_mod, "settings", lambda: SimpleNamespace(credential_encryption_key=key)
    )


@pytest.mark.parametrize(
    "plaintext",
    [
        "short",
        "",
        "a" * 2000,  # realistic Garmin session-token blob length
        '{"oauth1_token": "abc123", "oauth2_token": {"scope": "connect:all"}}',
        "unicode: café 日本語 emoji 🏋️",
    ],
)
def test_roundtrip(monkeypatch, plaintext):
    _patch_key(monkeypatch, VALID_KEY)
    blob = crypto_mod.encrypt(plaintext)
    assert crypto_mod.decrypt(blob) == plaintext


def test_encrypt_uses_random_nonce(monkeypatch):
    """Two encryptions of the same plaintext must differ — a reused nonce with
    AES-GCM breaks confidentiality and authentication."""
    _patch_key(monkeypatch, VALID_KEY)
    blob1 = crypto_mod.encrypt("same plaintext every time")
    blob2 = crypto_mod.encrypt("same plaintext every time")
    assert blob1 != blob2
    # And both still decrypt correctly despite differing.
    assert crypto_mod.decrypt(blob1) == "same plaintext every time"
    assert crypto_mod.decrypt(blob2) == "same plaintext every time"


def test_decrypt_rejects_tampered_ciphertext(monkeypatch):
    _patch_key(monkeypatch, VALID_KEY)
    blob = bytearray(crypto_mod.encrypt("do not tamper with me"))
    # Flip one bit well past the nonce, inside the ciphertext/tag.
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        crypto_mod.decrypt(bytes(blob))


def test_decrypt_rejects_tampered_nonce(monkeypatch):
    _patch_key(monkeypatch, VALID_KEY)
    blob = bytearray(crypto_mod.encrypt("do not tamper with me either"))
    blob[0] ^= 0x01
    with pytest.raises(InvalidTag):
        crypto_mod.decrypt(bytes(blob))


def test_encrypt_missing_key_raises_clear_error(monkeypatch):
    _patch_key(monkeypatch, "")
    with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEY"):
        crypto_mod.encrypt("anything")


def test_decrypt_missing_key_raises_clear_error(monkeypatch):
    _patch_key(monkeypatch, "")
    with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEY"):
        crypto_mod.decrypt(b"irrelevant-bytes")


def test_wrong_length_key_raises_clear_error(monkeypatch):
    bad_key = base64.b64encode(b"too-short").decode()
    _patch_key(monkeypatch, bad_key)
    with pytest.raises(RuntimeError, match="32 bytes"):
        crypto_mod.encrypt("anything")
