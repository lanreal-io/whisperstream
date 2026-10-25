"""Persistent HUD content for wrapped Claude sessions.

Reply routing, drafts and delivery are covered by test_agent_reply_routing.py.
"""

from tests.test_claude_hook_endpoint import _make_client
import pytest

pytestmark = pytest.mark.usefixtures("accepted_eula")


def _ready_session(server, *, console_visible=True):
    store = server._get_managed_sessions()
    session = store.register("wrapper-token", pid=None, cwd="/repo/one")
    server._record_managed_event("wrapper-token", "Stop", agent="claude", session_id="claude-session")
    if console_visible:
        desk = server._get_reply_desk()
        desk.present(
            seq=desk.presented().seq + 1,
            target_id=server._managed_reply_target(session),
            surface="console",
            painted=True,
            focused=False,
        )
    return store, session


def _finish_compose(server, session, generation, text="persistent reply"):
    for event in (
        {"type": "agent.compose.state", "generation": generation, "state": "processing"},
        {"type": "agent.compose.final", "generation": generation, "text": text},
        {"type": "agent.compose.state", "generation": generation, "state": "idle"},
    ):
        server._observe_routed_compose_event({"version": 1, "session_handle": session.session_handle, "recording_id": 1, **event})


def test_managed_snapshot_retains_session_specific_response_content():
    client, server = _make_client()
    try:
        store, session = _ready_session(server)
        assert store.record_response("wrapper-token", "latest full response") is True

        snapshot = server._agent_sessions_snapshot_event()

        assert snapshot["sessions"][0]["session_handle"] == session.session_handle
        assert snapshot["sessions"][0]["latest_response"] == "latest full response"
        assert snapshot["sessions"][0]["reply_target"] == server._managed_reply_target(session)
    finally:
        client.close()


def test_delivered_reply_joins_the_conversation_history():
    client, server = _make_client()
    try:
        store, session = _ready_session(server)
        desk = server._get_reply_desk()
        target = server._managed_reply_target(session)
        desk.update(target_id=target, base_revision=0, text="correct route")
        draft = desk.draft(target)
        assert server._submit_reply(draft.draft_id, draft.text_revision) == "applied"
        assert store.take_pending_reply(session.session_handle) == "correct route"
        history = server._agent_conversation_snapshot_event()["sessions"]
        assert history[0]["turns"][0]["text"] == "correct route"
    finally:
        client.close()
