"""Atomic file writes for persisted state.

Write-to-temp -> flush -> ``os.fsync`` -> ``os.replace`` so a crash or power
loss between the write and the rename can never leave a truncated/partial file
at the destination. Use this for ALL persisted state (JSON state files,
config.toml, the license blob).

A plain ``path.write_text(json.dumps(...))`` for state is a durability bug: a
torn write leaves invalid bytes, the next load's ``try/except`` falls back to
defaults, and the data is silently lost. For ``byok-consent.json`` (a
legally-required append-only consent trail) that is the worst case.

Originally ``eula._atomic_write_json`` (research.md "Legal Constraint #1");
promoted here so review_state, byok_consent, license_manager, config_manager,
and eula all share one tested writer. Imports stdlib only — no whisperstream
imports, so it is safe to import from any module without a cycle.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically write *text* to *path*.

    Creates parent directories. Writes to a sibling temp file, fsyncs it, then
    ``os.replace``s it into place (atomic on POSIX, best-effort on Windows). On
    any write or rename error the temp file is cleaned up and the existing
    *path* is left untouched.

    Raises:
        OSError: on any filesystem error (caller decides whether to swallow).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Failed replacement also leaves an owned temporary file to remove.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = None,
    ensure_ascii: bool = True,
    trailing_newline: bool = False,
) -> None:
    """Serialise *data* to JSON and atomically write it to *path*.

    Thin wrapper over :func:`atomic_write_text`. Serialisation happens before
    any file is created, so a non-serialisable *data* raises ``TypeError``
    without leaving a temp file behind. ``trailing_newline`` appends a final
    newline (matches the legacy eula record format).
    """
    payload = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
    if trailing_newline:
        payload += "\n"
    atomic_write_text(path, payload)
