"""Synthetic model policy contracts, independent of catalogs and network."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.enhancement_policy import (
    ModelBehaviorFamily, OutputPolicy, model_capabilities, resolve_model_policy,
)
from whisperstream.enhancement_prompts import (
    GEMMA_LOCAL_CHAT_PROMPT, LOCAL_CHAT_PROMPT_V2, LOCAL_STUDENT_EMAIL_PROMPT,
)
from whisperstream.stream_mini_prompts import STREAM_MINI_EMAIL_PROMPT
from whisperstream.llm_enhancer import LLMEnhancer


def test_customizable_policy_preserves_custom_profile_and_local_builtin_split():
    policy = resolve_model_policy("local", {"prompt_key": "gemma"}, model_id="any-id")
    profile = {"mode": "code", "prompt": "Synthetic custom instructions"}
    prepared = policy.prepare("custom", profile)
    assert prepared.system_prompt == profile["prompt"]
    assert prepared.contract.name == "custom"
    assert prepared.policy is policy
    assert policy.capabilities.editable_prompt
    assert policy.prepare("chat", profile).system_prompt == GEMMA_LOCAL_CHAT_PROMPT
    assert profile == {"mode": "code", "prompt": "Synthetic custom instructions"}
    assert "Synthetic custom instructions" not in repr(prepared)


@pytest.mark.parametrize("serving,mode,prompt,user", [
    ("wrap-v2", "chat", LOCAL_CHAT_PROMPT_V2, "<TRANSCRIPT>synthetic words</TRANSCRIPT>"),
    ("raw-v1", "email", LOCAL_STUDENT_EMAIL_PROMPT, "synthetic words"),
    ("chatml", "email", LOCAL_STUDENT_EMAIL_PROMPT, "synthetic words"),
])
def test_trained_family_binds_prompt_and_envelope(serving, mode, prompt, user):
    policy = resolve_model_policy("local", {
        "prompt_key": "stream", "serving_contract": serving,
        "trained_modes": ["chat", "email"],
    }, model_id="arbitrary-candidate")
    prepared = policy.prepare("custom", {"mode": mode, "prompt": "Ignored editor text"})
    assert prepared.system_prompt == prompt
    assert policy.user_content("synthetic words", profile_mode=mode) == user
    assert policy.capabilities.family is ModelBehaviorFamily.TRAINED_MODE
    assert not policy.prepare("code", None).mode_supported
    assert policy.output_policy is OutputPolicy.LOCAL_ORDERED


def test_fixed_policy_groups_prompt_format_sampling_and_supported_modes():
    policy = resolve_model_policy("local", {
        "prompt_key": "stream-mini", "serving_contract": "wrap-v2", "trained_modes": ["chat", "email"],
    })
    prepared = policy.prepare("email", {"prompt": "Ignored"})
    assert prepared.system_prompt == STREAM_MINI_EMAIL_PROMPT
    assert policy.user_content("synthetic words", profile_mode="email") == (
        "<TRANSCRIPT>synthetic words</TRANSCRIPT>"
    )
    assert policy.generation_options() == {
        "temperature": 0.0, "top_k": 1, "top_p": 1.0, "repeat_penalty": 1.0,
        "max_tokens": 512, "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert not policy.prepare("code", None).mode_supported


def test_capabilities_are_safe_detached_metadata_not_model_name_heuristics():
    metadata = {"id": "stream-1-mini", "prompt_key": "gemma", "trained_modes": ["chat"],
                "_artifact_path": "private path", "endpoint": "private endpoint", "api_key": "secret"}
    capabilities = model_capabilities("local", metadata)
    metadata["trained_modes"].append("code")
    public = capabilities.public_dict()
    assert public == {"family": "customizable", "editable_prompt": True,
                      "supported_modes": ["chat"]}
    public["supported_modes"].append("email")
    assert capabilities.supported_modes == ("chat",)
    assert model_capabilities("custom", metadata).supported_modes is None
    assert model_capabilities("custom", metadata).editable_prompt


def test_unknown_local_metadata_keeps_wrapped_fallback_and_cloud_defaults():
    local = resolve_model_policy("local", {"id": "custom-new-id", "serving_contract": "unknown"})
    cloud = resolve_model_policy("custom", {"prompt_key": "stream-mini", "trained_modes": []})
    assert local.user_content("synthetic", profile_mode="chat") == "<TRANSCRIPT>synthetic</TRANSCRIPT>"
    assert cloud.generation_options() == {}
    assert cloud.output_policy is OutputPolicy.REMOTE_TOGGLE
    assert cloud.prepare("new", {"prompt": "Exact custom prompt"}).system_prompt == "Exact custom prompt"


@pytest.mark.parametrize("operation", ["whole", "chunk"])
def test_prepared_policy_does_not_freeze_feature_admission(tmp_path, monkeypatch, operation):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    cfg = WhisperstreamConfig(enhancement_enabled=True, enhancement_provider="local")
    enhancer = LLMEnhancer(cfg, llama_server=MagicMock(is_ready=True, base_url="http://127.0.0.1:12345"))
    original = enhancer._resolve_prompt_contract
    def revoke(profile_id=None):
        result = original(profile_id)
        cfg.enhancement_enabled = False
        return result
    monkeypatch.setattr(enhancer, "_resolve_prompt_contract", revoke)
    captures = []
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: captures.append(request))) as client:
            enhancer._client = client
            raw = "these are synthetic input words"
            method = enhancer.enhance if operation == "whole" else enhancer.enhance_chunk
            assert await method(raw) == raw
    asyncio.run(run())
    assert captures == []


@pytest.mark.parametrize("change", ["revoke", "model", "endpoint", "credential"])
def test_prepared_policy_rechecks_current_recipient_consent_and_key(tmp_path, monkeypatch, change):
    from whisperstream import byok_consent
    from whisperstream.enhancement_security import consent_target

    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    cfg = WhisperstreamConfig(
        enhancement_enabled=True, enhancement_provider="custom", enhancement_api_key="synthetic",
        enhancement_custom_base_url="https://synthetic.invalid/v1", enhancement_custom_model="first",
    )
    target = consent_target(cfg)
    byok_consent.record_consent("custom", "Synthetic disclosure", target=target)
    enhancer = LLMEnhancer(cfg)
    prepare = enhancer._resolve_prompt_contract
    def change_after_prepare(profile_id=None):
        result = prepare(profile_id)
        if change == "revoke":
            byok_consent.remove_provider("custom")
        elif change == "model":
            cfg.enhancement_custom_model = "second"
        elif change == "endpoint":
            cfg.enhancement_custom_base_url = "https://different.invalid/v1"
        else:
            cfg.enhancement_api_key = ""
        return result
    monkeypatch.setattr(enhancer, "_resolve_prompt_contract", change_after_prepare)
    captures = []
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: captures.append(request))) as client:
            enhancer._client = client
            raw = "these are synthetic input words"
            assert await enhancer.enhance(raw) == raw
    asyncio.run(run())
    assert captures == []
