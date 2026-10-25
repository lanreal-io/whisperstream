#!/usr/bin/env python3
"""Content-free native Windows cleanup probe for parity-prep Unit 05."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import struct
import tempfile
import threading
import time
import uuid
from pathlib import Path

from whisperstream.history_security_contract import (
    FileWorkflowKind,
    HistoryFailure,
    HistoryRecordRef,
    LicenseState,
)
from whisperstream.license_manager import LicenseManager
from whisperstream import lockdown_keystore
from whisperstream.transcript_store import TranscriptStore
from whisperstream.windows_history_security import (
    WindowsFileWorkflowAdapter,
    WindowsHistoryStoreAdapter,
    WindowsLicenseStateAdapter,
)
from whisperstream.windows_protected_secret import WindowsDpapiProtectedSecretStore


def _require_native_windows() -> None:
    assert platform.system() == "Windows"
    assert platform.machine().lower() in {"amd64", "x86_64"}
    assert struct.calcsize("P") == 8
    assert not os.environ.get("WSL_INTEROP")


def _scan_files(root: Path, forbidden: tuple[bytes, ...]) -> tuple[int, int, int]:
    files = 0
    size = 0
    hits = 0
    if not root.exists():
        return files, size, hits
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        files += 1
        size += len(payload)
        hits += sum(payload.count(value) for value in forbidden if value)
    return files, size, hits


def main() -> int:
    _require_native_windows()
    run_id = uuid.uuid4().hex
    root = Path(tempfile.gettempdir()) / f"whisperstream-unit05-{run_id}"
    assert not root.exists()
    root.mkdir(parents=True)
    data_dir = root / "data"
    file_dir = root / "selected-files"
    file_dir.mkdir()
    original_data_dir = os.environ.get("WHISPERSTREAM_DATA_DIR")
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    owned_store: TranscriptStore | None = None
    selections_after = -1
    summary: dict[str, object] = {}
    marker = f"synthetic-{run_id}".encode("ascii")
    password = f"password-{run_id}"
    recovery = ""
    try:
        os.environ["WHISPERSTREAM_DATA_DIR"] = str(data_dir)
        protected = WindowsDpapiProtectedSecretStore()
        assert protected.availability().value == "available"

        owned_store = TranscriptStore(protected_secret_store=protected)
        now = int(time.time() * 1000)
        old_id = owned_store.save(
            raw_text=marker.decode("ascii") + "-old",
            enhanced_text=None,
            duration_ms=1,
            model="synthetic",
            source="dictation",
            audio_bytes=marker + b"-old-audio",
            created_at=now - 3 * 86_400_000,
        )
        live_id = owned_store.save(
            raw_text=marker.decode("ascii") + "-live",
            enhanced_text=None,
            duration_ms=1,
            model="synthetic",
            source="dictation",
            audio_bytes=marker + b"-live-audio",
            created_at=now,
        )
        assert old_id and live_id
        owned_store.close()
        owned_store = TranscriptStore(protected_secret_store=protected)
        assert owned_store.get(live_id) is not None

        history = WindowsHistoryStoreAdapter(
            store_provider=lambda: owned_store,
            protected_store_provider=lambda: protected,
            lockdown_enabled=lambda: False,
            lockdown_locked=lambda: False,
        )
        assert history.enforce_retention_with_count(1).count == 1
        assert owned_store.get(old_id) is None
        assert history.enforce_retention_with_count(0).count == 0

        recovery = lockdown_keystore.enable_lockdown(
            data_dir, password, store=protected
        )
        owned_store.clear_data_key()
        assert not (data_dir / "transcript.key").exists()
        assert lockdown_keystore.unlock_with_password(data_dir, "incorrect") is None
        key = lockdown_keystore.unlock_with_recovery_code(data_dir, recovery)
        assert key is not None and owned_store.set_data_key(key)
        assert owned_store.get(live_id) is not None
        assert lockdown_keystore.disable_lockdown(
            data_dir, password, store=protected
        )
        assert owned_store.reload_data_key()
        assert history.delete_with_count(HistoryRecordRef(live_id)).count == 1
        assert history.clear_with_count().count == 0

        source = file_dir / "source.wav"
        destination = file_dir / "export.wav"
        source.write_bytes(marker + b"-selected-source")
        source_hash = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
        files = WindowsFileWorkflowAdapter()
        imported = files.register_paths(FileWorkflowKind.IMPORT_AUDIO, [source])
        assert imported.failure is HistoryFailure.NONE
        read = asyncio.run(files.read(imported.selections[0]))
        assert read.failure is HistoryFailure.NONE and read.payload == source.read_bytes()
        exported = files.register_paths(
            FileWorkflowKind.EXPORT_TRANSCRIPT, [destination]
        )
        assert asyncio.run(
            files.write(exported.selections[0], marker + b"-export")
        ).failure is HistoryFailure.NONE
        files.release_many(imported.selections + exported.selections)
        files.close()
        selections_after = files.active_selection_count
        assert __import__("hashlib").sha256(source.read_bytes()).hexdigest() == source_hash
        source.rename(file_dir / "source-closed.wav")
        destination.rename(file_dir / "export-closed.wav")

        license_dir = root / "license-data"
        os.environ["WHISPERSTREAM_DATA_DIR"] = str(license_dir)
        manager = LicenseManager()
        trial = WindowsLicenseStateAdapter(
            manager_provider=lambda: manager
        ).snapshot()
        assert trial.state is LicenseState.PRO
        manager.mark_trial_started()
        restarted_manager = LicenseManager()
        assert WindowsLicenseStateAdapter(
            manager_provider=lambda: restarted_manager
        ).snapshot().state is LicenseState.PRO
        assert not license_dir.exists()
        license_dir.mkdir()
        (license_dir / "license.json").write_text("tampered", encoding="utf-8")
        tampered_manager = LicenseManager()
        assert WindowsLicenseStateAdapter(
            manager_provider=lambda: tampered_manager
        ).snapshot().state is LicenseState.PRO

        assert (license_dir / "license.json").read_text(encoding="utf-8") == "tampered"

        app_files, app_bytes, privacy_hits = _scan_files(
            data_dir,
            (
                marker,
                password.encode("utf-8"),
                recovery.encode("utf-8"),
            ),
        )
        assert privacy_hits == 0
        assert selections_after == 0
        assert not (
            {thread.ident for thread in threading.enumerate()} - baseline_threads
        )
        summary = {
            "verdict": "PASS",
            "platform": "Windows-AMD64",
            "history_restart_records": 1,
            "retention_rows_removed": 1,
            "exact_record_deletes": 1,
            "lockdown_password_recovery_cycles": 1,
            "dpapi_reference_present_after_disable": (
                data_dir / "transcript.key"
            ).read_text(encoding="utf-8").startswith("enc:"),
            "license_restart_states": 3,
            "file_handles_closed": True,
            "active_file_selections_after": selections_after,
            "app_files_scanned": app_files,
            "app_bytes_scanned": app_bytes,
            "privacy_hits": privacy_hits,
            "owned_threads_after": 0,
            "secret_material_recorded": False,
        }
    finally:
        if owned_store is not None:
            try:
                owned_store.clear_data_key()
                owned_store.close()
            except Exception:
                pass
        if original_data_dir is None:
            os.environ.pop("WHISPERSTREAM_DATA_DIR", None)
        else:
            os.environ["WHISPERSTREAM_DATA_DIR"] = original_data_dir
        shutil.rmtree(root, ignore_errors=False)
        assert not root.exists()

    summary["unit_temp_roots_after_cleanup"] = 0
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
