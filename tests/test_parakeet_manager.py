from __future__ import annotations

import os
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from whisperstream.parakeet_manager import ModelNotPreparedError, ParakeetManager
from whisperstream.stt_contracts import DetailedTranscriptionResult, SttLifecycleState
from whisperstream.stt_supervisor import SttWorkerRemoteError
from tests._platform_test_support import _FakeSupervisor  # noqa: F401


def _manager(
    supervisor: _FakeSupervisor,
    *,
    timeout: float = 0,
) -> ParakeetManager:
    return ParakeetManager(
        model_name="nemo-parakeet-tdt-0.6b-v3",
        quantization="int8",
        threads=2,
        logger=MagicMock(),
        model_dir=Path(r"C:\Models\Parakeet"),
        timeout=timeout,
        hotwords=["alpha"],
        hotwords_score=1.7,
        beam_width=4,
        _supervisor=supervisor,  # type: ignore[arg-type]
    )


def test_initial_load_sends_complete_worker_owned_model_configuration() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor)
    try:
        assert supervisor.load_calls == [
            {
                "model_name": "nemo-parakeet-tdt-0.6b-v3",
                "model_dir": r"C:\Models\Parakeet",
                "quantization": "int8",
                "threads": 2,
                "hotwords": ["alpha"],
                "hotword_score": 1.7,
                "beam_width": 4,
            }
        ]
        assert manager.worker_pid == supervisor.pid
        assert manager.worker_pid != os.getpid()
        assert manager.health().state is SttLifecycleState.READY
    finally:
        manager.shutdown()


def test_worker_model_preparation_failure_preserves_parent_exception_contract() -> None:
    supervisor = _FakeSupervisor()
    supervisor.load = MagicMock(
        side_effect=SttWorkerRemoteError(
            {
                "category": "python",
                "code": "python_exception",
                "exception_type": "ModelNotPreparedError",
                "message": "model file is missing",
            }
        )
    )
    with pytest.raises(ModelNotPreparedError, match="model file is missing"):
        _manager(supervisor)
    assert supervisor.shutdown_calls == 1


@patch("whisperstream.supervised_stt_manager.time.time")
def test_idle_unload_and_lazy_reload_preserve_last_access(mock_time) -> None:
    supervisor = _FakeSupervisor()
    mock_time.return_value = 1000.0
    manager = _manager(supervisor, timeout=100.0)
    try:
        assert manager._last_access == 1000.0
        mock_time.return_value = 1200.0
        first_pid = manager.worker_pid
        manager._unload_model()
        assert supervisor.unload_calls == 1
        assert supervisor.stop_calls == 1
        assert manager.worker_pid is None
        assert manager.health().state is SttLifecycleState.IDLE

        mock_time.return_value = 2000.0
        assert manager.transcribe_chunk(np.zeros(1600, dtype=np.float32)) == "hello"
        assert len(supervisor.load_calls) == 2
        assert manager.worker_pid is not None and manager.worker_pid != first_pid
        assert manager._last_access == 2000.0
    finally:
        manager.shutdown()


def test_timeout_zero_disables_monitor_but_keeps_model_loaded() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor, timeout=0)
    try:
        assert manager._monitor_thread is None
        assert manager.health().loaded is True
    finally:
        manager.shutdown()


def test_transcribe_chunk_preserves_audio_and_sample_rate() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor)
    try:
        audio = np.arange(12, dtype=np.int16).reshape(6, 2)
        assert manager.transcribe_chunk(audio, sample_rate=22_050) == "hello"
        operation, sent, sample_rate = supervisor.audio_calls[-1]
        assert operation == "transcribe_chunk"
        assert sample_rate == 22_050
        assert sent.dtype == np.float32
        np.testing.assert_array_equal(sent, np.arange(12, dtype=np.float32))
    finally:
        manager.shutdown()


def test_concurrent_chunk_calls_remain_serialized_by_parent_lock() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor)
    entered = threading.Event()
    release = threading.Event()
    second_finished = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def decode(_operation, _audio, _sample_rate):
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            entered.set()
            assert release.wait(timeout=3)
        return {"text": "hello"}

    supervisor.audio_side_effect = decode
    first = threading.Thread(
        target=manager.transcribe_chunk,
        args=(np.zeros(1600, dtype=np.float32),),
    )
    second = threading.Thread(
        target=lambda: (
            manager.transcribe_chunk(np.zeros(1600, dtype=np.float32)),
            second_finished.set(),
        )
    )
    try:
        first.start()
        assert entered.wait(timeout=3)
        second.start()
        time.sleep(0.1)
        assert not second_finished.is_set()
        release.set()
        first.join(timeout=3)
        second.join(timeout=3)
        assert not first.is_alive()
        assert not second.is_alive()
        assert second_finished.is_set()
        assert calls == 2
    finally:
        release.set()
        manager.shutdown()


def test_preview_returns_immutable_timed_metadata_from_worker() -> None:
    supervisor = _FakeSupervisor()
    supervisor.audio_result = {
        "text": "hello world",
        "tokens": ["hello", "world"],
        "timestamps": [0.12, 0.48],
        "durations": [0.30, 0.24],
        "ys_log_probs": [-0.1, -0.2],
    }
    manager = _manager(supervisor)
    try:
        result = manager.try_transcribe_preview(np.zeros(1600, dtype=np.float32))
        assert result == DetailedTranscriptionResult(
            text="hello world",
            tokens=("hello", "world"),
            timestamps=(0.12, 0.48),
            durations=(0.30, 0.24),
            ys_log_probs=(-0.1, -0.2),
        )
        assert supervisor.audio_calls[-1][0] == "preview"
        with pytest.raises(FrozenInstanceError):
            result.text = "changed"  # type: ignore[misc]
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    "body",
    [
        {"text": "hello", "tokens": ["hello"], "timestamps": []},
        {"text": "hello", "tokens": ["hello"], "timestamps": [0.1], "durations": []},
        {"text": "hello", "tokens": [1], "timestamps": [0.1]},
    ],
)
def test_preview_malformed_metadata_degrades_to_text_only(body) -> None:
    supervisor = _FakeSupervisor()
    supervisor.audio_result = body
    manager = _manager(supervisor)
    try:
        assert manager.try_transcribe_preview(
            np.zeros(1600, dtype=np.float32)
        ) == DetailedTranscriptionResult(text="hello")
    finally:
        manager.shutdown()


def test_preview_busy_admission_returns_without_worker_request() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor)
    finished = threading.Event()
    returned = []
    manager._lock.acquire()
    try:
        worker = threading.Thread(
            target=lambda: (
                returned.append(
                    manager.try_transcribe_preview(np.zeros(1600, dtype=np.float32))
                ),
                finished.set(),
            )
        )
        worker.start()
        assert finished.wait(timeout=0.25)
    finally:
        manager._lock.release()
    worker.join(timeout=1)
    manager.shutdown()
    assert returned == [None]
    assert supervisor.audio_calls == []


def test_preview_error_releases_admission_lock() -> None:
    supervisor = _FakeSupervisor()
    attempts = 0

    def decode(_operation, _audio, _sample_rate):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("decode failed")
        return {"text": "recovered"}

    supervisor.audio_side_effect = decode
    manager = _manager(supervisor)
    try:
        with pytest.raises(RuntimeError, match="decode failed"):
            manager.try_transcribe_preview(np.zeros(1600, dtype=np.float32))
        recovered = manager.try_transcribe_preview(np.zeros(1600, dtype=np.float32))
        assert recovered == DetailedTranscriptionResult(text="recovered")
    finally:
        manager.shutdown()


def test_hotword_update_is_forwarded_as_reload_and_never_raises() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor)
    try:
        manager.update_hotwords(["beta", "gamma"], 2.5)
        assert supervisor.hotword_calls == [(["beta", "gamma"], 2.5)]

        def fail(_hotwords, _score):
            raise RuntimeError("content-free reload failure")

        supervisor.update_hotwords = fail  # type: ignore[method-assign]
        manager.update_hotwords(["delta"], 3.0)
    finally:
        manager.shutdown()


def test_shutdown_stops_monitor_and_owned_supervisor_once() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor, timeout=60)
    monitor = manager._monitor_thread
    assert monitor is not None and monitor.is_alive()

    manager.shutdown()
    manager.shutdown()

    assert not monitor.is_alive()
    assert supervisor.shutdown_calls == 1
    assert manager.health().state is SttLifecycleState.SHUTDOWN


@pytest.fixture(autouse=True)
def _bypass_optional_detector(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_VAD_ENABLED", "0")
