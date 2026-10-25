"""Scoped, generation-correlated HUD reply dictation leases."""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from whisperstream.agent_compose import AgentComposeLeaseStore
from whisperstream.live_preview import PreviewUpdate
from whisperstream.main import WhisperstreamApp, _DictationFinalizationContext

pytestmark = pytest.mark.usefixtures("accepted_eula")


def test_lease_claim_is_session_and_recording_generation_correlated() -> None:
    now = [100.0]
    leases = AgentComposeLeaseStore(clock=lambda: now[0])

    pending = leases.acquire(
        "opaque-session",
        ttl_seconds=30,
    )
    claimed = leases.claim(pending.generation, recording_id=7)

    assert claimed is not None
    assert claimed.session_handle == "opaque-session"
    assert claimed.generation == pending.generation
    assert claimed.recording_id == 7
    assert leases.is_current(pending.generation, recording_id=7)
    assert not leases.is_current(pending.generation, recording_id=8)


def test_stale_generation_cannot_claim_or_release_newer_lease() -> None:
    leases = AgentComposeLeaseStore()
    first = leases.acquire("one", ttl_seconds=30)
    assert leases.release(generation=first.generation) == first
    second = leases.acquire("two", ttl_seconds=30)

    assert leases.claim(first.generation, recording_id=1) is None
    assert leases.release(generation=first.generation) is None
    assert leases.current() == second


def test_expired_lease_is_removed_on_read() -> None:
    now = [100.0]
    leases = AgentComposeLeaseStore(clock=lambda: now[0])
    leases.acquire("opaque-session", ttl_seconds=5)

    now[0] = 106.0
    assert leases.current() is None


def _compose_app(leases: AgentComposeLeaseStore) -> WhisperstreamApp:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app._agent_compose_leases = leases
    app._agent_compose_recording = None
    app._broadcast = MagicMock()
    app.text_injector = MagicMock()
    app.text_injector.process.side_effect = lambda value: f"processed {value}"
    app.review_state = MagicMock()
    app.license_manager = MagicMock()
    app.transcript_store = None
    app._experimental = True
    app.config = SimpleNamespace(
        enhancement_enabled=False,
        enhancement_chunked_cleanup=True,
        default_profile_id="",
        profiles=[],
        power_mode_map={},
        site_map={},
        input_style="clipboard",
        input_style_user_set=True,
        history_enabled=False,
    )
    app._dictation_app = None
    app._manual_profile_id = None
    app._override_anchor = None
    app._inject_suppressed = False
    return app


def _context(recording_id: int, lease) -> _DictationFinalizationContext:
    return _DictationFinalizationContext(
        recording_id=recording_id,
        waveform=None,
        chunk_futures=(),
        transcription_started_at=0.0,
        recording_started_at=0.0,
        stt_manager=None,
        dictation_app=None,
        manual_profile_id=None,
        override_anchor=None,
        screen_reader_detected=False,
        inject_suppressed=False,
        agent_compose_lease=lease,
        persistence_ready=threading.Event(),
    )


def test_compose_final_populates_draft_without_focused_injection() -> None:
    leases = AgentComposeLeaseStore()
    lease = leases.acquire("opaque-session", ttl_seconds=30)
    lease = leases.claim(lease.generation, recording_id=9)
    app = _compose_app(leases)
    app._persist_transcript = MagicMock()

    app._enhance_then_inject("reply words", 1.25, context=_context(9, lease))

    app.text_injector.inject.assert_not_called()
    app.text_injector.remember_original.assert_not_called()
    compose_final = next(
        call.args[0]
        for call in app._broadcast.call_args_list
        if call.args[0].get("type") == "agent.compose.final"
    )
    assert compose_final == {
        "type": "agent.compose.final",
        "version": 1,
        "session_handle": "opaque-session",
        "generation": lease.generation,
        "recording_id": 9,
        "text": "processed reply words",
    }
    assert not any(
        call.args[0].get("type") == "transcription.result"
        for call in app._broadcast.call_args_list
    )
    app._persist_transcript.assert_called_once()
    assert (
        app._persist_transcript.call_args.kwargs["inject_suppressed"] is False
    )


def test_stale_compose_generation_never_updates_draft_or_falls_back_to_injection() -> None:
    leases = AgentComposeLeaseStore()
    lease = leases.acquire("opaque-session", ttl_seconds=30)
    lease = leases.claim(lease.generation, recording_id=10)
    app = _compose_app(leases)
    leases.release(generation=lease.generation)

    app._enhance_then_inject("stale reply", 1.0, context=_context(10, lease))

    app.text_injector.inject.assert_not_called()
    assert not any(
        call.args[0].get("type") == "agent.compose.final"
        for call in app._broadcast.call_args_list
    )


def test_stale_generation_cannot_publish_compose_preview() -> None:
    leases = AgentComposeLeaseStore()
    lease = leases.acquire("opaque-session", ttl_seconds=30)
    lease = leases.claim(lease.generation, recording_id=11)
    app = _compose_app(leases)
    app._agent_compose_recording = lease
    app._live_preview_lock = threading.Lock()
    app._live_preview_coordinator = SimpleNamespace(recording_id=11)
    app.dictation_state_snapshot = MagicMock(
        return_value={
            "recording_id": 11,
            "phase": "recording",
            "preview_enabled": False,
        }
    )

    app._publish_live_preview(
        11, 1, PreviewUpdate(text="current words", stable_char_count=7)
    )
    leases.release(generation=lease.generation)
    app._publish_live_preview(
        11, 2, PreviewUpdate(text="stale words", stable_char_count=5)
    )

    compose_previews = [
        call.args[0]
        for call in app._broadcast.call_args_list
        if call.args[0].get("type") == "agent.compose.preview"
    ]
    assert len(compose_previews) == 1
    assert compose_previews[0]["text"] == "current words"
    assert not any(
        call.args[0].get("type") == "transcription.preview"
        for call in app._broadcast.call_args_list
    )


def test_ordinary_dictation_is_not_swallowed_by_unclaimed_lease() -> None:
    leases = AgentComposeLeaseStore()
    leases.acquire("opaque-session", ttl_seconds=30)
    app = _compose_app(leases)

    app._enhance_then_inject("ordinary words", 1.0, context=_context(12, None))

    app.text_injector.inject.assert_called_once()


def test_finalizer_exception_releases_matching_compose_generation() -> None:
    leases = AgentComposeLeaseStore()
    lease = leases.acquire("opaque-session", ttl_seconds=30)
    lease = leases.claim(lease.generation, recording_id=13)
    app = _compose_app(leases)
    app._finalize_transcription_and_inject = MagicMock(
        side_effect=RuntimeError("boom")
    )
    app._transition_dictation_state = MagicMock()

    try:
        app._finalize_dictation(_context(13, lease))
    except RuntimeError:
        pass

    assert leases.current() is None


@pytest.mark.parametrize(
    "store_state",
    ["missing", "already-released", "release-error"],
)
def test_finalizer_clears_compose_recording_when_store_cannot_release(
    store_state: str,
) -> None:
    leases = AgentComposeLeaseStore()
    acquired = leases.acquire("opaque-session", ttl_seconds=30)
    lease = leases.claim(acquired.generation, recording_id=14)
    assert lease is not None
    app = _compose_app(leases)
    app._agent_compose_recording = lease
    app._finalize_transcription_and_inject = MagicMock()
    app._transition_dictation_state = MagicMock()
    if store_state == "missing":
        app._agent_compose_leases = None
    elif store_state == "already-released":
        assert leases.release(generation=lease.generation) == lease
    else:
        app._agent_compose_leases = MagicMock()
        app._agent_compose_leases.release.side_effect = RuntimeError(
            "synthetic release failure"
        )

    app._finalize_dictation(_context(14, lease))

    assert app._agent_compose_recording is None
    assert not any(
        call.args[0].get("type") == "agent.compose.state"
        for call in app._broadcast.call_args_list
    )


def test_existing_hotkey_modes_route_before_ordinary_dictation() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app._lock = threading.RLock()
    app._recording = False
    app._idle_timer = None
    app._agent_reply_router = MagicMock()
    app._agent_reply_router.start_routed_reply.return_value = True
    app._agent_reply_router.stop_routed_reply.return_value = True
    app.license_manager = MagicMock()
    app.license_manager.can_start_recording.return_value = True
    app._try_begin_recording = MagicMock()

    app._on_hotkey_press()
    app._on_hotkey_release()
    app._on_toggle_start()
    app._on_toggle_stop()

    assert app._agent_reply_router.start_routed_reply.call_args_list == [
        call("push_to_talk"),
        call("toggle"),
    ]
    assert app._agent_reply_router.stop_routed_reply.call_args_list == [
        call("push_to_talk"),
        call("toggle"),
    ]
    app._try_begin_recording.assert_not_called()


def test_reply_router_exception_falls_through_to_ordinary_dictation() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app._lock = threading.RLock()
    app._recording = False
    app._require_acceptance = MagicMock(return_value=True)
    app._agent_reply_router = MagicMock()
    app._agent_reply_router.start_routed_reply.side_effect = RuntimeError("race")
    app._start_recording_for_origin = MagicMock(return_value="started")

    app._on_hotkey_press()

    app._start_recording_for_origin.assert_called_once_with("push_to_talk")
    app.logger.warning.assert_called_once_with(
        "[ClaudeHUD] reply route failed mode=%s reason=%s action=fallthrough",
        "push_to_talk",
        "RuntimeError",
    )


def test_unclaimed_reply_lease_cancels_capture_before_it_can_become_dictation() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.config = SimpleNamespace(mac_history_opt_in=False)
    app.logger = MagicMock(spec=logging.Logger)
    app._lock = threading.RLock()
    app._recording = False
    app._agent_compose_leases = MagicMock()
    app._agent_compose_leases.is_current.return_value = True
    app._agent_compose_pending_generation = None
    app._agent_compose_recording = None

    def start_without_claiming_lease() -> bool:
        app._recording = True
        return True

    app._try_begin_recording = MagicMock(side_effect=start_without_claiming_lease)
    app._cancel_recording = MagicMock(
        side_effect=lambda **_kwargs: setattr(app, "_recording", False)
    )

    assert app.start_agent_compose(7) is False

    app._cancel_recording.assert_called_once_with(trigger="agent_reply")
    assert app._agent_compose_pending_generation is None
    app.logger.warning.assert_called_once_with(
        "[ClaudeHUD] reply capture aborted reason=lease_not_claimed"
    )


def test_reply_router_association_defers_fixed_escape_and_enter_handlers() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.keyboard = MagicMock()
    router = MagicMock()

    app.set_agent_reply_router(router)

    assert app._agent_reply_router is router
    app.keyboard.register_early_key_handler.assert_not_called()

    app._register_agent_reply_early_handlers()

    assert app.keyboard.register_early_key_handler.call_args_list == [
        call(
            "escape",
            app._should_consume_escape,
            app._on_escape_press,
        ),
        call(
            "enter",
            router.should_consume_response_enter,
            app._on_agent_enter_press,
        ),
    ]
    app.keyboard.register_extra_hotkey.assert_not_called()


def test_combined_escape_prioritizes_response_route_over_active_recording() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app._lock = threading.RLock()
    app._recording = True
    app._recording_origin = "toggle"
    app._agent_compose_recording = None
    app._agent_reply_router = MagicMock()
    app._agent_reply_router.should_consume_response_escape.return_value = True
    app._cancel_recording = MagicMock()
    app.keyboard = MagicMock()

    assert app._should_consume_escape() is True
    app._on_escape_press()

    app._agent_reply_router.dismiss_response_routes.assert_called_once_with()
    app._cancel_recording.assert_not_called()
    app.keyboard.reset_toggle_state.assert_not_called()


def test_combined_escape_falls_through_without_route_or_recording() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app._lock = threading.RLock()
    app._recording = False
    app._recording_origin = None
    app._agent_compose_recording = None
    app._agent_reply_router = MagicMock()
    app._agent_reply_router.should_consume_response_escape.return_value = False
    app._cancel_recording = MagicMock()
    app.keyboard = MagicMock()

    assert app._should_consume_escape() is False
    app._on_escape_press()

    app._agent_reply_router.dismiss_response_routes.assert_not_called()
    app._cancel_recording.assert_not_called()
    app.keyboard.reset_toggle_state.assert_not_called()


def test_combined_escape_does_not_claim_unrouted_compose_recording() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app._lock = threading.RLock()
    app._recording = True
    app._recording_origin = "toggle"
    app._agent_compose_recording = object()
    app._agent_reply_router = MagicMock()
    app._agent_reply_router.should_consume_response_escape.return_value = False
    app._cancel_recording = MagicMock()
    app.keyboard = MagicMock()

    assert app._should_consume_escape() is False
    app._on_escape_press()

    app._agent_reply_router.dismiss_response_routes.assert_not_called()
    app._cancel_recording.assert_not_called()
    app.keyboard.reset_toggle_state.assert_not_called()


def test_combined_escape_does_not_claim_retained_hud_recording() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app._lock = threading.RLock()
    app._recording = True
    app._recording_origin = "hud"
    app._agent_compose_recording = None
    app._agent_reply_router = None
    app._cancel_recording = MagicMock()
    app.keyboard = MagicMock()

    assert app._should_consume_escape() is False
    app._on_escape_press()

    app._cancel_recording.assert_not_called()
    app.keyboard.reset_toggle_state.assert_not_called()


def test_no_live_reply_route_preserves_ordinary_hotkey_behavior() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.config = SimpleNamespace(mac_history_opt_in=False)
    app._lock = threading.RLock()
    app._recording = False
    app._idle_timer = None
    app._agent_reply_router = MagicMock()
    app._agent_reply_router.start_routed_reply.return_value = False
    app.license_manager = MagicMock()
    app.license_manager.can_start_recording.return_value = True
    app._try_begin_recording = MagicMock()

    app._on_hotkey_press()

    app._agent_reply_router.start_routed_reply.assert_called_once_with(
        "push_to_talk"
    )
    app._try_begin_recording.assert_called_once_with()


def test_dictation_state_broadcast_keeps_the_reply_marker_on_the_wire() -> None:
    """The HUD must learn a recording is a reply from its first lifecycle event.

    The server normalizes every ``dictation.state`` before publishing and
    retaining it; the reply marker has to survive that step, or the HUD shows
    the ordinary dictation pill for a frame on every push-to-talk reply.
    """
    import logging as _logging
    from unittest.mock import MagicMock as _MagicMock

    from whisperstream.server.app import WhisperstreamServer
    from whisperstream.server.websocket import DictationStateCache

    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._dictation_state = DictationStateCache()
    server._socket_delivery = _MagicMock()
    server._loop = None
    server._logger = _logging.getLogger("test.compose-wire")
    server._acceptance_is_current = lambda _grant=None: True
    server._current_acceptance_grant = lambda: None
    event = {
        "type": "dictation.state",
        "version": 1,
        "recording_id": 3,
        "phase": "recording",
        "origin": "push_to_talk",
        "hud_enabled": True,
        "preview_enabled": False,
        "screen_reader_detected": False,
        "compose_generation": 7,
    }

    server.broadcast(dict(event))

    published = server._socket_delivery.publish.call_args.args[0]
    assert published["compose_generation"] == 7
    assert server._dictation_state.snapshot()["compose_generation"] == 7

    # Malformed markers never reach the HUD; ordinary dictation carries none.
    for bad in (True, 0, -1, "7"):
        assert "compose_generation" not in DictationStateCache.normalize(
            {**event, "compose_generation": bad}
        )
    plain = {k: v for k, v in event.items() if k != "compose_generation"}
    assert "compose_generation" not in DictationStateCache.normalize(plain)
