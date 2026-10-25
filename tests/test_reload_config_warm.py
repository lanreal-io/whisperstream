"""Tests for the auto-warm diff logic in WhisperstreamApp.reload_config().

Task 2.2: Verify that kick_warm is called on exactly the two qualifying
config transitions and NOT on any other transition.

Qualifying transitions:
  1. User toggles enhancement ON while provider is already "local".
  2. User switches provider from cloud -> "local" while enhancement is enabled.

Non-qualifying (deferred per scope.md):
  - Enable ON with cloud provider.
  - Local model change only (enhancement_local_model changes, stays local+enabled).
  - Disable (enabled True -> False).
  - Any transition when _can_warm_local() guards fail.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from whisperstream.config_manager import WhisperstreamConfig

pytestmark = pytest.mark.usefixtures("accepted_eula")


# ---------------------------------------------------------------------------
# Fixture: minimal patched WhisperstreamApp
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    """Construct a WhisperstreamApp with all hardware/model touchpoints mocked."""
    with (
        patch("whisperstream.main.make_stt_manager"),
        patch("whisperstream.main.KeyboardShortcutManager"),
        patch("whisperstream.main.AudioCapture"),
        patch("whisperstream.main.AudioFeedback"),
        patch("whisperstream.main.LicenseManager"),
        patch("whisperstream.main.TextInjector"),
        patch("whisperstream.speech.controller.SpeechController"),
        patch("whisperstream.main.model_dir", return_value=tmp_path),
        patch(
            "whisperstream.main.ConfigManager.load",
            return_value=WhisperstreamConfig(),
        ),
    ):
        from whisperstream.main import WhisperstreamApp

        instance = WhisperstreamApp(headless=True)
        yield instance


# ---------------------------------------------------------------------------
# Helper: configure the app so _can_warm_local() returns True
# ---------------------------------------------------------------------------

def _make_app_warmable(app, monkeypatch, tmp_path):
    """Set up app + mocks so _can_warm_local() returns True for a local cfg."""
    from whisperstream.enhancement_models import get_local_bundled_model

    # Use a known model id that exists in the catalog.
    model_id = "gemma4-e2b-instruct"
    entry = get_local_bundled_model(model_id)
    assert entry is not None, "test model id not found in LOCAL_BUNDLED_MODELS"

    # Write a non-empty fake GGUF in tmp_path.
    (tmp_path / entry["gguf_filename"]).write_bytes(b"\x00" * 16)

    # Patch get_local_llm_dir so the inline import picks up tmp_path.
    monkeypatch.setattr(
        "whisperstream.config_manager.get_local_llm_dir",
        lambda: tmp_path,
    )

    # Install a MagicMock supervisor and a sentinel loop.
    mock_server = MagicMock()
    sentinel_loop = MagicMock()
    app.llama_server = mock_server
    app._server_loop = sentinel_loop

    return mock_server, model_id


# ---------------------------------------------------------------------------
# Tests: qualifying transitions that MUST kick_warm
# ---------------------------------------------------------------------------

def test_enable_on_with_local_provider_kicks_warm(app, monkeypatch, tmp_path):
    """Transition 1: enhancement goes False -> True while provider == 'local'."""
    mock_server, model_id = _make_app_warmable(app, monkeypatch, tmp_path)

    # Start with local provider, enhancement OFF.
    app.config = replace(
        app.config,
        enhancement_enabled=False,
        enhancement_provider="local",
        enhancement_local_model=model_id,
        enhancement_local_endpoint="",
    )

    new_config = replace(app.config, enhancement_enabled=True)
    app.reload_config(new_config)

    mock_server.kick_warm.assert_called_once()


def test_switch_cloud_to_local_while_enabled_kicks_warm(app, monkeypatch, tmp_path):
    """Transition 2: provider switches cloud -> 'local' while enhancement is enabled."""
    mock_server, model_id = _make_app_warmable(app, monkeypatch, tmp_path)

    # Start with cloud provider, enhancement ON.
    app.config = replace(
        app.config,
        enhancement_enabled=True,
        enhancement_provider="openai",
        enhancement_local_model=model_id,
        enhancement_local_endpoint="",
    )

    new_config = replace(app.config, enhancement_provider="local")
    app.reload_config(new_config)

    mock_server.kick_warm.assert_called_once()


# ---------------------------------------------------------------------------
# Test: combined transition must kick ONCE (not twice — branches are OR-ed)
# ---------------------------------------------------------------------------

def test_combined_switch_and_enable_kicks_once(app, monkeypatch, tmp_path):
    """Both transitions land in one reload_config call -> kick_warm called exactly once."""
    mock_server, model_id = _make_app_warmable(app, monkeypatch, tmp_path)

    # Start with cloud provider, enhancement OFF.
    app.config = replace(
        app.config,
        enhancement_enabled=False,
        enhancement_provider="openai",
        enhancement_local_model=model_id,
        enhancement_local_endpoint="",
    )

    # Single save flips both.
    new_config = replace(app.config, enhancement_enabled=True, enhancement_provider="local")
    app.reload_config(new_config)

    assert mock_server.kick_warm.call_count == 1


def test_manual_gemma_benchmark_then_config_put_creates_and_warms_supervisor(
    app, monkeypatch, tmp_path
):
    """Cloud/disabled startup -> manual bench -> Local PUT prepares before warm."""
    from whisperstream.gpu_benchmark import BenchmarkResult
    from whisperstream.server import WhisperstreamServer

    model_id = "gemma4-e2b-instruct"
    from whisperstream.enhancement_models import get_local_bundled_model

    entry = get_local_bundled_model(model_id)
    assert entry is not None
    gguf_path = tmp_path / entry["gguf_filename"]
    gguf_path.write_bytes(b"benchmark-download")
    monkeypatch.setattr(
        "whisperstream.config_manager.get_local_llm_dir", lambda: tmp_path
    )

    app.config = replace(
        app.config,
        enhancement_enabled=False,
        enhancement_provider="gemini",
        enhancement_local_model=model_id,
        enhancement_local_endpoint="",
        enhancement_local_gpu_verdicts={},
    )
    app.config_manager.save = MagicMock()
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    broadcasts: list[dict] = []
    server.broadcast = lambda event: broadcasts.append(event)  # type: ignore[method-assign]

    owned_loop = asyncio.new_event_loop()
    server._loop = owned_loop
    app.set_server_loop(owned_loop)
    supervisor = MagicMock()
    supervisor._gguf_path = gguf_path
    supervisor.is_ready = False
    supervisor.is_running = False
    supervisor.port = 0

    async def _bench(**_kwargs):
        return BenchmarkResult(
            verdict="viable", predicted_per_second=204.6, predicted_n=200
        )

    device_output = (
        "Available devices:\n"
        "  Vulkan0: NVIDIA GeForce RTX 4070 Ti SUPER "
        "(16063 MiB, 15295 MiB free)\n"
    )
    client = TestClient(server.fastapi, raise_server_exceptions=True)
    try:
        with (
            patch(
                "whisperstream.llama_server.find_bundled_llama_server",
                return_value=tmp_path / "llama-server.exe",
            ),
            patch(
                "whisperstream.gpu_benchmark.list_devices_stdout",
                return_value=device_output,
            ),
            patch(
                "whisperstream.gpu_benchmark.run_benchmark",
                side_effect=_bench,
            ),
            patch.object(
                server._gguf_download,
                "fetch",
                return_value=(True, False),
            ),
            patch(
                "whisperstream.llama_server.LlamaServerSupervisor",
                return_value=supervisor,
            ) as supervisor_cls,
        ):
            server._benchmark.run(mode="manual", model_id=model_id)

            # The benchmark may cache its verdict, but must not select Local or
            # create/warm a serving supervisor before the explicit config PUT.
            assert app.config.enhancement_provider == "gemini"
            assert app.config.enhancement_enabled is False
            assert app.llama_server is None
            supervisor_cls.assert_not_called()

            body = app.config.to_dict()
            body.update(
                enhancement_enabled=True,
                enhancement_provider="local",
                enhancement_local_model=model_id,
            )
            response = client.put("/api/config", json=body)
            assert response.status_code == 200

            # The requester is deliberately fire-and-forget on the server
            # loop. Drain it to observe prepare -> kick ordering.
            owned_loop.run_until_complete(asyncio.sleep(0))

        assert app.llama_server is supervisor
        assert supervisor_cls.call_args.args[0] == gguf_path
        assert supervisor_cls.call_args.kwargs["on_state_change"] is server.broadcast
        supervisor.kick_warm.assert_called_once_with(owned_loop)
        assert client.get("/api/llama/status").json() == {
            "state": "dormant",
            "model": gguf_path.name,
            "port": None,
        }
        assert any(
            event.get("type") == "benchmark.result"
            and event.get("measured_model_id") == model_id
            for event in broadcasts
        )
    finally:
        client.close()
        app.set_server_loop(None)
        server._loop = None
        owned_loop.close()


# ---------------------------------------------------------------------------
# Tests: non-qualifying transitions that must NOT kick_warm
# ---------------------------------------------------------------------------

def test_enable_on_with_cloud_provider_does_not_kick(app, monkeypatch, tmp_path):
    """Enable goes False -> True but provider is 'openai' -> no kick_warm."""
    mock_server, model_id = _make_app_warmable(app, monkeypatch, tmp_path)

    app.config = replace(
        app.config,
        enhancement_enabled=False,
        enhancement_provider="openai",
        enhancement_local_model=model_id,
        enhancement_local_endpoint="",
    )

    new_config = replace(app.config, enhancement_enabled=True)
    app.reload_config(new_config)

    mock_server.kick_warm.assert_not_called()


def test_local_model_change_does_not_kick(app, monkeypatch, tmp_path):
    """Provider stays 'local', enabled stays True, only local model changes -> no kick.

    This is the auto-warm-on-local-model-switch path explicitly deferred in scope.md.
    """
    mock_server, model_id = _make_app_warmable(app, monkeypatch, tmp_path)

    app.config = replace(
        app.config,
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_local_model=model_id,
        enhancement_local_endpoint="",
    )

    # Change only the local model name (no enable/provider transition).
    new_config = replace(app.config, enhancement_local_model="some-other-model")
    app.reload_config(new_config)

    mock_server.kick_warm.assert_not_called()


def test_disable_queues_stop_without_kick(app, monkeypatch, tmp_path):
    """Disable requests prompt reconciliation on the owning loop."""
    mock_server, model_id = _make_app_warmable(app, monkeypatch, tmp_path)

    app.config = replace(
        app.config,
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_local_model=model_id,
        enhancement_local_endpoint="",
    )

    new_config = replace(app.config, enhancement_enabled=False)
    app.reload_config(new_config)

    mock_server.kick_warm.assert_not_called()
    app._server_loop.call_soon_threadsafe.assert_called_once()


def test_idle_timeout_propagates_to_pool_without_mutating_other_config(
    app, monkeypatch, tmp_path
):
    mock_server, model_id = _make_app_warmable(app, monkeypatch, tmp_path)
    pool = MagicMock()
    app.resident_llama_pool = pool
    app.config = replace(
        app.config,
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_local_model=model_id,
        enhancement_local_idle_timeout=600,
    )
    new_config = replace(app.config, enhancement_local_idle_timeout=42)

    app.reload_config(new_config)

    mock_server.set_idle_timeout.assert_called_once_with(42)
    pool.set_idle_timeout.assert_called_once_with(42)
    assert app.config is new_config
    assert app.config.enhancement_local_model == model_id
    assert app.config.enhancement_enabled is True


def test_llm_enhancer_unwraps_explicit_resident_binding(app):
    from whisperstream.resident_llama_pool import ResidentLlama

    supervisor = object()
    resident = MagicMock(spec=ResidentLlama)
    resident.supervisor = supervisor
    app.llm_enhancer._llama_server_provider = lambda: resident

    assert app.llm_enhancer._resolve_llama_server() is supervisor


def test_no_kick_when_can_warm_local_false(app, monkeypatch, tmp_path):
    """Qualifying transition occurs but _can_warm_local returns False (GGUF missing) -> no kick."""
    from whisperstream.enhancement_models import get_local_bundled_model

    model_id = "gemma4-e2b-instruct"
    entry = get_local_bundled_model(model_id)
    assert entry is not None

    # Patch get_local_llm_dir to tmp_path but do NOT write the GGUF file,
    # so _can_warm_local returns False (gguf_not_installed branch).
    monkeypatch.setattr(
        "whisperstream.config_manager.get_local_llm_dir",
        lambda: tmp_path,
    )

    mock_server = MagicMock()
    app.llama_server = mock_server
    app._server_loop = object()

    app.config = replace(
        app.config,
        enhancement_enabled=False,
        enhancement_provider="local",
        enhancement_local_model=model_id,
        enhancement_local_endpoint="",
    )

    # Qualifying transition: enable goes True.
    new_config = replace(app.config, enhancement_enabled=True)
    app.reload_config(new_config)

    # _can_warm_local is False because GGUF is absent -> no kick.
    mock_server.kick_warm.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"enhancement_enabled": False},
    {"enhancement_provider": "openai"},
    {"enhancement_local_endpoint": "http://127.0.0.1:9000/v1"},
])
async def test_deactivation_stops_promptly_on_owning_loop(app, changes):
    from unittest.mock import AsyncMock
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    app.llama_server = supervisor
    app.set_server_loop(asyncio.get_running_loop())
    app.config = replace(app.config, enhancement_enabled=True,
                         enhancement_provider="local", enhancement_local_endpoint="")
    app.reload_config(replace(app.config, **changes))
    await asyncio.sleep(0)
    await app._local_stop_task
    supervisor.stop.assert_awaited_once()
    supervisor.kick_warm.assert_not_called()


@pytest.mark.asyncio
async def test_disable_cancels_real_pending_warmup(app, tmp_path):
    from whisperstream.llama_server import LlamaServerSupervisor
    from unittest.mock import AsyncMock
    supervisor = LlamaServerSupervisor(tmp_path / "model", binary_path=tmp_path / "binary")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def start():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    supervisor.start = start
    supervisor._warmup_inference = AsyncMock()
    app.llama_server = supervisor
    app.set_server_loop(asyncio.get_running_loop())
    app.config = replace(app.config, enhancement_enabled=True, enhancement_provider="local")
    supervisor.kick_warm(asyncio.get_running_loop())
    await started.wait()
    app.reload_config(replace(app.config, enhancement_enabled=False))
    await asyncio.sleep(0)
    await app._local_stop_task
    assert cancelled.is_set()
    assert not supervisor.is_ready
    supervisor._warmup_inference.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_disable_rechecks_current_config(app):
    from unittest.mock import AsyncMock
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    app.llama_server = supervisor
    app.set_server_loop(asyncio.get_running_loop())
    app._can_warm_local = MagicMock(return_value=True)
    app.config = replace(app.config, enhancement_enabled=True, enhancement_provider="local")
    app.reload_config(replace(app.config, enhancement_enabled=False))
    app.reload_config(replace(app.config, enhancement_enabled=True))
    await asyncio.sleep(0)
    await app._local_stop_task
    supervisor.stop.assert_not_awaited()
    supervisor.kick_warm.assert_called_once()


@pytest.mark.asyncio
async def test_reenable_during_stop_rewarms_after_cleanup(app):
    from unittest.mock import AsyncMock
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def stop():
        entered.set()
        await finish.wait()

    supervisor = MagicMock()
    supervisor.stop = AsyncMock(side_effect=stop)
    app.llama_server = supervisor
    app.set_server_loop(asyncio.get_running_loop())
    app._can_warm_local = MagicMock(return_value=True)
    app.config = replace(app.config, enhancement_enabled=True, enhancement_provider="local")
    app.reload_config(replace(app.config, enhancement_enabled=False))
    await entered.wait()
    app.reload_config(replace(app.config, enhancement_enabled=True))
    supervisor.kick_warm.reset_mock()
    finish.set()
    await app._local_stop_task
    supervisor.kick_warm.assert_called_once_with(asyncio.get_running_loop())


@pytest.mark.asyncio
async def test_deactivation_preserves_bakeoff_pin_and_lockdown_local(app):
    from unittest.mock import AsyncMock
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_residency_pinned = True
    app.llama_server = supervisor
    app.set_server_loop(asyncio.get_running_loop())
    app.config = replace(app.config, enhancement_enabled=True, enhancement_provider="local")
    app.reload_config(replace(app.config, enhancement_enabled=False))
    await asyncio.sleep(0)
    await app._local_stop_task
    supervisor.stop.assert_not_awaited()
    supervisor.is_residency_pinned = False
    app.config = replace(app.config, enhancement_enabled=True, enhancement_provider="local", lockdown_mode=True)
    app.reload_config(replace(app.config, enhancement_provider="openai"))
    await asyncio.sleep(0)
    supervisor.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_clearing_external_endpoint_requests_warm(app):
    supervisor = MagicMock()
    app.llama_server = supervisor
    app.set_server_loop(asyncio.get_running_loop())
    app._can_warm_local = MagicMock(return_value=True)
    app.config = replace(app.config, enhancement_enabled=True, enhancement_provider="local",
                         enhancement_local_endpoint="http://127.0.0.1:9000/v1")
    app.reload_config(replace(app.config, enhancement_local_endpoint=""))
    supervisor.kick_warm.assert_called_once()


@pytest.mark.asyncio
async def test_repeated_inactive_saves_coalesce_stop(app):
    from unittest.mock import AsyncMock
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def stop():
        entered.set()
        await finish.wait()

    supervisor = MagicMock()
    supervisor.stop = AsyncMock(side_effect=stop)
    app.llama_server = supervisor
    app.set_server_loop(asyncio.get_running_loop())
    app.config = replace(app.config, enhancement_enabled=True, enhancement_provider="local")
    app.reload_config(replace(app.config, enhancement_enabled=False))
    await entered.wait()
    first = app._local_stop_task
    app.reload_config(replace(app.config, enhancement_local_idle_timeout=123))
    await asyncio.sleep(0)
    assert app._local_stop_task is first
    supervisor.stop.assert_awaited_once()
    finish.set()
    await first
