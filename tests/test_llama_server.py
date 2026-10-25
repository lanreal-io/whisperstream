"""Tests for LlamaServerSupervisor (Task 1.5).

All subprocess and HTTP calls are mocked — no real llama-server is spawned.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_process(*, returncode: int | None = None, pid: int = 12345):
    """Return an asyncio.subprocess.Process-like mock."""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode

    async def _wait():
        return proc.returncode

    proc.wait = _wait
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


def _make_health_client(*, ok_after_n_calls: int = 1):
    """Return a context-manager-compatible httpx.AsyncClient mock.

    The first ``ok_after_n_calls - 1`` GET calls raise ``httpx.ConnectError``
    to simulate the server still starting up. The N-th call returns
    ``{"status": "ok"}``.

    The mock is returned by the ``httpx.AsyncClient(...)`` constructor call
    in ``start()``, so it must support ``async with`` (i.e. ``__aenter__``
    must return the inner client object with a ``.get`` method).
    """
    call_count = [0]

    async def _get(url, **kwargs):
        call_count[0] += 1
        if call_count[0] < ok_after_n_calls:
            raise httpx.ConnectError("not up yet")
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={"status": "ok"})
        return resp

    inner_client = MagicMock()
    inner_client.get = _get

    # The outer object is what httpx.AsyncClient() returns; it must be an
    # async context manager. __aenter__ returns the inner client.
    outer = MagicMock()
    outer.__aenter__ = AsyncMock(return_value=inner_client)
    outer.__aexit__ = AsyncMock(return_value=False)
    return outer


import httpx  # noqa: E402  (imported after helper that references it)


# ---------------------------------------------------------------------------
# Test: binary missing raises FileNotFoundError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_raises_when_binary_missing(tmp_path: Path):
    """start() raises FileNotFoundError with a helpful message when binary is absent."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"  # does NOT exist

    supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)

    with pytest.raises(FileNotFoundError, match="llama-server binary not found"):
        await supervisor.start()


# ---------------------------------------------------------------------------
# Test: happy-path start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_happy_path(tmp_path: Path):
    """start() spawns with the correct args and sets port + is_running."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)
        await supervisor.start()

    assert supervisor.is_running is True
    assert supervisor.port > 0
    assert supervisor.base_url == f"http://127.0.0.1:{supervisor.port}/v1"


@pytest.mark.asyncio
async def test_start_passes_correct_spawn_args(tmp_path: Path):
    """start() spawns with --model, --host, --port, --ctx-size, --n-gpu-layers, --log-disable."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "my_model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=fake_exec),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)
        await supervisor.start()

    assert str(binary_path) == captured_args[0]
    assert "--model" in captured_args
    assert str(gguf_path) in captured_args
    assert "--host" in captured_args
    assert "127.0.0.1" in captured_args
    assert "--port" in captured_args
    assert "--ctx-size" in captured_args
    assert "4096" in captured_args
    assert "--n-gpu-layers" in captured_args
    assert "0" in captured_args
    assert "--log-disable" in captured_args
    # The packaged macOS guardian forwards these arguments through the Nuitka
    # engine, whose self-execution guard rejects interpreter flags.
    assert "-m" not in captured_args
    assert "-c" not in captured_args


@pytest.mark.asyncio
async def test_start_resolves_n_gpu_layers_from_provider_at_spawn(tmp_path: Path):
    """ngl is read from the provider at spawn, not frozen at construction.

    Regression for the GPU-offload-on-CPU bug: in the benchmark flow the
    supervisor is constructed during the download step, BEFORE the decode
    benchmark commits verdict="viable", so a construction-time ngl froze at 0
    (CPU). A spawn-time provider lets start() read the current verdict.
    """
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "my_model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    ngl_state = {"value": 0}  # CPU at construction (verdict not yet "viable")
    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=fake_exec),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            n_gpu_layers=0,
            n_gpu_layers_provider=lambda: ngl_state["value"],
        )
        # Benchmark commits "viable" AFTER construction -> provider now returns 99.
        ngl_state["value"] = 99
        await supervisor.start()

    idx = captured_args.index("--n-gpu-layers")
    assert captured_args[idx + 1] == "99", (
        "start() must read --n-gpu-layers from the provider at spawn time, not "
        "freeze the construction-time value (0)"
    )


@pytest.mark.asyncio
async def test_start_pins_device_from_provider(tmp_path: Path):
    """start() pins --device VulkanN from the device_index_provider at spawn."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "my_model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=fake_exec),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            device_index_provider=lambda: 1,
        )
        await supervisor.start()

    assert "--device" in captured_args
    idx = captured_args.index("--device")
    assert captured_args[idx + 1] == "Vulkan1"


@pytest.mark.asyncio
async def test_start_omits_device_when_index_negative(tmp_path: Path):
    """No --device flag when the resolved device index is -1 (llama.cpp default)."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "my_model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=fake_exec),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            device_index_provider=lambda: -1,
        )
        await supervisor.start()

    assert "--device" not in captured_args


@pytest.mark.asyncio
async def test_start_retries_health_check_before_ready(tmp_path: Path):
    """start() keeps polling until /health responds ok."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    # First 3 calls fail; 4th succeeds
    health_client = _make_health_client(ok_after_n_calls=4)

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=health_client),
        # Speed up the poll interval so the test doesn't actually wait
        patch("whisperstream.llama_server._HEALTH_POLL_INTERVAL", 0.0),
    ):
        supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)
        await supervisor.start()

    assert supervisor.is_running is True


# ---------------------------------------------------------------------------
# Test: healthcheck timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_raises_on_healthcheck_timeout(tmp_path: Path):
    """start() raises RuntimeError and terminates the child when timeout elapses."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)

    # Always fail the health check
    always_fail_client = _make_health_client(ok_after_n_calls=99999)

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=always_fail_client),
        # Make the timeout effectively zero so we don't wait 30 s
        patch("whisperstream.llama_server._HEALTH_TIMEOUT", 0.0),
        patch("whisperstream.llama_server._HEALTH_POLL_INTERVAL", 0.0),
    ):
        supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)

        with pytest.raises(RuntimeError, match="llama-server failed to come up"):
            await supervisor.start()

    # terminate() should have been called on the child
    mock_proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# Test: stop() — terminate then kill path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_terminates_process(tmp_path: Path):
    """stop() calls terminate() on the child and waits for it to exit."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)

    # After terminate(), wait() returns with exit code 0
    proc_exited = [False]

    async def _wait_exits():
        proc_exited[0] = True
        mock_proc.returncode = 0
        return 0

    mock_proc.wait = _wait_exits

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)
        await supervisor.start()
        await supervisor.stop()

    mock_proc.terminate.assert_called_once()
    assert proc_exited[0] is True


@pytest.mark.asyncio
async def test_stop_kills_after_timeout(tmp_path: Path):
    """stop() kills the process if it does not exit within _KILL_WAIT seconds."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)

    # wait() hangs forever (simulates stubborn process)
    async def _wait_forever():
        await asyncio.sleep(9999)

    mock_proc.wait = _wait_forever

    # After kill(), wait() returns quickly
    kill_called = [False]
    original_kill = mock_proc.kill

    async def _after_kill_wait():
        mock_proc.returncode = -9
        return -9

    def _kill():
        kill_called[0] = True
        mock_proc.kill = MagicMock()
        mock_proc.wait = _after_kill_wait

    mock_proc.kill = _kill

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=health_client),
        # Make the KILL_WAIT near-zero so the test is fast
        patch("whisperstream.llama_server._KILL_WAIT", 0.001),
    ):
        supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)
        await supervisor.start()
        await supervisor.stop()

    mock_proc.terminate.assert_called_once()
    assert kill_called[0] is True


# ---------------------------------------------------------------------------
# Test: stop() is idempotent when never started or already stopped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_is_noop_when_not_started(tmp_path: Path):
    """stop() does not raise when called before start()."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)
    # No exception expected
    await supervisor.stop()


# ---------------------------------------------------------------------------
# Test: restart_for_model()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_for_model_stops_old_and_updates_path(tmp_path: Path):
    """restart_for_model() stops the running subprocess and updates _gguf_path,
    but does NOT respawn — the lazy lifecycle defers spawn to the next
    ``kick_warm`` call (typically the next recording start).
    """
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path_1 = tmp_path / "model_v1.gguf"
    gguf_path_1.write_bytes(b"gguf_v1")
    gguf_path_2 = tmp_path / "model_v2.gguf"
    gguf_path_2.write_bytes(b"gguf_v2")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    proc_1 = _make_mock_process(returncode=None, pid=1001)

    async def _wait_proc1():
        proc_1.returncode = 0
        return 0

    proc_1.wait = _wait_proc1

    create_calls: list[tuple] = []
    procs = iter([proc_1])

    async def fake_exec(*args, **kwargs):
        create_calls.append(args)
        return next(procs)

    health_client = _make_health_client(ok_after_n_calls=1)

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=fake_exec),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(gguf_path_1, binary_path=binary_path)
        await supervisor.start()
        assert supervisor._process.pid == 1001

        await supervisor.restart_for_model(gguf_path_2)
        # Old subprocess was terminated
        assert proc_1.returncode == 0
        # GGUF path updated, but no respawn (lazy: next kick_warm will spawn)
        assert supervisor._gguf_path == gguf_path_2

    # Only one spawn happened (the initial start; not the restart)
    assert len(create_calls) == 1


# ---------------------------------------------------------------------------
# Test: is_running reflects process state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_running_false_when_process_has_exited(tmp_path: Path):
    """is_running is False once the process's returncode is set."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)
        await supervisor.start()
        assert supervisor.is_running is True

        # Simulate the process having exited
        mock_proc.returncode = 1
        assert supervisor.is_running is False


def test_is_running_false_before_start(tmp_path: Path):
    """is_running is False before start() is ever called."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    binary_path = tmp_path / "llama-server.exe"

    supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)
    assert supervisor.is_running is False


# ---------------------------------------------------------------------------
# Awaitable readiness and runtime residency pinning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_ready_coalesces_concurrent_callers(tmp_path: Path):
    """Concurrent readiness callers share one spawn and warmup task."""
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(
        tmp_path / "model.gguf",
        binary_path=tmp_path / "llama-server.exe",
    )
    mock_proc = _make_mock_process(returncode=None)
    warm_started = asyncio.Event()
    release_warm = asyncio.Event()
    start_calls = 0

    async def fake_start():
        nonlocal start_calls
        start_calls += 1
        supervisor._process = mock_proc
        supervisor.port = 32123

    async def fake_warmup():
        warm_started.set()
        await release_warm.wait()

    with (
        patch.object(supervisor, "start", new=fake_start),
        patch.object(supervisor, "_warmup_inference", new=fake_warmup),
    ):
        callers = [
            asyncio.create_task(supervisor.ensure_ready())
            for _ in range(3)
        ]
        await asyncio.wait_for(warm_started.wait(), timeout=1.0)

        assert start_calls == 1
        assert supervisor._warm_task is not None

        release_warm.set()
        assert await asyncio.gather(*callers) == [True, True, True]

    assert supervisor.is_ready is True
    assert supervisor._warm_task is None


@pytest.mark.asyncio
async def test_ensure_ready_failure_is_content_free_and_stops_partial_child(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """Warm failure returns False, reports only its class, and drains the child."""
    from whisperstream.llama_server import LlamaServerSupervisor

    emitted: list[dict] = []
    supervisor = LlamaServerSupervisor(
        tmp_path / "model.gguf",
        binary_path=tmp_path / "llama-server.exe",
        on_state_change=emitted.append,
    )
    mock_proc = _make_mock_process(returncode=None)

    async def wait_for_exit():
        mock_proc.returncode = 0
        return 0

    mock_proc.wait = wait_for_exit

    async def fake_start():
        supervisor._process = mock_proc
        supervisor.port = 32123

    async def failing_warmup():
        raise RuntimeError("private transcript fragment")

    emitted.clear()
    with (
        patch.object(supervisor, "start", new=fake_start),
        patch.object(supervisor, "_warmup_inference", new=failing_warmup),
    ):
        assert await supervisor.ensure_ready() is False

    assert emitted == [
        {
            "type": "llama.status",
            "state": "error",
            "model": "model.gguf",
            "port": 32123,
            "message": "RuntimeError",
        }
    ]
    assert "private transcript fragment" not in repr(emitted)
    assert "private transcript fragment" not in caplog.text
    mock_proc.terminate.assert_called_once()
    assert supervisor._process is None
    assert supervisor._warm_task is None
    assert supervisor.is_ready is False


@pytest.mark.asyncio
async def test_stop_during_warm_clears_process_and_shared_task(tmp_path: Path):
    """Explicit stop cancels an in-flight warm and leaves no child or task."""
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(
        tmp_path / "model.gguf",
        binary_path=tmp_path / "llama-server.exe",
    )
    mock_proc = _make_mock_process(returncode=None)
    warm_started = asyncio.Event()

    async def wait_for_exit():
        mock_proc.returncode = 0
        return 0

    mock_proc.wait = wait_for_exit

    async def fake_start():
        supervisor._process = mock_proc
        supervisor.port = 32123

    async def blocked_warmup():
        warm_started.set()
        await asyncio.Future()

    with (
        patch.object(supervisor, "start", new=fake_start),
        patch.object(supervisor, "_warmup_inference", new=blocked_warmup),
    ):
        readiness = asyncio.create_task(supervisor.ensure_ready())
        await asyncio.wait_for(warm_started.wait(), timeout=1.0)
        await asyncio.wait_for(supervisor.stop(), timeout=1.0)
        assert await asyncio.wait_for(readiness, timeout=1.0) is False

    mock_proc.terminate.assert_called_once()
    assert supervisor._process is None
    assert supervisor._warm_task is None
    assert supervisor.is_running is False
    assert supervisor.is_ready is False


@pytest.mark.asyncio
async def test_stop_clears_warm_task_cancelled_before_it_starts(tmp_path: Path):
    """Stop handles a shared warm task cancelled before its body can clean up."""
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(
        tmp_path / "model.gguf",
        binary_path=tmp_path / "llama-server.exe",
    )
    task = asyncio.create_task(supervisor._full_warm())
    supervisor._warm_task = task

    await supervisor.stop()

    assert task.cancelled()
    assert supervisor._warm_task is None
    assert supervisor._process is None


def test_residency_pin_suppresses_idle_offload_without_mutating_timeout(
    tmp_path: Path,
):
    """A runtime pin cancels/suppresses idle timers but preserves config state."""
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(
        tmp_path / "model.gguf",
        binary_path=tmp_path / "llama-server.exe",
        idle_timeout_seconds=17.0,
    )
    pending_timer = MagicMock()
    supervisor._idle_timer = pending_timer

    supervisor.pin_resident()

    assert supervisor.is_residency_pinned is True
    assert supervisor._idle_timeout_seconds == 17.0
    pending_timer.cancel.assert_called_once()
    assert supervisor._idle_timer is None

    supervisor._loop = MagicMock()
    supervisor._process = _make_mock_process(returncode=None)
    with patch("threading.Timer") as timer_constructor:
        supervisor.mark_active()
        supervisor._on_idle_expired()

    timer_constructor.assert_not_called()
    supervisor._loop.call_soon_threadsafe.assert_not_called()


@pytest.mark.asyncio
async def test_unpinned_idle_expiry_still_schedules_stop(tmp_path: Path):
    """The existing idle-offload path remains active without a runtime pin."""
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(
        tmp_path / "model.gguf",
        binary_path=tmp_path / "llama-server.exe",
        idle_timeout_seconds=17.0,
    )
    supervisor._loop = asyncio.get_running_loop()
    supervisor._process = _make_mock_process(returncode=None)
    stopped = asyncio.Event()

    async def fake_stop_and_reset(generation):
        stopped.set()

    with patch.object(supervisor, "_stop_and_reset", new=fake_stop_and_reset):
        supervisor._on_idle_expired()
        await asyncio.wait_for(stopped.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_kick_warm_remains_fire_and_forget(tmp_path: Path):
    """The recording path still schedules warming without awaiting it."""
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(
        tmp_path / "model.gguf",
        binary_path=tmp_path / "llama-server.exe",
    )
    mock_proc = _make_mock_process(returncode=None)
    warmed = asyncio.Event()
    start_calls = 0

    async def fake_start():
        nonlocal start_calls
        start_calls += 1
        supervisor._process = mock_proc
        supervisor.port = 32123

    async def fake_warmup():
        warmed.set()

    with (
        patch.object(supervisor, "start", new=fake_start),
        patch.object(supervisor, "_warmup_inference", new=fake_warmup),
    ):
        result = supervisor.kick_warm(asyncio.get_running_loop())
        await asyncio.wait_for(warmed.wait(), timeout=1.0)
        await asyncio.sleep(0)

    assert result is None
    assert start_calls == 1
    assert supervisor.is_ready is True


@pytest.mark.asyncio
async def test_config_only_warm_arms_idle_offload(tmp_path: Path):
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(
        tmp_path / "model.gguf", binary_path=tmp_path / "llama-server.exe", idle_timeout_seconds=30,
    )
    supervisor._process = _make_mock_process(returncode=None)
    with (
        patch("threading.Timer") as timer,
        patch.object(supervisor, "_warmup_inference", new=AsyncMock()),
    ):
        assert await supervisor.ensure_ready()
        timer.assert_called_once()
        assert timer.call_args.args[0] == 30
        timer.return_value.start.assert_called_once()


@pytest.mark.asyncio
async def test_idle_reconfiguration_and_late_expiry_cannot_stop_active_child(tmp_path: Path):
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(
        tmp_path / "model.gguf", binary_path=tmp_path / "llama-server.exe", idle_timeout_seconds=0,
    )
    supervisor._loop = asyncio.get_running_loop()
    supervisor._process = _make_mock_process(returncode=None)
    supervisor._is_ready = True
    with patch("threading.Timer") as timer:
        supervisor.set_idle_timeout(30)
        timer.return_value.start.assert_called_once()
        expired_generation = supervisor._idle_generation
        supervisor._on_idle_expired(expired_generation)
        # Activity before the queued stop executes retires that deadline.
        supervisor.mark_active()
        current_timer = supervisor._idle_timer
        supervisor._on_idle_expired(expired_generation)
        assert supervisor._idle_timer is current_timer
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        supervisor._process.terminate.assert_not_called()
        supervisor.set_idle_timeout(0)
        assert supervisor._idle_timer is None
        await supervisor._stop_and_reset(supervisor._idle_generation - 1)
        supervisor._process.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_pinning_retires_already_queued_idle_stop(tmp_path: Path):
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(
        tmp_path / "model.gguf", binary_path=tmp_path / "llama-server.exe", idle_timeout_seconds=30,
    )
    supervisor._process = _make_mock_process(returncode=None)
    generation = supervisor._idle_generation
    supervisor.pin_resident()
    await supervisor._stop_and_reset(generation)
    supervisor._process.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# Test: find_bundled_llama_server()
# ---------------------------------------------------------------------------


def test_find_bundled_llama_server_returns_none_when_absent(tmp_path: Path):
    """find_bundled_llama_server() returns None when neither path exists."""
    from whisperstream.llama_server import find_bundled_llama_server

    with (
        patch("sys.executable", str(tmp_path / "fake_exe")),
        # Point the module's __file__ to somewhere with no binaries/
        patch("whisperstream.llama_server.__file__", str(tmp_path / "llama_server.py")),
    ):
        result = find_bundled_llama_server()

    assert result is None


def test_find_bundled_llama_server_finds_dev_binary(tmp_path: Path):
    """find_bundled_llama_server() returns the dev binary when it exists."""
    # Build a fake repo structure under tmp_path:
    # tmp_path/
    #   src/
    #     whisperstream/
    #       llama_server.py  <- __file__ (patched)
    #   src-tauri/
    #     binaries/
    #       llama-server-x86_64-pc-windows-msvc.exe
    fake_module = tmp_path / "src" / "whisperstream" / "llama_server.py"
    fake_module.parent.mkdir(parents=True, exist_ok=True)
    fake_module.write_text("# fake")

    binary_dir = tmp_path / "src-tauri" / "binaries"
    binary_dir.mkdir(parents=True, exist_ok=True)
    expected_binary = binary_dir / "llama-server-x86_64-pc-windows-msvc.exe"
    expected_binary.write_bytes(b"binary")

    import whisperstream.llama_server as _ls_mod

    with (
        patch("sys.executable", str(tmp_path / "no_such_exe")),  # packaged path misses
        patch.object(_ls_mod, "__file__", str(fake_module)),
    ):
        result = _ls_mod.find_bundled_llama_server()

    assert result is not None
    assert result == expected_binary


# ---------------------------------------------------------------------------
# Task 1.1: on_state_change callback tests
# ---------------------------------------------------------------------------


def test_emit_dormant_on_init(tmp_path: Path):
    """Passing on_state_change to __init__ triggers a single dormant emit
    with port=None and the correct GGUF name.
    """
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "my_model.gguf"
    binary_path = tmp_path / "llama-server.exe"

    emitted: list[dict] = []
    supervisor = LlamaServerSupervisor(
        gguf_path,
        binary_path=binary_path,
        on_state_change=emitted.append,
    )

    assert len(emitted) == 1
    payload = emitted[0]
    assert payload["type"] == "llama.status"
    assert payload["state"] == "dormant"
    assert payload["model"] == "my_model.gguf"
    assert payload["port"] is None


@pytest.mark.asyncio
async def test_emit_lifecycle_happy_path(tmp_path: Path):
    """Mock-driven happy path through _full_warm records the sequence
    [dormant, spawning, health_ok, warming, ready] in order.
    """
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "my_model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    emitted: list[dict] = []

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)

    # Mock _warmup_inference to avoid real HTTP; emit warming before returning
    # (mirrors what the real method does at its very top).
    async def fake_warmup(self_inner):
        self_inner._emit("warming")

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=health_client),
        patch(
            "whisperstream.llama_server.LlamaServerSupervisor._warmup_inference",
            new=fake_warmup,
        ),
    ):
        supervisor = LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            on_state_change=emitted.append,
        )
        # Clear the init dormant emit so we only track the lifecycle states
        emitted.clear()
        await supervisor._full_warm()

    states = [e["state"] for e in emitted]
    assert states == ["spawning", "health_ok", "warming", "ready"]


@pytest.mark.asyncio
async def test_emit_error_on_warmup_failure(tmp_path: Path):
    """When _warmup_inference raises, an error emit fires with a message field
    and is_ready stays False.
    """
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "my_model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    emitted: list[dict] = []

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)

    async def failing_warmup(self_inner):
        raise RuntimeError("warmup_fail")

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=health_client),
        patch(
            "whisperstream.llama_server.LlamaServerSupervisor._warmup_inference",
            new=failing_warmup,
        ),
    ):
        supervisor = LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            on_state_change=emitted.append,
        )
        emitted.clear()
        await supervisor._full_warm()

    error_emits = [e for e in emitted if e["state"] == "error"]
    assert len(error_emits) >= 1, f"Expected at least one error emit, got: {emitted}"
    assert "message" in error_emits[0]
    assert error_emits[0]["message"] == "RuntimeError"
    assert supervisor.is_ready is False


@pytest.mark.asyncio
async def test_emit_error_on_start_timeout(tmp_path: Path):
    """Healthcheck timeout path emits error before raising RuntimeError."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    emitted: list[dict] = []
    mock_proc = _make_mock_process(returncode=None)
    always_fail_client = _make_health_client(ok_after_n_calls=99999)

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=always_fail_client),
        patch("whisperstream.llama_server._HEALTH_TIMEOUT", 0.0),
        patch("whisperstream.llama_server._HEALTH_POLL_INTERVAL", 0.0),
    ):
        supervisor = LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            on_state_change=emitted.append,
        )
        emitted.clear()

        with pytest.raises(RuntimeError, match="llama-server failed to come up"):
            await supervisor.start()

    error_emits = [e for e in emitted if e["state"] == "error"]
    assert len(error_emits) >= 1, f"Expected error emit before raise, got: {emitted}"


@pytest.mark.asyncio
async def test_emit_callback_exception_is_swallowed(tmp_path: Path):
    """A callback that always raises must NOT propagate through lifecycle methods.

    Mirrors the _broadcast swallow semantics from main.py:155-161.
    """
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "my_model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    def always_raises(payload):
        raise RuntimeError("broadcaster broken!")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)

    async def fake_warmup(self_inner):
        pass

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=health_client),
        patch(
            "whisperstream.llama_server.LlamaServerSupervisor._warmup_inference",
            new=fake_warmup,
        ),
    ):
        # __init__ emits dormant — must not raise
        supervisor = LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            on_state_change=always_raises,
        )
        # _full_warm emits spawning, health_ok, warming, ready — none must raise
        await supervisor._full_warm()
        # stop emits stopping and dormant — none must raise
        await supervisor.stop()

    # If we reach here, all exceptions were swallowed — test passes.


# ---------------------------------------------------------------------------
# Task 1.2: n_gpu_layers constructor kwarg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_n_gpu_layers_default_zero_in_spawn_args(tmp_path: Path):
    """Default constructor (no n_gpu_layers kwarg) produces --n-gpu-layers 0 in args."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=fake_exec),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path)
        await supervisor.start()

    assert "--n-gpu-layers" in captured_args
    ngl_idx = captured_args.index("--n-gpu-layers")
    assert captured_args[ngl_idx + 1] == "0", (
        f"Expected '0' after --n-gpu-layers, got {captured_args[ngl_idx + 1]!r}"
    )


@pytest.mark.asyncio
async def test_n_gpu_layers_99_in_spawn_args(tmp_path: Path):
    """Constructing with n_gpu_layers=99 produces --n-gpu-layers 99 in args."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=fake_exec),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(gguf_path, binary_path=binary_path, n_gpu_layers=99)
        await supervisor.start()

    assert "--n-gpu-layers" in captured_args
    ngl_idx = captured_args.index("--n-gpu-layers")
    assert captured_args[ngl_idx + 1] == "99", (
        f"Expected '99' after --n-gpu-layers, got {captured_args[ngl_idx + 1]!r}"
    )


def test_n_gpu_layers_stored_on_supervisor(tmp_path: Path):
    """n_gpu_layers kwarg is stored as self._n_gpu_layers."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    binary_path = tmp_path / "llama-server.exe"

    sup_default = LlamaServerSupervisor(gguf_path, binary_path=binary_path)
    assert sup_default._n_gpu_layers == 0

    sup_gpu = LlamaServerSupervisor(gguf_path, binary_path=binary_path, n_gpu_layers=99)
    assert sup_gpu._n_gpu_layers == 99


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "acceleration,expected_device", [(None, "Vulkan2"), ("metal", "Metal")]
)
async def test_runtime_metadata_records_resolved_device_and_model_load_time(
    tmp_path: Path, acceleration, expected_device,
):
    """Result metadata reflects spawn-time providers and health-ready timing."""
    from whisperstream.local_llm_runtime_contract import RuntimeAcceleration
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")
    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("httpx.AsyncClient", return_value=health_client),
        patch("whisperstream.llama_server.time.perf_counter", side_effect=[10.0, 10.5]),
    ):
        supervisor = LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            n_gpu_layers_provider=lambda: 99,
            device_index_provider=lambda: 2,
            acceleration=RuntimeAcceleration.METAL if acceleration == "metal" else None,
        )
        await supervisor.start()

    assert supervisor.gguf_path == gguf_path
    assert supervisor.runtime_metadata.device_mode == "gpu"
    assert supervisor.runtime_metadata.device == expected_device
    assert supervisor.runtime_metadata.n_gpu_layers == 99
    assert supervisor.runtime_metadata.load_duration_ms == 500


# ---------------------------------------------------------------------------
# Gemma 4 canonical chat-template resource
# ---------------------------------------------------------------------------


def test_find_bundled_gemma_chat_template_from_source_tree():
    """Development and worktree runs resolve the tracked Tauri resource."""
    from whisperstream.llama_server import (
        _GEMMA_4_TEMPLATE_SHA256,
        find_bundled_gemma_chat_template,
    )

    template_path = find_bundled_gemma_chat_template()

    assert template_path is not None
    assert template_path.name == "gemma-4-e2b-chat-template.jinja"
    assert (
        hashlib.sha256(template_path.read_bytes()).hexdigest()
        == _GEMMA_4_TEMPLATE_SHA256
    )


def test_find_bundled_gemma_chat_template_from_onefile_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Nuitka onefile resolves Tauri's stable install resource directory."""
    from whisperstream.llama_server import find_bundled_gemma_chat_template

    install_dir = tmp_path / "installed"
    template_path = (
        install_dir / "resources" / "gemma-4-e2b-chat-template.jinja"
    )
    template_path.parent.mkdir(parents=True)
    template_path.write_text("installed-template", encoding="utf-8")
    monkeypatch.setenv(
        "NUITKA_ONEFILE_BINARY",
        str(install_dir / "whisperstream-engine.exe"),
    )

    assert find_bundled_gemma_chat_template() == template_path


def test_find_bundled_gemma_chat_template_from_macos_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Nuitka onefile resolves Tauri's Contents/Resources layout on macOS."""
    from whisperstream.llama_server import find_bundled_gemma_chat_template

    contents = tmp_path / "Whisperstream.app/Contents"
    template_path = contents / "Resources/gemma-4-e2b-chat-template.jinja"
    template_path.parent.mkdir(parents=True)
    template_path.write_text("installed-template", encoding="utf-8")
    monkeypatch.setenv(
        "NUITKA_ONEFILE_BINARY",
        str(contents / "MacOS/whisperstream-engine"),
    )

    assert find_bundled_gemma_chat_template() == template_path


def test_find_bundled_gemma_chat_template_from_explicit_bundle_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The Mac engine process contract supplies the exact resource root."""
    from whisperstream.llama_server import find_bundled_gemma_chat_template

    resources = tmp_path / "Whisperstream.app/Contents/Resources"
    template_path = resources / "gemma-4-e2b-chat-template.jinja"
    resources.mkdir(parents=True)
    template_path.write_text("installed-template", encoding="utf-8")
    monkeypatch.setenv("WHISPERSTREAM_BUNDLE_RESOURCES_DIR", str(resources))

    assert find_bundled_gemma_chat_template() == template_path


def test_gemma_chat_template_is_declared_as_tauri_resource():
    """The installer includes the external file that the onefile sidecar finds."""
    repo_root = Path(__file__).resolve().parents[1]
    config = json.loads(
        (repo_root / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )

    assert (
        "resources/gemma-4-e2b-chat-template.jinja"
        in config["bundle"]["resources"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["gemma4-e2b-instruct", "gemma4-e2b-google-qat"])
async def test_start_passes_pinned_template_for_production_gemma(tmp_path: Path, model_id):
    """The canonical production Gemma id gets --chat-template-file."""
    from whisperstream.llama_server import (
        LlamaServerSupervisor,
        find_bundled_gemma_chat_template,
    )

    gguf_path = tmp_path / "gemma-4-E2B-it-Q4_K_M.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")
    template_path = find_bundled_gemma_chat_template()
    assert template_path is not None

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=fake_exec),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            model_id_provider=lambda: model_id,
        )
        await supervisor.start()

    template_arg = captured_args.index("--chat-template-file")
    assert captured_args[template_arg + 1] == str(template_path)


@pytest.mark.asyncio
async def test_start_uses_embedded_jinja_without_gemma_template_for_qwen_candidate(
    tmp_path: Path,
):
    """Qwen candidates use their embedded ChatML template and no external file."""
    from whisperstream.llama_server import LlamaServerSupervisor

    gguf_path = tmp_path / "gemma-4-E2B-it-Q4_K_M.gguf"
    gguf_path.write_bytes(b"gguf")
    binary_path = tmp_path / "llama-server.exe"
    binary_path.write_bytes(b"binary")

    mock_proc = _make_mock_process(returncode=None)
    health_client = _make_health_client(ok_after_n_calls=1)
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with (
        patch("whisperstream.llama_server._create_subprocess_exec", new=fake_exec),
        patch("httpx.AsyncClient", return_value=health_client),
    ):
        supervisor = LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            model_id_provider=lambda: "qwen06b_iclv3-s1",
        )
        await supervisor.start()

    assert "--jinja" in captured_args
    reasoning_arg = captured_args.index("--reasoning")
    assert captured_args[reasoning_arg + 1] == "off"
    assert "--chat-template-file" not in captured_args


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
def test_extensionless_onefile_install_discovery_is_mac_only(monkeypatch, tmp_path, system):
    import whisperstream.llama_server as module
    install = tmp_path / "Whisperstream.app/Contents/MacOS"
    install.mkdir(parents=True)
    binary = install / "llama-server"
    binary.write_bytes(b"synthetic")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.delenv("NUITKA_ONEFILE_BINARY", raising=False)
    monkeypatch.setattr(module.platform, "system", lambda: system)
    monkeypatch.setattr(module.sys, "argv", [str(install / "whisperstream-engine")])
    monkeypatch.setattr(module.sys, "executable", str(cache / "whisperstream-engine"))
    monkeypatch.setattr(module, "__file__", str(cache / "whisperstream/llama_server.py"))
    assert module.find_bundled_llama_server() == (binary if system == "Darwin" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["kick", "scheduled"])
@pytest.mark.parametrize("previous_child", ["exited", "absent"])
async def test_ready_flag_without_live_child_rewarms_once(tmp_path, entrypoint, previous_child):
    """Recording/config warm requests recover an exited child and still coalesce."""
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(tmp_path / "model.gguf", binary_path=tmp_path / "llama-server")
    supervisor._is_ready = True
    supervisor._process = _make_mock_process(returncode=1) if previous_child == "exited" else None
    loop = asyncio.get_running_loop()
    supervisor._loop = loop
    entered, finish = asyncio.Event(), asyncio.Event()
    starts = 0

    async def start():
        nonlocal starts
        starts += 1
        supervisor._process = _make_mock_process(returncode=None)

    async def warmup():
        entered.set()
        await finish.wait()

    with patch.object(supervisor, "start", new=start), patch.object(supervisor, "_warmup_inference", new=warmup):
        for _ in range(3):
            if entrypoint == "kick":
                assert supervisor.kick_warm(loop) is None
            else:
                supervisor._schedule_warm(supervisor._warm_generation)
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert starts == 1
        assert not supervisor.is_ready
        supervisor.kick_warm(loop)
        await asyncio.sleep(0)
        assert starts == 1
        finish.set()
        await asyncio.sleep(0)
        assert supervisor.is_ready
        assert supervisor._warm_task is None


@pytest.mark.asyncio
async def test_live_ready_child_warm_request_remains_noop(tmp_path):
    from whisperstream.llama_server import LlamaServerSupervisor

    supervisor = LlamaServerSupervisor(tmp_path / "model.gguf", binary_path=tmp_path / "llama-server")
    supervisor._is_ready = True
    supervisor._process = _make_mock_process(returncode=None)
    with patch.object(supervisor, "start", new_callable=AsyncMock) as start:
        supervisor.kick_warm(asyncio.get_running_loop())
        supervisor._schedule_warm(supervisor._warm_generation)
        await asyncio.sleep(0)
        start.assert_not_awaited()
        assert supervisor.is_ready
        assert supervisor._warm_task is None


def test_warmup_request_uses_the_served_models_chat_request(tmp_path: Path):
    """Stream-1 Mini warms its own prompt so the first dictation hits the KV cache."""
    from whisperstream.enhancement_models import get_local_model
    from whisperstream.enhancement_policy import resolve_model_policy
    from whisperstream.llama_server import LlamaServerSupervisor

    sup = LlamaServerSupervisor(tmp_path / "m.gguf", binary_path=tmp_path / "s.exe", model_id_provider=lambda: "stream-1-mini")
    request = sup._warmup_request()
    expected = resolve_model_policy(
        "local", get_local_model("stream-1-mini"), model_id="stream-1-mini",
    ).prepare("chat", None).system_prompt
    assert request["messages"][0] == {"role": "system", "content": expected}
    assert request["cache_prompt"] is True
    assert request["chat_template_kwargs"] == {"enable_thinking": False}


def test_warmup_request_falls_back_to_generic_chat_prompt(tmp_path: Path):
    from whisperstream.llama_server import LlamaServerSupervisor
    from whisperstream.llm_enhancer import CHAT_PROMPT

    sup = LlamaServerSupervisor(tmp_path / "m.gguf", binary_path=tmp_path / "s.exe", model_id_provider=lambda: "not-a-model")
    assert sup._warmup_request()["messages"][0]["content"] == CHAT_PROMPT


@pytest.mark.asyncio
async def test_windows_launch_uses_protected_process_without_async_spawn():
    from whisperstream import llama_server
    with (
        patch("whisperstream.windows_child_process.spawn_windows_job_process") as spawn,
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as ordinary,
        patch.object(llama_server.os, "name", "nt"),
    ):
        process = await llama_server._create_subprocess_exec(
            "server.exe", "--port", "1234", cwd="runtime", env={"A": "B"}
        )
        assert process is spawn.return_value
        spawn.assert_called_once_with(
            ("server.exe", "--port", "1234"), cwd="runtime", env={"A": "B"}
        )
        ordinary.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_platform_launch_retains_asyncio_process():
    from whisperstream import llama_server
    with (
        patch("whisperstream.windows_child_process.spawn_windows_job_process") as spawn,
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as ordinary,
        patch.object(llama_server.os, "name", "posix"),
        patch.object(llama_server.sys, "platform", "linux"),
    ):
        process = await llama_server._create_subprocess_exec("server", cwd="runtime")
        assert process is ordinary.return_value
        ordinary.assert_awaited_once_with("server", cwd="runtime")
        spawn.assert_not_called()
