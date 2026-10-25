"""Always-registered Agent Speak setup routes (Task 1.3).

These three routes must exist in every build with no dev gate, while still
inheriting the existing hook gates (``claude_hud_enabled`` and EULA
acceptance). Teardown must be idempotent and must never raise.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from whisperstream.agent_setup_payloads import (
    setup_question_payloads,
    setup_response_payloads,
)

pytestmark = pytest.mark.usefixtures("accepted_eula")

FIRE_ROUTES = (
    "/api/setup/agent-speak/question",
    "/api/setup/agent-speak/response",
)
PREVIEW_ROUTE = "/api/setup/agent-speak/delivery-preview"
PREVIEW_ALIAS_ROUTE = "/api/setup/agent-speak/preview"
TEARDOWN_ROUTE = "/api/setup/agent-speak/teardown"


def _make_server(*, claude_hud_enabled: bool = False, logger=None):
    from whisperstream.server import WhisperstreamServer

    config = MagicMock()
    config.claude_hud_enabled = claude_hud_enabled
    config.codex_hud_enabled = False
    config.lockdown_mode = False
    config.agent_summary_text_enabled = False
    config.agent_speech_enabled = False
    config.agent_speech_activation = "always_on"

    app = MagicMock()
    app.config = config
    app.parakeet = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.dictation_state_snapshot.return_value = None
    return WhisperstreamServer(
        app, port=0, logger=logger, register_hotkeys=False
    )


def _client(server):
    return TestClient(server.fastapi, raise_server_exceptions=True)


def test_setup_routes_are_registered_without_dev_tools(monkeypatch):
    monkeypatch.delenv("WHISPERSTREAM_DEV_TOOLS", raising=False)
    server = _make_server()
    registered = {
        route.path
        for route in server.fastapi.routes
        if getattr(route, "path", None)
    }
    assert set(FIRE_ROUTES) | {TEARDOWN_ROUTE, PREVIEW_ROUTE, PREVIEW_ALIAS_ROUTE} <= registered

    with _client(server) as client:
        for path in (*FIRE_ROUTES, TEARDOWN_ROUTE):
            assert client.post(path).status_code == 200


def test_setup_source_has_no_dev_gate():
    """The routes must not inherit the dev-tools gate in any form."""
    import ast
    from pathlib import Path

    import whisperstream.server.routes.agent_setup as server_mod

    source = Path(server_mod.__file__).read_text(encoding="utf-8")
    assert "_dev_tools_enabled" not in source
    assert "DEV_TOOLS_AVAILABLE" not in source
    # The dev-only modules are excluded from the Nuitka build, so the routes
    # may name them in prose but must never import from them.
    imported_modules = {
        node.module.rsplit(".", 1)[-1]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        and node.module
    }
    assert "hud_simulator" not in imported_modules
    assert "dev_endpoints" not in imported_modules
    assert "agent_setup_payloads" in imported_modules


@pytest.mark.parametrize("path", FIRE_ROUTES)
def test_fire_routes_report_disabled_hud_instead_of_404(monkeypatch, path):
    monkeypatch.delenv("WHISPERSTREAM_DEV_TOOLS", raising=False)
    server = _make_server(claude_hud_enabled=False)
    server._handle_claude_hook_body = AsyncMock()
    with _client(server) as client:
        response = client.post(path)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["detail"]
    server._handle_claude_hook_body.assert_not_awaited()


@pytest.mark.parametrize(
    ("path", "payloads"),
    [
        (FIRE_ROUTES[0], setup_question_payloads()),
        (FIRE_ROUTES[1], setup_response_payloads()),
    ],
)
def test_fire_routes_return_immediately_when_enabled(path, payloads):
    server = _make_server(claude_hud_enabled=True)
    server._handle_claude_hook_body = AsyncMock()
    with _client(server) as client:
        response = client.post(path)
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        # A later request lets the background driver run; the route itself
        # never awaited the (deliberately blocking) hook handler.
        client.get("/api/health")

    assert server._handle_claude_hook_body.await_count <= len(payloads)


def test_teardown_is_idempotent_with_nothing_live():
    server = _make_server()
    server.broadcast = MagicMock()
    with _client(server) as client:
        first = client.post(TEARDOWN_ROUTE)
        second = client.post(TEARDOWN_ROUTE)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"ok": True, "closed_interactions": 0}
    assert second.json() == first.json()


def test_teardown_releases_every_live_surface():
    server = _make_server()
    server.broadcast = MagicMock()
    server._get_reply_desk().begin_capture("conv:x", 7, "toggle")
    server._cancel_routed_compose = MagicMock()
    question = MagicMock()
    response_coordinator = MagicMock()
    server._claude_question_coordinator = question
    server._claude_response_coordinator = response_coordinator
    broker = MagicMock()
    broker.snapshot.return_value = [SimpleNamespace(request_id="req-1")]
    broker.close.return_value = object()
    server._claude_interactions = broker

    with _client(server) as client:
        result = client.post(TEARDOWN_ROUTE)

    assert result.status_code == 200
    assert result.json() == {"ok": True, "closed_interactions": 1}
    server._cancel_routed_compose.assert_called_once_with(
        7, "agent_setup_teardown"
    )
    question.expire_current.assert_called_once_with()
    response_coordinator.dismiss_responses.assert_called_once_with()
    broker.close.assert_called_once_with("req-1")


def test_teardown_never_raises_when_a_release_fails():
    server = _make_server()
    server.broadcast = MagicMock(side_effect=RuntimeError("no clients"))
    server._get_reply_desk().begin_capture("conv:x", 3, "toggle")
    server._cancel_routed_compose = MagicMock(side_effect=RuntimeError("boom"))
    server._claude_question_coordinator = MagicMock(
        **{"expire_current.side_effect": RuntimeError("boom")}
    )
    server._claude_response_coordinator = MagicMock(
        **{"dismiss_responses.side_effect": RuntimeError("boom")}
    )
    server._claude_interactions = MagicMock(
        **{"snapshot.side_effect": RuntimeError("boom")}
    )

    with _client(server) as client:
        result = client.post(TEARDOWN_ROUTE)

    assert result.status_code == 200
    assert result.json() == {"ok": True, "closed_interactions": 0}


def test_setup_routes_log_no_payload_content():
    logger = MagicMock()
    server = _make_server(claude_hud_enabled=True, logger=logger)
    server._handle_claude_hook_body = AsyncMock()
    server.broadcast = MagicMock()
    with _client(server) as client:
        for path in (*FIRE_ROUTES, TEARDOWN_ROUTE):
            client.post(path)

    logged = " ".join(
        str(arg)
        for call in logger.mock_calls
        for arg in call.args
    )
    forbidden = [
        "This is a practice question",
        "A quieter voice",
        "A louder voice",
        "whisperstream:speak",
        "last_assistant_message",
    ]
    for fragment in forbidden:
        assert fragment not in logged


def test_delivery_preview_validates_payload():
    server = _make_server()
    with _client(server) as client:
        # Invalid JSON / non-dict
        res = client.post(PREVIEW_ROUTE, content="not json", headers={"Content-Type": "application/json"})
        assert res.status_code == 400
        assert res.json()["ok"] is False

        # Missing fields
        res = client.post(PREVIEW_ROUTE, json={})
        assert res.status_code == 400
        assert res.json()["ok"] is False

        # Non-boolean fields
        res = client.post(PREVIEW_ROUTE, json={"show_summary_text": "true", "agent_speech_enabled": False})
        assert res.status_code == 400
        assert res.json()["ok"] is False

        res = client.post(PREVIEW_ROUTE, json={"show_summary_text": True, "agent_speech_enabled": 1})
        assert res.status_code == 400
        assert res.json()["ok"] is False

        # Both false (requires at least one mode)
        res = client.post(PREVIEW_ROUTE, json={"show_summary_text": False, "agent_speech_enabled": False})
        assert res.status_code == 400
        assert res.json()["ok"] is False


def test_delivery_preview_arms_and_overrides_until_teardown():
    server = _make_server(claude_hud_enabled=True)
    server.broadcast = MagicMock()
    server._app.config.agent_summary_text_enabled = False
    server._app.config.agent_speech_enabled = False

    # Initially false
    assert server._effective_agent_summary_text_enabled() is False
    assert server._effective_agent_speech_enabled() is False
    snapshot = server._agent_response_queue_snapshot_event()
    assert snapshot["show_summary_text"] is False

    with _client(server) as client:
        # Arm summary only
        res = client.post(PREVIEW_ROUTE, json={"show_summary_text": True, "agent_speech_enabled": False})
        assert res.status_code == 200
        assert res.json() == {"ok": True, "agent_summary_text_enabled": True, "agent_speech_enabled": False}

        assert server._effective_agent_summary_text_enabled() is True
        assert server._effective_agent_speech_enabled() is False
        # Persisted config untouched
        assert server._app.config.agent_summary_text_enabled is False
        assert server._app.config.agent_speech_enabled is False

        snapshot = server._agent_response_queue_snapshot_event()
        assert snapshot["show_summary_text"] is True

        # Broadcast was triggered
        server.broadcast.assert_called()

        # Teardown clears preview
        td_res = client.post(TEARDOWN_ROUTE)
        assert td_res.status_code == 200

        assert server._effective_agent_summary_text_enabled() is False
        assert server._effective_agent_speech_enabled() is False
        snapshot = server._agent_response_queue_snapshot_event()
        assert snapshot["show_summary_text"] is False


def test_delivery_preview_speech_eligibility_override():
    server = _make_server(claude_hud_enabled=True)
    server.broadcast = MagicMock()
    server._app.config.agent_summary_text_enabled = False
    server._app.config.agent_speech_enabled = False

    controller = MagicMock()
    controller.can_speak.return_value = True
    controller.speak_spoken_summary.return_value = True
    server._app.speech_controller = controller

    coordinator = MagicMock()
    coordinator.enqueue = MagicMock(side_effect=lambda **kwargs: kwargs["speech_start"](None))
    server._get_agent_response_coordinator = MagicMock(return_value=coordinator)

    with _client(server) as client:
        # Without preview, speech does not start because persisted config is false
        server._queue_agent_response(
            agent="claude",
            result={"spoken_summary": "Here is the summary."},
            completion_id="c-1",
            completion_sequence=1,
        )
        controller.speak_spoken_summary.assert_not_called()

        # Arm speech in preview
        res = client.post(PREVIEW_ROUTE, json={"show_summary_text": False, "agent_speech_enabled": True})
        assert res.status_code == 200

        server._queue_agent_response(
            agent="claude",
            result={"spoken_summary": "Here is the summary."},
            completion_id="c-2",
            completion_sequence=2,
        )
        controller.speak_spoken_summary.assert_called_once()
        args, kwargs = controller.speak_spoken_summary.call_args
        assert args[0] == "Here is the summary."
        assert kwargs["completion_id"] == "c-2"


def test_clear_agent_speak_state_and_config_save_clears_preview():
    server = _make_server(claude_hud_enabled=True)
    server.broadcast = MagicMock()
    server._app.config.agent_summary_text_enabled = False
    server._app.config.agent_speech_enabled = False

    with _client(server) as client:
        res = client.post(PREVIEW_ROUTE, json={"show_summary_text": True, "agent_speech_enabled": True})
        assert res.status_code == 200
        assert server._effective_agent_summary_text_enabled() is True

        # Clear state (e.g. lockdown) clears preview
        server._clear_agent_speak_state(reason="lockdown_entered")
        assert server._effective_agent_summary_text_enabled() is False
        assert server._effective_agent_speech_enabled() is False

