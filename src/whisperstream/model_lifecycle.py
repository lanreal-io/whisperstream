"""Platform selector for the shared model lifecycle contract."""

from __future__ import annotations

import platform

from .model_lifecycle_contract import (
    ModelAssetRef,
    ModelFailure,
    ModelLifecycleAdapter,
    ModelLifecycleFactory,
    ModelOperation,
    ModelOperationRequest,
    ModelOperationResult,
    ModelSnapshot,
    ModelState,
    ProgressCallback,
)


class UnavailableModelLifecycleAdapter:
    """Report capability absence without importing a native storage owner."""

    def snapshot(self, asset: ModelAssetRef) -> ModelSnapshot:
        return ModelSnapshot(asset=asset, state=ModelState.UNAVAILABLE)

    def execute(
        self,
        request: ModelOperationRequest,
        on_progress: ProgressCallback | None = None,
    ) -> ModelOperationResult:
        del on_progress
        return ModelOperationResult(
            generation=request.generation,
            operation=request.operation,
            asset=request.asset,
            state=ModelState.UNAVAILABLE,
            failure=ModelFailure.UNSUPPORTED,
            changed=False,
        )

    def cancel(self, generation: int) -> ModelOperationResult:
        from .model_lifecycle_contract import ModelFamily

        return ModelOperationResult(
            generation=generation,
            operation=ModelOperation.CANCEL,
            asset=ModelAssetRef("unsupported-platform", ModelFamily.STT),
            state=ModelState.UNAVAILABLE,
            failure=ModelFailure.UNSUPPORTED,
            changed=False,
        )


class PlatformModelLifecycleFactory:
    def __init__(self, *, system: str | None = None, machine: str | None = None) -> None:
        self._system = system
        self._machine = machine

    def create(self) -> ModelLifecycleAdapter:
        system = self._system or platform.system()
        machine = (self._machine or platform.machine()).lower()
        if system == "Windows" and machine in {"amd64", "x86_64"}:
            from .windows_model_lifecycle import WindowsModelLifecycleAdapter

            return WindowsModelLifecycleAdapter()
        if system == "Darwin" and machine in {"arm64", "aarch64"}:
            from .macos_model_lifecycle import MacosModelLifecycleAdapter

            return MacosModelLifecycleAdapter()
        return UnavailableModelLifecycleAdapter()


assert isinstance(PlatformModelLifecycleFactory(), ModelLifecycleFactory)
