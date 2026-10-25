"""Quiet liveness and deadline/notification ownership regressions."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from tests.test_claude_hook_endpoint import _make_client
from tests.test_agent_transports import packet
from whisperstream.managed_sessions import ManagedSessionStore

pytestmark = pytest.mark.usefixtures("accepted_eula")


def test_successful_transport_probes_are_quiet_but_errors_remain_visible(caplog):
    client, server = _make_client()
    try:
        with caplog.at_level(logging.INFO):
            client.get("/api/agent-hook/verify")
            client.post("/api/agent-transport/claude", json=packet())
        ordinary = [r.message for r in caplog.records if r.message.startswith("api ")]
        assert ordinary == []
        caplog.clear()
        with caplog.at_level(logging.INFO):
            response = client.get("/api/agent-hook/verify",
                                  headers={"x-whisperstream-hook-token": "wrong"})
        assert response.status_code >= 400
        assert any("status=403" in r.message for r in caplog.records)
    finally:
        server._clear_agent_speak_state(reason="test")
        client.close()


def test_unchanged_heartbeats_do_not_publish_session_content():
    client, server = _make_client()
    events = []
    server.broadcast = events.append
    try:
        handle = client.post("/api/agent-transport/claude", json=packet()).json()["handle"]
        assert any(e["type"] == "agent.sessions" for e in events)
        events.clear()
        for sequence in range(2, 6):
            client.post("/api/agent-transport/claude", json=packet(
                action="poll", sequence=sequence, handle=handle,
            ))
        assert events == []
        client.post("/api/agent-transport/claude", json=packet(
            action="poll", sequence=6, handle=handle, state="busy",
        ))
        assert any(e["type"] == "agent.sessions" for e in events)
    finally:
        server._clear_agent_speak_state(reason="test")
        client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reply", "shutdown", "remove", "clear"])
async def test_pending_request_wakes_only_for_exact_session_and_cleans_subscription(action):
    client, server = _make_client()
    store = server._get_managed_sessions()
    session = store.register("one")
    other = store.register("two")
    endpoint = next(r.endpoint for r in server.fastapi.routes
                    if getattr(r, "path", None) == "/api/managed-sessions/{handle}/pending")
    request = SimpleNamespace(state=SimpleNamespace())
    pending = asyncio.create_task(endpoint(session.session_handle, request, wait=20))
    try:
        await asyncio.sleep(0)
        assert len(store._reply_waiters) == 1
        store.request_shutdown(other.session_handle)
        await asyncio.sleep(0)
        assert not pending.done()
        if action == "reply":
            assert store.set_pending_reply(session.session_handle, "synthetic reply")
        elif action == "shutdown":
            store.request_shutdown(session.session_handle)
        elif action == "remove":
            store.deregister(session.token)
        else:
            store.clear()
        result = await asyncio.wait_for(pending, .5)
        if action == "reply":
            assert result["reply"] == "synthetic reply"
        elif action == "shutdown":
            assert result["shutdown_requested"] is True
        else:
            assert result.status_code == 404
        assert not store._reply_waiters
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        server._clear_agent_speak_state(reason="test")
        client.close()


@pytest.mark.asyncio
async def test_last_explicit_removal_cancels_expiry_deadline():
    client, server = _make_client()
    store = server._get_managed_sessions()
    session = store.register("one")
    try:
        server._arm_managed_session_expiry()
        timer = server._managed_expiry_timer
        assert timer is not None
        store.deregister(session.token)
        server._arm_managed_session_expiry()
        assert timer.cancelled()
        assert server._managed_expiry_timer is None
    finally:
        server._clear_agent_speak_state(reason="test")
        client.close()


@pytest.mark.asyncio
async def test_expiry_preserves_refreshed_history_and_retires_deadline_when_empty():
    client, server = _make_client()
    now = [100.]
    store = ManagedSessionStore(clock=lambda: now[0])
    server._managed_sessions = store
    session = store.register("one")
    session.transport = "claude_mod"
    assert store.record_response(session.token, "synthetic retained answer")
    try:
        server._arm_managed_session_expiry()
        first = server._managed_expiry_timer
        now[0] += 7
        store.refresh_poll(session.session_handle)
        now[0] += 2
        callback = first._callback
        first.cancel()
        callback()
        assert store.get_by_handle(session.session_handle) is session
        assert store.view(session.session_handle).latest_response == "synthetic retained answer"
        timer = server._managed_expiry_timer
        assert timer is not None and timer is not first
        now[0] += 9
        callback = timer._callback
        timer.cancel()
        callback()
        assert store.get_by_handle(session.session_handle) is None
        assert not store._views
        assert server._managed_expiry_timer is None
    finally:
        server._clear_agent_speak_state(reason="test")
        client.close()
