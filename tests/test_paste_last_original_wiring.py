"""Tests for Task 1.4: paste-last-original wiring in main.py.

Verifies:
- After a single-call dictation with enhancement that rewrites the text,
  `text_injector.last_original` holds `process(raw_text)` (the pre-enhancement
  text through the normal pipeline) while `last_transcript` holds the enhanced
  processed text.
- `remember_original` is NOT called when `_inject_suppressed` is True (mirrors
  when `_last_transcript` is set).
- `_register_hotkey` registers a `paste_last_original` entry in `keyboard._extra_hotkeys`
  when the shortcut is set.
- `reload_config` triggers re-registration when only `paste_last_original_shortcut`
  changes.

Test construction mirrors test_main_chunked_cleanup.py: use WhisperstreamApp.__new__
to bypass __init__ and wire only the collaborators the methods touch.
"""
from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from whisperstream.enhancement_outcomes import EnhancementResult
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.main import WhisperstreamApp

pytestmark = pytest.mark.usefixtures("accepted_eula")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(
    *,
    enhancement_enabled: bool = True,
    inject_suppressed: bool = False,
    paste_last_original_shortcut: str = "",
    paste_last_shortcut: str = "ctrl+alt+v",
):
    """WhisperstreamApp stub wired with just what the wiring tests touch."""
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app.audio_feedback = MagicMock()
    app.stt_engine = MagicMock()
    app.text_injector = MagicMock()
    # process() prepends "PROCESSED:" so we can distinguish raw vs enhanced.
    app.text_injector.process.side_effect = lambda text, **kw: f"PROCESSED:{text}"
    app.text_injector.last_transcript = None
    app.config = WhisperstreamConfig(
        enhancement_enabled=enhancement_enabled,
        enhancement_provider="local",
        enhancement_local_model="gemma4-e2b-instruct",
        enhancement_chunked_cleanup=True,
        default_profile_id="chat",
        profiles=[{"id": "chat", "name": "Chat", "prompt": "p"}],
        paste_last_shortcut=paste_last_shortcut,
        paste_last_original_shortcut=paste_last_original_shortcut,
    )
    app._inject_suppressed = inject_suppressed
    app._broadcast = MagicMock()
    app.review_state = MagicMock()
    app.license_manager = MagicMock()
    app.transcript_store = None
    app._last_waveform = None
    # Power Mode state
    app._dictation_app = None
    app._manual_profile_id = None
    app._override_anchor = None
    return app


# ---------------------------------------------------------------------------
# Single-call path: remember_original called only when not suppressed
# ---------------------------------------------------------------------------

class TestSinglePathRememberOriginal:
    def test_remember_original_called_with_raw_text(self):
        """After a single-call dictation, remember_original is called with the
        raw pre-enhancement text."""
        app = _make_app(enhancement_enabled=True)

        # Stub the enhancer to rewrite text.
        enhancer = MagicMock()
        enhancer.will_enhance.return_value = True
        enhancer.enhance_with_outcome = AsyncMock(
            return_value=EnhancementResult("Enhanced output.", "success")
        )
        app.llm_enhancer = enhancer

        app._enhance_then_inject("raw dictation text", 1.0)

        # remember_original must have been called with the raw input.
        app.text_injector.remember_original.assert_called_once_with("raw dictation text")

    def test_remember_original_not_called_when_suppressed(self):
        """When _inject_suppressed is True (onboarding test mode), remember_original
        must NOT be called — mirrors when _last_transcript is set."""
        app = _make_app(enhancement_enabled=False, inject_suppressed=True)

        app._enhance_then_inject("onboarding text", 0.5)

        app.text_injector.remember_original.assert_not_called()
        app.text_injector.inject.assert_not_called()

    def test_remember_original_called_after_inject(self):
        """remember_original is called after inject() in the non-suppressed branch."""
        app = _make_app(enhancement_enabled=False)
        call_order = []
        # inject now takes an input_style kwarg (keystroke-input merge); accept any args.
        app.text_injector.inject.side_effect = lambda *a, **k: call_order.append("inject")
        app.text_injector.remember_original.side_effect = lambda *a, **k: call_order.append("remember")

        app._enhance_then_inject("hello", 0.5)

        assert call_order == ["inject", "remember"]

    def test_last_original_is_raw_while_last_transcript_is_enhanced(self):
        """Simulate the semantics: after an enhanced dictation, last_original holds
        process(raw_text) and last_transcript holds the enhanced processed text.

        We verify this by checking the args passed to remember_original (raw) and
        inject (enhanced); the injector's side-effects would set those slots in
        the real implementation — here we just assert the right args arrive.
        """
        raw_text = "hello world"
        enhanced_text = "Hello, world."

        app = _make_app(enhancement_enabled=True)
        enhancer = MagicMock()
        enhancer.will_enhance.return_value = True
        enhancer.enhance_with_outcome = AsyncMock(
            return_value=EnhancementResult(enhanced_text, "success")
        )
        app.llm_enhancer = enhancer

        app._enhance_then_inject(raw_text, 1.0)

        # inject receives the enhanced text as its first positional arg (it also
        # gets an input_style kwarg post keystroke-input merge — don't constrain it).
        app.text_injector.inject.assert_called_once()
        assert app.text_injector.inject.call_args.args == (enhanced_text,)
        # remember_original receives the raw text (pre-enhancement).
        app.text_injector.remember_original.assert_called_once_with(raw_text)


# ---------------------------------------------------------------------------
# Chunked path: remember_original called with raw input text
# ---------------------------------------------------------------------------

class TestChunkedPathRememberOriginal:
    def _make_chunked_app(self):
        from whisperstream.main import _CHUNK_THRESHOLD_WORDS
        # Build text long enough to exceed the chunked threshold.
        text = " ".join(f"word{i}" for i in range(_CHUNK_THRESHOLD_WORDS + 50))

        app = _make_app(enhancement_enabled=True)
        enhancer = MagicMock()
        enhancer.will_enhance.return_value = True
        enhancer.enhance_chunk_with_outcome = AsyncMock(
            side_effect=lambda c, **kw: EnhancementResult(c, "success")
        )
        app.llm_enhancer = enhancer
        return app, text

    def test_remember_original_called_with_raw_text_chunked(self):
        """After a chunked dictation, remember_original is called with the raw
        input text (the full pre-enhancement ASR text)."""
        app, text = self._make_chunked_app()

        app._enhance_then_inject_chunked(text, 2.0)

        app.text_injector.remember_original.assert_called_once_with(text)


# ---------------------------------------------------------------------------
# _register_hotkey: paste_last_original entry registered when shortcut set
# ---------------------------------------------------------------------------

@pytest.mark.skipif(__import__("sys").platform != "win32", reason="This fixture exercises the Windows pynput owner; portable registration is covered by the native adapter suites.")
class TestRegisterHotkeyOriginalSlot:
    def _make_app_with_keyboard(self, shortcut: str):
        """App with a real (but listener-less) KeyboardShortcutManager."""
        import logging as _logging
        from whisperstream.keyboard_shortcuts import KeyboardShortcutManager

        app = _make_app(paste_last_original_shortcut=shortcut)
        app.keyboard = KeyboardShortcutManager(
            logger=_logging.getLogger("test.register_hotkey")
        )
        # Stub the methods that would start the real OS listener.
        app.keyboard._start_listener = MagicMock()
        # Wire methods _register_hotkey calls that would blow up without a real app.
        app._register_profile_hotkeys = MagicMock()
        return app

    def test_paste_last_original_registered_when_shortcut_set(self):
        """When paste_last_original_shortcut is non-empty, _register_hotkey adds
        a 'paste_last_original' entry to keyboard._extra_hotkeys."""
        app = self._make_app_with_keyboard("ctrl+alt+o")
        # Also set a non-empty primary shortcut so PTT registration proceeds.
        app.config = WhisperstreamConfig(
            push_to_talk_shortcut="right shift",
            paste_last_shortcut="ctrl+alt+v",
            paste_last_original_shortcut="ctrl+alt+o",
        )

        app._register_hotkey()

        assert "paste_last_original" in app.keyboard._extra_hotkeys
        entry = app.keyboard._extra_hotkeys["paste_last_original"]
        # Bound method identity: compare the underlying function, not the wrapper.
        assert entry["on_press"].__func__ is WhisperstreamApp._on_paste_last_original_press

    def test_paste_last_original_not_registered_when_empty(self):
        """When paste_last_original_shortcut is empty (default), the slot is
        absent from keyboard._extra_hotkeys (graceful unbind, no raise)."""
        app = self._make_app_with_keyboard("")
        app.config = WhisperstreamConfig(
            push_to_talk_shortcut="right shift",
            paste_last_shortcut="ctrl+alt+v",
            paste_last_original_shortcut="",
        )

        app._register_hotkey()

        assert "paste_last_original" not in app.keyboard._extra_hotkeys

    def test_both_paste_hotkeys_registered_independently(self):
        """When both paste shortcut fields are set, both entries appear in
        keyboard._extra_hotkeys under their respective keys."""
        app = self._make_app_with_keyboard("ctrl+alt+o")
        app.config = WhisperstreamConfig(
            push_to_talk_shortcut="right shift",
            paste_last_shortcut="ctrl+alt+v",
            paste_last_original_shortcut="ctrl+alt+o",
        )

        app._register_hotkey()

        assert "paste_last" in app.keyboard._extra_hotkeys
        assert "paste_last_original" in app.keyboard._extra_hotkeys


# ---------------------------------------------------------------------------
# reload_config: re-registration triggered when paste_last_original_shortcut changes
# ---------------------------------------------------------------------------

class TestReloadConfigTriggersReregistration:
    def _make_app_for_reload(self):
        """App stub for reload_config tests."""
        app = WhisperstreamApp.__new__(WhisperstreamApp)
        app.logger = MagicMock(spec=logging.Logger)
        app.audio_feedback = MagicMock()
        app.audio_capture = MagicMock()
        app.review_state = MagicMock()
        app.license_manager = MagicMock()
        app.transcript_store = None
        app._manual_profile_id = None
        app._inject_suppressed = False
        app._lock = threading.Lock()
        app._loaded_stt_artifact_id = None
        app._loaded_stt_routing_state = None
        app._stt_activation_rollback = None
        app._broadcast = MagicMock()
        # Mock keyboard and _register_hotkey so we can assert on calls.
        app.keyboard = MagicMock()
        app._register_hotkey = MagicMock()
        app._register_profile_hotkeys = MagicMock()
        app.stt_engine = MagicMock()
        app.llm_enhancer = MagicMock()
        app.llama_server = None
        app._server_loop = None
        return app

    def test_reload_config_rereg_when_paste_original_shortcut_changes(self):
        """reload_config must call keyboard.unhook_all() + _register_hotkey() when
        only paste_last_original_shortcut changes."""
        app = self._make_app_for_reload()
        app.config = WhisperstreamConfig(
            push_to_talk_shortcut="right shift",
            paste_last_shortcut="ctrl+alt+v",
            paste_last_original_shortcut="",
        )

        new_config = WhisperstreamConfig(
            push_to_talk_shortcut="right shift",
            paste_last_shortcut="ctrl+alt+v",
            paste_last_original_shortcut="ctrl+alt+o",
        )

        app.reload_config(new_config)

        app.keyboard.unhook_all.assert_called_once()
        app._register_hotkey.assert_called_once()
