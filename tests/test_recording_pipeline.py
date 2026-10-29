"""Integration tests for the recording pipeline: timer behavior and chunking finalization.

Constructs a real WhisperstreamApp with all hardware/model touchpoints mocked
(sounddevice, ParakeetManager, KeyboardShortcutManager, AudioFeedback,
LicenseManager, TextInjector). Validates:

- PTT mode never starts a safety timer.
- Toggle mode starts a safety timer (and respects 0 = disabled).
- _on_chunk_ready submits work to the executor.
- _finalize_transcription_and_inject merges chunked results via overlap dedup.
- _finalize_transcription_and_inject falls back to single-shot when no chunks exist.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import sys
import threading
import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, call, patch

import numpy as np
import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.audio_capture import PreviewAudioSnapshot
from whisperstream.audio_policy_contract import AudioPolicyFailure, AudioPolicyMode
from whisperstream.capture_contract import (
    CaptureSelectionResolution,
    CaptureSession,
)
from whisperstream.app_context import UnavailableApplicationContextAdapter
from whisperstream.app_context_contract import (
    ApplicationRef,
    BrowserPageRef,
    ContextRequirement,
    ContextSnapshot,
    ContextStatus,
)
from whisperstream.foreground_window import RoutingTarget
from whisperstream.live_preview import PreviewUpdate
from whisperstream.llm_enhancer import EnhancementResult
from whisperstream.onnx_stt_manager import DetailedTranscriptionResult
from whisperstream.text_injector import DeliveryOutcome


_requires_windows_volume = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Core Audio endpoint volume diagnostics are Windows-only",
)
pytestmark = pytest.mark.usefixtures("accepted_eula")


@pytest.fixture
def _make_app(tmp_path):
    """Factory fixture: returns a function that builds a mocked WhisperstreamApp.

    Accepts keyword overrides for WhisperstreamConfig fields.
    """

    def _factory(**config_overrides):
        base_config = WhisperstreamConfig(**config_overrides)
        with (
            # This suite exercises parent-side recording/finalization behavior,
            # so keep the platform factory and every native worker out of it.
            patch("whisperstream.main.make_stt_manager"),
            patch("whisperstream.main.KeyboardShortcutManager") as _mock_keyboard,
            patch("whisperstream.main.AudioCapture") as _mock_audio_capture,
            patch("whisperstream.main.AudioFeedback") as _mock_audio_feedback,
            patch("whisperstream.main.LicenseManager") as mock_license_cls,
            patch("whisperstream.main.TextInjector") as _mock_text_injector,
            patch(
                "whisperstream.main.create_application_context_adapter",
                return_value=UnavailableApplicationContextAdapter(),
            ),
            patch("whisperstream.speech.controller.SpeechController"),
            patch(
                "whisperstream.main.model_dir",
                return_value=tmp_path,
            ),
            patch(
                "whisperstream.main.ConfigManager.load",
                return_value=base_config,
            ),
        ):
            # Make license manager allow recording by default.
            mock_license_cls.return_value.can_start_recording.return_value = True
            mock_license_cls.return_value.trial_expired = False
            mock_license_cls.return_value.status.return_value = {}

            # AudioCapture.start should not raise.
            _mock_audio_capture.return_value.start.return_value = None
            _mock_audio_feedback.return_value.play_start_blocking.side_effect = (
                lambda _path, *, on_complete=None: (
                    on_complete() if on_complete is not None else None
                )
            )
            # AudioCapture.stop returns a small waveform.
            _mock_audio_capture.return_value.stop.return_value = np.zeros(
                16000, dtype=np.float32
            )

            from whisperstream.main import WhisperstreamApp

            # These tests install synthetic stores below. Do not launch the
            # Mac background Keychain initializer over those test-owned stores.
            with patch.object(WhisperstreamApp, "initialize_accepted_content_services"):
                app = WhisperstreamApp(headless=True)
            app._transcript_store_initialized = True
            # Keep the general pipeline suite hermetic. The frozen contract
            # remains in the path, but no production endpoint is resolved.
            from whisperstream.audio_policy import (
                UnavailableRecordingAudioPolicyAdapter,
            )

            app.audio_policy = UnavailableRecordingAudioPolicyAdapter(
                feedback=app.audio_feedback,
                start_path=lambda: app.config.start_sound_path,
                stop_path=lambda: app.config.stop_sound_path,
            )
            app._observe_playback_volume = MagicMock(return_value=None)
            return app

    return _factory


def _start_retained_hud_recording(app) -> str:
    """Seed a HUD-origin session directly for outcome-path tests."""
    with app._lock:
        return app._start_recording_for_origin("hud")


def test_app_registers_combined_escape_handler_during_initialization(
    _make_app,
) -> None:
    app = _make_app()

    app.keyboard.register_early_key_handler.assert_any_call(
        "escape",
        app._should_consume_escape,
        app._on_escape_press,
    )


# ── Test 1: PTT mode does not start timer ──────────────────────────────


def test_ptt_mode_does_not_start_timer(_make_app) -> None:
    app = _make_app(recording_mode="push_to_talk", toggle_safety_timeout=1800)
    # The safety timer is gated on the runtime _started_via_toggle flag, which
    # _on_hotkey_press sets to False for a PTT activation.
    app._started_via_toggle = False
    app._start_recording()
    assert app._stop_timer is None, "PTT mode must never start a safety timer"


def test_startup_arming_event_wraps_microphone_open(_make_app) -> None:
    app = _make_app(recording_mode="push_to_talk")
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)

    def capture_start(*, discard_until_armed: bool) -> None:
        assert discard_until_armed is True
        assert broadcasts[-1] == {
            "type": "state.arming",
            "active": True,
            "hud_enabled": True,
            "screen_reader_detected": False,
            "agent_compose": False,
        }

    app.audio_capture.start.side_effect = capture_start
    with patch("whisperstream.main.screen_reader_active", return_value=False):
        assert app._start_recording() is True

    event_types = [event["type"] for event in broadcasts]
    recording_index = event_types.index("state.recording")
    arming_indices = [
        index for index, event in enumerate(broadcasts)
        if event["type"] == "state.arming"
    ]
    assert arming_indices[0] < arming_indices[1] < recording_index
    assert broadcasts[arming_indices[1]] == {
        "type": "state.arming",
        "active": False,
    }


def test_reply_capture_arming_is_tagged_for_the_agent_surface(_make_app) -> None:
    app = _make_app(recording_mode="push_to_talk")
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    app._agent_compose_pending_generation = 7

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        assert app._start_recording() is True

    arming = next(event for event in broadcasts if event["type"] == "state.arming")
    assert arming["agent_compose"] is True


# ── Test 2: Toggle mode starts safety timer ────────────────────────────


def test_toggle_mode_starts_safety_timer(_make_app) -> None:
    app = _make_app(recording_mode="toggle", toggle_safety_timeout=1800)
    # A toggle activation goes through _on_toggle_start, which sets this flag
    # before recording starts; that flag (not config.recording_mode) is what
    # arms the safety timer in _start_recording.
    app._started_via_toggle = True
    app._start_recording()
    try:
        assert app._stop_timer is not None, "Toggle mode must start a safety timer"
        assert app._stop_timer.interval == 1800
    finally:
        # Clean up the timer to avoid dangling threads.
        if app._stop_timer is not None:
            app._stop_timer.cancel()


# ── Test 3: Toggle mode with zero timeout does not start timer ─────────


def test_toggle_mode_zero_safety_timeout_no_timer(_make_app) -> None:
    app = _make_app(recording_mode="toggle", toggle_safety_timeout=0)
    # Even on a real toggle activation, a zero timeout must disable the timer.
    app._started_via_toggle = True
    app._start_recording()
    assert app._stop_timer is None, "toggle_safety_timeout=0 must disable the timer"


def _cancel_started_recording(app) -> None:
    if app._recording:
        with app._lock:
            app._cancel_recording(trigger="test")


def _context_snapshot(
    generation: int,
    process_name: str,
    domain: str | None = None,
) -> ContextSnapshot:
    application = ApplicationRef(process_name, f"opaque-{generation}")
    requirement = (
        ContextRequirement.APPLICATION_AND_BROWSER_PAGE
        if domain
        else ContextRequirement.APPLICATION
    )
    return ContextSnapshot(
        generation,
        requirement,
        ContextStatus.RESOLVED,
        application,
        BrowserPageRef(application, domain) if domain else None,
    )


def test_recording_without_routing_need_skips_foreground_probe(_make_app) -> None:
    app = _make_app(power_mode_map={}, site_map={})
    app.application_context = MagicMock()
    try:
        with (
            patch(
                "whisperstream.main.detect_foreground_routing_target"
            ) as probe,
            patch("whisperstream.main.screen_reader_active", return_value=False),
        ):
            assert app._start_recording() is True
        probe.assert_not_called()
        app.application_context.capture.assert_not_called()
        assert app._dictation_app is None
    finally:
        _cancel_started_recording(app)


def test_app_routing_reads_process_without_browser_uia(_make_app) -> None:
    app = _make_app(
        profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
        power_mode_map={"code.exe": "code"},
        site_map={},
    )
    target = RoutingTarget("code.exe")
    context = _context_snapshot(1, "code.exe")
    app.application_context = MagicMock()
    app.application_context.capture.return_value = context
    try:
        with (
            patch(
                "whisperstream.main.detect_foreground_routing_target",
                return_value=target,
            ) as probe,
            patch("whisperstream.main.screen_reader_active", return_value=False),
        ):
            assert app._start_recording() is True
        probe.assert_not_called()
        app.application_context.capture.assert_called_once_with(
            1, ContextRequirement.APPLICATION
        )
        assert app._dictation_app == context
    finally:
        _cancel_started_recording(app)


def test_site_routing_explicitly_enables_browser_uia(_make_app) -> None:
    app = _make_app(
        profiles=[{"id": "site", "name": "Site", "prompt": "p"}],
        power_mode_map={},
        site_map={"example.com": "site"},
    )
    target = RoutingTarget("chrome.exe", "example.com")
    context = _context_snapshot(1, "chrome.exe", "example.com")
    app.application_context = MagicMock()
    app.application_context.capture.return_value = context
    try:
        with (
            patch(
                "whisperstream.main.detect_foreground_routing_target",
                return_value=target,
            ) as probe,
            patch("whisperstream.main.screen_reader_active", return_value=False),
        ):
            assert app._start_recording() is True
        probe.assert_not_called()
        app.application_context.capture.assert_called_once_with(
            1, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
        )
        assert app._dictation_app == context
    finally:
        _cancel_started_recording(app)


def test_live_manual_override_reads_process_without_browser_uia(_make_app) -> None:
    app = _make_app(
        profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
        power_mode_map={},
        site_map={},
    )
    app._manual_profile_id = "code"
    app._override_anchor = "code.exe"
    app.application_context = MagicMock()
    app.application_context.capture.return_value = _context_snapshot(1, "code.exe")
    try:
        with (
            patch(
                "whisperstream.main.detect_foreground_routing_target",
                return_value=RoutingTarget("code.exe"),
            ) as probe,
            patch("whisperstream.main.screen_reader_active", return_value=False),
        ):
            assert app._start_recording() is True
        probe.assert_not_called()
        app.application_context.capture.assert_called_once_with(
            1, ContextRequirement.APPLICATION
        )
    finally:
        _cancel_started_recording(app)


# ── HUD-origin production lifecycle ────────────────────────────────────


@pytest.mark.parametrize("terminal_path", ["empty", "asr_failure"])
def test_direct_finalization_releases_live_routing_target(
    _make_app, terminal_path: str
) -> None:
    app = _make_app()
    app._dictation_app = RoutingTarget(
        "private-app.exe", "private.example"
    )
    if terminal_path == "empty":
        app._last_waveform = np.empty(0, dtype=np.float32)
    else:
        app._last_waveform = np.ones(16_000, dtype=np.float32)
        app.parakeet.transcribe.side_effect = RuntimeError("decode failed")

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._finalize_transcription_and_inject()

    assert app._dictation_app is None


def test_initial_idle_lifecycle_carries_startup_screen_reader_state(
    _make_app,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_FORCE_SCREEN_READER", "1")

    app = _make_app()

    state = app.dictation_state_snapshot()
    assert state["phase"] == "idle"
    assert state["screen_reader_detected"] is True


def test_hud_complete_retains_production_toggle_lifecycle(_make_app) -> None:
    app = _make_app(
        toggle_safety_timeout=1800,
        history_enabled=False,
        show_recording_control=True,
    )
    app.stt_engine.transcribe.return_value = "hud final dictation"
    app.text_injector.process.return_value = "hud final dictation"
    app.review_state = MagicMock()
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)

    try:
        with (
            patch("whisperstream.main.has_input_device", return_value=True),
            patch("whisperstream.main.screen_reader_active", return_value=False),
        ):
            assert _start_retained_hud_recording(app) == "started"

        recording_state = app.dictation_state_snapshot()
        assert recording_state["phase"] == "recording"
        assert recording_state["origin"] == "hud"
        assert app._recording is True
        assert app._started_via_toggle is True
        assert app._stop_timer is not None
        app.audio_feedback.play_start_blocking.assert_called_once_with(
            app.config.start_sound_path,
        )
        app.audio_capture.start.assert_called_once_with(discard_until_armed=True)
        app.audio_capture.arm_capture.assert_called_once_with()

        assert app.complete_hud_dictation() == "completed"
        assert app._recording_origin is None
        app._executor.submit(lambda: None).result(timeout=5)
    finally:
        if app._stop_timer is not None:
            app._stop_timer.cancel()
        if app._idle_timer is not None:
            app._idle_timer.cancel()

    lifecycle = [
        event for event in broadcasts if event.get("type") == "dictation.state"
    ]
    assert [event["phase"] for event in lifecycle] == [
        "recording",
        "processing",
        "idle",
    ]
    assert all(event["origin"] == "hud" for event in lifecycle)
    app.stt_engine.transcribe.assert_called_once()
    app.text_injector.inject.assert_called_once()
    app.license_manager.record_usage.assert_not_called()
    app.license_manager.mark_trial_started.assert_not_called()
    app.review_state.record_session.assert_called_once()
    app.audio_feedback.play_stop.assert_called_once_with(
        app.config.stop_sound_path
    )


def test_hud_cancel_discards_without_content_bearing_side_effects(
    _make_app,
) -> None:
    app = _make_app(
        toggle_safety_timeout=1800,
        show_live_preview=True,
        history_enabled=True,
        mac_history_opt_in=True,
        save_audio=True,
        show_recording_control=True,
    )
    app.review_state = MagicMock()
    app.transcript_store = MagicMock()
    broadcasts: list[dict] = []
    stats = MagicMock()

    def _broadcast(event: dict) -> None:
        broadcasts.append(event)
        if event.get("type") == "transcription.result":
            stats.record_transcription(
                event["text"],
                event["duration_ms"],
            )

    app.set_event_broadcaster(_broadcast)
    with (
        patch("whisperstream.main.has_input_device", return_value=True),
        patch("whisperstream.main.screen_reader_active", return_value=False),
    ):
        assert _start_retained_hud_recording(app) == "started"

    recording_id = app.dictation_state_snapshot()["recording_id"]
    pending_chunk = MagicMock()
    late_chunk = MagicMock()
    app._chunk_futures = [pending_chunk]
    app._last_waveform = np.ones(16_000, dtype=np.float32)
    app._dictation_app = RoutingTarget(
        "private-app.exe", "private.example"
    )
    app.license_manager.reset_mock()
    app.audio_feedback.play_stop.reset_mock()

    trace: list[str] = []
    invalidate_preview = app._invalidate_live_preview

    def _invalidate_preview() -> None:
        trace.append("preview_invalidated")
        invalidate_preview()

    app._invalidate_live_preview = MagicMock(side_effect=_invalidate_preview)
    app._restore_system_volume = MagicMock(
        side_effect=lambda: trace.append("volume_restored")
    )
    def _cancel_capture() -> None:
        trace.append("capture_stopped")
        app._chunk_futures.append(late_chunk)

    app.audio_capture.cancel.side_effect = _cancel_capture
    app.audio_feedback.play_stop.side_effect = (
        lambda _path: trace.append("stop_chime")
    )

    assert app.cancel_hud_dictation() == "cancelled"

    assert trace == [
        "preview_invalidated",
        "volume_restored",
        "capture_stopped",
        "stop_chime",
    ]
    assert app._recording is False
    assert app._stop_timer is None
    assert app._idle_timer is None
    assert app._chunk_futures == []
    assert app._recording_origin is None
    assert app._last_waveform is None
    assert app._dictation_app is None
    pending_chunk.cancel.assert_called_once_with()
    late_chunk.cancel.assert_called_once_with()
    app._invalidate_live_preview.assert_called_once_with()
    app.audio_capture.set_level_callback.assert_called_with(None)

    lifecycle = [
        event for event in broadcasts if event.get("type") == "dictation.state"
    ]
    assert lifecycle == [
        {
            "type": "dictation.state",
            "version": 1,
            "recording_id": recording_id,
            "phase": "recording",
            "origin": "hud",
            "hud_enabled": True,
            "preview_enabled": True,
            "screen_reader_detected": False,
        },
        {
            "type": "dictation.state",
            "version": 1,
            "recording_id": recording_id,
            "phase": "idle",
            "origin": "hud",
            "hud_enabled": True,
            "preview_enabled": True,
            "screen_reader_detected": False,
        },
    ]
    assert {"type": "state.recording", "recording": False} in broadcasts
    assert not any(
        event.get("type") in {"state.transcribing", "transcription.result"}
        for event in broadcasts
    )

    app.stt_engine.transcribe.assert_not_called()
    app.stt_engine.transcribe_chunk.assert_not_called()
    app.text_injector.process.assert_not_called()
    app.text_injector.inject.assert_not_called()
    app.text_injector.remember_original.assert_not_called()
    app.transcript_store.save.assert_not_called()
    app.review_state.record_session.assert_not_called()
    app.license_manager.record_usage.assert_not_called()
    app.license_manager.mark_trial_started.assert_not_called()
    app.license_manager.status.assert_not_called()
    stats.record_transcription.assert_not_called()


def test_escape_cancels_toggle_recording_as_true_discard_and_resets_toggle(
    _make_app,
) -> None:
    app = _make_app(
        toggle_safety_timeout=1800,
        history_enabled=True,
        mac_history_opt_in=True,
        save_audio=True,
    )
    app.review_state = MagicMock()
    app.transcript_store = MagicMock()
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)

    with (
        patch("whisperstream.main.has_input_device", return_value=True),
        patch("whisperstream.main.screen_reader_active", return_value=False),
    ):
        with app._lock:
            assert app._start_recording_for_origin("toggle") == "started"

    app.keyboard.reset_mock()
    app.parakeet.reset_mock()
    app.text_injector.reset_mock()
    app.transcript_store.reset_mock()
    app.review_state.reset_mock()
    app.license_manager.reset_mock()
    real_cancel = app._cancel_recording

    def _cancel_under_lock(**kwargs) -> None:
        assert app._lock.acquire(blocking=False) is False
        real_cancel(**kwargs)

    app._cancel_recording = MagicMock(side_effect=_cancel_under_lock)

    assert app._should_consume_escape() is True
    app._on_escape_press()

    app._cancel_recording.assert_called_once_with(trigger="escape")
    app.keyboard.reset_toggle_state.assert_called_once_with()
    assert app._recording is False
    assert app._recording_origin is None
    assert app._stop_timer is None
    app._on_hotkey_release()
    app.audio_capture.cancel.assert_called_once_with()
    assert not any(
        event.get("type") in {"state.transcribing", "transcription.result"}
        for event in broadcasts
    )
    app.parakeet.transcribe.assert_not_called()
    app.parakeet.transcribe_chunk.assert_not_called()
    app.text_injector.process.assert_not_called()
    app.text_injector.inject.assert_not_called()
    app.transcript_store.save.assert_not_called()
    app.review_state.record_session.assert_not_called()
    app.license_manager.record_usage.assert_not_called()
    app.license_manager.mark_trial_started.assert_not_called()


def test_push_to_talk_release_cannot_stop_hud_origin_recording(
    _make_app,
) -> None:
    app = _make_app(show_recording_control=True)
    with (
        patch("whisperstream.main.has_input_device", return_value=True),
        patch("whisperstream.main.screen_reader_active", return_value=False),
    ):
        assert _start_retained_hud_recording(app) == "started"

    try:
        app.audio_capture.stop.reset_mock()
        app._on_hotkey_release()
        app._on_toggle_stop()

        assert app._recording is True
        assert app.dictation_state_snapshot()["origin"] == "hud"
        assert app.dictation_state_snapshot()["phase"] == "recording"
        app.audio_capture.stop.assert_not_called()
    finally:
        app.cancel_hud_dictation()


def test_hud_outcome_commands_cannot_control_hotkey_origin_recording(
    _make_app,
) -> None:
    app = _make_app(show_recording_control=True)
    with (
        patch("whisperstream.main.has_input_device", return_value=True),
        patch("whisperstream.main.screen_reader_active", return_value=False),
    ):
        app._on_hotkey_press()

    assert app.dictation_state_snapshot()["origin"] == "push_to_talk"
    assert app.complete_hud_dictation() == "not_hud_recording"
    assert app.cancel_hud_dictation() == "not_hud_recording"
    assert app._recording is True
    app._recording = False
    app._invalidate_live_preview()


@pytest.mark.parametrize(
    ("indicator_enabled", "control_enabled"),
    [(False, False), (False, True), (True, False)],
)
def test_hud_start_requires_both_preferences_before_recording_side_effects(
    _make_app,
    indicator_enabled: bool,
    control_enabled: bool,
) -> None:
    app = _make_app(
        show_recording_indicator=indicator_enabled,
        show_recording_control=control_enabled,
    )
    app.license_manager.can_start_recording.return_value = False
    app.license_manager.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    idle_timer = MagicMock()
    app._idle_timer = idle_timer
    recording_id = app.dictation_state_snapshot()["recording_id"]

    assert app.start_hud_dictation() == "hud_control_disabled"

    assert app.dictation_state_snapshot()["phase"] == "idle"
    assert app.dictation_state_snapshot()["origin"] is None
    assert app.dictation_state_snapshot()["recording_id"] == recording_id
    assert app._recording is False
    assert app._started_via_toggle is False
    assert app.config.show_recording_indicator is indicator_enabled
    assert app.config.show_recording_control is control_enabled
    assert app._idle_timer is idle_timer
    idle_timer.cancel.assert_not_called()
    app.license_manager.can_start_recording.assert_not_called()
    app.license_manager.status.assert_not_called()
    app.audio_feedback.play_stop.assert_not_called()
    app.audio_feedback.play_start_blocking.assert_not_called()
    app.audio_capture.start.assert_not_called()
    assert broadcasts == []


def test_hud_start_is_available_when_both_production_preferences_are_enabled(
    _make_app,
) -> None:
    app = _make_app(
        show_recording_indicator=True,
        show_recording_control=True,
    )

    with (
        patch("whisperstream.main.has_input_device", return_value=True),
        patch("whisperstream.main.screen_reader_active", return_value=False),
    ):
        assert app.start_hud_dictation() == "started"

    try:
        assert app.dictation_state_snapshot()["origin"] == "hud"
        assert app.dictation_state_snapshot()["phase"] == "recording"
    finally:
        app.cancel_hud_dictation()


def test_stale_safety_timeout_cannot_stop_a_newer_hud_recording(
    _make_app,
) -> None:
    app = _make_app(
        show_recording_control=True,
        toggle_safety_timeout=1800,
    )
    with (
        patch("whisperstream.main.has_input_device", return_value=True),
        patch("whisperstream.main.screen_reader_active", return_value=False),
    ):
        assert _start_retained_hud_recording(app) == "started"
        old_timer = app._stop_timer
        old_recording_id = app.dictation_state_snapshot()["recording_id"]
        assert old_timer is not None

        assert app.cancel_hud_dictation() == "cancelled"
        assert _start_retained_hud_recording(app) == "started"
        new_recording_id = app.dictation_state_snapshot()["recording_id"]
        assert new_recording_id > old_recording_id

    try:
        app.audio_capture.stop.reset_mock()
        old_timer.function(*old_timer.args, **old_timer.kwargs)
        assert app._recording is True
        assert app.dictation_state_snapshot()["recording_id"] == new_recording_id
        app.audio_capture.stop.assert_not_called()
    finally:
        app.cancel_hud_dictation()


# ── Test 4: _on_chunk_ready submits to executor ────────────────────────


def _install_windows_audio_policy(app, endpoint_provider) -> None:
    from whisperstream.windows_audio_policy import WindowsRecordingAudioPolicyAdapter

    app.audio_policy = WindowsRecordingAudioPolicyAdapter(
        feedback=app.audio_feedback,
        logger=app.logger,
        start_path=lambda: app.config.start_sound_path,
        stop_path=lambda: app.config.stop_sound_path,
        mode=lambda: app.config.system_volume_mode,
        endpoint_provider=endpoint_provider,
    )


@_requires_windows_volume
def test_off_mode_observes_endpoint_changes_across_capture_lifecycle(
    _make_app,
) -> None:
    """Off mode remains read-only but logs all four endpoint snapshots."""
    app = _make_app(system_volume_mode="off")
    app._started_via_toggle = False
    app._executor = MagicMock()
    app.logger = MagicMock()

    a2dp_endpoint = MagicMock(name="a2dp_endpoint")
    a2dp_endpoint.GetMasterVolumeLevelScalar.return_value = 0.72
    a2dp_endpoint.GetMute.return_value = 0
    hfp_endpoint = MagicMock(name="hfp_endpoint")
    hfp_endpoint.GetMasterVolumeLevelScalar.return_value = 0.41
    hfp_endpoint.GetMute.return_value = 1

    endpoint_provider = MagicMock(
        side_effect=[
            ("endpoint-a2dp", a2dp_endpoint),
            ("endpoint-hfp", hfp_endpoint),
            ("endpoint-hfp", hfp_endpoint),
            ("endpoint-a2dp", a2dp_endpoint),
        ]
    )
    _install_windows_audio_policy(app, endpoint_provider)

    try:
        with patch("whisperstream.main.has_input_device", return_value=True):
            assert app._try_begin_recording() is True
            app._on_hotkey_release()
    finally:
        if app._idle_timer is not None:
            app._idle_timer.cancel()

    snapshot_args = [
        call.args
        for call in app.logger.info.call_args_list
        if call.args and call.args[0].startswith("playback snapshot phase=")
    ]
    assert [
        (args[1], args[2], args[3], args[4])
        for args in snapshot_args
    ] == [
        ("pre_capture", "off", 0.72, "false"),
        ("post_capture", "off", 0.41, "true"),
        ("pre_close", "off", 0.41, "true"),
        ("post_close", "off", 0.72, "false"),
    ]
    assert "endpoint-a2dp" not in str(snapshot_args)
    assert "endpoint-hfp" not in str(snapshot_args)
    assert endpoint_provider.call_count == 4
    a2dp_endpoint.SetMute.assert_not_called()
    a2dp_endpoint.SetMasterVolumeLevelScalar.assert_not_called()
    hfp_endpoint.SetMute.assert_not_called()
    hfp_endpoint.SetMasterVolumeLevelScalar.assert_not_called()


@_requires_windows_volume
def test_playback_observation_failure_never_aborts_recording(_make_app) -> None:
    """COM/default-endpoint probe failures are diagnostic-only in off mode."""
    app = _make_app(system_volume_mode="off")
    app._started_via_toggle = False
    app._executor = MagicMock()
    app.logger = MagicMock()
    endpoint_provider = MagicMock(
        side_effect=OSError("default endpoint unavailable")
    )
    _install_windows_audio_policy(app, endpoint_provider)

    try:
        with patch("whisperstream.main.has_input_device", return_value=True):
            assert app._try_begin_recording() is True
            assert app._recording is True
            app._on_hotkey_release()
    finally:
        if app._idle_timer is not None:
            app._idle_timer.cancel()

    warning_args = [
        call.args
        for call in app.logger.warning.call_args_list
        if call.args and call.args[0].startswith("playback snapshot failed")
    ]
    assert [(args[1], args[2], args[3]) for args in warning_args] == [
        ("pre_capture", "off", "endpoint-unavailable"),
        ("post_capture", "off", "endpoint-unavailable"),
        ("pre_close", "off", "endpoint-unavailable"),
        ("post_close", "off", "endpoint-unavailable"),
    ]
    assert app._recording is False
    app.audio_capture.start.assert_called_once_with(discard_until_armed=True)
    app.audio_capture.arm_capture.assert_called_once_with()
    app.audio_capture.stop.assert_called_once_with()


@_requires_windows_volume
def test_capture_starts_before_volume_endpoint_is_reacquired(_make_app) -> None:
    """Mute the HFP endpoint established by capture, never the cached A2DP one."""
    app = _make_app(system_volume_mode="mute")
    app._started_via_toggle = False

    a2dp_endpoint = MagicMock(name="a2dp_endpoint")
    hfp_endpoint = MagicMock(name="hfp_endpoint")
    hfp_endpoint.GetMute.return_value = 0

    events: list[str] = []
    def play_start_chime(_path, *, on_complete=None):
        events.append("start_chime")
        if on_complete is not None:
            on_complete()

    app.audio_feedback.play_start_blocking.side_effect = play_start_chime
    app.audio_capture.start.side_effect = (
        lambda **_kwargs: events.append("capture_start")
    )
    app.audio_capture.arm_capture.side_effect = lambda: events.append("capture_arm")
    a2dp_endpoint.GetMasterVolumeLevelScalar.side_effect = lambda: (
        events.append("get_a2dp_volume") or 0.9
    )
    a2dp_endpoint.GetMute.return_value = 0
    hfp_endpoint.GetMasterVolumeLevelScalar.side_effect = lambda: (
        events.append("get_hfp_volume") or 0.8
    )
    hfp_endpoint.SetMute.side_effect = (
        lambda _muted, _context: events.append("mute_hfp")
    )
    speaker_sequence = iter(
        [
            ("get_speakers_a2dp", "endpoint-a2dp", a2dp_endpoint),
            ("get_speakers_hfp", "endpoint-hfp", hfp_endpoint),
        ]
    )

    def _get_speakers():
        event, endpoint_id, endpoint = next(speaker_sequence)
        events.append(event)
        return endpoint_id, endpoint

    endpoint_provider = MagicMock(side_effect=_get_speakers)
    _install_windows_audio_policy(app, endpoint_provider)

    with patch("whisperstream.main.has_input_device", return_value=True):
        app._try_begin_recording()

    assert events == [
        "capture_start",
        "start_chime",
        "get_speakers_a2dp",
        "get_a2dp_volume",
        "capture_arm",
        "get_speakers_hfp",
        "get_hfp_volume",
        "get_hfp_volume",
        "mute_hfp",
    ]
    assert endpoint_provider.call_count == 2
    a2dp_endpoint.GetMasterVolumeLevelScalar.assert_called_once_with()
    a2dp_endpoint.SetMute.assert_not_called()


@_requires_windows_volume
def test_capture_start_failure_leaves_cached_endpoint_unchanged(_make_app) -> None:
    """A failed microphone open must not reacquire or alter system volume."""
    app = _make_app(system_volume_mode="mute")
    app._started_via_toggle = False
    cached_endpoint = MagicMock(name="cached_endpoint")
    cached_endpoint.GetMasterVolumeLevelScalar.return_value = 0.7
    cached_endpoint.GetMute.return_value = 0
    endpoint_provider = MagicMock(return_value=("cached", cached_endpoint))
    _install_windows_audio_policy(app, endpoint_provider)
    app.audio_capture.start.side_effect = OSError("device unavailable")

    with patch("whisperstream.main.has_input_device", return_value=True):
        app._try_begin_recording()

    assert app._recording is False
    endpoint_provider.assert_not_called()
    cached_endpoint.GetMasterVolumeLevelScalar.assert_not_called()
    cached_endpoint.SetMute.assert_not_called()


@pytest.mark.parametrize("replacement_failure", ["close", "construct"])
def test_capture_replacement_failure_precedes_policy_and_start_cue(
    _make_app,
    replacement_failure: str,
) -> None:
    app = _make_app()
    app.config = replace(app.config, audio_input_device="Changed Input")
    app._audio_capture_device = ""
    app._reserve_audio_policy_generation = MagicMock()
    app._play_recording_chime = MagicMock()
    app._close_audio_policy = MagicMock()
    app._dispatch_error_dialog = MagicMock()
    app.audio_capture.close.return_value = replacement_failure != "close"

    constructor = patch("whisperstream.main.AudioCapture")
    with constructor as capture_factory:
        if replacement_failure == "construct":
            capture_factory.side_effect = OSError("synthetic constructor failure")
        with patch("whisperstream.main.has_input_device") as input_probe:
            assert app._try_begin_recording() is False

    app.audio_capture.close.assert_called_once_with()
    input_probe.assert_not_called()
    app._reserve_audio_policy_generation.assert_not_called()
    app._play_recording_chime.assert_not_called()
    app._close_audio_policy.assert_not_called()
    app._dispatch_error_dialog.assert_called_once()
    assert app._dispatch_error_dialog.call_args.args[0] == "audio_capture_failed"
    app.audio_capture.start.assert_not_called()


def test_restart_after_incomplete_cleanup_cannot_reuse_retained_session(
    _make_app,
) -> None:
    app = _make_app()
    retained = CaptureSession(
        request_id="retained-capture",
        generation=9,
        device_id="retained-device",
        selection_resolution=CaptureSelectionResolution.SYSTEM_DEFAULT,
    )
    app.audio_capture.active_session = retained
    app.audio_capture.close.return_value = False
    app._reserve_audio_policy_generation = MagicMock()
    app._play_recording_chime = MagicMock()
    app._dispatch_error_dialog = MagicMock()

    with app._lock:
        result = app._start_recording_for_origin("toggle")

    assert result == "recording_not_started"
    assert app._recording is False
    assert app._active_capture_session_key is None
    app.audio_capture.close.assert_called_once_with()
    app.audio_capture.start.assert_not_called()
    app._reserve_audio_policy_generation.assert_not_called()
    app._play_recording_chime.assert_not_called()
    app._dispatch_error_dialog.assert_called_once()


@_requires_windows_volume
def test_single_playback_endpoint_keeps_same_volume_round_trip(_make_app) -> None:
    """Non-Bluetooth endpoints keep the same reduce/restore transaction."""
    app = _make_app(system_volume_mode="reduce")
    app._started_via_toggle = False
    app._executor = MagicMock()
    endpoint = MagicMock(name="playback_endpoint")
    endpoint.GetMasterVolumeLevelScalar.return_value = 0.5
    endpoint.GetMute.return_value = 0

    events: list[str] = []
    app.audio_capture.start.side_effect = (
        lambda **_kwargs: events.append("capture_start")
    )
    app.audio_capture.arm_capture.side_effect = lambda: events.append("capture_arm")
    endpoint.SetMasterVolumeLevelScalar.side_effect = (
        lambda volume, _context: events.append(f"set_volume:{volume}")
    )
    app.audio_capture.stop.side_effect = lambda: (
        events.append("capture_stop") or np.zeros(160, dtype=np.float32)
    )
    endpoint_provider = MagicMock(
        side_effect=lambda: events.append("get_speakers")
        or ("endpoint-wired", endpoint)
    )
    _install_windows_audio_policy(app, endpoint_provider)

    try:
        with patch("whisperstream.main.has_input_device", return_value=True):
            app._try_begin_recording()
            app._on_hotkey_release()
    finally:
        if app._idle_timer is not None:
            app._idle_timer.cancel()

    assert events == [
        "capture_start",
        "get_speakers",
        "capture_arm",
        "get_speakers",
        "set_volume:0.1",
        "get_speakers",
        "set_volume:0.5",
        "capture_stop",
        "get_speakers",
    ]
    assert endpoint_provider.call_count == 4


def test_restore_same_endpoint_before_capture_closes_then_stop_chime(
    _make_app,
) -> None:
    """Restore the changed HFP transaction before Windows can switch to A2DP."""
    app = _make_app(system_volume_mode="mute")
    app._recording = True
    hfp_endpoint = MagicMock(name="hfp_endpoint")
    hfp_endpoint.GetMasterVolumeLevelScalar.return_value = 0.65
    hfp_endpoint.GetMute.return_value = 0
    _install_windows_audio_policy(
        app, lambda: ("endpoint-hfp", hfp_endpoint)
    )
    app._audio_policy_generation = 1
    transaction, result = app.audio_policy.begin(1, AudioPolicyMode.MUTE)
    assert result.failure is AudioPolicyFailure.NONE
    app._audio_policy_transaction = transaction
    app._executor = MagicMock()

    events: list[str] = []
    hfp_endpoint.SetMute.side_effect = (
        lambda _muted, _context: events.append("restore_hfp_mute")
    )
    hfp_endpoint.SetMasterVolumeLevelScalar.side_effect = (
        lambda _volume, _context: events.append("restore_hfp_volume")
    )
    hfp_endpoint.SetMute.reset_mock()
    app.audio_capture.stop.side_effect = lambda: (
        events.append("capture_stop") or np.zeros(160, dtype=np.float32)
    )
    app.audio_feedback.play_stop.side_effect = (
        lambda _path: events.append("stop_chime")
    )

    try:
        app._on_hotkey_release()
    finally:
        if app._idle_timer is not None:
            app._idle_timer.cancel()

    assert events == [
        "restore_hfp_mute",
        "restore_hfp_volume",
        "capture_stop",
        "stop_chime",
    ]


def test_restore_preserves_preexisting_mute_state(_make_app) -> None:
    """A recording must not unmute an endpoint the user had already muted."""
    app = _make_app(system_volume_mode="mute")
    endpoint = MagicMock()
    endpoint.GetMasterVolumeLevelScalar.return_value = 0.4
    endpoint.GetMute.return_value = 1
    _install_windows_audio_policy(app, lambda: ("endpoint", endpoint))
    app._audio_policy_generation = 1

    app._reduce_system_volume()
    app._restore_system_volume()

    assert [mock_call.args for mock_call in endpoint.SetMute.call_args_list] == [
        (1, None),
        (1, None),
    ]
    endpoint.SetMasterVolumeLevelScalar.assert_called_once_with(0.4, None)


def test_chunk_ready_submits_to_executor(_make_app) -> None:
    app = _make_app()
    app._recording = True
    original_submit = app._executor.submit
    submitted_calls = []

    def tracking_submit(fn, *args, **kwargs):
        future = original_submit(fn, *args, **kwargs)
        submitted_calls.append((fn, args, kwargs))
        return future

    app._executor.submit = tracking_submit

    chunk = np.zeros(1000, dtype=np.float32)
    app._on_chunk_ready(chunk)

    assert len(submitted_calls) == 1, "Expected one submission to the executor"
    assert len(app._chunk_futures) == 1, "Expected one future appended to _chunk_futures"

    # Wait for the future to complete so the executor thread finishes cleanly.
    app._chunk_futures[0].result(timeout=5)


# ── Test 5: _finalize merges chunked results with overlap dedup ────────


def test_finalize_merges_chunk_results(_make_app) -> None:
    app = _make_app()

    # Pre-populate _chunk_futures with three already-resolved futures.
    futures = []
    for text in ["hello world how", "world how are you", "are you doing"]:
        f = concurrent.futures.Future()
        f.set_result(text)
        futures.append(f)

    app._chunk_futures = futures
    app._last_waveform = np.zeros(16000, dtype=np.float32)

    # Call directly (not via executor) so we can assert synchronously.
    app._finalize_transcription_and_inject()

    # text_injector.inject must have been called exactly once.
    app.text_injector.inject.assert_called_once()
    injected_text = app.text_injector.inject.call_args[0][0]

    # The merged text should exhibit overlap dedup: "world how" appears in
    # chunks 1 and 2, "are you" appears in chunks 2 and 3. After dedup the
    # combined text should not have duplicate runs.
    assert "world how world how" not in injected_text, (
        f"Overlap dedup failed: duplicated run in {injected_text!r}"
    )
    # Positive check: all unique words present.
    for word in ("hello", "world", "how", "are", "you", "doing"):
        assert word in injected_text, f"Missing word {word!r} in {injected_text!r}"


# ── Test 6: _finalize with no chunks falls back to single-shot ─────────


@pytest.mark.parametrize('chunked', [False, True])
def test_real_speech_gate_prevents_noisy_silence_delivery(_make_app, tmp_path, chunked):
    from whisperstream.supervised_stt_manager import SupervisedSttManager
    from whisperstream.speech_gate_client import SpeechGateClient
    from whisperstream.speech_gate_settings import SpeechGateSettings

    class PipelineManager(SupervisedSttManager):
        def _load_body(self):
            return {}

    app = _make_app()
    supervisor = MagicMock()
    supervisor.capabilities = {}
    manager = PipelineManager(model_name="pipeline-test", threads=None,
        logger=app.logger, model_dir=tmp_path, timeout=0, _supervisor=supervisor)
    manager._speech_gate = SpeechGateClient(SpeechGateSettings(),
        diagnostic_dir=tmp_path / "gate", logger=app.logger)
    app.parakeet = manager
    app.transcript_store = MagicMock()
    app.license_manager.reset_mock()
    app.llm_enhancer = MagicMock()
    app._last_waveform = np.random.default_rng(8).normal(0, 0.002, 16000).astype(np.float32)
    app._chunk_futures = []
    broadcasts = []
    app._broadcast = broadcasts.append
    try:
        if chunked:
            result = concurrent.futures.Future()
            result.set_result(manager.transcribe_chunk(app._last_waveform))
            app._chunk_futures.append(result)
        app._finalize_transcription_and_inject()
        supervisor.request_audio.assert_not_called()
        assert not manager._speech_gate.failed
        assert manager._speech_gate.is_running
        app.text_injector.inject.assert_not_called()
        assert not app.llm_enhancer.mock_calls
        assert not app.transcript_store.mock_calls
        assert not app.license_manager.mock_calls
        assert {'type': 'state.transcribing', 'active': False} in broadcasts
    finally:
        manager.shutdown()


def test_finalize_no_chunks_single_shot(_make_app) -> None:
    app = _make_app()

    waveform = np.zeros(16000, dtype=np.float32)
    app._last_waveform = waveform
    app._chunk_futures = []

    # The no-chunk fallback transcribes the full waveform through the chunked
    # entry (manager.transcribe splits + merges). parakeet is a mock here, so
    # stub transcribe() directly.
    app.stt_engine.transcribe.return_value = "single shot result"

    app._finalize_transcription_and_inject()

    # parakeet.transcribe must have been called with the full waveform.
    app.stt_engine.transcribe.assert_called_once()
    call_args = app.stt_engine.transcribe.call_args
    np.testing.assert_array_equal(call_args[0][0], waveform)

    # text_injector.inject must have been called once.
    app.text_injector.inject.assert_called_once()


# ── Test 7: empty waveform broadcasts state.transcribing:false ─────────


def test_finalize_empty_waveform_broadcasts_transcribing_false(_make_app) -> None:
    """Quick-tap regression: zero-frame waveform must still broadcast
    state.transcribing:false so the HUD exits its 'processing' state.
    """
    app = _make_app()
    app._last_waveform = np.empty(0, dtype=np.float32)
    app._chunk_futures = []

    broadcasts = []
    app._broadcast = broadcasts.append

    app._finalize_transcription_and_inject()

    # parakeet must NOT be invoked on empty audio.
    app.stt_engine.transcribe_chunk.assert_not_called()
    # The HUD-clearing event must still fire.
    assert {"type": "state.transcribing", "active": False} in broadcasts


def test_finalize_no_model_broadcasts_transcribing_false(_make_app) -> None:
    """Same lifecycle requirement when the model failed to load."""
    app = _make_app()
    app._last_waveform = np.zeros(16000, dtype=np.float32)
    app._chunk_futures = []
    app.stt_engine = None

    broadcasts = []
    app._broadcast = broadcasts.append

    app._finalize_transcription_and_inject()

    assert {"type": "state.transcribing", "active": False} in broadcasts


def test_dictation_lifecycle_is_versioned_correlated_and_keeps_legacy_events(
    _make_app,
) -> None:
    app = _make_app(show_live_preview=True)
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
        first_recording_id = app.dictation_state_snapshot()["recording_id"]
        app._stop_recording()

    # The single-worker executor preserves finalizer ordering. A barrier gives
    # the terminal lifecycle transition a deterministic completion point.
    app._executor.submit(lambda: None).result(timeout=5)
    if app._idle_timer is not None:
        app._idle_timer.cancel()

    lifecycle = [event for event in broadcasts if event["type"] == "dictation.state"]
    assert [event["phase"] for event in lifecycle] == [
        "recording",
        "processing",
        "idle",
    ]
    assert {event["recording_id"] for event in lifecycle} == {first_recording_id}
    assert first_recording_id > 0
    assert all(event["version"] == 1 for event in lifecycle)
    assert all(event["origin"] == "push_to_talk" for event in lifecycle)
    assert all(event["hud_enabled"] is True for event in lifecycle)
    assert all(event["preview_enabled"] is True for event in lifecycle)
    assert all(event["screen_reader_detected"] is False for event in lifecycle)

    # Existing consumers remain supported until the HUD migrates.
    assert any(
        event["type"] == "state.recording" and event["recording"] is True
        for event in broadcasts
    )
    assert any(
        event["type"] == "state.recording" and event["recording"] is False
        for event in broadcasts
    )
    assert any(event["type"] == "state.transcribing" for event in broadcasts)


@pytest.mark.parametrize(
    ("show_indicator", "show_preview", "screen_reader", "hud_enabled", "preview_enabled"),
    [
        (True, True, False, True, True),
        (True, True, True, False, False),
        (False, True, False, False, False),
        (True, False, False, True, False),
    ],
)
def test_dictation_lifecycle_carries_effective_presentation_flags(
    _make_app,
    show_indicator: bool,
    show_preview: bool,
    screen_reader: bool,
    hud_enabled: bool,
    preview_enabled: bool,
) -> None:
    app = _make_app(
        show_recording_indicator=show_indicator,
        show_live_preview=show_preview,
    )
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)

    with patch(
        "whisperstream.main.screen_reader_active", return_value=screen_reader
    ):
        app._start_recording()

    event = next(event for event in broadcasts if event["type"] == "dictation.state")
    assert event["phase"] == "recording"
    assert event["hud_enabled"] is hud_enabled
    assert event["preview_enabled"] is preview_enabled
    assert event["screen_reader_detected"] is screen_reader
    app._invalidate_live_preview()


def test_dictation_lifecycle_disables_preview_when_model_is_unavailable(
    _make_app,
) -> None:
    app = _make_app(
        show_recording_indicator=True,
        show_live_preview=True,
    )
    app.stt_engine = None
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()

    event = next(event for event in broadcasts if event["type"] == "dictation.state")
    assert event["hud_enabled"] is True
    assert event["preview_enabled"] is False
    assert app._live_preview_coordinator is None


@pytest.mark.parametrize(
    ("show_indicator", "show_preview", "screen_reader", "has_model", "enabled"),
    [
        (True, True, False, True, True),
        (False, True, False, True, False),
        (True, False, False, True, False),
        (True, True, True, True, False),
        (True, True, False, False, False),
    ],
)
def test_live_preview_runtime_gate_controls_capture_and_inference(
    _make_app,
    show_indicator: bool,
    show_preview: bool,
    screen_reader: bool,
    has_model: bool,
    enabled: bool,
) -> None:
    app = _make_app(
        show_recording_indicator=show_indicator,
        show_live_preview=show_preview,
    )
    manager = app.stt_engine
    if not has_model:
        app.stt_engine = None

    with patch(
        "whisperstream.main.screen_reader_active", return_value=screen_reader
    ):
        app._start_recording()

    callback = app.audio_capture.set_preview_callback.call_args.args[0]
    assert (callback is not None) is enabled
    assert (app._live_preview_coordinator is not None) is enabled
    manager.try_transcribe_preview.assert_not_called()
    app._invalidate_live_preview()


def test_live_preview_emits_correlated_monotonic_event_off_final_executor(
    _make_app,
) -> None:
    app = _make_app(show_live_preview=True)
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    app.audio_capture.sample_rate = 16_000
    app.stt_engine.try_transcribe_preview.return_value = DetailedTranscriptionResult(
        text="ephemeral preview working words mature",
        tokens=(
            " ephemeral",
            " preview",
            " working",
            " words",
            " mature",
        ),
        timestamps=(0.1, 0.4, 0.7, 1.0, 1.3),
    )
    app.audio_capture.preview_snapshot.return_value = PreviewAudioSnapshot(
        audio=np.zeros(32_000, dtype=np.float32),
        total_samples=32_000,
        start_sample=0,
    )

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
    recording_id = app.dictation_state_snapshot()["recording_id"]
    notify = app.audio_capture.set_preview_callback.call_args.args[0]
    notify()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not any(
        event.get("type") == "transcription.preview" for event in broadcasts
    ):
        time.sleep(0.005)

    preview_events = [
        event for event in broadcasts if event.get("type") == "transcription.preview"
    ]
    assert preview_events == [
        {
            "type": "transcription.preview",
            "recording_id": recording_id,
                "revision": 1,
                "text": "ephemeral preview working words mature",
                "stable_char_count": 0,
                "presentation_version": 1,
                "mutable_start_char_count": 0,
                "freeze": False,
        }
    ]
    assert app._chunk_futures == []
    app.stt_engine.try_transcribe_preview.assert_called_once()
    app._invalidate_live_preview()


def test_stop_invalidates_preview_before_capture_and_never_joins_decode(
    _make_app,
) -> None:
    app = _make_app(show_live_preview=True)
    decode_started = threading.Event()
    release_decode = threading.Event()
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)

    def blocking_preview(*args, **kwargs):
        decode_started.set()
        assert release_decode.wait(timeout=2.0)
        return DetailedTranscriptionResult(text="late preview")

    app.stt_engine.try_transcribe_preview.side_effect = blocking_preview
    app.stt_engine.transcribe.return_value = "authoritative final"
    app.audio_capture.preview_snapshot.return_value = PreviewAudioSnapshot(
        audio=np.zeros(64_000, dtype=np.float32),
        total_samples=64_000,
        start_sample=0,
    )

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
    notify = app.audio_capture.set_preview_callback.call_args.args[0]
    notify()
    assert decode_started.wait(timeout=1.0)

    started = time.perf_counter()
    app._stop_recording()
    assert time.perf_counter() - started < 0.25
    assert app._live_preview_coordinator is None

    release_decode.set()
    app._executor.submit(lambda: None).result(timeout=5)
    if app._idle_timer is not None:
        app._idle_timer.cancel()
    time.sleep(0.02)

    assert not any(
        event.get("type") == "transcription.preview" for event in broadcasts
    )
    app.stt_engine.transcribe.assert_called_once()
    app.text_injector.inject.assert_called_once()


def _run_authoritative_pipeline_with_optional_preview(
    _make_app,
    *,
    preview_enabled: bool,
    input_style: str,
) -> dict:
    """Run one complete dictation and return every transcript-bearing seam."""
    app = _make_app(
        show_live_preview=preview_enabled,
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_chunked_cleanup=False,
        profiles=[{"id": "default", "name": "Default", "prompt": "clean"}],
        default_profile_id="default",
        history_enabled=True,
        mac_history_opt_in=True,
        save_audio=True,
        input_style=input_style,
        input_style_user_set=True,
    )
    preview_text = "PREVIEW_ONLY_7fd849 never persist or deliver"
    final_text = "authoritative final dictation"
    enhanced_text = "clean authoritative final dictation"
    processed_text = "processed authoritative final dictation"
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    app.stt_engine.transcribe.return_value = final_text
    app.llama_server = None
    app.llm_enhancer = MagicMock()
    app.llm_enhancer.will_enhance.return_value = True
    app.llm_enhancer.enhance_with_outcome = AsyncMock(
        return_value=EnhancementResult(enhanced_text, "success")
    )
    app.text_injector.process.return_value = processed_text
    app.text_injector.inject.return_value = "focused"
    app.review_state = MagicMock()
    app.transcript_store = MagicMock()
    app.transcript_store.save.return_value = "stored-final"

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
    recording_id = app.dictation_state_snapshot()["recording_id"]
    if preview_enabled:
        app._publish_live_preview(
            recording_id,
            1,
            PreviewUpdate(text=preview_text, stable_char_count=13),
        )
    app._stop_recording()

    # Finalization and persistence use separate single-worker executors so
    # shutdown can drain them in dependency order.
    app._executor.submit(lambda: None).result(timeout=5)
    app._persistence_executor.submit(lambda: None).result(timeout=5)
    if app._idle_timer is not None:
        app._idle_timer.cancel()

    result_events = [
        event for event in broadcasts if event.get("type") == "transcription.result"
    ]
    preview_events = [
        event for event in broadcasts if event.get("type") == "transcription.preview"
    ]
    assert len(result_events) == 1
    assert app.transcript_store.save.call_count == 1
    store_kwargs = app.transcript_store.save.call_args.kwargs

    # These assertions intentionally enumerate every transcript-bearing seam in
    # the authoritative path. The preview may exist only in its dedicated event.
    assert app.llm_enhancer.enhance_with_outcome.await_args.args[0] == final_text
    app.text_injector.process.assert_called_once_with(enhanced_text)
    app.text_injector.inject.assert_called_once_with(
        processed_text,
        input_style=input_style,
        submit=False,
        request_id=f"dictation-{recording_id}",
        generation=recording_id,
        already_processed=True,
    )
    app.text_injector.remember_original.assert_called_once_with(final_text)
    assert result_events[0]["text"] == processed_text
    assert store_kwargs["raw_text"] == final_text
    assert store_kwargs["enhanced_text"] == enhanced_text
    assert store_kwargs["source"] == "dictation"
    assert store_kwargs["audio_bytes"] is not None
    assert app.review_state.record_session.call_count == 1
    assert app._idle_timer is not None
    assert app._idle_timer.function == app._check_review_prompt
    assert app.dictation_state_snapshot()["phase"] == "idle"
    app.stt_engine.transcribe.assert_called_once()
    final_audio = app.stt_engine.transcribe.call_args.args[0]

    transcript_surfaces = {
        "enhancer": app.llm_enhancer.enhance_with_outcome.await_args.args[0],
        "processor": app.text_injector.process.call_args.args[0],
        "delivery": app.text_injector.inject.call_args.args[0],
        "paste_last_original": app.text_injector.remember_original.call_args.args[0],
        "result": result_events[0]["text"],
        "history_raw": store_kwargs["raw_text"],
        "history_enhanced": store_kwargs["enhanced_text"],
    }
    assert all(preview_text not in value for value in transcript_surfaces.values())
    assert preview_events == (
        [
            {
                "type": "transcription.preview",
                "recording_id": recording_id,
                    "revision": 1,
                    "text": preview_text,
                    "stable_char_count": 13,
                    "presentation_version": 1,
                    "mutable_start_char_count": 0,
                    "freeze": False,
                }
        ]
        if preview_enabled
        else []
    )

    return {
        "transcript_surfaces": transcript_surfaces,
        "delivery_kwargs": app.text_injector.inject.call_args.kwargs,
        # Wall-clock measurements can differ by a millisecond between the two
        # sequential runs. Compare the authoritative event contract while
        # excluding timing telemetry, which is not transcript-bearing state.
        "result_event": {
            key: value
            for key, value in result_events[0].items()
            if key not in {"duration_ms", "enhance_ms"}
        },
        "history": {
            key: value
            for key, value in store_kwargs.items()
            if key not in {"audio_bytes", "peaks"}
        },
        "authoritative_audio_sha256": hashlib.sha256(final_audio.tobytes()).hexdigest(),
        "authoritative_asr_kwargs": app.stt_engine.transcribe.call_args.kwargs,
        "persisted_audio_sha256": hashlib.sha256(
            store_kwargs["audio_bytes"]
        ).hexdigest(),
        "review_calls": app.review_state.record_session.call_count,
    }


@pytest.mark.parametrize("input_style", ["clipboard", "keystroke"])
def test_live_preview_cannot_change_any_authoritative_pipeline_surface(
    _make_app, caplog, input_style: str
) -> None:
    """Preview on/off must produce byte-for-byte equivalent downstream calls."""
    without_preview = _run_authoritative_pipeline_with_optional_preview(
        _make_app, preview_enabled=False, input_style=input_style
    )
    with_preview = _run_authoritative_pipeline_with_optional_preview(
        _make_app, preview_enabled=True, input_style=input_style
    )

    assert with_preview == without_preview
    assert "PREVIEW_ONLY_7fd849 never persist or deliver" not in caplog.text


def test_processing_lifecycle_covers_slow_enhancement_and_delivery(_make_app) -> None:
    app = _make_app(
        show_live_preview=True,
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_chunked_cleanup=False,
        profiles=[{"id": "default", "name": "Default", "prompt": "clean"}],
        default_profile_id="default",
        history_enabled=False,
    )
    app.stt_engine.transcribe.return_value = "authoritative final"
    app.llama_server = None
    app.llm_enhancer = MagicMock()
    app.llm_enhancer.will_enhance.return_value = True
    enhancement_started = threading.Event()
    release_enhancement = threading.Event()
    trace: list[str] = []
    broadcasts: list[dict] = []

    async def _slow_enhance(
        text: str,
        *,
        profile_id=None,
        on_timeout=None,
        on_error=None,
    ) -> EnhancementResult:
        assert text == "authoritative final"
        enhancement_started.set()
        assert await asyncio.to_thread(release_enhancement.wait, 5)
        trace.append("enhanced")
        return EnhancementResult("clean final", "success")

    app.llm_enhancer.enhance_with_outcome = AsyncMock(side_effect=_slow_enhance)
    app.text_injector.process.return_value = "processed clean final"
    app.text_injector.inject.side_effect = (
        lambda *args, **kwargs: trace.append("delivery") or "focused"
    )

    def _broadcast(event: dict) -> None:
        broadcasts.append(event)
        if event.get("type") == "dictation.state":
            trace.append(event["phase"])

    app.set_event_broadcaster(_broadcast)
    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
        app._stop_recording()

    assert enhancement_started.wait(timeout=5)
    assert app.dictation_state_snapshot()["phase"] == "processing"
    assert "idle" not in trace
    app.text_injector.inject.assert_not_called()

    release_enhancement.set()
    app._executor.submit(lambda: None).result(timeout=5)
    if app._idle_timer is not None:
        app._idle_timer.cancel()

    assert trace.index("enhanced") < trace.index("delivery") < trace.index("idle")
    assert app.dictation_state_snapshot()["phase"] == "idle"


@pytest.mark.parametrize(
    "terminal_path", ["empty_audio", "no_model", "asr_error", "raw_fallback"]
)
def test_preview_text_stays_isolated_across_finalization_fallbacks(
    _make_app, terminal_path: str
) -> None:
    preview_text = "PREVIEW_FALLBACK_SENTINEL_c6401"
    final_text = "authoritative fallback text"
    app = _make_app(
        show_live_preview=True,
        history_enabled=True,
        mac_history_opt_in=True,
        save_audio=True,
        enhancement_enabled=terminal_path == "raw_fallback",
        enhancement_provider="local",
        enhancement_chunked_cleanup=False,
        profiles=[{"id": "default", "name": "Default", "prompt": "clean"}],
        default_profile_id="default",
    )
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    app.transcript_store = MagicMock()
    app.review_state = MagicMock()
    app.stt_engine.transcribe.return_value = final_text
    app.text_injector.process.side_effect = lambda text: text

    if terminal_path == "empty_audio":
        app.audio_capture.stop.return_value = np.empty(0, dtype=np.float32)
    elif terminal_path == "asr_error":
        app.stt_engine.transcribe.side_effect = RuntimeError("content-free decode error")
    elif terminal_path == "raw_fallback":
        app.llama_server = None
        app.llm_enhancer = MagicMock()
        app.llm_enhancer.will_enhance.return_value = True
        app.llm_enhancer.enhance_with_outcome = AsyncMock(
            side_effect=RuntimeError("content-free enhancement error")
        )

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
    recording_id = app.dictation_state_snapshot()["recording_id"]
    app._publish_live_preview(
        recording_id,
        1,
        PreviewUpdate(text=preview_text, stable_char_count=0),
    )
    if terminal_path == "no_model":
        # Model availability can change between recording start and stop. The
        # stop-time context must still fail closed without consulting preview.
        app.stt_engine = None
    app._stop_recording()

    app._executor.submit(lambda: None).result(timeout=5)
    app._persistence_executor.submit(lambda: None).result(timeout=5)
    if app._idle_timer is not None:
        app._idle_timer.cancel()

    preview_events = [
        event for event in broadcasts if event.get("type") == "transcription.preview"
    ]
    assert [event["text"] for event in preview_events] == [preview_text]
    non_preview_payloads = [
        event for event in broadcasts if event.get("type") != "transcription.preview"
    ]
    assert preview_text not in repr(non_preview_payloads)
    assert preview_text not in repr(app.text_injector.method_calls)
    assert preview_text not in repr(app.transcript_store.method_calls)
    assert app.dictation_state_snapshot()["phase"] == "idle"

    if terminal_path == "raw_fallback":
        app.llm_enhancer.enhance_with_outcome.assert_awaited_once()
        assert app.llm_enhancer.enhance_with_outcome.await_args.args[0] == final_text
        app.text_injector.inject.assert_called_once()
        assert app.text_injector.inject.call_args.args[0] == final_text
        assert app.transcript_store.save.call_args.kwargs["raw_text"] == final_text
        assert app.transcript_store.save.call_args.kwargs["enhanced_text"] is None
    else:
        app.text_injector.inject.assert_not_called()
        app.transcript_store.save.assert_not_called()


def test_stale_recording_id_cannot_clear_a_newer_dictation(_make_app) -> None:
    app = _make_app()
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
        first_id = app.dictation_state_snapshot()["recording_id"]
        app._recording = False
        app._start_recording()
        second_state = app.dictation_state_snapshot()

    assert second_state["recording_id"] > first_id
    before = len(broadcasts)
    assert app._transition_dictation_state(first_id, "idle") is False
    assert len(broadcasts) == before
    assert app.dictation_state_snapshot() == second_state


def test_stop_snapshots_waveform_manager_and_routing_inputs(_make_app) -> None:
    """A queued finalizer must not read globals overwritten by newer work."""
    app = _make_app(
        history_enabled=False,
        input_style="clipboard",
        input_style_user_set=True,
        profiles=[
            {"id": "chat", "name": "Chat", "prompt": "p"},
            {
                "id": "terminal",
                "name": "Terminal",
                "prompt": "p",
                "input_style": "keystroke",
            },
        ],
        power_mode_map={"first.exe": "terminal"},
    )
    first_manager = app.stt_engine
    first_manager.transcribe.return_value = "context owned result"
    app.text_injector.process.return_value = "context owned processed result"
    first_waveform = np.ones(16000, dtype=np.float32)
    app.audio_capture.stop.return_value = first_waveform

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
    app._dictation_app = RoutingTarget("first.exe")

    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def _block_executor() -> None:
        blocker_started.set()
        assert release_blocker.wait(timeout=5)

    app._executor.submit(_block_executor)
    assert blocker_started.wait(timeout=5)
    app._stop_recording()
    assert app._dictation_app is None

    replacement_manager = MagicMock()
    app.stt_engine = replacement_manager
    app._last_waveform = np.zeros(32, dtype=np.float32)
    app._chunk_futures = []
    app._chunk_start_time = 999.0
    app._dictation_app = RoutingTarget("second.exe")

    release_blocker.set()
    app._executor.submit(lambda: None).result(timeout=5)
    if app._idle_timer is not None:
        app._idle_timer.cancel()

    first_manager.transcribe.assert_called_once()
    np.testing.assert_array_equal(first_manager.transcribe.call_args.args[0], first_waveform)
    replacement_manager.transcribe.assert_not_called()
    app.text_injector.inject.assert_called_once_with(
        "context owned processed result",
        input_style="keystroke",
        submit=False,
        request_id="dictation-1",
        generation=1,
        already_processed=True,
    )


def test_idle_precedes_background_persistence_but_follows_delivery(_make_app) -> None:
    app = _make_app(history_enabled=True, mac_history_opt_in=True, save_audio=False)
    app.stt_engine.transcribe.return_value = "delivered before idle"
    trace: list[str] = []
    broadcasts: list[dict] = []

    def _broadcast(event: dict) -> None:
        broadcasts.append(event)
        if event.get("type") == "dictation.state":
            trace.append(event["phase"])

    app.set_event_broadcaster(_broadcast)
    app.text_injector.inject.side_effect = lambda *args, **kwargs: trace.append("delivery") or "focused"

    persist_started = threading.Event()
    release_persist = threading.Event()
    app.transcript_store = MagicMock()

    def _save(**kwargs):
        persist_started.set()
        assert release_persist.wait(timeout=5)
        return "recording-id"

    app.transcript_store.save.side_effect = _save

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
        app._stop_recording()

    assert persist_started.wait(timeout=5)
    assert app.dictation_state_snapshot()["phase"] == "idle"
    assert trace.index("delivery") < trace.index("idle")

    release_persist.set()
    app._persistence_executor.submit(lambda: None).result(timeout=5)
    if app._idle_timer is not None:
        app._idle_timer.cancel()


def test_terminal_idle_is_idempotent_for_matching_recording(_make_app) -> None:
    app = _make_app()
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
    recording_id = app.dictation_state_snapshot()["recording_id"]
    assert app._transition_dictation_state(recording_id, "processing") is True
    assert app._transition_dictation_state(recording_id, "idle") is True
    assert app._transition_dictation_state(recording_id, "idle") is False

    terminals = [
        event
        for event in broadcasts
        if event.get("type") == "dictation.state" and event.get("phase") == "idle"
    ]
    assert len(terminals) == 1


@pytest.mark.parametrize(
    "terminal_path",
    ["empty_audio", "no_model", "whitespace", "asr_error", "suppressed"],
)
def test_all_finalization_paths_emit_exactly_one_terminal_idle(
    _make_app, terminal_path: str
) -> None:
    app = _make_app(history_enabled=False)
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)

    if terminal_path == "empty_audio":
        app.audio_capture.stop.return_value = np.empty(0, dtype=np.float32)
    elif terminal_path == "no_model":
        app.stt_engine = None
    elif terminal_path == "whitespace":
        app.stt_engine.transcribe.return_value = "   "
    elif terminal_path == "asr_error":
        app.stt_engine.transcribe.side_effect = RuntimeError("decode failed")
    elif terminal_path == "suppressed":
        app.set_inject_suppressed(True)
        app.stt_engine.transcribe.return_value = "suppressed result"

    with patch("whisperstream.main.screen_reader_active", return_value=False):
        app._start_recording()
        app._stop_recording()

    app._executor.submit(lambda: None).result(timeout=5)
    if app._idle_timer is not None:
        app._idle_timer.cancel()

    lifecycle = [
        event for event in broadcasts if event.get("type") == "dictation.state"
    ]
    assert [event["phase"] for event in lifecycle] == [
        "recording",
        "processing",
        "idle",
    ]


def _begin_enhancement_context(app, target: RoutingTarget | None):
    """Create a current processing context without starting microphone I/O."""
    app._dictation_app = target
    recording_id = app._begin_dictation_state(
        origin="toggle", screen_reader_detected=False
    )
    assert app._transition_dictation_state(recording_id, "processing") is True
    return app._capture_finalization_context(
        recording_id=recording_id,
        waveform=None,
        dictation_app=target,
    )


@pytest.mark.parametrize(
    ("target", "manual_profile_id", "override_anchor", "expected_profile"),
    [
        (RoutingTarget("slack.exe"), "custom", "slack.exe", "custom"),
        (RoutingTarget("chrome.exe", "mail.example"), None, None, "email"),
        (RoutingTarget("code.exe"), None, None, "code"),
        (RoutingTarget("unmapped.exe"), None, None, "chat"),
    ],
    ids=["manual", "site", "app", "default"],
)
def test_effective_profile_event_covers_every_routing_tier(
    _make_app,
    target,
    manual_profile_id,
    override_anchor,
    expected_profile,
) -> None:
    profiles = [
        {"id": "chat", "name": "Chat", "prompt": "chat"},
        {"id": "code", "name": "Code", "prompt": "code"},
        {"id": "email", "name": "Email", "prompt": "email"},
        {
            "id": "custom",
            "name": "Standup",
            "prompt": "standup",
            "icon": "people",
        },
    ]
    app = _make_app(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_chunked_cleanup=False,
        profiles=profiles,
        default_profile_id="chat",
        power_mode_map={"code.exe": "code"},
        site_map={"mail.example": "email"},
        history_enabled=False,
    )
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    app._manual_profile_id = manual_profile_id
    app._override_anchor = override_anchor
    app.llm_enhancer = MagicMock()
    app.llm_enhancer.will_enhance.return_value = True
    app.llm_enhancer.enhance_with_outcome = AsyncMock(
        return_value=EnhancementResult("cleaned result", "success")
    )
    app.text_injector.process.side_effect = lambda value: value
    context = _begin_enhancement_context(app, target)

    app._enhance_then_inject(
        "authoritative text with enough words to enhance",
        1.0,
        context=context,
    )

    profile_events = [
        event for event in broadcasts if event.get("type") == "enhancement.profile"
    ]
    expected = {
        "type": "enhancement.profile",
        "recording_id": context.recording_id,
        "profile_id": expected_profile,
        "name": next(
            profile["name"] for profile in profiles if profile["id"] == expected_profile
        ),
    }
    if expected_profile == "custom":
        expected["icon"] = "people"
    assert profile_events == [expected]


def test_profile_event_is_silent_when_enhancement_will_not_run(_make_app) -> None:
    app = _make_app(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_chunked_cleanup=False,
        profiles=[{"id": "chat", "name": "Chat", "prompt": "chat"}],
        default_profile_id="chat",
        history_enabled=False,
    )
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    app.llm_enhancer = MagicMock()
    app.llm_enhancer.will_enhance.return_value = False
    app.llm_enhancer.enhance_with_outcome = AsyncMock(
        return_value=EnhancementResult("raw text stays raw", "not_attempted")
    )
    app.text_injector.process.side_effect = lambda value: value
    context = _begin_enhancement_context(app, RoutingTarget("unmapped.exe"))

    app._enhance_then_inject(
        "raw text stays raw with enough words",
        1.0,
        context=context,
    )

    assert not any(
        event.get("type") == "enhancement.profile" for event in broadcasts
    )


def test_whole_timeout_event_is_correlated_and_ordered_before_result(
    _make_app,
) -> None:
    app = _make_app(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_chunked_cleanup=False,
        profiles=[{"id": "chat", "name": "Chat", "prompt": "chat"}],
        default_profile_id="chat",
        history_enabled=False,
    )
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    app.llm_enhancer = MagicMock()
    app.llm_enhancer.will_enhance.return_value = True

    async def timeout_fallback(raw, *, profile_id=None, on_timeout=None, on_error=None):
        assert on_timeout is not None
        on_timeout("local")
        return EnhancementResult(raw, "timeout")

    app.llm_enhancer.enhance_with_outcome = AsyncMock(side_effect=timeout_fallback)
    app.text_injector.process.side_effect = lambda value: value
    context = _begin_enhancement_context(app, RoutingTarget("unmapped.exe"))

    app._enhance_then_inject(
        "raw text that will be pasted after timeout",
        1.0,
        context=context,
    )

    event_types = [event.get("type") for event in broadcasts]
    timeout_event = {
        "type": "enhancement.timeout",
        "recording_id": context.recording_id,
        "provider": "local",
    }
    assert timeout_event in broadcasts
    assert event_types.index("enhancement.profile") < event_types.index(
        "enhancement.timeout"
    ) < event_types.index("transcription.result")
    app.text_injector.inject.assert_called_once()
    assert app.text_injector.inject.call_args.args[0] == (
        "raw text that will be pasted after timeout"
    )


def test_reply_capture_enhancement_outcome_shows_no_dictation_pill(_make_app) -> None:
    import dataclasses

    app = _make_app(enhancement_enabled=True, enhancement_provider="local")
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    context = _begin_enhancement_context(app, RoutingTarget("unmapped.exe"))
    app._recording_context_is_current = lambda _context: True

    on_timeout, on_error = app._enhancement_outcome_observers(
        dataclasses.replace(context, agent_compose_lease=object())
    )
    on_timeout("local")
    on_error("local")
    outcomes = lambda: [  # noqa: E731
        event["type"] for event in broadcasts
        if event["type"].startswith("enhancement.")
    ]
    assert outcomes() == []

    _on_timeout, on_error = app._enhancement_outcome_observers(context)
    on_error("local")
    assert outcomes() == ["enhancement.error"]


def test_chunk_timeouts_emit_once_per_recording(_make_app) -> None:
    app = _make_app(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_words_per_chunk=4,
        enhancement_limit_first_chunk=False,
        profiles=[{"id": "chat", "name": "Chat", "prompt": "chat"}],
        default_profile_id="chat",
        history_enabled=False,
    )
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    app.llm_enhancer = MagicMock()

    async def timeout_each_chunk(raw, *, profile_id=None, on_timeout=None, on_error=None):
        assert on_timeout is not None
        on_timeout("local")
        return EnhancementResult(raw, "timeout")

    app.llm_enhancer.enhance_chunk_with_outcome = AsyncMock(
        side_effect=timeout_each_chunk
    )
    app.text_injector.process.side_effect = lambda value, **_kwargs: value
    app.text_injector.paste_chunk.return_value = DeliveryOutcome.focused()
    app.text_injector.last_transcript = "all raw chunks pasted"
    context = _begin_enhancement_context(app, RoutingTarget("unmapped.exe"))

    app._enhance_then_inject_chunked(
        "one two three four. five six seven eight. nine ten eleven twelve.",
        1.0,
        profile_id="chat",
        context=context,
    )

    assert app.llm_enhancer.enhance_chunk_with_outcome.await_count > 1
    assert [
        event for event in broadcasts if event.get("type") == "enhancement.timeout"
    ] == [
        {
            "type": "enhancement.timeout",
            "recording_id": context.recording_id,
            "provider": "local",
        }
    ]


def test_generic_enhancement_error_is_correlated(_make_app) -> None:
    app = _make_app(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_chunked_cleanup=False,
        profiles=[{"id": "chat", "name": "Chat", "prompt": "chat"}],
        default_profile_id="chat",
        history_enabled=False,
    )
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    app.llm_enhancer = MagicMock()
    app.llm_enhancer.will_enhance.return_value = True

    async def error_fallback(raw, *, profile_id=None, on_timeout=None, on_error=None):
        assert on_error is not None
        on_error("local")
        return EnhancementResult(raw, "error")

    app.llm_enhancer.enhance_with_outcome = AsyncMock(side_effect=error_fallback)
    app.text_injector.process.side_effect = lambda value: value
    context = _begin_enhancement_context(app, RoutingTarget("unmapped.exe"))

    app._enhance_then_inject(
        "raw text that survives a provider error",
        1.0,
        context=context,
    )

    assert {
        "type": "enhancement.error",
        "recording_id": context.recording_id,
        "provider": "local",
    } in broadcasts


def test_stale_recording_context_cannot_publish_enhancement_events(
    _make_app,
) -> None:
    app = _make_app(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_chunked_cleanup=False,
        profiles=[{"id": "chat", "name": "Chat", "prompt": "chat"}],
        default_profile_id="chat",
        history_enabled=False,
    )
    broadcasts: list[dict] = []
    app.set_event_broadcaster(broadcasts.append)
    app.llm_enhancer = MagicMock()
    app.llm_enhancer.will_enhance.return_value = True

    async def timeout_fallback(raw, *, profile_id=None, on_timeout=None, on_error=None):
        assert on_timeout is not None
        on_timeout("local")
        return EnhancementResult(raw, "timeout")

    app.llm_enhancer.enhance_with_outcome = AsyncMock(side_effect=timeout_fallback)
    app.text_injector.process.side_effect = lambda value: value
    stale_context = _begin_enhancement_context(
        app, RoutingTarget("unmapped.exe")
    )
    newer_id = app._begin_dictation_state(
        origin="toggle", screen_reader_detected=False
    )
    assert newer_id != stale_context.recording_id

    app._enhance_then_inject(
        "stale raw text that must not paint the new dictation",
        1.0,
        context=stale_context,
    )

    assert not any(
        event.get("type") in {"enhancement.profile", "enhancement.timeout"}
        for event in broadcasts
    )


def test_repeat_recordings_reserve_fresh_audio_policy_generations(_make_app):
    app = _make_app(system_volume_mode="off")
    generations = []
    with patch("whisperstream.main.has_input_device", return_value=True):
        for _ in range(2):
            assert app._try_begin_recording()
            generations.append(app._audio_policy_generation)
            app._cancel_recording(trigger="test", play_feedback=False)
    assert generations == [1, 2]
    assert app._audio_policy_transaction is None


def test_repeated_no_device_attempts_do_not_reuse_closed_audio_policy(_make_app):
    app = _make_app()
    with patch("whisperstream.main.has_input_device", return_value=False):
        assert not app._try_begin_recording()
        assert not app._try_begin_recording()
    assert app._audio_policy_generation == 2
