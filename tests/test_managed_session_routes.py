"""Tests for the managed-session REST routes (Task 2.3).

Registration and deregistration for `whisperstream claude`
wrapped sessions. All three require the per-run capability token (Task 2.1);
registration additionally reports a distinct machine-readable refusal when
the existing `claude_hud_enabled` gate is off, so the CLI can print the
locked HUD-off notice rather than the not-running one.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from whisperstream.managed_session_auth import HEADER_NAME, auth as managed_session_auth

pytestmark = pytest.mark.usefixtures("accepted_eula")


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "none"
    cfg.enhancement_local_model = "gemma4-e2b-instruct"
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.claude_hud_enabled = True
    cfg.codex_hud_enabled = True
    cfg.claude_hud_show_text = True
    cfg.lockdown_mode = False
    cfg.agent_speech_enabled = False
    cfg.agent_speech_activation = "per_session"
    cfg.claude_reply_grace_seconds = 6
    cfg.claude_reply_queue_delay_seconds = 2
    cfg.claude_reply_auto_submit = False
    cfg.claude_permission_decisions_enabled = False
    cfg.start_sound_path = None
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.to_dict = lambda: {}
    return cfg


def _make_client(*, config=None):
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.config = config or _make_mock_config()
    app.parakeet = MagicMock()
    app.llama_server = None
    app.config_manager = MagicMock()
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.set_event_broadcaster = MagicMock()
    app.audio_feedback = MagicMock()
    app.speech_controller = None
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    return TestClient(server.fastapi, raise_server_exceptions=True), server


@pytest.fixture()
def token(tmp_path):
    """Mint a capability token on the process-global singleton for one test."""
    value = managed_session_auth.mint(tmp_path)
    yield value
    managed_session_auth.clear(tmp_path)


def _headers(tok: str) -> dict:
    return {HEADER_NAME: tok}


def test_managed_cli_lifecycle_routes(monkeypatch):
    from whisperstream import managed_cli

    monkeypatch.setattr(
        managed_cli,
        "status",
        lambda: {
            "ok": True,
            "status": "not_installed",
            "available": True,
            "installed": False,
        },
    )
    monkeypatch.setattr(
        managed_cli,
        "install",
        lambda: {
            "ok": True,
            "status": "installed",
            "available": True,
            "installed": True,
        },
    )
    monkeypatch.setattr(
        managed_cli,
        "uninstall",
        lambda: {
            "ok": True,
            "status": "not_installed",
            "available": True,
            "installed": False,
        },
    )
    client, _server = _make_client()

    assert client.get("/api/managed-cli/status").json()["installed"] is False
    assert client.post("/api/managed-cli/install").json()["installed"] is True
    assert client.post("/api/managed-cli/uninstall").json()["installed"] is False


# ---------------------------------------------------------------------------
# Capability token gate (401)
# ---------------------------------------------------------------------------


def test_register_401_without_token(token):
    client, _server = _make_client()

    resp = client.post(
        "/api/managed-sessions/register",
        json={"token": "wrapper-tok", "pid": 111, "cwd": "/repo/one"},
    )

    assert resp.status_code == 401


def test_register_401_with_wrong_token(token):
    client, _server = _make_client()

    resp = client.post(
        "/api/managed-sessions/register",
        json={"token": "wrapper-tok", "pid": 111, "cwd": "/repo/one"},
        headers=_headers("not-the-token"),
    )

    assert resp.status_code == 401


def test_legacy_heartbeat_route_is_removed(token):
    client, _server = _make_client()

    resp = client.post(
        "/api/managed-sessions/heartbeat",
        json={"token": "wrapper-tok"},
        headers=_headers(token),
    )

    assert resp.status_code == 404


def test_deregister_401_without_token(token):
    client, _server = _make_client()

    resp = client.post(
        "/api/managed-sessions/deregister", json={"token": "wrapper-tok"}
    )

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# claude_hud_enabled gate (409 hud_disabled)
# ---------------------------------------------------------------------------


def test_register_409_hud_disabled_when_gate_off(token):
    client, _server = _make_client(config=_make_mock_config(claude_hud_enabled=False))

    resp = client.post(
        "/api/managed-sessions/register",
        json={"token": "wrapper-tok", "pid": 111, "cwd": "/repo/one"},
        headers=_headers(token),
    )

    assert resp.status_code == 409
    assert resp.json() == {"error": "hud_disabled"}


def test_register_succeeds_when_gate_on(token):
    client, _server = _make_client(config=_make_mock_config(claude_hud_enabled=True))

    resp = client.post(
        "/api/managed-sessions/register",
        json={"token": "wrapper-tok", "pid": 111, "cwd": "/repo/one"},
        headers=_headers(token),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["session_handle"], str)
    assert body["session_handle"]
    assert set(body) == {"session_handle"}


def test_v2_codex_registration_echoes_the_provider_and_protocol(token):
    client, server = _make_client()
    resp = client.post(
        "/api/managed-sessions/v2/register",
        json={"agent": "codex", "token": "wrapper-tok", "pid": 111, "cwd": "/repo/one"},
        headers=_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["agent"] == "codex"
    assert resp.json()["protocol_version"] == 2
    assert server._get_managed_sessions().get("wrapper-tok").agent == "codex"


def test_v2_registration_uses_the_requested_provider_gate(token):
    client, server = _make_client(config=_make_mock_config(codex_hud_enabled=False))
    resp = client.post(
        "/api/managed-sessions/v2/register",
        json={"agent": "codex", "token": "wrapper-tok"},
        headers=_headers(token),
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": "hud_disabled"}
    assert server._managed_sessions is None


def test_v2_registration_rejects_cross_provider_token_reuse(token):
    client, server = _make_client()
    server._get_managed_sessions().register("wrapper-tok", agent="claude")
    resp = client.post(
        "/api/managed-sessions/v2/register",
        json={"agent": "codex", "token": "wrapper-tok"},
        headers=_headers(token),
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": "provider_mismatch"}
    assert server._get_managed_sessions().get("wrapper-tok").agent == "claude"


def test_lockdown_blocks_registration_without_constructing_the_store(token):
    client, server = _make_client(config=_make_mock_config(lockdown_mode=True))
    resp = client.post(
        "/api/managed-sessions/v2/register",
        json={"agent": "codex", "token": "wrapper-tok"},
        headers=_headers(token),
    )
    assert resp.status_code == 423
    assert resp.json() == {"error": "lockdown"}
    assert server._managed_sessions is None


def test_selective_provider_cleanup_preserves_other_sessions_and_selection():
    _client, server = _make_client()
    server.broadcast = MagicMock()
    store = server._get_managed_sessions()
    claude = store.register("claude-token", agent="claude")
    codex = store.register("codex-token", agent="codex")
    server._record_managed_event("claude-token", "Stop", agent="claude", session_id="claude-sid")
    server._record_managed_event("codex-token", "Stop", agent="codex", session_id="codex-sid")
    desk = server._get_reply_desk()
    claude_target = server._managed_reply_target(claude)
    codex_target = server._managed_reply_target(codex)
    desk.update(target_id=claude_target, base_revision=0, text="keep")
    desk.update(target_id=codex_target, base_revision=0, text="drop")
    server.broadcast.reset_mock()

    server._remove_managed_sessions_for_agent("codex")

    assert store.sessions() == (claude,)
    assert desk.draft(codex_target) is None
    assert desk.draft(claude_target).text == "keep"
    event = next(
        call.args[0] for call in reversed(server.broadcast.call_args_list)
        if call.args[0]["type"] == "agent.sessions"
    )
    assert [item["agent"] for item in event["sessions"]] == ["claude"]


# ---------------------------------------------------------------------------
# Registration is idempotent on token
# ---------------------------------------------------------------------------


def test_registration_is_idempotent_on_token(token):
    client, _server = _make_client()

    first = client.post(
        "/api/managed-sessions/register",
        json={"token": "wrapper-tok", "pid": 111, "cwd": "/repo/one"},
        headers=_headers(token),
    )
    second = client.post(
        "/api/managed-sessions/register",
        json={"token": "wrapper-tok", "pid": 222, "cwd": "/repo/one"},
        headers=_headers(token),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["session_handle"] == second.json()["session_handle"]
    assert len(_server._get_managed_sessions()) == 1


# ---------------------------------------------------------------------------
# Deregistration removes the record
# ---------------------------------------------------------------------------


def test_deregistration_removes_the_record(token):
    client, server = _make_client()
    client.post(
        "/api/managed-sessions/register",
        json={"token": "wrapper-tok", "pid": 111, "cwd": "/repo/one"},
        headers=_headers(token),
    )
    store = server._get_managed_sessions()
    assert store.get("wrapper-tok") is not None

    resp = client.post(
        "/api/managed-sessions/deregister",
        json={"token": "wrapper-tok"},
        headers=_headers(token),
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert store.get("wrapper-tok") is None


def test_deregistration_of_unknown_token_reports_not_ok(token):
    client, _server = _make_client()

    resp = client.post(
        "/api/managed-sessions/deregister",
        json={"token": "ghost"},
        headers=_headers(token),
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": False}
