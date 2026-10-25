"""Tests for the lockdown_keystore module (envelope persistence + migration).

The keystore owns the on-disk lockdown key envelope file
(``lockdown.key.json``) layered on top of the EXISTING transcript data key from
``transcript_crypto.load_or_create_data_key`` (DPAPI-sealed ``transcript.key``).
Enabling lockdown is a TRANSPARENT migration: it re-wraps the SAME data key
under a password and a fresh recovery code, persisting both envelopes. It never
rewrites transcript DB rows or audio files, and it never regenerates the data
key, so existing transcripts keep decrypting across a full enable/disable cycle.

These tests use a throwaway ``tmp_path`` data dir. On the (non-Windows / no
pywin32) test platform ``encrypt_api_key``/``decrypt_api_key`` round-trip
plaintext, so ``load_or_create_data_key`` returns a stable 32-byte key.
"""
from __future__ import annotations

import json
import logging

import pytest

# The password used across the happy-path round-trip tests.
PASSWORD = "correct horse battery staple"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _data_key(data_dir):
    """The existing transcript data key for *data_dir* (created on first use)."""
    from whisperstream.transcript_crypto import load_or_create_data_key

    return load_or_create_data_key(data_dir / "transcript.key")


# --------------------------------------------------------------------------- #
# is_lockdown_enabled
# --------------------------------------------------------------------------- #


def test_is_lockdown_enabled_false_before_enable(tmp_path):
    from whisperstream.lockdown_keystore import is_lockdown_enabled

    assert is_lockdown_enabled(tmp_path) is False


def test_is_lockdown_enabled_true_after_enable(tmp_path):
    from whisperstream.lockdown_keystore import enable_lockdown, is_lockdown_enabled

    enable_lockdown(tmp_path, PASSWORD)
    assert is_lockdown_enabled(tmp_path) is True


def test_enable_writes_envelope_file(tmp_path):
    from whisperstream.lockdown_keystore import enable_lockdown

    enable_lockdown(tmp_path, PASSWORD)
    assert (tmp_path / "lockdown.key.json").exists()


# --------------------------------------------------------------------------- #
# enable_lockdown -> recovery code
# --------------------------------------------------------------------------- #


def test_enable_returns_grouped_recovery_code(tmp_path):
    import re

    from whisperstream.lockdown_keystore import enable_lockdown

    code = enable_lockdown(tmp_path, PASSWORD)
    assert isinstance(code, str)
    assert re.match(r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$", code), code


def test_enable_deletes_dpapi_key_but_wraps_the_same_key(tmp_path):
    """F2: enabling lockdown does NOT regenerate the data key, but it DOES delete
    the DPAPI-only ``transcript.key`` so the password becomes load-bearing at rest.

    (Rewritten from the pre-F2 ``test_enable_does_not_generate_a_new_data_key``,
    which asserted ``transcript.key`` was left untouched — that was exactly the
    F2 vulnerability: DPAPI alone could unwrap the data key without the password.
    The invariant "no new key is generated" still holds and is asserted via the
    password round-trip; the new, more-secure behavior is that transcript.key is
    gone after enable.)
    """
    from whisperstream.lockdown_keystore import enable_lockdown, unlock_with_password

    key_before = _data_key(tmp_path)
    assert (tmp_path / "transcript.key").exists()
    enable_lockdown(tmp_path, PASSWORD)
    # The DPAPI-sealed key file is gone: no DPAPI-only decryption path remains.
    assert not (tmp_path / "transcript.key").exists()
    # The password unwraps the SAME key (migrated into the envelope, not minted).
    assert unlock_with_password(tmp_path, PASSWORD) == key_before


# --------------------------------------------------------------------------- #
# unlock_with_password / unlock_with_recovery_code -> SAME data key
# --------------------------------------------------------------------------- #


def test_unlock_with_password_returns_the_existing_data_key(tmp_path):
    """The round-trip property: unlock yields the SAME bytes load_or_create_data_key gives."""
    from whisperstream.lockdown_keystore import enable_lockdown, unlock_with_password

    expected = _data_key(tmp_path)
    enable_lockdown(tmp_path, PASSWORD)
    assert unlock_with_password(tmp_path, PASSWORD) == expected


def test_unlock_with_recovery_code_returns_the_existing_data_key(tmp_path):
    from whisperstream.lockdown_keystore import enable_lockdown, unlock_with_recovery_code

    expected = _data_key(tmp_path)
    code = enable_lockdown(tmp_path, PASSWORD)
    assert unlock_with_recovery_code(tmp_path, code) == expected


def test_both_unlock_paths_return_identical_bytes(tmp_path):
    """Password and recovery code both unwrap to the exact same data key."""
    from whisperstream.lockdown_keystore import (
        enable_lockdown,
        unlock_with_password,
        unlock_with_recovery_code,
    )

    # Capture the key BEFORE enable: F2 deletes transcript.key on enable, so
    # calling _data_key() afterward would mint a fresh (mismatched) key.
    expected = _data_key(tmp_path)
    code = enable_lockdown(tmp_path, PASSWORD)
    via_pw = unlock_with_password(tmp_path, PASSWORD)
    via_rc = unlock_with_recovery_code(tmp_path, code)
    assert via_pw is not None
    assert via_pw == via_rc == expected


def test_unlock_with_wrong_password_returns_none(tmp_path):
    from whisperstream.lockdown_keystore import enable_lockdown, unlock_with_password

    enable_lockdown(tmp_path, PASSWORD)
    assert unlock_with_password(tmp_path, "not-the-password") is None


def test_unlock_with_wrong_recovery_code_returns_none(tmp_path):
    from whisperstream.lockdown_keystore import enable_lockdown, unlock_with_recovery_code

    enable_lockdown(tmp_path, PASSWORD)
    assert unlock_with_recovery_code(tmp_path, "WRNG-WRNG-WRNG-WRNG-WRNG") is None


def test_unlock_when_not_enabled_returns_none(tmp_path):
    from whisperstream.lockdown_keystore import unlock_with_password, unlock_with_recovery_code

    assert unlock_with_password(tmp_path, PASSWORD) is None
    assert unlock_with_recovery_code(tmp_path, "WRNG-WRNG-WRNG-WRNG-WRNG") is None


def test_recovery_code_normalization_through_keystore(tmp_path):
    """A lower-cased / hyphen-stripped recovery code still unlocks."""
    from whisperstream.lockdown_keystore import enable_lockdown, unlock_with_recovery_code

    expected = _data_key(tmp_path)
    code = enable_lockdown(tmp_path, PASSWORD)
    assert unlock_with_recovery_code(tmp_path, code.lower()) == expected
    assert unlock_with_recovery_code(tmp_path, code.replace("-", "")) == expected


# --------------------------------------------------------------------------- #
# change_password
# --------------------------------------------------------------------------- #


def test_change_password_succeeds_and_old_password_stops_working(tmp_path):
    from whisperstream.lockdown_keystore import (
        change_password,
        enable_lockdown,
        unlock_with_password,
    )

    expected = _data_key(tmp_path)
    enable_lockdown(tmp_path, PASSWORD)
    assert change_password(tmp_path, PASSWORD, "a-brand-new-password") is True
    # New password unlocks to the same data key.
    assert unlock_with_password(tmp_path, "a-brand-new-password") == expected
    # Old password no longer unlocks.
    assert unlock_with_password(tmp_path, PASSWORD) is None


def test_change_password_wrong_current_returns_false(tmp_path):
    from whisperstream.lockdown_keystore import (
        change_password,
        enable_lockdown,
        unlock_with_password,
    )

    enable_lockdown(tmp_path, PASSWORD)
    assert change_password(tmp_path, "wrong-current", "whatever-new") is False
    # The original password still works (no mutation on failure).
    assert unlock_with_password(tmp_path, PASSWORD) is not None


def test_change_password_when_not_enabled_returns_false(tmp_path):
    from whisperstream.lockdown_keystore import change_password

    assert change_password(tmp_path, PASSWORD, "new") is False


def test_change_password_leaves_recovery_code_intact(tmp_path):
    """Changing the password must not invalidate the recovery code envelope."""
    from whisperstream.lockdown_keystore import (
        change_password,
        enable_lockdown,
        unlock_with_recovery_code,
    )

    expected = _data_key(tmp_path)
    code = enable_lockdown(tmp_path, PASSWORD)
    assert change_password(tmp_path, PASSWORD, "new-password") is True
    assert unlock_with_recovery_code(tmp_path, code) == expected


# --------------------------------------------------------------------------- #
# regenerate_recovery_code
# --------------------------------------------------------------------------- #


def test_regenerate_recovery_code_invalidates_old_and_mints_new(tmp_path):
    from whisperstream.lockdown_keystore import (
        enable_lockdown,
        regenerate_recovery_code,
        unlock_with_recovery_code,
    )

    expected = _data_key(tmp_path)
    old_code = enable_lockdown(tmp_path, PASSWORD)
    new_code = regenerate_recovery_code(tmp_path, PASSWORD)
    assert isinstance(new_code, str)
    assert new_code != old_code
    # New code unlocks to the same data key.
    assert unlock_with_recovery_code(tmp_path, new_code) == expected
    # Old code no longer unlocks.
    assert unlock_with_recovery_code(tmp_path, old_code) is None


def test_regenerate_recovery_code_wrong_password_returns_none(tmp_path):
    from whisperstream.lockdown_keystore import (
        enable_lockdown,
        regenerate_recovery_code,
        unlock_with_recovery_code,
    )

    old_code = enable_lockdown(tmp_path, PASSWORD)
    assert regenerate_recovery_code(tmp_path, "wrong-password") is None
    # The old recovery code is untouched on failure.
    assert unlock_with_recovery_code(tmp_path, old_code) is not None


def test_regenerate_recovery_code_when_not_enabled_returns_none(tmp_path):
    from whisperstream.lockdown_keystore import regenerate_recovery_code

    assert regenerate_recovery_code(tmp_path, PASSWORD) is None


def test_regenerate_recovery_code_leaves_password_working(tmp_path):
    from whisperstream.lockdown_keystore import (
        enable_lockdown,
        regenerate_recovery_code,
        unlock_with_password,
    )

    expected = _data_key(tmp_path)
    enable_lockdown(tmp_path, PASSWORD)
    regenerate_recovery_code(tmp_path, PASSWORD)
    assert unlock_with_password(tmp_path, PASSWORD) == expected


# --------------------------------------------------------------------------- #
# disable_lockdown
# --------------------------------------------------------------------------- #


def test_disable_lockdown_requires_correct_password(tmp_path):
    from whisperstream.lockdown_keystore import (
        disable_lockdown,
        enable_lockdown,
        is_lockdown_enabled,
    )

    enable_lockdown(tmp_path, PASSWORD)
    assert disable_lockdown(tmp_path, "wrong-password") is False
    # Still enabled after a failed disable.
    assert is_lockdown_enabled(tmp_path) is True


def test_disable_lockdown_removes_only_the_lockdown_envelopes(tmp_path):
    """Disable verifies the password, RE-SEALS the data key to DPAPI, then
    deletes the lockdown envelope file.

    F2: enable deleted the DPAPI-only ``transcript.key``; disable re-materializes
    it (re-sealed to DPAPI) so transcripts stay decryptable at rest with no row
    rewrite. After disable, lockdown is no longer enabled and transcript.key is
    back on disk.
    """
    from whisperstream.lockdown_keystore import (
        disable_lockdown,
        enable_lockdown,
        is_lockdown_enabled,
    )

    enable_lockdown(tmp_path, PASSWORD)
    # Enable removed the DPAPI-only key (F2).
    assert not (tmp_path / "transcript.key").exists()
    assert disable_lockdown(tmp_path, PASSWORD) is True
    assert is_lockdown_enabled(tmp_path) is False
    assert not (tmp_path / "lockdown.key.json").exists()
    # Disable re-sealed the transcript key to DPAPI, so it is back on disk.
    assert (tmp_path / "transcript.key").exists()


def test_disable_lockdown_when_not_enabled_returns_false(tmp_path):
    from whisperstream.lockdown_keystore import disable_lockdown

    assert disable_lockdown(tmp_path, PASSWORD) is False


# --------------------------------------------------------------------------- #
# Transparent migration: transcripts survive a full enable/disable cycle
# --------------------------------------------------------------------------- #


def test_full_enable_disable_cycle_keeps_data_key_decryptable(tmp_path):
    """The end-to-end transparent-migration property.

    A transcript encrypted under the data key before lockdown is enabled must
    still decrypt after a full enable -> disable cycle, because the data key and
    its DPAPI seal are never changed by the keystore.
    """
    from whisperstream.lockdown_keystore import (
        disable_lockdown,
        enable_lockdown,
        unlock_with_password,
    )
    from whisperstream.transcript_crypto import decrypt_text, encrypt_text

    data_key = _data_key(tmp_path)
    nonce, ciphertext = encrypt_text(data_key, "a sensitive note")

    # Enable lockdown; the password must unlock to the SAME data key.
    enable_lockdown(tmp_path, PASSWORD)
    unlocked = unlock_with_password(tmp_path, PASSWORD)
    assert unlocked == data_key
    assert decrypt_text(unlocked, nonce, ciphertext) == "a sensitive note"

    # Disable lockdown; the data key (re-loaded via the unchanged seal) still
    # decrypts the pre-existing transcript.
    assert disable_lockdown(tmp_path, PASSWORD) is True
    reloaded = _data_key(tmp_path)
    assert reloaded == data_key
    assert decrypt_text(reloaded, nonce, ciphertext) == "a sensitive note"


def test_re_enable_after_disable_mints_a_fresh_recovery_code(tmp_path):
    """Re-enabling after a disable produces a working new password + recovery code."""
    from whisperstream.lockdown_keystore import (
        disable_lockdown,
        enable_lockdown,
        unlock_with_password,
        unlock_with_recovery_code,
    )

    expected = _data_key(tmp_path)
    enable_lockdown(tmp_path, PASSWORD)
    disable_lockdown(tmp_path, PASSWORD)
    new_code = enable_lockdown(tmp_path, "second-password")
    assert unlock_with_password(tmp_path, "second-password") == expected
    assert unlock_with_recovery_code(tmp_path, new_code) == expected


def test_enable_respects_data_dir_argument_not_env(tmp_path):
    """The keystore writes to the data_dir it is given, isolated per directory."""
    from whisperstream.lockdown_keystore import enable_lockdown, is_lockdown_enabled

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    enable_lockdown(dir_a, PASSWORD)
    assert is_lockdown_enabled(dir_a) is True
    # The other dir is untouched.
    assert is_lockdown_enabled(dir_b) is False


# --------------------------------------------------------------------------- #
# Persistence shape + secret hygiene
# --------------------------------------------------------------------------- #


def test_envelope_file_is_valid_json_with_both_wrappers(tmp_path):
    from whisperstream.lockdown_keystore import enable_lockdown

    enable_lockdown(tmp_path, PASSWORD)
    blob = json.loads((tmp_path / "lockdown.key.json").read_text(encoding="utf-8"))
    assert isinstance(blob, dict)
    # Two independent wrappers must be present.
    assert "password" in blob
    assert "recovery" in blob
    for wrapper in (blob["password"], blob["recovery"]):
        for f in ("salt", "nonce", "ciphertext", "kdf", "params"):
            assert f in wrapper


def test_data_key_never_appears_in_the_envelope_file(tmp_path):
    """The plaintext data key (and its base64/hex) must not leak to disk."""
    import base64

    from whisperstream.lockdown_keystore import enable_lockdown

    data_key = _data_key(tmp_path)
    enable_lockdown(tmp_path, PASSWORD)
    raw = (tmp_path / "lockdown.key.json").read_text(encoding="utf-8")
    assert base64.b64encode(data_key).decode("ascii") not in raw
    assert data_key.hex() not in raw


def test_keystore_does_not_log_secrets(tmp_path, caplog):
    """No password, recovery code, or data-key bytes are ever logged."""
    from whisperstream.lockdown_keystore import (
        change_password,
        disable_lockdown,
        enable_lockdown,
        regenerate_recovery_code,
        unlock_with_password,
    )

    data_key = _data_key(tmp_path)
    with caplog.at_level(logging.DEBUG):
        code = enable_lockdown(tmp_path, PASSWORD)
        unlock_with_password(tmp_path, PASSWORD)
        unlock_with_password(tmp_path, "wrong")
        change_password(tmp_path, PASSWORD, "new-password")
        new_code = regenerate_recovery_code(tmp_path, "new-password")
        disable_lockdown(tmp_path, "new-password")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert PASSWORD not in joined
    assert "new-password" not in joined
    assert code not in joined
    if new_code:
        assert new_code not in joined
    assert data_key.hex() not in joined


def test_enable_returns_none_handling_when_data_key_unavailable(tmp_path, monkeypatch):
    """If the data key cannot be loaded, enable must not silently mint a bogus envelope.

    ``load_or_create_data_key`` returns None on a DPAPI/account mismatch; the
    keystore must surface that as a failure (raise or return None) rather than
    wrapping a fake key.
    """
    import whisperstream.lockdown_keystore as ks

    monkeypatch.setattr(ks, "load_or_create_data_key", lambda *_a, **_k: None)
    with pytest.raises(Exception):
        ks.enable_lockdown(tmp_path, PASSWORD)
