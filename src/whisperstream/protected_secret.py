"""Lazy platform selection for protected secret storage.

Shared consumers import this module without importing DPAPI, pywin32, or
future Keychain implementation state. The platform adapter is selected only
when a protected-storage operation or availability query is requested.
"""
from __future__ import annotations

import importlib
import platform

from .protected_secret_contract import (
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
    ProtectedSecretStore,
)


class UnavailableProtectedSecretStore:
    """Fail-closed adapter used when no native provider can be selected."""

    def availability(self) -> ProtectionAvailability:
        return ProtectionAvailability.UNAVAILABLE

    @staticmethod
    def _raise(operation: ProtectedSecretOperation) -> None:
        raise ProtectedSecretError(operation, ProtectedSecretFailure.UNAVAILABLE)

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        del secret, purpose
        self._raise(ProtectedSecretOperation.STORE)

    def load(self, reference: str) -> bytes:
        del reference
        self._raise(ProtectedSecretOperation.LOAD)

    def delete(self, reference: str) -> None:
        del reference
        self._raise(ProtectedSecretOperation.DELETE)


def get_protected_secret_store() -> ProtectedSecretStore:
    """Return the current platform adapter without eager native imports.

    Import failures and unsupported platforms select an explicit unavailable
    adapter. Callers still receive typed failures from store/load/delete and
    can query :meth:`availability` without risking plaintext persistence.
    """

    system = platform.system()
    module_name: str | None = None
    class_name: str | None = None
    if system == "Windows":
        module_name = ".windows_protected_secret"
        class_name = "WindowsDpapiProtectedSecretStore"
    elif system == "Darwin":
        # Keeping this import lazy lets the shared Windows tree and file-only
        # shared imports remain free of Security.framework state.
        module_name = ".macos_protected_secret"
        class_name = "MacosKeychainProtectedSecretStore"

    if module_name is None or class_name is None:
        return UnavailableProtectedSecretStore()

    try:
        module = importlib.import_module(module_name, package=__package__)
        store_type = getattr(module, class_name)
        store = store_type()
    except Exception:  # noqa: BLE001 - adapter construction is a trust boundary
        return UnavailableProtectedSecretStore()
    if not isinstance(store, ProtectedSecretStore):
        return UnavailableProtectedSecretStore()
    return store


__all__ = [
    "UnavailableProtectedSecretStore",
    "get_protected_secret_store",
]
