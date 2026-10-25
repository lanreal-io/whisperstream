"""Sherpa-backed Parakeet runtime owned exclusively by the STT worker.

Importing this module initializes the Windows ONNX Runtime DLL search path and
imports sherpa-onnx.  Long-lived backend and parent-side modules must never
import it.  Worker replies may carry transcript-class data over the private
pipe; none of that data may be logged.
"""

from __future__ import annotations

import ctypes
import gc
import importlib.util
import math
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

# sherpa-onnx >= 1.12.21 on Windows does not bundle onnxruntime.dll, and
# Windows 11 has a stale system copy in System32. Prime the worker's DLL loader
# before importing sherpa. Native initialization belongs in this process.
if sys.platform == "win32":
    _spec = importlib.util.find_spec("onnxruntime")
    if _spec and _spec.origin:
        _ort_dir = str(Path(_spec.origin).parent)
        os.add_dll_directory(_ort_dir)
        os.environ["PATH"] = _ort_dir + os.pathsep + os.environ.get("PATH", "")
        _capi_dir = str(Path(_spec.origin).parent / "capi")
        if Path(_capi_dir).exists():
            os.add_dll_directory(_capi_dir)
            os.environ["PATH"] = _capi_dir + os.pathsep + os.environ["PATH"]

import sherpa_onnx  # noqa: E402 - must follow worker-local DLL setup

from .stt_errors import ModelNotPreparedError
from .stt_worker_protocol import (
    PROTOCOL_VERSION,
    SttWorkerCapabilityError,
    SttWorkerProtocolMismatch,
    SttWorkerValidationError,
    ValidatedRequest,
    decode_audio_request,
)


_MISSING_METADATA = object()


def _read_metadata(result: Any, name: str) -> Any:
    try:
        return getattr(result, name)
    except Exception:  # noqa: BLE001 - malformed native metadata fails safe
        return _MISSING_METADATA


def _metadata_items(value: Any) -> tuple[Any, ...] | None:
    try:
        if isinstance(value, np.ndarray):
            if value.ndim != 1:
                return None
            return tuple(value.tolist())
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return tuple(value)
    except Exception:  # noqa: BLE001 - malformed native metadata fails safe
        return None
    return None


def _string_metadata(value: Any) -> tuple[str, ...] | None:
    items = _metadata_items(value)
    if items is None or any(not isinstance(item, str) for item in items):
        return None
    return items


def _float_metadata(value: Any, *, nonnegative: bool) -> tuple[float, ...] | None:
    items = _metadata_items(value)
    if items is None:
        return None
    converted: list[float] = []
    for item in items:
        if isinstance(item, (bool, np.bool_)):
            return None
        try:
            number = float(item)
        except Exception:  # noqa: BLE001 - malformed native metadata fails safe
            return None
        if not math.isfinite(number) or (nonnegative and number < 0):
            return None
        converted.append(number)
    return tuple(converted)


def _detailed_result_body(result: Any, text: str) -> dict[str, Any]:
    # ============================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # ============================================================================
    body: dict[str, Any] = {"text": text}
    raw_tokens = _read_metadata(result, "tokens")
    raw_timestamps = _read_metadata(result, "timestamps")
    if raw_tokens is _MISSING_METADATA or raw_timestamps is _MISSING_METADATA:
        return body
    tokens = _string_metadata(raw_tokens)
    timestamps = _float_metadata(raw_timestamps, nonnegative=True)
    if tokens is None or timestamps is None or len(tokens) != len(timestamps):
        return body
    if text and not tokens:
        return body

    optional_values: dict[str, tuple[float, ...] | None] = {}
    for name, nonnegative in (("durations", True), ("ys_log_probs", False)):
        raw_value = _read_metadata(result, name)
        if raw_value is _MISSING_METADATA or raw_value is None:
            optional_values[name] = None
            continue
        values = _float_metadata(raw_value, nonnegative=nonnegative)
        if values is None or len(values) != len(tokens):
            return body
        optional_values[name] = values

    body["tokens"] = list(tokens)
    body["timestamps"] = list(timestamps)
    if optional_values["durations"] is not None:
        body["durations"] = list(optional_values["durations"] or ())
    if optional_values["ys_log_probs"] is not None:
        body["ys_log_probs"] = list(optional_values["ys_log_probs"] or ())
    return body


def _windows_short_path(path: Path) -> Path:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short_path_name = kernel32.GetShortPathNameW
    get_short_path_name.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    get_short_path_name.restype = ctypes.c_uint32

    size = 260
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        result = get_short_path_name(str(path), buffer, size)
        if result == 0:
            error = ctypes.get_last_error()
            raise OSError(
                error,
                ctypes.FormatError(error) if error else "GetShortPathNameW failed",
                str(path),
            )
        if result < size:
            return Path(buffer.value)
        size = result + 1


def model_dir_for_sherpa(model_dir: Path) -> Path:
    """Return a native-loader-safe model directory inside the worker."""

    if sys.platform != "win32" or str(model_dir).isascii():
        return model_dir
    if not model_dir.exists():
        raise ModelNotPreparedError(f"Speech model directory not found: {model_dir}")
    try:
        short_path = _windows_short_path(model_dir)
    except OSError as exc:
        raise ModelNotPreparedError(
            "Whisperstream cannot load this speech model because its Windows "
            "path contains special characters and Windows could not create a "
            f"compatible short path: {model_dir}. Move model storage on the "
            "Home page to an ASCII-only folder such as C:\\WhisperstreamModels, "
            "then try again."
        ) from exc
    if not str(short_path).isascii():
        raise ModelNotPreparedError(
            "Whisperstream cannot load this speech model because its Windows "
            "path contains special characters and Windows did not return an "
            f"ASCII-compatible short path: {model_dir}. Move model storage on "
            "the Home page to an ASCII-only folder such as "
            "C:\\WhisperstreamModels, then try again."
        )
    return short_path


class ParakeetWorkerRuntime:
    """Own the active Parakeet recognizer and every native inference call."""

    engine_id = "parakeet"
    capabilities = {
        "preview": {
            "metadata": "timed_tokens",
            "nonblocking_admission": True,
        },
        "hotwords": {"update_mode": "reload"},
    }

    def __init__(self) -> None:
        self.loaded = False
        self.shutdown_requested = False
        self._recognizer: Any | None = None
        self._model_name = ""
        self._model_dir: Path | None = None
        self._quantization = "int8"
        self._threads: int | None = None
        self._hotwords: list[str] = []
        self._hotwords_score = 1.5
        self._beam_width = 4

    def dispatch(
        self,
        request: ValidatedRequest,
        payload: bytes,
    ) -> tuple[dict[str, Any], bool]:
        if request.backend != self.engine_id:
            raise SttWorkerValidationError("worker backend is unavailable")
        operation = request.operation
        if operation == "hello":
            versions = request.body.get("supported_versions")
            if not isinstance(versions, list) or PROTOCOL_VERSION not in versions:
                raise SttWorkerProtocolMismatch("no mutually supported protocol version")
            return (
                {
                    "selected_version": PROTOCOL_VERSION,
                    "worker_pid": os.getpid(),
                    "engine_id": self.engine_id,
                    "capabilities": self.capabilities,
                    "limits": {"audio_dtype": "float32-le", "audio_rank": 1},
                },
                False,
            )
        if operation == "load":
            self._apply_load_body(request.body)
            self._recognizer = self._construct_recognizer()
            self.loaded = True
            return {"state": "ready", "loaded": True}, False
        if operation == "unload":
            self._recognizer = None
            self.loaded = False
            gc.collect()
            return {"state": "idle", "loaded": False}, False
        if operation == "health":
            return {
                "state": "ready" if self.loaded else "idle",
                "loaded": self.loaded,
                "engine_id": self.engine_id,
            }, False
        if operation in {"transcribe", "transcribe_chunk", "preview"}:
            waveform, sample_rate = decode_audio_request(request, payload)
            if self._recognizer is None:
                raise RuntimeError("Parakeet STT runtime is not loaded")
            raw_result, text = self._decode(waveform, sample_rate=sample_rate)
            if operation == "preview":
                body = _detailed_result_body(raw_result, text)
                body.update({"busy": False, "metadata": "timed_tokens"})
                return body, False
            return _detailed_result_body(raw_result, text), False
        if operation == "update_hotwords":
            hotwords = request.body.get("hotwords")
            score = request.body.get("score")
            if not isinstance(hotwords, list) or not all(
                isinstance(item, str) for item in hotwords
            ):
                raise SttWorkerValidationError("hotwords must be a string array")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise SttWorkerValidationError("hotword score must be numeric")
            self._hotwords = list(hotwords)
            self._hotwords_score = float(score)
            self._recognizer = self._construct_recognizer()
            self.loaded = True
            return {
                "updated_count": len(hotwords),
                "state": "ready",
                "loaded": True,
            }, False
        if operation == "set_language_hint":
            raise SttWorkerCapabilityError(
                "Parakeet does not support dynamic language hints"
            )
        if operation == "shutdown":
            self._recognizer = None
            self.loaded = False
            self.shutdown_requested = True
            gc.collect()
            return {"state": "shutdown", "loaded": False}, True
        raise SttWorkerValidationError("unsupported worker operation")

    def _apply_load_body(self, body: dict[str, Any]) -> None:
        model_name = body.get("model_name")
        model_dir = body.get("model_dir")
        quantization = body.get("quantization")
        threads = body.get("threads")
        hotwords = body.get("hotwords", [])
        hotword_score = body.get("hotword_score", 1.5)
        beam_width = body.get("beam_width", 4)
        if not isinstance(model_name, str) or not model_name:
            raise SttWorkerValidationError("model_name must be a non-empty string")
        if not isinstance(model_dir, str) or not model_dir:
            raise SttWorkerValidationError("model_dir must be a non-empty string")
        if not isinstance(quantization, str) or not quantization:
            raise SttWorkerValidationError("quantization must be a non-empty string")
        if threads is not None and (
            isinstance(threads, bool) or not isinstance(threads, int) or threads <= 0
        ):
            raise SttWorkerValidationError("threads must be a positive integer or null")
        if not isinstance(hotwords, list) or not all(
            isinstance(item, str) for item in hotwords
        ):
            raise SttWorkerValidationError("hotwords must be a string array")
        if isinstance(hotword_score, bool) or not isinstance(
            hotword_score, (int, float)
        ):
            raise SttWorkerValidationError("hotword_score must be numeric")
        if isinstance(beam_width, bool) or not isinstance(beam_width, int):
            raise SttWorkerValidationError("beam_width must be an integer")

        self._model_name = model_name
        self._model_dir = Path(model_dir)
        self._quantization = quantization
        self._threads = threads
        self._hotwords = list(hotwords)
        self._hotwords_score = float(hotword_score)
        self._beam_width = beam_width

    def _construct_recognizer(self) -> Any:
        if self._model_dir is None:
            raise RuntimeError("Parakeet model configuration is unavailable")
        q = self._quantization
        encoder = self._model_dir / f"encoder.{q}.onnx"
        decoder = self._model_dir / f"decoder.{q}.onnx"
        joiner = self._model_dir / f"joiner.{q}.onnx"
        tokens = self._model_dir / "tokens.txt"
        for path in (encoder, decoder, joiner, tokens):
            if not path.exists():
                raise ModelNotPreparedError(
                    f"Model file not found: {path} - run: uv run whisperstream-setup"
                )

        sherpa_model_dir = model_dir_for_sherpa(self._model_dir)
        started = time.monotonic()
        recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(sherpa_model_dir / encoder.name),
            decoder=str(sherpa_model_dir / decoder.name),
            joiner=str(sherpa_model_dir / joiner.name),
            tokens=str(sherpa_model_dir / tokens.name),
            num_threads=self._threads or 4,
            sample_rate=16_000,
            feature_dim=80,
            decoding_method="greedy_search",
            provider="cpu",
            model_type="nemo_transducer",
        )
        _ = time.monotonic() - started
        return recognizer

    def _decode(self, waveform: np.ndarray, *, sample_rate: int) -> tuple[Any, str]:
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        recognizer = self._recognizer
        if recognizer is None:
            raise RuntimeError("Parakeet STT runtime is not loaded")
        stream = recognizer.create_stream()
        stream.accept_waveform(sample_rate, waveform)
        recognizer.decode_stream(stream)
        raw_result = stream.result
        return raw_result, raw_result.text.strip()


__all__ = [
    "ModelNotPreparedError",
    "ParakeetWorkerRuntime",
    "model_dir_for_sherpa",
]
