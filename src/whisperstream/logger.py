"""Whisperstream logging utilities.

Log-line style convention (house style for all backend log sites)
=================================================================
Every structured log line follows the pattern::

    <event verb> [key=value ...]

Where:
- The **event verb** is a short lowercase description of what happened, written
  first (e.g. ``transcription``, ``recording stopped``, ``config loaded``).
- **Key=value pairs** follow, separated by spaces.  Values never contain spaces
  unless double-quoted.
- Boolean values are lowercase ``true`` / ``false``.
- Duration values carry a trailing ``s`` unit suffix (e.g. ``duration=1.23s``).
- Absent/optional fields are **omitted entirely** (no ``field=None`` noise).

Examples::

    transcription stage=inference duration=0.84s chars=127 words=21 truncated=false
    recording stopped duration=4.2s frames=67200 paste=clipboard
    config loaded path=C:/Users/.../config.toml primary_shortcut=right_shift
"""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.logging import RichHandler

# Names that must never appear as keyword arguments to log_transcription_event
# because they would carry transcript content into the log.
_BANNED_TRANSCRIPT_KWARGS: frozenset[str] = frozenset(
    {"text", "transcript", "transcription", "processed", "chunk"}
)

_EMAIL_RE = re.compile(
    r"(?i)(?<![A-Z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+"
)
_LABELED_SECRET_RE = re.compile(
    r"(?i)\b("
    r"password|passphrase|recovery_?code|api_?key|license_?(?:key|secret)|"
    r"access_?token|refresh_?token|auth(?:orization)?|bearer|secret"
    r")\s*[=:]\s*.*?(?=\s+[A-Za-z_][A-Za-z0-9_]*\s*[=:]|$)"
)
_RECOVERY_CODE_RE = re.compile(r"(?i)\b[A-Z0-9]{4}(?:-[A-Z0-9]{4}){4}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")


def sanitize_diagnostic_text(value: object) -> str:
    """Redact recognizable identity and secret values from diagnostic text.

    This is a defense-in-depth boundary, not permission to log user content.
    Transcript, prompt, reply, and audio-derived values remain prohibited at
    their callsites because arbitrary natural language cannot be reliably
    identified after it has been formatted into a log line.
    """
    text = str(value)
    text = _EMAIL_RE.sub("<redacted-email>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _RECOVERY_CODE_RE.sub("<redacted-recovery-code>", text)
    return _LABELED_SECRET_RE.sub(
        lambda match: f"{match.group(1).lower()}=<redacted>", text
    )


class _SensitiveDiagnosticFilter(logging.Filter):
    """Sanitize the fully formatted record before any handler persists it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_diagnostic_text(record.getMessage())
        record.args = ()
        return True


def _attach_safety_filter(handler: logging.Handler) -> None:
    if not any(isinstance(item, _SensitiveDiagnosticFilter) for item in handler.filters):
        handler.addFilter(_SensitiveDiagnosticFilter())


def _has_console() -> bool:
    """Return True only when stdout is an interactive terminal."""
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def get_logger(
    name: str = "whisperstream",
    *,
    level: int = logging.INFO,
    log_dir: Optional[Path] = None,
    lockdown: bool = False,
) -> logging.Logger:
    """Return (and lazily initialise) the named diagnostic logger.

    The ``lockdown`` argument is retained for compatibility. Safe diagnostics
    remain enabled in both voluntary and Managed Lockdown.

    The independent ``ws_access_audit`` access logger (Task 3.2) is
    deliberately decoupled from this function — the lockdown gate here can
    never silence the required audit trail.

    Parameters
    ----------
    name:
        Logger name (default: ``"whisperstream"``).
    level:
        Console handler level (default: ``logging.INFO``).
    log_dir:
        Directory for the rotating file handler.  Required for file logging;
        used for the rotating diagnostic file.
    lockdown:
        Compatibility flag; it no longer suppresses safe diagnostics.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            # Only adjust console handler level; file handler stays at DEBUG
            if isinstance(handler, RichHandler):
                handler.setLevel(level)
        return logger

    # Set logger to DEBUG so the file handler receives all messages;
    # the console handler filters to the requested level
    logger.setLevel(logging.DEBUG)

    # Console handler: only when a real terminal is available
    if _has_console():
        console = Console(force_terminal=True)
        rich_handler = RichHandler(console=console, show_time=True, markup=False)
        rich_handler.setLevel(level)
        rich_handler.setFormatter(logging.Formatter("%(message)s"))
        _attach_safety_filter(rich_handler)
        logger.addHandler(rich_handler)

    # File handler: when a log directory is provided
    # Always log at DEBUG to file so the log captures full detail for inspection
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "whisperstream.log",
            maxBytes=2 * 1024 * 1024,  # 2 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        _attach_safety_filter(file_handler)
        logger.addHandler(file_handler)

    # Fallback: if no handlers were added (no console, no log_dir), add a NullHandler
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    logger.propagate = False
    return logger


def reconfigure_logging(
    lockdown: bool,
    *,
    name: str = "whisperstream",
    level: int = logging.INFO,
    log_dir: Optional[Path] = None,
) -> logging.Logger:
    """Tear down existing diagnostic handlers and rebuild for the given lockdown flag.

    This is the runtime-toggle companion to ``get_logger()``.  Because
    ``get_logger()`` returns early when ``logger.handlers`` is non-empty, a
    runtime lockdown state change requires explicit handler teardown before
    re-attaching.

    The ``lockdown`` argument is retained for compatibility. Both values rebuild
    the same safe file/console diagnostic handlers.

    Calling this function twice with the same ``lockdown`` value is idempotent —
    it will not duplicate handlers.

    This function ONLY touches the named ``whisperstream`` logger.  It never
    modifies the independent ``ws_access_audit`` access logger (Task 3.2).

    Parameters
    ----------
    lockdown:
        Compatibility flag; it no longer suppresses safe diagnostics.
    name:
        Logger name to reconfigure (default: ``"whisperstream"``).
    level:
        Console handler level for the restored diagnostic logger.
    log_dir:
        Directory for the rotating file handler when lockdown is ``False``.
    """
    logger = logging.getLogger(name)
    # Remove all existing diagnostic handlers (RichHandler / RotatingFileHandler)
    # and any leftover NullHandlers so we start fresh.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    logger.setLevel(logging.DEBUG)

    # Restore diagnostic handlers using the same logic as get_logger().
    if _has_console():
        console = Console(force_terminal=True)
        rich_handler = RichHandler(console=console, show_time=True, markup=False)
        rich_handler.setLevel(level)
        rich_handler.setFormatter(logging.Formatter("%(message)s"))
        _attach_safety_filter(rich_handler)
        logger.addHandler(rich_handler)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "whisperstream.log",
            maxBytes=2 * 1024 * 1024,  # 2 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        _attach_safety_filter(file_handler)
        logger.addHandler(file_handler)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    logger.propagate = False
    return logger


def log_transcription_event(
    logger: logging.Logger,
    stage: str,
    *,
    duration_s: Optional[float] = None,
    char_count: Optional[int] = None,
    word_count: Optional[int] = None,
    truncated: bool = False,
    level: int = logging.INFO,
    **extra: Any,
) -> None:
    """Emit a single metadata-only transcription log line.

    Formats a ``key=value`` log line under the house style convention and
    emits it at the requested level.  Transcript content is explicitly
    forbidden: passing any of ``text``, ``transcript``, ``transcription``,
    ``processed``, or ``chunk`` as a keyword argument raises :exc:`TypeError`.

    Parameters
    ----------
    logger:
        The :class:`logging.Logger` instance to log on.
    stage:
        Enum-like name for the pipeline stage, e.g. ``"inference"``,
        ``"pre_process"``, ``"chunk_in"``, ``"chunk_out"``, ``"post_process"``,
        ``"final"``.
    duration_s:
        Wall-clock seconds for this stage; omitted from the line when ``None``.
    char_count:
        Character count of the text at this stage; omitted when ``None``.
    word_count:
        Word count of the text at this stage; omitted when ``None``.
    truncated:
        Whether chunk truncation was triggered; always included as
        ``truncated=true`` or ``truncated=false``.
    level:
        Python log level (default: ``logging.INFO``).
    **extra:
        Additional ``key=value`` pairs appended to the line.  Must not contain
        any of the banned transcript-content names.

    Raises
    ------
    TypeError
        If ``extra`` contains any of the banned names:
        ``text``, ``transcript``, ``transcription``, ``processed``, ``chunk``.
    """
    banned = _BANNED_TRANSCRIPT_KWARGS & extra.keys()
    if banned:
        raise TypeError(
            f"log_transcription_event() refuses transcript-content kwargs: "
            f"{sorted(banned)}.  Pass metadata (char_count, word_count, "
            f"duration_s, truncated) instead."
        )

    parts: list[str] = [f"transcription stage={stage}"]

    if duration_s is not None:
        parts.append(f"duration={duration_s:.2f}s")

    if char_count is not None:
        parts.append(f"chars={char_count}")

    if word_count is not None:
        parts.append(f"words={word_count}")

    parts.append(f"truncated={'true' if truncated else 'false'}")

    for key, value in extra.items():
        parts.append(f"{key}={value}")

    logger.log(level, " ".join(parts))
