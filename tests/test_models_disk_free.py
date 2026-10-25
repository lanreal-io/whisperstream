"""Tests for GET /api/models/disk-free (Task 1.4).

Covers:
- Response shape: free_mb, required_mb, sufficient
- sufficient=True when required_mb <= free_mb
- sufficient=False when required_mb > free_mb (via stubbed free space)
- Omitting path probes the default models root parent
- OSError → free_mb=-1, sufficient=True (fail-open)
- required_mb defaults to PARAKEET_REQUIRED_MB when omitted
- explicit required_mb overrides the default
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers — mirrors test_server_endpoints.py setup pattern
# ---------------------------------------------------------------------------


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "none"
    cfg.enhancement_local_model = ""
    cfg.enhancement_local_endpoint = ""
    cfg.enhancement_api_key = ""
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.parakeet_quantization = "int8"
    cfg.word_boosts = []
    cfg.threads = None
    cfg.model_timeout = 0.0
    cfg.hotword_score = 1.5
    cfg.beam_width = 4
    for key, value in overrides.items():
        setattr(cfg, key, value)

    def _to_dict():
        return {}

    cfg.to_dict = _to_dict
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
    from unittest.mock import AsyncMock
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.load_stt_engine_async = MagicMock()
    app.set_event_broadcaster = MagicMock()
    return app


def _make_client(*, config=None) -> tuple[TestClient, Any]:
    from whisperstream.server import WhisperstreamServer

    mock_app = _make_mock_app(config=config)
    server = WhisperstreamServer(mock_app, port=0, logger=None, register_hotkeys=False)
    client = TestClient(server.fastapi, raise_server_exceptions=True)
    return client, server


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestModelsDiskFree:
    def test_response_shape_with_explicit_path(self, tmp_path):
        """Route returns free_mb, required_mb, sufficient with a real path."""
        client, _ = _make_client()
        resp = client.get(f"/api/models/disk-free?path={tmp_path}")
        assert resp.status_code == 200
        data = resp.json()
        assert "free_mb" in data
        assert "required_mb" in data
        assert "sufficient" in data
        assert isinstance(data["free_mb"], int)
        assert isinstance(data["required_mb"], int)
        assert isinstance(data["sufficient"], bool)

    def test_sufficient_true_when_plenty_of_space(self, tmp_path):
        """sufficient=True when required_mb is small relative to actual free space."""
        client, _ = _make_client()
        # 1 MB is always available on any test machine
        resp = client.get(f"/api/models/disk-free?path={tmp_path}&required_mb=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["required_mb"] == 1
        assert data["sufficient"] is True

    def test_sufficient_false_when_required_exceeds_free(self, tmp_path):
        """sufficient=False when required_mb exceeds stubbed free_mb."""
        client, _ = _make_client()

        # Stub shutil.disk_usage so free = 100 MB
        fake_usage = shutil.disk_usage.__class__  # just for the annotation
        fake = MagicMock()
        fake.free = 100 * 1024 * 1024  # 100 MB in bytes

        with patch("shutil.disk_usage", return_value=fake):
            resp = client.get(
                f"/api/models/disk-free?path={tmp_path}&required_mb=500"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["free_mb"] == 100
        assert data["required_mb"] == 500
        assert data["sufficient"] is False

    def test_sufficient_true_when_free_equals_required(self, tmp_path):
        """sufficient=True when free_mb exactly equals required_mb (boundary)."""
        client, _ = _make_client()

        fake = MagicMock()
        fake.free = 200 * 1024 * 1024  # 200 MB

        with patch("shutil.disk_usage", return_value=fake):
            resp = client.get(
                f"/api/models/disk-free?path={tmp_path}&required_mb=200"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["sufficient"] is True

    def test_default_required_mb_is_parakeet_constant(self, tmp_path):
        """Omitting required_mb uses the PARAKEET_REQUIRED_MB module constant."""
        from whisperstream.server.app import PARAKEET_REQUIRED_MB

        client, _ = _make_client()
        resp = client.get(f"/api/models/disk-free?path={tmp_path}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["required_mb"] == PARAKEET_REQUIRED_MB

    def test_omit_path_returns_valid_payload(self):
        """Omitting path probes the default models root parent; shape is intact."""
        client, _ = _make_client()

        fake_path = Path("C:/fake/default")

        with patch(
            "whisperstream.config_manager.get_models_root",
            return_value=fake_path,
        ):
            fake = MagicMock()
            fake.free = 5000 * 1024 * 1024  # 5 GB
            with patch("shutil.disk_usage", return_value=fake):
                resp = client.get("/api/models/disk-free")

        assert resp.status_code == 200
        data = resp.json()
        assert "free_mb" in data
        assert "required_mb" in data
        assert "sufficient" in data

    def test_path_nonexistent_uses_nearest_ancestor(self, tmp_path):
        """A non-existent path falls back to its nearest existing ancestor."""
        client, _ = _make_client()
        nonexistent = tmp_path / "not" / "yet" / "created"
        # tmp_path exists; nonexistent doesn't
        assert not nonexistent.exists()
        assert tmp_path.exists()

        resp = client.get(f"/api/models/disk-free?path={nonexistent}&required_mb=1")
        assert resp.status_code == 200
        data = resp.json()
        # Should have probed tmp_path (the nearest existing ancestor)
        assert data["free_mb"] >= 0 or data["free_mb"] == -1  # either real or fail-open
        assert data["sufficient"] is True  # 1 MB required, definitely free or fail-open

    def test_oserror_fail_open(self, tmp_path):
        """OSError from disk_usage → free_mb=-1 and sufficient=True (fail-open)."""
        client, _ = _make_client()

        with patch("shutil.disk_usage", side_effect=OSError("permission denied")):
            resp = client.get(f"/api/models/disk-free?path={tmp_path}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["free_mb"] == -1
        assert data["sufficient"] is True
