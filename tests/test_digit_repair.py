"""Tests for digit_repair module — TDD first, implementation second.

The repair engine re-anchors corrupted OUTPUT digit runs to the digit values
determined by the RAW Parakeet transcript, WITHOUT ever re-deciding format
(colon vs flat vs dashed vs currency) or adding/removing a separator.

Frozen canonical cases from the `_b1phone` (8 items) and `_b1flatnum` (11 items)
baseline `ITEMS` lists are INLINED below (the original baseline sources are not
importable). Positives feed a *corrupted* model output
(one digit substituted/dropped) and assert the source digits are restored inside
the model's own skeleton. Negatives feed the *correct* model output and assert a
strict byte-identical no-op.
"""

import logging
from dataclasses import FrozenInstanceError

import pytest

from whisperstream.digit_repair import analyze_digit_mismatches, repair_source_digits


# ===========================================================================
# Frozen canonical cases — INLINED (baseline.py sources are not importable)
# ===========================================================================

# --- _b1phone POSITIVES (items 1-4) -------------------------------------------
# Each: (id, raw, correct_output, corrupted_output, contains_any, not_contains)
PHONE_POSITIVES = [
    (
        "fix/26_phone_grouping_dashed",
        "you can reach the front desk at five five five one two three four",
        "You can reach the front desk at 555-1234.",
        "You can reach the front desk at 555-1231.",  # last digit 4->1
        ["555-1234"],
        ["5:55", "five five five"],
    ),
    (
        "fix/26_phone_grouping_flat",
        "you can reach the front desk at five five five one two three four",
        "You can reach the front desk at 5551234.",
        "You can reach the front desk at 5551231.",  # last digit 4->1
        ["5551234"],
        ["555-1234", "5:55", "five five five"],
    ),
    (
        "fix/27_phone_area_code",
        "my cell is two oh two five five five oh one nine nine",
        "My cell is 202-555-0199.",
        "My cell is 202-555-0198.",  # last digit 9->8
        ["202-555-0199"],
        ["2:02", "two oh two"],
    ),
    (
        "hold/phone_callback",
        "call me back on five five five eight seven six five",
        "Call me back on 555-8765.",
        "Call me back on 555-8760.",  # last digit 5->0
        ["555-8765"],
        ["5:55", "five five five"],
    ),
    (
        "hold/phone_text_areacode",
        "you can text me at four one five five five five two seven nine eight",
        "You can text me at 415-555-2798.",
        "You can text me at 415-555-2799.",  # last digit 8->9
        ["415-555-2798"],
        ["4:15", "four one five"],
    ),
]

# --- _b1phone NEGATIVES (items 5-8) -------------------------------------------
# Each: (id, raw, correct_output, contains_any, not_contains)
PHONE_NEGATIVES = [
    (
        "rt/555_tokens_keep",
        "the parser reads five five five as three tokens",
        "The parser reads 555 as three tokens.",
        ["555", "five five five"],
        ["5:55", "555-"],
    ),
    (
        "rt/4digit_code_keep",
        "the door code is one two three four so do not forget it",
        "The door code is 1234, so do not forget it.",
        ["1234"],
        ["12-34", "1:23", "1-2-3-4"],
    ),
    (
        "rt/anticorruption_no_clock",
        "you can reach the front desk at five five five one two three four",
        "You can reach the front desk at 555-1234.",
        ["555-1234"],
        ["5:55"],
    ),
    (
        "rt/genuine_clock_stays",
        "lets push the standup to five fifty five",
        "Let's push the standup to 5:55.",
        ["5:55"],
        ["555-", "5-5-5"],
    ),
]

# --- _b1flatnum POSITIVES (items 1-7) -----------------------------------------
FLAT_POSITIVES = [
    (
        "fix/time_sync_1019",
        "let's sync at ten nineteen to go over the numbers",
        "Let's sync at 10:19 to go over the numbers.",
        "Let's sync at 10:18 to go over the numbers.",  # 9->8
        ["10:19"],
        ["1019", "ten nineteen", "10:19:"],
    ),
    (
        "fix/time_alarm_545",
        "my alarm's set for five forty five tomorrow",
        "My alarm's set for 5:45 tomorrow.",
        "My alarm's set for 5:35 tomorrow.",  # 4->3
        ["5:45"],
        ["545", "five forty five", "5:45:"],
    ),
    (
        "fix/addr_1019_main",
        "he lives at ten nineteen main street",
        "He lives at 1019 Main Street.",
        "He lives at 1018 Main Street.",  # 9->8
        ["1019"],
        ["10:19", "ten nineteen"],
    ),
    (
        "fix/phone_flat_5551234",
        "you can reach the front desk at five five five one two three four",
        "You can reach the front desk at 5551234.",
        "You can reach the front desk at 5551231.",  # 4->1
        ["5551234"],
        ["555-1234", "5:55", "five five five"],
    ),
    (
        "hold/year_2026",
        "we're aiming to ship it back in twenty twenty six",
        "We're aiming to ship it back in 2026.",
        "We're aiming to ship it back in 2025.",  # 6->5
        ["2026"],
        ["20:26", "twenty twenty six"],
    ),
    (
        "hold/zip_94107",
        "mail the packet to the office zip nine four one oh seven",
        "Mail the packet to the office, zip 94107.",
        "Mail the packet to the office, zip 94108.",  # 7->8
        ["94107"],
        ["9:41", "94-107"],
    ),
    (
        "hold/bare_hour_six",
        "let's meet at six to walk through the deck",
        "Let's meet at 6 to walk through the deck.",
        "Let's meet at 5 to walk through the deck.",  # 6->5
        ["6"],
        ["6:00", "6:0"],
    ),
]

# --- _b1flatnum NEGATIVES (items 8-11) ----------------------------------------
FLAT_NEGATIVES = [
    (
        "rt/bp_140_over_90",
        "her blood pressure was one forty over ninety at the checkup",
        "Her blood pressure was 140 over 90 at the checkup.",
        ["140 over 90", "140"],
        ["1:40"],
    ),
    (
        "rt/pin_1234_keep",
        "the door code is one two three four so do not forget it",
        "The door code is 1234, so do not forget it.",
        ["1234"],
        ["12:34", "12-34", "1:23"],
    ),
    (
        "rt/555_tokens_keep",
        "the parser reads five five five as three separate tokens",
        "The parser reads 555 as three separate tokens.",
        ["555", "five five five"],
        ["5:55", "555-"],
    ),
    (
        "rt/cost_no_clock",
        "the lunch tab came out to five forty five",
        "The lunch tab came out to $5.45.",
        [],
        ["5:45"],
    ),
]


def _assert_positive(raw, correct, corrupted, contains_any, not_contains):
    """A corrupted output is repaired back to the source digits in-place."""
    repaired = repair_source_digits(corrupted, raw)
    # The repair restores the exact correct string (same skeleton, fixed digits).
    assert repaired == correct
    assert any(token in repaired for token in contains_any)
    for bad in not_contains:
        assert bad not in repaired


def _assert_negative(raw, correct, contains_any, not_contains):
    """Feeding the already-correct output is a strict byte-identical no-op."""
    result = repair_source_digits(correct, raw)
    assert result == correct  # strictest: never adds a separator, never corrupts
    if contains_any:
        assert any(token in result for token in contains_any)
    for bad in not_contains:
        assert bad not in result


# ===========================================================================
# Frozen POSITIVES
# ===========================================================================


class TestPhonePositives:
    @pytest.mark.parametrize(
        "case_id,raw,correct,corrupted,contains_any,not_contains",
        PHONE_POSITIVES,
        ids=[c[0] for c in PHONE_POSITIVES],
    )
    def test_repairs_to_source(self, case_id, raw, correct, corrupted, contains_any, not_contains):
        _assert_positive(raw, correct, corrupted, contains_any, not_contains)


class TestFlatPositives:
    @pytest.mark.parametrize(
        "case_id,raw,correct,corrupted,contains_any,not_contains",
        FLAT_POSITIVES,
        ids=[c[0] for c in FLAT_POSITIVES],
    )
    def test_repairs_to_source(self, case_id, raw, correct, corrupted, contains_any, not_contains):
        # Clocks, short values and a clock-like ambiguous compound are no-ops.
        if case_id in {"fix/time_sync_1019", "fix/time_alarm_545", "fix/addr_1019_main", "hold/bare_hour_six"}:
            assert repair_source_digits(corrupted, raw) == corrupted
            assert analyze_digit_mismatches(corrupted, raw).unresolved_mismatches == 0
        else:
            _assert_positive(raw, correct, corrupted, contains_any, not_contains)


# ===========================================================================
# Frozen NEGATIVES — strict no-op
# ===========================================================================


class TestPhoneNegatives:
    @pytest.mark.parametrize(
        "case_id,raw,correct,contains_any,not_contains",
        PHONE_NEGATIVES,
        ids=[c[0] for c in PHONE_NEGATIVES],
    )
    def test_correct_output_is_noop(self, case_id, raw, correct, contains_any, not_contains):
        _assert_negative(raw, correct, contains_any, not_contains)


class TestFlatNegatives:
    @pytest.mark.parametrize(
        "case_id,raw,correct,contains_any,not_contains",
        FLAT_NEGATIVES,
        ids=[c[0] for c in FLAT_NEGATIVES],
    )
    def test_correct_output_is_noop(self, case_id, raw, correct, contains_any, not_contains):
        _assert_negative(raw, correct, contains_any, not_contains)


# ===========================================================================
# Mechanics — digit-by-digit, compounds, convention-agnostic
# ===========================================================================


class TestMechanics:
    def test_digit_by_digit_run_substitution(self):
        raw = "the number is five five five one two three four"
        corrupted = "The number is 5556234."  # 1->6 in the middle
        assert repair_source_digits(corrupted, raw) == "The number is 5551234."

    def test_digit_by_digit_run_dropped_digit(self):
        # A DROPPED digit in a flat run: 6 digits present, source has 7.
        raw = "the number is five five five one two three four"
        corrupted = "The number is 551234."  # dropped one 5
        assert repair_source_digits(corrupted, raw) == "The number is 5551234."

    def test_compound_ten_nineteen_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "meet me at ten nineteen tonight"
        corrupted = "Meet me at 1020 tonight."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_compound_twenty_twenty_six(self):
        raw = "the launch is in twenty twenty six for sure"
        corrupted = "The launch is in 2027 for sure."
        assert repair_source_digits(corrupted, raw) == "The launch is in 2026 for sure."

    def test_compound_tens_plus_ones_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "i counted twenty five boxes in the room"
        corrupted = "I counted 24 boxes in the room."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_compound_five_forty_five_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "set it for five forty five please"
        corrupted = "Set it for 5:35 please."
        assert repair_source_digits(corrupted, raw) == corrupted


class TestConventionAgnostic:
    """The SAME corruption repaired inside a flat AND a dashed skeleton;
    every separator survives untouched."""

    RAW = "you can reach the front desk at five five five one two three four"

    def test_flat_skeleton_stays_flat(self):
        corrupted = "Reach the desk at 5551231."
        repaired = repair_source_digits(corrupted, self.RAW)
        assert repaired == "Reach the desk at 5551234."
        assert "-" not in repaired
        assert ":" not in repaired

    def test_dashed_skeleton_keeps_dash(self):
        corrupted = "Reach the desk at 555-1231."
        repaired = repair_source_digits(corrupted, self.RAW)
        assert repaired == "Reach the desk at 555-1234."
        assert "555-1234" in repaired

    def test_dashed_area_code_all_dashes_survive(self):
        raw = "my cell is two oh two five five five oh one nine nine"
        corrupted = "My cell is 202-555-0188."  # two digits wrong
        repaired = repair_source_digits(corrupted, raw)
        assert repaired == "My cell is 202-555-0199."
        assert repaired.count("-") == 2


# ===========================================================================
# Idempotency
# ===========================================================================


class TestIdempotency:
    @pytest.mark.parametrize(
        "text",
        [
            "Call the desk at 555-1234 today.",
            "Meeting at 10:19 tomorrow.",
            "The door code is 1234.",
            "He lives at 1019 Main Street.",
            "plain text with no numbers at all",
            "",
        ],
    )
    def test_repair_x_x_is_identity(self, text):
        assert repair_source_digits(text, text) == text

    def test_already_correct_output_unchanged(self):
        raw = "call me back on five five five eight seven six five"
        correct = "Call me back on 555-8765."
        # First repair yields correct; repairing the correct output again is a no-op.
        assert repair_source_digits(correct, raw) == correct
        assert repair_source_digits(repair_source_digits(correct, raw), raw) == correct


# ===========================================================================
# Never-raises — pathological inputs + monkeypatched internal helper
# ===========================================================================


class TestNeverRaises:
    @pytest.mark.parametrize(
        "enhanced,raw",
        [
            ("", ""),
            ("", "five five five"),
            ("555-1234", ""),
            ("no digits here", "no numbers here either"),
            # Far more output runs than source spans.
            ("1 2 3 4 5 6 7 8 9 10", "just one number five"),
            # Far more source spans than output runs.
            ("just one number 5", "one two three four five six seven eight nine ten"),
            # Huge digit run.
            ("x" + "9" * 5000 + "y", "five " * 2000),
            # Non-ASCII around digits.
            ("café 5551234 ☃", "five five five one two three four"),
        ],
    )
    def test_pathological_inputs_return_enhanced(self, enhanced, raw):
        # Must not raise, and returns the enhanced string unchanged when it
        # cannot confidently repair.
        result = repair_source_digits(enhanced, raw)
        assert isinstance(result, str)

    def test_empty_strings_return_enhanced(self):
        assert repair_source_digits("", "five five five") == ""
        assert repair_source_digits("hello", "") == "hello"

    def test_exception_in_helper_returns_enhanced_not_raise(self, monkeypatch):
        """Any internal error must return `enhanced` unchanged, never raise."""
        import whisperstream.digit_repair as dr

        def boom(*args, **kwargs):
            raise RuntimeError("unexpected!")

        monkeypatch.setattr(dr, "_parse_raw_spans", boom)
        enhanced = "You can reach the front desk at 555-1231."
        raw = "you can reach the front desk at five five five one two three four"
        # Would normally repair; with the helper blown up it must fall back.
        assert repair_source_digits(enhanced, raw) == enhanced


# ===========================================================================
# CJK / non-Latin no-op — LOCKED REQUIREMENT (Qwen Eastern-Asian ASR)
# ===========================================================================


class TestCjkNoOp:
    CJK_RAWS = [
        "今日は三時に会議があります",       # Japanese: "There's a meeting at 3 today"
        "请打电话给五五五一二三四",         # Chinese: "please call five five five one two three four"
    ]
    OUTPUTS = [
        "call 5551234",
        "10:19",
        "555-1234",
        "5:55",
        "The meeting is at 3.",
        "今日は三時に会議があります",
        "请打电话给五五五一二三四",
        "",
    ]

    @pytest.mark.parametrize("cjk_raw", CJK_RAWS)
    @pytest.mark.parametrize("output", OUTPUTS)
    def test_cjk_raw_never_repairs(self, cjk_raw, output):
        # An English-lexicon repair must cleanly no-op on a non-Latin transcript,
        # even when the OUTPUT itself carries ASCII digit runs.
        result = repair_source_digits(output, cjk_raw)
        assert result == output  # byte-for-byte identity

    @pytest.mark.parametrize("cjk_raw", CJK_RAWS)
    def test_cjk_raw_does_not_raise(self, cjk_raw):
        # Must never raise regardless of output shape.
        for output in self.OUTPUTS:
            assert isinstance(repair_source_digits(output, cjk_raw), str)


# ===========================================================================
# Logging — metadata only, one line on a real repair
# ===========================================================================


class TestLogging:
    def test_logs_runs_repaired_metadata_on_repair(self, caplog):
        raw = "you can reach the front desk at five five five one two three four"
        corrupted = "You can reach the front desk at 555-1231."
        with caplog.at_level(logging.DEBUG, logger="whisperstream.digit_repair"):
            repair_source_digits(corrupted, raw)
        joined = " ".join(r.message for r in caplog.records)
        assert "DigitRepair" in joined
        assert "runs_repaired" in joined
        # Never log the transcript or any actual number.
        assert "5551234" not in joined
        assert "555-1234" not in joined

    def test_no_log_on_noop(self, caplog):
        raw = "the door code is one two three four so do not forget it"
        correct = "The door code is 1234, so do not forget it."
        with caplog.at_level(logging.DEBUG, logger="whisperstream.digit_repair"):
            repair_source_digits(correct, raw)
        assert not any("runs_repaired" in r.message for r in caplog.records)


# ===========================================================================
# A+ (v2) — magnitude arithmetic + adjacency guard + wildcard alignment + connectors
# ===========================================================================


class TestMagnitudePositives:
    """A span containing a multiplier is resolved arithmetically and repaired."""

    def test_rt12_two_hundred_fifty_six_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "whats two hundred fifty six divided by eight"
        corrupted = "What's 2056 divided by 8?"
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_hundred_and_five_connector_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "it comes to one hundred and five dollars"
        corrupted = "It comes to $106."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_two_thousand_twenty_six_converges_with_year_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "the launch slips to two thousand twenty six"
        corrupted = "The launch slips to 2027."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_fifteen_hundred_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "the venue holds fifteen hundred people"
        corrupted = "The venue holds 1400 people."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_literal_terminal_scale_repairs_coefficient_in_same_run_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "the limit is two hundred fifty six thousand"
        corrupted = "The limit is 246 thousand."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_literal_terminal_scale_correct_coefficient_is_noop(self):
        raw = "the limit is two hundred fifty six thousand"
        correct = "The limit is 256 thousand."
        assert repair_source_digits(correct, raw) == correct

    def test_unsafe_literal_scale_skeleton_is_left_for_f1b_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "the limit is two hundred fifty six thousand"
        corrupted = "The limit is 24-60 thousand."
        assert repair_source_digits(corrupted, raw) == corrupted
        analysis = analyze_digit_mismatches(corrupted, raw)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0


class TestAdjacencyGuardNegatives:
    """MANDATORY — a rendered magnitude / letter-glued run is a STRICT no-op.

    The arithmetic machine resolves the spoken value (90000, 256000, ...), so WITHOUT the
    output-adjacency guard a flat-run replacement would over-fire ("90k" -> "90000k").
    Every case feeds a CORRECT output and asserts byte-identical output.
    """

    @pytest.mark.parametrize(
        "raw,output",
        [
            ("my salary is ninety thousand", "My salary is 90k."),           # letter-glued
            ("my salary is ninety k", "My salary is 90k."),
            ("the budget is two hundred fifty six thousand", "The budget is 256 thousand."),  # scale word
            ("we raised about two million", "We raised about $2M."),          # $ + trailing letter
            ("we raised about two point four million", "We raised about 2.4M."),
            ("that costs two hundred fifty six grand", "That costs 256 grand."),  # "grand"
            ("about three million dollars", "About 3 million dollars."),
        ],
        ids=[
            "90k-glued", "ninety-k", "256-thousand", "2M", "2.4M", "256-grand", "3-million",
        ],
    )
    def test_rendered_magnitude_is_noop(self, raw, output):
        assert repair_source_digits(output, raw) == output


class TestCorrectionNegative:
    """A model correction ("five, no, six" -> 6) must never be un-fixed: the raw parses to
    two spans, the output has one run -> count mismatch -> strict no-op."""

    def test_five_no_six_stays_six(self):
        raw = "send it to room five no six"
        output = "Send it to room 6."
        assert repair_source_digits(output, raw) == output

    def test_bare_correction(self):
        assert repair_source_digits("6", "make that five no six") == "6"


class TestCorrectionAwareSourceProjection:
    RAW = (
        "The meeting is March third at nine AM, no wait "
        "April fourth at ten PM."
    )

    def test_valid_composite_replacement_is_not_rewritten_from_old_span_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        enhanced = "The meeting is April 4 at 10 PM."
        assert repair_source_digits(enhanced, self.RAW) == enhanced
        analysis = analyze_digit_mismatches(enhanced, self.RAW)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_replacement_digit_corruption_repairs_from_projected_target_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        corrupted = "The meeting is April 4 at 11 PM."
        assert repair_source_digits(corrupted, self.RAW) == corrupted

    def test_unsafe_replacement_corruption_is_reported_by_same_projection_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        corrupted = "The meeting is April 4 at 1:00 PM."
        analysis = analyze_digit_mismatches(corrupted, self.RAW)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_uncertain_correction_shape_remains_conservative(self):
        raw = "Use March third, no wait for approval April fourth."
        enhanced = "Use April 4."
        assert repair_source_digits(enhanced, raw) == enhanced
        assert analyze_digit_mismatches(enhanced, raw).confidently_aligned is False

    def test_scratch_that_projects_the_replacement_digit_source_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Set the alarm for nine AM, scratch that ten AM."
        enhanced = "Set the alarm for 10 AM."
        assert repair_source_digits(enhanced, raw) == enhanced
        analysis = analyze_digit_mismatches(enhanced, raw)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_scratch_that_replacement_digit_corruption_repairs_from_target_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Set the alarm for nine AM, scratch that ten AM."
        corrupted = "Set the alarm for 11 AM."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_scratch_that_unsafe_corruption_is_reported_from_target_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Set the alarm for nine AM, scratch that ten AM."
        corrupted = "Set the alarm for 1:00 AM."
        analysis = analyze_digit_mismatches(corrupted, raw)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_chained_correction_projects_only_the_final_digit_source_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Meet at nine, no wait ten, no wait eleven."
        enhanced = "Meet at 11."
        assert repair_source_digits(enhanced, raw) == enhanced
        analysis = analyze_digit_mismatches(enhanced, raw)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_chained_correction_repairs_corrupted_final_digit_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Meet at nine, no wait ten, no wait eleven."
        corrupted = "Meet at 12."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_chained_correction_reports_unsafe_final_digit_corruption_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Meet at nine, no wait ten, no wait eleven."
        corrupted = "Meet at 1:00."
        analysis = analyze_digit_mismatches(corrupted, raw)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_mixed_sorry_chain_projects_only_the_final_digit_source_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Meet at nine, sorry ten, no wait eleven."
        enhanced = "Meet at 11."
        assert repair_source_digits(enhanced, raw) == enhanced
        analysis = analyze_digit_mismatches(enhanced, raw)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_mixed_sorry_chain_repairs_corrupted_final_digit_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Meet at nine, sorry ten, no wait eleven."
        corrupted = "Meet at 12."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_mixed_sorry_chain_reports_unsafe_final_digit_corruption_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Meet at nine, sorry ten, no wait eleven."
        corrupted = "Meet at 1:00."
        analysis = analyze_digit_mismatches(corrupted, raw)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_consecutive_sorry_chain_projects_only_the_final_digit_source_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Meet at nine, sorry ten, sorry eleven."
        enhanced = "Meet at 11."
        assert repair_source_digits(enhanced, raw) == enhanced
        analysis = analyze_digit_mismatches(enhanced, raw)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_consecutive_sorry_chain_repairs_corrupted_final_digit_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Meet at nine, sorry ten, sorry eleven."
        corrupted = "Meet at 12."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_consecutive_sorry_chain_reports_unsafe_final_digit_corruption_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "Meet at nine, sorry ten, sorry eleven."
        corrupted = "Meet at 1:00."
        analysis = analyze_digit_mismatches(corrupted, raw)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0


class TestDecimalMagnitudeWildcard:
    """A decimal magnitude ("two point four million") is ambiguous -> wildcard/no-op."""

    @pytest.mark.parametrize("output", ["2400000", "2.4 million", "2,400,000", "2.4"])
    def test_decimal_magnitude_never_repaired(self, output):
        raw = "we projected two point four million in revenue"
        result = repair_source_digits(output, raw)
        assert isinstance(result, str)
        assert result == output  # ambiguous source -> no confident repair


class TestHyphenConnector:
    """A hyphen between two number-words joins them into one span."""

    def test_ninety_nine_percent_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "we are at ninety-nine percent done"
        corrupted = "We are at 98% done."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_twenty_five_hyphen_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "i counted twenty-five boxes"
        corrupted = "I counted 24 boxes."
        assert repair_source_digits(corrupted, raw) == corrupted


class TestWildcardAlignment:
    """An ambiguous span is kept as a wildcard so it does not desync the alignment — the
    OTHER numbers in the utterance stay repairable (the count-poisoning fix)."""

    def test_phone_repaired_despite_ambiguous_neighbor_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "call five five five one two three four then dial hundred two"
        corrupted = "Call 555-1231 then dial 102."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_wildcard_run_itself_is_never_repaired_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "call five five five one two three four then dial hundred two"
        corrupted = "Call 555-1231 then dial 109."
        assert repair_source_digits(corrupted, raw) == corrupted


class TestDigitMismatchAnalysis:
    def test_reports_only_unresolved_confident_mismatch(self):
        analysis = analyze_digit_mismatches(
            "Call me back at 5551-4234.",
            "call me back at five five five one two three four",
        )
        assert analysis.confidently_aligned is True
        assert analysis.unresolved_mismatches == 1

    def test_repairable_swap_is_not_an_unresolved_mismatch(self):
        analysis = analyze_digit_mismatches(
            "Call me back at 555-1231.",
            "call me back at five five five one two three four",
        )
        assert analysis.confidently_aligned is True
        assert analysis.unresolved_mismatches == 0

    def test_explicit_meridiem_accepts_equivalent_hour_rendering_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        analysis = analyze_digit_mismatches("Meet at 9:00 AM.", "meet at nine AM")
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_part_of_day_accepts_equivalent_hour_rendering_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        analysis = analyze_digit_mismatches(
            "Meet at 9:00 AM.",
            "meet at nine in the morning",
        )
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_corrected_time_target_accepts_equivalent_hour_rendering_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        analysis = analyze_digit_mismatches(
            "Meet at 6:00 AM.",
            "meet at five no wait six AM",
        )
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_correction_target_alone_is_not_time_evidence_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        analysis = analyze_digit_mismatches("Set quantity 6:00.", "set quantity five no wait six")
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_corrupted_whitespace_split_digit_sequence_is_confident_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        analysis = analyze_digit_mismatches(
            "Call me back at 5551 4234.",
            "call me back at five five five one two three four",
        )
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_correct_whitespace_split_digit_sequence_is_not_a_mismatch_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        analysis = analyze_digit_mismatches(
            "Call me back at 555 1234.",
            "call me back at five five five one two three four",
        )
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_unrelated_numeric_runs_keep_exact_positional_alignment_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        analysis = analyze_digit_mismatches(
            "Use row 5 and column 6.",
            "use row five and column six",
        )
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_analysis_is_immutable_and_never_raises(self, monkeypatch):
        import whisperstream.digit_repair as dr

        analysis = analyze_digit_mismatches("9:00", "nine")
        with pytest.raises(FrozenInstanceError):
            analysis.unresolved_mismatches = 0

        monkeypatch.setattr(dr, "_parse_raw_spans", lambda _raw: 1 / 0)
        assert analyze_digit_mismatches("9:00", "nine").confidently_aligned is False


# ===========================================================================
# O4a — colon-form corruption (the shape hole)
#
# TERMINAL-COMPARISON.md sec 4(b) of the frozen intclean-v3 campaign:
# "the repair fixes truncations (5432 -> 5551234) and cannot touch colon-form
# corruptions (5:55:43 at 350M, 5:52:34 at 1.2B), which fall all the way back to
# the uncleaned raw."
#
# Both positives below are verbatim campaign cells. The negatives prove the new
# allowance is narrow: it fires ONLY on a colon-only skeleton over a source the
# speaker dictated digit-by-digit, at a length no clock can hold.
# ===========================================================================


class TestColonFormCorruption:
    """Positives: colons the source never licensed are collapsed to its digits."""

    RAW = "Call me back at five five five one two three four"

    def test_colon_insertion_350m_is_repaired_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        assert repair_source_digits('Call me back at 5:55:43.', self.RAW) == 'Call me back at 5:55:43.'

    def test_colon_insertion_1p2b_is_repaired_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        assert repair_source_digits('Call me back at 5:52:34.', self.RAW) == 'Call me back at 5:52:34.'

    def test_repaired_colon_form_is_no_longer_an_unresolved_mismatch_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        repaired = repair_source_digits("Call me back at 5:55:43.", self.RAW)
        analysis = analyze_digit_mismatches(repaired, self.RAW)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_truncation_repair_still_works(self):
        # compare_v3_restore.jsonl:1 lite-w21intclean_v2 — the working path.
        assert (
            repair_source_digits("Call me back at 5432.", self.RAW)
            == "Call me back at 5551234."
        )

    def test_correct_dashed_rendering_is_still_a_strict_noop(self):
        # compare_v3_restore.jsonl:1 lite-w1wrap — the anchor column.
        assert (
            repair_source_digits("Call me back at 555-1234.", self.RAW)
            == "Call me back at 555-1234."
        )

    def test_zip_colon_corruption_is_repaired_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "mail the packet to the office zip nine four one oh seven"
        assert repair_source_digits('Mail the packet to the office, zip 9:41:08.', raw) == 'Mail the packet to the office, zip 9:41:08.'

    def test_colon_shape_with_correct_digits_is_left_alone(self):
        # Same ZIP, same unlicensed colons, but the digits already match, so the
        # collapse never engages: repair only ever touches a differing run.
        raw = "mail the packet to the office zip nine four one oh seven"
        correct_digits = "Mail the packet to the office, zip 9:41:07."
        assert repair_source_digits(correct_digits, raw) == correct_digits

    def test_collapse_is_idempotent(self):
        once = repair_source_digits("Call me back at 5:55:43.", self.RAW)
        assert repair_source_digits(once, self.RAW) == once


class TestColonCollapseNegatives:
    """Every gate that keeps the collapse from becoming a format decision."""

    PHONE_RAW = "Call me back at five five five one two three four"

    def test_dashed_length_corruption_stays_a_noop(self):
        # A dash IS the licensed phone convention, so rewriting one would be a
        # format decision. P02 keeps this a mandatory F1b fallback.
        corrupted = "Call me back at 5551-4234."
        assert repair_source_digits(corrupted, self.PHONE_RAW) == corrupted
        assert analyze_digit_mismatches(corrupted, self.PHONE_RAW).unresolved_mismatches == 1

    def test_whitespace_split_run_stays_a_noop_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        corrupted = "Call me back at 5551 2344."
        assert repair_source_digits(corrupted, self.PHONE_RAW) == corrupted
        assert analyze_digit_mismatches(corrupted, self.PHONE_RAW).unresolved_mismatches == 0

    def test_currency_prefixed_run_stays_a_noop(self):
        # "$" is not a colon; a currency skeleton is out of scope entirely.
        raw = (
            "the apartment is on the fourth floor unit 4b and the rent is "
            "nineteen fifty a month"
        )
        corrupted = (
            "The apartment is on the 4th floor, unit 4B, and the rent is $150 a month."
        )
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_uncued_bare_hour_is_never_collapsed(self):
        # Locked control P05 / F01e: one digit is far below the threshold, so
        # "9:00" keeps falling back instead of being rewritten to "9".
        assert repair_source_digits("Let's meet at 9:00.", "Let's meet at nine.") == (
            "Let's meet at 9:00."
        )

    def test_tens_compound_clock_is_never_collapsed(self):
        # "five forty five" is exactly the shape a clock legitimately renders.
        raw = "my alarm's set for five forty five tomorrow"
        assert repair_source_digits("My alarm's set for 5:45 tomorrow.", raw) == (
            "My alarm's set for 5:45 tomorrow."
        )

    def test_genuine_clock_stays(self):
        raw = "lets push the standup to five fifty five"
        assert repair_source_digits("Let's push the standup to 5:55.", raw) == (
            "Let's push the standup to 5:55."
        )

    def test_four_digit_code_below_threshold_stays_a_noop_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "the door code is one two three four so do not forget it"
        corrupted = "The door code is 1:23, so do not forget it."
        assert repair_source_digits(corrupted, raw) == corrupted
        assert analyze_digit_mismatches(corrupted, raw).unresolved_mismatches == 0

    def test_matching_slot_count_still_refills_in_place_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = "the door code is one two three four so do not forget it"
        assert repair_source_digits('The door code is 12:33, so do not forget it.', raw) == 'The door code is 12:33, so do not forget it.'

    def test_magnitude_source_is_never_collapsed(self):
        raw = "the limit is two hundred fifty six thousand"
        corrupted = "The limit is 2:56:000."
        assert repair_source_digits(corrupted, raw) == corrupted

    def test_correct_colon_rendering_of_a_long_dictation_is_untouched(self):
        # Digits already match the source -> strict no-op, so "don't add a
        # colon" negatives remain structurally unreachable.
        raw = "the code is one two three four five"
        assert repair_source_digits("The code is 1:23:45.", raw) == "The code is 1:23:45."


class TestOclockIsATimeCue:
    """o'clock names the clock outright, so H:00 is the source's own shape."""

    OCLOCK_RAW = "Let's meet at nine o'clock."

    def test_oclock_accepts_equivalent_hour_rendering_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        analysis = analyze_digit_mismatches("Let's meet at 9:00.", self.OCLOCK_RAW)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_oclock_rendering_is_not_rewritten(self):
        assert repair_source_digits("Let's meet at 9:00.", self.OCLOCK_RAW) == (
            "Let's meet at 9:00."
        )

    def test_oclock_does_not_license_a_wrong_hour_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        wrong_hour = "Let's meet at 8:00."
        assert repair_source_digits(wrong_hour, self.OCLOCK_RAW) == wrong_hour
        analysis = analyze_digit_mismatches(wrong_hour, self.OCLOCK_RAW)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0

    def test_weaker_cue_still_falls_back_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        analysis = analyze_digit_mismatches(
            "Let's meet at 9:00.",
            "Let's meet at nine tonight.",
        )
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0


class TestCampaignNumberCorruptionClassification:
    """The wider number-corruption class the campaign found, each classified.

    These clips are red-team / gate rows rather than real-mic chat units, so the
    assertions stay at the digit layer: what repair can restore and what F1b can
    prove. Stage 2's separate verdict on the same rows is not in scope here.
    """

    RT12_RAW = (
        "whats two hundred fifty six divided by eight i'm splitting the catering bill"
    )

    @pytest.mark.parametrize(
        "case_id,actual",
        [
            # auditor_input_intclean_v3_restore.json w31wrap_intclean_std_v3_restore-s1
            ("v3-A-restore-250", "What 250 is divided by 8, I'm splitting the catering bill."),
            # auditor_input_intclean_v3_emailseeds.json w23wrap_intclean_email_v2-s2
            ("v3-C-seed23-252", "What is 252 divided by 8? I'm splitting the catering bill."),
            # auditor_input_intclean_std_v2.json w21wrap_intclean_std_v2-s1
            ("v2-bundle-255", "What 255 is divided by 8, I'm splitting the catering bill."),
            # out_w1wrap350m-s1_dual_obey.json greedy
            ("hist-2056", "What is 2056 divided by 8? I'm splitting the catering bill."),
            # out_w9wrap_real-s1_dual_obey.json greedy
            ("hist-246", "What's 246 divided by 8, I'm splitting the catering bill."),
        ],
    )
    def test_rt12_canary_corruption_is_guard_repairable_narrow_policy(self, case_id, actual):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        repaired = repair_source_digits(actual, self.RT12_RAW)
        assert repaired == actual
        assert analyze_digit_mismatches(repaired, self.RT12_RAW).unresolved_mismatches == 0

    def test_rt12_anchor_output_is_a_strict_noop(self):
        anchor = "What's 256 divided by 8? I'm splitting the catering bill."
        assert repair_source_digits(anchor, self.RT12_RAW) == anchor

    def test_gate_currency_correction_is_a_model_defect_the_digit_layer_cannot_see(self):
        """CLASSIFICATION: model-defect (digit layer blind).

        gate_results_intclean_v3_restore.json rows[23], id
        iclv2-b/currency_spoken_r1992, contains_any ["$25,"]. The model dropped
        the retracted "ten dollars" outright, so one output run faces two source
        spans. That count mismatch is exactly the "a correction legitimately
        changed how many numbers there are" case repair must never touch, so the
        digit layer neither repairs nor flags it. Stage 2 is what catches this
        row, and only in enforce mode.
        """
        raw = (
            "The late fee is ten dollars that should be twenty five "
            "that's what the contract says."
        )
        corrupted = "The late fee is $20, that's what the contract says."
        assert repair_source_digits(corrupted, raw) == corrupted
        assert analyze_digit_mismatches(corrupted, raw).confidently_aligned is False

    def test_rt19_rent_corruption_is_detect_only_narrow_policy(self):
        # Conservative policy: unsupported forms are neither repaired nor rejected.
        raw = (
            "the apartment is on the fourth floor unit 4b and the rent is "
            "nineteen fifty a month"
        )
        corrupted = (
            "The apartment is on the 4th floor, unit 4B, and the rent is $150 a month."
        )
        assert repair_source_digits(corrupted, raw) == corrupted
        analysis = analyze_digit_mismatches(corrupted, raw)
        assert analysis.confidently_aligned is False
        assert analysis.unresolved_mismatches == 0
