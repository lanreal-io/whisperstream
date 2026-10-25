"""Tests for the sentence-aligned cleanup chunker (cleanup_chunker.py)."""
from __future__ import annotations

import re

from whisperstream.cleanup_chunker import (
    _split_sentences,
    chunk_for_cleanup,
    production_cleanup_chunks,
)


def _wc(s: str) -> int:
    return len(s.split())


def test_empty_input_returns_empty_list():
    assert chunk_for_cleanup("") == []
    assert chunk_for_cleanup("   \n  ") == []


def test_short_text_returns_single_chunk():
    text = "This is a short sentence. And another one here."
    chunks = chunk_for_cleanup(text, target_words=120)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_single_long_sentence_not_split_midsentence():
    # One sentence well over target, with no internal boundary -> stays whole.
    sentence = "word " * 300 + "end."
    chunks = chunk_for_cleanup(sentence.strip(), target_words=120)
    assert len(chunks) == 1
    assert chunks[0].endswith("end.")


def test_multi_sentence_splits_near_target():
    # 40 sentences x ~12 words = ~480 words; target 120 -> ~4 chunks.
    sentences = [
        f"This is sentence number {i} with several filler words padding it out."
        for i in range(40)
    ]
    text = " ".join(sentences)
    chunks = chunk_for_cleanup(text, target_words=120, min_tail_words=40)

    assert len(chunks) >= 3
    # Every chunk except possibly the last clears the target.
    for ch in chunks[:-1]:
        assert _wc(ch) >= 120
    # No chunk splits mid-sentence: each ends with sentence punctuation.
    for ch in chunks:
        assert re.search(r"[.!?]$", ch), f"chunk does not end on a boundary: {ch[-30:]!r}"


def test_tiny_tail_folds_into_previous_chunk():
    # Build sentences so the final remainder is a single short sentence below
    # min_tail_words. With target=20 and ~6-word sentences, chunks cut every
    # ~4 sentences; the lone trailing sentence (6 words) folds back.
    sentences = [f"Sentence {i} has six little words." for i in range(9)]
    sentences.append("Thanks.")  # 1-word tail
    text = " ".join(sentences)
    chunks = chunk_for_cleanup(text, target_words=20, min_tail_words=10)

    # The tiny "Thanks." must not be its own chunk.
    assert chunks[-1] != "Thanks."
    assert chunks[-1].endswith("Thanks.")
    assert not any(ch.strip() == "Thanks." for ch in chunks)


def test_tail_kept_standalone_when_large_enough():
    # A trailing remainder >= min_tail_words is emitted as its own chunk.
    sentences = [f"This is sentence number {i} with several filler words here." for i in range(13)]
    text = " ".join(sentences)
    chunks = chunk_for_cleanup(text, target_words=60, min_tail_words=5)
    # Reconstruct: nothing dropped.
    assert " ".join(chunks).split() == text.split()


def test_rejoin_preserves_all_sentences_in_order():
    sentences = [
        f"Sentence {i} carries some unique marker word zeta{i} for tracking."
        for i in range(25)
    ]
    text = " ".join(sentences)
    chunks = chunk_for_cleanup(text, target_words=80, min_tail_words=20)

    # Re-split the joined chunks and compare to the original sentence list.
    rejoined = " ".join(chunks)
    assert _split_sentences(rejoined) == _split_sentences(text)
    # Markers all present exactly once, in order.
    markers_in = re.findall(r"zeta\d+", text)
    markers_out = re.findall(r"zeta\d+", rejoined)
    assert markers_in == markers_out


def test_first_chunk_smaller_with_first_target_words():
    """first_target_words sizes the first chunk smaller (fast first paint); later
    chunks use the full target. Never splits mid-sentence."""
    # ~6-word sentences. first_target=12 -> first chunk ~2 sentences; target=36
    # -> later chunks ~6 sentences.
    sentences = [f"Sentence number {i} has six words." for i in range(20)]
    text = " ".join(sentences)

    chunks = chunk_for_cleanup(
        text, target_words=36, first_target_words=12, min_tail_words=6
    )

    assert len(chunks) >= 3
    # First chunk hits the small first target but not the full target.
    assert 12 <= _wc(chunks[0]) < 36
    # A middle chunk clears the full target.
    assert _wc(chunks[1]) >= 36
    # Every chunk ends on a sentence boundary.
    for ch in chunks:
        assert re.search(r"[.!?]$", ch)
    # Nothing dropped.
    assert " ".join(chunks).split() == text.split()


def test_first_target_defaults_to_target_when_unset():
    """Omitting first_target_words behaves exactly like the single-target form."""
    sentences = [f"This is sentence number {i} with several filler words here." for i in range(20)]
    text = " ".join(sentences)
    assert chunk_for_cleanup(text, target_words=80) == chunk_for_cleanup(
        text, target_words=80, first_target_words=80
    )


def test_newlines_are_sentence_boundaries():
    text = "First line here\nSecond line here\nThird line here"
    parts = _split_sentences(text)
    assert parts == ["First line here", "Second line here", "Third line here"]


def test_production_cleanup_chunks_pins_exact_live_boundaries():
    """The evaluator and live dictation must consume one production plan."""
    sentences = [
        f"Sentence {i} has exactly ten counted words for boundary testing."
        for i in range(31)
    ]
    text = " ".join(sentences)

    chunks = production_cleanup_chunks(text)

    assert [_wc(chunk) for chunk in chunks] == [60, 120, 130]
    assert chunks[0] == " ".join(sentences[:6])
    assert chunks[1] == " ".join(sentences[6:18])
    assert chunks[2] == " ".join(sentences[18:])
    assert " ".join(chunks).split() == text.split()


def test_production_cleanup_chunks_short_input_is_one_chunk():
    text = "A short saved transcript remains one production chunk."
    assert production_cleanup_chunks(text) == [text]


def test_production_cleanup_chunks_uses_configured_target_and_first_toggle():
    sentences = [
        f"Sentence {i} has exactly ten counted words for boundary testing."
        for i in range(18)
    ]
    text = " ".join(sentences)

    limited = production_cleanup_chunks(
        text, target_words=100, limit_first_chunk=True
    )
    uniform = production_cleanup_chunks(
        text, target_words=100, limit_first_chunk=False
    )

    assert [_wc(chunk) for chunk in limited] == [60, 120]
    assert [_wc(chunk) for chunk in uniform] == [100, 80]


def test_derived_tail_threshold_scales_with_small_target():
    sentences = [f"Sentence {i} has five words." for i in range(10)]
    text = " ".join(sentences)

    chunks = chunk_for_cleanup(text, target_words=20)

    assert [_wc(chunk) for chunk in chunks] == [20, 20, 10]
