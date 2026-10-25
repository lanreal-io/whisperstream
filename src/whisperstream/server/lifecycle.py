"""Owned event-loop draining and packaged-parent watchdog lifetime.

Composition retains engine startup and domain teardown ordering. This module
owns the blocking serve phase and loop resources. Explicit phase callbacks
close native queries before draining and release domain resources afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uvicorn

def _shutdown_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Finish and close a manually owned loop like ``asyncio.run()`` does."""

    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()

        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
            for task in pending:
                if task.cancelled():
                    continue
                exception = task.exception()
                if exception is not None:
                    loop.call_exception_handler(
                        {
                            "message": "unhandled exception during server shutdown",
                            "exception": exception,
                            "task": task,
                        }
                    )

        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def run_server_loop(
    loop: asyncio.AbstractEventLoop,
    server: uvicorn.Server,
    serve: Callable[[], Awaitable[None]],
    *,
    logger: logging.Logger,
    owner_pid: int | None,
    after_loop_shutdown: Callable[[], None],
    before_loop_shutdown: Callable[[], None] | None = None,
) -> None:
    owner_watchdog_stop = threading.Event()
    owner_watchdog: threading.Thread | None = None
    if owner_pid is not None:

        def _watch_owner() -> None:
            while not owner_watchdog_stop.wait(0.1):
                try:
                    os.kill(owner_pid, 0)
                except ProcessLookupError:
                    logger.warning(
                        "owner process exited owner_pid=%d action=shutdown",
                        owner_pid,
                    )
                    server.should_exit = True
                    return
                except PermissionError:
                    # The direct Tauri parent has the same uid. Treat an
                    # unexpected access failure as still alive so this
                    # watchdog cannot terminate an otherwise healthy app.
                    continue

        owner_watchdog = threading.Thread(
            target=_watch_owner,
            daemon=True,
            name="PackagedOwnerWatchdog",
        )
        owner_watchdog.start()

    try:
        loop.run_until_complete(serve())
    except KeyboardInterrupt:
        logger.info("server shutting down reason=keyboard_interrupt")
    finally:
        if before_loop_shutdown is not None:
            before_loop_shutdown()
        owner_watchdog_stop.set()
        if owner_watchdog is not None:
            owner_watchdog.join(timeout=1.0)
        try:
            _shutdown_event_loop(loop)
        finally:
            after_loop_shutdown()
