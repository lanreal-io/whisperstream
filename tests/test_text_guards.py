"""Tests for text_guards module — TDD first, implementation second."""

import logging
import pytest

from whisperstream.text_guards import (
    is_length_explosion,
    has_sentence_repetition,
    has_refusal_not_in_input,
    validate_and_fallback,
)


# ---------------------------------------------------------------------------
# is_length_explosion
# ---------------------------------------------------------------------------


class TestLengthExplosion:
    def test_trips_when_out_exceeds_1_5x_plus_one(self):
        raw = "a" * 100  # len=100, threshold = max(150, 150) = 150
        out = "a" * 151  # 151 > 150 → trips
        assert is_length_explosion(out, raw) is True

    def test_boundary_exactly_1_5x_does_not_trip(self):
        raw = "a" * 100
        out = "a" * 150  # 150 > 150 is False
        assert is_length_explosion(out, raw) is False

    def test_boundary_plus_50_does_not_trip(self):
        # For short input where +50 path dominates: raw=10, threshold=max(15,60)=60
        raw = "a" * 10
        out = "a" * 60  # 60 > 60 is False
        assert is_length_explosion(out, raw) is False

    def test_plus_50_path_trips(self):
        raw = "a" * 10
        out = "a" * 61  # 61 > 60 → trips
        assert is_length_explosion(out, raw) is True

    def test_empty_out_does_not_trip(self):
        raw = "hello world"
        assert is_length_explosion("", raw) is False

    def test_empty_both_does_not_trip(self):
        assert is_length_explosion("", "") is False

    def test_normal_expansion_does_not_trip(self):
        raw = "hello"
        out = "Hello, world!"
        assert is_length_explosion(out, raw) is False


# ---------------------------------------------------------------------------
# has_sentence_repetition
# ---------------------------------------------------------------------------


class TestSentenceRepetition:
    def _make_long_sentence(self, base="This is a long repeated sentence"):
        return base  # 6 words — qualifies

    def test_three_identical_sentences_trips(self):
        s = "This is a long repeated sentence."
        text = f"{s} {s} {s}"
        assert has_sentence_repetition(text) is True

    def test_two_identical_sentences_does_not_trip(self):
        s = "This is a long repeated sentence."
        text = f"{s} {s}"
        assert has_sentence_repetition(text) is False

    def test_short_sentences_repeated_five_times_does_not_trip(self):
        # "Yes." is 1 word — under 4 words threshold
        text = "Yes. Yes. Yes. Yes. Yes."
        assert has_sentence_repetition(text) is False

    def test_three_word_sentence_does_not_trip(self):
        # "Go home now." is 3 words — under threshold
        s = "Go home now."
        text = f"{s} {s} {s}"
        assert has_sentence_repetition(text) is False

    def test_casing_variants_count_as_same_sentence(self):
        s1 = "This is a repeated sentence."
        s2 = "THIS IS A REPEATED SENTENCE."
        s3 = "this is a repeated sentence."
        text = f"{s1} {s2} {s3}"
        assert has_sentence_repetition(text) is True

    def test_whitespace_variants_count_as_same_sentence(self):
        s1 = "This  is  a  repeated  sentence."
        s2 = "This is a repeated sentence."
        s3 = "This is a repeated sentence."
        # After normalization all three collapse to same string
        text = f"{s1} {s2} {s3}"
        assert has_sentence_repetition(text) is True

    def test_unique_sentences_do_not_trip(self):
        text = (
            "The quick brown fox jumps over the lazy dog. "
            "A completely different sentence follows here. "
            "And yet another unique thought comes next."
        )
        assert has_sentence_repetition(text) is False

    def test_exactly_four_word_sentence_qualifies(self):
        s = "One two three four."
        text = f"{s} {s} {s}"
        assert has_sentence_repetition(text) is True

    def test_repetition_already_present_in_raw_does_not_trip(self):
        s = "This is a repeated sentence."
        text = f"{s} {s} {s}"
        assert has_sentence_repetition(text, text) is False

    def test_repetition_introduced_relative_to_raw_trips(self):
        s = "This is a repeated sentence."
        assert has_sentence_repetition(f"{s} {s} {s}", s) is True


# ---------------------------------------------------------------------------
# has_refusal_not_in_input
# ---------------------------------------------------------------------------


class TestRefusalNotInInput:
    @pytest.mark.parametrize("phrase", [
        "as an AI",
        "as a language model",
        "I don't have the ability to",
        "I'm not able to fulfill",
        "I cannot assist with",
    ])
    def test_each_phrase_in_out_not_in_raw_trips(self, phrase):
        out = f"Sorry, {phrase} do that."
        raw = "Please transcribe this."
        assert has_refusal_not_in_input(out, raw) is True

    @pytest.mark.parametrize("phrase", [
        "as an AI",
        "as a language model",
        "I don't have the ability to",
        "I'm not able to fulfill",
        "I cannot assist with",
    ])
    def test_phrase_present_in_raw_does_not_trip(self, phrase):
        out = f"Sorry, {phrase} do that."
        raw = f"She said {phrase} in her email."
        assert has_refusal_not_in_input(out, raw) is False

    def test_case_insensitive_match_in_out(self):
        out = "AS AN AI I cannot do this."
        raw = "Please transcribe this."
        assert has_refusal_not_in_input(out, raw) is True

    def test_clean_output_does_not_trip(self):
        out = "Hello, how can I help you today?"
        raw = "Hello how can I help"
        assert has_refusal_not_in_input(out, raw) is False

    def test_phrase_in_raw_case_insensitive(self):
        # raw has "AS AN AI", out also has it — should NOT trip
        out = "As an AI I cannot do this."
        raw = "AS AN AI she mentioned that."
        assert has_refusal_not_in_input(out, raw) is False

# ---------------------------------------------------------------------------
# validate_and_fallback orchestrator
# ---------------------------------------------------------------------------


class TestValidateAndFallback:
    RAW = "Hello world this is a test."
    ENHANCED = "Hello, world! This is a test."

    def test_run_guards_false_returns_enhanced_even_if_guard_would_trip(self):
        # Create a length-exploding enhanced string
        exploding = "a" * (len(self.RAW) * 3)
        result = validate_and_fallback(exploding, self.RAW, run_guards=False)
        assert result == exploding

    def test_run_guards_true_no_trip_returns_enhanced(self):
        result = validate_and_fallback(self.ENHANCED, self.RAW, run_guards=True)
        assert result == self.ENHANCED

    def test_run_guards_true_length_explosion_returns_raw(self):
        exploding = "a" * (len(self.RAW) * 3)
        result = validate_and_fallback(exploding, self.RAW, run_guards=True)
        assert result == self.RAW

    def test_run_guards_true_sentence_repetition_returns_raw(self):
        s = "This is a long repeated sentence."
        repeated = f"{s} {s} {s}"
        result = validate_and_fallback(repeated, self.RAW, run_guards=True)
        assert result == self.RAW

    def test_run_guards_true_refusal_not_in_input_returns_raw(self):
        enhanced = "As an AI I cannot transcribe that."
        result = validate_and_fallback(enhanced, self.RAW, run_guards=True)
        assert result == self.RAW

    def test_run_guards_true_refusal_in_input_does_not_return_raw(self):
        # Refusal phrase is present in raw — should not trip
        raw_with_phrase = "She said as an AI in her note."
        enhanced = "She mentioned as an AI in her note."
        result = validate_and_fallback(enhanced, raw_with_phrase, run_guards=True)
        assert result == enhanced

    def test_warning_logged_on_length_explosion(self, caplog):
        exploding = "a" * (len(self.RAW) * 3)
        with caplog.at_level(logging.WARNING, logger="whisperstream.text_guards"):
            validate_and_fallback(exploding, self.RAW, run_guards=True)
        assert any("Guards" in r.message and "tripped" in r.message for r in caplog.records)

    def test_warning_logged_on_sentence_repetition(self, caplog):
        s = "This is a long repeated sentence."
        repeated = f"{s} {s} {s}"
        with caplog.at_level(logging.WARNING, logger="whisperstream.text_guards"):
            validate_and_fallback(repeated, self.RAW, run_guards=True)
        assert any("Guards" in r.message and "tripped" in r.message for r in caplog.records)

    def test_warning_logged_on_refusal(self, caplog):
        enhanced = "As an AI I cannot do that."
        with caplog.at_level(logging.WARNING, logger="whisperstream.text_guards"):
            validate_and_fallback(enhanced, self.RAW, run_guards=True)
        assert any("Guards" in r.message and "tripped" in r.message for r in caplog.records)

    def test_guard_name_in_log_length(self, caplog):
        exploding = "a" * (len(self.RAW) * 3)
        with caplog.at_level(logging.WARNING, logger="whisperstream.text_guards"):
            validate_and_fallback(exploding, self.RAW, run_guards=True)
        messages = " ".join(r.message for r in caplog.records)
        assert "length_explosion" in messages

    def test_guard_name_in_log_repetition(self, caplog):
        # Use a long raw so length explosion does NOT trip, only repetition does.
        # raw length = 200; threshold = max(300, 250) = 300.
        # repeated = "One two three four. " * 3 = 60 chars < 300 → length OK.
        long_raw = "x" * 200
        s = "One two three four."
        repeated = f"{s} {s} {s}"
        with caplog.at_level(logging.WARNING, logger="whisperstream.text_guards"):
            validate_and_fallback(repeated, long_raw, run_guards=True)
        messages = " ".join(r.message for r in caplog.records)
        assert "sentence_repetition" in messages

    def test_guard_name_in_log_refusal(self, caplog):
        enhanced = "As an AI I cannot do that."
        with caplog.at_level(logging.WARNING, logger="whisperstream.text_guards"):
            validate_and_fallback(enhanced, self.RAW, run_guards=True)
        messages = " ".join(r.message for r in caplog.records)
        assert "refusal" in messages

    def test_exception_in_guard_returns_raw_not_raise(self, monkeypatch):
        """Guards must never raise into the caller — return raw on any unexpected error."""
        import whisperstream.text_guards as tg

        def boom(out, raw):
            raise RuntimeError("unexpected!")

        monkeypatch.setattr(tg, "is_length_explosion", boom)
        result = validate_and_fallback(self.ENHANCED, self.RAW, run_guards=True)
        assert result == self.RAW
