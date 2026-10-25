"""Sentence-aligned chunking for incremental LLM cleanup.

Long dictations are cleaned chunk-by-chunk so each cleaned block can be pasted
as it completes (see ``main._enhance_then_inject``). Splitting happens ONLY on
sentence/paragraph boundaries, accumulating whole sentences up to a word target,
so a self-correction or list almost always stays inside one chunk and the cleanup
prompt's non-local decisions survive (validated in the round-03 spike). A small
trailing remainder folds into the previous chunk rather than becoming its own
tiny paste.

No external dependency: a regex on ``.!?`` / newlines plus word-count accumulation.
"""
from __future__ import annotations

import re
from typing import List, Optional

# ============================================================================
# TRANSCRIPT DATA — DO NOT LOG
# MAKE SURE you do not log transcript data anywhere. If you add a new function
# that receives, returns, or passes transcript text, COPY this banner to it.
# Use `log_transcription_event()` from whisperstream.logger for metadata only
# (char count, word count, duration, stage name). Never pass `text`,
# `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
# ============================================================================

# Split after sentence-ending punctuation followed by whitespace, or on any run
# of newlines. Keeps the punctuation attached to the sentence it ends.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")

# Production cleanup plan (round 03, chosen from the GPU spike). Keep these
# values here, beside the sentence planner, so live dictation and development
# evaluators cannot silently drift onto different chunk boundaries.
PRODUCTION_TARGET_WORDS = 120
PRODUCTION_FIRST_TARGET_WORDS = 60
PRODUCTION_MIN_TAIL_WORDS = 40


def _split_sentences(text: str) -> List[str]:
    # See TRANSCRIPT DATA banner above.
    return [part.strip() for part in _SENTENCE_BOUNDARY.split(text) if part.strip()]


def chunk_for_cleanup(
    text: str,
    *,
    target_words: int = 120,
    first_target_words: Optional[int] = None,
    min_tail_words: Optional[int] = None,
) -> List[str]:
    """Split *text* into sentence-aligned chunks of ~``target_words`` words.

    - Accumulates whole sentences; cuts a chunk once the running word count
      reaches the active target. Never splits mid-sentence (a single sentence
      longer than the target becomes its own chunk).
    - ``first_target_words`` (default = ``target_words``) sizes the FIRST chunk
      smaller so the first cleaned paragraph paints fast (the perceived-latency
      moment); every chunk after it uses ``target_words``.
    - A final remainder shorter than ``min_tail_words`` is appended to the
      previous chunk instead of emitted standalone. When omitted, the tail
      threshold is one third of ``target_words``.
    - Returns ``[]`` for empty/whitespace-only input, and ``[text.strip()]``
      for input with a single sentence (or no sentence boundaries).

    Re-joining the result with spaces preserves every sentence in order.
    """
    # See TRANSCRIPT DATA banner above.
    sentences = _split_sentences(text)
    if not sentences:
        return []

    first_target = first_target_words if first_target_words is not None else target_words
    # Keep the production 120 -> 40 behavior while scaling down with a user's
    # target. A fixed 40-word tail would otherwise swallow an entire extra
    # chunk when the configured target is small.
    tail_target = (
        min_tail_words
        if min_tail_words is not None
        else max(1, target_words // 3)
    )

    chunks: List[str] = []
    current: List[str] = []
    current_words = 0

    for sentence in sentences:
        current.append(sentence)
        current_words += len(sentence.split())
        # The first chunk cuts at first_target (small, for a fast first paint);
        # every chunk after uses the full target.
        active_target = first_target if not chunks else target_words
        if current_words >= active_target:
            chunks.append(" ".join(current))
            current = []
            current_words = 0

    if current:
        tail = " ".join(current)
        if chunks and current_words < tail_target:
            chunks[-1] = f"{chunks[-1]} {tail}"
        else:
            chunks.append(tail)

    return chunks


def production_cleanup_chunks(
    text: str,
    *,
    target_words: int = PRODUCTION_TARGET_WORDS,
    limit_first_chunk: bool = True,
) -> List[str]:
    """Return the configured sentence-aligned plan used by live cleanup.

    This plans chunks only. The live pipeline separately decides whether a
    transcript is long enough to activate incremental cleanup; evaluators may
    call this for any saved transcript, where a short input correctly produces
    one chunk.
    """
    # See TRANSCRIPT DATA banner above.
    return chunk_for_cleanup(
        text,
        target_words=target_words,
        first_target_words=(
            min(PRODUCTION_FIRST_TARGET_WORDS, target_words)
            if limit_first_chunk
            else target_words
        ),
        min_tail_words=max(1, target_words // 3),
    )
