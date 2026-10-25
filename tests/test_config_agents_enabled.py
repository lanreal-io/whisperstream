"""Master Agents pause gate and the Claude connection record config fields."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from whisperstream.config_manager import WhisperstreamConfig

pytestmark = pytest.mark.usefixtures("accepted_eula")


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


def test_defaults_and_round_trip():
    config = WhisperstreamConfig()
    assert config.agents_enabled is True
    assert config.claude_connected_plugin_version == ""
    config.validate()
    restored = WhisperstreamConfig.from_dict(
        WhisperstreamConfig(agents_enabled=False, claude_connected_plugin_version="2.0.0").to_dict()
    )
    assert restored.agents_enabled is False
    assert restored.claude_connected_plugin_version == "2.0.0"
    assert WhisperstreamConfig.from_dict({}).agents_enabled is True


@pytest.mark.parametrize(
    "values",
    [{"agents_enabled": "yes"}, {"claude_connected_plugin_version": 2}],
)
def test_validation_rejects_wrong_types(values):
    with pytest.raises(ValueError):
        WhisperstreamConfig(**values).validate()


def _real_server(config: WhisperstreamConfig):
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.config = config
    app.parakeet = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.dictation_state_snapshot.return_value = None
    app.reload_config.side_effect = lambda new: setattr(app, "config", new)
    return WhisperstreamServer(app, port=0, register_hotkeys=False)


def test_partial_put_preserves_pause_and_ignores_client_connection_record():
    server = _real_server(
        WhisperstreamConfig(
            agents_enabled=False,
            claude_hud_enabled=True,
            claude_connected_plugin_version="2.0.0",
        )
    )
    with TestClient(server.fastapi) as client:
        response = client.put(
            "/api/config",
            json={"claude_view_shortcut": "ctrl+shift+v", "claude_connected_plugin_version": "9.9.9"},
        )

    assert response.status_code == 200
    assert server._app.config.agents_enabled is False
    assert server._app.config.claude_connected_plugin_version == "2.0.0"


def test_put_cannot_reset_the_reply_hint_counter():
    server = _real_server(WhisperstreamConfig(claude_reply_hint_dictated_replies=3))
    with TestClient(server.fastapi) as client:
        response = client.put(
            "/api/config",
            json={"claude_view_shortcut": "ctrl+shift+v", "claude_reply_hint_dictated_replies": 0},
        )

    assert response.status_code == 200
    assert server._app.config.claude_reply_hint_dictated_replies == 3


@pytest.mark.parametrize(
    ("stored", "loaded"),
    [(0, 0), (2, 2), (3, 3), (9, 3), (-1, 0), (True, 0), ("2", 0), (2.0, 0)],
)
def test_reply_hint_counter_normalizes(stored, loaded):
    config = WhisperstreamConfig.from_dict({"claude_reply_hint_dictated_replies": stored})
    assert config.claude_reply_hint_dictated_replies == loaded
    config.validate()
    restored = WhisperstreamConfig.from_dict(config.to_dict())
    assert restored.claude_reply_hint_dictated_replies == loaded


def test_paused_master_gate_disables_both_providers():
    server = _real_server(
        WhisperstreamConfig(agents_enabled=False, claude_hud_enabled=True, codex_hud_enabled=True)
    )
    assert server._agent_provider_enabled("claude") is False
    assert server._agent_provider_enabled("codex") is False
    server._app.config.agents_enabled = True
    assert server._agent_provider_enabled("claude") is True
    assert server._agent_provider_enabled("codex") is True


def test_paused_hooks_and_transport_fail_open():
    server = _real_server(
        WhisperstreamConfig(agents_enabled=False, claude_hud_enabled=True, codex_hud_enabled=True)
    )
    headers = {"x-whisperstream-hook-token": server._agent_hook_token}
    stop = {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "done"}
    with TestClient(server.fastapi, headers=headers) as client:
        assert client.post("/api/claude-hook", json=stop).json() == {}
        assert client.post("/api/codex-hook", json=stop).json() == {}
        assert client.post("/api/agent-transport/claude", json={"action": "connect"}).json() == {}
    assert server._agent_interactions is None
    assert server._claude_interactions is None
