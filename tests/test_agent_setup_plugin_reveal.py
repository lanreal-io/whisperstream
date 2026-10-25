"""The always-registered plugin-detail reveal route (Task 1.4).

``_public_claude_plugin_status`` deliberately strips ``plugin_dir`` and
``marketplace_dir`` from every other ``/api/claude-hud/*`` response. This
route consciously reverses that, only here, so setup step 1 can offer Show
in Explorer and an expandable view of the installed hook definition. Both
values are exposed as data only: they must never be logged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("accepted_eula")

ROUTE = "/api/setup/agent-speak/plugin-detail"


def _make_server(*, logger=None):
    from whisperstream.server import WhisperstreamServer

    config = MagicMock()
    config.claude_hud_enabled = False
    config.lockdown_mode = False

    app = MagicMock()
    app.config = config
    app.parakeet = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.dictation_state_snapshot.return_value = None
    return WhisperstreamServer(
        app, port=0, logger=logger, register_hotkeys=False
    )


def _client(server):
    return TestClient(server.fastapi, raise_server_exceptions=True)


def test_route_is_registered_without_dev_tools(monkeypatch):
    monkeypatch.delenv("WHISPERSTREAM_DEV_TOOLS", raising=False)
    server = _make_server()
    registered = {
        route.path
        for route in server.fastapi.routes
        if getattr(route, "path", None)
    }
    assert ROUTE in registered


def test_returns_nulls_when_plugin_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    server = _make_server()
    with _client(server) as client:
        response = client.get(ROUTE)

    assert response.status_code == 200
    body = response.json()
    assert body == {"plugin_dir": None, "hook_definition": None}


def test_returns_path_and_verbatim_hook_definition_when_installed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    from whisperstream.claude_hud import install_claude_plugin
    from whisperstream.config_manager import get_app_dir

    install_claude_plugin(get_app_dir())

    server = _make_server()
    with _client(server) as client:
        response = client.get(ROUTE)

    assert response.status_code == 200
    body = response.json()
    assert body["plugin_dir"]
    assert body["hook_definition"]

    from whisperstream.claude_hud import _owned_plugin_paths

    _marketplace_dir, plugin_dir = _owned_plugin_paths(get_app_dir())
    expected_hook_definition = (plugin_dir / "hooks" / "hooks.json").read_text(
        encoding="utf-8"
    )
    assert body["hook_definition"] == expected_hook_definition
    assert body["plugin_dir"] == str(plugin_dir)


def test_public_claude_plugin_status_still_strips_plugin_dir():
    from whisperstream.server import WhisperstreamServer

    result = {
        "ok": True,
        "status": "installed",
        "plugin_dir": "/some/path",
        "marketplace_dir": "/some/marketplace",
    }
    public = WhisperstreamServer._public_claude_plugin_status(result)
    assert "plugin_dir" not in public
    assert "marketplace_dir" not in public
    assert public["status"] == "installed"


def test_route_source_never_logs_plugin_detail_fields():
    import ast
    from pathlib import Path

    import whisperstream.server.routes.agent_setup as server_mod

    source = Path(server_mod.__file__).read_text(encoding="utf-8")
    owners = [node for node in ast.walk(ast.parse(source))
              if isinstance(node, ast.AsyncFunctionDef)
              and node.name in {"setup_agent_speak_plugin_detail",
                                "setup_agent_speak_codex_plugin_detail"}]
    assert len(owners) == 2
    for owner in owners:
        assert not any(isinstance(node, ast.Name) and node.id == "logger"
                       for node in ast.walk(owner))
