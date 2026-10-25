from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from whisperstream.qwen_manager import QWEN_MAX_NEW_TOKENS, QwenManager
from whisperstream.stt_contracts import (
    DetailedTranscriptionResult,
    SttLifecycleState,
    SttPreviewMetadata,
)
from whisperstream.stt_errors import ModelNotPreparedError, SttWorkerProtocolFailure
from whisperstream.stt_supervisor import SttWorkerRemoteError
from tests._final_stt_support import _FakeSupervisor  # noqa: F401


def _manager(
    supervisor: _FakeSupervisor,
    *,
    timeout: float = 0,
    forced_language: str | None = None,
) -> QwenManager:
    return QwenManager(
        model_name="qwen3-asr-0.6b-int8",
        quantization="int8",
        threads=2,
        logger=MagicMock(),
        model_dir=Path(r"C:\Models\Qwen"),
        timeout=timeout,
        hotwords=[],
        hotwords_score=2.0,
        beam_width=7,
        forced_language=forced_language,
        _supervisor=supervisor,  # type: ignore[arg-type]
    )


def test_initial_load_sends_qwen_only_worker_configuration() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor, forced_language="Japanese")
    try:
        assert supervisor.load_calls == [
            {
                "model_name": "qwen3-asr-0.6b-int8",
                "model_dir": r"C:\Models\Qwen",
                "threads": 2,
                "forced_language": "Japanese",
                "max_new_tokens": QWEN_MAX_NEW_TOKENS,
            }
        ]
        assert "hotwords" not in supervisor.load_calls[0]
        assert "quantization" not in supervisor.load_calls[0]
        assert manager.worker_pid != os.getpid()
        assert manager.health().state is SttLifecycleState.READY
    finally:
        manager.shutdown()


def test_capabilities_keep_qwen_differences_explicit() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor)
    try:
        assert manager.capabilities.preview is not None
        assert manager.capabilities.preview.metadata is SttPreviewMetadata.TEXT
        assert manager.preview_capability is manager
        assert manager.hotword_capability is None
        assert manager.language_hint_capability is manager
    finally:
        manager.shutdown()


def test_transcribe_chunk_routes_audio_through_qwen_worker() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor)
    try:
        audio = np.arange(12, dtype=np.int16).reshape(6, 2)
        assert manager.transcribe_chunk(audio, sample_rate=22_050) == "hello world"
        operation, sent, sample_rate = supervisor.audio_calls[-1]
        assert operation == "transcribe_chunk"
        assert sample_rate == 22_050
        assert sent.dtype == np.float32
        np.testing.assert_array_equal(sent, np.arange(12, dtype=np.float32))
    finally:
        manager.shutdown()


def test_preview_is_text_only_even_when_worker_body_has_unusable_tokens() -> None:
    supervisor = _FakeSupervisor()
    supervisor.audio_result = {"text": "hello world", "tokens": ["hello", "world"]}
    manager = _manager(supervisor)
    try:
        result = manager.try_transcribe_preview(np.zeros(1600, dtype=np.float32))
        assert result == DetailedTranscriptionResult(text="hello world")
        assert supervisor.audio_calls[-1][0] == "preview"
    finally:
        manager.shutdown()


def test_hotword_compatibility_method_is_noop_without_worker_operation() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor)
    try:
        load_count = len(supervisor.load_calls)
        manager.update_hotwords([], 2.0)
        assert len(supervisor.load_calls) == load_count
        assert not hasattr(supervisor, "hotword_calls")
    finally:
        manager.shutdown()


@patch("whisperstream.supervised_stt_manager.time.time")
def test_idle_offload_stops_worker_and_lazy_access_recreates_it(mock_time) -> None:
    mock_time.return_value = 1000.0
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor, timeout=100.0)
    try:
        first_pid = manager.worker_pid
        mock_time.return_value = 1200.0
        manager._unload_model()
        assert supervisor.stop_calls == 1
        assert manager.worker_pid is None
        assert manager.health().state is SttLifecycleState.IDLE

        mock_time.return_value = 1300.0
        assert manager.transcribe_chunk(np.zeros(1600, dtype=np.float32)) == "hello world"
        assert manager.worker_pid is not None and manager.worker_pid != first_pid
        assert len(supervisor.started_pids) == 2
    finally:
        manager.shutdown()


def test_timeout_zero_disables_idle_monitor() -> None:
    supervisor = _FakeSupervisor()
    manager = _manager(supervisor, timeout=0)
    try:
        assert manager._monitor_thread is None
        assert manager.health().loaded is True
    finally:
        manager.shutdown()


def test_model_preparation_failure_stops_failed_supervisor() -> None:
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


def test_capability_mismatch_stops_failed_supervisor() -> None:
    supervisor = _FakeSupervisor()
    supervisor.capabilities = {}
    with pytest.raises(SttWorkerProtocolFailure) as captured:
        _manager(supervisor)
    assert captured.value.code == "worker_protocol_failed"
    assert captured.value.operation == "load"
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert "capability mismatch" in str(captured.value.__cause__)
    assert supervisor.shutdown_calls == 1


def test_shutdown_stops_monitor_and_supervisor_once() -> None:
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
