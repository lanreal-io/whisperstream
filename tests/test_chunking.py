import unittest

import numpy as np


class TestPlanChunks(unittest.TestCase):
    """Tests for plan_chunks() — pure slice-boundary computation."""

    def test_total_samples_less_than_chunk_duration(self):
        """When total_samples < chunk_duration_samples, return a single span."""
        from whisperstream.chunking import plan_chunks

        # 1 second of audio at 16kHz, chunk_duration=20s
        result = plan_chunks(
            total_samples=16000,
            sample_rate=16000,
            chunk_duration=20.0,
            chunk_overlap=1.0,
        )
        self.assertEqual(result, [(0, 16000)])

    def test_chunk_duration_zero_returns_single_span(self):
        """When chunk_duration=0, chunking is disabled — return one span."""
        from whisperstream.chunking import plan_chunks

        result = plan_chunks(
            total_samples=960000,
            sample_rate=16000,
            chunk_duration=0.0,
            chunk_overlap=1.0,
        )
        self.assertEqual(result, [(0, 960000)])

    def test_60s_audio_20s_chunks_1s_overlap(self):
        """60s audio at 16kHz with 20s chunks and 1s overlap produces correct windows."""
        from whisperstream.chunking import plan_chunks

        # 60s * 16000 = 960000 samples
        # chunk = 320000 samples, overlap = 16000 samples, hop = 304000 samples
        # chunk 0: 0 .. 320000
        # chunk 1: 304000 .. 624000
        # chunk 2: 608000 .. 928000
        # chunk 3: 912000 .. 960000 (trailing, < full chunk)
        result = plan_chunks(
            total_samples=960000,
            sample_rate=16000,
            chunk_duration=20.0,
            chunk_overlap=1.0,
        )
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0], (0, 320000))
        self.assertEqual(result[1], (304000, 624000))
        self.assertEqual(result[2], (608000, 928000))
        self.assertEqual(result[3], (912000, 960000))
        # Each chunk is at most 20s long
        for start, end in result:
            self.assertLessEqual(end - start, 320000)
        # Last chunk ends exactly at total_samples
        self.assertEqual(result[-1][1], 960000)

    def test_overlap_zero_produces_non_overlapping_windows(self):
        """With overlap=0, chunks are contiguous and non-overlapping."""
        from whisperstream.chunking import plan_chunks

        # 40s at 16kHz = 640000, chunk=20s=320000, overlap=0, hop=320000
        result = plan_chunks(
            total_samples=640000,
            sample_rate=16000,
            chunk_duration=20.0,
            chunk_overlap=0.0,
        )
        self.assertEqual(result, [(0, 320000), (320000, 640000)])


class TestMergeOverlappingTranscripts(unittest.TestCase):
    """Tests for merge_overlapping_transcripts() — word-level overlap dedup."""

    def test_three_word_overlap_dedup(self):
        """Overlapping words at chunk boundary are deduplicated."""
        from whisperstream.chunking import merge_overlapping_transcripts

        result = merge_overlapping_transcripts(
            ["hello world how", "world how are you"]
        )
        self.assertEqual(result, "hello world how are you")

    def test_no_overlap_joins_with_space(self):
        """When chunks share no words, they are joined with a space."""
        from whisperstream.chunking import merge_overlapping_transcripts

        result = merge_overlapping_transcripts(["hello", "world"])
        self.assertEqual(result, "hello world")

    def test_empty_list_returns_empty_string(self):
        """An empty chunk list produces an empty string."""
        from whisperstream.chunking import merge_overlapping_transcripts

        result = merge_overlapping_transcripts([])
        self.assertEqual(result, "")

    def test_single_chunk_returned_as_is(self):
        """A single chunk is returned verbatim (stripped)."""
        from whisperstream.chunking import merge_overlapping_transcripts

        result = merge_overlapping_transcripts(["only one chunk"])
        self.assertEqual(result, "only one chunk")

    def test_case_insensitive_overlap(self):
        """Overlap matching is case-insensitive; chunk i's casing preserved on overlap,
        chunk i+1's casing used for the non-overlap continuation."""
        from whisperstream.chunking import merge_overlapping_transcripts

        # "brown" in chunk 1 (lowercase) matches "Brown" in chunk 2 (title case).
        # We keep chunk 1's text up to the overlap boundary, then append chunk 2's
        # non-overlapping tail.
        result = merge_overlapping_transcripts(
            ["The quick brown", "Brown fox jumps"]
        )
        # chunk 1 contributes "The quick brown", overlap is "brown"/"Brown" (1 word),
        # chunk 2 contributes "fox jumps" (the non-overlapping part).
        self.assertEqual(result, "The quick brown fox jumps")

    def test_repeated_phrase_across_seam_is_kept(self):
        """MAC-052: a ten-word phrase spoken twice must survive the seam."""
        from whisperstream.chunking import merge_overlapping_transcripts

        phrase = "please send the quarterly report to the whole finance team"
        result = merge_overlapping_transcripts([phrase, phrase])
        self.assertEqual(result, f"{phrase} {phrase}")

    def test_overlap_longer_than_seam_audio_is_not_removed(self):
        """Matches longer than the 1s overlap can hold are treated as speech."""
        from whisperstream.chunking import merge_overlapping_transcripts

        result = merge_overlapping_transcripts(
            ["we said one two three four five", "one two three four five again"]
        )
        self.assertEqual(
            result,
            "we said one two three four five one two three four five again",
        )

    def test_seam_overlap_within_bound_still_deduplicated(self):
        """A four-word seam duplicate is still removed by default."""
        from whisperstream.chunking import merge_overlapping_transcripts

        result = merge_overlapping_transcripts(
            ["and then we went to the", "we went to the store"]
        )
        self.assertEqual(result, "and then we went to the store")

    def test_max_overlap_words_follows_overlap_duration(self):
        from whisperstream.chunking import max_overlap_words

        self.assertEqual(max_overlap_words(1.0), 4)
        self.assertEqual(max_overlap_words(0.0), 0)
        self.assertEqual(max_overlap_words(0.1), 1)

    def test_zero_overlap_never_deduplicates(self):
        from whisperstream.chunking import merge_overlapping_transcripts

        result = merge_overlapping_transcripts(["go go", "go go"], max_overlap=0)
        self.assertEqual(result, "go go go go")


class TestChunkBuffer(unittest.TestCase):
    """Tests for ChunkBuffer — thread-safe append-only audio accumulator."""

    def test_chunk_emission_and_flush(self):
        """ChunkBuffer emits complete chunks and flush_tail returns the remainder.

        With chunk_duration=2.0, overlap=0.2 at sample_rate=100:
        - chunk_samples = 200, overlap_samples = 20, hop = 180
        Feed 500 samples at once:
        - chunk 0: 0..200 (emitted when we have >= 200)
        - chunk 1: 180..380 (emitted when we have >= 380 from start, i.e. 200 from retained)
        - After chunk 1 emitted, buffer retains from 360 onward = 140 samples
        - 140 < 200, so no more chunks from append
        - flush_tail returns the remaining 140 samples
        Reconstructing: 200 + 200 + 140 = 540 raw samples, minus overlaps (20*2)=40 = 500.
        """
        from whisperstream.chunking import ChunkBuffer

        buf = ChunkBuffer(sample_rate=100, chunk_duration=2.0, chunk_overlap=0.2)
        audio = np.arange(500, dtype=np.float32)
        emitted = buf.append(audio)

        # We expect 2 complete chunks
        self.assertEqual(len(emitted), 2)
        self.assertEqual(len(emitted[0]), 200)
        self.assertEqual(len(emitted[1]), 200)

        # Verify chunk content
        np.testing.assert_array_equal(emitted[0], audio[0:200])
        np.testing.assert_array_equal(emitted[1], audio[180:380])

        # Flush tail — should be from 360 onward
        tail = buf.flush_tail()
        self.assertIsNotNone(tail)
        np.testing.assert_array_equal(tail, audio[360:500])
        self.assertEqual(len(tail), 140)

        # Verify reconstruction: all emitted + tail, dedup overlaps = original
        # Chunk 0 has 0..200, chunk 1 has 180..380, tail has 360..500
        # Non-overlapping: 0..180 from chunk0, 180..360 from chunk1, 360..500 from tail
        reconstructed = np.concatenate([
            emitted[0][:180],  # non-overlap portion of chunk 0
            emitted[1][:180],  # non-overlap portion of chunk 1
            tail,
        ])
        np.testing.assert_array_equal(reconstructed, audio)

    def test_memory_bound(self):
        """Internal buffer never exceeds ~2x chunk_duration after emitting chunks."""
        from whisperstream.chunking import ChunkBuffer

        sr = 16000
        chunk_dur = 20.0
        overlap = 1.0
        buf = ChunkBuffer(sample_rate=sr, chunk_duration=chunk_dur, chunk_overlap=overlap)

        chunk_samples = int(chunk_dur * sr)
        max_allowed = chunk_samples * 2  # 2x safety factor

        # Feed 60s of audio in 100ms increments
        increment = int(0.1 * sr)  # 1600 samples
        total = int(60.0 * sr)
        fed = 0
        while fed < total:
            batch_size = min(increment, total - fed)
            batch = np.zeros(batch_size, dtype=np.float32)
            buf.append(batch)
            fed += batch_size
            self.assertLessEqual(
                buf._buffer_len(),
                max_allowed,
                f"Internal buffer exceeded 2x chunk_duration at {fed} samples fed",
            )

    def test_chunking_disabled_accumulates_all(self):
        """With chunk_duration=0, append() never emits; flush_tail() returns all."""
        from whisperstream.chunking import ChunkBuffer

        sr = 16000
        buf = ChunkBuffer(sample_rate=sr, chunk_duration=0.0, chunk_overlap=0.0)

        # Feed 60s in increments
        increment = int(0.1 * sr)
        total = int(60.0 * sr)
        all_emitted = []
        fed = 0
        while fed < total:
            batch_size = min(increment, total - fed)
            batch = np.zeros(batch_size, dtype=np.float32)
            emitted = buf.append(batch)
            all_emitted.extend(emitted)
            fed += batch_size

        # No chunks emitted during append
        self.assertEqual(len(all_emitted), 0)

        # flush_tail returns the full buffer
        tail = buf.flush_tail()
        self.assertIsNotNone(tail)
        self.assertEqual(len(tail), total)


class _FakeManager:
    """Minimal STT manager: records chunk sizes, returns canned text per chunk."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.chunk_sizes = []

    def transcribe_chunk(self, audio, *, sample_rate):
        self.chunk_sizes.append(audio.size)
        return self._texts.pop(0)


class TestTranscribeWaveform(unittest.TestCase):
    """Tests for transcribe_waveform() — the full-waveform chunked entry."""

    def test_short_audio_one_chunk(self):
        """Audio that fits in one window is transcribed in a single call."""
        from whisperstream.chunking import transcribe_waveform

        mgr = _FakeManager(["hello world"])
        audio = np.zeros(16000, dtype=np.float32)  # 1s at 16kHz
        result = transcribe_waveform(mgr, audio, 16000)

        self.assertEqual(result, "hello world")
        self.assertEqual(mgr.chunk_sizes, [16000])

    def test_long_audio_split_and_merged(self):
        """Audio longer than the locked window is split and merged in order."""
        from whisperstream.chunking import (
            CHUNK_DURATION_SECONDS,
            transcribe_waveform,
        )

        # 70s at 16kHz > 20s window -> multiple chunks.
        audio = np.zeros(70 * 16000, dtype=np.float32)
        mgr = _FakeManager(["a", "b", "c", "d", "e"])
        result = transcribe_waveform(mgr, audio, 16000)

        # More than one chunk, no shared boundary words -> space-joined in order.
        self.assertGreater(len(mgr.chunk_sizes), 1)
        # Every chunk is bounded by the locked window.
        for size in mgr.chunk_sizes:
            self.assertLessEqual(size, int(CHUNK_DURATION_SECONDS * 16000))
        n = len(mgr.chunk_sizes)
        self.assertEqual(result, " ".join(["a", "b", "c", "d", "e"][:n]))

    def test_uses_locked_constants_by_default(self):
        """Default chunk_duration/overlap come from the locked module constants."""
        from whisperstream import chunking

        self.assertEqual(chunking.CHUNK_DURATION_SECONDS, 20.0)
        self.assertEqual(chunking.CHUNK_OVERLAP_SECONDS, 1.0)


if __name__ == "__main__":
    unittest.main()
