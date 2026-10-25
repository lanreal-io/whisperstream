"""Content-free consumer integration probe for the internal macOS engine.

The probe owns one fresh data directory and one UUID-suffixed Keychain service.
It generates all synthetic values inside the process, exercises the real
Security.framework adapter through the production API-key, transcript, and
Lockdown consumers, scans persistence and captured logs, and removes only its
exact data directory and Keychain identities. It accepts no secret material.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import io
import json
import logging
import os
import platform
import secrets
import shutil
import sqlite3
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .access_log import get_access_logger, log_access
from .config_manager import (
    DATA_DIR_ENV_VAR,
    PROTECTED_API_KEY_MARKER,
    ConfigManager,
    WhisperstreamConfig,
    is_loopback_endpoint,
)
from .key_encryption import fingerprint_api_key
from .lockdown_keystore import (
    change_password,
    disable_lockdown,
    enable_lockdown,
    is_lockdown_enabled,
    regenerate_recovery_code,
    reset_lockdown,
    unlock_with_password,
    unlock_with_recovery_code,
)
from .macos_keychain_probe import _write_new_json
from .macos_protected_secret import MacosKeychainProtectedSecretStore
from .protected_secret import UnavailableProtectedSecretStore
from .protected_secret_contract import (
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
    macos_keychain_account,
    macos_keychain_service,
    parse_protected_secret_reference,
)
from .transcript_crypto import delete_data_key, load_or_create_data_key
from .transcript_store import TranscriptStore


_OWNERSHIP_MARKER = ".macos-protected-storage-probe-owned"


class _TrackingStore:
    """Track exact phase-owned identities while delegating to real Keychain."""

    def __init__(self, delegate: MacosKeychainProtectedSecretStore) -> None:
        self.delegate = delegate
        self.created: dict[str, ProtectedSecretPurpose] = {}
        self.deleted: set[str] = set()

    @property
    def service(self) -> str:
        return self.delegate.service

    def availability(self) -> ProtectionAvailability:
        return self.delegate.availability()

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        reference = self.delegate.store(secret, purpose=purpose)
        self.created[reference] = purpose
        return reference

    def load(self, reference: str) -> bytes:
        return self.delegate.load(reference)

    def delete(self, reference: str) -> None:
        self.delegate.delete(reference)
        self.deleted.add(reference)

    def contains(self, reference: str) -> bool:
        return self.delegate.contains(reference)

    def update(
        self,
        reference: str,
        secret: bytes,
        *,
        purpose: ProtectedSecretPurpose,
    ) -> None:
        self.delegate.update(reference, secret, purpose=purpose)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _references(path: Path) -> dict[str, str]:
    with path.open("rb") as handle:
        parsed = tomllib.load(handle)
    return {
        key: value
        for key, value in parsed.items()
        if key.endswith("api_key")
        and isinstance(value, str)
        and value.startswith("keychain:v1:")
    }


def _tree_measurement(root: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    count = 0
    total_bytes = 0
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(hashlib.sha256(content).digest())
        count += 1
        total_bytes += len(content)
    return {
        "file_count": count,
        "total_bytes": total_bytes,
        "aggregate_sha256": digest.hexdigest(),
    }


def _encrypted_row_hash(database: Path, record_id: str) -> str:
    connection = sqlite3.connect(str(database))
    try:
        row = connection.execute(
            "SELECT raw_text, raw_nonce, enhanced_text, enhanced_nonce, "
            "audio_path, audio_nonce FROM recordings WHERE id = ?",
            (record_id,),
        ).fetchone()
    finally:
        connection.close()
    _require(row is not None, "encrypted transcript row missing")
    digest = hashlib.sha256()
    for value in row:
        if value is None:
            digest.update(b"none\0")
        elif isinstance(value, str):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        else:
            digest.update(bytes(value))
            digest.update(b"\0")
    return digest.hexdigest()


def _forbidden_forms(values: list[bytes]) -> list[bytes]:
    forms: set[bytes] = set()
    for value in values:
        if not value:
            continue
        forms.add(value)
        forms.add(base64.b64encode(value))
        forms.add(value.hex().encode("ascii"))
    return sorted(forms, key=lambda item: (len(item), item))


def _scan_plaintext(
    root: Path,
    *,
    captured_log: bytes,
    sensitive_values: list[bytes],
) -> dict[str, object]:
    files = [path for path in root.rglob("*") if path.is_file()]
    forms = _forbidden_forms(sensitive_values)
    matches = 0
    bytes_scanned = len(captured_log)
    for content in [*(path.read_bytes() for path in files), captured_log]:
        bytes_scanned += len(content) if content is not captured_log else 0
        for form in forms:
            if form in content:
                matches += content.count(form)
    return {
        "files_scanned": len(files),
        "bytes_scanned": bytes_scanned,
        "forbidden_forms": len(forms),
        "matches": matches,
        "captured_log_bytes": len(captured_log),
        "captured_log_sha256": hashlib.sha256(captured_log).hexdigest(),
    }


def _close_probe_log_handlers(data_dir: Path) -> None:
    logger = get_access_logger()
    for handler in list(logger.handlers):
        base_filename = getattr(handler, "baseFilename", None)
        if base_filename and Path(base_filename).resolve().is_relative_to(data_dir):
            logger.removeHandler(handler)
            handler.close()


def _cleanup_owned_data_dir(data_dir: Path, test_namespace: str) -> bool:
    marker = data_dir / _OWNERSHIP_MARKER
    try:
        owned = marker.read_text(encoding="ascii") == test_namespace
    except OSError:
        owned = False
    if not owned:
        return False
    shutil.rmtree(data_dir)
    return not data_dir.exists()


def run_probe(
    *,
    output: Path,
    test_namespace: str,
    data_dir: Path,
) -> dict[str, object]:
    data_dir = data_dir.resolve()
    if data_dir.exists():
        raise RuntimeError("probe data directory already exists")
    if data_dir == Path.home().resolve() or len(data_dir.parts) < 4:
        raise RuntimeError("probe data directory is too broad")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("protected-storage probe requires native Darwin arm64")

    service = macos_keychain_service(test_namespace=test_namespace)
    data_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.mkdir(mode=0o700)
    (data_dir / _OWNERSHIP_MARKER).write_text(test_namespace, encoding="ascii")
    os.chmod(data_dir / _OWNERSHIP_MARKER, 0o600)

    delegate = MacosKeychainProtectedSecretStore(test_namespace=test_namespace)
    store = _TrackingStore(delegate)
    stages: list[str] = []
    error: dict[str, str] | None = None
    sensitive_values: list[bytes] = []
    scan: dict[str, object] | None = None
    tree: dict[str, object] | None = None
    cleanup_verified = False
    api_reference_count = 0
    transcript_restart_count = 0
    concurrency_workers = 16
    old_data_dir = os.environ.get(DATA_DIR_ENV_VAR)
    log_stream = io.StringIO()
    log_handler = logging.StreamHandler(log_stream)
    probe_logger = logging.getLogger("whisperstream")
    probe_logger.addHandler(log_handler)
    previous_log_level = probe_logger.level
    probe_logger.setLevel(logging.DEBUG)
    transcript_store: TranscriptStore | None = None

    try:
        os.environ[DATA_DIR_ENV_VAR] = str(data_dir)
        _require(
            store.availability() is ProtectionAvailability.AVAILABLE,
            "native provider unavailable",
        )
        stages.append("provider-available")

        # API-key persistence, fixed client marker, fingerprint safety, stable
        # references, exact replacement/removal, and restart.
        api_one = f"unit04-{secrets.token_hex(24)}"
        api_two = f"unit04-{secrets.token_hex(24)}"
        sensitive_values.extend((api_one.encode(), api_two.encode()))
        manager = ConfigManager(protected_secret_store=store)
        config = manager.load()
        config.enhancement_provider = "gemini"
        config.enhancement_api_key = api_one
        config.enhancement_gemini_api_key = api_one
        config.enhancement_openai_api_key = api_two
        manager.save(config)
        first_references = _references(manager.config_path)
        _require(len(set(first_references.values())) == 2, "API reference count mismatch")
        _require(
            manager.client_dict(config)["enhancement_gemini_api_key"]
            == PROTECTED_API_KEY_MARKER,
            "client marker mismatch",
        )
        _require(
            "keychain:v1:" not in repr(manager.client_dict(config)),
            "provider reference crossed client boundary",
        )
        _require(
            fingerprint_api_key(api_one) not in {api_one, PROTECTED_API_KEY_MARKER},
            "fingerprint safety mismatch",
        )
        manager.save(config)
        _require(_references(manager.config_path) == first_references, "API reference churn")

        restarted_manager = ConfigManager(protected_secret_store=store)
        restarted_config = restarted_manager.load()
        _require(
            secrets.compare_digest(restarted_config.enhancement_gemini_api_key, api_one)
            and secrets.compare_digest(restarted_config.enhancement_openai_api_key, api_two),
            "API restart mismatch",
        )
        removed_reference = first_references["enhancement_gemini_api_key"]
        restarted_config.enhancement_api_key = ""
        restarted_config.enhancement_gemini_api_key = ""
        restarted_manager.save(restarted_config)
        _require(not store.contains(removed_reference), "removed API item remains")
        api_reference_count = len(set(_references(manager.config_path).values()))
        stages.append("api-key-restart-marker-cleanup")

        # Transcript save/list/read/audio and restart use the same AES-256-GCM
        # row/audio format while transcript.key stores only a Keychain ref.
        raw_text = f"raw-{secrets.token_hex(32)}"
        enhanced_text = f"enhanced-{secrets.token_hex(32)}"
        audio_bytes = secrets.token_bytes(320)
        password_one = f"Pw1-{secrets.token_urlsafe(24)}"
        password_two = f"Pw2-{secrets.token_urlsafe(24)}"
        sensitive_values.extend(
            (
                raw_text.encode(),
                enhanced_text.encode(),
                audio_bytes,
                password_one.encode(),
                password_two.encode(),
            )
        )
        transcript_store = TranscriptStore(protected_secret_store=store)
        _require(transcript_store.decryptable, "transcript key unavailable")
        record_id = transcript_store.save(
            raw_text=raw_text,
            enhanced_text=enhanced_text,
            duration_ms=1250,
            model="synthetic",
            source="probe",
            audio_bytes=audio_bytes,
            peaks=[0.1, 0.2],
        )
        _require(record_id is not None, "transcript save failed")
        key_reference = (data_dir / "transcript.key").read_text(encoding="utf-8").strip()
        parse_protected_secret_reference(key_reference)
        _require(store.contains(key_reference), "transcript Keychain item missing")
        row_hash_before = _encrypted_row_hash(data_dir / "transcripts.db", record_id)
        audio_path = data_dir / transcript_store._audio_path_for(record_id)
        audio_hash_before = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        transcript_store.close()
        transcript_store = TranscriptStore(protected_secret_store=store)
        listed = transcript_store.list(limit=10)
        loaded_record = transcript_store.get(record_id)
        loaded_audio = transcript_store.get_audio_bytes(record_id)
        _require(
            listed.get("decryptable") is not False and len(listed["items"]) == 1,
            "transcript list restart mismatch",
        )
        _require(
            loaded_record is not None
            and secrets.compare_digest(loaded_record["raw_text"], raw_text)
            and secrets.compare_digest(loaded_record["enhanced_text"], enhanced_text),
            "transcript text restart mismatch",
        )
        _require(
            loaded_audio is not None and secrets.compare_digest(loaded_audio, audio_bytes),
            "transcript audio restart mismatch",
        )
        transcript_restart_count = len(listed["items"])
        stages.append("transcript-save-list-read-audio-restart")

        # Lockdown moves the exact same data key from Keychain into the existing
        # password/recovery envelopes, then restores it without rewriting rows.
        recovery_one = enable_lockdown(data_dir, password_one, store=store)
        sensitive_values.append(recovery_one.encode())
        _require(is_lockdown_enabled(data_dir), "lockdown enable failed")
        _require(not (data_dir / "transcript.key").exists(), "provider ref survived enable")
        _require(not store.contains(key_reference), "provider item survived enable")
        transcript_store.clear_data_key()
        _require(transcript_store.get(record_id) is None, "locked read did not fail closed")
        unlocked = unlock_with_password(data_dir, password_one)
        _require(unlocked is not None, "password unlock failed")
        sensitive_values.append(unlocked)
        transcript_store.set_data_key(unlocked)
        _require(transcript_store.get(record_id) is not None, "unlocked read failed")
        _require(
            change_password(data_dir, password_one, password_two),
            "password change failed",
        )
        _require(unlock_with_password(data_dir, password_one) is None, "old password remained valid")
        recovery_two = regenerate_recovery_code(data_dir, password_two)
        _require(recovery_two is not None, "recovery regeneration failed")
        sensitive_values.append(recovery_two.encode())
        _require(
            unlock_with_recovery_code(data_dir, recovery_one) is None,
            "old recovery remained valid",
        )
        _require(
            unlock_with_recovery_code(data_dir, recovery_two) is not None,
            "new recovery failed",
        )
        _require(disable_lockdown(data_dir, password_two, store=store), "disable failed")
        transcript_store.reload_data_key()
        _require(transcript_store.get(record_id) is not None, "disable read failed")
        _require(
            _encrypted_row_hash(data_dir / "transcripts.db", record_id)
            == row_hash_before
            and hashlib.sha256(audio_path.read_bytes()).hexdigest() == audio_hash_before,
            "lockdown rewrote encrypted content",
        )
        transcript_store.close()
        transcript_store = TranscriptStore(protected_secret_store=store)
        _require(transcript_store.get(record_id) is not None, "post-disable restart failed")

        # Destructive reset semantics: wipe ciphertext first, then destroy the
        # envelopes and re-materialize a fresh protected key for the empty store.
        recovery_three = enable_lockdown(data_dir, password_two, store=store)
        sensitive_values.append(recovery_three.encode())
        _require(transcript_store.clear() == 1, "reset pre-wipe row count mismatch")
        reset_lockdown(data_dir, store=store)
        transcript_store.reload_data_key()
        _require(
            not is_lockdown_enabled(data_dir)
            and transcript_store.list(limit=10)["items"] == [],
            "destructive reset state mismatch",
        )
        _require(
            WhisperstreamConfig(
                lockdown_mode=True,
                enhancement_provider="gemini",
            ).effective_provider()
            == "local"
            and is_loopback_endpoint("http://127.0.0.1:38420")
            and not is_loopback_endpoint("https://example.invalid"),
            "lockdown cloud-egress boundary mismatch",
        )
        log_access("lock", None, enabled=True)
        log_access("unlock", None, enabled=True)
        log_access("reset", None, enabled=True)
        stages.append("lockdown-migrate-change-recover-disable-reset")

        # Missing and corrupt native items both fail closed for API and
        # transcript consumers. The test mutates only exact phase-owned refs.
        missing_path = data_dir / "missing-transcript.key"
        _require(load_or_create_data_key(missing_path, store=store) is not None, "missing setup failed")
        missing_reference = missing_path.read_text(encoding="utf-8").strip()
        store.delete(missing_reference)
        _require(
            load_or_create_data_key(missing_path, store=store) is None,
            "missing transcript item did not fail closed",
        )
        missing_path.unlink()

        corrupt_path = data_dir / "corrupt-transcript.key"
        _require(load_or_create_data_key(corrupt_path, store=store) is not None, "corrupt setup failed")
        corrupt_reference = corrupt_path.read_text(encoding="utf-8").strip()
        store.update(
            corrupt_reference,
            b"\x00" * 31,
            purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY,
        )
        _require(
            load_or_create_data_key(corrupt_path, store=store) is None,
            "corrupt transcript item did not fail closed",
        )
        delete_data_key(corrupt_path, store=store)

        missing_manager = ConfigManager(
            config_path=data_dir / "missing-api.toml",
            models_root=data_dir / "missing-api-models",
            protected_secret_store=store,
        )
        missing_config = WhisperstreamConfig(
            enhancement_provider="custom",
            enhancement_custom_api_key=f"missing-{secrets.token_hex(24)}",
        )
        sensitive_values.append(missing_config.enhancement_custom_api_key.encode())
        missing_manager.save(missing_config)
        missing_api_reference = _references(missing_manager.config_path)[
            "enhancement_custom_api_key"
        ]
        store.delete(missing_api_reference)
        missing_loaded = ConfigManager(
            config_path=missing_manager.config_path,
            models_root=data_dir / "missing-api-models",
            protected_secret_store=store,
        ).load()
        _require(missing_loaded.enhancement_custom_api_key == "", "missing API item loaded")

        corrupt_manager = ConfigManager(
            config_path=data_dir / "corrupt-api.toml",
            models_root=data_dir / "corrupt-api-models",
            protected_secret_store=store,
        )
        corrupt_config = WhisperstreamConfig(
            enhancement_provider="custom",
            enhancement_custom_api_key=f"corrupt-{secrets.token_hex(24)}",
        )
        sensitive_values.append(corrupt_config.enhancement_custom_api_key.encode())
        corrupt_manager.save(corrupt_config)
        corrupt_api_reference = _references(corrupt_manager.config_path)[
            "enhancement_custom_api_key"
        ]
        store.update(
            corrupt_api_reference,
            b"\xff\xfe\xfd",
            purpose=ProtectedSecretPurpose.API_KEY,
        )
        corrupt_loaded = ConfigManager(
            config_path=corrupt_manager.config_path,
            models_root=data_dir / "corrupt-api-models",
            protected_secret_store=store,
        ).load()
        _require(corrupt_loaded.enhancement_custom_api_key == "", "corrupt API item loaded")
        stages.append("missing-corrupt-items-fail-closed")

        # Explicit unavailable injection is the deterministic provider failure
        # seam; the real adapter path above separately proves native availability.
        unavailable = UnavailableProtectedSecretStore()
        unavailable_root = data_dir / "unavailable"
        unavailable_manager = ConfigManager(
            config_path=unavailable_root / "config.toml",
            models_root=unavailable_root / "models",
            protected_secret_store=unavailable,
        )
        unavailable_config = WhisperstreamConfig(
            enhancement_provider="gemini",
            enhancement_gemini_api_key=f"unavailable-{secrets.token_hex(24)}",
        )
        sensitive_values.append(unavailable_config.enhancement_gemini_api_key.encode())
        try:
            unavailable_manager.save(unavailable_config)
        except ProtectedSecretError as caught:
            _require(
                caught.operation is ProtectedSecretOperation.STORE
                and caught.failure is ProtectedSecretFailure.UNAVAILABLE,
                "unavailable API failure mismatch",
            )
        else:
            raise RuntimeError("unavailable API persistence succeeded")
        _require(not unavailable_manager.config_path.exists(), "unavailable API file exists")
        unavailable_key_path = unavailable_root / "transcript.key"
        _require(
            load_or_create_data_key(unavailable_key_path, store=unavailable) is None
            and not unavailable_key_path.exists(),
            "unavailable transcript persistence succeeded",
        )
        try:
            enable_lockdown(unavailable_root, password_one, store=unavailable)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("unavailable lockdown enable succeeded")
        _require(
            not (unavailable_root / "lockdown.key.json").exists(),
            "unavailable lockdown envelope exists",
        )
        stages.append("provider-unavailable-fail-closed")

        # Consumer-level concurrency proves config reconciliation and transcript
        # first-use stay single-item operations through the real adapter.
        concurrent_manager = ConfigManager(
            config_path=data_dir / "concurrent.toml",
            models_root=data_dir / "concurrent-models",
            protected_secret_store=store,
        )
        concurrent_value = f"concurrent-{secrets.token_hex(24)}"
        sensitive_values.append(concurrent_value.encode())
        concurrent_config = WhisperstreamConfig(
            enhancement_provider="gemini",
            enhancement_api_key=concurrent_value,
            enhancement_gemini_api_key=concurrent_value,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency_workers) as executor:
            list(executor.map(lambda _index: concurrent_manager.save(concurrent_config), range(32)))
        concurrent_references = set(_references(concurrent_manager.config_path).values())
        _require(len(concurrent_references) == 1, "concurrent API item count mismatch")
        concurrent_key_path = data_dir / "concurrent-transcript.key"
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency_workers) as executor:
            keys = list(
                executor.map(
                    lambda _index: load_or_create_data_key(concurrent_key_path, store=store),
                    range(32),
                )
            )
        _require(
            all(key is not None and secrets.compare_digest(key, keys[0]) for key in keys),
            "concurrent transcript key mismatch",
        )
        if keys[0] is not None:
            sensitive_values.append(keys[0])
        concurrent_config.enhancement_api_key = ""
        concurrent_config.enhancement_gemini_api_key = ""
        concurrent_manager.save(concurrent_config)
        _require(
            all(not store.contains(reference) for reference in concurrent_references),
            "concurrent API cleanup failed",
        )
        delete_data_key(concurrent_key_path, store=store)
        stages.append("concurrency-exact-cleanup")

        if transcript_store is not None:
            transcript_store.close()
            transcript_store = None
        log_handler.flush()
        captured_log = log_stream.getvalue().encode("utf-8")
        scan = _scan_plaintext(
            data_dir,
            captured_log=captured_log,
            sensitive_values=sensitive_values,
        )
        _require(scan["matches"] == 0, "plaintext marker found in persistence or logs")
        tree = _tree_measurement(data_dir)
        stages.append("plaintext-and-log-scan")
    except ProtectedSecretError as caught:
        error = {
            "operation": caught.operation.value,
            "failure": caught.failure.value,
        }
    except Exception:  # noqa: BLE001 - report remains content-free
        error = {"operation": "consumer-probe", "failure": "backend-failure"}
    finally:
        if transcript_store is not None:
            transcript_store.close()
        probe_logger.removeHandler(log_handler)
        probe_logger.setLevel(previous_log_level)
        log_handler.close()
        _close_probe_log_handlers(data_dir)
        for reference in sorted(store.created):
            try:
                if store.contains(reference):
                    store.delete(reference)
            except ProtectedSecretError:
                pass
        items_clean = True
        for reference in sorted(store.created):
            try:
                if store.contains(reference):
                    items_clean = False
            except ProtectedSecretError:
                items_clean = False
        data_clean = _cleanup_owned_data_dir(data_dir, test_namespace)
        cleanup_verified = items_clean and data_clean
        if old_data_dir is None:
            os.environ.pop(DATA_DIR_ENV_VAR, None)
        else:
            os.environ[DATA_DIR_ENV_VAR] = old_data_dir

    item_metadata = []
    for reference, purpose in sorted(store.created.items()):
        parsed = parse_protected_secret_reference(reference)
        item_metadata.append(
            {
                "reference": reference,
                "account": macos_keychain_account(parsed.opaque_id),
                "purpose": purpose.value,
                "cleanup_verified": reference in store.deleted,
            }
        )

    payload: dict[str, object] = {
        "schema_version": 1,
        "verdict": "PASS" if error is None and cleanup_verified else "BLOCKED",
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "provider": "macos-keychain-generic-password-v1",
        "service": service,
        "stages": stages,
        "api_reference_count_after_active_removal": api_reference_count,
        "transcript_restart_record_count": transcript_restart_count,
        "concurrency_workers": concurrency_workers,
        "items": item_metadata,
        "item_count": len(item_metadata),
        "persistence_measurement": tree,
        "plaintext_scan": scan,
        "cleanup_verified": cleanup_verified,
        "data_directory_absent_after_cleanup": not data_dir.exists(),
        "secret_material_recorded": False,
        "authentication_ui_allowed": False,
        "permission_state_queried": False,
        "security_configuration_changed": False,
        "interaction_surfaces_entered": False,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if error is not None:
        payload["error"] = error
    _write_new_json(output, payload)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-namespace", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = run_probe(
            output=args.output,
            test_namespace=args.test_namespace,
            data_dir=args.data_dir,
        )
    except Exception:  # noqa: BLE001 - never print native details or material
        print("BLOCKED macos-protected-storage-probe setup-failure", file=sys.stderr)
        return 1
    if payload["verdict"] != "PASS":
        print("BLOCKED macos-protected-storage-probe", file=sys.stderr)
        return 1
    print("PASS macos-protected-storage-probe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
