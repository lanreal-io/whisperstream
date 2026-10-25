"""Unit tests for routing every provider read through ``effective_provider()``.

Task 3.4 (lockdown-hipaa-mode): under Lockdown Mode the stored
``enhancement_provider`` is preserved (so it restores when lockdown turns off),
but every DISPATCH / CONSENT / WARM-UP read must resolve through
``WhisperstreamConfig.effective_provider()``, which forces ``"local"`` (the
bundled on-device llama-server) when ``lockdown_mode`` is True.

These tests assert that with ``lockdown_mode=True`` + a stored cloud provider:
  * ``LLMEnhancer._requires_api_key()`` is False (no cloud key needed),
  * dispatch resolves to the LOCAL path (the bundled llama-server URL), and
  * NO cloud endpoint URL is ever selected.

And that with ``lockdown_mode=False`` the cloud behavior is byte-identical to
today (gemini still requires a key and dispatches to the Gemini cloud URL).

Crucially, the stored ``enhancement_provider`` value is never mutated by any of
this — only what ``effective_provider()`` *returns* changes.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.llm_enhancer import LLMEnhancer


# Two transcripts long enough to clear the short-input skip threshold.
LONG_TEXT = "this is a sufficiently long dictation sample for the enhancer to act on"

# A cloud URL fragment to assert NO cloud endpoint is ever selected under lockdown.
_CLOUD_URL_FRAGMENTS = (
    "generativelanguage.googleapis.com",  # gemini
    "api.openai.com",                      # openai
    "api.anthropic.com",                   # anthropic
)


def _ready_supervisor(base_url: str = "http://127.0.0.1:8080"):
    """A mock llama-server supervisor that is fully warm + ready."""
    sup = MagicMock()
    sup.is_ready = True
    sup.is_running = True
    sup.base_url = base_url
    return sup


def _make_enhancer(cfg: WhisperstreamConfig, supervisor=None) -> LLMEnhancer:
    return LLMEnhancer(cfg, logger=MagicMock(), llama_server=supervisor)


# ---------------------------------------------------------------------------
# _requires_api_key()
# ---------------------------------------------------------------------------

def test_requires_api_key_false_under_lockdown_with_cloud_provider():
    """Lockdown forces effective provider to local, which needs no cloud key."""
    cfg = WhisperstreamConfig(
        lockdown_mode=True,
        enhancement_provider="gemini",
        enhancement_gemini_api_key="sk-stored-cloud-key",
    )
    enhancer = _make_enhancer(cfg)
    assert enhancer._requires_api_key() is False
    # The stored provider is NEVER mutated by the read.
    assert cfg.enhancement_provider == "gemini"


def test_requires_api_key_true_for_cloud_provider_when_lockdown_off():
    """Lockdown off: gemini still requires a key (behavior unchanged)."""
    cfg = WhisperstreamConfig(
        lockdown_mode=False,
        enhancement_provider="gemini",
        enhancement_gemini_api_key="sk-stored-cloud-key",
    )
    enhancer = _make_enhancer(cfg)
    assert enhancer._requires_api_key() is True


def test_requires_api_key_false_for_local_when_lockdown_off():
    """Lockdown off + explicit local selection: no key needed (unchanged)."""
    cfg = WhisperstreamConfig(lockdown_mode=False, enhancement_provider="local")
    enhancer = _make_enhancer(cfg)
    assert enhancer._requires_api_key() is False


# ---------------------------------------------------------------------------
# _skip_reason()
# ---------------------------------------------------------------------------

def test_skip_reason_not_no_api_key_under_lockdown_without_cloud_key():
    """Under lockdown the no_api_key short-circuit must not fire on a stored
    cloud provider with no key — the effective provider is local.

    With a ready local supervisor the only remaining gate is satisfied, so
    enhancement is greenlit (skip reason is None).
    """
    cfg = WhisperstreamConfig(
        lockdown_mode=True,
        enhancement_enabled=True,
        enhancement_provider="gemini",  # no gemini key set
        enhancement_local_endpoint="",
    )
    enhancer = _make_enhancer(cfg, supervisor=_ready_supervisor())
    assert enhancer._skip_reason(LONG_TEXT) is None


def test_skip_reason_no_api_key_for_cloud_when_lockdown_off():
    """Lockdown off + cloud provider + no key -> the no_api_key skip fires."""
    cfg = WhisperstreamConfig(
        lockdown_mode=False,
        enhancement_enabled=True,
        enhancement_provider="gemini",  # no gemini key set
    )
    enhancer = _make_enhancer(cfg)
    assert enhancer._skip_reason(LONG_TEXT) == "no_api_key"


def test_skip_reason_local_not_warm_under_lockdown_when_supervisor_cold():
    """Under lockdown the local-warmth gate engages (effective provider=local),
    so a cold/absent supervisor yields local_not_warm, not no_api_key."""
    cfg = WhisperstreamConfig(
        lockdown_mode=True,
        enhancement_enabled=True,
        enhancement_provider="gemini",
        enhancement_local_endpoint="",
    )
    enhancer = _make_enhancer(cfg, supervisor=None)
    assert enhancer._skip_reason(LONG_TEXT) == "local_not_warm"


# ---------------------------------------------------------------------------
# Dispatch: _call_provider / _call_openai_compat URL selection
# ---------------------------------------------------------------------------

def _capture_post_url(enhancer: LLMEnhancer) -> list[str]:
    """Patch the enhancer's async client so client.post records the URL.

    Returns a list that will hold the URL passed to client.post.
    """
    captured: list[str] = []

    async def fake_post(url, *args, **kwargs):
        captured.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(
            return_value={"choices": [{"message": {"content": "cleaned text"}}]}
        )
        return resp

    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=fake_post)
    enhancer._get_client = MagicMock(return_value=fake_client)
    return captured


def test_dispatch_resolves_to_local_under_lockdown_with_cloud_provider():
    """lockdown_mode=True + enhancement_provider='gemini' must dispatch to the
    LOCAL bundled llama-server URL, NOT a cloud endpoint."""
    cfg = WhisperstreamConfig(
        lockdown_mode=True,
        enhancement_enabled=True,
        enhancement_provider="gemini",
        enhancement_gemini_api_key="sk-stored-cloud-key",
        enhancement_local_endpoint="",
    )
    enhancer = _make_enhancer(cfg, supervisor=_ready_supervisor("http://127.0.0.1:8080"))
    captured = _capture_post_url(enhancer)

    asyncio.run(enhancer._call_provider(LONG_TEXT, "SYSTEM"))

    assert len(captured) == 1
    url = captured[0]
    # Resolved to the local bundled server.
    assert url.startswith("http://127.0.0.1:8080")
    assert "/v1/chat/completions" in url
    # NO cloud endpoint anywhere.
    for fragment in _CLOUD_URL_FRAGMENTS:
        assert fragment not in url
    # Stored provider untouched.
    assert cfg.enhancement_provider == "gemini"


def test_dispatch_does_not_take_anthropic_branch_under_lockdown():
    """A stored 'anthropic' provider must NOT hit the Anthropic Messages API
    under lockdown; the local OpenAI-compat path is used instead."""
    cfg = WhisperstreamConfig(
        lockdown_mode=True,
        enhancement_enabled=True,
        enhancement_provider="anthropic",
        enhancement_anthropic_api_key="sk-ant-stored",
        enhancement_local_endpoint="",
    )
    enhancer = _make_enhancer(cfg, supervisor=_ready_supervisor("http://127.0.0.1:8080"))
    # If the anthropic branch were taken, _call_anthropic would be invoked.
    enhancer._call_anthropic = AsyncMock(
        side_effect=AssertionError("anthropic branch must be unreachable under lockdown")
    )
    captured = _capture_post_url(enhancer)

    asyncio.run(enhancer._call_provider(LONG_TEXT, "SYSTEM"))

    assert len(captured) == 1
    assert captured[0].startswith("http://127.0.0.1:8080")
    assert cfg.enhancement_provider == "anthropic"


def test_dispatch_uses_cloud_url_for_gemini_when_lockdown_off():
    """Lockdown off: gemini dispatch still hits the Gemini cloud endpoint
    (behavior unchanged)."""
    cfg = WhisperstreamConfig(
        lockdown_mode=False,
        enhancement_enabled=True,
        enhancement_provider="gemini",
        enhancement_gemini_api_key="sk-stored-cloud-key",
    )
    from whisperstream.byok_consent import record_consent
    from whisperstream.enhancement_security import consent_target
    target = consent_target(cfg)
    record_consent(target.provider, "Synthetic test consent", target=target)
    enhancer = _make_enhancer(cfg)
    captured = _capture_post_url(enhancer)

    asyncio.run(enhancer._call_provider(LONG_TEXT, "SYSTEM"))

    assert len(captured) == 1
    assert "generativelanguage.googleapis.com" in captured[0]


def test_dispatch_takes_anthropic_branch_when_lockdown_off():
    """Lockdown off: a stored anthropic provider still uses _call_anthropic."""
    cfg = WhisperstreamConfig(
        lockdown_mode=False,
        enhancement_enabled=True,
        enhancement_provider="anthropic",
        enhancement_anthropic_api_key="sk-ant-stored",
    )
    from whisperstream.byok_consent import record_consent
    from whisperstream.enhancement_security import consent_target
    target = consent_target(cfg)
    record_consent(target.provider, "Synthetic test consent", target=target)
    enhancer = _make_enhancer(cfg)
    enhancer._call_anthropic = AsyncMock(return_value="cleaned text")

    asyncio.run(enhancer._call_provider(LONG_TEXT, "SYSTEM"))

    enhancer._call_anthropic.assert_awaited_once()


# ---------------------------------------------------------------------------
# enhance() end-to-end: no cloud URL under lockdown
# ---------------------------------------------------------------------------

def test_enhance_never_selects_cloud_url_under_lockdown():
    """enhance() under lockdown with a stored cloud provider routes entirely
    through the local path: a cloud URL is never selected."""
    cfg = WhisperstreamConfig(
        lockdown_mode=True,
        enhancement_enabled=True,
        enhancement_provider="openai",
        enhancement_openai_api_key="sk-stored-openai",
        enhancement_local_endpoint="",
    )
    enhancer = _make_enhancer(cfg, supervisor=_ready_supervisor("http://127.0.0.1:8080"))
    captured = _capture_post_url(enhancer)

    result = asyncio.run(enhancer.enhance(LONG_TEXT))

    assert len(captured) == 1
    url = captured[0]
    assert url.startswith("http://127.0.0.1:8080")
    for fragment in _CLOUD_URL_FRAGMENTS:
        assert fragment not in url
    assert isinstance(result, str)
    assert cfg.enhancement_provider == "openai"
