"""macOS generic-password Keychain protected-secret adapter.

The native bridge stays private to this module. Shared contracts reach it only
through the lazy selector in :mod:`whisperstream.protected_secret`. Secret
bytes cross the Security.framework boundary in process memory and never use a
subprocess, command line, environment variable, log, or plaintext fallback.
"""

from __future__ import annotations

import ctypes
import platform
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Protocol

from .protected_secret_contract import (
    MACOS_KEYCHAIN_PREFIX,
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretProvider,
    ProtectedSecretPurpose,
    macos_keychain_account,
    macos_keychain_service,
    parse_protected_secret_reference,
)


_SECURITY_FRAMEWORK = Path(
    "/System/Library/Frameworks/Security.framework/Security"
)
_CORE_FOUNDATION_FRAMEWORK = Path(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)

_ERR_SEC_SUCCESS = 0
_ERR_SEC_UNIMPLEMENTED = -4
_ERR_SEC_PARAM = -50
_ERR_SEC_USER_CANCELED = -128
_ERR_SEC_MISSING_ENTITLEMENT = -34018
_ERR_SEC_NOT_AVAILABLE = -25291
_ERR_SEC_READ_ONLY = -25292
_ERR_SEC_AUTH_FAILED = -25293
_ERR_SEC_NO_SUCH_KEYCHAIN = -25294
_ERR_SEC_INVALID_KEYCHAIN = -25295
_ERR_SEC_DUPLICATE_ITEM = -25299
_ERR_SEC_ITEM_NOT_FOUND = -25300
_ERR_SEC_INTERACTION_NOT_ALLOWED = -25308
_ERR_SEC_READ_ONLY_ATTRIBUTE = -25309
_ERR_SEC_INTERACTION_REQUIRED = -25315
_ERR_SEC_DECODE = -26275
_ERR_SEC_NOT_LOGGED_IN = -67729

_K_CF_STRING_ENCODING_UTF8 = 0x08000100
_AVAILABILITY_ACCOUNT = "availability.v1"
_MAX_DUPLICATE_RETRIES = 4
_ALLOW_INTERACTION: ContextVar[bool] = ContextVar("keychain_user_action", default=False)


@contextmanager
def allow_keychain_interaction() -> Iterator[None]:
    """Allow OS authentication only within an explicitly requested operation."""
    token = _ALLOW_INTERACTION.set(True)
    try:
        yield
    finally:
        _ALLOW_INTERACTION.reset(token)


def _purpose_label(purpose: ProtectedSecretPurpose) -> str:
    if purpose is ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY:
        return "Whisperstream transcript encryption key"
    return "Whisperstream AI provider API key"



class _KeychainBridge(Protocol):
    """Small injectable seam around the four blocking SecItem calls."""

    def copy(self, service: str, account: str, *, return_data: bool) -> tuple[int, object | None]: ...

    def add(self, service: str, account: str, secret: bytes, *, label: str) -> int: ...

    def update(self, service: str, account: str, secret: bytes) -> int: ...

    def set_label(self, service: str, account: str, label: str) -> int: ...

    def delete(self, service: str, account: str) -> int: ...


def _cf_constant(library: ctypes.CDLL, name: str) -> int:
    value = ctypes.c_void_p.in_dll(library, name).value
    if value is None:
        raise RuntimeError("native Keychain constant unavailable")
    return value


def _symbol_address(library: ctypes.CDLL, name: str) -> int:
    return ctypes.addressof((ctypes.c_byte * 1).in_dll(library, name))


class _SecurityFrameworkBridge:
    """Direct ctypes bridge to Security.framework and CoreFoundation."""

    def __init__(self) -> None:
        if platform.system() != "Darwin":
            raise RuntimeError("native Keychain provider unavailable")

        security = ctypes.CDLL(str(_SECURITY_FRAMEWORK))
        core_foundation = ctypes.CDLL(str(_CORE_FOUNDATION_FRAMEWORK))
        self._security = security
        self._cf = core_foundation

        security.SecItemCopyMatching.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecItemCopyMatching.restype = ctypes.c_int32
        security.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        security.SecItemAdd.restype = ctypes.c_int32
        security.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        security.SecItemUpdate.restype = ctypes.c_int32
        security.SecItemDelete.argtypes = [ctypes.c_void_p]
        security.SecItemDelete.restype = ctypes.c_int32

        core_foundation.CFDictionaryCreateMutable.argtypes = [
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        core_foundation.CFDictionaryCreateMutable.restype = ctypes.c_void_p
        core_foundation.CFDictionarySetValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        core_foundation.CFDictionarySetValue.restype = None
        core_foundation.CFStringCreateWithBytes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_long,
            ctypes.c_uint32,
            ctypes.c_bool,
        ]
        core_foundation.CFStringCreateWithBytes.restype = ctypes.c_void_p
        core_foundation.CFDataCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_long,
        ]
        core_foundation.CFDataCreate.restype = ctypes.c_void_p
        core_foundation.CFDataGetTypeID.argtypes = []
        core_foundation.CFDataGetTypeID.restype = ctypes.c_ulong
        core_foundation.CFGetTypeID.argtypes = [ctypes.c_void_p]
        core_foundation.CFGetTypeID.restype = ctypes.c_ulong
        core_foundation.CFDataGetLength.argtypes = [ctypes.c_void_p]
        core_foundation.CFDataGetLength.restype = ctypes.c_long
        core_foundation.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        core_foundation.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_uint8)
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        core_foundation.CFRelease.restype = None

        self._dictionary_key_callbacks = _symbol_address(
            core_foundation, "kCFTypeDictionaryKeyCallBacks"
        )
        self._dictionary_value_callbacks = _symbol_address(
            core_foundation, "kCFTypeDictionaryValueCallBacks"
        )
        self._true = _cf_constant(core_foundation, "kCFBooleanTrue")
        self._keys = {
            name: _cf_constant(security, name)
            for name in (
                "kSecClass",
                "kSecAttrService",
                "kSecAttrAccount",
                "kSecAttrLabel",
                "kSecValueData",
                "kSecReturnData",
                "kSecMatchLimit",
                "kSecUseAuthenticationUI",
            )
        }
        self._values = {
            name: _cf_constant(security, name)
            for name in (
                "kSecClassGenericPassword",
                "kSecMatchLimitOne",
                "kSecUseAuthenticationUIFail",
            )
        }

    def _string(self, value: str) -> int:
        encoded = value.encode("utf-8")
        buffer = (ctypes.c_uint8 * len(encoded)).from_buffer_copy(encoded)
        result = self._cf.CFStringCreateWithBytes(
            None,
            buffer,
            len(encoded),
            _K_CF_STRING_ENCODING_UTF8,
            False,
        )
        if not result:
            raise RuntimeError("native Keychain string allocation failed")
        return int(result)

    def _data(self, value: bytes) -> int:
        if value:
            buffer = (ctypes.c_uint8 * len(value)).from_buffer_copy(value)
            pointer = buffer
        else:
            pointer = ctypes.cast(None, ctypes.POINTER(ctypes.c_uint8))
        result = self._cf.CFDataCreate(None, pointer, len(value))
        if not result:
            raise RuntimeError("native Keychain data allocation failed")
        return int(result)

    @contextmanager
    def _dictionary(
        self,
        service: str,
        account: str,
        *,
        secret: bytes | None = None,
        return_data: bool = False,
        query_controls: bool = True,
        label: str | None = None,
    ) -> Iterator[int]:
        dictionary = self._cf.CFDictionaryCreateMutable(
            None,
            0,
            self._dictionary_key_callbacks,
            self._dictionary_value_callbacks,
        )
        if not dictionary:
            raise RuntimeError("native Keychain dictionary allocation failed")
        created: list[int] = []
        try:
            service_ref = self._string(service)
            account_ref = self._string(account)
            created.extend((service_ref, account_ref))
            entries = [
                (self._keys["kSecClass"], self._values["kSecClassGenericPassword"]),
                (self._keys["kSecAttrService"], service_ref),
                (self._keys["kSecAttrAccount"], account_ref),
            ]
            if label is not None:
                label_ref = self._string(label)
                created.append(label_ref)
                entries.append((self._keys["kSecAttrLabel"], label_ref))
            if query_controls and not _ALLOW_INTERACTION.get():
                entries.append(
                    (
                        self._keys["kSecUseAuthenticationUI"],
                        self._values["kSecUseAuthenticationUIFail"],
                    )
                )
            if secret is not None:
                secret_ref = self._data(secret)
                created.append(secret_ref)
                entries.append((self._keys["kSecValueData"], secret_ref))
            if return_data:
                entries.extend(
                    (
                        (self._keys["kSecReturnData"], self._true),
                        (
                            self._keys["kSecMatchLimit"],
                            self._values["kSecMatchLimitOne"],
                        ),
                    )
                )
            for key, value in entries:
                self._cf.CFDictionarySetValue(dictionary, key, value)
            yield int(dictionary)
        finally:
            for value in reversed(created):
                self._cf.CFRelease(value)
            self._cf.CFRelease(dictionary)

    def copy(
        self,
        service: str,
        account: str,
        *,
        return_data: bool,
    ) -> tuple[int, object | None]:
        with self._dictionary(
            service,
            account,
            return_data=return_data,
        ) as query:
            if not return_data:
                return int(self._security.SecItemCopyMatching(query, None)), None

            result = ctypes.c_void_p()
            status = int(
                self._security.SecItemCopyMatching(query, ctypes.byref(result))
            )
            if status != _ERR_SEC_SUCCESS or not result.value:
                if result.value:
                    self._cf.CFRelease(result.value)
                return status, None
            try:
                if self._cf.CFGetTypeID(result.value) != self._cf.CFDataGetTypeID():
                    return status, None
                length = int(self._cf.CFDataGetLength(result.value))
                if length < 0:
                    return status, None
                if length == 0:
                    return status, b""
                pointer = self._cf.CFDataGetBytePtr(result.value)
                if not pointer:
                    return status, None
                return status, ctypes.string_at(pointer, length)
            finally:
                self._cf.CFRelease(result.value)

    def add(self, service: str, account: str, secret: bytes, *, label: str) -> int:
        with self._dictionary(service, account, secret=secret, label=label) as attributes:
            return int(self._security.SecItemAdd(attributes, None))

    def update(self, service: str, account: str, secret: bytes) -> int:
        with self._dictionary(service, account) as query:
            # Update the secret value only. Item identity keys belong in the
            # query, not in attributesToUpdate.
            update_dictionary = self._cf.CFDictionaryCreateMutable(
                None,
                0,
                self._dictionary_key_callbacks,
                self._dictionary_value_callbacks,
            )
            if not update_dictionary:
                raise RuntimeError("native Keychain dictionary allocation failed")
            try:
                secret_ref = self._data(secret)
                try:
                    self._cf.CFDictionarySetValue(
                        update_dictionary,
                        self._keys["kSecValueData"],
                        secret_ref,
                    )
                    return int(
                        self._security.SecItemUpdate(query, update_dictionary)
                    )
                finally:
                    self._cf.CFRelease(secret_ref)
            finally:
                self._cf.CFRelease(update_dictionary)

    def set_label(self, service: str, account: str, label: str) -> int:
        with self._dictionary(service, account) as query:
            attributes = self._cf.CFDictionaryCreateMutable(
                None, 0, self._dictionary_key_callbacks, self._dictionary_value_callbacks
            )
            if not attributes:
                raise RuntimeError("native Keychain dictionary allocation failed")
            try:
                label_ref = self._string(label)
                try:
                    self._cf.CFDictionarySetValue(attributes, self._keys["kSecAttrLabel"], label_ref)
                    return int(self._security.SecItemUpdate(query, attributes))
                finally:
                    self._cf.CFRelease(label_ref)
            finally:
                self._cf.CFRelease(attributes)

    def delete(self, service: str, account: str) -> int:
        with self._dictionary(service, account) as query:
            return int(self._security.SecItemDelete(query))


def _failure_for_status(
    status: int,
    *,
    operation: ProtectedSecretOperation,
) -> ProtectedSecretFailure:
    del operation
    if status == _ERR_SEC_ITEM_NOT_FOUND:
        return ProtectedSecretFailure.NOT_FOUND
    if status in {
        _ERR_SEC_USER_CANCELED,
        _ERR_SEC_READ_ONLY,
        _ERR_SEC_AUTH_FAILED,
        _ERR_SEC_INTERACTION_NOT_ALLOWED,
        _ERR_SEC_READ_ONLY_ATTRIBUTE,
        _ERR_SEC_INTERACTION_REQUIRED,
    }:
        return ProtectedSecretFailure.ACCESS_DENIED
    if status in {
        _ERR_SEC_UNIMPLEMENTED,
        _ERR_SEC_MISSING_ENTITLEMENT,
        _ERR_SEC_NOT_AVAILABLE,
        _ERR_SEC_NO_SUCH_KEYCHAIN,
        _ERR_SEC_INVALID_KEYCHAIN,
        _ERR_SEC_NOT_LOGGED_IN,
    }:
        return ProtectedSecretFailure.UNAVAILABLE
    if status == _ERR_SEC_DECODE:
        return ProtectedSecretFailure.CORRUPT
    return ProtectedSecretFailure.BACKEND_FAILURE


class MacosKeychainProtectedSecretStore:
    """Store opaque secret bytes in isolated generic-password items."""

    def __init__(
        self,
        *,
        test_namespace: str | None = None,
        bridge: _KeychainBridge | None = None,
    ) -> None:
        self.service = macos_keychain_service(test_namespace=test_namespace)
        if bridge is not None:
            self._bridge: _KeychainBridge | None = bridge
        else:
            try:
                self._bridge = _SecurityFrameworkBridge()
            except Exception:  # noqa: BLE001 - native adapter construction boundary
                self._bridge = None

    def availability(self) -> ProtectionAvailability:
        bridge = self._bridge
        if bridge is None:
            return ProtectionAvailability.UNAVAILABLE
        try:
            status, _ = bridge.copy(
                self.service,
                _AVAILABILITY_ACCOUNT,
                return_data=False,
            )
        except Exception:  # noqa: BLE001 - native adapter boundary
            return ProtectionAvailability.UNAVAILABLE
        if status in {_ERR_SEC_SUCCESS, _ERR_SEC_ITEM_NOT_FOUND}:
            return ProtectionAvailability.AVAILABLE
        return ProtectionAvailability.UNAVAILABLE

    def _require_bridge(
        self,
        operation: ProtectedSecretOperation,
    ) -> _KeychainBridge:
        if self.availability() is ProtectionAvailability.UNAVAILABLE:
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.UNAVAILABLE,
            )
        if self._bridge is None:  # pragma: no cover - narrowed by availability
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.UNAVAILABLE,
            )
        return self._bridge

    @staticmethod
    def _secret_bytes(
        secret: bytes,
        *,
        operation: ProtectedSecretOperation,
    ) -> bytes:
        try:
            value = bytes(secret)
        except Exception:  # noqa: BLE001 - never expose caller material
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.CORRUPT,
            ) from None
        return value

    @staticmethod
    def _account(
        reference: str,
        *,
        operation: ProtectedSecretOperation,
    ) -> str:
        try:
            parsed = parse_protected_secret_reference(reference)
        except ProtectedSecretError as error:
            raise ProtectedSecretError(operation, error.failure) from None
        if parsed.provider is not ProtectedSecretProvider.MACOS_KEYCHAIN_GENERIC_PASSWORD_V1:
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.INVALID_REFERENCE,
            )
        return macos_keychain_account(parsed.opaque_id)

    @staticmethod
    def _raise_status(
        status: int,
        *,
        operation: ProtectedSecretOperation,
    ) -> None:
        raise ProtectedSecretError(
            operation,
            _failure_for_status(status, operation=operation),
        )

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        operation = ProtectedSecretOperation.STORE
        value = self._secret_bytes(secret, operation=operation)
        bridge = self._require_bridge(operation)
        for _attempt in range(_MAX_DUPLICATE_RETRIES):
            opaque_id = str(uuid.uuid4())
            account = macos_keychain_account(opaque_id)
            try:
                status = bridge.add(self.service, account, value, label=_purpose_label(purpose))
            except Exception:  # noqa: BLE001 - native adapter boundary
                raise ProtectedSecretError(
                    operation,
                    ProtectedSecretFailure.BACKEND_FAILURE,
                ) from None
            if status == _ERR_SEC_SUCCESS:
                return f"{MACOS_KEYCHAIN_PREFIX}{opaque_id}"
            if status != _ERR_SEC_DUPLICATE_ITEM:
                self._raise_status(status, operation=operation)
        raise ProtectedSecretError(
            operation,
            ProtectedSecretFailure.BACKEND_FAILURE,
        )

    def reserve_reference(self) -> str:
        """Return a fresh reference for ``store_reserved``.

        Lets a caller record an item's identity before the item exists, so a
        crash between creating it and publishing its reference can't orphan
        it. Darwin-adapter extension; not part of the portable contract.
        """
        return f"{MACOS_KEYCHAIN_PREFIX}{uuid.uuid4()}"

    def store_reserved(
        self,
        reference: str,
        secret: bytes,
        *,
        purpose: ProtectedSecretPurpose,
    ) -> None:
        """Create the item named by a ``reserve_reference`` result."""
        operation = ProtectedSecretOperation.STORE
        account = self._account(reference, operation=operation)
        value = self._secret_bytes(secret, operation=operation)
        bridge = self._require_bridge(operation)
        try:
            status = bridge.add(self.service, account, value, label=_purpose_label(purpose))
        except Exception:  # noqa: BLE001 - native adapter boundary
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.BACKEND_FAILURE,
            ) from None
        if status != _ERR_SEC_SUCCESS:
            self._raise_status(status, operation=operation)

    def load(self, reference: str) -> bytes:
        operation = ProtectedSecretOperation.LOAD
        account = self._account(reference, operation=operation)
        bridge = self._require_bridge(operation)
        try:
            status, value = bridge.copy(
                self.service,
                account,
                return_data=True,
            )
        except Exception:  # noqa: BLE001 - native adapter boundary
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.BACKEND_FAILURE,
            ) from None
        if status != _ERR_SEC_SUCCESS:
            self._raise_status(status, operation=operation)
        if not isinstance(value, bytes):
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.CORRUPT,
            )
        return value

    def update(
        self,
        reference: str,
        secret: bytes,
        *,
        purpose: ProtectedSecretPurpose,
    ) -> None:
        """Replace one exact item through SecItemUpdate.

        Update is a private Darwin-adapter operation used for native
        conformance. The frozen portable contract treats it as a store failure
        because consumers replace references atomically instead.
        """

        del purpose
        operation = ProtectedSecretOperation.STORE
        account = self._account(reference, operation=operation)
        value = self._secret_bytes(secret, operation=operation)
        bridge = self._require_bridge(operation)
        try:
            status = bridge.update(self.service, account, value)
        except Exception:  # noqa: BLE001 - native adapter boundary
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.BACKEND_FAILURE,
            ) from None
        if status != _ERR_SEC_SUCCESS:
            self._raise_status(status, operation=operation)

    def delete(self, reference: str) -> None:
        operation = ProtectedSecretOperation.DELETE
        account = self._account(reference, operation=operation)
        bridge = self._require_bridge(operation)
        try:
            status = bridge.delete(self.service, account)
        except Exception:  # noqa: BLE001 - native adapter boundary
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.BACKEND_FAILURE,
            ) from None
        if status != _ERR_SEC_SUCCESS:
            self._raise_status(status, operation=operation)

    def label_for_purpose(self, reference: str, purpose: ProtectedSecretPurpose) -> None:
        """Refresh a legacy item's label only after an explicit user action.

        Identity and secret bytes stay unchanged. A label update cannot change
        the wording of the OS prompt that authorized the preceding read.
        """
        if not _ALLOW_INTERACTION.get():
            return
        operation = ProtectedSecretOperation.STORE
        account = self._account(reference, operation=operation)
        bridge = self._require_bridge(operation)
        try:
            status = bridge.set_label(self.service, account, _purpose_label(purpose))
        except Exception:
            raise ProtectedSecretError(operation, ProtectedSecretFailure.BACKEND_FAILURE) from None
        if status != _ERR_SEC_SUCCESS:
            self._raise_status(status, operation=operation)

    def contains(self, reference: str) -> bool:
        """Return exact-item presence without asking Security for secret data."""

        operation = ProtectedSecretOperation.LOAD
        account = self._account(reference, operation=operation)
        bridge = self._require_bridge(operation)
        try:
            status, _ = bridge.copy(
                self.service,
                account,
                return_data=False,
            )
        except Exception:  # noqa: BLE001 - native adapter boundary
            raise ProtectedSecretError(
                operation,
                ProtectedSecretFailure.BACKEND_FAILURE,
            ) from None
        if status == _ERR_SEC_SUCCESS:
            return True
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return False
        self._raise_status(status, operation=operation)


__all__ = ["MacosKeychainProtectedSecretStore"]
