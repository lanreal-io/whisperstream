"""Regression tests for the computed ``data_dir`` field on /api/config.

``data_dir`` is the resolved default app data directory. The frontend's Home
storage row uses it to display the default model-storage path. It is a computed
field (not stored in config.toml), so BOTH GET and PUT must add it. If PUT drops
it, every saveConfig() round-trip wipes data_dir from the frontend's config and
the storage row blanks to "Default location". This guards that regression.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.parakeet_quantization = "int8"
    # GPU identity fields are read by the PUT handler off the live config and
    # copied onto the incoming config; they must be JSON-serializable.
    cfg.enhancement_local_gpu_device = ""
    cfg.enhancement_local_gpu_device_index = -1
    cfg.enhancement_local_gpu_fingerprint = ""
    # lockdown_mode is server-authoritative in put_config (F1): the handler
    # preserves the live config's value onto the incoming config. Give the mock a
    # real bool so the preserved value validates (an auto-MagicMock is truthy and
    # would trip the lockdown provider constraint).
    cfg.lockdown_mode = False
    cfg.to_dict = lambda: {}
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _make_mock_app(*, config=None) -> MagicMock:
    app = MagicMock()
    app.config = config or _make_mock_config()
    app.stt_engine = MagicMock()
    app.llama_server = None
    app.config_manager = MagicMock()
    app.config_manager.save = MagicMock()
    app.reload_config = MagicMock()
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True, "trial_remaining_seconds": 0}
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.load_stt_engine_async = MagicMock()
    app.set_event_broadcaster = MagicMock()
    return app


def _make_client(*, config=None) -> tuple[TestClient, Any]:
    from whisperstream.server import WhisperstreamServer

    mock_app = _make_mock_app(config=config)
    server = WhisperstreamServer(mock_app, port=0, logger=None, register_hotkeys=False)
    return TestClient(server.fastapi, raise_server_exceptions=True), server


def test_get_config_includes_data_dir(monkeypatch):
    monkeypatch.setattr("whisperstream.server.app.platform.system", lambda: "Windows")
    client, _ = _make_client()
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "data_dir" in data
    assert isinstance(data["data_dir"], str) and data["data_dir"]


def test_put_config_includes_data_dir(monkeypatch):
    """The regression: PUT must echo data_dir so saveConfig() doesn't drop it."""
    monkeypatch.setattr("whisperstream.server.app.platform.system", lambda: "Windows")
    client, _ = _make_client()
    resp = client.put("/api/config", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "data_dir" in data, "PUT /api/config dropped the computed data_dir field"
    assert isinstance(data["data_dir"], str) and data["data_dir"]


def test_get_and_put_data_dir_agree(monkeypatch):
    monkeypatch.setattr("whisperstream.server.app.platform.system", lambda: "Windows")
    client, _ = _make_client()
    get_dir = client.get("/api/config").json()["data_dir"]
    put_dir = client.put("/api/config", json={}).json()["data_dir"]
    assert get_dir == put_dir


def test_macos_config_omits_private_storage_paths(monkeypatch):
    monkeypatch.setattr("whisperstream.server.app.platform.system", lambda: "Darwin")
    client, _ = _make_client()
    for response in (client.get("/api/config"), client.put("/api/config", json={})):
        body = response.json()
        assert "data_dir" not in body
        assert body["model_storage_dir"] == ""
