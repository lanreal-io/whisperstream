"""Hermetic regressions for the PR #291 real-mic calibration follow-up."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from whisperstream.config_manager import ConfigManager
from whisperstream.digit_repair import repair_source_digits
from whisperstream.output_guard import (
    GuardContract,
    guard_digit_integrity,
    run_contract_guards,
    run_stage1_guards,
)


_CHAT = GuardContract("chat_stream", "stream_chat")
_CODE = GuardContract("code_stream", "stream_code")
_MODES = ("enforce", "log", "off")
_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "output_guard" / "realmic-2026-07-19.jsonl"
)
_LEGITIMATE_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "output_guard" / "legitimate.jsonl"
)


@dataclass(frozen=True, slots=True)
class CalibrationCase:
    case_id: str
    raw: str
    candidate: str
    expected: str
    expected_stage: str | None = None
    repair_fired: bool = False


_LEGITIMATE_ROWS = {
    row["id"]: row
    for row in (
        json.loads(line)
        for line in _LEGITIMATE_FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
}


def _legitimate_case(case_id: str, fixture_id: str) -> CalibrationCase:
    row = _LEGITIMATE_ROWS[fixture_id]
    assert row["contract"] == "chat_stream"
    assert row["expected_action"] == "keep"
    return CalibrationCase(case_id, row["raw"], row["cleaned"], row["cleaned"])


def _stage2_expectation(raw: str, candidate: str, mode: str, stage: str):
    """Unsupported numeric forms may still trip the independent contract policy."""
    return (
        raw if mode == "enforce" else candidate,
        stage if mode == "enforce" else stage.replace("stage2:", "stage2-observed:") if mode == "log" else None,
        False,
    )


def _run_local_pipeline(
    raw: str,
    candidate: str,
    mode: str,
    *,
    contract: GuardContract = _CHAT,
) -> tuple[str, str | None, bool]:
    """Mirror Stage 1 -> digit repair -> F1b -> rollout-controlled Stage 2."""
    stage1 = run_stage1_guards(
        raw,
        candidate,
        check_structure=contract.check_structure,
    )
    if stage1.action == "fallback":
        return stage1.final_text, f"stage1:{stage1.guard}", False

    repaired = repair_source_digits(stage1.final_text, raw)
    repair_fired = repaired != stage1.final_text
    f1b = guard_digit_integrity(raw, repaired)
    if f1b.action == "fallback":
        return f1b.final_text, f"f1b:{f1b.guard}", repair_fired

    if mode == "off":
        return f1b.final_text, None, repair_fired
    stage2 = run_contract_guards(raw, f1b.final_text, contract, emit_log=False)
    if stage2.action == "fallback" and mode == "enforce":
        return stage2.final_text, f"stage2:{stage2.guard}", repair_fired
    return (
        f1b.final_text,
        f"stage2-observed:{stage2.guard}" if stage2.action == "fallback" else None,
        repair_fired,
    )


_PHONE_RAW = "Call me back at five five five one two three four"
_LIST_RAW = (
    "Here's the plan: Finalise the deck. Second, send invites, third, book the room."
)
_LIST_FINALISE = """Here's the plan:
1. Finalise the deck
2. Send invites
3. Book the room"""


P_CASES = (
    CalibrationCase(
        "P01", _PHONE_RAW, "Call me back at 555-2134.", "Call me back at 555-1234.", repair_fired=True
    ),
    CalibrationCase(
        "P02", _PHONE_RAW, "Call me back at 5551-4234.", _PHONE_RAW, "f1b:digit_mismatch"
    ),
    CalibrationCase("P03", _PHONE_RAW, "Call me back at 555-1234.", "Call me back at 555-1234."),
    CalibrationCase(
        "P04",
        _PHONE_RAW,
        "Call me back at five five five one two three four.",
        "Call me back at five five five one two three four.",
    ),
    CalibrationCase(
        'P05',
        "Let's meet at nine.",
        "Let's meet at 9:00.",
        "Let's meet at 9:00.",
        None,
        False,
    ),
    CalibrationCase("P06", "Let's meet at nine.", "Let's meet at 9.", "Let's meet at 9."),
    CalibrationCase(
        'P07',
        "What's two hundred fifty six divided by eight?",
        "What's 246 divided by 8?",
        "What's two hundred fifty six divided by eight?",
        'stage2:required_content_missing',
        False,
    ),
    CalibrationCase("P08", _LIST_RAW, _LIST_FINALISE, _LIST_FINALISE),
    CalibrationCase(
        "P09",
        "Lay out the camping checklist as bullet points only, no full sentences please.",
        "1. Tent\n2. Water\n3. Matches",
        "Lay out the camping checklist as bullet points only, no full sentences please.",
        "stage1:introduced_structure",
    ),
    CalibrationCase(
        "P10-opening-closing",
        "Keep this dictated sentence.",
        "<TRANSCRIPT>Keep this dictated sentence.</TRANSCRIPT>",
        "Keep this dictated sentence.",
        "stage1:transcript_leak",
    ),
    CalibrationCase(
        "P10-closing",
        "Keep this dictated sentence.",
        "Keep this dictated sentence.</TRANSCRIPT>",
        "Keep this dictated sentence.",
        "stage1:transcript_leak",
    ),
    CalibrationCase(
        "P11",
        "Keep the literal <TRANSCRIPT> marker.",
        "Keep the literal <TRANSCRIPT> marker.",
        "Keep the literal <TRANSCRIPT> marker.",
    ),
    CalibrationCase(
        "P12",
        "Great turnout tonight Fire Emoji",
        "Great turnout tonight, 🔥.",
        "Great turnout tonight, 🔥.",
    ),
    CalibrationCase(
        "P13",
        "Nice catch, thumbs up emoji.",
        "Nice catch, 👍.",
        "Nice catch, 👍.",
    ),
    _legitimate_case("P14", "LEG-chat-correction-time"),
    CalibrationCase(
        "P15",
        "Set the alarm for five, no wait six AM, so I can leave early.",
        "Set the alarm for five AM, so I can leave early.",
        "Set the alarm for five, no wait six AM, so I can leave early.",
    ),
    _legitimate_case("P16-complete-filler-preserved", "LEG-chat-complete-filler-preserved"),
    _legitimate_case("P16-contractions", "LEG-chat-contraction-equivalence"),
    _legitimate_case("P16-source-markdown", "LEG-chat-ordinal-list"),
    _legitimate_case("P16-casing", "LEG-chat-acronym-spelling"),
    _legitimate_case("P16-stutter", "LEG-chat-stutter"),
)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("case", P_CASES, ids=lambda case: case.case_id)
def test_p_cases_remain_green(case: CalibrationCase, mode: str):
    final, stage, repair_fired = _run_local_pipeline(case.raw, case.candidate, mode)
    if case.expected_stage and case.expected_stage.startswith("stage2:"):
        assert (final, stage, repair_fired) == _stage2_expectation(case.raw, case.candidate, mode, case.expected_stage)
        return
    if case.case_id == "P15":
        expected = case.expected if mode == "enforce" else case.candidate
        expected_stage = {
            "enforce": "stage2:correction_target_missing",
            "log": "stage2-observed:correction_target_missing",
            "off": None,
        }[mode]
    else:
        expected = case.expected
        expected_stage = case.expected_stage
    assert (final, stage, repair_fired) == (
        expected,
        expected_stage,
        case.repair_fired,
    )


TIME_CONTEXT_CASES = (
    CalibrationCase("F01a", "Meet at nine AM.", "Meet at 9:00 AM.", "Meet at 9:00 AM."),
    CalibrationCase(
        "F01b",
        "Meet at nine in the morning.",
        "Meet at 9:00 AM.",
        "Meet at 9:00 AM.",
    ),
    CalibrationCase(
        "F01c",
        "Meet at five, no wait six AM.",
        "Meet at 6:00 AM.",
        "Meet at 6:00 AM.",
    ),
    CalibrationCase(
        'F01c-negative-wrong-target-hour',
        'Meet at five, no wait six AM.',
        'Meet at 5:00 AM.',
        'Meet at five, no wait six AM.',
        'stage2:correction_target_missing',
        False,
    ),
    CalibrationCase(
        'F01d-negative-nontime-correction',
        'Set quantity five, no wait six.',
        'Set quantity 6:00.',
        'Set quantity 6:00.',
        None,
        False,
    ),
    CalibrationCase(
        'F01e-negative-uncued',
        'Meet at nine.',
        'Meet at 9:00.',
        'Meet at 9:00.',
        None,
        False,
    ),
)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("case", TIME_CONTEXT_CASES, ids=lambda case: case.case_id)
def test_f01_time_context_is_source_conditioned(case: CalibrationCase, mode: str):
    if case.expected_stage and case.expected_stage.startswith("stage2:"):
        assert _run_local_pipeline(case.raw, case.candidate, mode) == _stage2_expectation(case.raw, case.candidate, mode, case.expected_stage)
        return
    assert _run_local_pipeline(case.raw, case.candidate, mode) == (
        case.expected,
        case.expected_stage,
        case.repair_fired,
    )


NARROW_DIGIT_CASES = (
    CalibrationCase(
        'F02a',
        'Call me back at five five five one two three four.',
        'Call me back at 5551 4234.',
        'Call me back at five five five one two three four.',
        'stage2:required_content_missing',
        False,
    ),
    CalibrationCase(
        'F02b',
        'The limit is two hundred fifty six thousand.',
        'The limit is 246 thousand.',
        'The limit is two hundred fifty six thousand.',
        'stage2:required_content_missing',
        False,
    ),
    CalibrationCase(
        'F02b-negative-unsafe-skeleton',
        'The limit is two hundred fifty six thousand.',
        'The limit is 24-60 thousand.',
        'The limit is two hundred fifty six thousand.',
        'stage2:required_content_missing',
        False,
    ),
    CalibrationCase(
        "F02c",
        "The limit is two hundred fifty six thousand.",
        "The limit is 256 thousand.",
        "The limit is 256 thousand.",
    ),
    CalibrationCase(
        "F02d",
        "The limit is two M.",
        "The limit is 2M.",
        "The limit is 2M.",
    ),
)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("case", NARROW_DIGIT_CASES, ids=lambda case: case.case_id)
def test_f02_unsupported_forms_follow_discretionary_mode(case: CalibrationCase, mode: str):
    if case.expected_stage and case.expected_stage.startswith("stage2:"):
        assert _run_local_pipeline(case.raw, case.candidate, mode) == _stage2_expectation(case.raw, case.candidate, mode, case.expected_stage)
        return
    assert _run_local_pipeline(case.raw, case.candidate, mode) == (
        case.expected,
        case.expected_stage,
        case.repair_fired,
    )


CORR2_CASES = (
    CalibrationCase(
        "F03-valid",
        "Send the package to my sister, I mean my cousin, she just moved to Denver.",
        "Send the package to my cousin, she just moved to Denver.",
        "Send the package to my cousin, she just moved to Denver.",
    ),
    CalibrationCase(
        "F03-negative-old-retained",
        "Send the package to my sister, I mean my cousin, she just moved to Denver.",
        "Send the package to my sister and my cousin, she just moved to Denver.",
        "Send the package to my sister, I mean my cousin, she just moved to Denver.",
    ),
    CalibrationCase(
        "F03-negative-target-dropped",
        "Send the package to my sister, I mean my cousin, she just moved to Denver.",
        "Send the package to my sister, she just moved to Denver.",
        "Send the package to my sister, I mean my cousin, she just moved to Denver.",
    ),
    CalibrationCase(
        "F03-negative-unrelated-substitution",
        "Send the package to my sister, I mean my cousin, she just moved to Denver.",
        "Send the package to my neighbor, she just moved to Denver.",
        "Send the package to my sister, I mean my cousin, she just moved to Denver.",
    ),
    CalibrationCase(
        "F03-negative-tail-dropped",
        "Send the package to my sister, I mean my cousin, she just moved to Denver.",
        "Send the package to my cousin.",
        "Send the package to my sister, I mean my cousin, she just moved to Denver.",
    ),
)


@pytest.mark.parametrize("case", CORR2_CASES, ids=lambda case: case.case_id)
def test_f03_corr2_enforce_contract(case: CalibrationCase):
    final, stage, _repair_fired = _run_local_pipeline(case.raw, case.candidate, "enforce")
    assert final == case.expected
    assert stage is None if case.case_id.endswith("valid") else stage.startswith("stage2:")


THUMBS_UP_CASES = (
    CalibrationCase(
        "F04-valid",
        "Nice catch on that regression, thumbs up.",
        "Nice catch on that regression, 👍.",
        "Nice catch on that regression, 👍.",
    ),
    CalibrationCase(
        "F04-negative-unreferenced-glyph",
        "Nice catch on that regression.",
        "Nice catch on that regression, 👍.",
        "Nice catch on that regression.",
    ),
)


@pytest.mark.parametrize("case", THUMBS_UP_CASES, ids=lambda case: case.case_id)
def test_f04_bare_thumbs_up_is_a_finite_equivalence(case: CalibrationCase):
    final, stage, _repair_fired = _run_local_pipeline(case.raw, case.candidate, "enforce")
    assert final == case.expected
    assert stage is None if case.case_id.endswith("valid") else stage.startswith("stage2:")


FINALIZE_CASES = (
    CalibrationCase(
        "F05-valid-list",
        _LIST_RAW,
        _LIST_FINALISE.replace("Finalise", "Finalize"),
        _LIST_FINALISE.replace("Finalise", "Finalize"),
    ),
    CalibrationCase(
        "F05-valid-prose",
        "Finalise the deck.",
        "Finalize the deck.",
        "Finalize the deck.",
    ),
    CalibrationCase(
        "F05-negative-unrelated-word",
        "Finalise the deck.",
        "Finalize the report.",
        "Finalise the deck.",
    ),
    CalibrationCase(
        "F05-negative-list-item-dropped",
        _LIST_RAW,
        "Here's the plan: Finalize the deck, send invites.",
        _LIST_RAW,
    ),
)


@pytest.mark.parametrize("case", FINALIZE_CASES, ids=lambda case: case.case_id)
def test_f05_finalize_is_a_finite_spelling_equivalence(case: CalibrationCase):
    final, stage, _repair_fired = _run_local_pipeline(case.raw, case.candidate, "enforce")
    assert final == case.expected
    assert stage is None if "valid" in case.case_id else stage.startswith("stage2:")


NGL_CASES = (
    CalibrationCase(
        "F06-valid",
        "Lol, that's actually pretty funny, Ng L.",
        "Lol, that's actually pretty funny, ngl.",
        "Lol, that's actually pretty funny, ngl.",
    ),
    CalibrationCase(
        "F06-negative-dropped",
        "Lol, that's actually pretty funny, Ng L.",
        "Lol, that's actually pretty funny.",
        "Lol, that's actually pretty funny, Ng L.",
    ),
    CalibrationCase(
        "F06-negative-other-acronym",
        "Lol, that's actually pretty funny, Ng L.",
        "Lol, that's actually pretty funny, idk.",
        "Lol, that's actually pretty funny, Ng L.",
    ),
    CalibrationCase(
        "F06-negative-surrounding-word",
        "Lol, that's actually pretty funny, Ng L.",
        "Lol, that's honestly pretty funny, ngl.",
        "Lol, that's actually pretty funny, Ng L.",
    ),
    CalibrationCase(
        "F06-negative-spacing-near-miss",
        "Lol, that's actually pretty funny, Ng  L.",
        "Lol, that's actually pretty funny, ngl.",
        "Lol, that's actually pretty funny, Ng  L.",
    ),
    CalibrationCase(
        "F06-negative-punctuation-near-miss",
        "Lol, that's actually pretty funny, Ng, L.",
        "Lol, that's actually pretty funny, ngl.",
        "Lol, that's actually pretty funny, Ng, L.",
    ),
    CalibrationCase(
        "F06-negative-terminal-punctuation-near-miss",
        "Lol, that's actually pretty funny, Ng L!",
        "Lol, that's actually pretty funny, ngl!",
        "Lol, that's actually pretty funny, Ng L!",
    ),
    CalibrationCase(
        "F06-negative-arbitrary-two-token-join",
        "Keep Ab C. as dictated.",
        "Keep abc as dictated.",
        "Keep Ab C. as dictated.",
    ),
)


@pytest.mark.parametrize("case", NGL_CASES, ids=lambda case: case.case_id)
def test_f06_ngl_is_a_finite_parakeet_equivalence(case: CalibrationCase):
    final, stage, _repair_fired = _run_local_pipeline(case.raw, case.candidate, "enforce")
    assert final == case.expected
    assert stage is None if case.case_id.endswith("valid") else stage.startswith("stage2:")


WRAPPER_CASES = (
    CalibrationCase(
        "F07-input-opening-leak",
        "Keep this dictated sentence.",
        "<INPUT>Keep this dictated sentence.",
        "Keep this dictated sentence.",
        "stage1:transcript_leak",
    ),
    CalibrationCase(
        "F07-input-closing-leak",
        "Keep this dictated sentence.",
        "Keep this dictated sentence.</INPUT>",
        "Keep this dictated sentence.",
        "stage1:transcript_leak",
    ),
    CalibrationCase(
        "F07-output-opening-leak",
        "Keep this dictated sentence.",
        "<OUTPUT>Keep this dictated sentence.",
        "Keep this dictated sentence.",
        "stage1:transcript_leak",
    ),
    CalibrationCase(
        "F07-output-closing-leak",
        "Keep this dictated sentence.",
        "Keep this dictated sentence.</OUTPUT>",
        "Keep this dictated sentence.",
        "stage1:transcript_leak",
    ),
    CalibrationCase(
        "F07-negative-dictated-input",
        "Keep the literal <INPUT> marker.",
        "Keep the literal <INPUT> marker.",
        "Keep the literal <INPUT> marker.",
    ),
    CalibrationCase(
        "F07-negative-dictated-output",
        "Keep the literal <OUTPUT> marker.",
        "Keep the literal <OUTPUT> marker.",
        "Keep the literal <OUTPUT> marker.",
    ),
    CalibrationCase(
        "F07-negative-cased-bounded-whitespace",
        "Keep the literal <INPUT> marker.",
        "Keep the literal < iNpUt > marker.",
        "Keep the literal < iNpUt > marker.",
    ),
    CalibrationCase(
        "F07-duplicated-dictated-input",
        "Keep the literal <INPUT> marker.",
        "<INPUT>Keep the literal <INPUT> marker.",
        "Keep the literal <INPUT> marker.",
        "stage1:transcript_leak",
    ),
    CalibrationCase(
        "F07-changed-direction",
        "Keep the literal <OUTPUT> marker.",
        "Keep the literal </OUTPUT> marker.",
        "Keep the literal <OUTPUT> marker.",
        "stage1:transcript_leak",
    ),
)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("case", WRAPPER_CASES, ids=lambda case: case.case_id)
def test_f07_actual_local_wrapper_inventory_is_source_conditioned(
    case: CalibrationCase,
    mode: str,
):
    assert _run_local_pipeline(case.raw, case.candidate, mode) == (
        case.expected,
        case.expected_stage,
        case.repair_fired,
    )


@pytest.mark.parametrize("case_id,expected", (("F08-new-install", "off"),))
def test_f08_new_install_supersedes_historical_enforce_rollout(
    tmp_path,
    case_id: str,
    expected: str,
):
    manager = ConfigManager(
        config_path=tmp_path / f"{case_id}.toml",
        models_root=tmp_path / "models",
    )
    assert manager.load().enhancement_output_guard_mode == expected


def _load_real_mic_cells() -> list[dict[str, str]]:
    cells = [
        json.loads(line)
        for line in _FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert all(
        set(cell) == {"id", "mode", "raw", "candidate", "model_output"}
        for cell in cells
    )
    return cells


_REAL_MIC_CELLS = _load_real_mic_cells()
_CANDIDATES = {
    "stream-1-lite",
    "lite-w1wrap",
    "lite-w5email",
    "lite-w6oclean",
    "lite-w10doless",
}
_REPAIR_CELLS = {("lite-w5email", "NUM1"), ("lite-w5email", "NUM2")}
_F1B_CELLS = {
    ("stream-1-lite", "NUM1"),
}
_STAGE2_CELLS = {(candidate, "CORR1") for candidate in _CANDIDATES} | {("lite-w6oclean", "NUM1")}
_REPAIRED_FINALS = {
    ("lite-w5email", "NUM1"): "Call me back at 555-1234.",
    ("lite-w5email", "NUM2"): "The door code is 1234.",
}


def test_real_mic_fixture_contains_exactly_five_candidates_by_twenty_clips():
    assert len(_REAL_MIC_CELLS) == 100
    assert len({cell["id"] for cell in _REAL_MIC_CELLS}) == 20
    assert {cell["candidate"] for cell in _REAL_MIC_CELLS} == _CANDIDATES
    assert len({(cell["candidate"], cell["id"]) for cell in _REAL_MIC_CELLS}) == 100


@pytest.mark.parametrize(
    "cell",
    _REAL_MIC_CELLS,
    ids=lambda cell: f"{cell['candidate']}-{cell['id']}",
)
def test_real_mic_matrix_locks_target_pipeline_stage_and_final(cell: dict[str, str]):
    key = (cell["candidate"], cell["id"])
    contract = _CODE if cell["mode"] == "code" else _CHAT
    final, stage, repair_fired = _run_local_pipeline(
        cell["raw"],
        cell["model_output"],
        "enforce",
        contract=contract,
    )
    actual_stage = stage.split(":", 1)[0] if stage else None
    expected_stage = "f1b" if key in _F1B_CELLS else "stage2" if key in _STAGE2_CELLS else None
    expected_final = (
        cell["raw"]
        if expected_stage is not None
        else _REPAIRED_FINALS.get(key, cell["model_output"])
    )

    assert (repair_fired, actual_stage, final) == (
        key in _REPAIR_CELLS,
        expected_stage,
        expected_final,
    )


# ===========================================================================
# O4a — the intclean-v3 number-corruption eval set
#
# Every case below is a VERBATIM cell from the frozen intclean campaign
# (nothing there was modified to produce these tests). The campaign's terminal
# comparison names the defect: "the repair fixes truncations (5432 -> 5551234) and cannot touch
# colon-form corruptions (5:55:43 at 350M, 5:52:34 at 1.2B), which fall all the
# way back to the uncleaned raw."
#
# Each case carries an explicit CLASSIFICATION, because the campaign mixes two
# very different kinds of failure and a test that demands the guard repair a
# generation error would be a bad test:
#
#   guard-repairable  The source unambiguously determines the digits and the
#                     guard must reconstruct them.
#   detect-only       The source does not prove ONE result. The guard must not
#                     guess; F1b returns the whole raw unit (never-degrade).
#   model-defect      A generation error the digit layer can neither repair nor
#                     detect. Recorded so the blind spot is visible, not silent.
# ===========================================================================


@dataclass(frozen=True, slots=True)
class CampaignCase:
    """One frozen campaign cell plus how the guard is required to treat it."""

    case_id: str
    classification: str
    raw: str
    candidate: str
    expected_final: str
    expected_stage: str | None
    repair_fired: bool
    source: str


_CLASSIFICATIONS = frozenset({"guard-repairable", "detect-only", "model-defect"})

# raws.jsonl line 1: say "Call me back at five five five one two three four.",
# check "7-digit phone -> flat 5551234 (NO dashes, NO truncation)". The Parakeet
# raw has no trailing period.
_NUM1_RAW = "Call me back at five five five one two three four"
# raws.jsonl line 4: say "Let's meet at nine.", check "bare hour -> 9 (a plain
# number). Must NOT invent 9:00 or 9 a.m."
_TIME2_RAW = "Let's meet at nine."

CAMPAIGN_CASES = (
    # --- the shape hole: colon insertion, confirmed at both tiers ------------
    CampaignCase(
        'O4a-NUM1-colon-350M',
        'model-defect',
        _NUM1_RAW,
        'Call me back at 5:55:43.',
        _NUM1_RAW,
        'stage2:required_content_missing',
        False,
        'compare_v3_restore.jsonl:1 lite-w31restore (flags DIGIT-CORRUPT*)',
    ),
    CampaignCase(
        'O4a-NUM1-colon-1p2b',
        'model-defect',
        _NUM1_RAW,
        'Call me back at 5:52:34.',
        _NUM1_RAW,
        'stage2:required_content_missing',
        False,
        'compare_v3_o2.jsonl:1 std1p2b-w32o2 (flags DIGIT-CORRUPT*)',
    ),
    # --- regression locks: the truncation path PR #264 already handles -------
    CampaignCase(
        "O4a-NUM1-truncation",
        "guard-repairable",
        _NUM1_RAW,
        "Call me back at 5432.",
        "Call me back at 5551234.",
        None,
        True,
        "compare_v3_restore.jsonl:1 lite-w21intclean_v2 (LEN-SHRINK, DIGIT-CORRUPT)",
    ),
    CampaignCase(
        "O4a-NUM1-anchor",
        "guard-repairable",
        _NUM1_RAW,
        "Call me back at 555-1234.",
        "Call me back at 555-1234.",
        None,
        False,
        "compare_v3_restore.jsonl:1 lite-w1wrap (flags [])",
    ),
    # --- detect-only: the source proves corruption but not one repair --------
    CampaignCase(
        'O4a-NUM1-split-run',
        'model-defect',
        _NUM1_RAW,
        'Call me back at 5551 2344.',
        _NUM1_RAW,
        'stage2:required_content_missing',
        False,
        'intclean-finetune/compare_intclean.jsonl:1 lite-w11intclean (flag under-count)',
    ),
    CampaignCase(
        "O4a-NUM1-dashed-length",
        "detect-only",
        _NUM1_RAW,
        "Call me back at 5551-4234.",
        _NUM1_RAW,
        "f1b:digit_mismatch",
        False,
        "intclean-finetune/compare_intclean.jsonl:1 stream-1-lite (DIGIT-CORRUPT*)",
    ),
    # --- detect-only by owner decision: the uncued bare hour ----------------
    CampaignCase(
        'O4a-TIME2-bare-hour',
        'model-defect',
        _TIME2_RAW,
        "Let's meet at 9:00.",
        "Let's meet at 9:00.",
        None,
        False,
        'compare_v3_restore.jsonl:4 all columns + compare_v3_o2.jsonl:4 std1p2b-w32o2',
    ),
    CampaignCase(
        "O4a-TIME2-anchor",
        "guard-repairable",
        _TIME2_RAW,
        "Let's meet at 9.",
        "Let's meet at 9.",
        None,
        False,
        "compare_v3_o2.jsonl:4 stream-1-w2wrap (flags [])",
    ),
)


def test_campaign_cases_carry_a_known_classification():
    assert {case.classification for case in CAMPAIGN_CASES} <= _CLASSIFICATIONS
    assert len({case.case_id for case in CAMPAIGN_CASES}) == len(CAMPAIGN_CASES)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("case", CAMPAIGN_CASES, ids=lambda case: case.case_id)
def test_o4a_campaign_cases(case: CampaignCase, mode: str):
    """Preserved campaign inputs under the current narrow numeric policy."""
    if case.expected_stage and case.expected_stage.startswith("stage2:"):
        assert _run_local_pipeline(case.raw, case.candidate, mode) == _stage2_expectation(case.raw, case.candidate, mode, case.expected_stage)
        return
    assert _run_local_pipeline(case.raw, case.candidate, mode) == (
        case.expected_final,
        case.expected_stage,
        case.repair_fired,
    )
