import logging
import sys
import unittest
from unittest.mock import MagicMock, patch

from whisperstream.audio_feedback import AudioFeedback


def _fake_pygame_modules():
    """Build a MagicMock tree for pygame + pygame.mixer suitable for sys.modules."""
    fake_mixer = MagicMock()
    fake_pygame = MagicMock()
    fake_pygame.mixer = fake_mixer
    return {"pygame": fake_pygame, "pygame.mixer": fake_mixer}, fake_mixer


class TestAudioFeedbackCache(unittest.TestCase):
    def setUp(self):
        self.logger = MagicMock(spec=logging.Logger)

    def test_caching_pygame(self):
        """Second play_start reuses the cached pygame.mixer.Sound object."""
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.logger, enabled=True)

            # First call — constructs + plays.
            af.play_start()
            # Second call — should hit cache path; no new Sound constructed.
            af.play_start()

            # Sound constructor invoked exactly once across both play_start calls.
            self.assertEqual(fake_mixer.Sound.call_count, 1)

            # The cached Sound's .play() was invoked for both calls.
            returned_sound = fake_mixer.Sound.return_value
            self.assertEqual(returned_sound.play.call_count, 2)

    def test_play_error_uses_winsound_messagebeep(self):
        """play_error with no override path triggers winsound.MessageBeep."""
        fakes, _fake_mixer = _fake_pygame_modules()
        fake_winsound = MagicMock()
        fake_winsound.MB_ICONHAND = 16

        merged = {**fakes, "winsound": fake_winsound}
        with patch.dict(sys.modules, merged):
            af = AudioFeedback(logger=self.logger, enabled=True)
            af.play_error()

            fake_winsound.MessageBeep.assert_called_once_with(16)


if __name__ == "__main__":
    unittest.main()
