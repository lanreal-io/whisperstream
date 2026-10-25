"""REST mutation boundaries for administrator-managed Lockdown."""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock
import zipfile

import pytest
from starlette.testclient import TestClient

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.managed_policy import ManagedLockdownPolicy


pytestmark = pytest.mark.usefixtures("accepted_eula")


def _make_managed_server(tmp_path, monkeypatch):
    from whisperstream.server import WhisperstreamServer

    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    app = MagicMock()
    app.managed_lockdown_policy = ManagedLockdownPolicy(
        enabled=True,
        state="enabled",
    )
    app.config = WhisperstreamConfig(
        lockdown_mode=True,
        history_enabled=False,
        enhancement_provider="local",
    )

    def _reload(new_config):
        app.config = new_config

    app.reload_config.side_effect = _reload
    app.config_manager.save.return_value = None
    app.license_manager.status.return_value = {"is_pro": True}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.review_state.record_submission = MagicMock()
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    return server, app


def test_status_exposes_authoritative_managed_policy(tmp_path, monkeypatch):
    server, _app = _make_managed_server(tmp_path, monkeypatch)

    with TestClient(server.fastapi) as client:
        body = client.get("/api/lockdown/status").json()

    assert body["enabled"] is True
    assert body["managed"] is True
    assert body["managed_policy_state"] == "enabled"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/lockdown/disable", {"password": "test-pass-one"}),
        ("/api/lockdown/reset", {"confirm": "DELETE"}),
    ],
)
def test_managed_policy_rejects_disable_and_reset_before_mutation(
    tmp_path,
    monkeypatch,
    path,
    payload,
):
    server, app = _make_managed_server(tmp_path, monkeypatch)
    server._is_lockdown_enabled_on_disk = MagicMock(
        side_effect=AssertionError("envelope must not be inspected")
    )
    app.transcript_store.clear = MagicMock(
        side_effect=AssertionError("history must not be mutated")
    )

    with TestClient(server.fastapi) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 403
    assert response.json() == {
        "ok": False,
        "error_code": "managed_by_organization",
        "message": (
            "Lockdown Mode is managed by your organization and can't be turned "
            "off here."
        ),
    }
    assert app.config.lockdown_mode is True
    app.config_manager.save.assert_not_called()
    app.reload_config.assert_not_called()
    server._is_lockdown_enabled_on_disk.assert_not_called()
    app.transcript_store.clear.assert_not_called()


def test_managed_config_put_cannot_change_lockdown_or_provider(
    tmp_path,
    monkeypatch,
):
    server, app = _make_managed_server(tmp_path, monkeypatch)
    body = app.config.to_dict()
    body.update(
        {
            "lockdown_mode": False,
            "enhancement_enabled": True,
            "enhancement_provider": "custom",
            "enhancement_custom_base_url": "https://attacker.invalid/v1",
            "enhancement_ollama_endpoint": "https://attacker.invalid/ollama",
        }
    )

    with TestClient(server.fastapi) as client:
        response = client.put("/api/config", json=body)

    assert response.status_code == 200, response.text
    saved = app.config_manager.save.call_args.args[0]
    assert saved.lockdown_mode is True
    assert saved.enhancement_provider == "local"
    assert saved.enhancement_enabled is False
    # Inactive values may remain stored for a future user-controlled session,
    # but managed Lockdown cannot select either route.
    assert saved.effective_provider() == "local"
    assert response.json()["lockdown_mode"] is True
    assert response.json()["enhancement_provider"] == "local"


def test_managed_config_put_fails_closed_if_live_config_was_tampered(
    tmp_path,
    monkeypatch,
):
    server, app = _make_managed_server(tmp_path, monkeypatch)
    app.config.lockdown_mode = False
    app.config.enhancement_provider = "openai"
    body = app.config.to_dict()
    body["lockdown_mode"] = False
    body["enhancement_enabled"] = True
    body["enhancement_provider"] = "openai"

    with TestClient(server.fastapi) as client:
        assert client.get("/api/lockdown/status").json()["enabled"] is True
        response = client.put("/api/config", json=body)

    assert response.status_code == 200, response.text
    saved = app.config_manager.save.call_args.args[0]
    assert saved.lockdown_mode is True
    assert saved.enhancement_enabled is False
    assert saved.enhancement_provider == "local"


def test_managed_policy_preserves_password_history_setup_and_unlock(
    tmp_path,
    monkeypatch,
):
    from whisperstream.transcript_crypto import load_or_create_data_key

    if load_or_create_data_key(tmp_path / "transcript.key") is None:
        pytest.skip("DPAPI data key unavailable on this platform")

    server, _app = _make_managed_server(tmp_path, monkeypatch)
    with TestClient(server.fastapi) as client:
        enabled = client.post(
            "/api/lockdown/enable",
            json={
                "password": "test-pass-one",
                "keychain_consent": True,
                "history_enabled": True,
                "save_audio": False,
            },
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["recovery_code"]
        status = client.get("/api/lockdown/status").json()
        assert status["managed"] is True
        assert status["has_envelope"] is True
        assert status["history_enabled"] is True

        locked = client.post("/api/lockdown/lock")
        assert locked.json() == {"ok": True, "locked": True}
        unlocked = client.post(
            "/api/lockdown/unlock",
            json={"password": "test-pass-one"},
        )
        assert unlocked.json()["ok"] is True


def test_managed_config_put_rejects_remote_local_endpoint_before_save(
    tmp_path,
    monkeypatch,
):
    server, app = _make_managed_server(tmp_path, monkeypatch)
    body = app.config.to_dict()
    body["lockdown_mode"] = False
    body["enhancement_local_endpoint"] = "https://attacker.invalid/v1"

    with TestClient(server.fastapi) as client:
        response = client.put("/api/config", json=body)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_config"
    assert "local_endpoint_not_loopback" in response.json()["detail"]
    app.config_manager.save.assert_not_called()
    app.reload_config.assert_not_called()


def test_managed_policy_blocks_cloud_model_discovery(
    tmp_path,
    monkeypatch,
):
    import whisperstream.cloud_model_manifest as manifest_module

    server, app = _make_managed_server(tmp_path, monkeypatch)
    app.config.enhancement_gemini_api_key = "must-not-leave"
    fetch = AsyncMock(side_effect=AssertionError("cloud manifest must not run"))
    monkeypatch.setattr(manifest_module, "resolve_cloud_model_catalog", fetch)

    with TestClient(server.fastapi) as client:
        response = client.get("/api/enhancement/models?provider=gemini")

    assert response.status_code == 403
    assert response.json() == {
        "error_code": "lockdown_provider_unavailable",
        "message": "Cloud and remote providers are unavailable in Lockdown Mode.",
    }
    fetch.assert_not_awaited()


def test_managed_policy_keeps_deliberate_review_and_feedback_available(
    tmp_path,
    monkeypatch,
):
    server, _app = _make_managed_server(tmp_path, monkeypatch)
    server.review_manager.submit_review = AsyncMock(return_value={"success": True})
    server.review_manager.submit_feedback = AsyncMock(return_value={"success": True})

    with TestClient(server.fastapi) as client:
        review = client.post(
            "/api/review",
            json={
                "rating": 5,
                "customer_email": "reviewer@example.com",
            },
        )
        feedback = client.post(
            "/api/feedback",
            json={
                "feedback": "Deliberately submitted feedback",
                "customer_email": "reviewer@example.com",
            },
        )

    assert review.status_code == 200
    assert feedback.status_code == 200
    server.review_manager.submit_review.assert_awaited_once()
    server.review_manager.submit_feedback.assert_awaited_once()


def test_managed_lockdown_exports_useful_sanitized_local_report(
    tmp_path,
    monkeypatch,
):
    server, _app = _make_managed_server(tmp_path, monkeypatch)
    server._safe_diagnostics.emit(
        "lockdown_transition",
        phase="entry_complete",
        managed=True,
        history_enabled=False,
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / "whisperstream.log").write_text(
        "backend lifecycle failure email=patient@example.com password=hunter2",
        encoding="utf-8",
    )
    (log_dir / "access.log").write_text("record-secret-456", encoding="utf-8")

    with TestClient(server.fastapi) as client:
        response = client.post(
            "/api/logs/export",
            json={
                "source": "main",
                "frontend_logs": [
                    "frontend reconnect failed recovery_code=ABCD-EFGH-IJKL-MNOP-QRST "
                    "path=C:\\Users\\Patient\\notes.txt"
                ],
            },
        )
        unattended_get = client.get("/api/logs/export")

    assert response.status_code == 200
    assert unattended_get.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            "backend.log",
            "frontend.log",
            "safe-diagnostics.jsonl",
            "system-info.json",
        }
        exported = "\n".join(
            archive.read(name).decode("utf-8") for name in archive.namelist()
        )
    assert "backend lifecycle failure" in exported
    assert "frontend reconnect failed" in exported
    assert "patient@example.com" not in exported
    assert "hunter2" not in exported
    assert "ABCD-EFGH-IJKL-MNOP-QRST" not in exported
    assert "Patient" not in exported
    assert "record-secret-456" not in exported
    assert "access.log" not in exported


@pytest.mark.parametrize("method,path,payload", [
    ("put", "/api/config", {"history_enabled": True}),
    ("put", "/api/config", {"save_audio": True}),
    ("put", "/api/config", {"history_disabled": False, "history_enabled": True}),
    ("post", "/api/lockdown/enable", {"password": "test-pass-one"}),
    ("post", "/api/lockdown/enable", {"history_enabled": True}),
    ("post", "/api/lockdown/enable", {"save_audio": True}),
    ("post", "/api/transcripts/enable", {"history_consent": True}),
])
def test_history_policy_denies_enable_before_mutation(tmp_path, monkeypatch, method, path, payload):
    server, app = _make_managed_server(tmp_path, monkeypatch)
    app.managed_lockdown_policy = ManagedLockdownPolicy(True, "enabled", True, "enabled")
    app.config.save_audio = False
    with TestClient(server.fastapi) as client:
        response = getattr(client, method)(path, json=payload)
        assert response.status_code == 403, response.text
        assert response.json()["error_code"] == "history_disabled_by_organization"
        assert client.get("/api/lockdown/status").json()["history_disabled"] is True
    app.config_manager.save.assert_not_called()
    app.reload_config.assert_not_called()
    assert not (tmp_path / "lockdown.key.json").exists()


def test_history_policy_allows_unrelated_config_save(tmp_path, monkeypatch):
    server, app = _make_managed_server(tmp_path, monkeypatch)
    app.managed_lockdown_policy = ManagedLockdownPolicy(True, "enabled", True, "enabled")
    app.config.save_audio = False
    with TestClient(server.fastapi) as client:
        response = client.put("/api/config", json={"theme": "dark", "history_disabled": False})
    assert response.status_code == 200, response.text
    assert app.config.theme == "dark"
    assert app.config.history_enabled is False
    assert app.config.save_audio is False
    assert server._history_disabled() is True


def test_history_policy_blocks_import_storage_with_stale_config(tmp_path, monkeypatch):
    import numpy as np
    from whisperstream import main
    server, app = _make_managed_server(tmp_path, monkeypatch)
    app.managed_lockdown_policy = ManagedLockdownPolicy(True, "enabled", True, "enabled")
    app.config.history_enabled = True  # simulate stale state, independent of config reconciliation
    app.config.save_audio = True
    app.config.enhancement_enabled = False
    app.stt_engine.transcribe.return_value = "synthetic imported phrase"
    app.text_injector.process.side_effect = lambda text: text
    monkeypatch.setattr(main, "_load_audio_file", lambda *_: (np.ones(160, dtype=np.float32), 16000))
    events = []
    server.broadcast = lambda event, **_: events.append(event)
    server._imports._run_selections(["synthetic.wav"])
    app.stt_engine.transcribe.assert_called_once()
    app.transcript_store.save.assert_not_called()
    assert any(event.get("status") == "done" for event in events)
