"""Tests for the inject-suppression toggle used by the onboarding test step.

The onboarding flow needs to show transcribed text inside a React input on
step 3 instead of typing it into the currently focused OS window. The mechanism
is a `_inject_suppressed` flag on `WhisperstreamApp` that gates the
`text_injector.inject()` call in `_finalize_transcription_and_inject` WITHOUT affecting
the `transcription.result` broadcast — the frontend still receives the text
via WebSocket and displays it via React state.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from whisperstream.main import WhisperstreamApp

pytestmark = pytest.mark.usefixtures("accepted_eula")


def _make_app() -> WhisperstreamApp:
    """Construct a WhisperstreamApp without running the heavy __init__.

    We bypass __init__ via __new__ and wire up just the attributes that
    `_finalize_transcription_and_inject` touches: logger, audio_feedback, parakeet,
    text_injector, config, _broadcast, _inject_suppressed, _chunk_futures,
    _chunk_start_time, _last_waveform.
    """
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app.audio_feedback = MagicMock()
    app.stt_engine = MagicMock()
    # The no-chunk finalize fallback transcribes the full waveform through the
    # chunked entry point (manager.transcribe splits + merges); stub both the
    # full-waveform and per-chunk methods.
    app.stt_engine.transcribe.return_value = "hello world"
    app.stt_engine.transcribe_chunk.return_value = "hello world"
    app.text_injector = MagicMock()
    app.text_injector.process.return_value = "hello world"
    app.config = SimpleNamespace(
        language="en",
        error_sound_path=None,
        enhancement_enabled=False,
        # Power Mode (round 04) fields the merged inject path reads.
        default_profile_id="chat",
        power_mode_map={},
        site_map={},
        profiles=[{"id": "chat", "name": "Chat", "prompt": "x"}],
        # keystroke-input (Task 1.5): effective-style resolution reads these.
        # user_set=True pins the clipboard choice so resolution is deterministic
        # regardless of whether a screen reader is running on the test host
        # (otherwise live SR detection would flip the effective style to keystroke).
        input_style="clipboard",
        input_style_user_set=True,
    )
    app._broadcast = MagicMock()
    app._inject_suppressed = False
    app._chunk_futures = []
    app._chunk_start_time = 0.0
    app._last_waveform = None
    app.review_state = MagicMock()
    # Round-04 app state the merged inject path reads (issue 14 override + #159
    # app-identity + #184 trial clock + #183 persistence); __new__ skips __init__.
    app._dictation_app = None
    app._manual_profile_id = None
    app._override_anchor = None
    app.license_manager = MagicMock()
    app.transcript_store = None
    return app


def test_inject_runs_when_not_suppressed():
    app = _make_app()
    waveform = np.ones(16_000, dtype=np.float32)
    app._last_waveform = waveform

    app._finalize_transcription_and_inject()

    # Ordinary dictation never requests auto-submit.
    app.text_injector.inject.assert_called_once_with(
        "hello world", input_style="clipboard", submit=False
    )
    # transcription.result should have been broadcast
    broadcast_types = [call.args[0].get("type") for call in app._broadcast.call_args_list]
    assert "transcription.result" in broadcast_types


def test_inject_suppressed_skips_inject_but_still_broadcasts():
    app = _make_app()
    waveform = np.ones(16_000, dtype=np.float32)
    app._last_waveform = waveform

    # First call: not suppressed, inject should fire.
    app._finalize_transcription_and_inject()
    assert app.text_injector.inject.call_count == 1

    # Flip the flag via the setter.
    app.set_inject_suppressed(True)
    assert app._inject_suppressed is True

    # Second call: suppressed, inject must NOT fire.
    # A new recording owns a new waveform; Phase 3 intentionally released the
    # prior recording's mutable app reference after its terminal path.
    app._last_waveform = waveform.copy()
    app._finalize_transcription_and_inject()
    assert app.text_injector.inject.call_count == 1, (
        "text_injector.inject should NOT be called when _inject_suppressed is True"
    )

    # But transcription.result must have been broadcast BOTH times — the
    # frontend still needs to receive the text via WebSocket.
    result_broadcasts = [
        call.args[0]
        for call in app._broadcast.call_args_list
        if call.args[0].get("type") == "transcription.result"
    ]
    assert len(result_broadcasts) == 2, (
        f"Expected 2 transcription.result broadcasts, got {len(result_broadcasts)}"
    )
    for payload in result_broadcasts:
        assert payload.get("text") == "hello world"


def test_set_inject_suppressed_toggles_flag():
    app = _make_app()
    assert app._inject_suppressed is False

    app.set_inject_suppressed(True)
    assert app._inject_suppressed is True

    app.set_inject_suppressed(False)
    assert app._inject_suppressed is False
