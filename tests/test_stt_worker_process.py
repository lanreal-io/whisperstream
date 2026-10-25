from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import psutil
import pytest

from whisperstream.stt_worker_process import (
    MAX_RETAINED_WORKER_DIAGNOSTICS,
    STARTUP_TIMEOUT_SECONDS,
    SttWorkerExited,
    SttWorkerProcess,
    SttWorkerRemoteError,
    SttWorkerRequestTimeout,
    cleanup_stt_worker_diagnostics,
    source_worker_command,
)
from whisperstream.stt_worker_protocol import (
    FRAME_MAGIC,
    PROTOCOL_VERSION,
    SttWorkerFrame,
    read_frame,
    request_header,
    write_frame,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMAND = source_worker_command(REPO_ROOT)

# A raw worker sends no ready frame, so its first reply also covers spawning a
# fresh Python interpreter and importing the worker. That startup is a harness
# cost that grows under parallel test load, not the contract under test, so it
# gets the same generous handoff budget the client allows for startup.
# The bounds these tests prove, such as exit after a protocol error, stay tight.
FIRST_FRAME_TIMEOUT_SECONDS = STARTUP_TIMEOUT_SECONDS
WORKER_EXIT_TIMEOUT_SECONDS = 3.0


def _client(
    tmp_path: Path,
    *,
    environment: dict[str, str] | None = None,
) -> SttWorkerProcess:
    return SttWorkerProcess(
        SOURCE_COMMAND,
        backend="prototype",
        diagnostic_dir=tmp_path,
        cwd=REPO_ROOT,
        environment=environment,
    )


def _read_frame_bounded(stream: object, *, timeout: float) -> SttWorkerFrame:
    results: queue.Queue[SttWorkerFrame | BaseException] = queue.Queue()

    def read() -> None:
        try:
            results.put(read_frame(stream))  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - bounded test handoff
            results.put(exc)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    try:
        item = results.get(timeout=timeout)
    except queue.Empty as exc:
        raise AssertionError("worker did not produce a bounded frame") from exc
    if isinstance(item, BaseException):
        raise item
    return item


def _raw_worker(tmp_path: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["WHISPERSTREAM_STT_WORKER_DIAGNOSTIC_DIR"] = str(tmp_path)
    return subprocess.Popen(
        SOURCE_COMMAND,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        env=env,
        bufsize=0,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    )


def _stop_raw_worker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def test_source_worker_negotiates_binary_audio_and_expected_shutdown(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    try:
        hello = client.start()
        assert hello["selected_version"] == PROTOCOL_VERSION
        assert hello["worker_pid"] == client.pid
        assert hello["capabilities"] == {
            "preview": {"metadata": "text", "nonblocking_admission": True},
            "hotwords": {"update_mode": "reload"},
            "language_hint": {"dynamic_updates": True},
        }
        client.request("load", timeout=3)
        result = client.request_audio(
            "transcribe",
            np.zeros(320_000, dtype=np.float32),
            timeout=3,
        )
        assert result["sample_count"] == 320_000
        assert result["text"] == ""
        snapshot = client.memory_snapshot()
        assert snapshot.parent_pid == os.getpid()
        assert snapshot.worker_pid == client.pid
        assert snapshot.worker_pid != snapshot.parent_pid
        assert snapshot.worker_rss_bytes > 0

        report = client.shutdown(timeout=3)
        assert report is not None
        assert report.expected is True
        assert report.operation == "shutdown"
        assert report.exit_code_signed == 0
        assert report.exit_code_hex == "0x00000000"
        assert client.is_running is False
    finally:
        if client.is_running:
            client.terminate_owned_worker(expected=False)


def test_worker_reports_malformed_message_and_exits_boundedly(tmp_path: Path) -> None:
    process = _raw_worker(tmp_path)
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        malformed_json = b"{"
        process.stdin.write(
            struct.pack(">4sII", FRAME_MAGIC, len(malformed_json), 0)
            + malformed_json
        )
        process.stdin.flush()

        reply = _read_frame_bounded(
            process.stdout, timeout=FIRST_FRAME_TIMEOUT_SECONDS
        )
        assert reply.header["ok"] is False
        assert reply.header["error"]["category"] == "protocol"
        assert process.wait(timeout=WORKER_EXIT_TIMEOUT_SECONDS) == 2
    finally:
        _stop_raw_worker(process)


def test_worker_protocol_mismatch_is_structured_then_eof_is_clean(
    tmp_path: Path,
) -> None:
    process = _raw_worker(tmp_path)
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        write_frame(
            process.stdin,
            request_header(
                request_id="mismatch-1",
                operation="hello",
                backend="prototype",
                body={"supported_versions": [PROTOCOL_VERSION]},
                version=PROTOCOL_VERSION + 1,
            ),
        )
        reply = _read_frame_bounded(
            process.stdout, timeout=FIRST_FRAME_TIMEOUT_SECONDS
        )
        assert reply.header["ok"] is False
        assert reply.header["request_id"] == "mismatch-1"
        assert reply.header["error"]["code"] == "protocol_mismatch"

        process.stdin.close()
        assert process.wait(timeout=WORKER_EXIT_TIMEOUT_SECONDS) == 0
        markers = list(tmp_path.glob("worker-*.last-operation.json"))
        assert len(markers) == 1
        marker = json.loads(markers[0].read_text(encoding="utf-8"))
        assert marker["operation"] == "eof"
        assert marker["phase"] == "complete"
    finally:
        _stop_raw_worker(process)


def test_expected_eof_leaves_no_worker(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.start()

    report = client.close_input_and_wait(timeout=3)

    assert report.expected is True
    assert report.operation == "eof"
    assert report.exit_code_signed == 0
    assert client.is_running is False


def test_python_exception_reply_retains_traceback_and_worker_survives(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION": "health"
        },
    )
    try:
        client.start()
        with pytest.raises(SttWorkerRemoteError) as captured:
            client.request("health", timeout=3)

        error = captured.value.error
        assert error["category"] == "python"
        assert error["code"] == "python_exception"
        assert error["exception_type"] == "RuntimeError"
        assert "Traceback (most recent call last)" in error["traceback"]
        assert "synthetic content-free STT worker exception" in error["traceback"]
        assert client.is_running is True
        assert client.diagnostic_path is not None
        diagnostic = client.diagnostic_path.read_text(encoding="utf-8")
        assert "Traceback (most recent call last)" in diagnostic
        assert client.diagnostic_path.stat().st_size <= 512 * 1024
        client.shutdown(timeout=3)
    finally:
        if client.is_running:
            client.terminate_owned_worker(expected=False)


@pytest.mark.parametrize("operation", ["load", "transcribe"])
def test_hard_exit_parent_survival_and_clean_restart(
    tmp_path: Path,
    operation: str,
) -> None:
    parent_pid = os.getpid()
    client = _client(
        tmp_path / "crash",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION": operation,
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_MS": "2500",
        },
    )
    errors: list[BaseException] = []
    try:
        client.start()
        if operation == "transcribe":
            client.request("load", timeout=3)

        def request_during_exit() -> None:
            try:
                if operation == "transcribe":
                    client.request_audio(
                        "transcribe",
                        np.zeros(320_000, dtype=np.float32),
                        timeout=5,
                    )
                else:
                    client.request("load", timeout=5)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        request_thread = threading.Thread(target=request_during_exit, daemon=True)
        request_thread.start()
        marker = client.wait_for_marker(
            operation=operation,
            phase="active",
            timeout=3,
        )
        worker_pid = client.pid
        report = client.terminate_owned_worker(expected=False)
        request_thread.join(timeout=3)

        assert request_thread.is_alive() is False
        assert errors and isinstance(errors[0], SttWorkerExited)
        assert psutil.pid_exists(parent_pid)
        assert os.getpid() == parent_pid
        assert worker_pid is not None and worker_pid != parent_pid
        assert marker["pid"] == worker_pid
        assert report is not None
        assert report.pid == worker_pid
        assert report.operation == operation
        assert report.marker_phase == "active"
        assert report.expected is False
        assert report.exit_code_signed is not None
        assert report.exit_code_hex is not None
        assert client.is_running is False

        replacement = _client(tmp_path / f"replacement-{operation}")
        try:
            replacement.start()
            assert replacement.request("health", timeout=3)["state"] == "idle"
            replacement.shutdown(timeout=3)
            assert replacement.is_running is False
        finally:
            if replacement.is_running:
                replacement.terminate_owned_worker(expected=False)
    finally:
        if client.is_running:
            client.terminate_owned_worker(expected=False)


def test_request_timeout_is_bounded_and_cleanup_is_owned(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION": "health",
            # The worker's maximum test delay. The bound below proves the
            # request gives up long before the reply could arrive, with room
            # for scheduler stalls under parallel test load.
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_MS": "10000",
        },
    )
    try:
        client.start()
        started = time.monotonic()
        with pytest.raises(SttWorkerRequestTimeout):
            client.request("health", timeout=0.05)
        assert time.monotonic() - started < 3.0
    finally:
        client.terminate_owned_worker(expected=False)
    assert client.is_running is False


def test_worker_stdout_and_stderr_share_one_bounded_diagnostic(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_OPERATION": "health",
            "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_BYTES": str(600 * 1024),
        },
    )
    try:
        client.start()
        client.request("health", timeout=3)
        path = client.diagnostic_path
        assert path is not None
        diagnostic = path.read_text(encoding="utf-8")
        assert "stdout-diagnostic-probe" in diagnostic
        assert "stderr-diagnostic-probe" in diagnostic
        assert path.stat().st_size <= 512 * 1024
        client.shutdown(timeout=3)
    finally:
        if client.is_running:
            client.terminate_owned_worker(expected=False)


def test_worker_diagnostics_redact_request_content_paths_and_secrets(
    tmp_path: Path,
) -> None:
    prohibited = "HOTWORD_SENTINEL_DO_NOT_RETAIN"
    private_path = r"C:\Users\Private Person\models\secret.onnx"
    secret = "private-token-value"
    email = "private.person@example.test"
    client = _client(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION": "update_hotwords",
            "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_MESSAGE": (
                f'{prohibited} path="{private_path}" token={secret} email={email}'
            ),
            "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_OPERATION": "update_hotwords",
            "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_VALUE": prohibited,
        },
    )
    try:
        client.start()
        with pytest.raises(SttWorkerRemoteError) as captured:
            client.request(
                "update_hotwords",
                body={"hotwords": [prohibited]},
                timeout=3,
            )

        error = captured.value.error
        path = client.diagnostic_path
        assert path is not None
        retained = "\n".join(
            [
                str(error.get("message", "")),
                str(error.get("traceback", "")),
                path.read_text(encoding="utf-8"),
            ]
        )
        for forbidden in (prohibited, private_path, secret, email, "Private Person"):
            assert forbidden not in retained
        assert "[content redacted]" in retained
        assert "[path redacted]" in retained
        assert "[secret redacted]" in retained
        assert "[email redacted]" in retained
        assert client.is_running is True
        client.shutdown(timeout=3)
    finally:
        if client.is_running:
            client.terminate_owned_worker(expected=False)


def test_worker_diagnostic_cleanup_is_bounded_and_preserves_other_files(
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / "keep-me.txt"
    unrelated.write_text("unrelated", encoding="utf-8")
    for pid in range(100, 112):
        (tmp_path / f"worker-{pid}.diagnostic.log").write_text(
            "content-free",
            encoding="utf-8",
        )
        (tmp_path / f"worker-{pid}.last-operation.json").write_text(
            "{}",
            encoding="utf-8",
        )

    removed = cleanup_stt_worker_diagnostics(tmp_path)
    retained = list(tmp_path.glob("worker-*"))

    assert removed == 24 - MAX_RETAINED_WORKER_DIAGNOSTICS
    assert len(retained) == MAX_RETAINED_WORKER_DIAGNOSTICS
    assert unrelated.read_text(encoding="utf-8") == "unrelated"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-lock retry contract")
def test_worker_diagnostic_cleanup_defers_a_persistently_locked_windows_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import whisperstream.stt_worker_process as worker_process

    locked = tmp_path / "worker-100.diagnostic.log"
    removable = tmp_path / "worker-101.diagnostic.log"
    locked.write_text("content-free", encoding="utf-8")
    removable.write_text("content-free", encoding="utf-8")
    real_unlink = Path.unlink

    def unlink_with_lock(path: Path, *args, **kwargs):
        if path == locked:
            raise PermissionError("synthetic Windows sharing violation")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_with_lock)
    monkeypatch.setattr(
        worker_process,
        "_WINDOWS_DIAGNOSTIC_UNLINK_RETRY_SECONDS",
        0.0,
    )

    removed = cleanup_stt_worker_diagnostics(tmp_path, retain=0)

    assert removed == 1
    assert locked.is_file()
    assert not removable.exists()
