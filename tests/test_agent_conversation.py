from whisperstream.agent_conversation import AgentConversationStore


def test_bounded_detached_deduplicated_history():
    store = AgentConversationStore(max_sessions=2, max_turns=2, max_chars=8)
    store.append("one", "a", "assistant", "1234")
    store.append("one", "a", "assistant", "duplicate")
    store.append("one", "b", "user", "5678")
    store.append("two", "c", "assistant", "90")
    result = store.snapshot()
    assert [t["id"] for t in result[0]["turns"]] == ["b"]
    result[0]["turns"][0]["text"] = "mutated"
    assert store.snapshot()[0]["turns"][0]["text"] == "5678"
    store.activity("three", True)
    assert [s["session_handle"] for s in store.snapshot()] == ["two", "three"]
    assert store.snapshot()[-1]["activity"] == "working"
    store.clear()
    assert store.snapshot() == []


def test_turn_and_single_text_caps():
    store = AgentConversationStore(max_turns=2)
    for n in range(3):
        store.append("handle", str(n), "assistant", "x" * 50_000)
    assert [t["id"] for t in store.snapshot()[0]["turns"]] == ["1", "2"]
    assert all(len(t["text"]) == 32_000 for t in store.snapshot()[0]["turns"])


def test_server_history_is_cleared_when_text_visibility_or_lockdown_changes():
    from tests.test_claude_hook_endpoint import _make_client
    client, server = _make_client()
    try:
        server._remember_agent_turn("handle", "one", "assistant", "Synthetic response")
        assert len(server._agent_conversation_snapshot_event()["sessions"]) == 1
        server._app.config.claude_hud_show_text = False
        assert server._agent_conversation_snapshot_event()["sessions"] == []
        server._remember_agent_turn("handle", "two", "assistant", "Hidden response")
        server._app.config.claude_hud_show_text = True
        assert server._agent_conversation_snapshot_event()["sessions"] == []
        server._remember_agent_turn("handle", "three", "user", "Synthetic reply")
        server._app.config.lockdown_mode = True
        assert server._agent_conversation_snapshot_event()["sessions"] == []
        server._app.config.lockdown_mode = False
        assert server._agent_conversation_snapshot_event()["sessions"] == []
    finally:
        client.close()
