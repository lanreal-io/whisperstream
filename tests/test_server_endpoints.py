"""Tests for local-LLM REST + WebSocket endpoints (Task 1.7).

Covers:
- GET /api/enhancement/models?provider=local
- POST /api/enhancement/local-download/start
- POST /api/enhancement/local-download/cancel

Uses FastAPI's ``TestClient`` with a mock ``WhisperstreamApp`` so no real
model, llama-server, or filesystem I/O is required.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from whisperstream.history_security import UnavailableLicenseStateAdapter
from whisperstream.history_security_contract import LicenseSnapshot, LicenseState
from tests._platform_test_support import _make_mock_config, _make_mock_app, _make_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("accepted_eula")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_client(*, config=None, llama_server=None) -> tuple[TestClient, Any]:
    """Return (TestClient, WhisperstreamServer) pair with a mock app."""
    server = _make_server(config=config, llama_server=llama_server)
    client = TestClient(server.fastapi, raise_server_exceptions=True)
    return client, server


def test_rest_cors_allows_exact_packaged_tauri_origin() -> None:
    client, _server = _make_client()
    try:
        response = client.get(
            "/api/config",
            headers={"Origin": "tauri://localhost"},
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"


def test_startup_api_reports_unsupported_without_mutating() -> None:
    client, _server = _make_client()
    with (
        patch("whisperstream.server.app.is_startup_supported", return_value=False),
        patch("whisperstream.server.app.is_startup_enabled", return_value=False),
        patch("whisperstream.server.app.set_startup_enabled") as set_enabled,
    ):
        try:
            status = client.get("/api/startup")
            mutation = client.put("/api/startup", json={"enabled": True})
        finally:
            client.close()

    assert status.json() == {"enabled": False, "supported": False}
    assert mutation.json() == {"enabled": False, "supported": False}
    set_enabled.assert_not_called()


def test_startup_api_preserves_supported_windows_fallback() -> None:
    client, _server = _make_client()
    with (
        patch("whisperstream.server.app.is_startup_supported", return_value=True),
        patch("whisperstream.server.app.is_startup_enabled", side_effect=[False, True]),
        patch("whisperstream.server.app.set_startup_enabled") as set_enabled,
    ):
        try:
            status = client.get("/api/startup")
            mutation = client.put("/api/startup", json={"enabled": True})
        finally:
            client.close()

    assert status.json() == {"enabled": False, "supported": True}
    assert mutation.json() == {"enabled": True, "supported": True}
    set_enabled.assert_called_once_with(True)


def test_license_routes_delegate_through_platform_adapter() -> None:
    client, server = _make_client()
    adapter = MagicMock()
    adapter.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }
    adapter.snapshot.return_value = LicenseSnapshot(
        state=LicenseState.PRO,
        cache_integrity_protected=True,
        cache_confidential=False,
        activation_available=True,
        offline_validation_available=True,
        deactivation_available=True,
    )
    adapter.activate = AsyncMock(return_value={"success": True})
    adapter.deactivate = AsyncMock(
        return_value={"success": True, "warning": "synthetic warning"}
    )
    server._license_state_adapter = adapter
    server.broadcast = MagicMock()

    try:
        status = client.get("/api/license").json()
        assert status["is_pro"] is True
        assert status["state"] == "pro"
        assert status["activation_available"] is True
        assert status["offline_validation_available"] is True
        assert status["deactivation_available"] is True
        activated = client.post(
            "/api/license/activate", json={"key": "WS-SYNTHETIC"}
        )
        deactivated = client.post("/api/license/deactivate")
    finally:
        client.close()

    assert activated.status_code == 200
    assert deactivated.json()["warning"] == "synthetic warning"
    adapter.activate.assert_awaited_once_with("WS-SYNTHETIC")
    adapter.deactivate.assert_awaited_once_with()
    assert adapter.status.call_count == 3


def test_license_route_reports_unavailable_capabilities_fail_closed() -> None:
    client, server = _make_client()
    server._license_state_adapter = UnavailableLicenseStateAdapter()

    try:
        status = client.get("/api/license")
    finally:
        client.close()

    assert status.status_code == 200
    assert status.json() == {
        "is_pro": False,
        "trial_remaining_seconds": 0.0,
        "state": "unavailable",
        "cache_integrity_protected": False,
        "cache_confidential": False,
        "activation_available": False,
        "offline_validation_available": False,
        "deactivation_available": False,
    }


def test_rest_cors_allows_packaged_tauri_json_post_preflight() -> None:
    client, _server = _make_client()
    try:
        response = client.options(
            "/api/logs/frontend",
            headers={
                "Origin": "tauri://localhost",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


@pytest.mark.parametrize(
    "origin",
    [
        "tauri://localhost.evil.example",
        "tauri://localhost:38420",
        "tauri://localhost/",
        "tauri://evil@localhost",
        "TAURI://localhost",
        "null",
    ],
)
def test_rest_cors_rejects_packaged_tauri_origin_variants(origin: str) -> None:
    client, _server = _make_client()
    try:
        response = client.get(
            "/api/config",
            headers={"Origin": origin},
        )
    finally:
        client.close()

    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_rest_cors_preserves_existing_localhost_development_origin() -> None:
    client, _server = _make_client()
    try:
        response = client.get(
            "/api/config",
            headers={"Origin": "http://localhost:1420"},
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:1420"

class TestLocalRecovery:
    @pytest.mark.parametrize("file_state", ["absent", "empty", "directory", "present"])
    def test_disk_presence_is_not_supervisor_readiness(self, tmp_path, file_state):
        from whisperstream.config_manager import WhisperstreamConfig
        from whisperstream.enhancement_models import get_local_bundled_model
        cfg = WhisperstreamConfig(enhancement_enabled=True, enhancement_provider="local")
        entry = get_local_bundled_model(cfg.enhancement_local_model)
        path = tmp_path / entry["gguf_filename"]
        if file_state == "empty":
            path.touch()
        elif file_state == "directory":
            path.mkdir()
        elif file_state == "present":
            path.write_bytes(b"test model")
        client, _ = _make_client(config=cfg, llama_server=None)
        with patch("whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path):
            result = client.get("/api/enhancement/local-recovery").json()
        assert (result["model"] is None) == (file_state == "present")
        if result["model"]:
            assert result["model"]["id"] == cfg.enhancement_local_model
            assert set(result["model"]) == {"id", "label", "disk_mb"}

    @pytest.mark.parametrize("overrides,expected", [
        ({"enhancement_enabled": False}, False),
        ({"enhancement_provider": "gemini"}, False),
        ({"enhancement_local_endpoint": "http://localhost:9999"}, False),
        ({"enhancement_local_model": "unknown"}, False),
        ({"lockdown_mode": True, "enhancement_provider": "gemini"}, True),
        ({"lockdown_mode": True, "enhancement_local_endpoint": "https://example.com"}, True),
    ])
    def test_effective_routing_and_overrides(self, tmp_path, overrides, expected):
        from whisperstream.config_manager import WhisperstreamConfig
        cfg = WhisperstreamConfig(**({"enhancement_enabled": True, "enhancement_provider": "local"} | overrides))
        client, _ = _make_client(config=cfg)
        with patch("whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path):
            result = client.get("/api/enhancement/local-recovery").json()
        assert bool(result["model"]) is expected

    def test_reports_busy_download(self, tmp_path):
        cfg = _make_mock_config(enhancement_enabled=True)
        client, server = _make_client(config=cfg)
        server._download_thread = MagicMock()
        server._download_thread.is_alive.return_value = True
        with patch("whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path):
            assert client.get("/api/enhancement/local-recovery").json()["busy"] is True

    def test_developer_candidate_is_not_a_managed_download(self):
        client, _ = _make_client(config=_make_mock_config(enhancement_enabled=True))
        with patch("whisperstream.enhancement_models.get_local_model", return_value={
            "downloadable": False, "artifact_available": False
        }):
            assert client.get("/api/enhancement/local-recovery").json()["model"] is None

    def test_start_passes_recovery_without_changing_selection(self, tmp_path):
        client, server = _make_client()
        with patch("whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path), patch.object(
            server, "_run_llm_model_download"
        ) as worker:
            response = client.post("/api/enhancement/local-download/start", json={
                "model_id": "gemma4-e2b-instruct", "recovery": True
            })
            assert response.status_code == 200
            server._download_thread.join(timeout=2)
            worker.assert_called_once_with("gemma4-e2b-instruct", recovery=True)

    def test_recovery_keeps_newer_selection_and_requests_normal_warmup(self, tmp_path):
        server = _make_server(config=_make_mock_config(enhancement_enabled=True))
        events = []
        server.broadcast = events.append
        loop = asyncio.new_event_loop()
        server._loop = loop
        server._ensure_selected_llama_server = AsyncMock()
        def fetch(*args):
            server._app.config.enhancement_local_model = "stream-1"
        try:
            with patch.object(server._gguf_download, "fetch", side_effect=fetch), patch(
                "whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path
            ):
                server._run_llm_model_download("gemma4-e2b-instruct", recovery=True)
                loop.run_until_complete(asyncio.sleep(0))
            assert server._app.config.enhancement_local_model == "stream-1"
            server._app.config_manager.save.assert_not_called()
            server._app.reload_config.assert_not_called()
            server._ensure_selected_llama_server.assert_awaited_once_with(
                prepare_when_inactive=True, warm_if_enabled=True
            )
            assert events[-1]["model_id"] == "gemma4-e2b-instruct"
        finally:
            loop.close()


class TestExactSttStatus:
    def test_reports_configured_loaded_and_exact_artifact_inventory(self) -> None:
        from whisperstream.model_downloader import ModelId

        client, server = _make_client()
        server._app.configured_stt_artifact_id = ModelId.PARAKEET_V2_INT8.value
        server._app.loaded_stt_artifact_id = ModelId.PARAKEET_V3_INT8.value
        exact = {
            "configured_artifact_id": ModelId.PARAKEET_V2_INT8.value,
            "loaded_artifact_id": ModelId.PARAKEET_V3_INT8.value,
            "artifacts": [
                {
                    "id": ModelId.PARAKEET_V3_INT8.value,
                    "backend": "parakeet",
                    "downloaded": True,
                    "active": True,
                    "size_bytes": 100,
                },
                {
                    "id": ModelId.PARAKEET_V2_INT8.value,
                    "backend": "parakeet",
                    "downloaded": False,
                    "active": False,
                    "size_bytes": 25,
                },
                {
                    "id": ModelId.QWEN3_ASR_06B_INT8.value,
                    "backend": "qwen",
                    "downloaded": True,
                    "active": False,
                    "size_bytes": 200,
                },
            ],
            "unused_size_bytes": 200,
        }

        with patch(
            "whisperstream.server.app.stt_artifact_inventory",
            return_value=exact,
        ) as inventory:
            response = client.get("/api/stt/status")

        assert response.status_code == 200
        payload = response.json()
        assert {
            key: payload[key]
            for key in (
                "configured_artifact_id",
                "loaded_artifact_id",
                "artifacts",
                "unused_size_bytes",
            )
        } == exact
        inventory.assert_called_once_with(
            configured_artifact_id=ModelId.PARAKEET_V2_INT8.value,
            loaded_artifact_id=ModelId.PARAKEET_V3_INT8.value,
        )


class TestSttEnsureAndActivate:
    def test_start_rejects_active_dictation(self) -> None:
        client, server = _make_client()
        server._app.dictation_state_snapshot.return_value = {
            "phase": "recording",
        }
        server._app.stt_activation_busy.return_value = True

        response = client.post("/api/download/start")

        assert response.status_code == 409
        assert response.json() == {
            "status": "busy",
            "reason": "dictation_active",
        }
        assert server._download_thread is None

    def test_start_rejects_an_in_progress_model_operation(self) -> None:
        client, server = _make_client()
        server._app.stt_activation_busy.return_value = False
        server._model_operation_lock.acquire()
        try:
            response = client.post("/api/download/start")
        finally:
            server._model_operation_lock.release()

        assert response.status_code == 409
        assert response.json() == {
            "status": "busy",
            "reason": "operation_busy",
        }
        assert server._download_thread is None

    def test_start_reserves_model_guard_before_accepting_worker(self) -> None:
        """An accepted start hands an already-held guard to its worker."""
        client, server = _make_client()
        server._app.stt_activation_busy.return_value = False
        server.broadcast = MagicMock()
        manager = MagicMock(name="prepared_manager")
        created_threads = []
        native_thread = threading.Thread

        class DeferredThread:
            def __init__(self, *, target, args, kwargs, daemon):
                self.target = target
                self.args = args
                self.kwargs = kwargs
                self.daemon = daemon
                self.alive = False
                created_threads.append(self)

            def start(self) -> None:
                assert server._model_operation_lock.locked() is True
                assert server._model_operation_lock.acquire(blocking=False) is False
                self.alive = True

            def is_alive(self) -> bool:
                return self.alive

            def run_deferred(self) -> None:
                try:
                    self.target(*self.args, **self.kwargs)
                finally:
                    self.alive = False

        with (
            patch("whisperstream.server.app.threading", SimpleNamespace(
                **{**vars(threading), "Thread": DeferredThread}
            )),
            patch("whisperstream.server.app.is_present", return_value=True),
            patch(
                "whisperstream.stt_factory.make_stt_manager",
                return_value=manager,
            ),
        ):
            # Do not replace stdlib Thread underneath unrelated live Timers.
            assert threading.Thread is native_thread
            response = client.post("/api/download/start")

            assert response.status_code == 200
            assert response.json() == {"status": "started"}
            assert server._model_operation_lock.locked() is True
            assert len(created_threads) == 1
            worker = created_threads[0]
            assert worker.kwargs == {"operation_lock_held": True}

            worker.run_deferred()

        assert server._model_operation_lock.locked() is False
        server._app.install_stt_manager.assert_called_once_with(
            manager,
            server._app.config,
        )
        assert not any(
            call.args[0].get("reason") == "operation_busy"
            for call in server.broadcast.call_args_list
        )

    def test_present_artifact_uses_same_worker_without_downloading(self) -> None:
        server = _make_server()
        manager = MagicMock(name="prepared_manager")
        rollback = MagicMock(name="rollback")
        server._app.stt_activation_rollback_state.return_value = rollback
        server.broadcast = MagicMock()

        with (
            patch("whisperstream.server.app.is_present", return_value=True),
            patch("whisperstream.setup.download_model") as download,
            patch(
                "whisperstream.stt_factory.make_stt_manager",
                return_value=manager,
            ),
        ):
            server._run_model_download(server._app.config)

        download.assert_not_called()
        server._app.install_stt_manager.assert_called_once_with(
            manager,
            server._app.config,
        )
        server._app.restore_stt_routing_state.assert_not_called()
        assert any(
            call.args[0]["type"] == "download.complete"
            for call in server.broadcast.call_args_list
        )

    def test_absent_cohere_uses_r2_wrapper_then_atomically_installs(self, tmp_path) -> None:
        config = _make_mock_config(
            language="en",
            stt_model_selection_mode="manual",
            stt_backend="cohere",
            qwen_force_language=False,
        )
        server = _make_server(config=config)
        manager = MagicMock(name="cohere_manager")
        server._app.stt_activation_rollback_state.return_value = MagicMock()
        server.broadcast = MagicMock()

        with (
            patch("whisperstream.stt_factory._resolve_backend", return_value=("cohere", "config")),
            patch("whisperstream.server.app.is_present", return_value=False),
            patch("whisperstream.server.app.model_dir", return_value=tmp_path / "cohere"),
            patch("whisperstream.cohere_download.download_cohere_model") as download,
            patch("whisperstream.stt_factory.make_stt_manager", return_value=manager),
        ):
            server._run_model_download(config)

        download.assert_called_once()
        assert download.call_args.args == (tmp_path / "cohere",)
        server._app.install_stt_manager.assert_called_once_with(manager, config)
        assert any(
            call.args[0]["type"] == "download.complete"
            for call in server.broadcast.call_args_list
        )

    def test_cancelled_cohere_prepare_restores_complete_routing(self, tmp_path) -> None:
        from whisperstream.setup import DownloadCancelled

        config = _make_mock_config(
            language="en",
            stt_model_selection_mode="manual",
            stt_backend="cohere",
            qwen_force_language=False,
        )
        server = _make_server(config=config)
        rollback = MagicMock(name="rollback")
        server._app.stt_activation_rollback_state.return_value = rollback
        server.broadcast = MagicMock()

        with (
            patch("whisperstream.stt_factory._resolve_backend", return_value=("cohere", "config")),
            patch("whisperstream.server.app.is_present", return_value=False),
            patch("whisperstream.server.app.model_dir", return_value=tmp_path / "cohere"),
            patch(
                "whisperstream.cohere_download.download_cohere_model",
                side_effect=DownloadCancelled,
            ),
            patch("whisperstream.stt_factory.make_stt_manager") as factory,
        ):
            server._run_model_download(config)

        factory.assert_not_called()
        server._app.install_stt_manager.assert_not_called()
        server._app.restore_stt_routing_state.assert_called_once_with(rollback, config)
        assert any(
            call.args[0]["type"] == "download.cancelled"
            for call in server.broadcast.call_args_list
        )

    def test_activation_failure_restores_routing_state(self) -> None:
        server = _make_server()
        rollback = MagicMock(name="rollback")
        server._app.stt_activation_rollback_state.return_value = rollback
        server.broadcast = MagicMock()

        with (
            patch("whisperstream.server.app.is_present", return_value=True),
            patch(
                "whisperstream.stt_factory.make_stt_manager",
                side_effect=RuntimeError("cannot load"),
            ),
        ):
            server._run_model_download(server._app.config)

        server._app.restore_stt_routing_state.assert_called_once_with(
            rollback,
            server._app.config,
        )
        assert any(
            call.args[0]["type"] == "download.error"
            for call in server.broadcast.call_args_list
        )

    def test_cancelled_prepare_restores_routing_state(self) -> None:
        from whisperstream.setup import DownloadCancelled

        server = _make_server()
        rollback = MagicMock(name="rollback")
        server._app.stt_activation_rollback_state.return_value = rollback
        server.broadcast = MagicMock()

        with (
            patch("whisperstream.server.app.is_present", return_value=False),
            patch(
                "whisperstream.setup.download_model",
                side_effect=DownloadCancelled,
            ),
        ):
            server._run_model_download(server._app.config)

        server._app.restore_stt_routing_state.assert_called_once_with(
            rollback,
            server._app.config,
        )
        server._app.install_stt_manager.assert_not_called()
        assert any(
            call.args[0]["type"] == "download.cancelled"
            for call in server.broadcast.call_args_list
        )

    def test_cancel_after_construction_closes_uninstalled_manager(self) -> None:
        server = _make_server()
        manager = MagicMock(name="uninstalled_manager")
        rollback = MagicMock(name="rollback")
        server._app.stt_activation_rollback_state.return_value = rollback
        server.broadcast = MagicMock()

        def construct_then_cancel(*_args, **_kwargs):
            server._download_cancel_event.set()
            return manager

        with (
            patch("whisperstream.server.app.is_present", return_value=True),
            patch(
                "whisperstream.stt_factory.make_stt_manager",
                side_effect=construct_then_cancel,
            ),
        ):
            server._run_model_download(server._app.config)

        manager.shutdown.assert_called_once_with()
        server._app.install_stt_manager.assert_not_called()
        server._app.restore_stt_routing_state.assert_called_once_with(
            rollback,
            server._app.config,
        )

    def test_direct_activation_worker_is_single_flight(self) -> None:
        server = _make_server()
        server.broadcast = MagicMock()
        server._stt_activation_lock.acquire()
        try:
            server._run_model_download(server._app.config)
        finally:
            server._stt_activation_lock.release()

        server._app.stt_activation_rollback_state.assert_not_called()
        server.broadcast.assert_called_once_with(
            {
                "type": "download.error",
                "reason": "operation_busy",
                "message": (
                    "Whisperstream is already updating a model. "
                    "Try again when it finishes."
                ),
            }
        )


class TestExactSttDeletion:
    @staticmethod
    def _status() -> dict[str, object]:
        from whisperstream.model_downloader import ModelId

        return {
            "configured_artifact_id": ModelId.PARAKEET_V2_INT8.value,
            "loaded_artifact_id": ModelId.PARAKEET_V3_INT8.value,
            "artifacts": [],
            "unused_size_bytes": 0,
        }

    def test_targeted_delete_uses_exact_id_off_event_loop_and_refreshes_status(
        self,
    ) -> None:
        from whisperstream.model_downloader import ModelId

        client, server = _make_client()
        server._app.configured_stt_artifact_id = ModelId.PARAKEET_V2_INT8.value
        server._app.loaded_stt_artifact_id = ModelId.PARAKEET_V3_INT8.value
        server._app.stt_activation_busy.return_value = False

        async def run_sync(func, *args):
            return func(*args)

        with (
            patch("whisperstream.server.app.delete_model") as delete,
            patch(
                "whisperstream.server.app.asyncio.to_thread",
                new=AsyncMock(side_effect=run_sync),
            ) as to_thread,
            patch(
                "whisperstream.server.app.stt_artifact_inventory",
                return_value=self._status(),
            ),
        ):
            response = client.post(
                "/api/stt/delete",
                json={"model_id": ModelId.QWEN3_ASR_06B_INT8.value},
            )

        assert response.status_code == 200
        assert response.json() == {
            "status": "deleted",
            "model_id": ModelId.QWEN3_ASR_06B_INT8.value,
            "stt_status": self._status(),
        }
        to_thread.assert_awaited_once_with(delete, ModelId.QWEN3_ASR_06B_INT8)

    @pytest.mark.parametrize(
        "model_id",
        [
            "nemo-parakeet-tdt-0.6b-v2",
            "nemo-parakeet-tdt-0.6b-v3",
        ],
    )
    def test_targeted_delete_protects_configured_and_loaded_ids(
        self,
        model_id: str,
    ) -> None:
        from whisperstream.model_downloader import ModelId

        client, server = _make_client()
        server._app.configured_stt_artifact_id = ModelId.PARAKEET_V2_INT8.value
        server._app.loaded_stt_artifact_id = ModelId.PARAKEET_V3_INT8.value
        server._app.stt_activation_busy.return_value = False

        with patch("whisperstream.server.app.delete_model") as delete:
            response = client.post(
                "/api/stt/delete",
                json={"model_id": model_id},
            )

        assert response.status_code == 409
        assert response.json()["reason"] == "active"
        delete.assert_not_called()

    @pytest.mark.parametrize(
        "model_id",
        ["gemma4-e2b-instruct", "supertonic-3-int8-2026-05-11", "bogus"],
    )
    def test_targeted_delete_rejects_non_stt_or_unknown_ids(
        self,
        model_id: str,
    ) -> None:
        client, _server = _make_client()

        response = client.post("/api/stt/delete", json={"model_id": model_id})

        assert response.status_code == 400
        assert response.json()["reason"] == "unknown_model"

    def test_targeted_delete_rejects_dictation_and_operation_busy(self) -> None:
        from whisperstream.model_downloader import ModelId

        client, server = _make_client()
        server._app.stt_activation_busy.return_value = True
        target = ModelId.QWEN3_ASR_06B_INT8.value

        response = client.post("/api/stt/delete", json={"model_id": target})

        assert response.status_code == 409
        assert response.json()["reason"] == "dictation_active"

        server._app.stt_activation_busy.return_value = False
        server._model_operation_lock.acquire()
        try:
            response = client.post("/api/stt/delete", json={"model_id": target})
        finally:
            server._model_operation_lock.release()

        assert response.status_code == 409
        assert response.json()["reason"] == "operation_busy"

    def test_bulk_delete_protects_both_ids_and_refreshes_status(self) -> None:
        from whisperstream.model_downloader import ModelId

        client, server = _make_client()
        server._app.configured_stt_artifact_id = ModelId.PARAKEET_V2_INT8.value
        server._app.loaded_stt_artifact_id = ModelId.PARAKEET_V3_INT8.value
        server._app.stt_activation_busy.return_value = False
        status = self._status()

        with (
            patch(
                "whisperstream.server.app.delete_unused_stt_models",
                return_value=[ModelId.QWEN3_ASR_06B_INT8],
            ) as delete_unused,
            patch(
                "whisperstream.server.app.stt_artifact_inventory",
                return_value=status,
            ),
        ):
            response = client.post("/api/stt/delete-unused")

        assert response.status_code == 200
        assert response.json() == {
            "status": "deleted",
            "removed_model_ids": [ModelId.QWEN3_ASR_06B_INT8.value],
            "stt_status": status,
        }
        delete_unused.assert_called_once_with(
            {ModelId.PARAKEET_V2_INT8, ModelId.PARAKEET_V3_INT8}
        )


def test_claude_connect_returns_and_retains_structured_failure(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    import whisperstream.claude_hud as hud_module
    import whisperstream.agent_runtime_compatibility as runtime
    compatibility = {"surfaces": []}
    monkeypatch.setattr(runtime, "inspect_provider", lambda _: compatibility)

    lifecycle = {
        "ok": False,
        "status": "failed",
        "source_status": "current",
        "version": "1.1.0",
        "installed_version": "1.0.0",
        "reload_required": False,
        "reload_message": None,
        "error": "plugin_update_failed",
        "plugin_dir": r"C:\\Users\\private\\plugin",
        "marketplace_dir": r"C:\\Users\\private\\marketplace",
    }
    monkeypatch.setattr(
        hud_module,
        "enable_claude_hud",
        lambda *, reconcile_cli=False: lifecycle,
    )
    client, _server = _make_client()
    try:
        response = client.post("/api/claude-hud/enable")

        assert response.status_code == 200
        public_lifecycle = {
            key: value
            for key, value in lifecycle.items()
            if key not in {"error", "plugin_dir", "marketplace_dir"}
        }
        assert response.json() == public_lifecycle
        assert client.get("/api/claude-hud/status").json() == {
            **public_lifecycle, "compatibility": compatibility, "connection": "none",
        }
        assert "private" not in response.text
    finally:
        client.close()


def test_successful_runtime_plugin_enable_refreshes_discovery(monkeypatch) -> None:
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")
    import whisperstream.claude_hud as hud_module

    monkeypatch.setattr(
        hud_module,
        "enable_claude_hud",
        lambda *, reconcile_cli=False: {
            "ok": True,
            "status": "current",
            "reload_required": False,
        },
    )
    client, server = _make_client()
    server._loop = object()
    server._write_claude_discovery_file = MagicMock()
    try:
        response = client.post("/api/claude-hud/enable")
        assert response.json()["ok"] is True
        server._write_claude_discovery_file.assert_called_once_with()
    finally:
        server._loop = None
        client.close()


def test_discovery_file_contains_only_nondefault_port(tmp_path, monkeypatch) -> None:
    import whisperstream.server.app as server_module

    monkeypatch.setattr(server_module, "get_app_dir", lambda: tmp_path)
    server = _make_server()
    server._port = 49123

    server._write_claude_discovery_file()

    path = tmp_path / "claude-hud-port.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"port": 49123, "token": server._agent_hook_token}
    assert len(payload["token"]) == 64


def test_effective_lockdown_removes_discovery_and_blocks_plugin_reconcile(
    tmp_path, monkeypatch
) -> None:
    import whisperstream.claude_hud as hud_module
    import whisperstream.server.app as server_module

    monkeypatch.setattr(server_module, "get_app_dir", lambda: tmp_path)
    reconcile = MagicMock(
        return_value={"ok": True, "status": "current", "reload_required": False}
    )
    monkeypatch.setattr(hud_module, "enable_claude_hud", reconcile)
    config = _make_mock_config(lockdown_mode=True, claude_hud_enabled=True)
    server = _make_server(config=config)
    stale = tmp_path / "claude-hud-port.json"
    stale.write_text('{"port":38420}', encoding="utf-8")

    with TestClient(server.fastapi) as client:
        response = client.post("/api/claude-hud/enable")

    assert response.status_code == 423
    assert response.json() == {"ok": False, "error": "lockdown"}
    assert not stale.exists()
    reconcile.assert_not_called()


def test_routed_reply_hotkey_is_inert_in_effective_lockdown() -> None:
    server = _make_server(config=_make_mock_config(lockdown_mode=True))
    server._get_agent_compose_leases = MagicMock()

    assert server.start_routed_reply("push_to_talk") is False
    server._get_agent_compose_leases.assert_not_called()


def _enable_candidate_overlay(tmp_path, monkeypatch):
    # Candidate rows come from the development registry, which source releases
    # omit; without it these cases have nothing to exercise.
    pytest.importorskip("whisperstream.dev_candidate_models")
    registry = tmp_path / "candidate-models.json"
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    available_path = candidate_dir / "available.gguf"
    available_path.write_bytes(b"candidate")
    registry.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "candidates": [
                    {
                        "id": "candidate-available",
                        "label": "candidate-available",
                        "artifact_filename": "available.gguf",
                        "parameter_size": "350M",
                        "role": "Ship pick",
                        "status": "Incumbent",
                        "serving_contract": "raw-v1",
                        "trained_modes": ["chat", "code"],
                        "prompt_key": "stream",
                    },
                    {
                        "id": "candidate-missing",
                        "label": "candidate-missing",
                        "artifact_filename": "missing.gguf",
                        "parameter_size": "1.2B",
                        "role": "Review candidate",
                        "status": "Owner choice pending",
                        "serving_contract": "wrap-v2",
                        "trained_modes": ["chat", "code"],
                        "prompt_key": "stream",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "whisperstream._build_flags.DEV_TOOLS_AVAILABLE", True
    )
    monkeypatch.setenv(
        "WHISPERSTREAM_DEV_CANDIDATE_REGISTRY", str(registry)
    )
    monkeypatch.setenv("WHISPERSTREAM_DEV_CANDIDATE_DIR", str(candidate_dir))
    return available_path


# ---------------------------------------------------------------------------
# Production HUD dictation commands
# ---------------------------------------------------------------------------

_HUD_COMMAND_HEADERS = {"X-Whisperstream-Client": "hud-v1"}


@pytest.mark.parametrize(
    ("path", "method_name", "success_status"),
    [
        ("/api/dictation/complete", "complete_hud_dictation", "completed"),
        ("/api/dictation/cancel", "cancel_hud_dictation", "cancelled"),
    ],
)
def test_hud_dictation_commands_use_production_application_entry_points(
    path: str,
    method_name: str,
    success_status: str,
) -> None:
    client, server = _make_client()

    response = client.post(path, headers=_HUD_COMMAND_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": success_status}
    getattr(server._app, method_name).assert_called_once_with()


def test_hud_dictation_start_returns_disabled_after_authentication() -> None:
    client, server = _make_client()
    server._app.start_hud_dictation.return_value = "hud_control_disabled"

    response = client.post("/api/dictation/start", headers=_HUD_COMMAND_HEADERS)

    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "error": "hud_control_disabled",
    }
    server._app.start_hud_dictation.assert_called_once_with()


@pytest.mark.parametrize(
    ("path", "method_name", "result", "status_code", "error"),
    [
        (
            "/api/dictation/start",
            "start_hud_dictation",
            "recording_active",
            409,
            "recording_active",
        ),
        (
            "/api/dictation/start",
            "start_hud_dictation",
            "recording_not_started",
            409,
            "recording_not_started",
        ),
        (
            "/api/dictation/complete",
            "complete_hud_dictation",
            "not_hud_recording",
            409,
            "not_hud_recording",
        ),
        (
            "/api/dictation/cancel",
            "cancel_hud_dictation",
            "not_hud_recording",
            409,
            "not_hud_recording",
        ),
    ],
)
def test_hud_dictation_commands_return_explicit_rejections(
    path: str,
    method_name: str,
    result: str,
    status_code: int,
    error: str,
) -> None:
    client, server = _make_client()
    getattr(server._app, method_name).return_value = result

    response = client.post(path, headers=_HUD_COMMAND_HEADERS)

    assert response.status_code == status_code
    assert response.json() == {"ok": False, "error": error}
    getattr(server._app, method_name).assert_called_once_with()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {
            **_HUD_COMMAND_HEADERS,
            "Origin": "https://evil.example",
        },
        {
            **_HUD_COMMAND_HEADERS,
            "Origin": "null",
        },
        {
            "X-Whisperstream-Client": "wrong-client",
            "Origin": "http://localhost:1420",
        },
    ],
)
def test_hud_dictation_commands_reject_untrusted_browser_requests(
    headers: dict[str, str],
) -> None:
    client, server = _make_client()

    response = client.post("/api/dictation/start", headers=headers)

    assert response.status_code == 403
    if headers.get("Origin") in {"https://evil.example", "null"}:
        assert response.json() == {"detail": "request forbidden"}
    else:
        assert response.json() == {"ok": False, "error": "untrusted_hud_client"}
    server._app.start_hud_dictation.assert_not_called()


@pytest.mark.parametrize(
    "origin",
    [
        "tauri://localhost",
        "http://tauri.localhost",
        "http://localhost:1420",
        "http://127.0.0.1:1420",
    ],
)
def test_hud_dictation_commands_accept_desktop_and_dev_origins(
    origin: str,
) -> None:
    client, server = _make_client()

    response = client.post(
        "/api/dictation/start",
        headers={**_HUD_COMMAND_HEADERS, "Origin": origin},
    )

    assert response.status_code == 200
    server._app.start_hud_dictation.assert_called_once_with()


def test_screen_reader_status_endpoint_reports_current_runtime_state() -> None:
    client, _ = _make_client()

    with patch("whisperstream.main.screen_reader_active", return_value=True):
        response = client.get("/api/accessibility/screen-reader")

    assert response.status_code == 200
    assert response.json() == {"active": True, "monitor": "off"}


# ---------------------------------------------------------------------------
# WebSocket dictation lifecycle replay
# ---------------------------------------------------------------------------


def _receive_initial_ws_events(ws, count: int = 3) -> list[dict[str, Any]]:
    return [ws.receive_json() for _ in range(count)]


def test_websocket_replays_latest_dictation_state_on_connect() -> None:
    client, server = _make_client()
    state = {
        "type": "dictation.state",
        "version": 1,
        "recording_id": 7,
        "phase": "processing",
        "origin": "hud",
        "hud_enabled": True,
        "preview_enabled": True,
        "screen_reader_detected": False,
    }
    server.broadcast(state)

    with client.websocket_connect(
        "/ws", headers={"origin": "http://testserver"}
    ) as ws:
        events = _receive_initial_ws_events(ws)

    assert state in events


@pytest.mark.parametrize("origin", [None, "unknown", {"invalid": True}])
def test_active_dictation_state_requires_a_known_origin(origin: object) -> None:
    _, server = _make_client()

    server.broadcast(
        {
            "type": "dictation.state",
            "version": 1,
            "recording_id": 7,
            "phase": "recording",
            "origin": origin,
            "hud_enabled": True,
            "preview_enabled": False,
            "screen_reader_detected": False,
        }
    )

    assert server._dictation_state.snapshot() is None


def test_websocket_replay_rejects_stale_ids_and_strips_extra_content() -> None:
    client, server = _make_client()
    current = {
        "type": "dictation.state",
        "version": 1,
        "recording_id": 9,
        "phase": "recording",
        "origin": "toggle",
        "hud_enabled": True,
        "preview_enabled": False,
        "screen_reader_detected": False,
        "text": "PRIVATE SENTINEL MUST NOT BE RETAINED",
    }
    stale = {
        "type": "dictation.state",
        "version": 1,
        "recording_id": 8,
        "phase": "idle",
        "origin": "push_to_talk",
        "hud_enabled": False,
        "preview_enabled": False,
        "screen_reader_detected": False,
    }
    server.broadcast(current)
    server.broadcast(stale)

    with client.websocket_connect(
        "/ws", headers={"origin": "http://testserver"}
    ) as ws:
        events = _receive_initial_ws_events(ws)

    replay = next(event for event in events if event["type"] == "dictation.state")
    assert replay["recording_id"] == 9
    assert replay["phase"] == "recording"
    assert replay["origin"] == "toggle"
    assert "text" not in replay
    assert "PRIVATE SENTINEL" not in json.dumps(events)


def test_preview_is_not_retained_counted_or_stored_as_agent_completion() -> None:
    client, server = _make_client()
    server.stats = MagicMock()
    state = {
        "type": "dictation.state",
        "version": 1,
        "recording_id": 11,
        "phase": "recording",
        "origin": "hud",
        "hud_enabled": True,
        "preview_enabled": True,
        "screen_reader_detected": False,
    }
    preview_text = "PREVIEW_SERVER_SENTINEL_46a32"
    preview = {
        "type": "transcription.preview",
        "recording_id": 11,
        "revision": 3,
        "text": preview_text,
        "stable_char_count": 8,
    }

    server.broadcast(state)
    server.broadcast(preview)

    assert server._agent_activity is None
    server.stats.record_transcription.assert_not_called()
    assert server._dictation_state.snapshot() == state

    with client.websocket_connect(
        "/ws", headers={"origin": "http://testserver"}
    ) as ws:
        reconnect_events = _receive_initial_ws_events(ws)

    assert state in reconnect_events
    assert not any(
        event.get("type") == "transcription.preview" for event in reconnect_events
    )
    assert preview_text not in json.dumps(reconnect_events)
    assert server._agent_activity is None

    final = {
        "type": "transcription.result",
        "text": "authoritative final",
        "duration_ms": 1200,
        "enhance_ms": 0,
    }
    server.broadcast(final)
    server.stats.record_transcription.assert_called_once_with(
        "authoritative final", 1200
    )


# ---------------------------------------------------------------------------
# GET /api/enhancement/builtin-prompt - editable profile templates
# ---------------------------------------------------------------------------


class TestGetBuiltinPrompt:
    def test_chat_returns_simpler_editable_template(self):
        from whisperstream.llm_enhancer import CHAT_PROMPT, CHAT_TEMPLATE_PROMPT

        client, _ = _make_client()
        resp = client.get("/api/enhancement/builtin-prompt?mode=chat")

        assert resp.status_code == 200
        assert resp.json() == {"mode": "chat", "prompt": CHAT_TEMPLATE_PROMPT}
        assert CHAT_TEMPLATE_PROMPT != CHAT_PROMPT

    def test_mode_is_normalized_before_lookup(self):
        from whisperstream.llm_enhancer import CHAT_TEMPLATE_PROMPT

        client, _ = _make_client()
        resp = client.get("/api/enhancement/builtin-prompt?mode=%20Chat%20")

        assert resp.status_code == 200
        assert resp.json() == {"mode": "chat", "prompt": CHAT_TEMPLATE_PROMPT}


# ---------------------------------------------------------------------------
# GET /api/enhancement/models?provider=local — catalog shape
# ---------------------------------------------------------------------------


class TestGetEnhancementModelsLocal:
    def test_returns_public_bundled_entry_no_installed(self, tmp_path):
        """With an empty local-llm dir, installed_ids should be empty."""
        client, _ = _make_client()

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.get("/api/enhancement/models?provider=local")

        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "local"
        assert [m["id"] for m in data["bundled"]] == [
            "stream-1-mini",
            "gemma4-e2b-instruct",
        ]
        assert data["installed_ids"] == []
        assert data["available_ids"] == ["stream-1-mini", "gemma4-e2b-instruct"]
        assert "discovered" not in data
        assert "discovery_error" not in data
        assert data["active_id"] == "gemma4-e2b-instruct"
        assert data["running"] is False
        by_id = {row["id"]: row["capabilities"] for row in data["bundled"]}
        assert by_id["gemma4-e2b-instruct"] == {
            "family": "customizable", "editable_prompt": True,
            "supported_modes": None,
        }
        assert by_id["stream-1-mini"] == {
            "family": "trained-mode", "editable_prompt": False,
            "supported_modes": ["chat", "email"],
        }

    def test_merges_candidate_rows_with_explicit_source_and_artifact_state(
        self, tmp_path, monkeypatch
    ):
        available_path = _enable_candidate_overlay(tmp_path, monkeypatch)
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        cfg = _make_mock_config(
            enhancement_local_model="candidate-available"
        )
        client, _ = _make_client(config=cfg)

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=managed_dir,
        ):
            resp = client.get("/api/enhancement/models?provider=local")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["bundled"]) == 4

        production = next(
            row
            for row in data["bundled"]
            if row["id"] == "gemma4-e2b-instruct"
        )
        assert production["source"] == "production"
        assert production["experiment_id"] is None
        assert production["artifact_available"] is False
        assert production["selectable"] is True
        assert production["downloadable"] is True
        assert production["active"] is False

        available = next(
            row
            for row in data["bundled"]
            if row["id"] == "candidate-available"
        )
        assert available == {
            "id": "candidate-available",
            "label": "candidate-available",
            "disk_mb": 0,
            "ram_mb": 0,
            # The tiny test artifact still proves the estimate came from its
            # backend-only size (ceil(1 MiB * 1.25) + 512 MiB), not a path or
            # the old frontend-only 512 MiB fallback.
            "resident_ram_mb": 514,
            "resident_ram_estimated": True,
            "min_system_ram_mb": 0,
            "gguf_filename": "available.gguf",
            "prompt_key": "stream",
            "source": "candidate",
            "experiment_id": "candidate-available",
            "parameter_size": "350M",
            "role": "Ship pick",
            "status": "Incumbent",
            "serving_contract": "raw-v1",
            "trained_modes": ["chat", "code"],
            "capabilities": {
                "family": "trained-mode", "editable_prompt": False,
                "supported_modes": ["chat", "code"],
            },
            "downloadable": False,
            "artifact_available": True,
            "selectable": True,
            "active": True,
        }
        assert str(available_path) not in resp.text

        missing = next(
            row
            for row in data["bundled"]
            if row["id"] == "candidate-missing"
        )
        assert missing["parameter_size"] == "1.2B"
        assert missing["artifact_available"] is False
        assert missing["selectable"] is False
        assert missing["downloadable"] is False
        assert missing["active"] is False

        # Compatibility arrays retain their production-managed semantics, so
        # an older frontend cannot send candidates into download/benchmark.
        assert data["installed_ids"] == []
        assert data["available_ids"] == ["stream-1-mini", "gemma4-e2b-instruct"]
        assert data["active_id"] == "candidate-available"

    def test_installed_ids_populated_when_gguf_present(self, tmp_path):
        """When a GGUF file exists on disk, its model id appears in installed_ids."""
        from whisperstream.enhancement_models import LOCAL_BUNDLED_MODELS

        # Stage the only catalog entry's GGUF as a non-empty file.
        entry = LOCAL_BUNDLED_MODELS[0]
        gguf = tmp_path / entry["gguf_filename"]
        gguf.write_bytes(b"fake gguf content")

        client, _ = _make_client()

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.get("/api/enhancement/models?provider=local")

        assert resp.status_code == 200
        data = resp.json()
        assert entry["id"] in data["installed_ids"]
        assert len(data["installed_ids"]) == 1

    def test_running_true_when_llama_server_up(self, tmp_path):
        """running=True when app.llama_server is set and is_running=True."""
        mock_supervisor = MagicMock()
        mock_supervisor.is_running = True

        client, _ = _make_client(llama_server=mock_supervisor)

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.get("/api/enhancement/models?provider=local")

        assert resp.status_code == 200
        assert resp.json()["running"] is True

    def test_running_false_when_llama_server_none(self, tmp_path):
        """running=False when app.llama_server is None."""
        client, _ = _make_client(llama_server=None)

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.get("/api/enhancement/models?provider=local")

        assert resp.status_code == 200
        assert resp.json()["running"] is False

    def test_zero_byte_gguf_not_counted_as_installed(self, tmp_path):
        """A zero-byte GGUF (interrupted download) should NOT appear in installed_ids."""
        from whisperstream.enhancement_models import LOCAL_BUNDLED_MODELS

        entry = LOCAL_BUNDLED_MODELS[0]
        gguf = tmp_path / entry["gguf_filename"]
        gguf.write_bytes(b"")  # zero bytes

        client, _ = _make_client()

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.get("/api/enhancement/models?provider=local")

        assert resp.status_code == 200
        assert resp.json()["installed_ids"] == []

    def test_unknown_provider_returns_400(self):
        """Non-existent provider names should return 400."""
        client, _ = _make_client()
        resp = client.get("/api/enhancement/models?provider=unknown_xyz")
        assert resp.status_code == 400

    def test_cloud_provider_returns_only_curated_manifest_fields(self):
        cfg = _make_mock_config(enhancement_provider="gemini")
        client, _ = _make_client(config=cfg)
        catalog = {
            "gemini": [
                {"id": "gemini-lite", "label": "Gemini Lite", "recommended": True}
            ]
        }
        with patch(
            "whisperstream.cloud_model_manifest.resolve_cloud_model_catalog",
            new=AsyncMock(return_value=catalog),
        ) as resolve:
            resp = client.get("/api/enhancement/models?provider=gemini")
        assert resp.status_code == 200
        assert resp.json() == {
            "provider": "gemini",
            "bundled": [{
                **catalog["gemini"][0],
                "capabilities": {
                    "family": "customizable", "editable_prompt": True,
                    "supported_modes": None,
                },
            }],
        }
        assert "capabilities" not in catalog["gemini"][0]
        resolve.assert_awaited_once()

    def test_local_bundled_entries_have_required_keys(self, tmp_path):
        """Every entry in 'bundled' must carry the common catalog keys.

        `gguf_repo` and `recommended` are NOT universal: the two Stream-1
        fine-tunes are R2-only (no HuggingFace repo) and only Gemma is
        flagged `recommended`.
        """
        required_keys = {"id", "label", "disk_mb", "ram_mb",
                         "min_system_ram_mb", "gguf_filename", "prompt_key"}
        client, _ = _make_client()

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.get("/api/enhancement/models?provider=local")

        entries = resp.json()["bundled"]
        for entry in entries:
            missing = required_keys - set(entry.keys())
            assert not missing, f"Entry {entry['id']!r} missing keys: {missing}"

        gemma = next(e for e in entries if e["id"] == "gemma4-e2b-instruct")
        assert gemma["gguf_repo"] == "unsloth/gemma-4-E2B-it-GGUF"
        assert gemma["recommended"] is False
        mini = next(e for e in entries if e["id"] == "stream-1-mini")
        assert (mini["gguf_repo"], mini["recommended"]) == ("whisperstream/stream-1-mini", True)

        assert {e["id"] for e in entries} == {"stream-1-mini", "gemma4-e2b-instruct"}


# ---------------------------------------------------------------------------
# POST /api/enhancement/local-download/start
# ---------------------------------------------------------------------------


class TestLocalLLMDownloadStart:
    def test_unknown_model_id_returns_400(self):
        """An unrecognized model_id should be rejected with 400."""
        client, _ = _make_client()
        resp = client.post(
            "/api/enhancement/local-download/start",
            json={"model_id": "nonexistent-model-9999"},
        )
        assert resp.status_code == 400
        assert "unknown model_id" in resp.json().get("error", "")

    def test_candidate_is_not_routed_to_production_downloader(
        self, tmp_path, monkeypatch
    ):
        available_path = _enable_candidate_overlay(tmp_path, monkeypatch)
        client, server = _make_client()

        with patch.object(server, "_run_llm_model_download") as mock_run:
            resp = client.post(
                "/api/enhancement/local-download/start",
                json={"model_id": "candidate-available"},
            )

        assert resp.status_code == 400
        mock_run.assert_not_called()
        assert available_path.read_bytes() == b"candidate"

    def test_valid_model_id_returns_started(self):
        """A recognized model_id should return {"status": "started"}."""
        client, server = _make_client()

        with patch.object(server, "_run_llm_model_download") as mock_run:
            # Immediately mark thread as done so cleanup doesn't hang
            def _fake_run(model_id):
                pass

            mock_run.side_effect = _fake_run

            with patch("threading.Thread") as MockThread:
                mock_thread = MagicMock()
                mock_thread.is_alive.return_value = False
                MockThread.return_value = mock_thread

                resp = client.post(
                    "/api/enhancement/local-download/start",
                    json={"model_id": "gemma4-e2b-instruct"},
                )

        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

    def test_second_call_while_busy_returns_409(self):
        """A second concurrent POST while a thread is alive should return 409."""
        client, server = _make_client()

        # Simulate a live download thread
        alive_thread = MagicMock()
        alive_thread.is_alive.return_value = True
        server._download_thread = alive_thread

        resp = client.post(
            "/api/enhancement/local-download/start",
            json={"model_id": "gemma4-e2b-instruct"},
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "busy"

    def test_legacy_qwen17_model_id_now_rejected(self):
        """qwen3-1.7b-instruct was removed from the catalog (variance check picked
        gemma3:1b); endpoint returns 400."""
        client, _ = _make_client()
        resp = client.post(
            "/api/enhancement/local-download/start",
            json={"model_id": "qwen3-1.7b-instruct"},
        )
        assert resp.status_code == 400
        assert "unknown model_id" in resp.json().get("error", "")

    def test_legacy_small_model_id_now_rejected(self):
        """qwen3-0.6b-instruct was removed from the catalog; endpoint returns 400."""
        client, _ = _make_client()
        resp = client.post(
            "/api/enhancement/local-download/start",
            json={"model_id": "qwen3-0.6b-instruct"},
        )
        assert resp.status_code == 400
        assert "unknown model_id" in resp.json().get("error", "")


# ---------------------------------------------------------------------------
# POST /api/enhancement/local-download/cancel
# ---------------------------------------------------------------------------


class TestLocalLLMDownloadCancel:
    def test_cancel_when_not_running_returns_not_running(self):
        """Cancel with no active thread should return {"status": "not_running"}."""
        client, server = _make_client()
        # Ensure no active download
        server._download_cancel_event = None
        server._download_thread = None

        resp = client.post("/api/enhancement/local-download/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_running"

    def test_cancel_sets_event_and_joins_thread(self):
        """Cancel should set the cancel event and join the thread."""
        client, server = _make_client()

        cancel_event = threading.Event()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False

        server._download_cancel_event = cancel_event
        server._download_thread = mock_thread

        resp = client.post("/api/enhancement/local-download/cancel")

        assert resp.status_code == 200
        assert cancel_event.is_set(), "cancel_event should have been set"
        mock_thread.join.assert_called_once()
        data = resp.json()
        assert data["status"] in ("cancelled", "cancelling")

    def test_cancel_returns_cancelling_when_thread_still_alive(self):
        """If the thread doesn't exit within the join timeout, return 'cancelling'."""
        client, server = _make_client()

        cancel_event = threading.Event()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True  # still alive after join timeout

        server._download_cancel_event = cancel_event
        server._download_thread = mock_thread

        resp = client.post("/api/enhancement/local-download/cancel")

        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelling"


# ---------------------------------------------------------------------------
# POST /api/enhancement/local-activate
# ---------------------------------------------------------------------------


class TestLocalCandidateActivation:
    def test_available_candidate_saves_exact_id_and_repoints_supervisor(
        self, tmp_path, monkeypatch
    ):
        available_path = _enable_candidate_overlay(tmp_path, monkeypatch)
        cfg = _make_mock_config(
            enhancement_local_model="gemma4-e2b-instruct",
            enhancement_local_idle_timeout=600,
            enhancement_local_gpu_device_index=-1,
        )
        supervisor = MagicMock()
        supervisor.restart_for_model = AsyncMock()
        client, server = _make_client(config=cfg, llama_server=supervisor)

        with (
            patch.object(server, "_run_llm_model_download") as download,
            patch.object(server._benchmark, "run") as benchmark,
        ):
            resp = client.post(
                "/api/enhancement/local-activate",
                json={"model_id": "CANDIDATE-AVAILABLE"},
            )

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "activated",
            "model_id": "candidate-available",
        }
        supervisor.restart_for_model.assert_awaited_once_with(available_path)
        assert server._app.llama_server is supervisor
        assert cfg.enhancement_local_model == "candidate-available"
        server._app.config_manager.save.assert_called_once_with(cfg)
        server._app.reload_config.assert_called_once_with(cfg)
        download.assert_not_called()
        benchmark.assert_not_called()

    def test_available_candidate_creates_lazy_supervisor_without_download_or_benchmark(
        self, tmp_path, monkeypatch
    ):
        available_path = _enable_candidate_overlay(tmp_path, monkeypatch)
        cfg = _make_mock_config(
            enhancement_local_model="gemma4-e2b-instruct",
            enhancement_local_idle_timeout=600,
            enhancement_local_gpu_device_index=-1,
        )
        client, server = _make_client(config=cfg, llama_server=None)
        lazy_supervisor = MagicMock()

        with (
            patch(
                "whisperstream.llama_server.find_bundled_llama_server",
                return_value=tmp_path / "llama-server.exe",
            ),
            patch(
                "whisperstream.llama_server.LlamaServerSupervisor",
                return_value=lazy_supervisor,
            ) as supervisor_cls,
            patch.object(server, "_run_llm_model_download") as download,
            patch.object(server._benchmark, "run") as benchmark,
        ):
            resp = client.post(
                "/api/enhancement/local-activate",
                json={"model_id": "candidate-available"},
            )

        assert resp.status_code == 200
        assert server._app.llama_server is lazy_supervisor
        assert supervisor_cls.call_args.args[0] == available_path
        assert cfg.enhancement_local_model == "candidate-available"
        download.assert_not_called()
        benchmark.assert_not_called()

    def test_available_candidate_promotes_matching_resident_without_repoint(
        self, tmp_path, monkeypatch
    ):
        available_path = _enable_candidate_overlay(tmp_path, monkeypatch)
        cfg = _make_mock_config(
            enhancement_local_model="gemma4-e2b-instruct",
            enhancement_local_idle_timeout=600,
        )
        old_supervisor = MagicMock()
        old_supervisor.is_residency_pinned = True
        old_supervisor.restart_for_model = AsyncMock()
        old_supervisor.stop = AsyncMock()
        promoted_supervisor = MagicMock()
        resident = MagicMock(supervisor=promoted_supervisor, owned_by_pool=True)
        pool = MagicMock()
        pool.find_resident.return_value = resident
        client, server = _make_client(config=cfg, llama_server=old_supervisor)
        server._app.resident_llama_pool = pool

        response = client.post(
            "/api/enhancement/local-activate",
            json={"model_id": "candidate-available"},
        )

        assert response.status_code == 200
        assert server._app.llama_server is promoted_supervisor
        assert cfg.enhancement_local_model == "candidate-available"
        old_supervisor.restart_for_model.assert_not_awaited()
        old_supervisor.stop.assert_not_awaited()
        pool.find_resident.assert_called_once_with(available_path)

    def test_missing_candidate_returns_controlled_error_without_mutation(
        self, tmp_path, monkeypatch
    ):
        _enable_candidate_overlay(tmp_path, monkeypatch)
        cfg = _make_mock_config(enhancement_local_model="gemma4-e2b-instruct")
        supervisor = MagicMock()
        supervisor.restart_for_model = AsyncMock()
        client, server = _make_client(config=cfg, llama_server=supervisor)

        resp = client.post(
            "/api/enhancement/local-activate",
            json={"model_id": "candidate-missing"},
        )

        assert resp.status_code == 409
        assert resp.json() == {
            "error": "artifact_unavailable",
            "model_id": "candidate-missing",
        }
        assert cfg.enhancement_local_model == "gemma4-e2b-instruct"
        assert server._app.llama_server is supervisor
        supervisor.restart_for_model.assert_not_awaited()
        server._app.config_manager.save.assert_not_called()
        server._app.reload_config.assert_not_called()

    def test_production_model_is_rejected_by_candidate_activation_route(self):
        cfg = _make_mock_config(enhancement_local_model="stream-1")
        client, server = _make_client(config=cfg)

        resp = client.post(
            "/api/enhancement/local-activate",
            json={"model_id": "gemma4-e2b-instruct"},
        )

        assert resp.status_code == 400
        assert resp.json() == {
            "error": "candidate_model_required",
            "model_id": "gemma4-e2b-instruct",
        }
        assert cfg.enhancement_local_model == "stream-1"
        server._app.config_manager.save.assert_not_called()
        server._app.reload_config.assert_not_called()

    def test_config_rejection_restores_prior_model_and_supervisor(
        self, tmp_path, monkeypatch
    ):
        available_path = _enable_candidate_overlay(tmp_path, monkeypatch)
        managed_dir = tmp_path / "managed"
        cfg = _make_mock_config(enhancement_local_model="gemma4-e2b-instruct")
        cfg.validate.side_effect = ValueError("rejected")
        supervisor = MagicMock()
        supervisor.restart_for_model = AsyncMock()
        client, server = _make_client(config=cfg, llama_server=supervisor)

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=managed_dir,
        ):
            resp = client.post(
                "/api/enhancement/local-activate",
                json={"model_id": "candidate-available"},
            )

        assert resp.status_code == 500
        assert resp.json()["error"] == "config_save_failed"
        assert cfg.enhancement_local_model == "gemma4-e2b-instruct"
        assert supervisor.restart_for_model.await_args_list == [
            ((available_path,), {}),
            ((managed_dir / "gemma-4-E2B-it-Q4_K_M.gguf",), {}),
        ]
        server._app.config_manager.save.assert_not_called()
        server._app.reload_config.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/enhancement/local-download/delete
# ---------------------------------------------------------------------------


class TestLocalLLMDownloadDelete:
    def test_delete_when_no_file_present_returns_deleted(self, tmp_path):
        """Happy path: empty dir, POST returns 200 with status=deleted."""
        client, _ = _make_client()

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.post(
                "/api/enhancement/local-download/delete",
                json={"model_id": "gemma4-e2b-instruct"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deleted"
        assert data["model_id"] == "gemma4-e2b-instruct"

    def test_unknown_model_id_returns_400(self):
        """An unrecognized model_id should be rejected with 400."""
        client, _ = _make_client()
        resp = client.post(
            "/api/enhancement/local-download/delete",
            json={"model_id": "nonexistent-model-9999"},
        )
        assert resp.status_code == 400
        assert "unknown model_id" in resp.json().get("error", "")

    def test_candidate_artifact_cannot_be_deleted_by_managed_endpoint(
        self, tmp_path, monkeypatch
    ):
        available_path = _enable_candidate_overlay(tmp_path, monkeypatch)
        client, _ = _make_client()

        resp = client.post(
            "/api/enhancement/local-download/delete",
            json={"model_id": "candidate-available"},
        )

        assert resp.status_code == 400
        assert available_path.read_bytes() == b"candidate"

    def test_delete_unlinks_gguf_and_tmp(self, tmp_path):
        """Both the GGUF and its sibling .tmp file should be removed on delete."""
        from whisperstream.enhancement_models import LOCAL_BUNDLED_MODELS

        entry = LOCAL_BUNDLED_MODELS[0]
        gguf = tmp_path / entry["gguf_filename"]
        tmp_file = tmp_path / (entry["gguf_filename"] + ".tmp")
        gguf.write_bytes(b"fake")
        tmp_file.write_bytes(b"partial")
        assert gguf.exists()
        assert tmp_file.exists()

        client, _ = _make_client()

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.post(
                "/api/enhancement/local-download/delete",
                json={"model_id": entry["id"]},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        assert not gguf.exists(), "GGUF file should have been unlinked"
        assert not tmp_file.exists(), "Sibling .tmp file should have been unlinked"

    def test_delete_active_model_stops_llama_server(self, tmp_path):
        """When deleting the active local model, the supervisor is stopped first
        and ``app.llama_server`` is reset to ``None`` per the finally-block
        contract in ``_maybe_stop_llama_server``."""
        from unittest.mock import AsyncMock

        mock_supervisor = MagicMock()
        mock_supervisor.stop = AsyncMock()

        cfg = _make_mock_config(enhancement_local_model="gemma4-e2b-instruct")
        client, server = _make_client(config=cfg, llama_server=mock_supervisor)

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.post(
                "/api/enhancement/local-download/delete",
                json={"model_id": "gemma4-e2b-instruct"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        assert mock_supervisor.stop.await_count == 1
        assert server._app.llama_server is None

    def test_delete_inactive_model_does_not_stop_llama_server(self, tmp_path):
        """If the deleted model id doesn't match the currently active local
        model, the supervisor stays untouched (research.md step 3 gate)."""
        from unittest.mock import AsyncMock

        mock_supervisor = MagicMock()
        mock_supervisor.stop = AsyncMock()

        # Catalog has only one bundled id; use a non-matching string for the
        # active local model so the conditional teardown gate stays closed.
        cfg = _make_mock_config(enhancement_local_model="some-other-id")
        client, server = _make_client(config=cfg, llama_server=mock_supervisor)

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.post(
                "/api/enhancement/local-download/delete",
                json={"model_id": "gemma4-e2b-instruct"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        mock_supervisor.stop.assert_not_awaited()
        assert server._app.llama_server is mock_supervisor

    def test_delete_does_not_mutate_config(self, tmp_path):
        """Delete must NOT change ``enhancement_local_model`` or
        ``enhancement_provider`` (research.md "Do NOT mutate" rule), and must
        not call ``config_manager.save``."""
        cfg = _make_mock_config(
            enhancement_provider="local",
            enhancement_local_model="gemma4-e2b-instruct",
        )
        before_provider = cfg.enhancement_provider
        before_local_model = cfg.enhancement_local_model

        client, server = _make_client(config=cfg)

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            resp = client.post(
                "/api/enhancement/local-download/delete",
                json={"model_id": "gemma4-e2b-instruct"},
            )

        assert resp.status_code == 200
        assert cfg.enhancement_provider == before_provider
        assert cfg.enhancement_local_model == before_local_model
        server._app.config_manager.save.assert_not_called()

    def test_delete_resident_model_requires_restart_without_mutation(self, tmp_path):
        from whisperstream.enhancement_models import get_local_bundled_model

        entry = get_local_bundled_model("gemma4-e2b-instruct")
        assert entry is not None
        gguf_path = tmp_path / entry["gguf_filename"]
        gguf_path.write_bytes(b"resident")
        cfg = _make_mock_config(
            enhancement_provider="local",
            enhancement_local_model="gemma4-e2b-instruct",
        )
        supervisor = MagicMock()
        supervisor.stop = AsyncMock()
        client, server = _make_client(config=cfg, llama_server=supervisor)
        pool = MagicMock()
        pool.has_resident.side_effect = lambda path=None: path == gguf_path
        server._app.resident_llama_pool = pool

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            response = client.post(
                "/api/enhancement/local-download/delete",
                json={"model_id": "gemma4-e2b-instruct"},
            )

        assert response.status_code == 409
        assert response.json() == {
            "error": "resident_models_restart_required",
            "model_id": "gemma4-e2b-instruct",
        }
        assert gguf_path.read_bytes() == b"resident"
        supervisor.stop.assert_not_awaited()
        server._app.config_manager.save.assert_not_called()


# ---------------------------------------------------------------------------
# _run_llm_model_download — happy path, cancel, and config-save integration
# ---------------------------------------------------------------------------


class TestRunLLMModelDownload:
    """Unit-level tests for the _run_llm_model_download worker method.

    We call it directly (not via HTTP) so we can inspect broadcast calls
    and config_manager interactions without spinning up the server loop.
    """

    def _server_with_broadcasts(self, **config_overrides):
        """Return (server, broadcasts_list) where broadcasts accumulates events."""
        server = _make_server(**config_overrides)
        broadcasts: list[dict] = []
        server.broadcast = lambda event: broadcasts.append(event)  # type: ignore[method-assign]
        return server, broadcasts

    def test_happy_path_broadcasts_complete_and_saves_config(self, tmp_path):
        """Successful download broadcasts download.complete and saves config."""
        server, broadcasts = self._server_with_broadcasts()

        with (
            patch(
                "whisperstream.setup.download_llm_model",
            ) as mock_dl,
            patch(
                "whisperstream.config_manager.get_local_llm_dir",
                return_value=tmp_path,
            ),
            patch(
                "whisperstream.setup.cleanup_stale_llm_models",
            ) as mock_cleanup,
        ):
            # Simulate download creating the file (needed for cleanup arg)
            mock_dl.return_value = None
            server._loop = None  # no async loop; skip supervisor restart

            server._run_llm_model_download("gemma4-e2b-instruct")

        # Should have a progress-less complete event
        types = [b["type"] for b in broadcasts]
        assert "download.complete" in types
        complete_event = next(b for b in broadcasts if b["type"] == "download.complete")
        assert complete_event.get("kind") == "local_llm"

        # Config should be saved with the new model id
        server._app.config_manager.save.assert_called_once()
        server._app.reload_config.assert_called_once()

        # Cleanup must NOT be called (picker keeps all installed models)
        mock_cleanup.assert_not_called()

    def test_cancelled_broadcasts_cancelled_event(self, tmp_path):
        """DownloadCancelled → download.cancelled event with kind=local_llm."""
        from whisperstream.setup import DownloadCancelled

        server, broadcasts = self._server_with_broadcasts()

        with (
            patch(
                "whisperstream.setup.download_llm_model",
                side_effect=DownloadCancelled("cancelled"),
            ),
            patch(
                "whisperstream.config_manager.get_local_llm_dir",
                return_value=tmp_path,
            ),
        ):
            server._run_llm_model_download("gemma4-e2b-instruct")

        types = [b["type"] for b in broadcasts]
        assert "download.cancelled" in types
        cancelled_event = next(b for b in broadcasts if b["type"] == "download.cancelled")
        assert cancelled_event.get("kind") == "local_llm"

        # Config must NOT be saved on cancel
        server._app.config_manager.save.assert_not_called()

    def test_error_broadcasts_error_event(self, tmp_path):
        """Unexpected exception → download.error event with kind=local_llm."""
        server, broadcasts = self._server_with_broadcasts()

        with (
            patch(
                "whisperstream.setup.download_llm_model",
                side_effect=RuntimeError("network failure"),
            ),
            patch(
                "whisperstream.config_manager.get_local_llm_dir",
                return_value=tmp_path,
            ),
        ):
            server._run_llm_model_download("gemma4-e2b-instruct")

        types = [b["type"] for b in broadcasts]
        assert "download.error" in types
        error_event = next(b for b in broadcasts if b["type"] == "download.error")
        assert error_event.get("kind") == "local_llm"
        assert "network failure" in error_event.get("message", "")

        # Config must NOT be saved on error
        server._app.config_manager.save.assert_not_called()

    def test_progress_events_carry_kind_discriminator(self, tmp_path):
        """Progress callback broadcasts must include kind='local_llm'."""
        server, broadcasts = self._server_with_broadcasts()

        captured_callback: list = []

        def _fake_dl(model_id, gguf_path, *, progress_callback=None, cancel_event=None):
            captured_callback.append(progress_callback)
            if progress_callback:
                progress_callback(1024, 2048)
                progress_callback(2048, 2048)

        with (
            patch("whisperstream.setup.download_llm_model", side_effect=_fake_dl),
            patch(
                "whisperstream.config_manager.get_local_llm_dir",
                return_value=tmp_path,
            ),
            patch("whisperstream.setup.cleanup_stale_llm_models"),
        ):
            server._loop = None
            server._run_llm_model_download("gemma4-e2b-instruct")

        progress_events = [b for b in broadcasts if b["type"] == "download.progress"]
        assert len(progress_events) == 2
        for ev in progress_events:
            assert ev.get("kind") == "local_llm"
            assert "downloaded" in ev
            assert "total" in ev

    def test_cleanup_not_called_after_successful_download(self, tmp_path):
        """cleanup_stale_llm_models must NOT run after a good download.

        A picker keeps all installed models (keep-all-downloaded); the
        download worker no longer evicts prior GGUFs. Explicit per-model
        delete (the ``/local-download/delete`` endpoint) is the only way
        a GGUF is removed.
        """
        server, broadcasts = self._server_with_broadcasts()

        with (
            patch("whisperstream.setup.download_llm_model"),
            patch(
                "whisperstream.config_manager.get_local_llm_dir",
                return_value=tmp_path,
            ),
            patch("whisperstream.setup.cleanup_stale_llm_models") as mock_cleanup,
        ):
            server._loop = None
            server._run_llm_model_download("gemma4-e2b-instruct")

        mock_cleanup.assert_not_called()

        # The download itself still succeeds: config is saved with the new
        # model id and a completion event is broadcast.
        server._app.config_manager.save.assert_called_once()
        types = [b["type"] for b in broadcasts]
        assert "download.complete" in types

    def test_thread_state_cleared_in_finally(self, tmp_path):
        """_download_thread and _download_cancel_event must be None after any outcome."""
        server, _ = self._server_with_broadcasts()

        with (
            patch("whisperstream.setup.download_llm_model"),
            patch(
                "whisperstream.config_manager.get_local_llm_dir",
                return_value=tmp_path,
            ),
            patch("whisperstream.setup.cleanup_stale_llm_models"),
        ):
            server._loop = None
            server._run_llm_model_download("gemma4-e2b-instruct")

        assert server._download_thread is None
        assert server._download_cancel_event is None


# ---------------------------------------------------------------------------
# GET /api/system/info — total physical RAM in MB
# ---------------------------------------------------------------------------


class TestGetSystemInfo:
    def test_returns_total_ram_mb_int_on_success(self):
        """Successful psutil call -> {total_ram_mb: <int>}."""
        client, _ = _make_client()
        with patch(
            "whisperstream.system_info.get_total_ram_mb",
            return_value=8192,
        ):
            resp = client.get("/api/system/info")

        assert resp.status_code == 200
        data = resp.json()
        assert data == {"total_ram_mb": 8192}
        assert isinstance(data["total_ram_mb"], int)

    def test_returns_null_when_get_total_ram_mb_returns_none(self):
        """Detection-failure path: helper returns None -> JSON null."""
        client, _ = _make_client()
        with patch(
            "whisperstream.system_info.get_total_ram_mb",
            return_value=None,
        ):
            resp = client.get("/api/system/info")

        assert resp.status_code == 200
        assert resp.json() == {"total_ram_mb": None}

    def test_returns_null_when_psutil_raises(self):
        """psutil raising at the leaf is caught by get_total_ram_mb -> null."""
        client, _ = _make_client()
        with patch(
            "whisperstream.system_info.psutil.virtual_memory",
            side_effect=RuntimeError("psutil unavailable"),
        ):
            resp = client.get("/api/system/info")

        assert resp.status_code == 200
        assert resp.json() == {"total_ram_mb": None}


# ---------------------------------------------------------------------------
# POST /api/enhancement/benchmark/start + GET /api/enhancement/benchmark/verdict
# ---------------------------------------------------------------------------


class TestBenchmarkEndpoints:
    """Endpoint-contract tests for the GPU benchmark surface (Task 2.4).

    ``run_benchmark`` is mocked throughout so these tests need no GPU, no
    llama-server binary, and no model download — they exercise only the REST
    contract (start → 200/started, concurrent → 409, verdict seed).
    """

    def test_start_returns_started(self):
        """A first POST kicks the background thread and returns started."""
        client, server = _make_client()

        # Patch the worker AND threading.Thread so nothing real runs and the
        # mock thread reports not-alive (so a follow-up call wouldn't 409).
        with (
            patch.object(server._benchmark, "run") as mock_run,
            patch("threading.Thread") as MockThread,
        ):
            mock_run.side_effect = lambda: None
            mock_thread = MagicMock()
            mock_thread.is_alive.return_value = False
            MockThread.return_value = mock_thread

            resp = client.post("/api/enhancement/benchmark/start")

        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        # The benchmark thread was wired up + started.
        assert server._benchmark.thread is mock_thread
        mock_thread.start.assert_called_once()

    def test_second_concurrent_call_returns_409(self):
        """A second POST while a benchmark thread is alive returns 409."""
        client, server = _make_client()

        alive_thread = MagicMock()
        alive_thread.is_alive.return_value = True
        server._benchmark.thread = alive_thread

        resp = client.post("/api/enhancement/benchmark/start")

        assert resp.status_code == 409
        assert resp.json()["status"] == "busy"

    def test_start_no_body_defaults_auto(self):
        """POST with no body still returns started and wires mode=auto kwargs."""
        client, server = _make_client()

        with (
            patch.object(server._benchmark, "run") as mock_run,
            patch("threading.Thread") as MockThread,
        ):
            mock_run.side_effect = lambda **_kw: None
            mock_thread = MagicMock()
            mock_thread.is_alive.return_value = False
            MockThread.return_value = mock_thread

            resp = client.post("/api/enhancement/benchmark/start")

        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        # The worker thread targets _run_benchmark with the auto default.
        assert MockThread.call_args.kwargs["kwargs"] == {
            "mode": "auto",
            "model_id": None,
        }

    def test_start_manual_body_wires_kwargs(self):
        """POST {"mode":"manual","model_id":"stream-1"} threads those kwargs."""
        client, server = _make_client()

        with (
            patch.object(server._benchmark, "run") as mock_run,
            patch("threading.Thread") as MockThread,
        ):
            mock_run.side_effect = lambda **_kw: None
            mock_thread = MagicMock()
            mock_thread.is_alive.return_value = False
            MockThread.return_value = mock_thread

            resp = client.post(
                "/api/enhancement/benchmark/start",
                json={"mode": "manual", "model_id": "stream-1"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        assert MockThread.call_args.kwargs["kwargs"] == {
            "mode": "manual",
            "model_id": "stream-1",
        }

    def test_start_logs_requested_and_active_models(self, caplog):
        config = _make_mock_config(
            enhancement_local_model="gemma4-e2b-instruct"
        )
        client, server = _make_client(config=config)

        with (
            patch.object(server._benchmark, "run"),
            patch("threading.Thread") as thread_cls,
            caplog.at_level("INFO", logger="whisperstream.server"),
        ):
            mock_thread = MagicMock()
            mock_thread.is_alive.return_value = False
            thread_cls.return_value = mock_thread
            response = client.post(
                "/api/enhancement/benchmark/start",
                json={"mode": "manual", "model_id": "stream-1"},
            )

        assert response.status_code == 200
        assert (
            "[Benchmark] start requested mode=manual requested_model=stream-1 "
            "active_model=gemma4-e2b-instruct"
        ) in caplog.messages

    def test_candidate_cannot_enter_manual_benchmark(
        self, tmp_path, monkeypatch
    ):
        _enable_candidate_overlay(tmp_path, monkeypatch)
        client, server = _make_client()

        with patch("threading.Thread") as thread_cls:
            resp = client.post(
                "/api/enhancement/benchmark/start",
                json={"mode": "manual", "model_id": "candidate-available"},
            )

        assert resp.status_code == 400
        assert resp.json() == {
            "error": "candidate_benchmark_not_supported",
            "model_id": "candidate-available",
        }
        thread_cls.assert_not_called()
        assert server._benchmark.thread is None

    def test_start_unknown_mode_defaults_auto(self):
        """A bogus mode in the body falls through to the auto default."""
        client, server = _make_client()

        with (
            patch.object(server._benchmark, "run") as mock_run,
            patch("threading.Thread") as MockThread,
        ):
            mock_run.side_effect = lambda **_kw: None
            mock_thread = MagicMock()
            mock_thread.is_alive.return_value = False
            MockThread.return_value = mock_thread

            resp = client.post(
                "/api/enhancement/benchmark/start",
                json={"mode": "bogus"},
            )

        assert resp.status_code == 200
        assert MockThread.call_args.kwargs["kwargs"] == {
            "mode": "auto",
            "model_id": None,
        }

    def test_verdict_endpoint_returns_cached_config_value(self, tmp_path, monkeypatch):
        monkeypatch.setattr("whisperstream.config_manager.get_local_llm_dir", lambda: tmp_path)
        (tmp_path / "gemma-4-E2B-it-Q4_K_M.gguf").write_bytes(b"installed baseline")
        """The verdict seed endpoint echoes the active model's verdict + device."""
        config = _make_mock_config(
            enhancement_local_gpu_verdicts={"gemma4-e2b-instruct": "viable"},
            enhancement_local_gpu_device="NVIDIA GeForce RTX 4070 Ti SUPER",
        )
        client, _ = _make_client(config=config)

        resp = client.get("/api/enhancement/benchmark/verdict")

        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "viable"
        assert data["device"] == "NVIDIA GeForce RTX 4070 Ti SUPER"

    def test_verdict_endpoint_defaults_not_run(self):
        """A fresh install (empty map, empty device) round-trips through the seed."""
        config = _make_mock_config(
            enhancement_local_gpu_verdicts={},
            enhancement_local_gpu_device="",
        )
        client, _ = _make_client(config=config)

        resp = client.get("/api/enhancement/benchmark/verdict")

        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "not_run"
        assert data["device"] == ""


class TestRunBenchmarkWorker:
    """Unit-level tests for the _run_benchmark worker (Task 2.4).

    Called directly (not via HTTP) with ``run_benchmark`` / the download leg
    mocked so we can assert the broadcast sequence and the single config write
    without real hardware.
    """

    def _server_with_broadcasts(self, **config_overrides):
        config = _make_mock_config(**config_overrides) if config_overrides else None
        server = _make_server(config=config)
        broadcasts: list[dict] = []
        server.broadcast = lambda event: broadcasts.append(event)  # type: ignore[method-assign]
        return server, broadcasts

    # Stable on-disk GGUF filenames used by the synthetic tier fixtures.
    _GEMMA_GGUF = "gemma-4-E2B-it-Q4_K_M.gguf"
    _STREAM_GGUF = "stream-1-Q4_K_M.gguf"

    _ONE_DEVICE = (
        "Available devices:\n"
        "  Vulkan0: NVIDIA GeForce RTX 4070 Ti SUPER (16063 MiB, 15295 MiB free)\n"
    )
    _TWO_DEVICE = (
        "Available devices:\n"
        "  Vulkan0: AMD Radeon(TM) Graphics (16186 MiB, 15376 MiB free)\n"
        "  Vulkan1: NVIDIA GeForce RTX 4070 Ti SUPER (16063 MiB, 15295 MiB free)\n"
    )

    def _auto_patches(
        self, server, tmp_path, list_out, fetch_side_effect, bench_side_effect
    ):
        """Return the 5 patches the auto/manual worker needs (unpack + `with`).

        Patches ``_fetch_gguf_streaming`` on the SERVER (the low-level download
        leg the auto ladder calls directly) plus the binary/list/dir/run_benchmark
        seams, so no GPU, binary, or network is required.
        """
        def fetch_with_artifact(model_id, cancel_event):
            result = fetch_side_effect(model_id, cancel_event)
            if result[0] and not cancel_event.is_set():
                from whisperstream.enhancement_models import get_local_bundled_model
                entry = get_local_bundled_model(model_id)
                if entry:
                    (tmp_path / entry["gguf_filename"]).write_bytes(b"mock benchmark artifact")
            return result
        return (
            patch(
                "whisperstream.llama_server.find_bundled_llama_server",
                return_value=tmp_path / "llama-server.exe",
            ),
            patch(
                "whisperstream.gpu_benchmark.list_devices_stdout",
                return_value=list_out,
            ),
            patch(
                "whisperstream.config_manager.get_local_llm_dir",
                return_value=tmp_path,
            ),
            patch.object(
                server._gguf_download, "fetch", side_effect=fetch_with_artifact
            ),
            patch(
                "whisperstream.gpu_benchmark.run_benchmark",
                side_effect=bench_side_effect,
            ),
        )

    @staticmethod
    def _fetch_ok(order=None, existed=False):
        """A ``_fetch_gguf_streaming`` stub: records the fetch + returns (ok, existed)."""
        def _fetch(model_id, cancel_event):
            if order is not None:
                order.append(("fetch", model_id))
            return (True, existed)
        return _fetch

    @staticmethod
    def _bench_by_file(mapping, order=None):
        """An async ``run_benchmark`` stub keyed by the GGUF filename.

        ``mapping`` is ``{gguf_filename: (verdict, tok_per_s)}``. A filename
        absent from the mapping raises KeyError, which doubles as an assertion
        that a rung was NOT supposed to be measured.
        """
        from whisperstream.gpu_benchmark import BenchmarkResult

        async def _bench(*, binary_path, gguf_path, device_index=None, on_tick=None, prompt_key=""):
            name = Path(gguf_path).name
            if order is not None:
                order.append(("bench", name, device_index))
            verdict, tps = mapping[name]
            return BenchmarkResult(
                verdict=verdict, predicted_per_second=tps, predicted_n=200
            )
        return _bench

    def test_bench_one_rung_broadcasts_live_and_terminal_device_events(self, tmp_path):
        """Each device sends start, live WPS, then its authoritative terminal result."""
        from whisperstream.gpu_benchmark import BenchmarkResult, VulkanDevice

        server, broadcasts = self._server_with_broadcasts()
        device = VulkanDevice(index=0, name="Test GPU", total_mib=8192)

        async def _bench(*, binary_path, gguf_path, device_index=None, on_tick=None, prompt_key=""):
            assert on_tick is not None
            on_tick(12.5)
            return BenchmarkResult(
                verdict="viable",
                predicted_per_second=80.0,
                predicted_n=200,
            )

        with patch(
            "whisperstream.gpu_benchmark.run_benchmark",
            side_effect=_bench,
        ):
            server._benchmark.bench_one_rung(
                tmp_path / "llama-server.exe",
                tmp_path / self._STREAM_GGUF,
                [device],
                "stream-1",
            )

        live_tick = next(b for b in broadcasts if "wps" in b)
        assert live_tick == {
            "type": "benchmark.progress",
            "phase": "running",
            "model_id": "stream-1",
            "device_name": "Test GPU",
            "device_index": 0,
            "wps": 12.5,
        }
        assert "tps" not in live_tick

        device_start = next(b for b in broadcasts if "wps" not in b)
        assert device_start == {
            "type": "benchmark.progress",
            "phase": "running",
            "model_id": "stream-1",
            "device_name": "Test GPU",
            "device_index": 0,
        }
        device_complete = next(
            b for b in broadcasts if b.get("phase") == "device_complete"
        )
        assert device_complete == {
            "type": "benchmark.progress",
            "phase": "device_complete",
            "model_id": "stream-1",
            "device_name": "Test GPU",
            "device_index": 0,
            "measured_tok_per_s": 80.0,
            "predicted_n": 200,
            "verdict": "viable",
        }
        assert broadcasts == [device_start, live_tick, device_complete]

    def test_bench_one_rung_broadcasts_fail_closed_device_completion(self, tmp_path):
        """A failed device attempt still gets an ordered terminal handoff event."""
        from whisperstream.gpu_benchmark import VulkanDevice

        server, broadcasts = self._server_with_broadcasts()
        device = VulkanDevice(index=3, name="Failing GPU", total_mib=4096)

        async def _bench(*, binary_path, gguf_path, device_index=None, on_tick=None, prompt_key=""):
            raise RuntimeError("benchmark subprocess exited")

        with patch(
            "whisperstream.gpu_benchmark.run_benchmark",
            side_effect=_bench,
        ):
            with pytest.raises(RuntimeError, match="no completed measurement"):
                server._benchmark.bench_one_rung(
                    tmp_path / "llama-server.exe",
                    tmp_path / self._STREAM_GGUF,
                    [device],
                    "stream-1",
                )

        assert broadcasts == [
            {
                "type": "benchmark.progress",
                "phase": "running",
                "model_id": "stream-1",
                "device_name": "Failing GPU",
                "device_index": 3,
            },
            {
                "type": "benchmark.progress",
                "phase": "device_complete",
                "model_id": "stream-1",
                "device_name": "Failing GPU",
                "device_index": 3,
                "measured_tok_per_s": None,
                "predicted_n": None,
                "verdict": "unviable",
            },
        ]

    def test_benchmark_event_send_logs_only_allowlisted_status(
        self, caplog
    ):
        server, broadcasts = self._server_with_broadcasts()
        event = {
            "type": "benchmark.progress",
            "phase": "running",
            "model_id": "gemma4-e2b-instruct",
            "device_name": "Test GPU",
            "device_index": 2,
            "wps": 41.25,
        }

        with caplog.at_level("DEBUG", logger="whisperstream.server"):
            server._benchmark.publish(event)

        assert broadcasts == [event]
        assert (
            "[Benchmark] event send type=benchmark.progress phase=running "
            "verdict=none mode=none"
        ) in caplog.messages
        assert "Test GPU" not in caplog.text
        assert "gemma4-e2b-instruct" not in caplog.text

    # ---- AUTO ladder -----------------------------------------------------

    @pytest.mark.parametrize("verdict,tps", [("viable", 180.0), ("marginal", 35.0), ("unviable", 10.0)])
    @pytest.mark.parametrize("model_id,filename", [
        ("gemma4-e2b-instruct", "gemma-4-E2B-it-Q4_K_M.gguf"),
    ])
    def test_manual_benchmarks_only_requested_public_gemma(self, tmp_path, verdict, tps, model_id, filename):
        order = []
        server, broadcasts = self._server_with_broadcasts()
        p1, p2, p3, p4, p5 = self._auto_patches(
            server, tmp_path, self._ONE_DEVICE, self._fetch_ok(order),
            self._bench_by_file({filename: (verdict, tps)}, order),
        )
        with p1, p2, p3, p4, p5:
            server._benchmark.run(mode="manual", model_id=model_id)
        assert [e for e in order if e[0] == "fetch"] == [("fetch", model_id)]
        assert [e[1] for e in order if e[0] == "bench"] == [filename]
        result = next(b for b in broadcasts if b["type"] == "benchmark.result")
        assert result["mode"] == "manual"
        assert result["measured_model_id"] == model_id
        assert result["verdict"] == verdict
        assert result["verdicts"] == {model_id: verdict}
        assert sum(b["type"] == "download.complete" for b in broadcasts) == 1
        server._app.config_manager.save.assert_called_once()

    def test_auto_download_cancelled_writes_no_config(self, tmp_path):
        """Cancel mid-download (event set) → no benchmark.result, no config write."""
        server, broadcasts = self._server_with_broadcasts()

        def _fetch_cancel(model_id, cancel_event):
            # Simulate the cancel endpoint firing mid-download.
            cancel_event.set()
            return (False, False)

        p1, p2, p3, p4, p5 = self._auto_patches(
            server, tmp_path, self._ONE_DEVICE,
            _fetch_cancel,
            self._bench_by_file({}),  # never reached
        )
        with p1, p2, p3, p4, p5:
            server._benchmark.run()

        server._app.config_manager.save.assert_not_called()
        assert not any(b["type"] == "benchmark.result" for b in broadcasts)
        assert not any(b["type"] == "download.complete" for b in broadcasts)

    def test_auto_download_cancelled_exception_writes_no_config(self, tmp_path):
        """A DownloadCancelled raised from the fetch helper aborts cleanly."""
        from whisperstream.setup import DownloadCancelled

        server, broadcasts = self._server_with_broadcasts()

        def _fetch_raise(model_id, cancel_event):
            raise DownloadCancelled()

        p1, p2, p3, p4, p5 = self._auto_patches(
            server, tmp_path, self._ONE_DEVICE,
            _fetch_raise,
            self._bench_by_file({}),
        )
        with p1, p2, p3, p4, p5:
            server._benchmark.run()

        server._app.config_manager.save.assert_not_called()
        assert not any(b["type"] == "benchmark.result" for b in broadcasts)
        assert not any(b["type"] == "download.complete" for b in broadcasts)

    def test_auto_download_error_is_retryable_without_false_verdict(self, tmp_path):
        """A download failure is infrastructure, not a measured slow model."""
        server, broadcasts = self._server_with_broadcasts()
        p1, p2, p3, p4, p5 = self._auto_patches(
            server,
            tmp_path,
            self._ONE_DEVICE,
            RuntimeError("Hugging Face unavailable"),
            self._bench_by_file({}),
        )

        with p1, p2, p3, p4, p5:
            server._benchmark.run()

        result = next(b for b in broadcasts if b["type"] == "benchmark.result")
        assert result["outcome"] == "infrastructure_error"
        assert not result.get("verdicts")
        server._app.config_manager.save.assert_not_called()
        assert server._app.config.enhancement_local_gpu_verdicts == {}

    def test_manual_benches_each_device_picks_highest_tps(self, tmp_path):
        """Two real GPUs → bench each for a rung, pick the highest-tps device."""
        server, broadcasts = self._server_with_broadcasts()

        from whisperstream.gpu_benchmark import BenchmarkResult

        async def _bench(*, binary_path, gguf_path, device_index=None, on_tick=None, prompt_key=""):
            # Vulkan0 (AMD) slow, Vulkan1 (RTX) fast — for stream-1's gguf.
            tps = 30.0 if device_index == 0 else 180.0
            verdict = "viable" if tps >= 50 else "unviable"
            return BenchmarkResult(
                verdict=verdict, predicted_per_second=tps, predicted_n=200
            )

        p1, p2, p3, p4, p5 = self._auto_patches(
            server, tmp_path, self._TWO_DEVICE,
            self._fetch_ok(),
            _bench,
        )
        with p1, p2, p3, p4, p5:
            server._benchmark.run(mode="manual", model_id="gemma4-e2b-instruct")

        result = next(b for b in broadcasts if b["type"] == "benchmark.result")
        assert result["verdict"] == "viable"
        assert result["measured_model_id"] == "gemma4-e2b-instruct"
        # Highest-tps device (Vulkan1, RTX @180) wins over Vulkan0 (AMD @30).
        assert result["measured_tok_per_s"] == 180.0
        assert result["device"] == "NVIDIA GeForce RTX 4070 Ti SUPER"
        device_starts = [
            b
            for b in broadcasts
            if b["type"] == "benchmark.progress"
            and b.get("phase") == "running"
            and "wps" not in b
        ]
        assert [
            (event["device_name"], event["device_index"])
            for event in device_starts
        ] == [
            ("AMD Radeon(TM) Graphics", 0),
            ("NVIDIA GeForce RTX 4070 Ti SUPER", 1),
        ]
        device_event_order = [
            (event["phase"], event["device_name"])
            for event in broadcasts
            if event["type"] == "benchmark.progress"
            and event.get("phase") in ("running", "device_complete")
        ]
        assert device_event_order == [
            ("running", "AMD Radeon(TM) Graphics"),
            ("device_complete", "AMD Radeon(TM) Graphics"),
            ("running", "NVIDIA GeForce RTX 4070 Ti SUPER"),
            ("device_complete", "NVIDIA GeForce RTX 4070 Ti SUPER"),
        ]
        saved = server._app.config
        assert saved.enhancement_local_gpu_device_index == 1

    # ---- MANUAL single-model --------------------------------------------

    def test_manual_gemma_marginal_returns_marginal(self, tmp_path):
        """Manual Gemma bench landing marginal → result verdict marginal, mode manual."""
        server, broadcasts = self._server_with_broadcasts()

        p1, p2, p3, p4, p5 = self._auto_patches(
            server, tmp_path, self._ONE_DEVICE,
            self._fetch_ok(),
            self._bench_by_file({self._GEMMA_GGUF: ("marginal", 40.0)}),
        )
        with p1, p2, p3, p4, p5:
            server._benchmark.run(mode="manual", model_id="gemma4-e2b-instruct")

        result = next(b for b in broadcasts if b["type"] == "benchmark.result")
        assert result["verdict"] == "marginal"
        assert result["mode"] == "manual"
        assert result["measured_model_id"] == "gemma4-e2b-instruct"
        assert result["measured_tok_per_s"] == 40.0
        assert result["verdicts"] == {"gemma4-e2b-instruct": "marginal"}
        assert result["freshly_downloaded"] is True
        saved = server._app.config
        assert saved.enhancement_local_gpu_verdicts["gemma4-e2b-instruct"] == "marginal"
        server._app.config_manager.save.assert_called_once()
        assert server._app.llama_server is None

    @pytest.mark.asyncio
    async def test_ensure_repoints_existing_supervisor_before_warm(self, tmp_path):
        """A selected-GGUF mismatch is corrected before kick_warm is requested."""
        selected_path = tmp_path / self._GEMMA_GGUF
        selected_path.write_bytes(b"selected")
        old_path = tmp_path / self._STREAM_GGUF
        old_path.write_bytes(b"old")
        config = _make_mock_config(
            enhancement_enabled=True,
            enhancement_provider="local",
            enhancement_local_model="gemma4-e2b-instruct",
            enhancement_local_idle_timeout=600,
        )
        supervisor = MagicMock()
        supervisor._gguf_path = old_path
        order: list[str] = []

        async def _restart(path):
            assert path == selected_path
            order.append("repoint")
            supervisor._gguf_path = path

        supervisor.restart_for_model = AsyncMock(side_effect=_restart)
        supervisor.kick_warm.side_effect = lambda _loop: order.append("warm")
        server = _make_server(config=config, llama_server=supervisor)
        running_loop = asyncio.get_running_loop()
        server._loop = running_loop
        server._app._server_loop = running_loop
        server._app._can_warm_local.return_value = True

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            await server._ensure_selected_llama_server(warm_if_enabled=True)

        supervisor.restart_for_model.assert_awaited_once_with(selected_path)
        supervisor.kick_warm.assert_called_once_with(running_loop)
        assert order == ["repoint", "warm"]

    @pytest.mark.asyncio
    async def test_ensure_endpoint_override_leaves_existing_supervisor_untouched(
        self, tmp_path
    ):
        """External endpoints must not repoint or warm a leftover supervisor."""
        selected_path = tmp_path / self._GEMMA_GGUF
        selected_path.write_bytes(b"selected")
        supervisor = MagicMock()
        supervisor._gguf_path = tmp_path / self._STREAM_GGUF
        supervisor.restart_for_model = AsyncMock()
        config = _make_mock_config(
            enhancement_enabled=True,
            enhancement_provider="local",
            enhancement_local_model="gemma4-e2b-instruct",
            enhancement_local_endpoint="http://127.0.0.1:11434/v1",
        )
        server = _make_server(config=config, llama_server=supervisor)
        server._loop = asyncio.get_running_loop()
        server._app._server_loop = server._loop

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            await server._ensure_selected_llama_server(warm_if_enabled=True)

        supervisor.restart_for_model.assert_not_awaited()
        supervisor.kick_warm.assert_not_called()
        server._app._can_warm_local.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_rechecks_acceptance_before_warm(self, tmp_path):
        """A grant revoked after scheduling prevents the eventual warm kick."""
        selected_path = tmp_path / self._GEMMA_GGUF
        selected_path.write_bytes(b"selected")
        supervisor = MagicMock()
        supervisor._gguf_path = selected_path
        supervisor.restart_for_model = AsyncMock()
        config = _make_mock_config(
            enhancement_enabled=True,
            enhancement_provider="local",
            enhancement_local_model="gemma4-e2b-instruct",
        )
        server = _make_server(config=config, llama_server=supervisor)
        server._loop = asyncio.get_running_loop()
        server._app._server_loop = server._loop
        server._app._acceptance_is_current.return_value = False

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            await server._ensure_selected_llama_server(warm_if_enabled=True)

        supervisor.restart_for_model.assert_not_awaited()
        supervisor.kick_warm.assert_not_called()
        server._app._can_warm_local.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_promotes_pool_owned_selected_model_without_duplicate(
        self, tmp_path
    ):
        selected_path = tmp_path / self._GEMMA_GGUF
        selected_path.write_bytes(b"selected")
        old_path = tmp_path / self._STREAM_GGUF
        old_path.write_bytes(b"old")
        config = _make_mock_config(
            enhancement_enabled=True,
            enhancement_provider="local",
            enhancement_local_model="gemma4-e2b-instruct",
            enhancement_local_idle_timeout=600,
        )
        old_supervisor = MagicMock()
        old_supervisor.gguf_path = old_path
        old_supervisor._gguf_path = old_path
        old_supervisor.is_residency_pinned = False
        old_supervisor.stop = AsyncMock()
        old_supervisor.restart_for_model = AsyncMock()
        promoted_supervisor = MagicMock()
        promoted_supervisor.gguf_path = selected_path
        promoted = MagicMock(supervisor=promoted_supervisor, owned_by_pool=True)
        pool = MagicMock()
        pool.find_resident.return_value = promoted
        pool.owns_supervisor.side_effect = lambda value: value is promoted_supervisor
        server = _make_server(config=config, llama_server=old_supervisor)
        server._app.resident_llama_pool = pool

        with patch(
            "whisperstream.config_manager.get_local_llm_dir",
            return_value=tmp_path,
        ):
            await server._ensure_selected_llama_server()

        assert server._app.llama_server is promoted_supervisor
        old_supervisor.stop.assert_awaited_once()
        old_supervisor.restart_for_model.assert_not_awaited()
        promoted_supervisor.restart_for_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_keeps_pinned_old_model_and_creates_new_production_alias(
        self, tmp_path
    ):
        selected_path = tmp_path / self._GEMMA_GGUF
        selected_path.write_bytes(b"selected")
        old_path = tmp_path / self._STREAM_GGUF
        old_path.write_bytes(b"old")
        config = _make_mock_config(
            enhancement_enabled=True,
            enhancement_provider="local",
            enhancement_local_model="gemma4-e2b-instruct",
            enhancement_local_idle_timeout=600,
        )
        old_supervisor = MagicMock()
        old_supervisor.gguf_path = old_path
        old_supervisor._gguf_path = old_path
        old_supervisor.is_residency_pinned = True
        old_supervisor.stop = AsyncMock()
        old_supervisor.restart_for_model = AsyncMock()
        new_supervisor = MagicMock()
        pool = MagicMock()
        pool.find_resident.return_value = None
        server = _make_server(config=config, llama_server=old_supervisor)
        server._app.resident_llama_pool = pool

        with (
            patch(
                "whisperstream.config_manager.get_local_llm_dir",
                return_value=tmp_path,
            ),
            patch(
                "whisperstream.llama_server.find_bundled_llama_server",
                return_value=tmp_path / "llama-server.exe",
            ),
            patch(
                "whisperstream.llama_server.LlamaServerSupervisor",
                return_value=new_supervisor,
            ) as supervisor_cls,
        ):
            await server._ensure_selected_llama_server()

        assert server._app.llama_server is new_supervisor
        assert supervisor_cls.call_args.args[0] == selected_path
        old_supervisor.stop.assert_not_awaited()
        old_supervisor.restart_for_model.assert_not_awaited()

    def test_manual_gemma_viable(self, tmp_path):
        """Manual Stream-1 viable → result viable for stream-1 only (no lite measure)."""
        order: list = []
        server, broadcasts = self._server_with_broadcasts()

        p1, p2, p3, p4, p5 = self._auto_patches(
            server, tmp_path, self._ONE_DEVICE,
            self._fetch_ok(),
            self._bench_by_file({self._GEMMA_GGUF: ("viable", 175.0)}, order),
        )
        with p1, p2, p3, p4, p5:
            server._benchmark.run(mode="manual", model_id="gemma4-e2b-instruct")

        # Only stream-1 measured — no ladder, no lite.
        benched = [e[1] for e in order if e[0] == "bench"]
        assert benched == [self._GEMMA_GGUF]
        result = next(b for b in broadcasts if b["type"] == "benchmark.result")
        assert result["verdict"] == "viable"
        assert result["mode"] == "manual"
        assert result["measured_model_id"] == "gemma4-e2b-instruct"
        assert result["verdicts"] == {"gemma4-e2b-instruct": "viable"}
        assert "freshly_downloaded_ids" not in result  # manual uses the singular bool
        assert result["freshly_downloaded"] is True

    def test_manual_download_error_keeps_stored_selection_and_verdicts(self, tmp_path):
        server, broadcasts = self._server_with_broadcasts(
            enhancement_local_model="stream-1",
            enhancement_local_gpu_verdicts={},
        )
        p1, p2, p3, p4, p5 = self._auto_patches(
            server,
            tmp_path,
            self._ONE_DEVICE,
            RuntimeError("download failed"),
            self._bench_by_file({}),
        )

        with p1, p2, p3, p4, p5:
            server._benchmark.run(
                mode="manual", model_id="gemma4-e2b-instruct"
            )

        result = next(b for b in broadcasts if b["type"] == "benchmark.result")
        assert result["outcome"] == "infrastructure_error"
        assert not result.get("verdicts")
        assert server._app.config.enhancement_local_model == "stream-1"
        assert server._app.config.enhancement_local_gpu_verdicts == {}
        server._app.config_manager.save.assert_not_called()


class TestMaybePrepareValidation:
    """Launch-time GPU validation in _maybe_prepare_llama_server (round 02)."""

    def _server(self, **config_overrides):
        config = _make_mock_config(
            enhancement_provider="local",
            enhancement_local_endpoint="",
            enhancement_local_idle_timeout=600,
            enhancement_enabled=True,
            **config_overrides,
        )
        return _make_server(config=config)

    def _patches(self, tmp_path, list_out):
        (tmp_path / "gemma-4-E2B-it-Q4_K_M.gguf").write_bytes(b"x" * 16)
        return (
            patch(
                "whisperstream.llama_server.find_bundled_llama_server",
                return_value=tmp_path / "llama-server.exe",
            ),
            patch(
                "whisperstream.config_manager.get_local_llm_dir", return_value=tmp_path
            ),
            patch(
                "whisperstream.gpu_benchmark.list_devices_stdout", return_value=list_out
            ),
        )

    def test_gpu_set_change_invalidates_and_disables(self, tmp_path):
        """A changed GPU set → verdict not_run, enhancement disabled, recheck flagged."""
        server = self._server(
            enhancement_local_gpu_verdicts={"gemma4-e2b-instruct": "viable"},
            enhancement_local_gpu_device="NVIDIA GeForce RTX 4070 Ti SUPER",
            enhancement_local_gpu_device_index=0,
            enhancement_local_gpu_fingerprint="STALE GPU:4096",
        )
        list_out = (
            "Available devices:\n"
            "  Vulkan0: NVIDIA GeForce RTX 4070 Ti SUPER (16063 MiB, 15295 MiB free)\n"
        )
        p1, p2, p3 = self._patches(tmp_path, list_out)
        with p1, p2, p3:
            server._maybe_prepare_llama_server()
        cfg = server._app.config
        assert cfg.enhancement_local_gpu_verdicts == {}
        assert cfg.enhancement_enabled is False
        assert cfg.enhancement_local_gpu_device_index == -1
        assert server._benchmark.recheck_pending is True

    def test_same_gpu_set_refreshes_index(self, tmp_path):
        """An unchanged GPU set keeps the verdict but refreshes a stale pinned index."""
        server = self._server(
            enhancement_local_gpu_verdicts={"gemma4-e2b-instruct": "viable"},
            enhancement_local_gpu_device="NVIDIA GeForce RTX 4070 Ti SUPER",
            enhancement_local_gpu_device_index=5,  # stale (device now at index 0)
            enhancement_local_gpu_fingerprint="NVIDIA GeForce RTX 4070 Ti SUPER:16063",
        )
        list_out = (
            "Available devices:\n"
            "  Vulkan0: NVIDIA GeForce RTX 4070 Ti SUPER (16063 MiB, 15295 MiB free)\n"
        )
        p1, p2, p3 = self._patches(tmp_path, list_out)
        with p1, p2, p3:
            server._maybe_prepare_llama_server()
        cfg = server._app.config
        assert cfg.enhancement_local_gpu_verdicts == {"gemma4-e2b-instruct": "viable"}
        assert cfg.enhancement_local_gpu_device_index == 0  # refreshed
        assert server._benchmark.recheck_pending is False

    def test_available_candidate_prepares_from_external_artifact_path(
        self, tmp_path, monkeypatch
    ):
        available_path = _enable_candidate_overlay(tmp_path, monkeypatch)
        server = self._server(
            enhancement_local_model="candidate-available",
            enhancement_local_gpu_verdicts={},
        )

        with patch(
            "whisperstream.llama_server.find_bundled_llama_server",
            return_value=tmp_path / "llama-server.exe",
        ):
            server._maybe_prepare_llama_server()

        assert server._app.llama_server is not None
        assert server._app.llama_server._gguf_path == available_path

    def test_missing_candidate_skips_startup_prepare_safely(
        self, tmp_path, monkeypatch
    ):
        _enable_candidate_overlay(tmp_path, monkeypatch)
        server = self._server(
            enhancement_local_model="candidate-missing",
            enhancement_local_gpu_verdicts={},
        )

        server._maybe_prepare_llama_server()

        assert server._app.llama_server is None


class TestResidentLlamaLifecycleBoundaries:
    def test_relocation_requires_restart_while_any_model_is_resident(self, tmp_path):
        client, server = _make_client()
        pool = MagicMock()
        pool.has_resident.return_value = True
        server._app.resident_llama_pool = pool

        check = client.post(
            "/api/models/relocate/check",
            json={"destination": str(tmp_path / "models")},
        )
        start = client.post(
            "/api/models/relocate",
            json={"destination": str(tmp_path / "models")},
        )

        expected = {"error": "resident_models_restart_required"}
        assert check.status_code == 409
        assert check.json() == expected
        assert start.status_code == 409
        assert start.json() == expected
        assert server._download_thread is None

    def test_relocation_worker_rechecks_residency_before_unload(self, tmp_path):
        server = _make_server()
        pool = MagicMock()
        pool.has_resident.return_value = True
        server._app.resident_llama_pool = pool
        events: list[dict] = []
        server.broadcast = lambda event: events.append(event)  # type: ignore[method-assign]
        server._relocation.classify = MagicMock()
        server._unload_models = MagicMock()

        server._run_model_relocation(str(tmp_path / "models"))

        server._relocation.classify.assert_not_called()
        server._unload_models.assert_not_called()
        assert events == [
            {
                "type": "download.error",
                "kind": "relocate",
                "message": "resident_models_restart_required",
            }
        ]
        assert server._download_thread is None

    @pytest.mark.asyncio
    async def test_shutdown_stops_borrowed_and_owned_pool_lifecycles(self):
        borrowed = MagicMock()
        borrowed.stop = AsyncMock()
        owned_alias = MagicMock()
        owned_alias.stop = AsyncMock()
        pool = MagicMock()
        pool.borrowed_supervisors.return_value = (borrowed,)
        pool.owns_supervisor.side_effect = lambda value: value is owned_alias
        pool.stop_all = AsyncMock()
        server = _make_server(llama_server=owned_alias)
        server._app.resident_llama_pool = pool

        await server._maybe_stop_resident_llama_pool()

        borrowed.stop.assert_awaited_once()
        owned_alias.stop.assert_not_awaited()
        pool.stop_all.assert_awaited_once()
        assert server._app.resident_llama_pool is None
        assert server._app.llama_server is None


# ---------------------------------------------------------------------------
# GET /api/health — shape and model_loaded tracking (Task 2.4)
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Verify /api/health response shape and that model_loaded tracks the active
    STT manager regardless of which backend is selected.

    The endpoint must return exactly {status: str, model_loaded: bool} — the
    shape is unchanged by the qwen3-asr feature branch.  model_loaded is True
    when app.stt_engine is non-None (the attribute stores whichever manager was
    returned by make_stt_manager after Task 2.3) and False when None.
    """

    def test_health_response_shape_unchanged(self):
        """GET /api/health returns exactly {status, model_loaded} with correct types."""
        client, _ = _make_client()
        resp = client.get("/api/health")

        assert resp.status_code == 200
        data = resp.json()
        # Exact key set — no extra fields added by the qwen3-asr branch.
        assert set(data.keys()) == {"status", "model_loaded"}
        assert isinstance(data["status"], str)
        assert isinstance(data["model_loaded"], bool)
        assert data["status"] == "ok"

    def test_health_model_loaded_true_when_manager_set(self):
        """model_loaded=True when app.stt_engine holds the active STT manager."""
        # _make_mock_app sets app.stt_engine = MagicMock() (non-None) by default,
        # which is exactly the post-Task-2.3 state when a manager has been
        # loaded (Parakeet or Qwen — the attribute name is unchanged).
        client, _ = _make_client()
        resp = client.get("/api/health")

        assert resp.status_code == 200
        assert resp.json()["model_loaded"] is True

    def test_health_model_loaded_false_when_no_manager(self):
        """model_loaded=False when app.stt_engine is None (model not yet loaded)."""
        # Build a server whose mock app has no loaded manager.
        server = _make_server()
        server._app.stt_engine = None
        from fastapi.testclient import TestClient
        client = TestClient(server.fastapi, raise_server_exceptions=True)

        resp = client.get("/api/health")

        assert resp.status_code == 200
        assert resp.json()["model_loaded"] is False

    def test_packaged_health_identifies_only_the_spawned_engine_generation(self):
        server = _make_server(engine_generation="generation-current")
        client = TestClient(server.fastapi, raise_server_exceptions=True)

        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.headers["x-whisperstream-engine-generation"] == "generation-current"
        assert response.json() == {"status": "ok", "model_loaded": True}


def test_packaged_shutdown_rejects_stale_generation_and_accepts_current_generation() -> None:
    server = _make_server(engine_generation="generation-current")
    server._uvicorn_server = MagicMock(should_exit=False)
    client = TestClient(server.fastapi, raise_server_exceptions=True)

    missing = client.post("/api/shutdown")
    stale = client.post(
        "/api/shutdown",
        headers={"x-whisperstream-engine-generation": "generation-stale"},
    )

    assert missing.status_code == 409
    assert stale.status_code == 409
    assert missing.json() == stale.json() == {
        "ok": False,
        "error": "stale_engine_generation",
    }
    assert server._uvicorn_server.should_exit is False

    current = client.post(
        "/api/shutdown",
        headers={"x-whisperstream-engine-generation": "generation-current"},
    )

    assert current.status_code == 200
    assert current.json() == {"ok": True}
    assert server._uvicorn_server.should_exit is True


def test_backend_and_devices_survive_typed_stt_failure() -> None:
    from whisperstream.stt_errors import NativeSttWorkerCrashed

    failure = NativeSttWorkerCrashed(
        backend="parakeet",
        model_identifier="nemo-parakeet-tdt-0.6b-v3",
        operation="load",
        report=None,
        recovery_attempted=True,
        suppressed=True,
    )
    server = _make_server()
    server._app.last_stt_failure = failure
    client = TestClient(server.fastapi, raise_server_exceptions=True)

    with (
        patch(
            "whisperstream.capture.list_input_devices",
            return_value=[{"index": 3, "name": "Test microphone"}],
        ),
        patch(
            "whisperstream.stt_factory.stt_model_status",
            return_value={
                "parakeet": {"downloaded": True, "active": True},
                "qwen": {"downloaded": False, "active": False},
            },
        ),
    ):
        health = client.get("/api/health")
        devices = client.get("/api/devices")
        config = client.get("/api/config")
        status = client.get("/api/stt/status")
        with client.websocket_connect(
            "/ws",
            headers={"origin": "http://testserver"},
        ) as websocket:
            connected_event = websocket.receive_json()

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "model_loaded": False,
        "model_health": "degraded",
        "stt_failure": failure.public_metadata(),
    }
    assert devices.status_code == 200
    assert devices.json() == [{"index": 3, "name": "Test microphone"}]
    assert config.status_code == 200
    assert status.status_code == 200
    assert connected_event["type"] == "license.status"


def test_macos_device_inventory_failure_is_not_converted_to_empty_inventory() -> None:
    client, _server = _make_client()
    with (
        patch("whisperstream.server.app.platform.system", return_value="Darwin"),
        patch(
            "whisperstream.capture.list_input_devices",
            side_effect=RuntimeError("synthetic inventory failure"),
        ),
    ):
        response = client.get("/api/devices")
    client.close()

    assert response.status_code == 503
    assert response.json() == {"error": "audio_device_inventory_unavailable"}


def test_windows_device_inventory_failure_keeps_existing_empty_list_response() -> None:
    client, _server = _make_client()
    with (
        patch("whisperstream.server.app.platform.system", return_value="Windows"),
        patch(
            "whisperstream.capture.list_input_devices",
            side_effect=RuntimeError("synthetic inventory failure"),
        ),
    ):
        response = client.get("/api/devices")
    client.close()

    assert response.status_code == 200
    assert response.json() == []


def test_windows_config_api_excludes_mac_input_identity_fields() -> None:
    from whisperstream.config_manager import WhisperstreamConfig

    config = WhisperstreamConfig(
        audio_input_device="Studio Mic",
        audio_input_device_label="Mac-only label",
    )
    client, _server = _make_client(config=config)
    with (
        patch("whisperstream.server.app.platform.system", return_value="Windows"),
        patch(
            "whisperstream.config_manager.platform.system",
            return_value="Windows",
        ),
    ):
        response = client.get("/api/config")
    client.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["audio_input_device"] == "Studio Mic"
    assert "audio_input_device_label" not in payload
    assert "audio_input_device_selection_mode" not in payload


def test_transcribe_api_returns_content_free_typed_stt_failure() -> None:
    from whisperstream.stt_errors import SttWorkerPythonFailure

    config = _make_mock_config(enhancement_enabled=False, language="en")
    server = _make_server(config=config)
    server._app.license_manager.can_start_recording.return_value = True
    failure = SttWorkerPythonFailure(
        backend="qwen",
        model_identifier=r"C:\Private\model.onnx",
        operation="transcribe_chunk",
        exception_type="RuntimeError",
        diagnostic_message="PRIVATE_DIAGNOSTIC_SENTINEL",
        diagnostic_traceback="PRIVATE_TRACEBACK_SENTINEL",
    )
    server._app.stt_engine.transcribe.side_effect = failure
    client = TestClient(server.fastapi, raise_server_exceptions=True)

    with patch(
        "whisperstream.main._load_audio_file",
        return_value=(np.zeros(1600, dtype=np.float32), 16_000),
    ):
        response = client.post(
            "/api/transcribe",
            files={"file": ("test.wav", b"content-free-test-bytes", "audio/wav")},
        )

    assert response.status_code == 503
    body = response.json()
    assert body == {"error": "stt_failure", "stt": failure.public_metadata()}
    serialized = response.text
    assert "PRIVATE_DIAGNOSTIC_SENTINEL" not in serialized
    assert "PRIVATE_TRACEBACK_SENTINEL" not in serialized
    server._app._broadcast_stt_failure.assert_called_once_with(failure)


# ---------------------------------------------------------------------------
# Task 1.3: Custom provider allow-lists
# ---------------------------------------------------------------------------


class TestCustomProviderAllowLists:
    """GET /api/enhancement/models?provider=custom is accepted (not 400)."""

    def test_custom_provider_models_returns_200_with_empty_bundled(self):
        """Custom remains in the allow-list without a curated catalog."""
        client, _ = _make_client()
        resp = client.get("/api/enhancement/models?provider=custom")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "custom"
        assert data["bundled"] == []


class TestKeyFingerprints:
    """GET /api/config exposes masked key fingerprints, never the full key."""

    def test_get_config_includes_masked_fingerprint_for_set_key(self):
        """A configured key surfaces as a first-4 + bullets + last-4 fingerprint;
        the full key and its middle are never sent to the client."""
        cfg = _make_mock_config(
            # Fake test key (not a real secret); annotated so the gitleaks
            # pre-commit hook does not flag the generic-api-key pattern.
            enhancement_gemini_api_key="AIzaSyTESTmiddleSECRET1234abcd3f9a",  # gitleaks:allow
        )
        client, _ = _make_client(config=cfg)
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        fp = data["enhancement_gemini_api_key_fingerprint"]
        assert fp == "AIza" + "•" * 10 + "3f9a"
        assert "middleSECRET" not in fp
        # The encrypted/plaintext full key is not what we display (the field that
        # carries the key value is separate from the fingerprint).
        assert "middleSECRET" not in str(data.get("enhancement_gemini_api_key", ""))

    def test_get_config_empty_fingerprint_when_no_key(self):
        """A provider with no stored key reports an empty fingerprint."""
        client, _ = _make_client()  # default mock: no keys set
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enhancement_openai_api_key_fingerprint"] == ""
        assert data["enhancement_custom_api_key_fingerprint"] == ""


class TestOutputGuardConfigEndpoint:
    """Rollout mode semantics at the replacement-style config PUT boundary."""

    def test_partial_put_preserves_current_output_guard_mode(self):
        from whisperstream.config_manager import WhisperstreamConfig

        cfg = WhisperstreamConfig(enhancement_output_guard_mode="log")
        client, server = _make_client(config=cfg)

        response = client.put(
            "/api/config",
            json={"enhancement_enabled": True},
        )

        assert response.status_code == 200
        assert response.json()["enhancement_output_guard_mode"] == "log"
        saved = server._app.config_manager.save.call_args.args[0]
        assert saved.enhancement_output_guard_mode == "log"

    def test_explicit_put_normalizes_output_guard_mode(self):
        from whisperstream.config_manager import WhisperstreamConfig

        client, server = _make_client(config=WhisperstreamConfig())
        response = client.put(
            "/api/config",
            json={"enhancement_output_guard_mode": "ENFORCE"},
        )

        assert response.status_code == 200
        assert response.json()["enhancement_output_guard_mode"] == "enforce"
        saved = server._app.config_manager.save.call_args.args[0]
        assert saved.enhancement_output_guard_mode == "enforce"

    def test_invalid_output_guard_mode_returns_400_without_saving(self):
        from whisperstream.config_manager import WhisperstreamConfig

        client, server = _make_client(config=WhisperstreamConfig())
        response = client.put(
            "/api/config",
            json={"enhancement_output_guard_mode": "sometimes"},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_config"
        assert "enhancement_output_guard_mode" in response.json()["detail"]
        server._app.config_manager.save.assert_not_called()


@pytest.mark.parametrize("verdict", ["viable", "marginal", "unviable"])
def test_auto_measures_only_stream_1_mini(tmp_path, verdict):
    from whisperstream.gpu_benchmark import BenchmarkResult
    helper = TestRunBenchmarkWorker()
    server, events = helper._server_with_broadcasts()
    requests = []
    benched = []

    def fetch(model_id, cancel_event):
        requests.append(model_id)
        return True, True

    async def bench(*, binary_path, gguf_path, device_index=None, on_tick=None, prompt_key=""):
        benched.append((Path(gguf_path).name, prompt_key))
        speed = {"viable": 100, "marginal": 40, "unviable": 20}[verdict]
        return BenchmarkResult(verdict=verdict, predicted_per_second=speed, predicted_n=200)

    patches = helper._auto_patches(server, tmp_path, helper._ONE_DEVICE, fetch, bench)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        server._benchmark.run()
    assert requests == ["stream-1-mini"]
    assert benched == [("stream-1-mini-vocabulary-q4_k_m.gguf", "stream-mini-vocabulary")]
    results = [e for e in events if e["type"] == "benchmark.result"]
    assert len(results) == 1
    assert results[0]["mode"] == "auto"
    assert results[0]["measured_model_id"] == "stream-1-mini"
    assert results[0]["verdict"] == verdict
    assert "model_results" not in results[0]
    assert server._app.config.enhancement_local_gpu_verdicts == {"stream-1-mini": verdict}


def test_auto_download_failure_never_benches_customizable(tmp_path):
    helper = TestRunBenchmarkWorker()
    server, events = helper._server_with_broadcasts()
    requests = []

    def fetch(model_id, cancel_event):
        requests.append(model_id)
        raise RuntimeError("source unavailable")

    bench = helper._bench_by_file({"gemma-4-E2B-it-Q4_K_M.gguf": ("viable", 90)})
    patches = helper._auto_patches(server, tmp_path, helper._ONE_DEVICE, fetch, bench)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        server._benchmark.run()
    assert requests == ["stream-1-mini"]
    result = next(e for e in events if e["type"] == "benchmark.result")
    assert result["verdict"] == "unviable"
    assert server._app.config.enhancement_local_gpu_verdicts.get("gemma4-e2b-instruct") is None


@pytest.mark.parametrize("model_id,filename", [
    ("stream-1-mini", "stream-1-mini-vocabulary-q4_k_m.gguf"),
    ("gemma4-e2b-instruct", "gemma-4-E2B-it-Q4_K_M.gguf"),
])
def test_single_model_benchmark_uses_restored_file_without_network(tmp_path, model_id, filename):
    helper = TestRunBenchmarkWorker()
    server, events = helper._server_with_broadcasts()
    (tmp_path / filename).write_bytes(b"restored model fixture")
    patches = helper._auto_patches(server, tmp_path, helper._ONE_DEVICE, None,
                                  helper._bench_by_file({filename: ("viable", 100)}))
    # Exercise the real download gate; only hardware inference is substituted.
    with patches[0], patches[1], patches[2], patches[4], patch(
        "whisperstream.model_downloader.new_http_client", side_effect=AssertionError("network")
    ):
        server._benchmark.run(mode="manual", model_id=model_id)
    result = next(e for e in events if e["type"] == "benchmark.result")
    assert result["verdicts"] == {model_id: "viable"}
    assert result["freshly_downloaded"] is False
    assert list(tmp_path.glob("*.gguf")) == [tmp_path / filename]


def test_model_selection_applies_its_own_server_authored_gpu():
    from whisperstream.config_manager import WhisperstreamConfig
    cfg = WhisperstreamConfig(
        enhancement_local_gpu_devices={"stream-1-mini": 2, "gemma4-e2b-instruct": 1},
        enhancement_local_gpu_device_names={"stream-1-mini": "GPU two", "gemma4-e2b-instruct": "GPU one"},
        enhancement_local_gpu_device_index=1,
        enhancement_local_gpu_device="GPU one",
    )
    client, server = _make_client(config=cfg)
    payload = cfg.to_dict()
    payload.update(enhancement_local_model="stream-1-mini", enhancement_local_gpu_devices={"stream-1-mini": 99})
    response = client.put("/api/config", json=payload)
    assert response.status_code == 200
    saved = server._app.config_manager.save.call_args.args[0]
    assert saved.enhancement_local_gpu_device_index == 2
    assert saved.enhancement_local_gpu_device == "GPU two"
