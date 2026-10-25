"""Registered GGUF acquisition shared by downloads and benchmarks."""
from __future__ import annotations

import threading
from collections.abc import Callable
from ..model_lifecycle_contract import (
    ModelAssetRef, ModelFamily, ModelOperation, ModelOperationRequest,
    ModelState, ModelFailure,
)


class RegisteredGGUFDownload:
    """Acquire an exact catalog artifact without taking activation authority."""

    def __init__(
        self, *, next_generation: Callable, execute: Callable,
        broadcast: Callable,
    ) -> None:
        self._next_generation = next_generation
        self._execute = execute
        self.broadcast = broadcast

    def fetch(
        self, model_id: str, cancel_event: threading.Event
    ) -> tuple[bool, bool]:
        """Download ONE GGUF, streaming ``download.progress kind=local_llm``.

        The LOW-LEVEL download leg shared by direct model downloads, the MANUAL
        single-model benchmark, and the AUTO ladder. It ONLY: resolves
        ``get_local_llm_dir()/gguf_filename``, captures whether the file
        already existed, calls ``setup.download_llm_model`` streaming raw
        ``(downloaded, total)`` byte progress over the WebSocket, and returns.

        It does NOT write config, refresh the supervisor, or broadcast any
        ``download.complete`` / ``download.cancelled`` / ``download.error``
        terminal event -- the CALLER owns those so the paths can differ. A
        direct model download writes ``enhancement_local_model`` and refreshes
        the supervisor; benchmark downloads remain side-effect free until the
        frontend applies a successful explicit flow.

        ``DownloadCancelled`` propagates to the caller. The shared
        ``_download_cancel_event`` / ``_download_thread`` wiring and terminal
        event policy are the caller's responsibility too.

        Returns
        -------
        tuple[bool, bool]
            ``(ok, existed_before)`` -- ``ok`` is True when the GGUF is present
            and non-empty after the call; ``existed_before`` is True when the
            file was already present (non-empty) BEFORE the download started,
            so the caller can distinguish a freshly-downloaded model from a
            reused one.
        """
        from ..enhancement_models import (
            get_local_bundled_model,
            resolve_local_model_path,
        )
        from ..setup import DownloadCancelled, download_llm_model

        catalog_entry = get_local_bundled_model(model_id)
        if catalog_entry is None:
            raise ValueError(f"unknown model_id {model_id!r}")

        gguf_path = resolve_local_model_path(catalog_entry)
        assert gguf_path is not None
        existed_before = gguf_path.exists() and gguf_path.stat().st_size > 0

        request = ModelOperationRequest(
            generation=self._next_generation(),
            operation=ModelOperation.ACQUIRE,
            asset=ModelAssetRef(model_id, ModelFamily.ENHANCEMENT),
        )

        def on_progress(progress) -> None:
            self.broadcast({
                "type": "download.progress",
                "kind": "local_llm",
                "model_id": model_id,
                "downloaded": progress.completed_bytes,
                "total": progress.total_bytes or 0,
                "speed_bps": 0,
            })

        operation_errors: list[Exception] = []

        def acquire_gguf(_event, report) -> ModelState:
            try:
                download_llm_model(
                    model_id,
                    gguf_path,
                    progress_callback=report,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                operation_errors.append(exc)
                raise
            return ModelState.READY

        result = self._execute(
            request,
            acquire_gguf,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        if result.failure is ModelFailure.CANCELLED:
            raise DownloadCancelled()
        if result.failure is not ModelFailure.NONE:
            if operation_errors:
                raise operation_errors[0]
            raise RuntimeError(result.failure.value)

        gguf_path = resolve_local_model_path(catalog_entry)
        assert gguf_path is not None
        ok = gguf_path.exists() and gguf_path.stat().st_size > 0
        return ok, existed_before
