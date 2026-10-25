"""Darwin owners for Agent Speak synthesis and current-output playback.

The shared controller owns logical generations only. This module keeps live
Core Audio/PortAudio output handles private, resolves the current output once
per utterance, and never retries frames after a write may have reached a device.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import numpy as np
import sounddevice as sd

from .speech.engine_specs import KOKORO_SPEC, SUPERTONIC_SPEC, get_engine_spec
from .speech.selection import EngineManager, SelectionPolicy
from .tts_playback_contract import (
    SpeechBuffer,
    SpeechEngineChoice,
    SpeechFailure,
    SpeechPlaybackAdapter,
    SpeechReceipt,
    SpeechRequest,
    SpeechStage,
    SpeechSynthesisAdapter,
    SpeechTerminalReason,
)

SUPERTONIC_RUNTIME_UNAVAILABLE_REASON = "runtime_unavailable"


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [part.strip() for part in parts if part.strip()]


class MacOSSpeechSynthesisAdapter:
    """Own the existing lazy, RAM-aware engine policy on Apple Silicon.

    Model selection and leases are platform-neutral. Mirroring that owner keeps
    the exact ten Supertonic and nine Kokoro presets, automatic thresholds, and
    pre-stream fallback policy identical while playback remains Darwin-owned.
    """

    def __init__(self, *, engine_manager: object, logger: object | None = None) -> None:
        if not isinstance(engine_manager, EngineManager):
            required = ("acquire", "engine", "shutdown")
            if not all(callable(getattr(engine_manager, name, None)) for name in required):
                raise TypeError("engine_manager does not implement the engine lifecycle")
        self._manager = engine_manager
        self._logger = logger or logging.getLogger(__name__)
        self._selection = SelectionPolicy(engine_manager)  # type: ignore[arg-type]
        self._closed = False
        # Model files and an executable reviewed runtime are separate facts.
        # The Mac milestone explicitly retains Kokoro-only TTS. The shared
        # sherpa dependency is now present for STT; that does not expand the
        # approved speech scope or establish native Supertonic playback proof.
        runtime_gate = getattr(self._manager, "mark_runtime_unavailable", None)
        if callable(runtime_gate):
            runtime_gate(
                SUPERTONIC_SPEC.key,
                SUPERTONIC_RUNTIME_UNAVAILABLE_REASON,
            )
        else:
            self._manager.mark_unhealthy(  # type: ignore[attr-defined]
                SUPERTONIC_SPEC.key,
                SUPERTONIC_RUNTIME_UNAVAILABLE_REASON,
            )

    @property
    def engine_manager(self) -> object:
        return self._manager

    @property
    def selection_policy(self) -> SelectionPolicy:
        return self._selection

    def engine(self, engine_key: str) -> object:
        return self._manager.engine(engine_key)  # type: ignore[attr-defined]

    def acquire(self, engine_key: str) -> object:
        if self._closed:
            raise RuntimeError("speech synthesis adapter is closed")
        return self._manager.acquire(engine_key)  # type: ignore[attr-defined]

    def mark_unhealthy(self, engine_key: str, reason: str) -> None:
        self._manager.mark_unhealthy(engine_key, reason)  # type: ignore[attr-defined]

    def mark_healthy(self, engine_key: str) -> None:
        self._manager.mark_healthy(engine_key)  # type: ignore[attr-defined]

    def health_reason(self, engine_key: str) -> str | None:
        return self._manager.health_reason(engine_key)  # type: ignore[attr-defined]

    @staticmethod
    def generate(
        engine: object,
        text: str,
        *,
        voice: object,
        speed: float,
        callback: Callable[..., int] | None,
    ) -> np.ndarray:
        return np.asarray(
            engine.generate(  # type: ignore[attr-defined]
                text,
                sid=voice,
                speed=speed,
                callback=callback,
            ),
            dtype=np.float32,
        ).reshape(-1)

    @staticmethod
    def sample_rate(engine: object) -> int:
        sample_rate = int(engine.sample_rate)  # type: ignore[attr-defined]
        if sample_rate < 1:
            raise ValueError("invalid speech sample rate")
        return sample_rate

    @staticmethod
    def _engine_key(request: SpeechRequest) -> str:
        if request.engine is SpeechEngineChoice.KOKORO:
            return KOKORO_SPEC.key
        if request.engine is SpeechEngineChoice.SUPERTONIC:
            return SUPERTONIC_SPEC.key
        raise ValueError("automatic requests require a resolved engine")

    @staticmethod
    def _voice(engine_key: str, token: str) -> object:
        value: object = token
        if engine_key == SUPERTONIC_SPEC.key:
            try:
                value = int(token)
            except ValueError:
                pass
        normalized = get_engine_spec(engine_key).normalize_voice_id(value)
        if str(normalized) != token:
            raise ValueError("voice token is outside the approved preset scope")
        return normalized

    def _resolved_engine_and_voice(self, request: SpeechRequest) -> tuple[str, object]:
        if request.engine is not SpeechEngineChoice.AUTOMATIC:
            key = self._engine_key(request)
            if key == SUPERTONIC_SPEC.key and not self._manager.is_available(  # type: ignore[attr-defined]
                key
            ):
                raise RuntimeError("reviewed native Supertonic runtime is unavailable")
            return key, self._voice(key, request.voice_token)
        try:
            supertonic_voice: object = int(request.voice_token)
        except ValueError:
            supertonic_voice = SUPERTONIC_SPEC.default_voice_id
        resolution = self._selection.resolve(
            "auto",
            supertonic_voice=supertonic_voice,
            kokoro_voice=request.voice_token,
        )
        if not resolution.candidates:
            raise RuntimeError("no automatic speech engine is available")
        candidate = resolution.candidates[0]
        return candidate.engine_key, candidate.voice_id

    def synthesize(self, request: SpeechRequest) -> Iterator[SpeechBuffer]:
        engine_key, voice = self._resolved_engine_and_voice(request)
        lease = self.acquire(engine_key)
        try:
            engine = lease.engine  # type: ignore[attr-defined]
            engine.load()  # type: ignore[attr-defined]
            sample_rate = self.sample_rate(engine)
            sentences = _split_sentences(request.text)
            for ordinal, sentence in enumerate(sentences):
                frames = self.generate(
                    engine,
                    sentence,
                    voice=voice,
                    speed=request.speed,
                    callback=None,
                )
                if frames.size == 0 or not np.all(np.isfinite(frames)):
                    raise ValueError("speech engine returned invalid audio")
                if request.volume != 1.0:
                    frames = frames * np.float32(request.volume)
                yield SpeechBuffer(
                    request.generation,
                    ordinal,
                    sample_rate,
                    1,
                    frames.astype("<f4", copy=False).tobytes(),
                    final=ordinal == len(sentences) - 1,
                )
        finally:
            lease.release()  # type: ignore[attr-defined]

    def shutdown(self, timeout: float = 2.0) -> bool:
        self._closed = True
        return bool(
            self._manager.shutdown(timeout=max(0.0, float(timeout)))  # type: ignore[attr-defined]
        )

    def close(self) -> None:
        self.shutdown(2.0)


class _OwnedOutputStream:
    """Close one native stream exactly once, exclusively from its worker."""

    def __init__(self, native_stream: object) -> None:
        self._native_stream = native_stream
        self._lock = threading.Lock()
        self._closed = False

    def write(self, frames: np.ndarray) -> None:
        self._native_stream.write(frames)  # type: ignore[attr-defined]

    def abort(self) -> None:
        try:
            self._native_stream.abort()  # type: ignore[attr-defined]
        except Exception:
            pass

    def close_once(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._native_stream.stop()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self._native_stream.close()  # type: ignore[attr-defined]
        except Exception:
            pass


class MacOSSpeechPlaybackAdapter:
    """Own current-output resolution and newest-wins Darwin stream teardown."""

    def __init__(
        self,
        *,
        logger: object | None = None,
        sounddevice_module: object = sd,
        current_output_resolver: Callable[[], int | None] | None = None,
        abort_join_timeout: float = 2.0,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._sd = sounddevice_module
        self._current_output_resolver = (
            current_output_resolver or self._resolve_current_output
        )
        self._abort_join_timeout = max(0.0, float(abort_join_timeout))
        self._lock = threading.RLock()
        self._active: dict[int, _OwnedOutputStream] = {}
        self._interruptions: dict[
            int, tuple[SpeechTerminalReason, SpeechFailure | None]
        ] = {}
        self._abort_workers: dict[int, threading.Thread] = {}
        self._finish_pending: set[int] = set()
        self._closed = False

    def _resolve_current_output(self) -> int | None:
        """Resolve PortAudio's live Core Audio default without exposing identity."""

        try:
            default_pair = self._sd.default.device  # type: ignore[attr-defined]
            output = default_pair[1]
            device = int(output)
            if device < 0:
                return None
            info = self._sd.query_devices(device)  # type: ignore[attr-defined]
            if int(info.get("max_output_channels", 0)) < 1:
                return None
            return device
        except Exception:
            return None

    @staticmethod
    def abort_stream(stream: object | None) -> None:
        if stream is None:
            return
        try:
            stream.abort()  # type: ignore[attr-defined]
        except Exception:
            pass

    @staticmethod
    def _close_stream(stream: object | None) -> None:
        if stream is None:
            return
        if isinstance(stream, _OwnedOutputStream):
            stream.close_once()
            return
        try:
            stream.stop()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            stream.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    def discard_failed_stream(self, stream: object | None) -> None:
        self.abort_stream(stream)
        self._close_stream(stream)

    def open_output_stream(self, sample_rate: int) -> _OwnedOutputStream:
        """Resolve and open the current output once for this utterance."""

        with self._lock:
            if self._closed:
                raise RuntimeError("speech playback adapter is closed")
        if sample_rate < 1:
            raise ValueError("invalid speech sample rate")
        device = self._current_output_resolver()
        if device is None:
            raise RuntimeError("current Core Audio output is unavailable")
        native_stream = None
        try:
            native_stream = self._sd.OutputStream(  # type: ignore[attr-defined]
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                device=device,
            )
            native_stream.start()
            return _OwnedOutputStream(native_stream)
        except Exception:
            if native_stream is not None:
                try:
                    native_stream.abort()
                except Exception:
                    pass
                try:
                    native_stream.close()
                except Exception:
                    pass
            raise

    def claim_stream(self, generation: int, stream: object) -> bool:
        if not isinstance(stream, _OwnedOutputStream):
            raise TypeError("Darwin playback requires an owned output stream")
        with self._lock:
            if self._closed or generation in self._interruptions:
                return False
            self._active[generation] = stream
            return True

    def _wait_for_abort(self, generation: int) -> bool:
        with self._lock:
            worker = self._abort_workers.get(generation)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self._abort_join_timeout)
        with self._lock:
            if (
                self._abort_workers.get(generation) is worker
                and (worker is None or not worker.is_alive())
            ):
                self._abort_workers.pop(generation, None)
                return True
            return worker is None

    def release_stream(self, generation: int, stream: object) -> None:
        # Retire the claim before native teardown. An interrupter either owns
        # an abort already, or can no longer obtain this handle. Only this
        # generation worker may stop/close it, after an owned abort completes.
        with self._lock:
            if self._active.get(generation) is stream:
                self._active.pop(generation, None)
            abort_worker = self._abort_workers.get(generation)
        if abort_worker is not None and abort_worker is not threading.current_thread():
            # Waiting here is safe: stop()/interrupt() never wait, and controller
            # shutdown bounds its engine-lease wait independently. A timeout
            # cannot authorize close while native abort is still using a handle.
            abort_worker.join()
        self._close_stream(stream)

    def finish_generation(self, generation: int) -> None:
        aborted = self._wait_for_abort(generation)
        with self._lock:
            if aborted:
                self._interruptions.pop(generation, None)
                self._finish_pending.discard(generation)
            else:
                self._finish_pending.add(generation)

    def _interruption_for(
        self,
        generation: int,
    ) -> tuple[SpeechTerminalReason, SpeechFailure | None] | None:
        with self._lock:
            return self._interruptions.get(generation) or (
                (SpeechTerminalReason.SHUTDOWN, None) if self._closed else None
            )

    def play(
        self,
        request: SpeechRequest,
        buffers: Iterable[SpeechBuffer],
    ) -> SpeechReceipt:
        try:
            prepared = tuple(buffers)
            if not prepared:
                raise ValueError("speech produced no buffers")
            sample_rate = prepared[0].sample_rate_hz
            for expected_ordinal, buffer in enumerate(prepared):
                if (
                    buffer.generation != request.generation
                    or buffer.ordinal != expected_ordinal
                    or buffer.channels != 1
                    or buffer.sample_rate_hz != sample_rate
                    or buffer.final != (expected_ordinal == len(prepared) - 1)
                ):
                    raise ValueError("speech buffer does not match the request")
        except (TypeError, ValueError):
            return SpeechReceipt(
                request.completion_id,
                request.generation,
                SpeechStage.FAILED,
                SpeechTerminalReason.PREPLAY_FAILURE,
                SpeechFailure.INVALID_REQUEST,
                0,
            )

        stream: _OwnedOutputStream | None = None
        completed = 0
        try:
            for buffer in prepared:
                interruption = self._interruption_for(request.generation)
                if interruption is not None:
                    reason, failure = interruption
                    return SpeechReceipt(
                        request.completion_id,
                        request.generation,
                        SpeechStage.FAILED if failure else SpeechStage.INTERRUPTED,
                        reason,
                        failure,
                        completed,
                    )
                if stream is None:
                    stream = self.open_output_stream(buffer.sample_rate_hz)
                    if not self.claim_stream(request.generation, stream):
                        interruption = self._interruption_for(request.generation)
                        reason, failure = interruption or (
                            SpeechTerminalReason.SHUTDOWN,
                            None,
                        )
                        return SpeechReceipt(
                            request.completion_id,
                            request.generation,
                            SpeechStage.FAILED if failure else SpeechStage.INTERRUPTED,
                            reason,
                            failure,
                            completed,
                        )
                frames = np.frombuffer(buffer.pcm_f32le, dtype="<f4")
                stream.write(frames.reshape(-1, 1))
                completed += 1
            return SpeechReceipt(
                request.completion_id,
                request.generation,
                SpeechStage.COMPLETED,
                SpeechTerminalReason.COMPLETED,
                buffers_completed=completed,
            )
        except Exception:
            interruption = self._interruption_for(request.generation)
            if interruption is not None:
                reason, failure = interruption
                return SpeechReceipt(
                    request.completion_id,
                    request.generation,
                    SpeechStage.FAILED if failure else SpeechStage.INTERRUPTED,
                    reason,
                    failure,
                    completed,
                )
            return SpeechReceipt(
                request.completion_id,
                request.generation,
                SpeechStage.FAILED,
                (
                    SpeechTerminalReason.UNAVAILABLE_OUTPUT
                    if stream is None
                    else SpeechTerminalReason.PLAYBACK_FAILURE
                ),
                (
                    SpeechFailure.OUTPUT_UNAVAILABLE
                    if stream is None
                    else SpeechFailure.PLAYBACK_FAILED
                ),
                completed,
            )
        finally:
            if stream is not None:
                self.release_stream(request.generation, stream)
            self.finish_generation(request.generation)

    def _begin_interrupt(
        self,
        generation: int,
        reason: SpeechTerminalReason,
        failure: SpeechFailure | None = None,
    ) -> None:
        with self._lock:
            self._interruptions.setdefault(generation, (reason, failure))
            stream = self._active.pop(generation, None)
            if stream is None or generation in self._abort_workers:
                return
            worker = threading.Thread(
                target=self._abort_stream_worker,
                args=(generation, stream),
                name="speech-output-abort",
                daemon=True,
            )
            self._abort_workers[generation] = worker
            try:
                worker.start()
            except Exception:
                self._abort_workers.pop(generation, None)
                # The generation worker still owns normal native teardown.
                self._active[generation] = stream

    def _abort_stream_worker(
        self,
        generation: int,
        stream: _OwnedOutputStream,
    ) -> None:
        try:
            self.abort_stream(stream)
        finally:
            with self._lock:
                if self._abort_workers.get(generation) is threading.current_thread():
                    self._abort_workers.pop(generation, None)
                if generation in self._finish_pending:
                    self._finish_pending.discard(generation)
                    self._interruptions.pop(generation, None)

    def interrupt(
        self,
        generation: int,
        reason: SpeechTerminalReason,
    ) -> None:
        self._begin_interrupt(generation, reason)

    def output_device_changed(self, generation: int) -> None:
        """Fail an uncertain write once; the next utterance resolves fresh output."""

        self._begin_interrupt(
            generation,
            SpeechTerminalReason.PLAYBACK_FAILURE,
            SpeechFailure.OUTPUT_CHANGED,
        )

    def suspend(self) -> None:
        """Abort owned output before sleep; wake remains lazy."""

        with self._lock:
            generations = tuple(self._active)
        for generation in generations:
            self._begin_interrupt(generation, SpeechTerminalReason.SHUTDOWN)

    def resume(self) -> None:
        return None

    def close(self) -> None:
        with self._lock:
            first_close = not self._closed
            self._closed = True
        if first_close:
            self.suspend()
        with self._lock:
            generations = tuple(self._abort_workers)
        for generation in generations:
            if self._wait_for_abort(generation):
                with self._lock:
                    self._interruptions.pop(generation, None)
        with self._lock:
            for generation in tuple(self._interruptions):
                if generation not in self._abort_workers:
                    self._interruptions.pop(generation, None)


assert isinstance(MacOSSpeechPlaybackAdapter(), SpeechPlaybackAdapter)


__all__ = ["MacOSSpeechPlaybackAdapter", "MacOSSpeechSynthesisAdapter"]
