"""Onboarding Enhance step contract on suppressed dictations.

While onboarding suppresses injection, the single-call ``transcription.result``
broadcast also carries ``raw_text`` (the raw transcript through the same
``process()`` pass) and a bounded ``enhancement`` outcome so the try-it card can
label the result by outcome rather than by a text diff. Suppressed dictations
also always use the built-in Chat profile, ignoring Power Mode routing for the
focused app. Ordinary dictations keep the existing payload exactly.

Construction mirrors test_paste_last_original_wiring.py.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.enhancement_outcomes import EnhancementResult
from whisperstream.main import WhisperstreamApp

pytestmark = pytest.mark.usefixtures("accepted_eula")


def _make_app(*, enhancement_enabled: bool = True, inject_suppressed: bool = True):
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app.audio_feedback = MagicMock()
    app.stt_engine = MagicMock()
    app.text_injector = MagicMock()
    app.text_injector.process.side_effect = lambda text, **kw: f"P:{text}"
    app.text_injector.last_transcript = None
    app.config = WhisperstreamConfig(
        enhancement_enabled=enhancement_enabled,
        enhancement_provider="local",
        enhancement_local_model="stream-1-mini",
        enhancement_chunked_cleanup=True,
        default_profile_id="chat",
        profiles=[
            {"id": "chat", "name": "Chat", "prompt": "p"},
            {"id": "code", "name": "Code", "prompt": "c"},
        ],
    )
    app._inject_suppressed = inject_suppressed
    app._broadcast = MagicMock()
    app.review_state = MagicMock()
    app.license_manager = MagicMock()
    app.transcript_store = None
    app._last_waveform = None
    app._dictation_app = None
    app._manual_profile_id = None
    app._override_anchor = None
    return app


def _enhancer(result: EnhancementResult | None = None, *, will: bool = True):
    enhancer = MagicMock()
    enhancer.will_enhance.return_value = will
    enhancer.enhance_with_outcome = AsyncMock(
        return_value=result or EnhancementResult("Clean text.", "success")
    )
    return enhancer


def _result_payload(app) -> dict:
    payloads = [
        call.args[0]
        for call in app._broadcast.call_args_list
        if call.args[0].get("type") == "transcription.result"
    ]
    assert len(payloads) == 1
    return payloads[0]


RAW = "lets meet on tuesday no wait thursday"


def test_applied_outcome_carries_raw_and_enhanced():
    app = _make_app()
    app.llm_enhancer = _enhancer(EnhancementResult("Clean text.", "success"))
    app._enhance_then_inject(RAW, 1.0)
    payload = _result_payload(app)
    assert payload["text"] == "P:Clean text."
    assert payload["raw_text"] == f"P:{RAW}"
    assert payload["enhancement"] == "applied"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("not_attempted", "skipped"), ("timeout", "failed"), ("error", "failed")],
)
def test_fallback_outcomes_are_labelled(outcome, expected):
    app = _make_app()
    app.llm_enhancer = _enhancer(EnhancementResult(RAW, outcome))
    app._enhance_then_inject(RAW, 1.0)
    payload = _result_payload(app)
    assert payload["text"] == f"P:{RAW}"
    assert payload["raw_text"] == f"P:{RAW}"
    assert payload["enhancement"] == expected


def test_enhancement_disabled_reports_off():
    app = _make_app(enhancement_enabled=False)
    app.llm_enhancer = _enhancer()
    app._enhance_then_inject(RAW, 1.0)
    payload = _result_payload(app)
    assert payload["enhancement"] == "off"
    assert payload["raw_text"] == f"P:{RAW}"


def test_ordinary_dictation_payload_is_unchanged():
    app = _make_app(inject_suppressed=False)
    app.llm_enhancer = _enhancer()
    app._enhance_then_inject(RAW, 1.0)
    payload = _result_payload(app)
    assert "raw_text" not in payload
    assert "enhancement" not in payload


def _route_focused_app_to_code(app, monkeypatch):
    """Make Power Mode resolve every dictation to the Code profile."""
    monkeypatch.setattr(
        WhisperstreamApp,
        "_resolve_profile_for_routing",
        lambda self, **kw: "code",
    )
    monkeypatch.setattr(
        WhisperstreamApp,
        "_resolve_active_profile",
        lambda self: "code",
    )


def test_suppressed_dictation_forces_chat_profile(monkeypatch):
    app = _make_app()
    _route_focused_app_to_code(app, monkeypatch)
    app.llm_enhancer = _enhancer()
    app._enhance_then_inject(RAW, 1.0)
    kwargs = app.llm_enhancer.enhance_with_outcome.call_args.kwargs
    assert kwargs["profile_id"] == "chat"


def test_unsuppressed_dictation_keeps_power_mode_routing(monkeypatch):
    app = _make_app(inject_suppressed=False)
    _route_focused_app_to_code(app, monkeypatch)
    app.llm_enhancer = _enhancer()
    app._enhance_then_inject(RAW, 1.0)
    kwargs = app.llm_enhancer.enhance_with_outcome.call_args.kwargs
    assert kwargs["profile_id"] == "code"
