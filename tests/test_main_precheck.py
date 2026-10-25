"""Unit tests for the input-device precheck before start chime.

These tests pin these decisions:

- When `has_input_device()` returns False (and `can_start_recording()` is True),
  calling `_on_hotkey_press()` or `_on_toggle_start()` must:
    * NOT call `play_start_blocking`.
    * Call `play_stop` exactly once (denial feedback, not start chime).
    * Call `_dispatch_error_dialog` with code="no_input_device",
      title="No microphone found", message="Plug in a microphone to start
      recording.", retry_action="rescan_devices".
    * NOT touch `_ensure_volume_interface` or `_reduce_system_volume`.
    * NOT call `_start_recording`.

- When `has_input_device()` returns True (and `can_start_recording()` is True),
  the happy path still works: start chime fires and `_start_recording` runs.
  Volume reduction belongs to the successful capture lifecycle inside that
  method and is covered by the recording-pipeline tests.

Mirrors the bypass-init pattern from `tests/test_main_trial_guard.py`:
uses `object.__new__(WhisperstreamApp)` to avoid pulling in sherpa_onnx,
sounddevice, pynput, or pycaw. Real collaborators are replaced with
MagicMocks. `has_input_device` is monkeypatched at `whisperstream.main`
(the import site) so the test controls what the callback sees.
"""

from __future__ import annotations

import logging
import threading

import pytest
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from whisperstream.foreground_window import RoutingTarget
from whisperstream.main import WhisperstreamApp, _DICTATION_STATE_VERSION


pytestmark = pytest.mark.usefixtures("accepted_eula")


def _make_app(*, can_start: bool) -> WhisperstreamApp:
    """Construct a bare WhisperstreamApp with only the attributes the hotkey
    callbacks read, bypassing the expensive __init__ (no model load).

    Wires up a per-call recorder on `_broadcaster` so assertions can inspect
    every event payload. `_ensure_volume_interface`, `_reduce_system_volume`,
    `_start_recording`, and `_dispatch_error_dialog` are replaced with
    MagicMocks on the instance so the tests can assert call counts.
    """
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)

    app.license_manager = MagicMock()
    app.license_manager.can_start_recording.return_value = can_start
    app.license_manager.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }

    app.audio_feedback = MagicMock()
    app.config = SimpleNamespace(
        start_sound_path=None,
        stop_sound_path=None,
        error_sound_path=None,
        system_volume_mode="off",
    )

    app._recording = False
    app._lock = threading.Lock()
    app._dictation_state_lock = threading.Lock()
    app._recording_sequence = 0
    app._dictation_state = {
        "type": "dictation.state",
        "version": _DICTATION_STATE_VERSION,
        "recording_id": 0,
        "phase": "idle",
        "origin": None,
        "hud_enabled": False,
        "preview_enabled": False,
        "screen_reader_detected": False,
    }
    app._idle_timer = None
    app._broadcaster = MagicMock()
    # Agent-speech barge-in (main.py _start_recording) reads self.speech_controller
    # as its first statement; the real __init__ always sets it (None off-gate).
    # This bypass-init fixture must set it too or the real _start_recording (used
    # by the audio-capture-failure tests) raises AttributeError.
    app.speech_controller = None

    # Replace volume helpers so we can assert they are not touched on the
    # error path. These are bound methods on the real class; override as
    # per-instance MagicMocks.
    app._ensure_volume_interface = MagicMock(return_value=True)
    app._reduce_system_volume = MagicMock()

    # Replace _start_recording with a spy so tests do not drive the real
    # audio capture + license state machine.
    app._start_recording = MagicMock()

    # Replace _dispatch_error_dialog with a spy — we assert its call args.
    app._dispatch_error_dialog = MagicMock()

    return app


class TestNoPrecheckNoDevice(unittest.TestCase):
    """Test A: no input device blocks _on_hotkey_press."""

    def test_no_device_blocks_hotkey_press_start_chime(self):
        """With no input device, play_start_blocking must NOT be called."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=False):
            app._on_hotkey_press()
        app.audio_feedback.play_start_blocking.assert_not_called()

    def test_no_device_hotkey_press_plays_stop_chime(self):
        """With no input device, play_stop is called once."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=False):
            app._on_hotkey_press()
        app.audio_feedback.play_stop.assert_called_once_with(None)

    def test_no_device_hotkey_press_dispatches_error_dialog(self):
        """With no input device, _dispatch_error_dialog fires with no_input_device."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=False):
            app._on_hotkey_press()
        app._dispatch_error_dialog.assert_called_once_with(
            "no_input_device",
            "No microphone found",
            "Plug in a microphone to start recording.",
            retry_action="rescan_devices",
        )

    def test_no_device_hotkey_press_skips_volume(self):
        """With no input device, volume helpers must NOT be touched."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=False):
            app._on_hotkey_press()
        app._ensure_volume_interface.assert_not_called()
        app._reduce_system_volume.assert_not_called()

    def test_no_device_hotkey_press_skips_start_recording(self):
        """With no input device, _start_recording must NOT be called."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=False):
            app._on_hotkey_press()
        app._start_recording.assert_not_called()


class TestNoPrecheckNoDeviceToggle(unittest.TestCase):
    """Test B: no input device blocks _on_toggle_start."""

    def test_no_device_blocks_toggle_start_start_chime(self):
        """With no input device, play_start_blocking must NOT be called."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=False):
            app._on_toggle_start()
        app.audio_feedback.play_start_blocking.assert_not_called()

    def test_no_device_toggle_start_plays_stop_chime(self):
        """With no input device, play_stop is called once."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=False):
            app._on_toggle_start()
        app.audio_feedback.play_stop.assert_called_once_with(None)

    def test_no_device_toggle_start_dispatches_error_dialog(self):
        """With no input device, _dispatch_error_dialog fires with no_input_device."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=False):
            app._on_toggle_start()
        app._dispatch_error_dialog.assert_called_once_with(
            "no_input_device",
            "No microphone found",
            "Plug in a microphone to start recording.",
            retry_action="rescan_devices",
        )

    def test_no_device_toggle_start_skips_volume(self):
        """With no input device, volume helpers must NOT be touched."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=False):
            app._on_toggle_start()
        app._ensure_volume_interface.assert_not_called()
        app._reduce_system_volume.assert_not_called()

    def test_no_device_toggle_start_skips_start_recording(self):
        """With no input device, _start_recording must NOT be called."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=False):
            app._on_toggle_start()
        app._start_recording.assert_not_called()


class TestHappyPathWithDevice(unittest.TestCase):
    """Test C: input device present allows recording (happy path)."""

    def test_device_present_hotkey_press_runs_happy_path(self):
        """With can_start=True and has_input_device=True, the full flow runs."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=True):
            app._on_hotkey_press()

        # The real _start_recording now owns the cue after the stream opens.
        app.audio_feedback.play_start_blocking.assert_not_called()
        app._start_recording.assert_called_once()
        app._dispatch_error_dialog.assert_not_called()

    def test_device_present_toggle_start_runs_happy_path(self):
        """With can_start=True and has_input_device=True, _on_toggle_start runs the full flow."""
        app = _make_app(can_start=True)
        with patch("whisperstream.main.has_input_device", return_value=True):
            app._on_toggle_start()

        app.audio_feedback.play_start_blocking.assert_not_called()
        app._start_recording.assert_called_once()
        app._dispatch_error_dialog.assert_not_called()


class TestAudioCaptureStartFailure(unittest.TestCase):
    """Test D: audio_capture.start() failure dispatches popup (Task 1.5)."""

    def _make_app_for_start_recording(self, *, capture_raises: Exception | None = None) -> WhisperstreamApp:
        """Extend _make_app with the extra attributes _start_recording reads.

        `license_manager.can_start_recording` is set to True so the test
        reaches the audio_capture branch (Task 1.5 spec: has_input_device
        -> True, so the precheck passes, then audio_capture.start() raises).

        `_start_recording` is NOT mocked here — we call the real method so the
        except block under test actually executes.  All other side-effecting
        helpers are mocked.
        """
        app = _make_app(can_start=True)

        # Restore _start_recording to the real implementation so the except
        # block executes (Task 1.5 tests _start_recording directly).
        del app._start_recording  # remove the per-instance mock from Task 1.4

        # Extra attributes _start_recording accesses.
        audio_capture = MagicMock()
        if capture_raises is not None:
            audio_capture.start.side_effect = capture_raises
        app.audio_capture = audio_capture

        app._chunk_futures = []
        app._chunk_start_time = 0.0
        app._last_waveform = None
        app._executor = MagicMock()
        app.stt_engine = MagicMock()
        app._broadcast = MagicMock()

        return app

    def test_capture_start_failure_plays_stop_not_error(self):
        """audio_capture.start() raising OSError must call play_stop, NOT play_error."""
        app = self._make_app_for_start_recording(capture_raises=OSError("no device"))
        with patch("whisperstream.main.has_input_device", return_value=True):
            app._start_recording()
        app.audio_feedback.play_stop.assert_called_once_with(None)
        app.audio_feedback.play_error.assert_not_called()

    def test_capture_start_failure_dispatches_error_dialog(self):
        """audio_capture.start() raising OSError must call _dispatch_error_dialog."""
        app = self._make_app_for_start_recording(capture_raises=OSError("no device"))
        with patch("whisperstream.main.has_input_device", return_value=True):
            app._start_recording()
        app._dispatch_error_dialog.assert_called_once_with(
            "audio_capture_failed",
            "Couldn't start recording",
            "Check that your microphone is connected, then try again.",
            retry_action="restart_capture",
        )

    def test_capture_start_failure_no_legacy_error_broadcast(self):
        """audio_capture.start() raising OSError must NOT broadcast legacy error type."""
        app = self._make_app_for_start_recording(capture_raises=OSError("no device"))
        with patch("whisperstream.main.has_input_device", return_value=True):
            app._start_recording()
        # Inspect every call to _broadcast — none should be {"type": "error", ...}
        for call in app._broadcast.call_args_list:
            payload = call.args[0] if call.args else {}
            self.assertNotEqual(
                payload.get("type"),
                "error",
                f"Legacy error broadcast found in _broadcast call: {payload}",
            )

    def test_capture_start_failure_clears_arming_state(self):
        app = self._make_app_for_start_recording(capture_raises=OSError("no device"))
        with patch("whisperstream.main.has_input_device", return_value=True):
            app._start_recording()

        payloads = [call.args[0] for call in app._broadcast.call_args_list]
        self.assertEqual(payloads[-1], {"type": "state.arming", "active": False})

    def test_capture_start_failure_clears_prior_routing_target_without_probe(self):
        app = self._make_app_for_start_recording(capture_raises=OSError("no device"))
        app._dictation_app = RoutingTarget("private.exe", "private.example")
        with patch(
            "whisperstream.main.detect_foreground_routing_target"
        ) as probe:
            self.assertFalse(app._start_recording())
        self.assertIsNone(app._dictation_app)
        probe.assert_not_called()


class TestStartRecordingModelNotReady(unittest.TestCase):
    """Recording must not crash — and must still show the HUD — when the STT
    model is not yet loaded (`self.stt_engine is None`).

    Regression for the missing-model HUD-suppression bug: `_start_recording`
    kicked `self.stt_engine.ensure_loaded` with no None guard, so a not-yet-prepared
    model (fresh install before download, or a data dir whose model went missing —
    e.g. a stale symlink) raised `AttributeError` on the hotkey thread BEFORE the
    `state.recording` broadcast, so the frontend HUD was never summoned. With the
    guard, recording starts and broadcasts `state.recording`; transcription is
    skipped by the existing downstream guards in `_on_chunk_ready` /
    `_finalize_transcription_and_inject`.

    Reuses the `_make_app` fixture + the real-`_start_recording` pattern from
    `TestAudioCaptureStartFailure`.
    """

    def _make_app_model(self, parakeet) -> WhisperstreamApp:
        app = _make_app(can_start=True)
        del app._start_recording  # exercise the real method

        app.audio_capture = MagicMock()  # start() succeeds (returns a MagicMock)
        app._chunk_futures = []
        app._chunk_start_time = 0.0
        app._last_waveform = None
        app._executor = MagicMock()
        app.stt_engine = parakeet
        app._broadcast = MagicMock()
        app._started_via_toggle = False
        app.llama_server = None
        app._server_loop = None
        # Extra config fields _start_recording reads beyond the precheck set.
        # enhancement_enabled=False short-circuits the local-LLM warm block (and
        # its effective_provider() call) before it is reached.
        app.config.enhancement_enabled = False
        app.config.toggle_safety_timeout = 0
        return app

    def _run(self, app: WhisperstreamApp) -> None:
        with patch("whisperstream.main.has_input_device", return_value=True), patch(
            "whisperstream.main.detect_foreground_routing_target", return_value=None
        ), patch("whisperstream.main.screen_reader_active", return_value=False):
            app._start_recording()

    def test_model_none_does_not_raise_and_records(self):
        """parakeet is None must NOT raise (no AttributeError) and still record."""
        app = self._make_app_model(None)
        self._run(app)  # pre-fix: AttributeError on self.stt_engine.ensure_loaded
        self.assertTrue(app._recording)

    def test_model_none_skips_ensure_loaded(self):
        """parakeet is None must skip the ensure_loaded warm-up submit."""
        app = self._make_app_model(None)
        self._run(app)
        app._executor.submit.assert_not_called()

    def test_model_none_still_broadcasts_state_recording(self):
        """The HUD-summoning state.recording broadcast must still fire when the
        model is absent — that is the whole point of the guard."""
        app = self._make_app_model(None)
        self._run(app)
        types = [c.args[0].get("type") for c in app._broadcast.call_args_list if c.args]
        self.assertIn("state.recording", types)

    def test_model_present_still_submits_ensure_loaded(self):
        """When the model IS present, the warm-up submit still happens (guard is
        a None-check, not a behavior change for the normal path)."""
        parakeet = MagicMock()
        app = self._make_app_model(parakeet)
        self._run(app)
        app._executor.submit.assert_called_once_with(parakeet.ensure_loaded)


if __name__ == "__main__":
    unittest.main()
