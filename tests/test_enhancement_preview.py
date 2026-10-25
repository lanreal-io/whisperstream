"""Tests for the per-profile "Try it" prompt tester (issue 4).

Covers:
- LLMEnhancer.preview(text, system_prompt) — runs the configured provider with
  the GIVEN prompt via the merged _call_provider path; returns raw text on
  failure; never re-applies the skip-reason gating or output guards.
- POST /api/enhancement/preview — returns {"output": ...} for a configured
  provider and a structured 400 (error_code="not_configured") when no
  provider/key is set up.

The provider HTTP call is always stubbed (monkeypatch / mock) so no network,
real key, or llama-server is required.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.llm_enhancer import LLMEnhancer

pytestmark = pytest.mark.usefixtures("accepted_eula")


# ---------------------------------------------------------------------------
# LLMEnhancer.preview — unit level
# ---------------------------------------------------------------------------


def test_preview_uses_given_prompt_and_returns_output():
    """preview() dispatches to _call_provider with the SUPPLIED prompt and
    returns the provider's output verbatim."""
    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="gemini",
        enhancement_gemini_api_key="fake-key",
    )
    enhancer = LLMEnhancer(config)

    captured: dict[str, str] = {}

    async def fake_call(text, system_prompt=None):
        captured["text"] = text
        captured["system_prompt"] = system_prompt or ""
        return "I'll be five minutes late."

    with patch.object(enhancer, "_call_provider", side_effect=fake_call):
        result = asyncio.run(
            enhancer.preview("tell him i'll be 5 min late", "MY CUSTOM PROMPT")
        )

    assert result == "I'll be five minutes late."
    # The in-editor prompt is passed straight through, not re-resolved.
    assert captured["system_prompt"] == "MY CUSTOM PROMPT"
    assert captured["text"] == "tell him i'll be 5 min late"


def test_preview_runs_on_short_input():
    """preview() bypasses the short-input skip-reason gating that enhance()
    applies, so even a 1-word sample reaches the provider."""
    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="gemini",
        enhancement_gemini_api_key="fake-key",
    )
    enhancer = LLMEnhancer(config)

    call = AsyncMock(return_value="Hi.")
    with patch.object(enhancer, "_call_provider", call):
        result = asyncio.run(enhancer.preview("hi", "PROMPT"))

    assert result == "Hi."
    call.assert_awaited_once()


def test_preview_returns_raw_text_on_provider_failure():
    """Any provider exception → preview() returns the input text unchanged."""
    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="gemini",
        enhancement_gemini_api_key="fake-key",
    )
    enhancer = LLMEnhancer(config)

    async def boom(text, system_prompt=None):
        raise RuntimeError("HTTP 500")

    with patch.object(enhancer, "_call_provider", side_effect=boom):
        result = asyncio.run(enhancer.preview("some sample text", "PROMPT"))

    assert result == "some sample text"


def test_preview_falls_back_to_raw_on_empty_output():
    """An empty / whitespace-only provider response falls back to the raw text
    rather than returning an empty string."""
    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="gemini",
        enhancement_gemini_api_key="fake-key",
    )
    enhancer = LLMEnhancer(config)

    with patch.object(enhancer, "_call_provider", AsyncMock(return_value="   ")):
        result = asyncio.run(enhancer.preview("keep me", "PROMPT"))

    assert result == "keep me"


def test_preview_uses_stream_mini_registry_contract_for_selected_email_profile():
    """A fixed-prompt candidate ignores the editor prompt and previews with
    its registry-owned mode, prompt, and user-content contract."""
    from whisperstream.stream_mini_prompts import STREAM_MINI_EMAIL_PROMPT

    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_local_model="stream-1-mini",
        default_profile_id="chat",
        profiles=[
            {"id": "chat", "name": "Chat", "prompt": "chat editor prompt"},
            {"id": "email", "name": "Email", "prompt": "email editor prompt"},
        ],
    )
    enhancer = LLMEnhancer(config)
    candidate = {
        "id": "stream-1-mini",
        "source": "production",
        "prompt_key": "stream-mini",
        "serving_contract": "wrap-v2",
        "trained_modes": ["chat", "email"],
    }
    captured = {}

    async def fake_call(text, system_prompt=None, **kwargs):
        captured.update(text=text, system_prompt=system_prompt, **kwargs)
        return "Clean email output."

    with (
        patch("whisperstream.llm_enhancer.get_local_model", return_value=candidate),
        patch.object(enhancer, "_call_provider", side_effect=fake_call),
    ):
        result = asyncio.run(
            enhancer.preview(
                "raw email transcript",
                "must be ignored",
                profile_id="email",
            )
        )

    assert result == "Clean email output."
    assert captured == {
        "text": "raw email transcript",
        "system_prompt": STREAM_MINI_EMAIL_PROMPT,
        "serving_contract": "wrap-v2",
        "prompt_key": "stream-mini",
        "profile_mode": "email",
    }


# ---------------------------------------------------------------------------
# POST /api/enhancement/preview — endpoint contract
# ---------------------------------------------------------------------------


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    defaults = WhisperstreamConfig()
    for provider in ("gemini", "openai", "anthropic", "custom", "ollama"):
        setattr(cfg, f"enhancement_{provider}_model", getattr(defaults, f"enhancement_{provider}_model"))
    cfg.enhancement_provider = "gemini"
    cfg.enhancement_local_model = "gemma4-e2b-instruct"
    cfg.enhancement_local_endpoint = ""
    cfg.enhancement_api_key = ""
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.parakeet_quantization = "int8"
    cfg.word_boosts = []
    cfg.threads = None
    cfg.model_timeout = 0.0
    cfg.hotword_score = 1.5
    cfg.beam_width = 4
    cfg.lockdown_mode = False
    # active_api_key() is what the endpoint gate reads.
    cfg.active_api_key = MagicMock(return_value="")
    # effective_provider() is the single dispatch chokepoint (Task 4.6).
    # Without lockdown, it mirrors the stored enhancement_provider.
    cfg.effective_provider = MagicMock(side_effect=lambda: cfg.enhancement_provider)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.enhancement_enabled = True
    if cfg.enhancement_provider != "local":
        from whisperstream.byok_consent import record_consent
        from whisperstream.enhancement_security import consent_target
        target = consent_target(cfg)
        record_consent(target.provider, "Synthetic test consent", target=target)
    return cfg


def _make_client(*, config=None, enhancer=None):
    """Return (TestClient, server) with a mock app whose llm_enhancer is the
    supplied (real or mock) enhancer."""
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.config = config or _make_mock_config()
    app.stt_engine = MagicMock()
    app.llama_server = None
    app.llm_enhancer = enhancer or MagicMock()
    app.config_manager = MagicMock()
    app.reload_config = MagicMock()
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.load_stt_engine_async = MagicMock()
    app.set_event_broadcaster = MagicMock()

    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    client = TestClient(server.fastapi, raise_server_exceptions=True)
    return client, server


def test_preview_endpoint_returns_output_for_configured_provider():
    """A configured cloud provider runs preview() and returns {"output": ...}."""
    cfg = _make_mock_config(enhancement_provider="gemini")
    cfg.active_api_key = MagicMock(return_value="fake-key")

    enhancer = MagicMock()
    enhancer.preview = AsyncMock(return_value="I'll be five minutes late.")

    client, _ = _make_client(config=cfg, enhancer=enhancer)
    resp = client.post(
        "/api/enhancement/preview",
        json={"text": "tell him i'll be 5 min late", "system_prompt": "P"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"output": "I'll be five minutes late."}
    # The endpoint passes text + the client-supplied prompt straight through.
    enhancer.preview.assert_awaited_once_with("tell him i'll be 5 min late", "P")


def test_preview_endpoint_unconfigured_returns_400():
    """A cloud provider with no stored key returns a structured 400 with
    error_code=not_configured, and does NOT call the provider."""
    cfg = _make_mock_config(enhancement_provider="gemini")
    cfg.active_api_key = MagicMock(return_value="")

    enhancer = MagicMock()
    enhancer.preview = AsyncMock(return_value="should not be called")

    client, _ = _make_client(config=cfg, enhancer=enhancer)
    resp = client.post(
        "/api/enhancement/preview",
        json={"text": "hello world", "system_prompt": "P"},
    )

    assert resp.status_code == 400
    assert resp.json().get("error_code") == "not_configured"
    enhancer.preview.assert_not_awaited()


def test_preview_endpoint_local_provider_needs_no_key():
    """Local provider has no API key but must still reach preview() (the warm
    gating is a frontend concern; the backend just runs it)."""
    cfg = _make_mock_config(enhancement_provider="local")
    cfg.active_api_key = MagicMock(return_value="")

    enhancer = MagicMock()
    enhancer.preview = AsyncMock(return_value="cleaned")

    client, _ = _make_client(config=cfg, enhancer=enhancer)
    resp = client.post(
        "/api/enhancement/preview",
        json={"text": "raw sample text here", "system_prompt": "P"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"output": "cleaned"}
    enhancer.preview.assert_awaited_once()


def test_preview_endpoint_forwards_selected_profile_id():
    cfg = _make_mock_config(enhancement_provider="local")
    cfg.active_api_key = MagicMock(return_value="")
    enhancer = MagicMock()
    enhancer.preview = AsyncMock(return_value="cleaned")

    client, _ = _make_client(config=cfg, enhancer=enhancer)
    resp = client.post(
        "/api/enhancement/preview",
        json={
            "text": "raw sample",
            "system_prompt": "editor prompt",
            "profile_id": "email",
        },
    )

    assert resp.status_code == 200
    enhancer.preview.assert_awaited_once_with(
        "raw sample", "editor prompt", profile_id="email"
    )
