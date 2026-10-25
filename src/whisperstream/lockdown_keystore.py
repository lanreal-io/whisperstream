"""On-disk lockdown key store: envelope persistence + transparent migration.

Lockdown Mode adds a user-supplied access gate on top of the existing
transcript-at-rest encryption. The transcript store already owns a random
32-byte AES-256 data key protected through the platform secret provider and
referenced at ``<data_dir>/transcript.key``. This module does NOT generate a
new data key. Instead it re-wraps the SAME existing data key under two
additional secrets and persists those wrappers to a separate JSON file,
``<data_dir>/lockdown.key.json``:

  * a PASSWORD wrapper  — the day-to-day access gate, and
  * a RECOVERY-CODE wrapper — the mandatory one-time escape hatch.

Both wrappers come from ``lockdown_crypto`` (scrypt-KDF + AES-256-GCM envelopes).

Password load-bearing at rest (F2). Enabling lockdown wraps the SAME existing
data key under the password + recovery code, THEN DELETES the protected
``transcript.key`` reference and its exact provider item so the platform
provider alone can no longer unwrap the data key. Under
lockdown the key lives ONLY in memory after a successful unlock (password or
recovery code); at rest, decryption genuinely requires one of the two secrets.
Disabling lockdown (after verifying the password) unwraps the data key from the
envelope, RE-PROTECTS it (recreating ``transcript.key``), then deletes the
envelope — restoring the non-lockdown at-rest path. Neither enable nor disable
rewrites transcript DB rows or audio files or regenerates the data key, so a
full enable -> disable cycle leaves history intact and decryptable.

The data dir is passed in explicitly by callers (so it composes cleanly with
``WHISPERSTREAM_DATA_DIR`` via ``get_app_dir()``); helpers below resolve the two
fixed file names underneath it.

# ============================================================================
# TRANSCRIPT DATA — DO NOT LOG
# MAKE SURE you do not log transcript data anywhere. If you add a new function
# that receives, returns, or passes transcript text, COPY this banner to it.
# Use `log_transcription_event()` from whisperstream.logger for metadata only
# (char count, word count, duration, stage name). Never pass `text`,
# `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
# Also never log key material (the data key or any nonce/ciphertext bytes).
#
# KEY MATERIAL — DO NOT LOG (lockdown extension)
# This module handles the user's PASSWORD, the RECOVERY CODE, the wrapping-key
# envelopes, and the unwrapped 32-byte data key. NEVER log any of these, their
# bytes, their base64/hex form, or any salt/nonce/ciphertext. These functions
# stay silent on the happy path by design; an error path must log a generic
# message only, never the secret or the envelope contents.
# ============================================================================
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from . import history_recovery
from .atomic_io import atomic_write_json, atomic_write_text
from .lockdown_crypto import (
    generate_recovery_code,
    unwrap_key,
    unwrap_key_with_recovery_code,
    wrap_key,
    wrap_key_with_recovery_code,
)
from .protected_secret_contract import ProtectedSecretStore
from .transcript_crypto import (
    delete_data_key,
    load_data_key,
    load_or_create_data_key,
    reconcile_key_cleanup,
    save_data_key,
)

_log = logging.getLogger(__name__)

# File names underneath the data dir. The provider reference and Lockdown
# envelopes are intentionally separate files: disabling Lockdown removes only
# the envelope file, leaving the protected reference so transcripts stay
# decryptable.
_DATA_KEY_FILE = "transcript.key"
_ENVELOPE_FILE = "lockdown.key.json"

# Keys inside the envelope JSON for the two independent wrappers.
_PASSWORD_KEY = "password"
_RECOVERY_KEY = "recovery"
_KEYSTORE_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# Path / file helpers
# --------------------------------------------------------------------------- #


def _data_key_path(data_dir: Path) -> Path:
    return Path(data_dir) / _DATA_KEY_FILE


def _envelope_path(data_dir: Path) -> Path:
    return Path(data_dir) / _ENVELOPE_FILE


def _load_existing_data_key(
    data_dir: Path,
    *,
    store: ProtectedSecretStore | None = None,
) -> bytes:
    """Load the EXISTING transcript data key (creating it on first use).

    Never generates a lockdown-specific key — this is the same key the
    transcript store uses. Raises ``RuntimeError`` when the data key cannot be
    loaded (for example, provider/account mismatch), so callers never wrap a
    bogus key.
    """
    key_path = _data_key_path(data_dir)
    if key_path.exists():
        # Load-only: a reference whose protected item is gone must never be
        # replaced by a fresh key that cannot read existing history.
        data_key, _failure = load_data_key(key_path, store=store)
    else:
        from .transcript_store import _encrypted_history_exists_without_key

        if _encrypted_history_exists_without_key(Path(data_dir) / "transcripts.db"):
            data_key = None
        else:
            data_key = load_or_create_data_key(key_path, store=store)
    if data_key is None:
        # Generic message only — no key material, no path detail beyond a flag.
        raise RuntimeError("transcript data key unavailable; cannot enable lockdown")
    return data_key


def _read_envelopes(data_dir: Path) -> dict | None:
    """Read and parse the lockdown envelope file, or None if absent/unreadable."""
    path = _envelope_path(data_dir)
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # A corrupt envelope file is treated as "not present" rather than
        # raising; no file detail is logged (it holds sensitive envelopes).
        _log.warning("lockdown envelope file is unreadable or malformed")
        return None
    if not isinstance(blob, dict):
        return None
    return blob


def _write_envelopes(
    data_dir: Path, password_env: dict, recovery_env: dict, data_key: bytes
) -> None:
    """Persist both wrappers atomically to the envelope file.

    The non-secret key check travels with the envelope, so after disable hands
    it to standard history it still proves which data key it wraps.
    """
    path = _envelope_path(data_dir)
    payload = {
        _PASSWORD_KEY: password_env,
        _RECOVERY_KEY: recovery_env,
        "key_check": history_recovery.key_check(data_key),
    }
    atomic_write_json(path, payload)


def _delete_data_key(
    data_dir: Path,
    *,
    store: ProtectedSecretStore | None = None,
) -> None:
    """Remove the protected ``transcript.key`` (F2: on Lockdown ENABLE).

    Called only AFTER the password + recovery envelopes are safely persisted, so
    the data key remains recoverable via either secret. No path/key detail is
    logged.
    """
    delete_data_key(_data_key_path(data_dir), store=store)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def is_lockdown_enabled(data_dir: Path) -> bool:
    """True when the lockdown envelope file is present under *data_dir*."""
    return _envelope_path(data_dir).exists()


def enable_lockdown(
    data_dir: Path,
    password: str,
    *,
    store: ProtectedSecretStore | None = None,
) -> str:
    """Enable lockdown over the EXISTING transcript data key.

    Loads the existing protected data key, wraps it
    under *password* AND a freshly-minted recovery code, persists both envelopes
    to ``lockdown.key.json``, and returns the recovery code (the caller shows it
    exactly once — it is not recoverable in plaintext afterward).

    F2 (password load-bearing at rest): once both envelopes are safely on disk,
    the protected ``transcript.key`` reference and exact provider item are
    DELETED so the provider alone can no longer unwrap the data key. The key
    thereafter exists ONLY in memory after an
    unlock. No transcript rows or audio files are rewritten and the data key is
    never regenerated — this stays a transparent migration.

    Raises ``RuntimeError`` if the data key cannot be loaded.
    """
    with _KEYSTORE_LOCK:
        if is_lockdown_enabled(data_dir):
            raise RuntimeError("lockdown is already enabled")
        # An orphaned provider copy of the key would survive enable and keep a
        # platform-only path open. Clear it first, or don't enable at all.
        if not reconcile_key_cleanup(_data_key_path(data_dir), store=store):
            raise RuntimeError("lockdown enable could not remove an orphaned key copy")
        data_key = _load_existing_data_key(data_dir, store=store)
        recovery_code = generate_recovery_code()
        password_env = wrap_key(data_key, password)
        recovery_env = wrap_key_with_recovery_code(data_key, recovery_code)
        _write_envelopes(data_dir, password_env, recovery_env, data_key)
        # Envelopes are durably written above; only now is it safe to drop the
        # other at-rest unwrap paths: first the standard recovery envelope, then
        # the provider reference. If either removal fails, roll back so the
        # password gate is never reported enabled while another way in remains.
        history_path = history_recovery.envelope_path(data_dir)
        try:
            saved_history = (
                history_path.read_text(encoding="utf-8") if history_path.exists() else None
            )
        except OSError:
            saved_history = None
        try:
            history_recovery.delete(data_dir, strict=True)
            _delete_data_key(data_dir, store=store)
        except Exception:
            try:
                _envelope_path(data_dir).unlink(missing_ok=True)
            except OSError:
                pass
            if saved_history is not None and not history_path.exists():
                try:
                    atomic_write_text(history_path, saved_history)
                except OSError:
                    _log.warning("failed to restore history recovery envelope on rollback")
            raise RuntimeError("lockdown enable could not remove other key copies") from None
        return recovery_code


def repair_interrupted_transition(
    data_dir: Path,
    *,
    store: ProtectedSecretStore | None = None,
) -> None:
    """Startup repair: while Lockdown is on, nothing else may unwrap the key.

    Enable publishes the Lockdown envelope before removing the standard
    recovery envelope and the protected ``transcript.key``; disable stages a
    new protected copy before handing the envelope back. A crash inside either
    window can leave a platform-only path into Lockdown history. With the
    envelope present (it stays authoritative in both cases) this finishes the
    enable or rolls back the disable by removing those other copies, then
    deletes any provider item a crash left unreferenced. Failures are retried
    on the next start. Never logs key material.
    """
    with _KEYSTORE_LOCK:
        envelope = _read_envelopes(data_dir) if is_lockdown_enabled(data_dir) else None
        if is_lockdown_enabled(data_dir) and not _usable_envelope(envelope):
            # A damaged marker can't unwrap anything. Deleting the other copies
            # on its say-so would destroy the only working keys; keep them and
            # leave history gated until the envelope is repaired or reset.
            _log.warning("lockdown startup repair: envelope unusable, keeping key copies")
        elif envelope is not None:
            history_recovery.delete(data_dir)
            try:
                _delete_data_key(data_dir, store=store)
            except Exception:  # noqa: BLE001 - retried on the next start
                _log.warning("lockdown startup repair: protected key removal failed")
        try:
            reconcile_key_cleanup(_data_key_path(data_dir), store=store)
        except Exception:  # noqa: BLE001 - retried on the next start
            _log.warning("lockdown startup repair: key cleanup failed")


_WRAPPER_FIELDS = ("salt", "nonce", "ciphertext", "kdf", "params")


def _usable_wrapper(wrapper: object) -> bool:
    return isinstance(wrapper, dict) and all(
        isinstance(wrapper.get(field), (str, dict)) and wrapper.get(field)
        for field in _WRAPPER_FIELDS
    )


def _usable_envelope(blob: dict | None) -> bool:
    """True when the Lockdown file has both complete wrappers a real enable writes."""
    return (
        isinstance(blob, dict)
        and _usable_wrapper(blob.get(_PASSWORD_KEY))
        and _usable_wrapper(blob.get(_RECOVERY_KEY))
    )


def unlock_with_password(data_dir: Path, password: str) -> bytes | None:
    """Unlock the data key with *password*, or None on failure.

    Returns the 32-byte data key when the password matches the stored wrapper.
    Returns None when lockdown is not enabled, the password is wrong, or the
    envelope is tampered. Never raises.
    """
    blob = _read_envelopes(data_dir)
    if not blob:
        return None
    env = blob.get(_PASSWORD_KEY)
    if not isinstance(env, dict):
        return None
    return unwrap_key(env, password)


def unlock_with_recovery_code(data_dir: Path, code: str) -> bytes | None:
    """Unlock the data key with a recovery *code*, or None on failure.

    Tolerates case / whitespace / hyphen variations (normalization happens in
    ``lockdown_crypto``). Returns None when lockdown is not enabled, the code is
    wrong, or the envelope is tampered. Never raises.
    """
    blob = _read_envelopes(data_dir)
    if not blob:
        return None
    env = blob.get(_RECOVERY_KEY)
    if not isinstance(env, dict):
        return None
    return unwrap_key_with_recovery_code(env, code)


def change_password(data_dir: Path, current_password: str, new_password: str) -> bool:
    """Re-wrap the data key under *new_password*, verifying *current_password*.

    Returns True on success, False when lockdown is not enabled or the current
    password is wrong. The recovery-code wrapper is left intact. On failure the
    stored envelopes are not mutated.
    """
    with _KEYSTORE_LOCK:
        blob = _read_envelopes(data_dir)
        if not blob:
            return False
        data_key = unlock_with_password(data_dir, current_password)
        if data_key is None:
            return False
        recovery_env = blob.get(_RECOVERY_KEY)
        if not isinstance(recovery_env, dict):
            return False
        new_password_env = wrap_key(data_key, new_password)
        _write_envelopes(data_dir, new_password_env, recovery_env, data_key)
        return True


def regenerate_recovery_code(data_dir: Path, password: str) -> str | None:
    """Mint a fresh recovery code (invalidating the old one), gated by *password*.

    Verifies *password* unlocks the data key, then wraps that SAME data key
    under a new recovery code and persists it (replacing the old recovery
    wrapper). The password wrapper is left intact. Returns the new recovery code
    on success, or None when lockdown is not enabled or the password is wrong.
    On failure the stored envelopes are not mutated.
    """
    with _KEYSTORE_LOCK:
        blob = _read_envelopes(data_dir)
        if not blob:
            return None
        data_key = unlock_with_password(data_dir, password)
        if data_key is None:
            return None
        password_env = blob.get(_PASSWORD_KEY)
        if not isinstance(password_env, dict):
            return None
        new_code = generate_recovery_code()
        new_recovery_env = wrap_key_with_recovery_code(data_key, new_code)
        _write_envelopes(data_dir, password_env, new_recovery_env, data_key)
        return new_code


def prepare_disable_lockdown(
    data_dir: Path,
    password: str,
    *,
    store: ProtectedSecretStore | None = None,
) -> bytes | None:
    """Verify the password and stage platform protection without opening access.

    The password envelope remains authoritative until the caller verifies the
    live store and persists normal-mode config. The returned key is secret
    material and must never be logged.
    """
    with _KEYSTORE_LOCK:
        if not is_lockdown_enabled(data_dir):
            return None
        data_key = unlock_with_password(data_dir, password)
        if data_key is None:
            return None
        try:
            save_data_key(_data_key_path(data_dir), data_key, store=store)
        except Exception:
            _log.warning("failed to stage transcript key on lockdown disable")
            return None
        return data_key


def abort_disable_lockdown(
    data_dir: Path,
    *,
    store: ProtectedSecretStore | None = None,
) -> None:
    """Remove staged platform protection while the Lockdown envelope remains."""
    with _KEYSTORE_LOCK:
        try:
            delete_data_key(_data_key_path(data_dir), store=store)
        except Exception:
            _log.warning("failed to remove staged transcript key on lockdown abort")


def finalize_disable_lockdown(data_dir: Path, data_key: bytes | None = None) -> bool:
    """Hand the envelope back to standard history as the final disable step.

    The Lockdown password and recovery key keep working as the standard-mode
    recovery password and recovery key. *data_key* (the verified key) lets an
    envelope written before key checks existed record which key it wraps.
    """
    with _KEYSTORE_LOCK:
        if not _move_envelope_to_history_recovery(data_dir):
            return False
        if data_key is not None:
            history_recovery.backfill_key_check(data_dir, data_key)
        return True


def _move_envelope_to_history_recovery(data_dir: Path) -> bool:
    try:
        os.replace(_envelope_path(data_dir), history_recovery.envelope_path(data_dir))
    except OSError:
        _log.warning("failed to move lockdown envelope to history recovery")
        return False
    return True


def disable_lockdown(
    data_dir: Path,
    password: str,
    *,
    store: ProtectedSecretStore | None = None,
) -> bool:
    """Disable lockdown after verifying *password*.

    Verifies the password unlocks the data key, then (F2) RE-PROTECTS that key,
    recreating ``transcript.key`` BEFORE deleting the Lockdown envelope file.
    Protecting first is crash-safe: a crash before the envelope is removed
    leaves both the protected key and the envelope on disk (still unlockable),
    never an unrecoverable gap. After disable, the store loads ``transcript.key``
    exactly as the non-lockdown path (no transcript row rewrite). Returns True on
    success, False when lockdown is not enabled or the password is wrong.
    """
    with _KEYSTORE_LOCK:
        if not is_lockdown_enabled(data_dir):
            return False
        data_key = unlock_with_password(data_dir, password)
        if data_key is None:
            return False
        try:
            # Re-materialize the provider reference FIRST so at-rest
            # decryptability is never lost mid-disable.
            save_data_key(_data_key_path(data_dir), data_key, store=store)
        except Exception:
            _log.warning("failed to protect transcript data key on lockdown disable")
            return False
        if not _envelope_path(data_dir).exists():
            return True
        if not _move_envelope_to_history_recovery(data_dir):
            return False
        history_recovery.backfill_key_check(data_dir, data_key)
        return True


def reset_lockdown(
    data_dir: Path,
    *,
    store: ProtectedSecretStore | None = None,
) -> None:
    """Last-resort teardown when BOTH the password and recovery code are lost.

    Deletes the Lockdown envelope and any protected ``transcript.key`` WITHOUT
    a password. The data key wrapped in the envelope (under the forgotten
    secrets) is NOT recovered — anything encrypted under it becomes permanently
    unreadable. This function NEVER decrypts and uses ordinary filesystem
    unlink only; it does not overwrite storage or promise secure erasure.

    The CALLER MUST have already ordinarily removed the encrypted transcript
    store, and after
    this MUST re-materialize a fresh key (``store.reload_data_key()`` /
    ``load_or_create_data_key``) so the now-empty store is usable again. Only
    meaningful when ``is_lockdown_enabled(data_dir)`` is True.
    """
    with _KEYSTORE_LOCK:
        try:
            _envelope_path(data_dir).unlink(missing_ok=True)
        except OSError:
            _log.warning("lockdown reset: failed to remove envelope file")
        history_recovery.delete(data_dir)
        try:
            delete_data_key(_data_key_path(data_dir), store=store)
        except Exception:
            # Destructive reset must still remove an unreadable/corrupt inline
            # reference after the transcript ciphertext has been wiped.
            try:
                _data_key_path(data_dir).unlink(missing_ok=True)
            except OSError:
                _log.warning("lockdown reset: failed to remove stale protected key")
