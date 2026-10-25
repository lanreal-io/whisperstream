"""Single-owner, newest-wins TTS playback (agent-speech Phase 1, Task 1.4).

:class:`PlaybackOwner` is the ONLY audio-output owner in the process. A single
generation counter enforces newest-wins: each :meth:`PlaybackOwner.speak` bumps the
counter and spawns a fresh daemon worker; any older worker notices it is stale
(between sentences, and best-effort inside the sherpa generate callback) and bails
without writing further frames. :meth:`PlaybackOwner.stop` is instant and
non-blocking -- it bumps the counter and aborts the active
``sounddevice.OutputStream`` but never joins a worker and never waits. That contract
is load-bearing: ``stop()`` is called from the pynput hotkey thread under
``WhisperstreamApp._lock`` (the PTT barge-in chokepoint), where blocking or raising
would stall dictation. See ``audio_capture.py`` for the never-block-the-audio-thread
discipline this mirrors.

The worker splits response text into sentences (a deliberate 4th local copy of the
``_split_sentences`` regex -- do NOT import it
from ``text_guards``) and synthesizes sentence-by-sentence so the first audio plays
after the FIRST sentence and cancellation has between-sentence checkpoints.

No sounddevice device work happens at import or construction: the ``OutputStream``
is opened inside the worker, on the first frames. Concrete engine implementations
arrive through an engine manager (or the legacy ``engine_provider`` seam), so
exercising playback never loads either model.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Literal, Optional

import numpy as np
import sounddevice as sd

from .engine_specs import SpeechInputError, VoiceId
from .selection import EngineCandidate, EngineManager
from ..tts_playback import (
    create_speech_playback_adapter,
    create_speech_synthesis_adapter,
)
from ..tts_playback_contract import SpeechTerminalReason as ContractTerminalReason

__all__ = ["PlaybackOwner"]

PlaybackTerminalReason = Literal[
    "completed",
    "stopped",
    "superseded",
    "failed",
]

KOKORO_MAX_REALTIME_FACTOR = 0.5
WASAPI_HOST_API_NAME = "Windows WASAPI"
MME_HOST_API_NAME = "MME"
WINDOWS_SOUND_MAPPER_NAME = "Microsoft Sound Mapper - Output"
WASAPI_OPEN_ATTEMPTS = 2


# Deliberate 4th local copy of the sentence splitter (these regexes are intentionally NOT centralized). Same shape as
# text_guards._split_sentences: split on .!? followed by whitespace, keep punctuation,
# drop empty fragments.
def _split_sentences(text: str) -> List[str]:
    """Split *text* into sentences on ``.!?`` + whitespace; drop empty fragments."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


class PlaybackOwner:
    """Single owner of the process's TTS audio output.

    Constructed cheaply (no engine load, no stream, no device work). All public
    methods (:meth:`speak`, :meth:`stop`) are thread-safe and never raise: internal
    errors are swallowed and logged as metadata only (never response text).
    """

    def __init__(
        self,
        *,
        engine_provider: Optional[Callable[[], Any]] = None,
        engine_manager: Optional[EngineManager] = None,
        broadcast: Callable[[Dict[str, object]], None],
        logger: Optional[logging.Logger] = None,
        first_sentence_clock: Callable[[], float] = time.perf_counter,
        synthesis_adapter: object | None = None,
        playback_adapter: object | None = None,
    ) -> None:
        self._broadcast = broadcast
        self._logger = logger or logging.getLogger(__name__)
        if engine_manager is None:
            if engine_provider is None:
                raise ValueError("engine_provider or engine_manager is required")
            engine_manager = EngineManager(
                engine_factory=lambda _key: engine_provider(),
                logger=self._logger,
            )
        self._engine_manager = engine_manager
        self._synthesis_adapter = synthesis_adapter or create_speech_synthesis_adapter(
            engine_manager=engine_manager,
            logger=self._logger,
        )
        self._playback_adapter = playback_adapter or create_speech_playback_adapter(
            logger=self._logger,
            sounddevice_module=sd,
            platform_provider=lambda: sys.platform,
            default_output_name=lambda: self._resolve_windows_default_output_name(),
            wasapi_device_resolver=lambda name: self._resolve_wasapi_output_device(name),
            mapper_device_resolver=lambda: self._resolve_windows_sound_mapper_device(),
        )
        self._first_sentence_clock = first_sentence_clock
        self._lock = threading.Lock()
        # Monotonic newest-wins counter. The worker for generation ``g`` is stale
        # the moment ``self._generation != g``.
        self._generation = 0
        # The OutputStream the current worker is playing into, so stop()/speak() can
        # abort it. ``None`` when no stream is open.
        self._active_stream: Optional[Any] = None
        self._active_engine_key: Optional[str] = None
        # The generation that currently "owns" the speaking state, i.e. emitted the
        # last ``speaking`` event and is responsible for the matching ``idle``.
        self._speaking_gen: Optional[int] = None
        # Count of spawned workers that have not exited yet. Covers the window
        # between speak() and the worker's first frames (synthesis + stream
        # open), during which _active_stream/_speaking_gen are still None.
        # speak_if_idle() treats ANY live worker as busy so a low-priority
        # utterance can never supersede speech that is about to start.
        self._busy_workers = 0
        # Stop-only generations never own a worker or a terminal-reason entry.
        self._worker_generations: set[int] = set()
        # First terminal cause for each generation. Freeze completion/failure
        # before native teardown; workers consume their own entry on exit.
        self._cancel_reasons: Dict[int, PlaybackTerminalReason] = {}

    # -- public surface -----------------------------------------------------

    def active_engine(self) -> Optional[str]:
        """Return the engine currently writing audio, or ``None`` while idle."""

        with self._lock:
            return self._active_engine_key

    def speak(
        self,
        text: str,
        sid: VoiceId = 0,
        speed: float = 1.0,
        volume: float = 1.0,
        completion_id: Optional[str] = None,
        on_terminal: Optional[Callable[[PlaybackTerminalReason], None]] = None,
        candidates: Optional[tuple[EngineCandidate, ...]] = None,
    ) -> None:
        """Speak *text*, superseding any in-flight speech (newest-wins).

        Non-blocking: spawns a daemon worker and returns immediately. Never raises.
        When supplied, ``completion_id`` is copied onto this generation's
        content-free ``speech.state`` events.
        """
        # ============================================================================
        # TRANSCRIPT DATA -- DO NOT LOG
        # `text` is Claude Code agent response content (transcript-class data). Never log it or any derived string. Metadata
        # only (state enum, reason=<type>) is fine. See text_injector.py's banner.
        # ============================================================================
        try:
            with self._lock:
                previous_generation = self._generation
                self._generation += 1
                gen = self._generation
                interrupt_previous = previous_generation in self._worker_generations
                if interrupt_previous:
                    self._cancel_reasons.setdefault(previous_generation, "superseded")
                prev = self._active_stream
                self._active_stream = None
                self._busy_workers += 1
                self._worker_generations.add(gen)
            if interrupt_previous and previous_generation > 0:
                self._playback_adapter.interrupt(
                    previous_generation,
                    ContractTerminalReason.SUPERSEDED,
                )
            else:
                self._playback_adapter.abort_stream(prev)
            self._spawn_worker(
                text,
                (
                    (EngineCandidate("supertonic", sid),)
                    if candidates is None
                    else candidates
                ),
                speed,
                volume,
                gen,
                completion_id,
                on_terminal,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise public surface
            self._logger.warning("[Speech] playback error reason=%s", type(exc).__name__)
            self._notify_terminal(on_terminal, "failed")

    def speak_if_idle(
        self,
        text: str,
        sid: VoiceId = 0,
        speed: float = 1.0,
        volume: float = 1.0,
        candidates: Optional[tuple[EngineCandidate, ...]] = None,
    ) -> bool:
        """Speak *text* ONLY when playback is fully idle. Never supersedes.

        Used for low-priority deterministic template utterances (progress
        milestones): unlike :meth:`speak`, this never interrupts current or
        pending speech, so a routine progress line can never kill a response
        that is playing (or still synthesizing). Returns True when the
        utterance was claimed, False when something else is (about to be)
        playing. Non-blocking; never raises.
        """
        try:
            with self._lock:
                if (
                    self._busy_workers > 0
                    or self._active_stream is not None
                    or self._speaking_gen is not None
                ):
                    return False
                self._generation += 1
                gen = self._generation
                self._busy_workers += 1
                self._worker_generations.add(gen)
            self._spawn_worker(
                text,
                (
                    (EngineCandidate("supertonic", sid),)
                    if candidates is None
                    else candidates
                ),
                speed,
                volume,
                gen,
            )
            return True
        except Exception as exc:  # noqa: BLE001 -- never-raise public surface
            self._logger.warning("[Speech] playback error reason=%s", type(exc).__name__)
            return False

    def _spawn_worker(
        self,
        text: str,
        candidates: tuple[EngineCandidate, ...],
        speed: float,
        volume: float,
        gen: int,
        completion_id: Optional[str] = None,
        on_terminal: Optional[Callable[[PlaybackTerminalReason], None]] = None,
    ) -> None:
        """Start the daemon worker for *gen*. Caller has already counted it in
        ``_busy_workers``; the count is released here on spawn failure or by the
        worker's ``finally`` on exit."""
        try:
            worker = threading.Thread(
                target=self._run,
                args=(
                    text,
                    candidates,
                    float(speed),
                    float(volume),
                    gen,
                    completion_id,
                    on_terminal,
                ),
                name="speech-playback",
                daemon=True,
            )
            worker.start()
        except Exception:
            with self._lock:
                self._busy_workers -= 1
                self._worker_generations.discard(gen)
                self._cancel_reasons.pop(gen, None)
            raise

    def stop(self) -> None:
        """Stop any current speech immediately. Non-blocking; never joins or raises.

        Called from the pynput hotkey thread under ``WhisperstreamApp._lock`` (PTT
        barge-in). Bumps the generation so any running worker goes stale, and aborts
        the active stream so audio cuts now. Safe to call before any :meth:`speak`.
        """
        try:
            gen, prev, interrupted = self._begin_new_generation("stopped")
            if interrupted and gen > 1:
                self._playback_adapter.interrupt(
                    gen - 1,
                    ContractTerminalReason.EXPLICIT_STOP,
                )
            else:
                self._playback_adapter.abort_stream(prev)
        except Exception as exc:  # noqa: BLE001 -- never-raise public surface
            self._logger.warning("[Speech] playback error reason=%s", type(exc).__name__)

    def shutdown(self, timeout: float = 2.0) -> bool:
        """Stop playback and wait a bounded time for engine leases. Never raises."""

        drained = False
        playback_closed = False
        try:
            self.stop()
            drained = bool(
                self._synthesis_adapter.shutdown(
                    timeout=max(0.0, float(timeout))
                )
            )
        except Exception as exc:  # noqa: BLE001 -- teardown is never-raise
            self._logger.warning(
                "[Speech] synthesis shutdown error reason=%s",
                type(exc).__name__,
            )
        try:
            self._playback_adapter.close()
            playback_closed = True
        except Exception as exc:  # noqa: BLE001 -- teardown is never-raise
            self._logger.warning(
                "[Speech] output shutdown error reason=%s",
                type(exc).__name__,
            )
        return drained and playback_closed

    # -- internal helpers ---------------------------------------------------

    def _begin_new_generation(
        self,
        reason: PlaybackTerminalReason = "superseded",
    ) -> "tuple[int, Optional[sd.OutputStream], bool]":
        """Bump the generation and detach the active stream.

        Returns ``(new_generation, previous_stream, interrupted)``. The generation is captured
        under the lock so the caller uses exactly the value it just minted (a
        concurrent speak/stop cannot make it hand a worker a foreign generation).
        The abort itself happens OUTSIDE the lock (see :meth:`_abort_stream`) so a
        potentially slow ``abort()`` never blocks the lock -- keeping ``stop()``
        non-blocking for the hotkey thread.
        """
        with self._lock:
            previous_generation = self._generation
            self._generation += 1
            gen = self._generation
            interrupted = previous_generation in self._worker_generations
            if interrupted:
                self._cancel_reasons.setdefault(previous_generation, reason)
            prev = self._active_stream
            self._active_stream = None
        return gen, prev, interrupted

    @staticmethod
    def _abort_stream(stream: Optional[sd.OutputStream]) -> None:
        if stream is None:
            return
        try:
            stream.abort()
        except Exception:  # noqa: BLE001 -- abort must never raise into a caller
            pass

    @staticmethod
    def _discard_failed_stream(stream: Optional[sd.OutputStream]) -> None:
        """Release a stream that failed before playback ownership was published."""

        if stream is None:
            return
        PlaybackOwner._abort_stream(stream)
        try:
            stream.close()
        except Exception:  # noqa: BLE001 -- cleanup is best-effort
            pass

    @property
    def selection_policy(self) -> object:
        """Expose the adapter-owned policy to the shared speech controller."""

        return self._synthesis_adapter.selection_policy

    @staticmethod
    def _resolve_windows_default_output_name() -> Optional[str]:
        """Read the live Windows Core Audio default endpoint name."""

        initialized = False
        try:
            import comtypes

            comtypes.CoInitialize()
            initialized = True
            from pycaw.pycaw import AudioUtilities

            name = str(getattr(AudioUtilities.GetSpeakers(), "FriendlyName", ""))
            return name.strip() or None
        except Exception:  # noqa: BLE001 -- endpoint lookup is fail-soft
            return None
        finally:
            if initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:  # noqa: BLE001 -- COM cleanup is best-effort
                    pass

    @staticmethod
    def _device_name(device: int) -> Optional[str]:
        try:
            info = sd.query_devices(device)
            if int(info.get("max_output_channels", 0)) <= 0:
                return None
            return str(info.get("name", "")).strip() or None
        except Exception:  # noqa: BLE001 -- stale PortAudio entry
            return None

    @classmethod
    def _resolve_wasapi_output_device(
        cls,
        windows_default_name: Optional[str] = None,
    ) -> Optional[int]:
        """Resolve the live Windows endpoint within PortAudio's WASAPI inventory."""

        for host_api in sd.query_hostapis():
            if str(host_api.get("name", "")).casefold() != (
                WASAPI_HOST_API_NAME.casefold()
            ):
                continue
            if windows_default_name is not None:
                wanted = windows_default_name.casefold()
                for raw_device in host_api.get("devices", ()):
                    try:
                        device = int(raw_device)
                    except (TypeError, ValueError):
                        continue
                    name = cls._device_name(device)
                    if name is not None and name.casefold() == wanted:
                        return device
                return None
            try:
                device = int(host_api["default_output_device"])
            except (KeyError, TypeError, ValueError):
                return None
            return device if device >= 0 else None
        return None

    @classmethod
    def _resolve_windows_sound_mapper_device(cls) -> Optional[int]:
        """Find the dynamic MME mapper used when PortAudio's WASAPI list is stale."""

        for host_api in sd.query_hostapis():
            if str(host_api.get("name", "")).casefold() != MME_HOST_API_NAME.casefold():
                continue
            for raw_device in host_api.get("devices", ()):
                try:
                    device = int(raw_device)
                except (TypeError, ValueError):
                    continue
                name = cls._device_name(device)
                if (
                    name is not None
                    and name.casefold() == WINDOWS_SOUND_MAPPER_NAME.casefold()
                ):
                    return device
            return None
        return None

    def _open_output_stream(self, sample_rate: int) -> Any:
        """Open and start the preferred output stream for one utterance.

        Windows resolves the live Core Audio default before each shared-WASAPI
        attempt. If PortAudio has not discovered that endpoint yet, the dynamic
        Windows Sound Mapper follows the system default. The original PortAudio
        default remains the final fallback. Recovery stops at open/start so frames
        already written to a live stream are never replayed.
        """

        return self._playback_adapter.open_output_stream(sample_rate)

    def _is_current(self, gen: int) -> bool:
        with self._lock:
            return self._generation == gen

    def _emit_state(
        self,
        state: str,
        completion_id: Optional[str] = None,
        *,
        engine_key: Optional[str] = None,
    ) -> None:
        """Broadcast a content-free ``speech.state`` event. Never raises."""
        try:
            payload: Dict[str, object] = {"type": "speech.state", "state": state}
            if engine_key is not None:
                payload["engine"] = engine_key
            if completion_id is not None:
                payload["completion_id"] = completion_id
            self._broadcast(payload)
        except Exception as exc:  # noqa: BLE001 -- broadcast is best-effort
            self._logger.warning("[Speech] playback error reason=%s", type(exc).__name__)

    def _mark_engine_unhealthy(
        self,
        engine_key: str,
        reason: str,
        gen: int,
    ) -> bool:
        """Atomically quarantine the engine only for the current generation."""

        with self._lock:
            if self._generation != gen:
                return False
            self._synthesis_adapter.mark_unhealthy(engine_key, reason)
            actual_reason = self._synthesis_adapter.health_reason(engine_key) or reason
            try:
                self._broadcast(
                    {
                        "type": "speech.engine_health",
                        "engine": engine_key,
                        "healthy": False,
                        "reason": actual_reason,
                    }
                )
            except Exception as exc:  # noqa: BLE001 -- broadcast is best-effort
                self._logger.warning(
                    "[Speech] playback error reason=%s",
                    type(exc).__name__,
                )
        return True

    def _mark_engine_healthy(self, engine_key: str, gen: int) -> bool:
        """Atomically restore the engine only for the current generation."""

        with self._lock:
            if self._generation != gen:
                return False
            self._synthesis_adapter.mark_healthy(engine_key)
        return True

    def _run(
        self,
        text: str,
        candidates: tuple[EngineCandidate, ...],
        speed: float,
        volume: float,
        gen: int,
        completion_id: Optional[str] = None,
        on_terminal: Optional[Callable[[PlaybackTerminalReason], None]] = None,
    ) -> None:
        """Worker body for one generation: synthesize + play sentence-by-sentence.

        Runs on a daemon thread. Bails the instant it detects it is stale
        (superseded by a newer speak, or stopped). Emits exactly one ``speaking``
        (before the first frames) and, iff it owned the speaking state, exactly one
        ``idle`` (on finish / error / stop).
        """
        # ============================================================================
        # TRANSCRIPT DATA -- DO NOT LOG
        # `text` (and every sentence derived from it) is Claude Code agent response
        # content (transcript-class data). Never log it. Metadata only. See the banner
        # on speak() above.
        # ============================================================================
        stream: Optional[sd.OutputStream] = None
        owns_speaking = False
        wrote_audio = False
        terminal_reason: PlaybackTerminalReason = "failed"
        lease = None
        try:
            sentences = _split_sentences(text)
            if not sentences or not candidates:
                return
            first_sentence = sentences[0]
            engine = None
            first_frames: np.ndarray | None = None
            sample_rate: int | None = None

            for index, candidate in enumerate(candidates):
                if not self._is_current(gen):
                    terminal_reason = self._terminal_reason_for(gen)
                    return
                candidate_lease = None
                stage = "load"
                try:
                    candidate_lease = self._synthesis_adapter.acquire(
                        candidate.engine_key
                    )
                    candidate_engine = candidate_lease.engine
                    candidate_engine.load()
                    if not self._is_current(gen):
                        terminal_reason = self._terminal_reason_for(gen)
                        candidate_lease.release()
                        return

                    stage = "generate"
                    # The performance ceiling measures first-sentence synthesis,
                    # not lazy model load. Load failures still take the ordinary
                    # pre-stream fallback path above, but a healthy cold Kokoro
                    # session is not rejected solely because weights had to load.
                    started = self._first_sentence_clock()
                    samples = self._synthesis_adapter.generate(
                        candidate_engine,
                        first_sentence,
                        voice=candidate.voice_id,
                        speed=speed,
                        callback=lambda _s, _p, _g=gen: self._cancel_callback(_g),
                    )
                    if not self._is_current(gen):
                        terminal_reason = self._terminal_reason_for(gen)
                        candidate_lease.release()
                        return
                    stage = "validate_output"
                    frames = self._validated_frames(samples)
                    elapsed = self._first_sentence_clock() - started
                    if not self._is_current(gen):
                        terminal_reason = self._terminal_reason_for(gen)
                        candidate_lease.release()
                        return
                    # The synthesis benchmark's 0.5 RTF ceiling measures engine
                    # speed independently of sentence length. An absolute wall-
                    # clock cutoff cannot do that because Kokoro produces the
                    # whole first sentence before playback begins: a healthy long
                    # sentence naturally takes longer than a healthy short one.
                    stage = "sample_rate"
                    candidate_sample_rate = self._synthesis_adapter.sample_rate(candidate_engine)
                    if candidate_sample_rate <= 0:
                        raise ValueError("invalid sample rate")
                    audio_seconds = frames.size / candidate_sample_rate
                    realtime_factor = elapsed / audio_seconds
                    if (
                        candidate.engine_key == "kokoro"
                        and realtime_factor > KOKORO_MAX_REALTIME_FACTOR
                        and index + 1 < len(candidates)
                    ):
                        self._mark_engine_unhealthy(
                            "kokoro",
                            "slow_first_sentence",
                            gen,
                        )
                        candidate_lease.release()
                        if index + 1 < len(candidates):
                            continue
                        return

                    # Sample rate belongs to the winning engine. Reading it only
                    # after valid first frames prevents a losing candidate from
                    # opening or configuring the output stream.
                    if candidate.engine_key == "kokoro":
                        self._mark_engine_healthy("kokoro", gen)
                except Exception as exc:  # noqa: BLE001 -- try next only pre-stream
                    if candidate_lease is not None:
                        candidate_lease.release()
                    if not self._is_current(gen):
                        terminal_reason = self._terminal_reason_for(gen)
                        return
                    if (
                        candidate.engine_key == "kokoro"
                        and not isinstance(exc, SpeechInputError)
                    ):
                        health_reason = (
                            "load_failed"
                            if stage == "load"
                            else (
                                "invalid_output"
                                if stage in {"validate_output", "sample_rate"}
                                else "generate_failed"
                            )
                        )
                        self._mark_engine_unhealthy(
                            "kokoro",
                            health_reason,
                            gen,
                        )
                    self._logger.warning(
                        "[Speech] playback candidate error engine=%s stage=%s reason=%s",
                        candidate.engine_key,
                        stage,
                        type(exc).__name__,
                    )
                    if index + 1 < len(candidates):
                        continue
                    return

                lease = candidate_lease
                engine = candidate_engine
                first_frames = frames
                sample_rate = candidate_sample_rate
                break

            if (
                lease is None
                or engine is None
                or first_frames is None
                or sample_rate is None
            ):
                return

            for sentence_index, sentence in enumerate(sentences):
                if not self._is_current(gen):
                    terminal_reason = self._terminal_reason_for(gen)
                    return
                if sentence_index == 0:
                    frames = first_frames
                else:
                    try:
                        samples = self._synthesis_adapter.generate(
                            engine,
                            sentence,
                            voice=self._voice_for_engine(
                                candidates,
                                lease.engine_key,
                            ),
                            speed=speed,
                            callback=lambda _s, _p, _g=gen: self._cancel_callback(_g),
                        )
                    except Exception as exc:
                        if (
                            lease.engine_key == "kokoro"
                            and self._is_current(gen)
                            and not isinstance(exc, SpeechInputError)
                        ):
                            self._mark_engine_unhealthy(
                                "kokoro",
                                "generate_failed",
                                gen,
                            )
                        raise
                    try:
                        frames = self._validated_frames(samples)
                    except Exception:
                        if lease.engine_key == "kokoro" and self._is_current(gen):
                            self._mark_engine_unhealthy(
                                "kokoro",
                                "invalid_output",
                                gen,
                            )
                        raise
                    if not self._is_current(gen):
                        terminal_reason = self._terminal_reason_for(gen)
                        return
                if frames.size == 0:
                    continue
                if volume != 1.0:
                    frames = frames * np.float32(volume)
                if stream is None:
                    stream = self._open_output_stream(sample_rate)
                    # Register + take speaking ownership atomically, but only if we
                    # are still current (a stop/newer speak may have landed during
                    # the first synth or the stream open).
                    with self._lock:
                        if self._generation != gen:
                            superseded = True
                        elif not self._playback_adapter.claim_stream(gen, stream):
                            superseded = True
                        else:
                            self._active_stream = stream
                            self._speaking_gen = gen
                            self._active_engine_key = lease.engine_key
                            owns_speaking = True
                            superseded = False
                            # Linearize the state event with the generation claim.
                            # A newer speak/stop cannot overtake this broadcast and
                            # leave a stale completion marked as speaking afterward.
                            self._emit_state(
                                "speaking",
                                completion_id,
                                engine_key=lease.engine_key,
                            )
                    if superseded:
                        terminal_reason = self._terminal_reason_for(gen)
                        self._playback_adapter.abort_stream(stream)
                        return
                stream.write(frames.reshape(-1, 1))
                wrote_audio = True
            if wrote_audio:
                terminal_reason = "completed"
        except Exception as exc:  # noqa: BLE001 -- worker must never crash the thread
            self._logger.warning("[Speech] playback error reason=%s", type(exc).__name__)
            terminal_reason = (
                self._terminal_reason_for(gen)
                if not self._is_current(gen)
                else "failed"
            )
        finally:
            # stop()/supersede may arrive inside the final native write. Keep
            # that earlier interruption, then freeze the outcome before cleanup
            # so a later stop while stop/close drains cannot rewrite completion.
            with self._lock:
                terminal_reason = self._cancel_reasons.setdefault(gen, terminal_reason)
            if stream is not None:
                self._playback_adapter.release_stream(gen, stream)
                with self._lock:
                    if self._active_stream is stream:
                        self._active_stream = None
            # Emit idle iff this generation still owns the speaking state. A newer
            # generation that already took ownership will emit its own idle; a
            # generation that never started speaking emits nothing (no spurious idle).
            with self._lock:
                self._busy_workers -= 1
                self._worker_generations.discard(gen)
                self._cancel_reasons.pop(gen, None)
                if owns_speaking and self._speaking_gen == gen:
                    self._speaking_gen = None
                    finished_engine_key = self._active_engine_key
                    self._active_engine_key = None
                    # Keep the matching idle ordered before a newer generation can
                    # claim speaking ownership and publish its own state.
                    self._emit_state(
                        "idle",
                        completion_id,
                        engine_key=finished_engine_key,
                    )
            if lease is not None:
                lease.release()
            self._playback_adapter.finish_generation(gen)
            self._notify_terminal(on_terminal, terminal_reason)

    @staticmethod
    def _validated_frames(samples: object) -> np.ndarray:
        frames = np.asarray(samples, dtype=np.float32).reshape(-1)
        if frames.size == 0 or not np.all(np.isfinite(frames)):
            raise ValueError("invalid speech output")
        return frames

    @staticmethod
    def _voice_for_engine(
        candidates: tuple[EngineCandidate, ...],
        engine_key: str,
    ) -> VoiceId:
        for candidate in candidates:
            if candidate.engine_key == engine_key:
                return candidate.voice_id
        raise RuntimeError("winning speech engine has no pinned voice")

    def _cancel_callback(self, gen: int) -> int:
        """sherpa generate callback: return non-zero to stop early when stale.

        Best-effort only -- per the 1a bench the callback fires once per generate, so
        the between-sentence :meth:`_is_current` checks are the primary cancel path.
        """
        return 0 if self._is_current(gen) else 1

    def _terminal_reason_for(self, gen: int) -> PlaybackTerminalReason:
        with self._lock:
            return self._cancel_reasons.get(gen, "superseded")

    def _notify_terminal(
        self,
        callback: Optional[Callable[[PlaybackTerminalReason], None]],
        reason: PlaybackTerminalReason,
    ) -> None:
        if callback is None:
            return
        try:
            callback(reason)
        except Exception as exc:  # noqa: BLE001 -- callback must not kill worker
            self._logger.warning(
                "[Speech] playback callback error reason=%s",
                type(exc).__name__,
            )
