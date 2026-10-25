"""Recovery envelope for standard (non-Lockdown) transcript history.

Standard history keeps its data key in Keychain/DPAPI (``transcript.key``) so
history is always readable. This module adds the way back in when that protected
copy is lost: the SAME data key wrapped under a required recovery key and an
optional recovery password, persisted to ``<data_dir>/history_recovery.json``
with the exact envelope shape Lockdown uses (``lockdown_crypto`` scrypt +
AES-256-GCM).

There is only ever one envelope. Turning Lockdown on removes this file once the
Lockdown envelope (``lockdown.key.json``) is authoritative; turning Lockdown off
moves that envelope back here, so its recovery key and password keep working as
recovery methods. While Lockdown is on, nothing here unlocks anything.

# ============================================================================
# KEY MATERIAL — DO NOT LOG
# This module handles the data key, the recovery key, the recovery password and
# their envelopes. NEVER log any of them or their encoded forms, salts, nonces
# or ciphertext. Error paths log generic messages only.
# ============================================================================
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
from pathlib import Path

from .atomic_io import atomic_write_json
from .lockdown_crypto import (
    generate_recovery_code,
    unwrap_key,
    unwrap_key_with_recovery_code,
    wrap_key,
    wrap_key_with_recovery_code,
)

_log = logging.getLogger(__name__)

RECOVERY_FILE = "history_recovery.json"
_LOCKDOWN_FILE = "lockdown.key.json"
_PASSWORD = "password"
_RECOVERY = "recovery"
# False while the key has been shown but the user hasn't confirmed saving it.
# Envelopes handed back from Lockdown carry no flag and count as confirmed.
_CONFIRMED = "confirmed"
# A regenerated key waits here until the user confirms saving it.
_PENDING_RECOVERY = "pending_recovery"
# Identifies the key awaiting confirmation, so a confirmation only promotes
# the key that client was shown (never a later retry's key).
_GENERATION = "generation"
# The last confirmed generation, so only an exact retry of that confirmation
# succeeds once nothing is pending.
_CONFIRMED_GENERATION = "confirmed_generation"
_KEY_CHECK = "key_check"
# Check value of the key a regenerated (pending) wrapper holds.
_PENDING_KEY_CHECK = "pending_key_check"
_KEY_CHECK_LABEL = b"whisperstream-history-recovery-key-check-v1"
_LOCK = threading.RLock()


def envelope_path(data_dir: Path) -> Path:
    return Path(data_dir) / RECOVERY_FILE


def _lockdown_enabled(data_dir: Path) -> bool:
    return (Path(data_dir) / _LOCKDOWN_FILE).exists()


def _read(data_dir: Path) -> dict | None:
    path = envelope_path(data_dir)
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.warning("history recovery envelope is unreadable or malformed")
        return None
    if not isinstance(blob, dict) or not isinstance(blob.get(_RECOVERY), dict):
        return None
    return blob


def _write(data_dir: Path, blob: dict) -> None:
    atomic_write_json(envelope_path(data_dir), blob)


def key_check(data_key: bytes) -> str:
    """Return a non-secret check value identifying *data_key*.

    HMAC of a fixed label under the key: it shows whether an envelope wraps the
    store's current key without revealing anything about the key itself.
    """
    return hmac.new(data_key, _KEY_CHECK_LABEL, hashlib.sha256).hexdigest()[:32]


def has_recovery(data_dir: Path) -> bool:
    """True when a confirmed standard-mode recovery envelope exists."""
    blob = _read(data_dir)
    return blob is not None and blob.get(_CONFIRMED) is not False


def matches_key(data_dir: Path, data_key: bytes) -> bool:
    """True only when the envelope is known to wrap *data_key*.

    An envelope without a check value (handed back from Lockdown before checks
    existed) can't vouch for the current key, so it doesn't count as recovery
    coverage. A successful unlock backfills the check.
    """
    blob = _read(data_dir)
    if not blob:
        return False
    stored = blob.get(_KEY_CHECK)
    return isinstance(stored, str) and hmac.compare_digest(stored, key_check(data_key))


def _is_pending(blob: dict) -> bool:
    return blob.get(_CONFIRMED) is False or isinstance(blob.get(_PENDING_RECOVERY), dict)


def pending_generation(data_dir: Path) -> str | None:
    """Return the identifier of the key awaiting confirmation, if any."""
    blob = _read(data_dir)
    if not blob or not _is_pending(blob):
        return None
    generation = blob.get(_GENERATION)
    return generation if isinstance(generation, str) else None


def generation_matches(data_dir: Path, generation: str | None) -> bool:
    """True when nothing is pending or *generation* names the pending key."""
    blob = _read(data_dir)
    if not blob or not _is_pending(blob):
        return True
    stored = blob.get(_GENERATION)
    if not isinstance(stored, str):
        # Pending envelopes written before generations existed.
        return True
    return isinstance(generation, str) and hmac.compare_digest(stored, generation)


def backfill_key_check(data_dir: Path, data_key: bytes) -> None:
    """Record which key an unchecked envelope wraps, after it proved it."""
    with _LOCK:
        blob = _read(data_dir)
        if not blob or isinstance(blob.get(_KEY_CHECK), str):
            return
        blob[_KEY_CHECK] = key_check(data_key)
        _write(data_dir, blob)


def has_envelope(data_dir: Path) -> bool:
    """True when any standard envelope exists, confirmed or still pending."""
    return _read(data_dir) is not None


def has_key_check(data_dir: Path) -> bool:
    """True when the envelope records which key it wraps."""
    blob = _read(data_dir)
    return blob is not None and isinstance(blob.get(_KEY_CHECK), str)


def has_password(data_dir: Path) -> bool:
    blob = _read(data_dir)
    return bool(blob) and isinstance(blob.get(_PASSWORD), dict)


def create(
    data_dir: Path,
    data_key: bytes,
    *,
    password: str | None = None,
    confirmed: bool = True,
) -> str:
    """Wrap *data_key* under a new recovery key (and optional password).

    Returns the recovery key for one-time display. Replaces any existing
    envelope, so an older recovery key and password stop working. Setup passes
    ``confirmed=False`` until the user confirms saving the key. Refuses while
    Lockdown is on, where the Lockdown envelope owns recovery.
    """
    with _LOCK:
        if _lockdown_enabled(data_dir):
            raise RuntimeError("lockdown owns recovery")
        recovery_key = generate_recovery_code()
        blob: dict = {
            _RECOVERY: wrap_key_with_recovery_code(data_key, recovery_key),
            _KEY_CHECK: key_check(data_key),
        }
        if password:
            blob[_PASSWORD] = wrap_key(data_key, password)
        if not confirmed:
            blob[_CONFIRMED] = False
            blob[_GENERATION] = secrets.token_urlsafe(18)
        _write(data_dir, blob)
        return recovery_key


def confirm(
    data_dir: Path,
    data_key: bytes,
    *,
    password: str | None = None,
    generation: str | None = None,
) -> bool:
    """Confirm the user saved the shown key.

    Promotes a pending regenerated key (the old key stops working only now),
    marks a pending setup confirmed, and adds or replaces the password wrapper
    when *password* is given. *generation* must name the pending key, so a
    stale confirmation can't promote a key the user never saw.
    """
    with _LOCK:
        blob = _read(data_dir)
        if not blob or _lockdown_enabled(data_dir):
            return False
        if not _is_pending(blob):
            # Nothing waits for confirmation: only an exact retry of the last
            # confirmation succeeds, and it changes nothing.
            done = blob.get(_CONFIRMED_GENERATION)
            return (
                isinstance(done, str)
                and isinstance(generation, str)
                and hmac.compare_digest(done, generation)
            )
        if not generation_matches(data_dir, generation):
            return False
        live_check = key_check(data_key)
        pending = blob.get(_PENDING_RECOVERY)
        if isinstance(pending, dict):
            staged_check = blob.get(_PENDING_KEY_CHECK)
            if not isinstance(staged_check, str) or not hmac.compare_digest(staged_check, live_check):
                # The live key changed since this key was staged, or the staged
                # key (written before checks existed) can't prove which key it
                # holds. The user creates a new key instead.
                return False
            if blob.get(_KEY_CHECK) != live_check:
                # The old password wrapper was never shown to hold this key.
                blob.pop(_PASSWORD, None)
            blob[_RECOVERY] = pending
        elif blob.get(_KEY_CHECK) != live_check:
            # A pending setup for a key that is no longer the live one.
            return False
        blob.pop(_PENDING_RECOVERY, None)
        blob.pop(_PENDING_KEY_CHECK, None)
        stored = blob.pop(_GENERATION, None)
        blob.pop(_CONFIRMED, None)
        if isinstance(stored, str):
            blob[_CONFIRMED_GENERATION] = stored
        if password:
            blob[_PASSWORD] = wrap_key(data_key, password)
        blob[_KEY_CHECK] = live_check
        _write(data_dir, blob)
        return True


def unlock(
    data_dir: Path,
    *,
    recovery_key: str | None = None,
    password: str | None = None,
) -> bytes | None:
    """Return the data key for a matching recovery key or password, else None.

    A regenerated key that is shown but not yet confirmed already works, so an
    interrupted confirmation never leaves the user without a usable key.
    """
    if _lockdown_enabled(data_dir):
        return None
    blob = _read(data_dir)
    if not blob:
        return None
    data_key: bytes | None = None
    if recovery_key is not None:
        data_key = unwrap_key_with_recovery_code(blob[_RECOVERY], recovery_key)
        pending = blob.get(_PENDING_RECOVERY)
        if data_key is None and isinstance(pending, dict):
            data_key = unwrap_key_with_recovery_code(pending, recovery_key)
    elif password is not None:
        env = blob.get(_PASSWORD)
        data_key = unwrap_key(env, password) if isinstance(env, dict) else None
    if data_key is not None:
        backfill_key_check(data_dir, data_key)
    return data_key


def regenerate(data_dir: Path, data_key: bytes) -> str | None:
    """Stage a new recovery key; the current one keeps working until confirm."""
    with _LOCK:
        blob = _read(data_dir)
        if not blob or _lockdown_enabled(data_dir):
            return None
        recovery_key = generate_recovery_code()
        blob[_PENDING_RECOVERY] = wrap_key_with_recovery_code(data_key, recovery_key)
        blob[_PENDING_KEY_CHECK] = key_check(data_key)
        # A newer request replaces the pending key; an older confirmation then
        # fails instead of promoting a key its client never showed.
        blob[_GENERATION] = secrets.token_urlsafe(18)
        _write(data_dir, blob)
        return recovery_key


def set_password(data_dir: Path, data_key: bytes, password: str | None) -> bool:
    """Set, replace or (with None) remove the recovery password wrapper."""
    with _LOCK:
        blob = _read(data_dir)
        if not blob or _lockdown_enabled(data_dir):
            return False
        stored = blob.get(_KEY_CHECK)
        if not isinstance(stored, str) or not hmac.compare_digest(stored, key_check(data_key)):
            # Never mix wrappers for different keys in one envelope.
            return False
        if password:
            blob[_PASSWORD] = wrap_key(data_key, password)
        else:
            blob.pop(_PASSWORD, None)
        _write(data_dir, blob)
        return True


def delete(data_dir: Path, *, strict: bool = False) -> None:
    """Remove the envelope. With *strict*, a failed removal raises ``OSError``."""
    with _LOCK:
        try:
            envelope_path(data_dir).unlink(missing_ok=True)
        except OSError:
            _log.warning("history recovery envelope delete failed")
            if strict:
                raise


def discard_if_lockdown(data_dir: Path) -> None:
    """Startup repair: Lockdown's envelope is the only way in while it is on.

    Only a Lockdown file that holds both wrappers counts; a damaged one can't
    unwrap anything, so the standard envelope is kept.
    """
    if not envelope_path(data_dir).exists() or not _lockdown_enabled(data_dir):
        return
    try:
        lockdown = json.loads((Path(data_dir) / _LOCKDOWN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    from .lockdown_keystore import _usable_envelope  # noqa: PLC0415 - avoids an import cycle

    if _usable_envelope(lockdown):
        delete(data_dir)
