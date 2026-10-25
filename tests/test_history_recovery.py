"""Standard-history recovery envelope and its Lockdown transitions."""

from __future__ import annotations

import json

import uuid
from pathlib import Path

import pytest

from whisperstream import history_recovery, lockdown_keystore
from whisperstream.protected_secret_contract import (
    MACOS_KEYCHAIN_PREFIX,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
    ProtectionAvailability,
)
from whisperstream.transcript_crypto import load_data_key, load_or_create_data_key

PASSWORD = "a long recovery passphrase"


class _Store:
    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}

    def availability(self) -> ProtectionAvailability:
        return ProtectionAvailability.AVAILABLE

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        del purpose
        reference = f"{MACOS_KEYCHAIN_PREFIX}{uuid.uuid4()}"
        self.items[reference] = bytes(secret)
        return reference

    def load(self, reference: str) -> bytes:
        if reference not in self.items:
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD, ProtectedSecretFailure.NOT_FOUND
            )
        return self.items[reference]

    def delete(self, reference: str) -> None:
        self.items.pop(reference, None)


@pytest.fixture
def key() -> bytes:
    return bytes(range(32))


def test_create_and_unlock_with_key_and_password(tmp_path: Path, key: bytes) -> None:
    recovery_key = history_recovery.create(tmp_path, key, password=PASSWORD)
    assert history_recovery.has_recovery(tmp_path)
    assert history_recovery.has_password(tmp_path)
    assert history_recovery.unlock(tmp_path, recovery_key=recovery_key) == key
    assert history_recovery.unlock(tmp_path, recovery_key=recovery_key.lower().replace("-", "")) == key
    assert history_recovery.unlock(tmp_path, password=PASSWORD) == key
    assert history_recovery.unlock(tmp_path, password="wrong password here") is None
    assert history_recovery.unlock(tmp_path, recovery_key="AAAA-AAAA-AAAA-AAAA-AAAA") is None


def test_envelope_never_contains_secrets(tmp_path: Path, key: bytes) -> None:
    recovery_key = history_recovery.create(tmp_path, key, password=PASSWORD)
    raw = history_recovery.envelope_path(tmp_path).read_bytes()
    assert recovery_key.encode() not in raw
    assert PASSWORD.encode() not in raw
    assert key not in raw


def test_password_is_optional_and_removable(tmp_path: Path, key: bytes) -> None:
    history_recovery.create(tmp_path, key)
    assert not history_recovery.has_password(tmp_path)
    assert history_recovery.set_password(tmp_path, key, PASSWORD)
    assert history_recovery.unlock(tmp_path, password=PASSWORD) == key
    assert history_recovery.set_password(tmp_path, key, None)
    assert history_recovery.unlock(tmp_path, password=PASSWORD) is None


def test_regenerate_switches_keys_only_on_confirm(tmp_path: Path, key: bytes) -> None:
    old = history_recovery.create(tmp_path, key, password=PASSWORD)
    new = history_recovery.regenerate(tmp_path, key)
    assert new and new != old
    # Until the user confirms saving it, both keys work and setup stays complete.
    assert history_recovery.unlock(tmp_path, recovery_key=old) == key
    assert history_recovery.unlock(tmp_path, recovery_key=new) == key
    assert history_recovery.has_recovery(tmp_path)
    assert not history_recovery.confirm(tmp_path, key, generation="stale")
    generation = history_recovery.pending_generation(tmp_path)
    assert history_recovery.confirm(tmp_path, key, generation=generation)
    assert history_recovery.unlock(tmp_path, recovery_key=old) is None
    assert history_recovery.unlock(tmp_path, recovery_key=new) == key
    assert history_recovery.unlock(tmp_path, password=PASSWORD) == key


def test_restore_after_keychain_loss(tmp_path: Path) -> None:
    store = _Store()
    key_path = tmp_path / "transcript.key"
    key = load_or_create_data_key(key_path, store=store)
    assert key is not None
    recovery_key = history_recovery.create(tmp_path, key)
    store.items.clear()  # the Keychain item disappears
    assert load_data_key(key_path, store=store)[0] is None
    restored = history_recovery.unlock(tmp_path, recovery_key=recovery_key)
    assert restored == key
    from whisperstream.transcript_crypto import save_data_key

    save_data_key(key_path, restored, store=store)
    assert load_data_key(key_path, store=store) == (key, None)


def test_one_envelope_across_lockdown_enable_and_disable(tmp_path: Path) -> None:
    store = _Store()
    key = load_or_create_data_key(tmp_path / "transcript.key", store=store)
    history_key = history_recovery.create(tmp_path, key, password=PASSWORD)

    lockdown_key = lockdown_keystore.enable_lockdown(tmp_path, "lockdown password 1", store=store)
    assert lockdown_keystore.is_lockdown_enabled(tmp_path)
    assert not history_recovery.envelope_path(tmp_path).exists()
    # Nothing in standard mode opens history while Lockdown is on.
    assert history_recovery.unlock(tmp_path, recovery_key=history_key) is None
    with pytest.raises(RuntimeError):
        history_recovery.create(tmp_path, key)

    assert lockdown_keystore.disable_lockdown(tmp_path, "lockdown password 1", store=store)
    assert not lockdown_keystore.is_lockdown_enabled(tmp_path)
    # The Lockdown envelope becomes the standard recovery envelope.
    assert history_recovery.unlock(tmp_path, recovery_key=lockdown_key) == key
    assert history_recovery.unlock(tmp_path, password="lockdown password 1") == key
    assert history_recovery.unlock(tmp_path, recovery_key=history_key) is None


def test_lockdown_reset_removes_every_envelope(tmp_path: Path) -> None:
    store = _Store()
    key = load_or_create_data_key(tmp_path / "transcript.key", store=store)
    lockdown_keystore.enable_lockdown(tmp_path, "lockdown password 1", store=store)
    history_recovery.envelope_path(tmp_path).write_text("{}", encoding="utf-8")
    lockdown_keystore.reset_lockdown(tmp_path, store=store)
    assert not lockdown_keystore.is_lockdown_enabled(tmp_path)
    assert not history_recovery.envelope_path(tmp_path).exists()
    del key


def test_malformed_envelope_is_absent(tmp_path: Path) -> None:
    history_recovery.envelope_path(tmp_path).write_text("not json", encoding="utf-8")
    assert not history_recovery.has_recovery(tmp_path)
    assert history_recovery.unlock(tmp_path, recovery_key="AAAA") is None


def test_pending_setup_counts_only_after_confirm(tmp_path: Path, key: bytes) -> None:
    recovery_key = history_recovery.create(tmp_path, key, confirmed=False)
    assert not history_recovery.has_recovery(tmp_path)
    # The shown key already works, so a crash before confirm loses nothing.
    assert history_recovery.unlock(tmp_path, recovery_key=recovery_key) == key
    generation = history_recovery.pending_generation(tmp_path)
    assert history_recovery.confirm(tmp_path, key, password=PASSWORD, generation=generation)
    assert history_recovery.has_recovery(tmp_path)
    assert history_recovery.unlock(tmp_path, password=PASSWORD) == key


def test_lockdown_enable_rolls_back_when_standard_envelope_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    key = load_or_create_data_key(tmp_path / "transcript.key", store=store)
    history_key = history_recovery.create(tmp_path, key)

    def fail(*_a, **_k):
        raise OSError("busy")

    monkeypatch.setattr(history_recovery, "delete", fail)
    with pytest.raises(RuntimeError):
        lockdown_keystore.enable_lockdown(tmp_path, "lockdown password 1", store=store)
    assert not lockdown_keystore.is_lockdown_enabled(tmp_path)
    assert history_recovery.unlock(tmp_path, recovery_key=history_key) == key
    assert load_data_key(tmp_path / "transcript.key", store=store) == (key, None)


def test_lockdown_enable_rolls_back_when_protected_key_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    key = load_or_create_data_key(tmp_path / "transcript.key", store=store)
    history_key = history_recovery.create(tmp_path, key)

    def fail(*_a, **_k):
        raise RuntimeError("provider failure")

    monkeypatch.setattr(lockdown_keystore, "_delete_data_key", fail)
    with pytest.raises(RuntimeError):
        lockdown_keystore.enable_lockdown(tmp_path, "lockdown password 1", store=store)
    assert not lockdown_keystore.is_lockdown_enabled(tmp_path)
    assert history_recovery.unlock(tmp_path, recovery_key=history_key) == key


def test_startup_repair_discards_standard_envelope_under_lockdown(tmp_path: Path, key: bytes) -> None:
    history_recovery.create(tmp_path, key)
    lockdown_file = tmp_path / "lockdown.key.json"
    # A damaged Lockdown file can't unwrap anything, so the standard envelope stays.
    lockdown_file.write_text("{}", encoding="utf-8")
    history_recovery.discard_if_lockdown(tmp_path)
    assert history_recovery.envelope_path(tmp_path).exists()
    lockdown_file.write_text('{"password": {}, "recovery": {}}', encoding="utf-8")
    history_recovery.discard_if_lockdown(tmp_path)
    assert history_recovery.envelope_path(tmp_path).exists()
    from whisperstream.lockdown_crypto import wrap_key, wrap_key_with_recovery_code

    lockdown_file.write_text(json.dumps({
        "password": wrap_key(key, "a long lockdown passphrase"),
        "recovery": wrap_key_with_recovery_code(key, "AAAA-BBBB-CCCC-DDDD-EEEE"),
    }), encoding="utf-8")
    history_recovery.discard_if_lockdown(tmp_path)
    assert not history_recovery.envelope_path(tmp_path).exists()


def test_key_check_detects_a_stale_envelope(tmp_path: Path, key: bytes) -> None:
    history_recovery.create(tmp_path, key)
    assert history_recovery.matches_key(tmp_path, key)
    assert not history_recovery.matches_key(tmp_path, bytes(32))
    raw = history_recovery.envelope_path(tmp_path).read_bytes()
    assert key not in raw
