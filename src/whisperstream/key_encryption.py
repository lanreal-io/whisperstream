"""Compatibility facade for short protected strings.

The shipped ``encrypt_api_key`` / ``decrypt_api_key`` names remain stable, but
all native protection is now provided through the platform-neutral protected
secret contract. Storage unavailability raises a typed failure and never falls
back to plaintext. Loading an unusable legacy value remains fail-closed as the
historical empty-string result.
"""
from __future__ import annotations

import logging

from .protected_secret import get_protected_secret_store
from .protected_secret_contract import (
    MACOS_KEYCHAIN_PREFIX,
    WINDOWS_DPAPI_PREFIX,
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretPurpose,
    ProtectedSecretStore,
)

_log = logging.getLogger(__name__)


def _selected_store(store: ProtectedSecretStore | None) -> ProtectedSecretStore:
    return store if store is not None else get_protected_secret_store()


def protected_secret_availability(
    *,
    store: ProtectedSecretStore | None = None,
) -> ProtectionAvailability:
    """Return the selected platform provider's truthful availability."""

    return _selected_store(store).availability()


def encrypt_api_key(
    plaintext: str,
    *,
    store: ProtectedSecretStore | None = None,
) -> str:
    """Protect *plaintext* and return an opaque platform reference.

    Empty strings remain an empty-string no-op. A non-empty value either
    returns an opaque reference or raises :class:`ProtectedSecretError`; it is
    never returned unchanged.
    """

    if not plaintext:
        return ""
    return _selected_store(store).store(
        plaintext.encode("utf-8"),
        purpose=ProtectedSecretPurpose.API_KEY,
    )


def fingerprint_api_key(plaintext: str) -> str:
    """Return a non-sensitive masked fingerprint of *plaintext* for display.

    Shows the first 4 and last 4 characters with a fixed run of bullets between
    them, so the UI can confirm which key is set without receiving the full
    secret. Short keys collapse to a fixed bullet run so their real length is
    not leaked. This is the only key-derived value sent to the frontend.
    """

    value = (plaintext or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * 8
    return value[:4] + "•" * 10 + value[-4:]


def decrypt_api_key(
    stored: str,
    *,
    store: ProtectedSecretStore | None = None,
) -> str:
    """Load an opaque reference, preserving the legacy fail-closed result.

    Plain strings remain accepted in memory for legacy config migration and
    new API input. A recognized protected reference either returns UTF-8 text
    or ``""`` on unavailable, wrong-user, corrupt, or otherwise unusable data.
    The typed operation/failure pair is logged without native detail or secret
    content.
    """

    if not stored:
        return ""
    if not stored.startswith((WINDOWS_DPAPI_PREFIX, MACOS_KEYCHAIN_PREFIX)):
        return stored
    try:
        plaintext = _selected_store(store).load(stored)
        return plaintext.decode("utf-8")
    except (ProtectedSecretError, UnicodeDecodeError) as error:
        if isinstance(error, ProtectedSecretError):
            _log.warning(
                "protected API key load failed operation=%s failure=%s",
                error.operation.value,
                error.failure.value,
            )
        else:
            _log.warning("protected API key load failed failure=corrupt")
        return ""


def delete_api_key(
    reference: str,
    *,
    store: ProtectedSecretStore | None = None,
) -> None:
    """Delete or validate the exact provider reference."""

    if not reference:
        return
    _selected_store(store).delete(reference)


__all__ = [
    "decrypt_api_key",
    "delete_api_key",
    "encrypt_api_key",
    "fingerprint_api_key",
    "protected_secret_availability",
]
