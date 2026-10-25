"""Password / recovery-code key wrappers for Lockdown Mode.

Lockdown Mode layers a user-supplied secret on top of the existing transcript
data key. The transcript store already encrypts content with a random 32-byte
AES-256 data key protected through the platform secret provider (see
``transcript_crypto``). This module adds two *additional* wrappers of that SAME
data key:

  * a password wrapper  — the day-to-day access gate, derived from the user's
    chosen password, and
  * a recovery-code wrapper — the mandatory one-time escape hatch derived from a
    grouped recovery code, so a forgotten password is not a total data loss.

Each wrapper is an envelope: a fresh random salt feeds a key-derivation function
(KDF) to produce a 32-byte wrapping key, and that wrapping key AES-256-GCM
encrypts the data key under a fresh random nonce. Unwrapping re-derives the
wrapping key from the entered secret and decrypts; a wrong secret or a tampered
envelope fails the GCM tag and returns ``None`` (never raises) — mirroring
``transcript_crypto.decrypt``.

KDF: ``scrypt`` from ``cryptography.hazmat.primitives.kdf.scrypt`` (memory-hard,
resistant to GPU/ASIC brute force). Parameters: ``n=2**15`` (cost),
``r=8``, ``p=1``, 16-byte salt, 32-byte output. These params are recorded inside
each envelope so they can be tuned in future without breaking old envelopes.

NOT FIPS-validated. AES-GCM and scrypt run through the pure-``cryptography``
library, NOT the Windows CNG FIPS module. Do not make a FIPS/NIST safe-harbor
claim on the strength of this module. The platform provider is intentionally
NOT used here; that layer stays in ``transcript_crypto.py``. This module only
performs the password / recovery-code layers.

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
# This module additionally handles the user's PASSWORD, the RECOVERY CODE, the
# derived wrapping keys, and the unwrapped data key. NEVER log any of these,
# their bytes, their base64/hex form, or any salt/nonce/ciphertext. These
# functions stay silent on the happy path by design; an error path must log a
# generic message only, never the secret or the envelope contents.
# ============================================================================
"""
from __future__ import annotations

import base64
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# AES-256 wrapping key is 32 bytes; GCM nonce is 12 bytes (the recommended size).
_KEY_BYTES = 32
_NONCE_BYTES = 12
_SALT_BYTES = 16

# scrypt KDF cost parameters. n must be a power of two; (n, r, p) are recorded in
# each envelope's "params" so they can be tuned without breaking older envelopes.
_KDF_NAME = "scrypt"
_SCRYPT_N = 2 ** 15  # CPU/memory cost factor
_SCRYPT_R = 8        # block size
_SCRYPT_P = 1        # parallelization

# Recovery-code alphabet: Crockford-style, unambiguous (no 0/O/1/I). 32 symbols.
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_RECOVERY_GROUPS = 5
_RECOVERY_GROUP_LEN = 4


# --------------------------------------------------------------------------- #
# KDF
# --------------------------------------------------------------------------- #


def derive_key(
    password: str,
    salt: bytes,
    *,
    n: int = _SCRYPT_N,
    r: int = _SCRYPT_R,
    p: int = _SCRYPT_P,
) -> bytes:
    """Derive a 32-byte wrapping key from *password* and *salt* via scrypt.

    Deterministic for a given ``(password, salt, n, r, p)``. The password is
    UTF-8 encoded before derivation. Returns 32 bytes. Never log the inputs or
    the output.
    """
    kdf = Scrypt(salt=salt, length=_KEY_BYTES, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Envelope wrap / unwrap (shared core)
# --------------------------------------------------------------------------- #


def _wrap(data_key: bytes, secret: str) -> dict:
    """Wrap *data_key* under *secret* via scrypt + AES-256-GCM.

    Generates a fresh salt and nonce per call. Returns a JSON-serializable
    envelope ``{salt, nonce, ciphertext, kdf, params}`` with the binary fields
    base64-encoded.
    """
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    wrapping_key = derive_key(secret, salt)
    ciphertext = AESGCM(wrapping_key).encrypt(nonce, data_key, None)
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "kdf": _KDF_NAME,
        "params": {"n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P},
    }


def _unwrap(envelope: dict, secret: str) -> bytes | None:
    """Unwrap the data key from *envelope* using *secret*.

    Returns the 32-byte data key, or ``None`` on a wrong secret, a tampered
    envelope, or any structural defect. Never raises — and never logs the
    secret or any envelope detail (salt/nonce/ciphertext are sensitive).
    """
    try:
        salt = base64.b64decode(envelope["salt"], validate=True)
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        params = envelope.get("params") or {}
        n = int(params.get("n", _SCRYPT_N))
        r = int(params.get("r", _SCRYPT_R))
        p = int(params.get("p", _SCRYPT_P))
        wrapping_key = derive_key(secret, salt, n=n, r=r, p=p)
        return AESGCM(wrapping_key).decrypt(nonce, ciphertext, None)
    except Exception:
        # InvalidTag (wrong secret / tamper), malformed base64, missing field,
        # bad params, etc. No detail is logged: every value here is sensitive.
        return None


# --------------------------------------------------------------------------- #
# Password wrappers
# --------------------------------------------------------------------------- #


def wrap_key(data_key: bytes, password: str) -> dict:
    """Wrap the 32-byte *data_key* under *password*.

    Returns a serializable envelope ``{salt, nonce, ciphertext, kdf, params}``
    (base64 binary fields) suitable for JSON persistence. A fresh salt and nonce
    are minted per call.
    """
    return _wrap(data_key, password)


def unwrap_key(envelope: dict, password: str) -> bytes | None:
    """Unwrap the data key from a password *envelope*.

    Returns the 32-byte data key, or ``None`` on a wrong password / tampered
    envelope. Never raises (mirrors ``transcript_crypto.decrypt``).
    """
    return _unwrap(envelope, password)


# --------------------------------------------------------------------------- #
# Recovery code: generation + normalization + wrappers
# --------------------------------------------------------------------------- #


def generate_recovery_code() -> str:
    """Generate a fresh recovery code in ``XXXX-XXXX-XXXX-XXXX-XXXX`` form.

    20 characters drawn (with ``secrets``) from an unambiguous alphabet (no
    0/O/1/I), grouped into five hyphen-separated blocks of four. ~99 bits of
    entropy. Never log the result.
    """
    chars = [secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUPS * _RECOVERY_GROUP_LEN)]
    groups = [
        "".join(chars[i : i + _RECOVERY_GROUP_LEN])
        for i in range(0, len(chars), _RECOVERY_GROUP_LEN)
    ]
    return "-".join(groups)


def _normalize_recovery_code(code: str) -> str:
    """Canonicalize a typed recovery code for KDF input.

    Uppercases, strips whitespace, and removes hyphens/spaces so a user may
    retype the code lower-case, without hyphens, or with spaces and still match.
    """
    return "".join(ch for ch in code.upper() if ch in _RECOVERY_ALPHABET)


def wrap_key_with_recovery_code(data_key: bytes, code: str) -> dict:
    """Wrap the 32-byte *data_key* under a recovery *code*.

    Same envelope shape as :func:`wrap_key`; the KDF runs over the normalized
    recovery code so the on-disk wrap matches what
    :func:`unwrap_key_with_recovery_code` re-derives from user input.
    """
    return _wrap(data_key, _normalize_recovery_code(code))


def unwrap_key_with_recovery_code(envelope: dict, code: str) -> bytes | None:
    """Unwrap the data key from a recovery-code *envelope*.

    Tolerates case / whitespace / hyphen variations via normalization. Returns
    the 32-byte data key, or ``None`` on a wrong code / tampered envelope. Never
    raises.
    """
    return _unwrap(envelope, _normalize_recovery_code(code))
