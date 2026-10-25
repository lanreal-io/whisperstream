import logging
import sys
import unittest
from unittest.mock import MagicMock, patch

from whisperstream.audio_feedback import AudioFeedback


def _fake_pygame_modules():
    """Build a MagicMock tree for pygame + pygame.mixer suitable for sys.modules.

    Returns a tuple ``(modules_dict, fake_mixer)`` where ``modules_dict`` can be
    passed to ``patch.dict(sys.modules, ...)`` and ``fake_mixer`` is the inner
    ``pygame.mixer`` mock the test can assert against.
    """
    fake_mixer = MagicMock()
    fake_pygame = MagicMock()
    fake_pygame.mixer = fake_mixer
    return {"pygame": fake_pygame, "pygame.mixer": fake_mixer}, fake_mixer


class TestAudioFeedback(unittest.TestCase):
    def setUp(self):
        self.mock_logger = MagicMock(spec=logging.Logger)

    def test_enabled_when_pygame_available(self):
        """AudioFeedback is enabled and mixer_ready when pygame.mixer imports cleanly."""
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True)
            self.assertTrue(af._enabled)
            self.assertTrue(af._mixer_ready)
            fake_mixer.init.assert_called_once()

    def test_disabled_when_explicitly_disabled(self):
        """AudioFeedback should respect enabled=False (no mixer init attempted)."""
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=False)
            self.assertFalse(af._enabled)
            # When disabled, __init__ should not even try to init the mixer.
            fake_mixer.init.assert_not_called()

    def test_play_start_calls_pygame_sound(self):
        """play_start constructs a pygame.mixer.Sound and calls .play() on it."""
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True)
            af.play_start()

            fake_mixer.Sound.assert_called_once()
            returned_sound = fake_mixer.Sound.return_value
            returned_sound.play.assert_called_once()

    def test_blocking_start_calls_completion_after_sound_duration(self):
        fakes, fake_mixer = _fake_pygame_modules()
        fake_mixer.Sound.return_value.get_length.return_value = 0.25
        completed = MagicMock()
        with (
            patch.dict(sys.modules, fakes),
            patch("whisperstream.audio_feedback.time.sleep") as sleep,
        ):
            af = AudioFeedback(logger=self.mock_logger, enabled=True)
            af.play_start_blocking(on_complete=completed)

        sleep.assert_called_once_with(0.25)
        completed.assert_called_once_with()

    def test_blocking_start_calls_completion_when_feedback_disabled(self):
        completed = MagicMock()
        af = AudioFeedback(logger=self.mock_logger, enabled=False)

        af.play_start_blocking(on_complete=completed)

        completed.assert_called_once_with()

    def test_play_caches_sound_object(self):
        """After play_start, the Sound object is cached under the asset name key."""
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True)
            af.play_start()

            self.assertEqual(len(af._cache), 1)
            self.assertIn("start.wav", af._cache)
            self.assertIs(af._cache["start.wav"], fake_mixer.Sound.return_value)

    def test_cached_sound_skips_construction_on_second_play(self):
        """Second play_start reuses cached Sound instead of reconstructing it."""
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True)
            af.play_start()
            af.play_start()

            self.assertEqual(fake_mixer.Sound.call_count, 1)
            returned_sound = fake_mixer.Sound.return_value
            self.assertEqual(returned_sound.play.call_count, 2)

    def test_volume_passed_to_sound_set_volume(self):
        """The configured volume is forwarded to the Sound via set_volume."""
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True, volume=0.5)
            af.play_start()

            returned_sound = fake_mixer.Sound.return_value
            returned_sound.set_volume.assert_called_with(0.5)

    def test_volume_clamp_above_one(self):
        """Volume should be clamped to 1.0 if above."""
        fakes, _fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True, volume=1.5)
            self.assertEqual(af._volume, 1.0)

    def test_volume_clamp_below_zero(self):
        """Volume should be clamped to 0.0 if below."""
        fakes, _fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True, volume=-0.5)
            self.assertEqual(af._volume, 0.0)

    def test_init_failure_leaves_mixer_not_ready(self):
        """If pygame.mixer.init raises, the instance stays enabled but not ready."""
        fakes, fake_mixer = _fake_pygame_modules()
        fake_mixer.init.side_effect = RuntimeError("no audio device")
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True)

            self.assertFalse(af._mixer_ready)
            # _enabled reflects the constructor argument; init failure only
            # affects _mixer_ready (so _play_sound becomes a no-op).
            self.assertTrue(af._enabled)
            self.mock_logger.warning.assert_called()

    def test_update_settings_does_not_quit_or_reinit_mixer(self):
        """Volume updates must not call mixer.quit() / mixer.init() on a live
        instance — that's the SDL race that aborts the process when a hotkey
        thread is concurrently calling sound.play() on a cached Sound.
        """
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True, volume=1.0)
            self.assertEqual(fake_mixer.init.call_count, 1)
            fake_mixer.init.reset_mock()
            fake_mixer.quit.reset_mock()

            af.update_settings(enabled=True, volume=0.3)

            fake_mixer.init.assert_not_called()
            fake_mixer.quit.assert_not_called()
            self.assertEqual(af._volume, 0.3)
            self.assertTrue(af._mixer_ready)

    def test_update_settings_propagates_volume_to_next_play(self):
        """The new volume from update_settings is read on the next play_*."""
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True, volume=1.0)
            af.play_start()
            af.update_settings(enabled=True, volume=0.25)
            af.play_start()

            returned_sound = fake_mixer.Sound.return_value
            # Last set_volume call reflects the post-update value.
            self.assertEqual(returned_sound.set_volume.call_args.args, (0.25,))

    def test_update_settings_lazy_inits_when_toggled_on(self):
        """If feedback was constructed disabled (mixer never initialized) and
        then turned on via update_settings, the mixer initializes on demand.
        """
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=False)
            self.assertFalse(af._mixer_ready)
            fake_mixer.init.assert_not_called()

            af.update_settings(enabled=True, volume=0.8)

            fake_mixer.init.assert_called_once()
            self.assertTrue(af._mixer_ready)
            self.assertTrue(af._enabled)

    def test_update_settings_disable_does_not_quit_mixer(self):
        """Disabling feedback at runtime keeps the mixer alive — only the
        _enabled gate flips. This avoids the quit/play race entirely.
        """
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True)
            fake_mixer.quit.reset_mock()

            af.update_settings(enabled=False, volume=1.0)

            fake_mixer.quit.assert_not_called()
            self.assertFalse(af._enabled)
            self.assertTrue(af._mixer_ready)

    def test_update_settings_volume_clamped(self):
        """Volume passed to update_settings is clamped to [0.0, 1.0]."""
        fakes, _fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.mock_logger, enabled=True)

            af.update_settings(enabled=True, volume=2.5)
            self.assertEqual(af._volume, 1.0)

            af.update_settings(enabled=True, volume=-1.0)
            self.assertEqual(af._volume, 0.0)


if __name__ == "__main__":
    unittest.main()
