"""Tests for the protected-secret string compatibility facade."""
from __future__ import annotations

import secrets
import sys

import pytest


def test_encrypt_decrypt_roundtrip():
    """Encrypt a key and decrypt it back — should round-trip correctly."""
    import whisperstream.key_encryption as key_encryption
    from whisperstream.protected_secret_contract import (
        ProtectedSecretError,
        ProtectedSecretFailure,
    )

    plaintext = secrets.token_urlsafe(24)
    if sys.platform not in {"darwin", "win32"}:
        with pytest.raises(ProtectedSecretError) as caught:
            key_encryption.encrypt_api_key(plaintext)
        assert caught.value.failure is ProtectedSecretFailure.UNAVAILABLE
        return

    reference = key_encryption.encrypt_api_key(plaintext)
    assert reference != plaintext
    if sys.platform == "win32":
        assert reference.startswith("enc:")
    else:
        assert reference.startswith("keychain:v1:")
    assert key_encryption.decrypt_api_key(reference) == plaintext


def test_encrypt_empty_string():
    """encrypt_api_key on empty string returns empty string (no-op)."""
    from whisperstream.key_encryption import encrypt_api_key

    assert encrypt_api_key("") == ""


def test_decrypt_empty_string():
    """decrypt_api_key on empty string returns empty string (no-op)."""
    from whisperstream.key_encryption import decrypt_api_key

    assert decrypt_api_key("") == ""


def test_decrypt_plaintext_passthrough():
    """A non-enc: value is returned unchanged by decrypt_api_key."""
    from whisperstream.key_encryption import decrypt_api_key

    val = "some-plaintext-key"
    assert decrypt_api_key(val) == val


def test_decrypt_corrupted_data():
    """Corrupted enc: blob returns empty string, does not crash."""
    from whisperstream.key_encryption import decrypt_api_key

    result = decrypt_api_key("enc:notbase64!!!garbage###")
    # Should return "" (empty) rather than raising or returning the corrupted blob.
    assert result == "", f"Expected empty string for corrupted data, got: {result!r}"


def test_fingerprint_api_key():
    """fingerprint_api_key masks the middle, keeping first 4 + last 4, and never
    leaks the real key length or middle. This is the only key-derived value sent
    to the frontend (the full plaintext stays server-side)."""
    from whisperstream.key_encryption import fingerprint_api_key

    # Empty / whitespace -> empty.
    assert fingerprint_api_key("") == ""
    assert fingerprint_api_key("   ") == ""

    # Long key: first 4 + a FIXED run of bullets + last 4; middle never present.
    fp = fingerprint_api_key("AIzaSyTESTmiddleSECRET1234abcd3f9a")
    assert fp == "AIza" + "•" * 10 + "3f9a"
    assert "middleSECRET" not in fp

    # Short key (<= 8 chars): all bullets, fixed length, never the chars (so the
    # real length is not leaked).
    short = fingerprint_api_key("sk-12345")
    assert short == "•" * 8
    assert "12345" not in short


def test_unavailable_provider_fails_closed(monkeypatch):
    """Provider unavailability never returns or writes plaintext."""
    import whisperstream.key_encryption as ke
    from whisperstream.protected_secret import UnavailableProtectedSecretStore
    from whisperstream.protected_secret_contract import (
        ProtectedSecretError,
        ProtectedSecretFailure,
    )

    monkeypatch.setattr(
        ke,
        "get_protected_secret_store",
        lambda: UnavailableProtectedSecretStore(),
    )

    with pytest.raises(ProtectedSecretError) as caught:
        ke.encrypt_api_key("synthetic-unavailable-value")
    assert caught.value.failure is ProtectedSecretFailure.UNAVAILABLE
    assert "synthetic-unavailable-value" not in str(caught.value)

    # A pre-existing reference remains unusable without its provider.
    assert ke.decrypt_api_key("enc:dGVzdA==") == ""
