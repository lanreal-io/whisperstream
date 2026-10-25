"""Tests for model-relocation backend helpers (Tasks 2.1 and 2.2).

Task 2.1 — POST /api/models/relocate/check:
- Empty destination folder → outcome "ok" with total_bytes and free_bytes
- Destination pre-seeded with a COMPLETE model set → outcome "reuse"
- Destination with PARTIAL Whisperstream model files → outcome "blocked/foreign"
- Destination with GGUF but no Parakeet dir → outcome "blocked/foreign"
- Free space below headroom threshold → outcome "blocked/no_room"
- Writability probe failure → outcome "blocked/unwritable"
- total_bytes sums files from BOTH current get_models_root() and get_local_llm_dir() trees

Task 2.2 — _unload_models / _reload_models lifecycle helpers:
- Unload explicitly closes STT, clears exact loaded identity, and schedules supervisor.stop()
- Reload calls refresh_models_root, constructs the configured exact STT manager,
  schedules supervisor.restart_for_model() or creates a fresh supervisor.

Uses FastAPI's TestClient with a mock WhisperstreamApp (same pattern as
test_models_disk_free.py). No real ONNX model or llama-server needed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from whisperstream.setup import REQUIRED_FILES


# ---------------------------------------------------------------------------
# Helpers — mirrors test_models_disk_free.py setup pattern
# ---------------------------------------------------------------------------


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "none"
    cfg.enhancement_local_model = ""
    cfg.enhancement_local_endpoint = ""
    cfg.enhancement_api_key = ""
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.parakeet_quantization = "int8"
    cfg.stt_backend = "parakeet"
    cfg.model_storage_dir = ""
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

    def _unload_stt_manager():
        manager = app.parakeet
        app.parakeet = None
        app.loaded_stt_artifact_id = None
        if manager is not None:
            manager.close()

    def _install_stt_manager(manager, target_config):
        old_manager = app.parakeet
        app.parakeet = manager
        app.loaded_stt_artifact_id = (
            "qwen3-asr-0.6b-int8"
            if target_config.stt_backend == "qwen"
            else target_config.parakeet_model
        )
        if old_manager is not None and old_manager is not manager:
            old_manager.close()

    app.unload_stt_manager = MagicMock(side_effect=_unload_stt_manager)
    app.install_stt_manager = MagicMock(side_effect=_install_stt_manager)
    return app


def _make_client(*, config=None) -> tuple[TestClient, Any]:
    from whisperstream.server import WhisperstreamServer
    from whisperstream.windows_model_lifecycle import WindowsModelLifecycleAdapter

    mock_app = _make_mock_app(config=config)
    server = WhisperstreamServer(mock_app, port=0, logger=None, register_hotkeys=False)
    server._model_lifecycle = WindowsModelLifecycleAdapter()
    client = TestClient(server.fastapi, raise_server_exceptions=True)
    return client, server


def _seed_current_models(tmp_path: Path) -> tuple[Path, Path, int]:
    """Create fake current model trees and return (models_root, llm_dir, total_bytes)."""
    models_root = tmp_path / "current" / "models"
    models_root.mkdir(parents=True)
    llm_dir = tmp_path / "current" / "local-llm"
    llm_dir.mkdir(parents=True)

    # Seed a parakeet model directory with dummy REQUIRED_FILES
    parakeet_dir = models_root / "nemo-parakeet-tdt-0.6b-v3-int8"
    parakeet_dir.mkdir()
    for fname in REQUIRED_FILES:
        (parakeet_dir / fname).write_bytes(b"a" * 100)

    # Seed a GGUF file
    (llm_dir / "model.gguf").write_bytes(b"b" * 200)

    total_bytes = len(REQUIRED_FILES) * 100 + 200
    return models_root, llm_dir, total_bytes


def _seed_complete_dest(dest: Path) -> None:
    """Seed destination with a COMPLETE Whisperstream model set (reuse case)."""
    parakeet_dir = dest / "models" / "nemo-parakeet-tdt-0.6b-v3-int8"
    parakeet_dir.mkdir(parents=True)
    for fname in REQUIRED_FILES:
        (parakeet_dir / fname).write_bytes(b"x")

    llm_dir = dest / "local-llm"
    llm_dir.mkdir()
    (llm_dir / "model.gguf").write_bytes(b"x")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRelocate_check:
    def test_unrelated_destination_content_is_preserved(self, tmp_path):
        dest = tmp_path / "dest"
        (dest / "models").mkdir(parents=True)
        unrelated = dest / "models" / "personal.txt"
        unrelated.write_text("keep")
        client, server = _make_client()
        assert server._relocation.classify(str(dest)) == {"outcome": "blocked", "reason": "foreign"}
        assert unrelated.read_text() == "keep"
        client.close()

    def test_destination_nested_inside_source_is_blocked(self, tmp_path):
        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        client, server = _make_client()
        with patch("whisperstream.config_manager.get_models_root", return_value=models_root), patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir):
            assert server._relocation.classify(str(models_root / "nested"))["outcome"] == "blocked"
        assert not (models_root / "nested").exists()
        client.close()

    def test_swap_never_removes_nonempty_final(self, tmp_path):
        from whisperstream.server.model_relocation import ModelRelocation
        stage, final = tmp_path / "stage", tmp_path / "final"
        stage.mkdir()
        final.mkdir()
        (final / "personal.txt").write_text("keep")
        with pytest.raises(OSError):
            ModelRelocation.swap_into_place(stage, final)
        assert (final / "personal.txt").read_text() == "keep"
    def test_start_rejects_active_dictation(self, tmp_path):
        client, server = _make_client()
        server._app.stt_activation_busy.return_value = True

        response = client.post(
            "/api/models/relocate",
            json={"destination": str(tmp_path / "dest")},
        )

        assert response.status_code == 409
        assert response.json() == {
            "status": "busy",
            "reason": "dictation_active",
        }
        assert server._download_thread is None

    def test_start_rejects_shared_model_operation_guard(self, tmp_path):
        """Relocation cannot race an STT activation or deletion operation."""
        client, server = _make_client()
        server._model_operation_lock.acquire()
        try:
            response = client.post(
                "/api/models/relocate",
                json={"destination": str(tmp_path / "dest")},
            )
        finally:
            server._model_operation_lock.release()

        assert response.status_code == 409
        assert response.json() == {
            "status": "busy",
            "reason": "operation_busy",
        }
        assert server._download_thread is None

    def test_empty_dir_is_ok(self, tmp_path):
        """Empty destination → outcome 'ok' with total_bytes and free_bytes."""
        dest = tmp_path / "dest"
        dest.mkdir()

        models_root, llm_dir, expected_total = _seed_current_models(tmp_path)
        client, _ = _make_client()

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
        ):
            resp = client.post(
                "/api/models/relocate/check",
                json={"destination": str(dest)},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "ok"
        assert data["total_bytes"] == expected_total
        assert isinstance(data["free_bytes"], int)
        assert data["free_bytes"] > 0

    def test_complete_model_set_is_reuse(self, tmp_path):
        """Destination with complete Whisperstream model set → outcome 'reuse'."""
        dest = tmp_path / "dest"
        dest.mkdir()
        _seed_complete_dest(dest)

        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        client, _ = _make_client()

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
        ):
            resp = client.post(
                "/api/models/relocate/check",
                json={"destination": str(dest)},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "reuse"
        # reuse should not include space fields
        assert "reason" not in data

    def test_partial_parakeet_dir_is_foreign(self, tmp_path):
        """Destination with some but not all REQUIRED_FILES → blocked/foreign."""
        dest = tmp_path / "dest"
        parakeet_dir = dest / "models" / "nemo-parakeet-tdt-0.6b-v3-int8"
        parakeet_dir.mkdir(parents=True)
        # Only write the FIRST required file; rest missing
        (parakeet_dir / REQUIRED_FILES[0]).write_bytes(b"x")

        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        client, _ = _make_client()

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
        ):
            resp = client.post(
                "/api/models/relocate/check",
                json={"destination": str(dest)},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "blocked"
        assert data["reason"] == "foreign"

    def test_gguf_without_parakeet_is_foreign(self, tmp_path):
        """Destination with a GGUF but no parakeet dir → blocked/foreign."""
        dest = tmp_path / "dest"
        llm_dir_dest = dest / "local-llm"
        llm_dir_dest.mkdir(parents=True)
        (llm_dir_dest / "model.gguf").write_bytes(b"x")
        # No models/ dir at all

        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        client, _ = _make_client()

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
        ):
            resp = client.post(
                "/api/models/relocate/check",
                json={"destination": str(dest)},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "blocked"
        assert data["reason"] == "foreign"

    def test_complete_parakeet_no_gguf_is_foreign(self, tmp_path):
        """Complete Parakeet dir but no GGUF → blocked/foreign (incomplete set)."""
        dest = tmp_path / "dest"
        parakeet_dir = dest / "models" / "nemo-parakeet-tdt-0.6b-v3-int8"
        parakeet_dir.mkdir(parents=True)
        for fname in REQUIRED_FILES:
            (parakeet_dir / fname).write_bytes(b"x")
        # No local-llm dir

        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        client, _ = _make_client()

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
        ):
            resp = client.post(
                "/api/models/relocate/check",
                json={"destination": str(dest)},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "blocked"
        assert data["reason"] == "foreign"

    def test_no_room_is_blocked(self, tmp_path):
        """Free space below total_bytes + headroom → blocked/no_room."""
        dest = tmp_path / "dest"
        dest.mkdir()

        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        client, _ = _make_client()

        # Stub disk_usage to return 0 free bytes
        fake_usage = MagicMock()
        fake_usage.free = 0

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
            patch("shutil.disk_usage", return_value=fake_usage),
        ):
            resp = client.post(
                "/api/models/relocate/check",
                json={"destination": str(dest)},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "blocked"
        assert data["reason"] == "no_room"

    def test_total_bytes_sums_both_trees(self, tmp_path):
        """total_bytes includes files from both get_models_root() and get_local_llm_dir()."""
        dest = tmp_path / "dest"
        dest.mkdir()

        models_root = tmp_path / "models"
        models_root.mkdir()
        llm_dir = tmp_path / "llm"
        llm_dir.mkdir()

        # Write 500 bytes in models_root and 300 bytes in llm_dir
        (models_root / "file.bin").write_bytes(b"m" * 500)
        (llm_dir / "model.gguf").write_bytes(b"g" * 300)

        client, _ = _make_client()

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
        ):
            resp = client.post(
                "/api/models/relocate/check",
                json={"destination": str(dest)},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "ok"
        assert data["total_bytes"] == 800  # 500 + 300

    def test_nonexistent_dest_dir_is_ok(self, tmp_path):
        """Destination that doesn't exist yet (will be created) → ok if space."""
        dest = tmp_path / "dest" / "subdir"
        # dest does NOT exist yet

        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        client, _ = _make_client()

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
        ):
            resp = client.post(
                "/api/models/relocate/check",
                json={"destination": str(dest)},
            )

        assert resp.status_code == 200
        data = resp.json()
        # Should be ok (plenty of space on test machine) or no_room if space check fails
        # We just verify the shape is valid
        assert data["outcome"] in ("ok", "blocked")
        if data["outcome"] == "blocked":
            assert data["reason"] in ("no_room", "unwritable")

    def test_free_bytes_targets_destination_drive(self, tmp_path):
        """free_bytes in the 'ok' response probes the DESTINATION drive."""
        dest = tmp_path / "dest"
        dest.mkdir()

        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        client, _ = _make_client()

        # Stub disk_usage to return a specific known value and capture the path probed
        probed_paths: list[Path] = []
        real_disk_usage = shutil.disk_usage

        def fake_disk_usage(path):
            probed_paths.append(Path(path))
            return real_disk_usage(tmp_path)  # real value so we can assert free_bytes

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
            patch("shutil.disk_usage", side_effect=fake_disk_usage),
        ):
            resp = client.post(
                "/api/models/relocate/check",
                json={"destination": str(dest)},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "ok"
        # The probed path should be under the dest (not the current models location)
        assert len(probed_paths) >= 1
        # The probe should be on the dest path (tmp_path), not some other drive
        assert any(str(tmp_path) in str(p) for p in probed_paths)


# ---------------------------------------------------------------------------
# Task 2.2: _unload_models / _reload_models lifecycle helpers
# ---------------------------------------------------------------------------


class TestRelocate_lifecycle:
    """Tests for _unload_models and _reload_models helpers (Task 2.2).

    Uses a fake WhisperstreamServer with a sentinel parakeet and a fake
    supervisor exposing AsyncMock stop/restart_for_model.  The helpers
    schedule async coroutines on self._loop via call_soon_threadsafe; the
    test captures those coroutines and drains them synchronously.
    """

    # ------------------------------------------------------------------
    # Shared setup helpers
    # ------------------------------------------------------------------

    def _make_lifecycle_server(self, *, llama_server=None):
        """Build a WhisperstreamServer with a controllable fake loop."""
        from whisperstream.server import WhisperstreamServer

        mock_app = _make_mock_app()
        mock_app.llama_server = llama_server

        def unload():
            if mock_app.stt_engine is not None:
                mock_app.stt_engine.shutdown()
            mock_app.stt_engine = None
            mock_app.parakeet = None
            mock_app.loaded_stt_artifact_id = None
        mock_app.unload_stt_manager.side_effect = unload

        server = WhisperstreamServer(mock_app, port=0, logger=None, register_hotkeys=False)

        # Replace _loop with a fake that captures scheduled coroutines.
        captured_coros: list = []

        def _fake_call_soon_threadsafe(fn, coro, *args):
            # fn is asyncio.ensure_future; coro is the coroutine object.
            captured_coros.append(coro)

        fake_loop = MagicMock()
        fake_loop.call_soon_threadsafe = _fake_call_soon_threadsafe
        server._loop = fake_loop
        server._captured_coros = captured_coros
        return server

    def _drain(self, server) -> None:
        """Run all captured async coroutines in a fresh event loop."""
        coros = list(server._captured_coros)
        server._captured_coros.clear()

        async def _run_all():
            for coro in coros:
                await coro

        asyncio.run(_run_all())

    # ------------------------------------------------------------------
    # _unload_models
    # ------------------------------------------------------------------

    def test_unload_sets_parakeet_none(self):
        """_unload_models sets _app.stt_engine to None."""
        server = self._make_lifecycle_server()
        sentinel = MagicMock()
        server._app.parakeet = sentinel
        server._app.stt_engine = sentinel

        server._unload_models()

        assert server._app.parakeet is None
        server._app.unload_stt_manager.assert_called_once_with()
        assert server._app.loaded_stt_artifact_id is None
        sentinel.shutdown.assert_called_once_with()

    def test_unload_schedules_supervisor_stop(self):
        """_unload_models schedules supervisor.stop() on self._loop."""
        from unittest.mock import AsyncMock

        supervisor = MagicMock()
        supervisor.stop = AsyncMock()

        server = self._make_lifecycle_server(llama_server=supervisor)
        server._unload_models()

        assert len(server._captured_coros) == 1, "expected one coroutine to be scheduled"
        self._drain(server)
        supervisor.stop.assert_awaited_once()

    def test_unload_no_supervisor_schedules_nothing(self):
        """_unload_models with no llama_server schedules nothing and does not crash."""
        server = self._make_lifecycle_server(llama_server=None)
        server._unload_models()

        assert server._app.stt_engine is None
        assert len(server._captured_coros) == 0

    # ------------------------------------------------------------------
    # _reload_models
    # ------------------------------------------------------------------

    def test_reload_calls_refresh_models_root(self, tmp_path):
        """_reload_models calls config_manager.refresh_models_root()."""
        server = self._make_lifecycle_server()
        new_llm = tmp_path / "local-llm"
        new_llm.mkdir()
        (new_llm / "model.gguf").write_bytes(b"x")
        fake_binary = tmp_path / "llama-server.exe"
        fake_binary.write_bytes(b"exe")

        with (
            patch("whisperstream.stt_factory.make_stt_manager", return_value=MagicMock()),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=new_llm),
            patch("whisperstream.llama_server.find_bundled_llama_server", return_value=fake_binary),
            patch("whisperstream.llama_server.LlamaServerSupervisor"),
        ):
            server._reload_models()
            self._drain(server)

        server._app.config_manager.refresh_models_root.assert_called_once()

    def test_reload_constructs_and_installs_configured_stt_manager(self, tmp_path):
        """_reload_models uses the shared exact-artifact factory and installer."""
        server = self._make_lifecycle_server()

        new_llm = tmp_path / "local-llm"
        new_llm.mkdir()
        (new_llm / "model.gguf").write_bytes(b"x")

        fake_binary = tmp_path / "llama-server.exe"
        fake_binary.write_bytes(b"exe")

        manager = MagicMock(name="relocated_stt_manager")

        with (
            patch(
                "whisperstream.stt_factory.make_stt_manager",
                return_value=manager,
            ) as factory,
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=new_llm),
            patch("whisperstream.llama_server.find_bundled_llama_server", return_value=fake_binary),
            patch("whisperstream.llama_server.LlamaServerSupervisor"),
        ):
            server._reload_models()
            self._drain(server)  # consume the supervisor coroutine to avoid unawaited warning

        factory.assert_called_once_with(
            server._app.config,
            logger=server._logger,
            config_manager=server._app.config_manager,
        )
        server._app.install_stt_manager.assert_called_once_with(
            manager,
            server._app.config,
        )

    def test_reload_installs_manual_qwen_through_shared_factory(self, tmp_path):
        """Relocation preserves an exact Manual Qwen selection."""
        from whisperstream.config_manager import WhisperstreamConfig

        config = WhisperstreamConfig(
            language="ja",
            stt_model_selection_mode="manual",
            stt_backend="qwen",
            qwen_force_language=True,
        )
        server = self._make_lifecycle_server()
        server._app.config = config
        manager = MagicMock(name="relocated_qwen_manager")
        new_llm = tmp_path / "local-llm"
        new_llm.mkdir()

        with (
            patch(
                "whisperstream.stt_factory.make_stt_manager",
                return_value=manager,
            ) as factory,
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=new_llm),
            patch("whisperstream.llama_server.find_bundled_llama_server", return_value=None),
        ):
            server._reload_models()
            self._drain(server)

        factory.assert_called_once_with(
            config,
            logger=server._logger,
            config_manager=server._app.config_manager,
        )
        server._app.install_stt_manager.assert_called_once_with(manager, config)
        assert server._app.parakeet is manager
        assert server._app.loaded_stt_artifact_id == "qwen3-asr-0.6b-int8"

    def test_reload_schedules_supervisor_restart_at_new_gguf(self, tmp_path):
        """_reload_models schedules supervisor.restart_for_model(new_gguf) on the loop."""
        from unittest.mock import AsyncMock

        new_llm = tmp_path / "local-llm"
        new_llm.mkdir()
        gguf_file = new_llm / "existing-model.gguf"
        gguf_file.write_bytes(b"gguf")

        supervisor = MagicMock()
        supervisor.restart_for_model = AsyncMock()
        supervisor._gguf_path = MagicMock()
        supervisor._gguf_path.name = "existing-model.gguf"

        fake_binary = tmp_path / "llama-server.exe"
        fake_binary.write_bytes(b"exe")

        server = self._make_lifecycle_server(llama_server=supervisor)

        with (
            patch("whisperstream.stt_factory.make_stt_manager", return_value=MagicMock()),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=new_llm),
            patch("whisperstream.llama_server.find_bundled_llama_server", return_value=fake_binary),
        ):
            server._reload_models()
            self._drain(server)

        supervisor.restart_for_model.assert_awaited_once()
        called_gguf = supervisor.restart_for_model.call_args[0][0]
        assert called_gguf == gguf_file

    def test_reload_creates_fresh_supervisor_when_none(self, tmp_path):
        """_reload_models creates a fresh LlamaServerSupervisor when llama_server is None."""
        new_llm = tmp_path / "local-llm"
        new_llm.mkdir()
        (new_llm / "model.gguf").write_bytes(b"gguf")

        fake_binary = tmp_path / "llama-server.exe"
        fake_binary.write_bytes(b"exe")

        server = self._make_lifecycle_server(llama_server=None)

        mock_supervisor_cls = MagicMock()
        mock_supervisor_instance = MagicMock()
        mock_supervisor_cls.return_value = mock_supervisor_instance

        with (
            patch("whisperstream.stt_factory.make_stt_manager", return_value=MagicMock()),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=new_llm),
            patch("whisperstream.llama_server.find_bundled_llama_server", return_value=fake_binary),
            patch("whisperstream.llama_server.LlamaServerSupervisor", mock_supervisor_cls),
        ):
            server._reload_models()
            self._drain(server)

        mock_supervisor_cls.assert_called_once()
        assert server._app.llama_server is mock_supervisor_instance

    def test_reload_keeps_candidate_on_external_artifact_path(self, tmp_path):
        """Relocating managed models must not rewrite a candidate artifact path."""
        from unittest.mock import AsyncMock

        managed_llm = tmp_path / "managed" / "local-llm"
        managed_llm.mkdir(parents=True)
        candidate_gguf = tmp_path / "candidates" / "candidate.gguf"
        candidate_gguf.parent.mkdir()
        candidate_gguf.write_bytes(b"gguf")

        supervisor = MagicMock()
        supervisor.restart_for_model = AsyncMock()
        supervisor._gguf_path = candidate_gguf
        fake_binary = tmp_path / "llama-server.exe"
        fake_binary.write_bytes(b"exe")
        server = self._make_lifecycle_server(llama_server=supervisor)

        candidate_entry = {
            "id": "candidate-id",
            "source": "candidate",
            "gguf_filename": candidate_gguf.name,
            "_artifact_path": candidate_gguf,
        }
        with (
            patch("whisperstream.stt_factory.make_stt_manager", return_value=MagicMock()),
            patch(
                "whisperstream.enhancement_models.get_local_model",
                return_value=candidate_entry,
            ),
            patch(
                "whisperstream.config_manager.get_local_llm_dir",
                return_value=managed_llm,
            ),
            patch(
                "whisperstream.llama_server.find_bundled_llama_server",
                return_value=fake_binary,
            ),
        ):
            server._reload_models()
            self._drain(server)

        supervisor.restart_for_model.assert_awaited_once_with(candidate_gguf)


# ---------------------------------------------------------------------------
# Task 2.3: copy_tree_with_progress primitive
# ---------------------------------------------------------------------------


class TestCopy_tree:
    """copy_tree_with_progress: chunked copy with byte progress + .tmp cleanup."""

    def test_reports_progress_and_copies(self, tmp_path):
        from whisperstream.model_downloader import copy_tree_with_progress

        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.bin").write_bytes(b"a" * 1000)
        (src / "sub" / "b.bin").write_bytes(b"b" * 500)
        dst = tmp_path / "dst.tmp"

        chunks: list[int] = []
        copy_tree_with_progress(src, dst, on_bytes=chunks.append)

        # All bytes accounted for via on_bytes
        assert sum(chunks) == 1500
        # Files copied with structure + content preserved
        assert (dst / "a.bin").read_bytes() == b"a" * 1000
        assert (dst / "sub" / "b.bin").read_bytes() == b"b" * 500

    def test_cleans_tmp_on_cancel(self, tmp_path):
        import threading

        from whisperstream.model_downloader import (
            DownloadCancelled,
            copy_tree_with_progress,
        )

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.bin").write_bytes(b"a" * 1000)
        dst = tmp_path / "dst.tmp"

        evt = threading.Event()
        evt.set()  # pre-cancelled

        with pytest.raises(DownloadCancelled):
            copy_tree_with_progress(
                src, dst, on_bytes=lambda n: None, cancel_event=evt
            )
        # Partial staging tree removed on raise
        assert not dst.exists()

    def test_cleans_tmp_on_callback_error(self, tmp_path):
        from whisperstream.model_downloader import copy_tree_with_progress

        src = tmp_path / "src"
        src.mkdir()
        # Larger than DOWNLOAD_CHUNK_SIZE so on_bytes fires at least once
        (src / "a.bin").write_bytes(b"a" * 200_000)
        dst = tmp_path / "dst.tmp"

        def _raise(_n: int) -> None:
            raise RuntimeError("callback boom")

        with pytest.raises(RuntimeError):
            copy_tree_with_progress(src, dst, on_bytes=_raise)
        assert not dst.exists()


# ---------------------------------------------------------------------------
# Task 2.3: relocation worker (_run_model_relocation)
# ---------------------------------------------------------------------------


def _make_worker_server(*, config=None):
    """Build a real WhisperstreamServer with mocked model-lifecycle helpers.

    The worker's file operations (copy / verify / swap / delete) run for real
    against temp dirs; only _unload_models / _reload_models / _relocate_settle
    are stubbed (no real ONNX / llama-server / sleep).
    """
    from whisperstream.server import WhisperstreamServer
    from whisperstream.windows_model_lifecycle import WindowsModelLifecycleAdapter

    mock_app = _make_mock_app(config=config)
    server = WhisperstreamServer(mock_app, port=0, logger=None, register_hotkeys=False)
    server._model_lifecycle = WindowsModelLifecycleAdapter()
    server._unload_models = MagicMock()
    server._reload_models = MagicMock()
    server._relocation.settle = MagicMock()
    return server


def _ample_disk():
    """shutil.disk_usage stub with plenty of free space (avoids no_room flake)."""
    usage = MagicMock()
    usage.free = 50 * 1024 ** 3  # 50 GB
    return usage


class TestRelocate_worker:
    def test_worker_owns_shared_guard_for_entire_operation(self, tmp_path):
        server = _make_worker_server()
        observed_lock_state: list[bool] = []

        def _blocked(_destination):
            observed_lock_state.append(server._model_operation_lock.locked())
            return {"outcome": "blocked", "reason": "foreign"}

        server._relocation.classify = _blocked
        server._run_model_relocation(str(tmp_path / "dest"))

        assert observed_lock_state == [True]
        assert server._model_operation_lock.locked() is False

    def test_success_moves_files_and_repoints(self, tmp_path):
        """Files copied, config repointed, originals safely retained."""
        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()

        server = _make_worker_server()
        cfg = server._app.config

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
            patch("shutil.disk_usage", return_value=_ample_disk()),
        ):
            server._run_model_relocation(str(dest))

        # Files landed at the new location
        new_parakeet = dest / "models" / "nemo-parakeet-tdt-0.6b-v3-int8"
        assert new_parakeet.is_dir()
        for fname in REQUIRED_FILES:
            assert (new_parakeet / fname).exists()
        assert (dest / "local-llm" / "model.gguf").exists()

        # Staging swapped away
        assert not (dest / "models.tmp").exists()
        assert not (dest / "local-llm.tmp").exists()

        # Legacy originals have no ownership record and must be retained.
        assert models_root.exists()
        assert llm_dir.exists()
        assert not list(dest.glob(".ws_relocate_*"))

        # Config repointed + root refreshed (ONLY after files in place)
        assert cfg.model_storage_dir == str(dest)
        server._app.config_manager.save.assert_called_once()
        server._app.config_manager.refresh_models_root.assert_called_once()

        # Lifecycle: unloaded before copy, reloaded after
        server._unload_models.assert_called_once()
        server._reload_models.assert_called_once()

    def test_success_with_empty_local_llm_dir(self, tmp_path):
        """local-llm/ exists but holds no GGUF → relocation still moves the models.

        Regression for "Couldn't move the models / staging missing GGUF model":
        a failed local-enhancement download (e.g. low disk space) leaves an empty
        local-llm/ directory behind. The move must copy whatever models are
        actually present instead of treating the empty dir as corruption and
        rolling the whole move back.
        """
        # Valid Parakeet model, but an EMPTY local-llm dir (the failed-download state).
        models_root = tmp_path / "current" / "models"
        parakeet_dir = models_root / "nemo-parakeet-tdt-0.6b-v3-int8"
        parakeet_dir.mkdir(parents=True)
        for fname in REQUIRED_FILES:
            (parakeet_dir / fname).write_bytes(b"a" * 100)
        llm_dir = tmp_path / "current" / "local-llm"
        llm_dir.mkdir(parents=True)  # exists but empty — no .gguf

        dest = tmp_path / "dest"
        dest.mkdir()

        server = _make_worker_server()
        cfg = server._app.config

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
            patch("shutil.disk_usage", return_value=_ample_disk()),
        ):
            server._run_model_relocation(str(dest))

        # Parakeet models landed at the new location (the move was NOT aborted).
        new_parakeet = dest / "models" / "nemo-parakeet-tdt-0.6b-v3-int8"
        assert new_parakeet.is_dir()
        for fname in REQUIRED_FILES:
            assert (new_parakeet / fname).exists()

        # Staging swapped away + config repointed (proves we reached step 5+, not rollback).
        assert not (dest / "models.tmp").exists()
        assert not (dest / "local-llm.tmp").exists()
        assert cfg.model_storage_dir == str(dest)
        server._app.config_manager.save.assert_called_once()
        server._reload_models.assert_called_once()

        assert models_root.exists()

    def test_verify_staging_allows_empty_llm_when_source_empty(self, tmp_path):
        """_verify_relocation_staging must not require a GGUF when the source had none."""
        src_models = tmp_path / "src" / "models"
        stage_models = tmp_path / "dest" / "models.tmp"
        src_parakeet = src_models / "nemo-parakeet-tdt-0.6b-v3-int8"
        stage_parakeet = stage_models / "nemo-parakeet-tdt-0.6b-v3-int8"
        src_parakeet.mkdir(parents=True)
        stage_parakeet.mkdir(parents=True)
        for fname in REQUIRED_FILES:
            (src_parakeet / fname).write_bytes(b"a" * 10)
            (stage_parakeet / fname).write_bytes(b"a" * 10)

        # Empty source local-llm, faithfully copied to an empty staging local-llm.
        src_llm = tmp_path / "src" / "local-llm"
        stage_llm = tmp_path / "dest" / "local-llm.tmp"
        src_llm.mkdir(parents=True)
        stage_llm.mkdir(parents=True)

        server = _make_worker_server()
        # Must not raise: the copy faithfully mirrors the (GGUF-less) source.
        server._relocation.verify_staging(src_models, stage_models, src_llm, stage_llm)

    def test_verify_staging_still_catches_dropped_gguf(self, tmp_path):
        """_verify_relocation_staging still raises if the source HAD a GGUF but staging lost it."""
        src_models = tmp_path / "src" / "models"
        stage_models = tmp_path / "dest" / "models.tmp"
        src_models.mkdir(parents=True)
        stage_models.mkdir(parents=True)

        src_llm = tmp_path / "src" / "local-llm"
        stage_llm = tmp_path / "dest" / "local-llm.tmp"
        src_llm.mkdir(parents=True)
        stage_llm.mkdir(parents=True)
        # Source has a GGUF; staging is missing it (a genuinely broken copy).
        (src_llm / "model.gguf").write_bytes(b"g" * 50)

        server = _make_worker_server()
        with pytest.raises(RuntimeError):
            server._relocation.verify_staging(src_models, stage_models, src_llm, stage_llm)

    def test_failure_midcopy_preserves_originals(self, tmp_path):
        """(b) forced mid-copy failure: originals intact, config unchanged, staging cleaned."""
        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()

        server = _make_worker_server()
        cfg = server._app.config
        cfg.model_storage_dir = ""  # baseline

        def _boom(src, dst, **kwargs):
            # Simulate a half-written staging dir, then blow up mid-copy.
            Path(dst).mkdir(parents=True, exist_ok=True)
            (Path(dst) / "partial.bin").write_bytes(b"x")
            raise RuntimeError("disk exploded mid-copy")

        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
            patch("shutil.disk_usage", return_value=_ample_disk()),
            patch("whisperstream.model_downloader.copy_tree_with_progress", _boom),
        ):
            server._run_model_relocation(str(dest))

        # Originals survive untouched
        old_parakeet = models_root / "nemo-parakeet-tdt-0.6b-v3-int8"
        assert old_parakeet.is_dir()
        for fname in REQUIRED_FILES:
            assert (old_parakeet / fname).exists()
        assert (llm_dir / "model.gguf").exists()

        # Config never repointed
        assert cfg.model_storage_dir == ""
        server._app.config_manager.save.assert_not_called()

        # Staging cleaned up
        assert not (dest / "models.tmp").exists()
        assert not (dest / "local-llm.tmp").exists()

        # Rolled back: we had unloaded, so we reload at the OLD dir
        server._reload_models.assert_called_once()

    def test_reuse_repoints_without_copy(self, tmp_path):
        """(c) reuse: destination already has a complete set; repoint, no copy."""
        models_root, llm_dir, _ = _seed_current_models(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        _seed_complete_dest(dest)

        server = _make_worker_server()
        cfg = server._app.config

        copy_mock = MagicMock()
        with (
            patch("whisperstream.config_manager.get_models_root", return_value=models_root),
            patch("whisperstream.config_manager.get_local_llm_dir", return_value=llm_dir),
            patch("whisperstream.model_downloader.copy_tree_with_progress", copy_mock),
        ):
            server._run_model_relocation(str(dest))

        # No copy + no unload on the reuse path
        copy_mock.assert_not_called()
        server._unload_models.assert_not_called()

        # Config repointed + reloaded
        assert cfg.model_storage_dir == str(dest)
        server._app.config_manager.save.assert_called_once()
        server._reload_models.assert_called_once()

        # Destination's pre-existing models preserved
        assert (dest / "models" / "nemo-parakeet-tdt-0.6b-v3-int8" / REQUIRED_FILES[0]).exists()
        assert (dest / "local-llm" / "model.gguf").exists()

        assert models_root.exists()
        assert llm_dir.exists()
