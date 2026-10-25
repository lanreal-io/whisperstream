"""Parent-side lifecycle shell for the private supervised STT worker.

This low-level shell sits beneath the production recovery supervisor. It owns
launch, correlated requests, bounded waits, diagnostic capture, expected
shutdown, EOF, and native-exit reporting without importing sherpa, ONNX
Runtime, or a platform-specific transport API.

DO NOT LOG request/reply bodies, stderr text, tracebacks, or binary payloads.
They may contain transcript-class data.  The dataclasses exposed here retain
only content-free process and operation metadata.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import psutil

from .stt_worker_protocol import (
    PROTOCOL_VERSION,
    SttWorkerEof,
    SttWorkerFrame,
    SttWorkerProtocolError,
    ValidatedRequest,
    encode_audio_request,
    read_frame,
    request_header,
    validate_reply,
    validate_request,
    write_frame,
)


STARTUP_TIMEOUT_SECONDS = 30.0
CONTROL_TIMEOUT_SECONDS = 10.0
LOAD_TIMEOUT_SECONDS = 180.0
INFERENCE_TIMEOUT_SECONDS = 300.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0
TERMINATE_TIMEOUT_SECONDS = 2.0
MAX_CAPTURED_STDERR_BYTES = 64 * 1024
MAX_RETAINED_WORKER_DIAGNOSTICS = 8
_WORKER_DIAGNOSTIC_SUFFIXES = (
    ".diagnostic.log",
    ".last-operation.json",
)
_WINDOWS_DIAGNOSTIC_UNLINK_RETRY_SECONDS = 0.75
_WINDOWS_DIAGNOSTIC_UNLINK_RETRY_INTERVAL_SECONDS = 0.025


def _unlink_worker_diagnostic(path: Path) -> bool:
    """Delete one owned artifact, deferring a transient Windows file lock."""

    deadline = time.monotonic() + _WINDOWS_DIAGNOSTIC_UNLINK_RETRY_SECONDS
    while True:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            if sys.platform != "win32" or time.monotonic() >= deadline:
                if sys.platform == "win32":
                    return False
                raise
            time.sleep(_WINDOWS_DIAGNOSTIC_UNLINK_RETRY_INTERVAL_SECONDS)


def cleanup_stt_worker_diagnostics(
    directory: Path,
    *,
    retain: int = MAX_RETAINED_WORKER_DIAGNOSTICS,
) -> int:
    """Remove only old worker-owned diagnostics from one explicit directory."""

    root = Path(directory)
    if retain < 0:
        raise ValueError("retain must be non-negative")
    if not root.is_dir():
        return 0
    candidates = [
        path
        for path in root.iterdir()
        if path.is_file()
        and path.name.startswith("worker-")
        and path.name.removeprefix("worker-").split(".", 1)[0].isdigit()
        and path.name.endswith(_WORKER_DIAGNOSTIC_SUFFIXES)
    ]
    candidates.sort(
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed = 0
    for path in candidates[retain:]:
        if _unlink_worker_diagnostic(path):
            removed += 1
    return removed


class SttWorkerProcessError(RuntimeError):
    """Base class for parent-observed worker failures."""


class SttWorkerRequestTimeout(SttWorkerProcessError):
    """A bounded worker request did not produce a reply in time."""


class SttWorkerExited(SttWorkerProcessError):
    """The child reached EOF or exited before a correlated reply arrived."""

    def __init__(self, message: str, report: "SttWorkerExitReport") -> None:
        super().__init__(message)
        self.report = report


class SttWorkerRemoteError(SttWorkerProcessError):
    """The worker returned a structured validation, protocol, or Python error."""

    def __init__(self, error: Mapping[str, Any]) -> None:
        self.error = dict(error)
        code = self.error.get("code", "worker_error")
        category = self.error.get("category", "unknown")
        super().__init__(f"STT worker error category={category} code={code}")


@dataclass(frozen=True, slots=True)
class SttWorkerExitReport:
    """Content-free process-exit baseline retained by the parent."""

    pid: int
    backend: str
    operation: str
    request_id: str | None
    uptime_ms: int
    expected: bool
    exit_code_signed: int | None
    exit_code_hex: str | None
    marker_phase: str | None
    stderr_captured_bytes: int


@dataclass(frozen=True, slots=True)
class SttWorkerMemorySnapshot:
    """Content-free RSS ownership snapshot for boundary measurements."""

    parent_pid: int
    parent_rss_bytes: int
    worker_pid: int
    worker_rss_bytes: int


class SttWorkerProcess:
    """Own one worker process and serialize v1 request/reply exchanges."""

    def __init__(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        backend: str,
        diagnostic_dir: Path,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("worker command cannot be empty")
        self._command = tuple(str(item) for item in command)
        self.backend = backend
        self._diagnostic_dir = Path(diagnostic_dir)
        self._cwd = Path(cwd) if cwd is not None else None
        self._environment = dict(environment or {})
        self._process: subprocess.Popen[bytes] | None = None
        self._worker_pid: int | None = None
        self._worker_termination_code: int | None = None
        self._frames: queue.Queue[SttWorkerFrame | BaseException] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail = bytearray()
        self._stderr_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._counter = 0
        self._started_at = 0.0
        self._last_operation = "startup"
        self._last_request_id: str | None = None
        self._expected_exit = False
        self._last_exit_report: SttWorkerExitReport | None = None
        self.negotiated: dict[str, Any] | None = None
        self.spawn_ms: float | None = None

    @property
    def pid(self) -> int | None:
        if self._worker_pid is not None:
            return self._worker_pid
        return self._process.pid if self._process is not None else None

    @property
    def launcher_pid(self) -> int | None:
        """Return the direct ``Popen`` PID (different for a Windows venv launcher)."""

        return self._process.pid if self._process is not None else None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def marker_path(self) -> Path | None:
        return (
            self._diagnostic_dir / f"worker-{self.pid}.last-operation.json"
            if self.pid is not None
            else None
        )

    @property
    def diagnostic_path(self) -> Path | None:
        return (
            self._diagnostic_dir / f"worker-{self.pid}.diagnostic.log"
            if self.pid is not None
            else None
        )

    @property
    def last_exit_report(self) -> SttWorkerExitReport | None:
        return self._last_exit_report

    def observed_exit(self, *, expected: bool = False) -> SttWorkerExitReport:
        """Build the content-free report for an already-observed child exit."""

        return self._build_exit_report(expected=expected, wait_timeout=1.0)

    def start(self, *, timeout: float = STARTUP_TIMEOUT_SECONDS) -> dict[str, Any]:
        """Spawn the owned child and complete mandatory hello negotiation."""

        if self.is_running:
            if self.negotiated is None:
                raise SttWorkerProcessError("worker is running without negotiation")
            return self.negotiated
        self._diagnostic_dir.mkdir(parents=True, exist_ok=True)
        # Leave one slot for this launch so the directory remains bounded after
        # the worker creates its marker and shared stdout/stderr traceback log.
        cleanup_stt_worker_diagnostics(
            self._diagnostic_dir,
            retain=max(0, MAX_RETAINED_WORKER_DIAGNOSTICS - 2),
        )
        child_env = os.environ.copy()
        child_env.update(self._environment)
        child_env["WHISPERSTREAM_STT_WORKER_DIAGNOSTIC_DIR"] = str(
            self._diagnostic_dir
        )
        creationflags = (
            subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        started = time.perf_counter()
        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self._cwd) if self._cwd is not None else None,
            env=child_env,
            bufsize=0,
            creationflags=creationflags,
        )
        self._started_at = time.monotonic()
        self._expected_exit = False
        self._last_exit_report = None
        self._worker_pid = None
        self._worker_termination_code = None
        self._start_reader_threads()
        try:
            self.negotiated = self.request(
                "hello",
                body={"supported_versions": [PROTOCOL_VERSION]},
                timeout=timeout,
            )
            worker_pid = self.negotiated.get("worker_pid")
            if (
                isinstance(worker_pid, bool)
                or not isinstance(worker_pid, int)
                or worker_pid <= 0
            ):
                raise SttWorkerProtocolError("hello reply is missing worker_pid")
            self._worker_pid = worker_pid
        except BaseException:
            self.terminate_owned_worker(expected=False)
            raise
        self.spawn_ms = (time.perf_counter() - started) * 1_000
        return self.negotiated

    def request(
        self,
        operation: str,
        *,
        body: Mapping[str, Any] | None = None,
        payload: bytes = b"",
        timeout: float = CONTROL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Send one request and wait for its exactly correlated reply."""

        with self._request_lock:
            process = self._require_process()
            if process.poll() is not None:
                raise self._exited("STT worker is not running")
            self._counter += 1
            request_id = f"p{os.getpid()}-{self._counter}"
            header = request_header(
                request_id=request_id,
                operation=operation,
                backend=self.backend,
                body=body,
            )
            validated = validate_request(header)
            self._last_operation = operation
            self._last_request_id = request_id
            assert process.stdin is not None
            try:
                write_frame(process.stdin, header, payload)
            except (BrokenPipeError, OSError) as exc:
                raise self._exited("STT worker pipe closed while sending") from exc

            try:
                item = self._frames.get(timeout=timeout)
            except queue.Empty as exc:
                if process.poll() is not None:
                    raise self._exited("STT worker exited before replying") from exc
                raise SttWorkerRequestTimeout(
                    f"STT worker operation timed out operation={operation} "
                    f"timeout_seconds={timeout:g}"
                ) from exc
            if isinstance(item, BaseException):
                if isinstance(item, SttWorkerEof) or process.poll() is not None:
                    raise self._exited("STT worker reached EOF before replying") from item
                if isinstance(item, SttWorkerProtocolError):
                    raise item
                raise SttWorkerProcessError("STT worker reader failed") from item

            validate_reply(item.header, request=validated)
            if not item.header["ok"]:
                raise SttWorkerRemoteError(item.header["error"])
            return dict(item.header.get("body", {}))

    def request_audio(
        self,
        operation: str,
        audio: np.ndarray,
        *,
        sample_rate: int = 16_000,
        body: Mapping[str, Any] | None = None,
        timeout: float = INFERENCE_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Normalize, copy, and send one raw float32 audio payload."""

        audio_body, payload = encode_audio_request(audio, sample_rate=sample_rate)
        merged = dict(body or {})
        merged.update(audio_body)
        return self.request(
            operation,
            body=merged,
            payload=payload,
            timeout=timeout,
        )

    def memory_snapshot(self) -> SttWorkerMemorySnapshot:
        """Measure distinct parent/worker RSS while the child is alive."""

        process = self._require_process()
        if process.poll() is not None:
            raise self._exited("cannot measure an exited STT worker")
        parent = psutil.Process(os.getpid())
        assert self.pid is not None
        child = psutil.Process(self.pid)
        return SttWorkerMemorySnapshot(
            parent_pid=parent.pid,
            parent_rss_bytes=parent.memory_info().rss,
            worker_pid=child.pid,
            worker_rss_bytes=child.memory_info().rss,
        )

    def last_operation_marker(self) -> dict[str, Any] | None:
        """Read the content-free worker marker, if it is complete JSON."""

        path = self.marker_path
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def wait_for_marker(
        self,
        *,
        operation: str,
        phase: str,
        timeout: float,
    ) -> dict[str, Any]:
        """Boundedly poll the marker used to prove mid-operation termination."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            marker = self.last_operation_marker()
            if (
                marker is not None
                and marker.get("operation") == operation
                and marker.get("phase") == phase
            ):
                return marker
            if self._process is not None and self._process.poll() is not None:
                raise self._exited("STT worker exited before marker was observed")
            time.sleep(0.01)
        raise SttWorkerRequestTimeout(
            f"STT worker marker timed out operation={operation} phase={phase}"
        )

    def close_input_and_wait(self, *, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> SttWorkerExitReport:
        """Close stdin to exercise clean EOF handling and wait boundedly."""

        process = self._require_process()
        self._expected_exit = True
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.terminate_owned_worker(expected=False)
            raise SttWorkerRequestTimeout("STT worker did not exit after EOF") from exc
        report = self._build_exit_report(expected=True)
        self._close_pipes()
        return report

    def shutdown(self, *, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> SttWorkerExitReport | None:
        """Request idempotent expected shutdown and leave no owned child alive."""

        if self._process is None:
            return self._last_exit_report
        process = self._process
        if process.poll() is not None:
            report = self._build_exit_report(expected=self._expected_exit)
            self._close_pipes()
            return report
        self._expected_exit = True
        try:
            self.request("shutdown", timeout=timeout)
            process.wait(timeout=timeout)
        except (SttWorkerProcessError, subprocess.TimeoutExpired):
            self.terminate_owned_worker(expected=False)
            raise
        report = self._build_exit_report(expected=True)
        self._close_pipes()
        return report

    def terminate_owned_worker(self, *, expected: bool) -> SttWorkerExitReport | None:
        """Terminate only this instance's child, then escalate to kill boundedly."""

        if self._process is None:
            return self._last_exit_report
        process = self._process
        self._expected_exit = expected
        if process.poll() is None:
            target_pid = self.pid
            if target_pid is not None and target_pid != process.pid:
                try:
                    worker = psutil.Process(target_pid)
                    worker.terminate()
                    try:
                        self._worker_termination_code = worker.wait(
                            timeout=TERMINATE_TIMEOUT_SECONDS
                        )
                    except psutil.TimeoutExpired:
                        worker.kill()
                        self._worker_termination_code = worker.wait(
                            timeout=TERMINATE_TIMEOUT_SECONDS
                        )
                except psutil.NoSuchProcess:
                    pass
                try:
                    process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            else:
                process.terminate()
                try:
                    process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        report = self._build_exit_report(expected=expected)
        self._close_pipes()
        return report

    def _start_reader_threads(self) -> None:
        process = self._require_process()
        assert process.stdout is not None
        assert process.stderr is not None

        def read_stdout() -> None:
            try:
                while True:
                    self._frames.put(read_frame(process.stdout))
            except BaseException as exc:  # noqa: BLE001 - thread handoff
                self._frames.put(exc)

        def drain_stderr() -> None:
            while True:
                try:
                    chunk = process.stderr.read(4_096)
                except OSError:
                    return
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr_tail.extend(chunk)
                    overflow = len(self._stderr_tail) - MAX_CAPTURED_STDERR_BYTES
                    if overflow > 0:
                        del self._stderr_tail[:overflow]

        self._reader_thread = threading.Thread(
            target=read_stdout,
            name="stt-worker-protocol-reader",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=drain_stderr,
            name="stt-worker-stderr-drain",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self._process is None:
            raise SttWorkerProcessError("STT worker has not been started")
        return self._process

    def _exited(self, message: str) -> SttWorkerExited:
        report = self._build_exit_report(expected=self._expected_exit, wait_timeout=1.0)
        return SttWorkerExited(message, report)

    def _build_exit_report(
        self,
        *,
        expected: bool,
        wait_timeout: float = 0.0,
    ) -> SttWorkerExitReport:
        process = self._require_process()
        if wait_timeout and process.poll() is None:
            try:
                process.wait(timeout=wait_timeout)
            except subprocess.TimeoutExpired:
                pass
        code = (
            self._worker_termination_code
            if self._worker_termination_code is not None
            else process.poll()
        )
        if code is None:
            signed_code = None
            hex_code = None
        else:
            unsigned_code = int(code) & 0xFFFFFFFF
            signed_code = (
                unsigned_code
                if unsigned_code < 0x80000000
                else unsigned_code - 0x100000000
            )
            hex_code = f"0x{unsigned_code:08X}"
        marker = self.last_operation_marker() or {}
        with self._stderr_lock:
            stderr_size = len(self._stderr_tail)
        report = SttWorkerExitReport(
            pid=self.pid or process.pid,
            backend=str(marker.get("backend") or self.backend),
            operation=str(marker.get("operation") or self._last_operation),
            request_id=(
                str(marker["request_id"])
                if isinstance(marker.get("request_id"), str)
                else self._last_request_id
            ),
            uptime_ms=max(0, int((time.monotonic() - self._started_at) * 1_000)),
            expected=expected,
            exit_code_signed=signed_code,
            exit_code_hex=hex_code,
            marker_phase=(
                str(marker["phase"]) if isinstance(marker.get("phase"), str) else None
            ),
            stderr_captured_bytes=stderr_size,
        )
        self._last_exit_report = report
        return report

    def _close_pipes(self) -> None:
        if self._process is None:
            return
        for stream in (
            self._process.stdin,
            self._process.stdout,
            self._process.stderr,
        ):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass


def source_worker_command(repo_root: Path) -> tuple[str, ...]:
    """Return the exact source-mode equivalent of the frozen worker entry."""

    return (
        sys.executable,
        str(Path(repo_root) / "whisperstream_entry.py"),
        "--stt-worker",
    )


def frozen_worker_command(executable: Path) -> tuple[str, ...]:
    """Return the private worker command for the existing onefile engine."""

    return (str(Path(executable)), "--stt-worker")


__all__ = [
    "CONTROL_TIMEOUT_SECONDS",
    "INFERENCE_TIMEOUT_SECONDS",
    "LOAD_TIMEOUT_SECONDS",
    "MAX_RETAINED_WORKER_DIAGNOSTICS",
    "SHUTDOWN_TIMEOUT_SECONDS",
    "STARTUP_TIMEOUT_SECONDS",
    "SttWorkerExitReport",
    "SttWorkerExited",
    "SttWorkerMemorySnapshot",
    "SttWorkerProcess",
    "SttWorkerProcessError",
    "SttWorkerRemoteError",
    "SttWorkerRequestTimeout",
    "cleanup_stt_worker_diagnostics",
    "frozen_worker_command",
    "source_worker_command",
]
