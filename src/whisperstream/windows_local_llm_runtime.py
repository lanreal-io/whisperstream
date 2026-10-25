"""Windows adapters for the native-free local LLM runtime contract."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from .local_llm_runtime_contract import (
    LocalInferenceRequest,
    LocalInferenceResult,
    LocalModelRef,
    LocalRuntimeKind,
    RuntimeAcceleration,
    RuntimeFailure,
    RuntimeOwnership,
    RuntimeSnapshot,
    RuntimeStage,
    RuntimeStartRequest,
)


InferenceTransport = Callable[[LocalInferenceRequest], Awaitable[str]]


def _artifact_token(path: Path) -> str:
    """Return an opaque identity without retaining an owner-visible path."""

    resolved = str(path.resolve(strict=False)).encode("utf-8", errors="surrogatepass")
    return f"sha256:{hashlib.sha256(resolved).hexdigest()}"


class WindowsManagedLlamaRuntime:
    """Generation-bound ownership around the existing llama-server supervisor."""

    def __init__(self, supervisor: object, *, catalog_id: str, artifact_path: Path):
        self._supervisor = supervisor
        self._model = LocalModelRef(catalog_id, _artifact_token(artifact_path))
        self._generation = 1
        self._active: dict[str, asyncio.Task[str]] = {}
        self._closed = False

    @property
    def supervisor(self) -> object:
        return self._supervisor

    @property
    def current_generation(self) -> int:
        return self._generation

    def snapshot(self) -> RuntimeSnapshot:
        running = bool(getattr(self._supervisor, "is_running", False))
        ready = bool(getattr(self._supervisor, "is_ready", False))
        stage = RuntimeStage.READY if ready else RuntimeStage.LOADING if running else RuntimeStage.STOPPED
        return RuntimeSnapshot(
            self._generation,
            LocalRuntimeKind.MANAGED_LLAMA,
            RuntimeOwnership.APPLICATION,
            stage,
            healthy=ready,
        )

    async def start(self, request: RuntimeStartRequest) -> RuntimeSnapshot:
        if request.generation != self._generation:
            return self._failed(RuntimeFailure.STALE_GENERATION)
        if (
            request.kind is not LocalRuntimeKind.MANAGED_LLAMA
            or request.ownership is not RuntimeOwnership.APPLICATION
            or request.model != self._model
        ):
            return self._failed(RuntimeFailure.UNAVAILABLE)
        try:
            await asyncio.wait_for(
                self._supervisor.start(), timeout=request.start_timeout_seconds
            )
        except asyncio.TimeoutError:
            return self._failed(RuntimeFailure.START_TIMEOUT)
        except FileNotFoundError:
            return self._failed(RuntimeFailure.MODEL_MISSING)
        except Exception:
            return self._failed(RuntimeFailure.SPAWN_FAILED)
        return self.snapshot()

    async def warm(self, generation: int) -> RuntimeSnapshot:
        if generation != self._generation:
            return self._failed(RuntimeFailure.STALE_GENERATION)
        if bool(getattr(self._supervisor, "is_ready", False)):
            return self.snapshot()
        try:
            await self._supervisor._warmup_inference()
            if hasattr(self._supervisor, "_is_ready"):
                self._supervisor._is_ready = True
        except Exception:
            return self._failed(RuntimeFailure.HEALTH_FAILED)
        return self.snapshot()

    async def infer(
        self, request: LocalInferenceRequest, transport: InferenceTransport
    ) -> LocalInferenceResult:
        if request.generation != self._generation:
            return self._raw(request, RuntimeFailure.STALE_GENERATION)
        if self._closed or not bool(getattr(self._supervisor, "is_ready", False)):
            return self._raw(request, RuntimeFailure.UNAVAILABLE)
        if request.request_id in self._active:
            return self._raw(request, RuntimeFailure.INFERENCE_FAILED)

        task = asyncio.create_task(transport(request))
        self._active[request.request_id] = task
        try:
            enhanced = await asyncio.wait_for(
                asyncio.shield(task), timeout=request.timeout_seconds
            )
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return self._raw(request, RuntimeFailure.INFERENCE_TIMEOUT)
        except httpx.TimeoutException:
            return self._raw(request, RuntimeFailure.INFERENCE_TIMEOUT)
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return self._raw(request, RuntimeFailure.CANCELLED)
        except Exception:
            return self._raw(request, RuntimeFailure.INFERENCE_FAILED)
        finally:
            self._active.pop(request.request_id, None)

        if not isinstance(enhanced, str) or not enhanced.strip():
            return self._raw(request, RuntimeFailure.INVALID_OUTPUT)
        return LocalInferenceResult(
            request.request_id,
            request.generation,
            False,
            enhanced_text=enhanced.strip(),
        )

    async def cancel(
        self, request_id: str, generation: int
    ) -> LocalInferenceResult:
        request = LocalInferenceRequest(request_id, generation, 1.0, "")
        if generation != self._generation:
            return self._raw(request, RuntimeFailure.STALE_GENERATION)
        task = self._active.get(request_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return self._raw(request, RuntimeFailure.CANCELLED)

    async def stop(self, generation: int) -> RuntimeSnapshot:
        if generation != self._generation:
            return self._failed(RuntimeFailure.STALE_GENERATION)
        await self._cancel_all()
        try:
            await self._supervisor.stop()
        except Exception:
            return self._failed(RuntimeFailure.CLEANUP_FAILED)
        return self.snapshot()

    async def restart_for_model(self, gguf_path: Path, *, catalog_id: str) -> None:
        await self._cancel_all()
        await self._supervisor.restart_for_model(gguf_path)
        self._generation += 1
        self._model = LocalModelRef(catalog_id, _artifact_token(gguf_path))

    async def close(self) -> None:
        if self._closed:
            return
        await self._cancel_all()
        await self._supervisor.stop()
        self._closed = True

    async def _cancel_all(self) -> None:
        tasks = tuple(self._active.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._active.clear()

    def _failed(self, failure: RuntimeFailure) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            self._generation,
            LocalRuntimeKind.MANAGED_LLAMA,
            RuntimeOwnership.APPLICATION,
            RuntimeStage.FAILED,
            healthy=False,
            failure=failure,
        )

    @staticmethod
    def _raw(
        request: LocalInferenceRequest, failure: RuntimeFailure
    ) -> LocalInferenceResult:
        return LocalInferenceResult(
            request.request_id,
            request.generation,
            True,
            failure=failure,
        )


class WindowsExternalOllamaRuntime:
    """Externally owned endpoint adapter. It never starts or stops a process."""

    def __init__(self) -> None:
        self._generation = 1
        self._active: dict[str, asyncio.Task[str]] = {}

    @property
    def current_generation(self) -> int:
        return self._generation

    async def start(self, request: RuntimeStartRequest) -> RuntimeSnapshot:
        if (
            request.generation != self._generation
            or request.kind is not LocalRuntimeKind.EXTERNAL_OLLAMA
            or request.ownership is not RuntimeOwnership.EXTERNAL
        ):
            return RuntimeSnapshot(
                self._generation,
                LocalRuntimeKind.EXTERNAL_OLLAMA,
                RuntimeOwnership.EXTERNAL,
                RuntimeStage.FAILED,
                False,
                RuntimeFailure.STALE_GENERATION,
            )
        return RuntimeSnapshot(
            self._generation,
            LocalRuntimeKind.EXTERNAL_OLLAMA,
            RuntimeOwnership.EXTERNAL,
            RuntimeStage.READY,
            True,
        )

    async def warm(self, generation: int) -> RuntimeSnapshot:
        if generation != self._generation:
            return RuntimeSnapshot(
                self._generation,
                LocalRuntimeKind.EXTERNAL_OLLAMA,
                RuntimeOwnership.EXTERNAL,
                RuntimeStage.FAILED,
                False,
                RuntimeFailure.STALE_GENERATION,
            )
        return RuntimeSnapshot(
            generation,
            LocalRuntimeKind.EXTERNAL_OLLAMA,
            RuntimeOwnership.EXTERNAL,
            RuntimeStage.READY,
            True,
        )

    async def infer(
        self, request: LocalInferenceRequest, transport: InferenceTransport
    ) -> LocalInferenceResult:
        if request.generation != self._generation:
            return LocalInferenceResult(
                request.request_id,
                request.generation,
                True,
                failure=RuntimeFailure.STALE_GENERATION,
            )
        task = asyncio.create_task(transport(request))
        self._active[request.request_id] = task
        try:
            output = await asyncio.wait_for(task, timeout=request.timeout_seconds)
        except asyncio.TimeoutError:
            return LocalInferenceResult(
                request.request_id,
                request.generation,
                True,
                failure=RuntimeFailure.INFERENCE_TIMEOUT,
            )
        except asyncio.CancelledError:
            return LocalInferenceResult(
                request.request_id,
                request.generation,
                True,
                failure=RuntimeFailure.CANCELLED,
            )
        except Exception:
            return LocalInferenceResult(
                request.request_id,
                request.generation,
                True,
                failure=RuntimeFailure.INFERENCE_FAILED,
            )
        finally:
            self._active.pop(request.request_id, None)
        if not isinstance(output, str) or not output.strip():
            return LocalInferenceResult(
                request.request_id,
                request.generation,
                True,
                failure=RuntimeFailure.INVALID_OUTPUT,
            )
        return LocalInferenceResult(
            request.request_id,
            request.generation,
            False,
            enhanced_text=output.strip(),
        )

    async def cancel(self, request_id: str, generation: int) -> LocalInferenceResult:
        task = self._active.get(request_id)
        if task is not None and not task.done() and generation == self._generation:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return LocalInferenceResult(
            request_id,
            generation,
            True,
            failure=(
                RuntimeFailure.CANCELLED
                if generation == self._generation
                else RuntimeFailure.STALE_GENERATION
            ),
        )

    async def stop(self, generation: int) -> RuntimeSnapshot:
        if generation != self._generation:
            return RuntimeSnapshot(
                self._generation,
                LocalRuntimeKind.EXTERNAL_OLLAMA,
                RuntimeOwnership.EXTERNAL,
                RuntimeStage.FAILED,
                False,
                RuntimeFailure.STALE_GENERATION,
            )
        await self._cancel_all()
        return RuntimeSnapshot(
            generation,
            LocalRuntimeKind.EXTERNAL_OLLAMA,
            RuntimeOwnership.EXTERNAL,
            RuntimeStage.STOPPED,
            False,
        )

    async def close(self) -> None:
        await self._cancel_all()

    async def _cancel_all(self) -> None:
        for task in tuple(self._active.values()):
            if not task.done():
                task.cancel()
        for task in tuple(self._active.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._active.clear()


__all__ = ["WindowsExternalOllamaRuntime", "WindowsManagedLlamaRuntime"]
