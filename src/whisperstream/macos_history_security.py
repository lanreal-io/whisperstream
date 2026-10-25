"""Darwin owners for history, licensing, and native file selections.

The existing AES-256-GCM transcript store, Keychain protected-secret adapter,
Lockdown envelopes, and license manager remain authoritative. Native panel
paths enter this module only through a permission-restricted registry written
by the Tauri owner. Shared frontend and REST state carry opaque tokens only.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

try:
    import fcntl
except ModuleNotFoundError:  # Windows collection of Darwin conformance tests
    fcntl = None  # type: ignore[assignment]

_LOCK_EX = fcntl.LOCK_EX if fcntl is not None else 0
_LOCK_UN = fcntl.LOCK_UN if fcntl is not None else 0

from .atomic_io import atomic_write_json
from .history_security_contract import (
    FileSelection,
    FileWorkflowAdapter,
    FileWorkflowKind,
    FileWorkflowResult,
    HistoryFailure,
    HistoryPolicy,
    HistoryRecordRef,
    HistoryResult,
    HistoryStoreAdapter,
    LicenseSnapshot,
    LicenseState,
    LicenseStateAdapter,
    LockdownModeState,
    SecurityAvailability,
    SecuritySnapshot,
)
from .protected_secret_contract import ProtectionAvailability


_DAY_MS = 86_400_000
_T = TypeVar("_T")

MACOS_HISTORY_POLICY = HistoryPolicy(
    cipher="AES-256-GCM",
    protected_key_required=True,
    zero_retention_means_forever=True,
    ordinary_delete_is_secure_erasure=False,
    max_import_files=100,
)


def _flock(descriptor: int, operation: int) -> None:
    if fcntl is None:
        return
    fcntl.flock(descriptor, operation)


@dataclass(frozen=True)
class MacosHistoryMutation:
    result: HistoryResult
    count: int

    def __post_init__(self) -> None:
        if self.count < 0 or self.result.changed != (self.count > 0):
            raise ValueError("history mutation count does not match result")


@dataclass(frozen=True)
class MacosFileBatch:
    failure: HistoryFailure
    selections: tuple[FileSelection, ...] = ()

    def __post_init__(self) -> None:
        if self.failure is HistoryFailure.NONE and not self.selections:
            raise ValueError("successful file batch requires selections")
        if self.failure is not HistoryFailure.NONE and self.selections:
            raise ValueError("failed file batch cannot expose selections")


@dataclass(frozen=True)
class _NativeSelection:
    path: str = field(repr=False)
    kind: FileWorkflowKind
    display_name: str


def native_selection_registry_path() -> Path:
    from .config_manager import get_app_dir

    return get_app_dir() / "macos-native-selections-v1.json"


class MacosNativeSelectionRegistry:
    """Atomically consume Tauri-owned native selections by opaque token."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else native_selection_registry_path()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock = threading.RLock()

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise OSError("native selection registry unavailable") from exc
        if payload.get("schema_version") != 1 or not isinstance(
            payload.get("selections"), dict
        ):
            raise OSError("native selection registry unavailable")
        return dict(payload["selections"])

    def _write(self, selections: dict[str, dict[str, object]]) -> None:
        if selections:
            atomic_write_json(
                self.path,
                {"schema_version": 1, "selections": selections},
            )
            self.path.chmod(0o600)
        else:
            self.path.unlink(missing_ok=True)

    def consume(
        self,
        token: str,
        *,
        expected_kind: FileWorkflowKind,
    ) -> _NativeSelection | None:
        if not token.startswith("macos-native:"):
            return None
        with self._lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                _flock(descriptor, _LOCK_EX)
                selections = self._load()
                raw = selections.get(token)
                if not isinstance(raw, dict) or raw.get("kind") != expected_kind.value:
                    return None
                path = raw.get("path")
                display_name = raw.get("display_name")
                if not isinstance(path, str) or not path or not isinstance(
                    display_name, str
                ) or not display_name:
                    return None
                selections.pop(token, None)
                self._write(selections)
                return _NativeSelection(path, expected_kind, display_name)
            finally:
                _flock(descriptor, _LOCK_UN)
                os.close(descriptor)

    def discard(self, tokens: Sequence[str]) -> None:
        with self._lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                _flock(descriptor, _LOCK_EX)
                selections = self._load()
                for token in tokens:
                    selections.pop(token, None)
                self._write(selections)
            finally:
                _flock(descriptor, _LOCK_UN)
                os.close(descriptor)

    def consume_many(
        self,
        tokens: Sequence[str],
        *,
        expected_kind: FileWorkflowKind,
    ) -> tuple[_NativeSelection, ...] | None:
        """Consume an entire batch or discard every supplied registry entry."""

        with self._lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                _flock(descriptor, _LOCK_EX)
                selections = self._load()
                prepared: list[_NativeSelection] = []
                valid = len(set(tokens)) == len(tokens)
                if valid:
                    for token in tokens:
                        raw = selections.get(token)
                        if (
                            not token.startswith("macos-native:")
                            or not isinstance(raw, dict)
                            or raw.get("kind") != expected_kind.value
                        ):
                            valid = False
                            break
                        path = raw.get("path")
                        display_name = raw.get("display_name")
                        if (
                            not isinstance(path, str)
                            or not path
                            or not isinstance(display_name, str)
                            or not display_name
                        ):
                            valid = False
                            break
                        prepared.append(
                            _NativeSelection(path, expected_kind, display_name)
                        )
                for token in tokens:
                    selections.pop(token, None)
                self._write(selections)
                return tuple(prepared) if valid else None
            finally:
                _flock(descriptor, _LOCK_UN)
                os.close(descriptor)


class MacosHistoryStoreAdapter:
    """Map the encrypted store and Keychain state into D-047."""

    def __init__(
        self,
        *,
        store_provider: Callable[[], Any],
        protected_store_provider: Callable[[], Any],
        lockdown_enabled: Callable[[], bool],
        lockdown_locked: Callable[[], bool],
    ) -> None:
        self._store_provider = store_provider
        self._protected_store_provider = protected_store_provider
        self._lockdown_enabled = lockdown_enabled
        self._lockdown_locked = lockdown_locked

    @property
    def policy(self) -> HistoryPolicy:
        return MACOS_HISTORY_POLICY

    def _protected_availability(self) -> SecurityAvailability:
        try:
            protected = self._protected_store_provider()
            if protected is None:
                return SecurityAvailability.UNAVAILABLE
            state = protected.availability()
        except Exception:
            return SecurityAvailability.UNAVAILABLE
        return (
            SecurityAvailability.READY
            if state is ProtectionAvailability.AVAILABLE
            else SecurityAvailability.UNAVAILABLE
        )

    def availability(self) -> SecurityAvailability:
        if self._lockdown_enabled() and self._lockdown_locked():
            return SecurityAvailability.LOCKED
        try:
            store = self._store_provider()
        except Exception:
            return SecurityAvailability.UNAVAILABLE
        if store is None:
            return SecurityAvailability.UNAVAILABLE
        if not bool(getattr(store, "decryptable", False)):
            if getattr(store, "key_failure", None) == "missing":
                return SecurityAvailability.KEY_MISSING
            return (
                SecurityAvailability.UNAVAILABLE
                if self._protected_availability() is SecurityAvailability.UNAVAILABLE
                else SecurityAvailability.KEY_ERROR
            )
        try:
            probe = getattr(store, "probe_content_integrity", None)
            if callable(probe) and not bool(probe()):
                return SecurityAvailability.NOT_DECRYPTABLE
            has_records = getattr(store, "has_records", None)
            populated = (
                bool(has_records())
                if callable(has_records)
                else bool(store.list(limit=1).get("items", []))
            )
        except Exception:
            return SecurityAvailability.UNAVAILABLE
        return SecurityAvailability.READY if populated else SecurityAvailability.EMPTY

    def security_snapshot(self) -> SecuritySnapshot:
        protected = self._protected_availability()
        history = self.availability()
        enabled = bool(self._lockdown_enabled())
        if not enabled:
            lockdown = LockdownModeState.DISABLED
        elif self._lockdown_locked():
            lockdown = LockdownModeState.LOCKED
        elif history in {SecurityAvailability.READY, SecurityAvailability.EMPTY}:
            lockdown = LockdownModeState.UNLOCKED
        elif history in {
            SecurityAvailability.KEY_ERROR,
            SecurityAvailability.KEY_MISSING,
            SecurityAvailability.NOT_DECRYPTABLE,
            SecurityAvailability.UNAVAILABLE,
        }:
            lockdown = LockdownModeState.RECOVERY_REQUIRED
        else:
            lockdown = LockdownModeState.UNAVAILABLE
        return SecuritySnapshot(
            protected_storage=protected,
            history=history,
            lockdown=lockdown,
            network_enhancement_allowed=not enabled,
            diagnostic_export_allowed=not enabled,
        )

    @staticmethod
    def _failure_for_availability(state: SecurityAvailability) -> HistoryFailure:
        if state is SecurityAvailability.LOCKED:
            return HistoryFailure.LOCKED
        if state in {
            SecurityAvailability.KEY_ERROR,
            SecurityAvailability.KEY_MISSING,
            SecurityAvailability.NOT_DECRYPTABLE,
            SecurityAvailability.UNAVAILABLE,
        }:
            return HistoryFailure.KEY_UNAVAILABLE
        return HistoryFailure.NONE

    def _ready_store(self) -> tuple[Any | None, HistoryFailure]:
        failure = self._failure_for_availability(self.availability())
        if failure is not HistoryFailure.NONE:
            return None, failure
        try:
            return self._store_provider(), HistoryFailure.NONE
        except Exception:
            return None, HistoryFailure.STORAGE

    def delete_with_count(self, record: HistoryRecordRef) -> MacosHistoryMutation:
        store, failure = self._ready_store()
        if failure is not HistoryFailure.NONE or store is None:
            return MacosHistoryMutation(HistoryResult(failure, False), 0)
        try:
            changed = bool(store.delete(record.record_id))
        except Exception:
            return MacosHistoryMutation(HistoryResult(HistoryFailure.STORAGE, False), 0)
        if not changed:
            return MacosHistoryMutation(HistoryResult(HistoryFailure.NOT_FOUND, False), 0)
        return MacosHistoryMutation(HistoryResult(HistoryFailure.NONE, True, record), 1)

    def delete(self, record: HistoryRecordRef) -> HistoryResult:
        return self.delete_with_count(record).result

    def clear_with_count(self) -> MacosHistoryMutation:
        store, failure = self._ready_store()
        if failure is not HistoryFailure.NONE or store is None:
            return MacosHistoryMutation(HistoryResult(failure, False), 0)
        try:
            count = int(store.clear())
        except Exception:
            return MacosHistoryMutation(HistoryResult(HistoryFailure.STORAGE, False), 0)
        return MacosHistoryMutation(HistoryResult(HistoryFailure.NONE, count > 0), count)

    def clear(self) -> HistoryResult:
        return self.clear_with_count().result

    def enforce_retention_with_count(self, retention_days: int) -> MacosHistoryMutation:
        if retention_days < 0:
            return MacosHistoryMutation(HistoryResult(HistoryFailure.INVALID_INPUT, False), 0)
        store, failure = self._ready_store()
        if failure is not HistoryFailure.NONE or store is None:
            return MacosHistoryMutation(HistoryResult(failure, False), 0)
        try:
            count = 0
            if retention_days > 0:
                cutoff_ms = int(time.time() * 1000) - retention_days * _DAY_MS
                count = int(store.purge_older_than(cutoff_ms))
            store.sweep_orphans()
        except Exception:
            return MacosHistoryMutation(HistoryResult(HistoryFailure.STORAGE, False), 0)
        return MacosHistoryMutation(HistoryResult(HistoryFailure.NONE, count > 0), count)

    def enforce_retention(self, retention_days: int) -> HistoryResult:
        return self.enforce_retention_with_count(retention_days).result


class MacosFileWorkflowAdapter:
    """Resolve one-shot native selections without exporting their paths."""

    def __init__(self, *, registry: MacosNativeSelectionRegistry | None = None) -> None:
        self._registry = registry or MacosNativeSelectionRegistry()
        self._lock = threading.RLock()
        self._selections: dict[str, _NativeSelection] = {}
        self._closed = False

    @property
    def active_selection_count(self) -> int:
        with self._lock:
            return len(self._selections)

    def register_tokens(
        self,
        kind: FileWorkflowKind,
        tokens: Sequence[str],
    ) -> MacosFileBatch:
        with self._lock:
            closed = self._closed
        if closed:
            if tokens:
                try:
                    self._registry.discard(tokens)
                except OSError:
                    pass
            return MacosFileBatch(HistoryFailure.UNSUPPORTED)
        if (
            not tokens
            or len(tokens) > MACOS_HISTORY_POLICY.max_import_files
            or len(set(tokens)) != len(tokens)
        ):
            if tokens:
                try:
                    self._registry.discard(tokens)
                except OSError:
                    pass
            return MacosFileBatch(HistoryFailure.INVALID_INPUT)
        try:
            native_batch = self._registry.consume_many(tokens, expected_kind=kind)
        except OSError:
            return MacosFileBatch(HistoryFailure.INVALID_INPUT)
        if native_batch is None:
            return MacosFileBatch(HistoryFailure.INVALID_INPUT)
        prepared = [
            (FileSelection(token, kind, native.display_name), native)
            for token, native in zip(tokens, native_batch, strict=True)
        ]
        with self._lock:
            if self._closed:
                return MacosFileBatch(HistoryFailure.UNSUPPORTED)
            for selection, native in prepared:
                self._selections[selection.selection_token] = native
        return MacosFileBatch(
            HistoryFailure.NONE,
            tuple(selection for selection, _native in prepared),
        )

    def _resolve(self, selection: FileSelection) -> _NativeSelection | None:
        with self._lock:
            native = self._selections.get(selection.selection_token)
        if native is None or native.kind is not selection.kind:
            return None
        return native

    async def choose(self, kind: FileWorkflowKind) -> FileWorkflowResult:
        del kind
        return FileWorkflowResult(HistoryFailure.UNSUPPORTED)

    async def read(self, selection: FileSelection) -> FileWorkflowResult:
        native = self._resolve(selection)
        if native is None:
            return FileWorkflowResult(HistoryFailure.NOT_FOUND)
        try:
            payload = Path(native.path).read_bytes()
        except OSError:
            return FileWorkflowResult(HistoryFailure.IO)
        return FileWorkflowResult(HistoryFailure.NONE, selection, payload)

    async def write(self, selection: FileSelection, payload: bytes) -> FileWorkflowResult:
        native = self._resolve(selection)
        if native is None:
            return FileWorkflowResult(HistoryFailure.NOT_FOUND)
        try:
            Path(native.path).write_bytes(bytes(payload))
        except OSError:
            return FileWorkflowResult(HistoryFailure.IO)
        return FileWorkflowResult(HistoryFailure.NONE, selection)

    def read_with(
        self,
        selection: FileSelection,
        reader: Callable[..., _T],
        *args: Any,
        **kwargs: Any,
    ) -> tuple[HistoryFailure, _T | None]:
        native = self._resolve(selection)
        if native is None:
            return HistoryFailure.NOT_FOUND, None
        try:
            return HistoryFailure.NONE, reader(native.path, *args, **kwargs)
        except FileNotFoundError:
            return HistoryFailure.NOT_FOUND, None
        except OSError:
            return HistoryFailure.IO, None
        except Exception:
            return HistoryFailure.INVALID_INPUT, None

    def release(self, selection: FileSelection) -> bool:
        with self._lock:
            return self._selections.pop(selection.selection_token, None) is not None

    def release_many(self, selections: Sequence[FileSelection]) -> None:
        with self._lock:
            for selection in selections:
                self._selections.pop(selection.selection_token, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._selections.clear()


class MacosLicenseStateAdapter:
    """Expose the unrestricted source entitlement through the shared contract."""

    def __init__(self, *, manager_provider: Callable[[], Any]) -> None:
        self._manager_provider = manager_provider

    def _manager(self) -> Any | None:
        try:
            return self._manager_provider()
        except Exception:
            return None

    def snapshot(self) -> LicenseSnapshot:
        """Describe unrestricted source use without a paid-license cache."""
        return LicenseSnapshot(
            state=LicenseState.PRO,
            cache_integrity_protected=False,
            cache_confidential=False,
            activation_available=False,
            offline_validation_available=False,
            deactivation_available=False,
        )

    def status(self) -> dict[str, Any]:
        manager = self._manager()
        if manager is None:
            return {"is_pro": False, "trial_remaining_seconds": 0.0}
        return manager.status()

    async def activate(self, key: str) -> dict[str, Any]:
        manager = self._manager()
        if manager is None:
            return {"success": False, "error": "License service unavailable."}
        return await manager.activate(key)

    async def deactivate(self) -> dict[str, Any]:
        manager = self._manager()
        if manager is None:
            return {"success": False, "error": "License service unavailable."}
        return await manager.deactivate()


assert isinstance(
    MacosHistoryStoreAdapter(
        store_provider=lambda: None,
        protected_store_provider=lambda: None,
        lockdown_enabled=lambda: False,
        lockdown_locked=lambda: False,
    ),
    HistoryStoreAdapter,
)
assert isinstance(MacosFileWorkflowAdapter(registry=MacosNativeSelectionRegistry()), FileWorkflowAdapter)
assert isinstance(MacosLicenseStateAdapter(manager_provider=lambda: None), LicenseStateAdapter)


__all__ = [
    "MACOS_HISTORY_POLICY",
    "MacosFileBatch",
    "MacosFileWorkflowAdapter",
    "MacosHistoryMutation",
    "MacosHistoryStoreAdapter",
    "MacosLicenseStateAdapter",
    "MacosNativeSelectionRegistry",
    "native_selection_registry_path",
]
