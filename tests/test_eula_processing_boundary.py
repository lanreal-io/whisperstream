"""Focused denial and revocation tests for the EULA processing boundary."""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import whisperstream.main as main_mod
import whisperstream.text_injector as injector_mod
from whisperstream.eula import AcceptanceGrant, AcceptanceGrantReason
from whisperstream.main import WhisperstreamApp, _DictationFinalizationContext
from whisperstream.text_injector import DeliveryStatus, TextInjector


ACCEPTED = AcceptanceGrant(
    True,
    AcceptanceGrantReason.ACCEPTED,
    "1.3",
    "a" * 64,
    "b" * 64,
)
BLOCKED = AcceptanceGrant(False, AcceptanceGrantReason.ACCEPTANCE_MISSING)
BLOCK_EVENT = {
    "type": "recording.blocked",
    "reason": "eula_acceptance_required",
}


def _denied_app() -> WhisperstreamApp:
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app._broadcast = MagicMock()
    app._acceptance_grant_provider = lambda: BLOCKED
    app._acceptance_grant_revalidator = lambda _grant: False
    app._lock = threading.Lock()
    app._start_recording_for_origin = MagicMock()
    app._agent_reply_router = MagicMock()
    app.text_injector = MagicMock()
    app.config = SimpleNamespace(input_style="clipboard")
    return app


@pytest.mark.parametrize(
    "invoke",
    [
        lambda app: app._on_hotkey_press(),
        lambda app: app._on_toggle_start(),
        lambda app: app._on_agent_enter_press(),
        lambda app: app._on_paste_last_press(),
        lambda app: app._on_paste_last_original_press(),
        lambda app: app._on_claude_view_press(),
        lambda app: app.start_hud_dictation(),
        lambda app: app._start_routed_reply("push_to_talk"),
    ],
)
def test_denied_entry_points_emit_only_content_free_block(invoke):
    app = _denied_app()

    invoke(app)

    app._broadcast.assert_called_once_with(BLOCK_EVENT)
    app._start_recording_for_origin.assert_not_called()
    app._agent_reply_router.start_routed_reply.assert_not_called()
    app._agent_reply_router.submit_ready_response_draft.assert_not_called()
    app.text_injector.paste_last.assert_not_called()
    app.text_injector.paste_last_original.assert_not_called()


@pytest.mark.parametrize(
    ("invoke", "origin"),
    [
        (lambda app: app._on_hotkey_press(), "push_to_talk"),
        (lambda app: app._on_toggle_start(), "toggle"),
    ],
)
def test_exact_accepted_state_preserves_primary_recording_entry_points(
    invoke, origin
):
    """The shared boundary must not turn into a blanket hotkey denial."""
    app = _denied_app()
    app._acceptance_grant_provider = lambda: ACCEPTED
    app._acceptance_grant_revalidator = lambda grant: grant == ACCEPTED
    app._agent_reply_router.start_routed_reply.return_value = False

    invoke(app)

    app._start_recording_for_origin.assert_called_once_with(origin)
    app._broadcast.assert_not_called()


def test_transcript_store_is_deferred_until_exact_acceptance(monkeypatch):
    monkeypatch.setattr(main_mod.sys, "platform", "win32")
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app._headless = True
    app._transcript_store_lock = threading.Lock()
    app._transcript_store_initialized = False
    app.transcript_store = None
    app.config = SimpleNamespace(mac_history_opt_in=True)
    current = {"grant": BLOCKED}
    app._acceptance_grant_provider = lambda: current["grant"]
    app._acceptance_grant_revalidator = lambda grant: (
        current["grant"] == ACCEPTED and grant == ACCEPTED
    )
    store = MagicMock()
    make_store = MagicMock(return_value=store)
    sweep = MagicMock()
    monkeypatch.setattr(main_mod, "TranscriptStore", make_store)
    monkeypatch.setattr(main_mod, "run_retention_sweep", sweep)

    assert app.initialize_accepted_content_services() is False
    make_store.assert_not_called()
    sweep.assert_not_called()

    current["grant"] = ACCEPTED
    assert app.initialize_accepted_content_services() is True
    assert app.initialize_accepted_content_services() is True
    make_store.assert_called_once_with()
    sweep.assert_called_once_with(app.config, store)


def test_revoked_grant_cancels_finalization_before_stt():
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app._broadcast = MagicMock()
    app._acceptance_grant_revalidator = lambda _grant: False
    future: concurrent.futures.Future[str] = concurrent.futures.Future()
    stt = MagicMock()
    context = _DictationFinalizationContext(
        recording_id=7,
        waveform=np.ones(32, dtype=np.float32),
        chunk_futures=(future,),
        transcription_started_at=1.0,
        recording_started_at=1.0,
        stt_manager=stt,
        dictation_app=None,
        manual_profile_id=None,
        override_anchor=None,
        screen_reader_detected=False,
        inject_suppressed=False,
        agent_compose_lease=None,
        acceptance_grant=ACCEPTED,
    )

    app._finalize_transcription_and_inject(context)

    assert future.cancelled()
    stt.transcribe.assert_not_called()
    app._broadcast.assert_called_once_with(BLOCK_EVENT)


class _QueuedExecutor:
    def __init__(self) -> None:
        self.callback = None

    def submit(self, callback):
        self.callback = callback
        return MagicMock()


def test_revocation_after_persist_queue_prevents_audio_and_store_work(monkeypatch):
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app._broadcast = MagicMock()
    current = {"allowed": True}
    app._acceptance_grant_revalidator = lambda grant: (
        current["allowed"] and grant == ACCEPTED
    )
    app.config = SimpleNamespace(
        history_enabled=True,
        mac_history_opt_in=True,
        save_audio=True,
        parakeet_model="test-model",
        lockdown_mode=False,
    )
    app.transcript_store = MagicMock()
    app._executor = _QueuedExecutor()
    waveform = np.ones(32, dtype=np.float32)
    app._last_waveform = waveform
    compute_peaks = MagicMock()
    monkeypatch.setattr(main_mod, "compute_peaks", compute_peaks)

    app._persist_transcript(
        raw_text="private text",
        enhanced_text=None,
        duration=1.0,
        waveform=waveform,
        acceptance_grant=ACCEPTED,
    )
    assert app._executor.callback is not None

    current["allowed"] = False
    app._executor.callback()

    compute_peaks.assert_not_called()
    app.transcript_store.save.assert_not_called()
    app._broadcast.assert_called_once_with(BLOCK_EVENT)
    assert app._last_waveform is None


def test_text_injector_denial_touches_no_clipboard_keyboard_or_memory(monkeypatch):
    keyboard = MagicMock()
    clipboard_copy = MagicMock()
    clipboard_paste = MagicMock(return_value="prior")
    monkeypatch.setattr(injector_mod.pyperclip, "copy", clipboard_copy)
    monkeypatch.setattr(injector_mod.pyperclip, "paste", clipboard_paste)
    injector = TextInjector(
        keyboard_manager=keyboard,
        logger=MagicMock(spec=logging.Logger),
        word_filters=[],
        word_replacements={},
        post_processing="",
        clipboard_behavior=True,
        clipboard_clear_delay=0.1,
        content_allowed=lambda: False,
    )
    injector._last_transcript = "stored private text"

    assert injector.inject("new private text").status is DeliveryStatus.BLOCKED
    assert injector.paste_last().status is DeliveryStatus.BLOCKED
    injector.remember_original("raw private text")

    assert injector.last_original is None
    clipboard_copy.assert_not_called()
    clipboard_paste.assert_not_called()
    keyboard.snapshot_held_modifiers.assert_not_called()


@pytest.mark.parametrize("completion", ["ready", "revoked", "shutdown"])
def test_macos_history_wait_does_not_block_acceptance_or_publish_after_revocation(monkeypatch, completion):
    app = object.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app._headless = True
    app._file_only = False
    app._shutting_down = False
    app._transcript_store_lock = threading.Lock()
    app._transcript_store_start_lock = threading.Lock()
    app._transcript_store_publish_lock = threading.Lock()
    app._transcript_store_initialized = False
    app._transcript_store_init_thread = None
    app.transcript_store = None
    app.config = SimpleNamespace(mac_history_opt_in=True)
    app._broadcast = MagicMock()
    current = {"grant": ACCEPTED}
    app._acceptance_grant_provider = lambda: current["grant"]
    app._acceptance_grant_revalidator = lambda grant: grant == ACCEPTED and current["grant"] == ACCEPTED
    entered, release = threading.Event(), threading.Event()
    store = MagicMock()

    def blocked_store():
        entered.set()
        assert release.wait(3), "initializer was awaited on the caller"
        return store

    make_store = MagicMock(side_effect=blocked_store)
    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    monkeypatch.setattr(main_mod, "TranscriptStore", make_store)
    sweep = MagicMock()
    monkeypatch.setattr(main_mod, "run_retention_sweep", sweep)
    try:
        assert app.initialize_accepted_content_services() is True
        assert entered.wait(1)
        assert app.history_initializing
        assert app.transcript_store is None
        # A second acceptance/startup call must neither wait nor create a worker.
        assert app.initialize_accepted_content_services() is True
        make_store.assert_called_once()
        if completion == "revoked":
            current["grant"] = BLOCKED
        elif completion == "shutdown":
            app._shutting_down = True
    finally:
        release.set()
        app._transcript_store_init_thread.join(2)
    assert not app.history_initializing
    if completion == "ready":
        assert app.transcript_store is store
        sweep.assert_called_once_with(app.config, store)
        app._broadcast.assert_called_once_with({"type": "transcripts.changed"})
        store.close.assert_not_called()
    else:
        assert app.transcript_store is None
        store.close.assert_called_once()
        sweep.assert_not_called()
        app._broadcast.assert_not_called()
