"""Deterministic RAM admission tests for resident llama.cpp processes."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from whisperstream.resident_llama_pool import (
    MemoryAdmissionError,
    ResidentLlamaLoadError,
    ResidentLlamaPool,
    ResidentLlamaPoolClosedError,
    ResidentMemoryAdmission,
    canonical_model_path,
    estimate_resident_ram_mb,
)
from whisperstream.llama_server import LlamaServerRuntimeMetadata
from whisperstream.system_info import MemorySnapshot, get_memory_snapshot


MIB = 1024 * 1024


def _snapshots(*values: MemorySnapshot | None):
    remaining = iter(values)
    return lambda: next(remaining)


def _sparse_model(path: Path, size_mib: int) -> Path:
    with path.open("wb") as model_file:
        model_file.truncate(size_mib * MIB)
    return path


def test_system_memory_snapshot_reads_total_and_available_together():
    virtual_memory = MagicMock(
        total=16 * 1024 * MIB,
        available=6 * 1024 * MIB,
    )
    with patch(
        "whisperstream.system_info.psutil.virtual_memory",
        return_value=virtual_memory,
    ) as probe:
        assert get_memory_snapshot() == MemorySnapshot(
            total_mb=16 * 1024,
            available_mb=6 * 1024,
        )
    probe.assert_called_once_with()


def test_system_memory_snapshot_never_raises():
    with patch(
        "whisperstream.system_info.psutil.virtual_memory",
        side_effect=OSError("probe unavailable"),
    ):
        assert get_memory_snapshot() is None


def test_candidate_zero_catalog_ram_uses_gguf_size(tmp_path: Path):
    model_path = _sparse_model(tmp_path / "candidate.gguf", 3)

    assert estimate_resident_ram_mb(model_path, catalog_ram_mb=0) == 516


def test_catalog_ram_wins_when_larger_than_artifact(tmp_path: Path):
    model_path = _sparse_model(tmp_path / "production.gguf", 3)

    assert estimate_resident_ram_mb(model_path, catalog_ram_mb=1000) == 1762


def test_estimate_rounds_artifact_and_overhead_up(tmp_path: Path):
    model_path = tmp_path / "fractional.gguf"
    with model_path.open("wb") as model_file:
        model_file.truncate(2 * MIB + 1)

    artifact_mib = math.ceil(model_path.stat().st_size / MIB)
    assert artifact_mib == 3
    assert estimate_resident_ram_mb(model_path, catalog_ram_mb=0) == 516


def test_canonical_aliases_and_case_variants_reserve_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    model_path = _sparse_model(tmp_path / "Model.gguf", 1)
    monkeypatch.chdir(tmp_path)
    probe = MagicMock(return_value=MemorySnapshot(16_384, 8_000))
    admission = ResidentMemoryAdmission(memory_probe=probe)

    first = admission.reserve(Path(".") / "Model.gguf", catalog_ram_mb=1000)
    alias = admission.reserve(
        Path(str(model_path).upper()),
        catalog_ram_mb=1000,
    )

    assert canonical_model_path(model_path) == first.canonical_path
    assert alias.canonical_path == first.canonical_path
    assert first.created is True
    assert alias.created is False
    assert admission.reserved_mb == 1762
    assert probe.call_count == 1


def test_exact_initial_and_live_memory_boundary_is_admitted(tmp_path: Path):
    model_path = tmp_path / "boundary.gguf"
    # 1000 MiB catalog estimate -> 1762 MiB reservation. Total RAM below
    # 10 GiB uses the 2048 MiB minimum production headroom.
    exact_available = 1762 + 2048
    admission = ResidentMemoryAdmission(
        memory_probe=lambda: MemorySnapshot(10_000, exact_available)
    )

    reservation = admission.reserve(model_path, catalog_ram_mb=1000)

    assert reservation.reserved_mb == 1762
    assert admission.headroom_mb == 2048
    assert admission.initial_available_mb == exact_available


def test_twenty_percent_headroom_is_reserved_on_large_system(tmp_path: Path):
    total_mb = 16_384
    headroom_mb = math.ceil(total_mb * 0.20)
    reservation_mb = 1762
    admission = ResidentMemoryAdmission(
        memory_probe=lambda: MemorySnapshot(
            total_mb,
            reservation_mb + headroom_mb,
        )
    )

    admission.reserve(tmp_path / "large-system.gguf", catalog_ram_mb=1000)

    assert admission.headroom_mb == headroom_mb


def test_cumulative_reservations_cannot_exceed_initial_budget(tmp_path: Path):
    per_model_mb = 1762
    initial_available_mb = 2 * per_model_mb + 2048
    probe = _snapshots(
        MemorySnapshot(10_000, initial_available_mb),
        MemorySnapshot(10_000, initial_available_mb),
        MemorySnapshot(10_000, 50_000),
    )
    admission = ResidentMemoryAdmission(memory_probe=probe)

    admission.reserve(tmp_path / "one.gguf", catalog_ram_mb=1000)
    admission.reserve(tmp_path / "two.gguf", catalog_ram_mb=1000)
    with pytest.raises(MemoryAdmissionError, match="initial") as error:
        admission.reserve(tmp_path / "three.gguf", catalog_ram_mb=1000)

    assert error.value.reason == "insufficient_initial_memory"
    assert admission.reserved_mb == 2 * per_model_mb


def test_repeated_run_does_not_consume_more_memory_or_reprobe(tmp_path: Path):
    model_path = tmp_path / "repeat.gguf"
    probe = MagicMock(return_value=MemorySnapshot(10_000, 5_000))
    admission = ResidentMemoryAdmission(memory_probe=probe)

    first = admission.reserve(model_path, catalog_ram_mb=1000)
    second = admission.reserve(model_path, catalog_ram_mb=1000)

    assert first.created is True
    assert second.created is False
    assert admission.reserved_mb == first.reserved_mb
    probe.assert_called_once_with()


def test_live_memory_drop_rejects_next_reservation(tmp_path: Path):
    probe = _snapshots(
        MemorySnapshot(10_000, 6_000),
        MemorySnapshot(10_000, 3_809),
    )
    admission = ResidentMemoryAdmission(memory_probe=probe)
    admission.reserve(tmp_path / "one.gguf", catalog_ram_mb=1000)

    with pytest.raises(MemoryAdmissionError, match="currently available") as error:
        admission.reserve(tmp_path / "two.gguf", catalog_ram_mb=1000)

    assert error.value.reason == "insufficient_current_memory"
    assert admission.reserved_mb == 1762


def test_unknown_available_memory_fails_closed_only_for_new_processes(
    tmp_path: Path,
):
    probe = MagicMock(return_value=None)
    admission = ResidentMemoryAdmission(memory_probe=probe)

    borrowed = admission.reserve(
        tmp_path / "borrowed.gguf",
        catalog_ram_mb=0,
        new_process=False,
    )

    assert borrowed.created is True
    assert borrowed.reserved_mb == 0
    probe.assert_not_called()
    with pytest.raises(MemoryAdmissionError, match="unavailable") as error:
        admission.reserve(tmp_path / "new.gguf", catalog_ram_mb=1000)
    assert error.value.reason == "memory_probe_unavailable"


def test_failed_warm_rollback_releases_cumulative_reservation(tmp_path: Path):
    per_model_mb = 1762
    available_mb = per_model_mb + 2048
    admission = ResidentMemoryAdmission(
        memory_probe=MagicMock(
            return_value=MemorySnapshot(10_000, available_mb)
        )
    )
    failed = admission.reserve(tmp_path / "failed.gguf", catalog_ram_mb=1000)

    with pytest.raises(MemoryAdmissionError):
        admission.reserve(tmp_path / "blocked.gguf", catalog_ram_mb=1000)

    assert admission.rollback(failed) is True
    replacement = admission.reserve(
        tmp_path / "replacement.gguf",
        catalog_ram_mb=1000,
    )

    assert replacement.created is True
    assert admission.reserved_mb == per_model_mb
    assert admission.rollback(failed) is False


def test_duplicate_handle_cannot_roll_back_owned_reservation(tmp_path: Path):
    model_path = tmp_path / "shared.gguf"
    admission = ResidentMemoryAdmission(
        memory_probe=lambda: MemorySnapshot(10_000, 5_000)
    )
    owner = admission.reserve(model_path, catalog_ram_mb=1000)
    duplicate = admission.reserve(model_path, catalog_ram_mb=1000)

    assert admission.rollback(duplicate) is False
    assert admission.reserved_mb == owner.reserved_mb


class _FakeSupervisor:
    _next_port = 41000

    def __init__(
        self,
        gguf_path: Path,
        *,
        ready_result: bool = True,
        ready_gate: asyncio.Event | None = None,
        runtime_metadata: LlamaServerRuntimeMetadata | None = None,
    ) -> None:
        self.gguf_path = Path(gguf_path)
        self.port = self._next_port
        type(self)._next_port += 1
        self.ready_result = ready_result
        self.ready_gate = ready_gate
        self.ensure_started = asyncio.Event()
        self.ensure_calls = 0
        self.pin_calls = 0
        self.stop_calls = 0
        self.idle_timeouts: list[float] = []
        self.is_running = False
        self.is_ready = False
        self.runtime_metadata = runtime_metadata or LlamaServerRuntimeMetadata(
            device_mode="cpu",
            device=None,
            n_gpu_layers=0,
            load_duration_ms=125,
        )

    async def ensure_ready(self) -> bool:
        self.ensure_calls += 1
        self.ensure_started.set()
        if self.ready_gate is not None:
            await self.ready_gate.wait()
        self.is_running = self.ready_result
        self.is_ready = self.ready_result
        return self.ready_result

    def pin_resident(self) -> None:
        self.pin_calls += 1

    def set_idle_timeout(self, seconds: float) -> None:
        self.idle_timeouts.append(seconds)

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_running = False
        self.is_ready = False


def _pool(
    tmp_path: Path,
    factory,
    *,
    admission: ResidentMemoryAdmission | None = None,
) -> ResidentLlamaPool:
    return ResidentLlamaPool(
        binary_path=tmp_path / "llama-server.exe",
        admission=admission
        or ResidentMemoryAdmission(
            memory_probe=lambda: MemorySnapshot(32_768, 24_000)
        ),
        supervisor_factory=factory,
    )


@pytest.mark.asyncio
async def test_pool_coalesces_readiness_and_shares_one_model_across_runs(
    tmp_path: Path,
):
    model_path = _sparse_model(tmp_path / "shared.gguf", 1)
    ready_gate = asyncio.Event()
    created: list[_FakeSupervisor] = []

    def factory(path: Path, **_kwargs):
        supervisor = _FakeSupervisor(path, ready_gate=ready_gate)
        created.append(supervisor)
        return supervisor

    pool = _pool(tmp_path, factory)
    first_task = asyncio.create_task(
        pool.acquire(
            model_id="stream-1",
            model_path=model_path,
            catalog_ram_mb=1000,
        )
    )
    second_task = asyncio.create_task(
        pool.acquire(
            model_id="stream-1",
            model_path=Path(str(model_path).upper()),
            catalog_ram_mb=1000,
        )
    )
    await asyncio.sleep(0)
    await asyncio.wait_for(created[0].ensure_started.wait(), timeout=1.0)
    ready_gate.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first is second
    assert first.supervisor is created[0]
    assert first.inference_lock is second.inference_lock
    assert created[0].ensure_calls == 1
    assert created[0].pin_calls == 1
    assert pool.entry_count == 1

    repeated = await pool.acquire(
        model_id="stream-1",
        model_path=model_path,
        catalog_ram_mb=1000,
    )
    assert repeated is first
    assert created[0].ensure_calls == 1


@pytest.mark.asyncio
async def test_distinct_paths_get_distinct_cpu_only_supervisors_and_locks(
    tmp_path: Path,
):
    created: list[tuple[_FakeSupervisor, dict]] = []

    def factory(path: Path, **kwargs):
        supervisor = _FakeSupervisor(path)
        created.append((supervisor, kwargs))
        return supervisor

    pool = _pool(tmp_path, factory)
    one = await pool.acquire(
        model_id="one",
        model_path=_sparse_model(tmp_path / "one.gguf", 1),
        catalog_ram_mb=1000,
    )
    two = await pool.acquire(
        model_id="two",
        model_path=_sparse_model(tmp_path / "two.gguf", 1),
        catalog_ram_mb=1000,
    )

    assert one.supervisor is not two.supervisor
    assert one.supervisor.port != two.supervisor.port
    assert one.inference_lock is not two.inference_lock
    assert [kwargs["n_gpu_layers"] for _, kwargs in created] == [0, 0]
    assert [kwargs["device_index_provider"] for _, kwargs in created] == [None, None]
    assert created[0][1]["model_id_provider"]() == "one"
    assert created[1][1]["model_id_provider"]() == "two"
    assert one.metadata.device_mode == "cpu"
    assert one.metadata.load_duration_ms == 125


@pytest.mark.asyncio
async def test_matching_production_supervisor_is_borrowed_and_never_pool_stopped(
    tmp_path: Path,
):
    model_path = _sparse_model(tmp_path / "production.gguf", 1)
    production = _FakeSupervisor(
        model_path,
        runtime_metadata=LlamaServerRuntimeMetadata(
            device_mode="gpu",
            device="Vulkan2",
            n_gpu_layers=99,
            load_duration_ms=347,
        ),
    )
    production.is_running = True
    production.is_ready = True
    factory = MagicMock()
    memory_probe = MagicMock(return_value=None)
    pool = _pool(
        tmp_path,
        factory,
        admission=ResidentMemoryAdmission(memory_probe=memory_probe),
    )

    resident = await pool.acquire(
        model_id="production",
        model_path=Path(str(model_path).upper()),
        catalog_ram_mb=0,
        production_supervisor=production,
    )

    assert resident.supervisor is production
    assert resident.owned_by_pool is False
    assert resident.metadata.device_mode == "gpu"
    assert resident.metadata.device == "Vulkan2"
    assert resident.metadata.load_duration_ms == 347
    factory.assert_not_called()
    memory_probe.assert_not_called()

    await pool.stop_all()
    assert production.stop_calls == 0
    assert pool.entry_count == 0


@pytest.mark.asyncio
async def test_dormant_matching_production_supervisor_requires_ram_admission(
    tmp_path: Path,
):
    model_path = _sparse_model(tmp_path / "dormant-production.gguf", 1)
    production = _FakeSupervisor(model_path)
    probe = MagicMock(return_value=MemorySnapshot(10_000, 5_000))
    admission = ResidentMemoryAdmission(memory_probe=probe)
    factory = MagicMock()
    pool = _pool(tmp_path, factory, admission=admission)

    resident = await pool.acquire(
        model_id="production",
        model_path=model_path,
        catalog_ram_mb=1000,
        production_supervisor=production,
        production_model_path=model_path,
        production_catalog_ram_mb=1000,
    )

    assert resident.supervisor is production
    assert resident.owned_by_pool is False
    assert production.ensure_calls == 1
    assert admission.reserved_mb == 1762
    probe.assert_called_once_with()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_dormant_configured_production_headroom_blocks_other_model_admission(
    tmp_path: Path,
):
    production_path = _sparse_model(tmp_path / "configured-production.gguf", 1)
    candidate_path = _sparse_model(tmp_path / "candidate.gguf", 1)
    # One model plus ordinary system headroom fits. The second configured,
    # dormant production model does not.
    available_mb = 1762 + 2048
    admission = ResidentMemoryAdmission(
        memory_probe=lambda: MemorySnapshot(10_000, available_mb)
    )
    factory = MagicMock(side_effect=lambda path, **_kwargs: _FakeSupervisor(path))
    pool = _pool(tmp_path, factory, admission=admission)

    with pytest.raises(MemoryAdmissionError) as error:
        await pool.acquire(
            model_id="candidate",
            model_path=candidate_path,
            catalog_ram_mb=1000,
            production_model_path=production_path,
            production_catalog_ram_mb=1000,
        )

    assert error.value.reason == "insufficient_initial_memory"
    assert admission.reserved_mb == 0
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_running_configured_production_does_not_add_dormant_headroom(
    tmp_path: Path,
):
    production_path = _sparse_model(tmp_path / "running-production.gguf", 1)
    candidate_path = _sparse_model(tmp_path / "candidate.gguf", 1)
    production = _FakeSupervisor(production_path)
    production.is_running = True
    available_mb = 1762 + 2048
    admission = ResidentMemoryAdmission(
        memory_probe=lambda: MemorySnapshot(10_000, available_mb)
    )
    pool = _pool(
        tmp_path,
        lambda path, **_kwargs: _FakeSupervisor(path),
        admission=admission,
    )

    resident = await pool.acquire(
        model_id="candidate",
        model_path=candidate_path,
        catalog_ram_mb=1000,
        production_supervisor=production,
        production_model_path=production_path,
        production_catalog_ram_mb=1000,
    )

    assert resident.owned_by_pool is True
    assert admission.reserved_mb == 1762


@pytest.mark.asyncio
async def test_resident_lookup_reconciles_canonical_path_and_ownership(
    tmp_path: Path,
):
    owned_path = _sparse_model(tmp_path / "owned.gguf", 1)
    borrowed_path = _sparse_model(tmp_path / "borrowed.gguf", 1)
    borrowed = _FakeSupervisor(borrowed_path)
    pool = _pool(tmp_path, lambda path, **_kwargs: _FakeSupervisor(path))

    owned = await pool.acquire(
        model_id="owned",
        model_path=owned_path,
        catalog_ram_mb=1000,
    )
    borrowed_entry = await pool.acquire(
        model_id="borrowed",
        model_path=borrowed_path,
        catalog_ram_mb=0,
        production_supervisor=borrowed,
    )

    assert pool.find_resident(Path(str(owned_path).upper())) is owned
    assert pool.find_resident(borrowed_path) is borrowed_entry
    assert pool.find_resident(tmp_path / "missing.gguf") is None
    assert pool.has_resident()
    assert pool.has_resident(owned_path)
    assert not pool.has_resident(tmp_path / "missing.gguf")
    assert pool.owns_supervisor(owned.supervisor)
    assert not pool.owns_supervisor(borrowed)
    assert pool.borrowed_supervisors() == (borrowed,)


@pytest.mark.asyncio
async def test_idle_timeout_propagates_without_changing_residency(
    tmp_path: Path,
):
    owned_path = _sparse_model(tmp_path / "owned.gguf", 1)
    borrowed_path = _sparse_model(tmp_path / "borrowed.gguf", 1)
    borrowed = _FakeSupervisor(borrowed_path)
    pool = _pool(tmp_path, lambda path, **_kwargs: _FakeSupervisor(path))

    owned = await pool.acquire(
        model_id="owned",
        model_path=owned_path,
        catalog_ram_mb=1000,
    )
    await pool.acquire(
        model_id="borrowed",
        model_path=borrowed_path,
        catalog_ram_mb=0,
        production_supervisor=borrowed,
    )

    pool.set_idle_timeout(42.0)

    assert owned.supervisor.idle_timeouts == [42.0]
    assert borrowed.idle_timeouts == [42.0]
    assert pool.has_resident(owned_path)
    assert pool.has_resident(borrowed_path)


@pytest.mark.asyncio
async def test_failed_warm_is_rolled_back_without_disturbing_other_residents(
    tmp_path: Path,
):
    supervisors: dict[str, list[_FakeSupervisor]] = {}

    def factory(path: Path, **_kwargs):
        supervisor = _FakeSupervisor(path, ready_result=path.name != "bad.gguf")
        supervisors.setdefault(path.name, []).append(supervisor)
        return supervisor

    pool = _pool(tmp_path, factory)
    good = await pool.acquire(
        model_id="good",
        model_path=_sparse_model(tmp_path / "good.gguf", 1),
        catalog_ram_mb=1000,
    )

    with pytest.raises(ResidentLlamaLoadError) as error:
        await pool.acquire(
            model_id="bad",
            model_path=_sparse_model(tmp_path / "bad.gguf", 1),
            catalog_ram_mb=1000,
        )

    assert error.value.reason == "load_failed"
    assert supervisors["bad.gguf"][0].stop_calls == 1
    assert good.supervisor.stop_calls == 0
    assert pool.entry_count == 1

    with pytest.raises(ResidentLlamaLoadError):
        await pool.acquire(
            model_id="bad",
            model_path=tmp_path / "bad.gguf",
            catalog_ram_mb=1000,
        )
    assert len(supervisors["bad.gguf"]) == 2


@pytest.mark.asyncio
async def test_pool_admission_rejection_never_spawns_or_disturbs_a_resident(
    tmp_path: Path,
):
    probe = _snapshots(
        MemorySnapshot(10_000, 6_000),
        MemorySnapshot(10_000, 3_000),
    )
    admission = ResidentMemoryAdmission(memory_probe=probe)
    created: list[_FakeSupervisor] = []

    def factory(path: Path, **_kwargs):
        supervisor = _FakeSupervisor(path)
        created.append(supervisor)
        return supervisor

    pool = _pool(tmp_path, factory, admission=admission)
    first_path = _sparse_model(tmp_path / "resident.gguf", 1)
    rejected_path = _sparse_model(tmp_path / "rejected.gguf", 1)
    resident = await pool.acquire(
        model_id="resident",
        model_path=first_path,
        catalog_ram_mb=1000,
    )

    with pytest.raises(MemoryAdmissionError) as error:
        await pool.acquire(
            model_id="rejected",
            model_path=rejected_path,
            catalog_ram_mb=1000,
        )

    assert error.value.reason == "insufficient_current_memory"
    assert created == [resident.supervisor]
    assert resident.supervisor.stop_calls == 0
    assert pool.find_resident(first_path) is resident
    assert pool.find_resident(rejected_path) is None
    assert pool.entry_count == 1
    assert admission.reserved_mb == 1762


@pytest.mark.asyncio
async def test_stop_all_drains_ready_and_warming_owned_entries_but_not_borrowed(
    tmp_path: Path,
):
    warming_gate = asyncio.Event()
    created: list[_FakeSupervisor] = []

    def factory(path: Path, **_kwargs):
        supervisor = _FakeSupervisor(
            path,
            ready_gate=warming_gate if path.name == "warming.gguf" else None,
        )
        created.append(supervisor)
        return supervisor

    admission = ResidentMemoryAdmission(
        memory_probe=lambda: MemorySnapshot(32_768, 24_000)
    )
    pool = _pool(tmp_path, factory, admission=admission)
    await pool.acquire(
        model_id="ready",
        model_path=_sparse_model(tmp_path / "ready.gguf", 1),
        catalog_ram_mb=1000,
    )
    warming_task = asyncio.create_task(
        pool.acquire(
            model_id="warming",
            model_path=_sparse_model(tmp_path / "warming.gguf", 1),
            catalog_ram_mb=1000,
        )
    )
    await asyncio.sleep(0)
    await asyncio.wait_for(created[1].ensure_started.wait(), timeout=1.0)
    borrowed = _FakeSupervisor(_sparse_model(tmp_path / "borrowed.gguf", 1))
    await pool.acquire(
        model_id="borrowed",
        model_path=borrowed.gguf_path,
        catalog_ram_mb=0,
        production_supervisor=borrowed,
    )

    await pool.stop_all()

    assert created[0].stop_calls == 1
    assert created[1].stop_calls == 1
    assert borrowed.stop_calls == 0
    assert warming_task.cancelled()
    assert pool.entry_count == 0
    assert admission.reserved_mb == 0
    # Repeated shutdown is a no-op: owned children are stopped exactly once
    # and no stale admission handle can be rolled back twice.
    await pool.stop_all()
    assert created[0].stop_calls == 1
    assert created[1].stop_calls == 1
    assert admission.reserved_mb == 0
    with pytest.raises(ResidentLlamaPoolClosedError):
        await pool.acquire(
            model_id="after-shutdown",
            model_path=tmp_path / "after.gguf",
            catalog_ram_mb=1000,
        )
