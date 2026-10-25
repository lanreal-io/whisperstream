"""Windows storage owner for the native-free model lifecycle contract."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from pathlib import Path

from .model_lifecycle_contract import (
    ModelAssetRef,
    ModelFailure,
    ModelLifecycleAdapter,
    ModelOperation,
    ModelOperationRequest,
    ModelOperationResult,
    ModelProgress,
    ModelSnapshot,
    ModelState,
    ProgressCallback,
)


WindowsOperation = Callable[[threading.Event, Callable[[int, int | None], None]], ModelState | None]


class WindowsModelOperationFailure(RuntimeError):
    """Typed, content-free failure raised by a Windows operation bridge."""

    def __init__(self, failure: ModelFailure) -> None:
        if failure is ModelFailure.NONE:
            raise ValueError("operation failure must not be NONE")
        self.failure = failure
        super().__init__(failure.value)


class WindowsModelLifecycleAdapter:
    """Serialize Windows model mutations and hide native storage paths.

    Existing download and relocation owners are invoked through
    :meth:`execute_windows`.  The shared request carries only an asset ID and
    opaque destination token; the Windows closure retains paths, handles, and
    volume identities.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_generation: int | None = None
        self._active_request: ModelOperationRequest | None = None
        self._cancel_event: threading.Event | None = None

    @property
    def active_generation(self) -> int | None:
        with self._lock:
            return self._active_generation

    @staticmethod
    def _location_token(asset: ModelAssetRef) -> str:
        digest = hashlib.sha256(
            f"{asset.family.value}:{asset.asset_id}".encode("utf-8")
        ).hexdigest()[:16]
        return f"windows-model-{digest}"

    @staticmethod
    def _nonempty_file(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def snapshot(self, asset: ModelAssetRef) -> ModelSnapshot:
        """Resolve registered STT/GGUF visibility without returning a path."""

        state = ModelState.UNAVAILABLE
        verified = False
        try:
            if asset.family.value == "stt":
                from .model_downloader import ModelId, is_present, model_dir

                model = ModelId(asset.asset_id)
                verified = is_present(model)
                if verified:
                    state = ModelState.READY
                else:
                    target = model_dir(model)
                    state = ModelState.PARTIAL if target.exists() else ModelState.ABSENT
            elif asset.family.value == "enhancement":
                from .enhancement_models import get_local_model, resolve_local_model_path

                entry = get_local_model(asset.asset_id)
                if entry is not None:
                    path = resolve_local_model_path(entry)
                    verified = self._nonempty_file(path)
                    if verified:
                        state = ModelState.READY
                    elif path is not None and path.exists():
                        state = ModelState.PARTIAL
                    else:
                        state = ModelState.ABSENT
            elif asset.family.value == "speech":
                from .model_downloader import ModelId, is_present, model_dir

                model = ModelId(asset.asset_id)
                verified = is_present(model)
                if verified:
                    state = ModelState.READY
                else:
                    target = model_dir(model)
                    state = ModelState.PARTIAL if target.exists() else ModelState.ABSENT
        except (KeyError, OSError, ValueError):
            state = ModelState.UNAVAILABLE
            verified = False

        with self._lock:
            active = self._active_generation
            active_request = self._active_request
        if active_request is not None and active_request.asset == asset:
            return ModelSnapshot(
                asset=asset,
                state=ModelState.BUSY,
                operation_generation=active,
            )
        return ModelSnapshot(
            asset=asset,
            state=state,
            location_token=(self._location_token(asset) if state is ModelState.READY else None),
            verified=verified,
        )

    def execute(
        self,
        request: ModelOperationRequest,
        on_progress: ProgressCallback | None = None,
    ) -> ModelOperationResult:
        """Execute storage-local verify/delete operations.

        Acquisition and relocation require their canonical Windows owner and
        therefore enter through :meth:`execute_windows` with a closure.
        """

        if request.operation is ModelOperation.VERIFY:
            snapshot = self.snapshot(request.asset)
            failure = (
                ModelFailure.UNSUPPORTED
                if snapshot.state is ModelState.UNAVAILABLE
                else ModelFailure.NONE
            )
            return ModelOperationResult(
                generation=request.generation,
                operation=request.operation,
                asset=request.asset,
                state=snapshot.state,
                failure=failure,
                changed=False,
            )
        if request.operation is ModelOperation.DELETE:
            return self.execute_windows(
                request,
                self._delete_registered_asset,
                on_progress=on_progress,
            )
        return ModelOperationResult(
            generation=request.generation,
            operation=request.operation,
            asset=request.asset,
            state=ModelState.UNAVAILABLE,
            failure=ModelFailure.UNSUPPORTED,
            changed=False,
        )

    def _delete_registered_asset(
        self,
        _cancel_event: threading.Event,
        _progress: Callable[[int, int | None], None],
    ) -> ModelState:
        assert self._active_request is not None
        asset = self._active_request.asset
        if asset.family.value in {"stt", "speech"}:
            from .model_downloader import ModelId, delete_model

            delete_model(ModelId(asset.asset_id))
            return ModelState.ABSENT
        if asset.family.value == "enhancement":
            from .enhancement_models import get_local_model, resolve_local_model_path

            entry = get_local_model(asset.asset_id)
            if entry is None or entry.get("source") == "candidate":
                raise WindowsModelOperationFailure(ModelFailure.UNSUPPORTED)
            path = resolve_local_model_path(entry)
            if path is not None:
                path.unlink(missing_ok=True)
                path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
            return ModelState.ABSENT
        raise WindowsModelOperationFailure(ModelFailure.UNSUPPORTED)

    def execute_windows(
        self,
        request: ModelOperationRequest,
        operation: WindowsOperation,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ModelOperationResult:
        """Run one canonical Windows operation under the shared generation."""

        event = cancel_event or threading.Event()
        with self._lock:
            if self._active_generation is not None:
                return ModelOperationResult(
                    generation=request.generation,
                    operation=request.operation,
                    asset=request.asset,
                    state=ModelState.BUSY,
                    failure=ModelFailure.BUSY,
                    changed=False,
                )
            self._active_generation = request.generation
            self._active_request = request
            self._cancel_event = event

        def report(completed: int, total: int | None) -> None:
            if on_progress is not None:
                on_progress(
                    ModelProgress(
                        generation=request.generation,
                        asset=request.asset,
                        completed_bytes=completed,
                        total_bytes=total,
                    )
                )

        try:
            if event.is_set():
                raise WindowsModelOperationFailure(ModelFailure.CANCELLED)
            state = operation(event, report) or ModelState.READY
            return ModelOperationResult(
                generation=request.generation,
                operation=request.operation,
                asset=request.asset,
                state=state,
                changed=True,
            )
        except WindowsModelOperationFailure as exc:
            return ModelOperationResult(
                generation=request.generation,
                operation=request.operation,
                asset=request.asset,
                state=ModelState.ABSENT,
                failure=exc.failure,
                changed=False,
            )
        except PermissionError:
            failure = ModelFailure.PERMISSION
        except OSError:
            failure = ModelFailure.IO
        except Exception as exc:
            failure = (
                ModelFailure.CANCELLED
                if type(exc).__name__ == "DownloadCancelled"
                else ModelFailure.SOURCE_UNAVAILABLE
            )
        finally:
            with self._lock:
                self._active_generation = None
                self._active_request = None
                self._cancel_event = None

        return ModelOperationResult(
            generation=request.generation,
            operation=request.operation,
            asset=request.asset,
            state=ModelState.ABSENT,
            failure=failure,
            changed=False,
        )

    def cancel(self, generation: int) -> ModelOperationResult:
        with self._lock:
            request = self._active_request
            event = self._cancel_event
            if request is None or self._active_generation != generation or event is None:
                return ModelOperationResult(
                    generation=generation,
                    operation=ModelOperation.CANCEL,
                    asset=ModelAssetRef("inactive-generation", self._inactive_family()),
                    state=ModelState.UNAVAILABLE,
                    failure=ModelFailure.UNSUPPORTED,
                    changed=False,
                )
            event.set()
            asset = request.asset
        return ModelOperationResult(
            generation=generation,
            operation=ModelOperation.CANCEL,
            asset=asset,
            state=ModelState.BUSY,
            failure=ModelFailure.CANCELLED,
            changed=True,
        )

    @staticmethod
    def _inactive_family():
        from .model_lifecycle_contract import ModelFamily

        return ModelFamily.STT


assert isinstance(WindowsModelLifecycleAdapter(), ModelLifecycleAdapter)
