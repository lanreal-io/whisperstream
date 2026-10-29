"""Deterministic Mac terminal ownership regressions, without native audio."""
import threading
import numpy as np
from tests.test_macos_capture_adapter import capture_stack, _start
from whisperstream import macos_capture as module
from whisperstream.capture_contract import CaptureRequest, CaptureFailure, CaptureCompletionStatus


def test_retired_callbacks_cannot_write_or_fail_successor(capture_stack):
    adapter = capture_stack.adapter()
    first = _start(adapter)
    old = capture_stack.created[-1]
    adapter.stop_capture(first)
    second = _start(adapter, CaptureRequest('second', 2))
    old.callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
    old.finished_callback()
    result = adapter.stop_capture(second)
    assert result.status is CaptureCompletionStatus.STOPPED
    assert result.audio.size == 0
    adapter.close()


def test_terminal_timeout_retains_owner_and_never_retries(capture_stack, monkeypatch):
    monkeypatch.setattr(module, 'CAPTURE_STOP_TIMEOUT_SECONDS', .02)
    adapter = capture_stack.adapter()
    session = _start(adapter)
    stream = capture_stack.created[-1]
    entered, release = threading.Event(), threading.Event()
    calls = []
    def stop(**kwargs):
        calls.append(threading.get_ident())
        entered.set()
        release.wait(2)
    stream.stop = stop
    result = adapter.stop_capture(session)
    assert entered.is_set()
    assert result.failure is CaptureFailure.TIMEOUT
    assert adapter.capture_available is False
    assert adapter.close() is False
    assert adapter.cancel_capture(session).failure is CaptureFailure.TIMEOUT
    assert len(calls) == 1
    release.set()


def test_optional_level_failure_does_not_abort_capture(capture_stack):
    adapter = capture_stack.adapter()
    result = adapter.start_capture(CaptureRequest('optional', 1), on_level=lambda _: 1 / 0)
    capture_stack.created[-1].callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
    assert adapter.stop_capture(result.session).status is CaptureCompletionStatus.STOPPED
    adapter.close()


def test_concurrent_cancel_joins_stop_and_wins_before_commit(capture_stack):
    adapter = capture_stack.adapter()
    session = _start(adapter)
    stream = capture_stack.created[-1]
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []
    def stop(**kwargs):
        calls.append('stop')
        entered.set()
        assert release.wait(2)
        stream.finished_callback()  # Reentrant expected finish must not fail.
    stream.stop = stop
    a = threading.Thread(target=lambda: results.append(adapter.stop_capture(session)))
    b = threading.Thread(target=lambda: results.append(adapter.cancel_capture(session)))
    a.start()
    assert entered.wait(2)
    b.start()
    # Synchronize on actual adapter admission, not thread scheduling.
    import time
    deadline = time.monotonic() + 2
    while not adapter._context.cancelled and time.monotonic() < deadline:
        threading.Event().wait(.001)
    assert adapter._context.cancelled
    release.set()
    a.join(2); b.join(2)
    assert calls == ['stop']
    assert len(results) == 2 and results[0] is results[1]
    assert results[0].status is CaptureCompletionStatus.CANCELLED


def test_genuine_callback_failure_requests_cleanup_without_abort(capture_stack):
    events = []
    adapter = capture_stack.adapter(terminal_callback=events.append)
    session = _start(adapter)
    class BadAudio:
        def __array__(self, *args, **kwargs):
            raise ValueError('synthetic processing failure')
    stream = capture_stack.created[-1]
    stream.callback(BadAudio(), 8, None, None)
    stream.callback(BadAudio(), 8, None, None)
    assert len(events) == 1
    assert not stream.stopped  # The app must restore before submitting cleanup.
    assert adapter.cancel_capture(session).failure is CaptureFailure.STREAM_FAILED


import pytest

@pytest.mark.parametrize('block_start', [False, True])
def test_abandoned_open_is_never_published_and_late_resource_is_closed(capture_stack, monkeypatch, block_start):
    monkeypatch.setattr(module, 'CAPTURE_START_TIMEOUT_SECONDS', .02)
    original = module.sd.InputStream
    entered, release = threading.Event(), threading.Event()
    calls = []
    def construct(**kwargs):
        stream = original(**kwargs)
        original_start = stream.start
        def start():
            calls.append('start')
            if block_start:
                entered.set(); release.wait(2)
            original_start()
        stream.start = start
        if not block_start:
            entered.set(); release.wait(2)
        return stream
    monkeypatch.setattr(module.sd, 'InputStream', construct)
    adapter = capture_stack.adapter()
    result = adapter.start_capture(CaptureRequest('abandoned', 1))
    assert entered.is_set() and result.failure is CaptureFailure.TIMEOUT
    assert adapter.capture_available is False
    release.set()
    assert adapter._context.released.wait(2)
    assert adapter.active_session is None
    assert capture_stack.created[-1].closed
    assert calls == (['start'] if block_start else [])
    assert adapter.start_capture(CaptureRequest('successor', 2)).failure


@pytest.mark.parametrize('stop_code,close_code', [(0, 0), (-9999, 0), (-9983, 0), (0, -9999)])
def test_checked_real_sounddevice_wrapper_release(capture_stack, monkeypatch, stop_code, close_code):
    from types import SimpleNamespace
    adapter = capture_stack.adapter()
    session = _start(adapter)
    calls = []
    original_lib = module.sd._lib
    class Library:
        def __getattr__(self, name):
            return getattr(original_lib, name)
        def Pa_StopStream(self, ptr):
            calls.append('stop'); return stop_code
        def Pa_CloseStream(self, ptr):
            calls.append('close'); return close_code
    # Use actual wrapper methods, including close's pointer-clearing behavior.
    stream = SimpleNamespace(_ptr=module.sd._ffi.cast('PaStream *', 1234))
    stream.stop = lambda **kw: module.sd._StreamBase.stop(stream, **kw)
    stream.close = lambda **kw: module.sd._StreamBase.close(stream, **kw)
    adapter._context.stream = stream
    monkeypatch.setattr(module.sd, '_lib', Library())
    result = adapter.stop_capture(session)
    assert calls == ['stop', 'close']
    assert stream._ptr == module.sd._ffi.NULL
    if close_code:
        assert result.failure is CaptureFailure.CLEANUP_FAILED
        assert adapter.capture_available is False
        assert adapter.close() is False
        assert calls == ['stop', 'close']
    else:
        assert adapter.capture_available
        assert not adapter.stream_open
        if stop_code == -9983:
            assert result.status is CaptureCompletionStatus.STOPPED


def test_shutdown_overtakes_open_without_publication(capture_stack, monkeypatch):
    original = module.sd.InputStream
    entered, release = threading.Event(), threading.Event()
    def construct(**kwargs):
        stream = original(**kwargs)
        entered.set()
        assert release.wait(2)
        return stream
    monkeypatch.setattr(module.sd, 'InputStream', construct)
    adapter = capture_stack.adapter()
    results = []
    thread = threading.Thread(target=lambda: results.append(adapter.start_capture(CaptureRequest('shutdown-open', 1))))
    thread.start()
    assert entered.wait(2)
    assert adapter.close() is False
    release.set()
    thread.join(2)
    assert results[0].failure is CaptureFailure.TIMEOUT
    assert adapter.active_session is None
    assert capture_stack.created[-1].closed and not capture_stack.created[-1].started


def test_detaching_optional_handlers_during_callback_keeps_snapshot(capture_stack, monkeypatch):
    adapter = capture_stack.adapter(status_callback=lambda _: 1 / 0)
    seen = []
    session = adapter.start_capture(CaptureRequest('snapshot', 1), on_level=seen.append).session
    original = module.np.mean
    def detach(*args, **kwargs):
        adapter.set_level_callback(None)
        return original(*args, **kwargs)
    monkeypatch.setattr(module.np, 'mean', detach)
    capture_stack.created[-1].callback(np.ones((8, 1), dtype=np.float32), 8, None, True)
    assert len(seen) == 1
    assert adapter.stop_capture(session).status is CaptureCompletionStatus.STOPPED
