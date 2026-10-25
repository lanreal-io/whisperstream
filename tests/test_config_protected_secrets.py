"""Windows-neutral API-key persistence tests for the protected boundary."""

from __future__ import annotations

import concurrent.futures
import threading
import uuid
from pathlib import Path

import pytest

from whisperstream.config_manager import (
    PROTECTED_API_KEY_MARKER,
    ConfigManager,
    WhisperstreamConfig,
)
from whisperstream.protected_secret import UnavailableProtectedSecretStore
from whisperstream.protected_secret_contract import (
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
)


class _MemoryStore:
    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self._lock = threading.Lock()

    def availability(self):
        raise AssertionError("availability is not needed by persistence")

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        assert purpose is ProtectedSecretPurpose.API_KEY
        reference = f"keychain:v1:{uuid.uuid4()}"
        with self._lock:
            self.items[reference] = bytes(secret)
            self.created.append(reference)
        return reference

    def load(self, reference: str) -> bytes:
        with self._lock:
            try:
                return self.items[reference]
            except KeyError:
                raise ProtectedSecretError(
                    ProtectedSecretOperation.LOAD,
                    ProtectedSecretFailure.NOT_FOUND,
                ) from None

    def delete(self, reference: str) -> None:
        with self._lock:
            if reference not in self.items:
                raise ProtectedSecretError(
                    ProtectedSecretOperation.DELETE,
                    ProtectedSecretFailure.NOT_FOUND,
                )
            del self.items[reference]
            self.deleted.append(reference)


def _manager(tmp_path: Path, store) -> ConfigManager:
    return ConfigManager(
        config_path=tmp_path / "config.toml",
        models_root=tmp_path / "models",
        protected_secret_store=store,
    )


def test_save_uses_stable_shared_references_and_client_marker(tmp_path: Path) -> None:
    store = _MemoryStore()
    manager = _manager(tmp_path, store)
    config = WhisperstreamConfig(
        enhancement_provider="gemini",
        enhancement_api_key="synthetic-one",
        enhancement_gemini_api_key="synthetic-one",
        enhancement_openai_api_key="synthetic-two",
    )

    manager.save(config)
    first_text = manager.config_path.read_text(encoding="utf-8")
    first_refs = set(store.items)
    assert len(first_refs) == 2
    assert "synthetic-one" not in first_text
    assert "synthetic-two" not in first_text
    assert first_text.count("keychain:v1:") == 3

    client = manager.client_dict(config)
    assert client["enhancement_api_key"] == PROTECTED_API_KEY_MARKER
    assert client["enhancement_gemini_api_key"] == PROTECTED_API_KEY_MARKER
    assert client["enhancement_openai_api_key"] == PROTECTED_API_KEY_MARKER
    assert "keychain:v1:" not in repr(client)
    assert "synthetic-one" not in repr(client)

    manager.save(config)
    assert set(store.items) == first_refs
    assert store.created == list(first_refs) or set(store.created) == first_refs
    assert store.deleted == []

    restarted = _manager(tmp_path, store)
    loaded = restarted.load()
    assert loaded.enhancement_gemini_api_key == "synthetic-one"
    assert loaded.enhancement_openai_api_key == "synthetic-two"
    echoed = restarted.config_from_client_dict(
        {"enhancement_gemini_api_key": PROTECTED_API_KEY_MARKER},
        current=loaded,
    )
    assert echoed.enhancement_gemini_api_key == "synthetic-one"
    assert echoed.enhancement_openai_api_key == "synthetic-two"


def test_replace_and_remove_delete_only_orphaned_references(tmp_path: Path) -> None:
    store = _MemoryStore()
    manager = _manager(tmp_path, store)
    config = WhisperstreamConfig(
        enhancement_provider="gemini",
        enhancement_api_key="synthetic-old",
        enhancement_gemini_api_key="synthetic-old",
    )
    manager.save(config)
    old_reference = next(iter(store.items))

    config.enhancement_api_key = "synthetic-new"
    config.enhancement_gemini_api_key = "synthetic-new"
    manager.save(config)
    assert old_reference not in store.items
    assert store.deleted == [old_reference]
    assert len(store.items) == 1

    remaining = next(iter(store.items))
    config.enhancement_api_key = ""
    config.enhancement_gemini_api_key = ""
    manager.save(config)
    assert store.items == {}
    assert store.deleted[-1] == remaining


def test_provider_unavailable_fails_without_writing_config(tmp_path: Path) -> None:
    manager = _manager(tmp_path, UnavailableProtectedSecretStore())
    config = WhisperstreamConfig(
        enhancement_provider="gemini",
        enhancement_gemini_api_key="synthetic-unavailable",
    )

    with pytest.raises(ProtectedSecretError) as caught:
        manager.save(config)

    assert caught.value.operation is ProtectedSecretOperation.STORE
    assert caught.value.failure is ProtectedSecretFailure.UNAVAILABLE
    assert not manager.config_path.exists()


def test_failed_atomic_write_cleans_new_exact_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    manager = _manager(tmp_path, store)
    config = WhisperstreamConfig(
        enhancement_provider="gemini",
        enhancement_gemini_api_key="synthetic-write-failure",
    )

    def _fail_write(*_args, **_kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr("whisperstream.atomic_io.atomic_write_text", _fail_write)
    with pytest.raises(OSError):
        manager.save(config)

    assert not manager.config_path.exists()
    assert store.items == {}
    assert set(store.created) == set(store.deleted)


def test_concurrent_unchanged_saves_do_not_churn_items(tmp_path: Path) -> None:
    store = _MemoryStore()
    manager = _manager(tmp_path, store)
    config = WhisperstreamConfig(
        enhancement_provider="gemini",
        enhancement_api_key="synthetic-concurrent",
        enhancement_gemini_api_key="synthetic-concurrent",
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: manager.save(config), range(24)))

    assert len(store.items) == 1
    assert len(store.created) == 1
    assert store.deleted == []
