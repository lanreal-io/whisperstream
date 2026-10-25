"""Windows conformance tests for the shared protected-secret boundary."""
from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
import platform
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from whisperstream.protected_secret_contract import (
    MACOS_KEYCHAIN_PREFIX,
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
)


pytestmark = pytest.mark.skipif(
    platform.system() != "Windows",
    reason="native Windows DPAPI conformance",
)


class _UnavailableStore:
    def availability(self) -> ProtectionAvailability:
        return ProtectionAvailability.UNAVAILABLE

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        del secret, purpose
        raise ProtectedSecretError(
            ProtectedSecretOperation.STORE,
            ProtectedSecretFailure.UNAVAILABLE,
        )

    def load(self, reference: str) -> bytes:
        del reference
        raise ProtectedSecretError(
            ProtectedSecretOperation.LOAD,
            ProtectedSecretFailure.UNAVAILABLE,
        )

    def delete(self, reference: str) -> None:
        del reference
        raise ProtectedSecretError(
            ProtectedSecretOperation.DELETE,
            ProtectedSecretFailure.UNAVAILABLE,
        )


class _MemoryStore:
    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self._lock = threading.Lock()

    def availability(self) -> ProtectionAvailability:
        return ProtectionAvailability.AVAILABLE

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        del purpose
        reference = f"{MACOS_KEYCHAIN_PREFIX}{uuid.uuid4()}"
        with self._lock:
            self.items[reference] = bytes(secret)
        return reference

    def load(self, reference: str) -> bytes:
        try:
            return self.items[reference]
        except KeyError as error:
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD,
                ProtectedSecretFailure.NOT_FOUND,
            ) from error

    def delete(self, reference: str) -> None:
        with self._lock:
            self.items.pop(reference, None)
            self.deleted.append(reference)


def test_shared_imports_are_native_free_until_adapter_selection() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    probe = (
        "import json,sys; "
        "import whisperstream.protected_secret_contract; "
        "import whisperstream.protected_secret; "
        "import whisperstream.key_encryption; "
        "import whisperstream.config_manager; "
        "import whisperstream.transcript_crypto; "
        "import whisperstream.transcript_store; "
        "import whisperstream.lockdown_keystore; "
        "import whisperstream.lockdown_state; "
        "print(json.dumps(sorted(set(sys.modules) & "
        "{'win32crypt','pywintypes','Security','Foundation'})))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(completed.stdout) == []


def test_native_windows_factory_and_api_key_round_trip() -> None:
    from whisperstream.key_encryption import decrypt_api_key, encrypt_api_key
    from whisperstream.protected_secret import get_protected_secret_store

    assert platform.system() == "Windows"
    store = get_protected_secret_store()
    assert store.availability() is ProtectionAvailability.AVAILABLE

    plaintext = "unit04-api-material-alpha"
    reference = encrypt_api_key(plaintext)
    assert reference.startswith("enc:")
    assert plaintext not in reference
    assert decrypt_api_key(reference) == plaintext
    store.delete(reference)
    assert store.load(reference) == plaintext.encode("utf-8")


def test_pre_contract_dpapi_blob_remains_readable_without_rewrite(tmp_path: Path) -> None:
    import win32crypt

    from whisperstream.key_encryption import decrypt_api_key
    from whisperstream.transcript_crypto import load_or_create_data_key

    api_plaintext = b"unit04-legacy-api-material"
    api_blob = win32crypt.CryptProtectData(
        api_plaintext, None, None, None, None, 0
    )
    api_reference = "enc:" + base64.b64encode(api_blob).decode("ascii")
    assert decrypt_api_key(api_reference) == api_plaintext.decode("utf-8")

    from whisperstream.config_manager import ConfigManager

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'enhancement_provider = "openai"\n'
        f'enhancement_openai_api_key = "{api_reference}"\n',
        encoding="utf-8",
    )
    config_bytes = config_path.read_bytes()
    manager = ConfigManager(
        config_path=config_path,
        models_root=tmp_path / "models",
    )
    assert manager.load().active_api_key() == api_plaintext.decode("utf-8")
    assert config_path.read_bytes() == config_bytes

    data_key = bytes(range(32))
    encoded_key = base64.b64encode(data_key)
    key_blob = win32crypt.CryptProtectData(
        encoded_key, None, None, None, None, 0
    )
    key_reference = "enc:" + base64.b64encode(key_blob).decode("ascii")
    key_path = tmp_path / "transcript.key"
    key_path.write_text(key_reference, encoding="utf-8")

    assert load_or_create_data_key(key_path) == data_key
    assert key_path.read_text(encoding="utf-8") == key_reference


def test_corrupt_dpapi_failure_is_typed_content_free(caplog) -> None:
    from whisperstream.protected_secret import get_protected_secret_store

    store = get_protected_secret_store()
    marker = "unit04-corrupt-material"
    reference = store.store(
        marker.encode("utf-8"),
        purpose=ProtectedSecretPurpose.API_KEY,
    )
    raw = bytearray(base64.b64decode(reference.removeprefix("enc:")))
    raw[len(raw) // 2] ^= 0x40
    corrupt = "enc:" + base64.b64encode(bytes(raw)).decode("ascii")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProtectedSecretError) as caught:
            store.load(corrupt)
    assert caught.value.failure in {
        ProtectedSecretFailure.CORRUPT,
        ProtectedSecretFailure.BACKEND_FAILURE,
    }
    assert marker not in str(caught.value)
    assert marker not in caplog.text
    assert corrupt not in caplog.text


def test_windows_native_error_mapping_is_content_free() -> None:
    from whisperstream.windows_protected_secret import _failure_for_native_error

    marker = "unit04-native-error-material"
    access_failure = _failure_for_native_error(
        OSError(5, marker),
        operation=ProtectedSecretOperation.LOAD,
    )
    corrupt_failure = _failure_for_native_error(
        OSError(13, marker),
        operation=ProtectedSecretOperation.LOAD,
    )
    unavailable_failure = _failure_for_native_error(
        OSError(126, marker),
        operation=ProtectedSecretOperation.STORE,
    )
    assert access_failure is ProtectedSecretFailure.ACCESS_DENIED
    assert corrupt_failure is ProtectedSecretFailure.CORRUPT
    assert unavailable_failure is ProtectedSecretFailure.UNAVAILABLE
    for failure in (access_failure, corrupt_failure, unavailable_failure):
        error = ProtectedSecretError(ProtectedSecretOperation.LOAD, failure)
        assert marker not in str(error)


def test_provider_unavailable_creates_no_api_or_transcript_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import whisperstream.key_encryption as key_encryption
    from whisperstream.config_manager import ConfigManager, WhisperstreamConfig
    from whisperstream.transcript_crypto import load_or_create_data_key

    unavailable = _UnavailableStore()
    monkeypatch.setattr(
        key_encryption,
        "get_protected_secret_store",
        lambda: unavailable,
    )

    config_path = tmp_path / "config.toml"
    manager = ConfigManager(config_path=config_path, models_root=tmp_path / "models")
    config = WhisperstreamConfig(
        enhancement_provider="openai",
        enhancement_openai_api_key="unit04-provider-unavailable",
    )
    with pytest.raises(ProtectedSecretError) as caught:
        manager.save(config)
    assert caught.value.failure is ProtectedSecretFailure.UNAVAILABLE
    assert not config_path.exists()

    key_path = tmp_path / "transcript.key"
    assert load_or_create_data_key(key_path, store=unavailable) is None
    assert not key_path.exists()

    from whisperstream.lockdown_state import LockdownState

    state_path = tmp_path / "lockdown.backoff"
    state = LockdownState(state_path=state_path)
    state.register_failed_attempt()
    assert not state_path.exists()


def test_config_save_failure_preserves_existing_atomic_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import whisperstream.key_encryption as key_encryption
    from whisperstream.config_manager import ConfigManager, WhisperstreamConfig

    config_path = tmp_path / "config.toml"
    manager = ConfigManager(config_path=config_path, models_root=tmp_path / "models")
    manager.save(WhisperstreamConfig(theme="dark"))
    before = config_path.read_bytes()

    monkeypatch.setattr(
        key_encryption,
        "get_protected_secret_store",
        lambda: _UnavailableStore(),
    )
    with pytest.raises(ProtectedSecretError):
        manager.save(
            WhisperstreamConfig(
                theme="light",
                enhancement_provider="gemini",
                enhancement_gemini_api_key="unit04-atomic-api-material",
            )
        )
    assert config_path.read_bytes() == before


def test_legacy_plaintext_config_migrates_and_unavailable_copy_is_scrubbed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import whisperstream.key_encryption as key_encryption
    from whisperstream.config_manager import ConfigManager

    available_path = tmp_path / "available.toml"
    available_value = "unit04-legacy-plain-alpha"
    available_path.write_text(
        'enhancement_provider = "openai"\n'
        f'enhancement_api_key = "{available_value}"\n',
        encoding="utf-8",
    )
    available_manager = ConfigManager(
        config_path=available_path,
        models_root=tmp_path / "models-a",
    )
    available = available_manager.load()
    assert available.active_api_key() == available_value
    migrated = available_path.read_text(encoding="utf-8")
    assert available_value not in migrated
    assert 'enhancement_api_key = "enc:' in migrated

    unavailable_path = tmp_path / "unavailable.toml"
    unavailable_value = "unit04-legacy-plain-beta"
    unavailable_path.write_text(
        'enhancement_provider = "anthropic"\n'
        f'enhancement_api_key = "{unavailable_value}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        key_encryption,
        "get_protected_secret_store",
        lambda: _UnavailableStore(),
    )
    unavailable_manager = ConfigManager(
        config_path=unavailable_path,
        models_root=tmp_path / "models-b",
    )
    unavailable = unavailable_manager.load()
    assert unavailable.active_api_key() == ""
    scrubbed = unavailable_path.read_text(encoding="utf-8")
    assert unavailable_value not in scrubbed
    assert 'enhancement_api_key = ""' in scrubbed


def test_transcript_key_atomic_write_cleanup_and_thread_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import whisperstream.transcript_crypto as transcript_crypto

    failing_path = tmp_path / "failed.key"
    memory = _MemoryStore()

    def fail_write(_path: Path, _value: str) -> None:
        raise OSError("synthetic write failure")

    monkeypatch.setattr(transcript_crypto, "atomic_write_text", fail_write)
    assert (
        transcript_crypto.load_or_create_data_key(failing_path, store=memory) is None
    )
    assert not failing_path.exists()
    assert memory.items == {}
    assert len(memory.deleted) == 1

    replacement_store = _MemoryStore()
    existing_key = bytes(range(32))
    existing_reference = replacement_store.store(
        base64.b64encode(existing_key),
        purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY,
    )
    replacement_path = tmp_path / "replacement.key"
    replacement_path.write_text(existing_reference, encoding="utf-8")
    with pytest.raises(OSError):
        transcript_crypto.save_data_key(
            replacement_path,
            bytes(reversed(range(32))),
            store=replacement_store,
        )
    assert replacement_path.read_text(encoding="utf-8") == existing_reference
    assert transcript_crypto.load_or_create_data_key(
        replacement_path,
        store=replacement_store,
    ) == existing_key
    assert set(replacement_store.items) == {existing_reference}

    monkeypatch.undo()
    race_path = tmp_path / "race.key"
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        keys = list(
            executor.map(
                lambda _index: transcript_crypto.load_or_create_data_key(race_path),
                range(24),
            )
        )
    assert all(key is not None and len(key) == 32 for key in keys)
    assert len(set(keys)) == 1
    assert race_path.read_text(encoding="utf-8").startswith("enc:")
    assert list(tmp_path.glob("*.tmp")) == []


def test_lockdown_unavailable_atomic_and_race_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import whisperstream.lockdown_keystore as keystore
    import whisperstream.transcript_crypto as transcript_crypto

    unavailable_dir = tmp_path / "unavailable"
    unavailable_dir.mkdir()
    monkeypatch.setattr(
        transcript_crypto,
        "get_protected_secret_store",
        lambda: _UnavailableStore(),
    )
    with pytest.raises(RuntimeError):
        keystore.enable_lockdown(unavailable_dir, "unit04-password-unavailable")
    assert not (unavailable_dir / "transcript.key").exists()
    assert not (unavailable_dir / "lockdown.key.json").exists()

    monkeypatch.undo()
    atomic_dir = tmp_path / "atomic"
    atomic_dir.mkdir()
    assert transcript_crypto.load_or_create_data_key(atomic_dir / "transcript.key")

    def fail_envelope(*_args, **_kwargs) -> None:
        raise OSError("synthetic envelope failure")

    monkeypatch.setattr(keystore, "atomic_write_json", fail_envelope)
    with pytest.raises(OSError):
        keystore.enable_lockdown(atomic_dir, "unit04-password-atomic")
    assert (atomic_dir / "transcript.key").exists()
    assert not (atomic_dir / "lockdown.key.json").exists()

    monkeypatch.undo()
    race_dir = tmp_path / "race-lockdown"
    race_dir.mkdir()
    assert transcript_crypto.load_or_create_data_key(race_dir / "transcript.key")

    def enable(index: int) -> tuple[int, str | None]:
        try:
            code = keystore.enable_lockdown(
                race_dir,
                f"unit04-race-password-{index}",
            )
            return index, code
        except RuntimeError:
            return index, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(enable, range(2)))
    winners = [(index, code) for index, code in results if code is not None]
    assert len(winners) == 1
    winner, _code = winners[0]
    assert keystore.unlock_with_password(
        race_dir,
        f"unit04-race-password-{winner}",
    ) is not None
    assert not (race_dir / "transcript.key").exists()


def test_api_config_reports_typed_unavailable_and_status(tmp_path, monkeypatch) -> None:
    import whisperstream.key_encryption as key_encryption
    import whisperstream.server.app as server_module
    from fastapi.testclient import TestClient
    from tests._platform_test_support import _make_server
    from whisperstream.config_manager import ConfigManager, WhisperstreamConfig

    unavailable = _UnavailableStore()
    monkeypatch.setattr(
        key_encryption,
        "get_protected_secret_store",
        lambda: unavailable,
    )
    monkeypatch.setattr(
        server_module,
        "get_protected_secret_store",
        lambda: unavailable,
    )
    server = _make_server()
    manager = ConfigManager(
        config_path=tmp_path / "config.toml",
        models_root=tmp_path / "models",
        protected_secret_store=unavailable,
    )
    server._app.config_manager = manager
    server._app.config = WhisperstreamConfig()
    client = TestClient(server.fastapi, raise_server_exceptions=True)

    status = client.get("/api/protected-secrets/status")
    assert status.status_code == 200
    assert status.json() == {"availability": "unavailable"}

    response = client.put(
        "/api/config",
        json={
            "enhancement_provider": "openai",
            "enhancement_openai_api_key": "unit04-api-put-material",
        },
    )
    assert response.status_code == 503
    assert response.json() == {
        "error": "protected_secret_error",
        "error_code": "secure_storage_failed",
        "operation": "store",
        "failure": "unavailable",
    }
    assert "unit04-api-put-material" not in response.text


def test_privacy_persistence_with_config_consent_transcript_and_lockdown(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    from whisperstream.byok_consent import record_consent
    from whisperstream.config_manager import ConfigManager, WhisperstreamConfig
    from whisperstream.lockdown_keystore import enable_lockdown
    from whisperstream.transcript_crypto import load_or_create_data_key

    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    api_value = "unit04-private-api-material"
    password = "unit04-private-password"
    dialog = "Synthetic consent copy for the unit test."

    manager = ConfigManager(
        config_path=tmp_path / "config.toml",
        models_root=tmp_path / "models",
    )
    with caplog.at_level(logging.DEBUG):
        manager.save(
            WhisperstreamConfig(
                enhancement_provider="openai",
                enhancement_openai_api_key=api_value,
            )
        )
        assert manager.load().active_api_key() == api_value
        record_consent("openai", dialog)
        data_key = load_or_create_data_key(tmp_path / "transcript.key")
        assert data_key is not None
        recovery_code = enable_lockdown(tmp_path, password)

    persisted = b"\n".join(
        path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    prohibited = (
        api_value.encode("utf-8"),
        password.encode("utf-8"),
        recovery_code.encode("utf-8"),
        data_key,
        base64.b64encode(data_key),
        data_key.hex().encode("ascii"),
    )
    assert not any(value in persisted for value in prohibited)
    assert not any(
        value.decode("utf-8", errors="ignore") in caplog.text
        for value in prohibited
    )
    assert (tmp_path / "config.toml").read_text(encoding="utf-8").count("enc:") >= 1
    assert (tmp_path / "lockdown.key.json").exists()
    assert not (tmp_path / "transcript.key").exists()
