"""Windows CurrentUser DPAPI protected-secret adapter.

This is the only protected-secret module that imports pywin32. Shared
contracts and consumers reach it through the lazy selector in
``protected_secret``.
"""
from __future__ import annotations

import base64
import binascii
import platform

from .protected_secret_contract import (
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretProvider,
    ProtectedSecretPurpose,
    WINDOWS_DPAPI_PREFIX,
    parse_protected_secret_reference,
)

try:
    import win32crypt  # type: ignore[import-untyped]

    _WIN32CRYPT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by isolated import probe
    win32crypt = None  # type: ignore[assignment]
    _WIN32CRYPT_AVAILABLE = False


def _native_error_code(error: BaseException) -> int | None:
    code = getattr(error, "winerror", None)
    if isinstance(code, int):
        return code
    if error.args and isinstance(error.args[0], int):
        return error.args[0]
    return None


def _failure_for_native_error(
    error: BaseException,
    *,
    operation: ProtectedSecretOperation,
) -> ProtectedSecretFailure:
    code = _native_error_code(error)
    if code == 5:
        return ProtectedSecretFailure.ACCESS_DENIED
    if code == 13 and operation is ProtectedSecretOperation.LOAD:
        # CurrentUser account mismatch and damaged DPAPI blobs both commonly
        # surface as ERROR_INVALID_DATA. Neither distinction is safe to invent.
        return ProtectedSecretFailure.CORRUPT
    if code in (50, 126, 127):
        return ProtectedSecretFailure.UNAVAILABLE
    return ProtectedSecretFailure.BACKEND_FAILURE


class WindowsDpapiProtectedSecretStore:
    """Protect bytes with Windows DPAPI in CurrentUser scope."""

    def availability(self) -> ProtectionAvailability:
        if platform.system() != "Windows" or not _WIN32CRYPT_AVAILABLE:
            return ProtectionAvailability.UNAVAILABLE
        return ProtectionAvailability.AVAILABLE

    def _require_available(self, operation: ProtectedSecretOperation) -> None:
        if self.availability() is ProtectionAvailability.UNAVAILABLE:
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.UNAVAILABLE,
            )

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        del purpose
        self._require_available(ProtectedSecretOperation.STORE)
        try:
            ciphertext: bytes = win32crypt.CryptProtectData(  # type: ignore[union-attr]
                bytes(secret), None, None, None, None, 0
            )
        except Exception as error:  # noqa: BLE001 - native adapter boundary
            raise ProtectedSecretError(
                ProtectedSecretOperation.STORE,
                _failure_for_native_error(
                    error,
                    operation=ProtectedSecretOperation.STORE,
                ),
            ) from None
        if not isinstance(ciphertext, bytes) or not ciphertext:
            raise ProtectedSecretError(
                ProtectedSecretOperation.STORE, ProtectedSecretFailure.BACKEND_FAILURE
            )
        return WINDOWS_DPAPI_PREFIX + base64.b64encode(ciphertext).decode("ascii")

    def load(self, reference: str) -> bytes:
        self._require_available(ProtectedSecretOperation.LOAD)
        try:
            parsed = parse_protected_secret_reference(reference)
        except ProtectedSecretError:
            raise
        if parsed.provider is not ProtectedSecretProvider.WINDOWS_DPAPI_CURRENT_USER_V1:
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD,
                ProtectedSecretFailure.INVALID_REFERENCE,
            )
        try:
            ciphertext = base64.b64decode(
                reference.removeprefix(WINDOWS_DPAPI_PREFIX),
                validate=True,
            )
        except (binascii.Error, ValueError):
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD,
                ProtectedSecretFailure.INVALID_REFERENCE,
            ) from None
        try:
            _, plaintext = win32crypt.CryptUnprotectData(  # type: ignore[union-attr]
                ciphertext, None, None, None, 0
            )
        except Exception as error:  # noqa: BLE001 - native adapter boundary
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD,
                _failure_for_native_error(
                    error,
                    operation=ProtectedSecretOperation.LOAD,
                ),
            ) from None
        return bytes(plaintext)

    def delete(self, reference: str) -> None:
        """Validate an inline DPAPI reference.

        DPAPI has no provider-side item to delete. The consumer owns deletion
        of the config field or key-reference file after this validation step.
        """

        try:
            parsed = parse_protected_secret_reference(reference)
        except ProtectedSecretError as error:
            raise ProtectedSecretError(
                ProtectedSecretOperation.DELETE,
                error.failure,
            ) from None
        if parsed.provider is not ProtectedSecretProvider.WINDOWS_DPAPI_CURRENT_USER_V1:
            raise ProtectedSecretError(
                ProtectedSecretOperation.DELETE,
                ProtectedSecretFailure.INVALID_REFERENCE,
            )


__all__ = ["WindowsDpapiProtectedSecretStore"]
