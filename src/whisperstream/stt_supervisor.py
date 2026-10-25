"""Reusable parent-side supervisor for crash-isolated STT workers.

This module owns process recovery policy, not native speech code.  It must stay
free of sherpa/ONNX imports and platform-specific transport types so the later
macOS worker can use the same parent lifecycle.

DO NOT LOG request/reply bodies, audio, transcript text, preview text,
hotwords, language hints, or worker traceback text.  Only content-free process
and operation metadata may cross into routine parent logs.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .stt_errors import (
    NativeSttWorkerCrashed,
    SttFailure,
    SttWorkerProtocolFailure,
    SttWorkerPythonFailure,
    SttWorkerStartFailure,
    SttWorkerTimeoutFailure,
    sanitize_stt_identifier,
)
from .stt_worker_process import (
    INFERENCE_TIMEOUT_SECONDS,
    LOAD_TIMEOUT_SECONDS,
    SHUTDOWN_TIMEOUT_SECONDS,
    STARTUP_TIMEOUT_SECONDS,
    SttWorkerExitReport,
    SttWorkerExited,
    SttWorkerProcess,
    SttWorkerProcessError,
    SttWorkerRemoteError,
    SttWorkerRequestTimeout,
    frozen_worker_command,
    source_worker_command,
)
from .stt_worker_protocol import SttWorkerProtocolError


RECOVERY_SUPPRESSION_SECONDS = 60.0


WorkerFactory = Callable[[], SttWorkerProcess]


def default_worker_command() -> tuple[str, ...]:
    """Return the source or current frozen-engine worker command."""

    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        onefile_binary = os.environ.get("NUITKA_ONEFILE_BINARY")
        if onefile_binary:
            return frozen_worker_command(Path(onefile_binary))
        try:
            launched_executable = Path(sys.argv[0]).resolve()
            if launched_executable.suffix.lower() == ".exe":
                return frozen_worker_command(launched_executable)
        except (IndexError, OSError):
            pass
        return frozen_worker_command(Path(sys.executable))
    repo_root = Path(__file__).resolve().parents[2]
    return source_worker_command(repo_root)


class SttWorkerSupervisor:
    """Own one worker and apply bounded one-recovery crash policy.

    All exchanges are serialized across process replacement.  The first
    eligible failure for an operation starts one replacement, reloads the
    remembered model when necessary, and retries the operation once.  A second
    matching failure enters a bounded suppression window so callers cannot
    create a restart loop.
    """

    def __init__(
        self,
        *,
        backend: str,
        model_identifier: str,
        diagnostic_dir: Path,
        logger: logging.Logger,
        command: Sequence[str | os.PathLike[str]] | None = None,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        process_factory: WorkerFactory | None = None,
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
        load_timeout: float = LOAD_TIMEOUT_SECONDS,
        inference_timeout: float = INFERENCE_TIMEOUT_SECONDS,
        shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
        suppression_seconds: float = RECOVERY_SUPPRESSION_SECONDS,
    ) -> None:
        self.backend = sanitize_stt_identifier(backend)
        self.model_identifier = sanitize_stt_identifier(model_identifier)
        self._diagnostic_dir = Path(diagnostic_dir)
        self._logger = logger
        self._command = tuple(str(item) for item in (command or default_worker_command()))
        self._cwd = Path(cwd) if cwd is not None else Path(__file__).resolve().parents[2]
        self._environment = dict(environment or {})
        self._process_factory = process_factory
        self._startup_timeout = startup_timeout
        self._load_timeout = load_timeout
        self._inference_timeout = inference_timeout
        self._shutdown_timeout = shutdown_timeout
        self._suppression_seconds = suppression_seconds
        self._lock = threading.RLock()
        self._process: SttWorkerProcess | None = None
        self._capabilities: dict[str, Any] = {}
        self._load_body: dict[str, Any] | None = None
        self._loaded = False
        self._shutdown = False
        self._recovery_count = 0
        self._recovery_model_key: tuple[str, str | None] = (
            self.model_identifier,
            None,
        )
        self._suppressed_until: dict[tuple[str, str, str], float] = {}
        self._last_failure_report: SttWorkerExitReport | None = None
        self._last_failure: SttFailure | None = None

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._process.pid if self._process is not None else None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.is_running

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._loaded

    @property
    def capabilities(self) -> dict[str, Any]:
        """Return the last negotiated optional capability map."""

        with self._lock:
            return dict(self._capabilities)

    @property
    def recovery_count(self) -> int:
        with self._lock:
            return self._recovery_count

    @property
    def last_failure_report(self) -> SttWorkerExitReport | None:
        with self._lock:
            return self._last_failure_report

    @property
    def last_failure(self) -> SttFailure | None:
        """Return the last content-free typed failure until a clean success."""

        with self._lock:
            return self._last_failure

    def start(self) -> dict[str, Any]:
        """Start and negotiate the owned worker once."""

        with self._lock:
            self._require_available()
            return self._ensure_process().negotiated or {}

    def load(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Load the configured native model, remembering config for recovery."""

        with self._lock:
            self._require_available()
            candidate = dict(body)
            candidate_model = candidate.get("model_name")
            candidate_quantization = candidate.get("quantization")
            if (
                isinstance(candidate_model, str)
                and candidate_model
            ):
                model_key = (
                    candidate_model,
                    (
                        candidate_quantization
                        if isinstance(candidate_quantization, str)
                        else None
                    ),
                )
                if model_key != self._recovery_model_key:
                    identifier = (
                        f"{candidate_model}:{candidate_quantization}"
                        if isinstance(candidate_quantization, str)
                        and candidate_quantization
                        else candidate_model
                    )
                    self.model_identifier = sanitize_stt_identifier(identifier)
                    self._recovery_model_key = model_key
                    self._clear_recovery_state()
            result = self._request_with_recovery(
                "load",
                body=candidate,
                timeout=self._load_timeout,
            )
            self._load_body = candidate
            self._loaded = bool(result.get("loaded", True))
            return result

    def unload(self) -> dict[str, Any]:
        """Release the recognizer while retaining lazy-reload configuration."""

        with self._lock:
            self._require_available()
            if self._process is None:
                self._loaded = False
                return {"state": "idle", "loaded": False}
            result = self._request_with_recovery("unload")
            self._loaded = False
            return result

    def stop_worker(self) -> SttWorkerExitReport | None:
        """Unload and exit the current worker while keeping lazy reuse enabled.

        Idle offload and explicit manager unload use this stronger lifecycle
        boundary so native libraries and recognizer memory leave the process
        tree. A later load creates exactly one fresh worker.
        """

        with self._lock:
            self._require_available()
            process = self._process
            if process is None:
                self._loaded = False
                return self._last_failure_report
            try:
                if self._loaded and process.is_running:
                    process.request("unload", timeout=self._control_timeout("unload"))
                return process.shutdown(timeout=self._shutdown_timeout)
            except (
                SttWorkerProcessError,
                SttWorkerProtocolError,
                subprocess.TimeoutExpired,
            ):
                return process.terminate_owned_worker(expected=False)
            finally:
                self._loaded = False
                self._process = None

    def health(self) -> dict[str, Any]:
        """Return content-free worker/engine lifecycle health."""

        with self._lock:
            if self._shutdown:
                return {
                    "engine_id": self.backend,
                    "state": "shutdown",
                    "loaded": False,
                }
            if self._process is None:
                if self._last_failure is not None:
                    return {
                        "engine_id": self.backend,
                        "state": "unavailable",
                        "loaded": False,
                    }
                return {
                    "engine_id": self.backend,
                    "state": "idle",
                    "loaded": False,
                }
            result = self._request_with_recovery("health")
            self._loaded = bool(result.get("loaded", False))
            return result

    def request_audio(
        self,
        operation: str,
        audio: np.ndarray,
        *,
        sample_rate: int,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send authoritative or preview audio through the supervised worker."""

        with self._lock:
            self._require_available()
            return self._request_with_recovery(
                operation,
                body=body,
                audio=audio,
                sample_rate=sample_rate,
                timeout=self._inference_timeout,
            )

    def update_hotwords(self, hotwords: list[str], score: float) -> dict[str, Any]:
        """Apply supported hotwords without exposing their values to logs."""

        with self._lock:
            self._require_available()
            result = self._request_with_recovery(
                "update_hotwords",
                body={"hotwords": list(hotwords), "score": float(score)},
                timeout=self._load_timeout,
            )
            if self._load_body is not None:
                self._load_body["hotwords"] = list(hotwords)
                self._load_body["hotword_score"] = float(score)
            self._loaded = bool(result.get("loaded", True))
            return result

    def set_language_hint(self, language: str | None) -> dict[str, Any]:
        """Apply a supported dynamic language hint and retain it for recovery."""

        with self._lock:
            self._require_available()
            if language is not None and not isinstance(language, str):
                raise TypeError("language hint must be a string or None")
            if self._process is None or not self._loaded:
                if self._load_body is not None:
                    self._load_body["forced_language"] = language
                return {"updated": True, "loaded": False}
            result = self._request_with_recovery(
                "set_language_hint",
                body={"language": language},
            )
            if self._load_body is not None:
                self._load_body["forced_language"] = language
            return result

    def shutdown(self) -> SttWorkerExitReport | None:
        """Idempotently stop only the owned worker and suppress recovery."""

        with self._lock:
            if self._shutdown:
                return (
                    self._process.last_exit_report
                    if self._process is not None
                    else self._last_failure_report
                )
            self._shutdown = True
            self._loaded = False
            process = self._process
            if process is None:
                return self._last_failure_report
            try:
                return process.shutdown(timeout=self._shutdown_timeout)
            except (
                SttWorkerProcessError,
                SttWorkerProtocolError,
                subprocess.TimeoutExpired,
            ):
                return process.terminate_owned_worker(expected=False)
            finally:
                self._process = None

    def _request_with_recovery(
        self,
        operation: str,
        *,
        body: Mapping[str, Any] | None = None,
        audio: np.ndarray | None = None,
        sample_rate: int = 16_000,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        key = (self.backend, self.model_identifier, operation)
        now = time.monotonic()
        suppressed_until = self._suppressed_until.get(key, 0.0)
        if suppressed_until > now:
            failure = NativeSttWorkerCrashed(
                backend=self.backend,
                model_identifier=self.model_identifier,
                operation=operation,
                report=self._last_failure_report,
                recovery_attempted=False,
                suppressed=True,
            )
            self._last_failure = failure
            raise failure
        self._suppressed_until.pop(key, None)

        try:
            result = self._request_once(
                operation,
                body=body,
                audio=audio,
                sample_rate=sample_rate,
                timeout=timeout,
            )
            self._record_success()
            return result
        except self._eligible_failures() as first_error:
            first_report = self._discard_failed_process(first_error)
            self._last_failure_report = first_report
            self._log_failure(
                operation=operation,
                report=first_report,
                restart_decision="recover_once",
            )

        self._recovery_count += 1
        try:
            self._ensure_process()
            if operation != "load" and self._load_body is not None and self._loaded:
                self._request_once(
                    "load",
                    body=self._load_body,
                    timeout=self._load_timeout,
                )
            result = self._request_once(
                operation,
                body=body,
                audio=audio,
                sample_rate=sample_rate,
                timeout=timeout,
            )
            self._record_success()
            return result
        except self._eligible_failures() as retry_error:
            retry_report = self._discard_failed_process(retry_error)
            self._last_failure_report = retry_report
            self._suppressed_until[key] = time.monotonic() + self._suppression_seconds
            self._log_failure(
                operation=operation,
                report=retry_report,
                restart_decision="suppressed",
            )
            failure = self._typed_recovery_failure(
                retry_error,
                operation=operation,
                report=retry_report,
                recovery_attempted=True,
                suppressed=True,
            )
            self._last_failure = failure
            raise failure from retry_error

    def _request_once(
        self,
        operation: str,
        *,
        body: Mapping[str, Any] | None = None,
        audio: np.ndarray | None = None,
        sample_rate: int = 16_000,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        process = self._ensure_process()
        try:
            if audio is not None:
                return process.request_audio(
                    operation,
                    audio,
                    sample_rate=sample_rate,
                    body=body,
                    timeout=timeout or self._inference_timeout,
                )
            return process.request(
                operation,
                body=body,
                timeout=timeout or self._control_timeout(operation),
            )
        except SttWorkerRemoteError as exc:
            failure = self._typed_remote_failure(operation, exc)
            self._last_failure = failure
            raise failure from exc

    def _ensure_process(self) -> SttWorkerProcess:
        process = self._process
        if process is not None:
            if process.is_running:
                return process
            raise SttWorkerExited(
                "STT worker exited between requests",
                process.observed_exit(expected=False),
            )
        process: SttWorkerProcess | None = None
        try:
            process = self._new_process()
            process.start(timeout=self._startup_timeout)
        except BaseException as exc:
            report = exc.report if isinstance(exc, SttWorkerExited) else None
            if process is not None:
                terminated = process.terminate_owned_worker(expected=False)
                if terminated is not None:
                    report = terminated
            raise SttWorkerStartFailure(
                backend=self.backend,
                model_identifier=self.model_identifier,
                operation="startup",
                report=report,
                recovery_attempted=False,
                suppressed=False,
            ) from exc
        negotiated = process.negotiated or {}
        if negotiated.get("engine_id") != self.backend:
            process.terminate_owned_worker(expected=False)
            raise SttWorkerProtocolError("worker negotiated the wrong STT backend")
        capabilities = negotiated.get("capabilities", {})
        if not isinstance(capabilities, dict):
            process.terminate_owned_worker(expected=False)
            raise SttWorkerProtocolError("worker negotiated invalid capabilities")
        if self._capabilities and capabilities != self._capabilities:
            process.terminate_owned_worker(expected=False)
            raise SttWorkerProtocolError(
                "replacement worker capability negotiation changed"
            )
        self._capabilities = dict(capabilities)
        self._process = process
        return process

    def _new_process(self) -> SttWorkerProcess:
        if self._process_factory is not None:
            return self._process_factory()
        return SttWorkerProcess(
            self._command,
            backend=self.backend,
            diagnostic_dir=self._diagnostic_dir,
            cwd=self._cwd,
            environment=self._environment,
        )

    def _discard_failed_process(
        self,
        error: BaseException,
    ) -> SttWorkerExitReport | None:
        process = self._process
        report = (
            error.report
            if isinstance(error, (SttWorkerExited, NativeSttWorkerCrashed))
            else None
        )
        if process is not None:
            try:
                terminated = process.terminate_owned_worker(expected=False)
                if terminated is not None:
                    report = terminated
            finally:
                self._process = None
        return report

    def _log_failure(
        self,
        *,
        operation: str,
        report: SttWorkerExitReport | None,
        restart_decision: str,
    ) -> None:
        self._logger.warning(
            "stt worker failure backend=%s model=%s operation=%s pid=%s "
            "uptime_ms=%s exit_signed=%s exit_hex=%s marker_phase=%s "
            "restart=%s recovery_count=%d",
            self.backend,
            self.model_identifier,
            operation,
            report.pid if report is not None else "unknown",
            report.uptime_ms if report is not None else "unknown",
            report.exit_code_signed if report is not None else "unknown",
            report.exit_code_hex if report is not None else "unknown",
            report.marker_phase if report is not None else "unknown",
            restart_decision,
            self._recovery_count,
        )

    @staticmethod
    def _eligible_failures() -> tuple[type[BaseException], ...]:
        return (
            SttWorkerExited,
            SttWorkerRequestTimeout,
            SttWorkerProtocolError,
            SttWorkerProtocolFailure,
            SttWorkerStartFailure,
        )

    def _typed_remote_failure(
        self,
        operation: str,
        error: SttWorkerRemoteError,
    ) -> SttFailure:
        category = str(error.error.get("category") or "unknown")
        if category == "python":
            return SttWorkerPythonFailure(
                backend=self.backend,
                model_identifier=self.model_identifier,
                operation=operation,
                exception_type=str(
                    error.error.get("exception_type") or "Exception"
                ),
                diagnostic_message=str(error.error.get("message") or ""),
                diagnostic_traceback=str(error.error.get("traceback") or ""),
            )
        return SttWorkerProtocolFailure(
            backend=self.backend,
            model_identifier=self.model_identifier,
            operation=operation,
            report=None,
            recovery_attempted=False,
            suppressed=False,
        )

    def _typed_recovery_failure(
        self,
        error: BaseException,
        *,
        operation: str,
        report: SttWorkerExitReport | None,
        recovery_attempted: bool,
        suppressed: bool,
    ) -> NativeSttWorkerCrashed:
        failure_type: type[NativeSttWorkerCrashed]
        if isinstance(error, SttWorkerStartFailure):
            failure_type = SttWorkerStartFailure
        elif isinstance(error, SttWorkerRequestTimeout):
            failure_type = SttWorkerTimeoutFailure
        elif isinstance(
            error,
            (SttWorkerProtocolError, SttWorkerProtocolFailure),
        ):
            failure_type = SttWorkerProtocolFailure
        else:
            failure_type = NativeSttWorkerCrashed
        return failure_type(
            backend=self.backend,
            model_identifier=self.model_identifier,
            operation=operation,
            report=report,
            recovery_attempted=recovery_attempted,
            suppressed=suppressed,
        )

    def _record_success(self) -> None:
        """Reset the failure episode only after a complete correlated success."""

        self._suppressed_until.clear()
        self._last_failure = None

    def _clear_recovery_state(self) -> None:
        self._suppressed_until.clear()
        self._last_failure_report = None
        self._last_failure = None

    def _control_timeout(self, operation: str) -> float:
        if operation in {"load", "update_hotwords"}:
            return self._load_timeout
        if operation == "shutdown":
            return self._shutdown_timeout
        return 10.0

    def _require_available(self) -> None:
        if self._shutdown:
            raise RuntimeError("STT supervisor has been shut down")


__all__ = [
    "NativeSttWorkerCrashed",
    "RECOVERY_SUPPRESSION_SECONDS",
    "SttFailure",
    "SttWorkerProtocolFailure",
    "SttWorkerPythonFailure",
    "SttWorkerRemoteError",
    "SttWorkerStartFailure",
    "SttWorkerSupervisor",
    "SttWorkerTimeoutFailure",
    "default_worker_command",
]
