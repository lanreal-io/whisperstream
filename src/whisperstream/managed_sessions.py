"""In-memory registry of Whisperstream-managed coding-agent sessions.

A *managed* session is one the user started through a Whisperstream CLI wrapper,
so a wrapper process holds its PTY and can deliver a dictated reply long after
the hook grace window closed. This module is the state model the rest of the
feature rests on: registration, the refreshable ``token -> session_id``
binding, long-poll liveness, and reaping.

It is deliberately I/O-free and memory-only, like ``agent_activity`` and
``agent_compose``. A backend restart forgets every session; the wrappers
re-register.

Four hard invariants, each with a named test in
``tests/test_managed_sessions.py``:

1. **The token -> session_id binding is refreshable, not one-shot.** A
   session's provider ``session_id`` changes mid-process on resume, compact,
   and new-conversation. Every arriving event that carries a different
   ``session_id`` for a known token rebinds the existing record. A second
   record for the same token is never created.
2. **Provider identity is immutable.** A token belongs to exactly one provider.
   Legacy Claude registration opens the initial prompt immediately. Codex waits
   for a correlated hook from the updated plugin before delivery can open.
   Once any hook is observed, only a safe idle event reopens delivery. A stale
   poll or ended session always remains unsafe.
3. **Arrival order is stable and is the tab order.** Removal never reorders
   survivors; re-registering an existing token never moves it.
4. **Reaping is inspection-only.** Records are dropped on explicit
   deregistration, delivery-poll expiry, or a PID that is gone. The store never
   signals, kills, or terminates anything: per scope decision 8, a hard kill
   would leave the user's console in raw mode with
   ``ENABLE_VIRTUAL_TERMINAL_INPUT`` set. A static test asserts no such call
   is even reachable from this module.

Naming reuses the existing path rather than inventing a second one: the
project basename and the resolve chain come from ``agent_activity``, which in
turn sanitizes through ``agent_session_names``. Names are deliberately never
collision-suffixed here (``agent_activity`` owns that decision: "a name is an
identity a human chose, not a uniqueness token"); tab-level disambiguation is
a view concern keyed on the opaque handle.

# ============================================================================
# TRANSCRIPT DATA - DO NOT LOG
# ``source_name`` is transcript-class, a pending reply payload IS dictated
# transcript text, and the correlation token authorizes delivery into the
# user's editor. Never log a session name, a token, a session ID, a cwd, a
# reply payload, or a serialized session record. Counts are safe.
# ============================================================================
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from .agent_activity import (
    MAX_PROJECT_LABEL_CHARS,
    SourceKey,
    _project_basename,
    _resolve_source_name,
)
from .agent_session_names import bounded_session_key, sanitize_session_name

try:  # pragma: no cover - psutil is a hard dependency; degrade rather than crash
    import psutil
except Exception:  # noqa: BLE001 - a probe must never break the store
    psutil = None  # type: ignore[assignment]

#: Providers supported by the shared managed-session state model.
MANAGED_AGENTS = frozenset({"claude", "codex"})

#: The one authoritative "the coding agent is idle at its own prompt" signal.
STOP_EVENT_KIND = "Stop"
SESSION_START_EVENT_KIND = "SessionStart"
SESSION_END_EVENT_KIND = "SessionEnd"

#: Events that park the session in a *blocked question*, waiting on the user.
#: Scope decision 14 calls this the strongest injection case: Claude cannot be
#: producing output while it waits for an answer.
#:
#: ``PreToolUse`` is here because ``claude_plugin/hooks/hooks.json`` registers
#: our forwarder for it under the ``AskUserQuestion`` matcher *only*, so a
#: ``PreToolUse`` that reaches this store is always an AskUserQuestion and never
#: an ordinary tool call. Widening that matcher would silently turn "Claude is
#: about to run a tool" into an allow, so keep the two in sync.
#:
#: ``PermissionRequest`` is deliberately **excluded**. Permissions stay outside
#: voice routing everywhere else in this codebase (``claude_interactions``), and
#: a permission prompt is a keystroke-driven choice, not a free-text answer.
QUESTION_EVENT_KINDS = frozenset({"PreToolUse"})

#: The wrapper continuously long-polls for replies (25s holds). A session that
#: stops polling for this long is retired.
DEFAULT_SESSION_TTL_SECONDS = 90.0

#: A generous ceiling on concurrent wrapped sessions. Bounded so a leaking or
#: hostile caller cannot grow the store without limit.
MAX_MANAGED_SESSIONS = 32

#: How long an accepted reply stays available for the wrapper to collect
#: (Task 3.3). Scope decision 6 forbids delivering a blocked reply later
#: without a new explicit user action, and a payload no wrapper collected
#: inside this window is exactly that: a reply whose moment has passed.
#: Enforced at pickup, so a wrapper that reconnects after a gap can never
#: inject a stale reply.
PENDING_REPLY_TTL_SECONDS = 30.0
# How long a reply accepted while the agent works may wait for its next Stop.
# Bounded so a forgotten reply cannot surface in a much later turn.
HELD_REPLY_TTL_SECONDS = 30.0 * 60.0

#: Default server-side hold for the wrapper's long-poll, and the hard ceiling
#: a caller may request. Bounded so no caller can pin a connection open.
PENDING_LONG_POLL_SECONDS = 25.0
MAX_PENDING_LONG_POLL_SECONDS = 30.0

#: Upper bound on one dictated reply. A dictation is a sentence or a
#: paragraph; anything past this is a bug or an abuse, not a reply.
MAX_REPLY_CHARS = 10_000
MAX_RESPONSE_CHARS = 200_000

__all__ = [
    "MANAGED_AGENTS",
    "DEFAULT_SESSION_TTL_SECONDS",
    "MAX_MANAGED_SESSIONS",
    "MAX_PENDING_LONG_POLL_SECONDS",
    "MAX_REPLY_CHARS",
    "MAX_RESPONSE_CHARS",
    "PENDING_LONG_POLL_SECONDS",
    "PENDING_REPLY_TTL_SECONDS",
    "ManagedSession",
    "ManagedSessionView",
    "ManagedSessionStore",
    "QUESTION_EVENT_KINDS",
    "SESSION_END_EVENT_KIND",
    "SESSION_START_EVENT_KIND",
    "STOP_EVENT_KIND",
    "USER_PROMPT_SUBMIT_EVENT_KIND",
    "can_reply",
]

USER_PROMPT_SUBMIT_EVENT_KIND = "UserPromptSubmit"


def _default_pid_is_alive(pid: int) -> bool:
    """Inspection-only liveness probe.

    Uses ``psutil.pid_exists``, which opens a query-only handle and never
    acquires one that could terminate the process. Deliberately does not
    construct a ``psutil.Process``: that object exposes ``kill`` /
    ``terminate`` / ``send_signal``, and scope decision 8 forbids all three.
    An unavailable ``psutil`` reports *alive*, so a probe failure can never
    reap a live session; delivery-poll expiry still retires it.
    """
    if psutil is None:
        return True
    return bool(psutil.pid_exists(pid))


def can_reply(
    session: object,
    *,
    now: float | None = None,
    ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
) -> bool:
    """The safe-to-inject predicate: **idle with a live wrapper**.

    This is the single chokepoint every delivery path runs through. It returns
    ``True`` only when all of the following hold:

    1. the trusted wrapper/plugin state is known, the last observed event was
       ``Stop`` (the agent finished its turn), or the session is parked in a
       blocked question;
    2. the wrapper's reply long-poll is fresh and the session is not marked
       ended.

    Terminal draft ownership is deliberately the user's responsibility. The
    wrapper does not parse or retain stdin, and a HUD reply is an explicit
    instruction to paste and submit into the selected session even when text
    is already present at the coding agent's prompt.

    Registration remains sufficient for Claude's first prompt for compatibility.
    Codex requires a correlated hook first, which proves the updated local
    plugin is running before the bridge can inject. Once a hook arrives, an
    unrecognized or active event blocks until the next authoritative idle event.

    **The answer is a bare boolean with no reason string.** This keeps session
    state off the wire.
    Do not add a reason field "for debugging"; it will reach the UI.

    ``now`` and ``ttl_seconds`` exist so a store with a custom clock can supply
    its own (see :meth:`ManagedSessionStore.can_reply`); callers with the
    default clock can omit both.
    """
    if not isinstance(session, ManagedSession):
        return False
    # A live session with no hook yet is the newly registered initial prompt.
    if not session.state_is_known:
        return False
    if session.transport != "pty" and session.last_event_kind != STOP_EVENT_KIND:
        return False
    if (
        session.last_event_kind is not None
        and session.last_event_kind not in {SESSION_START_EVENT_KIND, STOP_EVENT_KIND}
        and session.last_event_kind not in QUESTION_EVENT_KINDS
    ):
        return False
    if not session.is_fresh(
        time.monotonic() if now is None else now, ttl_seconds
    ):
        return False
    return True


def can_accept_reply(
    session: object,
    *,
    now: float | None = None,
    ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
) -> bool:
    """Whether a reply may be accepted now, to deliver at once or at next Stop.

    Unlike :func:`can_reply` this ignores whether the agent is mid-turn: a
    reply typed or dictated while the agent works is held and delivered at the
    next authoritative idle event, the way the coding agent's own prompt
    queues input. It still requires a live, known, not-ended session. A bare
    boolean, for the same reason as ``can_reply``.
    """
    if not isinstance(session, ManagedSession):
        return False
    if not session.state_is_known or session.ended:
        return False
    return session.is_fresh(time.monotonic() if now is None else now, ttl_seconds)


def _default_handle_factory() -> str:
    """Mint an opaque routing handle, unrelated to token, PID, or cwd."""
    return secrets.token_urlsafe(16)


@dataclass(slots=True)
class ManagedSession:
    """One wrapped coding-agent session's live state.

    ``session_handle`` is the routing identity and is **never painted in the
    UI**. ``pid`` exists for liveness and reaping only and is never used for
    correlation: hook payloads carry no PID, and two sessions in one terminal
    are indistinguishable by process identity.

    The working directory is *not* a field. Only the derived, sanitized
    ``source_name`` survives registration, matching ``agent_activity``'s
    treatment of cwd.
    """

    session_handle: str
    token: str
    source_name: str
    arrival_index: int
    last_seen: float
    agent: str = "claude"
    pid: int | None = None
    session_id: str | None = None
    last_event_kind: str | None = None
    ended: bool = False
    transport: str = "pty"
    surface: str | None = None
    runtime_version: str | None = None
    transport_sequence: int = -1
    transport_revision: int = -1
    #: Set only by :meth:`ManagedSessionStore.request_shutdown` (Task 3.4). A
    #: *request*, never a kill: the wrapper observes this on its next
    #: long-poll and drains itself through its own normal
    #: ``RawModeGuard`` exit path (see ``bridge.rs``). This module never
    #: signals, kills, or terminates the wrapper process to enact it.
    shutdown_requested: bool = False

    @property
    def source_key(self) -> SourceKey:
        """The semantic agent-session identity ``agent_activity`` already uses."""
        return SourceKey(self.agent, self.session_id)

    @property
    def state_is_known(self) -> bool:
        """True for the initial registered prompt or an observed live state.

        Claude registration remains initial-prompt authority for compatibility.
        Codex requires a correlated hook before its state is trusted. An ended
        session is never deliverable regardless of its last hook.
        """
        return (
            not self.ended
            and (self.agent == "claude" or self.last_event_kind is not None)
        )

    def is_fresh(self, now: float, ttl_seconds: float) -> bool:
        """True while the wrapper's last delivery poll is inside *ttl_seconds*."""
        if self.transport != "pty":
            ttl_seconds = min(ttl_seconds, 8.0)
        return (now - self.last_seen) <= ttl_seconds


@dataclass(slots=True)
class ManagedSessionView:
    """Transcript-class HUD state kept separate from the routing record.

    Reply drafts are not here: they belong to the reply owner
    (``agent_reply_desk``), keyed by conversation, so a draft survives this
    record being rebound, reconnected or reaped.
    """

    latest_response: str | None = None


class ManagedSessionStore:
    """Bounded, ordered, thread-safe registry of managed sessions.

    Keyed by the wrapper-minted correlation token (bounded through
    ``agent_session_names.bounded_session_key`` so a pathological value cannot
    grow memory). Insertion order is arrival order is tab order.
    """

    def __init__(
        self,
        *,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
        max_sessions: int = MAX_MANAGED_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
        handle_factory: Callable[[], str] = _default_handle_factory,
        pid_is_alive: Callable[[int], bool] = _default_pid_is_alive,
        on_reply_settled: Callable[[str, bool], None] | None = None,
    ) -> None:
        if (
            not isinstance(max_sessions, int)
            or isinstance(max_sessions, bool)
            or max_sessions < 1
        ):
            raise ValueError("max_sessions must be a positive integer")
        if not isinstance(session_ttl_seconds, (int, float)) or (
            session_ttl_seconds <= 0
        ):
            raise ValueError("session_ttl_seconds must be positive")
        self._session_ttl_seconds = float(session_ttl_seconds)
        self._max_sessions = max_sessions
        self._clock = clock
        self._handle_factory = handle_factory
        self._pid_is_alive = pid_is_alive
        self._lock = threading.RLock()
        self._sessions: "OrderedDict[str, ManagedSession]" = OrderedDict()
        self._by_handle: dict[str, str] = {}
        # Accepted-but-not-yet-collected replies, keyed by token as
        # ``(payload, offered_at)``. Deliberately **not** a field on
        # ``ManagedSession``: the payload is transcript-class, and a session
        # record is the thing a future tab view serializes to the wire. Keeping
        # it in a sibling map means no serialization of a session can ever
        # carry a reply payload by accident.
        # The third field is the reply owner's send tags: each tagged entry is
        # reported exactly once through ``on_reply_settled`` - True when the
        # wrapper collects it, False on every drop - so the HUD keeps the text
        # until it is really sent and gives it back otherwise.
        self._pending_replies: dict[str, tuple[str, float, tuple[str, ...]]] = {}
        # Replies accepted while the agent works: (payload, held_at,
        # session_id, tags). Promoted to pending at the next Stop of the same
        # conversation. Same sibling-map rule as pending replies.
        self._held_replies: dict[str, tuple[str, float, str | None, tuple[str, ...]]] = {}
        self._on_reply_settled = on_reply_settled
        # Settlements collected under the lock and reported after it.
        self._settled: list[tuple[str, bool]] = []
        # Full responses and dictated drafts are transcript-class. Keep them
        # in a sibling map so the routing record itself remains safe to inspect
        # and can never serialize content accidentally.
        self._views: dict[str, ManagedSessionView] = {}
        self._reply_waiters: dict[str, set[Callable[[], None]]] = {}
        self._next_arrival = 0

    # -- properties ------------------------------------------------------

    @property
    def session_ttl_seconds(self) -> float:
        return self._session_ttl_seconds

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    # -- registration ----------------------------------------------------

    def register(
        self,
        token: object,
        *,
        agent: object = "claude",
        pid: int | None = None,
        cwd: str | None = None,
        custom_name: str | None = None,
    ) -> ManagedSession:
        """Register a wrapped session, idempotently on *token*.

        A repeat registration returns the existing record, refreshes its PID
        and liveness, and never mints a second handle or moves the session in
        arrival order. ``cwd`` is consumed to derive ``source_name`` and then
        discarded.
        """
        key = _bounded_token(token)
        normalized_agent = _required_agent(agent)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                if existing.agent != normalized_agent:
                    raise ValueError("token is already registered for another agent")
                if pid is not None:
                    existing.pid = pid
                existing.last_seen = self._clock()
                return existing

            session = ManagedSession(
                session_handle=self._mint_handle(),
                token=key,
                source_name=_resolve_name(normalized_agent, cwd, custom_name),
                arrival_index=self._next_arrival,
                last_seen=self._clock(),
                agent=normalized_agent,
                pid=pid,
            )
            self._next_arrival += 1
            self._sessions[key] = session
            self._by_handle[session.session_handle] = key
            self._views[key] = ManagedSessionView()
            self._enforce_limit()
        self._flush_settled()
        return session

    def deregister(self, token: object) -> bool:
        """Drop a session's record. Never signals the process (decision 8)."""
        key = _bounded_token(token)
        with self._lock:
            if self._sessions.get(key) is None:
                return False
            self._drop(key)
        self._flush_settled()
        return True

    def clear(self) -> None:
        with self._lock:
            for handle in tuple(self._reply_waiters):
                self._notify_reply_waiters_locked(handle)
            self._reply_waiters.clear()
            self._sessions.clear()
            self._by_handle.clear()
            for key in tuple(self._pending_replies):
                self._drop_pending_locked(key)
            for key in tuple(self._held_replies):
                self._drop_held_locked(key)
            self._views.clear()
        self._flush_settled()

    def remove_agent(self, agent: object) -> tuple[str, ...]:
        """Drop one provider's sessions and all sibling private state."""
        normalized_agent = _required_agent(agent)
        with self._lock:
            keys = [
                key
                for key, session in self._sessions.items()
                if session.agent == normalized_agent
            ]
            handles = tuple(self._sessions[key].session_handle for key in keys)
            for key in keys:
                self._drop(key)
        self._flush_settled()
        return handles

    # -- lookup ----------------------------------------------------------

    def get(self, token: object) -> ManagedSession | None:
        with self._lock:
            return self._sessions.get(_bounded_token(token))

    def get_by_handle(self, session_handle: object) -> ManagedSession | None:
        if not isinstance(session_handle, str) or not session_handle:
            return None
        with self._lock:
            key = self._by_handle.get(session_handle)
            return self._sessions.get(key) if key is not None else None

    def sessions(self) -> tuple[ManagedSession, ...]:
        """Detached snapshot in stable arrival order."""
        with self._lock:
            return tuple(self._sessions.values())

    def view(self, session_handle: object) -> ManagedSessionView | None:
        """Return the in-memory HUD state for one opaque session handle."""
        if not isinstance(session_handle, str) or not session_handle:
            return None
        with self._lock:
            key = self._by_handle.get(session_handle)
            return self._views.get(key) if key is not None else None

    def record_response(self, token: object, text: object) -> bool:
        """Retain the latest bounded response for a wrapped session."""
        if not isinstance(text, str) or len(text) > MAX_RESPONSE_CHARS:
            return False
        key = _bounded_token(token)
        with self._lock:
            view = self._views.get(key)
            if view is None:
                return False
            view.latest_response = text
            return True

    # -- observations ----------------------------------------------------

    def record_event(
        self,
        token: object,
        kind: str,
        *,
        agent: object | None = None,
        session_id: object = None,
    ) -> ManagedSession | None:
        """Apply one observed hook event to a known session.

        Rebinds ``session_id`` whenever the arriving value differs, so resume,
        compact, and new-conversation keep one record rather than growing a
        second. An unknown token returns ``None`` and creates nothing:
        unwrapped sessions keep their existing behavior untouched (scope
        decision 10).
        """
        try:
            return self._record_event(token, kind, agent=agent, session_id=session_id)
        finally:
            self._flush_settled()

    def _record_event(
        self, token: object, kind: str, *, agent: object, session_id: object
    ) -> ManagedSession | None:
        key = _bounded_token(token)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if agent is not None:
                try:
                    observed_agent = _required_agent(agent)
                except ValueError:
                    return None
                if observed_agent != session.agent:
                    return None

            bound = bounded_session_key(session_id)
            if bound is not None and bound != session.session_id:
                if session.session_id is not None:
                    # The offered/held reply was bound to the old conversation.
                    # The user's draft lives in the reply owner and is re-keyed
                    # there, never dropped here.
                    self._drop_pending_locked(key)
                    self._drop_held_locked(key)
                    self._views[key] = ManagedSessionView()
                    if session.transport == "pty":
                        session.transport_revision += 1
                session.session_id = bound

            if session.transport == "pty" and (
                kind != STOP_EVENT_KIND or session.last_event_kind != STOP_EVENT_KIND
            ):
                # A detached Stop callback cannot regain authority after a
                # subsequent busy -> idle cycle in the same conversation.
                session.transport_revision += 1
                self._drop_pending_locked(key)
            session.last_event_kind = kind

            if kind == STOP_EVENT_KIND:
                # A Stop is authoritative: the agent ran a turn, so it is back at
                # its own prompt and can accept an explicit HUD reply again. A
                # reply held while it worked is offered now.
                session.ended = False
                self._promote_held_reply_locked(session)
            elif kind == SESSION_END_EVENT_KIND:
                session.ended = True
                # There is no longer a session to deliver into. Drop rather
                # than hold: a reply must never survive the conversation it
                # was written for. The reply owner gives the text back.
                self._drop_pending_locked(key)
                self._drop_held_locked(key)

            return session

    def refresh_poll(self, session_handle: object) -> ManagedSession | None:
        """Refresh liveness when the wrapper begins a reply long-poll."""
        if not isinstance(session_handle, str) or not session_handle:
            return None
        with self._lock:
            key = self._by_handle.get(session_handle)
            session = self._sessions.get(key)
            if session is None:
                return None
            session.last_seen = self._clock()
            return session

    # -- shutdown ----------------------------------------------------------

    def watch_reply(self, handle: str, wake: Callable[[], None]) -> Callable[[], None]:
        """Subscribe a nonblocking wakeup to exactly one opaque session."""
        with self._lock:
            self._reply_waiters.setdefault(handle, set()).add(wake)

        def unsubscribe() -> None:
            with self._lock:
                waiters = self._reply_waiters.get(handle)
                if waiters is not None:
                    waiters.discard(wake)
                    if not waiters:
                        self._reply_waiters.pop(handle, None)
        return unsubscribe

    def _notify_reply_waiters_locked(self, handle: str) -> None:
        for wake in tuple(self._reply_waiters.get(handle, ())):
            try:
                wake()
            except RuntimeError:
                # A retiring HTTP loop cannot prevent store cleanup.
                pass

    def request_shutdown(self, session_handle: object) -> bool:
        """Ask one session's wrapper to exit gracefully. **Never a kill**
        (scope decision 8).

        Sets a flag only; the wrapper is the one that acts on it, observing
        it on its next long-poll and draining itself through its
        own normal ``RawModeGuard`` exit path (see ``bridge.rs``). This method
        never signals, kills, or terminates the wrapper process -- it touches
        the in-memory record and nothing else, exactly like every other
        method in this store. The caller's job after this returns is to
        *wait*: a session is only ever removed once its wrapper deregisters on
        its own or its delivery poll expires (:meth:`reap`), never by force.

        A hard kill leaves the user's console in raw mode with
        ``ENABLE_VIRTUAL_TERMINAL_INPUT`` set and wrecks their shell, and
        nothing can run inside an already-terminated process to fix that
        after the fact -- which is exactly why this exists as a request
        instead. The NSIS installer's ``WHISPERSTREAM_GRACEFUL_CLOSE_AND_KILL``
        macro (``src-tauri/installer/hooks.nsh``) force-kills only
        ``Whisperstream.exe`` and ``whisperstream-engine.exe``; it must never
        be extended to find or kill the CLI wrapper, which is the exact hard
        kill this method exists to avoid.

        Returns ``False`` for an unknown handle and sets nothing.
        """
        with self._lock:
            session = self.get_by_handle(session_handle)
            if session is None:
                return False
            session.shutdown_requested = True
            self._notify_reply_waiters_locked(session.session_handle)
            return True

    def is_shutdown_requested(self, session_handle: object) -> bool:
        """Return the graceful-exit request for one opaque handle.

        The long-poll route uses this locked lookup so it can wake a wrapper
        promptly without retaining a mutable session object outside the store.
        Unknown or malformed handles are indistinguishable from no request.
        """
        if not isinstance(session_handle, str) or not session_handle:
            return False
        with self._lock:
            key = self._by_handle.get(session_handle)
            session = self._sessions.get(key) if key is not None else None
            return bool(session is not None and session.shutdown_requested)

    # -- reply delivery --------------------------------------------------

    def set_pending_reply(
        self,
        session_handle: object,
        payload: object,
        *,
        expected_revision: int | None = None,
        tag: str | None = None,
    ) -> bool:
        """Park one accepted reply for the wrapper's next long-poll (Task 3.3).

        Returns ``False`` - refuse, never queue - when the handle is unknown,
        the payload is not a bounded non-empty string, or a fresh reply is
        already waiting. One offered reply at a time: a reply sent while the
        agent works goes to :meth:`hold_reply` instead, which keeps order and
        offers it here at the next Stop (agent-speak-fixes, superseding scope
        decision 6's refuse-only rule).

        The caller runs the payload guard and can_reply first. Detached
        callbacks also supply expected_revision so their final idle/generation
        check and insertion are atomic with hook state changes.
        """
        if not isinstance(payload, str) or not payload or len(payload) > MAX_REPLY_CHARS:
            return False
        with self._lock:
            session = self.get_by_handle(session_handle)
            if session is None:
                return False
            if expected_revision is not None and (
                session.transport_revision != expected_revision or not self.can_reply(session)
            ):
                return False
            existing = self._pending_replies.get(session.token)
            if existing is not None and self._pending_is_fresh(existing[1]):
                return False
            self._drop_pending_locked(session.token)
            self._pending_replies[session.token] = (
                payload,
                self._clock(),
                () if tag is None else (tag,),
            )
            self._notify_reply_waiters_locked(session.session_handle)
        self._flush_settled()
        return True

    def tag_pending_reply(self, session_handle: object, tag: str) -> bool:
        """Attach a send tag to an untagged reply still waiting for pickup.

        A hook Stop bound to the PTY slot parks its reply untagged; the reply
        owner tags it once the hook resolves. False means the wrapper already
        collected it (or nothing waits), so the reply counts as sent.
        """
        with self._lock:
            session = self.get_by_handle(session_handle)
            if session is None:
                return False
            entry = self._pending_replies.get(session.token)
            if entry is None or entry[2]:
                return False
            self._pending_replies[session.token] = (entry[0], entry[1], (tag,))
            return True

    def withdraw_pending_reply(self, session_handle: object, tag: str | None = None) -> bool:
        """Withdraw an uncollected reply (one tag, or whatever waits).

        Used by the pickup window and by acceptance revocation: a late pickup
        can never send text that was already handed back to the user.
        """
        with self._lock:
            session = self.get_by_handle(session_handle)
            if session is None:
                return False
            entry = self._pending_replies.get(session.token)
            if entry is None or (tag is not None and tag not in entry[2]):
                return False
            self._drop_pending_locked(session.token)
        self._flush_settled()
        return True

    def take_pending_reply(self, session_handle: object) -> str | None:
        """Hand the waiting reply to the wrapper, at most once.

        Always consumes the slot, even when the payload is then dropped: an
        expired or no-longer-safe reply is discarded outright, never held for a
        later attempt. Re-evaluates :func:`can_reply` at pickup because
        acceptance and collection are separate moments and the session may have
        been touched in between - the wrapper runs its own local check on top
        of this one, which is the last line of defense.
        """
        with self._lock:
            session = self.get_by_handle(session_handle)
            if session is None:
                return None
            entry = self._pending_replies.pop(session.token, None)
            if entry is None:
                return None
            payload, offered_at, tags = entry
            delivered = self._pending_is_fresh(offered_at) and self.can_reply(session)
            self._settled.extend((tag, delivered) for tag in tags)
        self._flush_settled()
        return payload if delivered else None

    def has_pending_reply(self, session_handle: object) -> bool:
        """True while a fresh, uncollected reply is parked for this session."""
        with self._lock:
            session = self.get_by_handle(session_handle)
            if session is None:
                return False
            entry = self._pending_replies.get(session.token)
            return entry is not None and self._pending_is_fresh(entry[1])

    def invalidate_reply(self, session_handle: str) -> None:
        """Withdraw an offered reply when a session advances to another turn.

        Only the reply already offered to the transport is dropped; its
        revision authority is gone. The user's HUD draft and any reply held for
        the next Stop are kept: replies are allowed while the agent works, and
        typed or dictated text is never discarded by an agent event.
        """
        with self._lock:
            session = self.get_by_handle(session_handle)
            if session is None:
                return
            self._drop_pending_locked(session.token)
        self._flush_settled()

    def mark_replied(self, session_handle: object) -> bool:
        """Record that a HUD reply was submitted into the PTY (Task 3.3).

        Move the session out of its idle state immediately instead of waiting
        for Claude's ``UserPromptSubmit`` hook. This prevents a second HUD
        submission during the small delivery-to-hook race without inspecting
        or retaining terminal input.
        """
        with self._lock:
            session = self.get_by_handle(session_handle)
            if session is None:
                return False
            # A second reply can be accepted in the few milliseconds between
            # the wrapper collecting the first and reporting its PTY write.
            # Drop that raced payload when the authoritative delivery report
            # closes the idle state. It must never survive into a later Stop.
            self._drop_pending_locked(session.token)
            session.last_event_kind = USER_PROMPT_SUBMIT_EVENT_KIND
        self._flush_settled()
        return True

    def _pending_is_fresh(self, offered_at: float) -> bool:
        return (self._clock() - offered_at) <= PENDING_REPLY_TTL_SECONDS

    # -- safety ----------------------------------------------------------

    def can_accept_reply(self, session: object) -> bool:
        """Evaluate :func:`can_accept_reply` against this store's clock and TTL."""
        with self._lock:
            return can_accept_reply(
                session,
                now=self._clock(),
                ttl_seconds=self._session_ttl_seconds,
            )

    def hold_reply(
        self, session_handle: object, payload: object, *, tag: str | None = None
    ) -> bool:
        """Accept a reply for a busy session; it is delivered at its next Stop.

        A second reply while one is held is appended, as the agent's own prompt
        would queue both. The hold is bound to the current conversation and is
        dropped if the session ends, changes conversation, or is removed.
        """
        if not isinstance(payload, str) or not payload.strip():
            return False
        with self._lock:
            session = self.get_by_handle(session_handle)
            if session is None or not self.can_accept_reply(session):
                return False
            existing = self._held_replies.get(session.token)
            if existing is not None and self._held_is_fresh(existing[1]):
                combined = f"{existing[0].rstrip()}\n\n{payload}"
                tags = existing[3]
            else:
                combined = payload
                tags = ()
            if len(combined) > MAX_REPLY_CHARS:
                return False
            if existing is not None and not tags:
                self._drop_held_locked(session.token)
            self._held_replies[session.token] = (
                combined,
                self._clock(),
                session.session_id,
                tags + (() if tag is None else (tag,)),
            )
        self._flush_settled()
        return True

    def has_held_reply(self, session_handle: object) -> bool:
        """True while a reply waits for this session's next Stop."""
        with self._lock:
            session = self.get_by_handle(session_handle)
            if session is None:
                return False
            entry = self._held_replies.get(session.token)
            return entry is not None and self._held_is_fresh(entry[1])

    def _promote_held_reply_locked(self, session: ManagedSession) -> None:
        entry = self._held_replies.get(session.token)
        if entry is None:
            return
        payload, held_at, held_session_id, tags = entry
        if not self._held_is_fresh(held_at) or held_session_id != session.session_id:
            self._drop_held_locked(session.token)
            return
        if self._pending_replies.get(session.token) is not None:
            # Never displace a reply already offered; keep holding.
            return
        del self._held_replies[session.token]
        self._pending_replies[session.token] = (payload, self._clock(), tags)
        self._notify_reply_waiters_locked(session.session_handle)

    def pending_tags(self, session_handle: object) -> tuple[str, ...]:
        """Send tags of the reply waiting for pickup (for the pickup window)."""
        with self._lock:
            session = self.get_by_handle(session_handle)
            entry = self._pending_replies.get(session.token) if session is not None else None
            return () if entry is None else entry[2]

    def _held_is_fresh(self, held_at: float) -> bool:
        return (self._clock() - held_at) <= HELD_REPLY_TTL_SECONDS

    def can_reply(self, session: object) -> bool:
        """Evaluate :func:`can_reply` against this store's clock and TTL.

        Prefer this over the bare function wherever a store is in hand, so a
        store built with a custom clock or a non-default session TTL cannot
        be judged against the module defaults.
        """
        with self._lock:
            return can_reply(
                session,
                now=self._clock(),
                ttl_seconds=self._session_ttl_seconds,
            )

    # -- reaping ---------------------------------------------------------

    def next_expiry_delay(self) -> float | None:
        """Time to the oldest liveness deadline, without probing processes."""
        with self._lock:
            if not self._sessions:
                return None
            deadline = min(
                session.last_seen + (
                    min(self._session_ttl_seconds, 8.0)
                    if session.transport == "claude_mod"
                    else self._session_ttl_seconds
                )
                for session in self._sessions.values()
            )
            # Freshness includes the exact endpoint, so wake just beyond it.
            return max(0.01, deadline - self._clock() + 0.01)

    def reap(self, *, probe_pids: bool = True) -> tuple[str, ...]:
        """Drop records whose delivery poll expired or whose PID is gone.

        Returns the removed session handles. Inspection-only: nothing here
        signals, kills, or terminates a process, and a probe that raises keeps
        the session (poll expiry is the backstop).
        """
        now = self._clock()
        removed: list[str] = []
        with self._lock:
            for key, entry in list(self._held_replies.items()):
                if not self._held_is_fresh(entry[1]):
                    self._drop_held_locked(key)
            for key, session in list(self._sessions.items()):
                if not session.is_fresh(now, self._session_ttl_seconds):
                    removed.append(session.session_handle)
                    self._drop(key)
                    continue
                if probe_pids and session.pid is not None and not self._probe_alive(session.pid):
                    removed.append(session.session_handle)
                    self._drop(key)
        self._flush_settled()
        return tuple(removed)

    # -- internals -------------------------------------------------------

    def _probe_alive(self, pid: int) -> bool:
        try:
            return bool(self._pid_is_alive(pid))
        except Exception:  # noqa: BLE001 - an unreadable probe never reaps
            return True

    def _drop_pending_locked(self, key: str) -> None:
        entry = self._pending_replies.pop(key, None)
        if entry is not None:
            self._settled.extend((tag, False) for tag in entry[2])

    def _drop_held_locked(self, key: str) -> None:
        entry = self._held_replies.pop(key, None)
        if entry is not None:
            self._settled.extend((tag, False) for tag in entry[3])

    def _flush_settled(self) -> None:
        """Report settlements outside the lock, so the callback can take others."""
        with self._lock:
            settled, self._settled = self._settled, []
        callback = self._on_reply_settled
        if callback is None:
            return
        for tag, delivered in settled:
            try:
                callback(tag, delivered)
            except Exception:  # noqa: BLE001 - delivery bookkeeping never raises
                pass

    def _drop(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        self._drop_pending_locked(key)
        self._drop_held_locked(key)
        self._views.pop(key, None)
        if session is not None:
            self._by_handle.pop(session.session_handle, None)
            self._notify_reply_waiters_locked(session.session_handle)
            self._reply_waiters.pop(session.session_handle, None)

    def _mint_handle(self) -> str:
        for _ in range(8):
            handle = self._handle_factory()
            if not isinstance(handle, str) or not handle:
                raise ValueError("handle_factory must return a non-empty string")
            if handle not in self._by_handle:
                return handle
        raise ValueError("handle_factory returned duplicate handles")

    def _enforce_limit(self) -> None:
        while len(self._sessions) > self._max_sessions:
            oldest = next(iter(self._sessions))
            self._drop(oldest)


def _bounded_token(token: object) -> str:
    """Normalize a wrapper-minted token into a bounded dictionary key."""
    key = bounded_session_key(token)
    if key is None:
        raise ValueError("token must be a non-empty string")
    return key


def _required_agent(agent: object) -> str:
    """Return a supported provider identifier or reject the caller."""
    if not isinstance(agent, str) or agent not in MANAGED_AGENTS:
        raise ValueError("agent must be 'claude' or 'codex'")
    return agent


def _resolve_name(
    agent: str,
    cwd: str | None,
    custom_name: str | None,
) -> str:
    """Resolve the display name through the existing agent_activity chain.

    Custom name (sanitized) -> project basename -> the literal agent label.
    Deliberately unsuffixed: ``agent_activity`` owns the decision that a name
    is a human identity, not a uniqueness token.
    """
    sanitized_custom = sanitize_session_name(custom_name)
    name, _is_custom = _resolve_source_name(
        agent,
        sanitized_custom,
        sanitized_custom is not None,
        _project_basename(cwd),
    )
    return name[:MAX_PROJECT_LABEL_CHARS]
