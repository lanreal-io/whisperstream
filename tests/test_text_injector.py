import logging
import unittest
from unittest.mock import MagicMock, call, patch

from pynput.keyboard import Key


_PLATFORM_PATCHER = None


def setUpModule() -> None:
    """Exercise the established Windows adapter deterministically on every host."""

    global _PLATFORM_PATCHER
    _PLATFORM_PATCHER = patch(
        "whisperstream.text_injector.platform.system",
        return_value="Windows",
    )
    _PLATFORM_PATCHER.start()


def tearDownModule() -> None:
    if _PLATFORM_PATCHER is not None:
        _PLATFORM_PATCHER.stop()


class TestTextInjector(unittest.TestCase):
    def setUp(self):
        self.mock_keyboard = MagicMock()
        self.mock_keyboard._pressed = set()
        self.mock_keyboard.snapshot_held_modifiers = MagicMock(return_value=[])
        self.mock_keyboard._controller = MagicMock()
        # By default the real-OS-state check mirrors _pressed, so tests that
        # mutate _pressed to simulate a release drive the re-press gate as
        # before. Tests that need the mirror and the OS state to DIVERGE (the
        # stuck-modifier bug) override this side_effect explicitly.
        self.mock_keyboard.modifier_physically_down = MagicMock(
            side_effect=lambda k: k in self.mock_keyboard._pressed
        )
        self.mock_logger = MagicMock(spec=logging.Logger)

    def _create_injector(
        self,
        clipboard_behavior=True,
        post_processing="",
        clipboard_clear_delay=0.1,
    ):
        """Create injector with mocked dependencies."""
        from whisperstream.text_injector import TextInjector

        return TextInjector(
            keyboard_manager=self.mock_keyboard,
            logger=self.mock_logger,
            word_filters=[],
            word_replacements={},
            post_processing=post_processing,
            clipboard_behavior=clipboard_behavior,
            clipboard_clear_delay=clipboard_clear_delay,
        )

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    def test_windows_admitted_snapshot_restores_after_live_disable(
        self, clipboard, timer_factory
    ):
        from types import SimpleNamespace

        from whisperstream.delivery_contract import DeliveryStyle
        from whisperstream.windows_delivery_adapter import WindowsDeliveryAdapter

        clipboard.paste.return_value = "synthetic-prior-clipboard"
        injector = self._create_injector(clipboard_clear_delay=0.4)
        adapter = injector._delivery
        self.assertIsInstance(adapter, WindowsDeliveryAdapter)
        preparation = adapter._prepare_delivery(
            SimpleNamespace(style=DeliveryStyle.CLIPBOARD, restore_clipboard=True)
        )
        injector.update_clipboard_settings(behavior=False, clear_delay=0.2)
        adapter._schedule_clipboard_restore(preparation.saved_clipboard)
        delay, restore = timer_factory.call_args.args
        self.assertEqual(delay, 0.2)
        timer_factory.return_value.start.assert_called_once()
        restore()
        clipboard.copy.assert_called_once_with("synthetic-prior-clipboard")
        self.assertEqual(adapter.pending_restore_count, 0)
        injector.close()

    def test_live_clipboard_settings_keep_remembered_text_and_adapter(self):
        injector = self._create_injector()
        adapter = injector._delivery
        injector._last_transcript = "synthetic-completed"
        injector.remember_original("synthetic-original")
        original = injector.last_original
        injector.update_clipboard_settings(behavior=False, clear_delay=0.3)
        self.assertIs(injector._delivery, adapter)
        self.assertEqual(injector.last_transcript, "synthetic-completed")
        self.assertEqual(injector.last_original, original)
        self.assertFalse(injector._clipboard_behavior)
        self.assertFalse(adapter._clipboard_behavior)
        self.assertEqual(adapter._clipboard_clear_delay, 0.3)
        injector.close()

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_saves_prior_clipboard_when_behavior_true(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """When clipboard_behavior=True, inject saves prior clipboard and schedules restore."""
        mock_pyperclip.paste.return_value = "prior"
        mock_timer_instance = MagicMock()
        mock_timer_cls.return_value = mock_timer_instance

        injector = self._create_injector(clipboard_behavior=True)
        injector.inject("hello")

        # Our text was copied to clipboard
        mock_pyperclip.copy.assert_any_call("hello")

        # A timer was scheduled
        mock_timer_cls.assert_called_once()
        timer_args = mock_timer_cls.call_args
        # The first positional arg is the delay, the second is the callback
        callback = timer_args[0][1]

        # When the timer fires, it should restore the prior clipboard
        callback()
        mock_pyperclip.copy.assert_called_with("prior")

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_does_not_restore_when_behavior_false(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """When clipboard_behavior=False, inject does not save or schedule restore."""
        injector = self._create_injector(clipboard_behavior=False)
        injector.inject("hello")

        # Should NOT call paste (no save attempt)
        mock_pyperclip.paste.assert_not_called()

        # No timer scheduled
        mock_timer_cls.assert_not_called()

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_handles_pyperclip_paste_failure_gracefully(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """Snapshot failure selects direct typing before any clipboard write."""
        import pyperclip as real_pyperclip

        mock_pyperclip.PyperclipException = real_pyperclip.PyperclipException
        mock_pyperclip.paste.side_effect = real_pyperclip.PyperclipException(
            "no clipboard"
        )

        injector = self._create_injector(clipboard_behavior=True)
        injector.inject("hello")

        self.mock_keyboard.type_text.assert_called_once_with("hello", delay_ms=0)
        mock_pyperclip.copy.assert_not_called()
        self.mock_keyboard.send.assert_not_called()

        # No timer scheduled (nothing to restore)
        mock_timer_cls.assert_not_called()

        # Warning was logged
        self.mock_logger.warning.assert_called()

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_snapshot_failure_policy_is_shared_by_paste_last(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        mock_pyperclip.paste.side_effect = RuntimeError("synthetic unavailable")
        injector = self._create_injector(clipboard_behavior=True)

        first = injector.inject("hello")
        repeated = injector.paste_last()

        self.assertTrue(first)
        self.assertTrue(repeated)
        self.assertEqual(self.mock_keyboard.type_text.call_count, 2)
        mock_pyperclip.copy.assert_not_called()
        self.mock_keyboard.send.assert_not_called()
        mock_timer_cls.assert_not_called()

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_unpinned_clipboard_copy_failure_preserves_focused_outcome(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        import pyperclip as real_pyperclip
        from whisperstream.text_injector import DeliveryStatus

        mock_pyperclip.PyperclipException = (
            real_pyperclip.PyperclipException
        )
        mock_pyperclip.copy.side_effect = real_pyperclip.PyperclipException(
            "copy unavailable"
        )
        injector = self._create_injector(clipboard_behavior=False)

        outcome = injector.inject("hello")

        self.assertEqual(outcome.status, DeliveryStatus.FOCUSED)
        self.mock_keyboard.send.assert_not_called()
        mock_timer_cls.assert_not_called()

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_sends_shift_insert(self, mock_sleep, mock_pyperclip):
        """inject always sends Shift+Insert (universal Windows paste shortcut)."""
        injector = self._create_injector(clipboard_behavior=False)
        injector.inject("hello")

        self.mock_keyboard.send.assert_called_with("shift+insert")

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_no_held_modifiers_skips_release_dance(
        self, mock_sleep, mock_pyperclip
    ):
        """When no modifiers are held, release/press are not called."""
        self.mock_keyboard.snapshot_held_modifiers.return_value = []
        injector = self._create_injector(clipboard_behavior=False)
        injector.inject("hello")

        # send was called
        self.mock_keyboard.send.assert_called_with("shift+insert")

        # release and press were NOT called on _controller
        self.mock_keyboard._controller.release.assert_not_called()
        self.mock_keyboard._controller.press.assert_not_called()

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_releases_held_shift_around_send(self, mock_sleep, mock_pyperclip):
        """Held modifier is released before send and re-pressed after."""
        self.mock_keyboard._pressed = {Key.shift_r}
        self.mock_keyboard.snapshot_held_modifiers.return_value = [Key.shift_r]

        injector = self._create_injector(clipboard_behavior=False)
        injector.inject("hello")

        # Extract the relevant calls on the mock_keyboard (both _controller and send)
        all_calls = self.mock_keyboard.mock_calls

        # Find the indices for release, send, and press
        release_idx = None
        send_idx = None
        press_idx = None
        for i, c in enumerate(all_calls):
            if c == call._controller.release(Key.shift_r):
                release_idx = i
            elif c == call.send("shift+insert"):
                send_idx = i
            elif c == call._controller.press(Key.shift_r):
                press_idx = i

        self.assertIsNotNone(release_idx, "release(Key.shift_r) not found in calls")
        self.assertIsNotNone(send_idx, "send('ctrl+v') not found in calls")
        self.assertIsNotNone(press_idx, "press(Key.shift_r) not found in calls")

        # Assert ordering: release BEFORE send BEFORE press
        self.assertLess(
            release_idx,
            send_idx,
            "release should happen before send",
        )
        self.assertLess(
            send_idx,
            press_idx,
            "send should happen before press",
        )

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_restore_press_skipped_for_modifier_user_already_released(
        self, mock_sleep, mock_pyperclip
    ):
        """If user releases a modifier during paste, restore-press is skipped for that key."""
        self.mock_keyboard._pressed = {Key.shift_r, Key.ctrl_l}
        self.mock_keyboard.snapshot_held_modifiers.return_value = [
            Key.shift_r,
            Key.ctrl_l,
        ]

        # When send is called, simulate user releasing ctrl_l
        def send_side_effect(combo):
            self.mock_keyboard._pressed.discard(Key.ctrl_l)

        self.mock_keyboard.send.side_effect = send_side_effect

        injector = self._create_injector(clipboard_behavior=False)
        injector.inject("hello")

        # press should be called for shift_r (still held) but NOT ctrl_l (released by user)
        press_calls = self.mock_keyboard._controller.press.call_args_list
        pressed_keys = [c[0][0] for c in press_calls]
        self.assertIn(Key.shift_r, pressed_keys)
        self.assertNotIn(Key.ctrl_l, pressed_keys)

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_uses_modifier_guard_even_on_send_exception(
        self, mock_sleep, mock_pyperclip
    ):
        """If send() raises, held modifiers are still re-pressed (try/finally guarantee)."""
        self.mock_keyboard._pressed = {Key.shift_r}
        self.mock_keyboard.snapshot_held_modifiers.return_value = [Key.shift_r]
        self.mock_keyboard.send.side_effect = RuntimeError("paste failed")

        injector = self._create_injector(clipboard_behavior=False)
        injector.inject("hello")

        # Despite the exception, press should still be called to restore the modifier
        self.mock_keyboard._controller.press.assert_called_with(Key.shift_r)

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_does_not_repress_modifier_released_during_suppression(
        self, mock_sleep, mock_pyperclip
    ):
        """Regression (PTT/NVDA stuck-shift): when the tracked _pressed mirror is
        STALE -- it still lists right shift because the user's release was dropped
        while suppression was active -- the re-press must be gated on the real OS
        key state, NOT the mirror. With the key physically up, it must NOT be
        re-pressed (else it sticks down: dings, broken typing, screen-reader chaos).
        """
        # Mirror is stale: it still claims shift_r is held.
        self.mock_keyboard._pressed = {Key.shift_r}
        self.mock_keyboard.snapshot_held_modifiers.return_value = [Key.shift_r]
        # But the OS says shift_r is physically UP (user already released PTT).
        self.mock_keyboard.modifier_physically_down = MagicMock(return_value=False)

        injector = self._create_injector(clipboard_behavior=False)
        injector.inject("hello")

        # The released modifier must NOT be re-pressed.
        press_calls = [c[0][0] for c in self.mock_keyboard._controller.press.call_args_list]
        self.assertNotIn(Key.shift_r, press_calls)

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_keystroke_repress_gated_on_real_state(
        self, mock_sleep, mock_pyperclip
    ):
        """The same OS-state gate applies in keystroke mode: a genuinely-held
        modifier IS restored, and the text is typed (not pasted)."""
        self.mock_keyboard._pressed = {Key.shift_r}
        self.mock_keyboard.snapshot_held_modifiers.return_value = [Key.shift_r]
        # OS confirms shift_r is still physically down -> must be re-pressed.
        self.mock_keyboard.modifier_physically_down = MagicMock(return_value=True)

        injector = self._create_injector(clipboard_behavior=False)
        injector.inject("hello", input_style="keystroke")

        press_calls = [c[0][0] for c in self.mock_keyboard._controller.press.call_args_list]
        self.assertIn(Key.shift_r, press_calls)
        self.mock_keyboard.type_text.assert_called_once()

    def test_process_preserves_paragraph_breaks_and_normalizes_horizontal_spacing(self):
        injector = self._create_injector()

        processed = injector.process(
            "Hello Werner,\n\nFirst   thought  .\nSecond\tthought !"
            "\n\nBest regards,\nWerner"
        )

        self.assertEqual(
            processed,
            "Hello Werner,\n\nFirst thought.\nSecond thought!"
            "\n\nBest regards,\nWerner",
        )

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_keystroke_inject_preserves_paragraph_breaks(
        self, mock_sleep, mock_pyperclip
    ):
        injector = self._create_injector(clipboard_behavior=False)
        processed = injector.process("Hello Werner,\n\nFirst thought.")

        injector.inject(processed, input_style="keystroke")

        self.mock_keyboard.type_text.assert_called_once_with(
            "Hello Werner,\n\nFirst thought.", delay_ms=0
        )
        mock_pyperclip.copy.assert_not_called()


class TestTextInjectorFilters(unittest.TestCase):
    """Red-step tests for the Task 1.3 TextInjector filter/replacement refactor.

    These tests call the FUTURE constructor signature
    ``TextInjector(..., word_filters=[...], word_replacements={...}, ...)``
    and are expected to fail until Task 1.3 swaps ``word_overrides`` for the
    two new kwargs. Do not relax them to accept the legacy signature.
    """

    def setUp(self):
        self.mock_keyboard = MagicMock()
        self.mock_logger = MagicMock(spec=logging.Logger)

    def _create_injector(
        self,
        *,
        word_filters=None,
        word_replacements=None,
        post_processing="",
    ):
        """Create an injector using the future filter/replacement kwargs."""
        # Import here so any patches applied by the test are active.
        from whisperstream.text_injector import TextInjector

        return TextInjector(
            keyboard_manager=self.mock_keyboard,
            logger=self.mock_logger,
            word_filters=list(word_filters) if word_filters is not None else [],
            word_replacements=dict(word_replacements) if word_replacements is not None else {},
            post_processing=post_processing,
            clipboard_behavior=False,
            clipboard_clear_delay=0.1,
        )

    # (a) filter removes "um" and collapses surrounding whitespace + comma
    def test_filter_removes_um_with_comma_collapse(self):
        injector = self._create_injector(word_filters=["um"])
        self.assertEqual(injector.process("Hello, um, world"), "Hello, world")

    # (b) filter removes "uh" from the leading clause
    def test_filter_removes_leading_uh_clause(self):
        injector = self._create_injector(word_filters=["uh"])
        self.assertEqual(injector.process("uh, okay"), "okay")

    # (c) filter removes "er" mid-sentence and collapses the extra space
    def test_filter_removes_mid_sentence_er(self):
        injector = self._create_injector(word_filters=["er"])
        self.assertEqual(injector.process("well er fine"), "well fine")

    # (d) filter matching is case-insensitive
    def test_filter_is_case_insensitive(self):
        injector = self._create_injector(word_filters=["um"])
        self.assertEqual(injector.process("Hello, UM, world"), "Hello, world")

    # (e) filters run BEFORE replacements (so a filtered word cannot be the
    # source of a replacement)
    def test_filter_runs_before_replacement(self):
        injector = self._create_injector(
            word_filters=["um"],
            word_replacements={"um": "ummmm"},
        )
        # If replacement ran first we would see "ummmm" in the output. Filter
        # must strip "um" (and its trailing comma+space) before replacement
        # gets a chance.
        result = injector.process("Hello, um, world")
        self.assertEqual(result, "Hello, world")
        self.assertNotIn("ummmm", result)

    # (f) replacement-only path still works when there are no filters
    def test_replacement_only_path(self):
        injector = self._create_injector(
            word_filters=[],
            word_replacements={"hi": "hello"},
        )
        self.assertEqual(injector.process("hi there"), "hello there")

    # (g) empty lists and dicts are a no-op
    def test_empty_filters_and_replacements_are_noop(self):
        injector = self._create_injector(word_filters=[], word_replacements={})
        self.assertEqual(
            injector.process("Hello, um, world"),
            "Hello, um, world",
        )

    def test_filter_cleanup_preserves_paragraph_breaks(self):
        injector = self._create_injector(word_filters=["um", "uh"])

        self.assertEqual(
            injector.process("Hello um   there\n\nSecond uh thought ."),
            "Hello there\n\nSecond thought.",
        )


class TestTextInjectorPasteLast(unittest.TestCase):
    """Tests for last_transcript storage and paste_last functionality."""

    def setUp(self):
        self.mock_keyboard = MagicMock()
        self.mock_keyboard._pressed = set()
        self.mock_keyboard.snapshot_held_modifiers = MagicMock(return_value=[])
        self.mock_keyboard._controller = MagicMock()
        self.mock_logger = MagicMock(spec=logging.Logger)

    def _create_injector(
        self,
        clipboard_behavior=False,
        clipboard_clear_delay=0.1,
    ):
        from whisperstream.text_injector import TextInjector

        return TextInjector(
            keyboard_manager=self.mock_keyboard,
            logger=self.mock_logger,
            word_filters=[],
            word_replacements={},
            post_processing="",
            clipboard_behavior=clipboard_behavior,
            clipboard_clear_delay=clipboard_clear_delay,
        )

    def test_last_transcript_is_none_before_inject(self):
        """last_transcript is None before any inject() call."""
        injector = self._create_injector()
        self.assertIsNone(injector.last_transcript)

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_last_transcript_set_after_inject(self, mock_sleep, mock_pyperclip):
        """last_transcript stores the processed text after inject()."""
        injector = self._create_injector()
        injector.inject("hello world")
        self.assertEqual(injector.last_transcript, "hello world")

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_last_transcript_updates_on_each_inject(self, mock_sleep, mock_pyperclip):
        """last_transcript is updated to the most recent inject() call."""
        injector = self._create_injector()
        injector.inject("first")
        injector.inject("second")
        self.assertEqual(injector.last_transcript, "second")

    def test_paste_last_returns_false_when_no_transcript(self):
        """paste_last() returns False when no transcript has been stored."""
        injector = self._create_injector()
        result = injector.paste_last()
        self.assertFalse(result)
        self.assertIsNone(result)
        self.mock_keyboard.send.assert_not_called()

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_paste_last_sends_paste_keystroke_when_transcript_available(
        self, mock_sleep, mock_pyperclip
    ):
        """paste_last() copies the stored transcript and sends the paste keystroke."""
        injector = self._create_injector()
        # Populate last_transcript via inject()
        injector.inject("my transcript")
        self.mock_keyboard.reset_mock()
        mock_pyperclip.reset_mock()

        result = injector.paste_last()

        self.assertTrue(result)
        mock_pyperclip.copy.assert_called_with("my transcript")
        self.mock_keyboard.send.assert_called_with("shift+insert")

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_clipboard_inject_and_paste_last_preserve_paragraph_breaks(
        self, mock_sleep, mock_pyperclip
    ):
        injector = self._create_injector()
        processed = injector.process("Hello Werner,\n\nFirst thought.")

        injector.inject(processed)

        self.assertEqual(
            injector.last_transcript, "Hello Werner,\n\nFirst thought."
        )
        mock_pyperclip.copy.assert_called_with(
            "Hello Werner,\n\nFirst thought."
        )

        self.mock_keyboard.reset_mock()
        mock_pyperclip.reset_mock()

        result = injector.paste_last()

        self.assertTrue(result)
        mock_pyperclip.copy.assert_called_with(
            "Hello Werner,\n\nFirst thought."
        )
        self.mock_keyboard.send.assert_called_with("shift+insert")


class TestTextInjectorPasteLastOriginal(unittest.TestCase):
    """Tests for last_original storage and paste_last_original functionality.

    Mirrors TestTextInjectorPasteLast exactly, with the parallel
    remember_original / paste_last_original / last_original API.
    """

    def setUp(self):
        self.mock_keyboard = MagicMock()
        self.mock_keyboard._pressed = set()
        self.mock_keyboard.snapshot_held_modifiers = MagicMock(return_value=[])
        self.mock_keyboard._controller = MagicMock()
        self.mock_logger = MagicMock(spec=logging.Logger)

    def _create_injector(
        self,
        clipboard_behavior=False,
        clipboard_clear_delay=0.1,
        word_replacements=None,
    ):
        from whisperstream.text_injector import TextInjector

        return TextInjector(
            keyboard_manager=self.mock_keyboard,
            logger=self.mock_logger,
            word_filters=[],
            word_replacements=word_replacements or {},
            post_processing="",
            clipboard_behavior=clipboard_behavior,
            clipboard_clear_delay=clipboard_clear_delay,
        )

    def test_last_original_is_none_before_remember_original(self):
        """last_original is None before any remember_original() call."""
        injector = self._create_injector()
        self.assertIsNone(injector.last_original)

    def test_remember_original_stores_processed_text(self):
        """remember_original("hello world") sets last_original to process("hello world")."""
        injector = self._create_injector()
        injector.remember_original("hello world")
        # process() with no filters/replacements/style returns the text unchanged
        self.assertEqual(injector.last_original, "hello world")

    def test_remember_original_runs_through_process_pipeline(self):
        """remember_original stores process(raw_text), not the raw text itself."""
        # Use a word replacement to confirm process() is applied
        injector = self._create_injector(word_replacements={"hi": "hello"})
        injector.remember_original("hi there")
        # process("hi there") with replacement {"hi": "hello"} → "hello there"
        self.assertEqual(injector.last_original, "hello there")

    def test_paste_last_original_returns_false_when_no_stored_original(self):
        """paste_last_original() returns False with no stored original and does NOT call send."""
        injector = self._create_injector()
        result = injector.paste_last_original()
        self.assertFalse(result)
        self.assertIsNone(result)
        self.mock_keyboard.send.assert_not_called()

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_paste_last_original_sends_paste_keystroke_when_original_available(
        self, mock_sleep, mock_pyperclip
    ):
        """After remember_original, paste_last_original() copies the stored text
        and sends shift+insert."""
        injector = self._create_injector()
        injector.remember_original("original transcript")
        self.mock_keyboard.reset_mock()
        mock_pyperclip.reset_mock()

        result = injector.paste_last_original()

        self.assertTrue(result)
        mock_pyperclip.copy.assert_called_with("original transcript")
        self.mock_keyboard.send.assert_called_with("shift+insert")

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_paste_last_original_independent_of_last_transcript(
        self, mock_sleep, mock_pyperclip
    ):
        """last_original and last_transcript are independent slots; injecting
        enhanced text does not overwrite the stored original."""
        injector = self._create_injector()
        injector.remember_original("raw text")
        injector.inject("enhanced text")
        self.mock_keyboard.reset_mock()
        mock_pyperclip.reset_mock()

        result = injector.paste_last_original()

        self.assertTrue(result)
        mock_pyperclip.copy.assert_called_with("raw text")
        self.assertEqual(injector.last_transcript, "enhanced text")
        self.assertEqual(injector.last_original, "raw text")


class TestTextInjectorPasteChunk(unittest.TestCase):
    """Tests for incremental batch paste (paste_chunk) used by chunked cleanup."""

    def setUp(self):
        self.mock_keyboard = MagicMock()
        self.mock_keyboard._pressed = set()
        self.mock_keyboard.snapshot_held_modifiers = MagicMock(return_value=[])
        self.mock_keyboard._controller = MagicMock()
        self.mock_logger = MagicMock(spec=logging.Logger)

    def _create_injector(self, clipboard_behavior=True, post_processing=""):
        from whisperstream.text_injector import TextInjector

        return TextInjector(
            keyboard_manager=self.mock_keyboard,
            logger=self.mock_logger,
            word_filters=[],
            word_replacements={},
            post_processing=post_processing,
            clipboard_behavior=clipboard_behavior,
            clipboard_clear_delay=0.1,
        )

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_clipboard_saved_once_restored_once_across_sequence(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """A 3-chunk sequence saves the prior clipboard once and schedules exactly
        one restore (on the last chunk), never per chunk."""
        mock_pyperclip.paste.return_value = "prior"
        injector = self._create_injector(clipboard_behavior=True)

        injector.paste_chunk("alpha", first=True, last=False)
        injector.paste_chunk("beta", first=False, last=False)
        injector.paste_chunk("gamma", first=False, last=True)

        # Prior clipboard read exactly once (on the first chunk).
        mock_pyperclip.paste.assert_called_once()
        # Exactly one restore Timer for the whole sequence (on the last chunk).
        mock_timer_cls.assert_called_once()
        # The restore callback restores the original clipboard.
        callback = mock_timer_cls.call_args[0][1]
        callback()
        mock_pyperclip.copy.assert_called_with("prior")

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_leading_space_separator_on_non_first_chunks(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """First chunk pastes as-is; later chunks get a single leading space."""
        injector = self._create_injector(clipboard_behavior=False)

        injector.paste_chunk("alpha", first=True, last=False)
        injector.paste_chunk("beta", first=False, last=False)
        injector.paste_chunk("gamma", first=False, last=True)

        copied = [c.args[0] for c in mock_pyperclip.copy.call_args_list]
        self.assertEqual(copied, ["alpha", " beta", " gamma"])

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_last_transcript_is_full_joined_text(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """After the sequence, last_transcript is the full pasted text so
        paste_last re-pastes the whole dictation."""
        injector = self._create_injector(clipboard_behavior=False)

        injector.paste_chunk("alpha", first=True, last=False)
        injector.paste_chunk("beta", first=False, last=False)
        injector.paste_chunk("gamma", first=False, last=True)

        self.assertEqual(injector.last_transcript, "alpha beta gamma")

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_each_chunk_sends_shift_insert(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """Every chunk triggers a Shift+Insert paste."""
        injector = self._create_injector(clipboard_behavior=False)

        injector.paste_chunk("alpha", first=True, last=False)
        injector.paste_chunk("beta", first=False, last=True)

        self.assertEqual(self.mock_keyboard.send.call_count, 2)
        self.mock_keyboard.send.assert_called_with("shift+insert")

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_no_restore_on_middle_chunk(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """A non-last chunk never schedules a clipboard restore."""
        mock_pyperclip.paste.return_value = "prior"
        injector = self._create_injector(clipboard_behavior=True)

        injector.paste_chunk("alpha", first=True, last=False)
        mock_timer_cls.assert_not_called()

    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_abort_restores_batch_once_and_is_idempotent(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        mock_pyperclip.paste.return_value = "prior"
        injector = self._create_injector(clipboard_behavior=True)

        injector.paste_chunk("alpha", first=True, last=False)
        injector.abort_chunked_paste()
        injector.abort_chunked_paste()

        mock_timer_cls.assert_called_once()
        callback = mock_timer_cls.call_args.args[1]
        callback()
        mock_pyperclip.copy.assert_called_with("prior")
        self.assertFalse(injector._batch_active)
        self.assertIsNone(injector._batch_saved)
        self.assertEqual(injector._batch_accum, [])

    def test_process_affix_flags_control_prepend_append(self):
        """process()'s do_prepend / do_append gate the style-guide affixes so the
        chunked path applies prepend on the first chunk and append on the last."""
        injector = self._create_injector(
            clipboard_behavior=False, post_processing="prepend: >>\nappend: <<"
        )
        self.assertEqual(injector.process("hello world here"), ">> hello world here <<")
        self.assertEqual(
            injector.process("hello world here", do_append=False), ">> hello world here"
        )
        self.assertEqual(
            injector.process("hello world here", do_prepend=False), "hello world here <<"
        )
        self.assertEqual(
            injector.process("hello world here", do_prepend=False, do_append=False),
            "hello world here",
        )


class TestTextInjectorKeystrokeMode(unittest.TestCase):
    """Tests for input_style="keystroke" branch in TextInjector.

    All three injection paths (inject, paste_last, paste_chunk) must:
    - Call keyboard.type_text() instead of pyperclip.copy + keyboard.send("shift+insert")
    - Never call pyperclip.copy or pyperclip.paste
    - Still set last_transcript
    - Never schedule a clipboard-restore Timer
    - Still wrap the keystroke send inside the held-modifier release+re-press guard
    """

    def setUp(self):
        self.mock_keyboard = MagicMock()
        self.mock_keyboard._pressed = set()
        self.mock_keyboard.snapshot_held_modifiers = MagicMock(return_value=[])
        self.mock_keyboard._controller = MagicMock()
        self.mock_keyboard.type_text = MagicMock()
        # suppress_self_events must be a context manager
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=None)
        cm.__exit__ = MagicMock(return_value=False)
        self.mock_keyboard.suppress_self_events = MagicMock(return_value=cm)
        self.mock_logger = MagicMock(spec=logging.Logger)

    def _create_injector(
        self,
        input_style="clipboard",
        keystroke_delay_ms=0,
        clipboard_behavior=True,
    ):
        from whisperstream.text_injector import TextInjector

        return TextInjector(
            keyboard_manager=self.mock_keyboard,
            logger=self.mock_logger,
            word_filters=[],
            word_replacements={},
            post_processing="",
            clipboard_behavior=clipboard_behavior,
            clipboard_clear_delay=0.1,
            input_style=input_style,
            keystroke_delay_ms=keystroke_delay_ms,
        )

    # (a) inject in keystroke mode: type_text called, no clipboard, last_transcript set, no Timer
    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_keystroke_calls_type_text_not_clipboard(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """inject with input_style='keystroke' types via type_text, never touches clipboard."""
        injector = self._create_injector(input_style="keystroke")
        injector.inject("hello")

        # type_text must have been called with the processed text
        self.mock_keyboard.type_text.assert_called_once()
        call_args = self.mock_keyboard.type_text.call_args
        self.assertEqual(call_args[0][0], "hello")  # positional text arg

        # clipboard must never be touched
        mock_pyperclip.copy.assert_not_called()
        mock_pyperclip.paste.assert_not_called()

        # last_transcript must be set
        self.assertEqual(injector.last_transcript, "hello")

        # no clipboard-restore Timer scheduled
        mock_timer_cls.assert_not_called()

    # (b) inject in keystroke mode with per-call override: default is clipboard but call overrides
    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_per_call_input_style_overrides_default(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """inject(text, input_style='keystroke') overrides the clipboard default."""
        injector = self._create_injector(input_style="clipboard")
        injector.inject("hello", input_style="keystroke")

        self.mock_keyboard.type_text.assert_called_once()
        mock_pyperclip.copy.assert_not_called()
        mock_pyperclip.paste.assert_not_called()
        self.assertEqual(injector.last_transcript, "hello")
        mock_timer_cls.assert_not_called()

    # (b) paste_last in keystroke mode: types stored transcript, no clipboard
    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_paste_last_keystroke_types_stored_transcript(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """paste_last with input_style='keystroke' types stored transcript, no clipboard."""
        injector = self._create_injector(input_style="keystroke")
        # Populate last_transcript via inject (also in keystroke mode)
        injector.inject("my transcript")
        self.mock_keyboard.type_text.reset_mock()
        mock_pyperclip.reset_mock()
        mock_timer_cls.reset_mock()

        result = injector.paste_last(input_style="keystroke")

        self.assertTrue(result)
        self.mock_keyboard.type_text.assert_called_once()
        call_args = self.mock_keyboard.type_text.call_args
        self.assertEqual(call_args[0][0], "my transcript")
        mock_pyperclip.copy.assert_not_called()
        mock_pyperclip.paste.assert_not_called()
        mock_timer_cls.assert_not_called()

    # (b2) paste_last_original in keystroke mode: types stored original, no clipboard
    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_paste_last_original_keystroke_types_stored_original(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """paste_last_original honors keystroke mode: types the stored original via
        type_text and never touches the clipboard (keystroke-input integration)."""
        injector = self._create_injector(input_style="keystroke")
        injector.remember_original("raw original")
        stored = injector.last_original
        self.mock_keyboard.type_text.reset_mock()
        mock_pyperclip.reset_mock()
        mock_timer_cls.reset_mock()

        result = injector.paste_last_original(input_style="keystroke")

        self.assertTrue(result)
        self.mock_keyboard.type_text.assert_called_once()
        call_args = self.mock_keyboard.type_text.call_args
        self.assertEqual(call_args[0][0], stored)
        mock_pyperclip.copy.assert_not_called()
        mock_pyperclip.paste.assert_not_called()
        mock_timer_cls.assert_not_called()

    # (c) paste_chunk in keystroke mode: accumulates, sets last_transcript to joined, no clipboard
    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_paste_chunk_keystroke_accumulates_no_clipboard(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """paste_chunk in keystroke mode types chunks, never touches clipboard,
        and sets last_transcript to the full joined text after the last chunk."""
        injector = self._create_injector(input_style="keystroke")

        injector.paste_chunk("alpha", first=True, last=False, input_style="keystroke")
        injector.paste_chunk("beta", first=False, last=False, input_style="keystroke")
        injector.paste_chunk("gamma", first=False, last=True, input_style="keystroke")

        # type_text called once per chunk (3 times total)
        self.assertEqual(self.mock_keyboard.type_text.call_count, 3)

        # clipboard never touched
        mock_pyperclip.copy.assert_not_called()
        mock_pyperclip.paste.assert_not_called()
        mock_timer_cls.assert_not_called()

        # last_transcript is the full joined text (with space separator for non-first chunks)
        self.assertEqual(injector.last_transcript, "alpha beta gamma")

    # (d) held-modifier release+re-press still wraps the keystroke send
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_keystroke_releases_held_modifier_around_type_text(
        self, mock_sleep, mock_pyperclip
    ):
        """In keystroke mode, held modifiers are still released before type_text
        and re-pressed afterward."""
        from pynput.keyboard import Key

        self.mock_keyboard._pressed = {Key.shift_r}
        self.mock_keyboard.snapshot_held_modifiers.return_value = [Key.shift_r]

        injector = self._create_injector(input_style="keystroke")
        injector.inject("hello")

        # Extract calls order on the mock_keyboard
        all_calls = self.mock_keyboard.mock_calls

        release_idx = None
        type_text_idx = None
        press_idx = None
        for i, c in enumerate(all_calls):
            if c == call._controller.release(Key.shift_r):
                release_idx = i
            elif c.args and c.args[0] == "hello" and "type_text" in str(c):
                type_text_idx = i
            elif c == call._controller.press(Key.shift_r):
                press_idx = i

        self.assertIsNotNone(release_idx, "release(Key.shift_r) not found in calls")
        self.assertIsNotNone(press_idx, "press(Key.shift_r) not found in calls")
        # type_text should have been called
        self.mock_keyboard.type_text.assert_called_once()

        # type_text index: find it in the calls list
        for i, c in enumerate(all_calls):
            name = str(c)
            if "type_text" in name and "hello" in name:
                type_text_idx = i
                break

        if type_text_idx is not None:
            self.assertLess(release_idx, type_text_idx, "release before type_text")
            self.assertLess(type_text_idx, press_idx, "type_text before press")

    # (e) existing clipboard-mode tests still work — no input_style arg means clipboard default
    @patch("whisperstream.text_injector._Timer")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_clipboard_mode_unchanged_when_no_input_style_passed(
        self, mock_sleep, mock_pyperclip, mock_timer_cls
    ):
        """When input_style is not passed (defaults to 'clipboard'), behavior is unchanged."""
        mock_pyperclip.paste.return_value = "prior"
        mock_timer_cls.return_value = MagicMock()

        # Create injector using OLD-STYLE call (no input_style, no keystroke_delay_ms)
        from whisperstream.text_injector import TextInjector

        injector = TextInjector(
            keyboard_manager=self.mock_keyboard,
            logger=self.mock_logger,
            word_filters=[],
            word_replacements={},
            post_processing="",
            clipboard_behavior=True,
            clipboard_clear_delay=0.1,
        )
        injector.inject("hello")

        # Clipboard path: copy was called with the text
        mock_pyperclip.copy.assert_any_call("hello")
        # send was called with shift+insert
        self.mock_keyboard.send.assert_called_with("shift+insert")
        # type_text NOT called
        self.mock_keyboard.type_text.assert_not_called()

    # (f) keystroke_delay_ms is forwarded to type_text
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_keystroke_forwards_delay_ms(self, mock_sleep, mock_pyperclip):
        """inject passes keystroke_delay_ms to type_text as delay_ms kwarg."""
        injector = self._create_injector(input_style="keystroke", keystroke_delay_ms=10)
        injector.inject("hello")

        self.mock_keyboard.type_text.assert_called_once()
        call_kwargs = self.mock_keyboard.type_text.call_args[1]
        self.assertEqual(call_kwargs.get("delay_ms"), 10)


if __name__ == "__main__":
    unittest.main()
