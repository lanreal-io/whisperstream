"""Unit tests for the default-signature step (main.py).

`_apply_email_signature` is a pure helper on WhisperstreamApp: it appends the
built-in Email profile's `signature` field as a blank-line-separated block, but
ONLY when Email is active, it has a non-empty signature, and the enhanced text
does not already end in a (short, dictated) sign-off block. Stale signature
fields on Chat or custom profiles are ignored.

Built via __new__ (bypassing the heavy __init__) with only `app.config` wired,
so the helper is exercised in isolation with no mic / LLM / network.

The second section (chunked path) exercises the signature step via the
_enhance_then_inject_chunked path: signature must land exactly once on the last
chunk; intermediate chunks must be untouched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from whisperstream.enhancement_outcomes import EnhancementResult
from whisperstream.email_signature import _is_closing_phrase_line, _is_signoff_name_block
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.main import (
    WhisperstreamApp,
    _CHUNK_THRESHOLD_WORDS,
)

pytestmark = pytest.mark.usefixtures("accepted_eula")


def _make_app(*, signature="Best,\nAlex", default_profile_id="chat", signature_on="email"):
    """Build a WhisperstreamApp with `signature` set on ONE profile.

    signature_on = the profile id that carries the per-profile `signature`
    field (default "email"). Other profiles have no signature. An empty
    `signature` leaves every profile without one (feature disabled).
    """
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    profiles = [
        {"id": "chat", "name": "Chat", "prompt": "p"},
        {"id": "code", "name": "Code", "prompt": "c"},
        {"id": "email", "name": "Email", "prompt": "e"},
    ]
    if signature:
        for p in profiles:
            if p["id"] == signature_on:
                p["signature"] = signature
    app.config = WhisperstreamConfig(
        default_profile_id=default_profile_id,
        profiles=profiles,
    )
    return app


# ---------------------------------------------------------------------------
# (a) profile has a signature + no sign-off -> appended after a blank line
# ---------------------------------------------------------------------------
def test_profile_no_signoff_appends_signature():
    app = _make_app(signature="Best,\nAlex")
    text = "Hi Sam,\n\nThanks for the update. I will review it tomorrow."
    out = app._apply_email_signature(text, "email")
    assert out == text + "\n\nBest,\nAlex"


def test_profile_via_default_pointer_appends():
    # profile_id None but default_profile_id == "email" -> resolves to email.
    app = _make_app(signature="Best,\nAlex", default_profile_id="email")
    text = "Hi Sam,\n\nThanks for the update."
    out = app._apply_email_signature(text, None)
    assert out == text + "\n\nBest,\nAlex"


def test_custom_non_email_profile_with_stale_signature_is_unchanged():
    app = _make_app(signature="- Alex", default_profile_id="work", signature_on="work")
    app.config.profiles.append({"id": "work", "name": "Work", "prompt": "w", "signature": "- Alex"})
    text = "Quick note before the standup."
    out = app._apply_email_signature(text, "work")
    assert out == text


def test_chat_profile_with_stale_signature_is_unchanged():
    app = _make_app(
        signature="Best,\nAlex",
        default_profile_id="chat",
        signature_on="chat",
    )
    text = "Fix the parser bug in the tokenizer."
    assert app._apply_email_signature(text, "chat") == text


def test_default_pointer_to_chat_with_stale_signature_is_unchanged():
    app = _make_app(
        signature="Best,\nAlex",
        default_profile_id="chat",
        signature_on="chat",
    )
    text = "Some cleaned chat text."
    assert app._apply_email_signature(text, None) == text


# ---------------------------------------------------------------------------
# (b) existing short closing block -> unchanged (no double-sign)
# ---------------------------------------------------------------------------
def test_existing_short_closing_block_not_double_signed():
    app = _make_app(signature="Best,\nAlex")
    text = "Hi Sam,\n\nThe layout looks off.\n\nThanks,\nAlex"
    out = app._apply_email_signature(text, "email")
    assert out == text


def test_existing_single_line_signoff_not_double_signed():
    app = _make_app(signature="Best,\nAlex")
    text = "Hi Sam,\n\nGood to hear.\n\nCheers"
    out = app._apply_email_signature(text, "email")
    assert out == text


# ---------------------------------------------------------------------------
# boundary: a LONG final paragraph after a blank line is NOT a sign-off ->
# it still gets signed.
# ---------------------------------------------------------------------------
def test_long_final_paragraph_is_not_mistaken_for_signoff():
    app = _make_app(signature="Best,\nAlex")
    long_para = (
        "I went through the whole proposal in detail and I think the timeline "
        "you suggested is reasonable, but we should revisit the budget section "
        "before we send it on to the client for final approval."
    )
    text = "Hi Sam,\n\nThanks for sending this over.\n\n" + long_para
    out = app._apply_email_signature(text, "email")
    assert out == text + "\n\nBest,\nAlex"


def test_multi_line_final_block_over_line_cap_is_signed():
    # Final block is <= 3 lines but a line exceeds the char cap -> not a sign-off.
    app = _make_app(signature="Best,\nAlex")
    last = (
        "Let me know whether the proposed delivery date still works for your team."
    )
    text = "Hi Sam,\n\nHere is the recap.\n\n" + last
    out = app._apply_email_signature(text, "email")
    assert out == text + "\n\nBest,\nAlex"


def test_final_block_over_line_count_is_signed():
    # Final block has more than _SIGNOFF_MAX_LINES short lines -> a list, not a
    # sign-off -> still signed.
    app = _make_app(signature="Best,\nAlex")
    text = "Hi Sam,\n\nHere we go.\n\nOne\nTwo\nThree\nFour"
    out = app._apply_email_signature(text, "email")
    assert out == text + "\n\nBest,\nAlex"


# ---------------------------------------------------------------------------
# (b2) no-greeting 2-block email ending in a dictated sign-off -> NOT double-
# signed. The >=3-block shape match misses this (only 2 blocks); closing-phrase
# and name-shape detection on the trailing block catch it.
# ---------------------------------------------------------------------------
def test_no_greeting_two_block_dictated_signoff_not_double_signed():
    app = _make_app(signature="Best,\nAlex")
    text = "The layout looks off.\n\nThanks,\nAlex"
    out = app._apply_email_signature(text, "email")
    assert out == text


def test_no_greeting_two_block_bare_closing_phrase_not_double_signed():
    app = _make_app(signature="Best,\nAlex")
    text = "Sounds good to me.\n\nCheers"
    out = app._apply_email_signature(text, "email")
    assert out == text


def test_no_greeting_two_block_multiword_closing_phrase_not_double_signed():
    app = _make_app(signature="Best,\nAlex")
    text = "I'll send it over tonight.\n\nBest regards,\nPriya"
    out = app._apply_email_signature(text, "email")
    assert out == text


def test_two_block_body_starting_with_closing_word_is_still_signed():
    # "Thanks for the update." begins with a closing word but is a body sentence,
    # not an exact closing phrase -> must still be signed (no false positive).
    app = _make_app(signature="Best,\nAlex")
    text = "Quick note.\n\nThanks for the update."
    out = app._apply_email_signature(text, "email")
    assert out == text + "\n\nBest,\nAlex"


# ---------------------------------------------------------------------------
# (b3) EDGE CASE FIX: no-greeting 2-block email ending in a BARE NAME (not a
# known closing phrase) is now recognized as a dictated sign-off via the
# name-shape heuristic -> NOT double-signed.
# ---------------------------------------------------------------------------
def test_no_greeting_two_block_bare_name_not_double_signed():
    app = _make_app(signature="Best,\nAlex")
    text = "The deck is ready.\n\nAlex"
    out = app._apply_email_signature(text, "email")
    assert out == text


def test_no_greeting_two_block_bare_full_name_not_double_signed():
    app = _make_app(signature="Best,\nAlex")
    text = "Sending the notes now.\n\nAlex Chen"
    out = app._apply_email_signature(text, "email")
    assert out == text


def test_two_block_lowercase_or_sentence_tail_is_still_signed():
    # A short trailing body line that is NOT name-shaped (lowercase words, or a
    # trailing period) must still be signed -> the name-shape check is conservative.
    app = _make_app(signature="Best,\nAlex")
    for tail in ("call me back", "See you Monday."):
        text = f"Quick one.\n\n{tail}"
        out = app._apply_email_signature(text, "email")
        assert out == text + "\n\nBest,\nAlex", f"tail={tail!r} should be signed"


def test_is_signoff_name_block_unit():
    assert _is_signoff_name_block("Alex")
    assert _is_signoff_name_block("Alex Chen")
    assert _is_signoff_name_block("Alex\nChen")
    assert not _is_signoff_name_block("call me back")       # lowercase words
    assert not _is_signoff_name_block("See you Monday.")    # trailing period
    assert not _is_signoff_name_block("Let me know if that works for you")  # too many words


def test_is_closing_phrase_line_exact_match_only():
    # Recognized closing phrases (with/without trailing comma or period).
    assert _is_closing_phrase_line("Thanks,")
    assert _is_closing_phrase_line("Best regards,")
    assert _is_closing_phrase_line("Cheers")
    assert _is_closing_phrase_line("Thank you.")
    assert _is_closing_phrase_line("  sincerely  ")
    # Body lines that merely start with a closing word are NOT sign-offs.
    assert not _is_closing_phrase_line("Thanks for the update.")
    assert not _is_closing_phrase_line("Best of luck with it,")
    assert not _is_closing_phrase_line("The meeting is at 3.")


# ---------------------------------------------------------------------------
# (c) profile without a signature -> unchanged
# ---------------------------------------------------------------------------
def test_profile_without_signature_unchanged():
    # Signature is on "email"; the chat profile has none -> applying to "chat"
    # leaves the text unchanged.
    app = _make_app(signature="Best,\nAlex", default_profile_id="chat")
    text = "fix the parser bug in the tokenizer"
    assert app._apply_email_signature(text, "chat") == text


def test_default_pointer_to_signatureless_profile_unchanged():
    app = _make_app(signature="Best,\nAlex", default_profile_id="chat")
    text = "some cleaned text"
    # profile_id None falls back to default ("chat"), which has no signature.
    assert app._apply_email_signature(text, None) == text


def test_unknown_profile_id_unchanged():
    app = _make_app(signature="Best,\nAlex")
    text = "some text"
    assert app._apply_email_signature(text, "does-not-exist") == text


# ---------------------------------------------------------------------------
# (d) empty / whitespace signature -> unchanged (feature disabled)
# ---------------------------------------------------------------------------
def test_empty_signature_unchanged():
    app = _make_app(signature="", default_profile_id="email")
    text = "Hi Sam,\n\nThanks for the update."
    assert app._apply_email_signature(text, "email") == text


def test_whitespace_only_signature_unchanged():
    # A profile whose signature is whitespace-only is treated as no signature.
    app = _make_app(signature="   \n  \t ", default_profile_id="email")
    text = "Hi Sam,\n\nThanks for the update."
    assert app._apply_email_signature(text, "email") == text


# ---------------------------------------------------------------------------
# (e) signature whitespace is .strip()-normalized; body trailing ws collapsed
# ---------------------------------------------------------------------------
def test_signature_surrounding_whitespace_stripped():
    app = _make_app(signature="\n  Best,\nAlex  \n", default_profile_id="email")
    text = "Hi Sam,\n\nThanks for the update."
    out = app._apply_email_signature(text, "email")
    # Signature is .strip()'d; body is rstrip()'d before joining.
    assert out == "Hi Sam,\n\nThanks for the update.\n\nBest,\nAlex"


def test_body_trailing_whitespace_collapsed_before_join():
    app = _make_app(signature="Best,\nAlex", default_profile_id="email")
    text = "Hi Sam,\n\nThanks for the update.\n\n\n  "
    out = app._apply_email_signature(text, "email")
    assert out == "Hi Sam,\n\nThanks for the update.\n\nBest,\nAlex"


# ===========================================================================
# Chunked path: signature lands on the LAST chunk only
# ===========================================================================

# A multi-chunk text: enough words to clear _CHUNK_THRESHOLD_WORDS so that
# _enhance_then_inject routes to the chunked path.
_LONG_EMAIL = " ".join(
    f"Here is sentence number {i} discussing the project update in reasonable detail."
    for i in range(40)
)


def _make_chunked_app(
    *,
    signature="Best,\nAlex",
    default_profile_id="email",
    signature_on="email",
):
    """Build a WhisperstreamApp fixture for chunked-path signature tests.

    Uses __new__ + manual wiring (same pattern as test_main_chunked_cleanup.py)
    so __init__ (heavy, requires mic/model) is skipped. `signature_on` can put
    stale data on Chat to prove only Email consumes the field.
    """
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    profiles = [
        {"id": "chat", "name": "Chat", "prompt": "p"},
        {"id": "email", "name": "Email", "prompt": "e"},
    ]
    if signature:
        for p in profiles:
            if p["id"] == signature_on:
                p["signature"] = signature
    app.config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_local_model="gemma4-e2b-instruct",
        enhancement_chunked_cleanup=True,
        default_profile_id=default_profile_id,
        profiles=profiles,
    )
    app.llm_enhancer = MagicMock()
    app.llm_enhancer.will_enhance = MagicMock(return_value=True)
    app.llm_enhancer.enhance_with_outcome = AsyncMock(
        side_effect=lambda t, **kw: EnhancementResult("SINGLE_CLEANED", "success")
    )
    # Each chunk comes back as a short, clearly-not-a-signoff string.
    app.llm_enhancer.enhance_chunk_with_outcome = AsyncMock(
        side_effect=lambda c, **kw: EnhancementResult(f"CLEANED chunk: {c[:20]}", "success")
    )
    app.text_injector = MagicMock()
    app.text_injector.process = MagicMock(side_effect=lambda t, **kw: t)
    app.text_injector.last_transcript = "FULL_PASTED_TEXT"
    app.review_state = MagicMock()
    app.license_manager = MagicMock()
    app.transcript_store = None
    app._inject_suppressed = False
    app._dictation_app = None
    app._manual_profile_id = None
    app._override_anchor = None
    app._broadcast = MagicMock()
    return app


def test_chunked_path_signature_on_last_chunk_only():
    """Signature is appended to the last chunk's cleaned text and nowhere else."""
    app = _make_chunked_app(signature="Best,\nAlex", default_profile_id="email")

    # Sanity: text must be long enough to trigger the chunked path.
    assert len(_LONG_EMAIL.split()) > _CHUNK_THRESHOLD_WORDS

    app._enhance_then_inject(_LONG_EMAIL, 2.0)

    # Must have used the chunked path (paste_chunk called, not inject).
    assert app.text_injector.paste_chunk.call_count >= 2
    app.text_injector.inject.assert_not_called()

    # process() is called once per chunk.
    proc_calls = app.text_injector.process.call_args_list
    assert len(proc_calls) >= 2

    last_proc_text = proc_calls[-1].args[0]
    assert last_proc_text.endswith("Best,\nAlex"), (
        f"Expected last chunk to end with signature, got: {last_proc_text!r}"
    )

    # Intermediate chunks must NOT end with the signature.
    for call in proc_calls[:-1]:
        chunk_text = call.args[0]
        assert not chunk_text.endswith("Best,\nAlex"), (
            f"Intermediate chunk must not carry the signature, got: {chunk_text!r}"
        )


def test_chunked_path_chat_stale_signature_is_ignored():
    """A stale Chat signature is ignored, including on the last chunk."""
    app = _make_chunked_app(
        signature="Best,\nAlex",
        default_profile_id="chat",
        signature_on="chat",
    )

    app._enhance_then_inject(_LONG_EMAIL, 2.0)

    proc_calls = app.text_injector.process.call_args_list
    for call in proc_calls:
        chunk_text = call.args[0]
        assert not chunk_text.endswith("Best,\nAlex"), (
            f"Chat profile must not append a stale signature, got: {chunk_text!r}"
        )


def test_chunked_path_empty_signature_no_op():
    """Empty signature: last chunk is unchanged."""
    app = _make_chunked_app(signature="", default_profile_id="email")

    app._enhance_then_inject(_LONG_EMAIL, 2.0)

    proc_calls = app.text_injector.process.call_args_list
    last_proc_text = proc_calls[-1].args[0]
    # With no signature, the last chunk is just the raw enhanced text (no \n\n block).
    assert "\n\n" not in last_proc_text, (
        f"Empty signature must not add blank-line block, got: {last_proc_text!r}"
    )


def test_chunked_path_last_chunk_already_signed_no_double_sign():
    """If the last chunk already ends in a short closing block, skip appending."""
    app = _make_chunked_app(signature="Best,\nAlex", default_profile_id="email")

    # A properly-shaped dictated email tail: greeting (short) + body + sign-off.
    _SIGNED_TAIL = "Hi Jordan,\n\nThe proposal looks good to me.\n\nThanks,\nJordan"

    # Override enhance_chunk so the LAST call returns text with a dictated sign-off.
    call_count = [0]

    async def _enhance_chunk_with_signoff(chunk, **kw):
        call_count[0] += 1
        # First chunk: just body text, no sign-off.
        if call_count[0] == 1:
            return EnhancementResult("CLEANED first chunk body text here", "success")
        # All subsequent chunks (including the last): return a properly-signed tail.
        return EnhancementResult(_SIGNED_TAIL, "success")

    app.llm_enhancer.enhance_chunk_with_outcome = AsyncMock(side_effect=_enhance_chunk_with_signoff)

    app._enhance_then_inject(_LONG_EMAIL, 2.0)

    proc_calls = app.text_injector.process.call_args_list
    last_proc_text = proc_calls[-1].args[0]

    # The signature must NOT be appended because there's already a sign-off.
    assert last_proc_text == _SIGNED_TAIL, (
        f"Double-sign must be avoided, got: {last_proc_text!r}"
    )
