"""Mac history consent boundaries; all native storage is simulated."""
from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from whisperstream import config_manager, main as main_mod
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.main import WhisperstreamApp
from tests.test_eula_server_boundary import _make_server, ACCEPTED, BLOCKED


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
def test_platform_defaults_and_legacy_history_require_explicit_mac_opt_in(monkeypatch, system):
    monkeypatch.setattr(config_manager.platform, "system", lambda: system)
    assert WhisperstreamConfig().history_enabled is (system != "Darwin")
    old = WhisperstreamConfig.from_dict({"history_enabled": True})
    assert old.history_enabled is (system != "Darwin")
    assert old.mac_history_opt_in is False
    opted = WhisperstreamConfig.from_dict({"history_enabled": True, "mac_history_opt_in": True})
    assert opted.history_enabled is True
    paused = WhisperstreamConfig.from_dict({"history_enabled": False, "mac_history_opt_in": True})
    assert paused.history_enabled is False


def _app_server(monkeypatch):
    monkeypatch.setattr(config_manager.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    server, app, current = _make_server(ACCEPTED)
    app.config = WhisperstreamConfig()
    app.config_manager.save = MagicMock()
    app.reload_config = MagicMock(side_effect=lambda cfg: setattr(app, "config", cfg))
    app._headless = True
    app._file_only = False
    app._shutting_down = False
    app._transcript_store_lock = threading.Lock()
    app._transcript_store_start_lock = threading.Lock()
    app._transcript_store_publish_lock = threading.Lock()
    app._transcript_store_init_thread = None
    app._transcript_store_initialized = False
    app.transcript_store = None
    type(app).history_initializing = WhisperstreamApp.history_initializing
    app.initialize_accepted_content_services = WhisperstreamApp.initialize_accepted_content_services.__get__(app)
    app.ensure_transcript_store_initialized = WhisperstreamApp.ensure_transcript_store_initialized.__get__(app)
    monkeypatch.setattr(main_mod, "run_retention_sweep", MagicMock())
    return server, app, current


def test_startup_and_config_updates_cannot_trigger_unconsented_history(monkeypatch, tmp_path):
    server, app, _ = _app_server(monkeypatch)
    make_store = MagicMock()
    monkeypatch.setattr(main_mod, "TranscriptStore", make_store)
    # Existing files must survive the default change without being opened.
    saved = tmp_path / "existing.encrypted"
    saved.write_bytes(b"synthetic encrypted bytes")
    with TestClient(server.fastapi) as client:
        assert client.get("/api/health").status_code == 200
        assert app.ensure_transcript_store_initialized() is None
        for body in ({"history_enabled": True}, {"history_enabled": True, "mac_history_opt_in": True}):
            assert client.put("/api/config", json=body).status_code == 409
        assert client.post("/api/transcripts/enable", json={}).status_code == 400
        make_store.assert_not_called()
        app.config_manager.save.assert_not_called()
        assert app.config.history_enabled is False
    assert saved.read_bytes() == b"synthetic encrypted bytes"


@pytest.mark.parametrize("outcome", ["ready", "denied", "revoked"])
def test_explicit_enable_commits_only_after_usable_key_and_current_grant(monkeypatch, outcome):
    server, app, current = _app_server(monkeypatch)
    store = MagicMock()
    store.decryptable = outcome != "denied"

    def create_store():
        from whisperstream.macos_protected_secret import _ALLOW_INTERACTION
        assert _ALLOW_INTERACTION.get() is True
        if outcome == "revoked":
            current["grant"] = BLOCKED
        return store

    make_store = MagicMock(side_effect=create_store)
    monkeypatch.setattr(main_mod, "TranscriptStore", make_store)
    with TestClient(server.fastapi) as client:
        response = client.post("/api/transcripts/enable", json={"keychain_consent": True})
        make_store.assert_called_once()
        if outcome == "ready":
            assert response.status_code == 200
            assert app.config.mac_history_opt_in is True
            assert app.config.history_enabled is True
            app.config_manager.save.assert_called_once()
        else:
            assert response.status_code == (503 if outcome == "denied" else 403)
            assert app.config.mac_history_opt_in is False
            assert app.config.history_enabled is False
            app.config_manager.save.assert_not_called()
            if outcome == "denied":
                # OS denial is recoverable without restarting or re-saving config.
                ready_store = MagicMock()
                ready_store.decryptable = True
                make_store.side_effect = lambda: ready_store
                retry = client.post("/api/transcripts/enable", json={"keychain_consent": True})
                assert retry.status_code == 200
                assert make_store.call_count == 2


def test_lockdown_password_setup_also_requires_consent_before_key_access(monkeypatch):
    server, app, _ = _app_server(monkeypatch)
    enable = MagicMock()
    monkeypatch.setattr("whisperstream.server.app.lockdown_keystore.enable_lockdown", enable)
    with TestClient(server.fastapi) as client:
        response = client.post("/api/lockdown/enable", json={"password": "synthetic-password", "history_enabled": True})
        assert response.status_code == 409
        assert response.json()["error_code"] == "history_consent_required"
        enable.assert_not_called()


@pytest.mark.parametrize("locked", [True, False])
def test_existing_lockdown_history_uses_password_consent_without_keychain(monkeypatch, tmp_path, locked):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    # Store construction only needs presence; unlocking was completed earlier.
    (tmp_path / "lockdown.key.json").write_text("{}")
    server, app, _ = _app_server(monkeypatch)
    app.config.lockdown_mode = True
    app.config.enhancement_provider = "local"
    if locked:
        server._lockdown_state.lock()
    else:
        server._lockdown_state.unlock(b"s" * 32)
    keychain_read = MagicMock(side_effect=AssertionError("password history must not read Keychain"))
    monkeypatch.setattr(
        "whisperstream.transcript_store.load_or_create_data_key_with_failure", keychain_read
    )
    monkeypatch.setattr("whisperstream.transcript_store.load_data_key", keychain_read)
    try:
        with TestClient(server.fastapi) as client:
            response = client.post("/api/transcripts/enable", json={"history_consent": True})
            assert response.status_code == (423 if locked else 200)
            assert app.config.history_enabled is (not locked)
            assert app.config.mac_history_opt_in is (not locked)
            keychain_read.assert_not_called()
            if not locked:
                assert app.transcript_store.decryptable is True
    finally:
        if app.transcript_store is not None:
            app.transcript_store.close()


def test_lockdown_rechecks_pending_history_after_request_body_parsing(monkeypatch):
    server, app, _ = _app_server(monkeypatch)
    enable = MagicMock()
    monkeypatch.setattr("whisperstream.server.app.lockdown_keystore.enable_lockdown", enable)
    with TestClient(server.fastapi) as client:
        # Exercise the real status property and response helper on both sides
        # of body parsing, after startup has finished its own status reads.
        app._transcript_store_init_thread = MagicMock()
        app._transcript_store_init_thread.is_alive.side_effect = [False, True]
        result = client.post("/api/lockdown/enable", json={
            "password": "synthetic-password", "keychain_consent": True,
        })
    assert result.status_code == 503
    assert result.json()["detail"] == "history_initializing"
    enable.assert_not_called()
    app.config_manager.save.assert_not_called()
