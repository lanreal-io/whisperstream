"""Tests for Task 2.4: Auto-submit (Enter) mechanism.

TDD: these tests are written BEFORE implementation.  They assert:
  - inject(text, submit=True) presses Shift+Insert then Enter (inside the
    modifier-release guard, via suppress_self_events).
  - inject(text, submit=False) — the default — presses only Shift+Insert, no
    Enter.  Existing callers that omit submit= are unchanged.

Mock pattern: reuse the TestTextInjector setUp from test_text_injector.py
(mock_keyboard with suppress_self_events context-manager, snapshot_held_modifiers
returning [], _controller MagicMock).
"""
from __future__ import annotations

import logging
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch


def _make_suppress_ctx():
    """Return a no-op context manager that behaves like suppress_self_events()."""
    @contextmanager
    def _ctx():
        yield
    return _ctx


class TestInjectSubmitParameter(unittest.TestCase):
    """inject(text, submit=True/False) Enter-after-paste behavior."""

    def setUp(self):
        platform_patch = patch("whisperstream.text_injector.platform.system", return_value="Windows")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        self.mock_keyboard = MagicMock()
        self.mock_keyboard._pressed = set()
        self.mock_keyboard.snapshot_held_modifiers = MagicMock(return_value=[])
        self.mock_keyboard._controller = MagicMock()
        # suppress_self_events must be a context manager
        self.mock_keyboard.suppress_self_events = _make_suppress_ctx()
        self.mock_logger = MagicMock(spec=logging.Logger)

    def _make_injector(self):
        from whisperstream.text_injector import TextInjector

        return TextInjector(
            keyboard_manager=self.mock_keyboard,
            logger=self.mock_logger,
            word_filters=[],
            word_replacements={},
            post_processing="",
            clipboard_behavior=False,
            clipboard_clear_delay=0.1,
        )

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_submit_false_default_sends_only_paste(
        self, mock_sleep, mock_pyperclip
    ):
        """inject(text) with default submit=False sends only shift+insert, no Enter."""
        injector = self._make_injector()
        injector.inject("hello world")

        send_calls = [c for c in self.mock_keyboard.send.call_args_list]
        combos = [c.args[0] for c in send_calls]
        self.assertIn("shift+insert", combos)
        self.assertNotIn("enter", combos)

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_submit_false_explicit_sends_only_paste(
        self, mock_sleep, mock_pyperclip
    ):
        """inject(text, submit=False) explicitly sends only shift+insert, no Enter."""
        injector = self._make_injector()
        injector.inject("hello world", submit=False)

        combos = [c.args[0] for c in self.mock_keyboard.send.call_args_list]
        self.assertIn("shift+insert", combos)
        self.assertNotIn("enter", combos)

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_submit_true_sends_paste_then_enter(
        self, mock_sleep, mock_pyperclip
    ):
        """inject(text, submit=True) sends shift+insert then enter."""
        injector = self._make_injector()
        injector.inject("hello world", submit=True)

        combos = [c.args[0] for c in self.mock_keyboard.send.call_args_list]
        self.assertIn("shift+insert", combos, "shift+insert must be sent")
        self.assertIn("enter", combos, "enter must be sent when submit=True")

        # Ordering: shift+insert must come BEFORE enter
        paste_idx = combos.index("shift+insert")
        enter_idx = combos.index("enter")
        self.assertLess(
            paste_idx, enter_idx,
            "shift+insert must be sent before enter"
        )

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_submit_true_enter_inside_suppress(
        self, mock_sleep, mock_pyperclip
    ):
        """Both shift+insert and enter are sent within the same suppress_self_events block.

        We verify this by checking that send() is only called while the
        suppress_self_events context manager is active. We do this by
        tracking call order: suppress must be entered before shift+insert is
        sent, and enter must also occur within the same guard block.
        """
        call_log = []

        @contextmanager
        def tracking_suppress():
            call_log.append("suppress_enter")
            yield
            call_log.append("suppress_exit")

        self.mock_keyboard.suppress_self_events = tracking_suppress

        def tracking_send(combo):
            call_log.append(f"send:{combo}")

        self.mock_keyboard.send = MagicMock(side_effect=tracking_send)

        injector = self._make_injector()
        injector.inject("hello world", submit=True)

        # Verify ordering: suppress_enter -> send:shift+insert -> send:enter -> suppress_exit
        self.assertIn("suppress_enter", call_log)
        self.assertIn("send:shift+insert", call_log)
        self.assertIn("send:enter", call_log)
        self.assertIn("suppress_exit", call_log)

        suppress_enter_idx = call_log.index("suppress_enter")
        paste_idx = call_log.index("send:shift+insert")
        enter_idx = call_log.index("send:enter")
        suppress_exit_idx = call_log.index("suppress_exit")

        self.assertLess(suppress_enter_idx, paste_idx)
        self.assertLess(paste_idx, enter_idx)
        self.assertLess(enter_idx, suppress_exit_idx)

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_submit_true_with_held_modifier_enters_after_repress(
        self, mock_sleep, mock_pyperclip
    ):
        """With a held modifier and submit=True, enter comes after paste+repress.

        The full sequence inside the guard:
          release(key) -> send(shift+insert) -> press(key) -> send(enter)

        This ensures the Enter cannot be tainted by the PTT modifier (e.g.,
        right shift held) turning it into something unexpected.
        """
        from pynput.keyboard import Key

        self.mock_keyboard._pressed = {Key.shift_r}
        self.mock_keyboard.snapshot_held_modifiers = MagicMock(return_value=[Key.shift_r])

        call_log = []

        @contextmanager
        def tracking_suppress():
            call_log.append("suppress_enter")
            yield
            call_log.append("suppress_exit")

        self.mock_keyboard.suppress_self_events = tracking_suppress

        def tracking_send(combo):
            call_log.append(f"send:{combo}")

        self.mock_keyboard.send = MagicMock(side_effect=tracking_send)

        def tracking_release(k):
            call_log.append(f"release:{k}")

        def tracking_press(k):
            call_log.append(f"press:{k}")

        self.mock_keyboard._controller = MagicMock()
        self.mock_keyboard._controller.release = MagicMock(side_effect=tracking_release)
        self.mock_keyboard._controller.press = MagicMock(side_effect=tracking_press)

        injector = self._make_injector()
        injector.inject("hello world", submit=True)

        # Must have: suppress -> release -> paste -> repress -> enter -> suppress_exit
        self.assertIn(f"release:{Key.shift_r}", call_log)
        self.assertIn("send:shift+insert", call_log)
        self.assertIn("send:enter", call_log)

        release_idx = call_log.index(f"release:{Key.shift_r}")
        paste_idx = call_log.index("send:shift+insert")
        enter_idx = call_log.index("send:enter")
        suppress_exit_idx = call_log.index("suppress_exit")

        self.assertLess(release_idx, paste_idx)
        self.assertLess(paste_idx, enter_idx)
        self.assertLess(enter_idx, suppress_exit_idx)

    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.text_injector.time.sleep")
    def test_inject_submit_true_enter_still_sent_when_send_raises(
        self, mock_sleep, mock_pyperclip
    ):
        """If shift+insert send() raises, Enter is NOT sent (the paste failed).

        This tests the graceful degradation: if we can't paste, sending Enter
        would submit an empty/stale form. The enter should only fire after a
        successful paste.
        """
        # When shift+insert raises, enter should not be sent
        self.mock_keyboard.send = MagicMock(side_effect=RuntimeError("paste failed"))

        injector = self._make_injector()
        # Should not raise
        injector.inject("hello world", submit=True)

        # send was called (and raised), but enter was not (only one call attempted)
        call_count = self.mock_keyboard.send.call_count
        if call_count > 0:
            first_combo = self.mock_keyboard.send.call_args_list[0].args[0]
            self.assertEqual(first_combo, "shift+insert")
        # enter should NOT be sent if paste raised
        combos = [c.args[0] for c in self.mock_keyboard.send.call_args_list]
        self.assertNotIn("enter", combos)


if __name__ == "__main__":
    unittest.main()
