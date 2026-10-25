"""Real REST compatibility paths never change unrestricted entitlement."""
from unittest.mock import MagicMock
import pytest
from starlette.testclient import TestClient
from whisperstream.license_manager import LicenseManager
from whisperstream.managed_policy import ManagedLockdownPolicy
from tests.test_eula_server_boundary import _make_server, ACCEPTED, BLOCKED

@pytest.mark.parametrize("lockdown", [False, True])
def test_unrestricted_license_rest_and_historical_actions(tmp_path, monkeypatch, lockdown):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    server, app, _ = _make_server(ACCEPTED)
    app.license_manager = LicenseManager()
    app.config.lockdown_mode = lockdown
    app.managed_lockdown_policy = ManagedLockdownPolicy(enabled=lockdown, state="enabled" if lockdown else "absent")
    client = TestClient(server.fastapi)
    for response in [client.get("/api/license"), client.post("/api/license/activate",json={"key":"historical-key"}),client.post("/api/license/deactivate")]:
        assert response.status_code == 200
        assert response.json()["is_pro"] is True
        assert response.json()["trial_remaining_seconds"] == 0
    assert app.license_manager.can_start_recording()
    assert not hasattr(server,"_trial_policy_task")
    assert not (tmp_path/"license.json").exists()
    assert not (tmp_path/"trial-policy-refresh.json").exists()


def test_unrestricted_access_does_not_replace_acceptance_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR",str(tmp_path))
    server,app,_ = _make_server(BLOCKED)
    app.license_manager = LicenseManager()
    assert app.license_manager.can_start_recording()
    response = TestClient(server.fastapi).post("/api/transcribe", content=b"not-multipart")
    assert response.status_code == 403
    app.transcribe_audio.assert_not_called()
