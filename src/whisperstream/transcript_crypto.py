"""Envelope encryption for transcript content (AES-256-GCM over protected storage).

Transcript text and audio bytes are encrypted with AES-256-GCM using a randomly
generated 32-byte data key. That data key is itself stored through the shared
protected-secret boundary. Existing Windows CurrentUser DPAPI ``enc:`` values
remain readable without migration. This module does NOT implement a native
secret provider; it layers AES-GCM content encryption on top of the selected
platform adapter.

Threat model: a protected item may be unavailable after a profile/account
change, machine move, provider failure, or reference corruption.
``load_or_create_data_key`` therefore returns ``None`` (never an empty or
garbage key) when the key cannot be loaded, so callers can surface an
unavailable-history state instead of silently corrupting data.

# ============================================================================
# TRANSCRIPT DATA — DO NOT LOG
# MAKE SURE you do not log transcript data anywhere. If you add a new function
# that receives, returns, or passes transcript text, COPY this banner to it.
# Use `log_transcription_event()` from whisperstream.logger for metadata only
# (char count, word count, duration, stage name). Never pass `text`,
# `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
# Also never log key material (the data key or any nonce/ciphertext bytes).
# ============================================================================
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
from enum import StrEnum
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .atomic_io import atomic_write_json, atomic_write_text
from .protected_secret import get_protected_secret_store
from .protected_secret_contract import (
    MACOS_KEYCHAIN_PREFIX,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
    ProtectedSecretStore,
    parse_protected_secret_reference,
)

_log = logging.getLogger(__name__)

# AES-256 data key is 32 bytes; GCM nonce is 12 bytes (the recommended size).
_KEY_BYTES = 32
_NONCE_BYTES = 12
_DATA_KEY_LOCK = threading.RLock()


def _log_protected_failure(prefix: str, error: ProtectedSecretError) -> None:
    _log.warning(
        "%s operation=%s failure=%s",
        prefix,
        error.operation.value,
        error.failure.value,
    )


def _selected_store(store: ProtectedSecretStore | None) -> ProtectedSecretStore:
    return store if store is not None else get_protected_secret_store()


class DataKeyFailure(StrEnum):
    """Why an existing transcript data key could not be loaded.

    ``MISSING`` is permanent for the protected copy (the item is gone, the
    reference or payload is damaged, or a Windows profile can no longer unseal
    it); only a recovery envelope or Start fresh can resolve it. The other two
    may succeed on retry.
    """

    MISSING = "missing"
    ACCESS_DENIED = "access-denied"
    UNAVAILABLE = "unavailable"


# --------------------------------------------------------------------------- #
# Cleanup ledger for provider items
# --------------------------------------------------------------------------- #
# A provider item (a Mac Keychain entry) and the reference file that names it
# are written in two steps. ``<key>.cleanup`` records an item's reference
# before it can become unreferenced (before creation, before its file is
# removed) and is cleared once the item is either published or deleted. A
# crash in between leaves the reference here for ``reconcile_key_cleanup``, so
# no copy of the data key can be orphaned where Lockdown can't remove it.
# Only references to external items are ledgered: a Windows DPAPI reference is
# the protected key itself, so recording it would create another copy.


def _external_item(reference: str) -> bool:
    return reference.startswith(MACOS_KEYCHAIN_PREFIX)


def _ledger_path(path: Path) -> Path:
    return path.with_name(path.name + ".cleanup")


def _ledger_read(path: Path) -> list[str]:
    try:
        entries = json.loads(_ledger_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [entry for entry in entries if isinstance(entry, str)] if isinstance(entries, list) else []


def _ledger_write(path: Path, entries: list[str]) -> None:
    ledger = _ledger_path(path)
    if entries:
        atomic_write_json(ledger, entries)
    else:
        ledger.unlink(missing_ok=True)


def _ledger_add(path: Path, reference: str) -> None:
    if not _external_item(reference):
        return
    entries = _ledger_read(path)
    if reference not in entries:
        _ledger_write(path, [*entries, reference])


def _ledger_remove(path: Path, reference: str) -> None:
    entries = _ledger_read(path)
    if reference in entries:
        try:
            _ledger_write(path, [entry for entry in entries if entry != reference])
        except OSError:
            # A stale entry is harmless: reconcile skips the live reference.
            _log.warning("transcript key cleanup ledger update failed")


def _store_tracked(
    path: Path,
    store: ProtectedSecretStore,
    secret: bytes,
) -> str:
    """Create a provider item whose reference is ledgered before it exists."""
    reserve = getattr(store, "reserve_reference", None)
    store_reserved = getattr(store, "store_reserved", None)
    if callable(reserve) and callable(store_reserved):
        reference = reserve()
        _ledger_add(path, reference)
        store_reserved(
            reference, secret, purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY
        )
        return reference
    return store.store(secret, purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY)


def reconcile_key_cleanup(
    path: Path,
    *,
    store: ProtectedSecretStore | None = None,
) -> bool:
    """Delete provider items a crash left unreferenced. Retries on later calls.

    The reference currently in *path* is never deleted. Returns True when no
    unreferenced item remains. Never logs references.
    """
    path = Path(path)
    secret_store = _selected_store(store)
    with _DATA_KEY_LOCK:
        entries = _ledger_read(path)
        if not entries:
            return True
        try:
            live = path.read_text(encoding="utf-8").strip() if path.exists() else None
        except OSError:
            return False
        remaining: list[str] = []
        for reference in entries:
            if reference == live or not _external_item(reference):
                # Inline references are dropped from the ledger (writing it
                # below is what removes them); live ones need no cleanup.
                continue
            try:
                secret_store.delete(reference)
            except ProtectedSecretError as error:
                if error.failure is not ProtectedSecretFailure.NOT_FOUND:
                    _log_protected_failure("transcript key cleanup failed", error)
                    remaining.append(reference)
        try:
            _ledger_write(path, remaining)
        except OSError:
            _log.warning("transcript key cleanup ledger update failed")
            return False
        return not remaining


_PROVIDER_FAILURES = {
    ProtectedSecretFailure.NOT_FOUND: DataKeyFailure.MISSING,
    ProtectedSecretFailure.CORRUPT: DataKeyFailure.MISSING,
    ProtectedSecretFailure.INVALID_REFERENCE: DataKeyFailure.MISSING,
    ProtectedSecretFailure.ACCESS_DENIED: DataKeyFailure.ACCESS_DENIED,
}


def load_or_create_data_key(
    path: Path,
    *,
    store: ProtectedSecretStore | None = None,
) -> bytes | None:
    """Load the AES-256 data key from *path*, creating it on first use.

    On first use, a fresh 32-byte key is generated, base64-encoded, stored with
    the selected provider, and atomically written to *path* as an opaque
    reference. On later calls the reference is loaded through that provider and
    decoded back to 32 bytes.

    Returns the 32-byte key, or ``None`` when the protected key cannot be loaded
    (for example, provider unavailability, a different Windows account, a
    corrupt reference, or a payload with the wrong length). Never returns an
    empty or partial key.
    """
    return load_or_create_data_key_with_failure(path, store=store)[0]


def load_or_create_data_key_with_failure(
    path: Path,
    *,
    store: ProtectedSecretStore | None = None,
) -> tuple[bytes | None, DataKeyFailure | None]:
    """``load_or_create_data_key`` that also reports why loading failed."""

    path = Path(path)
    secret_store = _selected_store(store)
    with _DATA_KEY_LOCK:
        return _load_or_create_data_key_locked(path, secret_store, create=True)


def load_data_key(
    path: Path,
    *,
    store: ProtectedSecretStore | None = None,
) -> tuple[bytes | None, DataKeyFailure | None]:
    """Load an EXISTING data key without ever minting one.

    A missing reference file is ``MISSING``. Recovery and Lockdown enable use
    this so a lost key can never be silently replaced by a fresh, wrong key.
    """

    path = Path(path)
    secret_store = _selected_store(store)
    with _DATA_KEY_LOCK:
        return _load_or_create_data_key_locked(path, secret_store, create=False)


def _load_or_create_data_key_locked(
    path: Path,
    store: ProtectedSecretStore,
    *,
    create: bool,
) -> tuple[bytes | None, DataKeyFailure | None]:
    if path.exists():
        try:
            stored = path.read_text(encoding="utf-8").strip()
        except Exception as exc:
            _log.warning(
                "transcript data key read failed error_type=%s",
                type(exc).__name__,
            )
            return None, DataKeyFailure.UNAVAILABLE
        if not stored:
            _log.warning("transcript data key file is empty")
            return None, DataKeyFailure.MISSING
        try:
            unsealed = store.load(stored)
        except ProtectedSecretError as error:
            _log_protected_failure("transcript data key load failed", error)
            return None, _PROVIDER_FAILURES.get(error.failure, DataKeyFailure.UNAVAILABLE)
        try:
            key = base64.b64decode(unsealed, validate=True)
        except (binascii.Error, ValueError):
            _log.warning("transcript data key payload is not valid base64")
            return None, DataKeyFailure.MISSING
        if len(key) != _KEY_BYTES:
            _log.warning("transcript data key has unexpected length: %d", len(key))
            return None, DataKeyFailure.MISSING
        # Optional native metadata upgrade after a successful explicit opt-in.
        # The Mac adapter declines this operation during ordinary startup.
        label_item = getattr(store, "label_for_purpose", None)
        if callable(label_item):
            try:
                label_item(stored, ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY)
            except ProtectedSecretError:
                _log.warning("transcript key label update unavailable")
        return key, None

    if not create:
        return None, DataKeyFailure.MISSING

    # First use: generate, seal, and persist.
    key = AESGCM.generate_key(bit_length=256)
    try:
        reference = _store_tracked(path, store, base64.b64encode(key))
    except ProtectedSecretError as error:
        _log_protected_failure("transcript data key create failed", error)
        return None, (
            DataKeyFailure.ACCESS_DENIED
            if error.failure is ProtectedSecretFailure.ACCESS_DENIED
            else DataKeyFailure.UNAVAILABLE
        )
    try:
        atomic_write_text(path, reference)
    except OSError:
        try:
            store.delete(reference)
        except ProtectedSecretError:
            pass
        _log.warning("transcript data key create failed failure=write")
        return None, DataKeyFailure.UNAVAILABLE
    _ledger_remove(path, reference)
    return key, None


def save_data_key(
    path: Path,
    data_key: bytes,
    *,
    store: ProtectedSecretStore | None = None,
) -> None:
    """Protect an EXISTING 32-byte *data_key* and atomically persist its reference.

    Windows retains the existing ``enc:<base64>`` form; other adapters may use
    another frozen opaque-reference format. A later load round-trips the
    identical key. Used by lockdown DISABLE to re-materialize
    ``transcript.key`` after the password envelope is torn down (the reverse of
    the deletion performed on ENABLE), restoring the non-lockdown at-rest key
    path with no transcript row rewrite. Overwrites any existing file.

    Never logs key material (the data key or its base64 form).
    """
    if len(data_key) != _KEY_BYTES:
        # Generic message only — no key bytes.
        raise ValueError("data key must be 32 bytes")
    path = Path(path)
    secret_store = _selected_store(store)
    with _DATA_KEY_LOCK:
        previous_reference: str | None = None
        if path.exists():
            try:
                previous_reference = path.read_text(encoding="utf-8").strip() or None
            except OSError:
                previous_reference = None
        reference = _store_tracked(path, secret_store, base64.b64encode(data_key))
        if previous_reference and previous_reference != reference:
            # Replacing the file unreferences the previous item.
            _ledger_add(path, previous_reference)
        try:
            atomic_write_text(path, reference)
        except OSError:
            try:
                secret_store.delete(reference)
                _ledger_remove(path, reference)
            except ProtectedSecretError:
                pass
            raise
        _ledger_remove(path, reference)
        if previous_reference and previous_reference != reference:
            try:
                secret_store.delete(previous_reference)
                _ledger_remove(path, previous_reference)
            except ProtectedSecretError as error:
                _log_protected_failure("old transcript data key cleanup failed", error)


def delete_data_key(
    path: Path,
    *,
    store: ProtectedSecretStore | None = None,
) -> None:
    """Delete the exact protected item and then its opaque reference file."""

    path = Path(path)
    secret_store = _selected_store(store)
    with _DATA_KEY_LOCK:
        if not path.exists():
            return
        try:
            reference = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ProtectedSecretError(
                ProtectedSecretOperation.DELETE,
                ProtectedSecretFailure.BACKEND_FAILURE,
            ) from error
        if not reference:
            raise ProtectedSecretError(
                ProtectedSecretOperation.DELETE,
                ProtectedSecretFailure.CORRUPT,
            )
        try:
            parse_protected_secret_reference(reference)
        except ProtectedSecretError:
            raise ProtectedSecretError(
                ProtectedSecretOperation.DELETE,
                ProtectedSecretFailure.INVALID_REFERENCE,
            ) from None
        try:
            _ledger_add(path, reference)
        except OSError as error:
            raise ProtectedSecretError(
                ProtectedSecretOperation.DELETE,
                ProtectedSecretFailure.BACKEND_FAILURE,
            ) from error
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise ProtectedSecretError(
                ProtectedSecretOperation.DELETE,
                ProtectedSecretFailure.BACKEND_FAILURE,
            ) from error
        try:
            secret_store.delete(reference)
            _ledger_remove(path, reference)
        except ProtectedSecretError:
            # Restore the opaque reference if provider cleanup failed. This
            # keeps the original protected item reachable and lets the caller
            # roll back its higher-level transaction without a data-loss gap.
            try:
                atomic_write_text(path, reference)
                _ledger_remove(path, reference)
            except OSError:
                pass
            raise


def encrypt(data_key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt *plaintext* under *data_key* with AES-256-GCM.

    Generates a fresh 12-byte nonce via ``os.urandom(12)`` for every call (a
    unique nonce per encryption is mandatory for GCM). Returns
    ``(nonce, ciphertext)``.
    """
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, None)
    return nonce, ciphertext


def decrypt(data_key: bytes, nonce: bytes, ciphertext: bytes) -> bytes | None:
    """Decrypt *ciphertext* under *data_key* and *nonce*.

    Returns the plaintext bytes, or ``None`` on an ``InvalidTag`` (tampering /
    wrong key) or any other failure. Never raises to the caller.
    """
    try:
        return AESGCM(data_key).decrypt(nonce, ciphertext, None)
    except Exception:
        # InvalidTag (tamper / wrong key), bad nonce length, etc. No detail is
        # logged: nonce/ciphertext are sensitive material.
        return None


def encrypt_text(data_key: bytes, plaintext: str) -> tuple[bytes, bytes]:
    """Encrypt a string (utf-8). Returns ``(nonce, ciphertext)``."""
    return encrypt(data_key, plaintext.encode("utf-8"))


def decrypt_text(data_key: bytes, nonce: bytes, ciphertext: bytes) -> str | None:
    """Decrypt to a string (utf-8). Returns ``None`` on any failure."""
    raw = decrypt(data_key, nonce, ciphertext)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except Exception:
        return None
