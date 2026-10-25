"""Cohere native runtime, imported only by the supervised worker."""
from pathlib import Path

from .cohere_manager import COHERE_DEFAULT_THREADS, validated_cohere_language
from .model_downloader import COHERE_TRANSCRIBE_FILES
from .stt_errors import ModelNotPreparedError
from .stt_worker_protocol import SttWorkerValidationError
from .stt_worker_qwen import QwenWorkerRuntime, model_dir_for_sherpa

# Qwen worker initialization establishes native DLL discovery on Windows first.
import sherpa_onnx


class CohereWorkerRuntime(QwenWorkerRuntime):
    engine_id = "cohere"

    def _apply_load_body(self, body):
        if body.get("model_name") != "cohere-transcribe-03-2026-int8":
            raise SttWorkerValidationError("Unknown Cohere model")
        model_dir = body.get("model_dir")
        if not isinstance(model_dir, str) or not model_dir:
            raise SttWorkerValidationError("model_dir must be a non-empty string")
        threads = body.get("threads")
        if threads is not None and (isinstance(threads, bool) or not isinstance(threads, int) or threads <= 0):
            raise SttWorkerValidationError("threads must be a positive integer or null")
        self._model_name = body["model_name"]
        self._model_dir = Path(model_dir)
        self._threads = threads
        self._forced_language = validated_cohere_language(body.get("forced_language"))

    def dispatch(self, request, payload):
        if "language_override" in request.body:
            validated_cohere_language(request.body["language_override"])
        if request.operation == "set_language_hint":
            validated_cohere_language(request.body.get("language"))
        return super().dispatch(request, payload)

    def _construct_recognizer(self):
        missing = [
            self._model_dir / relative
            for relative in COHERE_TRANSCRIBE_FILES
            if not (self._model_dir / relative).is_file()
        ]
        if missing:
            raise ModelNotPreparedError(f"Cohere model file not found: {missing[0]}")

        sherpa_model_dir = model_dir_for_sherpa(self._model_dir)
        num_threads = self._threads or COHERE_DEFAULT_THREADS
        recognizer = sherpa_onnx.OfflineRecognizer.from_cohere_transcribe(
            encoder=str(sherpa_model_dir / "encoder.int8.onnx"),
            decoder=str(sherpa_model_dir / "decoder.int8.onnx"),
            tokens=str(sherpa_model_dir / "tokens.txt"),
            num_threads=num_threads,
            language=self._forced_language,
            use_punct=True,
            use_itn=True,
            decoding_method="greedy_search",
            provider="cpu",
        )
        return recognizer
