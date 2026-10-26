"""Deterministic D-055 conformance for generation-owned Darwin speech output."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
import threading
import time
from collections.abc import Callable

import numpy as np

from whisperstream.macos_tts_playback import (
    MacOSSpeechPlaybackAdapter,
    MacOSSpeechSynthesisAdapter,
)
from whisperstream.model_downloader import DOWNLOAD_REGISTRY, ModelId
from whisperstream.model_sources import MODEL_SOURCES
from whisperstream.speech.engine_specs import KOKORO_SPEC, SUPERTONIC_SPEC
from whisperstream.speech.selection import EngineManager, MemorySnapshot, SelectionPolicy
from whisperstream.tts_playback import (
    UnavailableSpeechPlaybackAdapter,
    create_speech_playback_adapter,
    create_speech_synthesis_adapter,
)
from whisperstream.tts_playback_contract import (
    SpeechBuffer,
    SpeechEngineChoice,
    SpeechFailure,
    SpeechReceipt,
    SpeechRequest,
    SpeechStage,
    SpeechTerminalReason,
)


KOKORO_ARCHIVE_SHA256 = (
    "a1a38e087ea56ba9cae78ca36c93f1faed84bc028360a50e9e84a4f74a2b528f"
)
REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeNativeStream:
    def __init__(self, *, fail_write: int | None = None) -> None:
        self.started = 0
        self.writes: list[np.ndarray] = []
        self.write_calls = 0
        self.fail_write = fail_write
        self.aborted = 0
        self.stopped = 0
        self.closed = 0
        self.abort_entered = threading.Event()
        self.abort_release = threading.Event()
        self.close_completed = threading.Event()
        self.block_abort = False
        self.on_write: Callable[[int], None] | None = None

    def start(self) -> None:
        self.started += 1

    def write(self, frames: np.ndarray) -> None:
        self.write_calls += 1
        if self.on_write is not None:
            self.on_write(self.write_calls)
        if self.write_calls == self.fail_write:
            raise RuntimeError("uncertain Core Audio write")
        self.writes.append(np.asarray(frames).copy())

    def abort(self) -> None:
        self.abort_entered.set()
        if self.block_abort:
            self.abort_release.wait(timeout=2.0)
        self.aborted += 1

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.closed += 1
        self.close_completed.set()


class FakeSoundDevice:
    def __init__(self, streams: list[FakeNativeStream]) -> None:
        self.streams = streams
        self.opens: list[dict[str, object]] = []

    def OutputStream(self, **kwargs: object) -> FakeNativeStream:  # noqa: N802
        self.opens.append(dict(kwargs))
        return self.streams[len(self.opens) - 1]


class FakeEngine:
    def __init__(self) -> None:
        self.loaded = 0
        self.unloaded = 0
        self.generated: list[tuple[object, str, float]] = []

    @staticmethod
    def is_installed() -> bool:
        return True

    def load(self) -> None:
        self.loaded += 1

    @property
    def sample_rate(self) -> int:
        return 44_100

    def generate(
        self,
        text: str,
        *,
        sid: object,
        speed: float,
        callback: Callable[..., int] | None,
    ) -> np.ndarray:
        del callback
        self.generated.append((sid, hashlib.sha256(text.encode()).hexdigest(), speed))
        return np.asarray([0.25, -0.5], dtype=np.float32)

    def unload(self) -> None:
        self.unloaded += 1


def _request(generation: int = 1) -> SpeechRequest:
    return SpeechRequest(
        "fixed-public-completion",
        generation,
        SpeechEngineChoice.SUPERTONIC,
        "1",
        1.0,
        1.0,
        "Whisperstream playback check.",
    )


def _buffer(generation: int, ordinal: int, *, final: bool = False) -> SpeechBuffer:
    frames = np.asarray([0.1, -0.1], dtype="<f4")
    return SpeechBuffer(generation, ordinal, 24_000, 1, frames.tobytes(), final)


def test_darwin_factories_are_lazy_and_platform_scoped() -> None:
    constructed: list[str] = []
    manager = EngineManager(
        engine_factory=lambda key: constructed.append(key) or FakeEngine()
    )

    synthesis = create_speech_synthesis_adapter(
        engine_manager=manager,
        system="Darwin",
        machine="arm64",
    )
    playback = create_speech_playback_adapter(
        system="Darwin",
        machine="arm64",
        sounddevice_module=FakeSoundDevice([FakeNativeStream()]),
        current_output_resolver=lambda: 7,
        # PlaybackOwner passes these Windows-only seams on every host.
        default_output_name=lambda: "private identity",
        wasapi_device_resolver=lambda _name: 9,
    )

    assert isinstance(synthesis, MacOSSpeechSynthesisAdapter)
    assert isinstance(playback, MacOSSpeechPlaybackAdapter)
    assert constructed == []
    assert manager.health_reason("supertonic") is None
    assert isinstance(
        create_speech_playback_adapter(system="Linux", machine="arm64"),
        UnavailableSpeechPlaybackAdapter,
    )
    assert manager.shutdown(timeout=0.0)


def test_frozen_models_hashes_and_exact_voice_scope() -> None:
    supertonic = MODEL_SOURCES[ModelId.SUPERTONIC_3_INT8.value]
    assert supertonic["revision"] == "cca5a0e6c96e1d2c720986bf7e75fcc81dee3ae4"
    assert "r2" not in supertonic
    assert MODEL_SOURCES[ModelId.KOKORO_82M_FP32.value]["revision"] == "f7b96bb6bef5c5da4d3aa4f4e0498fbbf62dc78b"
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


def test_supertonic_and_kokoro_use_reviewed_official_runtime() -> None:
    from importlib.metadata import version
    import tomllib
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "sherpa-onnx==1.13.8" in project["project"]["dependencies"]
    assert "sherpa-onnx-core==1.13.8" in project["project"]["dependencies"]
    assert version("sherpa-onnx") == "1.13.8"


def test_automatic_selection_needs_only_free_memory() -> None:
    manager = EngineManager(engine_factory=lambda _key: FakeEngine())
    manager.mark_unhealthy("supertonic", "runtime_unavailable")
    high = SelectionPolicy(
        manager,
        memory_probe=lambda: MemorySnapshot(8 * 1024, 2 * 1024),
    ).resolve("auto", supertonic_voice=1, kokoro_voice="af_heart")
    low = SelectionPolicy(
        manager,
        memory_probe=lambda: MemorySnapshot(16 * 1024, 2 * 1024 - 1),
    ).resolve("auto", supertonic_voice=1, kokoro_voice="af_heart")

    assert [candidate.engine_key for candidate in high.candidates] == ["kokoro"]
    assert low.candidates == ()
    assert manager.shutdown(timeout=0.0)


def test_synthesis_yields_ordered_hidden_float32_buffers_and_releases_model() -> None:
    engine = FakeEngine()
    manager = EngineManager(engine_factory=lambda _key: engine)
    adapter = MacOSSpeechSynthesisAdapter(engine_manager=manager)
    request = SpeechRequest(
        "fixed-public-completion",
        4,
        SpeechEngineChoice.KOKORO,
        "af_heart",
        1.25,
        0.5,
        "First sentence. Second sentence.",
    )

    buffers = list(adapter.synthesize(request))

    assert [buffer.ordinal for buffer in buffers] == [0, 1]
    assert [buffer.final for buffer in buffers] == [False, True]
    assert all(buffer.generation == 4 and buffer.channels == 1 for buffer in buffers)
    assert np.frombuffer(buffers[0].pcm_f32le, dtype="<f4").tolist() == [0.125, -0.25]
    assert "First sentence" not in repr(request)
    assert "pcm_f32le" not in repr(buffers[0])
    assert adapter.shutdown(timeout=0.0)
    assert engine.unloaded == 1


def test_current_output_is_resolved_once_per_utterance_and_never_persisted() -> None:
    devices = iter((4, 9))
    resolutions = 0

    def resolve() -> int:
        nonlocal resolutions
        resolutions += 1
        return next(devices)

    native = [FakeNativeStream(), FakeNativeStream()]
    sounddevice = FakeSoundDevice(native)
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=sounddevice,
        current_output_resolver=resolve,
    )

    first = adapter.play(_request(1), [_buffer(1, 0), _buffer(1, 1, final=True)])
    second = adapter.play(_request(2), [_buffer(2, 0, final=True)])

    assert first.terminal_reason is SpeechTerminalReason.COMPLETED
    assert second.terminal_reason is SpeechTerminalReason.COMPLETED
    assert resolutions == 2
    assert [opened["device"] for opened in sounddevice.opens] == [4, 9]
    assert all(stream.started == stream.stopped == stream.closed == 1 for stream in native)
    assert not adapter._active  # noqa: SLF001 - cleanup conformance
    assert not adapter._interruptions  # noqa: SLF001 - cleanup conformance


def test_invalid_buffer_fails_before_output_and_has_one_terminal_reason() -> None:
    sounddevice = FakeSoundDevice([FakeNativeStream()])
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=sounddevice,
        current_output_resolver=lambda: 3,
    )

    receipt = adapter.play(_request(7), [_buffer(7, 1, final=True)])

    assert receipt == SpeechReceipt(
        "fixed-public-completion",
        7,
        SpeechStage.FAILED,
        SpeechTerminalReason.PREPLAY_FAILURE,
        SpeechFailure.INVALID_REQUEST,
        0,
    )
    assert sounddevice.opens == []


def test_output_unavailable_and_uncertain_write_are_terminal_without_retry() -> None:
    unavailable_sd = FakeSoundDevice([FakeNativeStream()])
    unavailable = MacOSSpeechPlaybackAdapter(
        sounddevice_module=unavailable_sd,
        current_output_resolver=lambda: None,
    )
    missing = unavailable.play(_request(1), [_buffer(1, 0, final=True)])
    assert (missing.terminal_reason, missing.failure) == (
        SpeechTerminalReason.UNAVAILABLE_OUTPUT,
        SpeechFailure.OUTPUT_UNAVAILABLE,
    )
    assert unavailable_sd.opens == []

    failed_native = FakeNativeStream(fail_write=2)
    failed_sd = FakeSoundDevice([failed_native])
    failed = MacOSSpeechPlaybackAdapter(
        sounddevice_module=failed_sd,
        current_output_resolver=lambda: 2,
    )
    receipt = failed.play(_request(2), [_buffer(2, 0), _buffer(2, 1, final=True)])
    assert (receipt.terminal_reason, receipt.failure, receipt.buffers_completed) == (
        SpeechTerminalReason.PLAYBACK_FAILURE,
        SpeechFailure.PLAYBACK_FAILED,
        1,
    )
    assert len(failed_sd.opens) == 1
    assert failed_native.write_calls == 2
    assert failed_native.closed == 1


def test_interrupt_is_nonblocking_first_reason_wins_and_cleanup_is_exact() -> None:
    native = FakeNativeStream()
    native.block_abort = True
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=FakeSoundDevice([native]),
        current_output_resolver=lambda: 5,
    )
    stream = adapter.open_output_stream(24_000)
    assert adapter.claim_stream(11, stream)

    started = time.perf_counter()
    adapter.interrupt(11, SpeechTerminalReason.EXPLICIT_STOP)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05
    assert native.abort_entered.wait(timeout=1.0)
    adapter.output_device_changed(11)
    assert adapter._interruption_for(11) == (  # noqa: SLF001
        SpeechTerminalReason.EXPLICIT_STOP,
        None,
    )

    native.abort_release.set()
    adapter.release_stream(11, stream)
    adapter.finish_generation(11)
    assert (native.aborted, native.stopped, native.closed) == (1, 1, 1)
    assert not adapter._active  # noqa: SLF001
    assert not adapter._interruptions  # noqa: SLF001
    assert not adapter._abort_workers  # noqa: SLF001


def test_interrupt_after_first_buffer_prevents_a_second_uncertain_write() -> None:
    native = FakeNativeStream()
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=FakeSoundDevice([native]),
        current_output_resolver=lambda: 5,
    )
    native.on_write = lambda count: (
        adapter.interrupt(12, SpeechTerminalReason.SUPERSEDED)
        if count == 1
        else None
    )

    receipt = adapter.play(
        _request(12),
        [_buffer(12, 0), _buffer(12, 1, final=True)],
    )

    assert (receipt.stage, receipt.terminal_reason, receipt.buffers_completed) == (
        SpeechStage.INTERRUPTED,
        SpeechTerminalReason.SUPERSEDED,
        1,
    )
    assert native.write_calls == 1
    # A user stop the writer honors ends with a draining stop, not an abort.
    assert (native.aborted, native.stopped, native.closed) == (0, 1, 1)
    assert not adapter._active  # noqa: SLF001
    assert not adapter._interruptions  # noqa: SLF001
    assert not adapter._abort_workers  # noqa: SLF001


def test_close_leaves_claimed_stream_teardown_to_generation_worker() -> None:
    native = FakeNativeStream()
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=FakeSoundDevice([native]),
        current_output_resolver=lambda: 5,
    )
    stream = adapter.open_output_stream(24_000)
    assert adapter.claim_stream(19, stream)
    adapter.close()
    adapter.close()
    assert (native.aborted, native.stopped, native.closed) == (1, 0, 0)
    adapter.release_stream(19, stream)
    adapter.finish_generation(19)
    assert (native.aborted, native.stopped, native.closed) == (1, 1, 1)
    assert not adapter._active
    assert not adapter._abort_workers


def test_close_timeout_does_not_close_while_abort_still_owns_native_handle() -> None:
    native = FakeNativeStream()
    native.block_abort = True
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=FakeSoundDevice([native]),
        current_output_resolver=lambda: 5,
        abort_join_timeout=0.001,
    )
    stream = adapter.open_output_stream(24_000)
    assert adapter.claim_stream(23, stream)
    adapter.close()
    assert native.abort_entered.wait(timeout=1.0)
    assert native.closed == 0
    finalizer = threading.Thread(target=lambda: adapter.release_stream(23, stream))
    finalizer.start()
    finalizer.join(timeout=0.03)
    assert finalizer.is_alive()
    assert native.closed == 0
    native.abort_release.set()
    finalizer.join(timeout=1.0)
    assert not finalizer.is_alive()
    adapter.finish_generation(23)
    adapter.close()
    assert (native.aborted, native.stopped, native.closed) == (1, 1, 1)
    assert not adapter._interruptions
    assert not adapter._abort_workers


def test_late_interrupt_cannot_abort_a_stream_already_in_native_stop() -> None:
    native = FakeNativeStream()
    stop_entered, stop_release = threading.Event(), threading.Event()
    def stop() -> None:
        native.stopped += 1
        stop_entered.set()
        assert stop_release.wait(timeout=1.0)
    native.stop = stop
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=FakeSoundDevice([native]),
        current_output_resolver=lambda: 5,
    )
    stream = adapter.open_output_stream(24_000)
    assert adapter.claim_stream(24, stream)
    finalizer = threading.Thread(target=lambda: adapter.release_stream(24, stream))
    finalizer.start()
    try:
        assert stop_entered.wait(timeout=1.0)
        adapter.interrupt(24, SpeechTerminalReason.EXPLICIT_STOP)
        adapter.close()
        assert not native.abort_entered.is_set()
        assert native.closed == 0
    finally:
        stop_release.set()
        finalizer.join(timeout=1.0)
    adapter.finish_generation(24)
    assert (native.aborted, native.stopped, native.closed) == (0, 1, 1)


def test_interrupt_does_not_close_until_playback_worker_leaves_write() -> None:
    native = FakeNativeStream()
    write_entered, write_release = threading.Event(), threading.Event()
    close_threads: list[int] = []
    def write(_count: int) -> None:
        write_entered.set()
        assert write_release.wait(timeout=1.0)
    native.on_write = write
    original_close = native.close
    def close() -> None:
        close_threads.append(threading.get_ident())
        original_close()
    native.close = close
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=FakeSoundDevice([native]),
        current_output_resolver=lambda: 5,
    )
    worker = threading.Thread(target=lambda: adapter.play(_request(25), [_buffer(25, 0, final=True)]))
    worker.start()
    try:
        assert write_entered.wait(timeout=1.0)
        adapter.interrupt(25, SpeechTerminalReason.EXPLICIT_STOP)
        assert native.abort_entered.wait(timeout=1.0)
        adapter.close()
        assert native.closed == 0
    finally:
        write_release.set()
        worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert close_threads == [worker.ident]
    assert (native.aborted, native.stopped, native.closed) == (1, 1, 1)


def test_sleep_wake_device_change_and_close_release_only_owned_generations() -> None:
    native = [FakeNativeStream(), FakeNativeStream()]
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=FakeSoundDevice(native),
        current_output_resolver=lambda: 5,
    )
    first = adapter.open_output_stream(24_000)
    second = adapter.open_output_stream(24_000)
    assert adapter.claim_stream(1, first)
    assert adapter.claim_stream(2, second)

    adapter.output_device_changed(1)
    adapter.suspend()
    adapter.resume()
    adapter.release_stream(1, first)
    adapter.release_stream(2, second)
    assert adapter._interruption_for(1) == (  # noqa: SLF001
        SpeechTerminalReason.PLAYBACK_FAILURE,
        SpeechFailure.OUTPUT_CHANGED,
    )
    assert adapter._interruption_for(2) == (  # noqa: SLF001
        SpeechTerminalReason.SHUTDOWN,
        None,
    )
    adapter.finish_generation(1)
    adapter.finish_generation(2)
    adapter.close()
    adapter.close()
    assert all((stream.aborted, stream.stopped, stream.closed) == (1, 1, 1) for stream in native)
    assert not adapter._active  # noqa: SLF001
    assert not adapter._interruptions  # noqa: SLF001
    assert not adapter._abort_workers  # noqa: SLF001


def test_phase4_audio_policy_coordination_stops_speech_before_policy_or_license() -> None:
    from unittest.mock import MagicMock

    import pytest

    from tests._platform_test_support import StopAfterBargeIn, _bare_app

    order: list[str] = []
    controller = MagicMock()
    controller.stop.side_effect = lambda: order.append("speech-stop")
    app = _bare_app(controller=controller, order=order)
    app.audio_policy = MagicMock()
    app._require_acceptance = lambda *_args, **_kwargs: True

    with pytest.raises(StopAfterBargeIn):
        app._start_recording()
    assert order == ["speech-stop", "capture"]
    app.audio_policy.begin.assert_not_called()


def _speech_buffer(generation: int, frames: np.ndarray) -> SpeechBuffer:
    return SpeechBuffer(generation, 0, 24_000, 1, frames.astype("<f4").tobytes(), True)


def test_completed_speech_drains_silence_before_stop() -> None:
    from whisperstream import macos_tts_playback as module

    native = FakeNativeStream()
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=FakeSoundDevice([native]),
        current_output_resolver=lambda: 5,
    )
    loud = np.full(2_400, 0.5, dtype="<f4")

    receipt = adapter.play(_request(30), [_speech_buffer(30, loud)])

    assert receipt.stage is SpeechStage.COMPLETED
    np.testing.assert_array_equal(np.concatenate(native.writes[:-1]).reshape(-1), loud)
    pad = native.writes[-1].reshape(-1)
    assert pad.size == int(24_000 * module.END_SILENCE_SECONDS)
    assert not pad.any()
    assert (native.aborted, native.stopped, native.closed) == (0, 1, 1)


def test_user_stop_fades_the_next_chunk_then_drains_silence() -> None:
    from whisperstream import macos_tts_playback as module

    native = FakeNativeStream()
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=FakeSoundDevice([native]),
        current_output_resolver=lambda: 5,
    )
    native.on_write = lambda count: (
        adapter.interrupt(31, SpeechTerminalReason.EXPLICIT_STOP) if count == 2 else None
    )
    loud = np.full(24_000, 0.5, dtype="<f4")

    receipt = adapter.play(_request(31), [_speech_buffer(31, loud)])

    assert receipt.stage is SpeechStage.INTERRUPTED
    chunk = int(24_000 * module.WRITE_CHUNK_SECONDS)
    fade = int(24_000 * module.STOP_FADE_SECONDS)
    assert [w.shape[0] for w in native.writes] == [
        chunk,
        chunk,
        fade,
        int(24_000 * module.END_SILENCE_SECONDS),
    ]
    ramp = native.writes[2].reshape(-1)
    assert ramp[0] == np.float32(0.5) and ramp[-1] == 0.0
    assert np.all(np.diff(ramp) <= 0)
    assert not native.writes[3].any()
    assert (native.aborted, native.stopped, native.closed) == (0, 1, 1)
    assert not adapter._abort_workers  # noqa: SLF001


def test_device_change_still_aborts_without_fading() -> None:
    native = FakeNativeStream()
    adapter = MacOSSpeechPlaybackAdapter(
        sounddevice_module=FakeSoundDevice([native]),
        current_output_resolver=lambda: 5,
    )
    native.on_write = lambda count: (
        adapter.output_device_changed(32) if count == 1 else None
    )
    loud = np.full(24_000, 0.5, dtype="<f4")

    receipt = adapter.play(_request(32), [_speech_buffer(32, loud)])

    assert receipt.failure is SpeechFailure.OUTPUT_CHANGED
    assert native.aborted == 1
    assert all(np.all(w == np.float32(0.5)) for w in native.writes)
