"""Shared sanitizer and speech/label normalizer for Claude session names.

Two pure functions, no I/O, no logging. ``sanitize_session_name`` mirrors
``agent_activity._project_basename``'s treatment so a resolved name always
satisfies the WS boundary's ``isSafeSourceLabel`` contract (non-empty, no
slash, no control characters, length-capped). ``normalize_session_name_for_speech``
is the shared normalization contract used by both the TTS delivery prefix and,
via its TypeScript twin, the HUD's accessible labels.

Also owns a never-raising, byte-capped reader for the ``/rename`` name
(``read_custom_title``) plus its bounded, memory-only per-session cache
(``TranscriptTitleCache``). The reader never parses anything but the raw
``custom-title`` transcript record, and never lets a path reach an exception
message.

# ============================================================================
# TRANSCRIPT DATA - DO NOT LOG
# A session name is transcript-class. Never log the value returned by any
# function below, and never interpolate it (or a transcript path) into an
# exception message.
# ============================================================================
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path

MAX_SESSION_NAME_CHARS = 32

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WHITESPACE_RUN_RE = re.compile(r"\s+")

# 128 KB tail window: ~4x the measured 32.8 KB worst-case distance from EOF to
# the last `custom-title` record across 95 real transcripts (research.md).
_TAIL_WINDOW_BYTES = 131072
_CUSTOM_TITLE_MARKER = "custom-title"

# Shared with SessionActivationRegistry (agent_events.py), which imports
# `bounded_session_key` from this module rather than re-implementing it. This
# module has no whisperstream imports of its own, so agent_events.py importing
# from here cannot create a cycle.
_MAX_RAW_SESSION_CHARS = 256
_MAX_CACHED_TITLE_SESSIONS = 400


def _default_claude_projects_root() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_root = Path(configured) if configured else Path.home() / ".claude"
    return claude_root / "projects"


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _safe_transcript_stat(
    transcript_path: object,
    projects_root: Path,
) -> tuple[Path, os.stat_result] | None:
    """Validate one regular ``.jsonl`` beneath Claude's projects root."""

    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        root = Path(os.path.abspath(projects_root))
        candidate = Path(os.path.abspath(transcript_path))
        if candidate.suffix.lower() != ".jsonl":
            return None
        common = os.path.commonpath(
            (os.path.normcase(str(root)), os.path.normcase(str(candidate)))
        )
        if common != os.path.normcase(str(root)) or candidate == root:
            return None
        relative = candidate.relative_to(root)
        for component in (Path(), *relative.parents[::-1], relative):
            checked = root if component == Path() else root / component
            result = checked.lstat()
            if stat.S_ISLNK(result.st_mode) or _is_reparse(result):
                return None
        result = candidate.lstat()
        if not stat.S_ISREG(result.st_mode) or _is_reparse(result):
            return None
        return candidate, result
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return None


def bounded_session_key(session_id: object) -> str | None:
    """Normalize a session id into a bounded, collision-resistant key.

    Session ids at or under ``_MAX_RAW_SESSION_CHARS`` pass through
    unchanged; longer ones become a stable ``sha256:`` digest so a
    pathological id cannot grow a keyed collection's memory unbounded. Never
    raises. Shared by :class:`TranscriptTitleCache` and
    ``agent_events.SessionActivationRegistry``.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    if len(session_id) <= _MAX_RAW_SESSION_CHARS:
        return session_id
    digest = hashlib.sha256(session_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"sha256:{digest}"


def sanitize_session_name(value: object) -> str | None:
    """Sanitize an untrusted candidate session name for storage and display.

    Rejects non-``str``/empty input, replaces every non-printable character
    or ``/``/``\\`` with a space, collapses whitespace runs, and caps the
    result at ``MAX_SESSION_NAME_CHARS``. Returns ``None`` when the sanitized
    result is empty.
    """
    if not isinstance(value, str) or not value:
        return None
    printable = "".join(
        character if character.isprintable() and character not in "/\\" else " "
        for character in value
    )
    compact = " ".join(printable.split())
    if not compact:
        return None
    return compact[:MAX_SESSION_NAME_CHARS]


def normalize_session_name_for_speech(value: str) -> str:
    """Normalize a sanitized name for speech and accessible-label display.

    Hyphens, underscores, and slashes become spaces; camelCase/PascalCase
    boundaries get a space inserted; whitespace runs collapse to one space;
    leading/trailing whitespace is stripped. Does not change case and does
    not add or remove a terminator.
    """
    translated = "".join(" " if character in "-_/" else character for character in value)
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", translated)
    return _WHITESPACE_RUN_RE.sub(" ", spaced).strip()


def read_custom_title(
    transcript_path: object,
    *,
    projects_root: Path | None = None,
) -> str | None:
    """Best-effort tail read of a session transcript's last ``custom-title``.

    Opens the file in binary, seeks to the last ``_TAIL_WINDOW_BYTES`` (128 KB)
    of the file, decodes with ``errors="replace"``, and reverse-scans lines so
    the most recent title wins over an earlier one. Only a line whose raw text
    contains the substring ``custom-title`` is ever ``json.loads``'d; every
    other record type in the transcript is skipped without parsing. The
    winning title is passed through :func:`sanitize_session_name` before it is
    returned, so the result always satisfies the same safety contract as any
    other candidate name.

    Never raises: a non-``str``/empty path, a missing file, a directory, an
    unreadable file, invalid UTF-8, or an unparseable line all fall through to
    ``None``. Never puts ``transcript_path`` into an exception message.
    """
    validated = _safe_transcript_stat(
        transcript_path,
        projects_root or _default_claude_projects_root(),
    )
    if validated is None:
        return None
    safe_path, path_stat = validated
    try:
        with open(safe_path, "rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or _is_reparse(opened_stat)
                or opened_stat.st_dev != path_stat.st_dev
                or opened_stat.st_ino != path_stat.st_ino
            ):
                return None
            # Claude Code keeps appending while its hooks run, so the size can
            # grow between the path stat and the open. Identity (device and
            # inode) is the safety check; the tail is read from the open file.
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - _TAIL_WINDOW_BYTES))
            raw = handle.read()
    except (FileNotFoundError, OSError, ValueError):
        return None
    text = raw.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        if _CUSTOM_TITLE_MARKER not in line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") != _CUSTOM_TITLE_MARKER:
            continue
        title = record.get("customTitle")
        sanitized = sanitize_session_name(title)
        if sanitized is not None:
            return sanitized
    return None


class TranscriptTitleCache:
    """Bounded, memory-only, mtime/size-gated cache of resolved transcript titles.

    ``get(session_id, transcript_path)`` avoids re-reading a transcript that
    has not changed since the last look: it ``os.stat``s the file and only
    calls :func:`read_custom_title` again when ``(mtime_ns, size)`` differ
    from the cached entry. A failed safety check returns ``None`` rather than
    reusing a title for a different path. Oldest entry is evicted once the
    cache exceeds ``_MAX_CACHED_TITLE_SESSIONS`` sessions. A backend restart
    clears it, same as ``SessionActivationRegistry``.
    """

    def __init__(
        self,
        *,
        max_sessions: int = _MAX_CACHED_TITLE_SESSIONS,
        projects_root: Path | None = None,
    ) -> None:
        if (
            not isinstance(max_sessions, int)
            or isinstance(max_sessions, bool)
            or max_sessions < 1
        ):
            raise ValueError("max_sessions must be a positive integer")
        self._max_sessions = max_sessions
        self._projects_root = projects_root or _default_claude_projects_root()
        self._lock = threading.Lock()
        # key -> (mtime_ns, size, title)
        self._entries: dict[str, tuple[int, int, str | None]] = {}

    def get(self, session_id: object, transcript_path: object) -> str | None:
        key = bounded_session_key(session_id)
        if key is None:
            return None
        validated = _safe_transcript_stat(transcript_path, self._projects_root)
        stat_result = validated[1] if validated is not None else None
        with self._lock:
            cached = self._entries.get(key)
        if stat_result is None:
            return None
        mtime_ns, size = stat_result.st_mtime_ns, stat_result.st_size
        if cached is not None and cached[0] == mtime_ns and cached[1] == size:
            return cached[2]
        title = read_custom_title(
            transcript_path,
            projects_root=self._projects_root,
        )
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = (mtime_ns, size, title)
            while len(self._entries) > self._max_sessions:
                oldest = next(iter(self._entries))
                del self._entries[oldest]
        return title

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
