"""Native replies never borrow a held hook or another session's generation."""

from whisperstream.agent_transports import accept_claude_packet
from whisperstream.managed_sessions import ManagedSessionStore
from whisperstream.claude_interactions import ClaudeInteractionBroker
from whisperstream.config_manager import WhisperstreamConfig


def packet(**changes):
    return {"action": "connect", "instance": "a" * 32, "sequence": 1, "revision": 1,
            "session_id": "session-a", "surface": "cli", "engine_version": "2.1.287",
            "state": "idle", **changes}


def test_native_reply_outlives_stop_hook_and_is_consumed_once():
    store = ManagedSessionStore()
    session, response, _ = accept_claude_packet(store, packet())
    assert store.set_pending_reply(session.session_handle, "a reply")
    poll = packet(action="poll", handle=response["handle"], sequence=2)
    _, result, _ = accept_claude_packet(store, poll)
    assert result["reply"] == "a reply"
    assert not store.can_reply(session)
    assert accept_claude_packet(store, poll)[1] == {}
    assert accept_claude_packet(store, {**poll, "sequence": 3})[1]["reply"] is None


def test_replacement_cannot_collect_previous_draft_or_reply():
    store = ManagedSessionStore()
    old, _, _ = accept_claude_packet(store, packet())
    store.set_pending_reply(old.session_handle, "old reply")
    new, _, _ = accept_claude_packet(store, packet(instance="b" * 32))
    assert old.session_handle != new.session_handle
    assert accept_claude_packet(store, packet(action="poll", handle=old.session_handle, sequence=2))[1] == {"reset": True}
    assert store.take_pending_reply(new.session_handle) is None


def test_busy_and_expired_drop_pending_replies():
    now = [1.0]
    store = ManagedSessionStore(clock=lambda: now[0])
    session, _, _ = accept_claude_packet(store, packet())
    store.set_pending_reply(session.session_handle, "not later")
    _, result, _ = accept_claude_packet(store, packet(action="poll", handle=session.session_handle, sequence=2, state="busy"))
    assert result["reply"] is None
    accept_claude_packet(store, packet(action="poll", handle=session.session_handle, sequence=3))
    now[0] += 9
    assert not store.can_reply(session)


def test_malformed_and_old_runtime_do_not_register():
    store = ManagedSessionStore()
    for change in [{"engine_version": "2.1.286"}, {"sequence": True}, {"surface": "remote"}, {"instance": "bad"}]:
        assert accept_claude_packet(store, packet(**change))[0] is None
    assert len(store) == 0


def test_instance_cannot_switch_session_or_take_other_handle():
    store = ManagedSessionStore()
    session, _, _ = accept_claude_packet(store, packet())
    assert accept_claude_packet(store, packet(action="poll", handle="other", sequence=2))[1] == {"reset": True}
    assert accept_claude_packet(store, packet(session_id="different", sequence=2))[1] == {"reset": True}
    assert session.session_id == "session-a"


def test_broker_refusal_keeps_draft_pending_and_accepts_once():
    broker = ClaudeInteractionBroker()
    interaction = broker.create("handle", "stop")
    accepted = []
    broker.set_reply_handler(interaction.request_id, lambda text: bool(accepted))
    assert broker.resolve_reply(interaction.request_id, "reply") is None
    assert broker.get(interaction.request_id).status == "pending"
    broker.set_reply_handler(interaction.request_id, lambda text: accepted.append(text) or True)
    assert broker.resolve_reply(interaction.request_id, "reply").status == "resolved"
    broker.resolve_reply(interaction.request_id, "reply")
    assert accepted == ["reply"]
    broker.discard_terminal(interaction.request_id)
    assert broker._reply_handlers == {}


def test_surface_preferences_round_trip_and_invalid_fall_back():
    config = WhisperstreamConfig.from_dict({"claude_agent_surface": "desktop", "codex_agent_surface": "cli"})
    assert WhisperstreamConfig.from_dict(config.to_dict()).claude_agent_surface == "desktop"
    assert config.codex_agent_surface == "cli"
    assert WhisperstreamConfig.from_dict({"claude_agent_surface": []}).claude_agent_surface == "auto"


def test_unobserved_busy_turn_still_invalidates_old_reply_on_revision_change():
    store = ManagedSessionStore()
    session, _, _ = accept_claude_packet(store, packet())
    store.set_pending_reply(session.session_handle, "old turn reply")
    _, result, event = accept_claude_packet(store, packet(action="poll",
        handle=session.session_handle, sequence=2, revision=3,
        completion={"text": "New answer", "turn_id": "new-turn"}))
    assert event is not None
    assert result["reply"] is None
    assert store.take_pending_reply(session.session_handle) is None




def test_hook_delivered_turn_cancels_one_mod_completion():
    from whisperstream.agent_transports import HookDeliveredTurns

    now = [0.0]
    turns = HookDeliveredTurns(ttl_seconds=60, clock=lambda: now[0])
    turns.record("s", "Done.")
    assert not turns.consume("other", "Done.")
    assert not turns.consume("s", "Different.")
    assert turns.consume("s", "Done.")
    assert not turns.consume("s", "Done.")
    turns.record("s", "Done.")
    turns.forget_session("s")
    assert not turns.consume("s", "Done.")
    turns.record("s", "Done.")
    now[0] = 61.0
    assert not turns.consume("s", "Done.")


def test_mod_poll_gap_tracks_sessions_independently():
    from whisperstream.agent_transports import ModPollGaps

    now = [0.0]
    gaps = ModPollGaps(max_sessions=1, clock=lambda: now[0])
    assert gaps.observe("s") is None
    now[0] = 12.5
    assert gaps.observe("s") == 12.5
    assert gaps.seen("s")
    gaps.observe("t")
    assert not gaps.seen("s")
