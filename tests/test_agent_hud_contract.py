"""Platform-neutral HUD window and agent-view routing contracts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.usefixtures("accepted_eula")


def test_hud_is_the_only_agent_interaction_window() -> None:
    config = json.loads(
        (ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    windows = {window["label"]: window for window in config["app"]["windows"]}

    assert "agent-console" not in windows
    assert windows["hud"]["focusable"] is False

    capability = json.loads(
        (ROOT / "src-tauri" / "capabilities" / "default.json").read_text(
            encoding="utf-8"
        )
    )
    assert "agent-console" not in capability["windows"]


def test_frontend_has_no_secondary_agent_window_or_click_through() -> None:
    rust = (ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    main = (ROOT / "frontend" / "src" / "main.tsx").read_text(encoding="utf-8")
    hud = (ROOT / "frontend" / "src" / "pages" / "HUD.tsx").read_text(
        encoding="utf-8"
    )

    assert 'get_webview_window("agent-console")' not in rust
    assert 'pathname === "/agent-console"' not in main
    assert "openAgentConsole" not in hud
    assert "toggleHudExpansion" in hud
    assert 'listen("agent-hud-view", () => { if (!disposed) toggleHudExpansion(); })' in hud


def test_view_shortcut_broadcasts_only_the_fail_safe_hud_event() -> None:
    from whisperstream.main import WhisperstreamApp

    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app._broadcast = MagicMock()
    app.logger = MagicMock()

    app._on_claude_view_press()

    app._broadcast.assert_called_once_with({"type": "agent-hud-view"})
    app.logger.warning.assert_not_called()
