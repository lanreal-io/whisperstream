"""Tests for AudioCapture chunking integration.

Patches sounddevice.InputStream so no real audio hardware is needed.
Verifies that AudioCapture emits chunks via chunk_callback during recording
and flushes the tail on stop().
"""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np


class TestAudioCaptureChunking(unittest.TestCase):
    """AudioCapture with chunking enabled emits chunks via chunk_callback."""

    def setUp(self) -> None:
        self._device_query_patch = patch(
            "whisperstream.audio_capture._query_native_input_devices",
            return_value=[
                SimpleNamespace(
                    logical_id="logical-default",
                    is_default=True,
                )
            ],
        )
        self._device_query_patch.start()

    def tearDown(self) -> None:
        self._device_query_patch.stop()

    @patch("whisperstream.audio_capture.sd.InputStream")
    def test_initial_discard_gate_excludes_all_consumers_until_armed(
        self, mock_input_stream_cls
    ):
        from whisperstream.audio_capture import AudioCapture

        captured_callback = None

        def fake_init(**kwargs):
            nonlocal captured_callback
            captured_callback = kwargs.get("callback")
            mock_stream = MagicMock()
            mock_stream.device = 0
            mock_stream.samplerate = 16000.0
            return mock_stream

        mock_input_stream_cls.side_effect = fake_init
        chunks: list[np.ndarray] = []
        levels: list[float] = []
        previews: list[bool] = []
        ac = AudioCapture(
            sample_rate=16000,
            chunk_callback=lambda chunk: chunks.append(chunk.copy()),
            chunk_duration=0.5,
        )
        ac.set_level_callback(levels.append)
        ac.set_preview_callback(lambda: previews.append(True), max_duration=1.0)
        ac.start(discard_until_armed=True)

        cue_audio = np.ones((100, 1), dtype=np.float32)
        captured_callback(cue_audio, 100, None, None)
        self.assertEqual(ac.preview_snapshot().audio.size, 0)
        self.assertEqual(chunks, [])
        self.assertEqual(levels, [])
        self.assertEqual(previews, [])

        app_audio = np.full((25, 1), 0.25, dtype=np.float32)
        ac.arm_capture()
        captured_callback(app_audio, 25, None, None)
        waveform = ac.stop()

        np.testing.assert_array_equal(waveform, app_audio.reshape(-1))
        np.testing.assert_array_equal(ac.preview_snapshot().audio, np.empty(0))
        self.assertEqual(levels, [1.0])
        self.assertEqual(previews, [True])
        self.assertEqual(len(chunks), 1)
        np.testing.assert_array_equal(chunks[0], app_audio.reshape(-1))

    @patch("whisperstream.audio_capture.sd.InputStream")
    def test_chunking_emits_expected_chunks_and_tail(self, mock_input_stream_cls):
        """Feed 500 synthetic samples (sample_rate=16000, chunk_duration=.0125,
        overlap=.00125) and verify the callback receives the expected number of
        chunks plus the tail from stop().

        chunk_samples = 200, overlap_samples = 20, hop = 180
        Feed 500 samples total.
        - After 200 samples: chunk 0 emitted (samples 0..200)
        - After 380 samples (200 + 180): chunk 1 emitted (samples 180..380)
          Actually: buffer after chunk 0 has 20 samples overlap + new frames.
          We feed 500 in one go, so append(500) sees 500 >= 200 -> emit chunk 0,
          remainder = 500 - 180 = 320 samples. 320 >= 200 -> emit chunk 1,
          remainder = 320 - 180 = 140 samples. 140 < 200 -> stop.
        So append yields 2 chunks, flush_tail yields the remaining 140 samples.
        Total chunks via callback: 2 + 1 tail = 3.
        """
        from whisperstream.audio_capture import AudioCapture

        # Capture the sounddevice callback so we can invoke it manually.
        captured_callback = None

        def fake_init(**kwargs):
            nonlocal captured_callback
            captured_callback = kwargs.get("callback")
            mock_stream = MagicMock()
            mock_stream.device = 0
            mock_stream.samplerate = 16000.0
            return mock_stream

        mock_input_stream_cls.side_effect = fake_init

        # Collect chunks delivered via the callback.
        received_chunks: list[np.ndarray] = []
        chunk_event = threading.Event()

        def chunk_collector(chunk: np.ndarray) -> None:
            received_chunks.append(chunk.copy())
            chunk_event.set()

        ac = AudioCapture(
            sample_rate=16000,
            channels=1,
            dtype="float32",
            chunk_callback=chunk_collector,
            chunk_duration=0.0125,
            chunk_overlap=0.00125,
        )
        ac.start()
        self.assertIsNotNone(captured_callback, "sounddevice callback not captured")

        # Synthesize 500 samples in the canonical 16 kHz format.
        audio = np.arange(500, dtype=np.float32).reshape(-1, 1)

        # Simulate the sounddevice callback delivering all 500 samples at once.
        captured_callback(audio, 500, None, None)

        # Give the chunk dispatch thread(s) a moment to run.
        # We expect 2 chunks from append().
        for _ in range(20):
            if len(received_chunks) >= 2:
                break
            chunk_event.wait(timeout=0.1)
            chunk_event.clear()

        self.assertEqual(
            len(received_chunks), 2,
            f"Expected 2 chunks from append(), got {len(received_chunks)}",
        )

        # Each emitted chunk should be chunk_samples=200 long.
        self.assertEqual(len(received_chunks[0]), 200)
        self.assertEqual(len(received_chunks[1]), 200)

        # Now stop — this should flush the tail and deliver it via callback.
        full_waveform = ac.stop()

        # After stop, we should have 3 total chunks (2 from append + 1 tail).
        # Give the tail callback a moment if it's async.
        self.assertEqual(
            len(received_chunks), 3,
            f"Expected 3 total chunks (2 + tail), got {len(received_chunks)}",
        )

        # The tail should be the remaining 140 samples.
        self.assertEqual(len(received_chunks[2]), 140)

        # Verify the full waveform is still returned for backward compat.
        self.assertEqual(len(full_waveform), 500)

        # Verify that all original samples are covered: reconstruct from chunks
        # by deduplicating overlap regions.
        # chunk 0: indices 0..200, chunk 1: indices 180..380, tail: indices 360..500
        # After overlap dedup: 0..200 + 200..380 + 380..500 = 500 samples
        # (overlap region 180..200 from chunk 0 == 0..20 from chunk 1)
        # Simplest check: total unique samples across chunks equals 500.
        overlap_samples = 20
        total_unique = (
            len(received_chunks[0])
            + (len(received_chunks[1]) - overlap_samples)
            + (len(received_chunks[2]) - overlap_samples)
        )
        self.assertEqual(total_unique, 500)

    @patch("whisperstream.audio_capture.sd.InputStream")
    def test_chunking_disabled_no_callback_invoked(self, mock_input_stream_cls):
        """With chunk_duration=0, chunk_callback is never called and stop()
        returns the full buffer as before.
        """
        from whisperstream.audio_capture import AudioCapture

        captured_callback = None

        def fake_init(**kwargs):
            nonlocal captured_callback
            captured_callback = kwargs.get("callback")
            mock_stream = MagicMock()
            mock_stream.device = 0
            mock_stream.samplerate = 16000.0
            return mock_stream

        mock_input_stream_cls.side_effect = fake_init

        received_chunks: list[np.ndarray] = []

        def chunk_collector(chunk: np.ndarray) -> None:
            received_chunks.append(chunk.copy())

        ac = AudioCapture(
            sample_rate=16000,
            channels=1,
            dtype="float32",
            chunk_callback=chunk_collector,
            chunk_duration=0.0,
            chunk_overlap=0.0,
        )
        ac.start()
        self.assertIsNotNone(captured_callback)

        audio = np.arange(500, dtype=np.float32).reshape(-1, 1)
        captured_callback(audio, 500, None, None)

        full_waveform = ac.stop()

        # chunk_callback should NOT have been called at all (neither during
        # recording nor from stop's tail flush).
        self.assertEqual(
            len(received_chunks), 0,
            "chunk_callback should not be invoked when chunking is disabled",
        )

        # Full waveform still returned.
        self.assertEqual(len(full_waveform), 500)

    @patch("whisperstream.audio_capture.sd.InputStream")
    def test_chunking_enabled_no_callback_no_errors(self, mock_input_stream_cls):
        """Chunking enabled but no chunk_callback provided — no errors,
        accumulate into _frames and return full buffer from stop().
        """
        from whisperstream.audio_capture import AudioCapture

        captured_callback = None

        def fake_init(**kwargs):
            nonlocal captured_callback
            captured_callback = kwargs.get("callback")
            mock_stream = MagicMock()
            mock_stream.device = 0
            mock_stream.samplerate = 16000.0
            return mock_stream

        mock_input_stream_cls.side_effect = fake_init

        ac = AudioCapture(
            sample_rate=16000,
            channels=1,
            dtype="float32",
            # chunk_callback is NOT set (defaults to None)
            chunk_duration=0.0125,
            chunk_overlap=0.00125,
        )
        ac.start()
        self.assertIsNotNone(captured_callback)

        audio = np.arange(500, dtype=np.float32).reshape(-1, 1)
        captured_callback(audio, 500, None, None)

        # Should not raise — graceful degradation.
        full_waveform = ac.stop()

        # Full waveform returned as usual.
        self.assertEqual(len(full_waveform), 500)

    @patch("whisperstream.audio_capture.sd.InputStream")
    def test_preview_ring_is_bounded_without_changing_authoritative_audio(
        self, mock_input_stream_cls
    ):
        """Preview capture keeps only its bounded tail while the locked
        authoritative ChunkBuffer and full stop waveform remain unchanged.
        """
        from whisperstream.audio_capture import AudioCapture

        captured_callback = None

        def fake_init(**kwargs):
            nonlocal captured_callback
            captured_callback = kwargs.get("callback")
            mock_stream = MagicMock()
            mock_stream.device = 0
            mock_stream.samplerate = 16000.0
            return mock_stream

        mock_input_stream_cls.side_effect = fake_init
        chunks: list[np.ndarray] = []
        chunk_event = threading.Event()
        preview_notifications = 0

        def collect_chunk(chunk: np.ndarray) -> None:
            chunks.append(chunk.copy())
            chunk_event.set()

        def notify_preview() -> None:
            nonlocal preview_notifications
            preview_notifications += 1

        ac = AudioCapture(
            sample_rate=16000,
            chunk_callback=collect_chunk,
            chunk_duration=0.0125,
            chunk_overlap=0.00125,
        )
        ac.set_preview_callback(notify_preview, max_duration=0.0125)
        ac.start()

        audio = np.arange(500, dtype=np.float32).reshape(-1, 1)
        captured_callback(audio, 500, None, None)
        for _ in range(20):
            if len(chunks) >= 2:
                break
            chunk_event.wait(timeout=0.1)
            chunk_event.clear()

        preview = ac.preview_snapshot()
        self.assertEqual(preview_notifications, 1)
        self.assertEqual(preview.total_samples, 500)
        self.assertEqual(preview.start_sample, 300)
        np.testing.assert_array_equal(
            preview.audio, np.arange(300, 500, dtype=np.float32)
        )

        full_waveform = ac.stop()
        np.testing.assert_array_equal(
            full_waveform, np.arange(500, dtype=np.float32)
        )
        self.assertEqual([len(chunk) for chunk in chunks], [200, 200, 140])
        np.testing.assert_array_equal(chunks[0], np.arange(0, 200, dtype=np.float32))
        np.testing.assert_array_equal(chunks[1], np.arange(180, 380, dtype=np.float32))
        np.testing.assert_array_equal(chunks[2], np.arange(360, 500, dtype=np.float32))

    @patch("whisperstream.audio_capture.sd.InputStream")
    def test_disabled_preview_allocates_no_ring_or_notifications(
        self, mock_input_stream_cls
    ):
        from whisperstream.audio_capture import AudioCapture

        captured_callback = None

        def fake_init(**kwargs):
            nonlocal captured_callback
            captured_callback = kwargs.get("callback")
            mock_stream = MagicMock()
            mock_stream.device = 0
            mock_stream.samplerate = 16000.0
            return mock_stream

        mock_input_stream_cls.side_effect = fake_init
        ac = AudioCapture(sample_rate=16000)
        ac.start()
        captured_callback(np.ones((50, 1), dtype=np.float32), 50, None, None)

        preview = ac.preview_snapshot()
        self.assertEqual(preview.audio.size, 0)
        self.assertEqual(preview.total_samples, 0)
        self.assertEqual(preview.start_sample, 0)
        self.assertEqual(ac.stop().size, 50)


if __name__ == "__main__":
    unittest.main()
