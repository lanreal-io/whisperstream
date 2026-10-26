"""Authenticated local transport state. Content stays in the managed RAM store.

Packets are sequenced per mod instance. Reconnect, replacement and session changes
mint a new opaque handle; a delayed poll cannot collect another instance's reply.
No packet body, session identifier or reply may be logged.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from typing import Callable

from .agent_runtime_compatibility import CLAUDE_MOD_MINIMUM, meets_minimum, release_version
from .agent_session_names import bounded_session_key

#: A mod that has not polled for this long no longer owns its session's Stop
#: hook (``ManagedSession.is_fresh``); gaps at or past it are worth a log line.
MOD_POLL_GAP_LOG_SECONDS = 8.0
#: Backstop for a hook-delivered turn the mod never re-sends. The usual end
#: is the session's next prompt, after which the mod cannot send older turns.
HOOK_DELIVERED_TTL_SECONDS = 30.0 * 60.0
MAX_TRACKED_SESSIONS = 64


def accept_claude_packet(store, body: object):
    """Return (session, response, event) after validating one mod packet.

    The caller owns provider/consent/auth guards and rechecks them before output.
    Only connect may create state; all other actions require the exact handle.
    """
    if not isinstance(body, dict):
        return None, {}, None
    instance = body.get("instance")
    sid = body.get("session_id")
    sequence = body.get("sequence")
    revision = body.get("revision")
    surface = body.get("surface")
    version = release_version(body.get("engine_version"))
    action = body.get("action")
    state = body.get("state")
    if (not isinstance(instance, str) or not re.fullmatch(r"[a-f0-9]{32}", instance)
        or not isinstance(sid, str) or bounded_session_key(sid) != sid
        or type(sequence) is not int or not 0 <= sequence <= 2**53 - 1
        or type(revision) is not int or not 0 <= revision <= 2**53 - 1
        or surface not in {"cli", "desktop"} or version is None
        or not meets_minimum(version, CLAUDE_MOD_MINIMUM)
        or action not in {"connect", "poll", "end"}
        or state not in {"idle", "busy"}):
        return None, {}, None
    token = "claude-mod:" + instance
    session = store.get(token)
    if action != "connect":
        if session is None or session.session_handle != body.get("handle"):
            return None, {"reset": True}, None
    elif session is None:
        # One live route per provider session. Reload replaces its exact handle
        # and drops the old instance's offered reply. The user's draft is keyed
        # by conversation in the reply owner, so it survives the reconnect.
        for old in store.sessions():
            if old.agent == "claude" and old.session_id == sid and old.transport == "claude_mod":
                store.deregister(old.token)
        cwd = body.get("cwd")
        session = store.register(token, agent="claude", cwd=cwd if isinstance(cwd, str) and len(cwd) <= 4096 else None)
        session.transport = "claude_mod"
        session.surface = surface
        session.runtime_version = version
    if session.session_id is not None and session.session_id != sid:
        return None, {"reset": True}, None
    if sequence <= session.transport_sequence:
        return None, {}, None
    session.transport_sequence = sequence
    if revision < session.transport_revision:
        return None, {}, None
    if revision != session.transport_revision:
        store.invalidate_reply(session.session_handle)
        session.transport_revision = revision
    if action == "end":
        store.deregister(token)
        return None, {"ok": True}, None
    # A busy packet withdraws any reply already offered for the previous idle
    # turn. Replies accepted while busy are held by the store and offered again
    # at the next Stop. Pickup also tests freshness and state under the lock.
    store.record_event(token, "Stop" if state == "idle" else "UserPromptSubmit", agent="claude", session_id=sid)
    store.refresh_poll(session.session_handle)
    event = body.get("completion")
    if not isinstance(event, dict) or state != "idle":
        event = None
    else:
        text = event.get("text")
        turn_id = event.get("turn_id")
        event = ({"hook_event_name": "Stop", "session_id": sid,
                  "last_assistant_message": text, "prompt_id": turn_id}
                 if isinstance(text, str) and 0 < len(text) <= 200000
                 and isinstance(turn_id, str) and 0 < len(turn_id) <= 256 else None)
    # Process a new completion before offering any reply to its hook. The next
    # heartbeat collects the exact idle generation's reply independently.
    reply = store.take_pending_reply(session.session_handle) if event is None else None
    if reply is not None:
        store.mark_replied(session.session_handle)
    return session, {"ok": True, "handle": session.session_handle, "reply": reply}, event


def _turn_digest(session_id: str, text: str) -> str:
    return hashlib.sha256(
        f"{session_id}\0{text}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()


class HookDeliveredTurns:
    """Turns the Stop hook delivered while the session's mod looked absent.

    A mod whose timer is paused (a background session) cannot claim the Stop
    hook, so the hook speaks the turn. The paused mod still holds that turn
    and sends it when its timer resumes, seconds or minutes later. ``consume``
    lets the transport drop that one re-send. Each hook delivery cancels at
    most one mod completion, and a session's records end at its next prompt,
    so an equal answer in a later turn is never dropped.

    # ========================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # Only a fixed-size SHA-256 digest of session ID and text is retained.
    # ========================================================================
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = HOOK_DELIVERED_TTL_SECONDS,
        max_sessions: int = MAX_TRACKED_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = float(ttl_seconds)
        self._max_sessions = max_sessions
        self._clock = clock
        self._lock = threading.Lock()
        # session_id -> {digest: delivered_at}
        self._turns: "OrderedDict[str, dict[str, float]]" = OrderedDict()

    def record(self, session_id: object, text: object) -> None:
        if not isinstance(session_id, str) or not session_id or not isinstance(text, str):
            return
        with self._lock:
            turns = self._turns.setdefault(session_id, {})
            turns[_turn_digest(session_id, text)] = self._clock()
            self._turns.move_to_end(session_id)
            while len(self._turns) > self._max_sessions:
                self._turns.popitem(last=False)

    def consume(self, session_id: object, text: object) -> bool:
        """True exactly once for a turn the hook already delivered."""
        if not isinstance(session_id, str) or not session_id or not isinstance(text, str):
            return False
        with self._lock:
            turns = self._turns.get(session_id)
            if not turns:
                return False
            delivered_at = turns.pop(_turn_digest(session_id, text), None)
            if not turns:
                self._turns.pop(session_id, None)
            return (
                delivered_at is not None
                and self._clock() - delivered_at <= self._ttl_seconds
            )

    def forget_session(self, session_id: object) -> None:
        with self._lock:
            self._turns.pop(session_id, None)

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()


class ModPollGaps:
    """Seconds between one session's mod packets, across route reaps.

    The routing record is dropped once its poll goes stale, so the gap is kept
    here by session. Only timings are observable; nothing here is logged.
    """

    def __init__(
        self,
        *,
        max_sessions: int = MAX_TRACKED_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_sessions = max_sessions
        self._clock = clock
        self._lock = threading.Lock()
        self._last_packet: "OrderedDict[str, float]" = OrderedDict()

    def observe(self, session_id: object) -> float | None:
        """Record a packet and return the gap since the previous one."""
        if not isinstance(session_id, str) or not session_id:
            return None
        now = self._clock()
        with self._lock:
            previous = self._last_packet.pop(session_id, None)
            self._last_packet[session_id] = now
            while len(self._last_packet) > self._max_sessions:
                self._last_packet.popitem(last=False)
        return None if previous is None else now - previous

    def seen(self, session_id: object) -> bool:
        with self._lock:
            return isinstance(session_id, str) and session_id in self._last_packet

    def clear(self) -> None:
        with self._lock:
            self._last_packet.clear()
