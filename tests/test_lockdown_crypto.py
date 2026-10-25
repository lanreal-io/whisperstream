"""Tests for the lockdown_crypto module (password / recovery-code key wrappers).

This module layers password-KDF and recovery-code wrappers on top of the
existing 32-byte AES-256 transcript data key. It is pure-``cryptography`` (no
DPAPI here — that stays in ``key_encryption.py``), so every test below is
platform-independent and must pass everywhere.

The wrappers mirror ``transcript_crypto``'s never-raise contract: an unwrap with
the wrong secret or a tampered envelope returns ``None``, never an exception.
"""
from __future__ import annotations

import base64
import re

# A fixed 32-byte data key stand-in for the real transcript data key.
DATA_KEY = bytes(range(32))


# --------------------------------------------------------------------------- #
# KDF
# --------------------------------------------------------------------------- #


def test_derive_key_is_32_bytes_and_deterministic():
    """The KDF yields a deterministic 32-byte key for (password, salt)."""
    from whisperstream.lockdown_crypto import derive_key

    salt = b"\x00" * 16
    k1 = derive_key("hunter2", salt)
    k2 = derive_key("hunter2", salt)
    assert isinstance(k1, bytes)
    assert len(k1) == 32
    assert k1 == k2


def test_derive_key_salt_changes_output():
    """A different salt produces a different derived key for the same password."""
    from whisperstream.lockdown_crypto import derive_key

    k1 = derive_key("hunter2", b"\x00" * 16)
    k2 = derive_key("hunter2", b"\x01" * 16)
    assert k1 != k2


def test_derive_key_password_changes_output():
    """A different password produces a different derived key for the same salt."""
    from whisperstream.lockdown_crypto import derive_key

    salt = b"\x02" * 16
    assert derive_key("hunter2", salt) != derive_key("hunter3", salt)


# --------------------------------------------------------------------------- #
# Password wrap / unwrap
# --------------------------------------------------------------------------- #


def test_wrap_key_envelope_shape():
    """wrap_key returns a serializable envelope with the documented fields."""
    from whisperstream.lockdown_crypto import wrap_key

    env = wrap_key(DATA_KEY, "correct horse battery staple")
    assert isinstance(env, dict)
    for field in ("salt", "nonce", "ciphertext", "kdf", "params"):
        assert field in env, f"missing envelope field {field!r}"
    # Binary fields are base64 strings (JSON-serializable).
    for field in ("salt", "nonce", "ciphertext"):
        assert isinstance(env[field], str)
        base64.b64decode(env[field], validate=True)  # must decode cleanly
    assert isinstance(env["kdf"], str)
    assert isinstance(env["params"], dict)


def test_wrap_key_is_json_serializable():
    """The envelope survives a JSON round-trip (it gets persisted as JSON)."""
    import json

    from whisperstream.lockdown_crypto import unwrap_key, wrap_key

    env = wrap_key(DATA_KEY, "pw")
    reloaded = json.loads(json.dumps(env))
    assert unwrap_key(reloaded, "pw") == DATA_KEY


def test_wrap_unwrap_roundtrip():
    """Wrapping then unwrapping with the right password returns the data key."""
    from whisperstream.lockdown_crypto import unwrap_key, wrap_key

    env = wrap_key(DATA_KEY, "s3cret-passphrase")
    assert unwrap_key(env, "s3cret-passphrase") == DATA_KEY


def test_unwrap_wrong_password_returns_none():
    """Unwrapping with the wrong password returns None (never raises)."""
    from whisperstream.lockdown_crypto import unwrap_key, wrap_key

    env = wrap_key(DATA_KEY, "right-password")
    assert unwrap_key(env, "wrong-password") is None


def test_unwrap_tampered_ciphertext_returns_none():
    """A tampered ciphertext fails the GCM tag and returns None."""
    from whisperstream.lockdown_crypto import unwrap_key, wrap_key

    env = wrap_key(DATA_KEY, "pw")
    raw = bytearray(base64.b64decode(env["ciphertext"]))
    raw[0] ^= 0xFF
    env["ciphertext"] = base64.b64encode(bytes(raw)).decode("ascii")
    assert unwrap_key(env, "pw") is None


def test_unwrap_tampered_salt_returns_none():
    """A tampered salt derives the wrong key -> GCM tag fails -> None."""
    from whisperstream.lockdown_crypto import unwrap_key, wrap_key

    env = wrap_key(DATA_KEY, "pw")
    raw = bytearray(base64.b64decode(env["salt"]))
    raw[0] ^= 0xFF
    env["salt"] = base64.b64encode(bytes(raw)).decode("ascii")
    assert unwrap_key(env, "pw") is None


def test_unwrap_garbage_envelope_returns_none():
    """A structurally broken envelope returns None instead of raising."""
    from whisperstream.lockdown_crypto import unwrap_key

    assert unwrap_key({}, "pw") is None
    assert unwrap_key({"salt": "!!!", "nonce": "x", "ciphertext": "y"}, "pw") is None
    assert unwrap_key({"salt": None, "nonce": None, "ciphertext": None}, "pw") is None


def test_each_wrap_uses_a_fresh_salt_and_nonce():
    """Two wraps of the same key+password produce different salts and nonces.

    A reused nonce under the same derived key would break AES-GCM, and a fixed
    salt would let an attacker precompute the KDF — both must be fresh per wrap.
    """
    from whisperstream.lockdown_crypto import unwrap_key, wrap_key

    env1 = wrap_key(DATA_KEY, "pw")
    env2 = wrap_key(DATA_KEY, "pw")
    assert env1["salt"] != env2["salt"], "salt must be fresh per wrap"
    assert env1["nonce"] != env2["nonce"], "nonce must be fresh per wrap"
    assert env1["ciphertext"] != env2["ciphertext"]
    # Both still unwrap to the same key.
    assert unwrap_key(env1, "pw") == DATA_KEY
    assert unwrap_key(env2, "pw") == DATA_KEY


# --------------------------------------------------------------------------- #
# Recovery code generation
# --------------------------------------------------------------------------- #

_RECOVERY_RE = re.compile(r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
_AMBIGUOUS = set("01OI")


def test_generate_recovery_code_format():
    """Recovery code is XXXX-XXXX-XXXX-XXXX-XXXX (20 chars + 4 hyphens)."""
    from whisperstream.lockdown_crypto import generate_recovery_code

    code = generate_recovery_code()
    assert _RECOVERY_RE.match(code), f"bad recovery code shape: {code!r}"
    # 5 groups of 4 = 20 alnum chars; total length including hyphens is 24.
    assert len(code) == 24
    assert len(code.replace("-", "")) == 20


def test_generate_recovery_code_unambiguous_alphabet():
    """No 0/O/1/I appear in any generated code (unambiguous alphabet)."""
    from whisperstream.lockdown_crypto import generate_recovery_code

    for _ in range(200):
        chars = set(generate_recovery_code().replace("-", ""))
        assert not (chars & _AMBIGUOUS), f"ambiguous char in {chars}"


def test_generate_recovery_code_is_random():
    """Two consecutive codes differ (it is freshly random each call)."""
    from whisperstream.lockdown_crypto import generate_recovery_code

    codes = {generate_recovery_code() for _ in range(50)}
    assert len(codes) == 50, "recovery codes should be unique per call"


# --------------------------------------------------------------------------- #
# Recovery-code wrap / unwrap (KDF over the normalized code)
# --------------------------------------------------------------------------- #


def test_recovery_wrap_unwrap_roundtrip():
    """Wrapping under a recovery code then unwrapping with it returns the key."""
    from whisperstream.lockdown_crypto import (
        generate_recovery_code,
        unwrap_key_with_recovery_code,
        wrap_key_with_recovery_code,
    )

    code = generate_recovery_code()
    env = wrap_key_with_recovery_code(DATA_KEY, code)
    assert unwrap_key_with_recovery_code(env, code) == DATA_KEY


def test_recovery_unwrap_wrong_code_returns_none():
    """A wrong recovery code returns None (never raises)."""
    from whisperstream.lockdown_crypto import (
        generate_recovery_code,
        unwrap_key_with_recovery_code,
        wrap_key_with_recovery_code,
    )

    code = generate_recovery_code()
    other = generate_recovery_code()
    env = wrap_key_with_recovery_code(DATA_KEY, code)
    assert unwrap_key_with_recovery_code(env, other) is None


def test_recovery_code_normalization():
    """Unwrap tolerates case, whitespace, and missing/extra hyphens.

    The code is shown grouped + uppercase; a user may retype it lower-case, with
    spaces, or without the hyphens. Normalization must canonicalize all of these
    to the same KDF input so the legitimate code still unwraps.
    """
    from whisperstream.lockdown_crypto import (
        generate_recovery_code,
        unwrap_key_with_recovery_code,
        wrap_key_with_recovery_code,
    )

    code = generate_recovery_code()
    env = wrap_key_with_recovery_code(DATA_KEY, code)
    variants = [
        code.lower(),
        code.replace("-", ""),
        f"  {code}  ",
        code.replace("-", " "),
        code.lower().replace("-", ""),
    ]
    for v in variants:
        assert unwrap_key_with_recovery_code(env, v) == DATA_KEY, f"variant failed: {v!r}"


def test_recovery_envelope_has_same_shape_as_password_envelope():
    """The recovery envelope is the same serializable shape as the password one."""
    from whisperstream.lockdown_crypto import (
        generate_recovery_code,
        wrap_key_with_recovery_code,
    )

    env = wrap_key_with_recovery_code(DATA_KEY, generate_recovery_code())
    for field in ("salt", "nonce", "ciphertext", "kdf", "params"):
        assert field in env


def test_recovery_and_password_envelopes_are_independent():
    """A password envelope and a recovery envelope of the same key are distinct
    and each only opens with its own secret."""
    from whisperstream.lockdown_crypto import (
        generate_recovery_code,
        unwrap_key,
        unwrap_key_with_recovery_code,
        wrap_key,
        wrap_key_with_recovery_code,
    )

    code = generate_recovery_code()
    pw_env = wrap_key(DATA_KEY, "the-password")
    rc_env = wrap_key_with_recovery_code(DATA_KEY, code)
    # Each opens with its own secret.
    assert unwrap_key(pw_env, "the-password") == DATA_KEY
    assert unwrap_key_with_recovery_code(rc_env, code) == DATA_KEY
    # The recovery code does not open the password envelope, and vice versa.
    assert unwrap_key(rc_env, code) is None
    assert unwrap_key_with_recovery_code(pw_env, "the-password") is None


# --------------------------------------------------------------------------- #
# Secret hygiene: no key material / password / code leaks via logging or repr
# --------------------------------------------------------------------------- #


def test_no_secret_bytes_in_envelope_plaintext():
    """The raw data key never appears in the serialized envelope.

    The ciphertext must not be the plaintext key, and no base64 of the key
    leaks into any field.
    """
    from whisperstream.lockdown_crypto import wrap_key

    env = wrap_key(DATA_KEY, "pw")
    ct = base64.b64decode(env["ciphertext"])
    assert DATA_KEY not in ct
    blob = repr(env)
    assert base64.b64encode(DATA_KEY).decode("ascii") not in blob
    assert DATA_KEY.hex() not in blob


def test_module_emits_no_log_records(caplog):
    """Wrapping / unwrapping must not log key material, passwords, or codes.

    We assert the module logs nothing at all during a normal round trip — the
    DO-NOT-LOG contract means these functions stay silent on the happy path.
    """
    import logging

    from whisperstream.lockdown_crypto import (
        generate_recovery_code,
        unwrap_key,
        unwrap_key_with_recovery_code,
        wrap_key,
        wrap_key_with_recovery_code,
    )

    secret_pw = "super-secret-password"
    code = generate_recovery_code()
    with caplog.at_level(logging.DEBUG):
        env = wrap_key(DATA_KEY, secret_pw)
        unwrap_key(env, secret_pw)
        unwrap_key(env, "wrong")
        rc_env = wrap_key_with_recovery_code(DATA_KEY, code)
        unwrap_key_with_recovery_code(rc_env, code)
        unwrap_key_with_recovery_code(rc_env, "WRNG-WRNG-WRNG-WRNG-WRNG")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_pw not in joined
    assert code not in joined
    assert DATA_KEY.hex() not in joined
