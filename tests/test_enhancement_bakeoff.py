from __future__ import annotations

import asyncio
import copy
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import whisperstream.enhancement_bakeoff as bakeoff_module
import whisperstream.main as main_module
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.enhancement_bakeoff import (
    BakeoffBusyError,
    BakeoffCapacityError,
    BakeoffExecutionError,
    BakeoffExecutionMetadata,
    BakeoffExecutionResult,
    BakeoffPairSelection,
    BakeoffPairState,
    EnhancementBakeoffManager,
    EvaluationKind,
    EvaluationLease,
)
from whisperstream.main import WhisperstreamApp


def _pairs(count: int) -> tuple[BakeoffPairSelection, ...]:
    return tuple(
        BakeoffPairSelection(model_id=f"model-{index}", profile_id="chat")
        for index in range(count)
    )


class _ReadySupervisor:
    is_ready = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.base_url = f"http://127.0.0.1/{name}"


class _FakeResident:
    def __init__(
        self,
        *,
        model_id: str,
        path: Path,
        device_mode: str,
        load_duration_ms: int,
    ) -> None:
        self.model_id = model_id
        self.model_path = path
        self.supervisor = _ReadySupervisor(model_id)
        self.inference_lock = asyncio.Lock()
        self.metadata = SimpleNamespace(
            device_mode=device_mode,
            load_duration_ms=load_duration_ms,
        )


class _FakeResidentPool:
    def __init__(self) -> None:
        self.entries: dict[str, _FakeResident] = {}
        self.acquisitions: list[dict[str, object]] = []

    @staticmethod
    def _key(path: str | Path) -> str:
        return str(Path(path).resolve(strict=False)).casefold()

    def find_resident(self, path: str | Path) -> _FakeResident | None:
        return self.entries.get(self._key(path))

    async def acquire(self, **kwargs) -> _FakeResident:
        self.acquisitions.append(dict(kwargs))
        path = Path(kwargs["model_path"])
        key = self._key(path)
        resident = self.entries.get(key)
        if resident is None:
            resident = _FakeResident(
                model_id=str(kwargs["model_id"]),
                path=path,
                device_mode=(
                    "gpu" if kwargs.get("production_supervisor") is not None else "cpu"
                ),
                load_duration_ms=137,
            )
            self.entries[key] = resident
        return resident


def _app_for_production_bakeoff(
    config: WhisperstreamConfig,
    pool: _FakeResidentPool,
) -> WhisperstreamApp:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.config = config
    app.resident_llama_pool = pool
    app.llama_server = None
    app.logger = None
    app._simulated_tps_provider = lambda: None
    app._lock = threading.Lock()
    app._recording = False
    app.dictation_state_snapshot = lambda: {"phase": "idle"}
    app.evaluation_lease = EvaluationLease()
    app.enhancement_bakeoff = EnhancementBakeoffManager(
        lease=app.evaluation_lease
    )
    return app


@pytest.mark.asyncio
async def test_run_preserves_pair_order_executes_sequentially_and_emits_no_content() -> None:
    lease = EvaluationLease()
    events = []
    manager = EnhancementBakeoffManager(
        lease=lease,
        progress_callback=events.append,
    )
    active = 0
    max_active = 0
    seen: list[str] = []

    async def execute(selection, raw_text, mark_running):
        nonlocal active, max_active
        assert raw_text == "private raw transcript"
        assert mark_running(
            BakeoffExecutionMetadata(
                device_mode="cpu",
                load_ms=12,
                inference_ms=34,
            )
        )
        active += 1
        max_active = max(max_active, active)
        seen.append(selection.model_id)
        await asyncio.sleep(0)
        active -= 1
        return BakeoffExecutionResult(
            output=f"result from {selection.model_id}",
            metadata=BakeoffExecutionMetadata(
                device_mode="cpu",
                load_ms=12,
                inference_ms=34,
            ),
        )

    run_id = manager.start(
        raw_text="private raw transcript",
        selections=_pairs(3),
        execute_pair=execute,
    )
    await manager.wait(run_id)

    snapshot = manager.snapshot(run_id)
    assert snapshot is not None
    assert [pair.selection.model_id for pair in snapshot.pairs] == [
        "model-0",
        "model-1",
        "model-2",
    ]
    assert [pair.state for pair in snapshot.pairs] == [
        BakeoffPairState.COMPLETE,
        BakeoffPairState.COMPLETE,
        BakeoffPairState.COMPLETE,
    ]
    assert seen == ["model-0", "model-1", "model-2"]
    assert max_active == 1
    assert lease.active_kind() is None
    assert [event.state for event in events] == [
        BakeoffPairState.LOADING,
        BakeoffPairState.RUNNING,
        BakeoffPairState.COMPLETE,
        BakeoffPairState.LOADING,
        BakeoffPairState.RUNNING,
        BakeoffPairState.COMPLETE,
        BakeoffPairState.LOADING,
        BakeoffPairState.RUNNING,
        BakeoffPairState.COMPLETE,
    ]
    assert all(not hasattr(event, "raw_text") for event in events)
    assert all(not hasattr(event, "output") for event in events)
    assert "private raw transcript" not in repr(events)
    assert "result from" not in repr(events)


@pytest.mark.asyncio
async def test_partial_failure_keeps_completed_results_and_continues() -> None:
    manager = EnhancementBakeoffManager(lease=EvaluationLease())

    async def execute(selection, raw_text, mark_running):
        del raw_text
        assert mark_running()
        if selection.model_id == "model-1":
            raise BakeoffExecutionError("model_load_failed")
        return BakeoffExecutionResult(output=f"clean {selection.model_id}")

    run_id = manager.start(
        raw_text="raw",
        selections=_pairs(3),
        execute_pair=execute,
    )
    await manager.wait(run_id)

    snapshot = manager.snapshot(run_id)
    assert snapshot is not None
    assert [pair.state for pair in snapshot.pairs] == [
        BakeoffPairState.COMPLETE,
        BakeoffPairState.FAILED,
        BakeoffPairState.COMPLETE,
    ]
    assert snapshot.pairs[0].output == "clean model-0"
    assert snapshot.pairs[1].output is None
    assert snapshot.pairs[1].error_code == "model_load_failed"
    assert snapshot.pairs[2].output == "clean model-2"


@pytest.mark.asyncio
async def test_cancel_stops_current_and_queued_pairs_but_keeps_completed_output() -> None:
    lease = EvaluationLease()
    manager = EnhancementBakeoffManager(lease=lease)
    second_started = asyncio.Event()
    blocker = asyncio.Event()
    executed: list[str] = []

    async def execute(selection, raw_text, mark_running):
        del raw_text
        executed.append(selection.model_id)
        assert mark_running()
        if selection.model_id == "model-1":
            second_started.set()
            await blocker.wait()
        return BakeoffExecutionResult(output=f"clean {selection.model_id}")

    run_id = manager.start(
        raw_text="raw",
        selections=_pairs(3),
        execute_pair=execute,
    )
    await second_started.wait()
    assert manager.cancel(run_id) is True
    await manager.wait(run_id)

    snapshot = manager.snapshot(run_id)
    assert snapshot is not None
    assert [pair.state for pair in snapshot.pairs] == [
        BakeoffPairState.COMPLETE,
        BakeoffPairState.CANCELLED,
        BakeoffPairState.CANCELLED,
    ]
    assert snapshot.pairs[0].output == "clean model-0"
    assert executed == ["model-0", "model-1"]
    assert lease.active_kind() is None


@pytest.mark.asyncio
async def test_retry_runs_only_one_pair_and_stale_reporter_is_ignored() -> None:
    events = []
    manager = EnhancementBakeoffManager(
        lease=EvaluationLease(),
        progress_callback=events.append,
    )
    attempts: list[str] = []
    stale_reporter: Callable[..., bool] | None = None

    async def first_execute(selection, raw_text, mark_running):
        nonlocal stale_reporter
        del raw_text
        attempts.append(f"first:{selection.model_id}")
        assert mark_running()
        if selection.model_id == "model-1":
            stale_reporter = mark_running
            raise BakeoffExecutionError("inference_failed")
        return BakeoffExecutionResult(output=f"first {selection.model_id}")

    run_id = manager.start(
        raw_text="raw",
        selections=_pairs(2),
        execute_pair=first_execute,
    )
    await manager.wait(run_id)
    first_generation = manager.snapshot(run_id).generation

    async def retry_execute(selection, raw_text, mark_running):
        del raw_text
        attempts.append(f"retry:{selection.model_id}")
        assert mark_running()
        return BakeoffExecutionResult(output="retry result")

    assert manager.retry(run_id, pair_index=1, execute_pair=retry_execute) is True
    assert stale_reporter is not None
    event_count = len(events)
    assert stale_reporter() is False
    assert len(events) == event_count
    await manager.wait(run_id)

    snapshot = manager.snapshot(run_id)
    assert snapshot is not None
    assert snapshot.generation > first_generation
    assert snapshot.pairs[0].output == "first model-0"
    assert snapshot.pairs[1].output == "retry result"
    assert attempts == ["first:model-0", "first:model-1", "retry:model-1"]
    assert not any(
        event.generation == first_generation
        and event.pair_index == 1
        and event.state is BakeoffPairState.RUNNING
        for event in events[-3:]
    )


@pytest.mark.asyncio
async def test_memory_bounds_evict_oldest_terminal_session_and_reject_large_result() -> None:
    manager = EnhancementBakeoffManager(
        lease=EvaluationLease(),
        max_sessions=2,
        max_retained_chars=20,
    )

    async def execute(selection, raw_text, mark_running):
        del selection, raw_text
        assert mark_running()
        return BakeoffExecutionResult(output="1234")

    first = manager.start(raw_text="1234", selections=_pairs(1), execute_pair=execute)
    await manager.wait(first)
    second = manager.start(raw_text="1234", selections=_pairs(1), execute_pair=execute)
    await manager.wait(second)
    third = manager.start(raw_text="1234", selections=_pairs(1), execute_pair=execute)
    await manager.wait(third)

    assert manager.snapshot(first) is None
    assert manager.snapshot(second) is not None
    assert manager.snapshot(third) is not None
    assert manager.retained_char_count() <= 20

    async def oversized(selection, raw_text, mark_running):
        del selection, raw_text
        assert mark_running()
        return BakeoffExecutionResult(output="x" * 21)

    large = manager.start(raw_text="r", selections=_pairs(1), execute_pair=oversized)
    await manager.wait(large)
    large_snapshot = manager.snapshot(large)
    assert large_snapshot is not None
    assert large_snapshot.pairs[0].state is BakeoffPairState.FAILED
    assert large_snapshot.pairs[0].error_code == "result_too_large"
    assert large_snapshot.pairs[0].output is None
    assert manager.retained_char_count() <= 20


def test_capacity_and_evaluation_lease_fail_closed() -> None:
    lease = EvaluationLease()
    normal = lease.try_acquire(EvaluationKind.NORMAL)
    assert normal is not None
    assert lease.try_acquire(EvaluationKind.BAKEOFF) is None

    manager = EnhancementBakeoffManager(
        lease=lease,
        max_retained_chars=5,
    )

    async def execute(selection, raw_text, mark_running):
        raise AssertionError("must not execute")

    with pytest.raises(BakeoffCapacityError):
        manager.start(
            raw_text="too long",
            selections=_pairs(1),
            execute_pair=execute,
        )
    with pytest.raises(BakeoffBusyError):
        manager.start(
            raw_text="raw",
            selections=_pairs(1),
            execute_pair=execute,
        )
    assert lease.release(normal) is True


@pytest.mark.asyncio
async def test_production_binding_runs_explicit_pairs_from_one_raw_input(
    monkeypatch,
) -> None:
    raw_text = " ".join(f"word{index}" for index in range(25))
    config = WhisperstreamConfig(
        enhancement_enabled=False,
        enhancement_provider="openai",
        enhancement_local_model="global-model",
        enhancement_chunked_cleanup=True,
        enhancement_words_per_chunk=20,
        enhancement_limit_first_chunk=False,
        profiles=[
            {"id": "chat", "name": "Chat", "prompt": "global chat prompt"},
            {"id": "code", "name": "Code", "prompt": "global code prompt"},
        ],
        default_profile_id="chat",
    )
    original_config = copy.deepcopy(config)
    pool = _FakeResidentPool()
    app = _app_for_production_bakeoff(config, pool)
    model_rows = {
        "model-a": {
            "id": "model-a",
            "prompt_key": "stream",
            "serving_contract": "raw-v1",
            "trained_modes": ["chat", "code"],
            "ram_mb": 111,
            "_path": Path("model-a.gguf"),
        },
        "model-b": {
            "id": "model-b",
            "prompt_key": "stream",
            "serving_contract": "wrap-v2",
            "trained_modes": ["chat"],
            "ram_mb": 222,
            "_path": Path("model-b.gguf"),
        },
    }
    monkeypatch.setattr(
        bakeoff_module,
        "get_local_model",
        lambda model_id: model_rows.get(model_id),
        raising=False,
    )
    monkeypatch.setattr(
        bakeoff_module,
        "resolve_local_model_path",
        lambda model: model.get("_path"),
        raising=False,
    )

    calls: list[dict[str, object]] = []

    async def run_evaluation(enhancer, text, **kwargs):
        resolution = enhancer._resolve_prompt_contract(kwargs["profile_id"])
        calls.append(
            {
                "raw": text,
                "raw_bytes": text.encode("utf-8"),
                "config": copy.deepcopy(enhancer._config),
                "model_metadata": copy.deepcopy(enhancer._local_model_metadata),
                "supervisor": enhancer._resolve_llama_server(),
                "resolution": resolution,
                **kwargs,
            }
        )
        await asyncio.sleep(0)
        return (
            f"{enhancer._config.enhancement_local_model}:{kwargs['profile_id']}",
            3,
        )

    monkeypatch.setattr(main_module, "run_transcript_evaluation", run_evaluation)

    selections = (
        BakeoffPairSelection("model-a", "chat"),
        BakeoffPairSelection("model-a", "code"),
        BakeoffPairSelection("model-b", "chat"),
    )
    run_id = app.start_enhancement_bakeoff(
        raw_text=raw_text,
        selections=selections,
    )
    await app.enhancement_bakeoff.wait(run_id)

    snapshot = app.enhancement_bakeoff.snapshot(run_id)
    assert snapshot is not None
    assert [call["raw_bytes"] for call in calls] == [
        raw_text.encode("utf-8"),
        raw_text.encode("utf-8"),
        raw_text.encode("utf-8"),
    ]
    assert [call["profile_id"] for call in calls] == ["chat", "code", "chat"]
    assert [call["mode"] for call in calls] == ["batched", "batched", "batched"]
    assert all(call["target_words"] == 20 for call in calls)
    assert all(call["limit_first_chunk"] is False for call in calls)
    assert [call["config"].enhancement_provider for call in calls] == [
        "local",
        "local",
        "local",
    ]
    assert [call["config"].enhancement_local_model for call in calls] == [
        "model-a",
        "model-a",
        "model-b",
    ]
    assert all(call["config"].enhancement_enabled for call in calls)
    assert [call["model_metadata"]["trained_modes"] for call in calls] == [
        ["chat", "code"],
        ["chat", "code"],
        ["chat"],
    ]
    assert [call["resolution"].serving_contract for call in calls] == [
        "raw-v1",
        "raw-v1",
        "wrap-v2",
    ]
    assert calls[0]["supervisor"] is calls[1]["supervisor"]
    assert calls[0]["supervisor"] is not calls[2]["supervisor"]
    assert [pair.output for pair in snapshot.pairs] == [
        "model-a:chat",
        "model-a:code",
        "model-b:chat",
    ]
    assert [pair.metadata.serving_contract for pair in snapshot.pairs] == [
        "raw-v1",
        "raw-v1",
        "wrap-v2",
    ]
    assert [pair.metadata.resident_warm for pair in snapshot.pairs] == [
        False,
        True,
        False,
    ]
    assert all(pair.metadata.load_ms == 137 for pair in snapshot.pairs)
    assert all(pair.metadata.inference_ms is not None for pair in snapshot.pairs)
    assert all(pair.metadata.execution_mode == "batched" for pair in snapshot.pairs)
    assert all(pair.metadata.chunk_count == 3 for pair in snapshot.pairs)
    assert len(pool.entries) == 2
    assert config == original_config


@pytest.mark.asyncio
async def test_production_binding_deep_snapshots_nested_config_and_model_metadata(
    monkeypatch,
) -> None:
    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_local_model="global-model",
        enhancement_chunked_cleanup=True,
        enhancement_words_per_chunk=20,
        profiles=[
            {
                "id": "custom",
                "name": "Custom",
                "prompt": "snapshot prompt",
                "mode": "chat",
            }
        ],
        default_profile_id="custom",
    )
    pool = _FakeResidentPool()
    app = _app_for_production_bakeoff(config, pool)
    model_row = {
        "id": "model-a",
        "prompt_key": "gemma",
        "serving_contract": None,
        "trained_modes": ["chat"],
        "ram_mb": 100,
        "_path": Path("model-a.gguf"),
    }
    monkeypatch.setattr(
        bakeoff_module,
        "get_local_model",
        lambda _model_id: model_row,
        raising=False,
    )
    monkeypatch.setattr(
        bakeoff_module,
        "resolve_local_model_path",
        lambda model: model.get("_path"),
        raising=False,
    )

    observed: dict[str, object] = {}

    async def run_evaluation(enhancer, text, **kwargs):
        observed["prompt"] = enhancer._get_system_prompt(kwargs["profile_id"])
        observed["profiles"] = copy.deepcopy(enhancer._config.profiles)
        observed["trained_modes"] = list(
            enhancer._local_model_metadata["trained_modes"]
        )
        observed["mode"] = kwargs["mode"]
        observed["target_words"] = kwargs["target_words"]
        return text.upper(), 2

    monkeypatch.setattr(main_module, "run_transcript_evaluation", run_evaluation)

    raw_text = " ".join(f"word{index}" for index in range(25))
    run_id = app.start_enhancement_bakeoff(
        raw_text=raw_text,
        selections=(BakeoffPairSelection("model-a", "custom"),),
    )

    config.enhancement_provider = "openai"
    config.enhancement_local_model = "changed-global-model"
    config.enhancement_chunked_cleanup = False
    config.enhancement_words_per_chunk = 500
    config.profiles[0]["prompt"] = "changed global prompt"
    model_row["trained_modes"].append("code")
    await app.enhancement_bakeoff.wait(run_id)

    assert observed == {
        "prompt": "snapshot prompt",
        "profiles": [
            {
                "id": "custom",
                "name": "Custom",
                "prompt": "snapshot prompt",
                "mode": "chat",
            }
        ],
        "trained_modes": ["chat"],
        "mode": "batched",
        "target_words": 20,
    }


@pytest.mark.asyncio
async def test_revoke_all_synchronously_scrubs_content_and_ignores_late_work() -> None:
    lease = EvaluationLease()
    events = []
    manager = EnhancementBakeoffManager(
        lease=lease,
        progress_callback=events.append,
    )
    started = asyncio.Event()
    swallowed_cancel = asyncio.Event()

    async def execute(selection, raw_text, mark_running):
        del selection, raw_text
        assert mark_running()
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed_cancel.set()
            return BakeoffExecutionResult(output="late private output")

    run_id = manager.start(
        raw_text="private raw",
        selections=_pairs(1),
        execute_pair=execute,
    )
    await started.wait()
    before_revoke = len(events)

    manager.revoke_all()

    assert manager.snapshot(run_id) is None
    assert manager.session_count() == 0
    assert manager.retained_char_count() == 0
    assert lease.active_kind() is None
    await swallowed_cancel.wait()
    await asyncio.sleep(0)
    assert len(events) == before_revoke


def test_app_recording_gate_and_cleanup_use_the_bakeoff_manager() -> None:
    lease = EvaluationLease()
    token = lease.try_acquire(EvaluationKind.BAKEOFF)
    assert token is not None

    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.evaluation_lease = lease
    app._dictation_app = object()
    assert app._bakeoff_evaluation_active() is True
    assert app._recording_can_start() is False
    with app._normal_evaluation_scope() as acquired:
        assert acquired is False

    class _Manager:
        def __init__(self) -> None:
            self.revoked = 0

        def revoke_all(self) -> None:
            self.revoked += 1

    manager = _Manager()
    app.enhancement_bakeoff = manager
    app._cleanup_lock = None
    app._cleanup_complete = False
    app._executor = None
    app._persistence_executor = None
    app._import_executor = None
    app.transcript_store = None
    app._last_waveform = None
    app._chunk_futures = []
    app._agent_compose_recording = None
    app._agent_compose_pending_generation = None
    app.text_injector = None

    app.cleanup()

    assert manager.revoked == 1
