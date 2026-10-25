from __future__ import annotations

import importlib.util
import os
from pathlib import Path, PureWindowsPath
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

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
    reason="Windows sherpa Parakeet worker runtime is unavailable on this platform",
)

from whisperstream.stt_worker_parakeet import (
    ModelNotPreparedError,
    ParakeetWorkerRuntime,
    model_dir_for_sherpa,
)
from whisperstream.stt_worker_protocol import (
    PROTOCOL_VERSION,
    ValidatedRequest,
    encode_audio_request,
)


def _request(operation: str, body=None) -> ValidatedRequest:
    return ValidatedRequest(
        request_id=f"test-{operation}",
        operation=operation,
        backend="parakeet",
        body=dict(body or {}),
    )


def _load_body(model_dir: Path) -> dict[str, object]:
    return {
        "model_name": "nemo-parakeet-tdt-0.6b-v3",
        "model_dir": str(model_dir),
        "quantization": "int8",
        "threads": 2,
        "hotwords": ["alpha"],
        "hotword_score": 1.5,
        "beam_width": 4,
    }


def test_worker_constructs_native_recognizer_with_safe_paths(tmp_path: Path) -> None:
    for name in (
        "encoder.int8.onnx",
        "decoder.int8.onnx",
        "joiner.int8.onnx",
        "tokens.txt",
    ):
        (tmp_path / name).write_bytes(b"x")
    recognizer = MagicMock()

    with (
        patch(
            "whisperstream.stt_worker_parakeet.model_dir_for_sherpa",
            return_value=PureWindowsPath(r"C:\MODELS\PARAKEET"),
        ) as safe_path,
        patch(
            "whisperstream.stt_worker_parakeet.sherpa_onnx.OfflineRecognizer.from_transducer",
            return_value=recognizer,
        ) as construct,
    ):
        runtime = ParakeetWorkerRuntime()
        body, should_exit = runtime.dispatch(_request("load", _load_body(tmp_path)), b"")

    assert should_exit is False
    assert body == {"state": "ready", "loaded": True}
    safe_path.assert_called_once_with(tmp_path)
    kwargs = construct.call_args.kwargs
    assert kwargs["encoder"] == r"C:\MODELS\PARAKEET\encoder.int8.onnx"
    assert kwargs["decoder"] == r"C:\MODELS\PARAKEET\decoder.int8.onnx"
    assert kwargs["joiner"] == r"C:\MODELS\PARAKEET\joiner.int8.onnx"
    assert kwargs["tokens"] == r"C:\MODELS\PARAKEET\tokens.txt"
    assert kwargs["decoding_method"] == "greedy_search"
    assert kwargs["provider"] == "cpu"


def test_worker_load_failure_occurs_before_native_construction(tmp_path: Path) -> None:
    runtime = ParakeetWorkerRuntime()
    with (
        patch(
            "whisperstream.stt_worker_parakeet.sherpa_onnx.OfflineRecognizer.from_transducer"
        ) as construct,
        pytest.raises(ModelNotPreparedError, match="Model file not found"),
    ):
        runtime.dispatch(_request("load", _load_body(tmp_path)), b"")
    construct.assert_not_called()


@pytest.mark.parametrize("operation", ["preview", "transcribe_chunk", "transcribe"])
def test_worker_serializes_complete_timed_metadata(tmp_path: Path, operation: str) -> None:
    runtime = ParakeetWorkerRuntime()
    recognizer = MagicMock()
    stream = MagicMock()
    stream.result = SimpleNamespace(
        text="  hello world  ",
        tokens=["hello", "world"],
        timestamps=[0.12, 0.48],
        durations=[0.30, 0.24],
        ys_log_probs=[-0.1, -0.2],
    )
    recognizer.create_stream.return_value = stream
    runtime._recognizer = recognizer
    runtime.loaded = True
    audio_body, payload = encode_audio_request(
        np.zeros(1600, dtype=np.float32),
        sample_rate=22_050,
    )

    body, should_exit = runtime.dispatch(_request(operation, audio_body), payload)

    assert should_exit is False
    assert body == {
        "text": "hello world",
        "tokens": ["hello", "world"],
        "timestamps": [0.12, 0.48],
        "durations": [0.30, 0.24],
        "ys_log_probs": [-0.1, -0.2],
        **({"busy": False, "metadata": "timed_tokens"} if operation == "preview" else {}),
    }
    stream.accept_waveform.assert_called_once()
    assert stream.accept_waveform.call_args.args[0] == 22_050
    recognizer.decode_stream.assert_called_once_with(stream)


def test_worker_hotword_update_reconstructs_recognizer() -> None:
    runtime = ParakeetWorkerRuntime()
    runtime._model_dir = Path(r"C:\Models\Parakeet")
    first = MagicMock()
    second = MagicMock()
    with patch.object(runtime, "_construct_recognizer", side_effect=[first, second]) as construct:
        runtime.dispatch(_request("load", _load_body(runtime._model_dir)), b"")
        body, _ = runtime.dispatch(
            _request("update_hotwords", {"hotwords": ["beta"], "score": 2.0}),
            b"",
        )

    assert construct.call_count == 2
    assert runtime._recognizer is second
    assert body == {"updated_count": 1, "state": "ready", "loaded": True}


def test_worker_hello_and_shutdown_lifecycle() -> None:
    runtime = ParakeetWorkerRuntime()
    hello, should_exit = runtime.dispatch(
        _request("hello", {"supported_versions": [PROTOCOL_VERSION]}),
        b"",
    )
    assert should_exit is False
    assert hello["engine_id"] == "parakeet"
    assert hello["worker_pid"] > 0
    assert hello["capabilities"]["preview"]["metadata"] == "timed_tokens"

    shutdown, should_exit = runtime.dispatch(_request("shutdown"), b"")
    assert should_exit is True
    assert shutdown == {"state": "shutdown", "loaded": False}


def test_sherpa_model_dir_uses_ascii_short_path_on_windows(tmp_path: Path) -> None:
    model_dir = tmp_path / "Example" / "mödel"
    model_dir.mkdir(parents=True)
    short_model_dir = Path(r"C:\USERS\BRAKLI~1\MODEL")
    with (
        patch("whisperstream.stt_worker_parakeet.sys.platform", "win32"),
        patch(
            "whisperstream.stt_worker_parakeet._windows_short_path",
            return_value=short_model_dir,
        ) as short_path,
    ):
        assert model_dir_for_sherpa(model_dir) == short_model_dir
    short_path.assert_called_once_with(model_dir)


def test_sherpa_model_dir_rejects_unavailable_ascii_alias(tmp_path: Path) -> None:
    model_dir = tmp_path / "Example" / "mödel"
    model_dir.mkdir(parents=True)
    with (
        patch("whisperstream.stt_worker_parakeet.sys.platform", "win32"),
        patch(
            "whisperstream.stt_worker_parakeet._windows_short_path",
            side_effect=OSError("8.3 names unavailable"),
        ),
        pytest.raises(ModelNotPreparedError, match="ASCII-only folder"),
    ):
        model_dir_for_sherpa(model_dir)
