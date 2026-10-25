"""Speech controller: engine + playback + summarizer glue.

:class:`SpeechController` owns a lazy keyed engine manager, one
:class:`~whisperstream.speech.playback.PlaybackOwner`, one normal-utterance FIFO,
and the summarizer glue.
It is constructed cheaply behind the experimental gate on
``WhisperstreamApp`` (no model load, audio stream, or device work). Every public
method never raises outward; :meth:`stop` remains non-blocking for PTT barge-in.

**Normal Agent Speak is FIFO; cancellation is still newest-wins.** Validated summaries,
questions, and ordinary template announcements enter one controller-owned FIFO. The
controller waits for ordinary dictation to become idle and preserves the configured
inter-utterance gap before handing one item at a time to :class:`PlaybackOwner`. The
playback owner remains intentionally newest-wins as the low-level implementation of an
explicit stop, settings sample, or legacy cancellation. A separate controller generation
still spans the legacy summarize-latency window: :meth:`speak_response` discards a stale
summary after a stop or newer request.

**Import hygiene.** Engine implementation modules are imported by the keyed factory
only when their unloaded wrapper is first requested. Model weights and audio devices
remain untouched until a playback worker starts.

**Privacy.** Claude Code agent response text is transcript-class data (see the privacy rules
in ``text_injector.py``).
:meth:`speak_spoken_summary`, the legacy :meth:`speak_response` seam, and
:meth:`speak_text` (the onboarding voice-speed custom test box) carry TRANSCRIPT
DATA banners and log metadata only. The sample line spoken by :meth:`speak_sample`
is a fixed product string, not transcript data.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Deque, Dict, Optional

from ..agent_events import SpeechDebounce, template_for
from ..agent_session_names import normalize_session_name_for_speech
from . import summarizer
from .playback import PlaybackOwner
from .selection import EngineManager, SelectionResolution, normalize_selection

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config_manager import WhisperstreamConfig
    from ..llama_server import LlamaServerSupervisor

__all__ = ["SpeechController", "SAMPLE_TEXT"]


# Static line spoken by speak_sample() (the Controls "Hear a sample" button). A fixed
# product string, NOT transcript data -- safe to log and to keep verbatim here. No em
# dashes (user-visible copy rule).
SAMPLE_TEXT = "This is how your agent's replies will sound."


@dataclass(slots=True)
class _QueuedUtterance:
    """One transcript-class normal utterance retained only in process memory."""

    text: str
    selection: SelectionResolution
    speed: float
    volume: float
    completion_id: Optional[str]
    on_terminal: Optional[Callable[[str], None]]
    terminal: bool = False


class SpeechController:
    """Coordinates TTS engine, playback, and summarization for spoken agent replies.

    Construction is cheap and side-effect free (no model load, no stream). The
    collaborators are built here but do no device work until the first
    :meth:`speak_response` / :meth:`speak_sample`.
    """

    def __init__(
        self,
        *,
        config_provider: "Callable[[], WhisperstreamConfig]",
        broadcast: Callable[[Dict[str, object]], None],
        logger: Optional[logging.Logger] = None,
        supervisor_provider: "Callable[[], Optional[LlamaServerSupervisor]]",
        recording_provider: "Optional[Callable[[], bool]]" = None,
    ) -> None:
        self._config_provider = config_provider
        self._broadcast = broadcast
        self._logger = logger or logging.getLogger(__name__)
        self._supervisor_provider = supervisor_provider
        # Optional live "is a recording in progress" probe (wired to
        # WhisperstreamApp._recording). Barge-in outranks everything: while
        # the user is dictating, no normal utterance may start. PTT already
        # stopped in-flight speech; this closes the arrival-mid-recording gap.
        # None (tests, standalone use) means "never recording".
        self._recording_provider = recording_provider
        self._speech_enabled_provider: Optional[Callable[[], bool]] = None
        # Per-kind min-interval policy for template speech (agent_events).
        self._debounce = SpeechDebounce()

        # Newest-wins across the summarize-latency window. Guarded by a lock because
        # stop() runs on the pynput hotkey thread while speak_response runs as an
        # asyncio task on the server loop.
        self._lock = threading.RLock()
        self._generation = 0

        # Normal summaries, questions, and non-progress announcements share one
        # FIFO above the deliberately newest-wins playback owner. The worker is
        # lazy and daemonized; shutdown wakes and joins it within the caller's
        # existing bounded timeout. A short poll is required because the ordinary
        # recording probe exposes state, not a transition notification.
        self._queue_condition = threading.Condition(self._lock)
        self._normal_queue: Deque[_QueuedUtterance] = deque()
        self._normal_active: Optional[_QueuedUtterance] = None
        self._last_normal_terminal_at: Optional[float] = None
        self._recording_blocked = False
        self._recording_idle_at: Optional[float] = None
        self._queue_shutdown = False
        self._queue_worker: Optional[threading.Thread] = None

        # Both wrappers remain unloaded until a playback worker takes a lease.
        # Keep the historical ``_engine`` alias for Supertonic-only test fakes
        # and in-process callers while selection moves to the keyed manager.
        self._engine_manager = EngineManager(logger=self._logger)
        # ``_engine`` resolves on first access, never at construction. Building
        # the wrapper imports its native backend (sherpa-onnx for Supertonic),
        # which is a Windows-only pinned wheel, so an eager build made every
        # non-Windows startup fatal for a default-off feature nobody asked for.
        # Real selection already goes through the manager at speak time.
        self._engine_override: Optional[object] = None
        self._playback = PlaybackOwner(
            engine_manager=self._engine_manager,
            broadcast=self._broadcast,
            logger=self._logger,
        )
        # Windows selection and model lifecycle now live behind the frozen
        # synthesis adapter. Keep the aliases above for old in-process tests.
        self._selection_policy = self._playback.selection_policy
        self._residency_enabled: bool | None = None
        self.reconcile_residency()

    def reconcile_residency(self) -> None:
        """Apply persisted residency settings, excluding temporary setup preview."""

        with self._lock:
            config = self._config_provider()
            enabled = bool(getattr(config, "agent_speech_enabled", True))
            if self._residency_enabled is True and not enabled:
                self.stop()
            self._residency_enabled = enabled
            self._engine_manager.configure_residency(
                enabled=enabled,
                preferred=normalize_selection(getattr(config, "agent_speech_engine", "auto")),
                idle_timeout=getattr(config, "agent_speech_idle_timeout_seconds", 1800),
            )

    def release_idle_engines(self) -> None:
        """Release models at feature/privacy teardown after active leases finish."""

        self._engine_manager.request_unload()

    @property
    def _engine(self) -> object:
        """Historical Supertonic-only alias, resolved on demand.

        Reading this builds the wrapper (and imports its native backend) the
        first time. Nothing on the startup or dictation path touches it.
        """
        if self._engine_override is not None:
            return self._engine_override
        return self._engine_manager.engine("supertonic")

    @_engine.setter
    def _engine(self, engine: object) -> None:
        self._engine_override = engine

    # -- public surface -----------------------------------------------------

    def can_speak(self) -> bool:
        """Whether the requested selection has an available candidate. Never loads."""
        try:
            return self._resolve_selection(self._config_provider()).can_speak
        except Exception:  # noqa: BLE001 -- never-raise probe
            return False

    def active_engine(self) -> Optional[str]:
        """Return the concrete engine currently playing audio, if any."""

        try:
            return self._playback.active_engine()
        except Exception:  # noqa: BLE001 -- status probe is never-raise
            return None

    def speak_spoken_summary(
        self,
        spoken_summary: str,
        completion_id: Optional[str] = None,
        on_terminal: Optional[Callable[[str], None]] = None,
        *,
        name_prefix: Optional[str] = None,
    ) -> bool:
        """Speak one validated, marker-free agent utterance. Never raises.

        This is the normalized response-speech path. ``spoken_summary`` must
        already have been selected by the response content policy or the structured
        question composer. It enters the normal-utterance FIFO with no local
        summarizer or enhancement provider. The method name is retained for
        compatibility; full-response mode supplies normalized prose.

        ``name_prefix``, when supplied and non-empty, is a user-chosen session
        name (scope decision 4: only user-chosen names are spoken, never the
        project-basename fallback or the literal ``Claude``). It is normalized
        via :func:`normalize_session_name_for_speech` and joined onto a LOCAL
        copy of the summary as ``<name>. <summary>`` (copy-review string 10);
        ``spoken_summary`` itself is never mutated (scope decision 5), so the
        Spine's one-segment-per-sentence attribution, the dedup digest, and the
        marker-free validation are all untouched. If the normalized name already
        ends in ``.``, ``?``, or ``!``, no second terminator is added. A name
        that normalizes to empty is treated as absent.

        Returns ``True`` after the FIFO accepts the utterance. Every supplied
        terminal callback is invoked exactly once after playback terminates or
        the queued item is stopped, including unavailable and pre-playback
        failure paths.

        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # ``spoken_summary`` is Claude response content, and ``name_prefix`` is a
        # transcript-class session name. Never log either or a string derived from
        # either. Metadata only.
        # ============================================================================
        """
        try:
            if not isinstance(spoken_summary, str):
                self._notify_terminal(on_terminal, "unavailable")
                return False
            spoken = spoken_summary.strip()
            if not spoken:
                self._notify_terminal(on_terminal, "unavailable")
                return False

            if isinstance(name_prefix, str) and name_prefix:
                normalized_name = normalize_session_name_for_speech(name_prefix)
                if normalized_name:
                    if normalized_name[-1] in ".?!":
                        spoken = f"{normalized_name} {spoken}"
                    else:
                        spoken = f"{normalized_name}. {spoken}"

            config = self._config_provider()
            if not self._speech_enabled(config):
                self._notify_terminal(on_terminal, "unavailable")
                return False
            if getattr(config, "agent_speech_summary_source", "agent") != "agent":
                # Whisperstream-authored summaries are explicitly deferred.
                self._notify_terminal(on_terminal, "unavailable")
                return False
            selection = self._resolve_selection(config)
            if not selection.can_speak:
                self._notify_terminal(on_terminal, "unavailable")
                return False

            speed = float(getattr(config, "agent_speech_speed", 1.0))
            volume = float(getattr(config, "agent_speech_volume", 1.0))
            self._next_generation()

            return self._enqueue_normal_utterance(
                _QueuedUtterance(
                    text=spoken,
                    selection=selection,
                    speed=speed,
                    volume=volume,
                    completion_id=completion_id,
                    on_terminal=on_terminal,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise public surface
            self._logger.warning(
                "[Speech] speak_spoken_summary error reason=%s",
                type(exc).__name__,
            )
            self._notify_terminal(on_terminal, "failed")
            return False

    async def speak_response(
        self, text: str, completion_id: Optional[str] = None
    ) -> None:
        """Legacy full-response speech path. Never raises outward.

        Normalized configs always use :meth:`speak_spoken_summary`; this method
        becomes deliberately silent for them so arbitrary response content can
        never reach TTS while the server call site transitions. Objects that
        predate ``agent_speech_enabled`` retain the old behavior as an in-process
        compatibility seam.
        """
        # ============================================================================
        # TRANSCRIPT DATA -- DO NOT LOG
        # `text` (and the summarized/verbatim string derived from it) is Claude Code
        # agent response content (transcript-class data).
        # Never log it or any derived string. Metadata only (mode, reason=<type>) is
        # fine. See text_injector.py's banner. The
        # summary call always targets a LOCAL supervisor; response text never leaves
        # the machine.
        # ============================================================================
        try:
            if not text or not text.strip():
                return
            config = self._config_provider()
            if hasattr(config, "agent_speech_enabled"):
                # The normalized contract permits marked text only. The caller
                # must pass result["spoken_summary"] to speak_spoken_summary().
                return
            mode = getattr(config, "agent_speech_mode", "off")
            if mode == "off":
                return
            selection = self._resolve_selection(config)
            if not selection.can_speak:
                return

            speed = float(getattr(config, "agent_speech_speed", 1.0))
            volume = float(getattr(config, "agent_speech_volume", 1.0))

            # Snapshot the generation BEFORE the (possibly slow) summary await.
            gen = self._next_generation()

            # "conversational" responses are summarized exactly like "summary"
            # mode; the extra conversational behavior (progress/error templates)
            # lives in speak_event.
            supervisor = (
                self._supervisor_provider()
                if mode in ("summary", "conversational")
                else None
            )
            model_name = getattr(config, "enhancement_local_model", "") or ""
            spoken = await summarizer.speak_text_for(text, mode, supervisor, model_name)

            # Newest-wins across the summarize-latency window: a stop() or a newer
            # speak_response bumped the counter -> discard this stale result.
            if not self._is_current(gen):
                return
            if not spoken:
                return
            self._speak_with_selection(
                spoken,
                selection,
                speed=speed,
                volume=volume,
                completion_id=completion_id,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise public surface
            self._logger.warning(
                "[Speech] speak_response error reason=%s", type(exc).__name__
            )

    def speak_sample(
        self,
        *,
        engine: Optional[str] = None,
        voice: object = None,
    ) -> None:
        """Speak the fixed sample line at the current voice/speed/volume. Never raises.

        Fire-and-forget and non-blocking (playback spawns its own worker). Claims
        newest-wins ownership so an in-flight response's post-summary playback is
        discarded in favor of the sample the user just asked to hear. When a
        concrete engine is supplied by settings, resolve that engine and voice
        directly instead of rereading a possibly stale debounced preference.
        """
        try:
            config = self._config_provider()
            if engine is None:
                selection = self._resolve_selection(config)
            elif engine == "supertonic":
                selection = self._selection_policy.resolve(
                    engine,
                    supertonic_voice=(
                        getattr(config, "agent_speech_voice", 1)
                        if voice is None
                        else voice
                    ),
                    kokoro_voice=getattr(
                        config, "agent_speech_kokoro_voice", "af_heart"
                    ),
                )
            elif engine == "kokoro":
                selection = self._selection_policy.resolve(
                    engine,
                    supertonic_voice=getattr(config, "agent_speech_voice", 1),
                    kokoro_voice=(
                        getattr(config, "agent_speech_kokoro_voice", "af_heart")
                        if voice is None
                        else voice
                    ),
                )
            else:
                return
            if not selection.can_speak:
                return
            speed = float(getattr(config, "agent_speech_speed", 1.0))
            volume = float(getattr(config, "agent_speech_volume", 1.0))
            self._next_generation()
            self._speak_with_selection(
                SAMPLE_TEXT,
                selection,
                speed=speed,
                volume=volume,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise public surface
            self._logger.warning(
                "[Speech] speak_sample error reason=%s", type(exc).__name__
            )

    def speak_text(
        self,
        text: str,
        *,
        engine: Optional[str] = None,
        voice: object = None,
    ) -> None:
        """Speak arbitrary caller-supplied text at the current voice/speed/volume.

        Never raises. Mirrors :meth:`speak_sample`'s engine/voice resolution
        branches exactly (auto vs explicit supertonic/kokoro via
        ``_selection_policy``), but speaks ``text`` instead of the fixed
        :data:`SAMPLE_TEXT` line. Used by the onboarding voice-speed test box
        (Task 1.1) so the user can hear their own words in the selected
        voice/speed/volume before finishing setup. Fire-and-forget and
        non-blocking (playback spawns its own worker); claims newest-wins
        ownership the same way :meth:`speak_sample` does.

        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # ``text`` is user-typed content, not a fixed product string like
        # SAMPLE_TEXT. Never log it or any string derived from it. Metadata only
        # (reason=<type>) is fine on the exception path.
        # ============================================================================
        """
        try:
            config = self._config_provider()
            if engine is None:
                selection = self._resolve_selection(config)
            elif engine == "supertonic":
                selection = self._selection_policy.resolve(
                    engine,
                    supertonic_voice=(
                        getattr(config, "agent_speech_voice", 1)
                        if voice is None
                        else voice
                    ),
                    kokoro_voice=getattr(
                        config, "agent_speech_kokoro_voice", "af_heart"
                    ),
                )
            elif engine == "kokoro":
                selection = self._selection_policy.resolve(
                    engine,
                    supertonic_voice=getattr(config, "agent_speech_voice", 1),
                    kokoro_voice=(
                        getattr(config, "agent_speech_kokoro_voice", "af_heart")
                        if voice is None
                        else voice
                    ),
                )
            else:
                return
            if not selection.can_speak:
                return
            speed = float(getattr(config, "agent_speech_speed", 1.0))
            volume = float(getattr(config, "agent_speech_volume", 1.0))
            self._next_generation()
            self._speak_with_selection(
                text,
                selection,
                speed=speed,
                volume=volume,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise public surface
            self._logger.warning(
                "[Speech] speak_text error reason=%s", type(exc).__name__
            )

    def speak_event(self, kind: str, agent: str = "claude", detail: "Optional[str]" = None) -> None:
        """Speak the deterministic template for a conversational event. Never raises.

        The spoken line comes from ``agent_events.template_for`` ONLY (fixed
        product copy, never payload-derived text), so nothing here is
        transcript data. Policy, in order:

        - Mode gate: any template requires ``agent_speech_mode != "off"``;
          ``progress`` additionally requires the ``conversational`` mode.
        - Voice model installed (:meth:`can_speak`).
        - Never above barge-in: progress is dropped while recording; other
          templates wait in the normal FIFO until recording is idle plus the
          configured gap.
        - Debounce: per-kind minimum interval (``agent_events.SpeechDebounce``)
          collapses rapid repeats.
        - Priority: ``needs_input`` and ``error`` join the normal FIFO;
          ``progress`` uses ``speak_if_idle`` and is dropped (not queued) when
          anything else is speaking, queued, or about to speak.
        """
        try:
            config = self._config_provider()
            mode = getattr(config, "agent_speech_mode", "off")
            if not self._speech_enabled(config):
                return
            if kind == "progress" and mode != "conversational":
                return
            selection = self._resolve_selection(config)
            if not selection.can_speak:
                return
            text = template_for(agent, kind, detail)
            if not text:
                return
            if not self._debounce.allow(kind):
                return

            speed = float(getattr(config, "agent_speech_speed", 1.0))
            volume = float(getattr(config, "agent_speech_volume", 1.0))

            if kind == "progress":
                # Low priority: never interrupts, never cancels a pending
                # summary (no generation bump). Dropped when busy or recording.
                if self._is_recording() or self._normal_queue_is_busy():
                    return
                self._speak_if_idle_with_selection(
                    text,
                    selection,
                    speed=speed,
                    volume=volume,
                )
            else:
                # Ordinary announcements serialize with summaries/questions.
                self._next_generation()
                self._enqueue_normal_utterance(
                    _QueuedUtterance(
                        text=text,
                        selection=selection,
                        speed=speed,
                        volume=volume,
                        completion_id=None,
                        on_terminal=None,
                    )
                )
            self._logger.info("[Speech] speak_event kind=%s agent=%s", kind, agent)
        except Exception as exc:  # noqa: BLE001 -- never-raise public surface
            self._logger.warning(
                "[Speech] speak_event error reason=%s", type(exc).__name__
            )

    def stop(self) -> None:
        """Stop any current or pending speech immediately. Non-blocking; never raises.

        Called from the pynput hotkey thread under ``WhisperstreamApp._lock`` (PTT
        barge-in). Bumps the generation so an in-flight ``speak_response`` awaiting a
        summary discards its result, then delegates to :meth:`PlaybackOwner.stop`
        (which aborts the active stream without joining any thread). Safe before any
        speak.
        """
        callbacks: list[
            tuple[Optional[Callable[[str], None]], str]
        ] = []
        try:
            callbacks = self._cancel_normal_queue(mark_shutdown=False)
            self._playback.stop()
        except Exception as exc:  # noqa: BLE001 -- never-raise public surface
            self._logger.warning(
                "[Speech] speak stop error reason=%s", type(exc).__name__
            )
        finally:
            for callback, reason in callbacks:
                self._notify_terminal(callback, reason)

    def reset_queue_delay(self) -> None:
        """Honor an explicit response skip without carrying the old quiet gap.

        The coordinator calls this after stopping the old voice and cancelling
        capture, while successor promotion is barred. Natural completion and
        ordinary stop retain their configured pacing. Recording still gates
        playback through the live recording provider.
        """
        with self._queue_condition:
            self._last_normal_terminal_at = None
            self._recording_idle_at = None
            self._queue_condition.notify_all()

    def shutdown(self, timeout: float = 2.0) -> bool:
        """Bounded speech teardown for process shutdown. Never raises."""

        try:
            bounded_timeout = max(0.0, float(timeout))
            started_at = time.monotonic()
            callbacks = self._cancel_normal_queue(mark_shutdown=True)
            for callback, reason in callbacks:
                self._notify_terminal(callback, reason)

            worker = self._queue_worker
            if worker is not None and worker is not threading.current_thread():
                worker.join(
                    max(
                        0.0,
                        bounded_timeout - (time.monotonic() - started_at),
                    )
                )
            remaining = max(
                0.0,
                bounded_timeout - (time.monotonic() - started_at),
            )
            playback_stopped = bool(self._playback.shutdown(remaining))
            return playback_stopped and (worker is None or not worker.is_alive())
        except Exception as exc:  # noqa: BLE001 -- teardown is never-raise
            self._logger.warning(
                "[Speech] shutdown error reason=%s",
                type(exc).__name__,
            )
            return False

    # -- internal helpers ---------------------------------------------------

    def _speech_enabled(self, config: object) -> bool:
        """Read the normalized master toggle with a legacy-object fallback."""
        provider = getattr(self, "_speech_enabled_provider", None)
        if callable(provider):
            try:
                return bool(provider())
            except Exception:
                pass
        if hasattr(config, "agent_speech_enabled"):
            return bool(getattr(config, "agent_speech_enabled"))
        return getattr(config, "agent_speech_mode", "off") != "off"

    def _is_recording(self) -> bool:
        """True when a dictation recording is live. Never raises."""
        if self._recording_provider is None:
            return False
        try:
            return bool(self._recording_provider())
        except Exception:  # noqa: BLE001 -- probe must never break speech
            return False

    def _next_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def _is_current(self, gen: int) -> bool:
        with self._lock:
            return self._generation == gen

    def _queue_delay_seconds(self) -> float:
        """Read the validated normal-utterance gap without raising."""

        try:
            config = self._config_provider()
            if not hasattr(config, "claude_reply_queue_delay_seconds"):
                # Legacy in-process objects predate the setting and historically
                # started immediately.
                return 0.0
            raw = getattr(config, "claude_reply_queue_delay_seconds")
        except Exception:  # noqa: BLE001 -- queue policy must fail soft
            return 2.0
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not 0 <= raw <= 10
        ):
            return 2.0
        return float(raw)

    def _ensure_queue_worker_locked(self) -> None:
        worker = self._queue_worker
        if worker is not None and worker.is_alive():
            return
        try:
            worker = threading.Thread(
                target=self._run_normal_queue,
                name="speech-queue",
                daemon=True,
            )
            self._queue_worker = worker
            worker.start()
        except Exception:
            self._queue_worker = None
            raise

    def _enqueue_normal_utterance(self, item: _QueuedUtterance) -> bool:
        """Retain one normal utterance and start it when FIFO policy permits."""

        callback: Optional[Callable[[str], None]] = None
        with self._queue_condition:
            if self._queue_shutdown:
                item.terminal = True
                callback = item.on_terminal
            else:
                self._normal_queue.append(item)
                try:
                    self._ensure_queue_worker_locked()
                    self._try_start_normal_locked(time.monotonic())
                except Exception:
                    self._normal_queue.remove(item)
                    item.terminal = True
                    callback = item.on_terminal
                    self._queue_condition.notify_all()
                else:
                    self._queue_condition.notify_all()
                    return True
        self._notify_terminal(callback, "failed" if callback else "unavailable")
        return False

    def _normal_queue_is_busy(self) -> bool:
        with self._lock:
            return self._normal_active is not None or bool(self._normal_queue)

    def _run_normal_queue(self) -> None:
        """Wait for recording/gap gates and start one normal utterance at a time."""

        while True:
            with self._queue_condition:
                if self._queue_shutdown:
                    return
                if self._normal_active is not None or not self._normal_queue:
                    self._queue_condition.wait()
                    continue
                now = time.monotonic()
                if self._try_start_normal_locked(now):
                    continue
                # Poll the recording state and live gap setting without owning
                # a second timer lifecycle. Fifty milliseconds is well below the
                # user-configurable whole-second gap while remaining cheap.
                self._queue_condition.wait(0.05)

    def _try_start_normal_locked(self, now: float) -> bool:
        """Start the FIFO head when recording and minimum-gap gates permit."""

        if (
            self._queue_shutdown
            or self._normal_active is not None
            or not self._normal_queue
        ):
            return False

        if self._is_recording():
            self._recording_blocked = True
            self._recording_idle_at = None
            return False
        if self._recording_blocked:
            self._recording_blocked = False
            self._recording_idle_at = now

        delay = self._queue_delay_seconds()
        not_before = now
        if self._last_normal_terminal_at is not None:
            not_before = max(not_before, self._last_normal_terminal_at + delay)
        if self._recording_idle_at is not None:
            not_before = max(not_before, self._recording_idle_at + delay)
        if now < not_before:
            return False

        item = self._normal_queue.popleft()
        self._normal_active = item
        self._recording_idle_at = None
        callback = lambda reason: self._normal_terminal(item, reason)
        try:
            self._speak_with_selection(
                item.text,
                item.selection,
                speed=item.speed,
                volume=item.volume,
                completion_id=item.completion_id,
                on_terminal=callback,
            )
        except Exception as exc:  # noqa: BLE001 -- queue worker is never-raise
            self._logger.warning(
                "[Speech] queued playback error reason=%s",
                type(exc).__name__,
            )
            callback("failed")
        return True

    def _normal_terminal(self, item: _QueuedUtterance, reason: str) -> None:
        """Resolve one queued item exactly once and release the FIFO."""

        if reason not in {
            "completed",
            "stopped",
            "superseded",
            "failed",
            "unavailable",
        }:
            reason = "failed"
        callback: Optional[Callable[[str], None]] = None
        with self._queue_condition:
            if item.terminal:
                return
            item.terminal = True
            callback = item.on_terminal
            if self._normal_active is item:
                self._normal_active = None
                self._last_normal_terminal_at = time.monotonic()
            self._queue_condition.notify_all()
        self._notify_terminal(callback, reason)

    def _cancel_normal_queue(
        self,
        *,
        mark_shutdown: bool,
    ) -> list[tuple[Optional[Callable[[str], None]], str]]:
        """Atomically flush active/pending normal callbacks for stop/shutdown."""

        callbacks: list[tuple[Optional[Callable[[str], None]], str]] = []
        with self._queue_condition:
            self._generation += 1
            if mark_shutdown:
                self._queue_shutdown = True
            items = list(self._normal_queue)
            self._normal_queue.clear()
            had_active = self._normal_active is not None
            if self._normal_active is not None:
                items.insert(0, self._normal_active)
                self._normal_active = None
            for item in items:
                if item.terminal:
                    continue
                item.terminal = True
                callbacks.append((item.on_terminal, "stopped"))
            self._recording_blocked = False
            self._recording_idle_at = None
            if had_active:
                self._last_normal_terminal_at = time.monotonic()
            self._queue_condition.notify_all()
        return callbacks

    def _resolve_selection(self, config: object) -> SelectionResolution:
        return self._selection_policy.resolve(
            getattr(config, "agent_speech_engine", "auto"),
            supertonic_voice=getattr(config, "agent_speech_voice", 1),
            kokoro_voice=getattr(config, "agent_speech_kokoro_voice", "af_heart"),
        )

    @staticmethod
    def _is_legacy_supertonic(selection: SelectionResolution) -> bool:
        return (
            len(selection.candidates) == 1
            and selection.candidates[0].engine_key == "supertonic"
        )

    def _speak_with_selection(
        self,
        text: str,
        selection: SelectionResolution,
        *,
        speed: float,
        volume: float,
        completion_id: Optional[str] = None,
        on_terminal: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.reconcile_residency()
        kwargs: Dict[str, object] = {
            "speed": speed,
            "volume": volume,
        }
        if self._is_legacy_supertonic(selection):
            kwargs["sid"] = selection.candidates[0].voice_id
        else:
            kwargs["candidates"] = selection.candidates
        if completion_id is not None:
            kwargs["completion_id"] = completion_id
        if on_terminal is not None:
            kwargs["on_terminal"] = on_terminal
        self._playback.speak(text, **kwargs)

    def _speak_if_idle_with_selection(
        self,
        text: str,
        selection: SelectionResolution,
        *,
        speed: float,
        volume: float,
    ) -> bool:
        self.reconcile_residency()
        kwargs: Dict[str, object] = {
            "speed": speed,
            "volume": volume,
        }
        if self._is_legacy_supertonic(selection):
            kwargs["sid"] = selection.candidates[0].voice_id
        else:
            kwargs["candidates"] = selection.candidates
        return bool(self._playback.speak_if_idle(text, **kwargs))

    def _notify_terminal(
        self,
        callback: Optional[Callable[[str], None]],
        reason: str,
    ) -> None:
        if callback is None:
            return
        try:
            callback(reason)
        except Exception as exc:  # noqa: BLE001 -- never-raise callback seam
            self._logger.warning(
                "[Speech] terminal callback error reason=%s",
                type(exc).__name__,
            )
