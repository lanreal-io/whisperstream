"""Contract tests for the blocking Claude hook and console wire protocol."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from whisperstream.claude_interactions import (
    ENGAGED_CEILING_SECONDS,
    PASSIVE_DEADLINE_SECONDS,
    ClaudeInteractionBroker,
    ClaudeResponseCoordinator,
)
from whisperstream.config_manager import WhisperstreamConfig

pytestmark = pytest.mark.usefixtures("accepted_eula")

AGENT_EVENT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_events"


def _fixture(name: str) -> dict:
    return json.loads((AGENT_EVENT_FIXTURES / name).read_text(encoding="utf-8"))

STOP_CLAUDE = {
    "hook_event_name": "Stop",
    "last_assistant_message": "All tests pass.",
    "session_id": "claude-1",
    "prompt_id": "prompt-1",
    "cwd": r"C:\Users\USERNAME\Documents\Dev\whisperstream",
}
PROMPT_SUBMIT = {
    "hook_event_name": "UserPromptSubmit",
    "prompt": "do the thing",
    "session_id": "claude-1",
}


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "none"
    cfg.enhancement_local_model = "gemma4-e2b-instruct"
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.claude_hud_enabled = True
    cfg.claude_hud_show_text = True
    cfg.lockdown_mode = False
    cfg.agent_speech_enabled = False
    cfg.agent_summary_text_enabled = False
    cfg.agent_speech_activation = "per_session"
    cfg.claude_reply_grace_seconds = 6
    cfg.claude_reply_queue_delay_seconds = 2
    cfg.claude_reply_auto_submit = False
    cfg.claude_permission_decisions_enabled = False
    cfg.start_sound_path = None
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.to_dict = lambda: {}
    return cfg


def _make_controller() -> MagicMock:
    controller = MagicMock()
    controller.can_speak.return_value = True
    controller.speak_response = AsyncMock(return_value=None)
    return controller


def _make_client(*, config=None, speech_controller=None) -> tuple[TestClient, object]:
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.config = config or _make_mock_config()
    app.stt_engine = MagicMock()
    app.llama_server = None
    app.config_manager = MagicMock()
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.set_event_broadcaster = MagicMock()
    app.audio_feedback = MagicMock()
    app.speech_controller = speech_controller
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    return TestClient(server.fastapi, raise_server_exceptions=True, headers={"x-whisperstream-hook-token": server._agent_hook_token}), server


QUESTION = {
    "hook_event_name": "PreToolUse",
    "tool_name": "AskUserQuestion",
    "session_id": "claude-question",
    "tool_input": {
        "questions": [
            {
                "question": "Which release channel?",
                "header": "Channel",
                "options": [
                    {"label": "Stable", "description": "Ship broadly"},
                    {"label": "Beta", "description": "Test first"},
                ],
                "multiSelect": False,
            }
        ]
    },
}
QUESTION_ANSWERABLE = {
    **QUESTION,
    "hook_event_name": "PermissionRequest",
}

PERMISSION = {
    "hook_event_name": "PermissionRequest",
    "session_id": "claude-permission",
    "tool_name": "Bash",
    "tool_input": {"description": "Run the focused test suite"},
}


def _resolve_during_wait(server, decision):
    broker = server._get_claude_interactions()

    def wait(request_id: str):
        broker.resolve(request_id, decision)
        return broker.get(request_id)

    broker.wait = wait
    class ImmediateCoordinator:
        def enqueue(self, **kwargs):
            speech_start = kwargs.get("speech_start")
            if speech_start is not None:
                speech_start(lambda _reason: None)
            interaction = kwargs.get("interaction")
            waiter_loop = kwargs.get("waiter_loop")
            if interaction is None or waiter_loop is None:
                return None
            broker.activate_stop(interaction.request_id)
            broker.resolve(interaction.request_id, decision)
            future = waiter_loop.create_future()
            future.set_result(broker.get(interaction.request_id))
            return future

        @staticmethod
        def snapshot():
            return {
                "version": 1,
                "revision": 1,
                "phase": "idle",
                "ordered_completion_ids": [],
                "active": None,
                "quiet_delay_remaining_seconds": 0.0,
            }

    server._claude_response_coordinator = ImmediateCoordinator()


def _wait_for(predicate, *, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_gate_off_and_malformed_payload_fail_open():
    config = _make_mock_config(claude_hud_enabled=False)
    client, _ = _make_client(config=config)

    assert client.post("/api/claude-hook", json=STOP_CLAUDE).json() == {}

    config.claude_hud_enabled = True
    assert client.post(
        "/api/claude-hook",
        content=b"not-json",
        headers={"content-type": "application/json"},
    ).json() == {}


def test_effective_lockdown_fails_open_without_recreating_agent_state() -> None:
    config = _make_mock_config(lockdown_mode=True)
    client, server = _make_client(config=config)
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append

    try:
        response = client.post("/api/claude-hook", json=STOP_CLAUDE)
    finally:
        client.close()

    assert response.status_code == 200
    assert response.json() == {}
    assert broadcasts == []
    assert server._get_agent_activity() is None
    assert server._agent_activity is None
    assert server._agent_speech_focus is None
    assert server._claude_activation_registry is None
    assert server._claude_response_coordinator is None
    assert server._claude_question_coordinator is None
    assert server._claude_interactions is None
    assert server._agent_session_handles == {}


def test_hotkey_route_owns_exact_compose_lease_and_display_first_draft() -> None:
    client, server = _make_client()
    waiter_loop = asyncio.new_event_loop()
    try:
        app = server._app
        app.start_agent_compose.return_value = True
        broker = server._get_claude_interactions()
        interaction = broker.create(
            "opaque-session",
            "stop",
            raw_session_id="raw-session",
            passive_seconds=6,
            active=False,
        )
        coordinator = server._get_claude_response_coordinator()
        coordinator.enqueue(
            completion_id="completion",
            sequence=1,
            grace_seconds=6,
            speech_start=None,
            interaction=interaction,
            waiter_loop=waiter_loop,
        )

        # Nothing painted yet: the gesture is ordinary dictation.
        assert server.start_routed_reply("push_to_talk") is False
        desk = server._get_reply_desk()
        desk.present(seq=0, target_id="conv:opaque-session", surface="card", painted=True, focused=False)

        assert server.start_routed_reply("push_to_talk") is True
        lease = server._get_agent_compose_leases().current()
        assert lease is not None
        assert lease.session_handle == "opaque-session"
        assert desk.draft("conv:opaque-session").capture_mode == "push_to_talk"
        # Starting a reply engages the open Stop (its dwell moves to the ceiling).
        assert broker.get(interaction.request_id).engaged is True
        app.start_agent_compose.assert_called_once_with(lease.generation)
        assert server.stop_routed_reply("push_to_talk") is True
        app.stop_agent_compose.assert_called_once_with(lease.generation)

        server.broadcast(
            {
                "type": "agent.compose.state",
                "version": 1,
                "session_handle": "opaque-session",
                "generation": lease.generation,
                "state": "processing",
            }
        )
        server.broadcast(
            {
                "type": "agent.compose.final",
                "version": 1,
                "session_handle": "opaque-session",
                "generation": lease.generation,
                "recording_id": 8,
                "text": "display first",
            }
        )

        draft = desk.draft("conv:opaque-session")
        assert (draft.phase, draft.text) == ("ready", "display first")
        assert broker.get(interaction.request_id).status == "pending"
        assert server.should_consume_response_enter() is True

        server.submit_ready_response_draft()
        assert broker.get(interaction.request_id).decision == "display first"
    finally:
        coordinator = getattr(
            server, "_claude_response_coordinator", None
        )
        if coordinator is not None and coordinator._timer is not None:
            coordinator._timer.cancel()
        waiter_loop.close()
        client.close()


def test_enter_while_transcribing_submits_the_draft_when_it_lands() -> None:
    client, server = _make_client()
    waiter_loop = asyncio.new_event_loop()
    try:
        app = server._app
        app.start_agent_compose.return_value = True
        broker = server._get_claude_interactions()
        interaction = broker.create(
            "opaque-session", "stop", raw_session_id="raw-session",
            passive_seconds=6, active=False,
        )
        coordinator = server._get_claude_response_coordinator()
        coordinator.enqueue(
            completion_id="completion", sequence=1, grace_seconds=6,
            speech_start=None, interaction=interaction, waiter_loop=waiter_loop,
        )
        desk = server._get_reply_desk()
        desk.present(seq=0, target_id="conv:opaque-session", surface="card", painted=True, focused=False)
        assert server.start_routed_reply("push_to_talk") is True
        lease = server._get_agent_compose_leases().current()
        assert server.should_consume_response_enter() is False  # still recording

        base = {"version": 1, "session_handle": "opaque-session", "generation": lease.generation}
        server.broadcast({**base, "type": "agent.compose.state", "state": "processing"})
        assert server.should_consume_response_enter() is True
        server.submit_ready_response_draft()
        assert broker.get(interaction.request_id).status == "pending"

        server.broadcast({**base, "type": "agent.compose.final", "recording_id": 8, "text": "ship it"})
        assert broker.get(interaction.request_id).decision == "ship it"
    finally:
        coordinator = getattr(server, "_claude_response_coordinator", None)
        if coordinator is not None and coordinator._timer is not None:
            coordinator._timer.cancel()
        waiter_loop.close()
        client.close()


def test_enter_while_transcribing_dies_with_a_cancelled_capture() -> None:
    client, server = _make_client()
    waiter_loop = asyncio.new_event_loop()
    try:
        app = server._app
        app.start_agent_compose.return_value = True
        broker = server._get_claude_interactions()
        interaction = broker.create(
            "opaque-session", "stop", raw_session_id="raw-session",
            passive_seconds=6, active=False,
        )
        coordinator = server._get_claude_response_coordinator()
        coordinator.enqueue(
            completion_id="completion", sequence=1, grace_seconds=6,
            speech_start=None, interaction=interaction, waiter_loop=waiter_loop,
        )
        desk = server._get_reply_desk()
        desk.present(seq=0, target_id="conv:opaque-session", surface="card", painted=True, focused=False)
        leases = server._get_agent_compose_leases()

        def capture(text: str | None, *, cancel: bool = False, enter: bool = False) -> None:
            assert server.start_routed_reply("push_to_talk") is True
            generation = leases.current().generation
            base = {"version": 1, "session_handle": "opaque-session", "generation": generation}
            server.broadcast({**base, "type": "agent.compose.state", "state": "processing"})
            if enter:
                assert server.should_consume_response_enter() is True
                server.submit_ready_response_draft()
            if cancel:
                server._cancel_routed_compose(generation, "escape")
                server.broadcast({**base, "type": "agent.compose.state", "state": "cancelled"})
                return
            server.broadcast({**base, "type": "agent.compose.final", "recording_id": 8, "text": text})
            server.broadcast({**base, "type": "agent.compose.state", "state": "idle"})

        capture("first part")                     # a draft with text
        capture(None, cancel=True, enter=True)    # Enter, then Escape
        capture("second part")                    # no new Enter
        assert broker.get(interaction.request_id).status == "pending"
        assert desk.draft("conv:opaque-session").phase == "ready"
    finally:
        coordinator = getattr(server, "_claude_response_coordinator", None)
        if coordinator is not None and coordinator._timer is not None:
            coordinator._timer.cancel()
        waiter_loop.close()
        client.close()


def test_routed_final_auto_submits_only_when_opted_in() -> None:
    client, server = _make_client(
        config=_make_mock_config(claude_reply_auto_submit=True)
    )
    try:
        coordinator = MagicMock()
        coordinator.live_reply_remaining.return_value = 30.0
        resolved = SimpleNamespace(
            kind="stop",
            status="resolved",
            decision="automatic reply",
        )
        coordinator.resolve_reply.return_value = resolved
        server._claude_question_coordinator = None
        server._claude_response_coordinator = coordinator
        server._get_reply_desk().begin_capture("conv:opaque-session", 12, "toggle")
        server._record_sent_reply = MagicMock()

        server._observe_routed_compose_event(
            {
                "type": "agent.compose.final",
                "version": 1,
                "session_handle": "opaque-session",
                "generation": 12,
                "recording_id": 3,
                "text": "automatic reply",
            }
        )

        assert coordinator.resolve_reply.call_args.args == ("opaque-session", "automatic reply")
        server._record_sent_reply.assert_called_once_with(resolved)
    finally:
        client.close()


def test_prompt_submit_preserves_playback_and_returns_activation_context(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    config = _make_mock_config(agent_speech_activation="always_on")
    controller = _make_controller()
    client, _ = _make_client(config=config, speech_controller=controller)

    response = client.post("/api/claude-hook", json=PROMPT_SUBMIT)

    assert response.json()["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "speaker emoji" in response.json()["hookSpecificOutput"]["additionalContext"]
    controller.stop.assert_not_called()


def test_real_config_always_on_speaks_authored_summaries_in_fifo_order(
    monkeypatch,
):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    config = WhisperstreamConfig(
        claude_hud_enabled=True,
        agent_speech_enabled=True,
        agent_speech_activation="always_on",
        agent_speech_summary_source="agent",
        claude_reply_grace_seconds=0,
        claude_reply_queue_delay_seconds=0,
    )
    controller = _make_controller()
    scheduled: list[tuple[str, object]] = []

    def accept_summary(
        text: str,
        *,
        completion_id: str,
        on_terminal,
        name_prefix=None,
    ) -> bool:
        scheduled.append((text, on_terminal))
        return True

    controller.speak_spoken_summary.side_effect = accept_summary
    client, server = _make_client(
        config=config,
        speech_controller=controller,
    )
    now = [100.0]
    coordinator = ClaudeResponseCoordinator(
        broker=server._get_claude_interactions(),
        quiet_delay_provider=server._claude_reply_queue_delay_seconds,
        state_changed=lambda: server.broadcast(
            server._agent_response_queue_snapshot_event()
        ),
        interaction_changed=server._response_interaction_changed,
        completion_terminal=server._response_completion_terminal,
        chime=server._play_claude_response_chime,
        speech_stop=server._stop_response_speech,
        clock=lambda: now[0],
    )
    server._claude_response_coordinator = coordinator
    try:
        prompt = client.post("/api/claude-hook", json=PROMPT_SUBMIT)
        assert (
            prompt.json()["hookSpecificOutput"]["hookEventName"]
            == "UserPromptSubmit"
        )

        first = {
            **STOP_CLAUDE,
            "session_id": "claude-one",
            "prompt_id": "prompt-one",
            "last_assistant_message": (
                "First response.\n🔊 First authored summary."
            ),
        }
        second = {
            **STOP_CLAUDE,
            "session_id": "claude-two",
            "prompt_id": "prompt-two",
            "last_assistant_message": (
                "Second response.\n🔊 Second authored summary."
            ),
        }

        assert client.post("/api/claude-hook", json=first).json() == {}
        assert client.post("/api/claude-hook", json=second).json() == {}
        assert [text for text, _callback in scheduled] == [
            "First authored summary."
        ]
        queued = coordinator.snapshot()
        assert queued["phase"] == "active_speaking"
        assert len(queued["ordered_completion_ids"]) == 2
        assert (
            queued["active"]["completion_id"]
            == queued["ordered_completion_ids"][0]
        )

        scheduled[0][1]("completed")
        now[0] += coordinator.VISUAL_DWELL_SECONDS
        timer = coordinator._timer
        if timer is not None:
            timer.cancel()
        coordinator._on_timer(coordinator._timer_generation)

        assert [text for text, _callback in scheduled] == [
            "First authored summary.",
            "Second authored summary.",
        ]
        controller.speak_response.assert_not_called()
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        client.close()


def test_real_config_on_command_speaks_only_the_activated_session(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    config = WhisperstreamConfig(
        claude_hud_enabled=True,
        agent_speech_enabled=True,
        agent_speech_activation="per_session",
        agent_speech_summary_source="agent",
        claude_reply_grace_seconds=0,
        claude_reply_queue_delay_seconds=0,
    )
    controller = _make_controller()
    controller.speak_spoken_summary.return_value = True
    client, server = _make_client(
        config=config,
        speech_controller=controller,
    )
    activation = _fixture("claude_speech_activation.json")
    activated_session = activation["session_id"]
    try:
        assert client.post(
            "/api/claude-hook",
            json={**PROMPT_SUBMIT, "session_id": activated_session},
        ).json() == {}
        assert client.post(
            "/api/claude-hook",
            json={
                **STOP_CLAUDE,
                "session_id": activated_session,
                "prompt_id": "inactive-prompt",
                "last_assistant_message": (
                    "Inactive response.\n🔊 Must remain silent."
                ),
            },
        ).json() == {}
        controller.speak_spoken_summary.assert_not_called()

        assert server._get_claude_response_coordinator().dismiss_responses()
        assert client.post("/api/claude-hook", json=activation).json() == {}
        active_prompt = client.post(
            "/api/claude-hook",
            json={**PROMPT_SUBMIT, "session_id": activated_session},
        )
        assert (
            active_prompt.json()["hookSpecificOutput"]["hookEventName"]
            == "UserPromptSubmit"
        )
        assert client.post(
            "/api/claude-hook",
            json={
                **STOP_CLAUDE,
                "session_id": activated_session,
                "prompt_id": "active-prompt",
                "last_assistant_message": (
                    "Activated response.\n🔊 Exact activated summary."
                ),
            },
        ).json() == {}

        controller.speak_spoken_summary.assert_called_once()
        assert controller.speak_spoken_summary.call_args.args == (
            "Exact activated summary.",
        )
        controller.speak_response.assert_not_called()
    finally:
        coordinator = server._claude_response_coordinator
        if coordinator is not None and coordinator._timer is not None:
            coordinator._timer.cancel()
        client.close()


def test_per_session_activation_is_exact_and_does_not_cross_sessions(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, _ = _make_client(
        config=_make_mock_config(agent_speech_activation="per_session")
    )
    activation = _fixture("claude_speech_activation.json")
    activated_session = activation["session_id"]

    before = client.post(
        "/api/claude-hook",
        json={**PROMPT_SUBMIT, "session_id": activated_session},
    )
    invalid = client.post(
        "/api/claude-hook",
        json={**activation, "command_source": "user"},
    )
    activated = client.post("/api/claude-hook", json=activation)
    after = client.post(
        "/api/claude-hook",
        json={**PROMPT_SUBMIT, "session_id": activated_session},
    )
    other = client.post(
        "/api/claude-hook",
        json={**PROMPT_SUBMIT, "session_id": "fixture-other-session"},
    )

    assert before.json() == {}
    assert invalid.json() == {}
    assert activated.json() == {}
    assert "speaker emoji" in after.json()["hookSpecificOutput"]["additionalContext"]
    assert other.json() == {}


def test_stop_is_retained_speaks_only_marked_summary_and_returns_reply(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    config = _make_mock_config(
        agent_speech_enabled=True,
        agent_speech_activation="always_on",
        claude_reply_grace_seconds=6,
    )
    controller = _make_controller()
    client, server = _make_client(config=config, speech_controller=controller)
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append
    _resolve_during_wait(server, "Please also update the changelog.")
    body = {
        **STOP_CLAUDE,
        "last_assistant_message": "Implemented and tested.\n🔊 The change is ready.",
    }

    response = client.post("/api/claude-hook", json=body)

    assert response.json() == {
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": (
                "User reply from Whisperstream HUD: "
                "Please also update the changelog."
            ),
        }
    }
    controller.speak_spoken_summary.assert_called_once()
    assert controller.speak_spoken_summary.call_args.args == ("The change is ready.",)
    controller.speak_response.assert_not_called()
    assert any(event["type"] == "agent.interaction.state" for event in broadcasts)
    completion = next(
        event["completion"]
        for event in broadcasts
        if event["type"] == "agent.completion"
    )
    assert completion["text"] == body["last_assistant_message"]
    assert completion["spoken_summary"] == "The change is ready."
    assert "claude-1" not in repr(broadcasts)


def test_subagent_hooks_fail_open_without_agent_state(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    controller = _make_controller()
    client, server = _make_client(
        config=_make_mock_config(agent_speech_enabled=True),
        speech_controller=controller,
    )
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append

    response = client.post(
        "/api/claude-hook",
        json={**STOP_CLAUDE, "agent_id": "sub-1", "agent_type": "Explore"},
    )

    assert response.json() == {}
    assert broadcasts == []
    controller.speak_spoken_summary.assert_not_called()
    controller.speak_response.assert_not_called()
    assert server._claude_interactions is None
    assert server._agent_interactions is None


def test_agent_type_alone_is_still_the_main_session(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client(config=_make_mock_config())
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append
    _resolve_during_wait(server, "Continue.")

    response = client.post(
        "/api/claude-hook", json={**STOP_CLAUDE, "agent_type": "reviewer"}
    )

    assert response.json() != {}
    assert any(event["type"] == "agent.completion" for event in broadcasts)


def test_stop_timeout_falls_through_with_empty_json(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    config = _make_mock_config(claude_reply_grace_seconds=6)
    client, server = _make_client(config=config)
    now = [100.0]
    broker = ClaudeInteractionBroker(clock=lambda: now[0])
    server._claude_interactions = broker

    class ExpiringCoordinator:
        def enqueue(self, **kwargs):
            interaction = kwargs["interaction"]
            loop = kwargs["waiter_loop"]
            broker.activate_stop(interaction.request_id)
            now[0] += 6.0
            final = broker.expire(interaction.request_id)
            future = loop.create_future()
            future.set_result(final)
            return future

        @staticmethod
        def snapshot():
            return {
                "version": 1,
                "revision": 1,
                "phase": "idle",
                "ordered_completion_ids": [],
                "active": None,
                "quiet_delay_remaining_seconds": 0.0,
            }

    server._claude_response_coordinator = ExpiringCoordinator()

    response = client.post("/api/claude-hook", json=STOP_CLAUDE)

    assert response.json() == {}


# These deltas are differences between two real-clock reads, so the default
# pytest.approx relative tolerance (~0.12ms on 120s) makes them flaky under
# full-suite load. Half a second still catches a wrong constant, which would be
# off by seconds, while tolerating scheduler jitter.
_CLOCK_TOLERANCE = 0.5


@pytest.mark.parametrize("grace_seconds", range(1, 31))
def test_stop_uses_configured_grace_and_fixed_engaged_ceiling(
    monkeypatch,
    grace_seconds,
):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client(
        config=_make_mock_config(
            claude_reply_grace_seconds=grace_seconds,
        )
    )
    broker = ClaudeInteractionBroker(
        request_id_factory=lambda: "opaque-engaged-request"
    )
    server._claude_interactions = broker

    class ResolvingCoordinator:
        def enqueue(self, **kwargs):
            interaction = kwargs["interaction"]
            loop = kwargs["waiter_loop"]
            passive = broker.get(interaction.request_id)
            assert passive is not None
            assert passive.active is False
            assert passive.passive_seconds == pytest.approx(grace_seconds)
            assert passive.hard_deadline - passive.created_at == pytest.approx(
                120.0, abs=_CLOCK_TOLERANCE
            )
            passive = broker.activate_stop(interaction.request_id)
            assert passive is not None
            assert passive.passive_deadline - passive.created_at == pytest.approx(
                grace_seconds, abs=_CLOCK_TOLERANCE
            )
            assert passive.passive_seconds == pytest.approx(grace_seconds)
            assert passive.hard_deadline - passive.created_at == pytest.approx(
                120.0, abs=_CLOCK_TOLERANCE
            )
            engaged = broker.engage(interaction.request_id)
            assert engaged is not None and engaged.engaged is True
            assert engaged.effective_deadline == engaged.hard_deadline
            broker.resolve_reply(
                interaction.request_id,
                "Continue the synthetic run.",
            )
            future = loop.create_future()
            future.set_result(broker.get(interaction.request_id))
            return future

        @staticmethod
        def snapshot():
            return {
                "version": 1,
                "revision": 1,
                "phase": "idle",
                "ordered_completion_ids": [],
                "active": None,
                "quiet_delay_remaining_seconds": 0.0,
            }

    server._claude_response_coordinator = ResolvingCoordinator()
    response = client.post("/api/claude-hook", json=STOP_CLAUDE)

    assert response.json()["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "Continue the synthetic run." in response.json()["hookSpecificOutput"][
        "additionalContext"
    ]


def test_reply_grace_off_fails_open_without_creating_a_stop_route(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client(
        config=_make_mock_config(claude_reply_grace_seconds=0)
    )
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append

    response = client.post("/api/claude-hook", json=STOP_CLAUDE)

    assert response.json() == {}
    assert server._claude_interactions is not None
    assert server._claude_interactions._interactions == {}
    assert not any(event.get("type", "").startswith("agent.console") for event in broadcasts)
    assert not any(
        event.get("type") == "agent.interaction.state"
        for event in broadcasts
    )


def test_recursive_stop_uses_the_bounded_reply_route(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client(
        config=_make_mock_config(
            agent_speech_enabled=True,
            agent_speech_activation="always_on",
        ),
        speech_controller=_make_controller(),
    )
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append
    _resolve_during_wait(server, "Continue the recursive conversation.")

    response = client.post(
        "/api/claude-hook",
        json=_fixture("claude_recursive_stop.json"),
    )

    assert response.json() == {
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": (
                "User reply from Whisperstream HUD: "
                "Continue the recursive conversation."
            ),
        }
    }
    assert server._claude_interactions is not None
    assert len(server._claude_interactions._interactions) == 0
    assert any(event.get("type") == "agent.completion" for event in broadcasts)
    assert any(event.get("type") == "agent.hud.activity" for event in broadcasts)


def test_question_decision_preserves_original_questions(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client()
    surfaced = client.post("/api/claude-hook", json=QUESTION)
    assert surfaced.json() == {}
    surfaced_question = server._agent_question_snapshot_event()["active"]
    assert surfaced_question["phase"] == "surfaced"
    assert server._claude_interactions is not None
    assert server._claude_question_coordinator is not None
    _resolve_during_wait(server, {"Which release channel?": "Beta"})

    response = client.post("/api/claude-hook", json=QUESTION_ANSWERABLE)

    output = response.json()["hookSpecificOutput"]
    assert output == {
        # Claude Code rejects any hookSpecificOutput without hookEventName.
        "hookEventName": "PermissionRequest",
        "decision": {
            "behavior": "allow",
            "updatedInput": {
                "questions": QUESTION["tool_input"]["questions"],
                "answers": {"Which release channel?": "Beta"},
            },
        },
    }
    terminal_question = server._agent_question_snapshot_event()["active"]
    assert terminal_question is None or terminal_question["phase"] == "answered"


def test_question_speech_uses_exact_question_and_numbered_labels_only(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    controller = _make_controller()
    spoken: list[str] = []

    def accept(text, *, completion_id, on_terminal):
        spoken.append(text)
        on_terminal("completed")
        return True

    controller.speak_spoken_summary.side_effect = accept
    client, server = _make_client(
        config=_make_mock_config(agent_speech_enabled=True),
        speech_controller=controller,
    )
    assert client.post("/api/claude-hook", json=QUESTION).json() == {}
    _resolve_during_wait(server, {"Which release channel?": "Beta"})

    response = client.post("/api/claude-hook", json=QUESTION_ANSWERABLE)

    assert response.json()["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert spoken == ["Which release channel? 1. Stable 2. Beta"]
    assert "Ship broadly" not in spoken[0]
    assert "Test first" not in spoken[0]


def test_question_options_are_not_superseded_by_later_normal_speech(monkeypatch):
    """Exercise the server's question composer through the real speech FIFO."""

    from whisperstream.speech.controller import SpeechController

    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    config = WhisperstreamConfig(
        claude_hud_enabled=True,
        agent_speech_enabled=True,
        agent_speech_summary_source="agent",
        claude_reply_queue_delay_seconds=0,
    )
    controller = SpeechController(
        config_provider=lambda: config,
        broadcast=lambda _event: None,
        supervisor_provider=lambda: None,
    )
    from whisperstream.speech.selection import EngineCandidate, SelectionResolution
    controller._selection_policy.resolve = lambda *_args, **_kwargs: SelectionResolution(
        requested="kokoro", candidates=(EngineCandidate("kokoro", "af_heart"),), reason="explicit"
    )
    calls: list[dict] = []

    def record_speak(text, **kwargs):
        calls.append({"text": text, **kwargs})

    controller._playback.speak = record_speak  # type: ignore[method-assign]
    client, server = _make_client(config=config, speech_controller=controller)
    question_terminals: list[str] = []
    later_terminals: list[str] = []
    try:
        assert server._question_speech_start(
            QUESTION["tool_input"]["questions"],
            0,
            question_terminals.append,
        )
        assert controller.speak_spoken_summary(
            "The later response is ready.",
            on_terminal=later_terminals.append,
        )

        assert [call["text"] for call in calls] == [
            "Which release channel? 1. Stable 2. Beta"
        ]
        calls[0]["on_terminal"]("completed")
        assert _wait_for(lambda: len(calls) == 2)
        assert [call["text"] for call in calls] == [
            "Which release channel? 1. Stable 2. Beta",
            "The later response is ready.",
        ]
        assert question_terminals == ["completed"]
        calls[1]["on_terminal"]("completed")
        assert later_terminals == ["completed"]
    finally:
        controller.shutdown(0.25)
        client.close()


def test_question_speech_never_receives_a_name_prefix(monkeypatch):
    """Question speech opts out of the name prefix (Task 2.3, scope decision 4).

    A named session's question must still be spoken with no ``name_prefix``
    kwarg at all. ``accept`` below has no ``**kwargs`` catch-all, so if
    ``_question_speech_start`` were ever changed to pass ``name_prefix``, this
    call would raise ``TypeError`` instead of silently accepting it.
    """
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    controller = _make_controller()
    spoken: list[str] = []

    def accept(text, *, completion_id, on_terminal):
        spoken.append(text)
        on_terminal("completed")
        return True

    controller.speak_spoken_summary.side_effect = accept
    client, server = _make_client(
        config=_make_mock_config(agent_speech_enabled=True),
        speech_controller=controller,
    )
    name_body = {
        "hook_event_name": "UserPromptExpansion",
        "expansion_type": "slash_command",
        "command_source": "plugin",
        "command_name": "whisperstream:name",
        "prompt": "/whisperstream:name speak-harness",
        "session_id": "claude-question",
    }
    assert client.post("/api/claude-hook", json=name_body).json() == {}

    assert client.post("/api/claude-hook", json=QUESTION).json() == {}
    _resolve_during_wait(server, {"Which release channel?": "Beta"})

    response = client.post("/api/claude-hook", json=QUESTION_ANSWERABLE)

    assert response.json()["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert spoken == ["Which release channel? 1. Stable 2. Beta"]


def test_generation_scoped_ws_answer_is_held_until_exact_park(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client()
    assert client.post("/api/claude-hook", json=QUESTION).json() == {}
    active = server._agent_question_snapshot_event()["active"]
    ws = AsyncMock()

    asyncio.run(
        server._handle_claude_interaction_ws_message(
            {
                "type": "agent.question.answer",
                "version": 1,
                "session_handle": active["session_handle"],
                "generation": active["generation"],
                "answers": {"Which release channel?": "Beta"},
            },
            ws,
        )
    )
    response = client.post("/api/claude-hook", json=QUESTION_ANSWERABLE)

    assert response.json()["hookSpecificOutput"]["decision"]["updatedInput"][
        "answers"
    ] == {"Which release channel?": "Beta"}
    assert server._agent_question_snapshot_event()["active"]["phase"] == "answered"


def test_generation_scoped_ws_question_ack_scrubs_terminal_snapshot(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client()
    assert client.post("/api/claude-hook", json=QUESTION).json() == {}
    active = server._agent_question_snapshot_event()["active"]
    coordinator = server._get_claude_question_coordinator()
    coordinator.defer(active["session_handle"], active["generation"])

    asyncio.run(
        server._handle_ws_message(
            {
                "type": "agent.question.ack",
                "version": 1,
                "session_handle": active["session_handle"],
                "generation": active["generation"],
            },
            AsyncMock(),
        )
    )

    assert server._agent_question_snapshot_event()["active"] is None


def test_agent_websocket_messages_cannot_recreate_state_in_effective_lockdown(
    monkeypatch,
):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    _client, server = _make_client()
    server._diagnostics_locked = lambda: True
    ws = AsyncMock()

    asyncio.run(
        server._handle_ws_message(
            {
                "type": "agent.question.answer",
                "version": 1,
                "session_handle": "opaque-session",
                "generation": 1,
                "answers": {"confidential question": "confidential answer"},
            },
            ws,
        )
    )
    asyncio.run(
        server._handle_ws_message(
            {
                "type": "agent.response.draft.update",
                "version": 1,
                "generation": 1,
                "text": "confidential draft",
            },
            ws,
        )
    )

    assert server._claude_interactions is None
    assert server._claude_question_coordinator is None
    assert server._claude_response_coordinator is None
    ws.send_text.assert_not_awaited()


@pytest.mark.parametrize("mode", ("push_to_talk", "toggle"))
@pytest.mark.parametrize("auto_submit", (False, True))
def test_question_hotkey_capture_follows_shared_auto_submit_setting(
    monkeypatch,
    mode,
    auto_submit,
):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client(
        config=_make_mock_config(claude_reply_auto_submit=auto_submit)
    )
    server._app.start_agent_compose.return_value = True
    assert client.post("/api/claude-hook", json=QUESTION).json() == {}
    snapshot = server._agent_question_snapshot_event()["active"]
    broker = server._get_claude_interactions()
    interaction = broker.create(
        snapshot["session_handle"],
        "question",
        raw_session_id=QUESTION["session_id"],
        passive_seconds=6,
        active=False,
    )
    coordinator = server._get_claude_question_coordinator()
    coordinator.attach(
        interaction,
        QUESTION["tool_input"]["questions"],
        grace_seconds=6,
        speech_start=None,
    )
    server._sync_reply_question()
    desk = server._get_reply_desk()
    target = "question:%s:%s" % coordinator.current_target()
    desk.present(seq=0, target_id=target, surface="question", painted=True, focused=False)

    assert server.start_routed_reply(mode) is True
    lease = server._get_agent_compose_leases().current()
    assert lease is not None
    assert lease.session_handle == snapshot["session_handle"]
    server._observe_routed_compose_event(
        {
            "type": "agent.compose.final",
            "version": 1,
            "session_handle": lease.session_handle,
            "generation": lease.generation,
            "recording_id": 4,
            "text": "A dictated freeform answer",
        }
    )
    # Every completed recording follows its final with this, so the matrix has
    # to include it or it tests an event order production never produces.
    server._observe_routed_compose_event(
        {
            "type": "agent.compose.state",
            "version": 1,
            "session_handle": lease.session_handle,
            "generation": lease.generation,
            "recording_id": 4,
            "state": "idle",
        }
    )

    if auto_submit:
        assert broker.get(interaction.request_id).decision == {
            "Which release channel?": "A dictated freeform answer"
        }
        assert coordinator.snapshot()["active"]["phase"] == "answered"
        assert server.should_consume_response_enter() is False
    else:
        assert broker.get(interaction.request_id).status == "pending"
        # The answer lives in the reply owner; the question stays answerable.
        assert coordinator.snapshot()["active"]["phase"] == "grace"
        assert server.should_consume_response_enter() is True
        server.submit_ready_response_draft()
        assert broker.get(interaction.request_id).decision == {
            "Which release channel?": "A dictated freeform answer"
        }


def test_question_decision_round_trips_freeform_and_multiselect_shapes(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client()
    body = _fixture("claude_question_shapes.json")
    answers = {
        "Name the release note": "Synthetic verification",
        "Choose verification groups": "Backend, Frontend",
    }
    _resolve_during_wait(server, answers)
    assert client.post("/api/claude-hook", json=body).json() == {}
    answerable = {**body, "hook_event_name": "PermissionRequest"}

    response = client.post("/api/claude-hook", json=answerable)

    assert response.json() == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "allow",
                "updatedInput": {
                    "questions": body["tool_input"]["questions"],
                    "answers": answers,
                },
            },
        }
    }


def test_question_grace_off_closes_surface_without_answer_authority(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client(
        config=_make_mock_config(claude_reply_grace_seconds=0)
    )

    assert client.post("/api/claude-hook", json=QUESTION).json() == {}
    assert (
        client.post("/api/claude-hook", json=QUESTION_ANSWERABLE).json()
        == {}
    )

    question = server._agent_question_snapshot_event()["active"]
    assert question is None or question["phase"] == "expired"
    assert server._claude_interactions is not None


def test_permission_gate_and_allow_decision(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    disabled = _make_mock_config(claude_permission_decisions_enabled=False)
    client, server = _make_client(config=disabled)

    assert client.post("/api/claude-hook", json=PERMISSION).json() == {}
    assert server._claude_interactions is None

    enabled = _make_mock_config(claude_permission_decisions_enabled=True)
    client, server = _make_client(config=enabled)
    _resolve_during_wait(server, "allow")

    assert client.post("/api/claude-hook", json=PERMISSION).json() == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


def test_permission_created_by_the_hook_outlives_the_passive_grace(monkeypatch):
    """Feedback item 9: the permission pill must not vanish at six seconds.

    Nothing in the frontend sends ``agent.interaction.engage``, so the fix has
    to be authoritative at the broker. Driven on an injectable clock so the
    test never sleeps for the real 120 second ceiling.
    """

    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client(
        config=_make_mock_config(claude_permission_decisions_enabled=True)
    )

    class ManualClock:
        def __init__(self) -> None:
            self.value = 100.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock)
    server._claude_interactions = broker
    observed: list[tuple[str, object]] = []

    def wait(request_id: str):
        observed.append(("created", broker.get(request_id)))
        clock.advance(PASSIVE_DEADLINE_SECONDS + 1.0)
        observed.append(("past_passive", broker.get(request_id)))
        clock.advance(ENGAGED_CEILING_SECONDS)
        observed.append(("past_ceiling", broker.get(request_id)))
        return broker.get(request_id)

    broker.wait = wait

    assert client.post("/api/claude-hook", json=PERMISSION).json() == {}

    stages = dict(observed)
    assert stages["created"].kind == "permission"
    assert stages["created"].status == "pending"
    assert stages["created"].to_dict(now=100.0)[
        "remaining_seconds"
    ] == pytest.approx(ENGAGED_CEILING_SECONDS)
    assert stages["past_passive"].status == "pending"
    assert stages["past_ceiling"].status == "expired"


def test_permission_wire_carries_resolved_source_name(monkeypatch):
    """Scope decision 3: the permission pill shows the session name.

    Task 3.3 threaded ``sourceName`` through the component but hard-coded the
    literal fallback at the live call site because the interaction wire never
    carried a name. This proves the backend now resolves and ships it.
    """

    from whisperstream import claude_hud

    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    claude_hud._DEFAULT_NAME_REGISTRY.set("claude-permission", "pay kit")
    try:
        client, server = _make_client(
            config=_make_mock_config(claude_permission_decisions_enabled=True)
        )
        broadcasts: list[dict] = []
        server.broadcast = broadcasts.append
        _resolve_during_wait(server, "allow")

        assert client.post("/api/claude-hook", json=PERMISSION).json() == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }

        interaction_events = [
            event
            for event in broadcasts
            if event["type"] == "agent.interaction.state"
        ]
        assert interaction_events
        first = interaction_events[0]["interaction"]
        assert first["source_name"] == "pay kit"
        # Never the raw session id, source key, or transcript path.
        assert "claude-permission" not in json.dumps(interaction_events)
    finally:
        claude_hud._DEFAULT_NAME_REGISTRY.clear_name("claude-permission")


def test_permission_wire_source_name_is_none_when_unresolved(monkeypatch):
    """Unnamed degrades byte-for-byte: the field is present but null."""

    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client(
        config=_make_mock_config(claude_permission_decisions_enabled=True)
    )
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append
    _resolve_during_wait(server, "allow")

    assert client.post("/api/claude-hook", json=PERMISSION).json() == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }

    interaction_events = [
        event for event in broadcasts if event["type"] == "agent.interaction.state"
    ]
    assert interaction_events
    assert interaction_events[0]["interaction"]["source_name"] is None


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (
            "deny",
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny"},
                }
            },
        ),
        (None, {}),
    ],
    ids=["deny", "defer"],
)
def test_permission_deny_and_defer_are_validated_fail_open(
    monkeypatch, decision, expected
):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client(
        config=_make_mock_config(claude_permission_decisions_enabled=True)
    )
    _resolve_during_wait(server, decision)

    response = client.post(
        "/api/claude-hook",
        json=_fixture("claude_permission.json"),
    )

    assert response.json() == expected


def test_internal_wait_timeout_fails_open_without_logging_content(
    monkeypatch, caplog
):
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client()
    broker = ClaudeInteractionBroker()
    server._claude_interactions = broker
    server._queue_claude_response = MagicMock(
        side_effect=TimeoutError("synthetic timeout")
    )
    body = {
        **STOP_CLAUDE,
        "last_assistant_message": "PRIVATE_TIMEOUT_CONTENT",
        "session_id": "PRIVATE_TIMEOUT_SESSION",
    }

    response = client.post("/api/claude-hook", json=body)

    assert response.json() == {}
    assert "PRIVATE_TIMEOUT_CONTENT" not in caplog.text
    assert "PRIVATE_TIMEOUT_SESSION" not in caplog.text


def test_hidden_response_never_enters_websocket_but_keeps_local_dedupe(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client(
        config=_make_mock_config(claude_hud_show_text=False)
    )
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append
    server._queue_claude_response = MagicMock(return_value=None)
    body = {
        **STOP_CLAUDE,
        "last_assistant_message": "PRIVATE_HIDDEN_RESPONSE",
    }

    try:
        assert client.post("/api/agent-event", json=body).json() == {"ok": True}
        assert client.post("/api/agent-event", json=body).json() == {"ok": True}
    finally:
        client.close()

    assert server._queue_claude_response.call_count == 1
    assert not any(event.get("type") == "claude.response" for event in broadcasts)
    assert "PRIVATE_HIDDEN_RESPONSE" not in json.dumps(broadcasts)
    assert server._agent_completion_snapshot_event()["completions"] == []
    assert server._agent_response_queue_snapshot_event()["completions"] == []


def test_session_handle_cap_fails_open_when_every_mapping_is_protected() -> None:
    client, server = _make_client()
    try:
        with server._agent_session_handles_lock:
            for index in range(400):
                server._agent_session_handles[f"raw-{index}"] = f"handle-{index}"
        server._active_agent_session_handles = MagicMock(
            return_value={f"handle-{index}" for index in range(400)}
        )

        assert server._agent_session_handle("new-raw-session") is None
        assert len(server._agent_session_handles) == 400
        assert "new-raw-session" not in server._agent_session_handles
    finally:
        client.close()


def test_encoder_failure_still_discards_terminal_interaction() -> None:
    client, server = _make_client()
    broker = server._get_claude_interactions()
    interaction = broker.create(
        "opaque-session",
        "stop",
        raw_session_id="private-session",
    )
    terminal = broker.resolve_reply(interaction.request_id, "private reply")
    server._encode_claude_hook_decision = MagicMock(
        side_effect=RuntimeError("synthetic encoder failure")
    )
    try:
        with pytest.raises(RuntimeError, match="synthetic encoder failure"):
            server._encode_and_discard_claude_interaction(
                interaction=terminal,
                kind="stop",
            )
        assert broker.get(interaction.request_id) is None
        assert broker.raw_session_id_for(interaction.request_id) is None
    finally:
        client.close()


def test_missing_response_waiter_expires_and_discards_created_route(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    client, server = _make_client()
    server._queue_claude_response = MagicMock(return_value=None)
    try:
        response = client.post(
            "/api/claude-hook",
            json=_fixture("claude_recursive_stop.json"),
        )
        assert response.json() == {}
        assert server._claude_interactions is not None
        assert server._claude_interactions.snapshot() == ()
    finally:
        client.close()


def test_cleanup_is_idempotent_and_does_not_recreate_lazy_stores() -> None:
    controller = _make_controller()
    client, server = _make_client(speech_controller=controller)
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append
    server._get_agent_activity()
    server._get_agent_speech_focus()
    server._get_agent_compose_leases()
    managed = server._get_managed_sessions()
    managed.register("wrapper-tok", agent="codex")
    server._get_claude_response_coordinator()
    server._get_claude_question_coordinator()
    server._get_claude_activation_registry()

    try:
        server._clear_agent_speak_state(reason="test")
        server._clear_agent_speak_state(reason="test-again")
    finally:
        client.close()

    assert server._agent_activity is None
    assert server._agent_speech_focus is None
    assert server._agent_compose_leases is None
    assert server._claude_interactions is None
    assert server._claude_response_coordinator is None
    assert server._claude_question_coordinator is None
    assert server._claude_activation_registry is None
    assert server._managed_sessions is None
    assert server._reply_desk is None
    assert controller.stop.call_count == 2
    assert broadcasts[-5:] == [
        {
            "type": "agent.completions.snapshot",
            "version": 1,
            "completions": [],
        },
        {
            "type": "agent.response_queue.snapshot",
            "version": 1,
            "revision": 4,
            "phase": "idle",
            "ordered_completion_ids": [],
            "active": None,
            "quiet_delay_remaining_seconds": 0.0,
            "completions": [],
            "show_summary_text": False,
        },
        {
            "type": "agent.question.snapshot",
            "version": 1,
            "revision": 4,
            "active": None,
        },
        {"type": "agent.sessions", "sessions": []},
        {
            "type": "agent.interactions.snapshot",
            "version": 1,
            "revision": 2,
            "interactions": [],
        },
    ]


@pytest.mark.parametrize("finish_first", [False, True])
def test_app_escape_advances_real_server_and_speech_queues_without_delay(finish_first):
    """Exercise HTTP hooks through the actual Escape owner and speech scheduler.

    Only model availability and the audio output boundary are simulated. Hook
    waiters, server routing, both FIFOs and terminal callbacks are production code.
    """
    from concurrent.futures import ThreadPoolExecutor
    from whisperstream.main import WhisperstreamApp
    from tests.test_speech_controller import _make_controller as make_real_controller

    config = WhisperstreamConfig(
        claude_hud_enabled=True, agent_speech_enabled=True,
        agent_speech_activation="always_on", agent_speech_engine="supertonic",
        claude_reply_grace_seconds=30, claude_reply_queue_delay_seconds=10,
    )
    controller, _, playback, stops, _ = make_real_controller(enabled=True)
    controller._config_provider = lambda: config
    client, server = _make_client(config=config, speech_controller=controller)
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app._agent_reply_router = server
    pool = ThreadPoolExecutor(max_workers=3)
    futures = []
    coordinator = server._get_agent_response_coordinator()
    try:
        for index in range(3):
            session = f"synthetic-escape-{index}"
            client.post("/api/claude-hook", json={**PROMPT_SUBMIT, "session_id": session})
            futures.append(pool.submit(client.post, "/api/claude-hook", json={
                **STOP_CLAUDE, "session_id": session, "prompt_id": f"turn-{index}",
                "last_assistant_message": f"Synthetic reply {index}.\n🔊 Summary {index}.",
            }))
            assert _wait_for(lambda: len(coordinator.snapshot()["ordered_completion_ids"]) == index + 1)
        ids = coordinator.snapshot()["ordered_completion_ids"]
        assert [call["text"] for call in playback] == ["Summary 0."]
        assert all(not future.done() for future in futures)
        if finish_first:
            playback[0]["on_terminal"]("completed")
            assert coordinator.snapshot()["phase"] == "active_grace"

        desk = server._get_reply_desk()

        def present_active_card():
            # The HUD acknowledges the card it paints; Escape belongs to it.
            active = coordinator.snapshot()["active"]
            desk.present(
                seq=desk.presented().seq + 1,
                target_id=None if active is None else f"conv:{active['session_handle']}",
                surface=None if active is None else "card",
                painted=active is not None,
                focused=False,
            )

        for index in range(2):
            previous_terminal = playback[index]["on_terminal"]
            present_active_card()
            assert app._should_consume_escape()
            app._on_escape_press()
            assert futures[index].result(timeout=1).json() == {}
            assert all(not future.done() for future in futures[index + 1:])
            snapshot = coordinator.snapshot()
            assert snapshot["ordered_completion_ids"] == ids[index + 1:]
            assert snapshot["phase"] == "active_speaking"
            assert snapshot["quiet_delay_remaining_seconds"] == 0
            # Playback starts synchronously, despite the configured ten-second
            # gap in both the response coordinator and the speech controller.
            assert [call["text"] for call in playback] == [f"Summary {n}." for n in range(index + 2)]
            assert len(stops) == index + 1 - int(finish_first)
            previous_terminal("stopped")
            assert coordinator.snapshot()["active"]["completion_id"] == ids[index + 1]
            assert coordinator.snapshot()["phase"] == "active_speaking"

        present_active_card()
        assert app._should_consume_escape()
        app._on_escape_press()
        assert futures[2].result(timeout=1).json() == {}
        assert coordinator.snapshot()["ordered_completion_ids"] == []
        present_active_card()
        assert not app._should_consume_escape()
    finally:
        coordinator.clear()
        pool.shutdown(wait=True)
        controller.shutdown()
        client.close()
