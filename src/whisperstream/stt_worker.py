"""Private supervised STT worker entry and runtime dispatcher.

The worker owns its byte-stream protocol, diagnostics, and native adapter
imports. ``_PrototypeRuntime`` remains only for boundary tests; production
Parakeet and Qwen dispatch lazily import their sherpa-backed adapters here.

This module must never log request bodies, audio, transcript text, hotwords, or
language hints.  Diagnostics are limited to content-free operation metadata
and ordinary local Python tracebacks.
"""

from __future__ import annotations

import faulthandler
import io
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .stt_errors import sanitize_stt_diagnostic_text
from .stt_worker_protocol import (
    PROTOCOL_VERSION,
    SttWorkerCapabilityError,
    SttWorkerEof,
    SttWorkerProtocolError,
    SttWorkerProtocolMismatch,
    SttWorkerValidationError,
    ValidatedRequest,
    decode_audio_request,
    error_reply,
    read_frame,
    success_reply,
    validate_request,
    write_frame,
)


_DIAGNOSTIC_DIR_ENV = "WHISPERSTREAM_STT_WORKER_DIAGNOSTIC_DIR"
_TEST_DELAY_OPERATION_ENV = "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION"
_TEST_DELAY_MS_ENV = "WHISPERSTREAM_STT_WORKER_TEST_DELAY_MS"
_TEST_EXCEPTION_OPERATION_ENV = "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION"
_TEST_HARD_EXIT_OPERATION_ENV = "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION"
_TEST_HARD_EXIT_ONCE_SENTINEL_ENV = (
    "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL"
)
_TEST_MALFORMED_REPLY_OPERATION_ENV = (
    "WHISPERSTREAM_STT_WORKER_TEST_MALFORMED_REPLY_OPERATION"
)
_TEST_DIAGNOSTIC_OPERATION_ENV = "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_OPERATION"
_TEST_DIAGNOSTIC_BYTES_ENV = "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_BYTES"
_TEST_DIAGNOSTIC_VALUE_ENV = "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_VALUE"
_TEST_EXCEPTION_MESSAGE_ENV = "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_MESSAGE"

_MAX_DIAGNOSTIC_BYTES = 512 * 1024
_PROTOTYPE_BACKEND = "prototype"


class _BoundedDiagnosticFile:
    """One worker-local bounded sink shared by stderr and faulthandler.

    Python writes stop at the byte cap.  ``faulthandler`` writes directly to
    the descriptor, so its final native traceback may exceed the cap by that
    single bounded traceback only; the next worker startup uses a new PID file.
    """

    def __init__(self, path: Path, *, max_bytes: int = _MAX_DIAGNOSTIC_BYTES) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._max_bytes = max_bytes
        self._file = path.open("w+b", buffering=0)
        self._lock = threading.Lock()

    def fileno(self) -> int:
        return self._file.fileno()

    def write_text(self, text: str) -> int:
        encoded = str(text).encode("utf-8", errors="replace")
        with self._lock:
            size = self._file.seek(0, os.SEEK_END)
            remaining = max(0, self._max_bytes - size)
            if remaining:
                self._file.write(encoded[:remaining])
                self._file.flush()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.flush()
                self._file.close()


class _DiagnosticTextStream(io.TextIOBase):
    """Text stream facade used for worker-local stdout and stderr."""

    def __init__(self, sink: _BoundedDiagnosticFile) -> None:
        self._sink = sink
        self._prohibited_values: tuple[str, ...] = ()

    def set_prohibited_values(self, values: tuple[str, ...]) -> None:
        self._prohibited_values = tuple(values)

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        return self._sink.write_text(
            sanitize_stt_diagnostic_text(
                text,
                prohibited_values=self._prohibited_values,
            )
        )

    def flush(self) -> None:
        self._sink.flush()


class _PrototypeRuntime:
    """Native-free runtime proving the process boundary before migration.

    It intentionally returns empty transcript text.  It does not alter the
    production in-process Parakeet or Qwen paths.
    """

    engine_id = _PROTOTYPE_BACKEND

    def __init__(self) -> None:
        self.loaded = False
        self.shutdown_requested = False

    @property
    def capabilities(self) -> dict[str, Any]:
        return {
            "preview": {
                "metadata": "text",
                "nonblocking_admission": True,
            },
            "hotwords": {"update_mode": "reload"},
            "language_hint": {"dynamic_updates": True},
        }

    def dispatch(
        self,
        request: ValidatedRequest,
        payload: bytes,
    ) -> tuple[dict[str, Any], bool]:
        operation = request.operation
        if request.backend != self.engine_id:
            raise SttWorkerValidationError("worker backend is unavailable")
        if operation == "hello":
            versions = request.body.get("supported_versions")
            if not isinstance(versions, list) or PROTOCOL_VERSION not in versions:
                raise SttWorkerProtocolMismatch("no mutually supported protocol version")
            return (
                {
                    "selected_version": PROTOCOL_VERSION,
                    "worker_pid": os.getpid(),
                    "engine_id": self.engine_id,
                    "capabilities": self.capabilities,
                    "limits": {
                        "audio_dtype": "float32-le",
                        "audio_rank": 1,
                    },
                },
                False,
            )
        if operation == "load":
            self.loaded = True
            return {"state": "ready", "loaded": True}, False
        if operation == "unload":
            self.loaded = False
            return {"state": "idle", "loaded": False}, False
        if operation == "health":
            return {
                "state": "ready" if self.loaded else "idle",
                "loaded": self.loaded,
                "engine_id": self.engine_id,
            }, False
        if operation in {"transcribe", "transcribe_chunk", "preview"}:
            waveform, sample_rate = decode_audio_request(request, payload)
            if not self.loaded:
                raise RuntimeError("prototype STT runtime is not loaded")
            body: dict[str, Any] = {
                "text": "",
                "sample_count": int(waveform.size),
                "sample_rate": sample_rate,
            }
            if operation == "preview":
                body["busy"] = False
                body["metadata"] = "text"
            return body, False
        if operation == "update_hotwords":
            hotwords = request.body.get("hotwords")
            score = request.body.get("score")
            if not isinstance(hotwords, list) or not all(
                isinstance(item, str) for item in hotwords
            ):
                raise SttWorkerValidationError("hotwords must be a string array")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise SttWorkerValidationError("hotword score must be numeric")
            return {"updated_count": len(hotwords)}, False
        if operation == "set_language_hint":
            language = request.body.get("language")
            if language is not None and not isinstance(language, str):
                raise SttWorkerValidationError("language hint must be a string or null")
            return {"updated": True}, False
        if operation == "shutdown":
            self.loaded = False
            self.shutdown_requested = True
            return {"state": "shutdown", "loaded": False}, True
        raise SttWorkerValidationError("unsupported worker operation")


def _diagnostic_paths() -> tuple[Path, Path]:
    configured = os.environ.get(_DIAGNOSTIC_DIR_ENV, "").strip()
    root = (
        Path(configured)
        if configured
        else Path(tempfile.gettempdir()) / "Whisperstream-stt-worker"
    )
    root.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    return (
        root / f"worker-{pid}.diagnostic.log",
        root / f"worker-{pid}.last-operation.json",
    )


def _write_marker(
    marker_path: Path,
    *,
    backend: str,
    operation: str,
    request_id: str | None,
    phase: str,
) -> None:
    marker: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "pid": os.getpid(),
        "backend": backend,
        "operation": operation,
        "phase": phase,
        "monotonic_ns": time.monotonic_ns(),
    }
    if request_id is not None:
        marker["request_id"] = request_id
    # This marker is ephemeral process diagnostics, not persisted application
    # state. Write and fsync it in place because Windows denies os.replace while
    # the parent has the prior marker open for observation. The parent treats a
    # concurrent partial read as unavailable and retries; native code is not
    # entered until this complete write has returned.
    payload = json.dumps(marker, ensure_ascii=True, separators=(",", ":"))
    with marker_path.open("w", encoding="utf-8") as marker_file:
        marker_file.write(payload)
        marker_file.flush()
        os.fsync(marker_file.fileno())


def _probe_delay(operation: str) -> None:
    """Private test seam used only to terminate a worker mid-operation."""

    if os.environ.get(_TEST_DELAY_OPERATION_ENV) != operation:
        return
    try:
        delay_ms = max(0, min(10_000, int(os.environ.get(_TEST_DELAY_MS_ENV, "0"))))
    except ValueError:
        delay_ms = 0
    if delay_ms:
        time.sleep(delay_ms / 1_000)


def _probe_python_exception(operation: str) -> None:
    """Private test seam proving ordinary exceptions remain structured."""

    if os.environ.get(_TEST_EXCEPTION_OPERATION_ENV) == operation:
        raise RuntimeError(
            os.environ.get(
                _TEST_EXCEPTION_MESSAGE_ENV,
                "synthetic content-free STT worker exception",
            )
        )


def _probe_diagnostic_streams(operation: str) -> None:
    """Private seam proving stdout/stderr share one sanitized bounded sink."""

    if os.environ.get(_TEST_DIAGNOSTIC_OPERATION_ENV) != operation:
        return
    try:
        byte_count = max(
            0,
            min(
                2 * _MAX_DIAGNOSTIC_BYTES,
                int(os.environ.get(_TEST_DIAGNOSTIC_BYTES_ENV, "0")),
            ),
        )
    except ValueError:
        byte_count = 0
    diagnostic_value = os.environ.get(_TEST_DIAGNOSTIC_VALUE_ENV, "")
    if diagnostic_value:
        print("stdout-value-probe " + diagnostic_value)
        print("stderr-value-probe " + diagnostic_value, file=sys.stderr)
    if byte_count:
        half = byte_count // 2
        print("stdout-diagnostic-probe " + ("x" * half))
        print("stderr-diagnostic-probe " + ("y" * (byte_count - half)), file=sys.stderr)


def _probe_hard_exit(operation: str) -> None:
    """Private test/frozen-smoke seam for a native-style process exit.

    A configured sentinel makes the exit occur once across replacement
    workers.  Without a sentinel the matching operation exits every worker,
    which exercises crash-loop suppression.  No production API can set these
    process-environment controls.
    """

    if os.environ.get(_TEST_HARD_EXIT_OPERATION_ENV) != operation:
        return
    sentinel_value = os.environ.get(_TEST_HARD_EXIT_ONCE_SENTINEL_ENV, "").strip()
    if sentinel_value:
        sentinel = Path(sentinel_value)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                sentinel,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return
        else:
            os.close(descriptor)
    os._exit(86)


def _runtime_for_backend(backend: str) -> Any:
    """Lazily construct only the adapter selected by the hello request."""

    if backend == _PROTOTYPE_BACKEND:
        return _PrototypeRuntime()
    if backend == "speech_gate":
        from .stt_worker_speech_gate import SpeechGateWorkerRuntime

        return SpeechGateWorkerRuntime()
    if backend == "parakeet":
        from .stt_worker_parakeet import ParakeetWorkerRuntime

        return ParakeetWorkerRuntime()
    if backend == "qwen":
        from .stt_worker_qwen import QwenWorkerRuntime

        return QwenWorkerRuntime()
    if backend == "cohere":
        from .stt_worker_cohere import CohereWorkerRuntime

        return CohereWorkerRuntime()
    raise SttWorkerValidationError("worker backend is unavailable")


def _request_metadata(header: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    def safe(value: Any) -> str | None:
        return value if isinstance(value, str) and len(value) <= 128 else None

    return safe(header.get("request_id")), safe(header.get("operation")), safe(
        header.get("backend")
    )


def _request_prohibited_values(request: ValidatedRequest) -> tuple[str, ...]:
    """Return transcript-class request strings that diagnostics must redact."""

    values: list[str] = []
    hotwords = request.body.get("hotwords")
    if isinstance(hotwords, list):
        values.extend(item for item in hotwords if isinstance(item, str) and item)
    for name in ("language", "forced_language", "language_override"):
        value = request.body.get(name)
        if isinstance(value, str) and value:
            values.append(value)
    return tuple(values)


def run_worker(
    protocol_input: BinaryIO,
    protocol_output: BinaryIO,
) -> int:
    """Serve one parent connection until EOF, shutdown, or a fatal frame error."""

    diagnostic_path, marker_path = _diagnostic_paths()
    diagnostic = _BoundedDiagnosticFile(diagnostic_path)
    diagnostic_stream = _DiagnosticTextStream(diagnostic)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = diagnostic_stream
    sys.stderr = diagnostic_stream
    faulthandler.enable(file=diagnostic._file, all_threads=True)
    runtime: Any | None = None
    negotiated = False

    try:
        _write_marker(
            marker_path,
            backend="unselected",
            operation="startup",
            request_id=None,
            phase="ready",
        )
        while True:
            try:
                frame = read_frame(protocol_input)
            except SttWorkerEof:
                _write_marker(
                    marker_path,
                    backend=(runtime.engine_id if runtime is not None else "unselected"),
                    operation="eof",
                    request_id=None,
                    phase="complete",
                )
                return 0
            except SttWorkerProtocolError as exc:
                write_frame(
                    protocol_output,
                    error_reply(
                        request_id=None,
                        operation=None,
                        backend=(runtime.engine_id if runtime is not None else "unselected"),
                        code=exc.code,
                        category="protocol",
                        message=str(exc),
                    ),
                )
                _write_marker(
                    marker_path,
                    backend=(runtime.engine_id if runtime is not None else "unselected"),
                    operation="malformed_frame",
                    request_id=None,
                    phase="fatal",
                )
                return 2

            request_id, operation, backend = _request_metadata(frame.header)
            request: ValidatedRequest | None = None
            try:
                request = validate_request(frame.header)
                diagnostic_stream.set_prohibited_values(
                    _request_prohibited_values(request)
                )
                if not negotiated and request.operation != "hello":
                    raise SttWorkerProtocolMismatch("hello negotiation is required")
                if runtime is None:
                    if request.operation != "hello":
                        raise SttWorkerProtocolMismatch("hello negotiation is required")
                    runtime = _runtime_for_backend(request.backend)
                _write_marker(
                    marker_path,
                    backend=request.backend,
                    operation=request.operation,
                    request_id=request.request_id,
                    phase="active",
                )
                _probe_delay(request.operation)
                _probe_diagnostic_streams(request.operation)
                _probe_python_exception(request.operation)
                _probe_hard_exit(request.operation)
                body, should_exit = runtime.dispatch(request, frame.payload)
                if request.operation == "hello":
                    negotiated = True
                _write_marker(
                    marker_path,
                    backend=request.backend,
                    operation=request.operation,
                    request_id=request.request_id,
                    phase="complete",
                )
                # Update the durable marker before committing the one and only
                # success reply. If marker persistence fails, the ordinary
                # exception boundary sends one correlated error instead of a
                # success followed by a stale second reply.
                if (
                    os.environ.get(_TEST_MALFORMED_REPLY_OPERATION_ENV)
                    == request.operation
                ):
                    protocol_output.write(b"BAD!" + (b"\x00" * 8))
                    protocol_output.flush()
                else:
                    write_frame(protocol_output, success_reply(request, body=body))
                if should_exit:
                    return 0
            except (
                SttWorkerProtocolMismatch,
                SttWorkerValidationError,
                SttWorkerCapabilityError,
            ) as exc:
                write_frame(
                    protocol_output,
                    error_reply(
                        request_id=request_id,
                        operation=operation,
                        backend=backend,
                        code=exc.code,
                        category=(
                            "protocol"
                            if isinstance(exc, SttWorkerProtocolMismatch)
                            else (
                                "capability"
                                if isinstance(exc, SttWorkerCapabilityError)
                                else "validation"
                            )
                        ),
                        message=str(exc),
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - worker exception boundary
                prohibited_values = (
                    _request_prohibited_values(request)
                    if request is not None
                    else ()
                )
                traceback_text = sanitize_stt_diagnostic_text(
                    traceback.format_exc(),
                    prohibited_values=prohibited_values,
                )
                diagnostic_message = sanitize_stt_diagnostic_text(
                    str(exc),
                    prohibited_values=prohibited_values,
                )
                # Required local traceback.  Prototype exceptions are
                # content-free; production adapters must never raise with
                # transcript/audio/hotword values embedded in the message.
                diagnostic.write_text(traceback_text)
                write_frame(
                    protocol_output,
                    error_reply(
                        request_id=request_id,
                        operation=operation,
                        backend=backend,
                        code="python_exception",
                        category="python",
                        message=diagnostic_message,
                        exception_type=type(exc).__name__,
                        traceback_text=traceback_text,
                    ),
                )
            finally:
                diagnostic_stream.set_prohibited_values(())
    finally:
        try:
            faulthandler.disable()
        except Exception:
            pass
        sys.stdout, sys.stderr = original_stdout, original_stderr
        diagnostic.close()


def main() -> int:
    """Run worker mode using the original binary stdin/stdout streams."""

    protocol_input = getattr(sys.stdin, "buffer", sys.stdin)
    protocol_output = getattr(sys.stdout, "buffer", sys.stdout)
    return run_worker(protocol_input, protocol_output)


__all__ = ["main", "run_worker"]
