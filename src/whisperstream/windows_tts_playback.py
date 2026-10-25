"""Windows owners for Agent Speak model sessions and audio output.

The shared controller retains only generations, logical engine/voice identities,
and content-hidden PCM buffers. PortAudio devices, Core Audio endpoints, native
streams, model sessions, and engine leases stay inside these adapters.
"""

from __future__ import annotations

import logging
import re
import sys
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


WASAPI_HOST_API_NAME = "Windows WASAPI"
MME_HOST_API_NAME = "MME"
WINDOWS_SOUND_MAPPER_NAME = "Microsoft Sound Mapper - Output"
WASAPI_OPEN_ATTEMPTS = 2


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [part.strip() for part in parts if part.strip()]


class _StreamLifecycle:
    """Per-claim gate between asynchronous abort and worker-owned close."""

    __slots__ = ("generation", "stream", "abort_started", "abort_done")

    def __init__(self, generation: int, stream: object) -> None:
        self.generation = generation
        self.stream = stream
        self.abort_started = False
        self.abort_done = threading.Event()


class WindowsSpeechSynthesisAdapter:
    """Own lazy Windows engine selection, leases, sessions, and synthesis."""

    def __init__(
        self,
        *,
        engine_manager: object,
        logger: object | None = None,
    ) -> None:
        if not isinstance(engine_manager, EngineManager):
            # Test managers intentionally duck-type the production manager.
            required = ("acquire", "engine", "shutdown")
            if not all(callable(getattr(engine_manager, name, None)) for name in required):
                raise TypeError("engine_manager does not implement the engine lifecycle")
        self._manager = engine_manager
        self._logger = logger or logging.getLogger(__name__)
        self._selection = SelectionPolicy(engine_manager)  # type: ignore[arg-type]
        self._closed = False

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

    def _resolved_engine_and_voice(
        self,
        request: SpeechRequest,
    ) -> tuple[str, object]:
        if request.engine is not SpeechEngineChoice.AUTOMATIC:
            engine_key = self._engine_key(request)
            return engine_key, self._voice(engine_key, request.voice_token)
        try:
            supertonic_voice: object = int(request.voice_token)
        except ValueError:
            supertonic_voice = SUPERTONIC_SPEC.default_voice_id
        kokoro_voice: object = request.voice_token
        resolution = self._selection.resolve(
            "auto",
            supertonic_voice=supertonic_voice,
            kokoro_voice=kokoro_voice,
        )
        if not resolution.candidates:
            raise RuntimeError("no automatic speech engine is available")
        candidate = resolution.candidates[0]
        return candidate.engine_key, candidate.voice_id

    @staticmethod
    def _voice(engine_key: str, token: str) -> object:
        value: object = token
        if engine_key == SUPERTONIC_SPEC.key:
            try:
                value = int(token)
            except ValueError:
                value = token
        spec = get_engine_spec(engine_key)
        normalized = spec.normalize_voice_id(value)
        if str(normalized) != token:
            raise ValueError("voice token is outside the approved preset scope")
        return normalized

    def synthesize(self, request: SpeechRequest) -> Iterator[SpeechBuffer]:
        """Yield ordered mono float32 buffers while holding one engine lease."""

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


class WindowsSpeechPlaybackAdapter:
    """Own current-output resolution and exact native stream cleanup."""

    def __init__(
        self,
        *,
        logger: object | None = None,
        sounddevice_module: object = sd,
        platform_provider: Callable[[], str] = lambda: sys.platform,
        default_output_name: Callable[[], str | None] | None = None,
        wasapi_device_resolver: Callable[[str | None], int | None] | None = None,
        mapper_device_resolver: Callable[[], int | None] | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._sd = sounddevice_module
        self._platform_provider = platform_provider
        self._default_output_name = (
            default_output_name or self._resolve_windows_default_output_name
        )
        self._wasapi_device_resolver = (
            wasapi_device_resolver or self._resolve_wasapi_output_device
        )
        self._mapper_device_resolver = (
            mapper_device_resolver or self._resolve_windows_sound_mapper_device
        )
        self._lock = threading.RLock()
        self._active: dict[int, _StreamLifecycle] = {}
        self._interruptions: dict[int, tuple[SpeechTerminalReason, SpeechFailure | None]] = {}
        self._closed = False

    def _device_name(self, device: int) -> str | None:
        try:
            info = self._sd.query_devices(device)  # type: ignore[attr-defined]
            if int(info.get("max_output_channels", 0)) <= 0:
                return None
            return str(info.get("name", "")).strip() or None
        except Exception:
            return None

    @staticmethod
    def _resolve_windows_default_output_name() -> str | None:
        initialized = False
        try:
            import comtypes

            comtypes.CoInitialize()
            initialized = True
            from pycaw.pycaw import AudioUtilities

            name = str(getattr(AudioUtilities.GetSpeakers(), "FriendlyName", ""))
            return name.strip() or None
        except Exception:
            return None
        finally:
            if initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    def _resolve_wasapi_output_device(
        self,
        windows_default_name: str | None = None,
    ) -> int | None:
        for host_api in self._sd.query_hostapis():  # type: ignore[attr-defined]
            if str(host_api.get("name", "")).casefold() != WASAPI_HOST_API_NAME.casefold():
                continue
            if windows_default_name is not None:
                wanted = windows_default_name.casefold()
                for raw_device in host_api.get("devices", ()):
                    try:
                        device = int(raw_device)
                    except (TypeError, ValueError):
                        continue
                    name = self._device_name(device)
                    if name is not None and name.casefold() == wanted:
                        return device
                return None
            try:
                device = int(host_api["default_output_device"])
            except (KeyError, TypeError, ValueError):
                return None
            return device if device >= 0 else None
        return None

    def _resolve_windows_sound_mapper_device(self) -> int | None:
        for host_api in self._sd.query_hostapis():  # type: ignore[attr-defined]
            if str(host_api.get("name", "")).casefold() != MME_HOST_API_NAME.casefold():
                continue
            for raw_device in host_api.get("devices", ()):
                try:
                    device = int(raw_device)
                except (TypeError, ValueError):
                    continue
                name = self._device_name(device)
                if name is not None and name.casefold() == WINDOWS_SOUND_MAPPER_NAME.casefold():
                    return device
            return None
        return None

    @staticmethod
    def abort_stream(stream: object | None) -> None:
        if stream is None:
            return
        try:
            stream.abort()  # type: ignore[attr-defined]
        except Exception:
            pass

    def discard_failed_stream(self, stream: object | None) -> None:
        if stream is None:
            return
        self.abort_stream(stream)
        try:
            stream.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    def open_output_stream(self, sample_rate: int) -> object:
        """Resolve the live default for each utterance and open once."""

        if self._closed:
            raise RuntimeError("speech playback adapter is closed")
        kwargs = {"samplerate": sample_rate, "channels": 1, "dtype": "float32"}
        if self._platform_provider() != "win32":
            stream = self._sd.OutputStream(**kwargs)  # type: ignore[attr-defined]
            try:
                stream.start()
            except Exception:
                self.discard_failed_stream(stream)
                raise
            return stream

        missing_live_endpoint = False
        for attempt in range(1, WASAPI_OPEN_ATTEMPTS + 1):
            stream = None
            try:
                default_name = self._default_output_name()
                device = self._wasapi_device_resolver(default_name)
                if device is None:
                    if default_name is not None:
                        missing_live_endpoint = True
                        break
                    raise RuntimeError("WASAPI output unavailable")
                stream = self._sd.OutputStream(  # type: ignore[attr-defined]
                    **kwargs,
                    device=device,
                    extra_settings=self._sd.WasapiSettings(  # type: ignore[attr-defined]
                        exclusive=False,
                        auto_convert=True,
                    ),
                )
                stream.start()
                return stream
            except Exception as exc:
                self.discard_failed_stream(stream)
                self._logger.warning(
                    "[Speech] playback backend error backend=wasapi attempt=%d reason=%s",
                    attempt,
                    type(exc).__name__,
                )

        try:
            mapper = self._mapper_device_resolver()
        except Exception as exc:
            mapper = None
            self._logger.warning(
                "[Speech] playback backend error backend=windows_mapper reason=%s",
                type(exc).__name__,
            )
        if mapper is not None:
            reason = "wasapi_device_unavailable" if missing_live_endpoint else "wasapi_open_failed"
            self._logger.warning(
                "[Speech] playback backend fallback backend=windows_mapper reason=%s",
                reason,
            )
            stream = None
            try:
                stream = self._sd.OutputStream(**kwargs, device=mapper)  # type: ignore[attr-defined]
                stream.start()
                return stream
            except Exception as exc:
                self.discard_failed_stream(stream)
                self._logger.warning(
                    "[Speech] playback backend error backend=windows_mapper reason=%s",
                    type(exc).__name__,
                )

        self._logger.warning("[Speech] playback backend fallback backend=default")
        stream = self._sd.OutputStream(**kwargs)  # type: ignore[attr-defined]
        try:
            stream.start()
        except Exception:
            self.discard_failed_stream(stream)
            raise
        return stream

    def claim_stream(self, generation: int, stream: object) -> bool:
        with self._lock:
            if self._closed or generation in self._interruptions:
                return False
            self._active[generation] = _StreamLifecycle(generation, stream)
            return True

    def release_stream(self, generation: int, stream: object) -> None:
        # The playback worker is the only owner allowed to stop/close a claimed
        # native stream. If an interrupter claimed abort first, wait only on that
        # stream's gate. A worker that removes an active claim first can close
        # immediately, and every later interrupter sees no handle to abort.
        abort_done = None
        with self._lock:
            lifecycle = self._active.get(generation)
            if lifecycle is not None and lifecycle.stream is stream:
                self._active.pop(generation, None)
                if lifecycle.abort_started:
                    abort_done = lifecycle.abort_done
        if abort_done is not None:
            abort_done.wait()
        try:
            stream.stop()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            stream.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    def finish_generation(self, generation: int) -> None:
        """Forget terminal interruption metadata after the owner unwinds."""

        with self._lock:
            self._interruptions.pop(generation, None)

    def play(
        self,
        request: SpeechRequest,
        buffers: Iterable[SpeechBuffer],
    ) -> SpeechReceipt:
        stream = None
        completed = 0
        expected_ordinal = 0
        try:
            for buffer in buffers:
                if (
                    buffer.generation != request.generation
                    or buffer.ordinal != expected_ordinal
                    or buffer.channels != 1
                ):
                    raise ValueError("speech buffer does not match the request")
                if stream is None:
                    stream = self.open_output_stream(buffer.sample_rate_hz)
                    if not self.claim_stream(request.generation, stream):
                        self.discard_failed_stream(stream)
                        stream = None
                        reason, failure = self._interruption_for(request.generation)
                        return SpeechReceipt(
                            request.completion_id,
                            request.generation,
                            (
                                SpeechStage.FAILED
                                if failure is not None
                                else SpeechStage.INTERRUPTED
                            ),
                            reason,
                            failure,
                            completed,
                        )
                frames = np.frombuffer(buffer.pcm_f32le, dtype="<f4")
                stream.write(frames.reshape(-1, 1))
                completed += 1
                expected_ordinal += 1
            if completed == 0:
                raise ValueError("speech produced no buffers")
            return SpeechReceipt(
                request.completion_id,
                request.generation,
                SpeechStage.COMPLETED,
                SpeechTerminalReason.COMPLETED,
                buffers_completed=completed,
            )
        except ValueError:
            return SpeechReceipt(
                request.completion_id,
                request.generation,
                SpeechStage.FAILED,
                SpeechTerminalReason.PREPLAY_FAILURE,
                SpeechFailure.INVALID_REQUEST,
                completed,
            )
        except Exception:
            reason, failure = self._interruption_for(request.generation)
            if failure is not None:
                return SpeechReceipt(
                    request.completion_id,
                    request.generation,
                    SpeechStage.FAILED,
                    reason,
                    failure,
                    completed,
                )
            if reason is not SpeechTerminalReason.SUPERSEDED:
                return SpeechReceipt(
                    request.completion_id,
                    request.generation,
                    SpeechStage.INTERRUPTED,
                    reason,
                    buffers_completed=completed,
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
            with self._lock:
                self._interruptions.pop(request.generation, None)

    def _interruption_for(
        self,
        generation: int,
    ) -> tuple[SpeechTerminalReason, SpeechFailure | None]:
        with self._lock:
            return self._interruptions.get(
                generation,
                (SpeechTerminalReason.SUPERSEDED, None),
            )

    @staticmethod
    def _claim_abort(lifecycle: _StreamLifecycle | None) -> _StreamLifecycle | None:
        if lifecycle is None or lifecycle.abort_started:
            return None
        lifecycle.abort_started = True
        return lifecycle

    def _schedule_abort(self, lifecycle: _StreamLifecycle | None) -> None:
        if lifecycle is None:
            return

        def abort_then_release_close_gate() -> None:
            try:
                self.abort_stream(lifecycle.stream)
            finally:
                lifecycle.abort_done.set()

        worker = threading.Thread(
            target=abort_then_release_close_gate,
            name=f"speech-stream-abort-{lifecycle.generation}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            # A failed daemon launch must not strand the playback finalizer.
            lifecycle.abort_done.set()

    def interrupt(
        self,
        generation: int,
        reason: SpeechTerminalReason,
    ) -> None:
        with self._lock:
            if generation in self._interruptions:
                lifecycle = None
            else:
                self._interruptions[generation] = (reason, None)
                lifecycle = self._claim_abort(self._active.get(generation))
        # Native abort is allowed to block its daemon, never the hotkey caller
        # or the adapter-wide ownership mutex. The worker's close gate preserves
        # abort-before-close ordering for this exact stream.
        self._schedule_abort(lifecycle)

    def output_device_changed(self, generation: int) -> None:
        """Abort the matching utterance; the next one resolves the new default."""

        with self._lock:
            if generation in self._interruptions:
                lifecycle = None
            else:
                self._interruptions[generation] = (
                    SpeechTerminalReason.PLAYBACK_FAILURE,
                    SpeechFailure.OUTPUT_CHANGED,
                )
                lifecycle = self._claim_abort(self._active.get(generation))
        self._schedule_abort(lifecycle)

    def suspend(self) -> None:
        """Abort every owned stream before Windows suspends audio endpoints."""

        with self._lock:
            lifecycles = []
            for generation, lifecycle in self._active.items():
                claimed = self._claim_abort(lifecycle)
                if claimed is not None:
                    self._interruptions[generation] = (
                        SpeechTerminalReason.SHUTDOWN,
                        None,
                    )
                    lifecycles.append(claimed)
        for lifecycle in lifecycles:
            self._schedule_abort(lifecycle)

    def resume(self) -> None:
        """Resume is intentionally lazy; the next utterance resolves fresh output."""

        return None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.suspend()


assert isinstance(
    WindowsSpeechSynthesisAdapter(engine_manager=EngineManager()),
    SpeechSynthesisAdapter,
)
assert isinstance(WindowsSpeechPlaybackAdapter(), SpeechPlaybackAdapter)


__all__ = [
    "WindowsSpeechPlaybackAdapter",
    "WindowsSpeechSynthesisAdapter",
]
