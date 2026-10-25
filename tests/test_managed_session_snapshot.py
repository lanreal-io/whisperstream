"""Tests for the `agent.sessions` WebSocket snapshot (Task 4.1).

The frontend has a completion FIFO (`agent_activity`) but no session list. This
adds a session-level snapshot broadcast on `/ws`, replayed on reconnect exactly
like every other authoritative HUD snapshot: `{type: "agent.sessions",
sessions: HudSession[]}` in stable arrival order. It includes the five routing
fields plus reconnect-safe, transcript-class response and compose state.

Privacy boundary: `session_handle` is opaque and never painted; `source_name`
is the already-sanitized resolved name; `can_reply` is a bare boolean with no
reason string. The payload must never carry `cwd`, the raw provider
`session_id`, or the wrapper's correlation token.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from whisperstream.agent_activity import AgentActivityStore
from whisperstream.managed_session_auth import HEADER_NAME, auth as managed_session_auth
from whisperstream.server.app import _MANAGED_SESSION_TOKEN_HEADER

pytestmark = pytest.mark.usefixtures("accepted_eula")

ALL_KEYS = {
    "agent",
    "session_handle",
    "source_name",
    "unread",
    "needs_input",
    "can_reply",
    "conversation_handle",
    "latest_response",
    "ended",
    # Content-free booleans for the console's reply rail.
    "accepts_reply",
    "reply_held",
}
# Present only once the wrapper is bound to an agent session.
BOUND_KEYS = ALL_KEYS | {"reply_target"}


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "none"
    cfg.enhancement_local_model = "gemma4-e2b-instruct"
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.claude_hud_enabled = True
    cfg.claude_hud_show_text = True
    cfg.lockdown_mode = False
    cfg.agent_speech_enabled = False
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


def _make_client(*, config=None):
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.config = config or _make_mock_config()
    app.parakeet = MagicMock()
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
    app.speech_controller = None
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    return TestClient(server.fastapi, raise_server_exceptions=True, headers={"x-whisperstream-hook-token": server._agent_hook_token}), server


@pytest.fixture()
def token(tmp_path):
    """Mint a capability token on the process-global singleton for one test."""
    value = managed_session_auth.mint(tmp_path)
    yield value
    managed_session_auth.clear(tmp_path)


def _headers(tok: str) -> dict:
    return {HEADER_NAME: tok}


def _register(client, tok: str, *, token_header: str, pid: int = 111, cwd: str = "/repo/one"):
    return client.post(
        "/api/managed-sessions/register",
        json={"token": tok, "pid": pid, "cwd": cwd},
        headers=_headers(token_header),
    )


def _response_event(text: str, *, session_id: str | None, prompt_id: str | None = None) -> dict:
    event = {
        "kind": "response",
        "agent": "claude",
        "session_id": session_id,
        "cwd": r"C:\repo\one",
        "text": text,
        "empty": False,
    }
    if prompt_id is not None:
        event["prompt_id"] = prompt_id
    return event


# ---------------------------------------------------------------------------
# Shape: absent store, exact keys, no forbidden fields
# ---------------------------------------------------------------------------


def test_snapshot_is_empty_and_builds_no_store_when_none_ever_registered():
    _client, server = _make_client()

    event = server._agent_sessions_snapshot_event()

    assert event == {"type": "agent.sessions", "sessions": []}
    assert server._managed_sessions is None


def test_snapshot_serialized_keys_are_exactly_the_managed_view_contract(token):
    client, server = _make_client()
    _register(client, "wrapper-tok", token_header=token)

    event = server._agent_sessions_snapshot_event()

    assert event["type"] == "agent.sessions"
    (session,) = event["sessions"]
    assert set(session.keys()) == ALL_KEYS
    assert session["agent"] == "claude"

    server._get_managed_sessions().record_event(
        "wrapper-tok", "SessionStart", session_id="bound-session"
    )
    (bound,) = server._agent_sessions_snapshot_event()["sessions"]
    assert set(bound.keys()) == BOUND_KEYS


def test_snapshot_preserves_the_managed_session_provider():
    _client, server = _make_client()
    server._get_managed_sessions().register(
        "wrapper-tok", agent="codex", cwd="/repo/one"
    )

    (session,) = server._agent_sessions_snapshot_event()["sessions"]

    assert session["agent"] == "codex"


def test_snapshot_never_carries_cwd_session_id_or_the_raw_token(token):
    client, server = _make_client()
    # The basename ("real-project") is expected to survive as `source_name`;
    # the private parent-directory segment must not.
    _register(
        client,
        "wrapper-tok",
        token_header=token,
        cwd=r"C:\Users\PRIVATE_CWD_SENTINEL\real-project",
    )
    server._get_managed_sessions().record_event(
        "wrapper-tok", "PreToolUse", session_id="PRIVATE_SESSION_ID_SENTINEL"
    )

    serialized = str(server._agent_sessions_snapshot_event())

    for forbidden in (
        "PRIVATE_CWD_SENTINEL",
        "PRIVATE_SESSION_ID_SENTINEL",
        "wrapper-tok",
        "cwd",
        "reason",
    ):
        assert forbidden not in serialized


# ---------------------------------------------------------------------------
# Arrival order is stable and is tab order
# ---------------------------------------------------------------------------


def test_arrival_order_is_preserved(token):
    client, server = _make_client()
    for index, tok in enumerate(("tok-a", "tok-b", "tok-c")):
        _register(client, tok, token_header=token, pid=100 + index, cwd=f"/repo/{tok}")
    store = server._get_managed_sessions()
    expected = [store.get(tok).session_handle for tok in ("tok-a", "tok-b", "tok-c")]

    event = server._agent_sessions_snapshot_event()

    assert [session["session_handle"] for session in event["sessions"]] == expected


# ---------------------------------------------------------------------------
# unread: pending-completion identity, reused from agent_activity
# ---------------------------------------------------------------------------


def test_unread_true_only_while_a_completion_is_pending_for_the_bound_session(token):
    client, server = _make_client()
    _register(client, "wrapper-tok", token_header=token)
    store = server._get_managed_sessions()
    store.record_event("wrapper-tok", "Stop", session_id="sid-1")

    assert server._agent_sessions_snapshot_event()["sessions"][0]["unread"] is False

    server._agent_activity = AgentActivityStore()
    (completion,) = server._agent_activity.enqueue(
        _response_event("hello", session_id="sid-1")
    ).changed

    assert server._agent_sessions_snapshot_event()["sessions"][0]["unread"] is True

    server._agent_activity.acknowledge(completion.completion_id)

    assert server._agent_sessions_snapshot_event()["sessions"][0]["unread"] is False


def test_unread_false_when_the_session_id_was_never_bound(token):
    client, server = _make_client()
    _register(client, "wrapper-tok", token_header=token)
    server._agent_activity = AgentActivityStore()
    server._agent_activity.enqueue(_response_event("hello", session_id=None))

    assert server._agent_sessions_snapshot_event()["sessions"][0]["unread"] is False


# ---------------------------------------------------------------------------
# needs_input: reuses the Task 2.6 QUESTION_EVENT_KINDS parked-question state
# ---------------------------------------------------------------------------


def test_needs_input_true_only_while_parked_in_a_blocked_question(token):
    client, server = _make_client()
    _register(client, "wrapper-tok", token_header=token)
    store = server._get_managed_sessions()

    assert server._agent_sessions_snapshot_event()["sessions"][0]["needs_input"] is False

    store.record_event("wrapper-tok", "PreToolUse", session_id="sid-1")
    assert server._agent_sessions_snapshot_event()["sessions"][0]["needs_input"] is True

    store.record_event("wrapper-tok", "Stop", session_id="sid-1")
    assert server._agent_sessions_snapshot_event()["sessions"][0]["needs_input"] is False

    store.record_event("wrapper-tok", "PreToolUse", session_id="sid-1")
    store.record_event("wrapper-tok", "SessionEnd", session_id="sid-1")
    assert server._agent_sessions_snapshot_event()["sessions"][0]["needs_input"] is False


# ---------------------------------------------------------------------------
# can_reply: copied verbatim from the Task 2.6 chokepoint
# ---------------------------------------------------------------------------


def test_can_reply_matches_the_store_predicate(token):
    client, server = _make_client()
    _register(client, "wrapper-tok", token_header=token)
    store = server._get_managed_sessions()

    assert server._agent_sessions_snapshot_event()["sessions"][0]["can_reply"] is True

    store.record_event("wrapper-tok", "Stop", session_id="sid-1")
    assert server._agent_sessions_snapshot_event()["sessions"][0]["can_reply"] is True

    store.record_event("wrapper-tok", "UserPromptSubmit", session_id="sid-1")
    assert server._agent_sessions_snapshot_event()["sessions"][0]["can_reply"] is False


# ---------------------------------------------------------------------------
# WebSocket reconnect replay
# ---------------------------------------------------------------------------


def test_reconnecting_client_receives_the_full_current_session_list(token):
    client, server = _make_client()
    _register(client, "wrapper-tok", token_header=token)
    handle = server._get_managed_sessions().get("wrapper-tok").session_handle

    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "license.status"
        assert ws.receive_json()["type"] == "agent.completions.snapshot"
        assert ws.receive_json()["type"] == "agent.response_queue.snapshot"
        assert ws.receive_json()["type"] == "agent.question.snapshot"
        assert ws.receive_json()["type"] == "agent.conversation.snapshot"
        snapshot = ws.receive_json()

    assert snapshot["type"] == "agent.sessions"
    assert [session["session_handle"] for session in snapshot["sessions"]] == [handle]


def test_reconnecting_client_with_no_session_ever_registered_receives_an_empty_list():
    client, _server = _make_client()

    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "license.status"
        assert ws.receive_json()["type"] == "agent.completions.snapshot"
        assert ws.receive_json()["type"] == "agent.response_queue.snapshot"
        assert ws.receive_json()["type"] == "agent.question.snapshot"
        assert ws.receive_json()["type"] == "agent.conversation.snapshot"
        snapshot = ws.receive_json()

    assert snapshot == {"type": "agent.sessions", "sessions": []}


# ---------------------------------------------------------------------------
# Broadcast triggers: registration, deregistration, and observed state change
# ---------------------------------------------------------------------------


def test_register_broadcasts_the_snapshot(token):
    client, server = _make_client()
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append

    _register(client, "wrapper-tok", token_header=token)

    matching = [event for event in broadcasts if event["type"] == "agent.sessions"]
    assert len(matching) == 1
    assert len(matching[0]["sessions"]) == 1


def test_deregister_broadcasts_the_snapshot_only_when_a_session_was_removed(token):
    client, server = _make_client()
    _register(client, "wrapper-tok", token_header=token)
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append

    client.post(
        "/api/managed-sessions/deregister",
        json={"token": "ghost"},
        headers=_headers(token),
    )
    assert not any(event["type"] == "agent.sessions" for event in broadcasts)

    client.post(
        "/api/managed-sessions/deregister",
        json={"token": "wrapper-tok"},
        headers=_headers(token),
    )
    matching = [event for event in broadcasts if event["type"] == "agent.sessions"]
    assert len(matching) == 1
    assert matching[0]["sessions"] == []


def test_delivered_broadcasts_the_snapshot_only_for_a_known_handle(token):
    client, server = _make_client()
    resp = _register(client, "wrapper-tok", token_header=token)
    handle = resp.json()["session_handle"]
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append

    client.post(
        "/api/managed-sessions/ghost-handle/delivered", headers=_headers(token)
    )
    assert not any(event["type"] == "agent.sessions" for event in broadcasts)

    client.post(
        f"/api/managed-sessions/{handle}/delivered", headers=_headers(token)
    )
    assert any(event["type"] == "agent.sessions" for event in broadcasts)


def test_hook_correlation_broadcasts_the_snapshot_when_it_binds_a_session():
    client, server = _make_client()
    server._get_managed_sessions().register("wrapper-tok", pid=111, cwd="/repo/one")
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append

    resp = client.post(
        "/api/claude-hook",
        json={"hook_event_name": "Stop", "session_id": "sid-1"},
        headers={_MANAGED_SESSION_TOKEN_HEADER: "wrapper-tok"},
    )

    assert resp.status_code == 200
    assert any(event["type"] == "agent.sessions" for event in broadcasts)


def test_hook_correlation_does_not_broadcast_for_an_unregistered_token():
    client, server = _make_client()
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append

    client.post(
        "/api/claude-hook",
        json={"hook_event_name": "Stop", "session_id": "sid-1"},
        headers={_MANAGED_SESSION_TOKEN_HEADER: "never-registered"},
    )

    assert not any(event["type"] == "agent.sessions" for event in broadcasts)
