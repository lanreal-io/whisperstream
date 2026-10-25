"""Native-free parent lifecycle shared by supervised STT engines.

The parent owns chunking, nonblocking preview admission, lazy loading, idle
offload, and lifecycle serialization. Native imports, recognizer construction,
and inference remain exclusively in worker adapters selected by
``stt_worker``.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .speech_gate_client import SpeechGateClient
from .speech_gate_settings import SpeechGateSettings
from .config_manager import get_log_dir
from .logger import log_transcription_event
from .stt_contracts import (
    DetailedTranscriptionResult,
    SttCapabilities,
    SttHealth,
    SttHotwordCapability,
    SttLanguageHintCapability,
    SttLifecycleState,
    SttPreviewCapability,
    normalize_stt_audio,
)
from .stt_errors import (
    ModelNotPreparedError,
    SttFailure,
    SttWorkerProtocolFailure,
    SttWorkerPythonFailure,
)
from .stt_supervisor import (
    NativeSttWorkerCrashed,
    SttWorkerRemoteError,
    SttWorkerSupervisor,
)
from .stt_worker_protocol import SttWorkerProtocolError


def _text_from_body(body: dict[str, Any]) -> str:
    # ============================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # ============================================================================
    text = body.get("text")
    if not isinstance(text, str):
        raise SttWorkerProtocolError("worker transcription reply is missing text")
    return text


def _optional_string_tuple(body: dict[str, Any], name: str) -> tuple[str, ...] | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _optional_float_tuple(body: dict[str, Any], name: str) -> tuple[float, ...] | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or any(isinstance(item, bool) for item in value):
        return None
    try:
        return tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None


def _detailed_result_from_body(
    body: dict[str, Any], *, final_decode: bool = False
) -> DetailedTranscriptionResult:
    # ============================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # ============================================================================
    text = _text_from_body(body)
    tokens = _optional_string_tuple(body, "tokens")
    timestamps = _optional_float_tuple(body, "timestamps")
    if tokens is None or timestamps is None or len(tokens) != len(timestamps):
        return DetailedTranscriptionResult(text=text)
    durations = _optional_float_tuple(body, "durations")
    probabilities = _optional_float_tuple(body, "ys_log_probs")
    if final_decode:
        for name, values in (("durations", durations), ("ys_log_probs", probabilities)):
            if body.get(name) is not None and values is None:
                return DetailedTranscriptionResult(text=text)
        if probabilities is not None and any(not math.isfinite(p) for p in probabilities):
            return DetailedTranscriptionResult(text=text)
    if durations is not None and len(durations) != len(tokens):
        return DetailedTranscriptionResult(text=text)
    if probabilities is not None and len(probabilities) != len(tokens):
        return DetailedTranscriptionResult(text=text)
    return DetailedTranscriptionResult(
        text=text,
        tokens=tokens,
        timestamps=timestamps,
        durations=durations,
        ys_log_probs=probabilities,
    )


def _capabilities_body(capabilities: SttCapabilities) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if capabilities.preview is not None:
        body["preview"] = {
            "metadata": capabilities.preview.metadata.value,
            "nonblocking_admission": capabilities.preview.nonblocking_admission,
        }
    if capabilities.hotwords is not None:
        body["hotwords"] = {
            "update_mode": capabilities.hotwords.update_mode.value,
        }
    if capabilities.language_hint is not None:
        body["language_hint"] = {
            "dynamic_updates": capabilities.language_hint.dynamic_updates,
        }
    return body


class SupervisedSttManager:
    """Shared parent contract for one supervised native recognizer worker."""

    _log_tag = "stt"
    engine_id = "stt"
    capabilities = SttCapabilities()

    def __init__(
        self,
        *,
        model_name: str,
        threads: Optional[int],
        logger: logging.Logger,
        model_dir: Path,
        timeout: float = 300.0,
        _supervisor: SttWorkerSupervisor | None = None,
        _defer_initial_load: bool = False,
    ) -> None:
        self._model_name = model_name
        self._threads = threads
        self._logger = logger
        self._model_dir = Path(model_dir)
        self._timeout = timeout
        self._last_access = time.time()
        self._lock = threading.Lock()
        self._shutdown = False
        self._loaded = False
        self._stop_monitor = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._supervisor = _supervisor or SttWorkerSupervisor(
            backend=self.engine_id,
            model_identifier=self._supervisor_model_identifier(),
            diagnostic_dir=get_log_dir() / "stt-workers",
            logger=self._logger,
        )

        self._speech_gate = SpeechGateClient(
            SpeechGateSettings.from_environment(),
            diagnostic_dir=get_log_dir() / "speech-gate-workers", logger=self._logger,
        )

        if not _defer_initial_load:
            try:
                self._load_model_unlocked(reason="initial")
            except (SttWorkerRemoteError, SttWorkerPythonFailure) as exc:
                self._supervisor.shutdown()
                exception_type = (
                    exc.error.get("exception_type")
                    if isinstance(exc, SttWorkerRemoteError)
                    else exc.exception_type
                )
                message = (
                    str(exc.error.get("message") or "speech model is not prepared")
                    if isinstance(exc, SttWorkerRemoteError)
                    else exc.diagnostic_message
                )
                if exception_type == "ModelNotPreparedError":
                    raise ModelNotPreparedError(
                        message or "speech model is not prepared"
                    ) from exc
                raise
            except BaseException:
                self._supervisor.shutdown()
                raise

        if self._timeout > 0 and not _defer_initial_load:
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name=f"{self._log_tag}-idle-monitor",
            )
            self._monitor_thread.start()

    def for_language(self, language: str):
        return _LanguageBoundRecognizer(self, language)

    def _language_body(self, language: str | None) -> dict[str, Any]:
        return {}

    def _supervisor_model_identifier(self) -> str:
        return self._model_name

    def _load_body(self) -> dict[str, Any]:
        raise NotImplementedError

    def _log_loading(self, *, reason: str) -> None:
        self._logger.info(
            "%s model loading model=%s threads=%s reason=%s",
            self._log_tag,
            self._model_name,
            self._threads or "auto",
            reason,
        )

    def _log_loaded(self, *, elapsed: float) -> None:
        self._logger.info(
            "%s model loaded latency=%.2fs worker_pid=%s",
            self._log_tag,
            elapsed,
            self.worker_pid,
        )

    @property
    def preview_capability(self) -> SttPreviewCapability | None:
        return self if self.capabilities.preview is not None else None

    @property
    def hotword_capability(self) -> SttHotwordCapability | None:
        return None

    @property
    def language_hint_capability(self) -> SttLanguageHintCapability | None:
        return None

    @property
    def worker_pid(self) -> int | None:
        return self._supervisor.pid

    @property
    def recovery_count(self) -> int:
        return self._supervisor.recovery_count

    @property
    def last_failure(self) -> SttFailure | None:
        return self._supervisor.last_failure

    def _validate_worker_capabilities(self) -> None:
        expected = _capabilities_body(self.capabilities)
        actual = self._supervisor.capabilities
        if actual != expected:
            raise SttWorkerProtocolError(
                f"worker capability mismatch backend={self.engine_id}"
            )

    def _load_model_unlocked(self, *, reason: str) -> None:
        if self._shutdown:
            raise RuntimeError("STT engine has been shut down")
        self._log_loading(reason=reason)
        started = time.monotonic()
        try:
            self._supervisor.load(self._load_body())
            self._validate_worker_capabilities()
        except SttWorkerProtocolError as exc:
            raise SttWorkerProtocolFailure(
                backend=self.engine_id,
                model_identifier=self._supervisor_model_identifier(),
                operation="load",
                report=None,
                recovery_attempted=False,
                suppressed=False,
            ) from exc
        self._loaded = True
        self._log_loaded(elapsed=time.monotonic() - started)

    def _monitor_loop(self) -> None:
        while not self._stop_monitor.wait(5):
            with self._lock:
                should_unload = (
                    (self._loaded or self._speech_gate.is_running)
                    and self._timeout > 0
                    and (time.time() - self._last_access > self._timeout)
                )
            if should_unload:
                self._unload_model()

    def _unload_model(self) -> None:
        with self._lock:
            if (self._loaded or self._speech_gate.is_running) and (time.time() - self._last_access > self._timeout):
                self._logger.info("%s model unloading reason=idle_timeout", self._log_tag)
                try:
                    self._supervisor.stop_worker()
                finally:
                    self._loaded = False
                    self._speech_gate.close()

    def ensure_loaded(self) -> None:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("STT engine has been shut down")
            if not self._loaded:
                self._load_model_unlocked(reason="lazy_access")

    def load(self) -> None:
        self.ensure_loaded()

    def unload(self) -> None:
        with self._lock:
            try:
                self._supervisor.stop_worker()
            finally:
                self._loaded = False
                self._speech_gate.close()
        self._logger.info("%s model unloading reason=explicit", self._log_tag)

    def health(self) -> SttHealth:
        with self._lock:
            if self._shutdown:
                return SttHealth(
                    engine_id=self.engine_id,
                    state=SttLifecycleState.SHUTDOWN,
                    loaded=False,
                )
            body = self._supervisor.health()
            self._loaded = bool(body.get("loaded", False))
            try:
                state = SttLifecycleState(str(body.get("state", "unavailable")))
            except ValueError:
                state = SttLifecycleState.UNAVAILABLE
            return SttHealth(
                engine_id=self.engine_id,
                state=state,
                loaded=self._loaded,
            )

    def shutdown(self) -> None:
        self._stop_monitor.set()
        monitor = self._monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=5.0)
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._loaded = False
            try:
                self._supervisor.shutdown()
            finally:
                self._speech_gate.close()

    def update_hotwords(self, hotwords: list[str], score: float) -> None:
        """Compatibility no-op for engines without hotword capability."""

        self._logger.debug(
            "%s hotwords ignored count=%d score=%.1f action=noop "
            "reason=no_hotword_support",
            self._log_tag,
            len(hotwords),
            score,
        )

    def _decode_locked(
        self,
        waveform: np.ndarray,
        *,
        sample_rate: int,
        include_metadata: bool,
        language: str | None = None,
        final_decode: bool = False,
    ) -> DetailedTranscriptionResult:
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        if self._shutdown:
            raise RuntimeError("STT engine has been shut down")
        self._last_access = time.time()
        if not self._speech_gate.has_speech(waveform, sample_rate=sample_rate):
            return DetailedTranscriptionResult(text="")
        if not self._loaded:
            self._load_model_unlocked(reason="lazy_access")
        started = time.monotonic()
        try:
            request_body = self._language_body(language)
            body = self._supervisor.request_audio(
                "preview" if include_metadata and not final_decode else "transcribe_chunk",
                waveform,
                sample_rate=sample_rate,
                **({"body": request_body} if request_body else {}),
            )
        except NativeSttWorkerCrashed:
            self._loaded = False
            raise
        try:
            result = (
                _detailed_result_from_body(body, final_decode=final_decode)
                if include_metadata
                else DetailedTranscriptionResult(text=_text_from_body(body))
            )
            if final_decode and not self.capabilities.final_alignment:
                result = DetailedTranscriptionResult(text=result.text)
            elif final_decode and result.tokens is not None:
                tokens = result.tokens
                times = result.timestamps or ()
                duration = waveform.size / sample_rate
                valid = (
                    all(math.isfinite(t) and 0 <= t < duration for t in times)
                    and all(a <= b for a, b in zip(times, times[1:]))
                    and " ".join("".join(tokens).replace("▁", " ").split())
                    == " ".join(result.text.split())
                )
                if result.durations is not None:
                    valid = valid and all(
                        math.isfinite(d) and d >= 0 and t + d <= duration
                        for t, d in zip(times, result.durations)
                    )
                if not valid:
                    result = DetailedTranscriptionResult(text=result.text)
        except SttWorkerProtocolError as exc:
            self._loaded = False
            raise SttWorkerProtocolFailure(
                backend=self.engine_id,
                model_identifier=self._supervisor_model_identifier(),
                operation="preview" if include_metadata and not final_decode else "transcribe_chunk",
                report=None,
                recovery_attempted=False,
                suppressed=False,
            ) from exc
        log_transcription_event(
            self._logger,
            "inference",
            duration_s=time.monotonic() - started,
            char_count=len(result.text),
            word_count=len(result.text.split()) if result.text else 0,
            level=logging.DEBUG,
        )
        return result

    def transcribe_chunk(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = 16_000,
        language: str | None = None,
    ) -> str:
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        waveform = normalize_stt_audio(audio, sample_rate=sample_rate)
        if waveform.size == 0:
            return ""
        with self._lock:
            return self._decode_locked(
                waveform,
                sample_rate=sample_rate,
                include_metadata=False,
                language=language,
            ).text

    def transcribe_chunk_detailed(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = 16_000,
        language: str | None = None,
    ) -> DetailedTranscriptionResult:
        """Run a fresh final decode, retaining only supplied native metadata."""
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        waveform = normalize_stt_audio(audio, sample_rate=sample_rate)
        if waveform.size == 0:
            return DetailedTranscriptionResult(text="")
        with self._lock:
            return self._decode_locked(
                waveform,
                sample_rate=sample_rate,
                include_metadata=True,
                language=language,
                final_decode=True,
            )

    def try_transcribe_preview(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = 16_000,
        language: str | None = None,
    ) -> DetailedTranscriptionResult | None:
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        waveform = normalize_stt_audio(audio, sample_rate=sample_rate)
        if waveform.size == 0:
            return DetailedTranscriptionResult(text="")
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._decode_locked(
                waveform,
                sample_rate=sample_rate,
                include_metadata=True,
                language=language,
            )
        finally:
            self._lock.release()

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = 16_000,
        language: Optional[str] = None,
    ) -> str:
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        from .chunking import transcribe_waveform

        waveform = normalize_stt_audio(audio, sample_rate=sample_rate)
        return transcribe_waveform(self.for_language(language) if language is not None else self, waveform, sample_rate)


__all__ = ["SupervisedSttManager"]


class _LanguageBoundRecognizer:
    def __init__(self, manager, language):
        self._manager = manager
        self._language = language

    @property
    def preview_capability(self):
        return self if self._manager.preview_capability is not None else None

    def __getattr__(self, name):
        return getattr(self._manager, name)

    def transcribe_chunk(self, audio, *, sample_rate=16000):
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        return self._manager.transcribe_chunk(audio, sample_rate=sample_rate, language=self._language)

    def transcribe_chunk_detailed(self, audio, *, sample_rate=16000):
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        return self._manager.transcribe_chunk_detailed(
            audio, sample_rate=sample_rate, language=self._language
        )

    def try_transcribe_preview(self, audio, *, sample_rate=16000):
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        return self._manager.try_transcribe_preview(audio, sample_rate=sample_rate, language=self._language)

    def transcribe(self, audio, *, sample_rate=16000):
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        return self._manager.transcribe(audio, sample_rate=sample_rate, language=self._language)
