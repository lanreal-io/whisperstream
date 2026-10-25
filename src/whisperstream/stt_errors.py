"""Native-free STT failures and diagnostic sanitization.

The exceptions in this module are the parent-facing reliability contract.  A
failure's string form and public metadata are content-free: backend, model,
operation, and recovery state live in dedicated fields, while worker messages
and tracebacks remain bounded diagnostic fields that callers must never log.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any


_SAFE_METADATA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_QUOTED_ABSOLUTE_PATH_RE = re.compile(
    r"(?P<quote>[\"'])(?:(?:[A-Za-z]:[\\/])|(?:\\\\)|/)[^\"'\r\n]+(?P=quote)"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\"']+"
)
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|license[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+"
)


def sanitize_stt_identifier(value: object, *, fallback: str = "unknown") -> str:
    """Return one log/event-safe identifier without retaining a path value."""

    raw = str(value or "").strip()
    if _SAFE_METADATA_RE.fullmatch(raw):
        return raw
    if not raw:
        return fallback
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"redacted-{digest}"


def sanitize_stt_diagnostic_text(
    value: object,
    *,
    prohibited_values: Iterable[str] = (),
) -> str:
    """Redact known request content, paths, full emails, and secret assignments.

    The result is still diagnostic content and must stay local.  This sanitizer
    is a final containment layer for Python stdout/stderr and tracebacks; worker
    adapters must also avoid constructing exceptions from transcript-class data.
    """

    text = str(value)
    for prohibited in sorted(
        {item for item in prohibited_values if isinstance(item, str) and item},
        key=len,
        reverse=True,
    ):
        text = text.replace(prohibited, "[content redacted]")
    text = _QUOTED_ABSOLUTE_PATH_RE.sub(
        lambda match: f"{match.group('quote')}[path redacted]{match.group('quote')}",
        text,
    )
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub("[path redacted]", text)
    text = _EMAIL_RE.sub("[email redacted]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=[secret redacted]",
        text,
    )
    return text


class ModelNotPreparedError(RuntimeError):
    """The selected speech model cannot be prepared for native loading."""


class SttEngineUnavailableError(ModelNotPreparedError):
    """The configured STT engine is intentionally unavailable on this platform."""

    def __init__(self, *, backend: str, platform: str, reason: str) -> None:
        self.backend = sanitize_stt_identifier(backend)
        self.platform = sanitize_stt_identifier(platform)
        self.reason = sanitize_stt_identifier(reason)
        super().__init__(
            "STT engine unavailable "
            f"backend={self.backend} platform={self.platform} reason={self.reason}"
        )


class SttFailure(RuntimeError):
    """Base for typed, content-free parent-side STT failures."""

    code = "stt_failure"

    def __init__(
        self,
        *,
        backend: str,
        model_identifier: str,
        operation: str,
        recovery_attempted: bool = False,
        suppressed: bool = False,
    ) -> None:
        self.engine = "speech_to_text"
        self.backend = sanitize_stt_identifier(backend)
        self.model_identifier = sanitize_stt_identifier(model_identifier)
        self.operation = sanitize_stt_identifier(operation)
        self.recovery_attempted = bool(recovery_attempted)
        self.suppressed = bool(suppressed)
        super().__init__(
            "STT worker failure "
            f"code={self.code} backend={self.backend} "
            f"model={self.model_identifier} operation={self.operation}"
        )

    def public_metadata(self) -> dict[str, Any]:
        """Return the exact content-free API/WebSocket failure contract."""

        return {
            "engine": self.engine,
            "backend": self.backend,
            "model_id": self.model_identifier,
            "operation": self.operation,
            "code": self.code,
            "recovery_attempted": self.recovery_attempted,
            "suppressed": self.suppressed,
        }


class NativeSttWorkerCrashed(SttFailure):
    """An unexpected worker exit exhausted or was denied recovery."""

    code = "worker_native_exit"

    def __init__(
        self,
        *,
        backend: str,
        model_identifier: str,
        operation: str,
        report: object | None,
        recovery_attempted: bool,
        suppressed: bool,
    ) -> None:
        self.report = report
        super().__init__(
            backend=backend,
            model_identifier=model_identifier,
            operation=operation,
            recovery_attempted=recovery_attempted,
            suppressed=suppressed,
        )


class SttWorkerStartFailure(NativeSttWorkerCrashed):
    """The parent could not spawn or negotiate a worker process."""

    code = "worker_start_failed"


class SttWorkerProtocolFailure(NativeSttWorkerCrashed):
    """The worker transport or correlated reply violated protocol v1."""

    code = "worker_protocol_failed"


class SttWorkerTimeoutFailure(NativeSttWorkerCrashed):
    """A bounded worker operation did not finish before its deadline."""

    code = "worker_timeout"


class SttWorkerPythonFailure(SttFailure):
    """The live worker returned an ordinary structured Python exception."""

    code = "worker_python_exception"

    def __init__(
        self,
        *,
        backend: str,
        model_identifier: str,
        operation: str,
        exception_type: str,
        diagnostic_message: str,
        diagnostic_traceback: str,
    ) -> None:
        self.exception_type = sanitize_stt_identifier(
            exception_type,
            fallback="Exception",
        )
        # Diagnostic fields are deliberately separate from __str__ and
        # public_metadata(). They are bounded by protocol v1 and must not be
        # interpolated into parent logs or client events.
        self.diagnostic_message = sanitize_stt_diagnostic_text(diagnostic_message)
        self.diagnostic_traceback = sanitize_stt_diagnostic_text(
            diagnostic_traceback
        )
        super().__init__(
            backend=backend,
            model_identifier=model_identifier,
            operation=operation,
        )


__all__ = [
    "ModelNotPreparedError",
    "NativeSttWorkerCrashed",
    "SttEngineUnavailableError",
    "SttFailure",
    "SttWorkerProtocolFailure",
    "SttWorkerPythonFailure",
    "SttWorkerStartFailure",
    "SttWorkerTimeoutFailure",
    "sanitize_stt_diagnostic_text",
    "sanitize_stt_identifier",
]
