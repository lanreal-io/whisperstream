"""Tests for the ``claude_hud_show_text`` config field (Task 1.1).

Covers: default is True; to_dict() includes the key (both when default and
when set False); from_dict(to_dict()) round-trips both True and False;
from_dict({}) leaves the default True; validate() does not raise.
"""

import unittest

from whisperstream.config_manager import WhisperstreamConfig


class TestClaudeHudShowTextDefault(unittest.TestCase):
    def test_default_is_true(self):
        """claude_hud_show_text defaults to True (show text by default)."""
        self.assertTrue(WhisperstreamConfig().claude_hud_show_text)

    def test_default_validates(self):
        """The default state must not raise on validate()."""
        WhisperstreamConfig().validate()


class TestClaudeHudShowTextFromDict(unittest.TestCase):
    def test_from_dict_absent_key_stays_default(self):
        """Absent key does not change the default (stays True)."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertTrue(conf.claude_hud_show_text)

    def test_from_dict_false(self):
        conf = WhisperstreamConfig.from_dict({"claude_hud_show_text": False})
        self.assertFalse(conf.claude_hud_show_text)

    def test_from_dict_true(self):
        conf = WhisperstreamConfig.from_dict({"claude_hud_show_text": True})
        self.assertTrue(conf.claude_hud_show_text)


class TestClaudeHudShowTextToDict(unittest.TestCase):
    def test_to_dict_default_included(self):
        """to_dict() includes the key even when it's the default True."""
        d = WhisperstreamConfig().to_dict()
        self.assertIn("claude_hud_show_text", d)
        self.assertEqual(d["claude_hud_show_text"], True)

    def test_to_dict_false_included(self):
        """to_dict() includes the key when explicitly set False."""
        conf = WhisperstreamConfig(claude_hud_show_text=False)
        d = conf.to_dict()
        self.assertIn("claude_hud_show_text", d)
        self.assertEqual(d["claude_hud_show_text"], False)

    def test_round_trip_preserves_true(self):
        """from_dict(to_dict()) preserves claude_hud_show_text=True."""
        conf = WhisperstreamConfig(claude_hud_show_text=True)
        round_tripped = WhisperstreamConfig.from_dict(conf.to_dict())
        self.assertTrue(round_tripped.claude_hud_show_text)

    def test_round_trip_preserves_false(self):
        """from_dict(to_dict()) preserves claude_hud_show_text=False."""
        conf = WhisperstreamConfig(claude_hud_show_text=False)
        round_tripped = WhisperstreamConfig.from_dict(conf.to_dict())
        self.assertFalse(round_tripped.claude_hud_show_text)


if __name__ == "__main__":
    unittest.main()
