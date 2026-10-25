"""Exercise native transport gates and late replies after a hook has returned."""
import asyncio
import time

import pytest

from tests.test_agent_transports import packet
from tests.test_claude_hook_endpoint import _make_client, _make_mock_config, STOP_CLAUDE

pytestmark = pytest.mark.usefixtures("accepted_eula")


@pytest.mark.parametrize("changes", [
    {"claude_hud_enabled": False}, {"lockdown_mode": True},
    {"agents_enabled": False},
])
def test_native_ingestion_is_gated_before_state_creation(changes):
    client, server = _make_client(config=_make_mock_config(**changes))
    response = client.post("/api/agent-transport/claude", json=packet())
    assert response.status_code in {200, 403, 423}
    assert server._managed_sessions is None


def test_native_auth_and_provider_disable_cannot_collect_pending_content():
    client, server = _make_client()
    assert client.post("/api/agent-transport/claude", json=packet(),
                       headers={"x-whisperstream-hook-token": "wrong"}).json() == {}
    assert server._managed_sessions is None
    response = client.post("/api/agent-transport/claude", json=packet()).json()
    handle = response["handle"]
    server._managed_sessions.set_pending_reply(handle, "private draft")
    server._app.config.claude_hud_enabled = False
    assert client.post("/api/agent-transport/claude", json=packet(
        action="poll", sequence=2, handle=handle)).json() == {}


def test_real_completion_route_returns_while_response_coordinator_remains_active():
    client, server = _make_client()
    try:
        started = time.monotonic()
        result = client.post("/api/agent-transport/claude", json=packet(
            completion={"text": "A completed answer", "turn_id": "turn-native"})).json()
        assert time.monotonic() - started < 2
        assert result["ok"]
        coordinator = server._claude_response_coordinator
        assert coordinator is not None
        assert coordinator.snapshot()["active"] is not None
        store = server._get_managed_sessions()
        assert store.can_reply(store.get_by_handle(result["handle"]))
        # No held HTTP request is needed for a later explicit reply.
        assert store.set_pending_reply(result["handle"], "Later reply")
        response = client.post("/api/agent-transport/claude", json=packet(
            action="poll", sequence=2, handle=result["handle"])).json()
        assert response["reply"] == "Later reply"
    finally:
        server._clear_agent_speak_state(reason="test_cleanup")
        client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["claude", "codex"])
async def test_managed_stop_returns_before_reply_and_rechecks_exact_target(agent):
    _, server = _make_client(config=_make_mock_config(codex_hud_enabled=True))
    store = server._get_managed_sessions()
    session = store.register("fixture-token", agent=agent)
    pending = []

    def queue(**kwargs):
        interaction = kwargs["interaction"]
        server._get_agent_interactions().activate_stop(interaction.request_id)
        pending.append(interaction)
        return asyncio.get_running_loop().create_future()

    if agent == "claude":
        server._queue_claude_response = queue
        handler = server._handle_claude_hook_body
    else:
        server._queue_agent_response = queue
        handler = server._handle_codex_hook_body
    body = {**STOP_CLAUDE, "turn_id": "turn-a"}
    # An unresolved HUD future must never keep this hook request open.
    assert await asyncio.wait_for(handler(body, correlation_token=session.token), .5) == {}
    interaction = pending[0]
    broker = server._get_agent_interactions()
    assert broker.resolve_reply(interaction.request_id, "late reply") is not None
    assert store.take_pending_reply(session.session_handle) == "late reply"
    # A second completion's draft must not migrate across a resumed conversation.
    await handler({**body, "prompt_id": "prompt-b", "turn_id": "turn-b",
                   "last_assistant_message": "Second answer"}, correlation_token=session.token)
    store.record_event(session.token, "Stop", agent=agent, session_id="different-session")
    assert broker.resolve_reply(pending[-1].request_id, "stale reply") is None
    assert store.take_pending_reply(session.session_handle) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("resume", [False, True])
async def test_detached_reply_cannot_survive_a_new_turn_in_the_same_session(agent, resume):
    _, server = _make_client(config=_make_mock_config(codex_hud_enabled=True))
    store = server._get_managed_sessions()
    session = store.register("fixture-token", agent=agent)
    store.record_event(session.token, "Stop", agent=agent, session_id="same-session")
    broker = server._get_agent_interactions()
    interaction = broker.create(session.session_handle, "stop", passive_seconds=20, agent=agent)
    assert server._bind_persistent_stop(interaction, session, server._current_acceptance_grant())
    assert store.set_pending_reply(session.session_handle, "Already queued")
    store.record_event(session.token, "Stop" if resume else "UserPromptSubmit", agent=agent,
                       session_id="other-session" if resume else "same-session")
    store.record_event(session.token, "Stop", agent=agent, session_id="same-session")
    assert broker.resolve_reply(interaction.request_id, "Old reply") is None
    assert store.take_pending_reply(session.session_handle) is None


@pytest.mark.asyncio
async def test_live_status_is_per_surface_uncached_and_removed_on_disable(monkeypatch):
    from whisperstream import agent_runtime_compatibility as runtime
    _, server = _make_client(config=_make_mock_config())
    calls = []
    def inspect(agent):
        calls.append(agent)
        return {"surfaces": [{"surface": surface, "state": "unknown_engine",
                              "reply_verified": False} for surface in ("cli", "desktop")]}
    monkeypatch.setattr(runtime, "inspect_provider", inspect)
    from whisperstream.agent_transports import accept_claude_packet
    accept_claude_packet(server._get_managed_sessions(), packet(surface="desktop"))
    result = await server._agent_runtime_status("claude")
    assert result["surfaces"][0]["state"] == "unknown_engine"
    assert result["surfaces"][1]["state"] == "ready"
    server._app.config.claude_hud_enabled = False
    assert (await server._agent_runtime_status("claude"))["surfaces"][1]["state"] == "unknown_engine"
    assert len(calls) == 1
    await server._agent_runtime_status("claude", refresh=True)
    assert len(calls) == 2


@pytest.mark.parametrize(("versions", "expected"), [
    ({"standalone": (0, 159, 3), "embedded": (0, 162, 0)}, "embedded"),
    ({"standalone": (0, 163, 0), "embedded": (0, 162, 0)}, "standalone"),
    ({"standalone": None, "embedded": (0, 162, 0)}, "embedded"),
])
def test_codex_lifecycle_uses_the_newest_installed_engine(monkeypatch, versions, expected):
    from whisperstream import codex_plugin_lifecycle as lifecycle
    from whisperstream import agent_runtime_compatibility as runtime
    monkeypatch.setattr(lifecycle.shutil, "which", lambda _: "standalone")
    monkeypatch.setattr(runtime, "desktop_codex_executable", lambda: "embedded")
    monkeypatch.setattr(lifecycle, "_codex_engine_version", versions.get)
    with lifecycle.codex_plugin_runtime():
        assert lifecycle._codex_cli_path() == expected
    monkeypatch.setattr(lifecycle.shutil, "which", lambda _: None)
    assert lifecycle._codex_cli_path() == "embedded"


def test_claude_uses_its_cli_whenever_installed(monkeypatch):
    from whisperstream import claude_hud
    _, server = _make_client(config=_make_mock_config(claude_agent_surface="desktop"))
    monkeypatch.setattr(claude_hud, "_claude_cli_path", lambda: "standalone")
    assert server._claude_uses_cli()
    monkeypatch.setattr(claude_hud, "_claude_cli_path", lambda: None)
    assert not server._claude_uses_cli()


def test_any_surface_connects_regardless_of_stored_preference():
    client, server = _make_client(config=_make_mock_config(claude_agent_surface="cli"))
    response = client.post("/api/agent-transport/claude", json=packet(surface="desktop")).json()
    assert response["ok"] is True
    assert server._managed_sessions is not None


def test_connect_records_only_the_bundled_plugin_version(monkeypatch):
    client, server = _make_client()
    monkeypatch.setattr(server, "_bundled_claude_plugin_version", lambda: "2.0.0")
    server._app.config.claude_connected_plugin_version = ""
    client.post("/api/agent-transport/claude", json=packet(plugin_version="1.6.0"))
    assert server._app.config.claude_connected_plugin_version == ""
    client.post("/api/agent-transport/claude", json=packet(
        instance="b" * 32, session_id="session-b", plugin_version="2.0.0"))
    assert server._app.config.claude_connected_plugin_version == "2.0.0"
    server._app.config_manager.save.assert_called()


@pytest.mark.parametrize(("status", "record", "expected"), [
    ({"ok": True, "status": "current", "installed_version": "2.0.0"}, "2.0.0", "connected"),
    ({"ok": True, "status": "current", "installed_version": "1.6.0"}, "2.0.0", "waiting"),
    ({"ok": True, "status": "updated", "installed_version": "2.0.0"}, "", "waiting"),
    ({"ok": True, "status": "unknown"}, "2.0.0", "none"),
])
def test_claude_connection_state_without_a_live_session(monkeypatch, status, record, expected):
    _, server = _make_client()
    monkeypatch.setattr(server, "_bundled_claude_plugin_version", lambda: "2.0.0")
    server._claude_plugin_status = status
    server._app.config.claude_connected_plugin_version = record
    assert server._claude_connection_state({"surfaces": []}) == expected
    server._app.config.agents_enabled = False
    assert server._claude_connection_state({"surfaces": []}) == "none"


def test_live_handshake_is_connected_on_any_surface():
    _, server = _make_client()
    compat = {"surfaces": [{"surface": "desktop", "state": "ready", "reply_verified": True}]}
    assert server._claude_connection_state(compat) == "connected"


@pytest.mark.asyncio
@pytest.mark.parametrize('revocation', ['acceptance', 'lockdown', 'provider'])
async def test_authority_revoked_during_body_read_cannot_recreate_native_state(monkeypatch, revocation):
    import json
    from types import SimpleNamespace
    client, server = _make_client()
    grant = server._current_acceptance_grant()
    monkeypatch.setattr(server, '_request_acceptance_grant', lambda _: grant)

    async def read_body():
        if revocation == 'acceptance':
            monkeypatch.setattr(server, '_acceptance_is_current', lambda _: False)
        elif revocation == 'lockdown':
            server._app.config.lockdown_mode = True
        else:
            server._app.config.claude_hud_enabled = False
        return json.dumps(packet()).encode()

    request = SimpleNamespace(headers={'x-whisperstream-hook-token': server._agent_hook_token}, body=read_body)
    endpoint = next(route.endpoint for route in server.fastapi.routes if getattr(route, 'path', None) == '/api/agent-transport/claude')
    assert await endpoint(request) == {}
    assert server._managed_sessions is None
    client.close()


@pytest.mark.asyncio
async def test_pause_during_completion_cannot_publish_a_reply(monkeypatch):
    import json
    from types import SimpleNamespace
    client, server = _make_client()
    grant = server._current_acceptance_grant()
    monkeypatch.setattr(server, '_request_acceptance_grant', lambda _: grant)

    async def read_body():
        return json.dumps(packet(completion={'text': 'Answer', 'turn_id': 'turn'})).encode()

    async def completion(*args, **kwargs):
        server._app.config.agents_enabled = False

    monkeypatch.setattr(server, '_handle_claude_hook_body', completion)
    request = SimpleNamespace(headers={'x-whisperstream-hook-token': server._agent_hook_token}, body=read_body)
    endpoint = next(route.endpoint for route in server.fastapi.routes if getattr(route, 'path', None) == '/api/agent-transport/claude')
    assert await endpoint(request) == {}
    server._clear_agent_speak_state(reason='test_cleanup')
    client.close()
