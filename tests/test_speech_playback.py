"""Tests for whisperstream.speech.playback (agent-speech Phase 1, Task 1.4).

The PlaybackOwner is the single audio-output owner in the process. These tests use
a deterministic fake engine (controllable synth timing via a gate) and a
monkeypatched ``sounddevice.OutputStream`` (records writes / abort / start /
close). No real audio device, no sherpa-onnx, no model.

Contract under test:
- newest-wins: a second ``speak`` supersedes the first; the first generation writes
  no further frames and does not finish synthesizing its later sentences.
- ``stop()`` is instant + non-blocking: it never joins a worker, returns in well
  under 50ms even while a slow synth is in flight, and aborts the active stream.
- volume gain is multiplied into the written frames.
- speaking -> idle event ordering, exactly one idle per generation, and every
  broadcast payload is content-free (state plus optional completion identity,
  never response text).
- no sounddevice device work at import or construction (the stream opens inside the
  worker, on the first frames).

TRANSCRIPT DATA note: none of these tests assert on real agent response text; the
"secret" string in ``test_no_response_text_in_broadcast_payloads`` exists only to
prove it never leaks into a broadcast payload.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeStream:
    """Stand-in for ``sounddevice.OutputStream`` that records interactions.

    Class-level ``instances`` is reset per test by the ``patched_sd`` fixture. The
    first FakeStream constructed in a test is ``instances[0]`` (the first
    generation to reach playback), the second is ``instances[1]``, etc.
    """

    instances: List["FakeStream"] = []

    def __init__(
        self,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        device: Optional[int] = None,
        extra_settings: Optional[object] = None,
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.device = device
        self.extra_settings = extra_settings
        self.writes: List[np.ndarray] = []
        self.started = False
        self.aborted = False
        self.stopped = False
        self.closed = False
        self.close_calls = 0
        self.abort_called = threading.Event()
        self.start_error: Optional[Exception] = None
        self.write_error_on: Optional[int] = None
        self.write_calls = 0
        FakeStream.instances.append(self)

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def write(self, data: np.ndarray) -> None:
        # Real sounddevice discards buffered audio on abort; the fake simply stops
        # recording further frames so "writes no further frames" is observable.
        self.write_calls += 1
        if self.write_error_on == self.write_calls:
            raise RuntimeError("mid-stream device failure")
        if self.aborted:
            return
        self.writes.append(np.asarray(data).copy())

    def abort(self) -> None:
        self.aborted = True
        self.abort_called.set()

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls > 1:
            raise RuntimeError("native stream closed more than once")
        self.closed = True


class FakeEngine:
    """Deterministic SupertonicEngine stand-in with controllable synth timing.

    ``gate_on`` (1-based generate-call index) blocks that call inside
    ``generate`` until ``release`` is set, after signalling ``entered`` -- this
    lets a test deterministically interleave a supersede / stop while a synth is
    "in flight". ``synth_delay`` adds a plain sleep to every call.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 24000,
        samples: Optional[np.ndarray] = None,
        gate_on: Optional[int] = None,
        synth_delay: float = 0.0,
        load_raises: bool = False,
        generate_raises: bool = False,
    ) -> None:
        self._sample_rate = sample_rate
        self._samples = (
            np.asarray(samples, dtype=np.float32)
            if samples is not None
            else np.ones(8, dtype=np.float32)
        )
        self.gate_on = gate_on
        self.synth_delay = synth_delay
        self.load_raises = load_raises
        self.generate_raises = generate_raises
        self.load_called = 0
        self.generate_texts: List[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._counter_lock = threading.Lock()
        self._n = 0

    def load(self) -> None:
        self.load_called += 1
        if self.load_raises:
            raise FileNotFoundError("voice model not installed")

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def generate(
        self,
        text: str,
        *,
        sid: int = 0,
        speed: float = 1.0,
        callback: Optional[Any] = None,
    ) -> np.ndarray:
        with self._counter_lock:
            self._n += 1
            n = self._n
            self.generate_texts.append(text)
        # Exercise the cancellation callback path (best-effort; real sherpa fires
        # it once per generate). The return value is ignored by the fake.
        if callback is not None:
            callback(self._samples, 1.0)
        if self.gate_on is not None and n == self.gate_on:
            self.entered.set()
            self.release.wait(timeout=3.0)
        if self.synth_delay:
            time.sleep(self.synth_delay)
        if self.generate_raises:
            raise RuntimeError("synth failure")
        return np.array(self._samples, dtype=np.float32)


class Recorder:
    """Thread-safe broadcast sink that also signals when an idle event lands."""

    def __init__(self, raises: bool = False) -> None:
        self.events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._raises = raises

    def __call__(self, event: Dict[str, Any]) -> None:
        if self._raises:
            raise RuntimeError("broadcast boom")
        with self._condition:
            self.events.append(dict(event))
            self._condition.notify_all()

    def wait_idle(self, timeout: float = 3.0, *, count: int = 1) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: sum(
                    event.get("type") == "speech.state"
                    and event.get("state") == "idle"
                    for event in self.events
                )
                >= count,
                timeout=timeout,
            )

    def speech_states(self) -> List[str]:
        with self._lock:
            return [
                e["state"]
                for e in self.events
                if e.get("type") == "speech.state"
            ]

    def payloads(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.events)


class BlockingSpeakingRecorder(Recorder):
    """Pause completion A immediately before its speaking event is recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.a_speaking_entered = threading.Event()
        self.release_a_speaking = threading.Event()

    def __call__(self, event: Dict[str, Any]) -> None:
        if (
            event.get("state") == "speaking"
            and event.get("completion_id") == "completion-a"
        ):
            self.a_speaking_entered.set()
            self.release_a_speaking.wait(timeout=3.0)
        super().__call__(event)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patched_sd(monkeypatch: pytest.MonkeyPatch):
    """Use a non-Windows fake output path unless a test explicitly opts in."""
    from whisperstream.speech import playback

    FakeStream.instances = []
    monkeypatch.setattr(playback.sys, "platform", "linux")
    monkeypatch.setattr(playback.sd, "OutputStream", FakeStream)
    return FakeStream


def _make_owner(engine: FakeEngine, recorder: Recorder):
    from whisperstream.speech.playback import PlaybackOwner

    import logging

    return PlaybackOwner(
        engine_provider=lambda: engine,
        broadcast=recorder,
        logger=logging.getLogger("test.speech.playback"),
    )


def _terminal_recorder():
    reasons: list[str] = []
    called = threading.Event()

    def record(reason: str) -> None:
        reasons.append(reason)
        called.set()

    return reasons, called, record


# ---------------------------------------------------------------------------
# Construction / import: no device work
# ---------------------------------------------------------------------------


def test_construction_opens_no_stream() -> None:
    engine = FakeEngine()
    recorder = Recorder()
    _make_owner(engine, recorder)
    assert FakeStream.instances == []
    assert engine.load_called == 0
    assert recorder.speech_states() == []


class FakeWasapiSettings:
    instances: List["FakeWasapiSettings"] = []

    def __init__(self, *, exclusive: bool, auto_convert: bool) -> None:
        self.exclusive = exclusive
        self.auto_convert = auto_convert
        self.instances.append(self)


def _enable_fake_wasapi(
    monkeypatch: pytest.MonkeyPatch,
    host_api_provider: Any,
) -> None:
    from whisperstream.speech import playback
    from whisperstream.speech.playback import PlaybackOwner

    FakeWasapiSettings.instances = []
    monkeypatch.setattr(playback.sys, "platform", "win32")
    monkeypatch.setattr(playback.sd, "query_hostapis", host_api_provider)
    monkeypatch.setattr(playback.sd, "WasapiSettings", FakeWasapiSettings)
    monkeypatch.setattr(
        PlaybackOwner,
        "_resolve_windows_default_output_name",
        staticmethod(lambda: None),
    )


def _wasapi_host(
    default_output_device: int,
    *,
    devices: tuple[int, ...] = (),
) -> Dict[str, Any]:
    return {
        "name": "Windows WASAPI",
        "default_output_device": default_output_device,
        "devices": devices,
    }


def test_windows_prefers_shared_wasapi_with_format_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_fake_wasapi(
        monkeypatch,
        lambda: [
            {"name": "MME", "default_output_device": 1},
            _wasapi_host(7),
        ],
    )
    owner = _make_owner(FakeEngine(sample_rate=24_000), Recorder())
    reasons, called, callback = _terminal_recorder()

    owner.speak("Shared output.", on_terminal=callback)

    assert called.wait(timeout=3.0)
    assert reasons == ["completed"]
    assert len(FakeStream.instances) == 1
    stream = FakeStream.instances[0]
    assert stream.device == 7
    assert stream.samplerate == 24_000
    settings = stream.extra_settings
    assert isinstance(settings, FakeWasapiSettings)
    assert settings.exclusive is False
    assert settings.auto_convert is True


def test_windows_re_resolves_wasapi_default_for_each_utterance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_devices = iter((4, 9))
    query_calls = 0

    def query_hostapis() -> List[Dict[str, Any]]:
        nonlocal query_calls
        query_calls += 1
        return [_wasapi_host(next(resolved_devices))]

    _enable_fake_wasapi(monkeypatch, query_hostapis)
    owner = _make_owner(FakeEngine(), Recorder())

    for text in ("First.", "Second."):
        reasons, called, callback = _terminal_recorder()
        owner.speak(text, on_terminal=callback)
        assert called.wait(timeout=3.0)
        assert reasons == ["completed"]

    assert query_calls == 2
    assert [stream.device for stream in FakeStream.instances] == [4, 9]


def test_windows_uses_live_core_audio_default_instead_of_stale_host_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.speech import playback
    from whisperstream.speech.playback import PlaybackOwner

    _enable_fake_wasapi(
        monkeypatch,
        lambda: [_wasapi_host(7, devices=(7, 9))],
    )
    monkeypatch.setattr(
        PlaybackOwner,
        "_resolve_windows_default_output_name",
        staticmethod(lambda: "Headphones (AirPods)"),
    )
    monkeypatch.setattr(
        playback.sd,
        "query_devices",
        lambda device: {
            "name": {
                7: "Speakers",
                9: "Headphones (AirPods)",
            }[device],
            "max_output_channels": 2,
        },
    )
    owner = _make_owner(FakeEngine(), Recorder())
    reasons, called, callback = _terminal_recorder()

    owner.speak("Follow Windows.", on_terminal=callback)

    assert called.wait(timeout=3.0)
    assert reasons == ["completed"]
    assert [stream.device for stream in FakeStream.instances] == [9]
    assert isinstance(FakeStream.instances[0].extra_settings, FakeWasapiSettings)


def test_windows_uses_sound_mapper_when_live_endpoint_is_missing_from_wasapi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.speech import playback
    from whisperstream.speech.playback import PlaybackOwner

    _enable_fake_wasapi(
        monkeypatch,
        lambda: [
            {
                "name": "MME",
                "default_output_device": 3,
                "devices": (3,),
            },
            _wasapi_host(7, devices=(7,)),
        ],
    )
    monkeypatch.setattr(
        PlaybackOwner,
        "_resolve_windows_default_output_name",
        staticmethod(lambda: "Headphones (AirPods)"),
    )
    monkeypatch.setattr(
        playback.sd,
        "query_devices",
        lambda device: {
            "name": {
                3: "Microsoft Sound Mapper - Output",
                7: "Speakers",
            }[device],
            "max_output_channels": 2,
        },
    )
    owner = _make_owner(FakeEngine(), Recorder())
    reasons, called, callback = _terminal_recorder()

    owner.speak("Follow a new device.", on_terminal=callback)

    assert called.wait(timeout=3.0)
    assert reasons == ["completed"]
    assert [stream.device for stream in FakeStream.instances] == [3]
    assert FakeStream.instances[0].extra_settings is None


def test_windows_sound_mapper_open_failure_uses_final_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.speech import playback
    from whisperstream.speech.playback import PlaybackOwner

    _enable_fake_wasapi(
        monkeypatch,
        lambda: [
            {
                "name": "MME",
                "default_output_device": 3,
                "devices": (3,),
            },
            _wasapi_host(7, devices=(7,)),
        ],
    )
    monkeypatch.setattr(
        PlaybackOwner,
        "_resolve_windows_default_output_name",
        staticmethod(lambda: "Headphones (AirPods)"),
    )
    monkeypatch.setattr(
        playback.sd,
        "query_devices",
        lambda device: {
            "name": {
                3: "Microsoft Sound Mapper - Output",
                7: "Speakers",
            }[device],
            "max_output_channels": 2,
        },
    )

    def output_stream(**kwargs: Any) -> FakeStream:
        if kwargs.get("device") == 3:
            raise RuntimeError("mapper unavailable")
        return FakeStream(**kwargs)

    monkeypatch.setattr(playback.sd, "OutputStream", output_stream)
    owner = _make_owner(FakeEngine(), Recorder())
    reasons, called, callback = _terminal_recorder()

    owner.speak("Use final fallback.", on_terminal=callback)

    assert called.wait(timeout=3.0)
    assert reasons == ["completed"]
    assert [stream.device for stream in FakeStream.instances] == [None]


def test_windows_retries_wasapi_then_falls_back_and_cleans_failed_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_calls = 0

    def query_hostapis() -> List[Dict[str, Any]]:
        nonlocal query_calls
        query_calls += 1
        return [_wasapi_host(10 + query_calls)]

    _enable_fake_wasapi(monkeypatch, query_hostapis)
    from whisperstream.speech import playback

    def output_stream(**kwargs: Any) -> FakeStream:
        stream = FakeStream(**kwargs)
        if kwargs.get("device") is not None:
            stream.start_error = RuntimeError("device invalidated")
        return stream

    monkeypatch.setattr(playback.sd, "OutputStream", output_stream)
    owner = _make_owner(FakeEngine(sample_rate=44_100), Recorder())
    reasons, called, callback = _terminal_recorder()

    owner.speak("Recover.", on_terminal=callback)

    assert called.wait(timeout=3.0)
    assert reasons == ["completed"]
    # Two WASAPI resolutions, then one lookup for the dynamic Sound Mapper.
    assert query_calls == 3
    assert [stream.device for stream in FakeStream.instances] == [11, 12, None]
    for failed in FakeStream.instances[:2]:
        assert failed.aborted is True
        assert failed.closed is True
        assert failed.writes == []
    fallback = FakeStream.instances[2]
    assert fallback.started is True
    assert len(fallback.writes) == 1
    assert fallback.stopped is True
    assert fallback.closed is True


def test_windows_midstream_failure_never_replays_written_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_fake_wasapi(monkeypatch, lambda: [_wasapi_host(7)])
    from whisperstream.speech import playback

    def output_stream(**kwargs: Any) -> FakeStream:
        stream = FakeStream(**kwargs)
        stream.write_error_on = 2
        return stream

    monkeypatch.setattr(playback.sd, "OutputStream", output_stream)
    owner = _make_owner(FakeEngine(), Recorder())
    reasons, called, callback = _terminal_recorder()

    owner.speak("First. Second.", on_terminal=callback)

    assert called.wait(timeout=3.0)
    assert reasons == ["failed"]
    assert len(FakeStream.instances) == 1
    stream = FakeStream.instances[0]
    assert stream.device == 7
    assert stream.write_calls == 2
    assert len(stream.writes) == 1
    assert stream.stopped is True
    assert stream.closed is True


def test_non_windows_keeps_default_output_stream_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.speech import playback

    def unexpected_device_query() -> object:
        raise AssertionError("non-Windows playback queried a device")

    monkeypatch.setattr(playback.sd, "query_hostapis", unexpected_device_query)
    monkeypatch.setattr(playback.sd, "WasapiSettings", unexpected_device_query)
    owner = _make_owner(FakeEngine(), Recorder())
    reasons, called, callback = _terminal_recorder()

    owner.speak("Default path.", on_terminal=callback)

    assert called.wait(timeout=3.0)
    assert reasons == ["completed"]
    assert len(FakeStream.instances) == 1
    stream = FakeStream.instances[0]
    assert stream.device is None
    assert stream.extra_settings is None


# ---------------------------------------------------------------------------
# Happy path: speak writes frames, speaking -> idle, exactly one idle
# ---------------------------------------------------------------------------


def test_speak_writes_frames_and_emits_speaking_then_idle() -> None:
    engine = FakeEngine(sample_rate=24000, samples=np.ones(8, dtype=np.float32))
    recorder = Recorder()
    owner = _make_owner(engine, recorder)

    owner.speak(
        "Hello world. Second sentence.",
        sid=0,
        speed=1.0,
        volume=1.0,
        completion_id="completion-1",
    )
    assert recorder.wait_idle(), "generation never reached idle"

    # Exactly one OutputStream opened (single owner), correct params.
    assert len(FakeStream.instances) == 1
    stream = FakeStream.instances[0]
    assert stream.samplerate == 24000
    assert stream.channels == 1
    assert stream.dtype == "float32"
    assert stream.started is True
    assert stream.closed is True

    # Two sentences -> two writes into the one stream, but only one speaking event.
    assert len(stream.writes) == 2
    assert recorder.speech_states() == ["speaking", "idle"]
    assert recorder.payloads() == [
        {
            "type": "speech.state",
            "state": "speaking",
            "engine": "supertonic",
            "completion_id": "completion-1",
        },
        {
            "type": "speech.state",
            "state": "idle",
            "engine": "supertonic",
            "completion_id": "completion-1",
        },
    ]


def test_exactly_one_idle_per_generation() -> None:
    engine = FakeEngine()
    recorder = Recorder()
    owner = _make_owner(engine, recorder)
    reasons, called, callback = _terminal_recorder()

    owner.speak(
        "Only one.",
        sid=0,
        speed=1.0,
        volume=1.0,
        on_terminal=callback,
    )
    assert recorder.wait_idle()
    assert called.wait(timeout=3.0)
    assert reasons == ["completed"]
    assert recorder.speech_states() == ["speaking", "idle"]


# ---------------------------------------------------------------------------
# Volume gain
# ---------------------------------------------------------------------------


def test_volume_gain_applied_to_written_frames() -> None:
    engine = FakeEngine(samples=np.ones(8, dtype=np.float32))
    recorder = Recorder()
    owner = _make_owner(engine, recorder)

    owner.speak("Turn it down.", sid=0, speed=1.0, volume=0.25)
    assert recorder.wait_idle()

    stream = FakeStream.instances[0]
    assert len(stream.writes) == 1
    written = stream.writes[0]
    assert np.allclose(written, 0.25), f"expected 0.25 gain, got {written.flatten()[:3]}"


# ---------------------------------------------------------------------------
# Newest-wins: second speak supersedes the first
# ---------------------------------------------------------------------------


def test_newest_wins_supersedes_first_generation() -> None:
    # Engine blocks inside the FIRST generate call so we can deterministically
    # interleave the second speak while the first synth is in flight.
    engine = FakeEngine(gate_on=1, samples=np.ones(8, dtype=np.float32))
    recorder = Recorder()
    owner = _make_owner(engine, recorder)
    old_reasons, old_called, old_callback = _terminal_recorder()
    new_reasons, new_called, new_callback = _terminal_recorder()

    owner.speak(
        "A one. A two.",
        sid=0,
        speed=1.0,
        volume=1.0,
        completion_id="completion-a",
        on_terminal=old_callback,
    )
    assert engine.entered.wait(2.0), "first generation never entered synth"

    # Second speak bumps the generation and supersedes the first.
    owner.speak(
        "B one.",
        sid=0,
        speed=1.0,
        volume=1.0,
        completion_id="completion-b",
        on_terminal=new_callback,
    )
    # Release the first generation; it should notice it is stale and bail.
    engine.release.set()

    assert recorder.wait_idle(), "second generation never reached idle"
    assert old_called.wait(timeout=3.0)
    assert new_called.wait(timeout=3.0)
    assert old_reasons == ["superseded"]
    assert new_reasons == ["completed"]

    # The first generation never synthesized its second sentence.
    assert "A two." not in engine.generate_texts
    # The first generation wrote no frames at all (it bailed right after its
    # blocked first synth, before opening a stream), so the only stream is the
    # second generation's.
    assert len(FakeStream.instances) == 1
    assert engine.generate_texts.count("B one.") == 1
    assert len(FakeStream.instances[0].writes) == 1
    assert recorder.payloads() == [
        {
            "type": "speech.state",
            "state": "speaking",
            "engine": "supertonic",
            "completion_id": "completion-b",
        },
        {
            "type": "speech.state",
            "state": "idle",
            "engine": "supertonic",
            "completion_id": "completion-b",
        },
    ]


def test_superseded_worker_cannot_emit_speaking_after_newer_claim() -> None:
    """A generation's speaking event must linearize before a newer speak call."""
    engine = FakeEngine(samples=np.ones(8, dtype=np.float32))
    recorder = BlockingSpeakingRecorder()
    owner = _make_owner(engine, recorder)
    _old_reasons, old_called, old_callback = _terminal_recorder()
    _new_reasons, new_called, new_callback = _terminal_recorder()

    owner.speak(
        "A one.",
        completion_id="completion-a",
        on_terminal=old_callback,
    )
    assert recorder.a_speaking_entered.wait(2.0), "A never reached speaking emission"

    acquire_attempted = threading.Event()
    wrapped_lock = owner._lock

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "newer-speak":
                acquire_attempted.set()
            wrapped_lock.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            wrapped_lock.release()

    owner._lock = ObservedLock()
    newer = threading.Thread(
        target=lambda: owner.speak(
            "B one.",
            completion_id="completion-b",
            on_terminal=new_callback,
        ),
        name="newer-speak",
        daemon=True,
    )
    newer.start()
    assert acquire_attempted.wait(timeout=2.0), "newer speak never attempted the owner lock"
    assert newer.is_alive(), "newer speak overtook the blocked speaking broadcast"
    recorder.release_a_speaking.set()
    newer.join(timeout=2.0)
    assert not newer.is_alive(), "newer speak call did not return"
    assert old_called.wait(timeout=3.0)
    assert new_called.wait(timeout=3.0)

    speaking_ids = [
        payload.get("completion_id")
        for payload in recorder.payloads()
        if payload.get("state") == "speaking"
    ]
    assert speaking_ids == ["completion-a", "completion-b"]


def test_superseded_speaking_generation_cannot_emit_stale_idle() -> None:
    """Once B owns speaking state, A cannot later mark B's card idle."""
    engine = FakeEngine(gate_on=2, samples=np.ones(8, dtype=np.float32))
    recorder = Recorder()
    owner = _make_owner(engine, recorder)
    old_reasons, old_called, old_callback = _terminal_recorder()
    new_reasons, new_called, new_callback = _terminal_recorder()

    owner.speak(
        "A one. A two.",
        completion_id="completion-a",
        on_terminal=old_callback,
    )
    assert engine.entered.wait(2.0), "first generation never reached second synth"
    assert recorder.payloads() == [
        {
            "type": "speech.state",
            "state": "speaking",
            "engine": "supertonic",
            "completion_id": "completion-a",
        }
    ]

    owner.speak(
        "B one.",
        completion_id="completion-b",
        on_terminal=new_callback,
    )
    assert recorder.wait_idle(), "second generation never reached idle"
    engine.release.set()
    assert old_called.wait(timeout=3.0)
    assert new_called.wait(timeout=3.0)
    assert old_reasons == ["superseded"]
    assert new_reasons == ["completed"]
    assert all(stream.close_calls == 1 for stream in FakeStream.instances)

    assert recorder.payloads() == [
        {
            "type": "speech.state",
            "state": "speaking",
            "engine": "supertonic",
            "completion_id": "completion-a",
        },
        {
            "type": "speech.state",
            "state": "speaking",
            "engine": "supertonic",
            "completion_id": "completion-b",
        },
        {
            "type": "speech.state",
            "state": "idle",
            "engine": "supertonic",
            "completion_id": "completion-b",
        },
    ]


# ---------------------------------------------------------------------------
# stop(): instant, non-blocking, aborts the active stream
# ---------------------------------------------------------------------------


def test_stop_before_any_speak_is_a_noop() -> None:
    engine = FakeEngine()
    recorder = Recorder()
    owner = _make_owner(engine, recorder)

    owner.stop()  # must not raise
    assert FakeStream.instances == []
    assert recorder.speech_states() == []


def test_stop_is_nonblocking_during_slow_synth() -> None:
    # Engine blocks inside the first generate; the worker is stuck in synth.
    engine = FakeEngine(gate_on=1)
    recorder = Recorder()
    owner = _make_owner(engine, recorder)
    reasons, called, callback = _terminal_recorder()

    owner.speak(
        "Slow one.",
        sid=0,
        speed=1.0,
        volume=1.0,
        on_terminal=callback,
    )
    assert engine.entered.wait(2.0)

    t0 = time.perf_counter()
    owner.stop()
    dt = time.perf_counter() - t0
    assert dt < 0.05, f"stop() blocked for {dt * 1000:.1f}ms (must be non-blocking)"

    # Release so the daemon worker can exit cleanly.
    engine.release.set()
    assert called.wait(timeout=3.0)
    assert reasons == ["stopped"]


def test_stop_aborts_active_stream_and_emits_idle() -> None:
    # Engine blocks inside the SECOND generate: by then the stream is open, the
    # first sentence has been written, and "speaking" has been emitted.
    engine = FakeEngine(gate_on=2, samples=np.ones(8, dtype=np.float32))
    recorder = Recorder()
    owner = _make_owner(engine, recorder)
    reasons, called, callback = _terminal_recorder()

    owner.speak(
        "One. Two.",
        sid=0,
        speed=1.0,
        volume=1.0,
        on_terminal=callback,
    )
    assert engine.entered.wait(2.0), "worker never reached the second synth"

    # A stream is open and playing the first sentence.
    assert len(FakeStream.instances) == 1
    stream = FakeStream.instances[0]
    assert stream.started is True
    assert "speaking" in recorder.speech_states()

    owner.stop()
    assert stream.abort_called.wait(timeout=2.0)
    assert stream.aborted is True

    engine.release.set()
    assert recorder.wait_idle(), "stop() did not produce an idle event"
    assert called.wait(timeout=3.0)
    assert reasons == ["stopped"]
    assert recorder.speech_states() == ["speaking", "idle"]
    assert stream.close_calls == 1


def test_shutdown_during_active_playback_leaves_close_to_worker() -> None:
    engine = FakeEngine(gate_on=2, samples=np.ones(8, dtype=np.float32))
    recorder = Recorder()
    owner = _make_owner(engine, recorder)
    reasons, called, callback = _terminal_recorder()

    owner.speak("One. Two.", on_terminal=callback)
    assert engine.entered.wait(2.0), "worker never reached the second synth"
    stream = FakeStream.instances[0]

    assert owner.shutdown(0.01) is False
    assert stream.abort_called.wait(timeout=2.0)
    assert stream.aborted is True
    assert stream.close_calls == 0

    engine.release.set()
    assert called.wait(timeout=3.0)
    assert reasons == ["stopped"]
    assert stream.close_calls == 1


@pytest.mark.parametrize("action, expected", [("stop", "stopped"), ("speak", "superseded")])
def test_interruption_during_final_write_keeps_terminal_reason(
    monkeypatch: pytest.MonkeyPatch, action: str, expected: str,
) -> None:
    from whisperstream.speech import playback

    entered = threading.Event()
    release = threading.Event()

    class BlockingWriteStream(FakeStream):
        def write(self, data: np.ndarray) -> None:
            if self is FakeStream.instances[0]:
                entered.set()
                assert release.wait(3.0)
            super().write(data)

    monkeypatch.setattr(playback.sd, "OutputStream", BlockingWriteStream)
    owner = _make_owner(FakeEngine(), Recorder())
    reasons, called, callback = _terminal_recorder()
    owner.speak("One sentence.", on_terminal=callback)
    assert entered.wait(2.0)
    try:
        if action == "stop":
            owner.stop()
        else:
            owner.speak("Replacement.")
        assert FakeStream.instances[0].abort_called.wait(2.0)
    finally:
        release.set()
    assert called.wait(3.0)
    assert reasons == [expected]


def test_stop_during_close_preserves_completed_terminal_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.speech import playback

    entered = threading.Event()
    release = threading.Event()

    class BlockingCloseStream(FakeStream):
        def close(self) -> None:
            entered.set()
            assert release.wait(3.0)
            super().close()

    monkeypatch.setattr(playback.sd, "OutputStream", BlockingCloseStream)
    owner = _make_owner(FakeEngine(), Recorder())
    reasons, called, callback = _terminal_recorder()
    owner.speak("Finished.", on_terminal=callback)
    assert entered.wait(2.0)
    try:
        owner.stop()
    finally:
        release.set()
    assert called.wait(3.0)
    assert reasons == ["completed"]
    assert not FakeStream.instances[0].aborted


# ---------------------------------------------------------------------------
# Privacy: content-free broadcast payloads
# ---------------------------------------------------------------------------


def test_no_response_text_in_broadcast_payloads() -> None:
    secret = "The launch code is hunter2 and the meeting is at noon."
    engine = FakeEngine(samples=np.ones(8, dtype=np.float32))
    recorder = Recorder()
    owner = _make_owner(engine, recorder)

    owner.speak(secret, sid=0, speed=1.0, volume=1.0)
    assert recorder.wait_idle()

    for payload in recorder.payloads():
        assert set(payload.keys()) == {"type", "state", "engine"}
        assert payload["type"] == "speech.state"
        assert payload["state"] in ("speaking", "idle")
        assert payload["engine"] == "supertonic"
        for value in payload.values():
            assert secret not in str(value)


# ---------------------------------------------------------------------------
# Never-raise public surface
# ---------------------------------------------------------------------------


def test_speak_never_raises_when_engine_load_fails() -> None:
    engine = FakeEngine(load_raises=True)
    recorder = Recorder()
    owner = _make_owner(engine, recorder)
    reasons, called, callback = _terminal_recorder()

    owner.speak(
        "Whatever.",
        sid=0,
        speed=1.0,
        volume=1.0,
        on_terminal=callback,
    )  # must not raise
    assert called.wait(timeout=3.0)
    assert reasons == ["failed"]
    # Load failed before any audio -> no speaking, no spurious idle.
    assert recorder.speech_states() == []
    assert FakeStream.instances == []


def test_speak_never_raises_when_synth_fails() -> None:
    engine = FakeEngine(generate_raises=True)
    recorder = Recorder()
    owner = _make_owner(engine, recorder)
    reasons, called, callback = _terminal_recorder()

    owner.speak(
        "Boom.",
        sid=0,
        speed=1.0,
        volume=1.0,
        on_terminal=callback,
    )  # must not raise
    assert called.wait(timeout=3.0)
    assert reasons == ["failed"]
    assert recorder.speech_states() == []


def test_broadcast_error_is_swallowed() -> None:
    engine = FakeEngine(samples=np.ones(8, dtype=np.float32))
    recorder = Recorder(raises=True)
    owner = _make_owner(engine, recorder)
    reasons, called, callback = _terminal_recorder()

    # broadcast raising must not crash the worker or propagate out of speak().
    owner.speak(
        "Hello there.",
        sid=0,
        speed=1.0,
        volume=1.0,
        on_terminal=callback,
    )
    assert called.wait(timeout=3.0)
    assert reasons == ["completed"]
    # Nothing to assert on the recorder (it raises); the point is no crash.


# ---------------------------------------------------------------------------
# speak_if_idle (Phase 2 — low-priority template utterances)
# ---------------------------------------------------------------------------


def _wait_idle(
    owner,
    recorder: Recorder,
    *,
    count: int = 1,
    timeout: float = 2.0,
) -> None:
    """Wait for the terminal idle broadcast and every worker to exit."""
    assert recorder.wait_idle(timeout=timeout, count=count), (
        "playback did not emit idle in time"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with owner._lock:
            if owner._busy_workers == 0:
                return
        time.sleep(0.01)
    raise AssertionError("playback workers did not drain in time")


def test_speak_if_idle_speaks_when_idle() -> None:
    engine = FakeEngine(samples=np.ones(8, dtype=np.float32))
    recorder = Recorder()
    owner = _make_owner(engine, recorder)

    claimed = owner.speak_if_idle("A subtask finished.", sid=0, speed=1.0, volume=1.0)

    assert claimed is True
    _wait_idle(owner, recorder)
    assert engine.generate_texts == ["A subtask finished."]
    assert len(FakeStream.instances) == 1
    assert FakeStream.instances[0].writes  # frames actually played


def test_speak_if_idle_refuses_during_pending_synthesis() -> None:
    """The pending window (speak() called, no stream yet) counts as busy:
    a progress line must never supersede a response that is about to start."""
    engine = FakeEngine(gate_on=1)  # first generate blocks until released
    recorder = Recorder()
    owner = _make_owner(engine, recorder)

    owner.speak("The full response.", sid=0, speed=1.0, volume=1.0)
    assert engine.entered.wait(timeout=2.0)  # worker is mid-synth, no stream yet

    claimed = owner.speak_if_idle("A subtask finished.")
    assert claimed is False

    engine.release.set()
    _wait_idle(owner, recorder)
    # The response played untouched; the progress line never synthesized.
    assert engine.generate_texts == ["The full response."]


def test_speak_if_idle_allowed_again_after_drain() -> None:
    engine = FakeEngine(samples=np.ones(8, dtype=np.float32))
    recorder = Recorder()
    owner = _make_owner(engine, recorder)

    owner.speak("First response.", sid=0, speed=1.0, volume=1.0)
    _wait_idle(owner, recorder)

    assert owner.speak_if_idle("A subtask finished.") is True
    _wait_idle(owner, recorder, count=2)
    assert engine.generate_texts == ["First response.", "A subtask finished."]


def test_speak_supersedes_speak_if_idle() -> None:
    """High-priority speech still interrupts a playing template (newest-wins)."""
    engine = FakeEngine(gate_on=1)
    recorder = Recorder()
    owner = _make_owner(engine, recorder)

    assert owner.speak_if_idle("A subtask finished.") is True
    assert engine.entered.wait(timeout=2.0)

    owner.speak("Claude needs your approval to continue.", sid=0, speed=1.0, volume=1.0)
    engine.release.set()
    _wait_idle(owner, recorder)

    # The template's generation went stale; the interrupt line played.
    assert "Claude needs your approval to continue." in engine.generate_texts


def test_terminal_callback_reports_normal_completion_once() -> None:
    engine = FakeEngine()
    owner = _make_owner(engine, Recorder())
    reasons, called, callback = _terminal_recorder()

    owner.speak("Finished.", on_terminal=callback)

    assert called.wait(timeout=3.0)
    assert reasons == ["completed"]


def test_terminal_callback_reports_stop_and_supersede() -> None:
    stop_engine = FakeEngine(gate_on=1)
    stop_owner = _make_owner(stop_engine, Recorder())
    stop_reasons, stop_called, stop_callback = _terminal_recorder()
    stop_owner.speak("First.", on_terminal=stop_callback)
    assert stop_engine.entered.wait(timeout=3.0)
    stop_owner.stop()
    stop_engine.release.set()
    assert stop_called.wait(timeout=3.0)
    assert stop_reasons == ["stopped"]

    supersede_engine = FakeEngine(gate_on=1)
    supersede_owner = _make_owner(supersede_engine, Recorder())
    old_reasons, old_called, old_callback = _terminal_recorder()
    new_reasons, new_called, new_callback = _terminal_recorder()
    supersede_owner.speak("Old.", on_terminal=old_callback)
    assert supersede_engine.entered.wait(timeout=3.0)
    supersede_owner.speak("New.", on_terminal=new_callback)
    supersede_engine.release.set()
    assert old_called.wait(timeout=3.0)
    assert new_called.wait(timeout=3.0)
    assert old_reasons == ["superseded"]
    assert new_reasons == ["completed"]


def test_terminal_callback_reports_preplayback_failure() -> None:
    engine = FakeEngine(load_raises=True)
    owner = _make_owner(engine, Recorder())
    reasons, called, callback = _terminal_recorder()

    owner.speak("Unavailable.", on_terminal=callback)

    assert called.wait(timeout=3.0)
    assert reasons == ["failed"]


# ---------------------------------------------------------------------------
# Multi-engine pre-stream selection and fallback
# ---------------------------------------------------------------------------


class CandidateEngine(FakeEngine):
    def __init__(self, key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.key = key
        self.events: list[str] = []
        self.sids: list[int | str] = []
        self.unload_calls = 0

    def is_installed(self) -> bool:
        return True

    def load(self) -> None:
        self.events.append("load")
        super().load()

    @property
    def sample_rate(self) -> int:
        self.events.append("sample_rate")
        assert self.generate_texts, "sample rate was read before first generation"
        return self._sample_rate

    def generate(
        self,
        text: str,
        *,
        sid: int | str = 0,
        speed: float = 1.0,
        callback: Optional[Any] = None,
    ) -> np.ndarray:
        self.events.append("generate")
        self.sids.append(sid)
        return super().generate(
            text,
            sid=0,
            speed=speed,
            callback=callback,
        )

    def unload(self) -> None:
        self.unload_calls += 1


def _make_multi_owner(
    engines: dict[str, CandidateEngine],
    recorder: Recorder,
    *,
    clock=lambda: 0.0,
):
    # The default clock is frozen. Fake engines return a few frames (well under
    # a millisecond of audio), so measuring real time against the Kokoro RTF
    # ceiling marks Kokoro slow whenever the machine is loaded. Tests that
    # exercise the slow-first-sentence path inject explicit ticks instead.
    from whisperstream.speech.playback import PlaybackOwner
    from whisperstream.speech.selection import EngineManager

    manager = EngineManager(
        engine_factory=lambda key: engines[key],
        idle_timeout=60.0,
    )
    owner = PlaybackOwner(
        engine_manager=manager,
        broadcast=recorder,
        first_sentence_clock=clock,
    )
    return owner, manager


def test_automatic_discards_high_rtf_kokoro_frames_and_uses_supertonic() -> None:
    from whisperstream.speech.selection import EngineCandidate

    kokoro = CandidateEngine(
        "kokoro",
        sample_rate=24_000,
        samples=np.full(48_000, 0.25, dtype=np.float32),
    )
    supertonic = CandidateEngine(
        "supertonic",
        sample_rate=44_100,
        samples=np.full(8, 0.75, dtype=np.float32),
    )
    ticks = iter((0.0, 1.01, 2.0, 2.1))
    recorder = Recorder()
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        recorder,
        clock=lambda: next(ticks),
    )

    owner.speak(
        "One sentence.",
        candidates=(
            EngineCandidate("kokoro", "af_bella"),
            EngineCandidate("supertonic", 7),
        ),
    )
    assert recorder.wait_idle()

    assert kokoro.sids == ["af_bella"]
    assert supertonic.sids == [7]
    assert manager.health_reason("kokoro") == "slow_first_sentence"
    assert len(FakeStream.instances) == 1
    assert FakeStream.instances[0].samplerate == 44_100
    assert np.allclose(FakeStream.instances[0].writes[0], 0.75)


def test_automatic_keeps_long_kokoro_sentence_with_healthy_rtf() -> None:
    from whisperstream.speech.selection import EngineCandidate

    kokoro = CandidateEngine(
        "kokoro",
        sample_rate=24_000,
        samples=np.full(192_000, 0.25, dtype=np.float32),
    )
    supertonic = CandidateEngine(
        "supertonic",
        sample_rate=44_100,
        samples=np.full(8, 0.75, dtype=np.float32),
    )
    ticks = iter((0.0, 2.0))
    recorder = Recorder()
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        recorder,
        clock=lambda: next(ticks),
    )

    owner.speak(
        "A long but efficiently generated first sentence.",
        candidates=(
            EngineCandidate("kokoro", "af_bella"),
            EngineCandidate("supertonic", 7),
        ),
    )
    assert recorder.wait_idle()

    assert kokoro.sids == ["af_bella"]
    assert supertonic.sids == []
    assert manager.health_reason("kokoro") is None
    assert len(FakeStream.instances) == 1
    assert FakeStream.instances[0].samplerate == 24_000
    assert np.allclose(FakeStream.instances[0].writes[0], 0.25)


def test_explicit_kokoro_plays_even_when_first_sentence_exceeds_auto_rtf() -> None:
    from whisperstream.speech.selection import EngineCandidate

    kokoro = CandidateEngine(
        "kokoro",
        sample_rate=24_000,
        samples=np.full(48_000, 0.25, dtype=np.float32),
    )
    ticks = iter((0.0, 1.51))
    recorder = Recorder()
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro},
        recorder,
        clock=lambda: next(ticks),
    )

    owner.speak(
        "Explicit slow sentence.",
        candidates=(EngineCandidate("kokoro", "af_bella"),),
    )
    assert recorder.wait_idle()

    assert manager.health_reason("kokoro") is None
    assert owner.active_engine() is None
    assert [event["engine"] for event in recorder.payloads()] == [
        "kokoro",
        "kokoro",
    ]
    assert len(FakeStream.instances) == 1
    assert FakeStream.instances[0].samplerate == 24_000
    assert np.allclose(FakeStream.instances[0].writes[0], 0.25)


def test_automatic_first_sentence_ceiling_excludes_cold_load() -> None:
    from whisperstream.speech.selection import EngineCandidate

    now = [0.0]

    class SlowLoad(CandidateEngine):
        def load(self) -> None:
            super().load()
            now[0] = 1.51

    kokoro = SlowLoad("kokoro", sample_rate=24_000)
    supertonic = CandidateEngine("supertonic", sample_rate=44_100)
    recorder = Recorder()
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        recorder,
        clock=lambda: now[0],
    )

    owner.speak(
        "Cold start sentence.",
        candidates=(
            EngineCandidate("kokoro", "af_heart"),
            EngineCandidate("supertonic", 4),
        ),
    )
    assert recorder.wait_idle()

    assert kokoro.generate_texts == ["Cold start sentence."]
    assert supertonic.generate_texts == []
    assert manager.health_reason("kokoro") is None
    assert len(FakeStream.instances) == 1
    assert FakeStream.instances[0].samplerate == 24_000


@pytest.mark.parametrize(
    ("engine_kwargs", "health_reason"),
    [
        ({"load_raises": True}, "load_failed"),
        ({"samples": np.array([], dtype=np.float32)}, "invalid_output"),
        ({"samples": np.array([np.nan], dtype=np.float32)}, "invalid_output"),
    ],
)
def test_automatic_falls_back_before_stream_and_marks_kokoro_unhealthy(
    engine_kwargs: dict[str, Any],
    health_reason: str,
) -> None:
    from whisperstream.speech.selection import EngineCandidate

    kokoro = CandidateEngine("kokoro", **engine_kwargs)
    supertonic = CandidateEngine("supertonic", sample_rate=44_100)
    recorder = Recorder()
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        recorder,
    )

    owner.speak(
        "Fallback sentence.",
        candidates=(
            EngineCandidate("kokoro", "af_heart"),
            EngineCandidate("supertonic", 2),
        ),
    )
    assert recorder.wait_idle()

    assert supertonic.generate_texts == ["Fallback sentence."]
    assert manager.health_reason("kokoro") == health_reason
    assert FakeStream.instances[0].samplerate == 44_100


def test_generate_exception_falls_back_without_poisoning_kokoro() -> None:
    from whisperstream.speech.engine_specs import SpeechInputError
    from whisperstream.speech.selection import EngineCandidate

    class RejectsOneUtterance(CandidateEngine):
        def generate(self, text: str, **_kwargs: Any) -> np.ndarray:
            self.events.append("generate")
            self.generate_texts.append(text)
            raise SpeechInputError("one sentence cannot be pronounced")

    kokoro = RejectsOneUtterance("kokoro")
    supertonic = CandidateEngine("supertonic", sample_rate=44_100)
    recorder = Recorder()
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        recorder,
    )

    owner.speak(
        "Content-specific failure.",
        candidates=(
            EngineCandidate("kokoro", "af_heart"),
            EngineCandidate("supertonic", 2),
        ),
    )
    assert recorder.wait_idle()

    assert supertonic.generate_texts == ["Content-specific failure."]
    assert manager.health_reason("kokoro") is None
    assert manager.is_healthy("kokoro") is True


def test_structural_generate_exception_falls_back_and_quarantines_kokoro() -> None:
    from whisperstream.speech.selection import EngineCandidate

    kokoro = CandidateEngine("kokoro", generate_raises=True)
    supertonic = CandidateEngine("supertonic", sample_rate=44_100)
    recorder = Recorder()
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        recorder,
    )

    owner.speak(
        "Runtime failure.",
        candidates=(
            EngineCandidate("kokoro", "af_heart"),
            EngineCandidate("supertonic", 2),
        ),
    )
    assert recorder.wait_idle()

    assert supertonic.generate_texts == ["Runtime failure."]
    assert manager.health_reason("kokoro") == "generate_failed"
    assert {
        "type": "speech.engine_health",
        "engine": "kokoro",
        "healthy": False,
        "reason": "generate_failed",
    } in recorder.payloads()


def test_explicit_kokoro_failure_never_speaks_supertonic() -> None:
    from whisperstream.speech.selection import EngineCandidate

    kokoro = CandidateEngine("kokoro", load_raises=True)
    supertonic = CandidateEngine("supertonic")
    recorder = Recorder()
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        recorder,
    )
    reasons, called, callback = _terminal_recorder()

    owner.speak(
        "Explicit sentence.",
        candidates=(EngineCandidate("kokoro", "af_heart"),),
        on_terminal=callback,
    )
    assert called.wait(timeout=3.0)

    assert reasons == ["failed"]
    assert supertonic.generate_texts == []
    assert manager.health_reason("kokoro") == "load_failed"
    assert recorder.payloads() == [
        {
            "type": "speech.engine_health",
            "engine": "kokoro",
            "healthy": False,
            "reason": "load_failed",
        }
    ]


def test_successful_explicit_kokoro_retry_clears_health_gate() -> None:
    from whisperstream.speech.selection import EngineCandidate

    kokoro = CandidateEngine("kokoro")
    recorder = Recorder()
    owner, manager = _make_multi_owner({"kokoro": kokoro}, recorder)
    manager.mark_unhealthy("kokoro", "load_failed")

    owner.speak(
        "Direct retry.",
        candidates=(EngineCandidate("kokoro", "af_heart"),),
    )
    assert recorder.wait_idle()

    assert manager.health_reason("kokoro") is None
    assert recorder.speech_states() == ["speaking", "idle"]


def test_stale_generation_cannot_change_kokoro_health() -> None:
    kokoro = CandidateEngine("kokoro")
    owner, manager = _make_multi_owner({"kokoro": kokoro}, Recorder())

    manager.mark_unhealthy("kokoro", "load_failed")
    owner.stop()  # Generation 0 is now stale.
    assert owner._mark_engine_healthy("kokoro", 0) is False
    assert manager.health_reason("kokoro") == "load_failed"

    manager.mark_healthy("kokoro")
    assert owner._mark_engine_unhealthy("kokoro", "generate_failed", 0) is False
    assert manager.health_reason("kokoro") is None


def test_stop_during_first_candidate_never_triggers_fallback() -> None:
    from whisperstream.speech.selection import EngineCandidate

    kokoro = CandidateEngine("kokoro", gate_on=1)
    supertonic = CandidateEngine("supertonic")
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        Recorder(),
    )
    reasons, called, callback = _terminal_recorder()

    owner.speak(
        "Cancelled sentence.",
        candidates=(
            EngineCandidate("kokoro", "af_heart"),
            EngineCandidate("supertonic", 1),
        ),
        on_terminal=callback,
    )
    assert kokoro.entered.wait(timeout=2.0)
    owner.stop()
    kokoro.release.set()
    assert called.wait(timeout=3.0)

    assert reasons == ["stopped"]
    assert supertonic.generate_texts == []
    assert manager.is_healthy("kokoro") is True
    assert FakeStream.instances == []


def test_supersede_during_first_candidate_never_triggers_its_fallback() -> None:
    from whisperstream.speech.selection import EngineCandidate

    kokoro = CandidateEngine("kokoro", gate_on=1)
    supertonic = CandidateEngine("supertonic")
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        Recorder(),
    )
    old_reasons, old_called, old_callback = _terminal_recorder()
    new_reasons, new_called, new_callback = _terminal_recorder()

    owner.speak(
        "Old automatic sentence.",
        candidates=(
            EngineCandidate("kokoro", "af_heart"),
            EngineCandidate("supertonic", 1),
        ),
        on_terminal=old_callback,
    )
    assert kokoro.entered.wait(timeout=2.0)
    owner.speak(
        "New explicit sentence.",
        candidates=(EngineCandidate("supertonic", 8),),
        on_terminal=new_callback,
    )
    kokoro.release.set()
    assert old_called.wait(timeout=3.0)
    assert new_called.wait(timeout=3.0)

    assert old_reasons == ["superseded"]
    assert new_reasons == ["completed"]
    assert supertonic.generate_texts == ["New explicit sentence."]
    assert manager.is_healthy("kokoro") is True


def test_engine_voice_and_sample_rate_stay_pinned_after_stream_opens() -> None:
    from whisperstream.speech.selection import EngineCandidate

    kokoro = CandidateEngine(
        "kokoro",
        sample_rate=24_000,
        generate_raises=False,
    )
    supertonic = CandidateEngine("supertonic", sample_rate=44_100)
    recorder = Recorder()
    owner, _manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        recorder,
    )

    owner.speak(
        "First. Second.",
        candidates=(
            EngineCandidate("kokoro", "af_nicole"),
            EngineCandidate("supertonic", 8),
        ),
    )
    assert recorder.wait_idle()

    assert kokoro.generate_texts == ["First.", "Second."]
    assert kokoro.sids == ["af_nicole", "af_nicole"]
    assert supertonic.generate_texts == []
    assert FakeStream.instances[0].samplerate == 24_000
    assert kokoro.events[:3] == ["load", "generate", "sample_rate"]


def test_structural_failure_after_stream_opens_poison_but_does_not_fall_back() -> None:
    from whisperstream.speech.selection import EngineCandidate

    class FailsSecond(CandidateEngine):
        def generate(self, text: str, **kwargs: Any) -> np.ndarray:
            if self.generate_texts:
                raise RuntimeError("second sentence failed")
            return super().generate(text, **kwargs)

    kokoro = FailsSecond("kokoro", sample_rate=24_000)
    supertonic = CandidateEngine("supertonic", sample_rate=44_100)
    recorder = Recorder()
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        recorder,
    )
    reasons, called, callback = _terminal_recorder()

    owner.speak(
        "First. Second.",
        candidates=(
            EngineCandidate("kokoro", "af_heart"),
            EngineCandidate("supertonic", 1),
        ),
        on_terminal=callback,
    )
    assert called.wait(timeout=3.0)

    assert reasons == ["failed"]
    assert len(FakeStream.instances) == 1
    assert FakeStream.instances[0].samplerate == 24_000
    assert supertonic.generate_texts == []
    assert manager.health_reason("kokoro") == "generate_failed"
    assert {
        "type": "speech.engine_health",
        "engine": "kokoro",
        "healthy": False,
        "reason": "generate_failed",
    } in recorder.payloads()


def test_content_failure_after_stream_opens_does_not_poison_or_fall_back() -> None:
    from whisperstream.speech.engine_specs import SpeechInputError
    from whisperstream.speech.selection import EngineCandidate

    class RejectsSecond(CandidateEngine):
        def generate(self, text: str, **kwargs: Any) -> np.ndarray:
            if self.generate_texts:
                raise SpeechInputError("second sentence cannot be pronounced")
            return super().generate(text, **kwargs)

    kokoro = RejectsSecond("kokoro", sample_rate=24_000)
    supertonic = CandidateEngine("supertonic", sample_rate=44_100)
    recorder = Recorder()
    owner, manager = _make_multi_owner(
        {"kokoro": kokoro, "supertonic": supertonic},
        recorder,
    )
    reasons, called, callback = _terminal_recorder()

    owner.speak(
        "First. Second.",
        candidates=(
            EngineCandidate("kokoro", "af_heart"),
            EngineCandidate("supertonic", 1),
        ),
        on_terminal=callback,
    )
    assert called.wait(timeout=3.0)

    assert reasons == ["failed"]
    assert len(FakeStream.instances) == 1
    assert supertonic.generate_texts == []
    assert manager.health_reason("kokoro") is None


def test_empty_candidate_plan_never_falls_back_to_legacy_engine() -> None:
    engine = FakeEngine()
    owner = _make_owner(engine, Recorder())
    reasons, called, callback = _terminal_recorder()

    owner.speak("Unavailable.", candidates=(), on_terminal=callback)

    assert called.wait(timeout=3.0)
    assert reasons == ["failed"]
    assert engine.load_called == 0

@pytest.fixture(autouse=True)
def _enable_optional_kokoro(monkeypatch):
    # Exercise retained two-engine behavior; final-edition boundary tests cover availability.
    from whisperstream import _build_flags
    monkeypatch.setattr(_build_flags, "EXPERIMENTAL_AVAILABLE", True)
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")


@pytest.mark.parametrize("replace_after_stops", [False, True])
def test_repeated_stops_only_track_real_worker_generations(replace_after_stops: bool) -> None:
    engine = FakeEngine(gate_on=1)
    owner = _make_owner(engine, Recorder())
    reasons, called, callback = _terminal_recorder()
    owner.speak("Synthetic.", on_terminal=callback)
    assert engine.entered.wait(2.0)
    try:
        for _ in range(100):
            owner.stop()
        if replace_after_stops:
            _, replacement_called, replacement_callback = _terminal_recorder()
            owner.speak("Replacement.", on_terminal=replacement_callback)
            assert replacement_called.wait(3.0)
        assert owner._cancel_reasons == {1: "stopped"}
        assert owner._worker_generations == {1}
    finally:
        engine.release.set()
    assert called.wait(3.0)
    assert reasons == ["stopped"]
    assert owner._busy_workers == 0
    assert owner._cancel_reasons == {}
    assert owner._worker_generations == set()
