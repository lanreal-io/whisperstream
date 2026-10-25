"""Windows conformance tests for the Unit 02 model-lifecycle contract."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

from whisperstream.model_lifecycle import (
    PlatformModelLifecycleFactory,
    UnavailableModelLifecycleAdapter,
)
from whisperstream.model_lifecycle_contract import (
    ModelAssetRef,
    ModelFailure,
    ModelFamily,
    ModelLifecycleAdapter,
    ModelOperation,
    ModelOperationRequest,
    ModelState,
)
from whisperstream.windows_model_lifecycle import (
    WindowsModelLifecycleAdapter,
    WindowsModelOperationFailure,
)


def _request(
    generation: int,
    operation: ModelOperation = ModelOperation.ACQUIRE,
    *,
    asset_id: str = "fixture-model",
    family: ModelFamily = ModelFamily.STT,
) -> ModelOperationRequest:
    return ModelOperationRequest(
        generation=generation,
        operation=operation,
        asset=ModelAssetRef(asset_id, family),
    )


def test_platform_factory_selects_native_windows_and_darwin_owners(tmp_path: Path) -> None:
    windows = PlatformModelLifecycleFactory(system="Windows", machine="AMD64").create()
    with patch.dict("os.environ", {"CFFIXED_USER_HOME": str(tmp_path)}):
        darwin = PlatformModelLifecycleFactory(system="Darwin", machine="arm64").create()
    linux = PlatformModelLifecycleFactory(system="Linux", machine="x86_64").create()

    assert isinstance(windows, WindowsModelLifecycleAdapter)
    assert isinstance(windows, ModelLifecycleAdapter)
    from whisperstream.macos_model_lifecycle import MacosModelLifecycleAdapter

    assert isinstance(darwin, MacosModelLifecycleAdapter)
    assert isinstance(linux, UnavailableModelLifecycleAdapter)


def test_snapshot_reports_candidate_visibility_with_opaque_location(tmp_path: Path) -> None:
    artifact = tmp_path / "review-candidate.gguf"
    artifact.write_bytes(b"reviewed-public-fixture")
    entry = {
        "id": "candidate-review",
        "source": "candidate",
        "_artifact_path": artifact,
    }
    adapter = WindowsModelLifecycleAdapter()
    asset = ModelAssetRef("candidate-review", ModelFamily.ENHANCEMENT)

    with (
        patch("whisperstream.enhancement_models.get_local_model", return_value=entry),
        patch(
            "whisperstream.enhancement_models.resolve_local_model_path",
            return_value=artifact,
        ),
    ):
        snapshot = adapter.snapshot(asset)

    assert snapshot.state is ModelState.READY
    assert snapshot.verified is True
    assert snapshot.location_token is not None
    assert str(tmp_path) not in snapshot.location_token
    assert str(tmp_path) not in repr(snapshot)


def test_execute_serializes_progress_and_exact_generation_cancellation() -> None:
    adapter = WindowsModelLifecycleAdapter()
    entered = threading.Event()
    release = threading.Event()
    results = []
    progress = []

    def blocking(cancel_event, report):
        report(4, 10)
        entered.set()
        release.wait(timeout=5)
        if cancel_event.is_set():
            raise WindowsModelOperationFailure(ModelFailure.CANCELLED)
        return ModelState.READY

    thread = threading.Thread(
        target=lambda: results.append(
            adapter.execute_windows(_request(11), blocking, on_progress=progress.append)
        )
    )
    thread.start()
    assert entered.wait(timeout=2)

    busy = adapter.execute_windows(_request(12), blocking)
    wrong = adapter.cancel(12)
    cancelled = adapter.cancel(11)
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert busy.failure is ModelFailure.BUSY
    assert wrong.failure is ModelFailure.UNSUPPORTED
    assert cancelled.failure is ModelFailure.CANCELLED
    assert results[0].failure is ModelFailure.CANCELLED
    assert [(item.completed_bytes, item.total_bytes) for item in progress] == [(4, 10)]


def test_execute_maps_windows_failures_without_leaking_exception_text() -> None:
    adapter = WindowsModelLifecycleAdapter()
    private_marker = "private-owner-path-marker"

    def fail(_event, _report):
        raise PermissionError(private_marker)

    result = adapter.execute_windows(_request(21), fail)

    assert result.failure is ModelFailure.PERMISSION
    assert private_marker not in repr(result)


def test_registered_delete_is_idempotent_and_surfaces_real_failure() -> None:
    adapter = WindowsModelLifecycleAdapter()
    request = _request(
        31,
        ModelOperation.DELETE,
        asset_id="nemo-parakeet-tdt-0.6b-v3",
    )
    with patch("whisperstream.model_downloader.delete_model") as delete:
        result = adapter.execute(request)
    assert result.failure is ModelFailure.NONE
    delete.assert_called_once()

    with patch(
        "whisperstream.model_downloader.delete_model",
        side_effect=PermissionError("locked"),
    ):
        failed = adapter.execute(
            _request(
                32,
                ModelOperation.DELETE,
                asset_id="nemo-parakeet-tdt-0.6b-v3",
            )
        )
    assert failed.failure is ModelFailure.PERMISSION
