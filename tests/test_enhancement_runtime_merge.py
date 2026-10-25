"""Integration regressions across enhancement outcomes and native runtime owners."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.llm_enhancer import LLMEnhancer
from whisperstream.macos_local_llm_runtime import MacosManagedLlamaRuntime
from whisperstream.windows_local_llm_runtime import WindowsManagedLlamaRuntime


RAW = "a synthetic transcript with enough words to enhance"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_type", [None, MacosManagedLlamaRuntime, WindowsManagedLlamaRuntime])
@pytest.mark.parametrize("failure", ["http_timeout", "deadline", "http_error"])
@pytest.mark.parametrize("method", ["enhance_with_outcome", "enhance_chunk_with_outcome"])
async def test_timeout_outcome_crosses_real_adapter_boundary(
    runtime_type, failure, method, tmp_path, monkeypatch
):
    supervisor = SimpleNamespace(is_ready=True, is_running=True)
    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="local" if runtime_type else "openai",
        enhancement_api_key="synthetic-test-key",
        enhancement_local_model="gemma4-e2b-instruct",
        enhancement_timeout_seconds=0.01,
    )
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    if runtime_type is None:
        from whisperstream.byok_consent import record_consent
        from whisperstream.enhancement_security import consent_target
        target = consent_target(config)
        record_consent(target.provider, "Synthetic timeout test consent", target=target)
    runtime = (
        runtime_type(supervisor, catalog_id="gemma4-e2b-instruct", artifact_path=tmp_path / "model.gguf")
        if runtime_type else None
    )
    enhancer = LLMEnhancer(
        config, llama_server=supervisor, local_runtime_provider=lambda: runtime
    )
    cancelled = asyncio.Event()

    async def transport(*args, **kwargs):
        if failure == "http_timeout":
            raise httpx.ReadTimeout("synthetic timeout detail")
        if failure == "http_error":
            raise httpx.ConnectError("synthetic connection detail")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(enhancer, "_call_openai_compat", transport)
    on_timeout, on_error = Mock(), Mock()
    result = await getattr(enhancer, method)(
        RAW, on_timeout=on_timeout, on_error=on_error
    )

    assert result.text == RAW
    expected = "error" if failure == "http_error" else "timeout"
    assert result.outcome == expected
    assert on_timeout.call_count == (expected == "timeout")
    assert on_error.call_count == (expected == "error")
    if failure == "deadline":
        assert cancelled.is_set()
    if runtime is not None:
        assert not runtime._active
    assert not enhancer._active_provider_adapters


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_type", [MacosManagedLlamaRuntime, WindowsManagedLlamaRuntime])
@pytest.mark.parametrize("same_supervisor", [False, True])
async def test_shutdown_stops_current_alias_and_distinct_cached_runtime(
    runtime_type, same_supervisor, tmp_path
):
    from whisperstream.main import WhisperstreamApp
    from whisperstream.server import WhisperstreamServer

    old = SimpleNamespace(stop=AsyncMock())
    current = old if same_supervisor else SimpleNamespace(stop=AsyncMock())
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.llama_server = current
    app.local_llm_runtime = runtime_type(
        old, catalog_id="synthetic", artifact_path=tmp_path / "old.gguf"
    )
    app._local_llm_runtime_supervisor = old
    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._app = app
    server._logger = logging.getLogger(__name__)

    await server._maybe_stop_llama_server()

    old.stop.assert_awaited_once()
    current.stop.assert_awaited_once()
    assert app.llama_server is None
    assert app.local_llm_runtime is None


@pytest.mark.asyncio
async def test_failed_old_runtime_close_still_stops_replacement(tmp_path):
    from whisperstream.main import WhisperstreamApp
    from whisperstream.server import WhisperstreamServer

    old = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("synthetic stop failure")))
    current = SimpleNamespace(stop=AsyncMock())
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.llama_server = current
    app.local_llm_runtime = MacosManagedLlamaRuntime(
        old, catalog_id="synthetic", artifact_path=tmp_path / "old.gguf"
    )
    app._local_llm_runtime_supervisor = old
    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._app = app
    server._logger = logging.getLogger(__name__)

    await server._maybe_stop_llama_server()

    current.stop.assert_awaited_once()
    assert app.llama_server is None
