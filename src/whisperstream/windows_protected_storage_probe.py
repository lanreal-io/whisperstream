"""Content-free frozen Windows protected-storage consumer probe.

This private probe is dispatched by ``whisperstream_entry.py`` before the
ordinary backend starts.  It accepts no secret material, creates synthetic
values inside the frozen process, exercises the real CurrentUser DPAPI adapter
through the API-key, transcript, and Lockdown consumers, and deletes only its
fresh UUID-owned data directory.
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
import tempfile
import tomllib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .config_manager import (
    DATA_DIR_ENV_VAR,
    PROTECTED_API_KEY_MARKER,
    ConfigManager,
    WhisperstreamConfig,
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
from .protected_secret import UnavailableProtectedSecretStore
from .protected_secret_contract import (
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
)
from .transcript_crypto import load_or_create_data_key
from .transcript_store import TranscriptStore
from .windows_protected_secret import WindowsDpapiProtectedSecretStore


_OWNERSHIP_MARKER = ".windows-protected-storage-probe-owned"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _write_new_json(path: Path, payload: dict[str, object]) -> None:
    path = path.resolve()
    if path.exists():
        raise RuntimeError("probe output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _references(path: Path) -> dict[str, str]:
    with path.open("rb") as handle:
        parsed = tomllib.load(handle)
    return {
        key: value
        for key, value in parsed.items()
        if key.endswith("api_key")
        and isinstance(value, str)
        and value.startswith("enc:")
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
        if value:
            forms.update((value, base64.b64encode(value), value.hex().encode("ascii")))
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
        if content is not captured_log:
            bytes_scanned += len(content)
        for form in forms:
            matches += content.count(form)
    return {
        "files_scanned": len(files),
        "bytes_scanned": bytes_scanned,
        "forbidden_forms": len(forms),
        "matches": matches,
        "captured_log_bytes": len(captured_log),
        "captured_log_sha256": hashlib.sha256(captured_log).hexdigest(),
    }


def _cleanup_owned_data_dir(data_dir: Path, namespace: str) -> bool:
    marker = data_dir / _OWNERSHIP_MARKER
    try:
        owned = marker.read_text(encoding="ascii") == namespace
    except OSError:
        owned = False
    if not owned:
        return False
    shutil.rmtree(data_dir)
    return not data_dir.exists()


def run_probe(*, output: Path, test_namespace: str, data_dir: Path) -> dict[str, object]:
    canonical_namespace = str(uuid.UUID(test_namespace))
    _require(canonical_namespace == test_namespace, "test namespace must be canonical UUID")
    _require(platform.system() == "Windows", "probe requires native Windows")
    data_dir = data_dir.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    _require(data_dir.is_relative_to(temp_root), "probe data directory must be under OS temp")
    _require(not data_dir.exists(), "probe data directory must be fresh")
    _require(data_dir != temp_root and len(data_dir.parts) > len(temp_root.parts) + 1,
             "probe data directory is too broad")

    data_dir.parent.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir()
    (data_dir / _OWNERSHIP_MARKER).write_text(test_namespace, encoding="ascii")

    store = WindowsDpapiProtectedSecretStore()
    stages: list[str] = []
    sensitive_values: list[bytes] = []
    error: dict[str, str] | None = None
    scan: dict[str, object] | None = None
    tree: dict[str, object] | None = None
    cleanup_verified = False
    api_reference_count = 0
    transcript_restart_count = 0
    concurrency_workers = 8
    old_data_dir = os.environ.get(DATA_DIR_ENV_VAR)
    log_stream = io.StringIO()
    log_handler = logging.StreamHandler(log_stream)
    probe_logger = logging.getLogger("whisperstream")
    previous_log_level = probe_logger.level
    probe_logger.addHandler(log_handler)
    probe_logger.setLevel(logging.DEBUG)
    transcript_store: TranscriptStore | None = None

    try:
        os.environ[DATA_DIR_ENV_VAR] = str(data_dir)
        _require(
            store.availability() is ProtectionAvailability.AVAILABLE,
            "native DPAPI provider unavailable",
        )
        stages.append("provider-available")

        api_one = f"ws-probe-{secrets.token_hex(24)}"
        api_two = f"ws-probe-{secrets.token_hex(24)}"
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
        client = manager.client_dict(config)
        _require(
            client["enhancement_gemini_api_key"] == PROTECTED_API_KEY_MARKER,
            "client marker mismatch",
        )
        _require("enc:" not in repr(client), "DPAPI reference crossed client boundary")
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
        restarted_config.enhancement_api_key = ""
        restarted_config.enhancement_gemini_api_key = ""
        restarted_manager.save(restarted_config)
        remaining_references = _references(manager.config_path)
        _require(
            "enhancement_gemini_api_key" not in remaining_references,
            "removed API reference remains",
        )
        api_reference_count = len(set(remaining_references.values()))
        stages.append("api-key-restart-marker-cleanup")

        raw_text = f"raw-{secrets.token_hex(32)}"
        enhanced_text = f"enhanced-{secrets.token_hex(32)}"
        audio_bytes = secrets.token_bytes(320)
        password_one = f"Pw1-{secrets.token_urlsafe(24)}"
        password_two = f"Pw2-{secrets.token_urlsafe(24)}"
        sensitive_values.extend(
            (
                raw_text.encode(), enhanced_text.encode(), audio_bytes,
                password_one.encode(), password_two.encode(),
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
        _require(key_reference.startswith("enc:"), "transcript key is not DPAPI protected")
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

        recovery_one = enable_lockdown(data_dir, password_one, store=store)
        sensitive_values.append(recovery_one.encode())
        _require(is_lockdown_enabled(data_dir), "lockdown enable failed")
        _require(not (data_dir / "transcript.key").exists(), "DPAPI ref survived enable")
        transcript_store.clear_data_key()
        _require(transcript_store.get(record_id) is None, "locked read did not fail closed")
        unlocked = unlock_with_password(data_dir, password_one)
        _require(unlocked is not None, "password unlock failed")
        sensitive_values.append(unlocked)
        transcript_store.set_data_key(unlocked)
        _require(transcript_store.get(record_id) is not None, "unlocked read failed")
        _require(change_password(data_dir, password_one, password_two), "password change failed")
        _require(unlock_with_password(data_dir, password_one) is None, "old password remained valid")
        recovery_two = regenerate_recovery_code(data_dir, password_two)
        _require(recovery_two is not None, "recovery regeneration failed")
        sensitive_values.append(recovery_two.encode())
        _require(unlock_with_recovery_code(data_dir, recovery_one) is None,
                 "old recovery remained valid")
        _require(unlock_with_recovery_code(data_dir, recovery_two) is not None,
                 "new recovery failed")
        _require(disable_lockdown(data_dir, password_two, store=store), "disable failed")
        transcript_store.reload_data_key()
        _require(transcript_store.get(record_id) is not None, "disable read failed")
        _require(
            _encrypted_row_hash(data_dir / "transcripts.db", record_id) == row_hash_before
            and hashlib.sha256(audio_path.read_bytes()).hexdigest() == audio_hash_before,
            "lockdown rewrote encrypted content",
        )
        transcript_store.close()
        transcript_store = TranscriptStore(protected_secret_store=store)
        _require(transcript_store.get(record_id) is not None, "post-disable restart failed")
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
        stages.append("lockdown-change-recover-disable-reset")

        corrupt_key_path = data_dir / "corrupt-transcript.key"
        corrupt_key_path.write_text("enc:not-base64", encoding="utf-8")
        _require(
            load_or_create_data_key(corrupt_key_path, store=store) is None
            and corrupt_key_path.read_text(encoding="utf-8") == "enc:not-base64",
            "corrupt transcript reference was replaced",
        )
        corrupt_manager = ConfigManager(
            config_path=data_dir / "corrupt-api.toml",
            models_root=data_dir / "corrupt-api-models",
            protected_secret_store=store,
        )
        corrupt_value = f"corrupt-{secrets.token_hex(24)}"
        sensitive_values.append(corrupt_value.encode())
        corrupt_config = WhisperstreamConfig(
            enhancement_provider="custom",
            enhancement_custom_api_key=corrupt_value,
        )
        corrupt_manager.save(corrupt_config)
        valid_reference = _references(corrupt_manager.config_path)[
            "enhancement_custom_api_key"
        ]
        config_bytes = corrupt_manager.config_path.read_bytes()
        corrupt_manager.config_path.write_bytes(
            config_bytes.replace(valid_reference.encode(), b"enc:not-base64")
        )
        corrupt_loaded = ConfigManager(
            config_path=corrupt_manager.config_path,
            models_root=data_dir / "corrupt-api-models",
            protected_secret_store=store,
        ).load()
        _require(corrupt_loaded.enhancement_custom_api_key == "", "corrupt API ref loaded")
        _require(b"enc:not-base64" in corrupt_manager.config_path.read_bytes(),
                 "corrupt API reference was replaced")
        stages.append("corrupt-references-fail-closed")

        unavailable = UnavailableProtectedSecretStore()
        unavailable_root = data_dir / "unavailable"
        unavailable_manager = ConfigManager(
            config_path=unavailable_root / "config.toml",
            models_root=unavailable_root / "models",
            protected_secret_store=unavailable,
        )
        unavailable_value = f"unavailable-{secrets.token_hex(24)}"
        sensitive_values.append(unavailable_value.encode())
        unavailable_config = WhisperstreamConfig(
            enhancement_provider="gemini",
            enhancement_gemini_api_key=unavailable_value,
        )
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
        _require(not (unavailable_root / "lockdown.key.json").exists(),
                 "unavailable lockdown envelope exists")
        stages.append("provider-unavailable-fail-closed")

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
            list(executor.map(lambda _index: concurrent_manager.save(concurrent_config), range(16)))
        _require(len(set(_references(concurrent_manager.config_path).values())) == 1,
                 "concurrent API reference mismatch")
        concurrent_key_path = data_dir / "concurrent-transcript.key"
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency_workers) as executor:
            keys = list(executor.map(
                lambda _index: load_or_create_data_key(concurrent_key_path, store=store),
                range(16),
            ))
        _require(
            all(key is not None and secrets.compare_digest(key, keys[0]) for key in keys),
            "concurrent transcript key mismatch",
        )
        if keys[0] is not None:
            sensitive_values.append(keys[0])
        stages.append("concurrency-stable-references")

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
        error = {"operation": caught.operation.value, "failure": caught.failure.value}
    except Exception:  # noqa: BLE001 - retained report stays content-free
        error = {"operation": "consumer-probe", "failure": "backend-failure"}
    finally:
        if transcript_store is not None:
            transcript_store.close()
        probe_logger.removeHandler(log_handler)
        probe_logger.setLevel(previous_log_level)
        log_handler.close()
        cleanup_verified = _cleanup_owned_data_dir(data_dir, test_namespace)
        if old_data_dir is None:
            os.environ.pop(DATA_DIR_ENV_VAR, None)
        else:
            os.environ[DATA_DIR_ENV_VAR] = old_data_dir

    payload: dict[str, object] = {
        "schema_version": 1,
        "verdict": "PASS" if error is None and cleanup_verified else "BLOCKED",
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "provider": "windows-dpapi-current-user-v1",
        "test_namespace": test_namespace,
        "stages": stages,
        "api_reference_count_after_active_removal": api_reference_count,
        "transcript_restart_record_count": transcript_restart_count,
        "concurrency_workers": concurrency_workers,
        "persistence_measurement": tree,
        "plaintext_scan": scan,
        "cleanup_verified": cleanup_verified,
        "data_directory_absent_after_cleanup": not data_dir.exists(),
        "secret_material_recorded": False,
        "normal_user_state_mutated": False,
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
        print("BLOCKED windows-protected-storage-probe setup-failure", file=sys.stderr)
        return 1
    if payload["verdict"] != "PASS":
        print("BLOCKED windows-protected-storage-probe", file=sys.stderr)
        return 1
    print("PASS windows-protected-storage-probe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
