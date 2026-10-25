"""Tests for the transcript_crypto module (AES-256-GCM over the DPAPI wrapper).

The AESGCM round-trip itself is platform-independent and must pass everywhere.
DPAPI-seal-on-disk specifics are gated on ``sys.platform == "win32"`` (mirroring
``tests/test_key_encryption.py``) because a DPAPI ``enc:`` blob cannot be
unsealed on non-Windows / without pywin32.
"""
from __future__ import annotations

import sys


def _dpapi_available() -> bool:
    """True when DPAPI sealing actually persists an ``enc:`` blob.

    Mirrors the gating in test_key_encryption.py: only on win32 with pywin32
    does encrypt_api_key produce an ``enc:`` value that decrypt_api_key can
    later unseal to the SAME process/account.
    """
    if sys.platform != "win32":
        return False
    try:
        import win32crypt  # noqa: F401
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------- #
# AESGCM round-trip (platform-independent)
# --------------------------------------------------------------------------- #


def test_encrypt_decrypt_bytes_roundtrip():
    """encrypt -> decrypt returns the original bytes."""
    from whisperstream.transcript_crypto import decrypt, encrypt

    key = bytes(range(32))
    plaintext = b"some raw audio or text bytes \x00\x01\x02"
    nonce, ct = encrypt(key, plaintext)
    assert len(nonce) == 12
    assert ct != plaintext
    assert decrypt(key, nonce, ct) == plaintext


def test_encrypt_decrypt_text_roundtrip():
    """encrypt_text -> decrypt_text returns the original string (utf-8)."""
    from whisperstream.transcript_crypto import decrypt_text, encrypt_text

    key = bytes(range(32))
    plaintext = "Hello, world. Unicode: café, 日本語, emoji 🎙️"
    nonce, ct = encrypt_text(key, plaintext)
    assert decrypt_text(key, nonce, ct) == plaintext


def test_empty_text_roundtrip():
    """Empty string round-trips correctly (not confused with failure)."""
    from whisperstream.transcript_crypto import decrypt_text, encrypt_text

    key = bytes(range(32))
    nonce, ct = encrypt_text(key, "")
    assert decrypt_text(key, nonce, ct) == ""


def test_nonce_and_ciphertext_are_unique_per_call():
    """Two encryptions of the SAME plaintext produce DIFFERENT nonces AND ciphertexts.

    A reused nonce under the same key catastrophically breaks AES-GCM, so this
    is a load-bearing invariant.
    """
    from whisperstream.transcript_crypto import encrypt

    key = bytes(range(32))
    plaintext = b"identical plaintext"
    nonce1, ct1 = encrypt(key, plaintext)
    nonce2, ct2 = encrypt(key, plaintext)
    assert nonce1 != nonce2, "nonce must be fresh per call"
    assert ct1 != ct2, "ciphertext must differ when the nonce differs"
    # Both still decrypt back to the same plaintext.
    from whisperstream.transcript_crypto import decrypt

    assert decrypt(key, nonce1, ct1) == plaintext
    assert decrypt(key, nonce2, ct2) == plaintext


def test_decrypt_tampered_ciphertext_returns_none():
    """A tampered ciphertext fails the GCM tag and returns None (no crash)."""
    from whisperstream.transcript_crypto import decrypt, encrypt

    key = bytes(range(32))
    nonce, ct = encrypt(key, b"sensitive transcript")
    tampered = bytearray(ct)
    tampered[0] ^= 0xFF  # flip a bit
    assert decrypt(key, nonce, bytes(tampered)) is None


def test_decrypt_wrong_key_returns_none():
    """Decrypting with the wrong key returns None, never raises."""
    from whisperstream.transcript_crypto import decrypt, encrypt

    key = bytes(range(32))
    wrong_key = bytes(range(1, 33))
    nonce, ct = encrypt(key, b"sensitive transcript")
    assert decrypt(wrong_key, nonce, ct) is None


def test_decrypt_garbage_returns_none():
    """Garbage nonce/ciphertext returns None instead of raising."""
    from whisperstream.transcript_crypto import decrypt, decrypt_text

    key = bytes(range(32))
    assert decrypt(key, b"short", b"garbage") is None
    assert decrypt_text(key, b"short", b"garbage") is None


# --------------------------------------------------------------------------- #
# Data-key load / create / persist
# --------------------------------------------------------------------------- #


def test_load_or_create_data_key_returns_32_bytes(tmp_path):
    """First call creates a 32-byte AES-256 data key and writes the sealed file."""
    from whisperstream.transcript_crypto import load_or_create_data_key

    key_path = tmp_path / "transcript.key"
    key = load_or_create_data_key(key_path)
    assert key is not None
    assert isinstance(key, bytes)
    assert len(key) == 32
    assert key_path.exists()


def test_load_or_create_data_key_reloads_same_key(tmp_path):
    """A second call reads the file back and returns the SAME key bytes."""
    from whisperstream.transcript_crypto import load_or_create_data_key

    key_path = tmp_path / "transcript.key"
    key1 = load_or_create_data_key(key_path)
    key2 = load_or_create_data_key(key_path)
    assert key1 is not None and key2 is not None
    assert key1 == key2


def test_created_key_actually_encrypts(tmp_path):
    """The created/reloaded key is usable for a real encrypt/decrypt round-trip."""
    from whisperstream.transcript_crypto import (
        decrypt_text,
        encrypt_text,
        load_or_create_data_key,
    )

    key_path = tmp_path / "transcript.key"
    key1 = load_or_create_data_key(key_path)
    assert key1 is not None
    nonce, ct = encrypt_text(key1, "round-trip across reload")
    # Reload the key from disk and decrypt with the reloaded copy.
    key2 = load_or_create_data_key(key_path)
    assert key2 is not None
    assert decrypt_text(key2, nonce, ct) == "round-trip across reload"


def test_corrupt_key_file_yields_none(tmp_path):
    """A corrupt / foreign sealed key file yields None (not garbage / empty)."""
    from whisperstream.transcript_crypto import load_or_create_data_key

    key_path = tmp_path / "transcript.key"
    if _dpapi_available():
        # A real enc: blob that cannot be unsealed (truncated base64 garbage).
        key_path.write_text("enc:not-real-base64-dpapi-blob!!!", encoding="utf-8")
        assert load_or_create_data_key(key_path) is None
    else:
        # Without DPAPI an enc: blob is treated as undecryptable (decrypt_api_key
        # returns "" off-Windows), which the loader surfaces as None.
        key_path.write_text("enc:dGVzdA==", encoding="utf-8")
        assert load_or_create_data_key(key_path) is None


def test_non_base64_unsealed_payload_yields_none(tmp_path):
    """A stored value that unseals to a non-base64 / wrong-length payload yields None.

    On non-Windows the stored value passes through decrypt_api_key unchanged, so
    a non-enc: junk value lets us exercise the loader's base64/length guard.
    """
    from whisperstream.transcript_crypto import load_or_create_data_key

    key_path = tmp_path / "transcript.key"
    if not _dpapi_available():
        # Passthrough on non-Windows: this junk reaches the base64/length check.
        key_path.write_text("this is not base64 of a 32 byte key", encoding="utf-8")
        assert load_or_create_data_key(key_path) is None
