"""Bounded frontend diagnostic retention and user-initiated ZIP assembly."""

from __future__ import annotations

import io
import json
import logging
import platform
import re
import zipfile
from collections.abc import Callable

from ..config_manager import get_log_dir
from ..logger import sanitize_diagnostic_text
from ..safe_diagnostics import SafeDiagnosticSink

_FRONTEND_LOG_SOURCES = frozenset(
    {
        "main",
        "hud",
        "popup-eula",
        "popup-review",
        "popup-update",
        "recording-control",
        "snap-guide",
    }
)
_FRONTEND_LOG_UNKNOWN_SOURCE = "unknown"
_FRONTEND_LOG_MAX_ENTRIES = 500
_FRONTEND_LOG_MAX_LINE_BYTES = 2_048
_FRONTEND_LOG_MAX_TOTAL_BYTES = 128 * 1_024
_SENSITIVE_FRONTEND_FIELD_RE = re.compile(
    r"(?i)\b("
    r"(?:file|filename|path|url|endpoint|process|domain|title|device|hostname)"
    r"|(?:session|record|recording|prompt|interaction|request|source)_?id"
    r")\s*=\s*.*?(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)
_RAW_FRONTEND_ERROR_FIELD_RE = re.compile(r"(?i)\b(?:error|message|reason)\s*=.*$")
_URL_RE = re.compile(r"(?i)\b(?:https?|file)://\S+")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:\b[A-Z]:[\\/]|\\\\)[^\s]+")
_UNIX_USER_PATH_RE = re.compile(r"/(?:Users|home|tmp|var/tmp)/[^\s]+")


def _normalize_frontend_log_source(source: object) -> str:
    """Collapse client-controlled source labels onto a finite allowlist."""
    return source if isinstance(source, str) and source in _FRONTEND_LOG_SOURCES else _FRONTEND_LOG_UNKNOWN_SOURCE


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _sanitize_frontend_log_line(line: object) -> str | None:
    """Bound and redact one untrusted WebView diagnostic line."""
    if not isinstance(line, str):
        return None
    sanitized = "".join(
        character if character >= " " and character != "\x7f" else " "
        for character in line
    )
    sanitized = sanitize_diagnostic_text(sanitized)
    sanitized = _URL_RE.sub("<redacted-url>", sanitized)
    sanitized = _WINDOWS_PATH_RE.sub("<redacted-path>", sanitized)
    sanitized = _UNIX_USER_PATH_RE.sub("<redacted-path>", sanitized)
    sanitized = _SENSITIVE_FRONTEND_FIELD_RE.sub(
        lambda match: f"{match.group(1).lower()}=<redacted>", sanitized
    )
    sanitized = _RAW_FRONTEND_ERROR_FIELD_RE.sub(
        lambda match: f"{match.group(0).split('=', 1)[0].strip().lower()}=<redacted>",
        sanitized,
    )
    # Frontend diagnostic text is code-authored ASCII. Dropping non-ASCII
    # prevents hook/transcript content and content-derived Unicode fingerprints
    # from surviving a compromised or stale WebView caller.
    sanitized = sanitized.encode("ascii", errors="ignore").decode("ascii")
    sanitized = " ".join(sanitized.split())
    if not sanitized:
        return None
    return _truncate_utf8(sanitized, _FRONTEND_LOG_MAX_LINE_BYTES)


def _sanitize_frontend_logs(lines: object) -> list[str]:
    """Keep the newest bounded, sanitized lines within one byte budget."""
    if not isinstance(lines, list):
        return []
    kept_reversed: list[str] = []
    total_bytes = 0
    for raw_line in reversed(lines[-_FRONTEND_LOG_MAX_ENTRIES:]):
        line = _sanitize_frontend_log_line(raw_line)
        if line is None:
            continue
        line_bytes = len(line.encode("utf-8"))
        if total_bytes + line_bytes > _FRONTEND_LOG_MAX_TOTAL_BYTES:
            break
        kept_reversed.append(line)
        total_bytes += line_bytes
    return list(reversed(kept_reversed))


def _bound_frontend_log_buffers(buffers: dict[str, list[str]]) -> None:
    """Evict oldest lines until all WebView buffers share one byte cap."""
    def total_bytes() -> int:
        return sum(
            len(line.encode("utf-8"))
            for lines in buffers.values()
            for line in lines
        )

    timestamp_re = re.compile(r"^\[([^\]]+)\]")
    while total_bytes() > _FRONTEND_LOG_MAX_TOTAL_BYTES:
        oldest_source: str | None = None
        oldest_key: tuple[str, str] | None = None
        for source, lines in buffers.items():
            if not lines:
                continue
            match = timestamp_re.match(lines[0])
            key = (match.group(1) if match else "", source)
            if oldest_key is None or key < oldest_key:
                oldest_key = key
                oldest_source = source
        if oldest_source is None:
            break
        buffers[oldest_source].pop(0)


class DiagnosticLogs:
    """One server's frontend buffers and explicit support-export dependencies."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        safe_diagnostics: SafeDiagnosticSink,
        software_version: str,
        model_loaded: Callable[[], bool],
    ) -> None:
        self._logger = logger
        self._safe_diagnostics = safe_diagnostics
        self._software_version = software_version
        self._model_loaded = model_loaded
        # Windows post independently; one sync must not replace another buffer.
        self._frontend_logs: dict[str, list[str]] = {}

    def store(self, source: object, lines: object) -> tuple[str, int]:
        """Replace one source buffer and enforce the aggregate ingest cap."""
        normalized_source = _normalize_frontend_log_source(source)
        self._frontend_logs[normalized_source] = _sanitize_frontend_logs(lines)
        _bound_frontend_log_buffers(self._frontend_logs)
        return normalized_source, len(self._frontend_logs[normalized_source])

    def clear(self) -> None:
        """Idempotently reset all synced frontend diagnostics and accounting."""
        self._frontend_logs.clear()

    def merge_frontend_logs(self, override: list[str] | None = None, override_source: str = "main") -> list[str]:
        """Combine per-source log buffers into one chronologically-ordered list,
        tagged with the source webview. Optionally override one source with a
        fresher buffer (used when a window POSTs its current logs to /api/logs/export)."""
        buffers: dict[str, list[str]] = {
            _normalize_frontend_log_source(source): _sanitize_frontend_logs(lines)
            for source, lines in self._frontend_logs.items()
        }
        if override is not None:
            buffers[_normalize_frontend_log_source(override_source)] = (
                _sanitize_frontend_logs(override)
            )
        _bound_frontend_log_buffers(buffers)

        ts_pattern = re.compile(r"^\[([^\]]+)\]")  # leading [ISO timestamp]
        tagged: list[tuple[str, str]] = []  # (timestamp, line)
        for source, lines in buffers.items():
            for line in lines:
                m = ts_pattern.match(line)
                ts = m.group(1) if m else ""
                tagged.append((ts, f"[{source}] {line}"))
        tagged.sort(key=lambda t: t[0])
        return [line for _, line in tagged]

    def build_log_zip(self, frontend_logs: list[str]) -> io.BytesIO:
        """Assemble a zip with backend logs, frontend logs, and system info."""
        # --- Force flush every active log handler ---
        # FileHandler.emit() normally flushes per call, but explicitly flushing
        # before reading the file guarantees the export captures everything
        # buffered up to this moment (including any in-flight writes from
        # other threads). Cheap and defensive.
        for h in list(logging.getLogger().handlers) + list(self._logger.handlers):
            try:
                h.flush()
            except Exception:
                pass

        # --- Backend logs ---
        log_dir = get_log_dir()
        backend_lines: list[str] = []
        for suffix in [".3", ".2", ".1", ""]:
            log_file = log_dir / f"whisperstream{suffix}.log"
            if log_file.exists():
                try:
                    backend_lines.extend(
                        log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                    )
                except Exception:
                    pass
        backend_lines = [
            sanitize_diagnostic_text(line) for line in backend_lines[-5000:]
        ]

        # --- System info ---
        system_info = json.dumps({
            "app_version": self._software_version,
            "os": platform.platform(),
            "python_version": platform.python_version(),
            "model_loaded": self._model_loaded(),
        }, indent=2)

        # --- Assemble zip ---
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("backend.log", "\n".join(backend_lines))
            zf.writestr("frontend.log", "\n".join(frontend_logs))
            zf.writestr("system-info.json", system_info)
            safe_diagnostics = self._safe_diagnostics.serialized()
            if safe_diagnostics:
                zf.writestr("safe-diagnostics.jsonl", safe_diagnostics)
        buf.seek(0)
        return buf
