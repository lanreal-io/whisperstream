"""Tests for the per-backend STT readiness helper + GET /api/stt/status.

Issue 2 of qwen3-asr iteration 01 adds:
  * ``stt_factory.stt_model_status`` — reports ``{downloaded, active}`` for
    both Parakeet and Qwen so the confirm-then-download flow can skip the
    dialog when the resolved model is already on disk.
  * ``GET /api/stt/status`` — serves that shape to the frontend.

Both backends' presence is checked against their real on-disk file lists
(Parakeet: ``setup.REQUIRED_FILES``; Qwen: ``qwen_models.QWEN_MODELS`` files),
so the tests stage / omit those files in tmp dirs.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest


@pytest.fixture(autouse=True)
def _preserve_windows_status_contract(monkeypatch):
    """Keep the pre-existing status assertions pinned to Windows semantics."""

    import whisperstream.stt_factory as stt_factory

    monkeypatch.setattr(stt_factory.sys, "platform", "win32")


# ---------------------------------------------------------------------------
# Fixture helpers (mirror test_server_endpoints.py)
# ---------------------------------------------------------------------------


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.stt_backend = "parakeet"
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.parakeet_quantization = "int8"
    cfg.word_boosts = []
    cfg.threads = None
    cfg.model_timeout = 0.0
    cfg.hotword_score = 1.5
    cfg.beam_width = 4
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _make_mock_app(*, config=None, parakeet_dir: Path | None = None) -> MagicMock:
    app = MagicMock()
    app.config = config or _make_mock_config()
    app.stt_engine = MagicMock()
    app.config_manager = MagicMock()
    app.config_manager.model_dir.return_value = parakeet_dir or Path("/fake/parakeet-dir")
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True, "trial_remaining_seconds": 0}
    from unittest.mock import AsyncMock

    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.load_stt_engine_async = MagicMock()
    app.set_event_broadcaster = MagicMock()
    return app


def _make_client(*, config=None, parakeet_dir: Path | None = None) -> tuple[TestClient, Any]:
    from whisperstream.server import WhisperstreamServer
    from whisperstream.windows_model_lifecycle import WindowsModelLifecycleAdapter

    mock_app = _make_mock_app(config=config, parakeet_dir=parakeet_dir)
    server = WhisperstreamServer(mock_app, port=0, logger=None, register_hotkeys=False)
    server._model_lifecycle = WindowsModelLifecycleAdapter()
    client = TestClient(server.fastapi, raise_server_exceptions=True)
    return client, server


def _stage_parakeet(model_dir: Path) -> None:
    """Create the Parakeet REQUIRED_FILES so _model_ready() reports True."""
    from whisperstream.setup import REQUIRED_FILES

    model_dir.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_FILES:
        (model_dir / name).write_bytes(b"stub")


def _stage_qwen(qwen_dir: Path) -> None:
    """Create every Qwen catalog file so qwen_model_present() reports True."""
    from whisperstream.qwen_models import QWEN_MODELS

    for rel in QWEN_MODELS[0]["files"]:
        dest = qwen_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"stub")


def _stage_cohere(cohere_dir: Path) -> None:
    from whisperstream.model_downloader import COHERE_TRANSCRIBE_FILES

    for rel in COHERE_TRANSCRIBE_FILES:
        dest = cohere_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"stub")


def _patch_model_dir(
    target: str,
    *,
    parakeet_dir: Path,
    qwen_dir: Path,
    cohere_dir: Path | None = None,
):
    """Patch the unified ``model_dir(ModelId)`` resolver for *target* module.

    The destination resolver replaced the old per-family helpers
    (``config_manager.model_dir`` for Parakeet, ``get_qwen_model_dir`` for Qwen).
    Both presence checks and the delete endpoint now route through it, so tests
    drive the on-disk layout by mapping each ModelId to a staged tmp dir.

    Presence now flows through ``model_downloader.is_present`` (the single
    zero-byte-aware predicate), which resolves dirs via
    ``model_downloader.model_dir`` — so that module is ALWAYS patched. The named
    *target* (e.g. ``whisperstream.server.app`` for the delete endpoint, which calls
    its own imported ``model_dir`` directly) is also patched where it imports
    the name, so both the presence path and the direct-call path resolve to the
    staged tmp dirs.
    """
    from whisperstream.model_downloader import ModelId

    cohere_dir = cohere_dir or (parakeet_dir.parent / "cohere-transcribe")

    def _side_effect(model_id):
        if model_id is ModelId.QWEN3_ASR_06B_INT8:
            return qwen_dir
        if model_id is ModelId.COHERE_TRANSCRIBE_INT8:
            return cohere_dir
        return parakeet_dir

    targets = {"whisperstream.model_downloader", target}
    stack = contextlib.ExitStack()
    for mod in targets:
        stack.enter_context(patch(f"{mod}.model_dir", side_effect=_side_effect))
    return stack


# ---------------------------------------------------------------------------
# stt_model_status (unit)
# ---------------------------------------------------------------------------


class TestSttModelStatusHelper:
    def test_darwin_reports_exact_native_and_registry_presence(self, tmp_path, monkeypatch):
        from whisperstream.model_downloader import ModelId
        import whisperstream.stt_factory as factory
        monkeypatch.setattr(factory.sys, "platform", "darwin")
        monkeypatch.setattr("whisperstream.macos_stt_manager.macos_stt_model_present", lambda: True)
        monkeypatch.setattr(factory, "is_present", lambda model: model == ModelId.QWEN3_ASR_06B_INT8)
        config = _make_mock_config(stt_backend="qwen")
        status = factory.stt_model_status(config, MagicMock())
        assert status == {
            "parakeet": {"downloaded": True, "active": False},
            "qwen": {"downloaded": True, "active": True},
            "cohere": {"downloaded": False, "active": False},
        }

    def test_neither_present_parakeet_active(self, tmp_path):
        from whisperstream.stt_factory import stt_model_status

        config = _make_mock_config(stt_backend="parakeet")
        cm = MagicMock()

        with _patch_model_dir(
            "whisperstream.stt_factory",
            parakeet_dir=tmp_path / "parakeet",  # not staged
            qwen_dir=tmp_path / "qwen3-asr",  # not staged
        ):
            status = stt_model_status(config, cm)

        assert status == {
            "parakeet": {"downloaded": False, "active": True},
            "qwen": {"downloaded": False, "active": False},
            "cohere": {"downloaded": False, "active": False},
        }

    def test_parakeet_present_and_active(self, tmp_path):
        from whisperstream.stt_factory import stt_model_status

        parakeet_dir = tmp_path / "parakeet"
        _stage_parakeet(parakeet_dir)
        config = _make_mock_config(stt_backend="parakeet")
        cm = MagicMock()

        with _patch_model_dir(
            "whisperstream.stt_factory",
            parakeet_dir=parakeet_dir,
            qwen_dir=tmp_path / "qwen3-asr",
        ):
            status = stt_model_status(config, cm)

        assert status["parakeet"] == {"downloaded": True, "active": True}
        assert status["qwen"] == {"downloaded": False, "active": False}

    def test_qwen_present_and_active(self, tmp_path):
        from whisperstream.stt_factory import stt_model_status

        qwen_dir = tmp_path / "qwen3-asr"
        _stage_qwen(qwen_dir)
        config = _make_mock_config(stt_backend="qwen")
        cm = MagicMock()

        with _patch_model_dir(
            "whisperstream.stt_factory",
            parakeet_dir=tmp_path / "parakeet",  # not staged
            qwen_dir=qwen_dir,
        ):
            status = stt_model_status(config, cm)

        assert status["qwen"] == {"downloaded": True, "active": True}
        assert status["parakeet"] == {"downloaded": False, "active": False}

    def test_cohere_present_and_active(self, tmp_path):
        from whisperstream.stt_factory import stt_model_status

        cohere_dir = tmp_path / "cohere"
        _stage_cohere(cohere_dir)
        config = _make_mock_config(
            language="en",
            stt_model_selection_mode="manual",
            stt_backend="cohere",
            qwen_force_language=False,
        )
        with _patch_model_dir(
            "whisperstream.stt_factory",
            parakeet_dir=tmp_path / "parakeet",
            qwen_dir=tmp_path / "qwen",
            cohere_dir=cohere_dir,
        ):
            status = stt_model_status(config, MagicMock())

        assert status["cohere"] == {"downloaded": True, "active": True}
        assert status["parakeet"]["active"] is False
        assert status["qwen"]["active"] is False

    def test_unknown_backend_treated_as_parakeet_active(self, tmp_path):
        """An unknown stt_backend value resolves active=parakeet (default-fallback)."""
        from whisperstream.stt_factory import stt_model_status

        config = _make_mock_config(stt_backend="bogus")
        cm = MagicMock()

        with _patch_model_dir(
            "whisperstream.stt_factory",
            parakeet_dir=tmp_path / "parakeet",
            qwen_dir=tmp_path / "qwen3-asr",
        ):
            status = stt_model_status(config, cm)

        assert status["parakeet"]["active"] is True
        assert status["qwen"]["active"] is False

    def test_env_override_drives_active(self, tmp_path, monkeypatch):
        """WHISPERSTREAM_STT_BACKEND=qwen makes qwen the active backend."""
        from whisperstream.stt_factory import stt_model_status

        monkeypatch.setenv("WHISPERSTREAM_STT_BACKEND", "qwen")
        config = _make_mock_config(stt_backend="parakeet")  # config says parakeet
        cm = MagicMock()

        with _patch_model_dir(
            "whisperstream.stt_factory",
            parakeet_dir=tmp_path / "parakeet",
            qwen_dir=tmp_path / "qwen3-asr",
        ):
            status = stt_model_status(config, cm)

        assert status["qwen"]["active"] is True
        assert status["parakeet"]["active"] is False


# ---------------------------------------------------------------------------
# GET /api/stt/status (endpoint)
# ---------------------------------------------------------------------------


class TestGetSttStatusEndpoint:
    def test_endpoint_returns_both_backends(self, tmp_path):
        parakeet_dir = tmp_path / "parakeet"
        _stage_parakeet(parakeet_dir)
        client, _ = _make_client(
            config=_make_mock_config(stt_backend="parakeet"),
            parakeet_dir=parakeet_dir,
        )

        with _patch_model_dir(
            "whisperstream.stt_factory",
            parakeet_dir=parakeet_dir,
            qwen_dir=tmp_path / "qwen3-asr",  # not staged
        ):
            resp = client.get("/api/stt/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["parakeet"] == {"downloaded": True, "active": True}
        assert data["qwen"] == {"downloaded": False, "active": False}

    def test_endpoint_reports_qwen_present(self, tmp_path):
        qwen_dir = tmp_path / "qwen3-asr"
        _stage_qwen(qwen_dir)
        client, _ = _make_client(
            config=_make_mock_config(stt_backend="qwen"),
            parakeet_dir=tmp_path / "parakeet",  # not staged
        )

        with _patch_model_dir(
            "whisperstream.stt_factory",
            parakeet_dir=tmp_path / "parakeet",  # not staged
            qwen_dir=qwen_dir,
        ):
            resp = client.get("/api/stt/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["qwen"]["downloaded"] is True
        assert data["qwen"]["active"] is True
        assert data["parakeet"]["downloaded"] is False


# ---------------------------------------------------------------------------
# POST /api/stt/delete (Issue 5: manual model delete + protect active)
# ---------------------------------------------------------------------------


class TestDeleteSttModel:
    def test_delete_inactive_qwen_removes_dir(self, tmp_path):
        """Active=parakeet, delete qwen -> qwen dir gone, parakeet untouched."""
        parakeet_dir = tmp_path / "parakeet"
        qwen_dir = tmp_path / "qwen3-asr"
        _stage_parakeet(parakeet_dir)
        _stage_qwen(qwen_dir)
        assert qwen_dir.exists()

        client, _ = _make_client(
            config=_make_mock_config(stt_backend="parakeet"),
            parakeet_dir=parakeet_dir,
        )

        with _patch_model_dir(
            "whisperstream.server.app",
            parakeet_dir=parakeet_dir,
            qwen_dir=qwen_dir,
        ):
            resp = client.post("/api/stt/delete", json={"model": "qwen"})

        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted", "model": "qwen"}
        assert not qwen_dir.exists(), "Qwen dir should have been removed"
        assert parakeet_dir.exists(), "Active parakeet dir must be untouched"

    def test_delete_inactive_cohere_legacy_name_removes_exact_dir(self, tmp_path):
        parakeet_dir = tmp_path / "parakeet"
        qwen_dir = tmp_path / "qwen"
        cohere_dir = tmp_path / "cohere"
        _stage_parakeet(parakeet_dir)
        _stage_cohere(cohere_dir)
        client, _ = _make_client(
            config=_make_mock_config(stt_backend="parakeet"),
            parakeet_dir=parakeet_dir,
        )

        with _patch_model_dir(
            "whisperstream.server.app",
            parakeet_dir=parakeet_dir,
            qwen_dir=qwen_dir,
            cohere_dir=cohere_dir,
        ):
            resp = client.post("/api/stt/delete", json={"model": "cohere"})

        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted", "model": "cohere"}
        assert not cohere_dir.exists()
        assert parakeet_dir.exists()

    def test_delete_inactive_parakeet_removes_dir(self, tmp_path):
        """Active=qwen, delete parakeet -> parakeet dir gone, qwen untouched."""
        parakeet_dir = tmp_path / "parakeet"
        qwen_dir = tmp_path / "qwen3-asr"
        _stage_parakeet(parakeet_dir)
        _stage_qwen(qwen_dir)
        assert parakeet_dir.exists()

        client, _ = _make_client(
            config=_make_mock_config(stt_backend="qwen"),
            parakeet_dir=parakeet_dir,
        )

        with _patch_model_dir(
            "whisperstream.server.app",
            parakeet_dir=parakeet_dir,
            qwen_dir=qwen_dir,
        ):
            resp = client.post("/api/stt/delete", json={"model": "parakeet"})

        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted", "model": "parakeet"}
        assert not parakeet_dir.exists(), "Parakeet dir should have been removed"
        assert qwen_dir.exists(), "Active qwen dir must be untouched"

    def test_refuse_delete_active_parakeet(self, tmp_path):
        """Deleting the active backend is refused (409) and the dir survives."""
        parakeet_dir = tmp_path / "parakeet"
        _stage_parakeet(parakeet_dir)

        client, _ = _make_client(
            config=_make_mock_config(stt_backend="parakeet"),
            parakeet_dir=parakeet_dir,
        )

        with _patch_model_dir(
            "whisperstream.server.app",
            parakeet_dir=parakeet_dir,
            qwen_dir=tmp_path / "qwen3-asr",
        ):
            resp = client.post("/api/stt/delete", json={"model": "parakeet"})

        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"
        assert data["reason"] == "active"
        # Locked guard copy (copy-review.md -> Errors / guards).
        assert data["message"] == (
            "Can't delete the model you're currently using. "
            "Switch to another model first."
        )
        assert parakeet_dir.exists(), "Active model dir must NOT be deleted"

    def test_refuse_delete_active_qwen(self, tmp_path):
        """Active=qwen -> deleting qwen is refused (409), dir survives."""
        qwen_dir = tmp_path / "qwen3-asr"
        _stage_qwen(qwen_dir)

        client, _ = _make_client(
            config=_make_mock_config(stt_backend="qwen"),
            parakeet_dir=tmp_path / "parakeet",
        )

        with _patch_model_dir(
            "whisperstream.server.app",
            parakeet_dir=tmp_path / "parakeet",
            qwen_dir=qwen_dir,
        ):
            resp = client.post("/api/stt/delete", json={"model": "qwen"})

        assert resp.status_code == 409
        assert resp.json()["reason"] == "active"
        assert qwen_dir.exists()

    def test_delete_missing_dir_is_idempotent(self, tmp_path):
        """Deleting a non-active backend whose dir is absent still returns
        deleted (idempotent, like the local-LLM precedent)."""
        parakeet_dir = tmp_path / "parakeet"
        _stage_parakeet(parakeet_dir)
        qwen_dir = tmp_path / "qwen3-asr"  # never created

        client, _ = _make_client(
            config=_make_mock_config(stt_backend="parakeet"),
            parakeet_dir=parakeet_dir,
        )

        with _patch_model_dir(
            "whisperstream.server.app",
            parakeet_dir=parakeet_dir,
            qwen_dir=qwen_dir,
        ):
            resp = client.post("/api/stt/delete", json={"model": "qwen"})

        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    def test_unknown_model_returns_400(self):
        """A model value that isn't parakeet/qwen is rejected with 400."""
        client, _ = _make_client(
            config=_make_mock_config(stt_backend="parakeet"),
        )
        resp = client.post("/api/stt/delete", json={"model": "bogus"})
        assert resp.status_code == 400
        assert resp.json()["reason"] == "unknown_model"
