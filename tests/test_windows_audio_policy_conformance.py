"""Deterministic Windows conformance for recording audio-policy schema v1."""

from __future__ import annotations

import logging
import platform
from collections.abc import Callable

import pytest

from whisperstream.audio_policy import PlatformRecordingAudioPolicyFactory
from whisperstream.audio_policy_contract import (
    WINDOWS_REDUCE_TARGET_SCALAR,
    AudioPolicyFailure,
    AudioPolicyMode,
    AudioPolicyStage,
    ChimeKind,
    RecordingAudioPolicyAdapter,
)
from whisperstream.windows_audio_policy import WindowsRecordingAudioPolicyAdapter


pytestmark = pytest.mark.skipif(
    platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"},
    reason="native Windows AMD64 conformance only",
)


class FakeFeedback:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.start_result = True
        self.stop_result = True

    def play_start_blocking(self, path: str | None) -> bool:
        self.events.append(f"chime:start:{path}")
        return self.start_result

    def play_stop(self, path: str | None) -> bool:
        self.events.append(f"chime:stop:{path}")
        return self.stop_result


class FakeEndpoint:
    def __init__(
        self,
        token: str,
        scalar: float,
        muted: bool,
        events: list[str],
    ) -> None:
        self.token = token
        self.scalar = scalar
        self.muted = muted
        self.events = events
        self.fail_volume_writes = 0
        self.fail_mute_writes = 0

    def GetMasterVolumeLevelScalar(self) -> float:
        self.events.append(f"observe-volume:{self.token}")
        return self.scalar

    def GetMute(self) -> int:
        self.events.append(f"observe-mute:{self.token}")
        return int(self.muted)

    def SetMasterVolumeLevelScalar(self, scalar: float, _context: object) -> None:
        self.events.append(f"set-volume:{self.token}:{scalar:.3f}")
        if self.fail_volume_writes:
            self.fail_volume_writes -= 1
            raise OSError("synthetic volume write failure")
        self.scalar = scalar

    def SetMute(self, muted: int, _context: object) -> None:
        self.events.append(f"set-mute:{self.token}:{muted}")
        if self.fail_mute_writes:
            self.fail_mute_writes -= 1
            raise OSError("synthetic mute write failure")
        self.muted = bool(muted)


def _adapter(
    provider: Callable[[], tuple[str, FakeEndpoint]],
    events: list[str],
    *,
    mode: AudioPolicyMode = AudioPolicyMode.OFF,
) -> WindowsRecordingAudioPolicyAdapter:
    return WindowsRecordingAudioPolicyAdapter(
        feedback=FakeFeedback(events),
        logger=logging.getLogger("unit04.audio-policy"),
        start_path=lambda: "start.wav",
        stop_path=lambda: "stop.wav",
        mode=lambda: mode.value,
        endpoint_provider=provider,
    )


def _provider(sequence: list[FakeEndpoint], events: list[str]):
    remaining = iter(sequence)

    def resolve() -> tuple[str, FakeEndpoint]:
        endpoint = next(remaining)
        events.append(f"resolve:{endpoint.token}")
        return endpoint.token, endpoint

    return resolve


def test_factory_is_lazy_and_returns_frozen_adapter() -> None:
    events: list[str] = []
    feedback = FakeFeedback(events)
    adapter = PlatformRecordingAudioPolicyFactory(
        feedback=feedback,
        logger=logging.getLogger("unit04.factory"),
        start_path=lambda: None,
        stop_path=lambda: None,
        system="Windows",
        machine="AMD64",
    ).create()

    assert isinstance(adapter, RecordingAudioPolicyAdapter)
    assert isinstance(adapter, WindowsRecordingAudioPolicyAdapter)
    assert getattr(adapter, "_active_endpoint") is None
    assert events == []


def test_start_and_stop_chimes_keep_exact_blocking_order() -> None:
    events: list[str] = []
    endpoint = FakeEndpoint("wired", 0.75, False, events)
    adapter = _adapter(_provider([endpoint], events), events)

    start = adapter.play_chime(1, ChimeKind.START)
    stop = adapter.play_chime(1, ChimeKind.STOP)

    assert start.stage is AudioPolicyStage.START_CHIME
    assert stop.stage is AudioPolicyStage.STOP_CHIME
    assert events[0] == "chime:start:start.wav"
    assert events[-1] == "chime:stop:stop.wav"


def test_chime_failure_is_explicit_and_does_not_raise() -> None:
    events: list[str] = []
    endpoint = FakeEndpoint("wired", 0.75, False, events)
    adapter = _adapter(_provider([endpoint], events), events)
    adapter._feedback.play_stop = lambda _path: (_ for _ in ()).throw(  # type: ignore[method-assign]
        OSError("synthetic chime failure")
    )

    result = adapter.play_chime(1, ChimeKind.STOP)

    assert result.stage is AudioPolicyStage.STOP_CHIME
    assert result.failure is AudioPolicyFailure.MUTATION_FAILED


@pytest.mark.parametrize(
    ("mode", "expected_scalar", "expected_muted"),
    [
        (AudioPolicyMode.OFF, 0.8, False),
        (AudioPolicyMode.REDUCE, 0.8 * WINDOWS_REDUCE_TARGET_SCALAR, False),
        (AudioPolicyMode.MUTE, 0.8, True),
    ],
)
def test_off_reduce_and_mute_restore_exact_state(
    mode: AudioPolicyMode,
    expected_scalar: float,
    expected_muted: bool,
) -> None:
    events: list[str] = []
    endpoint = FakeEndpoint("wired", 0.8, False, events)
    adapter = _adapter(
        _provider([endpoint, endpoint, endpoint, endpoint], events), events
    )

    transaction, begun = adapter.begin(1, mode)
    assert transaction is not None
    assert begun.failure is AudioPolicyFailure.NONE
    assert endpoint.scalar == pytest.approx(expected_scalar)
    assert endpoint.muted is expected_muted
    restored = adapter.restore(transaction)
    assert restored.failure is AudioPolicyFailure.NONE
    assert endpoint.scalar == pytest.approx(0.8)
    assert endpoint.muted is False
    assert adapter.close(1).failure is AudioPolicyFailure.NONE


def test_bluetooth_transition_restores_recording_endpoint_not_new_default() -> None:
    events: list[str] = []
    a2dp_before = FakeEndpoint("a2dp-before", 0.9, False, events)
    hfp_recording = FakeEndpoint("hfp-recording", 0.6, False, events)
    a2dp_after = FakeEndpoint("a2dp-after", 0.35, True, events)
    adapter = _adapter(
        _provider(
            [a2dp_before, hfp_recording, a2dp_after, a2dp_after],
            events,
        ),
        events,
        mode=AudioPolicyMode.MUTE,
    )

    adapter.play_chime(1, ChimeKind.START)
    transaction, begun = adapter.begin(1, AudioPolicyMode.MUTE)
    assert begun.changed is True
    assert transaction is not None
    assert hfp_recording.muted is True
    adapter.restore(transaction)
    assert hfp_recording.scalar == pytest.approx(0.6)
    assert hfp_recording.muted is False
    assert a2dp_after.scalar == pytest.approx(0.35)
    assert a2dp_after.muted is True
    adapter.play_chime(1, ChimeKind.STOP)
    adapter.close(1)


def test_partial_mutation_rolls_back_and_is_explicit() -> None:
    events: list[str] = []
    endpoint = FakeEndpoint("rollback", 0.55, False, events)
    endpoint.fail_volume_writes = 1
    adapter = _adapter(_provider([endpoint], events), events)

    transaction, result = adapter.begin(1, AudioPolicyMode.REDUCE)

    assert transaction is None
    assert result.failure is AudioPolicyFailure.MUTATION_FAILED
    assert endpoint.scalar == pytest.approx(0.55)
    assert endpoint.muted is False


def test_restore_failure_retries_during_crash_safe_close() -> None:
    events: list[str] = []
    endpoint = FakeEndpoint("retry", 0.7, False, events)
    adapter = _adapter(
        _provider([endpoint, endpoint, endpoint, endpoint], events), events
    )
    transaction, result = adapter.begin(4, AudioPolicyMode.MUTE)
    assert transaction is not None and result.failure is AudioPolicyFailure.NONE
    endpoint.fail_mute_writes = 1

    failed = adapter.restore(transaction)
    closed = adapter.close(4)

    assert failed.failure is AudioPolicyFailure.RESTORE_FAILED
    assert closed.failure is AudioPolicyFailure.NONE
    assert endpoint.scalar == pytest.approx(0.7)
    assert endpoint.muted is False


def test_backend_crash_or_app_quit_close_restores_without_explicit_restore() -> None:
    events: list[str] = []
    endpoint = FakeEndpoint("shutdown", 0.62, False, events)
    adapter = _adapter(_provider([endpoint, endpoint, endpoint], events), events)
    transaction, begun = adapter.begin(6, AudioPolicyMode.REDUCE)
    assert transaction is not None and begun.changed is True

    closed = adapter.close(6)

    assert closed.failure is AudioPolicyFailure.NONE
    assert endpoint.scalar == pytest.approx(0.62)
    assert endpoint.muted is False


def test_endpoint_failure_and_stale_generation_are_typed() -> None:
    events: list[str] = []

    def unavailable() -> tuple[str, FakeEndpoint]:
        raise OSError("default endpoint unavailable")

    adapter = _adapter(unavailable, events)
    transaction, result = adapter.begin(7, AudioPolicyMode.REDUCE)
    assert transaction is None
    assert result.failure is AudioPolicyFailure.ENDPOINT_UNAVAILABLE
    assert adapter.close(8).failure is AudioPolicyFailure.STALE_GENERATION
    assert adapter.close(7).failure is AudioPolicyFailure.NONE
