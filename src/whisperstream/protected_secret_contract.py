"""Platform-neutral contract for user-scoped protected secret storage.

This module defines identifiers and failures only. Platform adapters own DPAPI
or Keychain calls and must never turn an unavailable provider into plaintext
persistence.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


WINDOWS_DPAPI_PREFIX = "enc:"
MACOS_KEYCHAIN_PREFIX = "keychain:v1:"
# Stable storage namespace, intentionally not the bundle identifier. It holds
# API keys and transcript data keys; renaming it would orphan existing secrets
# and make saved transcripts unreadable.
MACOS_INTERNAL_KEYCHAIN_SERVICE = (
    "com.whisperstream.app.macos-internal.protected-secrets"
)


class ProtectedSecretProvider(StrEnum):
    WINDOWS_DPAPI_CURRENT_USER_V1 = "windows-dpapi-current-user-v1"
    MACOS_KEYCHAIN_GENERIC_PASSWORD_V1 = "macos-keychain-generic-password-v1"


class ProtectedSecretOperation(StrEnum):
    AVAILABILITY = "availability"
    STORE = "store"
    LOAD = "load"
    DELETE = "delete"


class ProtectedSecretFailure(StrEnum):
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not-found"
    ACCESS_DENIED = "access-denied"
    INVALID_REFERENCE = "invalid-reference"
    CORRUPT = "corrupt"
    BACKEND_FAILURE = "backend-failure"


class ProtectionAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ProtectedSecretPurpose(StrEnum):
    API_KEY = "api-key"
    TRANSCRIPT_DATA_KEY = "transcript-data-key"


class ProtectedSecretError(RuntimeError):
    """Content-free protected-storage failure safe for diagnostics."""

    def __init__(
        self,
        operation: ProtectedSecretOperation,
        failure: ProtectedSecretFailure,
    ) -> None:
        self.operation = operation
        self.failure = failure
        super().__init__(f"protected-secret {operation.value} {failure.value}")


@dataclass(frozen=True, slots=True)
class ProtectedSecretReference:
    provider: ProtectedSecretProvider
    opaque_id: str


def parse_protected_secret_reference(value: str) -> ProtectedSecretReference:
    """Parse only the two frozen opaque reference formats.

    Legacy Windows DPAPI blobs retain their exact ``enc:<base64>`` format.
    A bare string is never interpreted as a secret or as a reference.
    """

    if value.startswith(WINDOWS_DPAPI_PREFIX):
        encoded = value.removeprefix(WINDOWS_DPAPI_PREFIX)
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            decoded = b""
        if not decoded:
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD,
                ProtectedSecretFailure.INVALID_REFERENCE,
            )
        return ProtectedSecretReference(
            ProtectedSecretProvider.WINDOWS_DPAPI_CURRENT_USER_V1,
            value,
        )

    if value.startswith(MACOS_KEYCHAIN_PREFIX):
        opaque_id = value.removeprefix(MACOS_KEYCHAIN_PREFIX)
        try:
            parsed = uuid.UUID(opaque_id)
        except ValueError:
            parsed = None
        if parsed is None or str(parsed) != opaque_id:
            raise ProtectedSecretError(
                ProtectedSecretOperation.LOAD,
                ProtectedSecretFailure.INVALID_REFERENCE,
            )
        return ProtectedSecretReference(
            ProtectedSecretProvider.MACOS_KEYCHAIN_GENERIC_PASSWORD_V1,
            opaque_id,
        )

    raise ProtectedSecretError(
        ProtectedSecretOperation.LOAD,
        ProtectedSecretFailure.INVALID_REFERENCE,
    )


def macos_keychain_account(opaque_id: str) -> str:
    """Return the stable generic-password account for a parsed reference."""

    parsed = parse_protected_secret_reference(f"{MACOS_KEYCHAIN_PREFIX}{opaque_id}")
    return f"secret.v1.{parsed.opaque_id}"


def macos_keychain_service(*, test_namespace: str | None = None) -> str:
    """Return the production service or an explicitly isolated test service."""

    if test_namespace is None:
        return MACOS_INTERNAL_KEYCHAIN_SERVICE
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27,28}", test_namespace):
        raise ValueError("test_namespace must be a lowercase UUID")
    if str(uuid.UUID(test_namespace)) != test_namespace:
        raise ValueError("test_namespace must be a canonical lowercase UUID")
    return f"{MACOS_INTERNAL_KEYCHAIN_SERVICE}.test.{test_namespace}"


@runtime_checkable
class ProtectedSecretStore(Protocol):
    """Fail-closed storage operations implemented by a platform adapter."""

    def availability(self) -> ProtectionAvailability:
        """Query provider availability without prompting for access."""

    def store(self, secret: bytes, *, purpose: ProtectedSecretPurpose) -> str:
        """Store bytes and return an opaque reference, or raise."""

    def load(self, reference: str) -> bytes:
        """Return protected bytes, or raise without a plaintext fallback."""

    def delete(self, reference: str) -> None:
        """Delete the exact referenced item, treating other items as out of scope."""


__all__ = [
    "MACOS_INTERNAL_KEYCHAIN_SERVICE",
    "MACOS_KEYCHAIN_PREFIX",
    "ProtectionAvailability",
    "ProtectedSecretError",
    "ProtectedSecretFailure",
    "ProtectedSecretOperation",
    "ProtectedSecretProvider",
    "ProtectedSecretPurpose",
    "ProtectedSecretReference",
    "ProtectedSecretStore",
    "WINDOWS_DPAPI_PREFIX",
    "macos_keychain_account",
    "macos_keychain_service",
    "parse_protected_secret_reference",
]
