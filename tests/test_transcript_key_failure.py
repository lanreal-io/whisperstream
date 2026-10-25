"""Typed transcript data-key failures and the key-missing history state."""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import pytest

from whisperstream.history_security_contract import SecurityAvailability
from whisperstream.macos_history_security import MacosHistoryStoreAdapter
from whisperstream.protected_secret_contract import (
    MACOS_KEYCHAIN_PREFIX,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
    ProtectionAvailability,
)
from whisperstream.transcript_crypto import (
    DataKeyFailure,
    load_data_key,
    load_or_create_data_key,
)
from whisperstream.windows_history_security import WindowsHistoryStoreAdapter


class _Store:
    """Synthetic protected-secret store whose load failure is configurable."""

    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}
        self.load_failure: ProtectedSecretFailure | None = None

    def availability(self) -> ProtectionAvailability:
        return ProtectionAvailability.AVAILABLE

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        del purpose
        reference = f"{MACOS_KEYCHAIN_PREFIX}{uuid.uuid4()}"
        self.items[reference] = bytes(secret)
        return reference

    def load(self, reference: str) -> bytes:
        if self.load_failure is not None:
            raise ProtectedSecretError(ProtectedSecretOperation.LOAD, self.load_failure)
        value = self.items.get(reference)
        if value is None:
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD, ProtectedSecretFailure.NOT_FOUND
            )
        return value

    def delete(self, reference: str) -> None:
        self.items.pop(reference, None)


def test_load_data_key_never_creates(tmp_path: Path) -> None:
    store = _Store()
    key, failure = load_data_key(tmp_path / "transcript.key", store=store)
    assert key is None
    assert failure is DataKeyFailure.MISSING
    assert not (tmp_path / "transcript.key").exists()
    assert store.items == {}


def test_load_data_key_round_trips_existing_key(tmp_path: Path) -> None:
    store = _Store()
    created = load_or_create_data_key(tmp_path / "transcript.key", store=store)
    key, failure = load_data_key(tmp_path / "transcript.key", store=store)
    assert key == created
    assert failure is None


@pytest.mark.parametrize(
    ("provider_failure", "expected"),
    [
        (ProtectedSecretFailure.NOT_FOUND, DataKeyFailure.MISSING),
        (ProtectedSecretFailure.CORRUPT, DataKeyFailure.MISSING),
        (ProtectedSecretFailure.INVALID_REFERENCE, DataKeyFailure.MISSING),
        (ProtectedSecretFailure.ACCESS_DENIED, DataKeyFailure.ACCESS_DENIED),
        (ProtectedSecretFailure.UNAVAILABLE, DataKeyFailure.UNAVAILABLE),
        (ProtectedSecretFailure.BACKEND_FAILURE, DataKeyFailure.UNAVAILABLE),
    ],
)
def test_provider_failures_map_to_typed_failures(
    tmp_path: Path,
    provider_failure: ProtectedSecretFailure,
    expected: DataKeyFailure,
) -> None:
    store = _Store()
    path = tmp_path / "transcript.key"
    load_or_create_data_key(path, store=store)
    store.load_failure = provider_failure
    key, failure = load_data_key(path, store=store)
    assert key is None
    assert failure is expected
    # The load-or-create wrapper must never replace an existing reference.
    assert load_or_create_data_key(path, store=store) is None


def test_malformed_payload_is_missing(tmp_path: Path) -> None:
    store = _Store()
    path = tmp_path / "transcript.key"
    reference = store.store(base64.b64encode(b"short"), purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY)
    path.write_text(reference, encoding="utf-8")
    assert load_data_key(path, store=store) == (None, DataKeyFailure.MISSING)


class _FakeTranscriptStore:
    def __init__(self, failure: DataKeyFailure | None) -> None:
        self.decryptable = failure is None
        self.key_failure = failure

    def has_records(self) -> bool:
        return True


def _macos(store: object) -> MacosHistoryStoreAdapter:
    return MacosHistoryStoreAdapter(
        store_provider=lambda: store,
        protected_store_provider=lambda: _Store(),
        lockdown_enabled=lambda: False,
        lockdown_locked=lambda: False,
    )


def _windows(store: object) -> WindowsHistoryStoreAdapter:
    return WindowsHistoryStoreAdapter(
        store_provider=lambda: store,
        protected_store_provider=lambda: _Store(),
        lockdown_enabled=lambda: False,
        lockdown_locked=lambda: False,
    )


@pytest.mark.parametrize("adapter", [_macos, _windows])
def test_missing_key_reports_key_missing(adapter) -> None:
    store = _FakeTranscriptStore(DataKeyFailure.MISSING)
    assert adapter(store).availability() is SecurityAvailability.KEY_MISSING


def test_access_denied_stays_retryable_on_macos() -> None:
    store = _FakeTranscriptStore(DataKeyFailure.ACCESS_DENIED)
    assert _macos(store).availability() is SecurityAvailability.KEY_ERROR


def test_lockdown_enable_refuses_when_protected_key_is_gone(tmp_path: Path) -> None:
    from whisperstream import lockdown_keystore

    store = _Store()
    path = tmp_path / "transcript.key"
    load_or_create_data_key(path, store=store)
    reference = path.read_text(encoding="utf-8")
    store.delete(reference)
    with pytest.raises(RuntimeError):
        lockdown_keystore.enable_lockdown(tmp_path, "correct horse battery", store=store)
    assert path.read_text(encoding="utf-8") == reference
    assert store.items == {}


def test_lockdown_enable_refuses_keyless_history(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    from whisperstream import lockdown_keystore

    with closing(sqlite3.connect(tmp_path / "transcripts.db")) as connection, connection:
        connection.execute("CREATE TABLE recordings (id TEXT)")
        connection.execute("INSERT INTO recordings VALUES ('r1')")
    store = _Store()
    with pytest.raises(RuntimeError):
        lockdown_keystore.enable_lockdown(tmp_path, "correct horse battery", store=store)
    assert not (tmp_path / "transcript.key").exists()
