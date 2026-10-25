"""Chunking utilities for long-audio recording support.

Pure-Python + numpy module. No sherpa-onnx, no I/O. Thread-safety in
ChunkBuffer uses threading.Lock (stdlib).

plan_chunks() — computes slice bounds for a buffer of known length.
merge_overlapping_transcripts() — word-level overlap dedup across chunk texts.
ChunkBuffer — append-only audio accumulator that emits fixed-size chunks.
"""

from __future__ import annotations

import math
import threading
from typing import Optional

import numpy as np


# Chunking is LOCKED, not user-configurable. Long recordings are always split
# into fixed 20-second windows (1s overlap) and merged. A larger or disabled
# window would silently truncate the Qwen backend (its decoder caps a single
# pass at max_total_len = 512 tokens, ~38s of audio) and inflate Parakeet
# latency/memory, so the value lives here rather than in user config. Both the
# live capture path (AudioCapture) and the full-waveform path
# (transcribe_waveform) use these.
CHUNK_DURATION_SECONDS = 20.0
CHUNK_OVERLAP_SECONDS = 1.0

# Seam dedup may only remove as many words as the shared audio can contain.
# Without this bound a phrase spoken twice across a chunk boundary matched the
# longer suffix/prefix search and its second occurrence was deleted (MAC-052).
# Four words per second covers fast dictation while leaving longer genuine
# repetitions intact.
MAX_OVERLAP_WORDS_PER_SECOND = 4


def max_overlap_words(chunk_overlap: float) -> int:
    """Return the most words a seam of ``chunk_overlap`` seconds may dedup."""
    if chunk_overlap <= 0:
        return 0
    return max(1, math.ceil(chunk_overlap * MAX_OVERLAP_WORDS_PER_SECOND))


def plan_chunks(
    total_samples: int,
    sample_rate: int,
    chunk_duration: float,
    chunk_overlap: float,
) -> list[tuple[int, int]]:
    """Return inclusive-exclusive (start, end) sample bounds for overlapping chunks.

    When chunk_duration <= 0 or total_samples fits in one chunk, returns a
    single ``(0, total_samples)`` entry.
    """
    if chunk_duration <= 0:
        return [(0, total_samples)]

    chunk_samples = int(chunk_duration * sample_rate)
    overlap_samples = int(chunk_overlap * sample_rate)
    hop = chunk_samples - overlap_samples

    if total_samples <= chunk_samples:
        return [(0, total_samples)]

    bounds: list[tuple[int, int]] = []
    start = 0
    while start < total_samples:
        end = min(start + chunk_samples, total_samples)
        bounds.append((start, end))
        start += hop
        # If end already reached total_samples, stop — the trailing chunk is
        # handled by the min() above.
        if end == total_samples:
            break

    return bounds


def merge_overlapping_transcripts(
    chunks: list[str],
    max_overlap: Optional[int] = None,
) -> str:
    """Merge ordered chunk transcripts with overlap-aware word-level dedup.

    Uses a longest-common-suffix/prefix search between the tail of chunk *i*
    and the head of chunk *i+1*, limited to ``max_overlap`` words (default:
    what the locked chunk overlap can hold, see :func:`max_overlap_words`) so a
    longer phrase genuinely spoken twice is never collapsed. Matching is
    case-insensitive. Chunk *i*'s text is preserved through the overlap
    boundary; chunk *i+1* contributes only the words after the overlap.

    If no overlap is found, chunks are joined with a single space.
    """
    # ============================================================================
    # TRANSCRIPT DATA — DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    if not chunks:
        return ""
    if max_overlap is None:
        max_overlap = max_overlap_words(CHUNK_OVERLAP_SECONDS)

    merged = chunks[0]

    for i in range(1, len(chunks)):
        prev_words = merged.split()
        next_words = chunks[i].split()

        if not prev_words or not next_words:
            # One side is empty — just concatenate.
            if next_words:
                merged = (merged + " " + chunks[i]).strip()
            continue

        # Search for the longest common suffix of prev / prefix of next that
        # fits inside the audio overlap.
        max_search = min(len(prev_words), len(next_words), max_overlap)
        best_overlap = 0

        for length in range(1, max_search + 1):
            suffix = [w.lower() for w in prev_words[-length:]]
            prefix = [w.lower() for w in next_words[:length]]
            if suffix == prefix:
                best_overlap = length

        if best_overlap > 0:
            # Append only the non-overlapping tail of chunk i+1.
            tail = next_words[best_overlap:]
            if tail:
                merged = merged + " " + " ".join(tail)
        else:
            # No overlap detected — fall back to space join.
            merged = merged + " " + chunks[i]

    return merged.strip()


def transcribe_waveform(
    manager,
    audio: np.ndarray,
    sample_rate: int,
    chunk_duration: float = CHUNK_DURATION_SECONDS,
    chunk_overlap: float = CHUNK_OVERLAP_SECONDS,
) -> str:
    """Transcribe a complete waveform through the chunked path.

    Splits *audio* into ``chunk_duration``-second windows (``chunk_overlap``
    overlap) via :func:`plan_chunks`, runs each window through
    ``manager.transcribe_chunk``, and stitches the results with
    :func:`merge_overlapping_transcripts`.

    This is the single entry point every full-recording caller uses (the
    ``--check --audio`` smoke path, the dev/server ``/api/transcribe``
    endpoints, the recording finalizer's no-chunk fallback) so a long clip is
    NEVER handed to one decode call — which would silently truncate the Qwen
    backend (~38s cap) and bloat Parakeet. The live recording path pre-chunks
    via :class:`ChunkBuffer` and calls ``transcribe_chunk`` per chunk, so it
    does not route through here.

    *manager* is any STT manager exposing ``transcribe_chunk(audio, *,
    sample_rate)`` (ParakeetManager or QwenManager).
    """
    # ============================================================================
    # TRANSCRIPT DATA — DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    if audio.ndim > 1:
        audio = audio.reshape(-1)
    bounds = plan_chunks(audio.size, sample_rate, chunk_duration, chunk_overlap)
    parts = [
        manager.transcribe_chunk(audio[start:end], sample_rate=sample_rate)
        for start, end in bounds
    ]
    return merge_overlapping_transcripts(parts, max_overlap_words(chunk_overlap))


class ChunkBuffer:
    """Thread-safe, append-only audio accumulator that emits fixed-size chunks.

    Internally stores audio in a pre-allocated numpy ring that is trimmed
    after each chunk emission so memory stays bounded by roughly
    ``chunk_duration * sample_rate * 4`` bytes.

    When ``chunk_duration <= 0``, chunking is disabled: ``append()`` never
    emits and ``flush_tail()`` returns the full accumulated buffer.
    """

    def __init__(
        self,
        sample_rate: int,
        chunk_duration: float,
        chunk_overlap: float,
    ) -> None:
        self._sample_rate = sample_rate
        self._chunk_duration = chunk_duration
        self._chunk_overlap = chunk_overlap

        self._chunk_samples = int(chunk_duration * sample_rate) if chunk_duration > 0 else 0
        self._overlap_samples = int(chunk_overlap * sample_rate) if chunk_duration > 0 else 0
        self._hop = self._chunk_samples - self._overlap_samples if self._chunk_samples > 0 else 0

        self._lock = threading.Lock()
        # _buf holds accumulated audio; _pos tracks the write cursor.
        self._buf: list[np.ndarray] = []
        self._buf_size: int = 0

    # -- public API --

    def append(self, frames: np.ndarray) -> list[np.ndarray]:
        """Append *frames* and return any fully-formed chunks ready to transcribe.

        Each returned chunk is an independent float32 array. Successive chunks
        share ``chunk_overlap`` worth of audio at their boundaries.
        """
        with self._lock:
            self._buf.append(frames.astype(np.float32, copy=False))
            self._buf_size += len(frames)

            if self._chunk_samples <= 0:
                # Chunking disabled — accumulate everything, emit nothing.
                return []

            emitted: list[np.ndarray] = []
            while self._buf_size >= self._chunk_samples:
                # Materialize the current buffer into a contiguous array so we
                # can slice cleanly.
                flat = np.concatenate(self._buf)
                chunk = flat[: self._chunk_samples].copy()
                emitted.append(chunk)

                # Retain only the overlap region for the next chunk.
                remainder = flat[self._hop :]
                self._buf = [remainder] if len(remainder) > 0 else []
                self._buf_size = len(remainder)

            return emitted

    def flush_tail(self) -> Optional[np.ndarray]:
        """Return whatever audio hasn't been emitted as a chunk yet.

        Called on user release. Returns ``None`` if the buffer is empty (e.g.
        the recording ended exactly on a chunk boundary).
        """
        with self._lock:
            if self._buf_size == 0:
                return None
            flat = np.concatenate(self._buf).astype(np.float32, copy=False)
            self._buf = []
            self._buf_size = 0
            return flat

    def reset(self) -> None:
        """Clear all internal state."""
        with self._lock:
            self._buf = []
            self._buf_size = 0

    def _buffer_len(self) -> int:
        """Return the number of samples currently in the buffer (test helper)."""
        with self._lock:
            return self._buf_size
