"""Native Apple Silicon conformance for D-047 history and file workflows."""

from __future__ import annotations

import asyncio
import json
import platform
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from whisperstream.eula import AcceptanceGrant, AcceptanceGrantReason
from whisperstream.history_security import PlatformHistorySecurityFactory
from whisperstream.history_security_contract import (
    FileWorkflowKind,
    HistoryFailure,
    HistoryRecordRef,
    HistoryStoreAdapter,
    LicenseState,
    SecurityAvailability,
)
from whisperstream.macos_history_security import (
    MacosFileWorkflowAdapter,
    MacosHistoryStoreAdapter,
    MacosLicenseStateAdapter,
    MacosNativeSelectionRegistry,
)
from whisperstream.macos_model_lifecycle import MacosModelStorage
from whisperstream.protected_secret_contract import (
    MACOS_KEYCHAIN_PREFIX,
    ProtectionAvailability,
    ProtectedSecretFailure,
    ProtectedSecretError,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
)
from whisperstream.transcript_crypto import load_or_create_data_key
from whisperstream.transcript_store import TranscriptIntegrityError, TranscriptStore


pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine().lower() not in {"arm64", "aarch64"},
    reason="native Apple Silicon conformance only",
)


class _MemoryKeychain:
    """UUID-scoped, synthetic-only Keychain substitute for automated tests."""

    def __init__(self, namespace: str | None = None) -> None:
        self.namespace = namespace or str(uuid.uuid4())
        self.items: dict[str, bytes] = {}
        self.lock = threading.Lock()

    def availability(self) -> ProtectionAvailability:
        return ProtectionAvailability.AVAILABLE

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        del purpose
        reference = f"{MACOS_KEYCHAIN_PREFIX}{uuid.uuid4()}"
        with self.lock:
            self.items[reference] = bytes(secret)
        return reference

    def load(self, reference: str) -> bytes:
        with self.lock:
            value = self.items.get(reference)
        if value is None:
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD, ProtectedSecretFailure.NOT_FOUND
            )
        return value

    def delete(self, reference: str) -> None:
        with self.lock:
            self.items.pop(reference, None)


class _UnavailableKeychain(_MemoryKeychain):
    def availability(self) -> ProtectionAvailability:
        return ProtectionAvailability.UNAVAILABLE

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        del secret, purpose
        raise ProtectedSecretError(
            ProtectedSecretOperation.STORE, ProtectedSecretFailure.UNAVAILABLE
        )


def _adapter(
    store: TranscriptStore | None,
    protected: object,
    *,
    enabled: bool = False,
    locked: bool = False,
) -> MacosHistoryStoreAdapter:
    return MacosHistoryStoreAdapter(
        store_provider=lambda: store,
        protected_store_provider=lambda: protected,
        lockdown_enabled=lambda: enabled,
        lockdown_locked=lambda: locked,
    )


def _write_registry(path: Path, entries: dict[str, tuple[str, Path]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selections": {
                    token: {
                        "kind": kind,
                        "path": str(source),
                        "display_name": source.name,
                    }
                    for token, (kind, source) in entries.items()
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


_ACCEPTED = AcceptanceGrant(
    True,
    AcceptanceGrantReason.ACCEPTED,
    "1.3",
    "a" * 64,
    "b" * 64,
)


def _history_server(
    store: TranscriptStore | None,
    protected: object,
):
    from whisperstream.server import WhisperstreamServer

    config = MagicMock()
    config.lockdown_mode = False
    config.claude_hud_enabled = False
    config.agent_speech_enabled = False
    config.enhancement_enabled = False
    config.history_enabled = True
    config.save_audio = True
    config.parakeet_model = "parakeet-tdt-0.6b-v3"
    config.effective_provider.return_value = "none"
    app = MagicMock()
    app.config = config
    app.transcript_store = store
    app.license_manager.status.return_value = {"is_pro": True}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app._current_acceptance_grant.return_value = _ACCEPTED
    app._acceptance_is_current.side_effect = lambda grant: grant == _ACCEPTED
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    server._history_store_adapter = _adapter(store, protected)
    server.broadcast = MagicMock()
    return server


def test_factory_selects_mac_owners_and_frozen_policy() -> None:
    factory = PlatformHistorySecurityFactory(
        history_store=lambda: None,
        license_manager=lambda: None,
        protected_store=lambda: None,
        lockdown_enabled=lambda: False,
        lockdown_locked=lambda: False,
        system="Darwin",
        machine="arm64",
    )
    assert isinstance(factory.history(), MacosHistoryStoreAdapter)
    assert isinstance(factory.history(), HistoryStoreAdapter)
    assert isinstance(factory.files(), MacosFileWorkflowAdapter)
    assert isinstance(factory.licensing(), MacosLicenseStateAdapter)
    policy = factory.history().policy
    assert policy.cipher == "AES-256-GCM"
    assert policy.zero_retention_means_forever
    assert not policy.ordinary_delete_is_secure_erasure
    assert policy.max_import_files == 100


def test_encrypted_restart_and_distinct_failure_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "history"
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(data_dir))
    protected = _MemoryKeychain()
    marker = b"synthetic-mac-history-marker"
    store = TranscriptStore(protected_secret_store=protected)
    assert _adapter(store, protected).availability() is SecurityAvailability.EMPTY
    record_id = store.save(
        raw_text=marker.decode(), enhanced_text=None, duration_ms=4,
        model="synthetic", source="dictation", audio_bytes=marker + b"-audio",
    )
    assert record_id
    reference = (data_dir / "transcript.key").read_text(encoding="utf-8")
    assert reference.startswith(MACOS_KEYCHAIN_PREFIX)
    store.close()
    assert marker not in b"".join(
        path.read_bytes() for path in data_dir.rglob("*") if path.is_file()
    )

    restarted = TranscriptStore(protected_secret_store=protected)
    assert _adapter(restarted, protected).availability() is SecurityAvailability.READY
    restarted.close()
    with closing(sqlite3.connect(data_dir / "transcripts.db")) as connection:
        connection.execute(
            "UPDATE recordings SET raw_text = ? WHERE id = ?", (b"corrupt", record_id)
        )
        connection.commit()
    corrupt = TranscriptStore(protected_secret_store=protected)
    assert _adapter(corrupt, protected).availability() is SecurityAvailability.NOT_DECRYPTABLE
    corrupt.close()

    protected.delete(reference)
    missing = TranscriptStore(protected_secret_store=protected)
    assert _adapter(missing, protected).availability() is SecurityAvailability.KEY_MISSING
    missing.close()

    empty_dir = tmp_path / "unavailable"
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(empty_dir))
    unavailable = TranscriptStore(protected_secret_store=_UnavailableKeychain())
    assert _adapter(unavailable, _UnavailableKeychain()).availability() is SecurityAvailability.UNAVAILABLE
    unavailable.close()


def test_history_integrity_cache_advances_across_trusted_mutations_and_pins_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path / "bounded"))
    protected = _MemoryKeychain()
    store = TranscriptStore(protected_secret_store=protected)
    record_ids = []
    for index in range(3):
        record_id = store.save(
            raw_text=f"public fixture {index}",
            enhanced_text=None,
            duration_ms=1,
            model="synthetic",
            source="dictation",
            audio_bytes=f"public audio {index}".encode(),
            created_at=index + 1,
        )
        assert record_id
        record_ids.append(record_id)

    read_bytes = Path.read_bytes
    audio_reads = 0

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal audio_reads
        if path.suffix == ".wav":
            audio_reads += 1
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    adapter = _adapter(store, protected)
    assert adapter.availability() is SecurityAvailability.READY
    first_scan_reads = audio_reads
    assert first_scan_reads == 3
    assert adapter.availability() is SecurityAvailability.READY
    assert audio_reads == first_scan_reads

    for index in range(2):
        assert store.save(
            raw_text=f"trusted new fixture {index}",
            enhanced_text=None,
            duration_ms=1,
            model="synthetic",
            source="dictation",
            audio_bytes=f"trusted new audio {index}".encode(),
            created_at=index + 10,
        )
        assert adapter.availability() is SecurityAvailability.READY
        assert audio_reads == first_scan_reads
    assert store.delete(record_ids[0]) is True
    assert adapter.availability() is SecurityAvailability.READY
    assert audio_reads == first_scan_reads

    with store._lock:
        store._conn.execute(
            "UPDATE recordings SET raw_text = ? WHERE id = ?",
            (b"corrupt", record_ids[1]),
        )
        store._conn.commit()
    with pytest.raises(TranscriptIntegrityError):
        store.get(record_ids[1])
    assert adapter.availability() is SecurityAvailability.NOT_DECRYPTABLE
    assert audio_reads == first_scan_reads

    # A trusted mutation must never turn a pinned authentication failure true.
    assert store.save(
        raw_text="trusted after failure",
        enhanced_text=None,
        duration_ms=1,
        model="synthetic",
        source="dictation",
    )
    assert adapter.availability() is SecurityAvailability.NOT_DECRYPTABLE
    assert audio_reads == first_scan_reads
    store.close()


def test_history_routes_fail_closed_for_unavailable_store_without_success_broadcasts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path / "unavailable-routes"))
    protected = _UnavailableKeychain()
    server = _history_server(None, protected)

    with TestClient(server.fastapi) as client:
        listed = client.get("/api/transcripts")
        responses = [
            client.get("/api/transcripts/missing"),
            client.get("/api/transcripts/missing/audio"),
            client.delete("/api/transcripts/missing"),
            client.post("/api/transcripts/clear"),
            client.post("/api/transcripts/purge", json={"retention_days": 7}),
        ]

    assert listed.status_code == 200
    assert listed.json()["availability"] == "unavailable"
    for response in responses:
        assert response.status_code == 503
        assert response.json() == {
            "error": "history_unavailable",
            "availability": "unavailable",
        }
    server.broadcast.assert_not_called()


def test_history_routes_distinguish_corrupt_ciphertext_from_missing_or_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path / "corrupt-routes"))
    protected = _MemoryKeychain()
    store = TranscriptStore(protected_secret_store=protected)
    record_id = store.save(
        raw_text="public corrupt fixture",
        enhanced_text=None,
        duration_ms=1,
        model="synthetic",
        source="dictation",
        audio_bytes=b"public audio fixture",
    )
    assert record_id
    with store._lock:
        store._conn.execute(
            "UPDATE recordings SET raw_text = ? WHERE id = ?",
            (b"corrupt", record_id),
        )
        store._conn.commit()
        store._invalidate_integrity_cache_locked()
    server = _history_server(store, protected)

    with TestClient(server.fastapi) as client:
        listed = client.get("/api/transcripts")
        responses = [
            client.get(f"/api/transcripts/{record_id}"),
            client.get(f"/api/transcripts/{record_id}/audio"),
            client.delete(f"/api/transcripts/{record_id}"),
            client.post("/api/transcripts/clear"),
            client.post("/api/transcripts/purge", json={"retention_days": 7}),
        ]

    assert listed.status_code == 200
    assert listed.json()["availability"] == "not-decryptable"
    for response in responses:
        assert response.status_code == 409
        assert response.json() == {
            "error": "history_unavailable",
            "availability": "not-decryptable",
        }
    assert store._conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 1
    server.broadcast.assert_not_called()
    store.close()


def test_requested_page_corruption_becomes_typed_after_cached_ready_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path / "page-corrupt"))
    protected = _MemoryKeychain()
    store = TranscriptStore(protected_secret_store=protected)
    older = store.save(
        raw_text="older public fixture",
        enhanced_text=None,
        duration_ms=1,
        model="synthetic",
        source="dictation",
        created_at=1,
    )
    newer = store.save(
        raw_text="newer public fixture",
        enhanced_text=None,
        duration_ms=1,
        model="synthetic",
        source="dictation",
        created_at=2,
    )
    assert older and newer
    adapter = _adapter(store, protected)
    assert adapter.availability() is SecurityAvailability.READY
    with store._lock:
        store._conn.execute(
            "UPDATE recordings SET raw_text = ? WHERE id = ?", (b"corrupt", older)
        )
        store._conn.commit()
    server = _history_server(store, protected)
    server._history_store_adapter = adapter

    with TestClient(server.fastapi) as client:
        first = client.get("/api/transcripts", params={"limit": 1})
        second = client.get(
            "/api/transcripts",
            params={"limit": 1, "before": first.json()["next_cursor"]},
        )

    assert [item["id"] for item in first.json()["items"]] == [newer]
    assert second.json() == {
        "items": [],
        "next_cursor": None,
        "decryptable": False,
        "availability": "not-decryptable",
    }
    store.close()


def test_first_use_concurrency_converges_on_one_keychain_reference(tmp_path: Path) -> None:
    protected = _MemoryKeychain()
    key_path = tmp_path / "concurrent" / "transcript.key"
    keys: list[bytes | None] = []
    threads = [
        threading.Thread(target=lambda: keys.append(load_or_create_data_key(key_path, store=protected)))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(keys) == 8 and keys[0] is not None
    assert all(key == keys[0] for key in keys)
    reference = key_path.read_text(encoding="utf-8")
    assert reference.startswith(MACOS_KEYCHAIN_PREFIX)
    protected.delete(reference)
    assert not protected.items


def test_retention_delete_clear_and_lockdown_denials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path / "mutations"))
    protected = _MemoryKeychain()
    store = TranscriptStore(protected_secret_store=protected)
    now = int(time.time() * 1000)
    old = store.save(raw_text="old", enhanced_text=None, duration_ms=1, model="x", source="dictation", created_at=now - 3 * 86_400_000)
    recent = store.save(raw_text="recent", enhanced_text=None, duration_ms=1, model="x", source="dictation", created_at=now)
    assert old and recent
    adapter = _adapter(store, protected)
    assert adapter.enforce_retention_with_count(0).count == 0
    assert adapter.enforce_retention_with_count(1).count == 1
    assert adapter.delete_with_count(HistoryRecordRef(recent)).count == 1
    assert adapter.delete(HistoryRecordRef(recent)).failure is HistoryFailure.NOT_FOUND
    locked = _adapter(store, protected, enabled=True, locked=True)
    snapshot = locked.security_snapshot()
    assert snapshot.history is SecurityAvailability.LOCKED
    assert not snapshot.network_enhancement_allowed
    assert not snapshot.diagnostic_export_allowed
    store.close()


def test_native_file_tokens_are_one_shot_bounded_and_release_paths(tmp_path: Path) -> None:
    source = tmp_path / "reviewed.wav"
    source.write_bytes(b"reviewed-public-fixture")
    registry_path = tmp_path / "macos-native-selections-v1.json"
    token = f"macos-native:{'1' * 48}"
    _write_registry(registry_path, {token: ("import-audio", source)})
    adapter = MacosFileWorkflowAdapter(
        registry=MacosNativeSelectionRegistry(registry_path)
    )
    batch = adapter.register_tokens(FileWorkflowKind.IMPORT_AUDIO, [token])
    assert batch.failure is HistoryFailure.NONE
    selection = batch.selections[0]
    assert str(tmp_path) not in repr(selection)
    assert not registry_path.exists()
    result = asyncio.run(adapter.read(selection))
    assert result.payload == source.read_bytes()
    assert adapter.release(selection)
    assert adapter.active_selection_count == 0
    assert adapter.register_tokens(
        FileWorkflowKind.IMPORT_AUDIO,
        [f"macos-native:{index:048x}" for index in range(101)],
    ).failure is HistoryFailure.INVALID_INPUT
    assert asyncio.run(adapter.choose(FileWorkflowKind.IMPORT_AUDIO)).failure is HistoryFailure.UNSUPPORTED
    source.rename(tmp_path / "released.wav")


def test_mixed_native_batch_failure_discards_all_paths_and_sessions(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    first.write_bytes(b"first-public-fixture")
    second.write_bytes(b"second-public-fixture")
    registry_path = tmp_path / "macos-native-selections-v1.json"
    valid = f"macos-native:{'3' * 48}"
    wrong_kind = f"macos-native:{'4' * 48}"
    _write_registry(
        registry_path,
        {
            valid: (FileWorkflowKind.IMPORT_AUDIO.value, first),
            wrong_kind: (FileWorkflowKind.SELECT_MODEL_STORAGE.value, second),
        },
    )
    adapter = MacosFileWorkflowAdapter(
        registry=MacosNativeSelectionRegistry(registry_path)
    )

    batch = adapter.register_tokens(
        FileWorkflowKind.IMPORT_AUDIO, [valid, wrong_kind]
    )

    assert batch.failure is HistoryFailure.INVALID_INPUT
    assert batch.selections == ()
    assert adapter.active_selection_count == 0
    assert not registry_path.exists()
    first.rename(tmp_path / "first-released.wav")
    second.rename(tmp_path / "second-released.wav")


def test_native_model_destination_token_is_consumed_and_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(data_dir))
    default_home = tmp_path / "default-home"
    destination = tmp_path / "selected-storage"
    default_home.mkdir()
    destination.mkdir()
    token = f"macos-native:{'2' * 48}"
    registry_path = data_dir / "macos-native-selections-v1.json"
    _write_registry(
        registry_path,
        {token: (FileWorkflowKind.SELECT_MODEL_STORAGE.value, destination)},
    )
    storage = MacosModelStorage(
        default_home=default_home,
        state_path=data_dir / "macos-model-storage.json",
    )
    assert storage.resolve_destination(token) == destination.resolve()
    assert not registry_path.exists()
    storage.release_destination(token)
    destination.rename(tmp_path / "released-storage")


@pytest.mark.parametrize(
    "status",
    [
        {"is_pro": True, "trial_remaining_seconds": 0},
        {"is_pro": False, "trial_remaining_seconds": 60},
        {"is_pro": False, "trial_remaining_seconds": 0},
    ],
)
def test_license_state_reports_unrestricted_entitlement(status: dict[str, object]) -> None:
    class Manager:
        def status(self):
            return status

    snapshot = MacosLicenseStateAdapter(manager_provider=Manager).snapshot()
    assert snapshot.state is LicenseState.PRO
    assert not snapshot.cache_integrity_protected
    assert not snapshot.cache_confidential
    assert not snapshot.activation_available
    assert not snapshot.offline_validation_available
    assert not snapshot.deactivation_available


def test_sources_keep_raw_macos_paths_out_of_shared_state() -> None:
    root = Path(__file__).resolve().parents[1]
    server_root = root / "src/whisperstream/server"
    server = (server_root / "app.py").read_text(encoding="utf-8")
    config_routes = (server_root / "routes/config.py").read_text(encoding="utf-8")
    config_payload = (server_root / "config_payload.py").read_text(encoding="utf-8")
    import_routes = (server_root / "routes/imports.py").read_text(encoding="utf-8")
    frontend = (root / "frontend/src/lib/useAudioImport.ts").read_text(encoding="utf-8")
    native = (root / "src-tauri/src/macos_file_workflows.rs").read_text(encoding="utf-8")
    # Every client config payload (GET and PUT responses, and the Lockdown
    # enable/disable payload) drops raw Mac storage paths.
    for redaction in ('payload.pop("data_dir", None)', 'payload["model_storage_dir"] = ""'):
        assert config_routes.count(redaction) == 2
        assert redaction in config_payload
    assert "/api/models/relocate/registered" in server
    assert "selection_tokens" in import_routes
    assert "chooseMacosImportAudio" in frontend
    assert "!active || privateMacFiles" in frontend
    assert "macos-native:" in native
    assert "cancelled_picker_result" in native
    assert "mode(0o600)" in native
