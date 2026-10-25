"""Routing tests for _enhance_then_inject / _enhance_then_inject_chunked (main.py).

Builds a WhisperstreamApp via __new__ (bypassing the heavy __init__) and wires
only the collaborators these two methods touch, then asserts the single-call vs
chunked routing decisions and the per-chunk paste sequence.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.cleanup_chunker import chunk_for_cleanup
from whisperstream.delivery_contract import (
    DeliveryFailure,
    DeliveryReceipt,
    DeliveryRoute,
    DeliveryStatus as ContractDeliveryStatus,
)
from whisperstream.llm_enhancer import EnhancementResult
from whisperstream.main import (
    WhisperstreamApp,
    _CHUNK_TARGET_WORDS,
    _CHUNK_FIRST_TARGET_WORDS,
    _CHUNK_MIN_TAIL_WORDS,
)
from whisperstream.text_injector import DeliveryOutcome, DeliveryStatus

pytestmark = pytest.mark.usefixtures("accepted_eula")


def _expected_chunks(text):
    """Mirror the params main passes to chunk_for_cleanup."""
    return chunk_for_cleanup(
        text,
        target_words=_CHUNK_TARGET_WORDS,
        first_target_words=_CHUNK_FIRST_TARGET_WORDS,
        min_tail_words=_CHUNK_MIN_TAIL_WORDS,
    )


# ~300 words of multi-sentence dictation so it clears _CHUNK_THRESHOLD_WORDS (200).
LONG_TEXT = " ".join(
    f"This is sentence number {i} carrying a few filler words to pad the length out nicely."
    for i in range(30)
)
SHORT_TEXT = "This is a short sentence. It has only a handful of words in total here."


def _make_app(
    *,
    enabled=True,
    provider="local",
    will_enhance=True,
    inject_suppressed=False,
    chunked_cleanup=True,
    words_per_chunk=120,
    limit_first_chunk=True,
    default_profile_id="chat",
):
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app.config = WhisperstreamConfig(
        enhancement_enabled=enabled,
        enhancement_provider=provider,
        enhancement_local_model="gemma4-e2b-instruct",
        enhancement_chunked_cleanup=chunked_cleanup,
        enhancement_words_per_chunk=words_per_chunk,
        enhancement_limit_first_chunk=limit_first_chunk,
        # A non-empty default keeps the existing enhance routing; the no-default
        # (raw inject) branch is exercised by its own test below.
        default_profile_id=default_profile_id,
        profiles=[{"id": "chat", "name": "Chat", "prompt": "p"}],
        # Pin clipboard as an explicit user choice so effective-style resolution
        # is deterministic regardless of the test host's screen-reader state
        # (live SR detection would otherwise flip these raw-inject paths to
        # keystroke when a reader is running).
        input_style="clipboard",
        input_style_user_set=True,
    )
    app.llm_enhancer = MagicMock()
    app.llm_enhancer.will_enhance = MagicMock(return_value=will_enhance)
    app.llm_enhancer.enhance_with_outcome = AsyncMock(
        side_effect=lambda t, **kw: EnhancementResult("SINGLE_CLEANED", "success")
    )
    app.llm_enhancer.enhance_chunk_with_outcome = AsyncMock(
        side_effect=lambda c, **kw: EnhancementResult(
            f"CLEANED::{c[:8]}", "success"
        )
    )
    app.text_injector = MagicMock()
    app.text_injector.process = MagicMock(side_effect=lambda t, **kw: t)
    app.text_injector.paste_chunk = MagicMock(
        return_value=DeliveryOutcome.focused()
    )
    app.text_injector.last_transcript = "FULL_PASTED_TEXT"
    app.review_state = MagicMock()
    # license_manager (trial clock) + transcript_store (history) are referenced
    # in the merged inject path (main's #184 trial start + #183 Transcripts); the
    # __new__ fixture must wire them. transcript_store=None => persistence skips.
    app.license_manager = MagicMock()
    app.transcript_store = None
    app._inject_suppressed = inject_suppressed
    # _dictation_app is set by __init__ in production (#159 app-identity capture);
    # the __new__ fixture must wire it so Power Mode routing resolves to None here.
    app._dictation_app = None
    # Manual-override state set by __init__ in production (#185 issue 14); the
    # __new__ fixture must wire it so _resolve_active_profile reads "no override".
    app._manual_profile_id = None
    app._override_anchor = None
    app._broadcast = MagicMock()
    return app


def test_long_enhanced_dictation_takes_chunked_path():
    app = _make_app(will_enhance=True)
    expected = _expected_chunks(LONG_TEXT)
    assert len(expected) >= 2  # sanity: the test text actually splits

    app._enhance_then_inject(LONG_TEXT, 1.5)

    # One LLM call + one paste per chunk; no whole-transcript single-call/inject.
    assert app.llm_enhancer.enhance_chunk_with_outcome.call_count == len(expected)
    assert app.text_injector.paste_chunk.call_count == len(expected)
    app.llm_enhancer.enhance_with_outcome.assert_not_called()
    app.text_injector.inject.assert_not_called()

    # first=True on the first paste, last=True on the last.
    first_call = app.text_injector.paste_chunk.call_args_list[0]
    last_call = app.text_injector.paste_chunk.call_args_list[-1]
    assert first_call.kwargs["first"] is True
    assert last_call.kwargs["last"] is True

    # Exactly one final result broadcast; session recorded.
    assert app._broadcast.call_count == 1
    assert app._broadcast.call_args.args[0]["type"] == "transcription.result"
    app.review_state.record_session.assert_called_once()


def test_chunked_path_applies_affixes_only_at_ends():
    app = _make_app(will_enhance=True)
    expected = _expected_chunks(LONG_TEXT)
    app._enhance_then_inject(LONG_TEXT, 1.0)

    proc_calls = app.text_injector.process.call_args_list
    assert len(proc_calls) == len(expected)
    # prepend only on the first chunk, append only on the last.
    assert proc_calls[0].kwargs == {"do_prepend": True, "do_append": len(expected) == 1}
    assert proc_calls[-1].kwargs == {"do_prepend": len(expected) == 1, "do_append": True}
    for mid in proc_calls[1:-1]:
        assert mid.kwargs == {"do_prepend": False, "do_append": False}


def test_chunked_cleanup_disabled_forces_single_call_on_long_text():
    # Advanced toggle off: a long enhanced dictation that would normally chunk
    # takes the single-call path instead (enhance() once, no per-chunk paste).
    app = _make_app(will_enhance=True, chunked_cleanup=False)
    expected = _expected_chunks(LONG_TEXT)
    assert len(expected) >= 2  # the text WOULD chunk if the toggle were on

    app._enhance_then_inject(LONG_TEXT, 1.0)

    app.llm_enhancer.enhance_chunk_with_outcome.assert_not_called()
    app.text_injector.paste_chunk.assert_not_called()
    app.llm_enhancer.enhance_with_outcome.assert_called_once()
    app.text_injector.inject.assert_called_once()
    assert app._broadcast.call_count == 1


def test_short_enhanced_dictation_takes_single_call_path():
    app = _make_app(will_enhance=True)
    app._enhance_then_inject(SHORT_TEXT, 0.5)

    app.llm_enhancer.enhance_with_outcome.assert_called_once()
    app.llm_enhancer.enhance_chunk_with_outcome.assert_not_called()
    app.text_injector.paste_chunk.assert_not_called()
    app.text_injector.inject.assert_called_once()
    assert app._broadcast.call_count == 1


def test_configured_word_target_replaces_hidden_activation_threshold():
    text = " ".join(
        f"Sentence {i} has exactly ten counted words for boundary testing."
        for i in range(9)
    )
    app = _make_app(will_enhance=True, words_per_chunk=60)
    assert 60 < len(text.split()) < 200

    app._enhance_then_inject(text, 0.5)

    app.llm_enhancer.enhance_chunk_with_outcome.assert_called()
    app.llm_enhancer.enhance_with_outcome.assert_not_called()


def test_first_chunk_toggle_uses_configured_target_when_disabled():
    limited = _make_app(
        will_enhance=True, words_per_chunk=100, limit_first_chunk=True
    )
    uniform = _make_app(
        will_enhance=True, words_per_chunk=100, limit_first_chunk=False
    )

    limited._enhance_then_inject(LONG_TEXT, 1.0)
    uniform._enhance_then_inject(LONG_TEXT, 1.0)

    limited_first = limited.llm_enhancer.enhance_chunk_with_outcome.call_args_list[0].args[0]
    uniform_first = uniform.llm_enhancer.enhance_chunk_with_outcome.call_args_list[0].args[0]
    assert len(limited_first.split()) < len(uniform_first.split())
    assert len(limited_first.split()) >= 60
    assert len(uniform_first.split()) >= 100


def test_long_but_not_warm_takes_single_call_path():
    # do_enhance True (local consent), but will_enhance False (e.g. not warm) ->
    # no chunking; enhance() runs (and would short-circuit to raw internally).
    app = _make_app(will_enhance=False)
    app._enhance_then_inject(LONG_TEXT, 1.0)

    app.llm_enhancer.enhance_chunk_with_outcome.assert_not_called()
    app.text_injector.paste_chunk.assert_not_called()
    app.llm_enhancer.enhance_with_outcome.assert_called_once()
    app.text_injector.inject.assert_called_once()


def test_enhancement_disabled_injects_raw_single_paste():
    app = _make_app(enabled=False)
    app._enhance_then_inject(LONG_TEXT, 1.0)

    app.llm_enhancer.enhance_with_outcome.assert_not_called()
    app.llm_enhancer.enhance_chunk_with_outcome.assert_not_called()
    app.text_injector.paste_chunk.assert_not_called()
    # Ordinary dictation never requests auto-submit.
    app.text_injector.inject.assert_called_once_with(
        LONG_TEXT, input_style="clipboard", submit=False
    )


def test_suppressed_long_dictation_does_not_chunk_or_paste():
    # Onboarding test mode: enhancement may run but nothing is pasted, and we
    # never take the chunked path.
    app = _make_app(will_enhance=True, inject_suppressed=True)
    app._enhance_then_inject(LONG_TEXT, 1.0)

    app.llm_enhancer.enhance_chunk_with_outcome.assert_not_called()
    app.text_injector.paste_chunk.assert_not_called()
    app.text_injector.inject.assert_not_called()
    app.llm_enhancer.enhance_with_outcome.assert_called_once()
    assert app._broadcast.call_count == 1


def test_empty_transcript_skips_everything():
    app = _make_app()
    app.transcript_store = MagicMock()
    app._enhance_then_inject("   ", 1.0)

    app.llm_enhancer.enhance_with_outcome.assert_not_called()
    app.llm_enhancer.enhance_chunk_with_outcome.assert_not_called()
    app.text_injector.inject.assert_not_called()
    app.text_injector.paste_chunk.assert_not_called()
    app._broadcast.assert_not_called()
    assert not app.transcript_store.mock_calls
    assert not app.license_manager.mock_calls
    assert not app.review_state.mock_calls


def test_no_default_and_no_rule_injects_raw(monkeypatch):
    # Issue 13: when no app/site rule matches (profile_id None) AND there is no
    # global default profile (default_profile_id=""), enhancement is skipped and
    # the raw transcript is injected via the single-call path (never chunked).
    app = _make_app(default_profile_id="", will_enhance=True)
    # _dictation_app is None -> resolve_profile_for_target returns None.
    app._enhance_then_inject(LONG_TEXT, 1.0)

    app.llm_enhancer.enhance_with_outcome.assert_not_called()
    app.llm_enhancer.enhance_chunk_with_outcome.assert_not_called()
    app.text_injector.paste_chunk.assert_not_called()
    # Ordinary dictation never requests auto-submit.
    app.text_injector.inject.assert_called_once_with(
        LONG_TEXT, input_style="clipboard", submit=False
    )
    assert app._broadcast.call_count == 1


def test_no_default_but_rule_matches_still_enhances():
    # A no-default config still enhances when a rule routes the focused app to a
    # profile: the raw-inject branch only fires when profile_id is None.
    app = _make_app(default_profile_id="", will_enhance=True)
    app.config.power_mode_map = {"slack.exe": "chat"}

    class _App:
        process_name = "slack.exe"
        domain = None

    app._dictation_app = _App()
    app._enhance_then_inject(LONG_TEXT, 1.0)

    # A profile matched, so enhancement runs (long -> chunked path).
    app.llm_enhancer.enhance_chunk_with_outcome.assert_called()
    app.text_injector.inject.assert_not_called()


def test_chunked_cleanup_aborts_batch_when_later_chunk_raises():
    app = _make_app(will_enhance=True)
    calls = 0

    async def _enhance_chunk(chunk, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("unexpected cleanup failure")
        return EnhancementResult(f"CLEANED::{chunk[:8]}", "success")

    app.llm_enhancer.enhance_chunk_with_outcome.side_effect = _enhance_chunk

    with pytest.raises(RuntimeError, match="unexpected cleanup failure"):
        app._enhance_then_inject(LONG_TEXT, 1.0)

    app.text_injector.paste_chunk.assert_called_once()
    app.text_injector.abort_chunked_paste.assert_called_once()


def test_failed_chunk_stops_batch_and_does_not_complete_or_persist():
    app = _make_app(will_enhance=True)
    receipt = DeliveryReceipt(
        request_id="chunk-failure",
        generation=1,
        route=DeliveryRoute.FOCUSED,
        status=ContractDeliveryStatus.FAILED,
        failure=DeliveryFailure.FOCUS_MISMATCH,
        units_attempted=2,
        units_completed=1,
        clipboard_restored=True,
    )
    app.text_injector.paste_chunk.side_effect = [
        DeliveryOutcome.focused(),
        DeliveryOutcome(
            DeliveryStatus.FAILED,
            DeliveryFailure.FOCUS_MISMATCH,
            receipt,
        ),
    ]
    app._persist_transcript = MagicMock()

    app._enhance_then_inject_chunked(LONG_TEXT, 1.0)

    assert app.text_injector.paste_chunk.call_count == 2
    assert app.llm_enhancer.enhance_chunk_with_outcome.call_count == 2
    app.text_injector.abort_chunked_paste.assert_called_once_with()
    app.text_injector.remember_original.assert_not_called()
    app.review_state.record_session.assert_not_called()
    app._persist_transcript.assert_not_called()
    app._broadcast.assert_not_called()
    app.logger.info.assert_any_call(
        "chunked cleanup stopped result=failed reason=%s",
        "focus-mismatch",
    )


def test_chunked_main_uses_public_outcome_not_private_batch_route():
    app = _make_app(will_enhance=True)
    app.text_injector.paste_chunk.return_value = DeliveryOutcome.focused()
    private_read = MagicMock(
        side_effect=AssertionError("main read private batch state")
    )
    type(app.text_injector)._batch_route = property(
        lambda _self: private_read()
    )
    app._persist_transcript = MagicMock()

    app._enhance_then_inject_chunked(LONG_TEXT, 1.0)

    broadcast_types = [
        call.args[0].get("type") for call in app._broadcast.call_args_list
    ]
    assert "transcription.result" in broadcast_types
    private_read.assert_not_called()


@pytest.mark.parametrize(
    ("outcomes", "expected_outcome", "has_enhanced"),
    [
        (["success", "success"], "success", True),
        (["success", "timeout"], "timeout", True),
        (["success", "timeout", "error"], "error", True),
        (["timeout", "timeout"], "timeout", False),
        (["timeout", "error"], "error", False),
        (["not_attempted", "not_attempted"], None, False),
    ],
    ids=[
        "success",
        "partial-timeout",
        "partial-error-precedence",
        "complete-timeout",
        "complete-error-precedence",
        "not-attempted",
    ],
)
def test_chunked_outcomes_drive_truthful_persistence(
    outcomes, expected_outcome, has_enhanced
):
    app = _make_app(will_enhance=True, words_per_chunk=4)
    app._persist_transcript = MagicMock()
    calls = 0

    async def _result(chunk, **_kwargs):
        nonlocal calls
        outcome = outcomes[min(calls, len(outcomes) - 1)]
        calls += 1
        text = f"CLEANED::{chunk}" if outcome == "success" else chunk
        return EnhancementResult(text, outcome)

    app.llm_enhancer.enhance_chunk_with_outcome.side_effect = _result
    app._enhance_then_inject_chunked(
        LONG_TEXT,
        1.0,
        profile_id="chat",
    )

    persisted = app._persist_transcript.call_args.kwargs
    assert (persisted["enhanced_text"] is not None) is has_enhanced
    provenance = persisted["enhancement_provenance"]
    if expected_outcome is None:
        assert provenance is None
    else:
        assert provenance.profile_id == "chat"
        assert provenance.profile_name == "Chat"
        assert provenance.outcome == expected_outcome


@pytest.mark.parametrize(
    ("outcomes", "expects_signature"),
    [
        (["success", "success"], True),
        (["success", "timeout"], True),
        (["timeout", "error"], False),
        (["not_attempted", "not_attempted"], False),
    ],
    ids=["success", "partial", "complete-fallback", "skip"],
)
def test_chunked_email_signature_requires_some_success(
    outcomes, expects_signature
):
    app = _make_app(will_enhance=True, words_per_chunk=4)
    app.config.default_profile_id = "email"
    app.config.profiles.append(
        {
            "id": "email",
            "name": "Email",
            "prompt": "p",
            "signature": "Best,\nAlex",
        }
    )
    calls = 0

    async def _result(chunk, **_kwargs):
        nonlocal calls
        outcome = outcomes[min(calls, len(outcomes) - 1)]
        calls += 1
        text = f"CLEANED::{chunk}" if outcome == "success" else chunk
        return EnhancementResult(text, outcome)

    app.llm_enhancer.enhance_chunk_with_outcome.side_effect = _result
    app._enhance_then_inject_chunked(
        LONG_TEXT,
        1.0,
        profile_id="email",
    )

    processed_last = app.text_injector.process.call_args_list[-1].args[0]
    assert processed_last.endswith("Best,\nAlex") is expects_signature
    assert sum(
        call.args[0].count("Best,\nAlex")
        for call in app.text_injector.process.call_args_list
    ) == (1 if expects_signature else 0)


@pytest.mark.parametrize("chunked", [False, True])
def test_balanced_mini_retrieval_reaches_app_delivery_with_raw_chunk_fallback(monkeypatch, chunked):
    from whisperstream.llm_enhancer import LLMEnhancer
    from whisperstream.vocabulary import VocabularyIndex, vocabulary_section
    from whisperstream.stream_mini_vocabulary_prompts import STREAM_MINI_VOCABULARY_CHAT_PROMPT

    app = _make_app(chunked_cleanup=chunked)
    app.config.enhancement_local_model = "r2-mixture-balanced-s23-20261022-v1"
    app.config.word_boosts = ["TanStack", "GraphQL", "Zygomatic"]
    metadata = {
        "id": app.config.enhancement_local_model,
        "prompt_key": "stream-mini-vocabulary", "serving_contract": "wrap-v2",
        "trained_modes": ["chat", "email"],
    }
    sup = MagicMock(is_ready=True, is_running=True, base_url="http://127.0.0.1:12345/v1")
    enhancer = LLMEnhancer(app.config, llama_server=sup, local_model_metadata=metadata)
    app.llm_enhancer = enhancer
    captured = []
    delivered = []

    async def post(url, **kwargs):
        payload = kwargs["json"]
        captured.append(payload)
        user = payload["messages"][1]["content"]
        source = user.split("</TRANSCRIPT>")[0].removeprefix("<TRANSCRIPT>")
        terms = VocabularyIndex(app.config.word_boosts).select(source, byte_budget=512)
        assert user == f"<TRANSCRIPT>{source}</TRANSCRIPT>" + vocabulary_section(terms)
        assert payload["messages"][0]["content"].startswith(STREAM_MINI_VOCABULARY_CHAT_PROMPT)
        fail = chunked and len(captured) == 2
        cleaned = source.replace("tan stack", "TanStack").replace("graph ql", "GraphQL")
        delivered.append(source if fail else cleaned)
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{
            "message": {"content": cleaned}, "finish_reason": "length" if fail else "stop",
        }]}
        return response

    monkeypatch.setattr(enhancer, "_get_client", lambda: MagicMock(post=post))
    raw = "Use tan stack for this project. " + LONG_TEXT + " Discuss graph ql at the next meeting."
    app._enhance_then_inject(raw, 1.0)
    expected = _expected_chunks(raw) if chunked else [raw]
    assert len(captured) == len(expected)
    assert "TanStack" in captured[0]["messages"][1]["content"]
    if chunked:
        assert len(expected) > 2
        assert [c.args[0] for c in app.text_injector.paste_chunk.call_args_list] == delivered
        assert delivered[1] == expected[1]
        app.text_injector.inject.assert_not_called()
    else:
        app.text_injector.inject.assert_called_once()
        assert app.text_injector.process.call_args.args[0] == delivered[0]
