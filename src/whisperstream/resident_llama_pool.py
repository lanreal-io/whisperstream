"""Admission and supervisor pooling for process-lifetime llama.cpp residency."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import system_info
from .llama_server import (
    LlamaServerRuntimeMetadata,
    LlamaServerSupervisor,
)

MIB = 1024 * 1024
MINIMUM_HEADROOM_MB = 2048
HEADROOM_FRACTION = 0.20
MODEL_OVERHEAD_MULTIPLIER = 1.25
PROCESS_OVERHEAD_MB = 512


class MemoryAdmissionError(RuntimeError):
    """A new resident process could not be admitted safely."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        required_mb: int | None = None,
        available_mb: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.required_mb = required_mb
        self.available_mb = available_mb


class ResidentLlamaLoadError(RuntimeError):
    """A resident supervisor did not become ready."""

    def __init__(self, reason: str = "load_failed") -> None:
        super().__init__("The resident model could not be loaded.")
        self.reason = reason


class ResidentLlamaPoolClosedError(RuntimeError):
    """The process-lifetime pool has begun shutdown."""


@dataclass(frozen=True)
class ResidentMemoryReservation:
    """One admission result returned to the future process owner.

    ``created`` is true only for the caller that inserted the canonical entry.
    Duplicate callers can reuse the entry but cannot roll its reservation back.
    """

    canonical_path: str
    reserved_mb: int
    new_process: bool
    created: bool
    _token: int


@dataclass(frozen=True)
class _LedgerEntry:
    reserved_mb: int
    new_process: bool
    token: int


def canonical_model_path(model_path: str | os.PathLike[str]) -> str:
    """Return an absolute, symlink-resolved, case-normalized GGUF identity."""

    resolved = Path(model_path).expanduser().resolve(strict=False)
    # Whisperstream is Windows-first and Windows model paths are
    # case-insensitive. casefold also keeps cross-platform tests faithful to
    # that production identity contract.
    return os.path.normpath(str(resolved)).casefold()


def estimate_resident_ram_mb(
    model_path: str | os.PathLike[str],
    *,
    catalog_ram_mb: int | float,
) -> int:
    """Estimate one new CPU llama-server resident in MiB.

    Policy: ``ceil(max(nonzero catalog RAM, ceil(GGUF bytes / MiB)) * 1.25)
    + 512 MiB``. A missing artifact is tolerable only when the catalog supplies
    a positive RAM estimate; model availability is validated by higher layers.
    """

    try:
        catalog_mb = float(catalog_ram_mb)
    except (TypeError, ValueError) as exc:
        raise ValueError("catalog RAM estimate must be numeric") from exc
    if not math.isfinite(catalog_mb) or catalog_mb < 0:
        raise ValueError("catalog RAM estimate must be finite and non-negative")

    try:
        artifact_mb = math.ceil(Path(model_path).stat().st_size / MIB)
    except OSError:
        artifact_mb = 0

    base_mb = max(catalog_mb if catalog_mb > 0 else 0, artifact_mb)
    if base_mb <= 0:
        raise ValueError("resident RAM cannot be estimated without catalog RAM or GGUF size")
    return math.ceil(base_mb * MODEL_OVERHEAD_MULTIPLIER) + PROCESS_OVERHEAD_MB


class ResidentMemoryAdmission:
    """Serialized, cumulative RAM admission for resident model processes."""

    def __init__(
        self,
        *,
        memory_probe: Callable[[], system_info.MemorySnapshot | None] | None = None,
    ) -> None:
        self._memory_probe = memory_probe or system_info.get_memory_snapshot
        self._lock = threading.Lock()
        self._entries: dict[str, _LedgerEntry] = {}
        self._reserved_mb = 0
        self._initial_available_mb: int | None = None
        self._headroom_mb: int | None = None
        self._next_token = 1

    @property
    def reserved_mb(self) -> int:
        with self._lock:
            return self._reserved_mb

    @property
    def initial_available_mb(self) -> int | None:
        with self._lock:
            return self._initial_available_mb

    @property
    def headroom_mb(self) -> int | None:
        with self._lock:
            return self._headroom_mb

    def reserve(
        self,
        model_path: str | os.PathLike[str],
        *,
        catalog_ram_mb: int | float,
        new_process: bool = True,
        protected_mb: int = 0,
    ) -> ResidentMemoryReservation:
        """Return a deduplicated reservation or raise ``MemoryAdmissionError``.

        Existing/borrowed processes pass ``new_process=False``. They enter the
        canonical ledger with a zero reservation and do not require a memory
        probe because their current footprint is already reflected in live RAM.
        """

        if (
            isinstance(protected_mb, bool)
            or not isinstance(protected_mb, int)
            or protected_mb < 0
        ):
            raise ValueError("protected_mb must be a non-negative integer")

        canonical_path = canonical_model_path(model_path)
        with self._lock:
            existing = self._entries.get(canonical_path)
            if existing is not None:
                return ResidentMemoryReservation(
                    canonical_path=canonical_path,
                    reserved_mb=existing.reserved_mb,
                    new_process=existing.new_process,
                    created=False,
                    _token=existing.token,
                )

            if not new_process:
                return self._insert(
                    canonical_path,
                    reserved_mb=0,
                    new_process=False,
                )

            try:
                reservation_mb = estimate_resident_ram_mb(
                    model_path,
                    catalog_ram_mb=catalog_ram_mb,
                )
            except ValueError as exc:
                raise MemoryAdmissionError(
                    "memory_estimate_unavailable",
                    "Resident memory estimate is unavailable for this model.",
                ) from exc

            try:
                snapshot = self._memory_probe()
            except Exception as exc:  # probes are best-effort even when injected
                raise MemoryAdmissionError(
                    "memory_probe_unavailable",
                    "Available system memory is unavailable.",
                ) from exc
            if (
                snapshot is None
                or snapshot.total_mb <= 0
                or snapshot.available_mb < 0
            ):
                raise MemoryAdmissionError(
                    "memory_probe_unavailable",
                    "Available system memory is unavailable.",
                )

            if self._initial_available_mb is None:
                self._initial_available_mb = snapshot.available_mb
                self._headroom_mb = max(
                    MINIMUM_HEADROOM_MB,
                    math.ceil(snapshot.total_mb * HEADROOM_FRACTION),
                )

            # Both values are initialized together on the first successful
            # probe and remain fixed for this process-lifetime ledger.
            assert self._headroom_mb is not None
            initial_required_mb = (
                self._reserved_mb
                + reservation_mb
                + protected_mb
                + self._headroom_mb
            )
            if initial_required_mb > self._initial_available_mb:
                raise MemoryAdmissionError(
                    "insufficient_initial_memory",
                    "The cumulative reservation exceeds initial available memory.",
                    required_mb=initial_required_mb,
                    available_mb=self._initial_available_mb,
                )

            current_required_mb = (
                reservation_mb + protected_mb + self._headroom_mb
            )
            if current_required_mb > snapshot.available_mb:
                raise MemoryAdmissionError(
                    "insufficient_current_memory",
                    "The next reservation exceeds currently available memory.",
                    required_mb=current_required_mb,
                    available_mb=snapshot.available_mb,
                )

            return self._insert(
                canonical_path,
                reserved_mb=reservation_mb,
                new_process=True,
            )

    def rollback(self, reservation: ResidentMemoryReservation) -> bool:
        """Release a creator-owned reservation after a failed process warm."""

        if not reservation.created:
            return False
        with self._lock:
            current = self._entries.get(reservation.canonical_path)
            if current is None or current.token != reservation._token:
                return False
            del self._entries[reservation.canonical_path]
            self._reserved_mb -= current.reserved_mb
            return True

    def _insert(
        self,
        canonical_path: str,
        *,
        reserved_mb: int,
        new_process: bool,
    ) -> ResidentMemoryReservation:
        token = self._next_token
        self._next_token += 1
        self._entries[canonical_path] = _LedgerEntry(
            reserved_mb=reserved_mb,
            new_process=new_process,
            token=token,
        )
        self._reserved_mb += reserved_mb
        return ResidentMemoryReservation(
            canonical_path=canonical_path,
            reserved_mb=reserved_mb,
            new_process=new_process,
            created=True,
            _token=token,
        )


@dataclass(frozen=True)
class ResidentLlamaMetadata:
    """Content-free execution facts exposed with a resident model."""

    model_id: str
    canonical_path: str
    owned_by_pool: bool
    device_mode: str
    device: str | None
    n_gpu_layers: int | None
    load_duration_ms: int | None


@dataclass
class ResidentLlama:
    """One immutable model/supervisor binding shared across Bakeoff runs."""

    model_id: str
    model_path: Path
    canonical_path: str
    supervisor: LlamaServerSupervisor
    owned_by_pool: bool
    inference_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _reservation: ResidentMemoryReservation | None = field(
        default=None,
        repr=False,
    )
    _readiness_task: asyncio.Task[bool] | None = field(
        default=None,
        repr=False,
    )

    @property
    def metadata(self) -> ResidentLlamaMetadata:
        runtime: LlamaServerRuntimeMetadata = self.supervisor.runtime_metadata
        return ResidentLlamaMetadata(
            model_id=self.model_id,
            canonical_path=self.canonical_path,
            owned_by_pool=self.owned_by_pool,
            device_mode=runtime.device_mode,
            device=runtime.device,
            n_gpu_layers=runtime.n_gpu_layers,
            load_duration_ms=runtime.load_duration_ms,
        )


class ResidentLlamaPool:
    """Serialized one-supervisor-per-artifact process-lifetime pool.

    The matching production supervisor is borrowed, preserving its configured
    device and external ownership. Every supervisor created by this pool is
    frozen to its requested model identity and forced to CPU-only execution.
    """

    def __init__(
        self,
        *,
        binary_path: Path,
        admission: ResidentMemoryAdmission | None = None,
        supervisor_factory: Callable[..., LlamaServerSupervisor] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._binary_path = Path(binary_path)
        self._admission = admission or ResidentMemoryAdmission()
        self._supervisor_factory = supervisor_factory or LlamaServerSupervisor
        self._logger = logger or logging.getLogger(__name__)
        self._entries: dict[str, ResidentLlama] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def entry_count(self) -> int:
        """Number of borrowed and owned models currently tracked."""

        return len(self._entries)

    def find_resident(
        self, model_path: str | os.PathLike[str]
    ) -> ResidentLlama | None:
        """Return the pinned entry for *model_path*, if already resident.

        Entries become visible only after ``acquire()`` has registered their
        canonical artifact identity. Normal production supervisors remain
        lazy and unpinned outside the pool until a resident caller borrows one.
        """

        return self._entries.get(canonical_model_path(model_path))

    def has_resident(
        self, model_path: str | os.PathLike[str] | None = None
    ) -> bool:
        """Whether any, or one specific, process-lifetime resident exists."""

        if model_path is None:
            return bool(self._entries)
        return self.find_resident(model_path) is not None

    def owns_supervisor(self, supervisor: object) -> bool:
        """Whether *supervisor* is an entry whose process the pool owns."""

        return any(
            entry.supervisor is supervisor and entry.owned_by_pool
            for entry in self._entries.values()
        )

    def borrowed_supervisors(self) -> tuple[LlamaServerSupervisor, ...]:
        """Return production-owned supervisors retained by resident entries.

        ``stop_all()`` deliberately never stops these. The application owner
        uses this snapshot at shutdown so a borrowed model remains owned by
        the normal lifecycle even after another resident is promoted into the
        ``app.llama_server`` compatibility alias.
        """

        return tuple(
            entry.supervisor
            for entry in self._entries.values()
            if not entry.owned_by_pool
        )

    def set_idle_timeout(self, seconds: float) -> None:
        """Propagate normal idle policy without clearing runtime residency.

        ``LlamaServerSupervisor.set_idle_timeout`` preserves its independent
        runtime pin, so config reloads update the stored policy but cannot
        evict a Bakeoff resident.
        """

        seen: set[int] = set()
        for entry in self._entries.values():
            supervisor = entry.supervisor
            identity = id(supervisor)
            if identity in seen:
                continue
            seen.add(identity)
            supervisor.set_idle_timeout(seconds)

    async def acquire(
        self,
        *,
        model_id: str,
        model_path: str | os.PathLike[str],
        catalog_ram_mb: int | float,
        production_supervisor: LlamaServerSupervisor | None = None,
        production_model_path: str | os.PathLike[str] | None = None,
        production_catalog_ram_mb: int | float = 0,
    ) -> ResidentLlama:
        """Return one ready, pinned entry for the canonical model artifact."""

        canonical_path = canonical_model_path(model_path)
        resolved_path = Path(model_path).expanduser().resolve(strict=False)

        async with self._lock:
            if self._closed:
                raise ResidentLlamaPoolClosedError(
                    "The resident llama pool is shutting down."
                )

            entry = self._entries.get(canonical_path)
            if entry is None:
                matches_production = self._matches_production(
                    canonical_path,
                    production_supervisor,
                )
                production_active = bool(
                    matches_production
                    and self._supervisor_consumes_memory(production_supervisor)
                )
                protected_mb = self._dormant_production_headroom_mb(
                    target_path=canonical_path,
                    production_model_path=production_model_path,
                    production_catalog_ram_mb=production_catalog_ram_mb,
                    production_supervisor=production_supervisor,
                )
                reservation = self._admission.reserve(
                    resolved_path,
                    catalog_ram_mb=catalog_ram_mb,
                    # A path-matching but dormant production supervisor will
                    # start a new process for this acquisition. External
                    # ownership does not make that RAM footprint free.
                    new_process=not production_active,
                    protected_mb=protected_mb,
                )
                try:
                    supervisor = (
                        production_supervisor
                        if matches_production
                        else self._create_owned_supervisor(
                            model_id=model_id,
                            model_path=resolved_path,
                        )
                    )
                except Exception:
                    self._admission.rollback(reservation)
                    raise

                assert supervisor is not None
                entry = ResidentLlama(
                    model_id=model_id,
                    model_path=resolved_path,
                    canonical_path=canonical_path,
                    supervisor=supervisor,
                    owned_by_pool=not matches_production,
                    _reservation=reservation,
                )
                self._entries[canonical_path] = entry

            if entry.supervisor.is_ready:
                entry.supervisor.pin_resident()
                return entry

            readiness_task = entry._readiness_task
            if readiness_task is None or readiness_task.done():
                readiness_task = asyncio.create_task(self._ensure_entry_ready(entry))
                entry._readiness_task = readiness_task

        ready = await asyncio.shield(readiness_task)
        if not ready:
            raise ResidentLlamaLoadError()
        return entry

    async def stop_all(self) -> None:
        """Drain pool work and stop every pool-owned supervisor exactly once."""

        async with self._lock:
            if self._closed and not self._entries:
                return
            self._closed = True
            entries = list(self._entries.values())
            self._entries.clear()
            for entry in entries:
                if entry._reservation is not None:
                    self._admission.rollback(entry._reservation)

        readiness_tasks = {
            entry._readiness_task
            for entry in entries
            if entry._readiness_task is not None
            and not entry._readiness_task.done()
        }
        for task in readiness_tasks:
            task.cancel()
        if readiness_tasks:
            await asyncio.gather(*readiness_tasks, return_exceptions=True)

        owned = [entry.supervisor for entry in entries if entry.owned_by_pool]
        if owned:
            results = await asyncio.gather(
                *(supervisor.stop() for supervisor in owned),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    self._logger.warning(
                        "[ResidentLlamaPool] shutdown failed reason=%s",
                        type(result).__name__,
                    )

    def _create_owned_supervisor(
        self,
        *,
        model_id: str,
        model_path: Path,
    ) -> LlamaServerSupervisor:
        # Capture identity in the closure. Never consult mutable production
        # config for a resident model's template or execution contract.
        return self._supervisor_factory(
            model_path,
            binary_path=self._binary_path,
            n_gpu_layers=0,
            device_index_provider=None,
            model_id_provider=lambda captured_model_id=model_id: captured_model_id,
        )

    @staticmethod
    def _matches_production(
        canonical_path: str,
        production_supervisor: LlamaServerSupervisor | None,
    ) -> bool:
        if production_supervisor is None:
            return False
        try:
            return canonical_model_path(production_supervisor.gguf_path) == canonical_path
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _supervisor_consumes_memory(supervisor: object | None) -> bool:
        """Whether a production child is genuinely running or ready."""

        return bool(
            supervisor is not None
            and (
                getattr(supervisor, "is_running", False)
                or getattr(supervisor, "is_ready", False)
            )
        )

    def _dormant_production_headroom_mb(
        self,
        *,
        target_path: str,
        production_model_path: str | os.PathLike[str] | None,
        production_catalog_ram_mb: int | float,
        production_supervisor: LlamaServerSupervisor | None,
    ) -> int:
        """Return RAM to preserve for a configured, dormant production model.

        Running processes already reduce the live-memory probe. A resident
        pool entry already owns an admission reservation. Only a configured
        artifact that is neither running nor resident needs this extra
        prospective allowance while another model is admitted.
        """

        if production_model_path is None:
            return 0
        try:
            production_key = canonical_model_path(production_model_path)
        except (OSError, TypeError, ValueError):
            return 0
        if production_key == target_path or production_key in self._entries:
            return 0
        if self._matches_production(
            production_key, production_supervisor
        ) and self._supervisor_consumes_memory(production_supervisor):
            return 0
        try:
            return estimate_resident_ram_mb(
                production_model_path,
                catalog_ram_mb=production_catalog_ram_mb,
            )
        except ValueError as exc:
            raise MemoryAdmissionError(
                "memory_estimate_unavailable",
                "Resident memory estimate is unavailable for the configured production model.",
            ) from exc

    async def _ensure_entry_ready(self, entry: ResidentLlama) -> bool:
        try:
            ready = await entry.supervisor.ensure_ready()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.warning(
                "[ResidentLlamaPool] warm failed reason=%s",
                type(exc).__name__,
            )
            ready = False

        if ready:
            entry.supervisor.pin_resident()
            return True

        if entry.owned_by_pool:
            try:
                await entry.supervisor.stop()
            except Exception as exc:
                self._logger.warning(
                    "[ResidentLlamaPool] failed-warm cleanup reason=%s",
                    type(exc).__name__,
                )

        async with self._lock:
            if self._entries.get(entry.canonical_path) is entry:
                del self._entries[entry.canonical_path]
                if entry._reservation is not None:
                    self._admission.rollback(entry._reservation)
        return False
