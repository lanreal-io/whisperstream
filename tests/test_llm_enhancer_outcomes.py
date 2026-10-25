"""Typed outcome coverage for the production LLM enhancement paths."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from whisperstream.config_manager import WhisperstreamConfig


RAW = "the original transcript has enough words to enhance safely"
CLEANED = "The original transcript has enough words to enhance safely."


def _cloud_enhancer(**overrides):
    from whisperstream.llm_enhancer import LLMEnhancer

    values = {
        "enhancement_enabled": True,
        "enhancement_provider": "openai",
        "enhancement_api_key": "sk-test",
        "enhancement_safeguards_enabled": True,
    }
    values.update(overrides)
    return LLMEnhancer(WhisperstreamConfig(**values))


def _local_enhancer(**overrides):
    from whisperstream.llm_enhancer import LLMEnhancer

    values = {
        "enhancement_enabled": True,
        "enhancement_provider": "local",
        "enhancement_local_endpoint": "http://127.0.0.1:1",
        "enhancement_local_model": "gemma4-e2b-instruct",
        "enhancement_output_guard_mode": "off",
    }
    values.update(overrides)
    return LLMEnhancer(WhisperstreamConfig(**values))


@pytest.mark.parametrize(
    ("method_name", "legacy_method_name"),
    (
        ("enhance_with_outcome", "enhance"),
        ("enhance_chunk_with_outcome", "enhance_chunk"),
    ),
)
@pytest.mark.parametrize("provider_output", (CLEANED, RAW))
def test_typed_success_includes_accepted_unchanged_output(
    method_name, legacy_method_name, provider_output
):
    enhancer = _cloud_enhancer()

    with patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, return_value=provider_output
    ):
        result = asyncio.run(getattr(enhancer, method_name)(RAW))

    assert result.text == provider_output
    assert result.outcome == "success"

    with patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, return_value=provider_output
    ):
        legacy_result = asyncio.run(getattr(enhancer, legacy_method_name)(RAW))

    assert legacy_result == provider_output
    assert isinstance(legacy_result, str)


@pytest.mark.parametrize(
    "method_name", ("enhance_with_outcome", "enhance_chunk_with_outcome")
)
def test_typed_timeout_is_bounded_and_contains_no_raw_error(method_name):
    enhancer = _cloud_enhancer()
    raw_detail = "provider request abc-123 timed out"

    with patch.object(
        enhancer,
        "_call_provider",
        new_callable=AsyncMock,
        side_effect=httpx.ReadTimeout(raw_detail),
    ):
        result = asyncio.run(getattr(enhancer, method_name)(RAW))

    assert result.text == RAW
    assert result.outcome == "timeout"
    assert {field.name for field in fields(result)} == {"text", "outcome"}
    assert raw_detail not in repr(result)


@pytest.mark.parametrize(
    "method_name", ("enhance_with_outcome", "enhance_chunk_with_outcome")
)
def test_typed_unexpected_error_is_bounded_and_contains_no_raw_error(method_name):
    enhancer = _cloud_enhancer()
    raw_detail = "provider response included secret detail"

    with patch.object(
        enhancer,
        "_call_provider",
        new_callable=AsyncMock,
        side_effect=RuntimeError(raw_detail),
    ):
        result = asyncio.run(getattr(enhancer, method_name)(RAW))

    assert result.text == RAW
    assert result.outcome == "error"
    assert raw_detail not in repr(result)


@pytest.mark.parametrize(
    "method_name", ("enhance_with_outcome", "enhance_chunk_with_outcome")
)
def test_typed_empty_output_is_error(method_name):
    enhancer = _cloud_enhancer()

    with patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, return_value="  \n  "
    ):
        result = asyncio.run(getattr(enhancer, method_name)(RAW))

    assert result.text == RAW
    assert result.outcome == "error"


@pytest.mark.parametrize(
    "method_name", ("enhance_with_outcome", "enhance_chunk_with_outcome")
)
def test_typed_cloud_guard_fallback_is_error(method_name):
    enhancer = _cloud_enhancer()
    refusal = "As an AI language model, I cannot assist with that request."

    with patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, return_value=refusal
    ):
        result = asyncio.run(getattr(enhancer, method_name)(RAW))

    assert result.text == RAW
    assert result.outcome == "error"


@pytest.mark.parametrize(
    "method_name", ("enhance_with_outcome", "enhance_chunk_with_outcome")
)
def test_typed_local_guard_fallback_is_error(method_name):
    enhancer = _local_enhancer()
    corrupted = RAW * 20

    with patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, return_value=corrupted
    ):
        result = asyncio.run(getattr(enhancer, method_name)(RAW))

    assert result.text == RAW
    assert result.outcome == "error"


@pytest.mark.parametrize(
    "method_name", ("enhance_with_outcome", "enhance_chunk_with_outcome")
)
def test_typed_local_accepted_unchanged_output_is_success(method_name):
    enhancer = _local_enhancer()

    with patch.object(
        enhancer, "_call_provider", new_callable=AsyncMock, return_value=RAW
    ):
        result = asyncio.run(getattr(enhancer, method_name)(RAW))

    assert result.text == RAW
    assert result.outcome == "success"


@pytest.mark.parametrize(
    "method_name", ("enhance_with_outcome", "enhance_chunk_with_outcome")
)
def test_typed_unsupported_mode_is_not_attempted(method_name):
    from whisperstream.llm_enhancer import LLMEnhancer

    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_local_endpoint="http://127.0.0.1:1",
        enhancement_local_model="test-candidate",
        default_profile_id="code",
        profiles=[{"id": "code", "name": "Code", "mode": "code", "prompt": "Clean."}],
    )
    enhancer = LLMEnhancer(
        config,
        local_model_metadata={
            "id": "test-candidate",
            "prompt_key": "stream",
            "serving_contract": "raw-v1",
            "trained_modes": ["chat"],
        },
    )

    with patch.object(enhancer, "_call_provider", new_callable=AsyncMock) as call:
        result = asyncio.run(getattr(enhancer, method_name)(RAW, profile_id="code"))

    assert result.text == RAW
    assert result.outcome == "not_attempted"
    call.assert_not_awaited()


@pytest.mark.parametrize(
    ("config_overrides", "text", "expected"),
    (
        ({"enhancement_enabled": False}, RAW, "not_attempted"),
        ({}, "only three words", "not_attempted"),
        ({"enhancement_api_key": ""}, RAW, "error"),
    ),
    ids=("disabled", "short-input", "missing-key"),
)
def test_typed_whole_call_maps_preflight_outcomes(config_overrides, text, expected):
    enhancer = _cloud_enhancer(**config_overrides)

    with patch.object(enhancer, "_call_provider", new_callable=AsyncMock) as call:
        result = asyncio.run(enhancer.enhance_with_outcome(text))

    assert result.text == text
    assert result.outcome == expected
    call.assert_not_awaited()


def test_typed_whole_call_maps_unavailable_local_runtime_to_error():
    from whisperstream.llm_enhancer import LLMEnhancer

    supervisor = MagicMock(is_ready=False)
    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_local_endpoint="",
        enhancement_local_model="gemma4-e2b-instruct",
    )
    enhancer = LLMEnhancer(config, llama_server=supervisor)

    with patch.object(enhancer, "_call_provider", new_callable=AsyncMock) as call:
        result = asyncio.run(enhancer.enhance_with_outcome(RAW))

    assert result.text == RAW
    assert result.outcome == "error"
    call.assert_not_awaited()
