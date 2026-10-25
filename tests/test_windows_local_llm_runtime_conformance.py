"""Windows conformance for the Unit 06 local-runtime contract.

The fine-tune candidate half (registry, evaluator, owner exclusion) is
development tooling and lives with the private tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.llm_enhancer import LLMEnhancer
from whisperstream.local_llm_runtime import create_managed_runtime
from whisperstream.local_llm_runtime_contract import (
    LocalInferenceRequest,
    RuntimeFailure,
)
from whisperstream.windows_local_llm_runtime import (
    WindowsExternalOllamaRuntime,
    WindowsManagedLlamaRuntime,
)


class _Supervisor:
    def __init__(self, path: Path) -> None:
        self._gguf_path = path
        self.is_running = True
        self.is_ready = True
        self.stop_calls = 0
        self.restart_calls = 0

    async def start(self) -> None:
        self.is_running = True

    async def _warmup_inference(self) -> None:
        self.is_ready = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_running = False
        self.is_ready = False

    async def restart_for_model(self, path: Path) -> None:
        self.restart_calls += 1
        self._gguf_path = path


@pytest.mark.asyncio
async def test_managed_runtime_success_timeout_restart_and_exact_cleanup(tmp_path: Path) -> None:
    artifact = tmp_path / "public.gguf"
    artifact.write_bytes(b"gguf")
    supervisor = _Supervisor(artifact)
    runtime = WindowsManagedLlamaRuntime(
        supervisor, catalog_id="candidate", artifact_path=artifact
    )
    request = LocalInferenceRequest("local-1", 1, 1.0, "private raw")

    async def success(_request: LocalInferenceRequest) -> str:
        return "enhanced"

    result = await runtime.infer(request, success)
    assert result.enhanced_text == "enhanced"
    assert "private raw" not in repr(request)

    async def slow(_request: LocalInferenceRequest) -> str:
        await asyncio.sleep(1)
        return "late"

    timed_out = await runtime.infer(
        LocalInferenceRequest("local-2", 1, 0.01, "private raw"), slow
    )
    assert timed_out.failure is RuntimeFailure.INFERENCE_TIMEOUT
    await runtime.restart_for_model(artifact, catalog_id="candidate")
    stale = await runtime.infer(request, success)
    assert stale.failure is RuntimeFailure.STALE_GENERATION
    await runtime.close()
    await runtime.close()
    assert supervisor.stop_calls == 1
    assert runtime._active == {}


@pytest.mark.asyncio
async def test_ollama_adapter_never_owns_a_process() -> None:
    runtime = WindowsExternalOllamaRuntime()

    async def transport(_request: LocalInferenceRequest) -> str:
        return "ollama output"

    result = await runtime.infer(
        LocalInferenceRequest("ollama-1", 1, 1.0, "private"), transport
    )
    assert result.enhanced_text == "ollama output"
    snapshot = await runtime.stop(1)
    assert snapshot.ownership.value == "external"
    assert not hasattr(runtime, "_process")


def test_native_selector_creates_the_windows_managed_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    artifact = tmp_path / "public.gguf"
    artifact.write_bytes(b"gguf")
    runtime = create_managed_runtime(
        _Supervisor(artifact), catalog_id="candidate", artifact_path=artifact
    )
    assert isinstance(runtime, WindowsManagedLlamaRuntime)


@pytest.mark.asyncio
async def test_llm_enhancer_routes_local_call_through_runtime(tmp_path: Path) -> None:
    artifact = tmp_path / "public.gguf"
    artifact.write_bytes(b"gguf")
    supervisor = _Supervisor(artifact)
    runtime = WindowsManagedLlamaRuntime(
        supervisor, catalog_id="candidate", artifact_path=artifact
    )
    config = WhisperstreamConfig()
    config.enhancement_provider = "local"
    config.enhancement_enabled = True
    enhancer = LLMEnhancer(
        config,
        llama_server=supervisor,
        local_runtime_provider=lambda: runtime,
    )
    enhancer._call_openai_compat = AsyncMock(return_value="contract output")
    output = await enhancer._call_provider("private raw", "system")
    assert output == "contract output"
    enhancer._call_openai_compat.assert_awaited_once()
    assert runtime._active == {}
