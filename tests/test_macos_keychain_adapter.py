"""macOS Keychain adapter contract, failure, and real-provider tests."""

from __future__ import annotations

import ast
import concurrent.futures
import json
import platform
import secrets
import threading
import uuid
from pathlib import Path

import pytest

import whisperstream.macos_protected_secret as adapter
from whisperstream.macos_keychain_probe import run_probe
from whisperstream.protected_secret_contract import (
    MACOS_INTERNAL_KEYCHAIN_SERVICE,
    MACOS_KEYCHAIN_PREFIX,
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
    macos_keychain_service,
    parse_protected_secret_reference,
)


class _FakeBridge:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], bytes] = {}
        self.labels: dict[tuple[str, str], str] = {}
        self.add_statuses: list[int] = []
        self.copy_override: tuple[int, object | None] | None = None
        self.raise_calls = False
        self._lock = threading.Lock()

    def copy(
        self,
        service: str,
        account: str,
        *,
        return_data: bool,
    ) -> tuple[int, object | None]:
        if self.raise_calls:
            raise RuntimeError("synthetic native failure")
        if self.copy_override is not None and return_data:
            return self.copy_override
        with self._lock:
            value = self.items.get((service, account))
        if value is None:
            return adapter._ERR_SEC_ITEM_NOT_FOUND, None
        return adapter._ERR_SEC_SUCCESS, value if return_data else None

    def add(self, service: str, account: str, secret: bytes, *, label: str) -> int:
        if self.raise_calls:
            raise RuntimeError("synthetic native failure")
        with self._lock:
            if self.add_statuses:
                status = self.add_statuses.pop(0)
                if status != adapter._ERR_SEC_SUCCESS:
                    return status
            key = (service, account)
            if key in self.items:
                return adapter._ERR_SEC_DUPLICATE_ITEM
            self.items[key] = bytes(secret)
            self.labels[key] = label
        return adapter._ERR_SEC_SUCCESS

    def update(self, service: str, account: str, secret: bytes) -> int:
        if self.raise_calls:
            raise RuntimeError("synthetic native failure")
        with self._lock:
            key = (service, account)
            if key not in self.items:
                return adapter._ERR_SEC_ITEM_NOT_FOUND
            self.items[key] = bytes(secret)
        return adapter._ERR_SEC_SUCCESS

    def set_label(self, service: str, account: str, label: str) -> int:
        with self._lock:
            if (service, account) not in self.items:
                return adapter._ERR_SEC_ITEM_NOT_FOUND
            self.labels[(service, account)] = label
        return adapter._ERR_SEC_SUCCESS

    def delete(self, service: str, account: str) -> int:
        if self.raise_calls:
            raise RuntimeError("synthetic native failure")
        with self._lock:
            key = (service, account)
            if key not in self.items:
                return adapter._ERR_SEC_ITEM_NOT_FOUND
            del self.items[key]
        return adapter._ERR_SEC_SUCCESS


def _store(bridge: _FakeBridge) -> adapter.MacosKeychainProtectedSecretStore:
    return adapter.MacosKeychainProtectedSecretStore(
        test_namespace=str(uuid.uuid4()),
        bridge=bridge,
    )


def _failure(
    call,
    expected_operation: ProtectedSecretOperation,
    expected_failure: ProtectedSecretFailure,
) -> None:
    with pytest.raises(ProtectedSecretError) as caught:
        call()
    assert caught.value.operation is expected_operation
    assert caught.value.failure is expected_failure


def test_fake_round_trip_update_delete_and_missing_are_typed() -> None:
    bridge = _FakeBridge()
    store = _store(bridge)
    first = secrets.token_bytes(32)
    second = secrets.token_bytes(32)

    assert store.availability() is ProtectionAvailability.AVAILABLE
    reference = store.store(first, purpose=ProtectedSecretPurpose.API_KEY)
    assert reference.startswith(MACOS_KEYCHAIN_PREFIX)
    assert first.hex() not in reference
    assert store.contains(reference)
    assert secrets.compare_digest(store.load(reference), first)

    store.update(reference, second, purpose=ProtectedSecretPurpose.API_KEY)
    assert secrets.compare_digest(store.load(reference), second)
    store.delete(reference)
    assert not store.contains(reference)
    _failure(
        lambda: store.load(reference),
        ProtectedSecretOperation.LOAD,
        ProtectedSecretFailure.NOT_FOUND,
    )
    _failure(
        lambda: store.delete(reference),
        ProtectedSecretOperation.DELETE,
        ProtectedSecretFailure.NOT_FOUND,
    )
    _failure(
        lambda: store.update(
            reference,
            secrets.token_bytes(32),
            purpose=ProtectedSecretPurpose.API_KEY,
        ),
        ProtectedSecretOperation.STORE,
        ProtectedSecretFailure.NOT_FOUND,
    )


def test_duplicate_retry_and_concurrent_stores_keep_exact_unique_items() -> None:
    bridge = _FakeBridge()
    bridge.add_statuses = [
        adapter._ERR_SEC_DUPLICATE_ITEM,
        adapter._ERR_SEC_DUPLICATE_ITEM,
    ]
    store = _store(bridge)
    reference = store.store(
        secrets.token_bytes(24),
        purpose=ProtectedSecretPurpose.API_KEY,
    )
    assert store.contains(reference)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        references = list(
            executor.map(
                lambda _index: store.store(
                    secrets.token_bytes(24),
                    purpose=ProtectedSecretPurpose.API_KEY,
                ),
                range(24),
            )
        )
    assert len(set(references)) == 24
    assert len(bridge.items) == 25

    for item_reference in [reference, *references]:
        store.delete(item_reference)
    assert bridge.items == {}


def test_duplicate_retry_exhaustion_fails_without_deleting_existing_items() -> None:
    bridge = _FakeBridge()
    bridge.add_statuses = [adapter._ERR_SEC_DUPLICATE_ITEM] * 4
    store = _store(bridge)

    _failure(
        lambda: store.store(
            secrets.token_bytes(16),
            purpose=ProtectedSecretPurpose.API_KEY,
        ),
        ProtectedSecretOperation.STORE,
        ProtectedSecretFailure.BACKEND_FAILURE,
    )
    assert bridge.items == {}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (adapter._ERR_SEC_ITEM_NOT_FOUND, ProtectedSecretFailure.NOT_FOUND),
        (adapter._ERR_SEC_AUTH_FAILED, ProtectedSecretFailure.ACCESS_DENIED),
        (
            adapter._ERR_SEC_INTERACTION_NOT_ALLOWED,
            ProtectedSecretFailure.ACCESS_DENIED,
        ),
        (adapter._ERR_SEC_NOT_AVAILABLE, ProtectedSecretFailure.UNAVAILABLE),
        (adapter._ERR_SEC_MISSING_ENTITLEMENT, ProtectedSecretFailure.UNAVAILABLE),
        (adapter._ERR_SEC_DECODE, ProtectedSecretFailure.CORRUPT),
        (adapter._ERR_SEC_PARAM, ProtectedSecretFailure.BACKEND_FAILURE),
        (adapter._ERR_SEC_DUPLICATE_ITEM, ProtectedSecretFailure.BACKEND_FAILURE),
    ],
)
def test_native_status_mapping_is_content_free(
    status: int,
    expected: ProtectedSecretFailure,
) -> None:
    failure = adapter._failure_for_status(
        status,
        operation=ProtectedSecretOperation.LOAD,
    )
    assert failure is expected
    error = ProtectedSecretError(ProtectedSecretOperation.LOAD, failure)
    assert str(status) not in str(error)


def test_unavailable_corrupt_unexpected_and_native_exception_fail_closed() -> None:
    unavailable_bridge = _FakeBridge()
    unavailable_bridge.copy_override = None
    unavailable_bridge.raise_calls = True
    unavailable = _store(unavailable_bridge)
    assert unavailable.availability() is ProtectionAvailability.UNAVAILABLE
    _failure(
        lambda: unavailable.store(
            secrets.token_bytes(16),
            purpose=ProtectedSecretPurpose.API_KEY,
        ),
        ProtectedSecretOperation.STORE,
        ProtectedSecretFailure.UNAVAILABLE,
    )

    corrupt_bridge = _FakeBridge()
    corrupt = _store(corrupt_bridge)
    reference = corrupt.store(
        secrets.token_bytes(16),
        purpose=ProtectedSecretPurpose.API_KEY,
    )
    corrupt_bridge.copy_override = (adapter._ERR_SEC_SUCCESS, object())
    _failure(
        lambda: corrupt.load(reference),
        ProtectedSecretOperation.LOAD,
        ProtectedSecretFailure.CORRUPT,
    )

    native_error_bridge = _FakeBridge()
    native_error_store = _store(native_error_bridge)
    native_reference = native_error_store.store(
        secrets.token_bytes(16),
        purpose=ProtectedSecretPurpose.API_KEY,
    )
    native_error_bridge.raise_calls = True
    _failure(
        lambda: native_error_store.load(native_reference),
        ProtectedSecretOperation.LOAD,
        ProtectedSecretFailure.UNAVAILABLE,
    )


def test_invalid_and_foreign_references_are_rejected_for_each_operation() -> None:
    store = _store(_FakeBridge())
    for reference in ("plaintext", "enc:YWJj", "keychain:v1:not-a-uuid"):
        _failure(
            lambda reference=reference: store.load(reference),
            ProtectedSecretOperation.LOAD,
            ProtectedSecretFailure.INVALID_REFERENCE,
        )
        _failure(
            lambda reference=reference: store.delete(reference),
            ProtectedSecretOperation.DELETE,
            ProtectedSecretFailure.INVALID_REFERENCE,
        )


def test_production_service_is_independent_of_bundle_identifier() -> None:
    # Existing API keys and transcript data keys live under this exact service;
    # a bundle-identifier change must not rename it.
    assert MACOS_INTERNAL_KEYCHAIN_SERVICE == (
        "com.whisperstream.app.macos-internal.protected-secrets"
    )


def test_phase_namespace_never_uses_production_service() -> None:
    namespace = str(uuid.uuid4())
    service = macos_keychain_service(test_namespace=namespace)

    assert service == f"{MACOS_INTERNAL_KEYCHAIN_SERVICE}.test.{namespace}"
    assert service != MACOS_INTERNAL_KEYCHAIN_SERVICE
    assert parse_protected_secret_reference(
        f"{MACOS_KEYCHAIN_PREFIX}{uuid.uuid4()}"
    ).opaque_id


@pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="native selector requires Apple Silicon macOS",
)
@pytest.mark.skip(
    reason="real Keychain access is manual-only; use the isolated protected-storage probe",
)
def test_lazy_darwin_selector_returns_available_real_adapter() -> None:
    from whisperstream.protected_secret import get_protected_secret_store

    store = get_protected_secret_store()
    assert isinstance(store, adapter.MacosKeychainProtectedSecretStore)
    assert store.service == MACOS_INTERNAL_KEYCHAIN_SERVICE
    assert store.availability() is ProtectionAvailability.AVAILABLE


def test_pytest_guard_blocks_native_keychain_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.protected_secret import get_protected_secret_store
    import whisperstream.protected_secret as protected_secret

    monkeypatch.setattr(protected_secret.platform, "system", lambda: "Darwin")

    store = get_protected_secret_store()
    assert isinstance(store, adapter.MacosKeychainProtectedSecretStore)
    assert store.availability() is ProtectionAvailability.UNAVAILABLE


def test_adapter_and_probe_have_no_plaintext_process_or_log_channel() -> None:
    root = Path(__file__).resolve().parents[1]
    adapter_source = (root / "src/whisperstream/macos_protected_secret.py").read_text(
        encoding="utf-8"
    )
    probe_source = (root / "src/whisperstream/macos_keychain_probe.py").read_text(
        encoding="utf-8"
    )
    builder_source = (root / "scripts/build_macos_internal.py").read_text(
        encoding="utf-8"
    )
    imports: set[str] = set()
    for node in ast.walk(ast.parse(adapter_source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])

    assert imports.isdisjoint({"logging", "subprocess"})
    assert "os.environ" not in adapter_source
    assert "/usr/bin/security" not in adapter_source + probe_source
    assert '"security"' not in adapter_source + probe_source
    assert "--include-module=whisperstream.macos_protected_secret" in builder_source
    assert "--include-module=whisperstream.macos_keychain_probe" in builder_source


@pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="real user Keychain requires native Apple Silicon macOS",
)
@pytest.mark.skip(
    reason="real Keychain access is manual-only; use the isolated protected-storage probe",
)
def test_real_synthetic_keychain_round_trip_update_delete_cleanup(tmp_path: Path) -> None:
    namespace = str(uuid.uuid4())
    output = tmp_path / "real-keychain-report.json"
    payload = run_probe(output=output, test_namespace=namespace)

    assert payload["verdict"] == "PASS"
    assert payload["service"] == macos_keychain_service(test_namespace=namespace)
    assert payload["stages"] == [
        "availability",
        "store",
        "load",
        "update",
        "delete",
        "missing",
    ]
    assert payload["cleanup_verified"] is True
    assert payload["secret_material_recorded"] is False
    assert payload["authentication_ui_allowed"] is False
    assert json.loads(output.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize("purpose,label", [
    (ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY, "Whisperstream transcript encryption key"),
    (ProtectedSecretPurpose.API_KEY, "Whisperstream AI provider API key"),
])
def test_new_items_have_a_purpose_specific_label(purpose, label):
    bridge = _FakeBridge()
    store = _store(bridge)
    reference = store.store(b"synthetic", purpose=purpose)
    assert list(bridge.labels.values()) == [label]
    assert store.load(reference) == b"synthetic"


def test_user_interaction_scope_does_not_leak_to_background_threads():
    assert not adapter._ALLOW_INTERACTION.get()
    observed = []
    with adapter.allow_keychain_interaction():
        assert adapter._ALLOW_INTERACTION.get()
        thread = threading.Thread(target=lambda: observed.append(adapter._ALLOW_INTERACTION.get()))
        thread.start()
        thread.join()
    assert observed == [False]
    assert not adapter._ALLOW_INTERACTION.get()


def test_legacy_label_update_requires_user_action_and_preserves_secret():
    bridge = _FakeBridge()
    store = _store(bridge)
    ref = store.store(b"synthetic", purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY)
    key = next(iter(bridge.items))
    bridge.labels[key] = "old service label"
    store.label_for_purpose(ref, ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY)
    assert bridge.labels[key] == "old service label"
    with adapter.allow_keychain_interaction():
        store.label_for_purpose(ref, ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY)
    assert bridge.labels[key] == "Whisperstream transcript encryption key"
    assert store.load(ref) == b"synthetic"


def test_reserved_reference_is_created_only_by_store_reserved() -> None:
    # The cleanup ledger records this reference before the item exists, so a
    # crash between creation and publication can't orphan a data key copy.
    bridge = _FakeBridge()
    store = _store(bridge)
    reference = store.reserve_reference()
    parse_protected_secret_reference(reference)
    assert not store.contains(reference)

    secret = secrets.token_bytes(32)
    store.store_reserved(reference, secret, purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY)
    assert secrets.compare_digest(store.load(reference), secret)
    _failure(
        lambda: store.store_reserved(reference, secret, purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY),
        ProtectedSecretOperation.STORE,
        ProtectedSecretFailure.BACKEND_FAILURE,
    )
