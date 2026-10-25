"""Owned temporary-file creation and ordinary cleanup for Whisperstream.

Only paths carrying ``OWNED_TEMP_PREFIX`` are eligible for the stale sweep.
Legacy generic temp names are deliberately unattributable and must never be
removed by this module. Cleanup is ordinary filesystem unlink, not secure
erasure of storage blocks, journals, backups, or prior copies.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Iterable, Protocol


OWNED_TEMP_PREFIX = "whisperstream-owned-v1-"
STALE_OWNED_TEMP_SECONDS = 24 * 60 * 60
_CLEANUP_ATTEMPTS = 3
_CLEANUP_RETRY_SECONDS = 0.05
_SAFE_SUFFIX_RE = re.compile(r"\.[a-z0-9]{1,12}\Z", re.IGNORECASE)
_SAFE_PURPOSE_RE = re.compile(r"[^a-z0-9]+", re.IGNORECASE)


class _Closeable(Protocol):
    def close(self) -> object: ...


def safe_source_suffix(filename: str | None, *, fallback: str = ".audio") -> str:
    """Return a harmless final suffix from an untrusted upload filename.

    Directory components, multiple-extension prefixes, punctuation, and long
    suffixes are discarded. The lowercase final suffix is preserved when it is
    a dot plus 1-12 ASCII letters/digits so audio loaders can select the right
    decoder without incorporating the source filename into the temp path.
    """
    if filename:
        suffix = Path(filename).suffix.lower()
        if _SAFE_SUFFIX_RE.fullmatch(suffix):
            return suffix
    return fallback


def _temp_root() -> Path:
    """System temp root seam, kept separate for conservative focused tests."""
    return Path(tempfile.gettempdir())


def _owned_prefix(purpose: str) -> str:
    normalized = _SAFE_PURPOSE_RE.sub("-", purpose).strip("-").lower()
    return f"{OWNED_TEMP_PREFIX}{normalized or 'temp'}-"


def create_owned_temp_file(
    *, purpose: str, suffix: str = ""
) -> tuple[BinaryIO, Path]:
    """Create a private temp file carrying Whisperstream's owned prefix.

    Before creating a new file, sweep only stale files from the same owned
    namespace. ``mkstemp`` creates the file with restrictive permissions where
    the platform supports them. The caller owns the returned binary handle and
    must pass it to :func:`close_then_unlink` on every terminal path.
    """
    sweep_stale_owned_temp_files()
    fd, raw_path = tempfile.mkstemp(
        dir=_temp_root(),
        prefix=_owned_prefix(purpose),
        suffix=suffix,
    )
    try:
        return os.fdopen(fd, "w+b"), Path(raw_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        close_then_unlink(raw_path)
        raise


def _unlink(path: Path) -> None:
    """Unlink seam used by the retry/close-order tests."""
    path.unlink(missing_ok=True)


def close_then_unlink(
    path: str | os.PathLike[str] | None,
    handles: Iterable[_Closeable | None] = (),
    *,
    attempts: int = _CLEANUP_ATTEMPTS,
    retry_seconds: float = _CLEANUP_RETRY_SECONDS,
) -> bool:
    """Close every supplied handle, then verify/retry ordinary path removal.

    Close failures never skip unlink attempts. This matters on Windows, where a
    still-open SQLite/file handle commonly blocks deletion. Returns ``True``
    when there is no path or the path is confirmed absent, otherwise ``False``.
    It never raises so cleanup cannot mask the operation's real result.
    """
    for handle in handles:
        if handle is None:
            continue
        try:
            handle.close()
        except Exception:  # noqa: BLE001 - cleanup remains best-effort
            pass

    if path is None:
        return True

    target = Path(path)
    try:
        in_owned_namespace = (
            target.name.startswith(OWNED_TEMP_PREFIX)
            and target.parent.resolve() == _temp_root().resolve()
        )
    except OSError:
        in_owned_namespace = False
    if not in_owned_namespace:
        return False

    for attempt in range(max(1, attempts)):
        try:
            _unlink(target)
        except OSError:
            pass
        try:
            if not target.exists():
                return True
        except OSError:
            # An unstatable path is not proof of deletion; retry conservatively.
            pass
        if attempt + 1 < max(1, attempts) and retry_seconds > 0:
            time.sleep(retry_seconds)
    return False


def sweep_stale_owned_temp_files(
    *,
    older_than_seconds: float = STALE_OWNED_TEMP_SECONDS,
    now: float | None = None,
) -> int:
    """Remove only stale regular files bearing Whisperstream's owned prefix.

    Symlinks, directories, fresh files, and every unattributable legacy name
    (including generic ``*.favicons`` files) are ignored. Returns the count of
    paths confirmed absent after cleanup and never raises.
    """
    root = _temp_root()
    cutoff = (time.time() if now is None else now) - max(0.0, older_than_seconds)
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0

    for entry in entries:
        if not entry.name.startswith(OWNED_TEMP_PREFIX):
            continue
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            if entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        if close_then_unlink(entry):
            removed += 1
    return removed
