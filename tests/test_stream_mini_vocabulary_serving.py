"""Balanced Mini request/delivery integration, separate from model quality."""
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.enhancement_policy import resolve_model_policy
from whisperstream.enhancement_prompts import STREAM_MINI_VOCABULARY_PROMPT_KEY
from whisperstream.llm_enhancer import LLMEnhancer
from whisperstream.stream_mini_vocabulary_prompts import (
    STREAM_MINI_VOCABULARY_CHAT_PROMPT, STREAM_MINI_VOCABULARY_CHAT_SHA256,
    STREAM_MINI_VOCABULARY_EMAIL_PROMPT, STREAM_MINI_VOCABULARY_EMAIL_SHA256,
)
from whisperstream.vocabulary import VOCABULARY_INSTRUCTIONS, supports_vocabulary, vocabulary_section
from whisperstream.windows_local_llm_runtime import WindowsManagedLlamaRuntime


CANDIDATE = {
    "id": "r2-mixture-balanced-s23-20261022-v1",
    "prompt_key": STREAM_MINI_VOCABULARY_PROMPT_KEY,
    "serving_contract": "wrap-v2",
    "trained_modes": ["chat", "email"],
}


@pytest.mark.parametrize("prompt,digest", [
    (STREAM_MINI_VOCABULARY_CHAT_PROMPT, STREAM_MINI_VOCABULARY_CHAT_SHA256),
    (STREAM_MINI_VOCABULARY_EMAIL_PROMPT, STREAM_MINI_VOCABULARY_EMAIL_SHA256),
])
def test_frozen_prompt_digest(prompt, digest):
    assert hashlib.sha256(prompt.encode()).hexdigest() == digest


@pytest.mark.parametrize("mode", ["chat", "email"])
@pytest.mark.parametrize("operation", ["whole", "chunk", "preview", "probe"])
@pytest.mark.parametrize("boost", [False, True])
@pytest.mark.asyncio
async def test_candidate_requests_cross_generation_bound_runtime(mode, operation, boost, tmp_path):
    raw = "Use tan stack for this project."
    cfg = WhisperstreamConfig(
        enhancement_enabled=True, enhancement_provider="local",
        enhancement_local_model=CANDIDATE["id"], default_profile_id=mode,
        word_boosts=["TanStack", "Zygomatic"] if boost else [],
    )
    supervisor = SimpleNamespace(is_ready=True, is_running=True, base_url="http://127.0.0.1:12345/v1")
    runtime = WindowsManagedLlamaRuntime(
        supervisor, catalog_id=CANDIDATE["id"], artifact_path=tmp_path / "model.gguf",
    )
    enhancer = LLMEnhancer(
        cfg, llama_server=supervisor, local_runtime_provider=lambda: runtime,
        local_model_metadata=CANDIDATE,
    )
    captured = []

    async def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Use TanStack for this project."}, "finish_reason": "stop"}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        enhancer._client = client
        if operation == "whole":
            result = await enhancer.enhance_with_outcome(raw, profile_id=mode)
            assert result.text == "Use TanStack for this project."
            assert result.outcome == "success"
        elif operation == "chunk":
            result = await enhancer.enhance_chunk_with_outcome(raw, profile_id=mode)
            assert result.text == "Use TanStack for this project."
            assert result.outcome == "success"
        elif operation == "preview":
            await enhancer.preview(raw, "Ignored custom prompt", profile_id=mode)
        else:
            await enhancer.check_connection()

    assert not runtime._active
    assert len(captured) == 1
    payload = captured[0]
    expected = STREAM_MINI_VOCABULARY_CHAT_PROMPT if mode == "chat" else STREAM_MINI_VOCABULARY_EMAIL_PROMPT
    if operation == "probe":
        expected = "Reply with OK."
        raw = "Hi"
    candidates = ["TanStack"] if boost and operation != "probe" else []
    assert payload["messages"] == [
        {"role": "system", "content": expected + (VOCABULARY_INSTRUCTIONS if candidates else "")},
        {"role": "user", "content": f"<TRANSCRIPT>{raw}</TRANSCRIPT>" + vocabulary_section(candidates)},
    ]
    if operation == "probe":
        # Connectivity probes deliberately use their own tiny generic request.
        return
    assert {k: payload[k] for k in (
        "temperature", "top_k", "top_p", "repeat_penalty", "max_tokens", "cache_prompt", "chat_template_kwargs",
    )} == {
        "temperature": 0, "top_k": 1, "top_p": 1, "repeat_penalty": 1,
        "max_tokens": 512, "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk", [False, True])
@pytest.mark.parametrize("failure", ["cap", "timeout", "wrapper", "empty"])
async def test_candidate_keeps_raw_fallback_and_cleanup_guards(chunk, failure, tmp_path):
    cfg = WhisperstreamConfig(
        enhancement_enabled=True, enhancement_provider="local",
        enhancement_local_model=CANDIDATE["id"], word_boosts=["TanStack"],
    )
    raw = "Use tan stack for this project."
    sup = SimpleNamespace(is_ready=True, is_running=True, base_url="http://127.0.0.1:12345/v1")
    runtime = WindowsManagedLlamaRuntime(sup, catalog_id=CANDIDATE["id"], artifact_path=tmp_path / "model.gguf")
    enhancer = LLMEnhancer(cfg, llama_server=sup, local_runtime_provider=lambda: runtime, local_model_metadata=CANDIDATE)

    async def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "" if failure == "empty" else "<TRANSCRIPT>Invented result</TRANSCRIPT>"},
            "finish_reason": "length" if failure == "cap" else "stop",
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        enhancer._client = client
        result = await (enhancer.enhance_chunk_with_outcome(raw) if chunk else enhancer.enhance_with_outcome(raw))
    assert result.text == raw
    assert result.outcome == ("timeout" if failure == "timeout" else "error")
    assert not runtime._active


def test_capability_is_versioned_not_inferred_from_mini_name(monkeypatch, tmp_path):
    pytest.importorskip("whisperstream.dev_candidate_models")  # omitted from source releases
    from whisperstream.enhancement_models import get_local_model
    from whisperstream.llama_server import _thinking_off_model
    from whisperstream.server.config_payload import _config_payload
    from whisperstream.config_manager import ConfigManager
    from pathlib import Path

    registry = Path(__file__).parents[1] / "dev/candidate-models.json"
    monkeypatch.setenv("WHISPERSTREAM_DEV_CANDIDATE_REGISTRY", str(registry))
    monkeypatch.setenv("WHISPERSTREAM_DEV_CANDIDATE_DIR", str(tmp_path))
    cfg = WhisperstreamConfig(enhancement_enabled=True, enhancement_provider="local", enhancement_local_model=CANDIDATE["id"])
    row = get_local_model(CANDIDATE["id"])
    assert row["prompt_key"] == STREAM_MINI_VOCABULARY_PROMPT_KEY
    assert supports_vocabulary(cfg)
    assert _thinking_off_model(CANDIDATE["id"])
    assert _config_payload(cfg, ConfigManager())["enhancement_vocabulary_supported"] is True
    policy = resolve_model_policy("local", row, model_id=cfg.enhancement_local_model)
    assert not policy.capabilities.editable_prompt
    assert not policy.prepare("code", {}).mode_supported
    enhancer = LLMEnhancer(cfg, local_model_metadata=row, llama_server=SimpleNamespace(is_ready=True))
    cfg.word_boosts = ["TanStack"]
    assert enhancer.will_enhance("tan stack")
    assert not enhancer.will_enhance("Okay thanks.")
    assert not supports_vocabulary(cfg, {**row, "serving_contract": "raw-v1"})
    cfg.enhancement_local_model = "stream-1-mini"
    assert supports_vocabulary(cfg)
