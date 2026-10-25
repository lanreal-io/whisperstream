"""Tests for the ``agent_speak_setup_completed`` config field (Task 1.1).

Covers: default is False; an explicit True survives save/load round-trip; a
non-bool value raises from validate(); an absent key loads as False. Written
as a structural mirror of tests/test_config_claude_hud_show_text.py.

This is a seed-only default (scope.md ## Deferred forbids any migration that
would flip the flag for existing installs), so this test file only asserts
the field's own default/validate/round-trip behavior.
"""

import unittest

from whisperstream.config_manager import WhisperstreamConfig


class TestAgentSpeakSetupCompletedDefault(unittest.TestCase):
    def test_default_is_false(self):
        """agent_speak_setup_completed defaults to False (seed-only, unset)."""
        self.assertFalse(WhisperstreamConfig().agent_speak_setup_completed)

    def test_default_validates(self):
        """The default state must not raise on validate()."""
        WhisperstreamConfig().validate()


class TestAgentSpeakSetupCompletedFromDict(unittest.TestCase):
    def test_from_dict_absent_key_stays_default(self):
        """Absent key does not change the default (stays False)."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertFalse(conf.agent_speak_setup_completed)

    def test_from_dict_false(self):
        conf = WhisperstreamConfig.from_dict({"agent_speak_setup_completed": False})
        self.assertFalse(conf.agent_speak_setup_completed)

    def test_from_dict_true(self):
        conf = WhisperstreamConfig.from_dict({"agent_speak_setup_completed": True})
        self.assertTrue(conf.agent_speak_setup_completed)


class TestAgentSpeakSetupCompletedToDict(unittest.TestCase):
    def test_to_dict_default_included(self):
        """to_dict() includes the key even when it's the default False."""
        d = WhisperstreamConfig().to_dict()
        self.assertIn("agent_speak_setup_completed", d)
        self.assertEqual(d["agent_speak_setup_completed"], False)

    def test_to_dict_true_included(self):
        """to_dict() includes the key when explicitly set True."""
        conf = WhisperstreamConfig(agent_speak_setup_completed=True)
        d = conf.to_dict()
        self.assertIn("agent_speak_setup_completed", d)
        self.assertEqual(d["agent_speak_setup_completed"], True)

    def test_round_trip_preserves_true(self):
        """from_dict(to_dict()) preserves agent_speak_setup_completed=True."""
        conf = WhisperstreamConfig(agent_speak_setup_completed=True)
        round_tripped = WhisperstreamConfig.from_dict(conf.to_dict())
        self.assertTrue(round_tripped.agent_speak_setup_completed)

    def test_round_trip_preserves_false(self):
        """from_dict(to_dict()) preserves agent_speak_setup_completed=False."""
        conf = WhisperstreamConfig(agent_speak_setup_completed=False)
        round_tripped = WhisperstreamConfig.from_dict(conf.to_dict())
        self.assertFalse(round_tripped.agent_speak_setup_completed)


class TestAgentSpeakSetupCompletedValidate(unittest.TestCase):
    def test_validate_rejects_non_bool(self):
        conf = WhisperstreamConfig()
        conf.agent_speak_setup_completed = "yes"  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            conf.validate()


if __name__ == "__main__":
    unittest.main()
