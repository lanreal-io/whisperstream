"""Admission applies to every isolated ASR route without changing its health."""
import logging
from pathlib import Path
from unittest.mock import MagicMock
import numpy as np
import pytest
from tests._speech_fixture import requires_speech_fixture, speech_audio
from whisperstream.speech_gate_client import SpeechGateClient
from whisperstream.speech_gate_settings import SpeechGateSettings
from whisperstream.supervised_stt_manager import SupervisedSttManager
from whisperstream.qwen_manager import QwenManager
from whisperstream.cohere_manager import CohereManager
from whisperstream.parakeet_manager import ParakeetManager
from whisperstream.macos_stt_manager import MacosFluidAudioManager
from tests._final_stt_support import FluidHarness, manager_for  # noqa: F401


@pytest.mark.parametrize('route', ['fluid', 'sherpa', 'qwen', 'cohere'])
@pytest.mark.parametrize('preview', [False, True])
def test_rejection_before_asr_reload_and_accepted_waveform_identity(route, preview, monkeypatch):
    manager, supervisor = manager_for(route, monkeypatch)
    gate = MagicMock()
    manager._speech_gate = gate
    audio = np.full(1600, .02, dtype=np.float32)
    method = manager.try_transcribe_preview if preview else manager.transcribe_chunk
    try:
        manager.unload()
        loads = len(supervisor.load_calls)
        gate.has_speech.return_value = False
        result = method(audio)
        assert (result.text if preview else result) == ''
        assert supervisor.audio_calls == []
        assert len(supervisor.load_calls) == loads
        gate.has_speech.return_value = True
        method(audio)
        assert len(supervisor.load_calls) == loads + 1
        np.testing.assert_array_equal(supervisor.audio_calls[-1][1], audio)
    finally:
        manager.shutdown()


@pytest.mark.parametrize('route', ['fluid', 'sherpa', 'qwen', 'cohere'])
def test_busy_preview_skips_detector_and_idle_shutdown_close_both(route, monkeypatch):
    manager, supervisor = manager_for(route, monkeypatch)
    gate = MagicMock()
    manager._speech_gate = gate
    manager._lock.acquire()
    try:
        assert manager.try_transcribe_preview(np.ones(1600, dtype=np.float32)) is None
        gate.has_speech.assert_not_called()
    finally:
        manager._lock.release()
    manager._last_access = 0
    manager._unload_model()
    assert supervisor.stop_calls == 1
    gate.close.assert_called_once()
    manager.shutdown()
    assert gate.close.call_count == 2
    assert supervisor.shutdown_calls == 1


@pytest.mark.parametrize('body', [{}, {'admitted': 1, 'detector_failed': False},
    {'admitted': False, 'detector_failed': 'false'},
    {'admitted': False, 'detector_failed': True}])
def test_invalid_or_failed_detector_reply_latches_bypass_until_new_client(body, tmp_path):
    process = MagicMock()
    process.request_audio.return_value = body
    factory = MagicMock(return_value=process)
    client = SpeechGateClient(SpeechGateSettings(), diagnostic_dir=tmp_path,
        logger=logging.getLogger(__name__), process_factory=factory)
    audio = np.ones(1600, dtype=np.float32)
    assert client.has_speech(audio)
    assert client.failed
    assert client.has_speech(np.zeros(1600, dtype=np.float32))
    client.close()
    factory.assert_called_once()
    process.shutdown.assert_called_once_with(timeout=1.0)


def test_detector_transport_timeout_closes_exact_child_without_retry(tmp_path):
    process = MagicMock()
    process.request_audio.side_effect = TimeoutError()
    factory = MagicMock(return_value=process)
    client = SpeechGateClient(SpeechGateSettings(), diagnostic_dir=tmp_path,
        logger=logging.getLogger(__name__), timeout=.1, process_factory=factory)
    assert client.has_speech(np.ones(1600, dtype=np.float32))
    assert client.failed
    assert client.has_speech(np.ones(1600, dtype=np.float32))
    factory.assert_called_once()
    assert process.request_audio.call_args.kwargs['timeout'] == .1
    process.shutdown.assert_called_once()


def test_healthy_unload_can_start_new_detector_with_frozen_settings(tmp_path, monkeypatch):
    first, second = MagicMock(), MagicMock()
    for process in (first, second):
        process.request_audio.return_value = {'admitted': True, 'detector_failed': False}
    client = SpeechGateClient(SpeechGateSettings(threshold=.3), diagnostic_dir=tmp_path,
        logger=logging.getLogger(__name__), process_factory=MagicMock(side_effect=[first, second]))
    assert client.has_speech(np.ones(1600, dtype=np.float32))
    client.close()
    monkeypatch.setenv('WHISPERSTREAM_VAD_THRESHOLD', '.9')
    assert client.has_speech(np.ones(1600, dtype=np.float32))
    assert second.request.call_args.kwargs['body']['threshold'] == .3
    assert not client.failed
    client.close()


@pytest.mark.parametrize('route,expected', [('qwen', 'Japanese'), ('cohere', 'ja')])
def test_bound_recording_language_leaves_configured_hint_unchanged(route, expected, monkeypatch):
    manager, supervisor = manager_for(route, monkeypatch)
    manager._speech_gate = MagicMock()
    captured = []
    def request(operation, audio, *, sample_rate, body=None):
        captured.append(body)
        return {'text': 'hello'}
    supervisor.request_audio = request
    original = manager._forced_language
    try:
        bound = manager.for_language('ja')
        bound.transcribe_chunk(np.ones(1600, dtype=np.float32))
        bound.try_transcribe_preview(np.ones(1600, dtype=np.float32))
        assert captured == [{'language_override': expected}] * 2
        assert manager._forced_language == original
        assert supervisor.language_calls == []
    finally:
        manager.shutdown()


def test_real_detector_child_rejects_room_noise_and_cleans_up(tmp_path):
    client = SpeechGateClient(SpeechGateSettings(), diagnostic_dir=tmp_path,
                              logger=logging.getLogger(__name__))
    try:
        waveform = np.random.default_rng(42).normal(0, .0001, 16000).astype(np.float32)
        assert client.has_speech(waveform) is False
        assert not client.failed
        process = client._process
        assert process.pid is not None
    finally:
        client.close()
    assert not process.is_running


@requires_speech_fixture
def test_real_detector_child_accepts_reviewed_speech_fixture(tmp_path):
    audio, rate = speech_audio()
    client = SpeechGateClient(SpeechGateSettings(), diagnostic_dir=tmp_path,
                              logger=logging.getLogger(__name__))
    try:
        assert client.has_speech(audio, sample_rate=rate)
        assert not client.failed
        assert client._process.pid is not None
    finally:
        client.close()


def test_failed_detector_cleanup_is_bypass_and_terminates_only_owned_worker(tmp_path):
    process = MagicMock()
    process.shutdown.side_effect = TimeoutError()
    client = SpeechGateClient(SpeechGateSettings(), diagnostic_dir=tmp_path,
        logger=logging.getLogger(__name__), process_factory=MagicMock(return_value=process))
    process.request_audio.return_value = {'admitted': True, 'detector_failed': False}
    assert client.has_speech(np.ones(1600, dtype=np.float32))
    client.close()
    assert client.failed
    process.terminate_owned_worker.assert_called_once_with(expected=False)
    assert client.has_speech(np.zeros(1600, dtype=np.float32))


def test_asr_shutdown_error_still_closes_detector(monkeypatch):
    manager, supervisor = manager_for('qwen', monkeypatch)
    manager._speech_gate = MagicMock()
    supervisor.shutdown = MagicMock(side_effect=RuntimeError('shutdown failed'))
    with pytest.raises(RuntimeError):
        manager.shutdown()
    manager._speech_gate.close.assert_called_once()


def test_detector_only_worker_is_released_on_idle_after_rejection(monkeypatch):
    manager, supervisor = manager_for('qwen', monkeypatch)
    manager.unload()
    gate = MagicMock()
    gate.has_speech.return_value = False
    gate.is_running = True
    manager._speech_gate = gate
    assert manager.transcribe_chunk(np.ones(1600, dtype=np.float32)) == ''
    assert not manager._loaded
    manager._last_access = 0
    manager._unload_model()
    gate.close.assert_called_once()
    manager.shutdown()


def test_disabled_and_other_sample_rates_do_not_start_detector(tmp_path):
    factory = MagicMock()
    for settings, rate in [(SpeechGateSettings(enabled=False), 16000),
                            (SpeechGateSettings(), 8000)]:
        client = SpeechGateClient(settings, diagnostic_dir=tmp_path,
            logger=logging.getLogger(__name__), process_factory=factory)
        assert client.has_speech(np.zeros(1600, dtype=np.float32), sample_rate=rate)
        client.close()
    factory.assert_not_called()


def test_qwen_child_empty_override_preserves_auto_detection(monkeypatch):
    import importlib
    import sys
    from types import SimpleNamespace
    name = 'whisperstream.stt_worker_qwen'
    saved = sys.modules.pop(name, None)
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', MagicMock())
    try:
        module = importlib.import_module(name)
        runtime = module.QwenWorkerRuntime()
        runtime._recognizer = MagicMock()
        runtime._recognizer.create_stream.return_value.result = SimpleNamespace(text='hello')
        runtime._forced_language = 'German'
        runtime._decode(np.ones(1600, dtype=np.float32), sample_rate=16000, language_override='')
        runtime._recognizer.create_stream.return_value.set_option.assert_called_with('language', '')
        assert runtime._forced_language == 'German'
    finally:
        sys.modules.pop(name, None)
        if saved is not None:
            sys.modules[name] = saved


@pytest.mark.parametrize('route', ['fluid', 'sherpa', 'qwen', 'cohere'])
@pytest.mark.parametrize('preview', [False, True])
def test_failed_detector_keeps_every_asr_route_healthy_and_does_not_retry(route, preview, monkeypatch, tmp_path):
    manager, supervisor = manager_for(route, monkeypatch)
    process = MagicMock()
    process.request_audio.side_effect = TimeoutError()
    factory = MagicMock(return_value=process)
    manager._speech_gate = SpeechGateClient(SpeechGateSettings(), diagnostic_dir=tmp_path,
        logger=logging.getLogger(__name__), process_factory=factory)
    method = manager.try_transcribe_preview if preview else manager.transcribe_chunk
    try:
        audio = np.ones(1600, dtype=np.float32)
        result = method(audio)
        assert (result.text if preview else result) == 'hello world'
        assert manager._speech_gate.failed
        result = method(audio)
        assert (result.text if preview else result) == 'hello world'
        assert manager.health().loaded
        assert manager.recovery_count == 0
        assert supervisor.stop_calls == 0
        factory.assert_called_once()
    finally:
        manager.shutdown()
