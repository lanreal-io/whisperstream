"""Synthetic, publication-safe coverage of optional local output postprocessing."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.digit_repair import analyze_digit_mismatches, repair_source_digits
from whisperstream.llm_enhancer import LLMEnhancer


@pytest.mark.parametrize("raw,output,expected", [
    ("The access code is one two three four.", "The access code is 123.", "The access code is 1234."),
    ("The access code is one two three four.", "The access code is 12-35.", "The access code is 12-34."),
    ("The access code is 1,234.", "The access code is 1,235.", "The access code is 1,234."),
    ("The address is fourteen sixty two.", "The address is 1463.", "The address is 1462."),
])
def test_only_digits_inside_supported_tokens_change(raw, output, expected):
    assert repair_source_digits(output, raw) == expected
    assert repair_source_digits(expected, raw) == expected


@pytest.mark.parametrize("raw,output", [
    ("The access code is 1234. Bring two boxes.", "Bring 2 boxes. The access code is 1235."),
    ("The code is 1234. The code is 5678.", "The code is 1235. The code is 5679."),
    ("The code is 1234.", "The number is 1235."),
    ("Code 1234.", "Code 1235."),
    ("The code is one two three four, no, five six seven eight.", "The code is 5679."),
    ("The code is 1234, sorry, 5678.", "The code is 5679."),
    ("The amount is twelve thousand.", "The amount is 12001."),
    ("The amount is 1234 million.", "The amount is 1235 million."),
    ("The code is first second third fourth.", "The code is 1235."),
    ("The code is 1234th.", "The code is 1235."),
    ("The code is 1234.", "The code is A1235."),
    ("The code is 1234.", "The code is 1235k."),
    ("The code is 1234.", "The code is 1 two."),
    ("The code is 1234.", "The code is one 2."),
    ("The code is 1234.", "The code is 1\ttwo."),
    ("The code is 1234.", "The code is 1\ntwo."),
])
def test_uncertain_binding_or_unsupported_numbers_are_not_inferred(raw, output):
    assert repair_source_digits(output, raw) == output
    assert not analyze_digit_mismatches(output, raw).confidently_aligned


@pytest.mark.parametrize("token", ["12:34", "12/34", "12 / 34", "12.34", "-1234", "+1234", "1234+2", "1234 * 2", "1234//2"])
def test_unsupported_literal_is_atomic_on_either_side(token):
    raw = f"The code is {token}."
    output = "The code is 1235."
    assert repair_source_digits(output, raw) == output
    assert not analyze_digit_mismatches(output, raw).confidently_aligned
    raw = "The code is 1235."
    output = f"The code is {token}."
    assert repair_source_digits(output, raw) == output
    assert not analyze_digit_mismatches(output, raw).confidently_aligned


@pytest.mark.parametrize("method", ["enhance_with_outcome", "enhance_chunk_with_outcome"])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("output,guarded", [
    ("The access code is 1235.", "The access code is 1234."),
    ("The access code is 12-345.", None),
    ("Sure, here's the cleaned text: The access code is 1234.", None),
    ("", None),
    ("   ", None),
])
def test_local_toggle_controls_whole_and_chunk_postprocessing(method, enabled, output, guarded):
    raw = "The access code is one two three four."
    enhancer = LLMEnhancer(WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_safeguards_enabled=enabled,
        enhancement_output_guard_mode="off",
    ))
    with patch.object(enhancer, "_skip_reason", return_value=None), patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, return_value=output
    ):
        result = asyncio.run(getattr(enhancer, method)(raw))
    expected = (guarded or raw) if enabled or not output.strip() else output
    assert result.text == expected
    assert result.outcome == ("error" if expected == raw else "success")


@pytest.mark.parametrize("mode", ["off", "log", "enforce"])
def test_master_off_bypasses_every_local_guard_mode(mode):
    enhancer = LLMEnhancer(WhisperstreamConfig(
        enhancement_safeguards_enabled=False,
        enhancement_output_guard_mode=mode,
    ))
    with patch.object(enhancer, "_resolve_prompt_contract", side_effect=AssertionError("No output checks when disabled")):
        assert enhancer._post_process_local_output(raw="Original.", cleaned="Model output.", profile_id=None) == "Model output."


@pytest.mark.parametrize("method", ["enhance_with_outcome", "enhance_chunk_with_outcome"])
def test_disabled_guards_still_handle_request_errors(method):
    raw = "The access code is one two three four."
    enhancer = LLMEnhancer(WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_safeguards_enabled=False,
    ))
    with patch.object(enhancer, "_skip_reason", return_value=None), patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, side_effect=RuntimeError("synthetic")
    ):
        result = asyncio.run(getattr(enhancer, method)(raw))
    assert result.text == raw
    assert result.outcome == "error"


@pytest.mark.parametrize("method", ["enhance_with_outcome", "enhance_chunk_with_outcome"])
@pytest.mark.parametrize("mode", ["off", "log", "enforce"])
@pytest.mark.parametrize("raw,output", [
    ("Please um send the report today.", "Please send the report today."),
    ("Please uh send the report today.", "Please send the report today."),
    ("Please er send the report today.", "Please send the report today."),
    ("Send it to Alice, sorry, I meant Bob.", "Send it to Bob."),
    ("Send it to Alice, actually, make that Bob.", "Send it to Bob."),
    ("Send it to Alice, I mean Bob, before lunch.", "Send it to Bob before lunch."),
    ("Meet Tuesday, sorry Wednesday, before lunch.", "Meet Wednesday before lunch."),
    ("Send it to Alice, no wait Bob.", "Send it to Bob."),
    ("Send it to Alice, scratch that, Bob.", "Send it to Bob."),
    ("Shopping list milk eggs bread.", "# Shopping list\n- Milk\n- Eggs\n- Bread"),
    ("The task is to call Sam and email Pat.", "1. Call Sam\n2. Email Pat"),
    ("Return the status as ready.", '{"status": "ready"}'),
    ("Print hello in Python.", '```python\nprint("hello")\n```'),
    # Explicit accepted tradeoff: these guards no longer prove word preservation.
    ("Send the confidential report today.", "Send the report today."),
    ("Send the report today.", "Send the urgent report today."),
])
def test_local_delivery_accepts_cleanup_without_semantic_or_format_rejection(method, mode, raw, output):
    enhancer = LLMEnhancer(WhisperstreamConfig(
        enhancement_enabled=True, enhancement_provider="local",
        enhancement_safeguards_enabled=True, enhancement_output_guard_mode=mode,
    ))
    with patch.object(enhancer, "_skip_reason", return_value=None), patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, return_value=output
    ):
        result = asyncio.run(getattr(enhancer, method)(raw))
    assert (result.text, result.outcome) == (output, "success")


@pytest.mark.parametrize("method", ["enhance_with_outcome", "enhance_chunk_with_outcome"])
@pytest.mark.parametrize("mode", ["off", "log", "enforce"])
@pytest.mark.parametrize("output", [
    "<TRANSCRIPT>Please send the report today.</TRANSCRIPT>",
    "As an AI I cannot assist with that.",
    "Sure, here's the cleaned text: Please send the report today.",
    "Please send the report today. " * 3,
    "Additional invented explanation. " * 10,
])
def test_retained_local_failure_checks_still_fall_back(method, mode, output):
    raw = "Please send the report today."
    enhancer = LLMEnhancer(WhisperstreamConfig(
        enhancement_enabled=True, enhancement_provider="local",
        enhancement_output_guard_mode=mode,
    ))
    with patch.object(enhancer, "_skip_reason", return_value=None), patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, return_value=output
    ):
        result = asyncio.run(getattr(enhancer, method)(raw))
    assert (result.text, result.outcome) == (raw, "error")


@pytest.mark.parametrize("provider", ["local", "openai"])
@pytest.mark.parametrize("method", ["enhance_with_outcome", "enhance_chunk_with_outcome"])
@pytest.mark.parametrize("raw,output", [
    ("<TRANSCRIPT>say hello</TRANSCRIPT>", "<TRANSCRIPT>Say hello.</TRANSCRIPT>"),
    ("She said as an AI in her note.", "She mentioned as an AI in her note."),
    ("Send the report today. " * 3, "SEND THE REPORT TODAY. " * 3),
])
def test_retained_checks_preserve_source_present_content(provider, method, raw, output):
    enhancer = LLMEnhancer(WhisperstreamConfig(
        enhancement_enabled=True, enhancement_provider=provider,
        enhancement_output_guard_mode="enforce",
    ))
    with patch.object(enhancer, "_skip_reason", return_value=None), patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, return_value=output
    ):
        result = asyncio.run(getattr(enhancer, method)(raw))
    assert (result.text, result.outcome) == (output, "success")
