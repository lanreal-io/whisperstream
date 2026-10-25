"""Thinking wire policy, final-answer isolation and bounded workspace recovery."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.enhancement_thinking import (
    normalize_thinking_levels, resolve_thinking, thinking_options,
)
from whisperstream.enhancement_response import (
    InvalidEnhancementResponse, parse_anthropic, parse_openai, workspace_error_code,
)


def config(provider="anthropic", **kwargs):
    return WhisperstreamConfig(enhancement_provider=provider, **kwargs)


def test_off_and_remembered_level_are_exact_model_and_endpoint_scoped():
    cfg = config(enhancement_anthropic_model="claude-haiku-5-5")
    initial = resolve_thinking(cfg)
    assert initial["effective"] == "off"
    assert thinking_options(cfg) == {"thinking": {"type": "disabled"}}
    cfg.enhancement_thinking_levels[initial["preference_key"]] = "high"
    assert thinking_options(cfg) == {
        "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
    }
    cfg.enhancement_anthropic_model = "claude-sonnet-5"
    assert resolve_thinking(cfg)["effective"] == "off"
    cfg.enhancement_anthropic_model = "claude-haiku-5-5"
    assert resolve_thinking(cfg)["effective"] == "high"
    cfg.enhancement_timeout_seconds = 17
    assert cfg.enhancement_timeout_seconds == 17


def test_custom_route_is_exact_and_canonical_not_a_hostname_substring():
    cfg = config("custom", enhancement_custom_model="anthropic/claude-haiku-5.5",
                 enhancement_custom_base_url="https://OPENROUTER.AI:443/api/v1/")
    descriptor = resolve_thinking(cfg)
    assert descriptor["options"] == ["off", "on"]
    assert thinking_options(cfg) == {"reasoning": {"enabled": False}}
    cfg.enhancement_custom_base_url = "https://openrouter.ai.evil.invalid/api/v1"
    assert resolve_thinking(cfg)["kind"] == "unknown"
    assert thinking_options(cfg) == {}
    cfg.enhancement_custom_base_url = "https://openrouter.ai/api/v1/other"
    assert thinking_options(cfg) == {}


def test_luna_none_and_mandatory_models_never_claim_off():
    cfg = config("openai", enhancement_openai_model="gpt-6-luna")
    assert thinking_options(cfg) == {"reasoning_effort": "none"}
    cfg.enhancement_openai_model = "gpt-6.1-sol"
    descriptor = resolve_thinking(cfg)
    cfg.enhancement_thinking_levels[descriptor["preference_key"]] = "off"
    assert resolve_thinking(cfg)["kind"] == "mandatory"
    assert resolve_thinking(cfg)["effective"] == "low"
    assert thinking_options(cfg) == {"reasoning_effort": "low"}


@pytest.mark.parametrize("model,minimum", [("gemini-3.5-flash-lite", "minimal"), ("gemini-3.7-flash", "low")])
def test_curated_gemini_uses_verified_minimum_without_claiming_off(model, minimum):
    cfg = config("gemini", enhancement_gemini_model=model)
    assert resolve_thinking(cfg)["kind"] == "mandatory"
    assert resolve_thinking(cfg)["default"] == minimum
    assert "off" not in resolve_thinking(cfg)["options"]
    assert thinking_options(cfg) == {"reasoning_effort": minimum}


def test_unknown_and_fixed_local_do_not_override_validated_sampling():
    cfg = config("custom", enhancement_custom_base_url="https://example.invalid/v1",
                 enhancement_custom_model="unverified")
    assert thinking_options(cfg) == {}
    cfg.enhancement_provider = "local"
    cfg.enhancement_local_model = "stream-1-mini"
    assert thinking_options(cfg) == {}  # Existing generation policy owns it.
    assert resolve_thinking(cfg, metadata={"prompt_key": "stream-mini"})["kind"] == "fixed"


def test_preferences_are_bounded_validated_and_round_trip(tmp_path):
    from whisperstream.config_manager import ConfigManager
    assert normalize_thinking_levels({"a" * 64: "high", "invalid": "off", "b" * 64: "bogus"}) == {"a" * 64: "high"}
    assert normalize_thinking_levels(["off"]) == {}
    assert len(normalize_thinking_levels({f"{n:064x}": "off" for n in range(200)})) == 128
    cfg = WhisperstreamConfig.from_dict({"enhancement_thinking_levels": {"a" * 64: "high"},
                                        "enhancement_anthropic_workspace_id": " wrkspc_demo "})
    manager = ConfigManager(config_path=tmp_path / "config.toml")
    manager.save(cfg)
    restored = manager.load()
    assert restored.enhancement_thinking_levels == cfg.enhancement_thinking_levels
    assert restored.enhancement_anthropic_workspace_id == "wrkspc_demo"


def test_mixed_anthropic_blocks_preserve_only_complete_final_text():
    assert parse_anthropic({"stop_reason": "end_turn", "content": [
        {"type": "thinking", "thinking": "private scratch"},
        {"type": "redacted_thinking", "data": "hidden"},
        {"type": "text", "text": "Final "}, {"type": "text", "text": "answer."},
    ]}) == "Final answer."


@pytest.mark.parametrize("data", [
    {}, {"content": []}, {"content": [{"type": "text", "text": "Missing completion marker"}]},
    {"content": [{"type": "thinking", "thinking": "scratch"}]},
    {"content": [{"type": "text", "text": ""}]},
    {"content": [{"type": "tool_use", "name": "something"}, {"type": "text", "text": "Partial"}]},
    {"content": [{"type": "text", "text": "Partial"}], "stop_reason": "max_tokens"},
    {"content": [{"type": "text", "text": "No"}], "stop_reason": "refusal"},
    {"content": [{"type": "text", "text": "Partial"}], "stop_reason": "unexpected"},
    {"content": [{"type": "text", "text": None}]},
])
def test_bad_anthropic_answers_fail_whole_unit(data):
    with pytest.raises(InvalidEnhancementResponse):
        parse_anthropic(data)


def test_openai_reasoning_is_separate_from_final_answer():
    assert parse_openai({"choices": [{"finish_reason": "stop", "message": {
        "reasoning": "private scratch", "content": [
            {"type": "text", "text": "Final "}, {"type": "text", "text": "answer"},
        ],
    }}]}) == "Final answer"


@pytest.mark.parametrize("message,reason", [
    ({"content": None, "reasoning": "scratch"}, "stop"),
    ({"content": "Partial"}, "length"), ({"content": "No", "refusal": "refused"}, "stop"),
    ({"content": "Partial", "tool_calls": [{}]}, "stop"),
    ({"content": [{"type": "reasoning", "text": "scratch"}]}, "stop"),
    ({"content": "partial"}, "unknown"),
])
def test_openai_invalid_final_answers_fail_whole_unit(message, reason):
    with pytest.raises(InvalidEnhancementResponse):
        parse_openai({"choices": [{"message": message, "finish_reason": reason}]})


@pytest.mark.parametrize("status,message,expected", [
    (400, "anthropic-workspace-id header is required for this API key", "workspace_required"),
    (400, "Missing required header: anthropic-workspace-id", "workspace_required"),
    (400, "A key not scoped to a workspace must include the anthropic-workspace-id header", "workspace_required"),
    (400, "Invalid anthropic-workspace-id header", "workspace_invalid"),
    (400, "anthropic-workspace-id header must be a valid workspace ID.", "workspace_invalid"),
    (404, "Workspace not found", "workspace_unavailable"),
    (404, "Workspace `00000000-0000-0000-0000-000000000000` not found.", "workspace_unavailable"),
    (401, "Invalid API key", None), (400, "max_tokens must be positive", None),
    (400, "Model not found for workspace", None), (429, "Workspace rate limit", None),
])
def test_bounded_workspace_errors(status, message, expected):
    body = {"error": {"type": "invalid_request_error" if status == 400 else "not_found_error", "message": message}}
    assert workspace_error_code(status, body, has_workspace=True) == expected
    assert workspace_error_code(status, {"error": {"message": 123}}, has_workspace=True) is None


def admitted_config(provider="anthropic", **kwargs):
    from whisperstream.byok_consent import record_consent
    from whisperstream.enhancement_security import consent_target
    cfg = config(provider, enhancement_enabled=True, enhancement_anthropic_api_key="synthetic-key",
                 enhancement_openai_api_key="synthetic-key", enhancement_custom_api_key="synthetic-key", **kwargs)
    target = consent_target(cfg)
    record_consent(target.provider, "Synthetic test consent", target=target)
    return cfg


@pytest.mark.parametrize("provider,fields,wire", [
    ("anthropic", {"enhancement_anthropic_model": "claude-haiku-5-5"}, {"thinking": {"type": "disabled"}}),
    ("openai", {"enhancement_openai_model": "gpt-6-luna"}, {"reasoning_effort": "none"}),
    ("custom", {"enhancement_custom_model": "anthropic/claude-haiku-5.5",
                "enhancement_custom_base_url": "https://openrouter.ai/api/v1"}, {"reasoning": {"enabled": False}}),
])
def test_every_entry_point_uses_production_thinking_policy(provider, fields, wire):
    import httpx
    from whisperstream.llm_enhancer import LLMEnhancer
    cfg = admitted_config(provider, enhancement_anthropic_workspace_id="wrkspc_synthetic", **fields)
    calls = []
    def respond(request):
        calls.append((json.loads(request.content), dict(request.headers)))
        if provider == "anthropic":
            return httpx.Response(200, json={"stop_reason": "end_turn", "content": [
                {"type": "thinking", "thinking": "scratch-only"}, {"type": "text", "text": "Clean final text."}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "reasoning": "scratch-only", "content": "Clean final text."}}]})
    async def run():
        enhancer = LLMEnhancer(cfg)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            enhancer._client = client
            assert await enhancer.enhance("please clean this final text") == "Clean final text."
            assert await enhancer.enhance_chunk("please clean this final text") == "Clean final text."
            assert await enhancer.preview("clean final text", "Clean it.") == "Clean final text."
            assert await enhancer.check_connection() == (True, "Connected")
    asyncio.run(run())
    assert len(calls) == 4
    for payload, headers in calls:
        assert all(payload[key] == value for key, value in wire.items())
        assert ("anthropic-workspace-id" in headers) == (provider == "anthropic")


@pytest.mark.parametrize("method", ["enhance", "enhance_chunk"])
@pytest.mark.parametrize("body", [
    {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Partial answer"}]},
    {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "scratch-only"}]},
])
def test_invalid_final_answer_keeps_entire_raw_unit(method, body, caplog):
    import httpx
    from whisperstream.llm_enhancer import LLMEnhancer
    raw = "synthetic raw transcript must stay complete"
    cfg = admitted_config(enhancement_anthropic_model="claude-haiku-5-5")
    async def run():
        enhancer = LLMEnhancer(cfg)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
            enhancer._client = client
            assert await getattr(enhancer, method)(raw) == raw
    asyncio.run(run())
    assert raw not in caplog.text and "scratch-only" not in caplog.text


def test_workspace_failure_survives_adapter_and_route_without_raw_error():
    import httpx
    from whisperstream.llm_enhancer import LLMEnhancer
    from whisperstream.server.routes.enhancement import register_enhancement_execution_routes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    cfg = admitted_config(enhancement_anthropic_model="claude-haiku-5-5")
    async def check():
        enhancer = LLMEnhancer(cfg)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400, json={
            "error": {"type": "invalid_request_error", "message": "This key requires the anthropic-workspace-id header. echoed-private-data"}
        }))) as client:
            enhancer._client = client
            return await enhancer.check_connection()
    result = asyncio.run(check())
    assert result.error_code == "workspace_required"
    api = FastAPI()
    register_enhancement_execution_routes(api, current_config=lambda: cfg,
        current_enhancer=lambda: type("Enhancer", (), {"check_connection": AsyncMock(return_value=result)})(),
        request_grant=lambda request: object(), acceptance_is_current=lambda grant: True,
        blocked_response=lambda path: None, broadcast=lambda *args, **kwargs: None)
    with TestClient(api) as client:
        response = client.post("/api/enhancement/check").json()
        assert response == {"connected": False, "message": "Anthropic workspace settings need attention.", "error_code": "workspace_required"}
        assert client.get("/api/enhancement/thinking").json() == resolve_thinking(cfg)


@pytest.mark.parametrize("value", ["abc\r\ninjected:header", "x" * 129, None, 12])
def test_workspace_rejects_header_injection_and_invalid_types(value):
    with pytest.raises(ValueError, match="Invalid Anthropic workspace setting"):
        WhisperstreamConfig.from_dict({"enhancement_anthropic_workspace_id": value})


def test_config_patch_clears_workspace_only_when_actual_anthropic_key_changes(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from whisperstream.config_manager import ConfigManager
    from whisperstream.server.routes.config import register_config_routes
    current = [config(enhancement_anthropic_api_key="old-synthetic-key",
                      enhancement_anthropic_workspace_id="wrkspc_synthetic")]
    manager = ConfigManager(config_path=tmp_path / "config.toml", models_root=tmp_path / "models")
    api = FastAPI()
    register_config_routes(api, current_config=lambda: current[0], current_config_manager=lambda: manager,
        reload_config=lambda cfg: current.__setitem__(0, cfg), reconcile_input_selection=lambda: (current[0], None),
        managed_lockdown_enabled=lambda: False, config_saved=lambda *args: None)
    with TestClient(api) as client:
        assert client.put("/api/config", json={"enhancement_timeout_seconds": 12}).status_code == 200
        assert current[0].enhancement_anthropic_workspace_id == "wrkspc_synthetic"
        assert client.put("/api/config", json={"enhancement_anthropic_api_key": "new-synthetic-key",
            "enhancement_anthropic_workspace_id": "wrkspc_stale"}).status_code == 200
        assert current[0].enhancement_anthropic_workspace_id == ""
        assert manager.load().enhancement_anthropic_workspace_id == ""
        assert client.put("/api/config", json={"enhancement_anthropic_workspace_id": "abc\r\nheader"}).status_code == 400
        assert current[0].enhancement_anthropic_workspace_id == ""


@pytest.mark.parametrize("provider,expected", [("anthropic", ""), ("openai", "wrkspc_synthetic")])
def test_remove_key_clears_only_active_anthropic_workspace_atomically(provider, expected):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from whisperstream.server.routes.consent import register_consent_mutation_routes
    cfg = config(provider, enhancement_anthropic_workspace_id="wrkspc_synthetic")
    saved = []
    api = FastAPI()
    register_consent_mutation_routes(api, current_config=lambda: cfg,
        save_config=saved.append, reload_config=lambda cfg: None)
    with TestClient(api) as client:
        assert client.post("/api/enhancement/remove-key").status_code == 200
    assert len(saved) == 1
    assert saved[0].enhancement_anthropic_workspace_id == expected
