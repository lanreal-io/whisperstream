from __future__ import annotations

import importlib.util
import os
from pathlib import Path, PureWindowsPath
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from whisperstream.stt_worker_protocol import (
    PROTOCOL_VERSION,
    SttWorkerCapabilityError,
    ValidatedRequest,
    encode_audio_request,
)

# Keep the worker's Windows DLL discovery order intact before capability
# collection imports sherpa itself.
if sys.platform == "win32":
    _spec = importlib.util.find_spec("onnxruntime")
    if _spec and _spec.origin:
        _ort_dir = str(Path(_spec.origin).parent)
        os.add_dll_directory(_ort_dir)
        os.environ["PATH"] = _ort_dir + os.pathsep + os.environ.get("PATH", "")
        _capi_dir = Path(_spec.origin).parent / "capi"
        if _capi_dir.exists():
            os.add_dll_directory(str(_capi_dir))
            os.environ["PATH"] = str(_capi_dir) + os.pathsep + os.environ["PATH"]

pytest.importorskip(
    "sherpa_onnx",
    reason="Windows sherpa Qwen worker runtime is unavailable on this platform",
)

from whisperstream.stt_worker_qwen import (
    ModelNotPreparedError,
    QWEN_MAX_NEW_TOKENS,
    QwenWorkerRuntime,
    model_dir_for_sherpa,
)


def _request(operation: str, body=None) -> ValidatedRequest:
    return ValidatedRequest(
        request_id=f"test-{operation}",
        operation=operation,
        backend="qwen",
        body=dict(body or {}),
    )


def _load_body(model_dir: Path, *, language: str | None = None) -> dict[str, object]:
    return {
        "model_name": "qwen3-asr-0.6b-int8",
        "model_dir": str(model_dir),
        "threads": 2,
        "forced_language": language,
        "max_new_tokens": QWEN_MAX_NEW_TOKENS,
    }


def _stage_model_files(model_dir: Path) -> None:
    for relative in (
        "conv_frontend.onnx",
        "encoder.int8.onnx",
        "decoder.int8.onnx",
        "tokenizer/vocab.json",
        "tokenizer/merges.txt",
        "tokenizer/tokenizer_config.json",
    ):
        path = model_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")


def test_worker_constructs_qwen_recognizer_with_safe_paths(tmp_path: Path) -> None:
    _stage_model_files(tmp_path)
    recognizer = MagicMock()
    safe_dir = PureWindowsPath(r"C:\MODELS\QWEN")
    with (
        patch(
            "whisperstream.stt_worker_qwen.model_dir_for_sherpa",
            return_value=safe_dir,
        ) as safe_path,
        patch(
            "whisperstream.stt_worker_qwen.sherpa_onnx.OfflineRecognizer.from_qwen3_asr",
            return_value=recognizer,
        ) as construct,
    ):
        runtime = QwenWorkerRuntime()
        body, should_exit = runtime.dispatch(
            _request("load", _load_body(tmp_path, language="Japanese")),
            b"",
        )

    assert should_exit is False
    assert body == {"state": "ready", "loaded": True}
    safe_path.assert_called_once_with(tmp_path)
    kwargs = construct.call_args.kwargs
    assert kwargs == {
        "conv_frontend": r"C:\MODELS\QWEN\conv_frontend.onnx",
        "encoder": r"C:\MODELS\QWEN\encoder.int8.onnx",
        "decoder": r"C:\MODELS\QWEN\decoder.int8.onnx",
        "tokenizer": r"C:\MODELS\QWEN\tokenizer",
        "num_threads": 2,
        "provider": "cpu",
        "max_new_tokens": 512,
    }


def test_worker_missing_file_fails_before_native_construction(tmp_path: Path) -> None:
    runtime = QwenWorkerRuntime()
    with (
        patch(
            "whisperstream.stt_worker_qwen.sherpa_onnx.OfflineRecognizer.from_qwen3_asr"
        ) as construct,
        pytest.raises(ModelNotPreparedError, match="Qwen model file not found"),
    ):
        runtime.dispatch(_request("load", _load_body(tmp_path)), b"")
    construct.assert_not_called()


def test_worker_decode_applies_forced_language_and_preview_is_text_only() -> None:
    runtime = QwenWorkerRuntime()
    recognizer = MagicMock()
    stream = MagicMock()
    stream.result = SimpleNamespace(text="  hello world  ", tokens=["hello", "world"])
    recognizer.create_stream.return_value = stream
    runtime._recognizer = recognizer
    runtime._forced_language = "Japanese"
    runtime.loaded = True
    audio_body, payload = encode_audio_request(
        np.zeros(1600, dtype=np.float32),
        sample_rate=22_050,
    )

    body, should_exit = runtime.dispatch(_request("preview", audio_body), payload)

    assert should_exit is False
    assert body == {"text": "hello world", "busy": False, "metadata": "text"}
    stream.set_option.assert_called_once_with("language", "Japanese")
    stream.accept_waveform.assert_called_once()
    assert stream.accept_waveform.call_args.args[0] == 22_050
    recognizer.decode_stream.assert_called_once_with(stream)


def test_worker_language_hint_updates_live_without_reconstruction() -> None:
    runtime = QwenWorkerRuntime()
    runtime._recognizer = MagicMock()
    runtime.loaded = True
    body, should_exit = runtime.dispatch(
        _request("set_language_hint", {"language": "German"}),
        b"",
    )
    assert should_exit is False
    assert body == {"updated": True, "loaded": True}
    assert runtime._forced_language == "German"

    runtime.dispatch(_request("set_language_hint", {"language": None}), b"")
    assert runtime._forced_language is None


def test_worker_hotwords_are_explicitly_unsupported() -> None:
    runtime = QwenWorkerRuntime()
    with pytest.raises(SttWorkerCapabilityError, match="does not support"):
        runtime.dispatch(
            _request("update_hotwords", {"hotwords": [], "score": 2.0}),
            b"",
        )


def test_worker_hello_unload_and_shutdown_capabilities() -> None:
    runtime = QwenWorkerRuntime()
    hello, should_exit = runtime.dispatch(
        _request("hello", {"supported_versions": [PROTOCOL_VERSION]}),
        b"",
    )
    assert should_exit is False
    assert hello["engine_id"] == "qwen"
    assert hello["worker_pid"] > 0
    assert hello["capabilities"] == {
        "preview": {"metadata": "text", "nonblocking_admission": True},
        "language_hint": {"dynamic_updates": True},
    }

    runtime._recognizer = MagicMock()
    runtime.loaded = True
    unloaded, should_exit = runtime.dispatch(_request("unload"), b"")
    assert should_exit is False
    assert unloaded == {"state": "idle", "loaded": False}

    shutdown, should_exit = runtime.dispatch(_request("shutdown"), b"")
    assert should_exit is True
    assert shutdown == {"state": "shutdown", "loaded": False}


def test_qwen_sherpa_model_dir_uses_ascii_short_path_on_windows(tmp_path: Path) -> None:
    model_dir = tmp_path / "Example" / "mödel"
    model_dir.mkdir(parents=True)
    short_model_dir = Path(r"C:\USERS\BRAKLI~1\QWEN")
    with (
        patch("whisperstream.stt_worker_qwen.sys.platform", "win32"),
        patch(
            "whisperstream.stt_worker_qwen._windows_short_path",
            return_value=short_model_dir,
        ) as short_path,
    ):
        assert model_dir_for_sherpa(model_dir) == short_model_dir
    short_path.assert_called_once_with(model_dir)
