"""Lazy platform selector for history, security, licensing, and file workflows."""

from __future__ import annotations

import platform
from collections.abc import Callable
from typing import Any

from .history_security_contract import (
    FileSelection,
    FileWorkflowAdapter,
    FileWorkflowKind,
    FileWorkflowResult,
    HistoryFailure,
    HistoryPolicy,
    HistoryRecordRef,
    HistoryResult,
    HistorySecurityFactory,
    HistoryStoreAdapter,
    LicenseSnapshot,
    LicenseState,
    LicenseStateAdapter,
    LockdownModeState,
    SecurityAvailability,
    SecuritySnapshot,
)


_UNAVAILABLE_POLICY = HistoryPolicy(
    cipher="AES-256-GCM",
    protected_key_required=True,
    zero_retention_means_forever=True,
    ordinary_delete_is_secure_erasure=False,
    max_import_files=100,
)


class UnavailableHistoryStoreAdapter:
    @property
    def policy(self) -> HistoryPolicy:
        return _UNAVAILABLE_POLICY

    def availability(self) -> SecurityAvailability:
        return SecurityAvailability.UNAVAILABLE

    def security_snapshot(self) -> SecuritySnapshot:
        return SecuritySnapshot(
            protected_storage=SecurityAvailability.UNAVAILABLE,
            history=SecurityAvailability.UNAVAILABLE,
            lockdown=LockdownModeState.UNAVAILABLE,
            network_enhancement_allowed=False,
            diagnostic_export_allowed=False,
        )

    def delete(self, record: HistoryRecordRef) -> HistoryResult:
        return HistoryResult(HistoryFailure.UNSUPPORTED, False, record=None)

    def clear(self) -> HistoryResult:
        return HistoryResult(HistoryFailure.UNSUPPORTED, False)

    def enforce_retention(self, retention_days: int) -> HistoryResult:
        del retention_days
        return HistoryResult(HistoryFailure.UNSUPPORTED, False)


class UnavailableFileWorkflowAdapter:
    async def choose(self, kind: FileWorkflowKind) -> FileWorkflowResult:
        del kind
        return FileWorkflowResult(HistoryFailure.UNSUPPORTED)

    async def read(self, selection: FileSelection) -> FileWorkflowResult:
        del selection
        return FileWorkflowResult(HistoryFailure.UNSUPPORTED)

    async def write(
        self, selection: FileSelection, payload: bytes
    ) -> FileWorkflowResult:
        del selection, payload
        return FileWorkflowResult(HistoryFailure.UNSUPPORTED)


class UnavailableLicenseStateAdapter:
    def snapshot(self) -> LicenseSnapshot:
        return LicenseSnapshot(
            state=LicenseState.UNAVAILABLE,
            cache_integrity_protected=False,
            cache_confidential=False,
            activation_available=False,
        )


class PlatformHistorySecurityFactory:
    """Select native owners without resolving a store, panel, or registry eagerly."""

    def __init__(
        self,
        *,
        history_store: Callable[[], Any],
        license_manager: Callable[[], Any],
        protected_store: Callable[[], Any],
        lockdown_enabled: Callable[[], bool],
        lockdown_locked: Callable[[], bool],
        system: str | None = None,
        machine: str | None = None,
        file_chooser: Callable[[FileWorkflowKind], Any] | None = None,
        native_selection_registry: Any | None = None,
    ) -> None:
        self._history_store = history_store
        self._license_manager = license_manager
        self._protected_store = protected_store
        self._lockdown_enabled = lockdown_enabled
        self._lockdown_locked = lockdown_locked
        self._system = system
        self._machine = machine
        self._file_chooser = file_chooser
        self._native_selection_registry = native_selection_registry
        self._history_adapter: HistoryStoreAdapter | None = None
        self._file_adapter: FileWorkflowAdapter | None = None
        self._license_adapter: LicenseStateAdapter | None = None

    def _is_windows_x64(self) -> bool:
        system = self._system or platform.system()
        machine = (self._machine or platform.machine()).lower()
        return system == "Windows" and machine in {"amd64", "x86_64"}

    def _is_darwin_arm64(self) -> bool:
        system = self._system or platform.system()
        machine = (self._machine or platform.machine()).lower()
        return system == "Darwin" and machine in {"arm64", "aarch64"}

    def history(self) -> HistoryStoreAdapter:
        if self._history_adapter is None:
            if self._is_windows_x64():
                from .windows_history_security import WindowsHistoryStoreAdapter

                self._history_adapter = WindowsHistoryStoreAdapter(
                    store_provider=self._history_store,
                    protected_store_provider=self._protected_store,
                    lockdown_enabled=self._lockdown_enabled,
                    lockdown_locked=self._lockdown_locked,
                )
            elif self._is_darwin_arm64():
                from .macos_history_security import MacosHistoryStoreAdapter

                self._history_adapter = MacosHistoryStoreAdapter(
                    store_provider=self._history_store,
                    protected_store_provider=self._protected_store,
                    lockdown_enabled=self._lockdown_enabled,
                    lockdown_locked=self._lockdown_locked,
                )
            else:
                self._history_adapter = UnavailableHistoryStoreAdapter()
        return self._history_adapter

    def files(self) -> FileWorkflowAdapter:
        if self._file_adapter is None:
            if self._is_windows_x64():
                from .windows_history_security import WindowsFileWorkflowAdapter

                self._file_adapter = WindowsFileWorkflowAdapter(
                    chooser=self._file_chooser
                )
            elif self._is_darwin_arm64():
                from .macos_history_security import MacosFileWorkflowAdapter

                self._file_adapter = MacosFileWorkflowAdapter(
                    registry=self._native_selection_registry,
                )
            else:
                self._file_adapter = UnavailableFileWorkflowAdapter()
        return self._file_adapter

    def licensing(self) -> LicenseStateAdapter:
        if self._license_adapter is None:
            if self._is_windows_x64():
                from .windows_history_security import WindowsLicenseStateAdapter

                self._license_adapter = WindowsLicenseStateAdapter(
                    manager_provider=self._license_manager
                )
            elif self._is_darwin_arm64():
                from .macos_history_security import MacosLicenseStateAdapter

                self._license_adapter = MacosLicenseStateAdapter(
                    manager_provider=self._license_manager
                )
            else:
                self._license_adapter = UnavailableLicenseStateAdapter()
        return self._license_adapter


assert isinstance(
    PlatformHistorySecurityFactory(
        history_store=lambda: None,
        license_manager=lambda: None,
        protected_store=lambda: None,
        lockdown_enabled=lambda: False,
        lockdown_locked=lambda: False,
    ),
    HistorySecurityFactory,
)


__all__ = [
    "PlatformHistorySecurityFactory",
    "UnavailableFileWorkflowAdapter",
    "UnavailableHistoryStoreAdapter",
    "UnavailableLicenseStateAdapter",
]
