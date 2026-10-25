"""Bounded, lazy background work for the Agent Speak response lifecycle."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class ScheduledCall:
    def __init__(self, owner: DeadlineScheduler, interval: float, callback: Callable[[], None]):
        self.interval = interval
        self._owner = owner
        self._callback = callback
        self._deadline = time.monotonic() + interval
        self._cancelled = False

    def cancel(self) -> None:
        with self._owner._condition:
            self._cancelled = True
            if self._owner._pending is self:
                self._owner._pending = None
            self._owner._condition.notify_all()


class DeadlineScheduler:
    """One scheduled deadline and at most one executing callback per owner.

    Rearming from a callback updates the same worker, never spawning a successor
    thread while that callback is still executing. Idle workers exit naturally.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: ScheduledCall | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def schedule(self, interval: float, callback: Callable[[], None]) -> ScheduledCall:
        call = ScheduledCall(self, interval, callback)
        with self._condition:
            if self._closed:
                call._cancelled = True
                return call
            if self._pending is not None:
                self._pending._cancelled = True
            self._pending = call
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="agent-response-deadline", daemon=True)
                try:
                    self._thread.start()
                except RuntimeError:
                    self._thread = None
                    self._pending = None
                    raise
            self._condition.notify_all()
        return call

    def close(self) -> None:
        with self._condition:
            self._closed = True
            if self._pending is not None:
                self._pending._cancelled = True
            self._pending = None
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                call = self._pending
                if self._closed or call is None:
                    self._thread = None
                    return
                remaining = call._deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                self._pending = None
            if not call._cancelled:
                try:
                    call._callback()
                except Exception:
                    # A callback failure must not strand a later scheduled call.
                    pass


class CoalescingPublisher:
    """One active publication and one dirty bit, with no payload queue.

    The callback reads current state when it runs. Producers never wait for slow
    publication, so UI snapshots cannot hold up response deadline transitions.
    """

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._dirty = False
        self._closed = False

    def submit(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._dirty = True
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="agent-response-publish", daemon=True)
                try:
                    self._thread.start()
                except RuntimeError:
                    self._thread = None
                    self._dirty = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._dirty = False

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._closed or not self._dirty:
                    self._thread = None
                    return
                self._dirty = False
            try:
                self._callback()
            except Exception:
                pass
