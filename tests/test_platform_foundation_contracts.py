from __future__ import annotations

import ast
import json
import os
import platform
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from whisperstream.key_encryption import decrypt_api_key, encrypt_api_key
from whisperstream.permission_contract import (
    PERMISSION_SCHEMA_VERSION,
    PermissionKind,
    PermissionReport,
    PermissionRequester,
    PermissionSnapshot,
    PermissionState,
    PermissionStatusProvider,
)
from whisperstream.protected_secret_contract import (
    MACOS_INTERNAL_KEYCHAIN_SERVICE,
    MACOS_KEYCHAIN_PREFIX,
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretProvider,
    ProtectedSecretPurpose,
    ProtectedSecretStore,
    macos_keychain_account,
    macos_keychain_service,
    parse_protected_secret_reference,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "tests/fixtures/platform-config-contract-v1.json"
TAURI_CONFIG_PATH = REPO_ROOT / "src-tauri/tauri.conf.json"
TAURI_WINDOWS_CONFIG_PATH = REPO_ROOT / "src-tauri/tauri.windows.conf.json"
TAURI_MACOS_CONFIG_PATH = REPO_ROOT / "src-tauri/tauri.macos.conf.json"


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _merge_config(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Requires the native Windows owner")
def test_platform_config_contract_accounts_for_current_windows_bundle() -> None:
    contract = _load_json(CONTRACT_PATH)
    base = _load_json(TAURI_CONFIG_PATH)
    windows_overlay = _load_json(TAURI_WINDOWS_CONFIG_PATH)
    config = _merge_config(base, windows_overlay)
    shared = contract["shared"]
    windows = contract["platforms"]["windows"]
    bundle = config["bundle"]

    assert platform.system() == "Windows"
    assert config["productName"] == shared["product_name"]
    assert config["identifier"] == windows["identifier"]
    assert bundle["targets"] == windows["bundle_targets"]
    # The official build's overlay supplies updater artifacts and the feed.
    assert "createUpdaterArtifacts" not in bundle
    assert bundle["externalBin"] == windows["external_bin"]
    assert bundle["licenseFile"] == shared["license_file"]
    assert bundle["icon"] == shared["icons"]
    assert bundle["resources"] == shared["resources"] + windows["resources"]
    assert "nsis" in bundle["windows"]
    assert "plugins" not in config
    assert "identifier" not in base
    assert "externalBin" not in base["bundle"]
    assert base["bundle"]["resources"] == shared["resources"]


def test_macos_overlay_contract_excludes_windows_artifacts_and_inerts_updater() -> None:
    contract = _load_json(CONTRACT_PATH)
    macos = contract["platforms"]["macos_internal"]
    base = _load_json(TAURI_CONFIG_PATH)
    overlay = _load_json(TAURI_MACOS_CONFIG_PATH)
    config = _merge_config(base, overlay)
    staged = macos["staged_external_bin"]

    assert macos["identifier"] != contract["platforms"]["windows"]["identifier"]
    assert macos["bundle_targets"] == ["app"]
    assert macos["create_updater_artifacts"] is False
    assert set(macos["external_bin"]) == {
        "binaries/whisperstream-engine",
        "binaries/whisperstream-macos-stt-worker",
        "binaries/llama-server",
    }
    assert all(name.endswith("-aarch64-apple-darwin") for name in staged)
    assert not any(name.endswith((".exe", ".dll")) for name in staged)
    assert macos["forbidden_bundle_keys"] == ["bundle.windows"]
    assert config["identifier"] == macos["identifier"]
    assert config["bundle"]["targets"] == macos["bundle_targets"]
    assert config["bundle"]["externalBin"] == macos["external_bin"]
    assert config["bundle"]["icon"] == macos["icons"]
    assert config["bundle"]["macOS"] == {
        "minimumSystemVersion": macos["minimum_system_version"],
        "signingIdentity": macos["signing_identity"],
        "hardenedRuntime": macos["hardened_runtime"],
        "infoPlist": macos["info_plist"],
    }
    assert config["bundle"]["resources"] == macos["resources"]
    assert macos["resources"] == [
        *contract["shared"]["resources"],
        "../NOTICE",
        "../LICENSES/Chirp-MIT.txt",
        "resources/llama-cpp/*",
    ]
    # The same upstream notices the Windows installer ships.
    windows_notices = [r for r in contract["platforms"]["windows"]["resources"] if not r.startswith("binaries/")]
    assert windows_notices == ["../NOTICE", "../LICENSES/Chirp-MIT.txt"]
    assert macos["bundle_resources_dir"] == "Contents/Resources/resources"
    assert macos["engine_resources_env"] == "WHISPERSTREAM_BUNDLE_RESOURCES_DIR"
    assert "windows" not in config["bundle"]
    assert config["plugins"]["updater"] == {"endpoints": [], "pubkey": ""}


def test_shared_contract_modules_have_no_platform_native_imports() -> None:
    paths = (
        REPO_ROOT / "src/whisperstream/protected_secret_contract.py",
        REPO_ROOT / "src/whisperstream/protected_secret.py",
        REPO_ROOT / "src/whisperstream/key_encryption.py",
        REPO_ROOT / "src/whisperstream/config_manager.py",
        REPO_ROOT / "src/whisperstream/transcript_crypto.py",
        REPO_ROOT / "src/whisperstream/transcript_store.py",
        REPO_ROOT / "src/whisperstream/lockdown_crypto.py",
        REPO_ROOT / "src/whisperstream/lockdown_keystore.py",
        REPO_ROOT / "src/whisperstream/lockdown_state.py",
        REPO_ROOT / "src/whisperstream/permission_contract.py",
        REPO_ROOT / "src/whisperstream/permission_status.py",
    )
    forbidden = {
        "AppKit",
        "AVFoundation",
        "CoreGraphics",
        "Quartz",
        "Security",
        "sounddevice",
        "pynput",
        "win32api",
        "win32crypt",
        "win32gui",
    }
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        assert imports.isdisjoint(forbidden), f"native import in shared contract {path}"

    probe = (
        "import json, sys; "
        "import whisperstream.protected_secret_contract; "
        "import whisperstream.protected_secret; "
        "import whisperstream.key_encryption; "
        "import whisperstream.config_manager; "
        "import whisperstream.transcript_crypto; "
        "import whisperstream.transcript_store; "
        "import whisperstream.lockdown_crypto; "
        "import whisperstream.lockdown_keystore; "
        "import whisperstream.lockdown_state; "
        "import whisperstream.permission_contract; "
        "import whisperstream.permission_status; "
        f"print(json.dumps(sorted(set(sys.modules) & {forbidden!r})))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(completed.stdout) == []


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Requires the native Windows owner")
def test_existing_windows_dpapi_reference_conforms_without_migration() -> None:
    plaintext = secrets.token_urlsafe(32)
    reference = encrypt_api_key(plaintext)
    parsed = parse_protected_secret_reference(reference)

    assert reference.startswith("enc:")
    assert parsed.provider is ProtectedSecretProvider.WINDOWS_DPAPI_CURRENT_USER_V1
    assert parsed.opaque_id == reference
    assert decrypt_api_key(reference) == plaintext


def test_protected_secret_reference_and_failure_vocabulary_is_fail_closed() -> None:
    opaque_id = str(uuid.uuid4())
    reference = f"{MACOS_KEYCHAIN_PREFIX}{opaque_id}"
    parsed = parse_protected_secret_reference(reference)
    assert parsed == (parsed.__class__)(
        ProtectedSecretProvider.MACOS_KEYCHAIN_GENERIC_PASSWORD_V1,
        opaque_id,
    )
    assert macos_keychain_account(opaque_id) == f"secret.v1.{opaque_id}"
    assert macos_keychain_service() == MACOS_INTERNAL_KEYCHAIN_SERVICE

    namespace = str(uuid.uuid4())
    assert macos_keychain_service(test_namespace=namespace).endswith(
        f".test.{namespace}"
    )
    for invalid in ("", "plaintext", "enc:", "enc:not-base64", "keychain:v1:nope"):
        with pytest.raises(ProtectedSecretError) as caught:
            parse_protected_secret_reference(invalid)
        assert caught.value.operation is ProtectedSecretOperation.LOAD
        assert caught.value.failure is ProtectedSecretFailure.INVALID_REFERENCE
        if invalid:
            assert invalid not in str(caught.value)
    assert set(ProtectedSecretFailure) == {
        ProtectedSecretFailure.UNAVAILABLE,
        ProtectedSecretFailure.NOT_FOUND,
        ProtectedSecretFailure.ACCESS_DENIED,
        ProtectedSecretFailure.INVALID_REFERENCE,
        ProtectedSecretFailure.CORRUPT,
        ProtectedSecretFailure.BACKEND_FAILURE,
    }


class _MemoryProtectedStore:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.items: dict[str, bytes] = {}

    def availability(self) -> ProtectionAvailability:
        if self.available:
            return ProtectionAvailability.AVAILABLE
        return ProtectionAvailability.UNAVAILABLE

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        if not self.available:
            raise ProtectedSecretError(
                ProtectedSecretOperation.STORE,
                ProtectedSecretFailure.UNAVAILABLE,
            )
        reference = f"{MACOS_KEYCHAIN_PREFIX}{uuid.uuid4()}"
        self.items[reference] = bytes(secret)
        return reference

    def load(self, reference: str) -> bytes:
        parse_protected_secret_reference(reference)
        if not self.available:
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD,
                ProtectedSecretFailure.UNAVAILABLE,
            )
        try:
            return self.items[reference]
        except KeyError as error:
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD,
                ProtectedSecretFailure.NOT_FOUND,
            ) from error

    def delete(self, reference: str) -> None:
        parse_protected_secret_reference(reference)
        self.items.pop(reference, None)


def test_protected_secret_store_contract_round_trip_and_unavailable_failure() -> None:
    store = _MemoryProtectedStore()
    assert isinstance(store, ProtectedSecretStore)
    secret = os.urandom(32)
    reference = store.store(secret, purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY)
    assert reference != secret.hex()
    assert store.load(reference) == secret
    store.delete(reference)
    with pytest.raises(ProtectedSecretError) as missing:
        store.load(reference)
    assert missing.value.failure is ProtectedSecretFailure.NOT_FOUND

    unavailable = _MemoryProtectedStore(available=False)
    with pytest.raises(ProtectedSecretError) as failure:
        unavailable.store(os.urandom(16), purpose=ProtectedSecretPurpose.API_KEY)
    assert failure.value.failure is ProtectedSecretFailure.UNAVAILABLE
    assert unavailable.items == {}


def test_permission_schema_is_complete_content_free_and_stable() -> None:
    report = PermissionReport(
        platform="windows",
        permissions=(
            PermissionSnapshot(
                PermissionKind.MICROPHONE,
                PermissionState.UNKNOWN,
                query_supported=False,
                request_supported=False,
            ),
            PermissionSnapshot(
                PermissionKind.ACCESSIBILITY,
                PermissionState.NOT_REQUIRED,
                query_supported=True,
                request_supported=False,
            ),
            PermissionSnapshot(
                PermissionKind.INPUT_MONITORING,
                PermissionState.NOT_REQUIRED,
                query_supported=True,
                request_supported=False,
            ),
        ),
    )
    assert report.as_dict() == {
        "schema_version": PERMISSION_SCHEMA_VERSION,
        "platform": "windows",
        "permissions": [item.as_dict() for item in report.permissions],
    }
    assert set(PermissionState) == {
        PermissionState.NOT_DETERMINED,
        PermissionState.GRANTED,
        PermissionState.DENIED,
        PermissionState.RESTRICTED,
        PermissionState.NOT_REQUIRED,
        PermissionState.UNSUPPORTED,
        PermissionState.UNKNOWN,
    }
    status_methods = {
        name for name in PermissionStatusProvider.__dict__ if not name.startswith("_")
    }
    request_methods = {
        name for name in PermissionRequester.__dict__ if not name.startswith("_")
    }
    assert status_methods == {"query_permissions"}
    assert request_methods == {"request_permission"}


def test_permission_schema_rejects_missing_or_duplicate_capabilities() -> None:
    one = PermissionSnapshot(
        PermissionKind.MICROPHONE,
        PermissionState.UNKNOWN,
        query_supported=False,
        request_supported=False,
    )
    with pytest.raises(ValueError, match="every permission kind"):
        PermissionReport(platform="windows", permissions=(one,))
    with pytest.raises(ValueError, match="unique"):
        PermissionReport(platform="windows", permissions=(one, one, one))
