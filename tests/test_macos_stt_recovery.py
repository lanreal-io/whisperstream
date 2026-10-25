"""Native macOS recovery, privacy, exactly-once, and cleanup gates."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest
from fastapi.testclient import TestClient

from whisperstream.macos_stt_manager import MacosFluidAudioManager
from whisperstream.main import WhisperstreamApp, _DictationFinalizationContext
from whisperstream.stt_errors import (
    NativeSttWorkerCrashed,
    SttWorkerProtocolFailure,
    SttWorkerPythonFailure,
    SttWorkerStartFailure,
    SttWorkerTimeoutFailure,
)
from whisperstream.stt_supervisor import SttWorkerSupervisor
from whisperstream.stt_worker_process import SttWorkerProcess

from tests._speech_fixture import requires_speech_fixture, speech_audio, speech_text, speech_wav_bytes


pytestmark = [pytest.mark.usefixtures("accepted_eula"), pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the native FluidAudio recovery suite is macOS-only",
)]

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER = (
    REPO_ROOT
    / "native"
    / "macos-stt-worker"
    / "target"
    / "release"
    / "whisperstream-macos-stt-worker"
)
MODEL_NAME = "parakeet-tdt-0.6b-v3"
LOAD_BODY = {"model_name": MODEL_NAME}


@pytest.fixture(scope="module")
def worker_binary() -> Path:
    configured = os.environ.get("WHISPERSTREAM_MACOS_STT_WORKER_BIN")
    binary = Path(configured).resolve() if configured else WORKER
    assert binary.is_file(), f"native macOS STT worker is missing: {binary}"
    assert os.access(binary, os.X_OK), f"native macOS STT worker is not executable: {binary}"
    return binary


def _supervisor(
    binary: Path,
    diagnostic_dir: Path,
    *,
    environment: dict[str, str] | None = None,
    startup_timeout: float = 3.0,
    inference_timeout: float = 30.0,
    suppression_seconds: float = 5.0,
) -> SttWorkerSupervisor:
    return SttWorkerSupervisor(
        backend="parakeet",
        model_identifier=MODEL_NAME,
        diagnostic_dir=diagnostic_dir,
        logger=logging.getLogger("tests.macos-stt-recovery"),
        command=(str(binary),),
        cwd=REPO_ROOT,
        environment=environment,
        startup_timeout=startup_timeout,
        load_timeout=180.0,
        inference_timeout=inference_timeout,
        shutdown_timeout=5.0,
        suppression_seconds=suppression_seconds,
    )


_fixture_audio = speech_audio
_fixture_wav_bytes = speech_wav_bytes
_expected_text = speech_text


def _wait_absent(pids: set[int], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(not psutil.pid_exists(pid) for pid in pids):
            return
        time.sleep(0.02)
    alive = sorted(pid for pid in pids if psutil.pid_exists(pid))
    raise AssertionError(f"owned native worker PIDs remained alive: {alive}")


def _finalization_context(manager: MacosFluidAudioManager) -> _DictationFinalizationContext:
    audio, _ = _fixture_audio()
    return _DictationFinalizationContext(
        recording_id=None,
        waveform=audio,
        chunk_futures=(),
        transcription_started_at=time.perf_counter(),
        recording_started_at=time.perf_counter(),
        stt_manager=manager,
        dictation_app=None,
        manual_profile_id=None,
        override_anchor=None,
        screen_reader_detected=False,
        inject_suppressed=False,
        agent_compose_lease=None,
        acceptance_grant=__import__("whisperstream.main", fromlist=["get_acceptance_grant"]).get_acceptance_grant(),
    )


def _finalization_app() -> tuple[WhisperstreamApp, list[dict], MagicMock]:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = logging.getLogger("tests.macos-stt-finalization")
    app.config = SimpleNamespace(error_sound_path=None)
    app.audio_feedback = MagicMock()
    app._last_stt_failure = None
    events: list[dict] = []
    app._broadcast = events.append
    injector = MagicMock()

    def deliver(text: str, _duration: float, *, context: object) -> None:
        assert context is not None
        injector.inject(text)

    app._enhance_then_inject = deliver
    return app, events, injector


@requires_speech_fixture
def test_native_typed_failures_are_bounded_and_expected_shutdown_is_clean(
    worker_binary: Path,
    tmp_path: Path,
) -> None:
    owned_pids: set[int] = set()

    start = _supervisor(
        worker_binary,
        tmp_path / "start",
        environment={"WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION": "hello"},
        startup_timeout=1.0,
    )
    started = time.monotonic()
    with pytest.raises(SttWorkerStartFailure) as start_failure:
        start.load(LOAD_BODY)
    assert time.monotonic() - started < 3.0
    assert start_failure.value.recovery_attempted is True
    assert start_failure.value.suppressed is True
    assert start.recovery_count == 1 and start.is_running is False
    if start_failure.value.report is not None:
        owned_pids.add(start_failure.value.report.pid)

    protocol = _supervisor(
        worker_binary,
        tmp_path / "protocol",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_MALFORMED_REPLY_OPERATION": "health"
        },
    )
    protocol.load(LOAD_BODY)
    owned_pids.add(protocol.pid or -1)
    started = time.monotonic()
    with pytest.raises(SttWorkerProtocolFailure) as protocol_failure:
        protocol.health()
    assert time.monotonic() - started < 5.0
    assert protocol_failure.value.recovery_attempted is True
    assert protocol_failure.value.suppressed is True
    assert protocol.recovery_count == 1 and protocol.is_running is False
    if protocol_failure.value.report is not None:
        owned_pids.add(protocol_failure.value.report.pid)

    timeout = _supervisor(
        worker_binary,
        tmp_path / "timeout",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION": "transcribe",
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_MS": "500",
        },
        inference_timeout=0.05,
    )
    timeout.load(LOAD_BODY)
    owned_pids.add(timeout.pid or -1)
    audio, sample_rate = _fixture_audio()
    started = time.monotonic()
    with pytest.raises(SttWorkerTimeoutFailure) as timeout_failure:
        timeout.request_audio("transcribe", audio, sample_rate=sample_rate)
    assert time.monotonic() - started < 3.0
    assert timeout_failure.value.recovery_attempted is True
    assert timeout_failure.value.suppressed is True
    assert timeout.recovery_count == 1 and timeout.is_running is False
    if timeout_failure.value.report is not None:
        owned_pids.add(timeout_failure.value.report.pid)

    ordinary = _supervisor(
        worker_binary,
        tmp_path / "ordinary",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION": "health"
        },
    )
    ordinary.load(LOAD_BODY)
    ordinary_pid = ordinary.pid
    assert ordinary_pid is not None
    owned_pids.add(ordinary_pid)
    with pytest.raises(SttWorkerPythonFailure) as ordinary_failure:
        ordinary.health()
    assert ordinary_failure.value.exception_type == "MacSttWorkerTestError"
    assert ordinary.recovery_count == 0
    assert ordinary.pid == ordinary_pid and ordinary.is_running is True
    report = ordinary.shutdown()
    assert report is not None and report.expected is True
    assert report.operation == "shutdown" and report.exit_code_signed == 0

    for operation in ("load", "transcribe"):
        sentinel = tmp_path / f"{operation}-once"
        crash = _supervisor(
            worker_binary,
            tmp_path / operation,
            environment={
                "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION": operation,
                "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL": str(sentinel),
            },
        )
        if operation == "load":
            crash.load(LOAD_BODY)
        else:
            crash.load(LOAD_BODY)
            first_pid = crash.pid
            assert first_pid is not None
            owned_pids.add(first_pid)
            result = crash.request_audio("transcribe", audio, sample_rate=sample_rate)
            assert result["text"].strip().casefold() == _expected_text().casefold()
        assert crash.recovery_count == 1
        failure_report = crash.last_failure_report
        assert failure_report is not None
        assert failure_report.operation == operation
        assert failure_report.marker_phase == "active"
        assert failure_report.exit_code_signed == 86
        assert failure_report.exit_code_hex == "0x00000056"
        owned_pids.add(failure_report.pid)
        replacement_pid = crash.pid
        assert replacement_pid is not None and replacement_pid != failure_report.pid
        owned_pids.add(replacement_pid)
        crash.shutdown()

    owned_pids.discard(-1)
    _wait_absent(owned_pids)


@requires_speech_fixture
def test_native_matching_crashes_suppress_and_success_resets_episode(
    worker_binary: Path,
    tmp_path: Path,
) -> None:
    supervisor = _supervisor(
        worker_binary,
        tmp_path / "diagnostics",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION": "transcribe"
        },
    )
    audio, sample_rate = _fixture_audio()
    owned_pids: set[int] = set()
    supervisor.load(LOAD_BODY)
    first_pid = supervisor.pid
    assert first_pid is not None
    owned_pids.add(first_pid)

    with pytest.raises(NativeSttWorkerCrashed) as first_episode:
        supervisor.request_audio("transcribe", audio, sample_rate=sample_rate)
    assert first_episode.value.recovery_attempted is True
    assert first_episode.value.suppressed is True
    assert supervisor.recovery_count == 1
    if first_episode.value.report is not None:
        owned_pids.add(first_episode.value.report.pid)

    with pytest.raises(NativeSttWorkerCrashed) as suppressed:
        supervisor.request_audio("transcribe", audio, sample_rate=sample_rate)
    assert suppressed.value.recovery_attempted is False
    assert suppressed.value.suppressed is True
    assert supervisor.recovery_count == 1
    assert supervisor.pid is None

    supervisor.load(LOAD_BODY)
    reset_pid = supervisor.pid
    assert reset_pid is not None
    owned_pids.add(reset_pid)
    assert supervisor.last_failure is None
    with pytest.raises(NativeSttWorkerCrashed) as reset_episode:
        supervisor.request_audio("transcribe", audio, sample_rate=sample_rate)
    assert reset_episode.value.recovery_attempted is True
    assert supervisor.recovery_count == 2
    if reset_episode.value.report is not None:
        owned_pids.add(reset_episode.value.report.pid)

    supervisor.shutdown()
    _wait_absent(owned_pids)


@requires_speech_fixture
def test_backend_survives_real_native_failure_and_keeps_parent_surfaces(
    worker_binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.server import WhisperstreamServer

    monkeypatch.setenv("WHISPERSTREAM_MACOS_STT_WORKER_BIN", str(worker_binary))
    monkeypatch.setenv("WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION", "transcribe_chunk")
    app = WhisperstreamApp(headless=True, file_only=True)
    app.license_manager.can_start_recording = lambda: True
    assert app.stt_engine is not None
    first_pid = app.stt_engine.worker_pid
    assert first_pid is not None
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    emitted: list[dict] = []
    original_broadcast = server.broadcast

    def capture(event: dict, **kwargs) -> None:
        emitted.append(dict(event))
        original_broadcast(event, **kwargs)

    server.broadcast = capture  # type: ignore[method-assign]
    app._broadcast = capture
    client = TestClient(server.fastapi)
    try:
        with client.websocket_connect(
            "/ws", headers={"origin": "http://testserver"}
        ) as websocket:
            assert websocket.receive_json()["type"] == "license.status"
            response = client.post(
                "/api/transcribe",
                files={"file": ("fixture.wav", _fixture_wav_bytes(), "audio/wav")},
            )
            assert response.status_code == 503
            assert response.json()["error"] == "stt_failure"

        health = client.get("/api/health")
        devices = client.get("/api/devices")
        config = client.get("/api/config")
        status = client.get("/api/stt/status")
        disk_status = client.get("/api/models/disk-free")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["model_health"] == "degraded"
        assert health.json()["stt_failure"]["code"] == "worker_native_exit"
        assert devices.status_code == 200
        assert config.status_code == 200
        assert status.status_code == 200
        assert disk_status.status_code == 200
        with client.websocket_connect(
            "/ws", headers={"origin": "http://testserver"}
        ) as websocket:
            assert websocket.receive_json()["type"] == "license.status"
        assert len([event for event in emitted if event.get("type") == "stt.failure"]) == 1
        assert not any(event.get("type") == "transcription.result" for event in emitted)
    finally:
        final_report = app.stt_engine._supervisor.last_failure_report  # type: ignore[union-attr]
        app.cleanup()
    owned = {first_pid}
    if final_report is not None:
        owned.add(final_report.pid)
    _wait_absent(owned)


@requires_speech_fixture
def test_single_native_retry_yields_one_api_response_and_one_result_event(
    worker_binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.server import WhisperstreamServer

    sentinel = tmp_path / "api-retry-once"
    monkeypatch.setenv("WHISPERSTREAM_MACOS_STT_WORKER_BIN", str(worker_binary))
    monkeypatch.setenv("WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION", "transcribe_chunk")
    monkeypatch.setenv(
        "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL", str(sentinel)
    )
    app = WhisperstreamApp(headless=True, file_only=True)
    app.license_manager.can_start_recording = lambda: True
    assert app.stt_engine is not None
    first_pid = app.stt_engine.worker_pid
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    emitted: list[dict] = []
    original_broadcast = server.broadcast

    def capture(event: dict, **kwargs) -> None:
        emitted.append(dict(event))
        original_broadcast(event, **kwargs)

    server.broadcast = capture  # type: ignore[method-assign]
    app._broadcast = capture
    client = TestClient(server.fastapi)
    try:
        response = client.post(
            "/api/transcribe",
            files={"file": ("fixture.wav", _fixture_wav_bytes(), "audio/wav")},
        )
        assert response.status_code == 200
        assert response.json()["raw"].strip().casefold() == _expected_text().casefold()
        assert app.stt_engine.recovery_count == 1
        result_events = [
            event for event in emitted if event.get("type") == "transcription.result"
        ]
        assert len(result_events) == 1
        assert not any(event.get("type") == "stt.failure" for event in emitted)
        replacement_pid = app.stt_engine.worker_pid
        assert replacement_pid is not None and replacement_pid != first_pid
    finally:
        app.cleanup()
    assert first_pid is not None
    _wait_absent({first_pid, replacement_pid})


@requires_speech_fixture
@pytest.mark.parametrize("exit_timing", ["before_reply", "after_reply"])
def test_native_recovery_boundaries_deliver_to_injection_once(
    worker_binary: Path,
    tmp_path: Path,
    exit_timing: str,
) -> None:
    sentinel = tmp_path / f"{exit_timing}-once"
    operation_env = (
        "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION"
        if exit_timing == "before_reply"
        else "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_AFTER_REPLY_OPERATION"
    )
    manager = MacosFluidAudioManager(
        logger=logging.getLogger(f"tests.macos-stt-{exit_timing}"),
        timeout=0,
        worker_path=worker_binary,
        worker_environment={
            operation_env: "transcribe_chunk",
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL": str(sentinel),
        },
    )
    first_pid = manager.worker_pid
    assert first_pid is not None
    app, events, injector = _finalization_app()
    try:
        app._finalize_transcription_and_inject(_finalization_context(manager))
        injector.inject.assert_called_once()
        assert not any(event.get("type") == "stt.failure" for event in events)
        assert [
            event["active"]
            for event in events
            if event.get("type") == "state.transcribing"
        ] == [True, False]
        if exit_timing == "before_reply":
            assert manager.recovery_count == 1
        else:
            assert manager.recovery_count == 0
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and manager._supervisor.is_running:
                time.sleep(0.02)
            assert manager._supervisor.is_running is False
            manager.health()
            assert manager.recovery_count == 1
        replacement_pid = manager.worker_pid
    finally:
        manager.shutdown()
    owned = {first_pid}
    if replacement_pid is not None:
        owned.add(replacement_pid)
    _wait_absent(owned)


@requires_speech_fixture
def test_native_diagnostics_are_bounded_sanitized_and_content_free(
    worker_binary: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostic_dir = tmp_path / "diagnostics"
    hotword = "HOTWORD_SENTINEL_DO_NOT_RETAIN"
    secret = "private-token-value"
    license_key = "license-key-value"
    email = "private.person@example.test"
    private_path = "/Users/username/models/secret.mlmodelc"
    exception_message = (
        f"{hotword} path={private_path} token={secret} "
        f"license_key={license_key} email={email}"
    )
    ordinary = _supervisor(
        worker_binary,
        diagnostic_dir,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION": "health",
            "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_MESSAGE": exception_message,
            "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_VALUE": hotword,
        },
    )
    ordinary.load(LOAD_BODY)
    with pytest.raises(SttWorkerPythonFailure) as captured:
        ordinary.health()
    ordinary_pid = ordinary.pid
    assert ordinary_pid is not None
    ordinary.shutdown()

    flood = SttWorkerProcess(
        (str(worker_binary),),
        backend="parakeet",
        diagnostic_dir=diagnostic_dir,
        cwd=REPO_ROOT,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_OPERATION": "health",
            "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_BYTES": str(700 * 1024),
        },
    )
    flood.start(timeout=3)
    flood_pid = flood.pid
    assert flood_pid is not None
    flood.request("health", timeout=3)
    report = flood.shutdown(timeout=5)
    assert report is not None
    assert report.stderr_captured_bytes <= 64 * 1024

    crash = _supervisor(
        worker_binary,
        diagnostic_dir,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION": "health"
        },
    )
    crash.load(LOAD_BODY)
    with caplog.at_level(logging.WARNING, logger="tests.macos-stt-recovery"):
        with pytest.raises(NativeSttWorkerCrashed):
            crash.health()
    crash_report = crash.last_failure_report
    assert crash_report is not None
    assert crash_report.pid > 0
    assert crash_report.backend == "parakeet"
    assert crash_report.operation == "health"
    assert crash_report.exit_code_signed == 86
    assert crash_report.marker_phase == "active"
    assert "restart=recover_once" in caplog.text
    assert "restart=suppressed" in caplog.text
    assert f"pid={crash_report.pid}" in caplog.text
    crash.shutdown()

    retained = bytearray()
    retained.extend(caplog.text.encode("utf-8"))
    retained.extend(captured.value.diagnostic_message.encode("utf-8"))
    retained.extend(captured.value.diagnostic_traceback.encode("utf-8"))
    for path in diagnostic_dir.glob("worker-*"):
        assert path.stat().st_size <= 512 * 1024
        retained.extend(path.read_bytes())
    for forbidden in (
        _expected_text().encode("utf-8"),
        _fixture_wav_bytes(),
        hotword.encode("utf-8"),
        secret.encode("utf-8"),
        license_key.encode("utf-8"),
        email.encode("utf-8"),
        private_path.encode("utf-8"),
    ):
        assert forbidden not in retained
    assert b"[content redacted]" in retained
    assert b"[path redacted]" in retained
    assert b"[secret redacted]" in retained
    assert b"[email redacted]" in retained
    marker = diagnostic_dir / f"worker-{crash_report.pid}.last-operation.json"
    marker_text = marker.read_text(encoding="utf-8")
    assert '"protocol_version":1' in marker_text
    assert '"backend":"parakeet"' in marker_text
    assert '"operation":"health"' in marker_text
    _wait_absent({ordinary_pid, flood_pid, crash_report.pid})


@requires_speech_fixture
def test_repeated_backend_supervisor_shutdown_closes_owned_processes_and_pipes(
    worker_binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.server import WhisperstreamServer

    audio, sample_rate = _fixture_audio()
    owned_pids: set[int] = set()
    for cycle in range(3):
        process = SttWorkerProcess(
            (str(worker_binary),),
            backend="parakeet",
            diagnostic_dir=tmp_path / f"process-{cycle}",
            cwd=REPO_ROOT,
        )
        process.start(timeout=3)
        pid = process.pid
        assert pid is not None
        owned_pids.add(pid)
        assert psutil.Process(pid).children(recursive=True) == []
        process.request("load", body=LOAD_BODY, timeout=180)
        process.request_audio("transcribe", audio, sample_rate=sample_rate, timeout=30)
        started = time.monotonic()
        report = process.shutdown(timeout=5)
        assert time.monotonic() - started < 5.0
        assert report is not None and report.expected is True
        child = process._process
        assert child is not None
        assert all(
            stream is None or stream.closed
            for stream in (child.stdin, child.stdout, child.stderr)
        )
        for thread in (process._reader_thread, process._stderr_thread):
            assert thread is not None
            thread.join(timeout=1)
            assert not thread.is_alive()

    monkeypatch.setenv("WHISPERSTREAM_MACOS_STT_WORKER_BIN", str(worker_binary))
    for _ in range(2):
        app = WhisperstreamApp(headless=True, file_only=True)
        app.license_manager.can_start_recording = lambda: True
        assert app.stt_engine is not None
        pid = app.stt_engine.worker_pid
        assert pid is not None
        owned_pids.add(pid)
        client = TestClient(
            WhisperstreamServer(app, port=0, register_hotkeys=False).fastapi
        )
        response = client.post(
            "/api/transcribe",
            files={"file": ("fixture.wav", _fixture_wav_bytes(), "audio/wav")},
        )
        assert response.status_code == 200
        started = time.monotonic()
        app.cleanup()
        assert time.monotonic() - started < 5.0

    _wait_absent(owned_pids)
