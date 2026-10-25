"""Content-free transcript access logger for HIPAA audit controls (45 CFR 164.312(b)).

This module provides a SEPARATE, INDEPENDENT logger deliberately decoupled from
``get_logger()`` so the diagnostic-logging gate (Task 3.3) can never silence the
required audit trail. It writes to ``access.log`` in the data-dir log directory
(honoring ``WHISPERSTREAM_DATA_DIR``) via its own ``RotatingFileHandler``.

# ============================================================================
# TRANSCRIPT DATA — DO NOT LOG
# This module logs ACCESS METADATA ONLY: timestamp, action, and record ID.
# It must NEVER receive, store, or write:
#   - transcript content (text, transcription, audio bytes, processed text)
#   - key material (data key, nonces, ciphertext, password, recovery code)
#   - personal health information of any kind
# The record_id field is validated and truncated to prevent content injection.
# ============================================================================

Allowed actions enumerate the TranscriptStore CRUD verbs plus the lockdown
lifecycle transitions the buyer-facing HIPAA summary promises are recorded
(45 CFR 164.312(b): "when your history is opened, locked, and unlocked, and
when an unlock fails"):
    open, list, get, get_audio, delete, clear, purge, save,
    lock, unlock, unlock_failed

The lifecycle actions are still content-free: they carry ONLY the action verb
and a timestamp (record_id is ``None``/``-``), never a password, recovery code,
data key, or transcript text. Any other action string is silently ignored (not
logged) so a typo or injection attempt never reaches the log file.

Log-line format (house style: key=value pairs)::

    2025-01-15 14:32:01,123 action=get id=01HXYZ-ABCD
    2025-01-15 14:32:05,456 action=list id=-
    2025-01-15 14:32:10,789 action=save id=01HABC-1234

Usage::

    from whisperstream.access_log import log_access

    log_access("get", record_id, enabled=lockdown_enabled)
    log_access("save", record_id, enabled=lockdown_enabled)
    log_access("delete", record_id, enabled=lockdown_enabled)
    log_access("list", None, enabled=lockdown_enabled)
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config_manager import get_log_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The exact set of TranscriptStore CRUD verbs this logger is allowed to record,
# plus the lockdown lifecycle transitions (lock / unlock / unlock_failed) that
# the buyer-facing 45 CFR 164.312(b) claim promises are audited. The lifecycle
# actions carry NO record_id (id=- in the log) and NEVER any secret.
# Any other action is silently rejected.
_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        "open",
        "list",
        "get",
        "get_audio",
        "delete",
        "clear",
        "purge",
        "save",
        "lock",
        "unlock",
        "unlock_failed",
        # Forgot-password last-resort reset: the encrypted history was destroyed
        # (still encrypted, never decrypted) and Lockdown turned off. Logged with
        # record_id=None — action + timestamp only, no content.
        "reset",
    }
)

# Maximum length for a sanitised record_id field.  Real transcript IDs are
# typically 26 chars (ULID) or a short UUID.  Anything longer than 128 chars
# is treated as potential content injection and truncated.
_MAX_ID_LENGTH: int = 128

# Sentinel string used when record_id is None (no specific record targeted).
_NULL_ID: str = "-"

# Logger name — deliberately outside the 'whisperstream' diagnostic hierarchy.
# The access trail has a distinct audit purpose and lifecycle.
_ACCESS_LOGGER_NAME: str = "ws_access_audit"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sanitise_id(record_id: str | None) -> str:
    """Return a safe, single-line, bounded record_id string.

    Real transcript IDs are ULIDs (26 alphanumeric chars), UUIDs (32-36
    chars), or similar compact identifiers.  Any input that contains characters
    outside the allowed set (alphanumeric, hyphen, underscore) is treated as
    not-a-real-ID and replaced with a ``[redacted]`` marker.  This prevents:

    - Log injection via embedded newlines/CRs.
    - Key=value field injection via spaces and ``=`` characters.
    - Accidental content leakage when transcript text is mistakenly passed as
      the record_id argument.

    Rules (in order):
    1. ``None``  → ``"-"`` (null sentinel).
    2. Strip leading/trailing whitespace from the raw input for a fair check.
    3. If the stripped input contains ANY character outside ``[A-Za-z0-9_-]``,
       replace the whole value with ``"[redacted]"`` (not-a-real-ID).
    4. Truncate to ``_MAX_ID_LENGTH`` chars (valid IDs are well under this;
       the cap guards against absurdly long but purely-alnum strings).
    5. Empty after stripping → ``"-"`` (null sentinel).
    """
    if record_id is None:
        return _NULL_ID
    stripped = record_id.strip()
    if not stripped:
        return _NULL_ID
    # Reject anything that contains characters outside the allowed ID charset.
    if not all(ch.isalnum() or ch in ("-", "_") for ch in stripped):
        return "[redacted]"
    # Truncate to prevent absurdly long alnum strings.
    if len(stripped) > _MAX_ID_LENGTH:
        stripped = stripped[:_MAX_ID_LENGTH]
    return stripped


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_access_logger() -> logging.Logger:
    """Return (and lazily initialise) the access audit logger.

    The access logger:
    - Has its own ``RotatingFileHandler`` writing to ``access.log`` under
      ``get_log_dir()``.  It does NOT share handlers with the ``whisperstream``
      diagnostic logger.
    - Is named ``"ws_access_audit"`` — a top-level name entirely outside the
      ``"whisperstream"`` hierarchy, so manipulating (or removing handlers from)
      the ``"whisperstream"`` diagnostic logger has zero effect here.
    - ``propagate=False`` prevents log records from travelling up to the root
      logger.
    - Never exposes transcript content, key material, or PHI.

    Design note: we resolve ``get_log_dir()`` on every call and compare it
    against the path the existing ``RotatingFileHandler`` was opened against.
    If they differ (e.g. ``WHISPERSTREAM_DATA_DIR`` changed between calls in
    tests), the old handler is closed and replaced.  In production the data dir
    never changes after startup, so the comparison is effectively free.
    """
    logger = logging.getLogger(_ACCESS_LOGGER_NAME)
    target_log_dir: Path = get_log_dir()
    expected_path: Path = target_log_dir / "access.log"

    # Check whether the existing RotatingFileHandler (if any) points at the
    # correct directory.  If not, close and remove it so we re-initialise.
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            existing_path = Path(handler.baseFilename).resolve()
            if existing_path != expected_path.resolve():
                logger.removeHandler(handler)
                handler.close()

    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        target_log_dir.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            expected_path,
            maxBytes=2 * 1024 * 1024,  # 2 MB — matches whisperstream.log sizing
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        # Minimal formatter: timestamp + the structured message (action=... id=...).
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s")
        )
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)

    # CRITICAL: do NOT propagate — this is what makes the access logger
    # independent of the 'whisperstream' diagnostic logger hierarchy.
    logger.propagate = False

    return logger


def log_access(
    action: str,
    record_id: str | None,
    *,
    enabled: bool,
) -> None:
    """Write a single content-free access-log entry.

    Logs ONLY ``action=<action> id=<sanitised_id>`` with a timestamp prefix.
    This function:

    - Silently ignores any ``action`` not in ``_ALLOWED_ACTIONS``.
    - Sanitises ``record_id`` to prevent log injection and content leakage
      (strips newlines, spaces, truncates at 128 chars).
    - Never writes transcript text, key material, or PHI.

    Parameters
    ----------
    action:
        One of the TranscriptStore CRUD verbs (``open``, ``list``, ``get``,
        ``get_audio``, ``delete``, ``clear``, ``purge``, ``save``) or a lockdown
        lifecycle transition (``lock``, ``unlock``, ``unlock_failed``). The
        lifecycle transitions pass ``record_id=None`` — action + timestamp only.
        Any other value is silently ignored.
    record_id:
        The transcript record identifier, or ``None`` for bulk/listing
        operations.  Sanitised before writing.
    enabled:
        The caller's explicit effective-Lockdown snapshot for this operation.
        ``False`` returns before the logger or audit file is created.
    """
    # The caller must pass the Lockdown-enabled snapshot captured for the
    # operation.  Keeping this gate here means an ordinary transcript call can
    # never create ``access.log`` merely because it shares a CRUD helper with
    # Lockdown Mode.
    if not enabled:
        return

    # Reject unknown / empty / injected actions before touching the logger.
    if not action or action not in _ALLOWED_ACTIONS:
        return

    safe_id = _sanitise_id(record_id)
    # House log style: key=value pairs, no content.
    message = f"action={action} id={safe_id}"
    get_access_logger().info(message)
