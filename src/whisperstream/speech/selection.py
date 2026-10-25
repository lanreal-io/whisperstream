"""Backend-authoritative speech-engine selection and lazy lifecycle ownership."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, Optional, Protocol, TypeAlias

from ..system_info import get_available_ram_mb, get_total_ram_mb
from .engine_specs import KOKORO_SPEC, SUPERTONIC_SPEC, VoiceId, get_engine_spec

__all__ = [
    "AVAILABLE_RAM_MIN_MB",
    "KOKORO_OVERRIDE_AVAILABLE_RAM_MIN_MB",
    "KOKORO_OVERRIDE_TOTAL_RAM_MIN_MB",
    "LOW_MEMORY_HOLD_SECONDS",
    "EngineCandidate",
    "EngineLease",
    "EngineManager",
    "MemorySnapshot",
    "SelectionPolicy",
    "SelectionResolution",
    "SpeechSelection",
    "classify_kokoro_memory",
    "normalize_selection",
]

AVAILABLE_RAM_MIN_MB = 2 * 1024
# After Automatic falls back for low memory it keeps Supertonic this long
# instead of re-probing free memory every reply and flipping engines
# (owner decision, 2026-10-25).
LOW_MEMORY_HOLD_SECONDS = 600.0
KOKORO_OVERRIDE_TOTAL_RAM_MIN_MB = 8 * 1024
KOKORO_OVERRIDE_AVAILABLE_RAM_MIN_MB = 1024
DEFAULT_SPEECH_IDLE_TIMEOUT_SECONDS = 1800.0
SAMPLE_IDLE_TIMEOUT_SECONDS = 30.0

SpeechSelection: TypeAlias = Literal["auto", "kokoro", "supertonic"]
KokoroMemoryTier: TypeAlias = Literal[
    "recommended",
    "caution",
    "blocked",
    "unknown",
]


class _Engine(Protocol):
    @staticmethod
    def is_installed() -> bool: ...

    def unload(self) -> None: ...


class _TimerHandle(Protocol):
    def cancel(self) -> None: ...


@dataclass(frozen=True)
class MemorySnapshot:
    """Physical-memory values used for one deterministic selection."""

    total_mb: int | None
    available_mb: int | None


@dataclass(frozen=True)
class EngineCandidate:
    """One concrete engine and its engine-scoped saved voice."""

    engine_key: str
    voice_id: VoiceId


@dataclass(frozen=True)
class SelectionResolution:
    """Ordered candidates and the content-free reason for their ordering."""

    requested: SpeechSelection
    candidates: tuple[EngineCandidate, ...]
    reason: str

    @property
    def can_speak(self) -> bool:
        return bool(self.candidates)


def normalize_selection(value: object) -> SpeechSelection:
    """Normalize persisted external input, defaulting unknown values to Automatic."""

    from .availability import kokoro_available
    if not kokoro_available():
        return "supertonic"
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"auto", "kokoro", "supertonic"}:
            return normalized  # type: ignore[return-value]
    return "auto"


def classify_kokoro_memory(
    total_mb: int | None,
    available_mb: int | None,
) -> KokoroMemoryTier:
    """Classify a fresh memory snapshot for automatic or explicit Kokoro use.

    Automatic needs 2 GiB available, whatever the total (the 16 GiB total
    minimum was dropped, owner decision 2026-10-25). An explicit override is
    allowed with a warning down to 8 GiB total and 1 GiB available; below that
    floor the UI must not offer a continue action.
    """

    if total_mb is None or available_mb is None:
        return "unknown"
    if available_mb >= AVAILABLE_RAM_MIN_MB:
        return "recommended"
    if (
        total_mb >= KOKORO_OVERRIDE_TOTAL_RAM_MIN_MB
        and available_mb >= KOKORO_OVERRIDE_AVAILABLE_RAM_MIN_MB
    ):
        return "caution"
    return "blocked"


def _call_bool(target: object, name: str, *args: object) -> bool:
    """Call an optional manager probe; managers without it answer False."""

    probe = getattr(target, name, None)
    if not callable(probe):
        return False
    try:
        return bool(probe(*args))
    except Exception:  # noqa: BLE001 -- selection probes are never-raise
        return False


def _default_memory_probe() -> MemorySnapshot:
    return MemorySnapshot(
        total_mb=get_total_ram_mb(),
        available_mb=get_available_ram_mb(),
    )


def _default_scheduler(
    delay: float,
    callback: Callable[[], None],
) -> _TimerHandle:
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer


class EngineLease:
    """Exactly-once reference-counted worker lease."""

    def __init__(self, manager: "EngineManager", engine_key: str, engine: _Engine) -> None:
        self._manager = manager
        self.engine_key = engine_key
        self.engine = engine
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._manager._release(self.engine_key)  # noqa: SLF001

    def __enter__(self) -> _Engine:
        return self.engine

    def __exit__(self, *_args: object) -> None:
        self.release()


class EngineManager:
    """Cache lazy engines and keep model unloads clear of active workers."""

    def __init__(
        self,
        *,
        engine_factory: Optional[Callable[[str], _Engine]] = None,
        logger: Optional[logging.Logger] = None,
        scheduler: Optional[
            Callable[[float, Callable[[], None]], _TimerHandle]
        ] = None,
        idle_timeout: float = DEFAULT_SPEECH_IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        if engine_factory is None:
            def _make_engine(key: str) -> _Engine:
                from .engines import make_engine

                return make_engine(key, logger=self._logger)

            engine_factory = _make_engine
        self._engine_factory = engine_factory
        self._scheduler = scheduler or _default_scheduler
        self._idle_timeout = max(0.0, float(idle_timeout))
        self._clock = clock
        self._retained_engines = {"kokoro", "supertonic"}
        self._preferred: SpeechSelection = "auto"
        self._last_used: dict[str, float] = {}
        self._retire: set[str] = set()
        self._condition = threading.Condition(threading.Lock())
        self._engines: dict[str, _Engine] = {}
        self._leases: dict[str, int] = {}
        self._idle_handles: dict[str, _TimerHandle] = {}
        self._idle_epochs: dict[str, int] = {}
        self._unhealthy: dict[str, str] = {}
        self._low_memory_hold_until: float | None = None
        self._shutdown = False
        self._shutdown_waiting = False

    def engine(self, engine_key: str) -> _Engine:
        """Return the cached unloaded-or-loaded wrapper for a validated key."""

        get_engine_spec(engine_key)
        with self._condition:
            engine = self._engines.get(engine_key)
            if engine is None:
                engine = self._engine_factory(engine_key)
                self._engines[engine_key] = engine
            return engine

    def is_installed(self, engine_key: str) -> bool:
        """Check model presence without loading model weights. Never raises."""

        try:
            with self._condition:
                if self._shutdown:
                    return False
            return bool(self.engine(engine_key).is_installed())
        except Exception:  # noqa: BLE001 -- availability probe is never-raise
            return False

    def is_healthy(self, engine_key: str) -> bool:
        with self._condition:
            return engine_key not in self._unhealthy

    def health_reason(self, engine_key: str) -> str | None:
        with self._condition:
            return self._unhealthy.get(engine_key)

    def is_available(self, engine_key: str) -> bool:
        return self.is_healthy(engine_key) and self.is_installed(engine_key)

    def is_resident(self, engine_key: str) -> bool:
        """Whether the engine is in use or warm (acquired, not yet unloaded).

        A resident model's memory is already spent, so Automatic counts it as
        available instead of probing free memory against itself.
        """

        with self._condition:
            return not self._shutdown and engine_key in self._last_used

    def hold_low_memory_fallback(self) -> None:
        """Keep Automatic on Supertonic for ``LOW_MEMORY_HOLD_SECONDS``."""

        with self._condition:
            self._low_memory_hold_until = self._clock() + LOW_MEMORY_HOLD_SECONDS

    def low_memory_hold_active(self) -> bool:
        with self._condition:
            until = self._low_memory_hold_until
            if until is None:
                return False
            if self._clock() >= until:
                self._low_memory_hold_until = None
                return False
            return True

    def mark_unhealthy(self, engine_key: str, reason: str) -> None:
        """Make an engine ineligible for Automatic until a direct retry succeeds."""

        with self._condition:
            self._unhealthy.setdefault(engine_key, reason)
        self._logger.warning(
            "[Speech] engine unhealthy engine=%s reason=%s",
            engine_key,
            reason,
        )

    def mark_runtime_unavailable(self, engine_key: str, reason: str) -> None:
        """Install a stable platform gate without logging a runtime failure."""

        get_engine_spec(engine_key)
        with self._condition:
            self._unhealthy.setdefault(engine_key, reason)

    def mark_healthy(self, engine_key: str) -> None:
        """Clear an Automatic health gate after a successful direct synthesis."""

        with self._condition:
            previous = self._unhealthy.pop(engine_key, None)
        if previous is not None:
            self._logger.info(
                "[Speech] engine health restored engine=%s previous_reason=%s",
                engine_key,
                previous,
            )

    def acquire(self, engine_key: str) -> EngineLease:
        """Acquire one worker reference without loading the engine."""

        engine = self.engine(engine_key)
        with self._condition:
            if self._shutdown:
                raise RuntimeError("speech engine manager is shut down")
            self._cancel_idle_locked(engine_key)
            self._retire.discard(engine_key)
            self._last_used[engine_key] = self._clock()
            self._leases[engine_key] = self._leases.get(engine_key, 0) + 1
        return EngineLease(self, engine_key, engine)

    def configure_residency(
        self, *, enabled: bool, preferred: SpeechSelection, idle_timeout: float
    ) -> None:
        """Apply saved preferences without extending an existing idle deadline."""

        retained = set()
        if enabled:
            retained = {"kokoro", "supertonic"} if preferred == "auto" else {preferred}
        timeout = max(0.0, float(idle_timeout))
        with self._condition:
            if self._shutdown or (
                retained == self._retained_engines
                and timeout == self._idle_timeout
                and preferred == self._preferred
            ):
                return
            self._retire.update((self._retained_engines - retained) & self._last_used.keys())
            if preferred != self._preferred and preferred != "auto":
                self._retire.update(self._last_used.keys() - {preferred})
            self._retire.difference_update(retained - self._retained_engines)
            self._retained_engines = retained
            self._preferred = preferred
            self._idle_timeout = timeout
            for key in self._last_used:
                if not self._leases.get(key):
                    self._schedule_idle_locked(key)

    def request_unload(self) -> None:
        """Release resident models asynchronously once their workers finish."""

        with self._condition:
            if self._shutdown:
                return
            self._retire.update(self._last_used)
            for key in self._last_used:
                if not self._leases.get(key):
                    self._schedule_idle_locked(key)

    def _release(self, engine_key: str) -> None:
        unload_now: _Engine | None = None
        final_unload = False
        with self._condition:
            count = self._leases.get(engine_key, 0)
            if count <= 1:
                self._leases.pop(engine_key, None)
                if self._shutdown:
                    if not self._shutdown_waiting:
                        unload_now = self._engines.get(engine_key)
                        final_unload = True
                else:
                    self._last_used[engine_key] = self._clock()
                    self._schedule_idle_locked(engine_key)
                self._condition.notify_all()
            else:
                self._leases[engine_key] = count - 1
        if unload_now is not None:
            self._safe_unload(engine_key, unload_now, final=final_unload)

    def _cancel_idle_locked(self, engine_key: str) -> None:
        self._idle_epochs[engine_key] = self._idle_epochs.get(engine_key, 0) + 1
        handle = self._idle_handles.pop(engine_key, None)
        if handle is not None:
            try:
                handle.cancel()
            except Exception:  # noqa: BLE001 -- cancellation is best-effort
                pass

    def _schedule_idle_locked(self, engine_key: str) -> None:
        self._cancel_idle_locked(engine_key)
        if engine_key in self._retire:
            delay = 0.0
        else:
            timeout = (
                self._idle_timeout
                if engine_key in self._retained_engines
                else SAMPLE_IDLE_TIMEOUT_SECONDS
            )
            if timeout == 0:
                return
            delay = max(0.0, self._last_used[engine_key] + timeout - self._clock())
        epoch = self._idle_epochs[engine_key]
        handle = self._scheduler(
            delay,
            lambda: self._idle_unload(engine_key, epoch),
        )
        self._idle_handles[engine_key] = handle

    def _idle_unload(self, engine_key: str, epoch: int) -> None:
        with self._condition:
            if (
                self._shutdown
                or self._idle_epochs.get(engine_key) != epoch
                or self._leases.get(engine_key, 0) != 0
            ):
                return
            self._idle_handles.pop(engine_key, None)
            engine = self._engines.get(engine_key)
            # Keep acquisition excluded until unload completes. Otherwise a new
            # lease could begin after the zero-reference check but before the
            # old native session is actually released.
            if engine is not None:
                self._safe_unload(engine_key, engine)
            self._last_used.pop(engine_key, None)
            self._retire.discard(engine_key)

    def shutdown(self, timeout: float = 2.0) -> bool:
        """Wait at most *timeout* for leases, unloading only unleased engines."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            self._shutdown = True
            self._shutdown_waiting = True
            for key in tuple(self._idle_handles):
                self._cancel_idle_locked(key)
            while self._leases:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            drained = not self._leases
            unload = [
                (key, engine)
                for key, engine in self._engines.items()
                if self._leases.get(key, 0) == 0
            ]
            self._shutdown_waiting = False
        for key, engine in unload:
            self._safe_unload(key, engine, final=True)
        return drained

    def _safe_unload(
        self, engine_key: str, engine: _Engine, *, final: bool = False
    ) -> None:
        # Shutdown is final disposal; idle release may keep engine-owned
        # immutable data (Kokoro's frontend) resident for the next load.
        dispose = getattr(engine, "dispose", None) if final else None
        try:
            if callable(dispose):
                dispose()
            else:
                engine.unload()
        except Exception as exc:  # noqa: BLE001 -- lifecycle teardown is best-effort
            self._logger.warning(
                "[Speech] engine unload error engine=%s reason=%s",
                engine_key,
                type(exc).__name__,
            )


class SelectionPolicy:
    """Resolve one immutable ordered candidate list per utterance."""

    def __init__(
        self,
        manager: EngineManager,
        *,
        memory_probe: Optional[Callable[[], MemorySnapshot]] = None,
        hold_on_fallback: bool = True,
    ) -> None:
        self._manager = manager
        self._memory_probe = memory_probe or _default_memory_probe
        # Status reads resolve without starting a hold; only speech does.
        self._hold_on_fallback = hold_on_fallback

    def resolve(
        self,
        requested: object,
        *,
        supertonic_voice: object,
        kokoro_voice: object,
    ) -> SelectionResolution:
        selection = normalize_selection(requested)
        st_voice = SUPERTONIC_SPEC.normalize_voice_id(supertonic_voice)
        kokoro_voice_id = KOKORO_SPEC.normalize_voice_id(kokoro_voice)

        if selection == "supertonic":
            candidates = (
                (EngineCandidate(SUPERTONIC_SPEC.key, st_voice),)
                if self._manager.is_available(SUPERTONIC_SPEC.key)
                else ()
            )
            return SelectionResolution(selection, candidates, "supertonic")

        if selection == "kokoro":
            if not self._manager.is_installed(KOKORO_SPEC.key):
                return SelectionResolution(selection, (), "missing_kokoro")
            # An explicit choice is also the user's recovery path after Automatic
            # observed a transient load failure or a slow first sentence. It never
            # redirects to Supertonic, and one successful synthesis clears the
            # process health gate so a later Automatic utterance may try again.
            reason = "kokoro"
            if not self._manager.is_healthy(KOKORO_SPEC.key):
                reason = (
                    "slow_kokoro"
                    if self._manager.health_reason(KOKORO_SPEC.key)
                    == "slow_first_sentence"
                    else "unhealthy_kokoro"
                )
            return SelectionResolution(
                selection,
                (EngineCandidate(KOKORO_SPEC.key, kokoro_voice_id),),
                reason,
            )

        fallback = (
            (EngineCandidate(SUPERTONIC_SPEC.key, st_voice),)
            if self._manager.is_available(SUPERTONIC_SPEC.key)
            else ()
        )
        # A warm Kokoro already holds its memory: switching away would only
        # add Supertonic's. Otherwise a recent low-memory fallback holds, so
        # Automatic does not flip engines reply to reply.
        if not _call_bool(self._manager, "is_resident", KOKORO_SPEC.key):
            if fallback and _call_bool(self._manager, "low_memory_hold_active"):
                return SelectionResolution(selection, fallback, "low_memory")
            memory = self._probe_memory()
            if memory.available_mb is None:
                return SelectionResolution(selection, fallback, "unknown_memory")
            if memory.available_mb < AVAILABLE_RAM_MIN_MB:
                if fallback and self._hold_on_fallback:
                    hold = getattr(self._manager, "hold_low_memory_fallback", None)
                    if callable(hold):
                        hold()
                return SelectionResolution(selection, fallback, "low_memory")
        if not self._manager.is_installed(KOKORO_SPEC.key):
            return SelectionResolution(selection, fallback, "missing_kokoro")
        if not self._manager.is_healthy(KOKORO_SPEC.key):
            reason = (
                "slow_kokoro"
                if self._manager.health_reason(KOKORO_SPEC.key)
                == "slow_first_sentence"
                else "unhealthy_kokoro"
            )
            return SelectionResolution(selection, fallback, reason)

        candidates = (EngineCandidate(KOKORO_SPEC.key, kokoro_voice_id),) + fallback
        return SelectionResolution(selection, candidates, "kokoro")

    def _probe_memory(self) -> MemorySnapshot:
        try:
            snapshot = self._memory_probe()
            if isinstance(snapshot, MemorySnapshot):
                return snapshot
        except Exception:  # noqa: BLE001 -- system probe is never-raise
            pass
        return MemorySnapshot(None, None)
