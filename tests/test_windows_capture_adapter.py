"""Windows device/capture conformance against capture contract schema v1."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import whisperstream.audio_capture as capture_module
from whisperstream.audio_capture import AudioCapture, WindowsCaptureAdapter
from whisperstream.capture_contract import (
    CAPTURE_CHANNELS,
    CAPTURE_STOP_TIMEOUT_SECONDS,
    CAPTURE_DTYPE,
    CAPTURE_SAMPLE_RATE,
    CaptureAdapter,
    CaptureCompletionStatus,
    CaptureFailure,
    CaptureRequest,
    CaptureSelection,
    CaptureSelectionResolution,
    CaptureSession,
    CaptureTerminalEvent,
)
from whisperstream.main import WhisperstreamApp


HOST_APIS = (
    {"name": "MME", "default_input_device": 0},
    {"name": "Windows WASAPI", "default_input_device": 2},
    {"name": "Windows DirectSound", "default_input_device": 4},
)
DEVICES = (
    {
        "name": "Built-in microphone",
        "index": 0,
        "hostapi": 0,
        "max_input_channels": 2,
        "max_output_channels": 0,
        "default_samplerate": 44_100.0,
    },
    {
        "name": "Bluetooth headset microphone",
        "index": 1,
        "hostapi": 0,
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 48_000.0,
    },
    {
        "name": "Built-in microphone",
        "index": 2,
        "hostapi": 1,
        "max_input_channels": 2,
        "max_output_channels": 0,
        "default_samplerate": 48_000.0,
    },
    {
        "name": "Bluetooth headset microphone",
        "index": 3,
        "hostapi": 1,
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 48_000.0,
    },
    {
        "name": "Built-in microphone",
        "index": 4,
        "hostapi": 2,
        "max_input_channels": 2,
        "max_output_channels": 0,
        "default_samplerate": 48_000.0,
    },
    {
        "name": "Speakers",
        "index": 5,
        "hostapi": 1,
        "max_input_channels": 0,
        "max_output_channels": 2,
        "default_samplerate": 48_000.0,
    },
)


class FakeStream:
    def __init__(self, kwargs: dict, *, fail_start: bool = False) -> None:
        self.kwargs = kwargs
        self.device = kwargs["device"] if kwargs["device"] is not None else 2
        self.samplerate = float(kwargs["samplerate"])
        self.fail_start = fail_start
        self.started = False
        self.stopped = False
        self.closed = False

    @property
    def callback(self):
        return self.kwargs["callback"]

    @property
    def finished_callback(self):
        return self.kwargs["finished_callback"]

    def start(self) -> None:
        if self.fail_start:
            raise OSError("synthetic open failure")
        self.started = True

    def stop(self, *, ignore_errors=True) -> None:
        self.stopped = True
        self.finished_callback()

    def close(self, *, ignore_errors=True) -> None:
        self.closed = True


@pytest.fixture
def native_devices(monkeypatch):
    monkeypatch.setattr(
        capture_module.sd,
        "query_hostapis",
        lambda: HOST_APIS,
    )
    monkeypatch.setattr(
        capture_module.sd,
        "query_devices",
        lambda: DEVICES,
    )
    monkeypatch.setattr(
        capture_module.sd,
        "default",
        SimpleNamespace(device=(0, 5)),
    )
    return DEVICES


@pytest.fixture
def streams(monkeypatch, native_devices):
    created: list[FakeStream] = []

    def _input_stream(*, owner, **kwargs):
        stream = FakeStream(kwargs)
        created.append(stream)
        return stream

    monkeypatch.setattr(capture_module, "_OwnedInputStream", _input_stream)
    return created


def _start(adapter: WindowsCaptureAdapter, request: CaptureRequest | None = None):
    outcome = adapter.start_capture(request or CaptureRequest("capture-request", 1))
    assert outcome.failure is None
    assert outcome.session is not None
    return outcome.session


@pytest.mark.parametrize("notification", ["status", "level", "preview"])
def test_optional_notification_failure_preserves_audio(streams, notification):
    def fail(*args):
        raise RuntimeError("synthetic notification failure")
    adapter = AudioCapture(status_callback=fail if notification == "status" else None)
    if notification == "preview":
        adapter.set_preview_callback(fail, max_duration=1)
    outcome = adapter.start_capture(CaptureRequest("notification", 1),
                                    on_level=fail if notification == "level" else None)
    streams[0].callback(np.ones((4, 1), dtype=np.float32), 4, None, "overflow")
    result = adapter.stop_capture(outcome.session)
    assert result.status is CaptureCompletionStatus.STOPPED
    np.testing.assert_array_equal(result.audio, np.ones(4))


@pytest.mark.parametrize("operation", ["stop_capture", "cancel_capture"])
def test_failure_during_native_stop_is_not_lost(streams, operation):
    adapter = AudioCapture()
    session = _start(adapter)
    def stop(**kwargs):
        adapter._record_failure(CaptureFailure.STREAM_FAILED)
    streams[0].stop = stop
    result = getattr(adapter, operation)(session)
    assert result.status is CaptureCompletionStatus.FAILED
    assert result.failure is CaptureFailure.STREAM_FAILED
    assert result.audio is None


def test_failed_worker_drain_cannot_become_success_on_retry(streams, monkeypatch):
    adapter = AudioCapture()
    session = _start(adapter)
    monkeypatch.setattr(adapter, "_stop_chunk_worker", lambda **kwargs: False)
    assert adapter.stop_capture(session).failure is CaptureFailure.TIMEOUT
    monkeypatch.setattr(adapter, "_stop_chunk_worker", lambda **kwargs: True)
    result = adapter.stop_capture(session)
    assert result.status is CaptureCompletionStatus.FAILED
    assert result.failure is CaptureFailure.TIMEOUT
    assert adapter.close() is True


@pytest.mark.parametrize("duration", [1.0, 4 / CAPTURE_SAMPLE_RATE])
def test_authoritative_chunk_consumer_failure_is_not_success(streams, duration):
    def fail(_chunk):
        raise RuntimeError("synthetic chunk admission failure")
    adapter = AudioCapture(chunk_duration=duration, chunk_overlap=0)
    session = adapter.start_capture(CaptureRequest("chunks", 1), on_chunk=fail).session
    streams[0].callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
    result = adapter.stop_capture(session)
    assert result.status is CaptureCompletionStatus.FAILED
    assert result.failure is CaptureFailure.STREAM_FAILED and result.audio is None
    assert adapter.close() is True


def test_device_contract_adapter_runtime_and_parent_import_isolation(streams) -> None:
    adapter = AudioCapture()
    assert isinstance(adapter, CaptureAdapter)
    assert isinstance(adapter, WindowsCaptureAdapter)
    assert (adapter.sample_rate, adapter.channels, adapter.dtype) == (
        CAPTURE_SAMPLE_RATE,
        CAPTURE_CHANNELS,
        CAPTURE_DTYPE,
    )
    main_source = Path("src/whisperstream/main.py").read_text(encoding="utf-8")
    assert "import sounddevice" not in main_source
    assert "sd.InputStream" not in main_source


def test_wasapi_inventory_has_stable_opaque_ids_and_no_native_indices(
    native_devices,
) -> None:
    first = capture_module.enumerate_input_devices()
    second = capture_module.enumerate_input_devices()
    assert len(first.devices) == 2
    assert [device.logical_id for device in first.devices] == [
        device.logical_id for device in second.devices
    ]
    assert sum(device.is_default for device in first.devices) == 1
    api_devices = capture_module.list_input_devices()
    assert all("id" in device and "index" not in device for device in api_devices)
    assert all(device["channels"] > 0 for device in api_devices)


def test_default_selection_opens_system_default_at_canonical_format(streams) -> None:
    adapter = AudioCapture()
    session = _start(adapter)
    assert session.selection_resolution is CaptureSelectionResolution.SYSTEM_DEFAULT
    assert streams[0].kwargs["device"] is None
    assert streams[0].kwargs["samplerate"] == CAPTURE_SAMPLE_RATE
    assert streams[0].kwargs["channels"] == CAPTURE_CHANNELS
    assert streams[0].kwargs["dtype"] == CAPTURE_DTYPE
    completion = adapter.stop_capture(session)
    assert completion.status is CaptureCompletionStatus.STOPPED
    assert completion.audio is not None and completion.audio.size == 0
    assert streams[0].stopped and streams[0].closed
    assert not adapter.stream_open


def test_remembered_bluetooth_name_prefers_mme_and_keeps_16khz(streams) -> None:
    adapter = AudioCapture(device="Bluetooth headset microphone")
    session = _start(
        adapter,
        CaptureRequest(
            "remembered-device",
            2,
            selection=CaptureSelection(
                legacy_name="Bluetooth headset microphone"
            ),
        ),
    )
    assert session.selection_resolution is CaptureSelectionResolution.SELECTED
    assert streams[0].kwargs["device"] == 1
    assert streams[0].kwargs["samplerate"] == CAPTURE_SAMPLE_RATE
    adapter.cancel_capture(session)
    assert streams[0].closed


def test_preferred_logical_id_resolves_privately_to_mme(streams) -> None:
    adapter = AudioCapture()
    selected_id = adapter.enumerate_input_devices().devices[0].logical_id
    session = _start(
        adapter,
        CaptureRequest(
            "logical-selection",
            3,
            selection=CaptureSelection(preferred_id=selected_id),
        ),
    )
    assert session.selection_resolution is CaptureSelectionResolution.SELECTED
    assert streams[0].kwargs["device"] == 0
    adapter.cancel_capture(session)


def test_missing_remembered_device_falls_back_to_default(streams) -> None:
    adapter = AudioCapture(device="Disconnected microphone")
    session = _start(
        adapter,
        CaptureRequest(
            "missing-selection",
            4,
            selection=CaptureSelection(legacy_name="Disconnected microphone"),
        ),
    )
    assert session.selection_resolution is CaptureSelectionResolution.FALLBACK_MISSING
    assert streams[0].kwargs["device"] is None
    adapter.cancel_capture(session)
    assert streams[0].closed


def test_selected_open_failure_closes_first_stream_then_falls_back(
    monkeypatch,
    native_devices,
) -> None:
    created: list[FakeStream] = []

    def _input_stream(*, owner, **kwargs):
        stream = FakeStream(kwargs, fail_start=kwargs["device"] == 1)
        created.append(stream)
        return stream

    monkeypatch.setattr(capture_module, "_OwnedInputStream", _input_stream)
    adapter = AudioCapture(device="Bluetooth headset microphone")
    session = _start(
        adapter,
        CaptureRequest(
            "open-fallback",
            5,
            selection=CaptureSelection(
                legacy_name="Bluetooth headset microphone"
            ),
        ),
    )
    assert session.selection_resolution is CaptureSelectionResolution.FALLBACK_OPEN_FAILED
    assert len(created) == 2
    assert created[0].closed
    assert created[1].kwargs["device"] is None
    adapter.cancel_capture(session)
    assert created[1].closed


def test_backend_open_failure_is_typed_and_closes_partial_stream(
    monkeypatch,
    native_devices,
) -> None:
    created: list[FakeStream] = []

    def _input_stream(*, owner, **kwargs):
        stream = FakeStream(kwargs, fail_start=True)
        created.append(stream)
        return stream

    monkeypatch.setattr(capture_module, "_OwnedInputStream", _input_stream)
    outcome = AudioCapture().start_capture(CaptureRequest("open-failure", 6))
    assert outcome.failure is CaptureFailure.OPEN_FAILED
    assert outcome.session is None
    assert len(created) == 1 and created[0].closed


def test_open_failure_retains_unclean_stream_and_blocks_next_start(
    monkeypatch,
    native_devices,
) -> None:
    created: list[FakeStream] = []
    close_attempts = 0

    def _input_stream(*, owner, **kwargs):
        stream = FakeStream(kwargs, fail_start=True)
        created.append(stream)
        return stream

    def _persistent_close(stream: FakeStream, **kwargs) -> None:
        nonlocal close_attempts
        close_attempts += 1
        if close_attempts < 3:
            raise OSError("synthetic persistent close failure")
        stream.closed = True

    monkeypatch.setattr(capture_module, "_OwnedInputStream", _input_stream)
    monkeypatch.setattr(FakeStream, "close", _persistent_close)
    adapter = AudioCapture()

    outcome = adapter.start_capture(CaptureRequest("unclean-open", 16))

    assert outcome.failure is CaptureFailure.CLEANUP_FAILED
    retained = adapter.active_session
    assert retained is not None
    assert adapter.stream_open
    blocked = adapter.start_capture(CaptureRequest("blocked-open", 17))
    assert blocked.failure is CaptureFailure.NATIVE_ERROR
    with pytest.raises(capture_module.AudioCaptureError):
        adapter.start()
    assert adapter.active_session is retained
    assert len(created) == 1
    assert adapter.close() is False
    assert close_attempts == 1
    assert adapter.active_session is retained
    assert not adapter.capture_available


def test_no_input_device_is_explicit_without_open_attempt(monkeypatch) -> None:
    monkeypatch.setattr(
        capture_module,
        "_query_native_input_devices",
        lambda *, display_only: [],
    )
    open_stream = MagicMock()
    monkeypatch.setattr(capture_module, "_OwnedInputStream", open_stream)
    outcome = AudioCapture().start_capture(CaptureRequest("no-input", 12))
    assert outcome.failure is CaptureFailure.NO_INPUT_DEVICE
    assert outcome.session is None
    open_stream.assert_not_called()


def test_format_normalization_flattens_mono_float32_and_preserves_callbacks(
    streams,
) -> None:
    levels: list[float] = []
    chunks: list[np.ndarray] = []
    adapter = AudioCapture(
        chunk_duration=4 / CAPTURE_SAMPLE_RATE,
        chunk_overlap=0.0,
    )
    session = adapter.start_capture(
        CaptureRequest("format", 7),
        on_level=levels.append,
        on_chunk=lambda chunk: chunks.append(chunk.copy()),
    ).session
    assert session is not None
    stereo_float64 = np.array(
        [[0.0, 0.2], [0.2, 0.4], [0.4, 0.6], [0.6, 0.8]],
        dtype=np.float64,
    )
    streams[0].callback(stereo_float64, 4, None, None)
    completion = adapter.stop_capture(session)
    assert completion.status is CaptureCompletionStatus.STOPPED
    assert completion.audio is not None
    assert completion.audio.dtype == np.float32
    assert completion.audio.ndim == 1
    np.testing.assert_allclose(completion.audio, [0.1, 0.3, 0.5, 0.7])
    assert levels and 0.0 <= levels[0] <= 1.0
    assert chunks and chunks[0].dtype == np.float32
    assert not adapter.chunk_worker_alive


def test_cancel_discards_audio_and_stale_generation_cannot_touch_stream(streams) -> None:
    adapter = AudioCapture()
    session = _start(adapter, CaptureRequest("cancel", 8))
    streams[0].callback(np.ones((16, 1), dtype=np.float32), 16, None, None)
    stale = CaptureSession(
        request_id="older",
        generation=7,
        device_id="opaque-old",
        selection_resolution=CaptureSelectionResolution.SYSTEM_DEFAULT,
    )
    stale_result = adapter.cancel_capture(stale)
    assert stale_result.failure is CaptureFailure.STALE_GENERATION
    assert adapter.stream_open and not streams[0].closed
    completion = adapter.cancel_capture(session)
    assert completion.status is CaptureCompletionStatus.CANCELLED
    assert completion.audio is None
    assert streams[0].stopped and streams[0].closed
    assert not adapter.stream_open


def test_unexpected_native_finish_reports_device_loss_and_closes(streams) -> None:
    terminal_events: list[CaptureTerminalEvent] = []
    adapter = AudioCapture(terminal_callback=terminal_events.append)
    session = _start(adapter, CaptureRequest("device-loss", 9))
    streams[0].finished_callback()
    streams[0].finished_callback()
    assert terminal_events == [
        CaptureTerminalEvent(
            request_id="device-loss",
            generation=9,
            failure=CaptureFailure.DEVICE_LOST,
            owner_epoch=adapter.owner_epoch,
        )
    ]
    completion = adapter.stop_capture(session)
    assert completion.status is CaptureCompletionStatus.FAILED
    assert completion.failure is CaptureFailure.DEVICE_LOST
    assert streams[0].stopped and streams[0].closed
    assert not adapter.stream_open


def test_retired_stream_callbacks_cannot_affect_next_recording(streams) -> None:
    terminal_events: list[CaptureTerminalEvent] = []
    adapter = AudioCapture(terminal_callback=terminal_events.append)
    first = _start(adapter, CaptureRequest("retired-stream", 20))
    old_stream = streams[0]
    adapter.cancel_capture(first)
    second = _start(adapter, CaptureRequest("new-stream", 21))

    old_stream.callback(np.ones((4, 1), dtype=np.float32), 4, None, None)
    old_stream.finished_callback()
    streams[1].callback(np.full((4, 1), 2.0, dtype=np.float32), 4, None, None)

    assert terminal_events == []
    assert adapter.active_session is second
    completion = adapter.stop_capture(second)
    assert completion.status is CaptureCompletionStatus.STOPPED
    np.testing.assert_array_equal(completion.audio, np.full(4, 2.0, dtype=np.float32))


def test_device_loss_close_failure_is_authoritative_and_not_retryable(streams) -> None:
    adapter = AudioCapture()
    session = _start(adapter, CaptureRequest("loss-close-failure", 14))
    stream = streams[0]
    stream.finished_callback()
    attempts = 0

    def flaky_close(**kwargs) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("synthetic close failure")
        stream.closed = True

    stream.close = flaky_close
    completion = adapter.cancel_capture(session)

    assert completion.failure is CaptureFailure.CLEANUP_FAILED
    assert adapter.active_session is session
    assert adapter.stream_open
    assert adapter.close() is False
    assert adapter.active_session is session
    created_before = len(streams)
    blocked = adapter.start_capture(CaptureRequest("blocked-after-cleanup", 15))
    assert blocked.failure is CaptureFailure.NATIVE_ERROR
    with pytest.raises(capture_module.AudioCaptureError):
        adapter.start()
    assert len(streams) == created_before
    assert adapter.close() is False
    assert attempts == 1
    assert adapter.active_session is session
    assert adapter.stream_open
    assert not adapter.capture_available


def test_device_loss_worker_timeout_is_authoritative_and_retryable(
    monkeypatch,
    streams,
) -> None:
    # The short stop bound is what this test proves while the worker is
    # blocked; harness waits get a generous budget so load cannot unblock it.
    monkeypatch.setattr(capture_module, "CAPTURE_STOP_TIMEOUT_SECONDS", 0.01)
    started = threading.Event()
    release = threading.Event()

    def slow_chunk(_chunk: np.ndarray) -> None:
        started.set()
        assert release.wait(30.0)

    adapter = AudioCapture(
        chunk_duration=4 / CAPTURE_SAMPLE_RATE,
        chunk_overlap=0.0,
    )
    session = adapter.start_capture(
        CaptureRequest("loss-worker-timeout", 15),
        on_chunk=slow_chunk,
    ).session
    assert session is not None
    stream = streams[0]
    stream.callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
    assert started.wait(30.0)
    stream.finished_callback()

    completion = adapter.cancel_capture(session)
    assert completion.failure is CaptureFailure.TIMEOUT
    assert adapter.active_session is session
    assert adapter.chunk_worker_alive
    assert adapter.close() is False
    assert adapter.active_session is session
    blocked = adapter.start_capture(CaptureRequest("blocked-worker", 16))
    assert blocked.failure is CaptureFailure.NATIVE_ERROR
    with pytest.raises(capture_module.AudioCaptureError):
        adapter.start()
    release.set()
    # Once released, retry teardown under the production stop budget: joining
    # a now-unblocked worker within 10 ms is not the bound under test.
    monkeypatch.setattr(
        capture_module,
        "CAPTURE_STOP_TIMEOUT_SECONDS",
        CAPTURE_STOP_TIMEOUT_SECONDS,
    )
    assert adapter.close() is True
    assert adapter.active_session is None
    assert not adapter.chunk_worker_alive


def test_callback_backend_failure_is_explicit_and_stream_closes(streams) -> None:
    def _status_failure(_status: str) -> None:
        raise RuntimeError("synthetic status callback failure")

    terminal_events: list[CaptureTerminalEvent] = []
    adapter = AudioCapture(
        status_callback=_status_failure, terminal_callback=terminal_events.append
    )
    session = _start(adapter, CaptureRequest("callback-failure", 10))
    class BrokenFrames(list):
        def append(self, value):
            raise RuntimeError("synthetic storage failure")
    adapter._frames = BrokenFrames()
    with pytest.raises(capture_module.sd.CallbackAbort):
        streams[0].callback(
            np.zeros((4, 1), dtype=np.float32),
            4,
            None,
            "input overflow",
        )
    streams[0].finished_callback()
    streams[0].finished_callback()
    assert terminal_events == [
        CaptureTerminalEvent(
            request_id=session.request_id,
            generation=session.generation,
            failure=CaptureFailure.STREAM_FAILED,
            owner_epoch=adapter.owner_epoch,
        )
    ]
    completion = adapter.stop_capture(session)
    assert completion.status is CaptureCompletionStatus.FAILED
    assert completion.failure is CaptureFailure.STREAM_FAILED
    assert streams[0].closed


def test_stop_failure_still_closes_stream_and_reports_cleanup(streams) -> None:
    adapter = AudioCapture()
    session = _start(adapter, CaptureRequest("stop-failure", 13))

    def _stop_failure(**kwargs) -> None:
        raise OSError("synthetic stop failure")

    streams[0].stop = _stop_failure
    completion = adapter.stop_capture(session)
    assert completion.status is CaptureCompletionStatus.FAILED
    assert completion.failure is CaptureFailure.CLEANUP_FAILED
    assert streams[0].closed
    assert not adapter.stream_open


def test_close_cancels_active_stream_and_is_idempotent(streams) -> None:
    adapter = AudioCapture()
    _start(adapter, CaptureRequest("close", 11))
    adapter.close()
    adapter.close()
    assert streams[0].stopped and streams[0].closed
    assert not adapter.stream_open
    assert not adapter.chunk_worker_alive


def test_app_shutdown_closes_active_capture_once_and_restores_volume() -> None:
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._file_only_stop = None
    app.stt_engine = None
    app.audio_capture = MagicMock()
    app._recording = True
    app._restore_system_volume = MagicMock()

    app.cleanup()
    app.cleanup()

    app._restore_system_volume.assert_called_once_with()
    app.audio_capture.close.assert_called_once_with()
    assert app._recording is False
