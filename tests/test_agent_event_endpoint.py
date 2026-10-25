"""Tests for the agent-event endpoint and completion WebSocket contract.

Covers: the ``/api/claude-event`` alias, rejection of non-Claude provider
discriminators, dispatch of normalized activity/error/progress kinds,
prompt_submitted -> working + speech focus without playback cancellation, speech
focus driving the speak-vs-chime split, visual completion retention, and
authoritative response queue snapshots.

Uses FastAPI's TestClient with a mock WhisperstreamApp (mirrors
test_speech_trigger.py). No real sherpa-onnx or sounddevice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.usefixtures("accepted_eula")


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_speech_trigger.py)
# ---------------------------------------------------------------------------


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "none"
    cfg.enhancement_local_model = "gemma4-e2b-instruct"
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.claude_hud_enabled = True
    cfg.claude_hud_show_text = True
    cfg.lockdown_mode = False
    cfg.agent_speech_enabled = True
    cfg.agent_summary_text_enabled = False
    cfg.agent_speech_activation = "always_on"
    cfg.agent_speech_mode = "verbatim"
    cfg.start_sound_path = None
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.to_dict = lambda: {}
    return cfg


def _make_mock_app(*, config=None, speech_controller=None) -> MagicMock:
    app = MagicMock()
    app.config = config or _make_mock_config()
    app.stt_engine = MagicMock()
    app.llama_server = None
    app.config_manager = MagicMock()
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True, "trial_remaining_seconds": 0}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.set_event_broadcaster = MagicMock()
    app.audio_feedback = MagicMock()
    app.speech_controller = speech_controller
    return app


def _make_client(*, config=None, speech_controller=None) -> tuple[TestClient, Any]:
    from whisperstream.server import WhisperstreamServer

    mock_app = _make_mock_app(config=config, speech_controller=speech_controller)
    server = WhisperstreamServer(mock_app, port=0, logger=None, register_hotkeys=False)
    client = TestClient(server.fastapi, raise_server_exceptions=True, headers={"x-whisperstream-hook-token": server._agent_hook_token})
    return client, server


def _ws_connect(client: TestClient, *, origin: str = "http://testserver"):
    return client.websocket_connect("/ws", headers={"origin": origin})


def _make_controller(*, can_speak=True) -> MagicMock:
    def accept_speech(*_args, on_terminal, **_kwargs):
        on_terminal("completed")
        return True

    controller = MagicMock()
    controller.can_speak.return_value = can_speak
    controller.speak_response = AsyncMock(return_value=None)
    controller.speak_spoken_summary = MagicMock(side_effect=accept_speech)
    return controller


STOP_CLAUDE = {
    "hook_event_name": "Stop",
    "last_assistant_message": "All tests pass.\n🔊 All tests pass.",
    "session_id": "claude-1",
    "prompt_id": "prompt-1",
    "cwd": r"C:\Users\USERNAME\Documents\Dev\whisperstream",
}
WORKING_NOTIFICATION = {
    "hook_event_name": "Notification",
    "notification_type": "permission_prompt",
    "session_id": "claude-2",
}
PROMPT_SUBMIT = {
    "hook_event_name": "UserPromptSubmit",
    "prompt": "do the thing",
    "session_id": "claude-1",
}
SUBAGENT_STOP = {
    "hook_event_name": "SubagentStop",
    "session_id": "claude-1",
}
AGENT_EVENT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_events"


def _load_agent_event_fixture(name: str) -> dict:
    return json.loads((AGENT_EVENT_FIXTURES / name).read_text(encoding="utf-8"))


def _completion_events(broadcasts: list[dict]) -> list[dict]:
    return [event for event in broadcasts if event.get("type") == "agent.completion"]


def _legacy_response_events(broadcasts: list[dict]) -> list[dict]:
    return [event for event in broadcasts if event.get("type") == "claude.response"]


def _response_queue_events(broadcasts: list[dict], server) -> list[dict]:
    coordinator = server._agent_response_coordinator or server._claude_response_coordinator
    worker = coordinator._publisher._thread
    if worker is not None:
        worker.join(5)
        assert not worker.is_alive()
    return [
        event
        for event in broadcasts
        if event.get("type") == "agent.response_queue.snapshot"
    ]


def _assert_spoken_summary_scheduled(
    controller: MagicMock,
    summary: str,
    *,
    completion_id: str,
) -> Any:
    controller.speak_spoken_summary.assert_called_once()
    args, kwargs = controller.speak_spoken_summary.call_args
    assert args == (summary,)
    assert kwargs["completion_id"] == completion_id
    assert callable(kwargs["on_terminal"])
    return kwargs["on_terminal"]


# ---------------------------------------------------------------------------
# Alias + discriminator
# ---------------------------------------------------------------------------


class TestAliasAndDiscriminator:
    @pytest.mark.parametrize("path", ["/api/agent-event", "/api/claude-event"])
    def test_missing_or_wrong_token_is_inert_before_parsing(self, path):
        client, server = _make_client()
        server._handle_agent_event = AsyncMock()
        client.headers.pop("x-whisperstream-hook-token")
        for headers in ({}, {"x-whisperstream-hook-token": "0" * 64}):
            assert client.post(path, content=b"not json", headers=headers).json() == {"ok": True}
        server._handle_agent_event.assert_not_called()
        client.close()

    def test_claude_event_alias_still_works(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post("/api/claude-event", json=STOP_CLAUDE)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert any(
            b["type"] == "agent.completion"
            and b["completion"]["agent"] == "claude"
            for b in broadcasts
        )
        _assert_spoken_summary_scheduled(
            controller,
            "All tests pass.",
            completion_id=_completion_events(broadcasts)[0]["completion"]["completion_id"],
        )
        controller.speak_response.assert_not_called()

    def test_agent_event_defaults_to_claude(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post("/api/agent-event", json=STOP_CLAUDE)

        assert resp.status_code == 200
        assert any(
            b.get("type") == "agent.completion"
            and b["completion"].get("agent") == "claude"
            for b in broadcasts
        )

    def test_non_claude_query_discriminator_is_inert(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post("/api/agent-event?agent=other", json=STOP_CLAUDE)

        assert resp.status_code == 200
        assert broadcasts == []
        controller.speak_spoken_summary.assert_not_called()
        controller.speak_response.assert_not_called()

    def test_explicit_claude_agent_in_body_is_accepted(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post("/api/agent-event", json={**STOP_CLAUDE, "agent": "claude"})

        assert resp.status_code == 200
        assert any(
            b.get("type") == "agent.completion"
            and b["completion"].get("agent") == "claude"
            for b in broadcasts
        )

    def test_unknown_agent_ignored(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post("/api/agent-event?agent=gemini", json=STOP_CLAUDE)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert broadcasts == []
        controller.speak_response.assert_not_called()

    def test_config_off_returns_ok_inert(self, monkeypatch):
        """The route is always registered (the experimental gate was removed in
        1.3.0), but ``claude_hud_enabled=False`` (the shipped default) keeps it
        completely inert: plain 200 {"ok": True}, nothing broadcast."""
        monkeypatch.delenv("WHISPERSTREAM_EXPERIMENTAL", raising=False)
        controller = _make_controller()
        client, server = _make_client(
            config=_make_mock_config(claude_hud_enabled=False),
            speech_controller=controller,
        )
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post("/api/agent-event", json=STOP_CLAUDE)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert broadcasts == []
        controller.speak_response.assert_not_called()
        controller.speak_spoken_summary.assert_not_called()

    def test_effective_lockdown_returns_ok_without_recreating_agent_state(
        self, monkeypatch
    ):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(
            config=_make_mock_config(lockdown_mode=True),
            speech_controller=controller,
        )
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        response = client.post("/api/agent-event", json=STOP_CLAUDE)

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert broadcasts == []
        assert server._get_agent_activity() is None
        assert server._agent_activity is None
        assert server._agent_speech_focus is None
        assert server._claude_activation_registry is None
        assert server._claude_response_coordinator is None
        assert server._claude_question_coordinator is None
        assert server._claude_interactions is None
        assert server._agent_session_handles == {}
        controller.stop.assert_not_called()
        controller.speak_response.assert_not_called()
        controller.speak_spoken_summary.assert_not_called()


# ---------------------------------------------------------------------------
# New kind dispatch
# ---------------------------------------------------------------------------


class TestKindDispatch:
    def test_exact_plugin_activation_enables_only_its_passive_session(
        self, monkeypatch
    ):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(
            config=_make_mock_config(agent_speech_activation="per_session"),
            speech_controller=controller,
        )
        server.broadcast = MagicMock()
        activation = _load_agent_event_fixture(
            "claude_speech_activation.json"
        )
        session_id = activation["session_id"]

        assert server._claude_speech_is_active(session_id) is False

        assert client.post(
            "/api/claude-event", json=activation
        ).status_code == 200
        assert client.post(
            "/api/claude-event",
            json={**PROMPT_SUBMIT, "session_id": session_id},
        ).status_code == 200
        assert client.post(
            "/api/claude-event",
            json={
                **STOP_CLAUDE,
                "session_id": session_id,
                "prompt_id": "fixture-activated-prompt",
            },
        ).status_code == 200

        _assert_spoken_summary_scheduled(
            controller,
            "All tests pass.",
            completion_id=server._agent_activity.snapshot()[-1].completion_id,
        )
        assert client.post(
            "/api/claude-event",
            json={
                **STOP_CLAUDE,
                "session_id": "inactive-session",
                "prompt_id": "inactive-prompt",
            },
        ).status_code == 200
        controller.speak_spoken_summary.assert_called_once()
        queue = server._claude_response_coordinator.snapshot()
        assert queue["phase"] == "active_grace"
        assert len(queue["ordered_completion_ids"]) == 2

    def test_notification_is_non_authoritative_working_activity(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post("/api/claude-event", json=WORKING_NOTIFICATION)

        assert resp.status_code == 200
        assert {
            "type": "agent.hud.activity",
            "version": 1,
            "active": True,
        } in broadcasts
        controller.speak_event.assert_not_called()
        controller.speak_response.assert_not_called()

    def test_notification_working_does_not_override_answer_authority(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        coordinator = server._get_claude_question_coordinator()
        coordinator.surface(
            "opaque-question-session",
            [{"question": "Continue?", "options": [{"label": "Yes"}]}],
        )
        before = server._agent_question_snapshot_event()

        resp = client.post("/api/claude-event", json=WORKING_NOTIFICATION)
        assert resp.status_code == 200
        assert server._agent_question_snapshot_event() == before
        controller.speak_event.assert_not_called()

    def test_progress_routed_with_detail(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post("/api/claude-event", json=SUBAGENT_STOP)

        assert resp.status_code == 200
        # Progress NEVER fires the HUD.
        assert broadcasts == []
        controller.speak_event.assert_called_once_with(
            "progress", "claude", "subagent_done"
        )

    @pytest.mark.parametrize(
        ("route", "body", "agent", "session_id"),
        [
            ("/api/claude-event", PROMPT_SUBMIT, "claude", "claude-1"),
            (
                "/api/agent-event?agent=codex",
                _load_agent_event_fixture("codex_prompt_submit.json"),
                "codex",
                "fixture-codex-session",
            ),
        ],
    )
    def test_prompt_submitted_preserves_playback_and_sets_speech_focus(
        self,
        monkeypatch,
        route,
        body,
        agent,
        session_id,
    ):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(
            config=_make_mock_config(codex_hud_enabled=True),
            speech_controller=controller,
        )
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post(route, json=body)

        assert resp.status_code == 200
        controller.stop.assert_not_called()
        activity = [event for event in broadcasts if event["type"] == "agent.hud.activity"]
        assert activity == [{
            "type": "agent.hud.activity",
            "version": 1,
            "active": True,
            **({"agent": agent} if agent != "claude" else {}),
        }]
        history = [event for event in broadcasts if event["type"] == "agent.conversation.snapshot"]
        assert len(history) == 1
        assert history[0]["sessions"] == [{
            "session_handle": server._agent_session_handle(session_id, agent),
            "activity": "working", "turns": [],
        }]
        assert server._agent_speech_focus is not None
        assert server._agent_speech_focus.should_speak(agent, session_id) is True
        assert server._agent_speech_focus.should_speak(agent, "other") is False

    @pytest.mark.parametrize(
        ("route", "body"),
        [
            ("/api/claude-event", {**PROMPT_SUBMIT, "agent_id": "sub-1"}),
            (
                "/api/agent-event?agent=codex",
                {**_load_agent_event_fixture("codex_prompt_submit.json"), "transcript_path": None},
            ),
        ],
    )
    def test_noninteractive_sessions_are_inert(self, monkeypatch, route, body):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(
            config=_make_mock_config(codex_hud_enabled=True),
            speech_controller=controller,
        )
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        resp = client.post(route, json=body)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert broadcasts == []
        assert server._agent_speech_focus is None

    def test_no_controller_never_raises_on_new_kinds(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, server = _make_client(speech_controller=None)
        server.broadcast = MagicMock()

        for body in (WORKING_NOTIFICATION, SUBAGENT_STOP, PROMPT_SUBMIT):
            resp = client.post("/api/claude-event", json=body)
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Speech focus drives the speak-vs-chime split
# ---------------------------------------------------------------------------


class TestSpeechFocusOnResponses:
    def test_inactive_session_response_chimes_instead_of_speaking(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(
            config=_make_mock_config(agent_speech_activation="per_session"),
            speech_controller=controller,
        )
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        # claude-1 becomes the focused source for routine speech.
        client.post("/api/claude-event", json=PROMPT_SUBMIT)
        controller.stop.reset_mock()

        # A DIFFERENT session's response: HUD still fires, speech does not,
        # the chime takes over (Phase 1 behavior for non-spoken responses).
        other = {**STOP_CLAUDE, "session_id": "claude-2"}
        resp = client.post("/api/claude-event", json=other)

        assert resp.status_code == 200
        assert _completion_events(broadcasts)
        controller.speak_response.assert_not_called()
        server._app.audio_feedback.play_start.assert_called_once()

    def test_active_session_response_speaks(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        server.broadcast = MagicMock()

        client.post("/api/claude-event", json=PROMPT_SUBMIT)
        resp = client.post("/api/claude-event", json=STOP_CLAUDE)

        assert resp.status_code == 200
        _assert_spoken_summary_scheduled(
            controller,
            "All tests pass.",
            completion_id=server._agent_activity.snapshot()[0].completion_id,
        )
        controller.speak_response.assert_not_called()
        server._app.audio_feedback.play_start.assert_not_called()

    def test_error_respects_speech_focus(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        server.broadcast = MagicMock()

        client.post("/api/claude-event", json=PROMPT_SUBMIT)

        error_other = {
            "hook_event_name": "Notification",
            "notification_type": "error",
            "session_id": "claude-2",
        }
        resp = client.post("/api/claude-event", json=error_other)
        assert resp.status_code == 200
        controller.speak_event.assert_not_called()

        error_active = {**error_other, "session_id": "claude-1"}
        resp = client.post("/api/claude-event", json=error_active)
        assert resp.status_code == 200
        controller.speak_event.assert_called_once_with("error", "claude")

    def test_sessionless_unmarked_response_does_not_speak(self, monkeypatch):
        """Old forwarder payloads stay visible but never become speech."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        server.broadcast = MagicMock()

        client.post("/api/claude-event", json=PROMPT_SUBMIT)
        sessionless = {
            "hook_event_name": "Stop",
            "last_assistant_message": "Done.",
        }
        resp = client.post("/api/claude-event", json=sessionless)

        assert resp.status_code == 200
        controller.speak_spoken_summary.assert_not_called()
        controller.speak_response.assert_not_called()


# ---------------------------------------------------------------------------
# Versioned visual completion delivery
# ---------------------------------------------------------------------------


class TestCompletionDelivery:
    def test_visible_response_is_enqueued_before_upsert_without_legacy_event(
        self, monkeypatch
    ):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        broadcasts: list[dict] = []

        assert server._agent_activity is None

        def capture(event: dict) -> None:
            if event.get("type") == "agent.completion":
                pending_ids = [
                    item.completion_id for item in server._agent_activity.snapshot()
                ]
                assert event["pending_completion_ids"] == pending_ids
            broadcasts.append(event)

        server.broadcast = capture
        response = client.post("/api/claude-event", json=STOP_CLAUDE)

        assert response.status_code == 200
        assert server._agent_activity is not None
        (upsert,) = _completion_events(broadcasts)
        completion = upsert["completion"]
        assert upsert["version"] == 1
        assert upsert["show_text"] is True
        assert completion["agent"] == "claude"
        assert "session_id" not in completion
        assert "source_key" not in completion
        assert completion["source_label"] == "Claude · whisperstream"
        assert completion["text"] == STOP_CLAUDE["last_assistant_message"]
        assert "cwd" not in completion
        assert "prompt_id" not in completion

        _assert_spoken_summary_scheduled(
            controller,
            "All tests pass.",
            completion_id=completion["completion_id"],
        )
        controller.speak_response.assert_not_called()

        assert _legacy_response_events(broadcasts) == []

    def test_provider_retry_does_not_double_broadcast_or_speak(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        first = client.post("/api/claude-event", json=STOP_CLAUDE)
        assert first.status_code == 200
        broadcasts.clear()
        controller.speak_response.reset_mock()
        server._app.audio_feedback.play_start.reset_mock()

        retry = client.post("/api/claude-event", json=dict(STOP_CLAUDE))

        assert retry.status_code == 200
        assert _completion_events(broadcasts) == []
        assert _legacy_response_events(broadcasts) == []
        controller.speak_spoken_summary.assert_called_once()
        controller.speak_response.assert_not_called()
        server._app.audio_feedback.play_start.assert_not_called()

    def test_distinct_stops_in_one_prompt_both_publish_and_schedule_speech(
        self, monkeypatch
    ):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(
            config=_make_mock_config(claude_reply_grace_seconds=0),
            speech_controller=controller,
        )
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        coordinator = MagicMock()

        def start_speech(**kwargs):
            assert kwargs["speech_start"](lambda _reason: None) is True

        coordinator.enqueue.side_effect = start_speech
        server._claude_response_coordinator = coordinator
        first_stop = {
            **STOP_CLAUDE,
            "last_assistant_message": "First response.\n🔊 First response.",
        }
        second_stop = {
            **STOP_CLAUDE,
            "last_assistant_message": "Second response.\n🔊 Second response.",
            "stop_hook_active": True,
        }

        assert client.post("/api/claude-hook", json=first_stop).json() == {}
        assert client.post("/api/claude-hook", json=second_stop).json() == {}

        completions = [
            event["completion"] for event in _completion_events(broadcasts)
        ]
        assert [item["text"] for item in completions] == [
            first_stop["last_assistant_message"],
            second_stop["last_assistant_message"],
        ]
        assert [item["sequence"] for item in completions] == [1, 2]
        assert coordinator.enqueue.call_count == 2
        assert [
            call.kwargs["completion_id"] for call in coordinator.enqueue.call_args_list
        ] == [item["completion_id"] for item in completions]
        assert [
            call.args[0] for call in controller.speak_spoken_summary.call_args_list
        ] == ["First response.", "Second response."]

    def test_hidden_response_retains_only_bounded_dedupe_identity(
        self, monkeypatch
    ):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        config = _make_mock_config(claude_hud_show_text=False)
        controller = _make_controller()
        client, server = _make_client(config=config, speech_controller=controller)
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        response = client.post("/api/claude-event", json=STOP_CLAUDE)

        assert response.status_code == 200
        assert server._agent_activity is not None
        assert server._agent_activity.snapshot() == ()
        assert server._agent_activity.stats()["recent_dedup_identities"] == 1
        assert _completion_events(broadcasts) == []
        assert _legacy_response_events(broadcasts) == []
        assert STOP_CLAUDE["last_assistant_message"] not in json.dumps(broadcasts)
        queue_event = _response_queue_events(broadcasts, server)[-1]
        active_id = queue_event["active"]["completion_id"]
        _assert_spoken_summary_scheduled(
            controller,
            "All tests pass.",
            completion_id=active_id,
        )
        controller.speak_response.assert_not_called()

    def test_response_evicted_by_limits_keeps_opaque_lifecycle_identity(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        from whisperstream.agent_activity import (
            OVERFLOW_COPY_TEMPLATE,
            AgentActivityStore,
        )

        controller = _make_controller()
        client, server = _make_client(speech_controller=controller)
        ids = iter(("oversized", "overflow"))
        server._agent_activity = AgentActivityStore(
            max_items=2,
            max_response_chars=len(OVERFLOW_COPY_TEMPLATE.format(count=1)),
            id_factory=lambda: next(ids),
        )
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append
        oversized = {
            **STOP_CLAUDE,
            "last_assistant_message": "A response too large for test retention.",
        }

        response = client.post("/api/claude-event", json=oversized)

        assert response.status_code == 200
        completion_events = _completion_events(broadcasts)
        changed = [event["completion"] for event in completion_events]
        assert [(item["completion_id"], item["kind"]) for item in changed] == [
            ("overflow", "overflow")
        ]
        assert completion_events[0]["pending_completion_ids"] == ["overflow"]
        assert _legacy_response_events(broadcasts) == []
        lifecycle = _response_queue_events(broadcasts, server)[-1]
        synthetic_id = lifecycle["active"]["completion_id"]
        assert isinstance(synthetic_id, str) and synthetic_id
        assert synthetic_id != "overflow"
        assert lifecycle["completions"] == []
        controller.speak_spoken_summary.assert_not_called()
        controller.speak_response.assert_not_called()
        server._app.audio_feedback.play_start.assert_called_once()

    def test_all_sources_are_visible_independent_of_speech_focus(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        controller = _make_controller()
        client, server = _make_client(
            config=_make_mock_config(agent_speech_activation="per_session"),
            speech_controller=controller,
        )
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        client.post("/api/claude-event", json=PROMPT_SUBMIT)
        controller.stop.reset_mock()

        background_claude = {
            **STOP_CLAUDE,
            "session_id": "claude-2",
            "prompt_id": "prompt-2",
        }
        second_claude = {
            **STOP_CLAUDE,
            "session_id": "claude-3",
            "prompt_id": "prompt-3",
            "last_assistant_message": "Second background response.",
        }
        assert client.post("/api/claude-event", json=background_claude).status_code == 200
        assert client.post("/api/claude-event", json=second_claude).status_code == 200

        completions = [event["completion"] for event in _completion_events(broadcasts)]
        assert [item["agent"] for item in completions] == ["claude", "claude"]
        assert completions[0]["source_label"] == "Claude · whisperstream"
        assert completions[1]["source_label"].startswith(
            "Claude · whisperstream · "
        )
        assert all("session_id" not in item and "source_key" not in item for item in completions)
        assert [item["sequence"] for item in completions] == [1, 2]
        controller.speak_response.assert_not_called()
        # Only the promoted head chimes. The second response remains queued
        # until the first grace and quiet delay finish.
        assert server._app.audio_feedback.play_start.call_count == 1
        lifecycle = _response_queue_events(broadcasts, server)[-1]
        assert lifecycle["phase"] == "active_grace"
        assert lifecycle["ordered_completion_ids"] == [
            item["completion_id"] for item in completions
        ]

    def test_broadcasts_only_post_limit_enqueue_changes(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        from whisperstream.agent_activity import AgentActivityStore

        client, server = _make_client(speech_controller=None)
        ids = iter(("first", "second", "third", "overflow"))
        server._agent_activity = AgentActivityStore(
            max_items=2,
            max_response_chars=1_000,
            id_factory=lambda: next(ids),
        )
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append

        client.post("/api/claude-event", json=STOP_CLAUDE)
        client.post(
            "/api/claude-event",
            json={
                **STOP_CLAUDE,
                "last_assistant_message": "second",
                "prompt_id": "prompt-2",
            },
        )
        broadcasts.clear()

        response = client.post(
            "/api/claude-event",
            json={
                **STOP_CLAUDE,
                "last_assistant_message": "third",
                "prompt_id": "prompt-3",
            },
        )

        assert response.status_code == 200
        completion_events = _completion_events(broadcasts)
        changed = [event["completion"] for event in completion_events]
        assert [(item["completion_id"], item["kind"]) for item in changed] == [
            ("overflow", "overflow"),
            ("third", "response"),
        ]
        authoritative_pending_ids = [
            item.completion_id for item in server._agent_activity.snapshot()
        ]
        assert all(
            event["pending_completion_ids"] == authoritative_pending_ids
            for event in completion_events
        )
        assert all(item["completion_id"] != "second" for item in changed)
        assert "first" not in authoritative_pending_ids
        assert _legacy_response_events(broadcasts) == []
        assert "third" in _response_queue_events(broadcasts, server)[-1][
            "ordered_completion_ids"
        ]

    def test_store_failure_is_always_200_and_logs_no_payload_metadata(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, server = _make_client(speech_controller=None)
        server._get_agent_activity = MagicMock(side_effect=RuntimeError("store failed"))
        caplog.set_level(logging.DEBUG)
        body = {
            **STOP_CLAUDE,
            "last_assistant_message": "PRIVATE_RESPONSE_SENTINEL",
            "session_id": "PRIVATE_SESSION_SENTINEL",
            "prompt_id": "PRIVATE_PROMPT_SENTINEL",
            "cwd": r"C:\PRIVATE_PATH_SENTINEL\secret-project",
        }

        response = client.post("/api/claude-event", json=body)

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        for sentinel in (
            "PRIVATE_RESPONSE_SENTINEL",
            "PRIVATE_SESSION_SENTINEL",
            "PRIVATE_PROMPT_SENTINEL",
            "PRIVATE_PATH_SENTINEL",
            "secret-project",
        ):
            assert sentinel not in caplog.text


class TestCrossProcessMultisessionRegression:
    def test_two_synthetic_sources_survive_normalize_queue_replay_and_retry_dedupe(
        self, monkeypatch, caplog
    ):
        """Reproduce the rapid JSON/overwrite report across the backend seams."""
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        caplog.set_level(logging.DEBUG)
        controller = _make_controller(can_speak=False)
        client, server = _make_client(speech_controller=controller)
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append
        claude_stop = _load_agent_event_fixture("claude_stop.json")
        second_claude_stop = {
            **claude_stop,
            "last_assistant_message": "SECOND_CLAUDE_REPLY",
            "session_id": "fixture-claude-session-two",
            "prompt_id": "fixture-claude-prompt-two",
        }

        assert client.post("/api/claude-event", json=claude_stop).status_code == 200
        assert client.post(
            "/api/claude-event", json=second_claude_stop
        ).status_code == 200

        completions = [event["completion"] for event in _completion_events(broadcasts)]
        completion_events = _completion_events(broadcasts)
        assert [item["sequence"] for item in completions] == [1, 2]
        assert completion_events[0]["pending_completion_ids"] == [
            completions[0]["completion_id"]
        ]
        assert completion_events[1]["pending_completion_ids"] == [
            completions[0]["completion_id"],
            completions[1]["completion_id"],
        ]
        assert all(
            "session_id" not in item and "source_key" not in item
            for item in completions
        )
        assert [
            (item["agent"], item["text"], item["source_label"])
            for item in completions
        ] == [
            (
                "claude",
                '{"fixture":"LEGITIMATE_JSON_LITERAL"}',
                "Claude \u00b7 synthetic-project",
            ),
            (
                "claude",
                "SECOND_CLAUDE_REPLY",
                completions[1]["source_label"],
            ),
        ]
        assert completions[1]["source_label"].startswith(
            "Claude \u00b7 synthetic-project \u00b7 "
        )

        with _ws_connect(client) as first_ws:
            assert first_ws.receive_json()["type"] == "license.status"
            assert first_ws.receive_json()["type"] == "agent.completions.snapshot"
            first_snapshot = first_ws.receive_json()
            assert first_snapshot["type"] == "agent.response_queue.snapshot"
            assert first_snapshot["phase"] == "active_grace"
            assert first_snapshot["ordered_completion_ids"] == [
                item["completion_id"] for item in completions
            ]
            assert [
                item["completion_id"] for item in first_snapshot["completions"]
            ] == [item["completion_id"] for item in completions]

        with _ws_connect(client) as second_ws:
            assert second_ws.receive_json()["type"] == "license.status"
            assert second_ws.receive_json()["type"] == "agent.completions.snapshot"
            second_snapshot = second_ws.receive_json()
        assert (
            second_snapshot["ordered_completion_ids"]
            == first_snapshot["ordered_completion_ids"]
        )
        assert second_snapshot["completions"] == first_snapshot["completions"]
        assert second_snapshot["active"]["completion_id"] == (
            first_snapshot["active"]["completion_id"]
        )

        # A provider retry must not append or reorder an in-flight completion.
        broadcasts.clear()
        controller.speak_response.reset_mock()
        server._app.audio_feedback.play_start.reset_mock()
        assert client.post(
            "/api/claude-event",
            json=dict(second_claude_stop),
        ).status_code == 200
        assert broadcasts == []
        assert [item.sequence for item in server._agent_activity.snapshot()] == [
            1,
            2,
        ]
        controller.speak_response.assert_not_called()
        server._app.audio_feedback.play_start.assert_not_called()

        with _ws_connect(client) as final_ws:
            assert final_ws.receive_json()["type"] == "license.status"
            assert final_ws.receive_json()["type"] == "agent.completions.snapshot"
            final_snapshot = final_ws.receive_json()
        assert (
            final_snapshot["ordered_completion_ids"]
            == first_snapshot["ordered_completion_ids"]
        )
        assert final_snapshot["completions"] == first_snapshot["completions"]

        for private_value in (
            claude_stop["last_assistant_message"],
            claude_stop["session_id"],
            claude_stop["prompt_id"],
            claude_stop["cwd"],
            claude_stop["transcript_path"],
            second_claude_stop["last_assistant_message"],
            second_claude_stop["session_id"],
            second_claude_stop["prompt_id"],
            "synthetic-project",
            completions[0]["completion_id"],
            completions[1]["completion_id"],
        ):
            assert private_value not in caplog.text


# ---------------------------------------------------------------------------
# WebSocket response-queue snapshots
# ---------------------------------------------------------------------------


class TestWebSocketOriginPolicy:
    @pytest.mark.parametrize(
        "origin",
        (
            "tauri://localhost",
            "http://tauri.localhost",
            "https://tauri.localhost",
            "http://localhost",
            "http://localhost:5173",
            "https://localhost:443",
            "http://127.0.0.1",
            "https://127.0.0.1:38420",
            "http://testserver",
        ),
    )
    def test_allows_tauri_loopback_and_testclient_origins(self, origin):
        client, server = _make_client(speech_controller=None)

        with _ws_connect(client, origin=origin) as ws:
            assert ws.receive_json()["type"] == "license.status"
            assert ws.receive_json()["type"] == "agent.completions.snapshot"

        assert server._socket_delivery.clients == set()

    @pytest.mark.parametrize(
        "origin",
        (
            None,
            "null",
            "https://foreign.example",
            "http://localhost.foreign.example",
            "http://127.0.0.1.foreign.example",
            "http://tauri.localhost:5173",
            "tauri://foreign.example",
            "http://localhost/path",
            "http://localhost:99999",
        ),
    )
    def test_rejects_missing_or_foreign_origins_before_accept(self, origin):
        client, server = _make_client(speech_controller=None)
        headers = {} if origin is None else {"origin": origin}

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws", headers=headers):
                pass

        assert exc_info.value.code == 1008
        assert server._socket_delivery.clients == set()

    def test_rejected_origin_value_is_not_logged(self, caplog):
        caplog.set_level(logging.WARNING)
        client, _server = _make_client(speech_controller=None)
        origin = "https://PRIVATE_ORIGIN_SENTINEL.example"

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": origin}):
                pass

        assert "PRIVATE_ORIGIN_SENTINEL" not in caplog.text
        assert "origin_policy" in caplog.text

    def test_disconnect_during_initial_send_always_discards_client(self):
        client, server = _make_client(speech_controller=None)
        endpoint = next(
            route.endpoint
            for route in server.fastapi.routes
            if getattr(route, "path", None) == "/ws"
        )

        class DisconnectingWebSocket:
            headers = {"origin": "http://testserver"}
            client = SimpleNamespace(host="testclient", port=12345)
            accepted = False

            async def accept(self):
                self.accepted = True

            async def send_text(self, _payload):
                raise WebSocketDisconnect(code=1001)

        ws = DisconnectingWebSocket()
        asyncio.run(endpoint(ws))

        assert ws.accepted is True
        assert server._socket_delivery.clients == set()


class TestCompletionWebSocket:
    @staticmethod
    def _receive_snapshot(ws) -> dict:
        assert ws.receive_json()["type"] == "license.status"
        assert ws.receive_json()["type"] == "agent.completions.snapshot"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "agent.response_queue.snapshot"
        return snapshot

    def test_connect_sends_empty_snapshot_without_constructing_store(self):
        client, server = _make_client(speech_controller=None)
        assert server._agent_activity is None

        with _ws_connect(client) as ws:
            snapshot = self._receive_snapshot(ws)
            assert snapshot == {
                "type": "agent.response_queue.snapshot",
                "version": 1,
                "revision": 0,
                "phase": "idle",
                "ordered_completion_ids": [],
                "completions": [],
                "active": None,
                "quiet_delay_remaining_seconds": 0.0,
                "show_summary_text": False,
            }

        assert server._agent_activity is None

    def test_connect_snapshot_reflects_agent_summary_text_enabled_true(self):
        config = _make_mock_config(agent_summary_text_enabled=True)
        client, server = _make_client(config=config, speech_controller=None)

        with _ws_connect(client) as ws:
            snapshot = self._receive_snapshot(ws)
            assert snapshot["show_summary_text"] is True

    def test_reconnect_replays_authoritative_fifo_in_order(self, monkeypatch):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        client, server = _make_client(speech_controller=None)
        assert client.post("/api/claude-event", json=STOP_CLAUDE).status_code == 200
        assert client.post(
            "/api/claude-event",
            json={
                **STOP_CLAUDE,
                "session_id": "claude-2",
                "prompt_id": "prompt-2",
                "last_assistant_message": "Second response.",
            },
        ).status_code == 200

        with _ws_connect(client) as first_ws:
            first_snapshot = self._receive_snapshot(first_ws)
            assert first_snapshot["version"] == 1
            assert first_snapshot["phase"] == "active_grace"
            assert first_snapshot["active"]["completion_id"] == (
                first_snapshot["ordered_completion_ids"][0]
            )
            assert [item["sequence"] for item in first_snapshot["completions"]] == [1, 2]
            assert all(
                "session_id" not in item and "source_key" not in item
                for item in first_snapshot["completions"]
            )

        with _ws_connect(client) as second_ws:
            second_snapshot = self._receive_snapshot(second_ws)

        assert second_snapshot["revision"] == first_snapshot["revision"]
        assert (
            second_snapshot["ordered_completion_ids"]
            == first_snapshot["ordered_completion_ids"]
        )
        assert second_snapshot["active"]["completion_id"] == (
            first_snapshot["active"]["completion_id"]
        )
        assert second_snapshot["active"]["generation"] == (
            first_snapshot["active"]["generation"]
        )
        assert second_snapshot["active"]["phase"] == (
            first_snapshot["active"]["phase"]
        )
        assert second_snapshot["active"]["passive_remaining_seconds"] <= (
            first_snapshot["active"]["passive_remaining_seconds"]
        )
        assert second_snapshot["completions"] == first_snapshot["completions"]

    def test_response_queue_snapshot_payloads_are_not_logged(self, monkeypatch, caplog):
        monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
        caplog.set_level(logging.DEBUG)
        client, server = _make_client(speech_controller=None)
        body = {
            **STOP_CLAUDE,
            "last_assistant_message": "LOG_PRIVATE_RESPONSE",
            "session_id": "LOG_PRIVATE_SESSION",
            "prompt_id": "LOG_PRIVATE_PROMPT",
            "cwd": r"C:\LOG_PRIVATE_PATH\private-project",
        }
        assert client.post("/api/claude-event", json=body).status_code == 200

        with _ws_connect(client) as ws:
            snapshot = self._receive_snapshot(ws)
            (completion,) = snapshot["completions"]

        for private_value in (
            "LOG_PRIVATE_RESPONSE",
            "LOG_PRIVATE_SESSION",
            "LOG_PRIVATE_PROMPT",
            "LOG_PRIVATE_PATH",
            "private-project",
            completion["completion_id"],
            completion["source_label"],
            repr(snapshot),
        ):
            assert private_value not in caplog.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
