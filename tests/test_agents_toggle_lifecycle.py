"""Agents master switch transitions and the startup plugin refresh."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from whisperstream.config_manager import WhisperstreamConfig, get_app_dir

pytestmark = pytest.mark.usefixtures("accepted_eula")


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


def _server(config: WhisperstreamConfig):
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.config = config
    app.parakeet = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.dictation_state_snapshot.return_value = None
    app.reload_config.side_effect = lambda new: setattr(app, "config", new)
    return WhisperstreamServer(app, port=0, register_hotkeys=False)


def _discovery(agent: str) -> Path:
    return get_app_dir() / f"{agent}-hud-port.json"


def test_pause_clears_agent_state_and_disables_only_claude(monkeypatch):
    server = _server(WhisperstreamConfig(claude_hud_enabled=True, codex_hud_enabled=True))
    paused = []
    monkeypatch.setattr(server, "_schedule_claude_pause", lambda: paused.append(True))
    codex_calls = []
    import whisperstream.codex_plugin_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "disable_codex_plugin", lambda *_a, **_k: codex_calls.append("disable"))
    server._loop = asyncio.new_event_loop()
    try:
        server._write_claude_discovery_file()
        server._write_codex_discovery_file()
        with TestClient(server.fastapi) as client:
            assert client.put("/api/config", json={"agents_enabled": False}).status_code == 200
    finally:
        server._loop.close()

    assert paused == [True]
    assert codex_calls == []
    assert not _discovery("claude").exists()
    assert not _discovery("codex").exists()
    # The remembered connections survive the pause.
    assert server._app.config.claude_hud_enabled is True
    assert server._app.config.codex_hud_enabled is True


def test_resume_republishes_discovery_for_remembered_providers(monkeypatch):
    server = _server(
        WhisperstreamConfig(agents_enabled=False, claude_hud_enabled=False, codex_hud_enabled=True)
    )
    monkeypatch.setattr(server, "_schedule_claude_pause", lambda: pytest.fail("no pause on resume"))
    server._loop = asyncio.new_event_loop()
    try:
        with TestClient(server.fastapi) as client:
            assert client.put("/api/config", json={"agents_enabled": True}).status_code == 200
    finally:
        server._loop.close()

    assert _discovery("codex").exists()
    assert not _discovery("claude").exists()


def test_unrelated_save_while_paused_does_not_republish(monkeypatch):
    server = _server(WhisperstreamConfig(agents_enabled=False, codex_hud_enabled=True))
    server._loop = asyncio.new_event_loop()
    try:
        with TestClient(server.fastapi) as client:
            client.put("/api/config", json={"claude_view_shortcut": "ctrl+shift+v"})
    finally:
        server._loop.close()
    assert not _discovery("codex").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("config", "expected_keep_disabled"), [
    ({"claude_hud_enabled": True}, False),
    ({"claude_hud_enabled": True, "agents_enabled": False}, True),
    ({"claude_hud_enabled": False}, True),
])
async def test_startup_refresh_never_resumes_an_inactive_claude(real_agent_plugin_refresh, monkeypatch, config, expected_keep_disabled):
    server = _server(WhisperstreamConfig(**config))
    seen = []

    def apply(action, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(state=None, failure=None), {"ok": True, "status": "current"}

    monkeypatch.setattr(server, "_apply_agent_plugin_action", apply)
    await server._refresh_claude_plugin()
    assert seen == [{"keep_disabled": expected_keep_disabled}]


@pytest.mark.asyncio
async def test_startup_refresh_clears_the_claude_record_after_an_update(real_agent_plugin_refresh, monkeypatch):
    server = _server(WhisperstreamConfig(claude_hud_enabled=True, claude_connected_plugin_version="1.7.0"))
    monkeypatch.setattr(
        server, "_apply_agent_plugin_action",
        lambda *_a, **_k: (SimpleNamespace(state=None, failure=None), {"ok": True, "status": "updated"}),
    )
    await server._refresh_claude_plugin()
    assert server._app.config.claude_connected_plugin_version == ""


def test_startup_schedules_refresh_for_owned_plugins_even_when_off(monkeypatch):
    server = _server(WhisperstreamConfig())
    monkeypatch.setattr(server, "_owned_plugin_on_disk", lambda agent: agent == "claude")
    started = []

    async def claude():
        started.append("claude")

    async def codex():
        started.append("codex")

    monkeypatch.setattr(server, "_refresh_claude_plugin", claude)
    monkeypatch.setattr(server, "_refresh_codex_plugin", codex)
    with TestClient(server.fastapi):
        pass
    assert started == ["claude"]


def test_codex_refresh_only_never_installs(tmp_path: Path, monkeypatch):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    calls: list[list[str]] = []

    def run(_executable: str, arguments: list[str]):
        calls.append(arguments)
        payload = {"marketplaces": []} if arguments[0] == "marketplace" else {"installed": [], "available": []}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "codex")
    monkeypatch.setattr(lifecycle, "_run_codex_plugin_cli", run)
    result = lifecycle.enable_codex_plugin(tmp_path / "app-data", refresh_only=True)
    assert result["ok"] is True and result["status"] == "not_installed"
    assert calls == [["marketplace", "list", "--json"]]


def test_claude_background_refresh_never_installs(tmp_path: Path, monkeypatch):
    import whisperstream.claude_hud as hud

    calls: list[list[str]] = []

    def run(_executable: str, arguments: list[str]):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(hud, "_claude_cli_path", lambda: "claude")
    monkeypatch.setattr(hud, "_run_claude_plugin_cli", run)
    result = hud.reconcile_claude_plugin(tmp_path, keep_disabled=True)
    assert result["ok"] is True and result["status"] == "not_installed"
    assert calls == [["marketplace", "list", "--json"]]
