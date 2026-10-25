"""Deterministic typed-recovery and exactly-once delivery contract tests."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import psutil
import pytest

from whisperstream.main import WhisperstreamApp, _DictationFinalizationContext
from whisperstream.stt_errors import (
    NativeSttWorkerCrashed,
    SttWorkerProtocolFailure,
    SttWorkerPythonFailure,
    SttWorkerStartFailure,
    SttWorkerTimeoutFailure,
)
from whisperstream.stt_supervisor import SttWorkerSupervisor
from whisperstream.stt_worker_process import (
    STARTUP_TIMEOUT_SECONDS,
    source_worker_command,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMAND = source_worker_command(REPO_ROOT)
pytestmark = pytest.mark.usefixtures("accepted_eula")


def _supervisor(
    tmp_path: Path,
    *,
    environment: dict[str, str] | None = None,
    inference_timeout: float = 3.0,
    command: tuple[str, ...] = SOURCE_COMMAND,
) -> SttWorkerSupervisor:
    return SttWorkerSupervisor(
        backend="prototype",
        model_identifier="prototype-v1",
        diagnostic_dir=tmp_path,
        logger=logging.getLogger("tests.stt-recovery-contract"),
        command=command,
        cwd=REPO_ROOT,
        environment=environment,
        # Cold interpreter startup slows under parallel load; it is not the
        # bound under test, so allow the production startup budget.
        startup_timeout=STARTUP_TIMEOUT_SECONDS,
        load_timeout=3.0,
        inference_timeout=inference_timeout,
        shutdown_timeout=3.0,
        suppression_seconds=10.0,
    )


def _context(manager: object) -> _DictationFinalizationContext:
    return _DictationFinalizationContext(
        recording_id=None,
        waveform=np.zeros(1600, dtype=np.float32),
        chunk_futures=(),
        transcription_started_at=0.0,
        recording_started_at=0.0,
        stt_manager=manager,
        dictation_app=None,
        manual_profile_id=None,
        override_anchor=None,
        screen_reader_detected=False,
        inject_suppressed=False,
        agent_compose_lease=None,
    )


def _finalization_app() -> tuple[WhisperstreamApp, list[dict], MagicMock]:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = logging.getLogger("tests.stt-recovery-finalization")
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


def test_worker_start_failure_is_typed_and_recovery_is_bounded(tmp_path: Path) -> None:
    missing = tmp_path / "missing-worker.exe"
    supervisor = _supervisor(tmp_path, command=(str(missing), "--stt-worker"))

    with pytest.raises(SttWorkerStartFailure) as captured:
        supervisor.load({"model_name": "prototype-v1"})

    assert captured.value.code == "worker_start_failed"
    assert captured.value.recovery_attempted is True
    assert captured.value.suppressed is True
    assert supervisor.recovery_count == 1
    assert supervisor.is_running is False


def test_load_crash_recovers_once_and_graceful_shutdown_cleans_child(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "load-crash-once.sentinel"
    supervisor = _supervisor(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION": "load",
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL": str(sentinel),
        },
    )
    supervisor.load({"model_name": "prototype-v1"})
    worker_pid = supervisor.pid
    assert supervisor.recovery_count == 1
    assert worker_pid is not None and psutil.pid_exists(worker_pid)

    report = supervisor.shutdown()

    assert report is not None and report.expected is True
    assert not psutil.pid_exists(worker_pid)


def test_repeated_load_crash_is_suppressed_without_another_restart(
    tmp_path: Path,
) -> None:
    supervisor = _supervisor(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION": "load"
        },
    )
    with pytest.raises(NativeSttWorkerCrashed) as first:
        supervisor.load({"model_name": "broken-model"})
    assert first.value.recovery_attempted is True
    assert supervisor.recovery_count == 1

    with pytest.raises(NativeSttWorkerCrashed) as repeated:
        supervisor.load({"model_name": "broken-model"})
    assert repeated.value.recovery_attempted is False
    assert repeated.value.suppressed is True
    assert supervisor.recovery_count == 1
    supervisor.shutdown()


def test_inference_crash_retries_once_before_returning_one_result(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "inference-crash-once.sentinel"
    supervisor = _supervisor(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION": "transcribe_chunk",
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL": str(sentinel),
        },
    )
    try:
        supervisor.load({"model_name": "prototype-v1"})
        first_pid = supervisor.pid
        result = supervisor.request_audio(
            "transcribe_chunk",
            np.zeros(1600, dtype=np.float32),
            sample_rate=16_000,
        )

        assert result["text"] == ""
        assert supervisor.recovery_count == 1
        assert supervisor.pid is not None and supervisor.pid != first_pid
    finally:
        supervisor.shutdown()


def test_repeated_native_exit_suppresses_then_model_success_resets(
    tmp_path: Path,
) -> None:
    supervisor = _supervisor(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION": "transcribe_chunk"
        },
    )
    supervisor.load({"model_name": "model-one"})
    with pytest.raises(NativeSttWorkerCrashed) as first:
        supervisor.request_audio(
            "transcribe_chunk",
            np.zeros(1600, dtype=np.float32),
            sample_rate=16_000,
        )
    assert first.value.recovery_attempted is True
    assert first.value.suppressed is True
    assert supervisor.recovery_count == 1

    with pytest.raises(NativeSttWorkerCrashed) as repeated:
        supervisor.request_audio(
            "transcribe_chunk",
            np.zeros(1600, dtype=np.float32),
            sample_rate=16_000,
        )
    assert repeated.value.recovery_attempted is False
    assert repeated.value.suppressed is True
    assert supervisor.recovery_count == 1

    supervisor.load({"model_name": "model-two"})
    with pytest.raises(NativeSttWorkerCrashed):
        supervisor.request_audio(
            "transcribe_chunk",
            np.zeros(1600, dtype=np.float32),
            sample_rate=16_000,
        )
    assert supervisor.recovery_count == 2
    supervisor.shutdown()


def test_timeout_and_protocol_failures_are_typed(tmp_path: Path) -> None:
    # Both the first request and the recovery retry must time out. A loaded
    # runner can deschedule the waiting thread past a short reply delay, so
    # delay replies by the worker's 10 s maximum; each timed-out worker is
    # terminated, so the test never waits for the delay itself.
    timeout_supervisor = _supervisor(
        tmp_path / "timeout",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION": "transcribe_chunk",
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_MS": "10000",
        },
        inference_timeout=0.05,
    )
    timeout_supervisor.load({})
    with pytest.raises(SttWorkerTimeoutFailure):
        timeout_supervisor.request_audio(
            "transcribe_chunk",
            np.zeros(1600, dtype=np.float32),
            sample_rate=16_000,
        )
    timeout_supervisor.shutdown()

    protocol_supervisor = _supervisor(
        tmp_path / "protocol",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_MALFORMED_REPLY_OPERATION": "health"
        },
    )
    protocol_supervisor.load({})
    with pytest.raises(SttWorkerProtocolFailure):
        protocol_supervisor.health()
    protocol_supervisor.shutdown()


def test_python_exception_is_typed_without_restart_and_success_resets(
    tmp_path: Path,
) -> None:
    supervisor = _supervisor(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION": "health"
        },
    )
    try:
        supervisor.load({})
        worker_pid = supervisor.pid
        with pytest.raises(SttWorkerPythonFailure) as captured:
            supervisor.health()
        assert captured.value.code == "worker_python_exception"
        assert captured.value.public_metadata().keys() == {
            "engine",
            "backend",
            "model_id",
            "operation",
            "code",
            "recovery_attempted",
            "suppressed",
        }
        assert supervisor.recovery_count == 0
        assert supervisor.pid == worker_pid
        assert supervisor.is_running is True

        supervisor.load({"model_name": "prototype-v1"})
        assert supervisor.last_failure is None
    finally:
        supervisor.shutdown()


def test_recording_finalization_delivers_once_after_internal_single_retry() -> None:
    app, events, injector = _finalization_app()
    native_attempts = 0

    class RecoveredManager:
        def transcribe(self, _waveform: np.ndarray, *, sample_rate: int) -> str:
            nonlocal native_attempts
            assert sample_rate == 16_000
            native_attempts += 2
            return "content held outside diagnostics"

    app._finalize_transcription_and_inject(_context(RecoveredManager()))

    assert native_attempts == 2
    injector.inject.assert_called_once_with("content held outside diagnostics")
    assert [event["active"] for event in events if event["type"] == "state.transcribing"] == [True, False]
    assert not any(event["type"] == "stt.failure" for event in events)


def test_recording_finalization_never_injects_or_emits_result_after_failure() -> None:
    app, events, injector = _finalization_app()

    class FailedManager:
        def transcribe(self, _waveform: np.ndarray, *, sample_rate: int) -> str:
            assert sample_rate == 16_000
            raise SttWorkerTimeoutFailure(
                backend="parakeet",
                model_identifier="parakeet-v1",
                operation="transcribe_chunk",
                report=None,
                recovery_attempted=True,
                suppressed=True,
            )

    app._finalize_transcription_and_inject(_context(FailedManager()))

    injector.inject.assert_not_called()
    failures = [event for event in events if event["type"] == "stt.failure"]
    assert len(failures) == 1
    assert failures[0]["code"] == "worker_timeout"
    assert not any(event["type"] == "transcription.result" for event in events)
    assert [event["active"] for event in events if event["type"] == "state.transcribing"] == [True, False]


def test_parent_event_and_logs_exclude_worker_diagnostics(caplog) -> None:
    app, events, _injector = _finalization_app()
    private_path = r"C:\Users\Private Person\models\secret.onnx"
    diagnostic = "PRIVATE_WORKER_DIAGNOSTIC"
    failure = SttWorkerPythonFailure(
        backend="parakeet",
        model_identifier=private_path,
        operation="load",
        exception_type="RuntimeError",
        diagnostic_message=diagnostic,
        diagnostic_traceback=diagnostic,
    )

    with caplog.at_level(logging.ERROR, logger=app.logger.name):
        app._broadcast_stt_failure(failure)

    assert len(events) == 1
    serialized_event = str(events[0])
    assert private_path not in serialized_event
    assert diagnostic not in serialized_event
    assert private_path not in caplog.text
    assert diagnostic not in caplog.text
    assert events[0]["model_id"].startswith("redacted-")
