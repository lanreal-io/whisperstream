"""Final alignment comes from fresh authoritative decodes, never preview."""

from unittest.mock import MagicMock

import numpy as np
import pytest

from whisperstream.stt_contracts import DetailedTranscriptionResult
from whisperstream.stt_errors import SttWorkerProtocolFailure
from tests._final_stt_support import manager_for


@pytest.mark.parametrize("route", ["fluid", "sherpa", "qwen", "cohere"])
def test_final_detailed_decode_uses_final_operation_and_no_preview(route, monkeypatch):
    manager, supervisor = manager_for(route, monkeypatch)
    manager._speech_gate = MagicMock()
    audio = np.ones(1600, dtype=np.float32)
    supervisor.audio_result = {"text": "hello", "tokens": ["hello"],
                               "timestamps": [0.01], "durations": [0.02]}
    try:
        manager.try_transcribe_preview = MagicMock(side_effect=AssertionError("preview"))
        first = manager.transcribe_chunk_detailed(audio)
        supervisor.audio_result = {"text": "second"}
        second = manager.transcribe_chunk_detailed(audio)
        if route in {"sherpa", "fluid"}:
            assert manager.capabilities.final_alignment
            assert first == DetailedTranscriptionResult(
                text="hello", tokens=("hello",), timestamps=(0.01,), durations=(0.02,))
        else:
            assert not manager.capabilities.final_alignment
            assert first == DetailedTranscriptionResult(text="hello")
        assert second == DetailedTranscriptionResult(text="second")
        assert [call[0] for call in supervisor.audio_calls] == ["transcribe_chunk"] * 2
        np.testing.assert_array_equal(supervisor.audio_calls[0][1], audio)
        assert manager.transcribe_chunk(audio) == "second"
    finally:
        manager.shutdown()


def test_final_detailed_language_binding_survives_without_mutating_hint(monkeypatch):
    manager, supervisor = manager_for("qwen", monkeypatch)
    manager._speech_gate = MagicMock()
    request = MagicMock(return_value={"text": "bonjour"})
    supervisor.request_audio = request
    try:
        bound = manager.for_language("fr")
        assert bound.transcribe_chunk_detailed(np.ones(1600, dtype=np.float32)).text == "bonjour"
        assert request.call_args.args[0] == "transcribe_chunk"
        assert request.call_args.kwargs["body"] == {"language_override": "French"}
        assert supervisor.language_calls == []
    finally:
        manager.shutdown()


def test_final_detailed_rejection_empty_and_shutdown_never_dispatch(monkeypatch):
    manager, supervisor = manager_for("sherpa", monkeypatch)
    gate = MagicMock()
    gate.has_speech.return_value = False
    manager._speech_gate = gate
    try:
        assert manager.transcribe_chunk_detailed(np.ones(1600, dtype=np.float32)).text == ""
        assert manager.transcribe_chunk_detailed(np.array([], dtype=np.float32)).text == ""
        assert supervisor.audio_calls == []
        manager.shutdown()
        with pytest.raises(RuntimeError, match="shut down"):
            manager.transcribe_chunk_detailed(np.ones(1600, dtype=np.float32))
        assert supervisor.audio_calls == []
    finally:
        manager.shutdown()


def test_final_detailed_protocol_failure_reports_final_operation(monkeypatch):
    manager, supervisor = manager_for("sherpa", monkeypatch)
    manager._speech_gate = MagicMock()
    supervisor.audio_result = {"text": None}
    try:
        with pytest.raises(SttWorkerProtocolFailure) as failure:
            manager.transcribe_chunk_detailed(np.ones(1600, dtype=np.float32))
        assert failure.value.operation == "transcribe_chunk"
    finally:
        manager.shutdown()


@pytest.mark.parametrize("metadata", [
    {"tokens": ["hello"], "timestamps": [float("nan")]},
    {"tokens": ["hello"], "timestamps": [-0.1]},
    {"tokens": ["hello"], "timestamps": [0.1]},
    {"tokens": ["hel", "lo"], "timestamps": [0.04, 0.01]},
    {"tokens": ["other"], "timestamps": [0.01]},
    {"tokens": ["hello"], "timestamps": [0.01], "durations": [float("inf")]},
    {"tokens": ["hello"], "timestamps": [0.01], "durations": [-0.01]},
    {"tokens": ["hello"], "timestamps": [0.01], "durations": [0.1]},
    {"tokens": ["hello"], "timestamps": []},
    {"tokens": ["hello"], "timestamps": [10 ** 1000]},
    {"tokens": ["hello"], "timestamps": [0.01], "durations": [10 ** 1000]},
    {"tokens": ["hello"], "timestamps": [0.01], "durations": [True]},
    {"tokens": ["hello"], "timestamps": [0.01], "durations": ["bad"]},
    {"tokens": ["hello"], "timestamps": [0.01], "durations": "bad"},
    {"tokens": ["hello"], "timestamps": [0.01], "ys_log_probs": [float("nan")]},
    {"tokens": ["hello"], "timestamps": [0.01], "ys_log_probs": [10 ** 1000]},
])
def test_final_invalid_alignment_preserves_all_text(metadata, monkeypatch):
    manager, supervisor = manager_for("sherpa", monkeypatch)
    manager._speech_gate = MagicMock()
    supervisor.audio_result = {"text": "hello", **metadata}
    try:
        assert manager.transcribe_chunk_detailed(np.ones(1600, dtype=np.float32)) == (
            DetailedTranscriptionResult(text="hello"))
    finally:
        manager.shutdown()
