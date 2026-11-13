"""Agents on/off shortcut: config field, hotkey wiring and the server toggle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from whisperstream.config_manager import WhisperstreamConfig

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


def _run_toggle(server, events: list[dict]) -> None:
    loop = asyncio.new_event_loop()
    server._loop = loop
    server.broadcast = lambda event: events.append(event)
    try:
        loop.run_until_complete(server._toggle_agents_from_shortcut())
    finally:
        loop.close()
        server._loop = None


def _toggled(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("type") == "agents.toggled"]


# --- config -----------------------------------------------------------------


def test_agents_toggle_shortcut_defaults_unbound_and_normalizes() -> None:
    assert WhisperstreamConfig().agents_toggle_shortcut == ""
    assert WhisperstreamConfig.from_dict({}).agents_toggle_shortcut == ""
    assert (
        WhisperstreamConfig.from_dict({"agents_toggle_shortcut": "CTRL+ALT+A"})
        .agents_toggle_shortcut
        == "ctrl+alt+a"
    )


@pytest.mark.parametrize(
    "other",
    [
        {"toggle_shortcut": "ctrl+alt+a"},
        {"claude_view_shortcut": "ctrl+alt+a"},
        {"profile_hotkeys": {"profile-1": "CTRL+ALT+A"}},
    ],
)
def test_agents_toggle_shortcut_clears_on_conflict(other: dict) -> None:
    conf = WhisperstreamConfig(agents_toggle_shortcut="ctrl+alt+a", **other)
    conf.validate()
    assert conf.agents_toggle_shortcut == ""


def test_agents_toggle_shortcut_keeps_a_free_binding() -> None:
    conf = WhisperstreamConfig(agents_toggle_shortcut="ctrl+alt+a")
    conf.validate()
    assert conf.agents_toggle_shortcut == "ctrl+alt+a"


# --- hotkey wiring ------------------------------------------------------------


def test_press_hands_off_to_server_handler() -> None:
    from whisperstream.main import WhisperstreamApp

    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._require_acceptance = MagicMock(return_value=True)
    handler = MagicMock()
    app.set_agents_toggle_handler(handler)

    app._on_agents_toggle_press()

    handler.assert_called_once_with()


def test_press_without_acceptance_does_nothing() -> None:
    from whisperstream.main import WhisperstreamApp

    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._require_acceptance = MagicMock(return_value=False)
    handler = MagicMock()
    app.set_agents_toggle_handler(handler)

    app._on_agents_toggle_press()

    handler.assert_not_called()


def test_server_installs_its_toggle_handler() -> None:
    server = _server(WhisperstreamConfig())
    server._app.set_agents_toggle_handler.assert_called_once_with(
        server._schedule_agents_shortcut_toggle
    )


# --- server toggle ------------------------------------------------------------


def test_shortcut_pauses_and_clears_agent_state(monkeypatch) -> None:
    server = _server(
        WhisperstreamConfig(
            agent_speak_setup_completed=True,
            claude_hud_enabled=True,
            codex_hud_enabled=True,
        )
    )
    paused: list[bool] = []
    monkeypatch.setattr(server, "_schedule_claude_pause", lambda: paused.append(True))
    cleared: list[str] = []
    monkeypatch.setattr(
        server, "_clear_agent_speak_state", lambda *, reason: cleared.append(reason)
    )
    events: list[dict] = []

    _run_toggle(server, events)

    assert server._app.config.agents_enabled is False
    # The remembered providers survive the pause.
    assert server._app.config.claude_hud_enabled is True
    assert server._app.config.codex_hud_enabled is True
    server._app.config_manager.save.assert_called_once()
    assert paused == [True]
    assert cleared == ["feature_paused"]
    assert _toggled(events) == [
        {"type": "agents.toggled", "enabled": False, "attention": None, "settled": True}
    ]


def test_shortcut_resumes_remembered_providers_and_settles(monkeypatch) -> None:
    server = _server(
        WhisperstreamConfig(
            agent_speak_setup_completed=True,
            agents_enabled=False,
            claude_hud_enabled=True,
            codex_hud_enabled=False,
        )
    )
    claude = AsyncMock(return_value=(200, {"ok": True, "status": "installed"}))
    codex = AsyncMock(return_value=(200, {"ok": True}))
    monkeypatch.setattr(server, "_enable_claude_plugin", claude)
    monkeypatch.setattr(server, "_enable_codex_plugin", codex)
    events: list[dict] = []

    _run_toggle(server, events)

    assert server._app.config.agents_enabled is True
    claude.assert_awaited_once_with(force=False)
    codex.assert_not_awaited()
    assert _toggled(events) == [
        {"type": "agents.toggled", "enabled": True, "attention": None, "settled": False},
        {"type": "agents.toggled", "enabled": True, "attention": None, "settled": True},
    ]


@pytest.mark.parametrize(
    "codex_result",
    [
        (200, {"ok": True, "trust_required": True}),
        (200, {"ok": False, "status": "failed"}),
        (500, {"ok": False, "error": "RuntimeError"}),
    ],
)
def test_shortcut_resume_names_provider_that_needs_the_user(
    monkeypatch, codex_result
) -> None:
    server = _server(
        WhisperstreamConfig(
            agent_speak_setup_completed=True,
            agents_enabled=False,
            claude_hud_enabled=True,
            codex_hud_enabled=True,
        )
    )
    monkeypatch.setattr(
        server,
        "_enable_claude_plugin",
        AsyncMock(return_value=(200, {"ok": True, "status": "installed"})),
    )
    monkeypatch.setattr(
        server, "_enable_codex_plugin", AsyncMock(return_value=codex_result)
    )
    events: list[dict] = []

    _run_toggle(server, events)

    assert _toggled(events)[-1] == {
        "type": "agents.toggled", "enabled": True, "attention": "codex", "settled": True,
    }


def test_shortcut_does_nothing_before_setup_completes() -> None:
    server = _server(WhisperstreamConfig(agent_speak_setup_completed=False))
    events: list[dict] = []

    _run_toggle(server, events)

    assert server._app.config.agents_enabled is True
    server._app.config_manager.save.assert_not_called()
    assert _toggled(events) == []


def test_shortcut_failed_save_changes_nothing() -> None:
    server = _server(WhisperstreamConfig(agent_speak_setup_completed=True))
    server._app.config_manager.save.side_effect = OSError("disk")
    events: list[dict] = []

    _run_toggle(server, events)

    assert server._app.config.agents_enabled is True
    assert _toggled(events) == []
