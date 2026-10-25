import logging
import threading
import unittest
from unittest.mock import MagicMock, patch
import io
import sys
import types

import pytest

pytestmark = pytest.mark.usefixtures("accepted_eula")

# Mock sounddevice globally because it's missing in the test environment
# and imported at top-level by whisperstream.audio_capture
if "sounddevice" not in sys.modules:
    mock_sd = types.ModuleType("sounddevice")
    mock_sd.InputStream = MagicMock()
    sys.modules["sounddevice"] = mock_sd

# Mock winsound if on Windows (but we are likely on Linux here, but just in case)
if sys.platform == "win32" and "winsound" not in sys.modules:
    mock_winsound = types.ModuleType("winsound")
    sys.modules["winsound"] = mock_winsound

from whisperstream.main import WhisperstreamApp
from whisperstream.server import WhisperstreamServer


class TestLoggingSecurity(unittest.TestCase):
    @patch("whisperstream.text_injector.time.sleep")
    @patch("whisperstream.text_injector.pyperclip")
    @patch("whisperstream.stt_factory.is_present", return_value=True)
    @patch("whisperstream.stt_factory.ParakeetManager")
    @patch("whisperstream.main.AudioCapture")
    @patch("whisperstream.main.AudioFeedback")
    @patch("whisperstream.main.KeyboardShortcutManager")
    @patch("whisperstream.main.ConfigManager")
    def test_sensitive_transcription_not_logged_at_info(self, mock_config, mock_keyboard, mock_feedback, mock_capture, mock_parakeet, mock_is_present, mock_pyperclip, mock_sleep):
        """Verify that sensitive transcription text is NOT logged at INFO level."""
        # Setup mocks
        mock_config_instance = mock_config.return_value
        mock_config_instance.load.return_value.parakeet_model = "test-model"
        mock_config_instance.load.return_value.parakeet_quantization = None
        mock_config_instance.load.return_value.onnx_providers = "cpu"
        mock_config_instance.load.return_value.threads = 1
        mock_config_instance.load.return_value.word_boosts = []
        mock_config_instance.load.return_value.word_filters = []
        mock_config_instance.load.return_value.word_replacements = {}
        mock_config_instance.load.return_value.post_processing = ""
        mock_config_instance.load.return_value.clipboard_behavior = False
        mock_config_instance.load.return_value.clipboard_clear_delay = 1.0
        mock_config_instance.load.return_value.max_recording_duration = 45.0
        mock_config_instance.load.return_value.audio_feedback = True
        mock_config_instance.load.return_value.audio_feedback_volume = 1.0
        mock_config_instance.load.return_value.start_sound_path = None
        mock_config_instance.load.return_value.stop_sound_path = None
        mock_config_instance.load.return_value.model_timeout = 300.0
        mock_config_instance.model_dir.return_value = "models/test-model"

        # Capture logs
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.INFO)

        # Configure the whisperstream logger
        logger = logging.getLogger("whisperstream")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        # Initialize app
        with (
            patch("whisperstream.speech.controller.SpeechController"),
            patch("whisperstream.main.make_stt_manager"),
        ):
            app = WhisperstreamApp()

        # Simulate transcription
        sensitive_text = "My secret password is hunter2"
        app.stt_engine.transcribe_chunk.return_value = sensitive_text

        # Simulate stop recording which triggers transcribe
        import numpy as np
        waveform = np.zeros(16000)
        app._chunk_futures = []
        app._chunk_start_time = 0.0
        app._last_waveform = waveform
        app._finalize_transcription_and_inject()

        # Check logs
        log_contents = log_capture.getvalue()

        # Assert that sensitive text is NOT in the logs
        self.assertNotIn(sensitive_text, log_contents, "Sensitive transcription text leaked into INFO logs!")

        # Verify that it IS logged if we enable DEBUG (optional, to verify it's not lost completely)
        # But we can't easily change logger level mid-run and re-capture without clearing, so we skip that for now.
        # The important part is security.


def test_preview_payload_is_not_logged_or_misclassified(caplog):
    """Even DEBUG WebSocket logging may retain only the preview event type."""
    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._app = types.SimpleNamespace()
    from whisperstream.server.websocket import DictationStateCache, WebSocketDelivery

    server._dictation_state = DictationStateCache()
    server.stats = MagicMock()
    server._loop = None
    server._logger = logging.getLogger("whisperstream.preview-security-test")
    server._socket_delivery = WebSocketDelivery(
        logger=server._logger,
        acceptance_is_current=server._acceptance_is_current,
        bakeoff_is_current=lambda grant: True,
    )
    sentinel = "PREVIEW_LOG_SENTINEL_9b57d"

    with caplog.at_level(logging.DEBUG, logger=server._logger.name):
        server.broadcast(
            {
                "type": "transcription.preview",
                "recording_id": 5,
                "revision": 1,
                "text": sentinel,
                "stable_char_count": 0,
            }
        )

    assert sentinel not in caplog.text
    assert "ws broadcast type=transcription.preview clients=0" in caplog.text
    server.stats.record_transcription.assert_not_called()
    assert server._dictation_state.snapshot() is None


if __name__ == "__main__":
    unittest.main()
