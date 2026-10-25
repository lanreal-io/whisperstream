"""Sherpa-backed Qwen runtime owned exclusively by the STT worker."""

from __future__ import annotations

import ctypes
import gc
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

# Native DLL discovery and sherpa import are deliberately child-local. Keep
# these imports at module scope so Nuitka includes the native extensions.
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

from .qwen_models import get_qwen_model
from .stt_errors import ModelNotPreparedError
from .stt_worker_protocol import (
    PROTOCOL_VERSION,
    SttWorkerCapabilityError,
    SttWorkerProtocolMismatch,
    SttWorkerValidationError,
    ValidatedRequest,
    decode_audio_request,
)


QWEN_MAX_NEW_TOKENS = 512


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
    """Return a native-loader-safe Qwen model directory inside the worker."""

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


_WARMUP_SAMPLES = 16_000


class QwenWorkerRuntime:
    """Own the active Qwen recognizer and every native inference call."""

    engine_id = "qwen"
    capabilities = {
        "preview": {
            "metadata": "text",
            "nonblocking_admission": True,
        },
        "language_hint": {"dynamic_updates": True},
    }

    def __init__(self) -> None:
        self.loaded = False
        self.shutdown_requested = False
        self._recognizer: Any | None = None
        self._model_name = ""
        self._model_dir: Path | None = None
        self._threads: int | None = None
        self._forced_language: str | None = None

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
            self._warm_recognizer()
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
                raise RuntimeError("Qwen STT runtime is not loaded")
            override = request.body.get("language_override")
            if override is not None and not isinstance(override, str):
                raise SttWorkerValidationError("language override must be a string")
            text = self._decode(waveform, sample_rate=sample_rate, language_override=override)
            if operation == "preview":
                return {
                    "text": text,
                    "busy": False,
                    "metadata": "text",
                }, False
            return {"text": text}, False
        if operation == "set_language_hint":
            language = request.body.get("language")
            if language is not None and not isinstance(language, str):
                raise SttWorkerValidationError(
                    "language hint must be a string or null"
                )
            self._forced_language = language
            return {"updated": True, "loaded": self.loaded}, False
        if operation == "update_hotwords":
            raise SttWorkerCapabilityError("Qwen does not support hotword updates")
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
        threads = body.get("threads")
        forced_language = body.get("forced_language")
        max_new_tokens = body.get("max_new_tokens")
        if not isinstance(model_name, str) or not model_name:
            raise SttWorkerValidationError("model_name must be a non-empty string")
        if not isinstance(model_dir, str) or not model_dir:
            raise SttWorkerValidationError("model_dir must be a non-empty string")
        if threads is not None and (
            isinstance(threads, bool) or not isinstance(threads, int) or threads <= 0
        ):
            raise SttWorkerValidationError("threads must be a positive integer or null")
        if forced_language is not None and not isinstance(forced_language, str):
            raise SttWorkerValidationError(
                "forced_language must be a string or null"
            )
        if max_new_tokens != QWEN_MAX_NEW_TOKENS:
            raise SttWorkerValidationError(
                f"max_new_tokens must equal {QWEN_MAX_NEW_TOKENS}"
            )
        if get_qwen_model(model_name) is None:
            raise ModelNotPreparedError(
                f"Unknown Qwen model id {model_name!r}: not in qwen_models catalog"
            )
        self._model_name = model_name
        self._model_dir = Path(model_dir)
        self._threads = threads
        self._forced_language = forced_language

    def _construct_recognizer(self) -> Any:
        if self._model_dir is None:
            raise RuntimeError("Qwen model configuration is unavailable")
        entry = get_qwen_model(self._model_name)
        if entry is None:
            raise ModelNotPreparedError(
                f"Unknown Qwen model id {self._model_name!r}: not in qwen_models catalog"
            )
        for relative in entry["files"]:
            path = self._model_dir / relative
            if not path.exists():
                raise ModelNotPreparedError(
                    f"Qwen model file not found: {path} - stage the Qwen model first"
                )
        safe_dir = model_dir_for_sherpa(self._model_dir)
        return sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=str(safe_dir / "conv_frontend.onnx"),
            encoder=str(safe_dir / "encoder.int8.onnx"),
            decoder=str(safe_dir / "decoder.int8.onnx"),
            tokenizer=str(safe_dir / "tokenizer"),
            num_threads=self._threads or 4,
            provider="cpu",
            max_new_tokens=QWEN_MAX_NEW_TOKENS,
        )

    def _warm_recognizer(self) -> None:
        """Run one silent decode so the first dictation is not a cold pass.

        Cold first decodes took about 3 s on Mac, long enough that a short
        recording got no Live Preview at all. Warm-up is best effort.
        """
        try:
            self._decode(np.zeros(_WARMUP_SAMPLES, dtype=np.float32), sample_rate=16_000)
        except Exception:  # noqa: BLE001 - a real decode reports real failures
            pass

    def _decode(self, waveform: np.ndarray, *, sample_rate: int, language_override: str | None = None) -> str:
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        recognizer = self._recognizer
        if recognizer is None:
            raise RuntimeError("Qwen STT runtime is not loaded")
        stream = recognizer.create_stream()
        hint = self._forced_language if language_override is None else language_override
        if hint or language_override is not None:
            stream.set_option("language", hint)
        stream.accept_waveform(sample_rate, waveform)
        recognizer.decode_stream(stream)
        return stream.result.text.strip()


__all__ = [
    "ModelNotPreparedError",
    "QWEN_MAX_NEW_TOKENS",
    "QwenWorkerRuntime",
    "model_dir_for_sherpa",
]
