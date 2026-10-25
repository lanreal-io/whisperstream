from __future__ import annotations

import logging
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from tests._speech_fixture import requires_speech_fixture, speech_audio, speech_text
from whisperstream.stt_supervisor import SttWorkerSupervisor
from whisperstream.stt_worker_process import (
    SttWorkerProcess,
    SttWorkerRemoteError,
    SttWorkerRequestTimeout,
)
from whisperstream.stt_worker_protocol import (
    FRAME_MAGIC,
    PROTOCOL_VERSION,
    SttWorkerFrame,
    read_frame,
    request_header,
    write_frame,
)


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the native FluidAudio worker is available only on macOS",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKER = (
    REPO_ROOT
    / "native"
    / "macos-stt-worker"
    / "target"
    / "release"
    / "whisperstream-macos-stt-worker"
)
MODEL_NAME = "parakeet-tdt-0.6b-v3"
LOAD_BODY = {"model_name": MODEL_NAME}
PARENT_STT_MODULES = (
    "parakeet_manager.py",
    "qwen_manager.py",
    "onnx_stt_manager.py",
    "supervised_stt_manager.py",
    "stt_factory.py",
    "stt_supervisor.py",
    "stt_worker_process.py",
    "stt_worker_protocol.py",
)


@pytest.fixture(scope="module")
def worker_binary() -> Path:
    configured = os.environ.get("WHISPERSTREAM_MACOS_STT_WORKER_BIN")
    binary = Path(configured).resolve() if configured else DEFAULT_WORKER
    assert binary.is_file(), f"native macOS STT worker is missing: {binary}"
    assert os.access(binary, os.X_OK), f"native macOS STT worker is not executable: {binary}"
    return binary


def _client(
    binary: Path,
    diagnostic_dir: Path,
    *,
    environment: dict[str, str] | None = None,
) -> SttWorkerProcess:
    return SttWorkerProcess(
        (str(binary),),
        backend="parakeet",
        diagnostic_dir=diagnostic_dir,
        cwd=REPO_ROOT,
        environment=environment,
    )


def _supervisor(
    binary: Path,
    diagnostic_dir: Path,
    *,
    environment: dict[str, str] | None = None,
    inference_timeout: float = 30.0,
) -> SttWorkerSupervisor:
    return SttWorkerSupervisor(
        backend="parakeet",
        model_identifier=MODEL_NAME,
        diagnostic_dir=diagnostic_dir,
        logger=logging.getLogger("tests.macos-stt-worker"),
        command=(str(binary),),
        cwd=REPO_ROOT,
        environment=environment,
        startup_timeout=30.0,
        load_timeout=180.0,
        inference_timeout=inference_timeout,
        shutdown_timeout=5.0,
        suppression_seconds=5.0,
    )


_fixture_audio = speech_audio
_expected_text = speech_text


# A raw worker sends no ready frame, so its first reply also covers process
# startup, which grows under parallel test load. Give that handoff the same
# startup budget the supervisor uses; the exit bounds asserted after it stay tight.
FIRST_FRAME_TIMEOUT_SECONDS = 30.0


def _read_frame_bounded(stream: object, *, timeout: float) -> SttWorkerFrame:
    results: queue.Queue[SttWorkerFrame | BaseException] = queue.Queue()

    def read() -> None:
        try:
            results.put(read_frame(stream))  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - bounded test handoff
            results.put(exc)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    item = results.get(timeout=timeout)
    if isinstance(item, BaseException):
        raise item
    return item


def _raw_worker(binary: Path, diagnostic_dir: Path) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment["WHISPERSTREAM_STT_WORKER_DIAGNOSTIC_DIR"] = str(diagnostic_dir)
    return subprocess.Popen(
        (str(binary),),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        env=environment,
        bufsize=0,
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


def test_protocol_conformance_malformed_mismatch_timeout_eof_and_shutdown(
    worker_binary: Path,
    tmp_path: Path,
) -> None:
    malformed = _raw_worker(worker_binary, tmp_path / "malformed")
    try:
        assert malformed.stdin is not None and malformed.stdout is not None
        malformed_json = b"{"
        malformed.stdin.write(
            struct.pack(">4sII", FRAME_MAGIC, len(malformed_json), 0)
            + malformed_json
        )
        malformed.stdin.flush()
        reply = _read_frame_bounded(
            malformed.stdout, timeout=FIRST_FRAME_TIMEOUT_SECONDS
        )
        assert reply.header["ok"] is False
        assert reply.header["error"]["category"] == "protocol"
        assert malformed.wait(timeout=3) == 2
    finally:
        _stop_raw_worker(malformed)

    mismatch = _raw_worker(worker_binary, tmp_path / "mismatch")
    try:
        assert mismatch.stdin is not None and mismatch.stdout is not None
        write_frame(
            mismatch.stdin,
            request_header(
                request_id="mismatch-1",
                operation="hello",
                backend="parakeet",
                body={"supported_versions": [PROTOCOL_VERSION]},
                version=PROTOCOL_VERSION + 1,
            ),
        )
        reply = _read_frame_bounded(
            mismatch.stdout, timeout=FIRST_FRAME_TIMEOUT_SECONDS
        )
        assert reply.header["request_id"] == "mismatch-1"
        assert reply.header["error"]["code"] == "protocol_mismatch"
        mismatch.stdin.close()
        assert mismatch.wait(timeout=3) == 0
    finally:
        _stop_raw_worker(mismatch)

    timeout_client = _client(
        worker_binary,
        tmp_path / "timeout",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION": "health",
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_MS": "500",
        },
    )
    timeout_pid = None
    try:
        timeout_client.start(timeout=30)
        timeout_pid = timeout_client.pid
        started = time.monotonic()
        with pytest.raises(SttWorkerRequestTimeout):
            timeout_client.request("health", timeout=0.05)
        assert time.monotonic() - started < 1.0
    finally:
        timeout_client.terminate_owned_worker(expected=False)
    assert timeout_pid is not None and not psutil.pid_exists(timeout_pid)

    eof_client = _client(worker_binary, tmp_path / "eof")
    eof_client.start(timeout=30)
    eof_pid = eof_client.pid
    report = eof_client.close_input_and_wait(timeout=5)
    assert report.expected is True and report.operation == "eof"
    assert eof_pid is not None and not psutil.pid_exists(eof_pid)

    shutdown_client = _client(worker_binary, tmp_path / "shutdown")
    shutdown_client.start(timeout=30)
    shutdown_pid = shutdown_client.pid
    with pytest.raises(SttWorkerRemoteError) as malformed_audio:
        shutdown_client.request(
            "transcribe",
            body={
                "audio": {
                    "dtype": "float32-le",
                    "shape": [1],
                    "sample_count": 2,
                    "sample_rate": 16_000,
                    "byte_length": 4,
                }
            },
            payload=b"\x00\x00\x00\x00",
            timeout=3,
        )
    assert malformed_audio.value.error["category"] == "validation"
    assert shutdown_client.is_running is True
    report = shutdown_client.shutdown(timeout=5)
    assert report is not None and report.expected is True
    assert report.operation == "shutdown" and report.exit_code_signed == 0
    assert shutdown_pid is not None and not psutil.pid_exists(shutdown_pid)


def test_capability_contract_is_truthful_and_unsupported_is_explicit(
    worker_binary: Path,
    tmp_path: Path,
) -> None:
    client = _client(worker_binary, tmp_path)
    try:
        hello = client.start(timeout=30)
        assert hello == {
            "selected_version": 1,
            "worker_pid": client.pid,
            "engine_id": "parakeet",
            "capabilities": {},
            "limits": {"audio_dtype": "float32-le", "audio_rank": 1},
        }
        for operation, body in (
            ("preview", {}),
            ("update_hotwords", {"hotwords": [], "score": 1.5}),
            ("set_language_hint", {"language": None}),
        ):
            with pytest.raises(SttWorkerRemoteError) as captured:
                client.request(operation, body=body, timeout=3)
            assert captured.value.error["category"] == "capability"
            assert captured.value.error["code"] == "capability_unsupported"
            assert client.is_running is True
    finally:
        client.shutdown(timeout=5)


def test_parent_python_boundary_contains_no_fluidaudio_native_state() -> None:
    parent_root = REPO_ROOT / "src" / "whisperstream"
    for module in PARENT_STT_MODULES:
        source = (parent_root / module).read_text(encoding="utf-8").casefold()
        for native_import in (
            "import fluidaudio",
            "from fluidaudio",
            "import fluidaudio_rs",
            "from fluidaudio_rs",
        ):
            assert native_import not in source, (
                f"FluidAudio native ownership leaked into {module}"
            )

    # The server is a package; every module in it is a production surface.
    production_modules = [
        parent_root / "main.py",
        # Development endpoints, when present (source releases omit them).
        *[path for path in (parent_root / "dev_endpoints.py",) if path.is_file()],
        *sorted((parent_root / "server").rglob("*.py")),
    ]
    assert parent_root / "server" / "app.py" in production_modules
    production_surfaces = "\n".join(
        module.read_text(encoding="utf-8") for module in production_modules
    )
    for control in (
        "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION",
        "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_AFTER_REPLY_OPERATION",
        "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION",
        "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION",
        "WHISPERSTREAM_STT_WORKER_TEST_MALFORMED_REPLY_OPERATION",
        "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_OPERATION",
    ):
        assert control not in production_surfaces


@requires_speech_fixture
def test_load_two_transcriptions_unload_reload_and_cleanup(
    worker_binary: Path,
    tmp_path: Path,
) -> None:
    audio, sample_rate = _fixture_audio()
    expected = _expected_text()
    client = _client(worker_binary, tmp_path)
    worker_pid = None
    try:
        client.start(timeout=30)
        worker_pid = client.pid
        assert worker_pid is not None and worker_pid != os.getpid()
        assert client.request("load", body=LOAD_BODY, timeout=180) == {
            "state": "ready",
            "loaded": True,
        }
        results = [
            client.request_audio(
                "transcribe", audio, sample_rate=sample_rate, timeout=300
            )["text"]
            for _ in range(2)
        ]
        assert all(result.strip().casefold() == expected.casefold() for result in results)
        assert results[0].strip().casefold() == results[1].strip().casefold()
        assert client.request("unload", timeout=10) == {
            "state": "idle",
            "loaded": False,
        }
        assert client.request("health", timeout=10)["state"] == "idle"
        assert client.request("load", body=LOAD_BODY, timeout=180)["loaded"] is True
        chunk = client.request_audio(
            "transcribe_chunk", audio, sample_rate=sample_rate, timeout=300
        )["text"]
        assert chunk.strip().casefold() == expected.casefold()
    finally:
        client.shutdown(timeout=5)
    assert worker_pid is not None and not psutil.pid_exists(worker_pid)


@requires_speech_fixture
@pytest.mark.parametrize("operation", ["load", "transcribe"])
def test_ordinary_native_errors_are_structured_and_worker_survives(
    worker_binary: Path,
    tmp_path: Path,
    operation: str,
) -> None:
    client = _client(
        worker_binary,
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION": operation,
        },
    )
    try:
        client.start(timeout=30)
        if operation == "transcribe":
            client.request("load", body=LOAD_BODY, timeout=180)
        with pytest.raises(SttWorkerRemoteError) as captured:
            if operation == "load":
                client.request("load", body=LOAD_BODY, timeout=30)
            else:
                audio, sample_rate = _fixture_audio()
                client.request_audio(
                    "transcribe", audio, sample_rate=sample_rate, timeout=30
                )
        error = captured.value.error
        assert error["category"] == "python"
        assert error["code"] == "python_exception"
        assert error["exception_type"] == "MacSttWorkerTestError"
        assert "MacSttWorkerTestError" in error["traceback"]
        assert client.is_running is True
        assert client.request("health", timeout=10)["engine_id"] == "parakeet"
        diagnostic = client.diagnostic_path
        assert diagnostic is not None and 0 < diagnostic.stat().st_size <= 512 * 1024
    finally:
        client.shutdown(timeout=5)


def test_load_hard_exit_recovers_once_and_cleans_crashed_worker(
    worker_binary: Path,
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "load-exit-once"
    supervisor = _supervisor(
        worker_binary,
        tmp_path / "diagnostics",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION": "load",
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL": str(sentinel),
        },
    )
    try:
        supervisor.load(LOAD_BODY)
        replacement_pid = supervisor.pid
        failure = supervisor.last_failure_report
        assert supervisor.recovery_count == 1
        assert replacement_pid is not None and replacement_pid != os.getpid()
        assert failure is not None and failure.operation == "load"
        assert failure.marker_phase == "active"
        assert failure.pid != replacement_pid
        assert not psutil.pid_exists(failure.pid)
        assert supervisor.health()["loaded"] is True
    finally:
        final_pid = supervisor.pid
        supervisor.shutdown()
    assert final_pid is not None and not psutil.pid_exists(final_pid)


@requires_speech_fixture
def test_inference_hard_exit_recovers_once_without_duplicate_result(
    worker_binary: Path,
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "inference-exit-once"
    supervisor = _supervisor(
        worker_binary,
        tmp_path / "diagnostics",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION": "transcribe",
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL": str(sentinel),
        },
    )
    audio, sample_rate = _fixture_audio()
    expected = _expected_text()
    supervisor.load(LOAD_BODY)
    first_pid = supervisor.pid
    try:
        body = supervisor.request_audio(
            "transcribe", audio, sample_rate=sample_rate
        )
        replacement_pid = supervisor.pid
        assert body["text"].strip().casefold() == expected.casefold()
        assert supervisor.recovery_count == 1
        assert first_pid is not None and not psutil.pid_exists(first_pid)
        assert replacement_pid is not None and replacement_pid != first_pid
    finally:
        final_pid = supervisor.pid
        supervisor.shutdown()
    assert final_pid is not None and not psutil.pid_exists(final_pid)
