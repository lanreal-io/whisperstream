"""Real sounddevice wrapper with wholly fake native operations. No device I/O."""

import gc
import weakref

import numpy as np
import pytest
import sounddevice as sd

from whisperstream.audio_capture import AudioCapture
from whisperstream.capture_contract import (
    CaptureCompletionStatus, CaptureFailure, CaptureRequest,
    CaptureSelection, CaptureSelectionResolution,
)


class Native:
    paNoFlag = 0
    paAbort = 2
    paUnanticipatedHostError = -9999
    paStreamIsStopped = -9983
    paStreamIsNotStopped = -9982

    def __init__(self):
        self.failure = None
        self.close_error = False
        self.stop_error = 0
        self.opens = self.closes = self.stops = 0
        self.on_stop = lambda: None
        self.message = sd._ffi.new("char[]", b"synthetic native error")
        self.info = sd._ffi.new("PaStreamInfo *", dict(
            structVersion=1, inputLatency=0.01, outputLatency=0, sampleRate=16000))

    def Pa_OpenStream(self, ptr, *args):
        self.opens += 1
        if self.failure == "pre_open":
            return -9986
        ptr[0] = sd._ffi.cast("PaStream *", 1234)
        return 0

    def Pa_GetStreamInfo(self, ptr):
        return sd._ffi.NULL if self.failure == "info" else self.info

    def Pa_SetStreamFinishedCallback(self, *args):
        return -9986 if self.failure == "finished" else 0

    def Pa_StartStream(self, ptr):
        return -9986 if self.failure == "start" else 0

    def Pa_StopStream(self, ptr):
        assert ptr != sd._ffi.NULL
        self.stops += 1
        self.on_stop()
        return self.stop_error

    def Pa_CloseStream(self, ptr):
        assert ptr != sd._ffi.NULL
        self.closes += 1
        return -9986 if self.close_error else 0

    def Pa_GetErrorText(self, code):
        return self.message


@pytest.fixture
def native(monkeypatch):
    lib = Native()
    parameters = sd._ffi.new("PaStreamParameters *", dict(
        device=0, channelCount=1, sampleFormat=1, suggestedLatency=0.01,
        hostApiSpecificStreamInfo=sd._ffi.NULL))
    monkeypatch.setattr(sd, "_lib", lib)
    monkeypatch.setattr(sd, "_get_stream_parameters",
                        lambda *a, **kw: (parameters, "float32", 4, 16000))
    monkeypatch.setattr(AudioCapture, "_resolve_request", lambda *a:
                        (None, "default", CaptureSelectionResolution.SYSTEM_DEFAULT, "fake"))
    return lib


@pytest.mark.parametrize("failure", ["pre_open", "info", "finished", "start"])
@pytest.mark.parametrize("close_error", [False, True])
def test_partial_construction_retains_actual_wrapper_and_callbacks(native, failure, close_error):
    native.failure, native.close_error = failure, close_error
    adapter = AudioCapture()
    result = adapter.start_capture(CaptureRequest("construction", 1))
    uncertain = failure != "pre_open" and close_error
    assert result.failure is (CaptureFailure.CLEANUP_FAILED if uncertain else CaptureFailure.OPEN_FAILED)
    assert native.closes == (failure != "pre_open")
    assert adapter.capture_available is not uncertain
    if uncertain:
        retained = adapter._stream
        callback = weakref.ref(retained._callback)
        assert retained._ptr == sd._ffi.NULL
        gc.collect()
        assert callback() is not None
        assert adapter.close() is False
        assert adapter.start_capture(CaptureRequest("next", 2)).failure is not None
        assert native.opens == 1 and native.closes == 1
    else:
        native.failure, native.close_error = None, False
        assert adapter.start_capture(CaptureRequest("next", 2)).session is not None
        assert adapter.close() is True


@pytest.mark.parametrize("operation", ["stop_capture", "cancel_capture"])
def test_checked_native_close_error_never_retries_cleared_pointer(native, operation):
    adapter = AudioCapture()
    session = adapter.start_capture(CaptureRequest("close", 1)).session
    stream = adapter._stream
    callback = weakref.ref(stream._callback)
    native.close_error = True
    result = getattr(adapter, operation)(session)
    assert result.failure is CaptureFailure.CLEANUP_FAILED
    assert stream._ptr == sd._ffi.NULL
    assert not adapter.cleanup_retryable and not adapter.capture_available
    del stream
    gc.collect()
    assert callback() is not None
    assert adapter.close() is False
    assert adapter.stop_capture(session).failure is CaptureFailure.CLEANUP_FAILED
    assert adapter.start_capture(CaptureRequest("next", 2)).failure is not None
    assert (native.opens, native.stops, native.closes) == (1, 1, 1)


@pytest.mark.parametrize("error", [-9986, Native.paStreamIsStopped])
def test_checked_stop_error_with_confirmed_close(native, error):
    adapter = AudioCapture()
    session = adapter.start_capture(CaptureRequest("stop", 1)).session
    native.stop_error = error
    result = adapter.stop_capture(session)
    expected = CaptureCompletionStatus.STOPPED if error == Native.paStreamIsStopped else CaptureCompletionStatus.FAILED
    assert result.status is expected
    if error != Native.paStreamIsStopped:
        assert adapter.stop_capture(session).status is not CaptureCompletionStatus.STOPPED
    assert adapter.close() is True
    assert native.closes == 1


def test_real_wrapper_stop_overlap_latches_failure_before_tail(native):
    chunks = []
    adapter = AudioCapture(chunk_duration=1, chunk_overlap=0)
    session = adapter.start_capture(CaptureRequest("overlap", 1), on_chunk=chunks.append).session
    adapter._stream_callback(np.ones((4, 1), dtype=np.float32), 4, None, None,
                             token=adapter._native_callback_token)
    native.on_stop = lambda: adapter._record_failure(CaptureFailure.STREAM_FAILED)
    result = adapter.stop_capture(session)
    assert result.failure is CaptureFailure.STREAM_FAILED
    assert result.audio is None and chunks == []
    assert adapter.close() is True


@pytest.mark.parametrize("close_error", [False, True])
def test_selected_constructor_failure_only_falls_back_after_confirmed_close(native, monkeypatch, close_error):
    import whisperstream.audio_capture as module
    from types import SimpleNamespace
    adapter = AudioCapture()
    monkeypatch.setattr(adapter, "_resolve_request", lambda *a:
                        (1, "selected", CaptureSelectionResolution.SELECTED, "fake"))
    monkeypatch.setattr(module, "_query_native_input_devices", lambda **kw:
                        [SimpleNamespace(is_default=True, logical_id="default")])
    native.failure, native.close_error = "info", close_error
    close = native.Pa_CloseStream
    def finish_first(ptr):
        result = close(ptr)
        native.failure = None
        return result
    native.Pa_CloseStream = finish_first
    result = adapter.start_capture(CaptureRequest("fallback", 1,
                                    selection=CaptureSelection(legacy_name="selected")))
    if close_error:
        assert result.failure is CaptureFailure.CLEANUP_FAILED
        assert native.opens == 1 and adapter.close() is False
    else:
        assert result.session.selection_resolution is CaptureSelectionResolution.FALLBACK_OPEN_FAILED
        assert native.opens == 2 and adapter.close() is True


def test_selected_start_failure_cannot_fallback_while_chunk_worker_is_owned(native, monkeypatch):
    import threading
    import whisperstream.audio_capture as module
    entered, release = threading.Event(), threading.Event()
    def consume(_chunk):
        entered.set()
        assert release.wait(3)
    adapter = AudioCapture(chunk_duration=4 / 16000, chunk_overlap=0)
    monkeypatch.setattr(module, "CAPTURE_STOP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(adapter, "_resolve_request", lambda *a:
                        (1, "selected", CaptureSelectionResolution.SELECTED, "fake"))
    def start(ptr):
        adapter._stream_callback(np.ones((8, 1), dtype=np.float32), 8, None, None,
                                 token=adapter._native_callback_token)
        assert entered.wait(2)
        return -9986
    native.Pa_StartStream = start
    try:
        result = adapter.start_capture(CaptureRequest("fallback-worker", 1), on_chunk=consume)
        assert result.failure is CaptureFailure.TIMEOUT
        assert native.opens == 1 and native.closes == 1
        assert adapter.chunk_worker_alive and adapter.cleanup_retryable
    finally:
        release.set()
        monkeypatch.setattr(module, "CAPTURE_STOP_TIMEOUT_SECONDS", 2)
        assert adapter.close() is True
