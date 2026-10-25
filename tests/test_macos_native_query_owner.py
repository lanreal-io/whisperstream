"""Read-only main-owner lifetime, scheduling and cancellation contracts."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from whisperstream.macos_native_query_owner import NativeQueryOwner, NativeQueryUnavailable

class Loop:

    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, callback, request):
        self.calls.append((callback, request))

    def drain(self):
        for callback, request in self.calls:
            callback(request)
        self.calls.clear()

def wait_pending(loop):
    deadline = time.monotonic() + 1
    while not loop.calls and time.monotonic() < deadline:
        time.sleep(0.001)
    assert loop.calls

def test_snapshot_runs_on_main_owner():
    loop = Loop()
    owner = NativeQueryOwner(loop=loop)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(owner.query, lambda: threading.current_thread())
        wait_pending(loop)
        loop.drain()
        assert future.result() is threading.main_thread()
    owner.close()

def test_starved_owner_cancels_queued_query_and_late_callback():
    loop = Loop()
    owner = NativeQueryOwner(loop=loop, timeout=0.01)
    observed = []
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(owner.query, lambda: observed.append('ran'))
        with pytest.raises(NativeQueryUnavailable, match='timeout'):
            future.result()
        loop.drain()
    assert observed == []
    owner.close()

def test_shutdown_releases_waiter_without_late_query():
    loop = Loop()
    owner = NativeQueryOwner(loop=loop)
    observed = []
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(owner.query, lambda: observed.append('ran'))
        wait_pending(loop)
        owner.close()
        with pytest.raises(NativeQueryUnavailable):
            future.result()
        loop.drain()
    assert observed == []
    with pytest.raises(NativeQueryUnavailable):
        owner.query(lambda: None)

def test_standalone_service_preserves_worker_result():
    owner = NativeQueryOwner()
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(owner.query, lambda: 42)
        deadline = time.monotonic() + 1
        while not future.done() and time.monotonic() < deadline:
            owner.service()
            time.sleep(0.001)
        assert future.result() == 42
    owner.close()

def test_snapshot_exception_propagates_fail_closed():
    owner = NativeQueryOwner()

    def failure():
        raise ValueError('synthetic')
    with pytest.raises(ValueError):
        owner.query(failure)
    owner.close()

def test_standalone_shortcut_wait_services_queries_and_preserves_stop_event():
    from whisperstream.macos_native_query_owner import bind_owner, close_owner
    from whisperstream.macos_shortcuts import MacosShortcutAdapter
    owner = bind_owner()
    adapter = object.__new__(MacosShortcutAdapter)
    adapter._wait_event = threading.Event()

    def snapshot():
        adapter._wait_event.set()
        return 7
    try:
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(owner.query, snapshot)
            adapter.wait()
            assert future.result() == 7
            assert adapter._wait_event.is_set()
    finally:
        close_owner()

def test_app_server_loop_binding_and_shutdown_clear_owner():
    import sys
    if sys.platform != 'darwin':
        pytest.skip('Mac app lifecycle')
    import asyncio
    import whisperstream.macos_native_query_owner as native_owner
    from whisperstream.main import WhisperstreamApp
    app = object.__new__(WhisperstreamApp)
    loop = asyncio.new_event_loop()
    try:
        app.set_server_loop(loop)
        owner = native_owner._owner
        assert owner is not None and owner.loop is loop
        app.set_server_loop(None)
        assert native_owner._owner is None
        with pytest.raises(NativeQueryUnavailable):
            owner.query(lambda: 1)
    finally:
        native_owner.close_owner()
        loop.close()

def test_standalone_app_run_binds_owner_until_keyboard_wait_returns():
    import sys
    if sys.platform != 'darwin':
        pytest.skip('Mac app lifecycle')
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import whisperstream.macos_native_query_owner as native_owner
    from whisperstream.main import WhisperstreamApp
    app = object.__new__(WhisperstreamApp)
    app._server_loop = None
    app._file_only = False
    app.logger = MagicMock()
    observed = []
    app.keyboard = SimpleNamespace(wait=lambda: observed.append(native_owner._owner))
    try:
        app.run(register_hotkeys=False)
        assert len(observed) == 1 and observed[0] is not None
        assert native_owner._owner is None
    finally:
        native_owner.close_owner()

def test_active_timeout_discards_result_after_atomic_start_claim():
    # Order the race with events, not sleeps: Windows rounds a 10 ms wait up to
    # its ~15.6 ms timer tick, and a loaded runner can deschedule either thread,
    # so a sleep margin let the timeout fire before the claim or after the result.
    started = threading.Event()

    class GatedLoop(Loop):
        # Hold the waiter until the main owner has claimed the request, so its
        # timeout can only start after the atomic start claim.
        def call_soon_threadsafe(self, callback, request):
            super().call_soon_threadsafe(callback, request)
            assert started.wait(5)

    loop = GatedLoop()
    owner = NativeQueryOwner(loop=loop, timeout=0.01)
    calls = []

    def slow_snapshot():
        assert loop.calls[0][1].started
        calls.append('started')
        started.set()
        # Return only after the waiter has timed out and canceled the request.
        deadline = time.monotonic() + 5
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert future.done()
        return 'discarded-synthetic-result'
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(owner.query, slow_snapshot)
        wait_pending(loop)
        request = loop.calls[0][1]
        loop.drain()
        with pytest.raises(NativeQueryUnavailable, match='timeout'):
            future.result()
    assert calls == ['started']
    assert request.result is None
    assert request.operation is None
    owner.close()

def test_canceled_undrained_requests_keep_admission_bounded():
    loop = Loop()
    owner = NativeQueryOwner(loop=loop, timeout=0.01, capacity=1)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(owner.query, lambda: None)
        with pytest.raises(NativeQueryUnavailable, match='timeout'):
            future.result()
        future = executor.submit(owner.query, lambda: None)
        with pytest.raises(NativeQueryUnavailable, match='busy'):
            future.result()
        assert len(loop.calls) == 1
        assert loop.calls[0][1].operation is None
        loop.drain()
        owner.timeout = 0.5
        future = executor.submit(owner.query, lambda: 42)
        wait_pending(loop)
        loop.drain()
        assert future.result() == 42
    owner.close()

def test_closed_loop_scheduling_fails_without_retaining_request():

    class ClosedLoop:

        def call_soon_threadsafe(self, *args):
            raise RuntimeError('loop closed')
    owner = NativeQueryOwner(loop=ClosedLoop())
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(owner.query, lambda: None)
        with pytest.raises(NativeQueryUnavailable, match='scheduling failed'):
            future.result()
    assert owner._pending == set()
    owner.close()

def test_nested_snapshot_services_native_loop_only_once(monkeypatch):
    import whisperstream.macos_native_query_owner as native_owner
    observed = []
    monkeypatch.setattr(native_owner, '_permission_bridge', type('Bridge', (), {'_drain_main_run_loop': lambda self: observed.append('pump')})())
    native_owner.close_owner()
    assert native_owner.query_on_main(lambda: native_owner.query_on_main(lambda: 42)) == 42
    assert observed == ['pump']


def test_file_only_background_loop_does_not_bind_or_close_native_owner(monkeypatch):
    import sys
    from whisperstream.main import WhisperstreamApp
    import whisperstream.macos_native_query_owner as native_owner
    monkeypatch.setattr(sys, 'platform', 'darwin')
    app = object.__new__(WhisperstreamApp)
    app._file_only = True
    existing = native_owner.bind_owner()
    errors = []

    def serve():
        try:
            app.set_server_loop(object())
            app.set_server_loop(None)
            app.close_native_query_owner()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve)
    try:
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert errors == []
        assert native_owner._owner is existing
        assert existing.query(lambda: 42) == 42
    finally:
        native_owner.close_owner()


def test_stale_app_shutdown_preserves_new_global_owner(monkeypatch):
    import sys
    from whisperstream.main import WhisperstreamApp
    import whisperstream.macos_native_query_owner as native_owner
    monkeypatch.setattr(sys, 'platform', 'darwin')
    first = object.__new__(WhisperstreamApp)
    second = object.__new__(WhisperstreamApp)
    try:
        first.set_server_loop(object())
        old_owner = native_owner._owner
        second.set_server_loop(object())
        new_owner = native_owner._owner
        first.close_native_query_owner()
        first.set_server_loop(None)
        assert native_owner._owner is new_owner
        assert new_owner.query(lambda: 42) == 42
        with pytest.raises(NativeQueryUnavailable):
            old_owner.query(lambda: 1)
        second.set_server_loop(None)
        assert native_owner._owner is None
    finally:
        native_owner.close_owner()


def test_standalone_file_only_run_leaves_native_owner_untouched(monkeypatch):
    import sys
    from unittest.mock import Mock
    from whisperstream.main import WhisperstreamApp
    import whisperstream.macos_native_query_owner as native_owner
    monkeypatch.setattr(sys, 'platform', 'darwin')
    app = object.__new__(WhisperstreamApp)
    app._file_only = True
    app._server_loop = None
    app._file_only_stop = Mock()
    app.logger = Mock()
    existing = native_owner.bind_owner()
    try:
        app.run(register_hotkeys=False)
        app._file_only_stop.wait.assert_called_once_with()
        assert native_owner._owner is existing
        assert existing.query(lambda: 42) == 42
    finally:
        native_owner.close_owner()


def test_rejected_worker_binding_preserves_existing_owner():
    import whisperstream.macos_native_query_owner as native_owner
    existing = native_owner.bind_owner()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(native_owner.bind_owner)
            with pytest.raises(NativeQueryUnavailable):
                future.result(timeout=1)
        assert native_owner._owner is existing
        assert existing.query(lambda: 42) == 42
    finally:
        native_owner.close_owner()
