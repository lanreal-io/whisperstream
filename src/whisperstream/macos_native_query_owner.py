"""Bounded main-thread ownership for read-only AppKit snapshots.

Input, capture, routing locks and content never enter this service. A canceled
queued query cannot run later; results from a timed-out active query are discarded.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Any


class NativeQueryUnavailable(RuntimeError):
    pass


@dataclass(eq=False)
class _Request:
    operation: Callable[[], Any] | None
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    canceled: bool = False
    started: bool = False
    result: Any = None
    error: BaseException | None = None


class NativeQueryOwner:
    def __init__(self, *, loop=None, timeout: float = 0.5, capacity: int = 32):
        if threading.current_thread() is not threading.main_thread():
            raise NativeQueryUnavailable("native query owner requires main thread")
        self.loop = loop
        self.timeout = timeout
        self.capacity = capacity
        self._lock = threading.Lock()
        self._closed = False
        self._pending: set[_Request] = set()
        self._queue: queue.Queue[_Request] = queue.Queue()

    def query(self, operation: Callable[[], Any]) -> Any:
        with self._lock:
            if self._closed:
                raise NativeQueryUnavailable("native query owner closed")
        if threading.current_thread() is threading.main_thread():
            return operation()
        request = _Request(operation)
        with self._lock:
            if self._closed:
                raise NativeQueryUnavailable("native query owner closed")
            if len(self._pending) >= self.capacity:
                raise NativeQueryUnavailable("native query owner busy")
            self._pending.add(request)
        try:
            try:
                if self.loop is None:
                    self._queue.put(request)
                else:
                    self.loop.call_soon_threadsafe(self._execute, request)
            except Exception as exc:
                with request.lock:
                    request.canceled = True
                    request.operation = None
                request.done.set()
                raise NativeQueryUnavailable("native query scheduling failed") from exc
            if not request.done.wait(self.timeout):
                with request.lock:
                    request.canceled = True
                    if not request.started:
                        request.operation = None
                raise NativeQueryUnavailable("native query owner timeout")
            if request.error is not None:
                raise request.error
            with request.lock:
                if request.canceled:
                    raise NativeQueryUnavailable("native query canceled")
                return request.result
        finally:
            if request.done.is_set():
                with self._lock:
                    self._pending.discard(request)

    def _execute(self, request: _Request) -> None:
        try:
            if threading.current_thread() is not threading.main_thread():
                raise NativeQueryUnavailable("native query dispatched off main thread")
            with self._lock:
                closed = self._closed
            with request.lock:
                if request.canceled or closed:
                    request.canceled = True
                    request.operation = None
                    return
                # Claim execution atomically against timeout and shutdown.
                request.started = True
                operation = request.operation
            if operation is None:
                return
            result = operation()
            with request.lock:
                if not request.canceled:
                    request.result = result
        except BaseException as exc:
            request.error = exc
        finally:
            request.operation = None
            request.done.set()
            with self._lock:
                self._pending.discard(request)

    def service(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise NativeQueryUnavailable("native queries require main thread")
        deadline = time.monotonic() + 0.01
        while time.monotonic() < deadline:
            try:
                request = self._queue.get_nowait()
            except queue.Empty:
                break
            self._execute(request)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = tuple(self._pending)
            self._pending.clear()
        for request in pending:
            with request.lock:
                request.canceled = True
                request.result = None
                if not request.started:
                    request.operation = None
            request.done.set()


_owner: NativeQueryOwner | None = None
_snapshot_state = threading.local()
_permission_bridge = None


def bind_owner(loop=None):
    global _owner
    replacement = NativeQueryOwner(loop=loop)
    close_owner()
    _owner = replacement
    return _owner


def close_owner(*, expected_owner: NativeQueryOwner | None = None) -> None:
    global _owner
    if expected_owner is not None and _owner is not expected_owner:
        expected_owner.close()
        return
    owner, _owner = _owner, None
    if owner is not None:
        owner.close()


def pump_main_native_events() -> None:
    """One guarded 1 ms native slice; nested snapshots do not pump twice."""
    global _permission_bridge
    if getattr(_snapshot_state, "active", False):
        return
    from .macos_permission_status import _NativeMacosPermissionBridge
    if _permission_bridge is None:
        _permission_bridge = _NativeMacosPermissionBridge()
    _permission_bridge._drain_main_run_loop()


def _snapshot(operation: Callable[[], Any]) -> Any:
    if getattr(_snapshot_state, "active", False):
        return operation()
    pump_main_native_events()
    _snapshot_state.active = True
    try:
        return operation()
    finally:
        _snapshot_state.active = False


def query_on_main(operation: Callable[[], Any]) -> Any:
    owner = _owner
    if owner is not None:
        return owner.query(lambda: _snapshot(operation))
    if threading.current_thread() is threading.main_thread():
        return _snapshot(operation)
    raise NativeQueryUnavailable("native query owner unavailable")


def service_main_queries() -> None:
    if _owner is not None:
        _owner.service()
