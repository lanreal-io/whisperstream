"""Selection policy and speech-engine lifecycle tests."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Callable

from whisperstream.speech.selection import (
    EngineManager,
    LOW_MEMORY_HOLD_SECONDS,
    MemorySnapshot,
    SelectionPolicy,
    SelectionResolution,
    classify_kokoro_memory,
    normalize_selection,
)


GIB_MB = 1024


class _Engine:
    def __init__(self, key: str, installed: bool = True) -> None:
        self.key = key
        self.installed = installed
        self.unload_calls = 0

    def is_installed(self) -> bool:
        return self.installed

    def unload(self) -> None:
        self.unload_calls += 1


class _Scheduled:
    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[float, _Scheduled]] = []

    def __call__(self, delay: float, callback: Callable[[], None]) -> _Scheduled:
        handle = _Scheduled(callback)
        self.calls.append((delay, handle))
        return handle


def _manager(
    *,
    kokoro_installed: bool = True,
    supertonic_installed: bool = True,
    scheduler: _Scheduler | None = None,
) -> tuple[EngineManager, dict[str, _Engine]]:
    engines = {
        "kokoro": _Engine("kokoro", kokoro_installed),
        "supertonic": _Engine("supertonic", supertonic_installed),
    }
    manager = EngineManager(
        engine_factory=lambda key: engines[key],
        scheduler=scheduler,
        idle_timeout=17.0,
    )
    return manager, engines


def _policy(
    manager: EngineManager,
    *,
    total_mb: int | None = 32 * GIB_MB,
    available_mb: int | None = 8 * GIB_MB,
) -> SelectionPolicy:
    return SelectionPolicy(
        manager,
        memory_probe=lambda: MemorySnapshot(total_mb, available_mb),
    )


def test_normalize_selection_accepts_only_public_keys() -> None:
    assert normalize_selection("AUTO") == "auto"
    assert normalize_selection(" kokoro ") == "kokoro"
    assert normalize_selection("supertonic") == "supertonic"
    assert normalize_selection(None) == "auto"
    assert normalize_selection("future-engine") == "auto"


def test_kokoro_memory_tiers_need_only_free_memory_and_bound_overrides() -> None:
    assert classify_kokoro_memory(16 * GIB_MB, 2 * GIB_MB) == "recommended"
    # No total-memory minimum: 2 GiB free is enough on any machine.
    assert classify_kokoro_memory(8 * GIB_MB - 1, 4 * GIB_MB) == "recommended"
    assert classify_kokoro_memory(12 * GIB_MB, 1536) == "caution"
    assert classify_kokoro_memory(8 * GIB_MB, GIB_MB) == "caution"
    assert classify_kokoro_memory(8 * GIB_MB - 1, 1536) == "blocked"
    assert classify_kokoro_memory(32 * GIB_MB, GIB_MB - 1) == "blocked"
    assert classify_kokoro_memory(None, 4 * GIB_MB) == "unknown"


def test_automatic_prefers_kokoro_at_locked_memory_thresholds() -> None:
    manager, _engines = _manager()
    resolution = _policy(
        manager,
        total_mb=16 * GIB_MB,
        available_mb=2 * GIB_MB,
    ).resolve(
        "auto",
        supertonic_voice=7,
        kokoro_voice="af_bella",
    )

    assert [(item.engine_key, item.voice_id) for item in resolution.candidates] == [
        ("kokoro", "af_bella"),
        ("supertonic", 7),
    ]
    assert resolution.reason == "kokoro"


def test_automatic_falls_back_for_low_or_unknown_memory() -> None:
    small_total = _policy(_manager()[0], total_mb=4 * GIB_MB).resolve(
        "auto", supertonic_voice=3, kokoro_voice="af_heart"
    )
    low_available = _policy(_manager()[0], available_mb=2 * GIB_MB - 1).resolve(
        "auto", supertonic_voice=3, kokoro_voice="af_heart"
    )
    unknown = _policy(_manager()[0], available_mb=None).resolve(
        "auto", supertonic_voice=3, kokoro_voice="af_heart"
    )

    assert small_total.reason == "kokoro"
    assert [item.engine_key for item in low_available.candidates] == ["supertonic"]
    assert low_available.reason == "low_memory"
    assert [item.engine_key for item in unknown.candidates] == ["supertonic"]
    assert unknown.reason == "unknown_memory"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_automatic_counts_a_resident_kokoro_as_available() -> None:
    manager, _engines = _manager(scheduler=_Scheduler())
    manager.acquire("kokoro").release()

    resolution = _policy(manager, available_mb=512).resolve(
        "auto", supertonic_voice=3, kokoro_voice="af_heart"
    )

    assert [item.engine_key for item in resolution.candidates] == [
        "kokoro",
        "supertonic",
    ]
    assert resolution.reason == "kokoro"


def test_automatic_holds_supertonic_after_a_low_memory_fallback() -> None:
    clock = _Clock()
    engines = {"kokoro": _Engine("kokoro"), "supertonic": _Engine("supertonic")}
    manager = EngineManager(
        engine_factory=lambda key: engines[key],
        scheduler=_Scheduler(),
        clock=clock,
    )
    memory = {"available": 1 * GIB_MB}
    policy = SelectionPolicy(
        manager,
        memory_probe=lambda: MemorySnapshot(32 * GIB_MB, memory["available"]),
    )

    def resolve() -> SelectionResolution:
        return policy.resolve("auto", supertonic_voice=3, kokoro_voice="af_heart")

    assert resolve().reason == "low_memory"
    # Memory recovers, but the next replies inside the hold stay on Supertonic.
    memory["available"] = 8 * GIB_MB
    clock.now += LOW_MEMORY_HOLD_SECONDS - 1
    held = resolve()
    assert [item.engine_key for item in held.candidates] == ["supertonic"]
    assert held.reason == "low_memory"
    clock.now += 1
    assert resolve().reason == "kokoro"


def test_status_resolution_never_starts_a_low_memory_hold() -> None:
    manager, _engines = _manager()
    status_policy = SelectionPolicy(
        manager,
        memory_probe=lambda: MemorySnapshot(32 * GIB_MB, 512),
        hold_on_fallback=False,
    )
    assert status_policy.resolve(
        "auto", supertonic_voice=3, kokoro_voice="af_heart"
    ).reason == "low_memory"
    assert manager.low_memory_hold_active() is False


def test_explicit_choices_ignore_the_low_memory_hold() -> None:
    manager, _engines = _manager()
    manager.hold_low_memory_fallback()
    explicit = _policy(manager).resolve(
        "kokoro", supertonic_voice=3, kokoro_voice="af_heart"
    )
    assert [item.engine_key for item in explicit.candidates] == ["kokoro"]


def test_automatic_applies_install_and_process_health_gates() -> None:
    missing_manager, _engines = _manager(kokoro_installed=False)
    missing = _policy(missing_manager).resolve(
        "auto", supertonic_voice=1, kokoro_voice="af_heart"
    )
    assert [item.engine_key for item in missing.candidates] == ["supertonic"]
    assert missing.reason == "missing_kokoro"

    unhealthy_manager, _engines = _manager()
    unhealthy_manager.mark_unhealthy("kokoro", "load_failed")
    unhealthy = _policy(unhealthy_manager).resolve(
        "auto", supertonic_voice=1, kokoro_voice="af_heart"
    )
    assert [item.engine_key for item in unhealthy.candidates] == ["supertonic"]
    assert unhealthy.reason == "unhealthy_kokoro"

    slow_manager, _engines = _manager()
    slow_manager.mark_unhealthy("kokoro", "slow_first_sentence")
    slow = _policy(slow_manager).resolve(
        "auto", supertonic_voice=1, kokoro_voice="af_heart"
    )
    assert slow.reason == "slow_kokoro"


def test_explicit_engine_never_adds_a_fallback() -> None:
    manager, _engines = _manager()
    policy = _policy(manager, total_mb=4 * GIB_MB, available_mb=512)

    kokoro = policy.resolve(
        "kokoro", supertonic_voice=8, kokoro_voice="af_nicole"
    )
    supertonic = policy.resolve(
        "supertonic", supertonic_voice=8, kokoro_voice="af_nicole"
    )

    assert [(item.engine_key, item.voice_id) for item in kokoro.candidates] == [
        ("kokoro", "af_nicole")
    ]
    assert [(item.engine_key, item.voice_id) for item in supertonic.candidates] == [
        ("supertonic", 8)
    ]


def test_explicit_kokoro_can_retry_an_engine_automatic_marked_unhealthy() -> None:
    manager, _engines = _manager()
    manager.mark_unhealthy("kokoro", "slow_first_sentence")

    explicit = _policy(manager).resolve(
        "kokoro", supertonic_voice=8, kokoro_voice="af_nicole"
    )
    automatic = _policy(manager).resolve(
        "auto", supertonic_voice=8, kokoro_voice="af_nicole"
    )

    assert [(item.engine_key, item.voice_id) for item in explicit.candidates] == [
        ("kokoro", "af_nicole")
    ]
    assert explicit.reason == "slow_kokoro"
    assert [item.engine_key for item in automatic.candidates] == ["supertonic"]
    assert automatic.reason == "slow_kokoro"


def test_mark_healthy_clears_a_process_health_gate() -> None:
    manager, _engines = _manager()
    manager.mark_unhealthy("kokoro", "load_failed")

    manager.mark_healthy("kokoro")

    assert manager.is_healthy("kokoro") is True
    assert manager.health_reason("kokoro") is None


def test_explicit_unavailable_engine_does_not_switch() -> None:
    manager, _engines = _manager(kokoro_installed=False)
    resolution = _policy(manager).resolve(
        "kokoro", supertonic_voice=1, kokoro_voice="af_heart"
    )

    assert resolution.candidates == ()
    assert resolution.reason == "missing_kokoro"
    assert resolution.can_speak is False


def test_voice_identity_is_normalized_without_cross_type_coercion() -> None:
    manager, _engines = _manager()
    policy = _policy(manager)

    auto = policy.resolve(
        "auto",
        supertonic_voice="7",
        kokoro_voice=1,
    )

    assert [(item.engine_key, item.voice_id) for item in auto.candidates] == [
        ("kokoro", "af_heart"),
        ("supertonic", 1),
    ]


def test_engine_manager_caches_engines_and_reference_counts_leases() -> None:
    scheduler = _Scheduler()
    calls: list[str] = []
    engines = {"kokoro": _Engine("kokoro")}
    manager = EngineManager(
        engine_factory=lambda key: calls.append(key) or engines[key],
        scheduler=scheduler,
        idle_timeout=17.0,
    )

    first = manager.acquire("kokoro")
    second = manager.acquire("kokoro")
    assert first.engine is second.engine
    assert calls == ["kokoro"]

    first.release()
    assert scheduler.calls == []
    second.release()
    assert scheduler.calls[0][0] == pytest.approx(17.0, abs=0.01)
    scheduler.calls[0][1].fire()
    assert engines["kokoro"].unload_calls == 1


def test_new_lease_cancels_idle_unload_until_final_release() -> None:
    scheduler = _Scheduler()
    manager, engines = _manager(scheduler=scheduler)

    first = manager.acquire("kokoro")
    first.release()
    old_handle = scheduler.calls[-1][1]
    second = manager.acquire("kokoro")
    assert old_handle.cancelled is True
    old_handle.fire()
    assert engines["kokoro"].unload_calls == 0

    second.release()
    scheduler.calls[-1][1].fire()
    assert engines["kokoro"].unload_calls == 1


def test_manager_install_probe_is_lazy_and_never_loads() -> None:
    calls: list[str] = []
    engine = SimpleNamespace(
        is_installed=lambda: True,
        load=lambda: calls.append("load"),
        unload=lambda: None,
    )
    manager = EngineManager(engine_factory=lambda _key: engine)

    assert manager.is_available("kokoro") is True
    assert calls == []


def test_manager_shutdown_is_bounded_while_a_worker_holds_a_lease() -> None:
    manager, engines = _manager()
    lease = manager.acquire("kokoro")

    drained = manager.shutdown(timeout=0.0)

    assert drained is False
    assert engines["kokoro"].unload_calls == 0
    lease.release()
    assert engines["kokoro"].unload_calls == 1


def test_manager_shutdown_unloads_a_draining_engine_once() -> None:
    manager, engines = _manager()
    lease = manager.acquire("kokoro")

    releaser = threading.Thread(target=lease.release)
    releaser.start()
    assert manager.shutdown(timeout=1.0) is True
    releaser.join(timeout=1.0)

    assert engines["kokoro"].unload_calls == 1
    assert manager.is_available("kokoro") is False


import pytest

@pytest.fixture(autouse=True)
def experimental_voice_runtime(monkeypatch):
    """These cases exercise the retained experimental Kokoro implementation."""
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")


class _DisposableEngine(_Engine):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


@pytest.mark.parametrize("key", ["supertonic", "kokoro"])
def test_shared_idle_policy_counts_from_final_completion_and_rejects_stale_timer(key):
    scheduler = _Scheduler()
    now = [0.0]
    engine = _DisposableEngine(key)
    manager = EngineManager(engine_factory=lambda _: engine, scheduler=scheduler, clock=lambda: now[0])
    first = manager.acquire(key)
    second = manager.acquire(key)
    first.release()
    assert scheduler.calls == []
    now[0] = 100
    second.release()
    assert scheduler.calls[-1][0] == 1800
    stale = scheduler.calls[-1][1]
    now[0] = 200
    manager.configure_residency(enabled=True, preferred="auto", idle_timeout=300)
    assert scheduler.calls[-1][0] == 200
    stale.callback()
    assert engine.unload_calls == 0
    lease = manager.acquire(key)
    scheduler.calls[-1][1].callback()
    assert engine.unload_calls == 0
    lease.release()
    assert scheduler.calls[-1][0] == 300
    scheduler.calls[-1][1].fire()
    assert (engine.unload_calls, engine.dispose_calls) == (1, 0)


def test_never_still_releases_on_disable_and_disabled_samples_get_grace():
    scheduler = _Scheduler()
    engine = _DisposableEngine("supertonic")
    manager = EngineManager(engine_factory=lambda _: engine, scheduler=scheduler, clock=lambda: 0)
    manager.configure_residency(enabled=True, preferred="supertonic", idle_timeout=0)
    manager.acquire("supertonic").release()
    assert scheduler.calls == []
    lease = manager.acquire("supertonic")
    manager.configure_residency(enabled=False, preferred="supertonic", idle_timeout=0)
    assert scheduler.calls == []
    lease.release()
    assert scheduler.calls[-1][0] == 0
    scheduler.calls[-1][1].fire()
    assert engine.unload_calls == 1
    manager.acquire("supertonic").release()
    assert scheduler.calls[-1][0] == 30
    scheduler.calls[-1][1].fire()
    assert engine.unload_calls == 2


def test_switch_and_privacy_teardown_defer_until_worker_finishes():
    scheduler = _Scheduler()
    manager, engines = _manager(scheduler=scheduler)
    lease = manager.acquire("kokoro")
    manager.configure_residency(enabled=True, preferred="supertonic", idle_timeout=0)
    assert scheduler.calls == []
    lease.release()
    assert scheduler.calls[-1][0] == 0
    scheduler.calls[-1][1].fire()
    assert engines["kokoro"].unload_calls == 1
    lease = manager.acquire("supertonic")
    manager.request_unload()
    assert engines["supertonic"].unload_calls == 0
    lease.release()
    assert scheduler.calls[-1][0] == 0
    scheduler.calls[-1][1].fire()
    assert engines["supertonic"].unload_calls == 1


def test_reenable_cancels_retirement_and_install_probe_does_not_schedule():
    scheduler = _Scheduler()
    manager, engines = _manager(scheduler=scheduler)
    manager.is_installed("kokoro")
    manager.configure_residency(enabled=False, preferred="auto", idle_timeout=300)
    assert scheduler.calls == []
    manager.acquire("kokoro").release()
    stale = scheduler.calls[-1][1]
    manager.configure_residency(enabled=True, preferred="auto", idle_timeout=0)
    stale.callback()
    assert engines["kokoro"].unload_calls == 0


def test_controller_uses_saved_policy_despite_setup_preview_and_stop_keeps_warm():
    from unittest.mock import Mock
    from whisperstream.speech.controller import SpeechController

    config = SimpleNamespace(agent_speech_enabled=False, agent_speech_engine="supertonic", agent_speech_idle_timeout_seconds=0)
    controller = SpeechController(config_provider=lambda: config, broadcast=lambda _: None, supervisor_provider=lambda: None)
    scheduler = _Scheduler()
    manager = controller._engine_manager
    engine = _DisposableEngine("supertonic")
    manager._scheduler = scheduler
    manager._clock = lambda: 0
    manager._engine_factory = lambda _: engine
    controller._speech_enabled_provider = lambda: True
    controller.reconcile_residency()
    manager.acquire("supertonic").release()
    assert scheduler.calls[-1][0] == 30
    config.agent_speech_enabled = True
    controller.reconcile_residency()
    controller.stop()
    assert engine.unload_calls == 0
    assert not manager._idle_handles
    controller.stop = Mock()
    config.agent_speech_enabled = False
    controller.reconcile_residency()
    controller.stop.assert_called_once()
    assert scheduler.calls[-1][0] == 0
    scheduler.calls[-1][1].fire()
    assert (engine.unload_calls, engine.dispose_calls) == (1, 0)
    controller.shutdown(timeout=0)


def test_switch_while_disabled_releases_previous_live_sample():
    scheduler = _Scheduler()
    manager, engines = _manager(scheduler=scheduler)
    manager.configure_residency(enabled=False, preferred="supertonic", idle_timeout=1800)
    manager.acquire("supertonic").release()
    manager.configure_residency(enabled=False, preferred="kokoro", idle_timeout=1800)
    assert scheduler.calls[-1][0] == 0
    scheduler.calls[-1][1].fire()
    assert engines["supertonic"].unload_calls == 1


def test_idle_release_unloads_but_shutdown_disposes() -> None:
    scheduler = _Scheduler()
    engine = _DisposableEngine("kokoro")
    manager = EngineManager(
        engine_factory=lambda _key: engine,
        scheduler=scheduler,
        idle_timeout=5.0,
    )
    manager.acquire("kokoro").release()
    scheduler.calls[-1][1].fire()
    assert (engine.unload_calls, engine.dispose_calls) == (1, 0)

    assert manager.shutdown(timeout=0.0) is True
    assert (engine.unload_calls, engine.dispose_calls) == (1, 1)


def test_shutdown_disposes_an_engine_released_after_the_wait() -> None:
    engine = _DisposableEngine("kokoro")
    manager = EngineManager(engine_factory=lambda _key: engine)
    lease = manager.acquire("kokoro")

    assert manager.shutdown(timeout=0.0) is False
    assert engine.dispose_calls == 0
    lease.release()
    assert (engine.unload_calls, engine.dispose_calls) == (0, 1)
