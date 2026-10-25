"""Native and deterministic Darwin conformance for audio-policy schema v1."""

from __future__ import annotations

import logging
import platform
import tomllib
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from whisperstream.audio_policy import PlatformRecordingAudioPolicyFactory
from whisperstream.audio_policy_contract import (
    AUDIO_REDUCE_RATIO,
    AudioPolicyFailure,
    AudioPolicyMode,
    AudioPolicyStage,
    ChimeKind,
    OutputEndpointRef,
    RecordingAudioPolicyAdapter,
)
from whisperstream.macos_audio_policy import (
    MacosRecordingAudioPolicyAdapter,
    _MacosOutputObservation,
    _NativeCoreAudioOutputBridge,
    probe_current_output_policy_support,
)


class FakeFeedback:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def play_start_blocking(self, path: str | None) -> bool:
        self.events.append(f"chime:start:{path}")
        return True

    def play_stop(self, path: str | None) -> bool:
        self.events.append(f"chime:stop:{path}")
        return True


@dataclass
class FakeOutput:
    identity: str
    scalar: float | None
    muted: bool | None
    events: list[str]
    volume_writable: bool = True
    mute_writable: bool = True
    connected: bool = True
    fail_volume_writes: int = 0
    fail_mute_writes: int = 0


class FakeBridge:
    def __init__(self, outputs: list[FakeOutput]) -> None:
        self.outputs = outputs
        self.current = 0
        self.resolve_count = 0

    def resolve_current_output(self) -> _MacosOutputObservation:
        self.resolve_count += 1
        output = self.outputs[self.current]
        output.events.append(f"resolve:{output.identity}")
        if not output.connected:
            raise OSError("synthetic endpoint unavailable")
        return _MacosOutputObservation(
            handle=output,
            reference=OutputEndpointRef(f"opaque:{output.identity}"),
            scalar_volume=output.scalar,
            muted=output.muted,
            volume_writable=output.volume_writable,
            mute_writable=output.mute_writable,
        )

    def observe(
        self, observation: _MacosOutputObservation
    ) -> tuple[float | None, bool | None]:
        output = observation.handle
        output.events.append(f"observe:{output.identity}")
        if not output.connected:
            raise OSError("synthetic endpoint disconnected")
        return output.scalar, output.muted

    def set_scalar(
        self, observation: _MacosOutputObservation, scalar: float
    ) -> None:
        output = observation.handle
        output.events.append(f"set-volume:{output.identity}:{scalar:.3f}")
        if not output.connected:
            raise OSError("synthetic endpoint disconnected")
        if output.fail_volume_writes:
            output.fail_volume_writes -= 1
            raise OSError("synthetic volume write failure")
        output.scalar = scalar

    def set_muted(
        self, observation: _MacosOutputObservation, muted: bool
    ) -> None:
        output = observation.handle
        output.events.append(f"set-mute:{output.identity}:{int(muted)}")
        if not output.connected:
            raise OSError("synthetic endpoint disconnected")
        if output.fail_mute_writes:
            output.fail_mute_writes -= 1
            raise OSError("synthetic mute write failure")
        output.muted = muted


def _adapter(
    outputs: list[FakeOutput], events: list[str]
) -> tuple[MacosRecordingAudioPolicyAdapter, FakeBridge]:
    bridge = FakeBridge(outputs)
    return (
        MacosRecordingAudioPolicyAdapter(
            feedback=FakeFeedback(events),
            logger=logging.getLogger("task-4-3.macos-audio-policy"),
            start_path=lambda: "start.wav",
            stop_path=lambda: "stop.wav",
            mode=lambda: AudioPolicyMode.OFF.value,
            _bridge=bridge,
        ),
        bridge,
    )


def test_factory_is_lazy_and_selects_macos_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        "whisperstream.macos_audio_policy._NativeCoreAudioOutputBridge",
        lambda: FakeBridge([]),
    )
    factory = PlatformRecordingAudioPolicyFactory(
        feedback=FakeFeedback(events),
        logger=logging.getLogger("task-4-3.factory"),
        start_path=lambda: None,
        stop_path=lambda: None,
        system="Darwin",
        machine="arm64",
    )

    adapter = factory.create()

    assert isinstance(adapter, RecordingAudioPolicyAdapter)
    assert isinstance(adapter, MacosRecordingAudioPolicyAdapter)
    assert getattr(adapter, "_active_output") is None
    assert events == []


def test_internal_builder_explicitly_includes_lazy_macos_adapter() -> None:
    builder = (Path(__file__).parents[1] / "scripts/build_macos_internal.py").read_text(
        encoding="utf-8"
    )

    assert "--include-module=whisperstream.macos_audio_policy" in builder
    # Runtime versions come from the shared lockfile, not the build-tool list.
    lock = tomllib.loads((Path(__file__).parents[1] / "uv.lock").read_text(encoding="utf-8"))
    pygame = [package for package in lock["package"] if package["name"] == "pygame"]
    assert len(pygame) == 1 and pygame[0]["version"] == "2.6.1"
    assert "--include-package=pygame" in builder
    assert "--nofollow-import-to=pygame" not in builder


def test_start_chime_finishes_before_post_capture_observation() -> None:
    events: list[str] = []
    output = FakeOutput("built-in", 0.8, False, events)
    adapter, _bridge = _adapter([output], events)

    start = adapter.play_chime(1, ChimeKind.START)
    transaction, begun = adapter.begin(1, AudioPolicyMode.REDUCE)

    assert start.stage is AudioPolicyStage.START_CHIME
    assert transaction is not None
    assert begun.changed is True
    assert events[0] == "chime:start:start.wav"
    assert events.index("resolve:built-in") > events.index("chime:start:start.wav")


@pytest.mark.parametrize(
    ("mode", "expected_scalar", "expected_muted"),
    [
        (AudioPolicyMode.OFF, 0.8, False),
        (AudioPolicyMode.REDUCE, 0.8 * AUDIO_REDUCE_RATIO, False),
        (AudioPolicyMode.MUTE, 0.8, True),
    ],
)
def test_modes_restore_exact_snapshot_before_close(
    mode: AudioPolicyMode,
    expected_scalar: float,
    expected_muted: bool,
) -> None:
    events: list[str] = []
    output = FakeOutput("captured", 0.8, False, events)
    adapter, _bridge = _adapter([output], events)

    transaction, begun = adapter.begin(2, mode)
    assert transaction is not None
    assert begun.failure is AudioPolicyFailure.NONE
    assert output.scalar == pytest.approx(expected_scalar)
    assert output.muted is expected_muted

    restored = adapter.restore(transaction)
    closed = adapter.close(2)

    assert restored.failure is AudioPolicyFailure.NONE
    assert closed.failure is AudioPolicyFailure.NONE
    assert output.scalar == pytest.approx(0.8)
    assert output.muted is False
    assert getattr(adapter, "_active_output") is None


def test_default_change_restores_only_the_generation_bound_output() -> None:
    events: list[str] = []
    captured = FakeOutput("captured", 0.6, False, events)
    new_default = FakeOutput("new-default", 0.35, True, events)
    adapter, bridge = _adapter([captured, new_default], events)

    transaction, begun = adapter.begin(3, AudioPolicyMode.MUTE)
    assert transaction is not None and begun.changed
    bridge.current = 1
    restored = adapter.restore(transaction)

    assert restored.failure is AudioPolicyFailure.NONE
    assert (captured.scalar, captured.muted) == pytest.approx((0.6, False))
    assert new_default.scalar == pytest.approx(0.35)
    assert new_default.muted is True
    assert not any(event.startswith("set-") and "new-default" in event for event in events)


@pytest.mark.parametrize(
    ("mode", "volume_writable", "mute_writable"),
    [
        (AudioPolicyMode.REDUCE, False, True),
        (AudioPolicyMode.MUTE, True, False),
        (AudioPolicyMode.MUTE, False, True),
    ],
)
def test_unsupported_native_controls_are_explicit_without_writes(
    mode: AudioPolicyMode,
    volume_writable: bool,
    mute_writable: bool,
) -> None:
    events: list[str] = []
    output = FakeOutput(
        "unsupported",
        0.7,
        False,
        events,
        volume_writable=volume_writable,
        mute_writable=mute_writable,
    )
    adapter, _bridge = _adapter([output], events)

    transaction, result = adapter.begin(4, mode)

    assert transaction is None
    assert result.failure is AudioPolicyFailure.UNSUPPORTED
    assert not any(event.startswith("set-") for event in events)
    assert adapter.close(4).failure is AudioPolicyFailure.NONE


def test_absent_optional_mute_property_does_not_hide_off_or_reduce() -> None:
    events: list[str] = []
    output = FakeOutput(
        "volume-only",
        0.7,
        None,
        events,
        volume_writable=True,
        mute_writable=False,
    )
    adapter, bridge = _adapter([output], events)

    off_transaction, off_result = adapter.begin(41, AudioPolicyMode.OFF)
    assert off_transaction is not None
    assert off_result.failure is AudioPolicyFailure.NONE
    assert adapter.close(41).failure is AudioPolicyFailure.NONE

    reduce_transaction, reduce_result = adapter.begin(42, AudioPolicyMode.REDUCE)
    assert reduce_transaction is not None
    assert reduce_result.changed is True
    assert adapter.restore(reduce_transaction).failure is AudioPolicyFailure.NONE
    assert adapter.close(42).failure is AudioPolicyFailure.NONE

    mute_transaction, mute_result = adapter.begin(43, AudioPolicyMode.MUTE)
    assert mute_transaction is None
    assert mute_result.failure is AudioPolicyFailure.UNSUPPORTED
    assert bridge.resolve_count == 3


def test_off_needs_no_optional_output_control_property() -> None:
    events: list[str] = []
    output = FakeOutput(
        "identity-only",
        None,
        None,
        events,
        volume_writable=False,
        mute_writable=False,
    )
    adapter, _bridge = _adapter([output], events)

    transaction, result = adapter.begin(44, AudioPolicyMode.OFF)

    assert transaction is not None
    assert result.failure is AudioPolicyFailure.NONE
    assert adapter.restore(transaction).failure is AudioPolicyFailure.NONE
    assert adapter.close(44).failure is AudioPolicyFailure.NONE


def test_passive_support_probe_performs_no_writes_and_fails_closed() -> None:
    events: list[str] = []
    output = FakeOutput("supported", 0.5, False, events)
    bridge = FakeBridge([output])

    assert probe_current_output_policy_support(_bridge=bridge) is True
    assert not any(event.startswith("set-") for event in events)

    output.mute_writable = False
    assert probe_current_output_policy_support(_bridge=bridge) is False
    assert not any(event.startswith("set-") for event in events)


def test_capability_endpoint_uses_passive_current_output_result() -> None:
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.set_event_broadcaster = MagicMock()
    app.license_manager.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    client = TestClient(server.fastapi, raise_server_exceptions=True)

    with (
        patch("platform.system", return_value="Darwin"),
        patch(
            "whisperstream.macos_audio_policy.probe_current_output_policy_support",
            return_value=False,
        ) as probe,
    ):
        response = client.get("/api/capabilities/daily-use")

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["capabilities"]}
    assert by_id["audio-policy"] == {
        "id": "audio-policy",
        "state": "unavailable",
        "reason": "software-output-control-unsupported",
        "presentation": "disable",
    }
    probe.assert_called_once_with()


def test_disconnect_and_reconnect_retry_exact_output_without_using_new_default() -> None:
    events: list[str] = []
    captured = FakeOutput("captured", 0.72, False, events)
    new_default = FakeOutput("new-default", 0.4, False, events)
    adapter, bridge = _adapter([captured, new_default], events)
    transaction, begun = adapter.begin(5, AudioPolicyMode.REDUCE)
    assert transaction is not None and begun.changed

    captured.connected = False
    bridge.current = 1
    assert adapter.restore(transaction).failure is AudioPolicyFailure.RESTORE_FAILED
    assert adapter.close(5).failure is AudioPolicyFailure.RESTORE_FAILED
    assert getattr(adapter, "_active_output") is not None

    captured.connected = True
    closed = adapter.close(5)

    assert closed.failure is AudioPolicyFailure.NONE
    assert captured.scalar == pytest.approx(0.72)
    assert new_default.scalar == pytest.approx(0.4)
    assert not any(event.startswith("set-") and "new-default" in event for event in events)


def test_sleep_wake_restore_and_quit_close_leave_no_owned_state() -> None:
    events: list[str] = []
    output = FakeOutput("sleep-wake", 0.5, False, events)
    adapter, _bridge = _adapter([output], events)
    transaction, begun = adapter.begin(6, AudioPolicyMode.MUTE)
    assert transaction is not None and begun.changed

    # A synthetic suspension changes no adapter ownership. Closing after wake
    # must still restore the exact retained output before releasing it.
    output.connected = False
    output.connected = True
    closed = adapter.close(6)

    assert closed.failure is AudioPolicyFailure.NONE
    assert (output.scalar, output.muted) == pytest.approx((0.5, False))
    assert getattr(adapter, "_active_output") is None
    assert getattr(adapter, "_active_transaction") is None


def test_restore_failure_is_retried_on_the_exact_retained_output() -> None:
    events: list[str] = []
    output = FakeOutput("retry", 0.64, False, events)
    adapter, _bridge = _adapter([output], events)
    transaction, begun = adapter.begin(7, AudioPolicyMode.MUTE)
    assert transaction is not None and begun.changed
    output.fail_mute_writes = 1

    restored = adapter.restore(transaction)
    events.append("capture-close")

    assert restored.failure is AudioPolicyFailure.NONE
    assert (output.scalar, output.muted) == pytest.approx((0.64, False))
    assert events.index("set-mute:retry:0") < events.index("capture-close")
    assert sum(event == "set-mute:retry:0" for event in events) == 2


def test_partial_mutation_failure_retains_snapshot_until_restore_succeeds() -> None:
    events: list[str] = []
    output = FakeOutput(
        "partial",
        0.61,
        False,
        events,
        fail_volume_writes=2,
    )
    adapter, _bridge = _adapter([output], events)

    transaction, result = adapter.begin(71, AudioPolicyMode.REDUCE)

    assert transaction is None
    assert result.failure is AudioPolicyFailure.RESTORE_FAILED
    retained = getattr(adapter, "_active_transaction")
    assert retained is not None
    assert output.scalar == pytest.approx(0.61)
    assert adapter.close(71).failure is AudioPolicyFailure.NONE
    assert getattr(adapter, "_active_output") is None


def test_stale_generation_and_endpoint_failure_are_typed() -> None:
    events: list[str] = []
    output = FakeOutput("gone", 0.4, False, events, connected=False)
    adapter, _bridge = _adapter([output], events)

    transaction, result = adapter.begin(8, AudioPolicyMode.REDUCE)

    assert transaction is None
    assert result.failure is AudioPolicyFailure.ENDPOINT_UNAVAILABLE
    assert adapter.close(9).failure is AudioPolicyFailure.STALE_GENERATION
    assert adapter.close(8).failure is AudioPolicyFailure.NONE


def test_repr_and_events_do_not_expose_native_identity() -> None:
    events: list[str] = []
    private_identity = "private-output-device-name"
    output = FakeOutput(private_identity, 0.6, False, events)
    adapter, _bridge = _adapter([output], events)

    transaction, _result = adapter.begin(10, AudioPolicyMode.OFF)

    assert transaction is not None
    assert private_identity not in repr(transaction)
    assert private_identity not in repr(getattr(adapter, "_active_output"))


@pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="native CoreAudio output observation requires Apple Silicon",
)
def test_native_current_output_observation_is_passive_and_content_free() -> None:
    bridge = _NativeCoreAudioOutputBridge()

    observation = bridge.resolve_current_output()
    scalar, muted = bridge.observe(observation)

    assert scalar is None or 0.0 <= scalar <= 1.0
    assert muted is None or isinstance(muted, bool)
    assert observation.reference.token.startswith("macos-output:")
    fields = observation.__dataclass_fields__
    assert "name" not in fields
    assert "uid" not in fields
    expected_support = (
        observation.scalar_volume is not None
        and observation.muted is not None
        and observation.volume_writable
        and observation.mute_writable
    )
    assert probe_current_output_policy_support(_bridge=bridge) is expected_support
