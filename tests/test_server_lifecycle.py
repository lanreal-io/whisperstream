"""Focused regression tests for the blocking server lifecycle."""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, call


def test_run_drives_shutdown_callbacks_and_closes_owned_loop(monkeypatch):
    """Ctrl+C must not strand non-daemon workers behind a stopped loop."""

    import uvicorn

    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.stt_engine = object()
    app.config = SimpleNamespace(claude_hud_enabled=False)

    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._app = app
    server._port = 0
    server._logger = logging.getLogger(__name__)
    server._run_engine = MagicMock()
    server.fastapi = object()
    server._loop = None
    server._uvicorn_server = None
    server._imports = MagicMock()
    server._lockdown_state = MagicMock()

    fake_uvicorn_server = object()
    monkeypatch.setattr(uvicorn, "Config", MagicMock(return_value=object()))
    monkeypatch.setattr(
        uvicorn, "Server", MagicMock(return_value=fake_uvicorn_server)
    )

    worker_stop = threading.Event()
    pending_cancelled = threading.Event()
    worker = threading.Thread(
        target=worker_stop.wait,
        name="LifecycleRegressionWorker",
        daemon=False,
    )
    owned_loop: asyncio.AbstractEventLoop | None = None

    async def wait_until_cancelled() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            pending_cancelled.set()

    async def interrupt_after_start(_server) -> None:
        nonlocal owned_loop
        owned_loop = asyncio.get_running_loop()
        root_task = asyncio.current_task()
        assert root_task is not None
        root_task.add_done_callback(lambda _task: worker_stop.set())
        worker.start()
        asyncio.create_task(wait_until_cancelled())
        await asyncio.sleep(0)
        raise KeyboardInterrupt

    server._serve_with_llama = interrupt_after_start

    try:
        server.run()
        worker.join(timeout=1.0)

        assert not worker.is_alive()
        assert pending_cancelled.wait(timeout=1.0)
        assert owned_loop is not None
        assert owned_loop.is_closed()
        assert server._loop is None
        assert server._uvicorn_server is None
        server._imports.cancel.assert_called_once_with()
        server._lockdown_state.lock.assert_called_once_with()
        assert app.set_server_loop.call_args_list == [call(owned_loop), call(None)]
        app.set_event_broadcaster.assert_called_once_with(server.broadcast)
    finally:
        worker_stop.set()
        worker.join(timeout=1.0)
        if owned_loop is not None and not owned_loop.is_closed():
            asyncio.set_event_loop(None)
            owned_loop.close()
