"""REST lifecycle and setup-detail contracts for the Codex plugin."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("accepted_eula")


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


def _server(*, codex_enabled: bool = False, lockdown: bool = False, logger=None):
    from whisperstream.server import WhisperstreamServer

    config = MagicMock()
    config.claude_hud_enabled = False
    config.codex_hud_enabled = codex_enabled
    config.lockdown_mode = lockdown
    app = MagicMock()
    app.config = config
    app.parakeet = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.dictation_state_snapshot.return_value = None
    return WhisperstreamServer(app, port=0, logger=logger, register_hotkeys=False)


def test_codex_lifecycle_and_detail_routes_are_always_registered(monkeypatch):
    monkeypatch.delenv("WHISPERSTREAM_DEV_TOOLS", raising=False)
    server = _server()
    paths = {getattr(route, "path", None) for route in server.fastapi.routes}
    assert {
        "/api/codex-hud/enable",
        "/api/codex-hud/status",
        "/api/codex-hud/disable",
        "/api/setup/agent-speak/codex-plugin-detail",
    }.issubset(paths)


def test_enable_exposes_content_free_status_and_strips_paths(monkeypatch):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "enable_codex_plugin",
        lambda _app_dir: {
            "ok": True,
            "status": "installed",
            "source_status": "installed",
            "version": "1.0.0",
            "installed_version": "1.0.0",
            "codex_cli_available": True,
            "codex_cli_supported": True,
            "trust_required": True,
            "trust_message": "Open Codex and run /hooks to trust Whisperstream.",
            "plugin_dir": "C:/private/user/path",
            "marketplace_dir": "C:/private/user/path/marketplace",
        },
    )
    server = _server()

    with TestClient(server.fastapi) as client:
        response = client.post("/api/codex-hud/enable")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "installed"
    assert body["trust_required"] is True
    assert "plugin_dir" not in body
    assert "marketplace_dir" not in body


def test_enable_is_blocked_in_lockdown_without_calling_lifecycle(monkeypatch):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    called = False

    def enable(_app_dir):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(lifecycle, "enable_codex_plugin", enable)
    server = _server(lockdown=True)

    with TestClient(server.fastapi) as client:
        response = client.post("/api/codex-hud/enable")

    assert response.status_code == 423
    assert called is False


def test_disable_does_not_clear_claude_connection(monkeypatch):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "disable_codex_plugin",
        lambda _app_dir: {
            "ok": True,
            "status": "removed",
            "codex_cli_available": True,
            "codex_cli_supported": True,
            "trust_required": False,
        },
    )
    server = _server(codex_enabled=True)
    server._app.config.claude_hud_enabled = True

    with TestClient(server.fastapi) as client:
        response = client.post("/api/codex-hud/disable")

    assert response.status_code == 200
    assert server._app.config.claude_hud_enabled is True


def test_partial_config_put_preserves_codex_connection(monkeypatch):
    from whisperstream.config_manager import WhisperstreamConfig
    from whisperstream.server import WhisperstreamServer
    import whisperstream.codex_plugin_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "enable_codex_plugin",
        lambda _app_dir: {
            "ok": True,
            "status": "installed",
            "codex_cli_available": True,
            "codex_cli_supported": True,
            "trust_required": True,
        },
    )
    app = MagicMock()
    app.config = WhisperstreamConfig(codex_hud_enabled=True)
    app.parakeet = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.dictation_state_snapshot.return_value = None
    app.reload_config.side_effect = lambda config: setattr(app, "config", config)
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)

    with TestClient(server.fastapi) as client:
        response = client.put(
            "/api/config",
            json={"claude_view_shortcut": "ctrl+shift+v"},
        )

    assert response.status_code == 200
    assert response.json()["codex_hud_enabled"] is True
    assert response.json()["claude_view_shortcut"] == "ctrl+shift+v"
    assert app.config.codex_hud_enabled is True


def test_setup_detail_returns_only_owned_payload_data(tmp_path: Path):
    from whisperstream.codex_plugin_lifecycle import materialize_codex_plugin
    from whisperstream.config_manager import get_app_dir

    materialized = materialize_codex_plugin(get_app_dir())
    expected = (
        Path(materialized["plugin_dir"]) / "hooks" / "hooks.json"
    ).read_text("utf-8")
    server = _server()

    with TestClient(server.fastapi) as client:
        response = client.get("/api/setup/agent-speak/codex-plugin-detail")

    assert response.status_code == 200
    assert response.json() == {
        "plugin_dir": materialized["plugin_dir"],
        "hook_definition": expected,
    }


def test_setup_detail_returns_nulls_for_unowned_or_missing_payload():
    server = _server()

    with TestClient(server.fastapi) as client:
        response = client.get("/api/setup/agent-speak/codex-plugin-detail")

    assert response.status_code == 200
    assert response.json() == {"plugin_dir": None, "hook_definition": None}


def test_public_status_allowlist_drops_error_details_and_paths():
    from whisperstream.server import WhisperstreamServer

    result = WhisperstreamServer._public_codex_plugin_status(
        {
            "ok": False,
            "status": "failed",
            "error": "marketplace_add_failed",
            "plugin_dir": "C:/private/path",
            "marketplace_dir": "C:/private/path/marketplace",
            "stderr": "private content",
        }
    )

    assert result == {"ok": False, "status": "failed"}


@pytest.mark.parametrize(("body", "expected"), [(None, False), ({"force": True}, True), ({"force": "yes"}, False)])
def test_enable_passes_connect_force_only_when_requested(monkeypatch, body, expected):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    seen = []

    def enable(_app_dir, **kwargs):
        seen.append(kwargs.get("force", False))
        return {"ok": True, "status": "current", "codex_cli_available": True,
                "codex_cli_supported": True, "trust_required": False}

    monkeypatch.setattr(lifecycle, "enable_codex_plugin", enable)
    server = _server()
    with TestClient(server.fastapi) as client:
        response = client.post("/api/codex-hud/enable", json=body) if body is not None else client.post("/api/codex-hud/enable")
    assert response.status_code == 200
    assert seen == [expected]


def test_status_polls_reuse_a_recent_read_until_refresh_or_a_write(monkeypatch):
    import contextlib

    import whisperstream.codex_plugin_lifecycle as lifecycle

    calls = 0

    def status(_app_dir):
        nonlocal calls
        calls += 1
        return {"ok": True, "status": "current", "version": "1.0.0", "installed_version": "1.0.0"}

    monkeypatch.setattr(lifecycle, "codex_plugin_status", status)
    monkeypatch.setattr(lifecycle, "codex_plugin_runtime", contextlib.nullcontext)
    monkeypatch.setattr(lifecycle, "enable_codex_plugin", lambda _app_dir: {"ok": True, "status": "installed"})
    server = _server()
    server._agent_runtime_status = AsyncMock(return_value={"surfaces": []})

    with TestClient(server.fastapi) as client:
        assert client.get("/api/codex-hud/status").json()["status"] == "current"
        assert client.get("/api/codex-hud/status").json()["status"] == "current"
        assert calls == 1
        client.get("/api/codex-hud/status", params={"refresh": "true"})
        assert calls == 2
        client.post("/api/codex-hud/enable")
        client.get("/api/codex-hud/status")
        assert calls == 3
