"""Socket delivery and the content-free dictation reconnect cache.

Recording, usage accounting, compose observation and snapshot replay ordering
remain with their domain/composition owners. Delivery revalidates the exact
captured grant before serialization and before every client send.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from typing import Any, Dict, Optional

from fastapi import WebSocket, WebSocketDisconnect

from ..eula import AcceptanceGrant

_DICTATION_STATE_EVENT_VERSION = 1


_DICTATION_PHASE_ORDER = {"recording": 0, "processing": 1, "idle": 2}


_DICTATION_ORIGINS = frozenset({"push_to_talk", "toggle", "hud"})


# Central outbound defense for transcript-class and provider-returned payloads.
# Content-free lifecycle, download, license, cancellation, and blocked events
# are intentionally absent from this set.
_CONTENT_WS_EVENT_TYPES = frozenset(
    {
        "audio.level",
        "transcription.preview",
        "transcription.result",
        "import.progress",
        "enhancement.status",
        "claude.response",
        "agent.completion",
        "agent.completions.snapshot",
        "agent.compose.state",
        "agent.compose.final",
        "agent.hud.activity",
        "agent.interaction.state",
        "agent.interactions.snapshot",
        "agent.question.snapshot",
        "agent.response_queue.snapshot",
        "agent.sessions",
        "agent.reply.snapshot",
        "agent.reply.result",
        "agent.conversation.snapshot",
        "enhancement.bakeoff.result",
    }
)


def _is_content_ws_event(event: Dict[str, Any]) -> bool:
    """Classify one outbound event without inspecting its content fields."""
    return event.get("type") in _CONTENT_WS_EVENT_TYPES


class DictationStateCache:
    """Thread-safe retention of normalized lifecycle metadata, never preview text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: Optional[Dict[str, Any]] = None

    @staticmethod
    def normalize(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate and strip a lifecycle event to its content-free contract."""
        recording_id = event.get("recording_id")
        phase = event.get("phase")
        origin = event.get("origin")
        if (
            event.get("type") != "dictation.state"
            or event.get("version") != _DICTATION_STATE_EVENT_VERSION
            or isinstance(recording_id, bool)
            or not isinstance(recording_id, int)
            or recording_id < 0
            or phase not in _DICTATION_PHASE_ORDER
            or not (
                (
                    recording_id == 0
                    and phase == "idle"
                    and origin is None
                )
                or (
                    isinstance(origin, str)
                    and origin in _DICTATION_ORIGINS
                )
            )
            or not isinstance(event.get("hud_enabled"), bool)
            or not isinstance(event.get("preview_enabled"), bool)
            or not isinstance(event.get("screen_reader_detected"), bool)
        ):
            return None
        # A reply capture names its compose lease on its own lifecycle event,
        # so the HUD never shows it as ordinary dictation for even one frame.
        compose_generation = event.get("compose_generation")
        if compose_generation is not None and (
            isinstance(compose_generation, bool)
            or not isinstance(compose_generation, int)
            or compose_generation <= 0
        ):
            compose_generation = None
        normalized = {
            "type": "dictation.state",
            "version": _DICTATION_STATE_EVENT_VERSION,
            "recording_id": recording_id,
            "phase": phase,
            "origin": origin,
            "hud_enabled": event["hud_enabled"],
            "preview_enabled": event["preview_enabled"],
            "screen_reader_detected": event["screen_reader_detected"],
        }
        if compose_generation is not None:
            normalized["compose_generation"] = compose_generation
        return normalized

    def retain(
        self, event: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Retain a non-stale normalized lifecycle state, if valid."""
        normalized = self.normalize(event)
        if normalized is None:
            return None
        with self._lock:
            current = self._latest
            if current is not None:
                new_id = normalized["recording_id"]
                current_id = current["recording_id"]
                if new_id < current_id:
                    return None
                if (
                    new_id == current_id
                    and _DICTATION_PHASE_ORDER[normalized["phase"]]
                    < _DICTATION_PHASE_ORDER[current["phase"]]
                ):
                    return None
            self._latest = normalized
            return dict(normalized)

    def snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._latest is None:
                return None
            return dict(self._latest)

# Upper bound on one client send. A client that cannot accept a frame within
# this window is dropped and closed. Clients reconnect and receive the
# connect-time replay.
SEND_TIMEOUT_SECONDS = 5.0

# Upper bound on frames queued for one client. A client that falls this far
# behind is dropped rather than buffering without limit.
MAX_PENDING_FRAMES = 512


class ClientDropped(Exception):
    """A queued direct send could not complete because the client was dropped."""


_Guard = Optional[Callable[[], bool]]


class _ClientChannel:
    """One client's bounded frame queue and its single writer task.

    The writer is the only coroutine that sends on this socket, so a stalled
    peer delays only itself and the number of pending frames, tasks and
    serialized payloads per client is bounded.
    """

    def __init__(self, delivery: "WebSocketDelivery", ws: WebSocket) -> None:
        self._delivery = delivery
        self.ws = ws
        self.queue: asyncio.Queue[
            tuple[str, _Guard, Optional[asyncio.Future[bool]]]
        ] = asyncio.Queue(maxsize=delivery._max_pending)
        self.task: asyncio.Task[None] = asyncio.ensure_future(self._run())

    def offer(
        self, payload: str, guard: _Guard, done: Optional[asyncio.Future[bool]]
    ) -> bool:
        try:
            self.queue.put_nowait((payload, guard, done))
            return True
        except asyncio.QueueFull:
            return False

    async def _run(self) -> None:
        timeout = self._delivery._send_timeout_seconds
        while True:
            payload, guard, done = await self.queue.get()
            if guard is not None and not guard():
                # Acceptance or bake-off authority ended after queueing.
                if done is not None and not done.done():
                    done.set_result(False)
                continue
            try:
                await asyncio.wait_for(self.ws.send_text(payload), timeout=timeout)
            except asyncio.TimeoutError:
                self._fail(done)
                self._delivery._drop(self.ws, reason="send_timeout", close=True)
                return
            except asyncio.CancelledError:
                self._fail(done)
                raise
            except Exception:  # noqa: BLE001 - dead peer, same as before
                self._fail(done)
                self._delivery._drop(self.ws, reason="send_failed", close=False)
                return
            if done is not None and not done.done():
                done.set_result(True)

    @staticmethod
    def _fail(done: Optional[asyncio.Future[bool]]) -> None:
        if done is not None and not done.done():
            done.set_exception(ClientDropped())

    def close(self) -> None:
        """Stop the writer and fail pending direct sends. Idempotent."""

        if not self.task.done() and self.task is not asyncio.current_task():
            self.task.cancel()
        while True:
            try:
                _payload, _guard, done = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._fail(done)


class WebSocketDelivery:
    """Asyncio-owned socket set with one bounded delivery owner per client.

    ``publish`` may be called from any thread. It schedules a single fan-out
    callback on the loop (never a task per event) that serializes once and
    enqueues to each client's bounded queue. Queue overflow or a send timeout
    drops and closes only that client. Exact content grants are revalidated
    before every client send.
    """

    def __init__(
        self,
        *,
        logger: logging.Logger,
        acceptance_is_current: Callable[[AcceptanceGrant], bool],
        bakeoff_is_current: Callable[[AcceptanceGrant], bool],
        send_timeout_seconds: float = SEND_TIMEOUT_SECONDS,
        max_pending_frames: int = MAX_PENDING_FRAMES,
    ) -> None:
        self.clients: set[WebSocket] = set()
        self._channels: dict[WebSocket, _ClientChannel] = {}
        self._logger = logger
        self._acceptance_is_current = acceptance_is_current
        self._bakeoff_is_current = bakeoff_is_current
        self._send_timeout_seconds = send_timeout_seconds
        self._max_pending = max(1, int(max_pending_frames))
        # Strong references for fire-and-forget closes of dropped clients.
        self._closing: set[asyncio.Task[None]] = set()

    # -- membership (event loop) --------------------------------------------

    def register(self, ws: WebSocket) -> None:
        self.clients.add(ws)

    def unregister(self, ws: WebSocket) -> None:
        self.clients.discard(ws)
        channel = self._channels.pop(ws, None)
        if channel is not None:
            channel.close()

    def pending_frames(self, ws: WebSocket) -> int:
        channel = self._channels.get(ws)
        return 0 if channel is None else channel.queue.qsize()

    def _channel(self, ws: WebSocket) -> Optional[_ClientChannel]:
        if ws not in self.clients:
            return None
        channel = self._channels.get(ws)
        if channel is None or channel.task.done():
            channel = _ClientChannel(self, ws)
            self._channels[ws] = channel
        return channel

    def _prune(self) -> None:
        for ws in [ws for ws in self._channels if ws not in self.clients]:
            self.unregister(ws)

    def _drop(self, ws: WebSocket, *, reason: str, close: bool) -> None:
        """Remove one client; optionally close it with a bounded close."""

        was_member = ws in self.clients
        self.unregister(ws)
        if not was_member or not close:
            return
        self._logger.warning("ws client dropped reason=%s", reason)

        async def _close() -> None:
            try:
                await asyncio.wait_for(
                    ws.close(code=1011), timeout=self._send_timeout_seconds
                )
            except Exception:  # noqa: BLE001 - the client is already dropped
                pass

        task = asyncio.ensure_future(_close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _enqueue(
        self,
        ws: WebSocket,
        payload: str,
        guard: _Guard,
        done: Optional[asyncio.Future[bool]],
    ) -> bool:
        channel = self._channel(ws)
        if channel is None:
            return False
        if channel.offer(payload, guard, done):
            return True
        self._drop(ws, reason="queue_overflow", close=True)
        return False

    # -- direct sends (event loop) ------------------------------------------

    async def send(self, ws: WebSocket, payload: str) -> None:
        """Send one frame to ``ws`` in order with its other frames.

        Registered clients go through their bounded channel; the call waits
        for that frame and raises :class:`WebSocketDisconnect` if the client is
        dropped first. Unregistered sockets (before registration or in unit
        tests) get a bounded direct send.
        """

        if ws not in self.clients:
            await asyncio.wait_for(
                ws.send_text(payload), timeout=self._send_timeout_seconds
            )
            return
        done: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        if not self._enqueue(ws, payload, None, done):
            raise WebSocketDisconnect(code=1011)
        try:
            await done
        except ClientDropped:
            raise WebSocketDisconnect(code=1011) from None

    # -- broadcast (any thread) ---------------------------------------------

    def publish(
        self,
        event: Dict[str, Any],
        *,
        loop: asyncio.AbstractEventLoop | None,
        acceptance_grant: AcceptanceGrant | None,
    ) -> None:
        # TRANSCRIPT DATA - DO NOT LOG: event may contain transcript, preview,
        # response or reply text. Log only the event type and client count.
        event_type = event.get("type", "unknown")
        n_clients = len(self.clients)
        self._logger.debug("ws broadcast type=%s clients=%d", event_type, n_clients)

        if not self.clients or loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._fan_out, event, acceptance_grant)
        except RuntimeError:
            pass  # Loop closed

    def _guard_for(
        self, event: Dict[str, Any], grant: AcceptanceGrant | None
    ) -> _Guard:
        content_event = _is_content_ws_event(event)
        bakeoff_result = event.get("type") == "enhancement.bakeoff.result"
        if not content_event and not bakeoff_result:
            return None

        def still_admitted() -> bool:
            if content_event and (
                grant is None or not self._acceptance_is_current(grant)
            ):
                return False
            if (
                bakeoff_result
                and grant is not None
                and not self._bakeoff_is_current(grant)
            ):
                return False
            return True

        return still_admitted

    def _fan_out(
        self, event: Dict[str, Any], grant: AcceptanceGrant | None
    ) -> None:
        """Loop callback: serialize once and enqueue to every client."""

        self._prune()
        guard = self._guard_for(event, grant)
        if guard is not None and not guard():
            return
        if not self.clients:
            return
        payload = json.dumps(event)
        for ws in list(self.clients):
            self._enqueue(ws, payload, guard, None)
