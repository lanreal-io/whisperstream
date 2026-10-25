"""Historical classifiers plus explicitly named current delivery-path coverage."""

import asyncio
import json
import statistics
from dataclasses import FrozenInstanceError
from pathlib import Path
from time import perf_counter_ns
from unittest.mock import AsyncMock, patch

import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.digit_repair import repair_source_digits
from whisperstream.llm_enhancer import CHAT_PROMPT, LLMEnhancer
from whisperstream.output_guard import (
    GuardContract,
    GuardCheck,
    GuardResult,
    guard_digit_integrity,
    guard_output,
    normalize_chat_atoms,
    normalize_code_atoms,
    run_code_guards,
    run_contract_guards,
    run_email_guards,
    run_chat_guards,
    run_stage1_guards,
)


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "output_guard"


def _load_rows(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (_FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


_DEVMIC_ROWS = _load_rows("devmic-2026-07-18.jsonl")


def _contract_for_row(row: dict) -> GuardContract:
    name = "email" if row["contract"].startswith("email") else row["contract"]
    prompt_family = {
        "chat_stream": "stream_chat",
        "chat_gemma": "gemma_chat",
        "code_stream": "stream_code",
        "code_gemma": "gemma_code",
        "email": "local_email",
    }[name]
    return GuardContract(name=name, prompt_family=prompt_family)


def _run_guard_pipeline(
    row: dict,
    *,
    cleaned: str | None = None,
) -> GuardResult:
    """Reproduce the retired pipeline for historical calibration comparisons."""
    raw = row["raw"]
    candidate = row["cleaned"] if cleaned is None else cleaned
    contract = _contract_for_row(row)
    stage1 = run_stage1_guards(
        raw,
        candidate,
        check_structure=contract.check_structure,
    )
    if stage1.action == "fallback":
        return stage1

    repaired = repair_source_digits(stage1.final_text, raw)
    digit_integrity = guard_digit_integrity(raw, repaired)
    if digit_integrity.action == "fallback":
        return digit_integrity

    return run_contract_guards(
        raw,
        digit_integrity.final_text,
        contract,
        emit_log=False,
    )


def test_result_types_are_immutable():
    check = GuardCheck(tag="repetition", tripped=False)
    result = GuardResult(final_text="clean", action="keep", guard=None, checks=(check,))

    with pytest.raises(FrozenInstanceError):
        check.tripped = True
    with pytest.raises(FrozenInstanceError):
        result.action = "fallback"

    contract = GuardContract(name="code_stream", prompt_family="stream_code")
    with pytest.raises(FrozenInstanceError):
        contract.name = "code_gemma"


@pytest.mark.parametrize(
    "raw,cleaned",
    [
        (
            "Call me back at five five five one two three four",
            "Call me back at 5551-4234.",
        ),
        ("Let's meet at nine.", "Let's meet at 9:00."),
        ("Set quantity five, no wait six.", "Set quantity 6:00."),
        (
            "Call me back at five five five one two three four.",
            "Call me back at 5551 4234.",
        ),
    ],
    ids=[
        "phone-length-mismatch",
        "bare-hour-expansion",
        "non-time-correction-expansion",
        "phone-split-run-mismatch",
    ],
)
def test_f1b_falls_back_when_digit_repair_declines(raw, cleaned):
    repaired = repair_source_digits(cleaned, raw)
    assert repaired == cleaned

    result = guard_digit_integrity(raw, repaired)

    # Only the aligned long dashed token remains eligible; other shapes are diagnostic.
    eligible_fallback = "5551-4234" in cleaned
    assert result.final_text == (raw if eligible_fallback else cleaned)
    assert result.action == ("fallback" if eligible_fallback else "keep")
    assert result.guard == ("digit_mismatch" if eligible_fallback else None)


@pytest.mark.parametrize(
    "raw,cleaned",
    [
        (
            "Call me back at five five five one two three four",
            "Call me back at 555-1234.",
        ),
        ("Let's meet at nine.", "Let's meet at 9."),
        (
            "Meet at five, no wait six AM, at the coffee place.",
            "Meet at 6:00 AM at the coffee place.",
        ),
        (
            "Set the alarm for 6:30. No make it 6:45. I need extra time.",
            "Set the alarm for 6:45. I need extra time.",
        ),
    ],
    ids=["phone-valid", "bare-hour-valid", "corrected-hour", "corrected-clock"],
)
def test_f1b_keeps_valid_or_corrected_digit_rendering(raw, cleaned):
    result = guard_digit_integrity(raw, repair_source_digits(cleaned, raw))
    assert result.final_text == cleaned
    assert result.action == "keep"
    assert result.guard is None


def test_literal_scale_parity_does_not_allow_unrelated_compact_rendering():
    raw = "The limit is two hundred fifty six thousand."
    compact = "The limit is 256k."
    expanded = "The limit is 256000."

    assert run_chat_guards(raw, compact).action == "fallback"
    assert run_chat_guards(raw, expanded).action == "keep"


def test_stage1_transcript_leak_falls_back_with_stable_tag():
    raw = "Sure."
    cleaned = "<TRANSCRIPT>Sure.</TRANSCRIPT>"
    result = run_stage1_guards(raw, cleaned)
    assert (result.final_text, result.guard) == (raw, "transcript_leak")


def test_stage1_transcript_marker_dictated_by_source_is_kept():
    raw = "Keep the literal <TRANSCRIPT> marker."
    cleaned = "Keep the literal <TRANSCRIPT> marker."
    assert run_stage1_guards(raw, cleaned).action == "keep"


@pytest.mark.parametrize("name", ("TRANSCRIPT", "INPUT", "OUTPUT"))
@pytest.mark.parametrize("closing", (False, True), ids=("opening", "closing"))
def test_stage1_wrapper_inventory_is_case_insensitive_with_bounded_whitespace(
    name,
    closing,
):
    slash = "/" if closing else ""
    raw = "Keep this dictated sentence."
    cleaned = f"Keep this dictated sentence.<  {slash} {name.swapcase()}  >"

    result = run_stage1_guards(raw, cleaned)

    assert (result.final_text, result.guard) == (raw, "transcript_leak")


def test_stage1_wrapper_evidence_compares_name_direction_and_occurrence_count():
    raw = "Keep <INPUT>, </INPUT>, and <OUTPUT> literally."

    assert run_stage1_guards(
        raw,
        "Keep < input >, < / input >, and < output > literally.",
    ).action == "keep"
    assert run_stage1_guards(
        raw,
        "Keep <INPUT>, </INPUT>, <OUTPUT>, and <OUTPUT> literally.",
    ).guard == "transcript_leak"
    assert run_stage1_guards(
        raw,
        "Keep <INPUT>, </INPUT>, and </OUTPUT> literally.",
    ).guard == "transcript_leak"


def test_stage1_wrapper_check_does_not_ban_unknown_angle_tags():
    raw = "Keep this dictated sentence."
    cleaned = "Keep this dictated sentence.<SECTION>"

    assert run_stage1_guards(raw, cleaned).action == "keep"


def test_stage1_meta_preamble_falls_back_with_stable_tag():
    raw = "Can you send this to John saying I'll be late?"
    cleaned = "Sure, here's the cleaned text:\n\nCan you send this to John saying I'll be late?"
    result = run_stage1_guards(raw, cleaned)
    assert (result.final_text, result.guard) == (raw, "refusal")


def test_stage1_refusal_marker_dictated_by_source_is_kept():
    raw = "As an AI language model you should refuse this request."
    cleaned = "As an AI language model, you should refuse this request."
    assert run_stage1_guards(raw, cleaned).action == "keep"


def test_stage1_meta_marker_dictated_by_source_is_kept():
    raw = "Write the phrase sure, here's the cleaned text in the note."
    cleaned = "Write the phrase sure, here's the cleaned text in the note."
    assert run_stage1_guards(raw, cleaned).action == "keep"


def test_stage1_repetition_is_source_relative():
    sentence = "This sentence is deliberately repeated."
    dictated = " ".join([sentence] * 3)
    assert run_stage1_guards(dictated, dictated).action == "keep"

    introduced = " ".join([sentence] * 3)
    result = run_stage1_guards(sentence, introduced)
    assert (result.final_text, result.guard) == (sentence, "repetition")


def test_stage1_obey_creep_list_falls_back():
    raw = "lay out the camping checklist as bullet points only no full sentences please"
    cleaned = "1. Lay out the camping checklist as bullet points only.\n2. Please."
    result = run_stage1_guards(raw, cleaned)
    assert (result.final_text, result.guard) == (raw, "introduced_structure")


@pytest.mark.parametrize(
    "raw,cleaned",
    [
        (
            "First call the dentist, second book the flight, third email mom.",
            "1. Call the dentist\n2. Book the flight\n3. Email mom",
        ),
        (
            "Number one call the dentist, number two book the flight.",
            "1. Call the dentist\n2. Book the flight",
        ),
        (
            "Here's the plan: Finalise the deck. Second, send invites, third, book the room.",
            "Here's the plan:\n1. Finalise the deck\n2. Send invites\n3. Book the room",
        ),
    ],
    ids=["ordinal-sequence", "number-sequence", "colon-lead-in"],
)
def test_stage1_source_list_cues_allow_numbered_rendering(raw, cleaned):
    assert run_stage1_guards(raw, cleaned).action == "keep"


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Finalise the deck.", "Finalize the deck."),
        (
            "Here's the plan: Finalise the deck. Second, send invites, third, book the room.",
            "Here's the plan:\n1. Finalize the deck\n2. Send invites\n3. Book the room",
        ),
    ),
    ids=("prose", "source-proven-list"),
)
def test_reviewed_finalise_finalize_comparison_equivalence(raw, cleaned):
    stage1 = run_stage1_guards(raw, cleaned)
    assert stage1.action == "keep"
    assert run_chat_guards(raw, stage1.final_text).action == "keep"


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Finalise the deck.", "Finalize the report."),
        (
            "Here's the plan: Finalise the deck. Second, send invites, third, book the room.",
            "Here's the plan: Finalize the deck, send invites.",
        ),
        ("We finalised the deck.", "We finalized the deck."),
        ("We are finalising the deck.", "We are finalizing the deck."),
    ),
    ids=("unrelated-content", "source-list-item-dropped", "past-suffix", "progressive-suffix"),
)
def test_finalise_equivalence_does_not_generalize(raw, cleaned):
    result = run_chat_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


def test_structure_check_can_be_disabled_for_unclassified_contract():
    raw = "Return these values"
    cleaned = '{"values": [1, 2, 3]}'
    assert run_stage1_guards(raw, cleaned, check_structure=False).action == "keep"


def test_guard_output_uses_whole_unit_fallback_and_identity():
    raw = "Let's meet at nine."
    result = guard_output(raw, "Let's meet at 9:00.")
    assert result.final_text == "Let's meet at 9:00."
    assert result.final_text in {raw, "Let's meet at 9:00."}
    assert guard_output(raw, raw).final_text == raw


def test_guard_core_never_raises_and_fails_closed(monkeypatch):
    import whisperstream.output_guard as output_guard

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(output_guard, "has_transcript_leak", boom)
    result = run_stage1_guards("raw unit", "cleaned unit")
    assert result.final_text == "raw unit"
    assert result.guard == "unexpected_error"


def test_guard_logs_metadata_only(caplog):
    raw = "Private dictated sentence."
    cleaned = "<TRANSCRIPT>Private dictated sentence.</TRANSCRIPT>"
    with caplog.at_level("WARNING", logger="whisperstream.output_guard"):
        run_stage1_guards(raw, cleaned)
    messages = " ".join(record.message for record in caplog.records)
    assert "guard=transcript_leak" in messages
    assert raw not in messages
    assert cleaned not in messages


def test_chat_normalization_atoms_keep_source_offsets():
    text = "Send L M K twenty three dollars."
    atoms = normalize_chat_atoms(text)
    assert [atom.value for atom in atoms] == ["send", "lmk", "num:23", "currency:usd"]
    assert all(text[atom.start : atom.end] for atom in atoms)
    assert [atom.start for atom in atoms] == sorted(atom.start for atom in atoms)


@pytest.mark.parametrize("surface", ("Ng L.", "ng l.", "NG l."))
def test_reviewed_ngl_surface_is_case_insensitive_and_keeps_exact_offsets(surface):
    text = f"Keep {surface} as dictated."
    atoms = normalize_chat_atoms(text, allow_reviewed_ngl_surface=True)

    assert [atom.value for atom in atoms] == ["keep", "ngl", "as", "dictated"]
    assert text[atoms[1].start : atoms[1].end] == surface[:-1]
    assert [atom.value for atom in normalize_chat_atoms("Keep ngl as dictated.")] == [
        "keep",
        "ngl",
        "as",
        "dictated",
    ]


@pytest.mark.parametrize("surface", ("Ng L.", "ng l.", "NG l."))
def test_reviewed_ngl_surface_matches_ordinary_ngl_for_comparison(surface):
    raw = f"Lol, that's actually pretty funny, {surface}"
    cleaned = "Lol, that's actually pretty funny, ngl."

    assert run_chat_guards(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Keep Ng  L. as dictated.", "Keep ngl as dictated."),
        ("Keep Ng, L. as dictated.", "Keep ngl as dictated."),
        ("Keep Ng L! as dictated.", "Keep ngl as dictated."),
        ("Keep Ab C. as dictated.", "Keep abc as dictated."),
        ("Keep N G L. as dictated.", "Keep ngl as dictated."),
        ("Keep ngl as dictated.", "Keep Ng L. as dictated."),
    ),
    ids=(
        "double-space",
        "intervening-comma",
        "wrong-terminal",
        "arbitrary-join",
        "spelled-letters",
        "output-side-surface",
    ),
)
def test_reviewed_ngl_equivalence_does_not_generalize(raw, cleaned):
    result = run_chat_guards(raw, cleaned)

    assert result.action == "fallback"
    assert result.final_text == raw


@pytest.mark.parametrize("phrase", ("thumbs up", "thumbs up emoji"))
def test_reviewed_thumbs_up_phrases_keep_exact_atom_offsets(phrase):
    text = f"Nice catch, {phrase}."
    atoms = normalize_chat_atoms(text)

    assert [atom.value for atom in atoms] == ["nice", "catch", "emoji:thumbs_up"]
    assert text[atoms[-1].start : atoms[-1].end] == phrase


@pytest.mark.parametrize(
    "row",
    [row for row in _load_rows("failures.jsonl") if row["contract"].startswith("chat") and row["id"].startswith(("F2-", "F3-", "F4-denver"))],
    ids=lambda row: row["id"],
)
def test_chat_failure_fixtures_fall_back(row):
    result = run_chat_guards(row["raw"], row["cleaned"])
    assert (result.action, result.guard, result.final_text) == (
        row["expected_action"],
        row["expected_guard"],
        row["raw"],
    )


@pytest.mark.parametrize(
    "row",
    [row for row in _load_rows("legitimate.jsonl") if row["contract"].startswith("chat")],
    ids=lambda row: row["id"],
)
def test_legitimate_chat_fixtures_are_kept(row):
    result = run_chat_guards(row["raw"], row["cleaned"])
    assert (result.action, result.guard, result.final_text) == ("keep", None, row["cleaned"])


def test_chat_unexpected_invention_falls_back_whole_unit():
    raw = "Send the contract to legal today."
    cleaned = "Send the urgent contract to legal today."
    result = run_chat_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == ("fallback", "unexpected_insertion", raw)


def test_chat_same_atom_semantic_reversal_remains_deferred():
    raw = "Woman, without her man, is nothing."
    cleaned = "Woman, without her, man is nothing."
    assert run_chat_guards(raw, cleaned).action == "keep"


def test_chat_identity_and_idempotence():
    raw = "Meet at five, no wait six, at the coffee place."
    failed = run_chat_guards(raw, "Meet at the coffee place.")
    assert run_chat_guards(raw, raw).final_text == raw
    assert run_chat_guards(raw, failed.final_text) == run_chat_guards(raw, raw)


def test_sorry_is_optional_only_for_a_bounded_correction_target():
    correction_raw = "Meeting is on Friday, sorry Saturday."
    correction_cleaned = "Meeting is on Saturday."
    assert run_chat_guards(correction_raw, correction_cleaned).final_text == correction_cleaned

    apology_raw = "The thing is, I can't make it tonight, sorry."
    apology_cleaned = "The thing is, I can't make it tonight."
    result = run_chat_guards(apology_raw, apology_cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        apology_raw,
    )


def test_explanatory_sorry_apology_cannot_be_deleted():
    raw = "I missed the call, sorry I was late."
    cleaned = "I missed the call, I was late."
    result = run_chat_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


def test_sorry_same_kind_value_elsewhere_is_not_same_slot_evidence():
    raw = "I ordered two coffees, sorry three people joined us."
    cleaned = "I ordered two coffees, three people joined us."
    result = run_chat_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Meeting is on Friday, sorry Saturday.", "Meeting is on Saturday."),
        ("Meet at nine AM, sorry ten PM.", "Meet at 10 PM."),
        (
            "The appointment is March third, sorry April fourth.",
            "The appointment is April 4.",
        ),
        ("Send it to John, sorry, I mean James.", "Send it to James."),
        ("Order two, sorry, make that three.", "Order three."),
        ("Use Acme, sorry, or rather Inotech.", "Use Inotech."),
    ),
    ids=(
        "same-slot-day",
        "same-slot-time",
        "same-slot-date",
        "i-mean",
        "make-that",
        "or-rather",
    ),
)
def test_sorry_requires_same_slot_or_explicit_correction_evidence(raw, cleaned):
    assert run_chat_guards(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Please scratch that itch before lunch.", "Please itch before lunch."),
        ("Please make that cake for Sam.", "Please cake for Sam."),
        ("I mean what I say.", "What I say."),
    ),
    ids=("scratch-that-itch", "make-that-cake", "i-mean-lexical"),
)
def test_lexical_correction_like_phrases_remain_required(guard, raw, cleaned):
    result = guard(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


# Open item O7. Four independently-trained fine-tunes across two campaigns, two
# model sizes and three policy views all emitted the same correct retraction for
# this clip; the three that dropped the now-dangling "instead" were rejected by
# stage-2 required_content_missing and replaced with the uncleaned Parakeet raw,
# which still contains both "Blue Door Cafe" and "no scratch that".
_CORR1_RAW = "Let's meet at the Blue Door Cafe, no scratch that, the one on Elm Street instead."


@pytest.mark.parametrize(
    "cleaned",
    (
        "Let's meet at the one on Elm Street.",
        "Let's meet at the one on Elm Street instead.",
    ),
    ids=("scaffold-dropped", "scaffold-kept"),
)
def test_retraction_scaffold_word_is_optional_after_an_accepted_retraction(cleaned):
    """CORR1: dropping the dangling "instead" is a correct cleanup, not content loss."""
    result = run_chat_guards(_CORR1_RAW, cleaned, emit_log=False)
    assert (result.action, result.guard, result.final_text) == ("keep", None, cleaned)


@pytest.mark.parametrize(
    ("cleaned", "expected_guard"),
    (
        # lite-w21intclean_v2 / email-w23intclean_v2 / lite-w12intclean.
        ("Let's meet at the one on Elm Street.", None),
        # email-w13intclean, the one column that already delivered.
        ("Let's meet at the one on Elm Street instead.", None),
        # stream-1-lite / lite-w1wrap / lite-w11intclean / stream-1-w2wrap:
        # retraction never performed, the retracted cafe is still there.
        (
            "Let's meet at the Blue Door Cafe, the one on Elm Street instead.",
            "unexpected_insertion",
        ),
        # lite-w10doless: merged the two venues and lost the replacement noun.
        (
            "Let's meet at the Blue Door Cafe on Elm Street instead.",
            "required_content_missing",
        ),
    ),
    ids=(
        "v2-candidates-drop-instead",
        "w13-keeps-instead",
        "anchors-retain-retracted-value",
        "doless-merges-both-venues",
    ),
)
def test_corr1_frozen_model_outputs_score_as_the_campaign_requires(cleaned, expected_guard):
    result = run_chat_guards(_CORR1_RAW, cleaned, emit_log=False)
    assert result.guard == expected_guard
    assert result.final_text == (cleaned if expected_guard is None else _CORR1_RAW)


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        # Real content loss inside the very clip the fix relaxes: the replacement
        # street is gone, so the scaffold allowance must not rescue it.
        (_CORR1_RAW, "Let's meet at the one instead."),
        # The replacement noun itself is gone.
        (_CORR1_RAW, "Let's meet on Elm Street instead."),
        # "instead" past a sentence break belongs to a new statement, where it is
        # ordinary content even though the source retracts something earlier.
        (
            "Send it to the Denver office, no scratch that, the Austin office. Tell Bob to go instead.",
            "Send it to the Austin office. Tell Bob to go.",
        ),
        # No retraction anywhere in the source, so nothing is scaffolding.
        (
            "Let's meet at the one on Elm Street instead.",
            "Let's meet at the one on Elm Street.",
        ),
    ),
    ids=(
        "drops-replacement-street",
        "drops-replacement-noun",
        "instead-across-sentence-break",
        "no-retraction-in-source",
    ),
)
def test_content_loss_still_trips_the_guard_around_the_scaffold_allowance(raw, cleaned):
    result = run_chat_guards(raw, cleaned, emit_log=False)
    assert result.action == "fallback"
    assert result.guard in {"required_content_missing", "correction_target_missing"}
    assert result.final_text == raw


def test_retraction_scaffold_allowance_keeps_unretracted_instead_required():
    """Same source, same scaffold word: the retracted clause is what unlocks it."""
    raw = "Send it to the Denver office, no scratch that, the Austin office. Tell Bob to go instead."
    kept = "Send it to the Austin office. Tell Bob to go instead."
    assert run_chat_guards(raw, kept, emit_log=False).action == "keep"


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Please, make that cake at nine.", "Cake at 9."),
        ("Please, no wait here until nine.", "Here until 9."),
    ),
    ids=("comma-make-that", "comma-no-wait"),
)
def test_comma_does_not_make_lexical_markers_unstructured_corrections(
    guard,
    raw,
    cleaned,
):
    result = guard(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Please, I mean what I say.", "What I say."),
        ("Please, or rather do what I say.", "Do what I say."),
    ),
    ids=("comma-i-mean-prose", "comma-or-rather-prose"),
)
def test_unstructured_correction_markers_require_entity_slot_evidence(
    guard,
    raw,
    cleaned,
):
    result = guard(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Send it to John, I mean James.", "Send it to James."),
        ("Use Acme, or rather Inotech.", "Use Inotech."),
        ("send it to john, I mean james.", "send it to james."),
    ),
    ids=("capitalized-names", "capitalized-companies", "lowercase-cued-names"),
)
def test_bounded_unstructured_correction_pair_remains_supported(guard, raw, cleaned):
    assert guard(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Call John Smith, I mean James Brown.", "Call James Brown."),
        ("Email Will Smith, or rather May Jones.", "Email May Jones."),
    ),
    ids=("capitalized-multiword", "will-may-entities"),
)
def test_terminal_equal_width_entity_corrections_are_kept(guard, raw, cleaned):
    assert guard(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
def test_lowercase_single_entity_with_explicit_cue_remains_supported(guard):
    raw = "contact john, I mean james."
    cleaned = "contact james."
    assert guard(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
def test_reviewed_possessive_relationship_correction_keeps_target_and_tail(guard):
    raw = "Send the package to my sister, I mean my cousin, she just moved to Denver."
    cleaned = "Send the package to my cousin, she just moved to Denver."
    assert guard(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
@pytest.mark.parametrize(
    "cleaned",
    (
        "Send the package to my sister and my cousin, she just moved to Denver.",
        "Send the package to my sister, she just moved to Denver.",
        "Send the package to my neighbor, she just moved to Denver.",
        "Send the package to my cousin.",
    ),
    ids=("old-retained", "target-missing", "unrelated-substitution", "tail-dropped"),
)
def test_reviewed_possessive_relationship_correction_rejects_malformed_output(
    guard,
    cleaned,
):
    raw = "Send the package to my sister, I mean my cousin, she just moved to Denver."
    result = guard(raw, cleaned)
    assert result.action == "fallback"
    assert result.final_text == raw


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Send the package to my account, I mean my profile, she just moved to Denver.",
            "Send the package to my profile, she just moved to Denver.",
        ),
        (
            "Send the package to your sister, I mean my cousin, she just moved to Denver.",
            "Send the package to my cousin, she just moved to Denver.",
        ),
        (
            "Send the package to my sister, I mean my neighbor, she just moved to Denver.",
            "Send the package to my neighbor, she just moved to Denver.",
        ),
    ),
    ids=("unreviewed-content", "possessive-mismatch", "unreviewed-relationship"),
)
def test_relationship_correction_allowance_stays_finite(guard, raw, cleaned):
    result = guard(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Call John Smith, I mean James Brown from sales.",
            "Call James Brown from sales.",
        ),
        (
            "Email Will Smith, or rather May Jones when ready.",
            "Email May Jones when ready.",
        ),
    ),
    ids=("trailing-prepositional-explanation", "trailing-time-explanation"),
)
def test_entity_correction_replacement_must_be_terminal(guard, raw, cleaned):
    result = guard(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Call John Smith, I mean privacy matters.", "Call privacy matters."),
        ("Call John Smith, I mean James resigned.", "Call James resigned."),
        (
            "Call John Smith Lee, I mean James left early.",
            "Call James left early.",
        ),
        (
            "contact john smith, I mean james brown.",
            "contact james brown.",
        ),
    ),
    ids=("lowercase-tail", "capitalized-verb-tail", "three-word-tail", "lowercase-name-limit"),
)
def test_multiword_unstructured_targets_require_capitalized_source_atoms(
    guard,
    raw,
    cleaned,
):
    result = guard(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("I was so happy about the result.", "I was happy about the result."),
        ("I like this plan a lot.", "I this plan a lot."),
    ),
    ids=("content-bearing-so", "content-bearing-like"),
)
def test_early_content_bearing_discourse_words_cannot_be_deleted(raw, cleaned):
    result = run_chat_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
def test_comparative_like_the_one_is_bounded_by_a_reviewed_content_noun(guard):
    raw = "Please send the file like the one from yesterday."
    cleaned = "Please send the file from yesterday."
    assert guard(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize(
    ("guard", "raw", "cleaned"),
    (
        (run_chat_guards, "I like the one on the left.", "I the one on the left."),
        (run_email_guards, "We like the one on the left.", "We the one on the left."),
    ),
    ids=("chat-subject-pronoun", "email-subject-pronoun"),
)
def test_lexical_like_the_one_cannot_be_deleted(guard, raw, cleaned):
    result = guard(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


@pytest.mark.parametrize(
    ("guard", "raw", "cleaned"),
    (
        (
            run_chat_guards,
            "The meeting is March third at nine AM, no wait April fourth at ten PM.",
            "The meeting is April 4 at 10 PM.",
        ),
        (
            run_email_guards,
            "The meeting is Friday at nine AM, sorry Saturday at ten PM.",
            "The meeting is Saturday at 10 PM.",
        ),
        (
            run_chat_guards,
            "The call is at 9:30 AM, no wait 10:45 PM.",
            "The call is at 10:45 PM.",
        ),
    ),
    ids=("chat-month-day-time", "email-weekday-time", "chat-clock-meridiem"),
)
def test_composite_correction_value_spans_are_kept(guard, raw, cleaned):
    assert guard(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize(
    ("guard", "raw", "cleaned"),
    (
        (
            run_chat_guards,
            "The meeting is March third, no wait March fourth.",
            "The meeting is March 4.",
        ),
        (
            run_email_guards,
            "The meeting is Friday at nine AM, no wait Friday at ten AM.",
            "The meeting is Friday at 10 AM.",
        ),
    ),
    ids=("chat-same-month-reduced", "email-same-weekday-reduced"),
)
def test_correction_alignment_binds_shared_atoms_to_the_target(guard, raw, cleaned):
    assert guard(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize(
    ("guard", "raw", "cleaned"),
    (
        (
            run_chat_guards,
            "The meeting is March third, no wait March fourth.",
            "The meeting is March 3 March 4.",
        ),
        (
            run_email_guards,
            "The meeting is Friday at nine AM, no wait Friday at ten AM.",
            "The meeting is Friday at 9 AM Friday at 10 AM.",
        ),
    ),
    ids=("chat-retained-same-month", "email-retained-same-weekday"),
)
def test_correction_alignment_rejects_retained_shared_atom_blends(guard, raw, cleaned):
    result = guard(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "unexpected_insertion",
        raw,
    )


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
def test_chained_corrections_keep_only_the_final_target(guard):
    raw = "Meet at nine, no wait ten, no wait eleven."
    cleaned = "Meet at 11."
    assert guard(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
def test_chained_corrections_reject_an_intermediate_target_blend(guard):
    raw = "Meet at nine, no wait ten, no wait eleven."
    cleaned = "Meet at 10 11."
    result = guard(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "unexpected_insertion",
        raw,
    )


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
def test_nonterminal_bare_sorry_is_allowed_only_in_a_proven_chain(guard):
    raw = "Meet at nine, sorry ten, no wait eleven."
    cleaned = "Meet at 11."
    assert guard(raw, cleaned).final_text == cleaned


@pytest.mark.parametrize("guard", (run_chat_guards, run_email_guards), ids=("chat", "email"))
def test_consecutive_bare_sorry_chain_keeps_only_the_final_target(guard):
    raw = "Meet at nine, sorry ten, sorry eleven."
    cleaned = "Meet at 11."
    assert guard(raw, cleaned).final_text == cleaned


def test_every_composite_correction_target_atom_remains_required():
    raw = "The meeting is March third at nine AM, no wait April fourth at ten PM."
    corrupted = "The meeting is April 4 at 11 PM."
    result = run_chat_guards(raw, corrupted)
    assert result.action == "fallback"
    assert result.final_text == raw


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Meeting is Friday, sorry Saturday.", "Meeting is Friday Saturday."),
        (
            "The meeting is March third, no wait April fourth.",
            "The meeting is March April 4.",
        ),
        ("Meet at nine, no wait ten.", "Meet at nine 10."),
    ),
    ids=("weekday-blend", "date-blend", "time-blend"),
)
def test_recognized_correction_old_span_must_disappear(raw, cleaned):
    result = run_chat_guards(raw, cleaned)
    assert result.action == "fallback"
    assert result.final_text == raw


def test_email_recognized_correction_old_span_must_disappear():
    raw = "The meeting is Friday, sorry Saturday."
    cleaned = "The meeting is Friday Saturday."
    result = run_email_guards(raw, cleaned)
    assert result.action == "fallback"
    assert result.final_text == raw


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("The date is March third, no wait April.", "The date is April."),
        ("Meet at nine AM, no wait ten.", "Meet at 10."),
    ),
    ids=("date-reduction", "time-reduction"),
)
def test_family_compatible_correction_reductions_are_kept(raw, cleaned):
    assert run_chat_guards(raw, cleaned).final_text == cleaned


def test_value_adjacent_explanatory_sorry_remains_lexical():
    raw = "The office closes Monday, sorry Tuesday is a holiday."
    cleaned = "The office closes Monday, Tuesday is a holiday."
    result = run_chat_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


@pytest.mark.parametrize(
    "cleaned",
    (
        "1. Call the dentist\n1. Book the flight\n3. Email mom",
        "1. Call the dentist\n9. Book the flight\n3. Email mom",
        "2. Call the dentist\n3. Book the flight\n4. Email mom",
    ),
    ids=("duplicate", "arbitrary", "wrong-start"),
)
def test_source_cued_list_markers_must_be_sequential(cleaned):
    raw = "First call the dentist, second book the flight, third email mom."
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize(
    "raw",
    (
        "Note: this is important.",
        "Meet at 9:30 tomorrow.",
        "Visit https://example.com/path for details.",
    ),
    ids=("ordinary-prose", "time", "url"),
)
def test_non_list_colons_do_not_authorize_numbered_structure(raw):
    cleaned = "1. First item\n2. Second item"
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "That was a bad plan: unfortunately, it failed.",
            "1: Unfortunately\n2: It failed",
        ),
        ("The plan: today, not tomorrow.", "1 - Today\n2 - Not tomorrow"),
    ),
    ids=("cue-word-prose", "contrast-fragment"),
)
def test_cue_word_prose_and_contrast_are_not_colon_enumerations(raw, cleaned):
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Plan: ship today, ship tomorrow.", "1. Ship today\n2. Ship tomorrow"),
        ("Agenda: review sales, review hiring.", "1. Review sales\n2. Review hiring"),
        (
            "Packing list: rain jacket, otherwise stay home.",
            "1. Rain jacket\n2. Otherwise stay home",
        ),
        (
            "Items: milk, although eggs are optional.",
            "1. Milk\n2. Although eggs are optional",
        ),
    ),
    ids=("bare-plan", "bare-agenda", "otherwise-prose", "although-prose"),
)
def test_colon_lists_require_positive_non_subordinate_evidence(raw, cleaned):
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Shopping list: milk, eggs, bread.",
            "Shopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
        (
            "The following: finalize the deck; send invites; book the room.",
            "The following:\n1. Finalize the deck\n2. Send invites\n3. Book the room",
        ),
    ),
    ids=("cue-noun-comma-items", "cue-phrase-semicolon-items"),
)
def test_cue_led_colon_enumerations_authorize_numbered_structure(raw, cleaned):
    assert run_stage1_guards(raw, cleaned).action == "keep"


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Task list: review sales, send invites.",
            "Task list:\n1. Review sales\n2. Send invites",
        ),
        (
            "Items: the final copy, updated logos.",
            "Items:\n1. The final copy\n2. Updated logos",
        ),
    ),
    ids=("action-fragments", "noun-fragments"),
)
def test_colon_list_positive_fragment_grammar_accepts_bounded_items(raw, cleaned):
    assert run_stage1_guards(raw, cleaned).action == "keep"


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Packing list: your passport, your charger.",
            "Packing list:\n1. Your passport\n2. Your charger",
        ),
        (
            "Task list: call her, email him.",
            "Task list:\n1. Call her\n2. Email him",
        ),
    ),
    ids=("possessive-item-heads", "object-pronoun-actions"),
)
def test_colon_item_fragments_allow_possessives_and_action_objects(raw, cleaned):
    assert run_stage1_guards(raw, cleaned).action == "keep"


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Packing list: a can opener, rain jacket.",
            "Packing list:\n1. A can opener\n2. Rain jacket",
        ),
        (
            "Task list: do laundry, have lunch.",
            "Task list:\n1. Do laundry\n2. Have lunch",
        ),
        (
            "Guest list: Will Smith, Jane Doe.",
            "Guest list:\n1. Will Smith\n2. Jane Doe",
        ),
    ),
    ids=("auxiliary-shaped-noun", "verb-shaped-actions", "proper-name-will"),
)
def test_colon_comma_lists_use_structural_short_fragment_proof(raw, cleaned):
    assert run_stage1_guards(raw, cleaned).action == "keep"


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Items: milk, if the store is open.",
            "Items:\n1. Milk\n2. If the store is open",
        ),
        (
            "Items: milk, while eggs are optional.",
            "Items:\n1. Milk\n2. While eggs are optional",
        ),
        (
            "Items: milk, when the store opens.",
            "Items:\n1. Milk\n2. When the store opens",
        ),
        (
            "Items: milk, eggs are optional.",
            "Items:\n1. Milk\n2. Eggs are optional",
        ),
        (
            "Items: milk, the store was closed.",
            "Items:\n1. Milk\n2. The store was closed",
        ),
    ),
    ids=("if-clause", "while-clause", "when-clause", "finite-copula", "finite-past"),
)
def test_colon_list_positive_fragment_grammar_rejects_clauses(raw, cleaned):
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Packing list: your passport, your charger is ready.",
            "Packing list:\n1. Your passport\n2. Your charger is ready",
        ),
        (
            "Task list: call her, she emails him tomorrow.",
            "Task list:\n1. Call her\n2. She emails him tomorrow",
        ),
    ),
    ids=("possessive-finite-clause", "subject-pronoun-clause"),
)
def test_colon_item_fragment_person_words_do_not_authorize_nearby_prose(raw, cleaned):
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Shopping list: milk, eggs. Bread is optional.",
            "Shopping list:\n1. Milk\n2. Eggs\n3. Bread is optional",
        ),
        (
            "Items: milk, eggs are very optional.",
            "Items:\n1. Milk\n2. Eggs are very optional",
        ),
        (
            "Task list: call Sam; send the revised invitation to every attendee before noon today; book room.",
            "Task list:\n1. Call Sam\n2. Send the revised invitation to every attendee before noon today\n3. Book room",
        ),
    ),
    ids=("extra-sentence", "long-comma-fragment", "overlong-semicolon-fragment"),
)
def test_colon_structural_fragment_proof_rejects_unbounded_prose(raw, cleaned):
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize(
    "cleaned",
    (
        "Shopping list:\n1. Milk\n2. Eggs and bread",
        "Shopping list:\n1. Eggs\n2. Milk\n3. Bread",
        "Shopping list:\n1. Milk and eggs\n2. Bread\n3. Fresh",
        "Shopping list:\n1. Milk\nFresh only\n2. Eggs\n3. Bread",
    ),
    ids=("merge-count", "regroup-order", "split-merge", "newline-extra"),
)
def test_colon_numbered_output_must_bind_one_to_one_to_source_fragments(cleaned):
    raw = "Shopping list: milk, eggs, bread."
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


def test_colon_source_suffix_newline_cannot_authorize_numbered_output():
    raw = "Shopping list: milk,\neggs, bread."
    cleaned = "Shopping list:\n1. Milk\n2. Eggs\n3. Bread"
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize(
    "cleaned",
    (
        "---\nShopping list:\n1. Milk\n2. Eggs\n3. Bread",
        "Shopping list:\n1. Milk\n2. Eggs\n3. Bread\n***",
    ),
    ids=("punctuation-prefix", "punctuation-suffix"),
)
def test_introduced_numbered_list_rejects_punctuation_only_framing(cleaned):
    raw = "Shopping list: milk, eggs, bread."
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


def test_ordinal_numbered_list_rejects_punctuation_only_framing():
    raw = "First call Sam, second email Pat."
    cleaned = "---\n1. Call Sam\n2. Email Pat"
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


def test_colon_numbered_list_rejects_attached_markdown_heading_prefix():
    raw = "Shopping list: milk, eggs, bread."
    cleaned = "### Shopping list:\n1. Milk\n2. Eggs\n3. Bread"
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_markdown_heading_prefix_passes_in_real_pipeline_in_every_mode(mode):
    raw = "Shopping list: milk, eggs, bread."
    cleaned = "### Shopping list:\n1. Milk\n2. Eggs\n3. Bread"
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=cleaned,
        profile_id="chat",
    ) == cleaned


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "### Shopping list: milk, eggs, bread.",
            "### Shopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
        (
            "---\nFirst call Sam, second email Pat.",
            "---\n1. Call Sam\n2. Email Pat",
        ),
    ),
    ids=("dictated-heading", "dictated-horizontal-rule"),
)
def test_source_present_markdown_framing_remains_allowed(raw, cleaned):
    assert run_stage1_guards(raw, cleaned).action == "keep"


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "# Notes\nShopping list: milk, eggs, bread.",
            "Notes\n# Shopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
        (
            "# Notes\nShopping list: milk, eggs, bread.",
            "# Notes\n# Shopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
        (
            "Notes\n---\nShopping list: milk, eggs, bread.",
            "---\nNotes\nShopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
        (
            "---\nShopping list: milk, eggs, bread.",
            "---\n---\nShopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
    ),
    ids=("moved-heading", "duplicated-heading", "moved-rule", "duplicated-rule"),
)
def test_markdown_framing_cannot_be_moved_or_duplicated(raw, cleaned):
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "# Notes\nShopping list: milk, eggs, bread.",
            "Notes\n# Shopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
        (
            "# Notes\nShopping list: milk, eggs, bread.",
            "# Notes\n# Shopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
        (
            "Notes\n---\nShopping list: milk, eggs, bread.",
            "---\nNotes\nShopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
        (
            "---\nShopping list: milk, eggs, bread.",
            "---\n---\nShopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
    ),
    ids=("moved-heading", "duplicated-heading", "moved-rule", "duplicated-rule"),
)
def test_markdown_framing_provenance_passes_in_every_mode(
    raw,
    cleaned,
    mode,
):
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=cleaned,
        profile_id="chat",
    ) == cleaned


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Notes\n### Shopping list: milk, eggs, bread.",
            "Notes\n### Shopping list:\n1. Milk\n2. Eggs\n3. Bread",
        ),
        (
            "Notes\n---\nFirst call Sam, second email Pat.",
            "Notes\n---\n1. Call Sam\n2. Email Pat",
        ),
    ),
    ids=("positioned-heading", "positioned-horizontal-rule"),
)
def test_exact_positioned_markdown_framing_remains_allowed(raw, cleaned):
    assert run_stage1_guards(raw, cleaned).action == "keep"


_TRAILING_MARKDOWN_TRANSFORMS = (
    (
        "Shopping list: milk, eggs, bread.",
        "Shopping list:\n1. Milk\n2. Eggs\n3. Bread",
    ),
    (
        "Number one call Sam, number two email Pat.",
        "1. Call Sam\n2. Email Pat",
    ),
    (
        "First call Sam, second email Pat.",
        "1. Call Sam\n2. Email Pat",
    ),
    (
        "Meet at nine, no wait ten.",
        "Meet at 10.",
    ),
)


@pytest.mark.parametrize(
    ("raw_body", "cleaned_body"),
    _TRAILING_MARKDOWN_TRANSFORMS,
    ids=("colon", "number-one", "ordinal", "accepted-correction"),
)
@pytest.mark.parametrize("framing", ("---", "## Next"), ids=("rule", "heading"))
def test_trailing_markdown_framing_survives_supported_transform(
    raw_body,
    cleaned_body,
    framing,
):
    raw = f"{raw_body}\n{framing}"
    cleaned = f"{cleaned_body}\n{framing}"
    assert run_stage1_guards(raw, cleaned).action == "keep"


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
@pytest.mark.parametrize(
    ("raw_body", "cleaned_body"),
    _TRAILING_MARKDOWN_TRANSFORMS,
    ids=("colon", "number-one", "ordinal", "accepted-correction"),
)
@pytest.mark.parametrize("framing", ("---", "## Next"), ids=("rule", "heading"))
def test_trailing_markdown_framing_survives_real_pipeline_in_every_mode(
    raw_body,
    cleaned_body,
    framing,
    mode,
):
    raw = f"{raw_body}\n{framing}"
    cleaned = f"{cleaned_body}\n{framing}"
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=cleaned,
        profile_id="chat",
    ) == cleaned


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        (
            "Shopping list: milk, eggs, bread.\n---",
            "Shopping list:\n---\n1. Milk\n2. Eggs\n3. Bread",
        ),
        (
            "First call Sam, second email Pat.\n---",
            "1. Call Sam\n---\n2. Email Pat",
        ),
        (
            "Meet at nine, no wait ten.\n---",
            "Meet at\n---\n10.",
        ),
    ),
    ids=("colon-lead-and-items", "ordinal-items", "correction-target"),
)
def test_canonical_markdown_positions_keep_lexical_content_atoms(raw, cleaned):
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


def test_colon_list_cue_requires_at_least_two_item_segments():
    raw = "Shopping list: milk and eggs."
    cleaned = "1. Milk\n2. Eggs"
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


@pytest.mark.parametrize(
    "cleaned",
    (
        "1: Call the dentist\n2: Book the flight\n3: Email mom",
        "1 - Call the dentist\n2 - Book the flight\n3 - Email mom",
    ),
    ids=("colon-markers", "spaced-hyphen-markers"),
)
def test_source_cued_lists_allow_sequential_alternate_numeric_markers(cleaned):
    raw = "First call the dentist, second book the flight, third email mom."
    assert run_stage1_guards(raw, cleaned).action == "keep"


@pytest.mark.parametrize(
    "cleaned",
    (
        "1: Call the dentist\n1: Book the flight\n3: Email mom",
        "1 - Call the dentist\n9 - Book the flight\n3 - Email mom",
    ),
    ids=("colon-duplicate", "hyphen-arbitrary"),
)
def test_alternate_numeric_markers_must_be_sequential(cleaned):
    raw = "First call the dentist, second book the flight, third email mom."
    result = run_stage1_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


def test_existing_alternate_numeric_markers_require_exact_preservation():
    raw = "2: Keep the heading\n4: Keep the footer"
    assert run_stage1_guards(raw, raw).action == "keep"

    changed_shape = "2 - Keep the heading\n4 - Keep the footer"
    result = run_stage1_guards(raw, changed_shape)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "introduced_structure",
        raw,
    )


def test_single_line_subtraction_is_not_a_hyphen_list_marker():
    raw = "One minus two equals negative one."
    cleaned = "1 - 2 = -1"
    assert run_stage1_guards(raw, cleaned).action == "keep"


@pytest.mark.parametrize(
    ("marker", "mode"),
    ((marker, mode) for marker in (":", " -") for mode in ("enforce", "log", "off")),
    ids=(
        "colon-enforce", "colon-log", "colon-off",
        "hyphen-enforce", "hyphen-log", "hyphen-off",
    ),
)
def test_alternate_marker_presentation_has_pipeline_parity(marker, mode):
    raw = "First call the dentist, second book the flight, third email mom."
    cleaned = (
        f"1{marker} Call the dentist\n"
        f"2{marker} Book the flight\n"
        f"3{marker} Email mom"
    )
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=cleaned,
        profile_id="chat",
    ) == cleaned


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_composite_correction_survives_real_ordered_pipeline(mode):
    raw = (
        "The meeting is March third at nine AM, no wait "
        "April fourth at ten PM."
    )
    cleaned = "The meeting is April 4 at 10 PM."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=cleaned,
        profile_id="chat",
    ) == cleaned


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_unresolved_composite_digit_corruption_falls_back_before_rollout_mode_narrow_policy(mode):
    raw = (
        "The meeting is March third at nine AM, no wait "
        "April fourth at ten PM."
    )
    corrupted = "The meeting is April 4 at 1:00 PM."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=corrupted,
        profile_id="chat",
    ) == corrupted


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_scratch_that_digit_repair_is_mandatory_in_every_rollout_mode_narrow_policy(mode):
    raw = "Set the alarm for nine AM, scratch that ten AM."
    corrupted = "Set the alarm for 11 AM."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=corrupted,
        profile_id="chat",
    ) == corrupted


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_scratch_that_unresolved_digit_corruption_falls_back_in_every_mode_narrow_policy(mode):
    raw = "Set the alarm for nine AM, scratch that ten AM."
    corrupted = "Set the alarm for 1:00 AM."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=corrupted,
        profile_id="chat",
    ) == corrupted


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_chained_correction_final_digit_repairs_in_every_mode_narrow_policy(mode):
    raw = "Meet at nine, no wait ten, no wait eleven."
    corrupted = "Meet at 12."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=corrupted,
        profile_id="chat",
    ) == corrupted


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_chained_correction_final_digit_f1b_falls_back_in_every_mode_narrow_policy(mode):
    raw = "Meet at nine, no wait ten, no wait eleven."
    corrupted = "Meet at 1:00."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=corrupted,
        profile_id="chat",
    ) == corrupted


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_mixed_sorry_chain_final_digit_repairs_in_every_mode_narrow_policy(mode):
    raw = "Meet at nine, sorry ten, no wait eleven."
    corrupted = "Meet at 12."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=corrupted,
        profile_id="chat",
    ) == corrupted


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_mixed_sorry_chain_final_digit_f1b_falls_back_in_every_mode_narrow_policy(mode):
    raw = "Meet at nine, sorry ten, no wait eleven."
    corrupted = "Meet at 1:00."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=corrupted,
        profile_id="chat",
    ) == corrupted


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_consecutive_sorry_chain_final_digit_repairs_in_every_mode_narrow_policy(mode):
    raw = "Meet at nine, sorry ten, sorry eleven."
    corrupted = "Meet at 12."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=corrupted,
        profile_id="chat",
    ) == corrupted


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_consecutive_sorry_chain_final_digit_f1b_falls_back_in_every_mode_narrow_policy(mode):
    raw = "Meet at nine, sorry ten, sorry eleven."
    corrupted = "Meet at 1:00."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )
    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=corrupted,
        profile_id="chat",
    ) == corrupted


@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_alternate_numeric_structure_does_not_block_digit_processing(mode):
    raw = "Lay out the checklist as short lines only."
    cleaned = "1: Lay out the checklist\n2: Use short lines only"
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )

    with patch("whisperstream.llm_enhancer.repair_source_digits", return_value=cleaned) as repair:
        assert enhancer._post_process_local_output(
            raw=raw,
            cleaned=cleaned,
            profile_id="chat",
        ) == cleaned
    repair.assert_called_once_with(enhanced=cleaned, raw=raw)


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Um, please send the report.", "Please send the report."),
        ('Write the word "uh" in the note.', "Write the word in the note."),
        ("This is very very important.", "This is very important."),
        ("Please call call support.", "Please call support."),
    ),
    ids=("complete-filler", "quoted-filler", "intentional-emphasis", "unreviewed-duplicate"),
)
def test_complete_fillers_and_arbitrary_duplicates_remain_required(raw, cleaned):
    result = run_chat_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


@pytest.mark.parametrize(
    "row",
    [row for row in _load_rows("legitimate.jsonl") if row["contract"].startswith("code_")],
    ids=lambda row: row["id"],
)
def test_legitimate_code_fixtures_use_separate_reviewed_contracts(row):
    family = row["contract"].removeprefix("code_")
    result = run_code_guards(row["raw"], row["cleaned"], family=family)
    assert (result.action, result.guard, result.final_text) == ("keep", None, row["cleaned"])


def test_stream_code_does_not_allow_gemma_casing_directive_consumption():
    raw = "snake case auth middleware should wrap every handler"
    cleaned = "auth_middleware should wrap every handler."

    assert run_code_guards(raw, cleaned, family="gemma").action == "keep"
    result = run_code_guards(raw, cleaned, family="stream")
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


def test_code_explicit_correction_keeps_the_reviewed_target():
    raw = "Set timeout to thirty that should be sixty seconds."
    cleaned = "Set timeout to 60 seconds."

    assert run_code_guards(raw, cleaned, family="gemma").final_text == cleaned

    missing_target = run_code_guards(raw, "Set timeout to seconds.", family="gemma")
    assert (missing_target.action, missing_target.guard, missing_target.final_text) == (
        "fallback",
        "correction_target_missing",
        raw,
    )


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Use value equals null.", "Use value = null."),
        ("Use value double equals null.", "Use value == null."),
        ("Use value triple equals null.", "Use value === null."),
        ("Use value not equals null.", "Use value != null."),
        ("Open config dot json.", "Open config.json."),
        ("Use src slash auth.", "Use src/auth."),
        ("Use await instead of dot then.", "Use await instead of .then()."),
        ("Use async oight here.", "Use async/await here."),
        ("Install at tanstack slash react query.", "Install @tanstack/react-query."),
    ),
    ids=(
        "equals",
        "double-equals",
        "triple-equals",
        "not-equals",
        "dot",
        "slash",
        "dot-then",
        "async-await",
        "scoped-package",
    ),
)
def test_code_spoken_operator_mappings_are_preserved(raw, cleaned):
    assert run_code_guards(raw, cleaned, family="gemma").final_text == cleaned


def test_code_atoms_retain_reviewed_operator_evidence():
    atoms = normalize_code_atoms(
        "value triple equals other slash path dot json",
        family="gemma",
    )
    assert [atom.value for atom in atoms] == [
        "value",
        "operator:===",
        "other",
        "operator:/",
        "path",
        "operator:.",
        "json",
    ]


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    (
        ("Use value equals null.", "Use value null."),
        ("Use value double equals null.", "Use value = null."),
        ("Use value not equals null.", "Use value == null."),
        ("Use src slash auth.", "Use src.auth."),
    ),
    ids=("omitted", "narrowed", "changed", "slash-to-dot"),
)
def test_code_operator_omission_or_substitution_falls_back(raw, cleaned):
    result = run_code_guards(raw, cleaned, family="gemma")
    assert result.action == "fallback"
    assert result.final_text == raw
    assert result.guard in {"required_content_missing", "unexpected_insertion"}


@pytest.mark.parametrize(
    "row",
    [row for row in _load_rows("legitimate.jsonl") if row["contract"].startswith("email")],
    ids=lambda row: row["id"],
)
def test_legitimate_email_blocks_routing_lists_and_signoffs_are_kept(row):
    result = run_email_guards(row["raw"], row["cleaned"])
    assert (result.action, result.guard, result.final_text) == ("keep", None, row["cleaned"])


@pytest.mark.parametrize("greeting", ("good morning", "good afternoon", "good evening"))
def test_email_routed_multiword_greeting_moves_with_recipient(greeting):
    raw = f"email Priya {greeting} quick update the deploy went through and everything looks stable"
    rendered_greeting = greeting.capitalize()
    cleaned = f"{rendered_greeting} Priya,\n\nQuick update: the deploy went through and everything looks stable."

    result = run_email_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == ("keep", None, cleaned)

    missing_recipient = run_email_guards(
        raw,
        f"{rendered_greeting},\n\nQuick update: the deploy went through and everything looks stable.",
    )
    assert (missing_recipient.action, missing_recipient.final_text) == ("fallback", raw)


@pytest.mark.parametrize(
    "case_id",
    ("F6-routing-greeting-invention", "F6-disfluency-name-invention"),
)
def test_email_inventions_fall_back_with_evidenced_tags(case_id):
    row = next(row for row in _load_rows("failures.jsonl") if row["id"] == case_id)
    result = run_email_guards(row["raw"], row["cleaned"])
    assert (result.action, result.guard, result.final_text) == (
        row["expected_action"],
        row["expected_guard"],
        row["raw"],
    )


def test_email_content_bearing_so_cannot_be_deleted():
    raw = "I was so happy about the result."
    cleaned = "I was happy about the result."
    result = run_email_guards(raw, cleaned)
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "required_content_missing",
        raw,
    )


def test_contract_dispatch_is_finite_and_unknown_is_observation_only():
    raw = "Send the contract to legal today."
    invented = "Send the urgent contract to legal today."
    known = run_contract_guards(
        raw,
        invented,
        GuardContract(name="code_stream", prompt_family="stream_code"),
    )
    unknown = run_contract_guards(
        raw,
        invented,
        GuardContract(name="unknown", prompt_family="unknown"),
    )
    assert (known.action, known.guard) == ("fallback", "unexpected_insertion")
    assert (unknown.action, unknown.guard, unknown.final_text) == ("keep", None, invented)


def test_devmic_fixture_is_complete_with_reviewed_expectation_labels():
    case_ids = {row["id"] for row in _DEVMIC_ROWS}
    assert len(_DEVMIC_ROWS) == 12
    assert {
        "MIC-NUM1-f1b",
        "MIC-TIME2-f1b",
        "MIC-REFUSE1-source-conditioned",
        "MIC-LIST1-source-cues",
    } <= case_ids
    assert all(
        row["expected_action"] in {"keep", "fallback"}
        and "expected_guard" in row
        for row in _DEVMIC_ROWS
    )


@pytest.mark.parametrize("row", _DEVMIC_ROWS, ids=lambda row: row["id"])
def test_reviewed_devmic_calibration_locks_action_tag_and_whole_unit(row):
    result = _run_guard_pipeline(row)
    expected_text = row["raw"] if row["expected_action"] == "fallback" else row["cleaned"]

    assert (result.action, result.guard, result.final_text) == (
        row["expected_action"],
        row["expected_guard"],
        expected_text,
    )
    assert result.final_text in {row["raw"], row["cleaned"]}


@pytest.mark.parametrize("row", _DEVMIC_ROWS, ids=lambda row: row["id"])
def test_reviewed_devmic_identity_and_final_text_idempotence(row):
    identity = _run_guard_pipeline(row, cleaned=row["raw"])
    first = _run_guard_pipeline(row)
    second = _run_guard_pipeline(row, cleaned=first.final_text)

    assert identity.final_text == row["raw"]
    assert second.final_text == first.final_text


def _remove_punctuation(text: str) -> str:
    return text.translate(str.maketrans("", "", ".,!?;:"))


def _vary_whitespace(text: str) -> str:
    return "\n  " + " \t ".join(text.split()) + "  \n"


@pytest.mark.parametrize(
    "variant",
    (str.swapcase, _remove_punctuation, _vary_whitespace),
    ids=("case", "punctuation", "whitespace"),
)
def test_chat_decisions_are_stable_under_deterministic_presentation_variants(variant):
    raw = "Send the contract to Legal today."
    invented = "Send the urgent contract to Legal today."

    kept = run_chat_guards(raw, variant(raw), emit_log=False)
    rejected = run_chat_guards(variant(raw), variant(invented), emit_log=False)

    assert (kept.action, kept.guard, kept.final_text) == ("keep", None, variant(raw))
    assert (rejected.action, rejected.guard, rejected.final_text) == (
        "fallback",
        "unexpected_insertion",
        variant(raw),
    )




@pytest.mark.parametrize("mode", ("enforce", "log", "off"))
def test_universal_f1b_fallback_remains_mandatory_in_every_mode(mode):
    raw = "Call me back at five five five one two three four"
    cleaned = "Call me back at 5551-4234."
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_provider="local",
            enhancement_local_model="test-stream",
            enhancement_output_guard_mode=mode,
        )
    )

    assert enhancer._post_process_local_output(
        raw=raw,
        cleaned=cleaned,
        profile_id="chat",
    ) == raw


@pytest.mark.parametrize("row", _DEVMIC_ROWS, ids=lambda row: row["id"])
def test_current_delivery_against_historical_devmic_outputs(row):
    model = "gemma4-e2b-instruct" if row["contract"] == "chat_gemma" else "test-stream"
    enhancer = LLMEnhancer(
        WhisperstreamConfig(
            enhancement_enabled=True,
            enhancement_provider="local",
            enhancement_local_endpoint="http://127.0.0.1:1",
            enhancement_local_model=model,
            enhancement_output_guard_mode="enforce",
            default_profile_id="chat",
            profiles=[{"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT}],
        )
    )
    expected = row["raw"] if row["expected_action"] == "fallback" else row["cleaned"]
    # Accepted tradeoff: semantic rejection no longer blocks this model rewrite.
    if row["id"] == "MIC-INJ2-obeyed":
        expected = row["cleaned"]

    with patch.object(
        enhancer,
        "_call_provider",
        new_callable=AsyncMock,
        return_value=row["cleaned"],
    ):
        whole = asyncio.run(enhancer.enhance(row["raw"]))
        chunk = asyncio.run(enhancer.enhance_chunk(row["raw"]))

    assert whole == chunk == expected


@pytest.mark.parametrize(
    "call_guard",
    (
        lambda: run_stage1_guards(None, object()),
        lambda: guard_digit_integrity(None, object()),
        lambda: guard_output(None, object()),
        lambda: run_chat_guards(None, object(), emit_log=False),
        lambda: run_code_guards(None, object(), family="stream", emit_log=False),
        lambda: run_email_guards(None, object(), emit_log=False),
        lambda: run_contract_guards(
            None,
            object(),
            GuardContract(name="chat_stream", prompt_family="stream_chat"),
            emit_log=False,
        ),
    ),
    ids=("stage1", "digit", "combined", "chat", "code", "email", "contract"),
)
def test_guard_entrypoints_never_raise_and_fail_closed_on_invalid_input(call_guard):
    result = call_guard()
    assert (result.action, result.guard, result.final_text) == (
        "fallback",
        "unexpected_error",
        "",
    )


def test_guard_pipeline_median_latency_is_below_ten_ms_per_call():
    phrase = "project owners reviewed the draft and confirmed each remaining action before release"
    raw = " ".join([phrase] * 18) + "."
    row = {
        "raw": raw,
        "cleaned": raw,
        "contract": "chat_stream",
    }
    assert 150 <= len(raw.split()) <= 250

    for _ in range(20):
        assert _run_guard_pipeline(row).action == "keep"

    batch_ms = []
    for _ in range(7):
        started = perf_counter_ns()
        for _ in range(100):
            _run_guard_pipeline(row)
        batch_ms.append((perf_counter_ns() - started) / 100 / 1_000_000)

    median_ms = statistics.median(batch_ms)
    print(f"output_guard_median_ms={median_ms:.3f}")
    assert median_ms < 10.0, f"median guard latency was {median_ms:.3f} ms per call"


@pytest.fixture(autouse=True)
def synthetic_prompt_catalog(monkeypatch):
    from whisperstream import enhancement_models
    from tests.legacy_model_fixtures import SYNTHETIC_PROMPT_MODELS
    monkeypatch.setattr(enhancement_models, "LOCAL_BUNDLED_MODELS",
        enhancement_models.LOCAL_BUNDLED_MODELS + SYNTHETIC_PROMPT_MODELS)
