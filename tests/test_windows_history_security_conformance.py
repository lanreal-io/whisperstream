"""Native Windows conformance for history/security/file schema v1."""

from __future__ import annotations

import asyncio
import ast
import platform
import threading
import time
from pathlib import Path

import pytest

from whisperstream.history_security import (
    PlatformHistorySecurityFactory,
    UnavailableHistoryStoreAdapter,
)
from whisperstream.history_security_contract import (
    FileWorkflowKind,
    HistoryFailure,
    HistoryRecordRef,
    HistoryStoreAdapter,
    LicenseState,
    SecurityAvailability,
)
from whisperstream.lockdown_keystore import (
    disable_lockdown,
    enable_lockdown,
    unlock_with_password,
    unlock_with_recovery_code,
)
from whisperstream.protected_secret_contract import (
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
)
from whisperstream.transcript_crypto import load_or_create_data_key
from whisperstream.transcript_store import TranscriptStore
from whisperstream.windows_history_security import (
    WindowsFileWorkflowAdapter,
    WindowsHistoryStoreAdapter,
    WindowsLicenseStateAdapter,
)
from whisperstream.windows_protected_secret import WindowsDpapiProtectedSecretStore


pytestmark = pytest.mark.skipif(
    platform.system() != "Windows"
    or platform.machine().lower() not in {"amd64", "x86_64"},
    reason="native Windows AMD64 conformance only",
)


class _UnavailableProtectedStore:
    def availability(self) -> ProtectionAvailability:
        return ProtectionAvailability.UNAVAILABLE

    @staticmethod
    def _raise(operation: ProtectedSecretOperation):
        raise ProtectedSecretError(operation, ProtectedSecretFailure.UNAVAILABLE)

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        del secret, purpose
        self._raise(ProtectedSecretOperation.STORE)

    def load(self, reference: str) -> bytes:
        del reference
        self._raise(ProtectedSecretOperation.LOAD)

    def delete(self, reference: str) -> None:
        del reference
        self._raise(ProtectedSecretOperation.DELETE)


def _adapter(
    store: TranscriptStore | None,
    protected_store,
    *,
    enabled: bool = False,
    locked: bool = False,
) -> WindowsHistoryStoreAdapter:
    return WindowsHistoryStoreAdapter(
        store_provider=lambda: store,
        protected_store_provider=lambda: protected_store,
        lockdown_enabled=lambda: enabled,
        lockdown_locked=lambda: locked,
    )


def test_factory_is_lazy_native_windows_only_and_parents_are_native_free() -> None:
    factory = PlatformHistorySecurityFactory(
        history_store=lambda: None,
        license_manager=lambda: None,
        protected_store=lambda: _UnavailableProtectedStore(),
        lockdown_enabled=lambda: False,
        lockdown_locked=lambda: False,
        system="Windows",
        machine="AMD64",
    )
    assert isinstance(factory.history(), WindowsHistoryStoreAdapter)
    assert isinstance(factory.history(), HistoryStoreAdapter)
    assert isinstance(factory.files(), WindowsFileWorkflowAdapter)
    assert isinstance(factory.licensing(), WindowsLicenseStateAdapter)

    darwin = PlatformHistorySecurityFactory(
        history_store=lambda: None,
        license_manager=lambda: None,
        protected_store=lambda: None,
        lockdown_enabled=lambda: False,
        lockdown_locked=lambda: False,
        system="Darwin",
        machine="arm64",
    )
    assert darwin.history().__class__.__name__ == "MacosHistoryStoreAdapter"
    assert isinstance(darwin.history(), HistoryStoreAdapter)

    root = Path(__file__).resolve().parents[1]
    for relative in (
        "src/whisperstream/history_security_contract.py",
        "src/whisperstream/history_security.py",
    ):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        imports = {
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not imports & {"AppKit", "Security", "ctypes", "winreg"}


def test_encrypted_restart_missing_corrupt_and_unavailable_key_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "history"
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(data_dir))
    protected = WindowsDpapiProtectedSecretStore()
    marker = b"unit05-synthetic-record-marker"

    store = TranscriptStore(protected_secret_store=protected)
    record_id = store.save(
        raw_text=marker.decode("ascii"),
        enhanced_text=None,
        duration_ms=10,
        model="synthetic",
        source="dictation",
        audio_bytes=marker + b"-audio",
        peaks=[0.1, 0.2],
    )
    assert record_id
    reference = (data_dir / "transcript.key").read_text(encoding="utf-8")
    assert reference.startswith("enc:")
    store.close()

    persisted = b"".join(
        path.read_bytes() for path in data_dir.rglob("*") if path.is_file()
    )
    assert marker not in persisted

    restarted = TranscriptStore(protected_secret_store=protected)
    assert restarted.get(record_id)["raw_text"] == marker.decode("ascii")
    restarted.close()

    (data_dir / "transcript.key").unlink()
    missing = TranscriptStore(protected_secret_store=protected)
    assert missing.decryptable is False
    assert missing.list() == {
        "items": [],
        "next_cursor": None,
        "decryptable": False,
    }
    assert not (data_dir / "transcript.key").exists()
    missing.close()

    (data_dir / "transcript.key").write_text("enc:not-valid", encoding="utf-8")
    corrupt = TranscriptStore(protected_secret_store=protected)
    assert corrupt.decryptable is False
    corrupt.close()

    empty_dir = tmp_path / "empty-unavailable"
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(empty_dir))
    unavailable = TranscriptStore(
        protected_secret_store=_UnavailableProtectedStore()
    )
    assert unavailable.decryptable is False
    unavailable.close()
    recovered_empty = TranscriptStore(protected_secret_store=protected)
    assert recovered_empty.decryptable is True
    recovered_empty.close()


def test_dpapi_first_use_concurrency_converges_on_one_reference(
    tmp_path: Path,
) -> None:
    protected = WindowsDpapiProtectedSecretStore()
    key_path = tmp_path / "concurrent" / "transcript.key"
    keys: list[bytes | None] = []
    threads = [
        threading.Thread(
            target=lambda: keys.append(
                load_or_create_data_key(key_path, store=protected)
            )
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert len(keys) == 8 and keys[0] is not None
    assert all(key == keys[0] for key in keys)
    assert key_path.read_text(encoding="utf-8").startswith("enc:")


def test_history_mutations_retention_forever_and_exact_audio_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "mutations"
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(data_dir))
    protected = WindowsDpapiProtectedSecretStore()
    store = TranscriptStore(protected_secret_store=protected)
    now = int(time.time() * 1000)
    old_id = store.save(
        raw_text="synthetic-old",
        enhanced_text=None,
        duration_ms=1,
        model="synthetic",
        source="dictation",
        audio_bytes=b"old-audio",
        created_at=now - 3 * 86_400_000,
    )
    recent_id = store.save(
        raw_text="synthetic-recent",
        enhanced_text=None,
        duration_ms=1,
        model="synthetic",
        source="dictation",
        audio_bytes=b"recent-audio",
        created_at=now,
    )
    assert old_id and recent_id
    adapter = _adapter(store, protected)
    purged = adapter.enforce_retention_with_count(1)
    assert purged.count == 1 and purged.result.changed
    assert store.get(old_id) is None and store.get(recent_id) is not None

    orphan = data_dir / "recordings" / "orphan.wav"
    orphan.write_bytes(b"synthetic-orphan")
    forever = adapter.enforce_retention_with_count(0)
    assert forever.count == 0 and not forever.result.changed
    assert store.get(recent_id) is not None and not orphan.exists()

    deleted = adapter.delete_with_count(HistoryRecordRef(recent_id))
    assert deleted.count == 1 and deleted.result.record == HistoryRecordRef(recent_id)
    assert not (data_dir / store._audio_path_for(recent_id)).exists()
    missing = adapter.delete_with_count(HistoryRecordRef(recent_id))
    assert missing.result.failure is HistoryFailure.NOT_FOUND

    for value in ("one", "two"):
        assert store.save(
            raw_text=f"synthetic-{value}",
            enhanced_text=None,
            duration_ms=1,
            model="synthetic",
            source="dictation",
        )
    cleared = adapter.clear_with_count()
    assert cleared.count == 2 and store.list()["items"] == []
    store.close()


def test_security_snapshot_distinguishes_locked_not_decryptable_and_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path / "snapshot"))
    protected = WindowsDpapiProtectedSecretStore()
    store = TranscriptStore(protected_secret_store=protected)
    ready = _adapter(store, protected)
    assert ready.availability() is SecurityAvailability.READY
    assert ready.security_snapshot().network_enhancement_allowed is True

    locked = _adapter(store, protected, enabled=True, locked=True)
    locked_snapshot = locked.security_snapshot()
    assert locked_snapshot.history is SecurityAvailability.LOCKED
    assert locked_snapshot.diagnostic_export_allowed is False

    store.clear_data_key()
    unavailable_history = _adapter(store, protected)
    assert unavailable_history.availability() is SecurityAvailability.NOT_DECRYPTABLE
    assert (
        _adapter(None, _UnavailableProtectedStore()).availability()
        is SecurityAvailability.UNAVAILABLE
    )
    store.close()


def test_lockdown_password_recovery_disable_restart_preserves_ciphertext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "lockdown"
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(data_dir))
    protected = WindowsDpapiProtectedSecretStore()
    store = TranscriptStore(protected_secret_store=protected)
    record_id = store.save(
        raw_text="synthetic-lockdown",
        enhanced_text=None,
        duration_ms=1,
        model="synthetic",
        source="dictation",
    )
    assert record_id

    recovery = enable_lockdown(
        data_dir, "unit05-password", store=protected
    )
    store.clear_data_key()
    assert not (data_dir / "transcript.key").exists()
    assert unlock_with_password(data_dir, "wrong") is None
    recovered = unlock_with_recovery_code(data_dir, recovery)
    assert recovered is not None and store.set_data_key(recovered)
    assert store.get(record_id)["raw_text"] == "synthetic-lockdown"
    assert disable_lockdown(
        data_dir, "unit05-password", store=protected
    )
    assert store.reload_data_key()
    store.close()

    restarted = TranscriptStore(protected_secret_store=protected)
    assert restarted.get(record_id)["raw_text"] == "synthetic-lockdown"
    restarted.clear()
    restarted.close()


def test_file_workflows_hide_paths_bound_batches_and_close_handles(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic-source.wav"
    destination = tmp_path / "synthetic-export.wav"
    source.write_bytes(b"reviewed-public-fixture-bytes")
    before = source.read_bytes()
    adapter = WindowsFileWorkflowAdapter()

    invalid = adapter.register_paths(
        FileWorkflowKind.IMPORT_AUDIO,
        [tmp_path / f"source-{index}.wav" for index in range(101)],
    )
    assert invalid.failure is HistoryFailure.INVALID_INPUT
    batch = adapter.register_paths(FileWorkflowKind.IMPORT_AUDIO, [source])
    selection = batch.selections[0]
    assert str(tmp_path) not in repr(selection)
    assert selection.selection_token not in repr(selection)
    result = asyncio.run(adapter.read(selection))
    assert result.payload == before and str(tmp_path) not in repr(result)

    output = adapter.register_paths(
        FileWorkflowKind.EXPORT_TRANSCRIPT, [destination]
    ).selections[0]
    written = asyncio.run(adapter.write(output, b"synthetic-export"))
    assert written.failure is HistoryFailure.NONE
    assert destination.read_bytes() == b"synthetic-export"
    assert source.read_bytes() == before

    assert adapter.release(selection)
    adapter.release(output)
    assert adapter.active_selection_count == 0
    source.rename(tmp_path / "source-renamed.wav")
    destination.rename(tmp_path / "export-renamed.wav")
    adapter.close()
    assert adapter.active_selection_count == 0


def test_file_picker_cancellation_and_error_are_explicit(tmp_path: Path) -> None:
    cancelled = WindowsFileWorkflowAdapter(chooser=lambda _kind: None)
    result = asyncio.run(cancelled.choose(FileWorkflowKind.RETAIN_EULA))
    assert result.failure is HistoryFailure.CANCELLED
    assert cancelled.active_selection_count == 0

    def fail(_kind):
        raise OSError("synthetic dialog failure")

    failed = WindowsFileWorkflowAdapter(chooser=fail)
    result = asyncio.run(failed.choose(FileWorkflowKind.EXPORT_LOGS))
    assert result.failure is HistoryFailure.IO
    assert str(tmp_path) not in repr(result)


@pytest.mark.parametrize(
    "status",
    [
        {"is_pro": True, "trial_remaining_seconds": 0},
        {"is_pro": False, "trial_remaining_seconds": 60},
        {"is_pro": False, "trial_remaining_seconds": 0},
    ],
)
def test_license_snapshot_reports_unrestricted_entitlement(status: dict) -> None:
    class Manager:
        def status(self):
            return dict(status)

    for provider in (lambda: Manager(), lambda: None):
        snapshot = WindowsLicenseStateAdapter(manager_provider=provider).snapshot()
        assert snapshot.state is LicenseState.PRO
        assert snapshot.cache_integrity_protected is False
        assert snapshot.cache_confidential is False
        assert snapshot.activation_available is False
        assert snapshot.offline_validation_available is False
        assert snapshot.deactivation_available is False


def test_license_adapter_preserves_manager_activation_and_deactivation_outcomes() -> None:
    class Manager:
        async def activate(self, key: str):
            assert key == "WS-SYNTHETIC"
            return {"success": True, "activated": True}

        async def deactivate(self):
            return {"success": True, "warning": "synthetic warning"}

        def status(self):
            return {"is_pro": True, "trial_remaining_seconds": 0}

    adapter = WindowsLicenseStateAdapter(manager_provider=Manager)
    assert adapter.status() == {"is_pro": True, "trial_remaining_seconds": 0}
    assert asyncio.run(adapter.activate("WS-SYNTHETIC")) == {
        "success": True,
        "activated": True,
    }
    assert asyncio.run(adapter.deactivate()) == {
        "success": True,
        "warning": "synthetic warning",
    }
