"""Windows owners for history/security/file schema v1.

The existing transcript, CurrentUser DPAPI, Lockdown, and license managers stay
authoritative.  This module maps their outcomes into the native-free contract
and retains paths, file handles, registry details, and protected references on
the Windows side of that boundary.
"""

from __future__ import annotations

import inspect
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from .history_security_contract import (
    WINDOWS_HISTORY_POLICY,
    FileSelection,
    FileWorkflowAdapter,
    FileWorkflowKind,
    FileWorkflowResult,
    HistoryFailure,
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
_MAX_WINDOWS_PATH_CHARS = 32_767
_T = TypeVar("_T")


@dataclass(frozen=True)
class WindowsHistoryMutation:
    result: HistoryResult
    count: int

    def __post_init__(self) -> None:
        if self.count < 0 or self.result.changed != (self.count > 0):
            raise ValueError("history mutation count does not match result")


@dataclass(frozen=True)
class WindowsFileBatch:
    failure: HistoryFailure
    selections: tuple[FileSelection, ...] = ()

    def __post_init__(self) -> None:
        if self.failure is HistoryFailure.NONE and not self.selections:
            raise ValueError("successful file batch requires selections")
        if self.failure is not HistoryFailure.NONE and self.selections:
            raise ValueError("failed file batch cannot expose selections")


@dataclass(frozen=True)
class _WindowsSelection:
    path: str = field(repr=False)
    kind: FileWorkflowKind


class WindowsHistoryStoreAdapter:
    """Map the canonical encrypted store into history/security schema v1."""

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
    def policy(self):
        return WINDOWS_HISTORY_POLICY

    def _protected_availability(self) -> SecurityAvailability:
        try:
            store = self._protected_store_provider()
            if store is None:
                return SecurityAvailability.UNAVAILABLE
            availability = store.availability()
        except Exception:
            return SecurityAvailability.UNAVAILABLE
        return (
            SecurityAvailability.READY
            if availability is ProtectionAvailability.AVAILABLE
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
            return SecurityAvailability.NOT_DECRYPTABLE
        try:
            probe = getattr(store, "probe_content_integrity", None)
            if callable(probe) and not bool(probe()):
                return SecurityAvailability.NOT_DECRYPTABLE
        except Exception:
            return SecurityAvailability.UNAVAILABLE
        return SecurityAvailability.READY

    def security_snapshot(self) -> SecuritySnapshot:
        protected = self._protected_availability()
        history = self.availability()
        enabled = bool(self._lockdown_enabled())
        if not enabled:
            lockdown = LockdownModeState.DISABLED
        elif self._lockdown_locked():
            lockdown = LockdownModeState.LOCKED
        elif history is SecurityAvailability.READY:
            lockdown = LockdownModeState.UNLOCKED
        elif history in {
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
    def _failure_for_availability(
        availability: SecurityAvailability,
    ) -> HistoryFailure:
        if availability is SecurityAvailability.LOCKED:
            return HistoryFailure.LOCKED
        if availability in {
            SecurityAvailability.KEY_MISSING,
            SecurityAvailability.NOT_DECRYPTABLE,
            SecurityAvailability.UNAVAILABLE,
        }:
            return HistoryFailure.KEY_UNAVAILABLE
        return HistoryFailure.NONE

    def _ready_store(self) -> tuple[Any | None, HistoryFailure]:
        availability = self.availability()
        failure = self._failure_for_availability(availability)
        if failure is not HistoryFailure.NONE:
            return None, failure
        try:
            return self._store_provider(), HistoryFailure.NONE
        except Exception:
            return None, HistoryFailure.STORAGE

    def delete_with_count(self, record: HistoryRecordRef) -> WindowsHistoryMutation:
        store, failure = self._ready_store()
        if failure is not HistoryFailure.NONE or store is None:
            return WindowsHistoryMutation(HistoryResult(failure, False), 0)
        try:
            changed = bool(store.delete(record.record_id))
        except Exception:
            return WindowsHistoryMutation(
                HistoryResult(HistoryFailure.STORAGE, False), 0
            )
        if not changed:
            return WindowsHistoryMutation(
                HistoryResult(HistoryFailure.NOT_FOUND, False), 0
            )
        return WindowsHistoryMutation(
            HistoryResult(HistoryFailure.NONE, True, record), 1
        )

    def delete(self, record: HistoryRecordRef) -> HistoryResult:
        return self.delete_with_count(record).result

    def clear_with_count(self) -> WindowsHistoryMutation:
        store, failure = self._ready_store()
        if failure is not HistoryFailure.NONE or store is None:
            return WindowsHistoryMutation(HistoryResult(failure, False), 0)
        try:
            count = int(store.clear())
        except Exception:
            return WindowsHistoryMutation(
                HistoryResult(HistoryFailure.STORAGE, False), 0
            )
        return WindowsHistoryMutation(
            HistoryResult(HistoryFailure.NONE, count > 0), count
        )

    def clear(self) -> HistoryResult:
        return self.clear_with_count().result

    def enforce_retention_with_count(
        self, retention_days: int
    ) -> WindowsHistoryMutation:
        if retention_days < 0:
            return WindowsHistoryMutation(
                HistoryResult(HistoryFailure.INVALID_INPUT, False), 0
            )
        store, failure = self._ready_store()
        if failure is not HistoryFailure.NONE or store is None:
            return WindowsHistoryMutation(HistoryResult(failure, False), 0)
        try:
            count = 0
            if retention_days > 0:
                cutoff_ms = int(time.time() * 1000) - retention_days * _DAY_MS
                count = int(store.purge_older_than(cutoff_ms))
            store.sweep_orphans()
        except Exception:
            return WindowsHistoryMutation(
                HistoryResult(HistoryFailure.STORAGE, False), 0
            )
        return WindowsHistoryMutation(
            HistoryResult(HistoryFailure.NONE, count > 0), count
        )

    def enforce_retention(self, retention_days: int) -> HistoryResult:
        return self.enforce_retention_with_count(retention_days).result


class WindowsLicenseStateAdapter:
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


class WindowsFileWorkflowAdapter:
    """Own Windows paths and expose only opaque, process-local selections."""

    def __init__(
        self,
        *,
        chooser: Callable[
            [FileWorkflowKind],
            str | Path | None | Awaitable[str | Path | None],
        ]
        | None = None,
    ) -> None:
        self._chooser = chooser
        self._lock = threading.RLock()
        self._selections: dict[str, _WindowsSelection] = {}
        self._closed = False

    @property
    def active_selection_count(self) -> int:
        with self._lock:
            return len(self._selections)

    @staticmethod
    def _display_name(path: Path) -> str:
        name = path.name.strip()
        return name or "selected-file"

    def register_paths(
        self,
        kind: FileWorkflowKind,
        paths: Sequence[str | Path],
    ) -> WindowsFileBatch:
        if self._closed or not paths or len(paths) > WINDOWS_HISTORY_POLICY.max_import_files:
            return WindowsFileBatch(HistoryFailure.INVALID_INPUT)
        prepared: list[tuple[FileSelection, _WindowsSelection]] = []
        for raw in paths:
            value = str(raw)
            if not value.strip() or len(value) > _MAX_WINDOWS_PATH_CHARS:
                return WindowsFileBatch(HistoryFailure.INVALID_INPUT)
            path = Path(value)
            token = f"windows-file:{uuid.uuid4()}"
            selection = FileSelection(token, kind, self._display_name(path))
            # Preserve the exact picker spelling for canonical readers.  Path
            # normalization can change slash form even though the selected
            # Windows object is the same.
            prepared.append((selection, _WindowsSelection(value, kind)))
        with self._lock:
            if self._closed:
                return WindowsFileBatch(HistoryFailure.UNSUPPORTED)
            for selection, native in prepared:
                self._selections[selection.selection_token] = native
        return WindowsFileBatch(
            HistoryFailure.NONE, tuple(selection for selection, _ in prepared)
        )

    def _resolve(self, selection: FileSelection) -> _WindowsSelection | None:
        with self._lock:
            native = self._selections.get(selection.selection_token)
        if native is None or native.kind is not selection.kind:
            return None
        return native

    async def choose(self, kind: FileWorkflowKind) -> FileWorkflowResult:
        if self._chooser is None or self._closed:
            return FileWorkflowResult(HistoryFailure.UNSUPPORTED)
        try:
            selected = self._chooser(kind)
            if inspect.isawaitable(selected):
                selected = await selected
        except Exception:
            return FileWorkflowResult(HistoryFailure.IO)
        if selected is None:
            return FileWorkflowResult(HistoryFailure.CANCELLED)
        batch = self.register_paths(kind, [selected])
        if batch.failure is not HistoryFailure.NONE:
            return FileWorkflowResult(batch.failure)
        return FileWorkflowResult(HistoryFailure.NONE, batch.selections[0])

    async def read(self, selection: FileSelection) -> FileWorkflowResult:
        native = self._resolve(selection)
        if native is None:
            return FileWorkflowResult(HistoryFailure.NOT_FOUND)
        try:
            payload = Path(native.path).read_bytes()
        except OSError:
            return FileWorkflowResult(HistoryFailure.IO)
        return FileWorkflowResult(HistoryFailure.NONE, selection, payload)

    async def write(
        self, selection: FileSelection, payload: bytes
    ) -> FileWorkflowResult:
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
        """Invoke an existing Windows reader without exporting its native path."""

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


assert isinstance(
    WindowsHistoryStoreAdapter(
        store_provider=lambda: None,
        protected_store_provider=lambda: None,
        lockdown_enabled=lambda: False,
        lockdown_locked=lambda: False,
    ),
    HistoryStoreAdapter,
)
assert isinstance(
    WindowsFileWorkflowAdapter(), FileWorkflowAdapter
)
assert isinstance(
    WindowsLicenseStateAdapter(manager_provider=lambda: None), LicenseStateAdapter
)


__all__ = [
    "WindowsFileBatch",
    "WindowsFileWorkflowAdapter",
    "WindowsHistoryMutation",
    "WindowsHistoryStoreAdapter",
    "WindowsLicenseStateAdapter",
]
