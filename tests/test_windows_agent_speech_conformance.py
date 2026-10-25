"""Deterministic Windows conformance for Agent Speak schema-v1 adapters."""

from __future__ import annotations

import json
import logging
import platform
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from whisperstream.agent_integration import create_agent_integration_adapters
from whisperstream.agent_integration_contract import (
    AgentFailure,
    AgentHookDisposition,
    AgentHookEvent,
    AgentHookRequest,
    AgentInteractionKind,
    AgentLifecycleAction,
    AgentLifecycleRequest,
    AgentLifecycleState,
    AgentProvider,
    AgentSessionRef,
)
from whisperstream.speech.engine_specs import KOKORO_SPEC, SUPERTONIC_SPEC
from whisperstream.speech.selection import EngineManager
from whisperstream.tts_playback import (
    create_speech_playback_adapter,
    create_speech_synthesis_adapter,
)
from whisperstream.tts_playback_contract import (
    SpeechBuffer,
    SpeechEngineChoice,
    SpeechFailure,
    SpeechPlaybackAdapter,
    SpeechRequest,
    SpeechStage,
    SpeechSynthesisAdapter,
    SpeechTerminalReason,
)
from whisperstream.windows_agent_integration import (
    WindowsAgentHookAdapter,
    WindowsAgentPluginAdapter,
)
from whisperstream.windows_tts_playback import (
    WindowsSpeechPlaybackAdapter,
    WindowsSpeechSynthesisAdapter,
)


pytestmark = [
    pytest.mark.usefixtures("accepted_eula"),
    pytest.mark.skipif(
        platform.system() != "Windows"
        or platform.machine().lower() not in {"amd64", "x86_64"},
        reason="native Windows AMD64 conformance only",
    ),
]


class FakeEngine:
    def __init__(self, key: str) -> None:
        self.key = key
        self.loaded = False
        self.unloads = 0

    @staticmethod
    def is_installed() -> bool:
        return True

    def load(self) -> None:
        self.loaded = True

    @property
    def sample_rate(self) -> int:
        return 24_000 if self.key == "kokoro" else 44_100

    def generate(self, text: str, *, sid, speed: float, callback=None):
        del text, sid, speed
        if callback is not None:
            callback(np.ones(8, dtype=np.float32), 1.0)
        return np.linspace(-0.25, 0.25, 16, dtype=np.float32)

    def unload(self) -> None:
        self.loaded = False
        self.unloads += 1


class FakeStream:
    def __init__(self, events: list[str], **kwargs) -> None:
        self.events = events
        self.kwargs = kwargs
        self.frames = 0
        self.aborts = 0
        self.closes = 0
        self.abort_called = threading.Event()

    def start(self) -> None:
        self.events.append("start")

    def write(self, frames: np.ndarray) -> None:
        self.frames += len(frames)
        self.events.append("write")

    def abort(self) -> None:
        self.aborts += 1
        self.events.append("abort")
        self.abort_called.set()

    def stop(self) -> None:
        self.events.append("stop")

    def close(self) -> None:
        self.closes += 1
        self.events.append("close")


class NonIdempotentFakeStream(FakeStream):
    """Model Pa_CloseStream's one-shot lifetime instead of hiding double close."""

    def close(self) -> None:
        self.closes += 1
        self.events.append("close")
        if self.closes > 1:
            raise RuntimeError("native stream closed more than once")


class CoordinatedCloseStream(NonIdempotentFakeStream):
    """Hold the worker's close open so a late interruption race is deterministic."""

    def __init__(self, events: list[str], **kwargs) -> None:
        super().__init__(events, **kwargs)
        self.close_entered = threading.Event()
        self.allow_close = threading.Event()
        self._close_lock = threading.Lock()

    def close(self) -> None:
        with self._close_lock:
            self.closes += 1
            closes = self.closes
        self.events.append("close")
        if closes > 1:
            raise RuntimeError("native stream closed more than once")
        self.close_entered.set()
        assert self.allow_close.wait(timeout=2.0), "worker close was not released"


class BlockingAbortStream(NonIdempotentFakeStream):
    """Hold native abort open while exposing close ordering to the test."""

    def __init__(self, events: list[str], **kwargs) -> None:
        super().__init__(events, **kwargs)
        self.abort_entered = threading.Event()
        self.allow_abort = threading.Event()
        self.close_entered = threading.Event()

    def abort(self) -> None:
        self.aborts += 1
        self.events.append("abort")
        self.abort_entered.set()
        assert self.allow_abort.wait(timeout=2.0), "native abort was not released"
        self.abort_called.set()

    def close(self) -> None:
        self.close_entered.set()
        super().close()


class FakeSoundDevice:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.streams: list[FakeStream] = []

    def OutputStream(self, **kwargs):
        stream = FakeStream(self.events, **kwargs)
        self.streams.append(stream)
        return stream

    @staticmethod
    def WasapiSettings(**kwargs):
        return SimpleNamespace(**kwargs)


def _speech_request(
    generation: int = 1,
    *,
    engine: SpeechEngineChoice = SpeechEngineChoice.SUPERTONIC,
    voice: str = "1",
) -> SpeechRequest:
    return SpeechRequest(
        f"completion-{generation}",
        generation,
        engine,
        voice,
        1.0,
        0.5,
        "Synthetic sentence one. Synthetic sentence two.",
    )


def test_factories_select_concrete_windows_contract_adapters_lazily() -> None:
    manager = EngineManager(engine_factory=FakeEngine)
    synthesis = create_speech_synthesis_adapter(
        engine_manager=manager,
        system="Windows",
        machine="AMD64",
    )
    playback = create_speech_playback_adapter(
        system="Windows",
        machine="AMD64",
    )
    plugin, hook = create_agent_integration_adapters(
        system="Windows",
        machine="AMD64",
    )

    assert isinstance(synthesis, SpeechSynthesisAdapter)
    assert isinstance(playback, SpeechPlaybackAdapter)
    assert isinstance(synthesis, WindowsSpeechSynthesisAdapter)
    assert isinstance(playback, WindowsSpeechPlaybackAdapter)
    assert isinstance(plugin, WindowsAgentPluginAdapter)
    assert isinstance(hook, WindowsAgentHookAdapter)
    assert manager._engines == {}


def test_approved_voice_scope_and_deterministic_synthesis_buffers() -> None:
    assert tuple(voice.id for voice in SUPERTONIC_SPEC.voices) == tuple(range(10))
    assert tuple(voice.id for voice in KOKORO_SPEC.voices) == (
        "af_heart",
        "af_bella",
        "af_nicole",
        "af_aoede",
        "af_kore",
        "af_sarah",
        "am_fenrir",
        "am_michael",
        "am_puck",
    )
    manager = EngineManager(engine_factory=FakeEngine)
    adapter = WindowsSpeechSynthesisAdapter(engine_manager=manager)

    buffers = list(adapter.synthesize(_speech_request()))

    assert [buffer.ordinal for buffer in buffers] == [0, 1]
    assert [buffer.final for buffer in buffers] == [False, True]
    assert all(buffer.sample_rate_hz == 44_100 for buffer in buffers)
    assert all(buffer.channels == 1 for buffer in buffers)
    samples = np.frombuffer(buffers[0].pcm_f32le, dtype="<f4")
    assert np.all(np.isfinite(samples))
    assert np.max(np.abs(samples)) <= 0.125
    assert adapter.shutdown(0.1) is True


def test_voice_outside_exact_scope_fails_before_model_load() -> None:
    manager = EngineManager(engine_factory=FakeEngine)
    adapter = WindowsSpeechSynthesisAdapter(engine_manager=manager)

    with pytest.raises(ValueError, match="approved preset scope"):
        list(adapter.synthesize(_speech_request(voice="99")))

    assert manager._engines == {}


def _output_adapter(events: list[str]) -> tuple[WindowsSpeechPlaybackAdapter, FakeSoundDevice]:
    sounddevice = FakeSoundDevice(events)
    adapter = WindowsSpeechPlaybackAdapter(
        logger=logging.getLogger("unit09.output"),
        sounddevice_module=sounddevice,
        platform_provider=lambda: "win32",
        default_output_name=lambda: "Synthetic Output",
        wasapi_device_resolver=lambda _name: 7,
        mapper_device_resolver=lambda: 8,
    )
    return adapter, sounddevice


def test_output_adapter_resolves_per_utterance_and_cleans_stream() -> None:
    events: list[str] = []
    adapter, sounddevice = _output_adapter(events)
    request = _speech_request()
    buffers = [
        SpeechBuffer(1, 0, 44_100, 1, np.ones(8, dtype="<f4").tobytes(), True)
    ]

    receipt = adapter.play(request, buffers)

    assert receipt.stage is SpeechStage.COMPLETED
    assert receipt.terminal_reason is SpeechTerminalReason.COMPLETED
    assert receipt.buffers_completed == 1
    assert sounddevice.streams[0].kwargs["device"] == 7
    assert events == ["start", "write", "stop", "close"]


def test_output_change_suspend_resume_and_close_interrupt_exact_streams() -> None:
    events: list[str] = []
    adapter, sounddevice = _output_adapter(events)
    first = adapter.open_output_stream(44_100)
    assert adapter.claim_stream(3, first) is True

    adapter.output_device_changed(3)
    assert first.abort_called.wait(timeout=2.0)
    assert sounddevice.streams[0].closes == 0
    adapter.release_stream(3, first)
    adapter.resume()
    second = adapter.open_output_stream(44_100)
    assert adapter.claim_stream(4, second) is True
    adapter.suspend()
    assert second.abort_called.wait(timeout=2.0)
    assert sounddevice.streams[1].closes == 0
    adapter.release_stream(4, second)
    adapter.close()
    adapter.close()

    assert sounddevice.streams[0].aborts == 1
    assert sounddevice.streams[1].aborts == 1
    assert sounddevice.streams[0].closes == 1
    assert sounddevice.streams[1].closes == 1
    assert len(sounddevice.streams) == 2


@pytest.mark.parametrize(
    ("action", "terminal_reason"),
    (
        ("supersede", SpeechTerminalReason.SUPERSEDED),
        ("explicit_stop", SpeechTerminalReason.EXPLICIT_STOP),
        ("close", SpeechTerminalReason.SHUTDOWN),
    ),
)
def test_interruption_paths_leave_close_to_the_playback_worker(
    action: str,
    terminal_reason: SpeechTerminalReason,
) -> None:
    """Repeatedly prove callers abort, while the coordinated worker closes once."""

    for generation in range(1, 21):
        events: list[str] = []
        adapter, _sounddevice = _output_adapter(events)
        stream = NonIdempotentFakeStream(events)
        assert adapter.claim_stream(generation, stream) is True
        release_worker = threading.Event()

        def finalize_from_worker() -> None:
            assert release_worker.wait(timeout=2.0), "worker finalizer was not released"
            adapter.release_stream(generation, stream)

        worker = threading.Thread(target=finalize_from_worker, daemon=True)
        worker.start()
        if action == "supersede":
            adapter.interrupt(generation, SpeechTerminalReason.SUPERSEDED)
        elif action == "explicit_stop":
            adapter.interrupt(generation, SpeechTerminalReason.EXPLICIT_STOP)
        else:
            adapter.close()

        assert stream.abort_called.wait(timeout=2.0)
        assert stream.aborts == 1
        assert stream.closes == 0
        reason, failure = adapter._interruption_for(generation)
        assert reason is terminal_reason
        assert failure is None

        release_worker.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive(), "playback worker did not finalize"
        assert stream.closes == 1
        adapter.finish_generation(generation)


def test_interruption_cannot_touch_stream_after_worker_close_begins() -> None:
    """The ownership handoff linearizes before Pa_CloseStream can start."""

    events: list[str] = []
    adapter, _sounddevice = _output_adapter(events)
    stream = CoordinatedCloseStream(events)
    assert adapter.claim_stream(1, stream) is True
    worker = threading.Thread(
        target=lambda: adapter.release_stream(1, stream),
        daemon=True,
    )
    worker.start()
    assert stream.close_entered.wait(timeout=2.0), "worker never began close"

    interrupter = threading.Thread(
        target=lambda: adapter.interrupt(1, SpeechTerminalReason.SUPERSEDED),
        daemon=True,
    )
    interrupter.start()
    interrupter.join(timeout=2.0)
    assert not interrupter.is_alive(), "late interruption did not return"

    stream.allow_close.set()
    worker.join(timeout=2.0)
    assert not worker.is_alive(), "worker close did not finish"
    assert stream.aborts == 0
    assert stream.closes == 1


def test_blocking_abort_is_async_and_gates_only_its_worker_close() -> None:
    """Hotkey callers and the adapter lock stay free while Pa_AbortStream blocks."""

    events: list[str] = []
    adapter, _sounddevice = _output_adapter(events)
    blocked = BlockingAbortStream(events)
    unrelated = NonIdempotentFakeStream(events)
    assert adapter.claim_stream(1, blocked) is True
    assert adapter.claim_stream(2, unrelated) is True

    adapter.interrupt(1, SpeechTerminalReason.SUPERSEDED)
    assert blocked.abort_entered.wait(timeout=2.0), "daemon abort never started"
    assert blocked.allow_abort.is_set() is False

    # A repeated stop of the blocked stream and an unrelated generation both
    # return before its native abort is released. The unrelated stop must
    # acquire the same adapter-wide ownership lock, proving Pa_AbortStream does
    # not hold it.
    repeated_stop = threading.Thread(
        target=lambda: adapter.interrupt(1, SpeechTerminalReason.EXPLICIT_STOP),
        daemon=True,
    )
    unrelated_stop = threading.Thread(
        target=lambda: adapter.interrupt(2, SpeechTerminalReason.EXPLICIT_STOP),
        daemon=True,
    )
    repeated_stop.start()
    unrelated_stop.start()
    repeated_stop.join(timeout=0.5)
    unrelated_stop.join(timeout=0.5)
    assert not repeated_stop.is_alive(), "repeated stop waited for native abort"
    assert not unrelated_stop.is_alive(), "unrelated stop waited for native abort"
    assert unrelated.abort_called.wait(timeout=2.0)

    finalizer = threading.Thread(
        target=lambda: adapter.release_stream(1, blocked),
        daemon=True,
    )
    finalizer.start()
    assert not blocked.close_entered.wait(timeout=0.05)
    assert finalizer.is_alive(), "worker close did not wait for its abort gate"

    blocked.allow_abort.set()
    assert blocked.abort_called.wait(timeout=2.0)
    finalizer.join(timeout=2.0)
    assert not finalizer.is_alive(), "worker close did not resume after abort"
    assert blocked.closes == 1

    adapter.release_stream(2, unrelated)
    assert unrelated.closes == 1


@pytest.mark.parametrize("later_action", ("suspend", "close"))
@pytest.mark.parametrize(
    ("first_action", "terminal_reason", "failure"),
    (
        ("supersede", SpeechTerminalReason.SUPERSEDED, None),
        ("explicit_stop", SpeechTerminalReason.EXPLICIT_STOP, None),
        (
            "output_change",
            SpeechTerminalReason.PLAYBACK_FAILURE,
            SpeechFailure.OUTPUT_CHANGED,
        ),
    ),
)
def test_first_interruption_reason_survives_suspend_or_close_during_abort(
    first_action: str,
    terminal_reason: SpeechTerminalReason,
    failure: SpeechFailure | None,
    later_action: str,
) -> None:
    events: list[str] = []
    adapter, _sounddevice = _output_adapter(events)
    stream = BlockingAbortStream(events)
    assert adapter.claim_stream(1, stream) is True

    if first_action == "output_change":
        adapter.output_device_changed(1)
    else:
        adapter.interrupt(1, terminal_reason)
    assert stream.abort_entered.wait(timeout=2.0), "daemon abort never started"

    later = threading.Thread(
        target=adapter.close if later_action == "close" else adapter.suspend,
        daemon=True,
    )
    later.start()
    later.join(timeout=0.5)
    assert not later.is_alive(), "later suspend/close waited for native abort"
    assert adapter._interruption_for(1) == (terminal_reason, failure)

    finalizer = threading.Thread(
        target=lambda: adapter.release_stream(1, stream),
        daemon=True,
    )
    finalizer.start()
    assert not stream.close_entered.wait(timeout=0.05)

    stream.allow_abort.set()
    assert stream.abort_called.wait(timeout=2.0)
    finalizer.join(timeout=2.0)
    assert not finalizer.is_alive(), "worker close did not resume after abort"
    assert stream.aborts == 1
    assert stream.closes == 1


def test_owner_closes_output_when_synthesis_shutdown_fails() -> None:
    from whisperstream.speech.playback import PlaybackOwner

    synthesis = MagicMock()
    synthesis.selection_policy = MagicMock()
    synthesis.shutdown.side_effect = RuntimeError("synthetic shutdown failure")
    playback = MagicMock()
    owner = PlaybackOwner(
        engine_manager=MagicMock(),
        broadcast=lambda _event: None,
        synthesis_adapter=synthesis,
        playback_adapter=playback,
    )

    assert owner.shutdown(0.01) is False
    playback.close.assert_called_once_with()


def test_output_change_before_claim_is_typed_and_writes_nothing() -> None:
    events: list[str] = []
    adapter, _sounddevice = _output_adapter(events)
    request = _speech_request()
    buffer = SpeechBuffer(
        1,
        0,
        44_100,
        1,
        np.ones(8, dtype="<f4").tobytes(),
        True,
    )

    adapter.output_device_changed(request.generation)
    receipt = adapter.play(request, [buffer])

    assert receipt.stage is SpeechStage.FAILED
    assert receipt.failure is SpeechFailure.OUTPUT_CHANGED
    assert "write" not in events


def test_plugin_lifecycle_is_generation_bound_and_payload_paths_stay_private() -> None:
    calls: list[str] = []
    adapter = WindowsAgentPluginAdapter(
        enable=lambda **_kwargs: {
            "ok": True,
            "status": "updated",
            "plugin_dir": "PRIVATE_PLUGIN_ROOT",
        },
        disable=lambda: {
            "ok": True,
            "status": "removed",
            "plugin_dir": "PRIVATE_PLUGIN_ROOT",
        },
    )
    reconcile = AgentLifecycleRequest(
        "plugin-1",
        1,
        AgentProvider.CLAUDE_CODE,
        AgentLifecycleAction.RECONCILE,
    )
    result = adapter.apply(reconcile)
    calls.append(result.state.value)
    assert result.state is AgentLifecycleState.UPDATED
    assert "PRIVATE_PLUGIN_ROOT" not in repr(result)
    assert adapter.payload_for("plugin-1", 1)["plugin_dir"] == "PRIVATE_PLUGIN_ROOT"

    stale = adapter.apply(
        AgentLifecycleRequest(
            "plugin-stale",
            0 + 1,
            AgentProvider.CLAUDE_CODE,
            AgentLifecycleAction.REMOVE,
        )
    )
    # Equal generation is idempotently allowed. A later request makes it stale.
    assert stale.state is AgentLifecycleState.REMOVED
    adapter.apply(
        AgentLifecycleRequest(
            "plugin-3",
            3,
            AgentProvider.CLAUDE_CODE,
            AgentLifecycleAction.REMOVE,
        )
    )
    stale = adapter.apply(
        AgentLifecycleRequest(
            "plugin-2",
            2,
            AgentProvider.CLAUDE_CODE,
            AgentLifecycleAction.RECONCILE,
        )
    )
    assert stale.failure is AgentFailure.STALE_GENERATION
    adapter.close()
    assert adapter.payload_for("plugin-3", 3) is None


def _hook_request(request_id: str = "hook-1", generation: int = 1) -> AgentHookRequest:
    return AgentHookRequest(
        request_id,
        generation,
        AgentSessionRef(AgentProvider.CLAUDE_CODE, "PRIVATE_SESSION_TOKEN"),
        AgentHookEvent.PERMISSION_REQUEST,
        1.0,
        json.dumps({"synthetic": True, "body": "PRIVATE_HOOK_BODY"}),
    )


def test_hook_forwarding_resolution_failure_and_content_hiding() -> None:
    adapter = WindowsAgentHookAdapter(
        transport=lambda _body, _event, _deadline: {
            "hookSpecificOutput": {"decision": {"behavior": "allow"}}
        }
    )

    resolved = adapter.forward(_hook_request())

    assert resolved.disposition is AgentHookDisposition.RESOLVED
    assert resolved.interaction is AgentInteractionKind.PERMISSION
    assert "PRIVATE_SESSION_TOKEN" not in repr(resolved)
    assert "PRIVATE_HOOK_BODY" not in repr(resolved)
    timed_out = WindowsAgentHookAdapter(
        transport=MagicMock(side_effect=TimeoutError("synthetic"))
    ).forward(_hook_request("hook-2", 2))
    assert timed_out.disposition is AgentHookDisposition.FAILED_OPEN
    assert timed_out.failure is AgentFailure.TIMEOUT
    assert timed_out.response_json == "{}"


def test_hook_cancel_and_close_release_only_owned_routes() -> None:
    entered = threading.Event()
    release = threading.Event()

    def transport(_body: str, _event: AgentHookEvent, _deadline: float):
        entered.set()
        release.wait(1.0)
        return {}

    adapter = WindowsAgentHookAdapter(transport=transport)
    output: list[object] = []
    worker = threading.Thread(
        target=lambda: output.append(adapter.forward(_hook_request())),
        daemon=True,
    )
    worker.start()
    assert entered.wait(1.0)
    cancelled = adapter.cancel("hook-1", 1)
    release.set()
    worker.join(1.0)

    assert cancelled.disposition is AgentHookDisposition.EXPIRED
    assert output[0].disposition is AgentHookDisposition.EXPIRED
    adapter.close()
    unavailable = adapter.forward(_hook_request("hook-closed", 2))
    assert unavailable.disposition is AgentHookDisposition.FAILED_OPEN
    assert unavailable.response_json == "{}"


def test_ducking_coordination_stops_speech_before_policy_or_license_work() -> None:
    import pytest

    from tests._platform_test_support import StopAfterBargeIn, _bare_app

    order: list[str] = []
    controller = MagicMock()
    controller.stop.side_effect = lambda: order.append("speech-stop")
    app = _bare_app(controller=controller, order=order)
    app.audio_policy = MagicMock()

    with pytest.raises(StopAfterBargeIn):
        app._start_recording()
    assert order == ["speech-stop", "capture"]
    app.audio_policy.begin.assert_not_called()
