import logging
import unittest
from unittest.mock import MagicMock

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.shortcut_contract import ShortcutAdapter
from whisperstream.text_injector import TextInjector


class _DisposableDeliveryAdapter:
    """Native-free delivery seam for processing-only security assertions."""

    def __init__(self) -> None:
        self.closed = False

    def resolve_target(self, *_args, **_kwargs):
        raise AssertionError("processing-only test attempted target resolution")

    def deliver(self, *_args, **_kwargs):
        raise AssertionError("processing-only test attempted delivery")

    def cancel(self, *_args, **_kwargs):
        raise AssertionError("processing-only test attempted cancellation")

    def close(self) -> None:
        self.closed = True


class TestTextInjectorSecurity(unittest.TestCase):
    def setUp(self):
        self.mock_keyboard = MagicMock(spec=ShortcutAdapter)
        self.logger = logging.getLogger("test_logger")
        self.delivery = _DisposableDeliveryAdapter()

        self.injector = TextInjector(
            keyboard_manager=self.mock_keyboard,
            logger=self.logger,
            word_filters=[],
            word_replacements={},
            post_processing="",
            clipboard_behavior=False,
            clipboard_clear_delay=0.1,
            delivery_adapter=self.delivery,
        )

    def tearDown(self):
        self.injector.close()
        self.assertTrue(self.delivery.closed)

    def test_control_char_sanitization(self):
        """Verify that non-printable control characters are removed."""
        # \x07 (Bell), \x1b (Escape), \x08 (Backspace)
        dirty_text = "Hello\x07 \x1bWorld\x08!"
        processed = self.injector.process(dirty_text)

        # Verify no control characters remain
        for char in processed:
            if not char.isprintable():
                self.fail(f"Found non-printable character code: {ord(char)}")

        # Verify expected text is preserved
        self.assertEqual(processed, "Hello World!")

    def test_tabs_and_newlines_preserved(self):
        """Verify that tabs and newlines are preserved as valid whitespace."""
        text = "Line1\nLine2\tTabbed"
        processed = self.injector.process(text)
        self.assertEqual(processed, "Line1\nLine2 Tabbed")

    def test_override_injection_sanitization(self):
        """Verify that word_replacements cannot introduce control characters."""
        replacements = {"unsafe": "back\x08space"}
        injector = TextInjector(
            keyboard_manager=self.mock_keyboard,
            logger=self.logger,
            word_filters=[],
            word_replacements=replacements,
            post_processing="",
            clipboard_behavior=False,
            clipboard_clear_delay=0.1,
            delivery_adapter=_DisposableDeliveryAdapter(),
        )
        try:
            processed = injector.process("unsafe")
            self.assertNotIn("\x08", processed)
            self.assertEqual(processed, "backspace")
        finally:
            injector.close()

    def test_sanitize_preserves_whitespace(self):
        """Verify that _sanitize with strip_text=False preserves whitespace."""
        # Test the static method directly
        text = "  spaced  "
        sanitized = TextInjector._sanitize(text, strip_text=False)
        self.assertEqual(sanitized, "  spaced  ")

        # Verify it still removes control chars
        dirty = "  spaced\x08  "
        sanitized_dirty = TextInjector._sanitize(dirty, strip_text=False)
        self.assertEqual(sanitized_dirty, "  spaced  ")


class TestConfigSecurity(unittest.TestCase):
    def test_default_recording_limit(self):
        """Verify the default recording limit is 0 (disabled); toggle mode uses toggle_safety_timeout instead."""
        config = WhisperstreamConfig()
        self.assertEqual(config.max_recording_duration, 0.0)
        self.assertEqual(config.toggle_safety_timeout, 1800.0)

    def test_recording_limit_zero_disables(self):
        """Verify that 0 can be used to disable the limit."""
        config = WhisperstreamConfig(max_recording_duration=0)
        self.assertEqual(config.max_recording_duration, 0)


if __name__ == "__main__":
    unittest.main()
