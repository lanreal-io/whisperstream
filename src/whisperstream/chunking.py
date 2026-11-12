"""Chunking utilities for long-audio recording support.

Pure-Python + numpy module. No sherpa-onnx, no I/O. Thread-safety in
ChunkBuffer uses threading.Lock (stdlib).

plan_chunks() — computes slice bounds for a buffer of known length.
merge_overlapping_transcripts() — word-level overlap dedup across chunk texts.
merge_timed_chunk_transcripts() — timing-aware seam ownership for aligned decodes.
ChunkBuffer — append-only audio accumulator that emits fixed-size chunks.
"""

from __future__ import annotations

import difflib
import math
import threading
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from .stt_contracts import DetailedTranscriptionResult


# Chunking is LOCKED, not user-configurable. Long recordings are always split
# into fixed 20-second windows (2s overlap) and merged. A larger or disabled
# window would silently truncate the Qwen backend (its decoder caps a single
# pass at max_total_len = 512 tokens, ~38s of audio) and inflate Parakeet
# latency/memory, so the value lives here rather than in user config. Both the
# live capture path (AudioCapture) and the full-waveform path
# (transcribe_waveform) use these. The overlap was raised from 1s to 2s: with
# timed seam ownership, Parakeet replays of real dictation left about a third
# fewer extra words at 2s, for ~6% more decode work. FluidAudio's Parakeet
# chunking also overlaps its windows by 2s.
CHUNK_DURATION_SECONDS = 20.0
CHUNK_OVERLAP_SECONDS = 2.0

# Seam dedup may only remove as many words as the shared audio can contain.
# Without this bound a phrase spoken twice across a chunk boundary matched the
# longer suffix/prefix search and its second occurrence was deleted (MAC-052).
# Four words per second covers fast dictation while leaving longer genuine
# repetitions intact.
MAX_OVERLAP_WORDS_PER_SECOND = 4


# Timed seam reconciliation (aligned decoders only). The two windows decode the
# same shared audio independently, so one spoken word gets a start time in
# each. Real Parakeet replays put those starts 0.04-0.36 s apart (the later
# window's edge runs late), so a match must allow that skew while staying far
# below the spacing of a phrase genuinely spoken twice. A clipped window edge
# can also report a word that began just before the shared audio.
SEAM_MATCH_TOLERANCE_SECONDS = 0.5
SEAM_EDGE_SLACK_SECONDS = 0.5


def chunk_hop_samples(
    sample_rate: int,
    chunk_duration: float = CHUNK_DURATION_SECONDS,
    chunk_overlap: float = CHUNK_OVERLAP_SECONDS,
) -> int:
    """Return the sample distance between successive chunk starts."""
    return int(chunk_duration * sample_rate) - int(chunk_overlap * sample_rate)


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


def _words_similar(a: str, b: str) -> bool:
    """Whether two seam keys plausibly name the same spoken word.

    Allows the variants independent decodes of shared audio produce: a word
    clipped at a window edge (``rese``/``research``) and small recognition
    differences (``two``/``to``).
    """
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):
        return True
    return min(len(a), len(b)) >= 2 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _text_seam_cut(
    prev_words: list[str], next_words: list[str], max_overlap: int
) -> Optional[tuple[int, int]]:
    """Find the shared-audio words of an untimed seam from text alone.

    Aligns the last ``max_overlap`` words of the earlier window with the first
    ``max_overlap`` of the later one (in-order LCS, case and punctuation
    ignored, near-miss words allowed). The matches must look like one stretch
    of shared audio: they reach both window edges (allowing one clipped edge
    word on each side) and cover most of the words between. The cut is the
    middle match. Returns ``(prev_keep_end, next_keep_start)`` or ``None``.
    """
    # ============================================================================
    # TRANSCRIPT DATA — DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    if max_overlap <= 0:
        return None
    tail, head = prev_words[-max_overlap:], next_words[:max_overlap]
    offset = len(prev_words) - len(tail)
    tail_keys = [_seam_key(w) for w in tail]
    head_keys = [_seam_key(w) for w in head]
    rows, cols = len(tail), len(head)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for a in range(rows - 1, -1, -1):
        for b in range(cols - 1, -1, -1):
            if _words_similar(tail_keys[a], head_keys[b]):
                table[a][b] = table[a + 1][b + 1] + 1
            else:
                table[a][b] = max(table[a + 1][b], table[a][b + 1])
    pairs: list[tuple[int, int]] = []
    a = b = 0
    while a < rows and b < cols:
        if _words_similar(tail_keys[a], head_keys[b]) and table[a][b] == table[a + 1][b + 1] + 1:
            pairs.append((a, b))
            a += 1
            b += 1
        elif table[a + 1][b] >= table[a][b + 1]:
            a += 1
        else:
            b += 1
    if not pairs:
        return None
    (first_a, first_b), (last_a, last_b) = pairs[0], pairs[-1]
    if rows - 1 - last_a > 1 or first_b > 1:
        return None
    if len(pairs) == 1 and not (last_a == rows - 1 and last_b == 0):
        return None
    if len(pairs) < math.ceil(0.6 * max(rows - first_a, last_b + 1)):
        return None
    a, b = pairs[len(pairs) // 2]
    return offset + a + 1, b + 1


def merge_overlapping_transcripts(
    chunks: list[str],
    max_overlap: Optional[int] = None,
) -> str:
    """Merge ordered chunk transcripts without timing information.

    Used for recognizers that report no word timings (Qwen, Cohere) and for
    any decode whose alignment is unusable. Each seam compares only as many
    words as the shared audio can hold (``max_overlap``, default
    :func:`max_overlap_words` of the locked overlap) via
    :func:`_text_seam_cut`, so case, punctuation, a clipped edge word or a
    small recognition difference no longer leave the shared speech twice. A
    phrase genuinely spoken twice survives when it is more than one word
    longer than that budget; shorter exact repeats landing on a seam
    can still collapse, because text alone cannot place them in time.

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
    if max_overlap is None:
        max_overlap = max_overlap_words(CHUNK_OVERLAP_SECONDS)
    merged: list[str] = []
    for chunk in chunks:
        words = chunk.split()
        if not words:
            continue
        if merged:
            cut = _text_seam_cut(merged, words, max_overlap)
            if cut is not None:
                merged = merged[:cut[0]]
                words = words[cut[1]:]
        merged.extend(words)
    return " ".join(merged)


@dataclass(frozen=True, slots=True)
class TimedChunkTranscript:
    """One final chunk decode with its absolute position in the recording.

    ``start_sample``/``end_sample`` bound the decoded audio, including the
    overlap it shares with its neighbours. Result timestamps are relative to
    ``start_sample``, in seconds.
    """

    start_sample: int
    end_sample: int
    result: DetailedTranscriptionResult

    def __post_init__(self) -> None:
        if not 0 <= self.start_sample <= self.end_sample:
            raise ValueError("chunk sample bounds must be nonnegative and ordered")


def _seam_key(word: str) -> str:
    """Case- and punctuation-insensitive identity used only for seam matching."""
    return "".join(ch for ch in word.casefold() if ch.isalnum())


def _timed_words(
    chunk: TimedChunkTranscript, sample_rate: int
) -> Optional[list[tuple[str, float]]]:
    """Return ``(word, absolute_start_seconds)`` for each word, or ``None``.

    ``None`` means the decode carries no usable alignment, so its seams must
    fall back to the text merger.
    """
    # ============================================================================
    # TRANSCRIPT DATA — DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    result = chunk.result
    words = result.text.split()
    if not words:
        return []
    tokens, times = result.tokens, result.timestamps
    if not tokens or times is None or len(tokens) != len(times):
        return None
    source = "".join(tokens).replace("\u2581", " ")
    if source.split() != words:
        return None
    # A word starts at the first non-space character after a boundary; its
    # time is the start of the token that carries that character.
    starts: list[float] = []
    at_boundary = True
    for token, offset in zip(tokens, times):
        for ch in token.replace("\u2581", " "):
            if ch.isspace():
                at_boundary = True
            elif at_boundary:
                starts.append(offset)
                at_boundary = False
    if len(starts) != len(words):
        return None
    base = chunk.start_sample / sample_rate
    absolute = [base + offset for offset in starts]
    if any(not math.isfinite(t) for t in absolute) or any(
        b < a for a, b in zip(absolute, absolute[1:])
    ):
        return None
    return list(zip(words, absolute))


def _timed_seam_cut(
    prev_words: list[tuple[str, float]],
    next_words: list[tuple[str, float]],
    shared_start: float,
    shared_end: float,
) -> Optional[tuple[int, int]]:
    """Find one spoken word both windows decoded, and split ownership there.

    Candidates are words near the shared audio whose normalized text matches
    and whose start times agree within ``SEAM_MATCH_TOLERANCE_SECONDS``. A
    time-constrained LCS pairs them in order, so repeated words pair with the
    same occurrence rather than a neighbouring one. The pair nearest the middle
    of the shared audio (where both windows have the most context) becomes the
    anchor. Returns ``(prev_keep_end, next_keep_start)`` word indices, or
    ``None`` when the windows share no anchor.
    """
    # ============================================================================
    # TRANSCRIPT DATA — DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    prev_idx = [
        i for i, (_, t) in enumerate(prev_words)
        if t >= shared_start - SEAM_EDGE_SLACK_SECONDS
    ]
    next_idx = [
        j for j, (_, t) in enumerate(next_words)
        if t < shared_end + SEAM_EDGE_SLACK_SECONDS
    ]
    if not prev_idx or not next_idx:
        return None
    prev_keys = [_seam_key(prev_words[i][0]) for i in prev_idx]
    next_keys = [_seam_key(next_words[j][0]) for j in next_idx]

    def matches(a: int, b: int) -> bool:
        return (
            bool(prev_keys[a])
            and prev_keys[a] == next_keys[b]
            and abs(prev_words[prev_idx[a]][1] - next_words[next_idx[b]][1])
            <= SEAM_MATCH_TOLERANCE_SECONDS
        )

    rows, cols = len(prev_idx), len(next_idx)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for a in range(rows - 1, -1, -1):
        for b in range(cols - 1, -1, -1):
            if matches(a, b):
                table[a][b] = table[a + 1][b + 1] + 1
            else:
                table[a][b] = max(table[a + 1][b], table[a][b + 1])
    pairs: list[tuple[int, int]] = []
    a = b = 0
    while a < rows and b < cols:
        if matches(a, b) and table[a][b] == table[a + 1][b + 1] + 1:
            pairs.append((prev_idx[a], next_idx[b]))
            a += 1
            b += 1
        elif table[a + 1][b] >= table[a][b + 1]:
            a += 1
        else:
            b += 1
    if not pairs:
        return None
    middle = (shared_start + shared_end) / 2
    i, j = min(
        pairs,
        key=lambda pair: abs(
            (prev_words[pair[0]][1] + next_words[pair[1]][1]) / 2 - middle
        ),
    )
    return i + 1, j + 1


def merge_timed_chunk_transcripts(
    chunks: Sequence[TimedChunkTranscript],
    *,
    sample_rate: int,
    max_overlap: Optional[int] = None,
) -> str:
    """Merge ordered chunk decodes, owning each moment of shared audio once.

    When every decode carries alignment, each seam keeps the earlier window
    up to a word both windows place at the same time and the later window
    after it. Punctuation, casing, recognition differences and overlap length
    then cannot leave a duplicate, and a phrase genuinely spoken twice stays
    because its occurrences sit at different times. A seam with no shared
    anchor uses the bounded text rule of :func:`merge_overlapping_transcripts`.
    Without alignment on any non-empty decode, that text merger runs as-is.
    """
    # ============================================================================
    # TRANSCRIPT DATA — DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    if max_overlap is None:
        max_overlap = max_overlap_words(CHUNK_OVERLAP_SECONDS)
    timed = [_timed_words(chunk, sample_rate) for chunk in chunks]
    if any(words is None for words in timed):
        return merge_overlapping_transcripts(
            [chunk.result.text for chunk in chunks], max_overlap
        )

    keep = [[0, len(words)] for words in timed]
    previous: Optional[int] = None
    for index, words in enumerate(timed):
        if not words:
            continue
        if previous is not None:
            prev_chunk, chunk = chunks[previous], chunks[index]
            prev_words = timed[previous]
            if prev_chunk.start_sample < chunk.start_sample < prev_chunk.end_sample:
                cut = _timed_seam_cut(
                    prev_words,
                    words,
                    chunk.start_sample / sample_rate,
                    prev_chunk.end_sample / sample_rate,
                )
                if cut is not None:
                    keep[previous][1] = max(keep[previous][0], cut[0])
                    keep[index][0] = cut[1]
                else:
                    # Bounded text rule, limited to words that sit in the
                    # shared audio so a repeat spoken seconds apart survives.
                    shared_start = chunk.start_sample / sample_rate
                    shared_end = prev_chunk.end_sample / sample_rate
                    kept_prev = prev_words[keep[previous][0]:keep[previous][1]]
                    limit = min(len(kept_prev), len(words), max_overlap)
                    for length in range(limit, 0, -1):
                        suffix, prefix = kept_prev[-length:], words[:length]
                        if (
                            suffix[0][1] >= shared_start - SEAM_EDGE_SLACK_SECONDS
                            and prefix[-1][1] < shared_end + SEAM_EDGE_SLACK_SECONDS
                            and [w.lower() for w, _ in suffix]
                            == [w.lower() for w, _ in prefix]
                        ):
                            keep[index][0] = length
                            break
        previous = index

    return " ".join(
        word
        for words, (lo, hi) in zip(timed, keep)
        for word, _ in words[lo:hi]
    )


def supports_timed_chunks(manager) -> bool:
    """Return whether *manager* declares aligned final chunk decodes."""
    capabilities = getattr(manager, "capabilities", None)
    return getattr(capabilities, "final_alignment", False) is True and callable(
        getattr(manager, "transcribe_chunk_detailed", None)
    )


def transcribe_waveform(
    manager,
    audio: np.ndarray,
    sample_rate: int,
    chunk_duration: float = CHUNK_DURATION_SECONDS,
    chunk_overlap: float = CHUNK_OVERLAP_SECONDS,
) -> str:
    """Transcribe a complete waveform through the chunked path.

    Splits *audio* into ``chunk_duration``-second windows (``chunk_overlap``
    overlap) via :func:`plan_chunks` and decodes each window. Managers that
    declare ``final_alignment`` return aligned decodes stitched by
    :func:`merge_timed_chunk_transcripts`; others use ``transcribe_chunk`` and
    :func:`merge_overlapping_transcripts`.

    This is the single entry point every full-recording caller uses (the
    ``--check --audio`` smoke path, the dev/server ``/api/transcribe``
    endpoints, the recording finalizer's no-chunk fallback) so a long clip is
    NEVER handed to one decode call — which would silently truncate the Qwen
    backend (~38s cap) and bloat Parakeet. The live recording path pre-chunks
    via :class:`ChunkBuffer` and calls ``transcribe_chunk`` per chunk, so it
    does not route through here.

    *manager* is any STT manager exposing ``transcribe_chunk(audio, *,
    sample_rate)`` (ParakeetManager or QwenManager), plus
    ``transcribe_chunk_detailed`` when it declares final alignment.
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
    if supports_timed_chunks(manager):
        timed = [
            TimedChunkTranscript(
                start,
                end,
                manager.transcribe_chunk_detailed(
                    audio[start:end], sample_rate=sample_rate
                ),
            )
            for start, end in bounds
        ]
        return merge_timed_chunk_transcripts(
            timed,
            sample_rate=sample_rate,
            max_overlap=max_overlap_words(chunk_overlap),
        )
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
