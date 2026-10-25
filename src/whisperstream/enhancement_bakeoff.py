"""Memory-only orchestration for sequential enhancement Bakeoff runs.

The session manager deliberately knows nothing about transcript persistence,
HTTP routes, or WebSocket payloads. Its production pair executor snapshots the
enhancement configuration and local model metadata, then binds each pair to an
explicit resident supervisor. Progress callbacks contain lifecycle metadata
only; result text stays behind :meth:`EnhancementBakeoffManager.snapshot`.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .config_manager import WhisperstreamConfig
from .enhancement_models import get_local_model, resolve_local_model_path
from .llm_enhancer import LLMEnhancer, resolve_serving_contract
from .resident_llama_pool import ResidentLlamaLoadError


MAX_BAKEOFF_PAIRS = 8
MAX_BAKEOFF_SESSIONS = 4
MAX_BAKEOFF_RETAINED_CHARS = 800_000
_MAX_ID_CHARS = 256
_MAX_ERROR_CODE_CHARS = 64


class EvaluationKind(StrEnum):
    """The two mutually exclusive enhancement workloads."""

    NORMAL = "normal"
    BAKEOFF = "bakeoff"


@dataclass(frozen=True, slots=True)
class EvaluationLeaseToken:
    """Exact capability returned by :class:`EvaluationLease`."""

    kind: EvaluationKind
    generation: int


class EvaluationLease:
    """Thread-safe, non-blocking ownership for one enhancement workload.

    Acquisition and release are synchronous so recording threads can inspect
    the lease without retaining the app's broader lifecycle lock while async
    Bakeoff work is running.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._active: EvaluationLeaseToken | None = None

    def try_acquire(self, kind: EvaluationKind) -> EvaluationLeaseToken | None:
        if not isinstance(kind, EvaluationKind):
            raise TypeError("kind must be an EvaluationKind")
        with self._lock:
            if self._active is not None:
                return None
            self._generation += 1
            self._active = EvaluationLeaseToken(kind, self._generation)
            return self._active

    def release(self, token: EvaluationLeaseToken) -> bool:
        if not isinstance(token, EvaluationLeaseToken):
            return False
        with self._lock:
            if self._active != token:
                return False
            self._active = None
            return True

    def revoke(self, kind: EvaluationKind | None = None) -> bool:
        """Synchronously invalidate the active token, optionally by kind."""

        with self._lock:
            if self._active is None:
                return False
            if kind is not None and self._active.kind is not kind:
                return False
            self._active = None
            self._generation += 1
            return True

    def active_kind(self) -> EvaluationKind | None:
        with self._lock:
            return self._active.kind if self._active is not None else None

    def is_active(self, kind: EvaluationKind | None = None) -> bool:
        with self._lock:
            return bool(
                self._active is not None
                and (kind is None or self._active.kind is kind)
            )


class BakeoffPairState(StrEnum):
    QUEUED = "queued"
    LOADING = "loading"
    RUNNING = "running"
    COMPLETE = "complete"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STATES = frozenset(
    {
        BakeoffPairState.COMPLETE,
        BakeoffPairState.UNCHANGED,
        BakeoffPairState.FAILED,
        BakeoffPairState.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class BakeoffPairSelection:
    model_id: str
    profile_id: str

    def __post_init__(self) -> None:
        _bounded_id("model_id", self.model_id)
        _bounded_id("profile_id", self.profile_id)


@dataclass(frozen=True, slots=True)
class BakeoffExecutionMetadata:
    """Content-free execution facts shared by progress and result snapshots."""

    serving_contract: str | None = None
    device_mode: str | None = None
    resident_warm: bool | None = None
    load_ms: int | None = None
    inference_ms: int | None = None
    execution_mode: str | None = None
    chunk_count: int | None = None

    def __post_init__(self) -> None:
        if self.serving_contract is not None:
            _bounded_id("serving_contract", self.serving_contract, max_chars=64)
        if self.device_mode not in {None, "cpu", "gpu"}:
            raise ValueError("device_mode must be cpu, gpu, or None")
        if self.resident_warm is not None and not isinstance(
            self.resident_warm, bool
        ):
            raise TypeError("resident_warm must be a bool or None")
        _optional_nonnegative_int("load_ms", self.load_ms)
        _optional_nonnegative_int("inference_ms", self.inference_ms)
        if self.execution_mode not in {None, "whole", "batched"}:
            raise ValueError("execution_mode must be whole, batched, or None")
        if self.chunk_count is not None and (
            isinstance(self.chunk_count, bool)
            or not isinstance(self.chunk_count, int)
            or self.chunk_count < 1
        ):
            raise ValueError("chunk_count must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class BakeoffExecutionResult:
    """One executor result retained only in the manager's bounded memory."""

    output: str
    metadata: BakeoffExecutionMetadata = field(
        default_factory=BakeoffExecutionMetadata
    )

    def __post_init__(self) -> None:
        if not isinstance(self.output, str):
            raise TypeError("output must be a string")
        if not isinstance(self.metadata, BakeoffExecutionMetadata):
            raise TypeError("metadata must be BakeoffExecutionMetadata")


@dataclass(frozen=True, slots=True)
class BakeoffPairSnapshot:
    selection: BakeoffPairSelection
    state: BakeoffPairState
    output: str | None
    error_code: str | None
    metadata: BakeoffExecutionMetadata


@dataclass(frozen=True, slots=True)
class BakeoffSessionSnapshot:
    run_id: str
    generation: int
    raw_text: str
    pairs: tuple[BakeoffPairSnapshot, ...]
    active: bool

    @property
    def finished_count(self) -> int:
        return sum(pair.state in _TERMINAL_STATES for pair in self.pairs)


@dataclass(frozen=True, slots=True)
class BakeoffProgressEvent:
    """Metadata-only notification. Raw input and output are intentionally absent."""

    run_id: str
    generation: int
    pair_index: int
    state: BakeoffPairState
    finished_count: int
    total_count: int
    error_code: str | None
    metadata: BakeoffExecutionMetadata


class BakeoffBusyError(RuntimeError):
    """Raised when ordinary or Bakeoff evaluation already owns the lease."""


class BakeoffCapacityError(RuntimeError):
    """Raised when bounded session memory cannot admit another raw input."""


class BakeoffExecutionError(RuntimeError):
    """Content-free, stable pair failure supplied by an executor."""

    def __init__(self, code: str) -> None:
        _bounded_id("code", code, max_chars=_MAX_ERROR_CODE_CHARS)
        self.code = code
        super().__init__(code)


RunningReporter = Callable[[BakeoffExecutionMetadata | None], bool]
PairExecutor = Callable[
    [BakeoffPairSelection, str, RunningReporter],
    Awaitable[BakeoffExecutionResult],
]
ProgressCallback = Callable[[BakeoffProgressEvent], None]
EvaluationRunner = Callable[..., Awaitable[tuple[str, int]]]


class ProductionBakeoffPairExecutor:
    """Run explicit local model/profile pairs through production evaluation.

    One instance belongs to one Bakeoff run. It owns deep snapshots of the
    enhancement config and every selected catalog row, while resident process
    ownership stays with ``ResidentLlamaPool``. The executor is sequentially
    invoked by :class:`EnhancementBakeoffManager`.
    """

    def __init__(
        self,
        *,
        config: WhisperstreamConfig,
        selections: Sequence[BakeoffPairSelection],
        resident_pool: object,
        production_supervisor_provider: Callable[[], object | None],
        logger: logging.Logger | None = None,
        simulated_tps_provider: Callable[[], float | None] | None = None,
        evaluation_runner: EvaluationRunner | None = None,
    ) -> None:
        if not isinstance(config, WhisperstreamConfig):
            raise TypeError("config must be a WhisperstreamConfig")
        if resident_pool is None:
            raise RuntimeError("resident llama pool is unavailable")
        if not callable(production_supervisor_provider):
            raise TypeError("production_supervisor_provider must be callable")
        if evaluation_runner is not None and not callable(evaluation_runner):
            raise TypeError("evaluation_runner must be callable or None")

        self._config = copy.deepcopy(config)
        self._resident_pool = resident_pool
        self._production_supervisor_provider = production_supervisor_provider
        self._logger = logger
        self._simulated_tps_provider = simulated_tps_provider
        self._evaluation_runner = evaluation_runner
        self._production_model_path: Path | None = None
        self._production_catalog_ram_mb: int | float = 0
        production_model = get_local_model(self._config.enhancement_local_model)
        production_path = (
            resolve_local_model_path(production_model)
            if production_model is not None
            else None
        )
        try:
            production_available = bool(
                production_path is not None
                and Path(production_path).is_file()
                and Path(production_path).stat().st_size > 0
            )
        except OSError:
            production_available = False
        if production_available and production_model is not None:
            self._production_model_path = Path(production_path)
            self._production_catalog_ram_mb = production_model.get(
                "ram_mb", 0
            )
        self._models: dict[
            str,
            tuple[dict[str, object], Path] | None,
        ] = {}
        for selection in selections:
            if selection.model_id in self._models:
                continue
            model = get_local_model(selection.model_id)
            model_snapshot = (
                copy.deepcopy(dict(model)) if model is not None else None
            )
            model_path = (
                resolve_local_model_path(model_snapshot)
                if model_snapshot is not None
                else None
            )
            self._models[selection.model_id] = (
                (model_snapshot, Path(model_path))
                if model_snapshot is not None and model_path is not None
                else None
            )

    async def __call__(
        self,
        selection: BakeoffPairSelection,
        raw_text: str,
        mark_running: RunningReporter,
    ) -> BakeoffExecutionResult:
        # ======================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never log raw_text, model output, or exceptions from evaluation.
        # ======================================================================
        binding = self._models.get(selection.model_id)
        if binding is None:
            raise BakeoffExecutionError("model_unavailable")
        model, model_path = binding

        pool = self._resident_pool
        prior_resident = pool.find_resident(model_path)
        prior_ready = bool(
            prior_resident is not None
            and getattr(prior_resident.supervisor, "is_ready", False)
        )
        production_supervisor = self._production_supervisor_provider()
        production_ready = bool(
            production_supervisor is not None
            and getattr(production_supervisor, "is_ready", False)
        )
        catalog_ram_mb = model.get("ram_mb", 0)
        try:
            resident = await pool.acquire(
                model_id=str(model["id"]),
                model_path=model_path,
                catalog_ram_mb=catalog_ram_mb,
                production_supervisor=production_supervisor,
                production_model_path=self._production_model_path,
                production_catalog_ram_mb=self._production_catalog_ram_mb,
            )
        except ResidentLlamaLoadError as exc:
            raise BakeoffExecutionError("model_load_failed") from exc

        resident_warm = prior_ready or bool(
            resident.supervisor is production_supervisor and production_ready
        )
        enhancer = LLMEnhancer.for_local_evaluation(
            self._config,
            model_metadata=model,
            llama_server=resident.supervisor,
            logger=self._logger,
            simulated_tps_provider=self._simulated_tps_provider,
        )
        target_words = self._config.enhancement_words_per_chunk
        execution_mode = (
            "batched"
            if self._config.enhancement_chunked_cleanup
            and enhancer.will_enhance(raw_text)
            and len(raw_text.split()) > target_words
            else "whole"
        )
        runtime = resident.metadata
        loaded_metadata = BakeoffExecutionMetadata(
            serving_contract=resolve_serving_contract(model),
            device_mode=runtime.device_mode,
            resident_warm=resident_warm,
            load_ms=runtime.load_duration_ms,
            execution_mode=execution_mode,
        )
        if not mark_running(loaded_metadata):
            raise asyncio.CancelledError

        runner = self._evaluation_runner
        if runner is None:
            # Imported lazily to avoid enhancement_bakeoff -> main ->
            # enhancement_bakeoff module initialization recursion.
            from .main import run_transcript_evaluation  # noqa: PLC0415

            runner = run_transcript_evaluation

        async with resident.inference_lock:
            started_at = time.perf_counter()
            output, chunk_count = await runner(
                enhancer,
                raw_text,
                mode=execution_mode,
                profile_id=selection.profile_id,
                target_words=target_words,
                limit_first_chunk=self._config.enhancement_limit_first_chunk,
            )
            inference_ms = int((time.perf_counter() - started_at) * 1000)

        return BakeoffExecutionResult(
            output=output,
            metadata=BakeoffExecutionMetadata(
                serving_contract=loaded_metadata.serving_contract,
                device_mode=loaded_metadata.device_mode,
                resident_warm=loaded_metadata.resident_warm,
                load_ms=loaded_metadata.load_ms,
                inference_ms=inference_ms,
                execution_mode=execution_mode,
                chunk_count=chunk_count,
            ),
        )


@dataclass(slots=True)
class _PairState:
    selection: BakeoffPairSelection
    state: BakeoffPairState = BakeoffPairState.QUEUED
    output: str | None = None
    error_code: str | None = None
    metadata: BakeoffExecutionMetadata = field(
        default_factory=BakeoffExecutionMetadata
    )


@dataclass(slots=True)
class _Session:
    run_id: str
    generation: int
    raw_text: str
    pairs: list[_PairState]
    task: asyncio.Task[None] | None = None
    loop: asyncio.AbstractEventLoop | None = None


class EnhancementBakeoffManager:
    """Own bounded, ordered, generation-scoped Bakeoff sessions in memory."""

    def __init__(
        self,
        *,
        lease: EvaluationLease,
        progress_callback: ProgressCallback | None = None,
        max_sessions: int = MAX_BAKEOFF_SESSIONS,
        max_retained_chars: int = MAX_BAKEOFF_RETAINED_CHARS,
        max_pairs: int = MAX_BAKEOFF_PAIRS,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(lease, EvaluationLease):
            raise TypeError("lease must be an EvaluationLease")
        for name, value in {
            "max_sessions": max_sessions,
            "max_retained_chars": max_retained_chars,
            "max_pairs": max_pairs,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._lease = lease
        self._callback = progress_callback
        self._max_sessions = max_sessions
        self._max_retained_chars = max_retained_chars
        self._max_pairs = max_pairs
        self._run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex)
        self._lock = threading.RLock()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._next_generation = 1

    def set_progress_callback(
        self, callback: ProgressCallback | None
    ) -> None:
        if callback is not None and not callable(callback):
            raise TypeError("callback must be callable or None")
        with self._lock:
            self._callback = callback

    def start(
        self,
        *,
        raw_text: str,
        selections: Sequence[BakeoffPairSelection],
        execute_pair: PairExecutor,
    ) -> str:
        """Start a background sequential run and return its opaque run ID."""

        # ==========================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never log raw_text, evaluator output, session snapshots, or exceptions.
        # Progress callbacks below are deliberately metadata-only.
        # ==========================================================================
        safe_raw = _raw_text(raw_text)
        safe_selections = self._selections(selections)
        if not callable(execute_pair):
            raise TypeError("execute_pair must be callable")

        with self._lock:
            self._prune_locked(
                required_sessions=1,
                required_chars=len(safe_raw),
            )
            if (
                len(self._sessions) + 1 > self._max_sessions
                or self._retained_chars_locked() + len(safe_raw)
                > self._max_retained_chars
            ):
                raise BakeoffCapacityError("bakeoff session capacity exceeded")

            token = self._lease.try_acquire(EvaluationKind.BAKEOFF)
            if token is None:
                raise BakeoffBusyError("evaluation lease is active")
            try:
                loop = asyncio.get_running_loop()
                run_id = self._new_run_id_locked()
                generation = self._allocate_generation_locked()
                session = _Session(
                    run_id=run_id,
                    generation=generation,
                    raw_text=safe_raw,
                    pairs=[_PairState(selection=item) for item in safe_selections],
                    loop=loop,
                )
                self._sessions[run_id] = session
                task = loop.create_task(
                    self._run_pairs(
                        run_id,
                        generation,
                        tuple(range(len(session.pairs))),
                        execute_pair,
                        token,
                    ),
                    name=f"enhancement-bakeoff-{generation}",
                )
                session.task = task
                return run_id
            except BaseException:
                self._lease.release(token)
                raise

    def cancel(self, run_id: str) -> bool:
        """Cancel unfinished pairs while preserving every terminal result."""

        events: list[BakeoffProgressEvent] = []
        callback: ProgressCallback | None = None
        task: asyncio.Task[None] | None = None
        loop: asyncio.AbstractEventLoop | None = None
        with self._lock:
            session = self._sessions.get(run_id)
            if session is None or session.task is None or session.task.done():
                return False
            session.generation = self._allocate_generation_locked()
            for index, pair in enumerate(session.pairs):
                if pair.state not in _TERMINAL_STATES:
                    pair.state = BakeoffPairState.CANCELLED
                    pair.output = None
                    pair.error_code = None
                    pair.metadata = BakeoffExecutionMetadata()
                    events.append(self._event_locked(session, index))
            callback = self._callback
            task = session.task
            loop = session.loop
        self._emit_all(callback, events)
        _cancel_task(task, loop)
        return True

    def retry(
        self,
        run_id: str,
        *,
        pair_index: int,
        execute_pair: PairExecutor,
    ) -> bool:
        """Retry exactly one failed or cancelled pair in its existing slot."""

        if not callable(execute_pair):
            raise TypeError("execute_pair must be callable")
        with self._lock:
            session = self._sessions.get(run_id)
            if (
                session is None
                or session.task is not None
                or isinstance(pair_index, bool)
                or not isinstance(pair_index, int)
                or pair_index < 0
                or pair_index >= len(session.pairs)
                or session.pairs[pair_index].state
                not in {BakeoffPairState.FAILED, BakeoffPairState.CANCELLED}
            ):
                return False
            token = self._lease.try_acquire(EvaluationKind.BAKEOFF)
            if token is None:
                raise BakeoffBusyError("evaluation lease is active")
            try:
                loop = asyncio.get_running_loop()
                generation = self._allocate_generation_locked()
                session.generation = generation
                pair = session.pairs[pair_index]
                pair.state = BakeoffPairState.QUEUED
                pair.output = None
                pair.error_code = None
                pair.metadata = BakeoffExecutionMetadata()
                session.loop = loop
                task = loop.create_task(
                    self._run_pairs(
                        run_id,
                        generation,
                        (pair_index,),
                        execute_pair,
                        token,
                    ),
                    name=f"enhancement-bakeoff-{generation}",
                )
                session.task = task
                return True
            except BaseException:
                self._lease.release(token)
                raise

    async def wait(self, run_id: str) -> None:
        """Wait for the current run/retry task, if the session still exists."""

        with self._lock:
            session = self._sessions.get(run_id)
            task = session.task if session is not None else None
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise

    def snapshot(self, run_id: str) -> BakeoffSessionSnapshot | None:
        with self._lock:
            session = self._sessions.get(run_id)
            if session is None:
                return None
            return self._snapshot_locked(session)

    def revoke_all(self) -> None:
        """Synchronously invalidate work and scrub every transcript/result."""

        tasks: list[tuple[asyncio.Task[None], asyncio.AbstractEventLoop | None]] = []
        with self._lock:
            for session in self._sessions.values():
                session.generation = self._allocate_generation_locked()
                if session.task is not None and not session.task.done():
                    tasks.append((session.task, session.loop))
            self._sessions.clear()
            self._lease.revoke(EvaluationKind.BAKEOFF)
        for task, loop in tasks:
            _cancel_task(task, loop)

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def retained_char_count(self) -> int:
        with self._lock:
            return self._retained_chars_locked()

    async def _run_pairs(
        self,
        run_id: str,
        generation: int,
        pair_indices: tuple[int, ...],
        execute_pair: PairExecutor,
        token: EvaluationLeaseToken,
    ) -> None:
        # ==========================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # The raw input and returned output remain inside bounded session state.
        # Exceptions are reduced to stable codes without retaining messages.
        # ==========================================================================
        current_task = asyncio.current_task()
        try:
            for pair_index in pair_indices:
                if not self._transition(
                    run_id,
                    generation,
                    pair_index,
                    BakeoffPairState.LOADING,
                ):
                    return
                with self._lock:
                    session = self._sessions.get(run_id)
                    if session is None or session.generation != generation:
                        return
                    selection = session.pairs[pair_index].selection
                    raw_text = session.raw_text

                def mark_running(
                    metadata: BakeoffExecutionMetadata | None = None,
                    *,
                    _run_id: str = run_id,
                    _generation: int = generation,
                    _pair_index: int = pair_index,
                ) -> bool:
                    return self._transition(
                        _run_id,
                        _generation,
                        _pair_index,
                        BakeoffPairState.RUNNING,
                        metadata=(
                            metadata
                            if metadata is not None
                            else BakeoffExecutionMetadata()
                        ),
                    )

                try:
                    result = await execute_pair(selection, raw_text, mark_running)
                except asyncio.CancelledError:
                    self._transition(
                        run_id,
                        generation,
                        pair_index,
                        BakeoffPairState.CANCELLED,
                    )
                    raise
                except BakeoffExecutionError as exc:
                    self._fail_pair(run_id, generation, pair_index, exc.code)
                    continue
                except Exception:  # noqa: BLE001 - pair failure is isolated
                    self._fail_pair(
                        run_id,
                        generation,
                        pair_index,
                        "evaluation_failed",
                    )
                    continue
                if not isinstance(result, BakeoffExecutionResult):
                    self._fail_pair(
                        run_id,
                        generation,
                        pair_index,
                        "evaluation_failed",
                    )
                    continue
                self._complete_pair(
                    run_id,
                    generation,
                    pair_index,
                    result,
                )
        finally:
            self._finish_task(run_id, current_task, token)

    def _transition(
        self,
        run_id: str,
        generation: int,
        pair_index: int,
        state: BakeoffPairState,
        *,
        metadata: BakeoffExecutionMetadata | None = None,
    ) -> bool:
        if metadata is not None and not isinstance(
            metadata, BakeoffExecutionMetadata
        ):
            return False
        callback: ProgressCallback | None = None
        event: BakeoffProgressEvent | None = None
        with self._lock:
            session = self._sessions.get(run_id)
            if (
                session is None
                or session.generation != generation
                or pair_index < 0
                or pair_index >= len(session.pairs)
            ):
                return False
            pair = session.pairs[pair_index]
            if not _valid_transition(pair.state, state):
                return False
            pair.state = state
            if metadata is not None:
                pair.metadata = metadata
            pair.error_code = None
            if state not in {BakeoffPairState.COMPLETE, BakeoffPairState.UNCHANGED}:
                pair.output = None
            callback = self._callback
            event = self._event_locked(session, pair_index)
        self._emit(callback, event)
        return True

    def _fail_pair(
        self,
        run_id: str,
        generation: int,
        pair_index: int,
        error_code: str,
    ) -> bool:
        callback: ProgressCallback | None = None
        event: BakeoffProgressEvent | None = None
        with self._lock:
            session = self._sessions.get(run_id)
            if session is None or session.generation != generation:
                return False
            pair = session.pairs[pair_index]
            if pair.state in _TERMINAL_STATES:
                return False
            pair.state = BakeoffPairState.FAILED
            pair.output = None
            pair.error_code = _bounded_id(
                "error_code", error_code, max_chars=_MAX_ERROR_CODE_CHARS
            )
            callback = self._callback
            event = self._event_locked(session, pair_index)
        self._emit(callback, event)
        return True

    def _complete_pair(
        self,
        run_id: str,
        generation: int,
        pair_index: int,
        result: BakeoffExecutionResult,
    ) -> bool:
        callback: ProgressCallback | None = None
        event: BakeoffProgressEvent | None = None
        with self._lock:
            session = self._sessions.get(run_id)
            if session is None or session.generation != generation:
                return False
            pair = session.pairs[pair_index]
            if pair.state in _TERMINAL_STATES:
                return False
            output_chars = len(result.output)
            self._prune_locked(
                required_chars=output_chars,
                protect_run_id=run_id,
            )
            if (
                output_chars > self._max_retained_chars
                or self._retained_chars_locked() + output_chars
                > self._max_retained_chars
            ):
                pair.state = BakeoffPairState.FAILED
                pair.output = None
                pair.error_code = "result_too_large"
            else:
                pair.state = (
                    BakeoffPairState.UNCHANGED
                    if result.output == session.raw_text
                    else BakeoffPairState.COMPLETE
                )
                pair.output = result.output
                pair.error_code = None
                pair.metadata = result.metadata
            callback = self._callback
            event = self._event_locked(session, pair_index)
        self._emit(callback, event)
        return True

    def _finish_task(
        self,
        run_id: str,
        task: asyncio.Task[object] | None,
        token: EvaluationLeaseToken,
    ) -> None:
        with self._lock:
            session = self._sessions.get(run_id)
            if session is not None and session.task is task:
                session.task = None
                session.loop = None
            self._lease.release(token)
            self._prune_locked()

    def _event_locked(
        self, session: _Session, pair_index: int
    ) -> BakeoffProgressEvent:
        pair = session.pairs[pair_index]
        return BakeoffProgressEvent(
            run_id=session.run_id,
            generation=session.generation,
            pair_index=pair_index,
            state=pair.state,
            finished_count=sum(
                item.state in _TERMINAL_STATES for item in session.pairs
            ),
            total_count=len(session.pairs),
            error_code=pair.error_code,
            metadata=pair.metadata,
        )

    def _snapshot_locked(self, session: _Session) -> BakeoffSessionSnapshot:
        return BakeoffSessionSnapshot(
            run_id=session.run_id,
            generation=session.generation,
            raw_text=session.raw_text,
            pairs=tuple(
                BakeoffPairSnapshot(
                    selection=pair.selection,
                    state=pair.state,
                    output=pair.output,
                    error_code=pair.error_code,
                    metadata=pair.metadata,
                )
                for pair in session.pairs
            ),
            active=bool(session.task is not None and not session.task.done()),
        )

    def _selections(
        self, selections: Sequence[BakeoffPairSelection]
    ) -> tuple[BakeoffPairSelection, ...]:
        if isinstance(selections, (str, bytes)):
            raise TypeError("selections must be a sequence of pairs")
        items = tuple(selections)
        if not items or len(items) > self._max_pairs:
            raise ValueError(f"selections must contain 1 to {self._max_pairs} pairs")
        if not all(isinstance(item, BakeoffPairSelection) for item in items):
            raise TypeError("every selection must be BakeoffPairSelection")
        return items

    def _new_run_id_locked(self) -> str:
        for _ in range(8):
            run_id = _bounded_id("run_id", self._run_id_factory())
            if run_id not in self._sessions:
                return run_id
        raise RuntimeError("run ID factory produced repeated values")

    def _allocate_generation_locked(self) -> int:
        generation = self._next_generation
        self._next_generation += 1
        return generation

    def _prune_locked(
        self,
        *,
        required_sessions: int = 0,
        required_chars: int = 0,
        protect_run_id: str | None = None,
    ) -> None:
        for run_id, session in tuple(self._sessions.items()):
            if (
                len(self._sessions) + required_sessions <= self._max_sessions
                and self._retained_chars_locked() + required_chars
                <= self._max_retained_chars
            ):
                break
            if run_id == protect_run_id or session.task is not None:
                continue
            self._sessions.pop(run_id, None)

    def _retained_chars_locked(self) -> int:
        return sum(
            len(session.raw_text)
            + sum(len(pair.output) for pair in session.pairs if pair.output)
            for session in self._sessions.values()
        )

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        event: BakeoffProgressEvent | None,
    ) -> None:
        if callback is None or event is None:
            return
        try:
            callback(event)
        except Exception:  # noqa: BLE001 - observers cannot break orchestration
            return

    @classmethod
    def _emit_all(
        cls,
        callback: ProgressCallback | None,
        events: Sequence[BakeoffProgressEvent],
    ) -> None:
        for event in events:
            cls._emit(callback, event)


def _valid_transition(current: BakeoffPairState, target: BakeoffPairState) -> bool:
    if current in _TERMINAL_STATES:
        return False
    if target is BakeoffPairState.LOADING:
        return current is BakeoffPairState.QUEUED
    if target is BakeoffPairState.RUNNING:
        return current in {BakeoffPairState.LOADING, BakeoffPairState.RUNNING}
    if target in _TERMINAL_STATES:
        return current in {
            BakeoffPairState.QUEUED,
            BakeoffPairState.LOADING,
            BakeoffPairState.RUNNING,
        }
    return False


def _cancel_task(
    task: asyncio.Task[object] | None,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    if task is None or task.done():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is not None and loop.is_running() and running is not loop:
        loop.call_soon_threadsafe(task.cancel)
    else:
        task.cancel()


def _raw_text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("raw_text must be a string")
    if not value:
        raise ValueError("raw_text must not be empty")
    return value


def _bounded_id(name: str, value: object, *, max_chars: int = _MAX_ID_CHARS) -> str:
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise TypeError(f"{name} must be a bounded non-empty string")
    return value


def _optional_nonnegative_int(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer or None")
