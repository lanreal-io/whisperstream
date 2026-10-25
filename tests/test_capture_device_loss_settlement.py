"""Shared generation-bound active-input-loss lifecycle coverage."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from whisperstream.capture_contract import (
    CaptureCompletion,
    CaptureCompletionStatus,
    CaptureFailure,
    CaptureSelectionResolution,
    CaptureSession,
    CaptureTerminalEvent,
)
from whisperstream.main import WhisperstreamApp
from whisperstream.agent_compose import AgentComposeLeaseStore
from whisperstream.agent_reply_desk import ReplyDesk, reply_target
from whisperstream.server import WhisperstreamServer


class FakeCapture:
    def __init__(
        self,
        session: CaptureSession,
        events: list[str],
        *,
        owner_epoch: int = 17,
        failure: CaptureFailure = CaptureFailure.DEVICE_LOST,
    ) -> None:
        self.active_session: CaptureSession | None = session
        self.owner_epoch = owner_epoch
        self.failure = failure
        self.events = events
        self.cancel_calls = 0
        self.close_calls = 0

    def set_level_callback(self, _callback) -> None:
        self.events.append("level-clear")

    def cancel_capture(self, session: CaptureSession) -> CaptureCompletion:
        assert session is self.active_session
        self.cancel_calls += 1
        self.events.append("capture-cancel-close")
        self.active_session = None
        return CaptureCompletion(
            request_id=session.request_id,
            generation=session.generation,
            status=CaptureCompletionStatus.FAILED,
            failure=self.failure,
        )

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.events.append("capture-cancel-close")
        self.active_session = None

    def close(self) -> bool:
        self.close_calls += 1
        self.events.append("capture-close-fallback")
        return True


def _initialize_terminal_scheduler(app: WhisperstreamApp) -> None:
    app._capture_terminal_lock = threading.Lock()
    app._capture_terminal_pending = None
    app._capture_terminal_scheduled = False


@contextmanager
def _mock_terminal_thread(factory):
    constructor = MagicMock(side_effect=factory)
    # Patching main.threading.Thread otherwise replaces stdlib Thread for the
    # whole process. Timer.__init__ then calls the mock's __init__, resetting
    # its call history/spec when an unrelated background timer is constructed.
    namespace = SimpleNamespace(**(vars(threading) | {"Thread": constructor}))
    with patch("whisperstream.main.threading", namespace):
        yield constructor


def _loss_event(generation: int = 7, *, owner_epoch: int = 17) -> CaptureTerminalEvent:
    return CaptureTerminalEvent(
        request_id=f"capture-{generation}",
        generation=generation,
        failure=CaptureFailure.DEVICE_LOST,
        owner_epoch=owner_epoch,
    )


def _make_app(
    *,
    restore_fails: bool = False,
    capture_failure: CaptureFailure = CaptureFailure.DEVICE_LOST,
):
    events: list[str] = []
    event = _loss_event()
    session = CaptureSession(
        request_id=event.request_id,
        generation=event.generation,
        device_id="opaque-input",
        selection_resolution=CaptureSelectionResolution.SELECTED,
    )
    capture = FakeCapture(session, events, failure=capture_failure)
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._lock = threading.Lock()
    _initialize_terminal_scheduler(app)
    app._shutting_down = False
    app._recording = True
    app._recording_origin = "push_to_talk"
    app._active_capture_session_key = (
        event.owner_epoch,
        event.request_id,
        event.generation,
    )
    app._stop_timer = MagicMock()
    app._idle_timer = MagicMock()
    app._dictation_app = object()
    app._discarding_recording = False
    app._chunk_futures_lock = threading.Lock()
    queued = MagicMock()
    running = MagicMock()
    queued.cancel.return_value = True
    running.cancel.return_value = False
    app._chunk_futures = [queued, running]
    app._chunk_start_time = 1.0
    app._last_waveform = object()
    app._recording_start_time = 2.0
    app._started_via_toggle = False
    app._recording_acceptance_grant = object()
    app.audio_capture = capture
    app.keyboard = MagicMock()
    app._invalidate_live_preview = MagicMock(
        side_effect=lambda: events.append("preview-invalidate")
    )

    def restore() -> None:
        events.append("output-restore-attempt")
        if restore_fails:
            raise OSError("synthetic restore failure")

    app._restore_system_volume = restore
    app.dictation_state_snapshot = MagicMock(
        return_value={"recording_id": 11, "phase": "recording"}
    )
    app._transition_dictation_state = MagicMock(
        side_effect=lambda _recording_id, _phase: events.append("idle-publish")
        or True
    )
    app._broadcast = MagicMock()
    app._play_recording_chime = MagicMock(
        side_effect=lambda _kind: events.append("stop-cue")
    )
    app._close_audio_policy = MagicMock(
        side_effect=lambda: events.append("audio-policy-close")
    )
    app._dispatch_error_dialog = MagicMock(
        side_effect=lambda *_args, **_kwargs: events.append("failure-publish")
    )

    # Consumers that a failed generation must never reach.
    app.stt_engine = MagicMock()
    app.llm_enhancer = MagicMock()
    app.transcript_store = MagicMock()
    app.license_manager = MagicMock()
    app.text_injector = SimpleNamespace(
        _last_transcript="paste-last-sentinel",
        _last_original="original-sentinel",
        inject=MagicMock(),
    )
    app._persistence_executor = MagicMock()
    app._agent_compose_recording = None
    app._agent_compose_pending_generation = None
    app._agent_compose_leases = None
    return (
        app,
        capture,
        event,
        events,
        queued,
        running,
        app._stop_timer,
        app._idle_timer,
    )


@pytest.mark.parametrize("platform_owner", ["windows", "macos"])
@pytest.mark.parametrize("restore_fails", [False, True])
@pytest.mark.parametrize("failure", [CaptureFailure.DEVICE_LOST, CaptureFailure.STREAM_FAILED])
def test_active_device_loss_settles_once_in_frozen_order_and_suppresses_consumers(
    platform_owner: str,
    restore_fails: bool,
    failure: CaptureFailure,
) -> None:
    # The platform label documents that both adapters enter this one owner path;
    # adapter suites separately prove each native finish emits the same event.
    assert platform_owner in {"windows", "macos"}
    (
        app,
        capture,
        event,
        events,
        queued,
        running,
        stop_timer,
        idle_timer,
    ) = _make_app(restore_fails=restore_fails)
    event = CaptureTerminalEvent(
        request_id=event.request_id,
        generation=event.generation,
        failure=failure,
        owner_epoch=event.owner_epoch,
    )

    app._settle_capture_terminal_event(event)
    app._settle_capture_terminal_event(event)
    app._settle_capture_terminal_event(_loss_event(6))

    assert capture.cancel_calls == 1
    assert capture.close_calls == 0
    app.keyboard.reset_toggle_state.assert_called_once_with()
    assert events.index("output-restore-attempt") < events.index(
        "capture-cancel-close"
    )
    assert events.index("capture-cancel-close") < events.index("stop-cue")
    assert events.index("idle-publish") < events.index("failure-publish")
    assert events.count("failure-publish") == 1
    assert events[-2:] == ["audio-policy-close", "failure-publish"]
    assert app._recording is False
    assert app._active_capture_session_key is None
    assert app._chunk_futures == []
    assert app._last_waveform is None
    assert app._recording_origin is None
    assert app._recording_acceptance_grant is None
    queued.cancel.assert_called_once_with()
    running.cancel.assert_called_once_with()
    stop_timer.cancel.assert_called_once_with()
    idle_timer.cancel.assert_called_once_with()
    app._dispatch_error_dialog.assert_called_once()

    app.stt_engine.assert_not_called()
    app.llm_enhancer.assert_not_called()
    app.transcript_store.assert_not_called()
    app.license_manager.assert_not_called()
    app.text_injector.inject.assert_not_called()
    assert app.text_injector._last_transcript == "paste-last-sentinel"
    assert app.text_injector._last_original == "original-sentinel"
    app._persistence_executor.assert_not_called()


def test_manual_stop_cancel_and_quit_win_loss_races_without_failure_publish() -> None:
    stop_app, stop_capture, stop_event, *_ = _make_app()
    stop_app._recording = False
    stop_app._active_capture_session_key = None
    stop_capture.active_session = None
    stop_app._settle_capture_terminal_event(stop_event)
    assert stop_capture.cancel_calls == 0
    stop_app._dispatch_error_dialog.assert_not_called()

    app, capture, event, _events, _queued, _running, _stop, _idle = _make_app()
    with app._lock:
        app._cancel_recording(trigger="hud")
    app._settle_capture_terminal_event(event)
    assert capture.cancel_calls == 1
    app._dispatch_error_dialog.assert_not_called()

    (
        quit_app,
        quit_capture,
        quit_event,
        _events,
        _queued,
        _running,
        _stop,
        _idle,
    ) = _make_app()
    quit_app._shutting_down = True
    quit_app._settle_capture_terminal_event(quit_event)
    assert quit_capture.cancel_calls == 0
    quit_app._dispatch_error_dialog.assert_not_called()


def test_terminal_callback_schedules_exact_event_off_callback_thread() -> None:
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._shutting_down = False
    _initialize_terminal_scheduler(app)
    done = threading.Event()
    handled: list[tuple[CaptureTerminalEvent, int]] = []
    event = _loss_event()

    def settle(received: CaptureTerminalEvent) -> None:
        handled.append((received, threading.get_ident()))
        done.set()

    app._settle_capture_terminal_event = settle
    callback_thread = threading.get_ident()
    app._enqueue_capture_terminal_event(event)

    assert done.wait(2.0)
    assert len(handled) == 1
    assert handled[0][0] is event
    assert handled[0][1] != callback_thread


def test_terminal_callback_burst_keeps_latest_epoch_and_starts_one_drain() -> None:
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._shutting_down = False
    _initialize_terminal_scheduler(app)
    handled: list[CaptureTerminalEvent] = []
    app._settle_capture_terminal_event = handled.append
    deferred: list[SimpleNamespace] = []

    def thread_factory(*, target, daemon, name):
        thread = SimpleNamespace(target=target, start=MagicMock())
        deferred.append(thread)
        return thread

    events = [
        _loss_event(900, owner_epoch=16),
        _loss_event(1, owner_epoch=18),
        _loss_event(2, owner_epoch=18),
        _loss_event(999, owner_epoch=17),
    ]
    real_thread = threading.Thread
    with _mock_terminal_thread(thread_factory) as constructor:
        producers = [
            real_thread(target=app._enqueue_capture_terminal_event, args=(event,))
            for event in events
        ]
        for producer in producers:
            producer.start()
        for producer in producers:
            producer.join(2.0)
            assert not producer.is_alive()
        # Deterministically reproduce the unrelated Timer construction that
        # used to corrupt the process-wide Thread mock during the full suite.
        timer = threading.Timer(0, lambda: None)
        assert isinstance(timer, real_thread)
        assert threading.Thread is real_thread

    constructor.assert_called_once()
    assert len(deferred) == 1
    deferred[0].start.assert_called_once_with()
    deferred[0].target()

    assert handled == [_loss_event(2, owner_epoch=18)]
    assert app._capture_terminal_pending is None
    assert app._capture_terminal_scheduled is False


def test_terminal_drain_processes_event_queued_during_settlement() -> None:
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._shutting_down = False
    _initialize_terminal_scheduler(app)
    entered = threading.Event()
    release = threading.Event()
    handled: list[CaptureTerminalEvent] = []

    def settle(event: CaptureTerminalEvent) -> None:
        handled.append(event)
        if len(handled) == 1:
            entered.set()
            assert release.wait(2.0)

    app._settle_capture_terminal_event = settle
    drain_threads: list[threading.Thread] = []
    real_thread = threading.Thread

    def thread_factory(**kwargs):
        thread = real_thread(**kwargs)
        drain_threads.append(thread)
        return thread

    first = _loss_event(1, owner_epoch=20)
    second = _loss_event(2, owner_epoch=20)
    with _mock_terminal_thread(thread_factory):
        app._enqueue_capture_terminal_event(first)
        assert entered.wait(2.0)
        app._enqueue_capture_terminal_event(second)
        assert len(drain_threads) == 1
        release.set()
        drain_threads[0].join(2.0)

    assert handled == [first, second]
    assert app._capture_terminal_scheduled is False


def test_terminal_callback_after_shutdown_is_inert() -> None:
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._shutting_down = True
    app._settle_capture_terminal_event = MagicMock()

    app._enqueue_capture_terminal_event(_loss_event())

    app._settle_capture_terminal_event.assert_not_called()


def test_terminal_event_queued_before_shutdown_drains_as_noop() -> None:
    app, capture, event, *_ = _make_app()
    deferred: list[SimpleNamespace] = []

    def thread_factory(*, target, daemon, name):
        thread = SimpleNamespace(target=target, start=MagicMock())
        deferred.append(thread)
        return thread

    with _mock_terminal_thread(thread_factory):
        app._enqueue_capture_terminal_event(event)
    app._shutting_down = True
    deferred[0].target()

    assert capture.cancel_calls == 0
    app._dispatch_error_dialog.assert_not_called()
    assert app._capture_terminal_pending is None
    assert app._capture_terminal_scheduled is False


def test_retired_adapter_events_cannot_claim_reloaded_adapter_session() -> None:
    app, capture, event, *_ = _make_app()
    old_exact = CaptureTerminalEvent(
        event.request_id,
        event.generation,
        CaptureFailure.DEVICE_LOST,
        event.owner_epoch - 1,
    )
    old_higher = CaptureTerminalEvent(
        event.request_id,
        event.generation + 100,
        CaptureFailure.DEVICE_LOST,
        event.owner_epoch - 1,
    )

    app._settle_capture_terminal_event(old_exact)
    app._settle_capture_terminal_event(old_higher)
    assert capture.cancel_calls == 0
    app._settle_capture_terminal_event(event)
    assert capture.cancel_calls == 1


@pytest.mark.parametrize(
    "failure",
    [CaptureFailure.CLEANUP_FAILED, CaptureFailure.TIMEOUT],
)
def test_device_loss_cleanup_failure_uses_bounded_owner_retry(
    failure: CaptureFailure,
) -> None:
    app, capture, event, events, *_ = _make_app(capture_failure=failure)

    app._settle_capture_terminal_event(event)

    assert capture.cancel_calls == 1
    assert capture.close_calls == 1
    assert events.index("capture-cancel-close") < events.index(
        "capture-close-fallback"
    )
    app.text_injector.inject.assert_not_called()
    app._dispatch_error_dialog.assert_called_once()


def test_device_loss_releases_routed_compose_and_retires_server_route() -> None:
    app, capture, event, *_ = _make_app()
    leases = AgentComposeLeaseStore()
    acquired = leases.acquire("opaque-session", ttl_seconds=30)
    lease = leases.claim(acquired.generation, recording_id=11)
    assert lease is not None
    app._agent_compose_leases = leases
    app._agent_compose_recording = lease

    server = object.__new__(WhisperstreamServer)
    server._managed_sessions = None
    server._reply_desk = ReplyDesk()
    server._reply_desk.begin_capture(reply_target("opaque-session"), lease.generation, "toggle")
    server._agent_compose_leases = leases
    server._agent_question_coordinator = None
    server._agent_response_coordinator = MagicMock()
    server._claude_question_coordinator = None
    server._claude_response_coordinator = MagicMock()
    server._app = SimpleNamespace(
        config=SimpleNamespace(claude_reply_auto_submit=True)
    )
    broadcasts: list[dict] = []

    def broadcast(message: dict) -> None:
        broadcasts.append(message)
        WhisperstreamServer._observe_routed_compose_event(server, message)

    app._broadcast = broadcast
    app._settle_capture_terminal_event(event)

    assert capture.cancel_calls == 1
    assert leases.current() is None
    assert app._agent_compose_recording is None
    assert server._reply_desk.capturing() is None
    server._agent_response_coordinator.set_capture_active.assert_called_with(False)
    server._claude_response_coordinator.resolve_reply.assert_not_called()
    assert any(
        message.get("type") == "agent.compose.state"
        and message.get("state") == "cancelled"
        and message.get("reason") == "device_lost"
        for message in broadcasts
    )
    app.text_injector.inject.assert_not_called()


@pytest.mark.parametrize(
    "store_state",
    ["missing", "already-released", "release-error"],
)
def test_device_loss_clears_compose_recording_when_store_cannot_release(
    store_state: str,
) -> None:
    app, _capture, event, *_ = _make_app()
    leases = AgentComposeLeaseStore()
    acquired = leases.acquire("opaque-session", ttl_seconds=30)
    lease = leases.claim(acquired.generation, recording_id=11)
    assert lease is not None
    app._agent_compose_recording = lease
    if store_state == "missing":
        app._agent_compose_leases = None
    elif store_state == "already-released":
        assert leases.release(generation=lease.generation) == lease
        app._agent_compose_leases = leases
    else:
        app._agent_compose_leases = MagicMock()
        app._agent_compose_leases.release.side_effect = RuntimeError(
            "synthetic release failure"
        )

    app._settle_capture_terminal_event(event)

    assert app._agent_compose_recording is None
    assert not any(
        call.args
        and isinstance(call.args[0], dict)
        and call.args[0].get("type") == "agent.compose.state"
        for call in app._broadcast.call_args_list
    )


def test_cleanup_waits_for_settlement_owner_before_closing_capture() -> None:
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._lock = threading.Lock()
    _initialize_terminal_scheduler(app)
    app._file_only_stop = None
    app.stt_engine = None
    app.audio_capture = MagicMock()
    app.audio_capture.close.return_value = True
    app._recording = False
    app._shutting_down = False
    entered = threading.Event()
    release = threading.Event()
    cleanup_done = threading.Event()
    events: list[str] = []

    def settlement(_event: CaptureTerminalEvent) -> None:
        with app._lock:
            events.append("settlement-enter")
            entered.set()
            assert release.wait(2.0)
            events.append("settlement-exit")

    app.audio_capture.close.side_effect = lambda: events.append(
        "capture-close"
    ) or True
    app._settle_capture_terminal_event = settlement
    app._enqueue_capture_terminal_event(_loss_event())
    assert entered.wait(2.0)

    cleanup_thread = threading.Thread(
        target=lambda: (app.cleanup(), cleanup_done.set())
    )
    cleanup_thread.start()
    assert cleanup_done.wait(0.02) is False
    app.audio_capture.close.assert_not_called()
    release.set()
    cleanup_thread.join(2.0)

    assert cleanup_done.is_set()
    assert events.index("settlement-exit") < events.index("capture-close")


def test_cleanup_retries_once_then_reports_persistent_capture_ownership() -> None:
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._lock = threading.Lock()
    app._file_only_stop = None
    app.stt_engine = None
    app.audio_capture = MagicMock()
    app.audio_capture.close.return_value = False
    app._recording = False

    app.cleanup()

    assert app.audio_capture.close.call_count == 2
    assert any(
        call.args
        and "persistent_incomplete" in call.args[0]
        for call in app.logger.warning.call_args_list
    )


def test_terminal_event_rejects_invalid_generation_and_failure() -> None:
    with pytest.raises(ValueError, match="generation"):
        CaptureTerminalEvent("capture", 0, CaptureFailure.DEVICE_LOST, 1)
    with pytest.raises(ValueError, match="failure"):
        CaptureTerminalEvent("capture", 1, "device-lost", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="owner_epoch"):
        CaptureTerminalEvent("capture", 1, CaptureFailure.DEVICE_LOST, 0)
