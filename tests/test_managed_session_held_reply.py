"""Replies accepted while the agent works are held and delivered at its next Stop."""

import pytest

from whisperstream.agent_transports import accept_claude_packet
from whisperstream.managed_sessions import HELD_REPLY_TTL_SECONDS, ManagedSessionStore
from tests.test_agent_transports import packet
from tests.test_claude_hook_endpoint import _make_client
from tests.test_managed_session_persistent_reply import _finish_compose, _ready_session


def _busy_pty_store(now):
    store = ManagedSessionStore(clock=lambda: now[0])
    session = store.register("wrapper-token", pid=None, cwd="/repo/one")
    store.record_event("wrapper-token", "Stop", session_id="claude-session")
    store.record_event("wrapper-token", "UserPromptSubmit", session_id="claude-session")
    return store, session


def test_busy_session_accepts_and_holds_until_stop():
    now = [1.0]
    store, session = _busy_pty_store(now)
    assert not store.can_reply(session)
    assert store.can_accept_reply(session)

    assert store.hold_reply(session.session_handle, "wait for me")
    assert store.has_held_reply(session.session_handle)
    assert store.take_pending_reply(session.session_handle) is None

    store.record_event("wrapper-token", "Stop", session_id="claude-session")
    assert not store.has_held_reply(session.session_handle)
    assert store.take_pending_reply(session.session_handle) == "wait for me"


def test_second_held_reply_is_appended_in_order():
    now = [1.0]
    store, session = _busy_pty_store(now)
    store.hold_reply(session.session_handle, "first")
    store.hold_reply(session.session_handle, "second")
    store.record_event("wrapper-token", "Stop", session_id="claude-session")
    assert store.take_pending_reply(session.session_handle) == "first\n\nsecond"


def test_question_event_does_not_deliver_a_held_reply_as_its_answer():
    now = [1.0]
    store, session = _busy_pty_store(now)
    store.hold_reply(session.session_handle, "not an answer")
    store.record_event("wrapper-token", "PreToolUse", session_id="claude-session")
    assert store.take_pending_reply(session.session_handle) is None
    assert store.has_held_reply(session.session_handle)


@pytest.mark.parametrize("event", ["SessionEnd", "conversation_change"])
def test_held_reply_is_dropped_with_its_conversation(event):
    now = [1.0]
    store, session = _busy_pty_store(now)
    store.hold_reply(session.session_handle, "stale")
    if event == "SessionEnd":
        store.record_event("wrapper-token", "SessionEnd", session_id="claude-session")
    else:
        store.record_event("wrapper-token", "UserPromptSubmit", session_id="other-session")
    store.record_event("wrapper-token", "Stop", session_id="other-session")
    assert store.take_pending_reply(session.session_handle) is None


def test_held_reply_expires():
    now = [1.0]
    store, session = _busy_pty_store(now)
    store.hold_reply(session.session_handle, "too old")
    now[0] += HELD_REPLY_TTL_SECONDS + 1
    store.refresh_poll(session.session_handle)
    store.record_event("wrapper-token", "Stop", session_id="claude-session")
    assert store.take_pending_reply(session.session_handle) is None


@pytest.mark.usefixtures("accepted_eula")
def test_turn_change_keeps_the_users_draft():
    client, server = _make_client()
    try:
        store, session = _ready_session(server)
        desk = server._get_reply_desk()
        target = server._managed_reply_target(session)
        desk.update(target_id=target, base_revision=0, text="half typed")

        server._record_managed_event("wrapper-token", "UserPromptSubmit", agent="claude", session_id="claude-session")

        draft = desk.draft(target)
        assert (draft.phase, draft.text) == ("editing", "half typed")
    finally:
        client.close()


def test_mod_transport_delivers_a_held_reply_on_its_next_idle_heartbeat():
    store = ManagedSessionStore()
    session, response, _ = accept_claude_packet(store, packet())
    handle = response["handle"]
    accept_claude_packet(store, packet(action="poll", handle=handle, sequence=2, revision=2, state="busy"))
    assert not store.can_reply(session)
    assert store.hold_reply(handle, "after this turn")

    _, busy, _ = accept_claude_packet(store, packet(action="poll", handle=handle, sequence=3, revision=2, state="busy"))
    assert busy["reply"] is None

    _, idle, _ = accept_claude_packet(store, packet(action="poll", handle=handle, sequence=4, revision=3))
    assert idle["reply"] == "after this turn"


@pytest.mark.usefixtures("accepted_eula")
def test_submitting_to_a_busy_session_holds_and_reports_it():
    client, server = _make_client()
    try:
        store, session = _ready_session(server)
        server._app.start_agent_compose.return_value = True
        assert server.start_routed_reply("push_to_talk") is True
        lease = server._get_agent_compose_leases().current()
        _finish_compose(server, session, lease.generation, "reply while busy")
        server._record_managed_event("wrapper-token", "UserPromptSubmit", agent="claude", session_id="claude-session")

        # The draft survived the turn start and Enter still owns it.
        assert server.should_consume_response_enter() is True
        server.submit_ready_response_draft()

        held = server._get_reply_desk().draft(server._managed_reply_target(session))
        assert (held.phase, held.held) == ("sending", True)
        assert store.take_pending_reply(session.session_handle) is None
        snapshot = server._agent_sessions_snapshot_event()["sessions"][0]
        assert snapshot["reply_held"] is True
        assert snapshot["accepts_reply"] is True

        server._record_managed_event("wrapper-token", "Stop", agent="claude", session_id="claude-session")
        assert store.take_pending_reply(session.session_handle) == "reply while busy"
        assert server._get_reply_desk().draft(server._managed_reply_target(session)) is None
    finally:
        client.close()


@pytest.mark.usefixtures("accepted_eula")
def test_dictating_into_a_busy_visible_console_routes_instead_of_falling_through():
    client, server = _make_client()
    try:
        store, session = _ready_session(server)
        server._record_managed_event("wrapper-token", "UserPromptSubmit", agent="claude", session_id="claude-session")
        server._app.start_agent_compose.return_value = True
        assert server.start_routed_reply("push_to_talk") is True
    finally:
        client.close()
