"""Cohere parent isolation and child raw-decoding contracts."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import importlib
import sys

import numpy as np
import pytest

from whisperstream.cohere_manager import COHERE_DEFAULT_THREADS, CohereManager
from whisperstream.model_downloader import COHERE_TRANSCRIBE_FILES
from whisperstream.stt_errors import ModelNotPreparedError
from whisperstream.stt_worker_protocol import ValidatedRequest, encode_audio_request
from tests._final_stt_support import _FakeSupervisor


def _manager(root, language="en", threads=None):
    supervisor = _FakeSupervisor()
    manager = CohereManager(
        model_name="cohere-transcribe-03-2026-int8", model_dir=root,
        language=language, threads=threads, logger=MagicMock(), timeout=0,
        _supervisor=supervisor,
    )
    return manager, supervisor


def test_parent_load_and_language_update_use_supervised_contract(tmp_path):
    manager, supervisor = _manager(tmp_path)
    try:
        assert supervisor.load_calls == [{
            "model_name": "cohere-transcribe-03-2026-int8", "model_dir": str(tmp_path),
            "threads": COHERE_DEFAULT_THREADS, "forced_language": "en",
        }]
        manager.set_language_hint("DE")
        assert supervisor.language_calls == ["de"]
        assert manager.transcribe_chunk(np.zeros(1600, dtype=np.float32)) == "hello world"
    finally:
        manager.shutdown()
    assert supervisor.shutdown_calls == 1


@pytest.mark.parametrize("language", ["", "ru", "xx"])
def test_invalid_language_fails_before_worker_start(tmp_path, language):
    with pytest.raises(ValueError, match="Unsupported Cohere"):
        _manager(tmp_path, language=language)


@pytest.fixture
def native_module(monkeypatch):
    # Import the child behind a synthetic native binding; the parent tests above
    # never import it. Restore sys.modules to avoid contaminating native tests.
    names = ["whisperstream.stt_worker_cohere", "whisperstream.stt_worker_qwen"]
    saved = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    monkeypatch.setitem(sys.modules, "sherpa_onnx", MagicMock())
    module = importlib.import_module("whisperstream.stt_worker_cohere")
    yield module
    for name in names:
        sys.modules.pop(name, None)
    sys.modules.update(saved)


def _request(op, body):
    return ValidatedRequest(request_id="cohere-test", operation=op, backend="cohere", body=body)


def _stage_model(root):
    for relative in COHERE_TRANSCRIBE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")


@pytest.mark.parametrize("threads,expected", [(None, 6), (3, 3)])
def test_worker_uses_raw_cpu_greedy_contract(native_module, tmp_path, threads, expected):
    _stage_model(tmp_path)
    runtime = native_module.CohereWorkerRuntime()
    recognizer = native_module.sherpa_onnx.OfflineRecognizer.from_cohere_transcribe
    runtime.dispatch(_request("load", {
        "model_name": "cohere-transcribe-03-2026-int8", "model_dir": str(tmp_path),
        "forced_language": "en", "threads": threads,
    }), b"")
    assert recognizer.call_args.kwargs == {
        "encoder": str(tmp_path / "encoder.int8.onnx"),
        "decoder": str(tmp_path / "decoder.int8.onnx"),
        "tokens": str(tmp_path / "tokens.txt"), "num_threads": expected,
        "language": "en", "use_punct": True, "use_itn": True,
        "decoding_method": "greedy_search", "provider": "cpu",
    }
    stream = recognizer.return_value.create_stream.return_value
    stream.result = SimpleNamespace(text=" hello ")
    body, payload = encode_audio_request(np.zeros(1600, dtype=np.float32), sample_rate=16000)
    result, _ = runtime.dispatch(_request("transcribe_chunk", body), payload)
    assert result["text"] == "hello"
    stream.set_option.assert_called_with("language", "en")
    runtime.dispatch(_request("set_language_hint", {"language": "de"}), b"")
    runtime.dispatch(_request("transcribe_chunk", body), payload)
    stream.set_option.assert_called_with("language", "de")


def test_missing_file_fails_before_native_constructor(native_module, tmp_path):
    _stage_model(tmp_path)
    (tmp_path / "tokens.txt").unlink()
    with pytest.raises(ModelNotPreparedError, match="tokens.txt"):
        native_module.CohereWorkerRuntime().dispatch(_request("load", {
            "model_name": "cohere-transcribe-03-2026-int8", "model_dir": str(tmp_path),
            "forced_language": "en", "threads": None,
        }), b"")
    native_module.sherpa_onnx.OfflineRecognizer.from_cohere_transcribe.assert_not_called()


@pytest.fixture(autouse=True)
def _bypass_optional_detector(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_VAD_ENABLED", "0")


def test_recording_override_only_changes_one_child_stream(native_module, tmp_path):
    _stage_model(tmp_path)
    runtime = native_module.CohereWorkerRuntime()
    recognizer = native_module.sherpa_onnx.OfflineRecognizer.from_cohere_transcribe
    runtime.dispatch(_request('load', {
        'model_name': 'cohere-transcribe-03-2026-int8', 'model_dir': str(tmp_path),
        'forced_language': 'en', 'threads': None,
    }), b'')
    stream = recognizer.return_value.create_stream.return_value
    stream.result = SimpleNamespace(text='hello')
    body, payload = encode_audio_request(np.ones(1600, dtype=np.float32), sample_rate=16000)
    runtime.dispatch(_request('preview', {**body, 'language_override': 'ja'}), payload)
    stream.set_option.assert_called_with('language', 'ja')
    assert runtime._forced_language == 'en'
    runtime.dispatch(_request('transcribe_chunk', body), payload)
    stream.set_option.assert_called_with('language', 'en')
