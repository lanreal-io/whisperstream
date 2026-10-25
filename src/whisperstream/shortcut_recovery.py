"""Serialized, prompt-free recovery of the Mac application's hotkey listener."""

from __future__ import annotations

import threading
from typing import Callable, Any


class ShortcutRecovery:
    def __init__(self, keyboard: Any, register: Callable[[], None],
                 busy: Callable[[], bool], *, timer_factory=threading.Timer) -> None:
        self.keyboard = keyboard
        self.register = register
        self.busy = busy
        self.lock = threading.RLock()
        self.enabled = True
        self.closed = False
        self.state = "pending"
        self.failure: str | None = None
        self._timer_factory = timer_factory
        self._deferred = None

    def _defer(self) -> dict[str, str | None]:
        self.state, self.failure = "pending", "busy"
        if self._deferred is None:
            def resume():
                with self.lock:
                    if self._deferred is not timer:
                        return
                    self._deferred = None
                    if not self.closed and self.enabled:
                        self.reconcile()
            timer = self._timer_factory(0.5, resume)
            timer.daemon = True
            self._deferred = timer
            timer.start()
        return self.snapshot()

    def _cancel_deferred(self) -> None:
        if self._deferred is not None:
            self._deferred.cancel()
            self._deferred = None

    def snapshot(self) -> dict[str, str | None]:
        with self.lock:
            if self.closed or not self.enabled:
                return {"state": "disabled", "failure": "shutdown" if self.closed else None}
            if self.state == "ready" and not self.keyboard.listener_ready:
                self.state, self.failure = "failed", "native_error"
            if self.keyboard.capture_active:
                return {"state": "pending", "failure": "busy"}
            return {"state": self.state, "failure": self.failure}

    def reconcile(self, *, force: bool = False) -> dict[str, str | None]:
        # Concurrent UI checks serialize; native creation itself is
        # bounded by the adapter. Never wait for recording or held-key release.
        with self.lock:
            if self.closed or not self.enabled:
                return self.snapshot()
            if force:
                self._registered = False
            reconcile_releases = getattr(self.keyboard, "reconcile_lost_releases", None)
            if reconcile_releases is not None:
                reconcile_releases()
            if self.busy() or self.keyboard.capture_active:
                return self._defer()
            self.state, self.failure = "pending", None
            try:
                # Recheck permission even for an existing listener. This also
                # starts an empty listener so first-run shortcut capture works.
                if not self.keyboard.ensure_ready():
                    return self._defer()
                if force or not getattr(self, "_registered", False):
                    self.keyboard.unhook_all()
                    if getattr(self.keyboard, "last_cleanup_failure", None) is not None:
                        raise RuntimeError("shortcut cleanup incomplete")
                    self.register()
                    if not self.keyboard.ensure_ready():
                        return self._defer()
                    self._registered = True
                self.state = "ready"
            except Exception as exc:
                self._registered = False
                self.state = "blocked" if isinstance(exc, PermissionError) else "failed"
                self.failure = "permission_required" if isinstance(exc, PermissionError) else "native_error"
                # Partial registration must never masquerade as a ready set.
                try:
                    self.keyboard.unhook_all()
                except Exception:
                    self.state, self.failure = "failed", "native_error"
            self._cancel_deferred()
            return self.snapshot()

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self._cancel_deferred()
        # The dispatch thread may be finishing a capture callback that needs
        # this lock. Fence restarts first, then drain it outside the lock.
        self.keyboard.close()
