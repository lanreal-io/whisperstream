"""Generation-scoped, memory-only leases for HUD reply dictation."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class AgentComposeLease:
    """One short-lived capability to route a recording into a HUD reply."""

    session_handle: str
    generation: int
    expires_at: float
    recording_id: int | None = None


class AgentComposeLeaseStore:
    """Thread-safe single-owner compose lease with monotonic generations."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._generation = 0
        self._active: AgentComposeLease | None = None

    def acquire(
        self,
        session_handle: str,
        *,
        ttl_seconds: float,
    ) -> AgentComposeLease:
        safe_handle = _required_opaque_id("session_handle", session_handle)
        if (
            not isinstance(ttl_seconds, (int, float))
            or isinstance(ttl_seconds, bool)
            or not math.isfinite(float(ttl_seconds))
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive finite number")
        with self._lock:
            self._expire_locked()
            if self._active is not None:
                raise RuntimeError("agent compose lease is already active")
            self._generation += 1
            self._active = AgentComposeLease(
                session_handle=safe_handle,
                generation=self._generation,
                expires_at=self._clock() + float(ttl_seconds),
            )
            return self._active

    def claim(
        self,
        generation: int,
        *,
        recording_id: int,
    ) -> AgentComposeLease | None:
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or isinstance(recording_id, bool)
            or not isinstance(recording_id, int)
            or recording_id < 1
        ):
            return None
        with self._lock:
            self._expire_locked()
            active = self._active
            if (
                active is None
                or active.generation != generation
                or active.recording_id is not None
            ):
                return None
            self._active = AgentComposeLease(
                session_handle=active.session_handle,
                generation=active.generation,
                expires_at=active.expires_at,
                recording_id=recording_id,
            )
            return self._active

    def current(self) -> AgentComposeLease | None:
        with self._lock:
            self._expire_locked()
            return self._active

    def is_current(
        self,
        generation: int,
        *,
        recording_id: int | None = None,
    ) -> bool:
        with self._lock:
            self._expire_locked()
            active = self._active
            return bool(
                active is not None
                and active.generation == generation
                and (
                    recording_id is None
                    or active.recording_id == recording_id
                )
            )

    def release(
        self,
        *,
        generation: int | None = None,
    ) -> AgentComposeLease | None:
        with self._lock:
            self._expire_locked()
            active = self._active
            if active is None:
                return None
            if generation is not None and active.generation != generation:
                return None
            self._active = None
            return active

    def clear(self) -> AgentComposeLease | None:
        """Release the active lease regardless of generation."""

        return self.release()

    def _expire_locked(self) -> AgentComposeLease | None:
        active = self._active
        if active is None or self._clock() < active.expires_at:
            return None
        self._active = None
        return active


def _required_opaque_id(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise TypeError(f"{name} must be a bounded non-empty string")
    return value
