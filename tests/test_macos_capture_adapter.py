"""macOS device/capture conformance without unattended microphone access."""

from __future__ import annotations

import platform
import subprocess
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import whisperstream.capture as capture_selector
import whisperstream.macos_capture as capture_module
from whisperstream.capture_contract import (
    CAPTURE_CHANNELS,
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
from whisperstream.macos_capture import MacosCaptureAdapter
from whisperstream.permission_contract import PermissionState




class FakeBridge:
    def __init__(self, devices) -> None:
        self.devices = tuple(devices)

    def enumerate_devices(self):
        return self.devices

    def stream_input_uid(self, stream) -> str:
        return stream.uid


class FakeStream:
    def __init__(
        self,
        kwargs: dict,
        *,
        uid: str,
        fail_start: bool = False,
        fail_stop: bool = False,
    ) -> None:
        self.kwargs = kwargs
        self.uid = uid
        self.samplerate = float(kwargs["samplerate"])
        self.fail_start = fail_start
        self.fail_stop = fail_stop
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

    def stop(self) -> None:
        self.stopped = True
        if self.fail_stop:
            raise OSError("synthetic stop failure")
        self.finished_callback()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def capture_stack(monkeypatch):
    built_in = capture_module._CoreAudioDevice(
        object_id=101,
        uid="reviewed-coreaudio-uid-built-in",
        name="Built-in Test Input",
        nominal_sample_rate=48_000.0,
        available_sample_rate_ranges=((16_000.0, 16_000.0), (48_000.0, 48_000.0)),
        is_default=True,
    )
    virtual = capture_module._CoreAudioDevice(
        object_id=202,
        uid="reviewed-coreaudio-uid-virtual",
        name="Reviewed Virtual Input",
        nominal_sample_rate=48_000.0,
        available_sample_rate_ranges=((44_100.0, 96_000.0),),
        is_default=False,
    )
    bridge = FakeBridge((built_in, virtual))
    state = SimpleNamespace(
        portaudio=(
            {
                "index": 7,
                "name": built_in.name,
                "max_input_channels": 1,
                "default_sample_rate": 48_000.0,
                "is_portaudio_default": True,
            },
            {
                "index": 9,
                "name": virtual.name,
                "max_input_channels": 2,
                "default_sample_rate": 48_000.0,
                "is_portaudio_default": False,
            },
        ),
        default_index=7,
        uid_by_index={
            7: built_in.uid,
            9: virtual.uid,
        },
        fail_start=set(),
        fail_stop=set(),
        unsupported=set(),
    )
    created: list[FakeStream] = []
    checked: list[dict] = []

    monkeypatch.setattr(capture_module, "_portaudio_inputs", lambda: state.portaudio)
    monkeypatch.setattr(
        capture_module.sd,
        "CoreAudioSettings",
        lambda **kwargs: ("coreaudio-settings", kwargs),
    )

    def check_input_settings(**kwargs):
        checked.append(kwargs)
        device = kwargs["device"]
        effective = state.default_index if device is None else device
        if effective in state.unsupported:
            raise ValueError("synthetic unsupported format")

    def input_stream(**kwargs):
        device = kwargs["device"]
        effective = state.default_index if device is None else device
        stream = FakeStream(
            kwargs,
            uid=state.uid_by_index[effective],
            fail_start=effective in state.fail_start,
            fail_stop=effective in state.fail_stop,
        )
        created.append(stream)
        return stream

    monkeypatch.setattr(capture_module.sd, "check_input_settings", check_input_settings)
    monkeypatch.setattr(capture_module.sd, "InputStream", input_stream)

    def adapter(**kwargs):
        return MacosCaptureAdapter(
            _bridge=bridge,
            _permission_state=lambda: PermissionState.GRANTED,
            **kwargs,
        )

    return SimpleNamespace(
        adapter=adapter,
        bridge=bridge,
        state=state,
        created=created,
        checked=checked,
        built_in=built_in,
        virtual=virtual,
    )


def _start(adapter: MacosCaptureAdapter, request: CaptureRequest | None = None):
    outcome = adapter.start_capture(request or CaptureRequest("capture", 1))
    assert outcome.failure is None
    assert outcome.session is not None
    return outcome.session


def _fixture_audio() -> np.ndarray:
    """Generate deterministic samples without a recording or private corpus."""
    return np.tile(np.array([0.0, 0.125, -0.125, 0.0], dtype=np.float32), 16_000)


@pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="native CoreAudio enumeration requires Apple Silicon",
)
def test_native_coreaudio_enumeration_is_prompt_free_and_stable(monkeypatch) -> None:
    opened = MagicMock(side_effect=AssertionError("enumeration opened an input stream"))
    monkeypatch.setattr(capture_module.sd, "InputStream", opened)

    bridge = capture_module._NativeCoreAudioBridge()
    native_devices = bridge.enumerate_devices()
    first = capture_module.enumerate_input_devices()
    second = capture_module.enumerate_input_devices()

    assert native_devices
    assert any(device.name == "BlackHole 2ch" for device in native_devices)
    assert first.devices
    assert [device.logical_id for device in first.devices] == [
        device.logical_id for device in second.devices
    ]
    assert len({device.logical_id for device in first.devices}) == len(first.devices)
    assert sum(device.is_default for device in first.devices) <= 1
    assert all(device.logical_id.startswith("macos-input-") for device in first.devices)
    assert not any(
        device.logical_id == native.uid
        for device in first.devices
        for native in native_devices
    )
    opened.assert_not_called()


def test_platform_selector_and_runtime_protocol_use_macos_adapter(capture_stack) -> None:
    adapter = capture_stack.adapter()
    assert isinstance(adapter, CaptureAdapter)
    assert isinstance(adapter, MacosCaptureAdapter)
    assert capture_selector._adapter_module("Darwin") is capture_module
    assert (adapter.sample_rate, adapter.channels, adapter.dtype) == (
        CAPTURE_SAMPLE_RATE,
        CAPTURE_CHANNELS,
        CAPTURE_DTYPE,
    )
    assert "sounddevice" not in sys.modules.get(
        "whisperstream.capture_contract"
    ).__dict__


def test_main_import_keeps_macos_capture_adapter_lazy() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import whisperstream.main; "
                "raise SystemExit('whisperstream.macos_capture' in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_uid_identity_survives_reorder_and_default_change(capture_stack) -> None:
    adapter = capture_stack.adapter()
    first = adapter.enumerate_input_devices()
    first_ids = {device.display_name: device.logical_id for device in first.devices}
    assert next(device.display_name for device in first.devices if device.is_default) == capture_stack.built_in.name

    capture_stack.bridge.devices = (
        replace(capture_stack.virtual, is_default=True),
        replace(capture_stack.built_in, is_default=False),
    )
    capture_stack.state.portaudio = tuple(
        {**record, "is_portaudio_default": record["index"] == 9}
        for record in reversed(capture_stack.state.portaudio)
    )
    capture_stack.state.default_index = 9

    second = adapter.enumerate_input_devices()
    second_ids = {device.display_name: device.logical_id for device in second.devices}
    assert second_ids == first_ids
    assert next(device.display_name for device in second.devices if device.is_default) == capture_stack.virtual.name

    session = _start(adapter, CaptureRequest("new-default", 2))
    assert session.device_id == first_ids[capture_stack.virtual.name]
    assert session.selection_resolution is CaptureSelectionResolution.SYSTEM_DEFAULT
    adapter.cancel_capture(session)


def test_remembered_selection_missing_and_open_failure_fallback(capture_stack) -> None:
    adapter = capture_stack.adapter()
    inventory = adapter.enumerate_input_devices()
    virtual_id = next(
        device.logical_id
        for device in inventory.devices
        if device.display_name == capture_stack.virtual.name
    )
    selected = _start(
        adapter,
        CaptureRequest(
            "selected",
            3,
            selection=CaptureSelection(preferred_id=virtual_id),
        ),
    )
    assert selected.device_id == virtual_id
    assert selected.selection_resolution is CaptureSelectionResolution.SELECTED
    assert capture_stack.created[-1].kwargs["device"] == 9
    adapter.cancel_capture(selected)

    missing = _start(
        adapter,
        CaptureRequest(
            "missing",
            4,
            selection=CaptureSelection(preferred_id="macos-input-missing"),
        ),
    )
    assert missing.selection_resolution is CaptureSelectionResolution.FALLBACK_MISSING
    assert capture_stack.created[-1].kwargs["device"] is None
    adapter.cancel_capture(missing)

    capture_stack.state.fail_start.add(9)
    fallback = _start(
        adapter,
        CaptureRequest(
            "open-fallback",
            5,
            selection=CaptureSelection(legacy_name=capture_stack.virtual.name),
        ),
    )
    assert fallback.selection_resolution is CaptureSelectionResolution.FALLBACK_OPEN_FAILED
    assert capture_stack.created[-2].closed
    assert capture_stack.created[-1].kwargs["device"] is None
    adapter.cancel_capture(fallback)


def test_supported_format_discovery_uses_non_mutating_coreaudio_conversion(capture_stack) -> None:
    capture_stack.state.unsupported.add(9)
    support = capture_stack.adapter().supported_capture_formats()

    assert len(support) == 2
    assert [item.canonical_format_supported for item in support] == [True, False]
    assert support[0].native_sample_rate == 48_000.0
    assert support[0].native_sample_rate_ranges == capture_stack.built_in.available_sample_rate_ranges
    assert capture_stack.checked
    for query in capture_stack.checked:
        assert query["samplerate"] == CAPTURE_SAMPLE_RATE
        assert query["channels"] == CAPTURE_CHANNELS
        assert query["dtype"] == CAPTURE_DTYPE
        label, settings = query["extra_settings"]
        assert label == "coreaudio-settings"
        assert settings == {
            "change_device_parameters": False,
            "fail_if_conversion_required": False,
            "conversion_quality": "max",
        }


def test_open_failure_retains_unclean_stream_and_blocks_next_start(
    monkeypatch,
    capture_stack,
) -> None:
    capture_stack.state.fail_start.add(capture_stack.state.default_index)
    close_attempts = 0

    def _persistent_close(stream: FakeStream) -> None:
        nonlocal close_attempts
        close_attempts += 1
        if close_attempts < 3:
            raise OSError("synthetic persistent close failure")
        stream.closed = True

    monkeypatch.setattr(FakeStream, "close", _persistent_close)
    adapter = capture_stack.adapter()

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
    assert len(capture_stack.created) == 1
    assert adapter.close() is True
    assert adapter.active_session is None
    assert not adapter.stream_open


def test_supported_format_discovery_does_not_touch_portaudio_before_grant(
    capture_stack, monkeypatch
) -> None:
    checked = MagicMock()
    monkeypatch.setattr(capture_module.sd, "check_input_settings", checked)
    adapter = MacosCaptureAdapter(
        _bridge=capture_stack.bridge,
        _permission_state=lambda: PermissionState.NOT_DETERMINED,
    )

    support = adapter.supported_capture_formats()

    assert support
    assert all(item.canonical_format_supported is None for item in support)
    assert all(item.native_sample_rate_ranges for item in support)
    checked.assert_not_called()


@pytest.mark.parametrize(
    ("state", "failure"),
    [
        (PermissionState.NOT_DETERMINED, CaptureFailure.OPEN_FAILED),
        (PermissionState.RESTRICTED, CaptureFailure.OPEN_FAILED),
        (PermissionState.DENIED, CaptureFailure.OPEN_FAILED),
        (PermissionState.UNKNOWN, CaptureFailure.NATIVE_ERROR),
        (PermissionState.UNSUPPORTED, CaptureFailure.NATIVE_ERROR),
    ],
)
def test_permission_gate_is_typed_and_never_opens(
    capture_stack, monkeypatch, state, failure
) -> None:
    opened = MagicMock()
    monkeypatch.setattr(capture_module.sd, "InputStream", opened)
    adapter = MacosCaptureAdapter(
        _bridge=capture_stack.bridge,
        _permission_state=lambda: state,
    )

    outcome = adapter.start_capture(CaptureRequest("permission", 6))

    assert outcome.failure is failure
    assert outcome.session is None
    opened.assert_not_called()
    assert not adapter.stream_open
    assert not adapter.chunk_worker_alive


def test_synthetic_samples_normalize_levels_chunks_preview_and_stop(capture_stack) -> None:
    levels: list[float] = []
    chunks: list[np.ndarray] = []
    previews = 0

    def preview() -> None:
        nonlocal previews
        previews += 1

    adapter = capture_stack.adapter(
        chunk_duration=0.5,
        chunk_overlap=0.1,
    )
    adapter.set_preview_callback(preview, max_duration=1.0)
    session = adapter.start_capture(
        CaptureRequest("reviewed-fixture", 7),
        on_level=levels.append,
        on_chunk=lambda chunk: chunks.append(chunk.copy()),
    ).session
    assert session is not None
    audio = _fixture_audio()
    capture_stack.created[-1].callback(audio.reshape(-1, 1), audio.size, None, None)
    snapshot = adapter.preview_snapshot()
    completion = adapter.stop_capture(session)

    assert completion.status is CaptureCompletionStatus.STOPPED
    assert completion.audio is not None
    assert completion.audio.dtype == np.float32
    assert completion.audio.ndim == 1
    np.testing.assert_array_equal(completion.audio, audio)
    assert levels and all(0.0 <= level <= 1.0 for level in levels)
    assert chunks and all(chunk.dtype == np.float32 for chunk in chunks)
    assert sum(chunk.size for chunk in chunks) >= audio.size
    assert previews == 1
    assert snapshot.total_samples == audio.size
    assert snapshot.audio.size == CAPTURE_SAMPLE_RATE
    assert capture_stack.created[-1].stopped and capture_stack.created[-1].closed
    assert not adapter.stream_open
    assert not adapter.chunk_worker_alive


def test_cancel_stale_generation_device_loss_and_callback_failure_close(capture_stack) -> None:
    terminal_events: list[CaptureTerminalEvent] = []
    adapter = capture_stack.adapter(terminal_callback=terminal_events.append)
    session = _start(adapter, CaptureRequest("cancel", 8))
    stream = capture_stack.created[-1]
    stream.callback(np.ones((32, 1), dtype=np.float32), 32, None, None)
    stale = CaptureSession(
        request_id="stale",
        generation=7,
        device_id="opaque-stale",
        selection_resolution=CaptureSelectionResolution.SYSTEM_DEFAULT,
    )
    assert adapter.cancel_capture(stale).failure is CaptureFailure.STALE_GENERATION
    assert adapter.stream_open and not stream.closed
    cancelled = adapter.cancel_capture(session)
    assert cancelled.status is CaptureCompletionStatus.CANCELLED
    assert cancelled.audio is None
    assert stream.closed and not adapter.stream_open

    lost_session = _start(adapter, CaptureRequest("lost", 9))
    lost_stream = capture_stack.created[-1]
    lost_stream.finished_callback()
    lost_stream.finished_callback()
    assert terminal_events == [
        CaptureTerminalEvent(
            request_id="lost",
            generation=9,
            failure=CaptureFailure.DEVICE_LOST,
            owner_epoch=adapter.owner_epoch,
        )
    ]
    lost = adapter.stop_capture(lost_session)
    assert lost.status is CaptureCompletionStatus.FAILED
    assert lost.failure is CaptureFailure.DEVICE_LOST
    assert lost_stream.closed and not adapter.stream_open

    failing = capture_stack.adapter()
    failed_session = failing.start_capture(
        CaptureRequest("callback", 10),
        on_level=lambda _level: (_ for _ in ()).throw(RuntimeError("synthetic")),
    ).session
    assert failed_session is not None
    failed_stream = capture_stack.created[-1]
    with pytest.raises(capture_module.sd.CallbackAbort):
        failed_stream.callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
    failed = failing.stop_capture(failed_session)
    assert failed.failure is CaptureFailure.STREAM_FAILED
    assert failed_stream.closed and not failing.stream_open


def test_device_loss_close_failure_is_authoritative_and_retryable(
    capture_stack,
) -> None:
    adapter = capture_stack.adapter()
    session = _start(adapter, CaptureRequest("loss-close-failure", 14))
    stream = capture_stack.created[-1]
    stream.finished_callback()
    attempts = 0

    def flaky_close() -> None:
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
    created_before = len(capture_stack.created)
    blocked = adapter.start_capture(CaptureRequest("blocked-after-cleanup", 15))
    assert blocked.failure is CaptureFailure.NATIVE_ERROR
    with pytest.raises(capture_module.AudioCaptureError):
        adapter.start()
    assert len(capture_stack.created) == created_before
    assert adapter.close() is True
    assert attempts == 3
    assert adapter.active_session is None
    assert not adapter.stream_open


def test_device_loss_worker_timeout_is_authoritative_and_retryable(
    monkeypatch,
    capture_stack,
) -> None:
    monkeypatch.setattr(capture_module, "CAPTURE_STOP_TIMEOUT_SECONDS", 0.01)
    started = threading.Event()
    release = threading.Event()

    def slow_chunk(_chunk: np.ndarray) -> None:
        started.set()
        assert release.wait(2.0)

    adapter = capture_stack.adapter(
        chunk_duration=4 / CAPTURE_SAMPLE_RATE,
        chunk_overlap=0.0,
    )
    session = adapter.start_capture(
        CaptureRequest("loss-worker-timeout", 15),
        on_chunk=slow_chunk,
    ).session
    assert session is not None
    stream = capture_stack.created[-1]
    stream.callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
    assert started.wait(2.0)
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
    assert adapter.close() is True
    assert adapter.active_session is None
    assert not adapter.chunk_worker_alive


def test_stream_closes_after_stt_failure_cleanup_failure_and_app_shutdown(
    capture_stack,
) -> None:
    callback_seen = threading.Event()

    def failing_stt(_chunk: np.ndarray) -> None:
        callback_seen.set()
        raise RuntimeError("synthetic downstream STT failure")

    adapter = capture_stack.adapter(
        chunk_duration=4 / CAPTURE_SAMPLE_RATE,
        chunk_overlap=0.0,
    )
    session = adapter.start_capture(
        CaptureRequest("stt-failure", 11),
        on_chunk=failing_stt,
    ).session
    assert session is not None
    stream = capture_stack.created[-1]
    stream.callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
    completion = adapter.stop_capture(session)
    assert callback_seen.is_set()
    assert completion.status is CaptureCompletionStatus.STOPPED
    assert stream.closed and not adapter.chunk_worker_alive

    cleanup_adapter = capture_stack.adapter()
    cleanup_session = _start(cleanup_adapter, CaptureRequest("cleanup", 12))
    cleanup_stream = capture_stack.created[-1]
    cleanup_stream.fail_stop = True
    cleanup = cleanup_adapter.stop_capture(cleanup_session)
    assert cleanup.failure is CaptureFailure.CLEANUP_FAILED
    assert cleanup_stream.closed and not cleanup_adapter.stream_open

    shutdown_adapter = capture_stack.adapter()
    _start(shutdown_adapter, CaptureRequest("shutdown", 13))
    shutdown_stream = capture_stack.created[-1]
    shutdown_adapter.close()
    shutdown_adapter.close()
    assert shutdown_stream.closed and not shutdown_adapter.stream_open
    assert not shutdown_adapter.chunk_worker_alive


def test_device_names_and_reviewed_audio_are_absent_from_adapter_diagnostics(
    capture_stack, caplog
) -> None:
    caplog.set_level("DEBUG")
    capture_stack.state.fail_start.add(9)
    adapter = capture_stack.adapter(device=capture_stack.virtual.name)
    adapter.start()
    adapter.cancel()

    assert capture_stack.built_in.name not in caplog.text
    assert capture_stack.virtual.name not in caplog.text
    assert "reviewed-coreaudio-uid" not in caplog.text
