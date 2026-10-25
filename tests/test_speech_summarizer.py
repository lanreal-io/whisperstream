"""Tests for whisperstream.speech.summarizer (agent-speech Phase 1, Task 1.5).

TDD: written before the implementation. Covers the three concerns
(prepare_verbatim cleanup/truncation, summarize's json_schema/json_object
retry + head-tail bounding, extract_deterministic keyword extraction) and the
speak_text_for fallback-chain orchestrator's never-silent contract. No live
model calls: the HTTP layer is mocked via monkeypatch on
``httpx.AsyncClient.post``.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub_supervisor(base_url: str = "http://127.0.0.1:12345/v1") -> Any:
    """A minimal stand-in for LlamaServerSupervisor (only .base_url is used)."""
    return SimpleNamespace(base_url=base_url)


def _ok_response(payload: dict) -> httpx.Response:
    """Build a real httpx.Response carrying a chat/completions-shaped body."""
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(payload)}}]},
        request=httpx.Request("POST", "http://127.0.0.1:12345/v1/chat/completions"),
    )


def _speech_payload(
    speech: str = "The tests pass now.",
    status: str = "success",
    requires_user: bool = False,
) -> dict:
    return {"status": status, "requires_user": requires_user, "speech": speech}


# ---------------------------------------------------------------------------
# prepare_verbatim: markdown cleanup
# ---------------------------------------------------------------------------

def test_prepare_verbatim_code_fence_becomes_literal_sentence():
    from whisperstream.speech.summarizer import prepare_verbatim

    text = "Here is a fix:\n\n```python\ndef foo():\n    pass\n```\n\nDone."
    result = prepare_verbatim(text)
    assert "```" not in result
    assert "def foo" not in result
    assert "Code block." in result
    assert "Here is a fix:" in result
    assert "Done." in result


def test_prepare_verbatim_inline_code_keeps_content_minus_backticks():
    from whisperstream.speech.summarizer import prepare_verbatim

    result = prepare_verbatim("Run `pytest -v` to test.")
    assert "`" not in result
    assert "pytest -v" in result


def test_prepare_verbatim_link_becomes_label():
    from whisperstream.speech.summarizer import prepare_verbatim

    result = prepare_verbatim("See [the docs](https://example.com/docs) for more.")
    assert "https://example.com/docs" not in result
    assert "the docs" in result
    assert "[" not in result and "]" not in result


def test_prepare_verbatim_heading_and_emphasis_stripped():
    from whisperstream.speech.summarizer import prepare_verbatim

    result = prepare_verbatim("# Summary\n\nThis is **bold** and *italic* text.")
    assert "#" not in result
    assert "*" not in result
    assert "bold" in result
    assert "italic" in result
    assert "Summary" in result


def test_prepare_verbatim_list_markers_stripped():
    from whisperstream.speech.summarizer import prepare_verbatim

    result = prepare_verbatim("- item one\n- item two\n- item three")
    assert "-" not in result
    assert "item one" in result
    assert "item two" in result
    assert "item three" in result


def test_prepare_verbatim_truncates_at_word_boundary_never_mid_word():
    from whisperstream.speech.summarizer import prepare_verbatim, _VERBATIM_CHAR_CAP

    text = ("A" * 590) + " " + ("B" * 590)
    result = prepare_verbatim(text)
    assert len(result) <= _VERBATIM_CHAR_CAP
    # Never cut mid-word: the result must end on a whole token from the source.
    assert result == result.rstrip()
    last_token = result.split(" ")[-1]
    assert last_token in text.split(" ")


def test_prepare_verbatim_appends_nothing_on_truncation():
    from whisperstream.speech.summarizer import prepare_verbatim

    text = "word " * 300  # far over the ~600 char cap
    result = prepare_verbatim(text)
    assert not result.endswith("...")
    assert not result.endswith("…")


def test_prepare_verbatim_short_text_unchanged_besides_cleanup():
    from whisperstream.speech.summarizer import prepare_verbatim

    result = prepare_verbatim("All tests passed.")
    assert result == "All tests passed."


# ---------------------------------------------------------------------------
# extract_deterministic: keyword-sentence fallback
# ---------------------------------------------------------------------------

def test_extract_deterministic_selects_keyword_sentences():
    from whisperstream.speech.summarizer import extract_deterministic

    text = "Implemented the feature. Tests failed on CI. Everything else is fine."
    result = extract_deterministic(text)
    assert "Tests failed on CI." in result
    assert "Implemented the feature." not in result
    assert "Everything else is fine." not in result


def test_extract_deterministic_empty_when_no_keywords():
    from whisperstream.speech.summarizer import extract_deterministic

    text = "All good here. Nothing more to say."
    assert extract_deterministic(text) == ""


def test_extract_deterministic_keeps_question_sentences():
    from whisperstream.speech.summarizer import extract_deterministic

    text = "Should I proceed with the deploy? Let me know."
    result = extract_deterministic(text)
    assert "Should I proceed with the deploy?" in result


def test_extract_deterministic_capped_to_verbatim_cap():
    from whisperstream.speech.summarizer import extract_deterministic, _VERBATIM_CHAR_CAP

    text = " ".join([f"This test{n} failed badly today." for n in range(200)])
    result = extract_deterministic(text)
    assert len(result) <= _VERBATIM_CHAR_CAP


# ---------------------------------------------------------------------------
# summarize: json_schema call, 400->json_object retry, timeout, malformed JSON
# ---------------------------------------------------------------------------

def test_summarize_valid_json_schema_response_parsed(monkeypatch):
    from whisperstream.speech import summarizer

    calls = []

    async def fake_post(self, url, *, json=None, **kwargs):
        calls.append(json)
        return _ok_response(_speech_payload("Done, all tests passed."))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(
        summarizer.summarize("raw agent reply", _stub_supervisor(), "gemma-4-e2b")
    )
    assert result == "Done, all tests passed."
    assert len(calls) == 1
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[0]["response_format"]["json_schema"]["name"] == "speech"


def test_summarize_http_400_retries_once_with_json_object(monkeypatch):
    from whisperstream.speech import summarizer

    calls = []

    async def fake_post(self, url, *, json=None, **kwargs):
        calls.append(json)
        if len(calls) == 1:
            return httpx.Response(
                400,
                json={"error": "schema not supported"},
                request=httpx.Request("POST", url),
            )
        return _ok_response(_speech_payload("Blocked, needs your input."))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(
        summarizer.summarize("raw agent reply", _stub_supervisor(), "gemma-4-e2b")
    )
    assert result == "Blocked, needs your input."
    assert len(calls) == 2
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[1]["response_format"]["type"] == "json_object"


def test_summarize_persistent_400_returns_none_after_one_retry(monkeypatch):
    from whisperstream.speech import summarizer

    calls = []

    async def fake_post(self, url, *, json=None, **kwargs):
        calls.append(json)
        return httpx.Response(400, json={"error": "nope"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(
        summarizer.summarize("raw agent reply", _stub_supervisor(), "gemma-4-e2b")
    )
    assert result is None
    assert len(calls) == 2  # exactly one retry, never more


def test_summarize_http_500_returns_none_without_retry(monkeypatch):
    from whisperstream.speech import summarizer

    calls = []

    async def fake_post(self, url, *, json=None, **kwargs):
        calls.append(json)
        return httpx.Response(500, text="boom", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(
        summarizer.summarize("raw agent reply", _stub_supervisor(), "gemma-4-e2b")
    )
    assert result is None
    assert len(calls) == 1  # no retry for non-400 failures


def test_summarize_timeout_returns_none(monkeypatch):
    from whisperstream.speech import summarizer

    async def fake_post(self, url, *, json=None, **kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(
        summarizer.summarize("raw agent reply", _stub_supervisor(), "gemma-4-e2b", timeout_s=0.1)
    )
    assert result is None


def test_summarize_malformed_json_content_returns_none(monkeypatch):
    from whisperstream.speech import summarizer

    async def fake_post(self, url, *, json=None, **kwargs):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not valid json {{"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(
        summarizer.summarize("raw agent reply", _stub_supervisor(), "gemma-4-e2b")
    )
    assert result is None


def test_summarize_empty_speech_field_returns_none(monkeypatch):
    from whisperstream.speech import summarizer

    async def fake_post(self, url, *, json=None, **kwargs):
        return _ok_response(_speech_payload(""))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(
        summarizer.summarize("raw agent reply", _stub_supervisor(), "gemma-4-e2b")
    )
    assert result is None


def test_summarize_supervisor_missing_attribute_returns_none(monkeypatch):
    """A caller-side None/broken supervisor must degrade to None, never raise."""
    from whisperstream.speech import summarizer

    result = asyncio.run(
        summarizer.summarize("raw agent reply", None, "gemma-4-e2b")
    )
    assert result is None


def test_summarize_head_tail_bounding_keeps_both_ends_drops_middle(monkeypatch):
    from whisperstream.speech import summarizer

    filler = "x" * 200
    head_marker = "HEAD_MARKER_TOKEN"
    middle_marker = "MIDDLE_MARKER_TOKEN"
    tail_marker = "TAIL_MARKER_TOKEN"
    # Build a long transcript: head marker at the very start, middle marker
    # buried in the center, tail marker at the very end. Comfortably bigger
    # than the ~12000 char budget.
    text = (
        head_marker + " " + (filler + " ") * 40
        + middle_marker + " " + (filler + " ") * 40
        + tail_marker
    )
    assert len(text) > summarizer._SUMMARY_INPUT_BUDGET_CHARS

    captured = {}

    async def fake_post(self, url, *, json=None, **kwargs):
        captured["payload"] = json
        return _ok_response(_speech_payload("summary"))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    asyncio.run(summarizer.summarize(text, _stub_supervisor(), "gemma-4-e2b"))

    user_content = captured["payload"]["messages"][-1]["content"]
    assert head_marker in user_content
    assert tail_marker in user_content
    assert middle_marker not in user_content
    # Bounded content must not blow past the fixed context budget.
    assert len(user_content) <= summarizer._SUMMARY_INPUT_BUDGET_CHARS + 50


def test_summarize_url_suffix_handles_base_url_with_v1(monkeypatch):
    from whisperstream.speech import summarizer

    seen_urls = []

    async def fake_post(self, url, *, json=None, **kwargs):
        seen_urls.append(url)
        return _ok_response(_speech_payload("ok"))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    asyncio.run(
        summarizer.summarize(
            "reply", _stub_supervisor(base_url="http://127.0.0.1:9999/v1"), "m"
        )
    )
    assert seen_urls == ["http://127.0.0.1:9999/v1/chat/completions"]


# ---------------------------------------------------------------------------
# speak_text_for: fallback chain orchestrator + never-silent contract
# ---------------------------------------------------------------------------

def test_speak_text_for_verbatim_mode_uses_prepare_verbatim():
    from whisperstream.speech import summarizer

    text = "The build finished. All green."
    result = asyncio.run(
        summarizer.speak_text_for(text, "verbatim", _stub_supervisor(), "m")
    )
    assert result == summarizer.prepare_verbatim(text)


def test_speak_text_for_summary_uses_summary_when_available(monkeypatch):
    from whisperstream.speech import summarizer

    async def fake_summarize(text, supervisor, model_name, timeout_s=8.0):
        return "A short spoken summary."

    monkeypatch.setattr(summarizer, "summarize", fake_summarize)

    result = asyncio.run(
        summarizer.speak_text_for(
            "irrelevant long reply", "summary", _stub_supervisor(), "m"
        )
    )
    assert result == "A short spoken summary."


def test_speak_text_for_falls_back_to_deterministic_extraction(monkeypatch):
    from whisperstream.speech import summarizer

    async def fake_summarize(text, supervisor, model_name, timeout_s=8.0):
        return None

    monkeypatch.setattr(summarizer, "summarize", fake_summarize)

    text = "Refactored the module. Tests failed after the change. Cleaned up imports."
    result = asyncio.run(
        summarizer.speak_text_for(text, "summary", _stub_supervisor(), "m")
    )
    assert result == summarizer.extract_deterministic(text)
    assert "Tests failed after the change." in result


def test_speak_text_for_falls_back_to_verbatim_when_extraction_empty(monkeypatch):
    from whisperstream.speech import summarizer

    async def fake_summarize(text, supervisor, model_name, timeout_s=8.0):
        return None

    monkeypatch.setattr(summarizer, "summarize", fake_summarize)

    text = "Everything looks good and clean today."  # no fallback keywords
    assert summarizer.extract_deterministic(text) == ""

    result = asyncio.run(
        summarizer.speak_text_for(text, "summary", _stub_supervisor(), "m")
    )
    assert result == summarizer.prepare_verbatim(text)
    assert result != ""


def test_speak_text_for_never_empty_for_non_empty_input(monkeypatch):
    from whisperstream.speech import summarizer

    async def fake_summarize(text, supervisor, model_name, timeout_s=8.0):
        return None

    monkeypatch.setattr(summarizer, "summarize", fake_summarize)

    samples = [
        "Done.",
        "Fixed the bug in the parser and reran the suite; all green now.",
        "```\ncode only\n```",
        "Should I continue with the deploy?",
    ]
    for mode in ("verbatim", "summary"):
        for text in samples:
            result = asyncio.run(
                summarizer.speak_text_for(text, mode, _stub_supervisor(), "m")
            )
            assert result != "", f"empty result for mode={mode} text={text!r}"


def test_speak_text_for_summary_never_calls_llm_for_verbatim_mode(monkeypatch):
    """Sanity: verbatim mode must not touch the network at all."""
    from whisperstream.speech import summarizer

    def boom(*args, **kwargs):
        raise AssertionError("summarize() must not be called in verbatim mode")

    monkeypatch.setattr(summarizer, "summarize", boom)

    result = asyncio.run(
        summarizer.speak_text_for("Some reply text.", "verbatim", _stub_supervisor(), "m")
    )
    assert result == "Some reply text."
