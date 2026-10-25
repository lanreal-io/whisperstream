"""text_guards — conservative heuristic output safeguards for the LLM enhancement pipeline.

Three guards:
  1. Length explosion: enhanced output is longer than max(1.5x raw length, raw length + 50 chars).
  2. Sentence repetition: any sentence (≥4 words) appears 3+ times in output.
  3. Distinctive refusal phrases present in output but absent from raw input.

All functions are sync — safe to call from asyncio.run() on the hotkey path.
Stdlib-only: re, logging.
"""

import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)

_REFUSAL_PHRASES = (
    "as an ai",
    "as a language model",
    "i don't have the ability to",
    "i'm not able to fulfill",
    "i cannot assist with",
)


def _split_sentences(text: str) -> list[str]:
    """Split text on .!? followed by whitespace (or end of string).

    Strips empty/whitespace-only fragments. Private helper.
    """
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [p.strip() for p in parts if p.strip()]


def is_length_explosion(out: str, raw: str) -> bool:
    """Return True iff len(out) > max(len(raw) * 1.5, len(raw) + 50)."""
    out_chars = len(out)
    in_chars = len(raw)
    threshold = max(in_chars * 1.5, in_chars + 50)
    return out_chars > threshold


def _repetition_counts(text: str) -> Counter:
    """Return normalized counts for repetition-eligible sentences."""
    normalized = []
    for sentence in _split_sentences(text):
        words = sentence.casefold().split()
        if len(words) >= 4:
            normalized.append(" ".join(words))
    return Counter(normalized)


def has_sentence_repetition(out: str, raw: str = "") -> bool:
    """Return True iff output introduces a third-or-later copy of a sentence.

    Comparison is case-folded and whitespace-collapsed so trivial variants still count.
    Sentences with fewer than 4 whitespace-separated words are ignored. Repetition
    already present in ``raw`` is dictated content and therefore does not trip.
    """
    out_counts = _repetition_counts(out)
    raw_counts = _repetition_counts(raw)
    return any(
        count >= 3 and count > raw_counts[sentence]
        for sentence, count in out_counts.items()
    )


def has_refusal_not_in_input(out: str, raw: str) -> bool:
    """Return True iff any distinctive refusal phrase is in out but NOT in raw.

    Uses a case-folded substring check conditioned on the source. This is a
    heuristic, not a guarantee against false rejection after legitimate edits.
    """
    out_lower = out.casefold()
    raw_lower = raw.casefold()
    return any(
        phrase in out_lower and phrase not in raw_lower
        for phrase in _REFUSAL_PHRASES
    )


def validate_and_fallback_outcome(
    enhanced: str, raw_input: str, *, run_guards: bool
) -> tuple[str, bool]:
    """Return selected text plus whether a guard forced raw fallback."""
    if not run_guards:
        return enhanced, False

    try:
        if is_length_explosion(enhanced, raw_input):
            logger.warning("[Guards] tripped reason=length_explosion")
            return raw_input, True

        if has_sentence_repetition(enhanced, raw_input):
            logger.warning("[Guards] tripped reason=sentence_repetition")
            return raw_input, True

        if has_refusal_not_in_input(enhanced, raw_input):
            logger.warning("[Guards] tripped reason=refusal_not_in_input")
            return raw_input, True

        return enhanced, False
    except Exception:
        logger.warning("[Guards] tripped reason=unexpected_error", exc_info=True)
        return raw_input, True


def validate_and_fallback(enhanced: str, raw_input: str, *, run_guards: bool) -> str:
    """Run all three guards; return raw_input if any trip, else return enhanced.

    If run_guards is False, returns enhanced unchanged (cloud mode with toggle off).
    Wraps the guard calls in try/except so guards can never raise into the caller.
    Logs at WARNING with tag [Guards] when a guard trips.
    """
    return validate_and_fallback_outcome(
        enhanced, raw_input, run_guards=run_guards
    )[0]
