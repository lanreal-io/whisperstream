"""Synthetic transport capture; never contact a provider or use real content."""
import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest

from whisperstream.config_manager import ConfigManager, WhisperstreamConfig
from whisperstream import byok_consent
from whisperstream.enhancement_security import (
    ConsentTarget, EnhancementAdmissionError, consent_target, normalize_endpoint,
    require_enhancement_admission,
)
from whisperstream.llm_enhancer import LLMEnhancer


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))


def config_for(provider):
    return WhisperstreamConfig(
        enhancement_provider=provider, enhancement_enabled=True,
        enhancement_api_key="synthetic-key",
        enhancement_custom_base_url="https://synthetic.invalid/team/v1",
        enhancement_custom_model="synthetic-model",
        profiles=[{"id": "secret", "name": "Secret", "mode": "chat", "prompt": "SAVED PRIVATE PROFILE"}],
        default_profile_id="secret",
    )


def grant(cfg):
    target = consent_target(cfg)
    byok_consent.record_consent(target.provider, "Synthetic disclosure", target=target)


@pytest.mark.parametrize("value", ["http://remote.invalid", "https://user:secret@host.invalid", "https://host.invalid/?key=secret", "https://host.invalid/#secret", "ftp://localhost", "https://localhost\\@remote.invalid", "https://host.invalid:99999", "http://127.0.0.1.remote.invalid", "https://host.invalid/\npath"])
def test_invalid_endpoint_rejected(value):
    with pytest.raises(EnhancementAdmissionError):
        normalize_endpoint(value)


@pytest.mark.parametrize("value,expected", [(" HTTPS://EXAMPLE.COM:443/v1/ ", "https://example.com/v1"), ("http://127.2.3.4:80/", "http://127.2.3.4"), ("http://[::1]:8080/v1", "http://[::1]:8080/v1")])
def test_endpoint_normalization(value, expected):
    assert normalize_endpoint(value) == expected


@pytest.mark.parametrize("field,value", [("enhancement_custom_base_url", "http://remote.invalid"), ("enhancement_ollama_endpoint", "http://remote.invalid"), ("enhancement_local_endpoint", "https://remote.invalid")])
def test_config_validation_rejects_unsafe_routes(field, value):
    cfg = WhisperstreamConfig()
    setattr(cfg, field, value)
    with pytest.raises(ValueError):
        cfg.validate()


@pytest.mark.parametrize("field,value", [("enhancement_custom_base_url", "http://remote.invalid"), ("enhancement_ollama_endpoint", "http://remote.invalid"), ("enhancement_local_endpoint", "https://remote.invalid")])
def test_legacy_unsafe_config_opens_disabled_without_overwriting_source(field, value):
    manager = ConfigManager()
    before = f'enhancement_enabled = true\n{field} = "{value}"\n'.encode()
    manager.config_path.write_bytes(before)
    restored = manager.load()
    assert restored.enhancement_enabled is False
    assert getattr(restored, field) == ""
    assert manager.config_path.read_bytes() == before


@pytest.mark.parametrize("provider", ["gemini", "openai", "anthropic", "ollama", "custom", "local"])
def test_probe_never_contains_profile_content(provider):
    cfg = config_for(provider)
    if provider != "local":
        grant(cfg)
    supervisor = MagicMock(is_ready=True, base_url="http://127.0.0.1:12345")
    enhancer = LLMEnhancer(cfg, llama_server=supervisor)
    captures = []
    def capture(request):
        captures.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}], "stop_reason": "end_turn", "content": [{"type": "text", "text": "OK"}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
            enhancer._client = client
            assert (await enhancer.check_connection())[0]
    asyncio.run(run())
    assert len(captures) == 1
    assert "SAVED PRIVATE PROFILE" not in captures[0].content.decode()


@pytest.mark.parametrize("provider", ["gemini", "openai", "anthropic", "ollama", "custom"])
@pytest.mark.parametrize("operation", ["probe", "preview", "chunk", "whole"])
def test_missing_consent_blocks_actual_dispatch(provider, operation):
    cfg = config_for(provider)
    enhancer = LLMEnhancer(cfg)
    captures = []
    def capture(request):
        captures.append(request)
        return httpx.Response(500)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
            enhancer._client = client
            if operation == "probe":
                await enhancer.check_connection()
            elif operation == "preview":
                await enhancer.preview("synthetic input text", "synthetic prompt")
            elif operation == "chunk":
                await enhancer.enhance_chunk("this is synthetic input text")
            else:
                await enhancer.enhance("this is synthetic input text")
    asyncio.run(run())
    assert captures == []


def test_disabled_preview_and_remote_local_fail_at_dispatch():
    for cfg in [WhisperstreamConfig(enhancement_enabled=False), WhisperstreamConfig(enhancement_enabled=True, enhancement_provider="local", enhancement_local_endpoint="https://remote.invalid")]:
        enhancer = LLMEnhancer(cfg, llama_server=MagicMock(is_ready=True, base_url="http://127.0.0.1:12345"))
        with pytest.raises(EnhancementAdmissionError):
            asyncio.run(enhancer._call_provider("synthetic", "fixed"))


def test_recipient_and_processing_changes_require_consent():
    cfg = config_for("ollama")
    grant(cfg)
    require_enhancement_admission(cfg)
    cfg.enhancement_ollama_model = "different-cloud-model"
    with pytest.raises(EnhancementAdmissionError, match="consent_required"):
        require_enhancement_admission(cfg)
    assert len(byok_consent.list_records()) == 1


def test_legacy_consent_is_retained_but_not_current():
    byok_consent.record_consent("custom", "old provider-only disclosure")
    assert not byok_consent.has_target_consent(consent_target(config_for("custom")))
    assert len(byok_consent.list_records()) == 1


@pytest.mark.parametrize("evidence", [b"unreadable historical evidence", b"\xff\xfe"])
def test_malformed_ledger_is_not_overwritten_by_new_consent(evidence):
    path = byok_consent.get_consent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(evidence)
    assert not byok_consent.has_target_consent(consent_target(config_for("openai")))
    with pytest.raises(ValueError, match="consent_store_unavailable"):
        grant(config_for("openai"))
    assert path.read_bytes() == evidence


@pytest.mark.parametrize("provider,method", [("openai", "_call_anthropic"), ("anthropic", "_call_openai_compat")])
def test_direct_dispatch_cannot_use_consent_for_a_different_provider(provider, method):
    cfg = config_for(provider)
    grant(cfg)
    with pytest.raises(EnhancementAdmissionError, match="provider_dispatch_mismatch"):
        asyncio.run(getattr(LLMEnhancer(cfg), method)("synthetic input", "fixed"))


@pytest.mark.parametrize("provider,field", [("custom", "enhancement_custom_base_url"), ("ollama", "enhancement_ollama_endpoint")])
def test_hand_edited_http_endpoint_fails_at_dispatch(provider, field):
    cfg = config_for(provider)
    setattr(cfg, field, "http://offdevice.invalid")
    with pytest.raises(EnhancementAdmissionError, match="endpoint_https_required"):
        asyncio.run(LLMEnhancer(cfg)._call_provider("synthetic input", "fixed"))


def test_client_disables_environment_proxies_and_redirects():
    async def run():
        client = LLMEnhancer(config_for("openai"))._get_client()
        assert client.trust_env is False
        assert client.follow_redirects is False
        await client.aclose()
    asyncio.run(run())
