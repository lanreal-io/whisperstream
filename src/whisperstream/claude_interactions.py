"""Thread-safe, memory-only broker for blocking Claude interactions.

The broker deliberately stops at storing validated decisions. Encoding those
decisions into Claude hook responses belongs at the HTTP integration boundary.

Privacy boundary: raw Claude session IDs and decision content are backend-only.
They are never included in :meth:`ClaudeInteraction.to_dict`, and this module
does not log interaction data.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
from asyncio import AbstractEventLoop, Future
from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Literal, TypeAlias

from .background_work import CoalescingPublisher, DeadlineScheduler, ScheduledCall

PASSIVE_DEADLINE_SECONDS = 6.0
MAX_PASSIVE_DEADLINE_SECONDS = 30.0
ENGAGED_CEILING_SECONDS = 120.0

InteractionKind: TypeAlias = Literal["stop", "question", "permission"]
AgentProvider: TypeAlias = Literal["claude", "codex"]
InteractionStatus: TypeAlias = Literal["pending", "resolved", "expired"]
InteractionDecision: TypeAlias = str | dict[str, str] | None
ResponsePhase: TypeAlias = Literal[
    "idle",
    "active_speaking",
    "active_grace",
    "quiet_delay",
]
ResponseCaptureMode: TypeAlias = Literal["push_to_talk", "toggle"]
QuestionPhase: TypeAlias = Literal[
    "surfaced",
    "speaking",
    "grace",
    "answered",
    "deferred",
    "expired",
    "held_failed",
]
SpeechTerminalReason: TypeAlias = Literal[
    "completed",
    "stopped",
    "superseded",
    "failed",
    "unavailable",
]

_KINDS = frozenset({"stop", "question", "permission"})
_AGENTS = frozenset({"claude", "codex"})
_PERMISSION_DECISIONS = frozenset({"allow", "deny"})


@dataclass(frozen=True, slots=True)
class ClaudeInteraction:
    """Privacy-safe snapshot of one broker interaction."""

    request_id: str
    session_handle: str
    kind: InteractionKind
    sequence: int
    created_at: float
    passive_deadline: float
    hard_deadline: float
    passive_seconds: float
    agent: AgentProvider = "claude"
    status: InteractionStatus = "pending"
    active: bool = True
    engaged: bool = False
    decision: InteractionDecision = None
    # Resolved display name for the session this interaction belongs to
    # (never a raw provider id). None when the chain resolved nothing; the
    # frontend owns the literal "Claude" fallback. Mirrors
    # _QuestionOpportunity.source_name.
    source_name: "str | None" = None

    @property
    def effective_deadline(self) -> float:
        """Return the active deadline without ever extending the hard ceiling.

        A permission request has no passive phase. Stop and question grace
        exist so an unattended HUD lets Claude continue quickly; a permission
        is the ask itself, and Claude is blocked on it either way, so the
        deadline is the hard ceiling from creation.

        Why the kind branch rather than creating permissions already
        ``engaged``: ``engaged`` means the user reached for the interaction,
        and nothing in the frontend ever sends ``agent.interaction.engage``.
        Overloading it would make the wire state lie about user intent and
        would only hold at the one creation site in ``server.py``. Branching
        here keeps the deadline authoritative for every permission however it
        was created, and leaves ``PASSIVE_DEADLINE_SECONDS`` (shared with Stop
        and question grace) untouched.

        Carried risk, accepted deliberately (scope decision 7): with no HUD
        listening, a permission now stalls Claude for the full 120 second
        ceiling instead of six seconds before failing open.
        """

        if self.kind == "permission":
            return self.hard_deadline
        if not self.active:
            return self.hard_deadline
        return self.hard_deadline if self.engaged else self.passive_deadline

    def to_dict(self, *, now: float) -> dict[str, object]:
        """Serialize only privacy-safe frontend fields.

        Decision payloads can contain replies or question answers, so even a
        resolved decision remains backend-only.
        """

        if self.status == "pending":
            remaining = max(0.0, self.effective_deadline - now)
            hard_remaining = max(0.0, self.hard_deadline - now)
        else:
            remaining = 0.0
            hard_remaining = 0.0

        payload: dict[str, object] = {
            "request_id": self.request_id,
            "session_handle": self.session_handle,
            "kind": self.kind,
            "sequence": self.sequence,
            "status": self.status,
            "active": self.active,
            "engaged": self.engaged,
            "passive_seconds": self.passive_seconds,
            "remaining_seconds": remaining,
            "hard_remaining_seconds": hard_remaining,
            "source_name": self.source_name,
        }
        if self.agent != "claude":
            payload["agent"] = self.agent
        return payload

    def to_wire(self, *, now: float) -> dict[str, object]:
        """Alias used by wire-protocol integration."""

        return self.to_dict(now=now)


class ClaudeInteractionBroker:
    """Condition-backed broker with idempotent terminal transitions."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        request_id_factory: Callable[[], str] | None = None,
        passive_seconds: float = PASSIVE_DEADLINE_SECONDS,
        hard_seconds: float = ENGAGED_CEILING_SECONDS,
    ) -> None:
        if (
            not math.isfinite(passive_seconds)
            or passive_seconds <= 0
            or passive_seconds > MAX_PASSIVE_DEADLINE_SECONDS
        ):
            raise ValueError(
                "passive_seconds must be in "
                f"(0, {MAX_PASSIVE_DEADLINE_SECONDS}]"
            )
        if (
            not math.isfinite(hard_seconds)
            or hard_seconds < passive_seconds
            or hard_seconds > ENGAGED_CEILING_SECONDS
        ):
            raise ValueError(
                "hard_seconds must be at least passive_seconds and no greater "
                f"than {ENGAGED_CEILING_SECONDS}"
            )

        self._clock = clock
        self._request_id_factory = request_id_factory or (
            lambda: secrets.token_urlsafe(18)
        )
        self._passive_seconds = float(passive_seconds)
        self._hard_seconds = float(hard_seconds)
        self._condition = threading.Condition(threading.RLock())
        self._interactions: dict[str, ClaudeInteraction] = {}
        self._raw_session_ids: dict[str, str] = {}
        self._reply_handlers: dict[str, Callable[[str], bool]] = {}
        self._next_sequence = 1

    def create(
        self,
        session_handle: str,
        kind: InteractionKind,
        *,
        raw_session_id: str | None = None,
        request_id: str | None = None,
        passive_seconds: float | None = None,
        active: bool = True,
        source_name: str | None = None,
        agent: AgentProvider = "claude",
    ) -> ClaudeInteraction:
        """Create an interaction, or return an identical explicit-ID retry.

        ``session_handle`` and ``request_id`` are process-local opaque values
        safe for the frontend. ``raw_session_id`` is retained in a separate
        backend-only association and never copied into returned snapshots.

        ``source_name`` is the already-resolved, already-sanitized display
        name for the interaction's session, or ``None`` when the chain
        resolved nothing. It is transcript-class: never log it.
        """

        session_handle = _require_nonempty_string(session_handle, "session_handle")
        kind = _validate_kind(kind)
        agent = _validate_agent(agent)
        safe_name = (
            source_name if isinstance(source_name, str) and source_name else None
        )
        if raw_session_id is not None:
            raw_session_id = _require_nonempty_string(
                raw_session_id, "raw_session_id"
            )
        if request_id is not None:
            request_id = _require_nonempty_string(request_id, "request_id")
        duration = (
            self._passive_seconds
            if passive_seconds is None
            else _validate_passive_seconds(passive_seconds)
        )
        if duration > self._hard_seconds:
            raise ValueError("passive_seconds cannot exceed hard_seconds")
        if not isinstance(active, bool):
            raise ValueError("active must be a boolean")
        if kind == "permission" and not active:
            raise ValueError("permission interactions must begin active")

        with self._condition:
            if request_id is not None and request_id in self._interactions:
                existing = self._interactions[request_id]
                existing_raw = self._raw_session_ids.get(request_id)
                if (
                    existing.session_handle != session_handle
                    or existing.kind != kind
                    or existing.agent != agent
                    or existing.passive_seconds != duration
                    or existing.active != active
                    or (
                        raw_session_id is not None
                        and existing_raw != raw_session_id
                    )
                ):
                    raise ValueError(
                        "request_id is already associated with another interaction"
                    )
                return self._snapshot(existing)

            opaque_request_id = request_id or self._new_request_id_locked()
            created_at = float(self._clock())
            if not math.isfinite(created_at):
                raise ValueError("clock must return a finite value")

            interaction = ClaudeInteraction(
                request_id=opaque_request_id,
                session_handle=session_handle,
                kind=kind,
                sequence=self._next_sequence,
                created_at=created_at,
                passive_deadline=created_at + duration,
                hard_deadline=created_at + self._hard_seconds,
                passive_seconds=duration,
                agent=agent,
                active=active,
                source_name=safe_name,
            )
            self._next_sequence += 1
            self._interactions[opaque_request_id] = interaction
            if raw_session_id is not None:
                self._raw_session_ids[opaque_request_id] = raw_session_id
            self._condition.notify_all()
            return self._snapshot(interaction)

    def get(self, request_id: str) -> ClaudeInteraction | None:
        """Return the current snapshot, expiring a reached deadline first."""

        with self._condition:
            interaction = self._current_locked(request_id)
            return None if interaction is None else self._snapshot(interaction)

    def snapshot(self) -> tuple[ClaudeInteraction, ...]:
        """Return every interaction in creation order after applying expiry."""

        with self._condition:
            interactions = []
            for request_id in tuple(self._interactions):
                interaction = self._current_locked(request_id)
                if interaction is not None:
                    interactions.append(self._snapshot(interaction))
            interactions.sort(key=lambda item: item.sequence)
            return tuple(interactions)

    def raw_session_id_for(self, request_id: str) -> str | None:
        """Return the backend-only raw session association."""

        with self._condition:
            return self._raw_session_ids.get(request_id)

    def versioned_snapshot(
        self, next_revision: Callable[[], int],
    ) -> tuple[int, tuple[ClaudeInteraction, ...]]:
        """Order the snapshot while its state is still locked.

        Allocating after releasing the broker lock could give an older read a
        newer revision than a concurrent resolution.
        """
        with self._condition:
            return next_revision(), self.snapshot()

    def active_session_handles(self) -> set[str]:
        """Return only handles still protected by a pending interaction."""

        with self._condition:
            handles: set[str] = set()
            for request_id in tuple(self._interactions):
                interaction = self._current_locked(request_id)
                if interaction is not None and interaction.status == "pending":
                    handles.add(interaction.session_handle)
            return handles

    def discard_terminal(self, request_id: str) -> bool:
        """Forget one delivered terminal interaction and all private fields.

        The waiting hook owns the call order: it first encodes and broadcasts
        the terminal result, then calls this method. Pending interactions are
        never discarded because their waiter still needs the decision route.
        """

        with self._condition:
            interaction = self._interactions.get(request_id)
            if interaction is None or interaction.status == "pending":
                return False
            self._interactions.pop(request_id, None)
            self._raw_session_ids.pop(request_id, None)
            self._reply_handlers.pop(request_id, None)
            self._condition.notify_all()
            return True

    def clear(self) -> None:
        """Fail open every waiter and scrub all interaction state."""

        with self._condition:
            self._interactions.clear()
            self._raw_session_ids.clear()
            self._reply_handlers.clear()
            self._condition.notify_all()

    def engage(self, request_id: str) -> ClaudeInteraction | None:
        """Hold a still-passive request until its absolute hard deadline."""

        with self._condition:
            interaction = self._current_locked(request_id)
            if interaction is None or interaction.status != "pending":
                return (
                    None
                    if interaction is None
                    else self._snapshot(interaction)
                )
            if interaction.kind == "stop" and not interaction.active:
                return self._snapshot(interaction)
            if not interaction.engaged:
                interaction = replace(interaction, engaged=True)
                self._interactions[request_id] = interaction
                self._condition.notify_all()
            return self._snapshot(interaction)

    def activate_stop(
        self,
        request_id: str,
        *,
        passive_seconds: float | None = None,
    ) -> ClaudeInteraction | None:
        """Start one queued Stop interaction's passive reply deadline.

        Queue residence never consumes passive grace. The absolute hard
        deadline remains anchored to arrival.
        """

        with self._condition:
            interaction = self._current_locked(request_id)
            if interaction is None:
                return None
            if interaction.kind != "stop":
                raise ValueError("only Stop interactions can be activated")
            if interaction.status != "pending" or interaction.active:
                return self._snapshot(interaction)
            duration = (
                interaction.passive_seconds
                if passive_seconds is None
                else _validate_passive_seconds(passive_seconds)
            )
            now = float(self._clock())
            if now >= interaction.hard_deadline:
                expired = replace(
                    interaction,
                    status="expired",
                    active=False,
                    decision=None,
                )
                self._interactions[request_id] = expired
                self._condition.notify_all()
                return self._snapshot(expired)
            activated = replace(
                interaction,
                active=True,
                passive_seconds=duration,
                passive_deadline=min(
                    interaction.hard_deadline,
                    now + duration,
                ),
            )
            self._interactions[request_id] = activated
            self._condition.notify_all()
            return self._snapshot(activated)

    def activate_question(
        self,
        request_id: str,
        *,
        passive_seconds: float | None = None,
    ) -> ClaudeInteraction | None:
        """Start one question's full post-speech answer grace."""

        with self._condition:
            interaction = self._current_locked(request_id)
            if interaction is None:
                return None
            if interaction.kind != "question":
                raise ValueError("only question interactions can be activated")
            if interaction.status != "pending" or interaction.active:
                return self._snapshot(interaction)
            duration = (
                interaction.passive_seconds
                if passive_seconds is None
                else _validate_passive_seconds(passive_seconds)
            )
            now = float(self._clock())
            if now >= interaction.hard_deadline:
                expired = replace(
                    interaction,
                    status="expired",
                    active=False,
                    decision=None,
                )
                self._interactions[request_id] = expired
                self._condition.notify_all()
                return self._snapshot(expired)
            activated = replace(
                interaction,
                active=True,
                passive_seconds=duration,
                passive_deadline=min(interaction.hard_deadline, now + duration),
            )
            self._interactions[request_id] = activated
            self._condition.notify_all()
            return self._snapshot(activated)

    def pause_question(self, request_id: str) -> ClaudeInteraction | None:
        """Suspend answer grace while the next question is being spoken."""

        with self._condition:
            interaction = self._current_locked(request_id)
            if interaction is None:
                return None
            if interaction.kind != "question":
                raise ValueError("only question interactions can be paused")
            if interaction.status != "pending" or not interaction.active:
                return self._snapshot(interaction)
            paused = replace(interaction, active=False, engaged=False)
            self._interactions[request_id] = paused
            self._condition.notify_all()
            return self._snapshot(paused)

    def set_reply_handler(self, request_id: str, handler: Callable[[str], bool]) -> None:
        """Bind an exact Stop to a persistent local delivery slot."""
        with self._condition:
            interaction = self._current_locked(request_id)
            if interaction is not None and interaction.kind == "stop" and interaction.status == "pending":
                self._reply_handlers[request_id] = handler

    def resolve(
        self,
        request_id: str,
        decision: object,
    ) -> ClaudeInteraction | None:
        """Validate and store the decision appropriate for the request kind."""

        with self._condition:
            interaction = self._current_locked(request_id)
            if interaction is None:
                return None
            if interaction.status != "pending":
                return self._snapshot(interaction)

            validated = _validate_decision(interaction.kind, decision)
            handler = self._reply_handlers.get(request_id)
            if handler is not None:
                try:
                    if not isinstance(validated, str) or not handler(validated):
                        return None
                except Exception:
                    return None
            resolved = replace(
                interaction,
                status="resolved",
                decision=validated,
            )
            self._interactions[request_id] = resolved
            self._condition.notify_all()
            return self._snapshot(resolved)

    def resolve_reply(
        self,
        request_id: str,
        reply: object,
    ) -> ClaudeInteraction | None:
        """Resolve a Stop interaction with a non-empty reply."""

        return self._resolve_kind(request_id, "stop", reply)

    def resolve_question(
        self,
        request_id: str,
        answers: object,
    ) -> ClaudeInteraction | None:
        """Resolve a question with its verbatim-question-to-answer map."""

        return self._resolve_kind(request_id, "question", answers)

    def resolve_permission(
        self,
        request_id: str,
        decision: object,
    ) -> ClaudeInteraction | None:
        """Resolve a permission request with ``allow`` or ``deny`` only."""

        return self._resolve_kind(request_id, "permission", decision)

    def defer(self, request_id: str) -> ClaudeInteraction | None:
        """Resolve without a decision so Claude's own prompt remains authoritative."""

        with self._condition:
            interaction = self._current_locked(request_id)
            if interaction is None:
                return None
            if interaction.status != "pending":
                return self._snapshot(interaction)

            deferred = replace(
                interaction,
                status="resolved",
                decision=None,
            )
            self._interactions[request_id] = deferred
            self._condition.notify_all()
            return self._snapshot(deferred)

    def expire(self, request_id: str) -> ClaudeInteraction | None:
        """Apply deadline expiry if the interaction's active deadline was reached."""

        with self._condition:
            interaction = self._current_locked(request_id)
            return None if interaction is None else self._snapshot(interaction)

    def close(self, request_id: str) -> ClaudeInteraction | None:
        """Expire a pending interaction immediately without a decision."""

        with self._condition:
            interaction = self._current_locked(request_id)
            if interaction is None:
                return None
            if interaction.status != "pending":
                return self._snapshot(interaction)
            closed = replace(
                interaction,
                status="expired",
                decision=None,
            )
            self._interactions[request_id] = closed
            self._condition.notify_all()
            return self._snapshot(closed)

    def wait(self, request_id: str) -> ClaudeInteraction | None:
        """Block without polling until resolution, deferral, or expiry."""

        with self._condition:
            while True:
                interaction = self._current_locked(request_id)
                if interaction is None or interaction.status != "pending":
                    return (
                        None
                        if interaction is None
                        else self._snapshot(interaction)
                    )

                remaining = max(
                    0.0, interaction.effective_deadline - float(self._clock())
                )
                if remaining == 0.0:
                    # The next pass performs the terminal transition.
                    continue
                self._condition.wait(timeout=remaining)

    def _resolve_kind(
        self,
        request_id: str,
        expected_kind: InteractionKind,
        decision: object,
    ) -> ClaudeInteraction | None:
        with self._condition:
            interaction = self._current_locked(request_id)
            if interaction is None:
                return None
            if interaction.kind != expected_kind:
                raise ValueError(
                    f"{expected_kind} decision cannot resolve "
                    f"{interaction.kind} interaction"
                )
            if expected_kind in {"stop", "question"} and not interaction.active:
                raise ValueError(f"inactive {expected_kind} interaction is not routable")
            if interaction.status != "pending":
                return self._snapshot(interaction)

            return self.resolve(request_id, decision)

    def _new_request_id_locked(self) -> str:
        for _ in range(32):
            candidate = _require_nonempty_string(
                self._request_id_factory(), "generated request_id"
            )
            if candidate not in self._interactions:
                return candidate
        raise RuntimeError("request_id factory repeatedly returned duplicate IDs")

    def _current_locked(self, request_id: str) -> ClaudeInteraction | None:
        interaction = self._interactions.get(request_id)
        if (
            interaction is not None
            and interaction.status == "pending"
            and float(self._clock()) >= interaction.effective_deadline
        ):
            interaction = replace(
                interaction,
                status="expired",
                decision=None,
            )
            self._interactions[request_id] = interaction
            self._condition.notify_all()
        return interaction

    @staticmethod
    def _snapshot(interaction: ClaudeInteraction) -> ClaudeInteraction:
        return replace(interaction, decision=deepcopy(interaction.decision))


@dataclass(slots=True)
class _QuestionOpportunity:
    session_handle: str
    generation: int
    questions: list[dict[str, Any]]
    surfaced_at: float
    # Resolved display name for the asking session (never a raw provider id).
    # None when the chain resolved nothing; the HUD owns the fallback string.
    source_name: str | None = None
    phase: QuestionPhase = "surfaced"
    request_id: str | None = None
    grace_seconds: float = 0.0
    current_index: int = 0
    answers: dict[str, str] | None = None
    held_answers: dict[str, str] | None = None
    held_at: float | None = None
    defer_sticky: bool = False
    # The user started answering (a capture began). The answer text and the
    # capture itself live in the reply owner (agent_reply_desk), never here.
    engaged: bool = False
    speech_start: Callable[[int, Callable[[SpeechTerminalReason], None]], bool] | None = None
    terminal_at: float | None = None


class ClaudeQuestionCoordinator:
    """Own the surfaced-to-terminal lifecycle of the current Claude question.

    The coordinator deliberately keeps the pre-park opportunity separate from
    the broker. Early answers and Escape are generation-scoped and sticky for
    five seconds so the later PermissionRequest can be answered or failed open
    without ever targeting a successor question.
    """

    EARLY_HOLD_SECONDS = 5.0
    TERMINAL_TTL_SECONDS = 15.0
    MAX_DRAFT_CHARS = 50_000

    def __init__(
        self,
        *,
        broker: ClaudeInteractionBroker,
        state_changed: Callable[[], None],
        interaction_changed: Callable[[ClaudeInteraction], None],
        speech_stop: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        next_revision: Callable[[], int] | None = None,
    ) -> None:
        self._broker = broker
        self._state_changed = state_changed
        self._interaction_changed = interaction_changed
        self._speech_stop = speech_stop
        self._clock = clock
        self._lock = threading.RLock()
        self._current: _QuestionOpportunity | None = None
        self._next_generation = 1
        self._next_revision = next_revision or (lambda: self._revision + 1)
        self._revision = next_revision() if next_revision is not None else 0
        self._early_timer: threading.Timer | None = None
        self._terminal_timer: threading.Timer | None = None

    @staticmethod
    def _is_terminal_phase(phase: QuestionPhase) -> bool:
        return phase in {"answered", "deferred", "expired", "held_failed"}

    def _set_phase_locked(
        self,
        item: _QuestionOpportunity,
        phase: QuestionPhase,
    ) -> None:
        item.phase = phase
        if self._is_terminal_phase(phase):
            if item.terminal_at is None:
                item.terminal_at = float(self._clock())
            self._schedule_terminal_timer_locked(item)
        else:
            item.terminal_at = None
            self._cancel_terminal_timer_locked()

    def _schedule_terminal_timer_locked(self, item: _QuestionOpportunity) -> None:
        self._cancel_terminal_timer_locked()
        if item.terminal_at is None:
            return
        remaining = max(
            0.001,
            item.terminal_at
            + self.TERMINAL_TTL_SECONDS
            - float(self._clock()),
        )
        timer = threading.Timer(
            remaining,
            self._expire_terminal,
            args=(item.generation,),
        )
        try:
            timer.daemon = True
            self._terminal_timer = timer
            timer.start()
        except RuntimeError:
            self._terminal_timer = None

    def _cancel_terminal_timer_locked(self) -> None:
        if self._terminal_timer is not None:
            self._terminal_timer.cancel()
            self._terminal_timer = None

    def _prune_terminal_locked(self, now: float) -> bool:
        item = self._current
        if (
            item is None
            or not self._is_terminal_phase(item.phase)
            or item.terminal_at is None
            or now < item.terminal_at + self.TERMINAL_TTL_SECONDS
        ):
            return False
        self._cancel_terminal_timer_locked()
        self._current = None
        self._revision = self._next_revision()
        return True

    def _expire_terminal(self, generation: int) -> None:
        changed = False
        with self._lock:
            item = self._current
            if item is None or item.generation != generation:
                return
            changed = self._prune_terminal_locked(float(self._clock()))
            if not changed and self._is_terminal_phase(item.phase):
                self._schedule_terminal_timer_locked(item)
        if changed:
            self._state_changed()

    def acknowledge(self, session_handle: str, generation: int) -> bool:
        """Scrub one exact terminal question after the HUD finishes its receipt."""

        with self._lock:
            item = self._current
            if (
                item is None
                or item.session_handle != session_handle
                or item.generation != generation
                or not self._is_terminal_phase(item.phase)
            ):
                return False
            self._cancel_early_timer_locked()
            self._cancel_terminal_timer_locked()
            self._current = None
            self._revision = self._next_revision()
        self._state_changed()
        return True

    def active_session_handles(self) -> set[str]:
        """Return the question handle only while content is still actionable."""

        with self._lock:
            self._prune_terminal_locked(float(self._clock()))
            item = self._current
            if item is None or self._is_terminal_phase(item.phase):
                return set()
            return {item.session_handle}

    def clear(self) -> None:
        """Scrub every question and answer field."""

        stop_speech = False
        with self._lock:
            item = self._current
            if item is not None:
                stop_speech = item.phase == "speaking"
            self._cancel_early_timer_locked()
            self._cancel_terminal_timer_locked()
            self._current = None
            self._revision = self._next_revision()
        if stop_speech and self._speech_stop is not None:
            try:
                self._speech_stop()
            except Exception:
                pass
        self._state_changed()

    def surface(
        self,
        session_handle: str,
        questions: list[dict[str, Any]],
        *,
        source_name: str | None = None,
    ) -> int:
        """Surface an exact validated question batch and return its generation.

        ``source_name`` is the already-resolved, already-sanitized display name
        for the asking session, or ``None`` when the chain resolved nothing. It
        is transcript-class: never log it.
        """

        safe_handle = _require_nonempty_string(session_handle, "session_handle")
        safe_name = source_name if isinstance(source_name, str) and source_name else None
        safe_questions = deepcopy(questions)
        if not safe_questions:
            raise ValueError("questions must not be empty")
        with self._lock:
            current = self._current
            if (
                current is not None
                and current.session_handle == safe_handle
                and current.questions == safe_questions
                and current.phase not in {
                    "answered",
                    "deferred",
                    "expired",
                    "held_failed",
                }
            ):
                return current.generation
            self._cancel_early_timer_locked()
            self._cancel_terminal_timer_locked()
            self._current = _QuestionOpportunity(
                session_handle=safe_handle,
                generation=self._next_generation,
                questions=safe_questions,
                surfaced_at=float(self._clock()),
                source_name=safe_name,
                answers={},
            )
            self._next_generation += 1
            self._revision = self._next_revision()
            generation = self._current.generation
        self._state_changed()
        return generation

    def attach(
        self,
        interaction: ClaudeInteraction,
        questions: list[dict[str, Any]],
        *,
        grace_seconds: float,
        speech_start: Callable[
            [int, Callable[[SpeechTerminalReason], None]], bool
        ] | None,
    ) -> ClaudeInteraction:
        """Attach only the exact answer park and start question presentation."""

        if interaction.kind != "question" or interaction.active:
            raise ValueError("question park must begin inactive")
        terminal: ClaudeInteraction | None = None
        start_index: int | None = None
        with self._lock:
            item = self._current
            if (
                item is None
                or item.session_handle != interaction.session_handle
                or item.questions != questions
                or item.phase not in {"surfaced", "deferred"}
            ):
                terminal = self._broker.close(interaction.request_id)
            else:
                self._cancel_early_timer_locked()
                item.request_id = interaction.request_id
                item.grace_seconds = float(grace_seconds)
                item.speech_start = speech_start
                now = float(self._clock())
                if item.defer_sticky:
                    self._set_phase_locked(item, "deferred")
                    terminal = self._broker.defer(interaction.request_id)
                elif item.held_answers is not None:
                    if now - item.surfaced_at <= self.EARLY_HOLD_SECONDS:
                        activated = self._broker.activate_question(
                            interaction.request_id,
                            passive_seconds=grace_seconds,
                        )
                        if activated is not None and activated.status == "pending":
                            terminal = self._broker.resolve_question(
                                interaction.request_id,
                                item.held_answers,
                            )
                            self._set_phase_locked(item, "answered")
                        else:
                            terminal = activated
                            self._set_phase_locked(item, "held_failed")
                    else:
                        self._set_phase_locked(item, "held_failed")
                        terminal = self._broker.close(interaction.request_id)
                elif item.engaged:
                    # The user is already answering: no speech, straight to
                    # the answer window.
                    activated = self._broker.activate_question(
                        interaction.request_id,
                        passive_seconds=grace_seconds,
                    )
                    if activated is not None:
                        terminal = activated
                else:
                    item.phase = "speaking"
                    start_index = item.current_index
                self._revision = self._next_revision()
        if terminal is not None:
            self._interaction_changed(terminal)
        self._state_changed()
        if start_index is not None:
            accepted = False
            if speech_start is not None:
                try:
                    accepted = bool(
                        speech_start(
                            start_index,
                            lambda reason: self.speech_terminal(
                                interaction.request_id, reason
                            ),
                        )
                    )
                except Exception:
                    accepted = False
            if not accepted:
                self.speech_terminal(interaction.request_id, "unavailable")
        return terminal or interaction

    def speech_terminal(
        self,
        request_id: str,
        reason: SpeechTerminalReason,
    ) -> None:
        """Begin the full grace only after exact question playback terminates."""

        interaction: ClaudeInteraction | None = None
        with self._lock:
            item = self._current
            if (
                item is None
                or item.request_id != request_id
                or item.phase != "speaking"
            ):
                return
            interaction = self._broker.activate_question(
                request_id,
                passive_seconds=item.grace_seconds,
            )
            self._set_phase_locked(
                item,
                "grace"
                if interaction is not None and interaction.status == "pending"
                else "expired",
            )
            self._revision = self._next_revision()
        if interaction is not None:
            self._interaction_changed(interaction)
        self._state_changed()

    def begin_capture(self) -> float | None:
        """The user started answering by voice: give the question priority.

        Activates and engages the parked request, stops question speech, and
        returns the seconds the answer may take (None: nothing to answer).
        The capture itself is bound by the reply owner.
        """

        stop_speech = False
        interaction: ClaudeInteraction | None = None
        with self._lock:
            item = self._current
            if item is None or self._is_terminal_phase(item.phase):
                return None
            now = float(self._clock())
            if item.request_id is None:
                remaining = self.EARLY_HOLD_SECONDS - (now - item.surfaced_at)
                if remaining <= 0:
                    self._set_phase_locked(item, "held_failed")
                    self._revision = self._next_revision()
                    result = None
                else:
                    result = remaining
            else:
                interaction = self._broker.get(item.request_id)
                if interaction is None or interaction.status != "pending":
                    return None
                if not interaction.active:
                    interaction = self._broker.activate_question(
                        item.request_id,
                        passive_seconds=item.grace_seconds,
                    )
                if interaction is None or interaction.status != "pending":
                    return None
                interaction = self._broker.engage(item.request_id)
                result = max(0.0, interaction.hard_deadline - now)
                if result <= 0:
                    return None
                stop_speech = item.phase == "speaking"
                if stop_speech:
                    self._set_phase_locked(item, "grace")
            if result is not None:
                item.engaged = True
                self._revision = self._next_revision()
        if stop_speech and self._speech_stop is not None:
            try:
                self._speech_stop()
            except Exception:
                pass
        if interaction is not None:
            self._interaction_changed(interaction)
        self._state_changed()
        return result

    def current_target(self) -> tuple[str, int] | None:
        """(session_handle, generation) of the question that takes answers."""

        with self._lock:
            item = self._current
            if item is None or self._is_terminal_phase(item.phase):
                return None
            return item.session_handle, item.generation

    def submit_text(
        self, session_handle: str, generation: int, text: object
    ) -> ClaudeInteraction | None | bool:
        """Answer the current question with one freeform text, then advance.

        Returns the resolved interaction, True when the answer was accepted
        but more questions (or the early hold) remain, or None when refused.
        """

        if not isinstance(text, str) or not text.strip() or len(text) > self.MAX_DRAFT_CHARS:
            return None
        interaction: ClaudeInteraction | None = None
        speech: tuple[
            Callable[[int, Callable[[SpeechTerminalReason], None]], bool],
            int,
            str,
        ] | None = None
        with self._lock:
            item = self._current
            if (
                item is None
                or item.session_handle != session_handle
                or item.generation != generation
                or self._is_terminal_phase(item.phase)
            ):
                return None
            question_key = item.questions[item.current_index]["question"]
            answers = dict(item.answers or {})
            answers[question_key] = text
            item.answers = answers
            if item.current_index + 1 < len(item.questions):
                item.current_index += 1
                if item.request_id is None:
                    item.phase = "surfaced"
                else:
                    interaction = self._broker.pause_question(item.request_id)
                    item.phase = "speaking"
                    if item.speech_start is not None:
                        speech = (
                            item.speech_start,
                            item.current_index,
                            item.request_id,
                        )
            elif item.request_id is None:
                item.held_answers = answers
                item.held_at = float(self._clock())
                item.phase = "surfaced"
                self._schedule_early_hold_locked(item)
            else:
                interaction = self._broker.resolve_question(
                    item.request_id, answers
                )
                self._set_phase_locked(item, "answered")
            self._revision = self._next_revision()
        resolved = interaction if interaction is not None and interaction.status != "pending" else None
        if speech is not None:
            if interaction is not None:
                self._interaction_changed(interaction)
                interaction = None
            starter, index, request_id = speech
            accepted = False
            try:
                accepted = bool(
                    starter(
                        index,
                        lambda reason: self.speech_terminal(
                            request_id, reason
                        ),
                    )
                )
            except Exception:
                accepted = False
            if not accepted:
                self.speech_terminal(request_id, "unavailable")
        if interaction is not None:
            self._interaction_changed(interaction)
        self._state_changed()
        return resolved if resolved is not None else True

    def answer(
        self,
        session_handle: str,
        generation: int,
        answers: object,
    ) -> ClaudeInteraction | None:
        """Resolve or hold an exact verbatim answer map."""

        validated: dict[str, str]
        stop_speech = False
        with self._lock:
            item = self._current
            if (
                item is None
                or item.session_handle != session_handle
                or item.generation != generation
                or item.phase in {"answered", "deferred", "expired", "held_failed"}
            ):
                return None
            validated = _validate_answers_for_questions(item.questions, answers)
            if item.request_id is None:
                if float(self._clock()) - item.surfaced_at > self.EARLY_HOLD_SECONDS:
                    self._set_phase_locked(item, "held_failed")
                else:
                    item.held_answers = validated
                    item.held_at = float(self._clock())
                    self._schedule_early_hold_locked(item)
                self._revision = self._next_revision()
                interaction = None
            else:
                broker_item = self._broker.get(item.request_id)
                stop_speech = item.phase == "speaking"
                if broker_item is not None and not broker_item.active:
                    broker_item = self._broker.activate_question(
                        item.request_id,
                        passive_seconds=item.grace_seconds,
                    )
                interaction = (
                    self._broker.resolve_question(item.request_id, validated)
                    if broker_item is not None and broker_item.status == "pending"
                    else broker_item
                )
                # Record what was sent, exactly as the auto-submit path does.
                # The snapshot's `answers` map is the only wire-visible record
                # of an answer this surface produced, and the HUD's sent
                # confirmation reads it to know a receipt is owed. Leaving it
                # empty here made an explicitly submitted answer look identical
                # to one typed straight into Claude Code.
                item.answers = dict(validated)
                self._set_phase_locked(item, "answered")
                self._revision = self._next_revision()
        if stop_speech and self._speech_stop is not None:
            self._speech_stop()
        if interaction is not None:
            self._interaction_changed(interaction)
        self._state_changed()
        return interaction

    def defer(self, session_handle: str, generation: int) -> ClaudeInteraction | None:
        """Make Escape sticky across the PreToolUse/PermissionRequest gap."""

        interaction: ClaudeInteraction | None = None
        stop_speech = False
        with self._lock:
            item = self._current
            if (
                item is None
                or item.session_handle != session_handle
                or item.generation != generation
                or item.phase in {"answered", "deferred", "expired", "held_failed"}
            ):
                return None
            item.defer_sticky = True
            self._cancel_early_timer_locked()
            stop_speech = item.phase == "speaking"
            self._set_phase_locked(item, "deferred")
            if item.request_id is not None:
                interaction = self._broker.defer(item.request_id)
            self._revision = self._next_revision()
        if stop_speech and self._speech_stop is not None:
            self._speech_stop()
        if interaction is not None:
            self._interaction_changed(interaction)
        self._state_changed()
        return interaction

    def can_defer(self) -> bool:
        with self._lock:
            item = self._current
            return bool(
                item is not None
                and item.phase not in {
                    "answered",
                    "deferred",
                    "expired",
                    "held_failed",
                }
            )

    def defer_current(self) -> ClaudeInteraction | None:
        with self._lock:
            item = self._current
            identity = (
                None
                if item is None
                else (item.session_handle, item.generation)
            )
        if identity is None:
            return None
        return self.defer(*identity)

    def complete(self, interaction: ClaudeInteraction) -> None:
        """Apply hook wait expiry/resolution."""

        stop_speech = False
        with self._lock:
            item = self._current
            if item is None or item.request_id != interaction.request_id:
                return
            self._cancel_early_timer_locked()
            stop_speech = item.phase == "speaking"
            if interaction.status == "expired":
                self._set_phase_locked(item, "expired")
            elif interaction.decision is None:
                self._set_phase_locked(item, "deferred")
            else:
                self._set_phase_locked(item, "answered")
            self._revision = self._next_revision()
        if stop_speech and self._speech_stop is not None:
            self._speech_stop()
        self._state_changed()

    def expire_current(self) -> bool:
        """Dismiss and release the current question without answer authority."""

        stop_speech = False
        with self._lock:
            item = self._current
            if item is None or item.phase in {
                "answered",
                "deferred",
                "expired",
                "held_failed",
            }:
                return False
            self._cancel_early_timer_locked()
            stop_speech = item.phase == "speaking"
            self._set_phase_locked(item, "expired")
            self._revision = self._next_revision()
        if stop_speech and self._speech_stop is not None:
            self._speech_stop()
        self._state_changed()
        return True

    def _schedule_early_hold_locked(self, item: _QuestionOpportunity) -> None:
        self._cancel_early_timer_locked()
        remaining = max(
            0.001,
            item.surfaced_at + self.EARLY_HOLD_SECONDS - float(self._clock()),
        )
        timer = threading.Timer(
            remaining,
            self._expire_early_hold,
            args=(item.generation,),
        )
        try:
            timer.daemon = True
            self._early_timer = timer
            timer.start()
        except RuntimeError:
            self._early_timer = None

    def _cancel_early_timer_locked(self) -> None:
        if self._early_timer is not None:
            self._early_timer.cancel()
            self._early_timer = None

    def _expire_early_hold(self, generation: int) -> None:
        with self._lock:
            item = self._current
            if (
                item is None
                or item.generation != generation
                or item.request_id is not None
                or item.held_answers is None
                or item.phase != "surfaced"
            ):
                return
            remaining = (
                item.surfaced_at
                + self.EARLY_HOLD_SECONDS
                - float(self._clock())
            )
            if remaining > 0:
                self._schedule_early_hold_locked(item)
                return
            timer = self._early_timer
            self._early_timer = None
            if timer is not None:
                timer.cancel()
            item.held_answers = None
            self._set_phase_locked(item, "held_failed")
            self._revision = self._next_revision()
        self._state_changed()

    def snapshot(self) -> dict[str, Any]:
        """Return the exact reconnect-safe question view model."""

        with self._lock:
            self._prune_terminal_locked(float(self._clock()))
            item = self._current
            payload: dict[str, Any] | None = None
            if item is not None:
                interaction = (
                    self._broker.get(item.request_id)
                    if item.request_id is not None
                    else None
                )
                now = float(self._clock())
                payload = {
                    "session_handle": item.session_handle,
                    "source_name": item.source_name,
                    "generation": item.generation,
                    "phase": item.phase,
                    "questions": deepcopy(item.questions),
                    "current_index": item.current_index,
                    "request_id": item.request_id,
                    "routable": (
                        item.request_id is not None
                        and item.phase not in {
                            "answered",
                            "deferred",
                            "expired",
                            "held_failed",
                        }
                    ),
                    "capture_available": (
                        item.phase not in {
                            "answered",
                            "deferred",
                            "expired",
                            "held_failed",
                        }
                        and (
                            item.request_id is not None
                            or now - item.surfaced_at
                            <= self.EARLY_HOLD_SECONDS
                        )
                    ),
                    "remaining_seconds": (
                        max(0.0, interaction.effective_deadline - now)
                        if interaction is not None
                        and interaction.status == "pending"
                        and interaction.active
                        else 0.0
                    ),
                    "answers": deepcopy(item.answers or {}),
                }
            return {
                "type": "agent.question.snapshot",
                "version": 1,
                "revision": self._revision,
                "active": payload,
            }


@dataclass(slots=True)
class _ResponseQueueItem:
    completion_id: str
    sequence: int
    generation: int
    grace_seconds: float
    speech_start: (
        Callable[[Callable[[SpeechTerminalReason], None]], bool] | None
    )
    agent: AgentProvider = "claude"
    request_id: str | None = None
    session_handle: str | None = None
    hard_deadline: float | None = None
    waiter_loop: AbstractEventLoop | None = None
    waiter: Future[ClaudeInteraction | None] | None = None
    read_only: bool = False
    phase_deadline: float | None = None
    speech_completed: bool = False


class ClaudeResponseCoordinator:
    """Backend-authoritative, all-session Claude response FIFO.

    One timer owns hard-ceiling expiry, response grace, quiet delay, and
    periodic countdown snapshots. Queue residence consumes only the absolute
    Stop fail-open budget. It never consumes passive reply grace.
    """

    VISUAL_DWELL_SECONDS = 6.0
    SNAPSHOT_TICK_SECONDS = 1.0
    # How long a card whose Stop closed with unsent text stays up read-only,
    # so the user can copy the text. Escape releases it sooner.
    RETAINED_DRAFT_TTL_SECONDS = 120.0
    MAX_DRAFT_CHARS = 50_000

    def __init__(
        self,
        *,
        broker: ClaudeInteractionBroker,
        quiet_delay_provider: Callable[[], float],
        state_changed: Callable[[], None],
        interaction_changed: Callable[[ClaudeInteraction], None],
        completion_terminal: Callable[[str], None],
        chime: Callable[[], None],
        speech_stop: Callable[[], None] | None = None,
        speech_skip_delay: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        next_revision: Callable[[], int] | None = None,
        draft_present: Callable[[str], bool] | None = None,
    ) -> None:
        self._broker = broker
        # Whether the reply owner holds unsent text for a conversation that
        # has no other route. Read at the hard ceiling to keep that card up
        # read-only for copying. Called under this lock; it must not call back.
        self._draft_present = draft_present
        self._quiet_delay_provider = quiet_delay_provider
        self._state_changed = state_changed
        self._interaction_changed_callback = interaction_changed
        self._completion_terminal = completion_terminal
        self._chime = chime
        self._speech_stop = speech_stop
        self._speech_skip_delay = speech_skip_delay
        self._clock = clock
        self._lock = threading.RLock()
        self._waiting: deque[_ResponseQueueItem] = deque()
        self._active: _ResponseQueueItem | None = None
        self._phase: ResponsePhase = "idle"
        self._quiet_deadline: float | None = None
        self._skip_in_progress = False
        self._next_generation = 1
        self._next_revision = next_revision or (lambda: self._revision + 1)
        self._revision = next_revision() if next_revision is not None else 0
        self._timer: ScheduledCall | None = None
        self._scheduler = DeadlineScheduler()
        self._publisher = CoalescingPublisher(state_changed)
        self._closed = False
        self._timer_generation = 0
        self._capture_active = False
        # The user is writing in the open console: new responses wait.
        self._compose_hold = False

    def clear(self) -> None:
        """Fail open waiters and scrub response and reply data."""

        items: list[_ResponseQueueItem] = []
        stop_speech = False
        with self._lock:
            stop_speech = self._phase == "active_speaking"
            if self._active is not None:
                items.append(self._active)
            items.extend(self._waiting)
            for item in items:
                self._resolve_waiter_locked(item, None)
            self._active = None
            self._waiting.clear()
            self._phase = "idle"
            self._quiet_deadline = None
            self._capture_active = False
            self._compose_hold = False
            self._revision = self._next_revision()
            self._schedule_locked(float(self._clock()))
        if stop_speech and self._speech_stop is not None:
            try:
                self._speech_stop()
            except Exception:
                pass
        for item in items:
            self._completion_terminal(item.completion_id)
        self._emit_state()

    def enqueue(
        self,
        *,
        completion_id: str,
        sequence: int,
        grace_seconds: float,
        speech_start: (
            Callable[[Callable[[SpeechTerminalReason], None]], bool] | None
        ),
        agent: AgentProvider = "claude",
        interaction: ClaudeInteraction | None = None,
        waiter_loop: AbstractEventLoop | None = None,
    ) -> Future[ClaudeInteraction | None] | None:
        """Append one response and promote it when the coordinator is idle."""

        completion_id = _require_nonempty_string(
            completion_id, "completion_id"
        )
        agent = _validate_agent(agent)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        if (
            isinstance(grace_seconds, bool)
            or not isinstance(grace_seconds, (int, float))
            or not math.isfinite(grace_seconds)
            or not 0 <= grace_seconds <= MAX_PASSIVE_DEADLINE_SECONDS
        ):
            raise ValueError(
                "grace_seconds must be in "
                f"[0, {MAX_PASSIVE_DEADLINE_SECONDS}]"
            )
        if interaction is not None:
            if interaction.kind != "stop":
                raise ValueError("response queue accepts only Stop interactions")
            if interaction.active:
                raise ValueError("queued Stop interaction must begin inactive")
            if waiter_loop is None:
                raise ValueError("queued Stop interaction requires an event loop")

        waiter: Future[ClaudeInteraction | None] | None = None
        if interaction is not None and waiter_loop is not None:
            waiter = waiter_loop.create_future()

        with self._lock:
            if self._closed:
                if waiter is not None:
                    waiter.set_result(None)
                return waiter
            item = _ResponseQueueItem(
                completion_id=completion_id,
                sequence=sequence,
                generation=self._next_generation,
                grace_seconds=float(grace_seconds),
                speech_start=speech_start,
                agent=agent,
                request_id=(
                    interaction.request_id if interaction is not None else None
                ),
                session_handle=(
                    interaction.session_handle if interaction is not None else None
                ),
                hard_deadline=(
                    interaction.hard_deadline if interaction is not None else None
                ),
                waiter_loop=waiter_loop,
                waiter=waiter,
            )
            self._next_generation += 1
            self._waiting.append(item)
            self._revision = self._next_revision()
            start = self._promote_locked(float(self._clock()))
            self._schedule_locked(float(self._clock()))
        self._emit_state()
        self._run_start(start)
        return waiter

    def interaction_changed(self, interaction: ClaudeInteraction) -> None:
        """Observe a broker mutation and advance the matching active route."""

        terminal_item: _ResponseQueueItem | None = None
        start: _ResponseQueueItem | None = None
        with self._lock:
            item = self._find_request_locked(interaction.request_id)
            if item is None:
                return
            if interaction.status != "pending":
                item.read_only = True
                self._resolve_waiter_locked(item, interaction)
                if item is self._active and self._phase == "active_grace":
                    # A recording into this conversation's draft continues:
                    # the draft belongs to the reply owner, not this Stop.
                    terminal_item = self._finish_active_locked(
                        float(self._clock())
                    )
                    if self._phase == "idle":
                        start = self._promote_locked(float(self._clock()))
            elif (
                item is self._active
                and self._phase == "active_grace"
                and interaction.engaged
            ):
                item.phase_deadline = interaction.hard_deadline
            self._revision = self._next_revision()
            self._schedule_locked(float(self._clock()))
        if terminal_item is not None:
            self._completion_terminal(terminal_item.completion_id)
        self._emit_state()
        self._run_start(start)

    def speech_terminal(
        self,
        completion_id: str,
        generation: int,
        reason: SpeechTerminalReason,
    ) -> None:
        """Finish the exact active speech generation and start full grace."""

        if reason not in {
            "completed",
            "stopped",
            "superseded",
            "failed",
            "unavailable",
        }:
            reason = "failed"
        terminal_item: _ResponseQueueItem | None = None
        interaction_event: ClaudeInteraction | None = None
        with self._lock:
            item = self._active
            if (
                item is None
                or self._phase != "active_speaking"
                or item.completion_id != completion_id
                or item.generation != generation
            ):
                return
            item.speech_completed = reason == "completed"
            item.speech_start = None
            interaction_event, terminal_item = self._begin_grace_locked(
                item, float(self._clock())
            )
            self._revision = self._next_revision()
            self._schedule_locked(float(self._clock()))
        if interaction_event is not None:
            self._interaction_changed_callback(interaction_event)
        if terminal_item is not None:
            self._completion_terminal(terminal_item.completion_id)
        self._emit_state()

    def _live_item_locked(
        self, conversation_handle: object
    ) -> tuple[_ResponseQueueItem, ClaudeInteraction] | None:
        """The active item for one conversation while its Stop is still open."""

        item = self._active
        if (
            item is None
            or self._phase not in {"active_speaking", "active_grace"}
            or item.request_id is None
            or item.session_handle is None
            or item.session_handle != conversation_handle
            or item.read_only
        ):
            return None
        interaction = self._broker.get(item.request_id)
        if interaction is None or interaction.status != "pending":
            return None
        if float(self._clock()) >= interaction.hard_deadline:
            return None
        return item, interaction

    def live_reply_remaining(self, conversation_handle: object) -> float | None:
        """Seconds left on the open Stop for this conversation, if any."""

        with self._lock:
            live = self._live_item_locked(conversation_handle)
            if live is None:
                return None
            return max(0.0, live[1].hard_deadline - float(self._clock()))

    def active_conversation(self) -> str | None:
        """The conversation the visible response belongs to, if any."""

        with self._lock:
            item = self._active
            if item is None or self._phase not in {"active_speaking", "active_grace"}:
                return None
            return item.session_handle

    def engage(self, conversation_handle: object) -> float | None:
        """Hold the open Stop for a reply the user started (voice or typing).

        Activates and engages the exact Stop, stops speech, and moves the
        card's dwell to the hook's hard ceiling. Engagement never extends that
        ceiling. Returns the seconds left, or None when no Stop is open for
        this conversation. Draft text is not this coordinator's concern.
        """

        interaction_event: ClaudeInteraction | None = None
        with self._lock:
            live = self._live_item_locked(conversation_handle)
            if live is None:
                return None
            item, interaction = live
            if interaction.engaged and self._phase == "active_grace":
                return max(0.0, interaction.hard_deadline - float(self._clock()))
            if not interaction.active:
                interaction = self._broker.activate_stop(
                    item.request_id, passive_seconds=item.grace_seconds,
                )
            if interaction is None or interaction.status != "pending":
                return None
            engaged = self._broker.engage(item.request_id)
            if engaged is None or not engaged.engaged:
                return None
            interaction_event = engaged
            stop_speech = self._phase == "active_speaking"
            self._phase = "active_grace"
            item.speech_start = None
            item.phase_deadline = engaged.hard_deadline
            self._revision = self._next_revision()
            self._schedule_locked(float(self._clock()))
            remaining = max(0.0, engaged.hard_deadline - float(self._clock()))
            # stop() is non-blocking and its callbacks reenter this RLock.
            # Keep the scheduling edge serialized: a simultaneous Escape must
            # not promote the next voice before the old stop has finished.
            if stop_speech and self._speech_stop is not None:
                try:
                    self._speech_stop()
                except Exception:
                    pass
        self._interaction_changed_callback(interaction_event)
        self._emit_state()
        return remaining

    def resolve_reply(
        self,
        conversation_handle: object,
        text: object,
        *,
        on_resolved: Callable[[ClaudeInteraction], None] | None = None,
    ) -> ClaudeInteraction | None:
        """Deliver one reply through this conversation's open Stop, once.

        Returns None when there is no open Stop or its delivery handler
        refused (wrapped sessions route the hook through the guarded PTY
        slot); the caller records the refusal and keeps the text.
        ``on_resolved`` runs after the broker resolves and before this
        coordinator publishes the terminal state, so a receipt for the reply
        always reaches the HUD while its card is still showing.
        """

        if not isinstance(text, str) or not text.strip() or len(text) > self.MAX_DRAFT_CHARS:
            return None
        with self._lock:
            live = self._live_item_locked(conversation_handle)
            if live is None:
                return None
            item, _interaction = live
            interaction = self._broker.resolve_reply(item.request_id, text)
            if (
                interaction is None
                or interaction.status != "resolved"
                or not isinstance(interaction.decision, str)
            ):
                return None
            self._revision = self._next_revision()
        if on_resolved is not None:
            try:
                on_resolved(interaction)
            except Exception:
                pass
        self.interaction_changed(interaction)
        return interaction

    def is_speaking(self) -> bool:
        """The foreground response is being read aloud right now."""

        with self._lock:
            return self._phase == "active_speaking"

    def can_dismiss_responses(self) -> bool:
        """Fast predicate for selective global Escape capture."""

        with self._lock:
            return bool(self._active is not None or self._waiting)

    def skip_current(self) -> bool:
        """Release only the foreground response, then promote without quiet delay.

        Promotion stays barred while cancellation callbacks run. In particular,
        a capture-idle notification or a new enqueue must not start a successor
        before the old voice receives its stop.
        """
        interaction = None
        with self._lock:
            if self._skip_in_progress:
                return False
            item = self._active
            if item is None and not self._waiting:
                return False
            self._skip_in_progress = True
            stop_speech = item is not None and self._phase == "active_speaking"
            if item is not None and item.request_id is not None:
                interaction = self._broker.close(item.request_id)
                self._resolve_waiter_locked(item, interaction)
            self._active = None
            self._phase = "idle"
            self._quiet_deadline = None
            self._revision = self._next_revision()
        try:
            if stop_speech and self._speech_stop is not None:
                try:
                    self._speech_stop()
                except Exception:
                    pass
            # The speech FIFO has its own pacing clock. Reset that clock only
            # after cancellation, before the successor can be scheduled.
            if self._speech_skip_delay is not None:
                self._speech_skip_delay()
            if interaction is not None:
                self._interaction_changed_callback(interaction)
            if item is not None:
                self._completion_terminal(item.completion_id)
        finally:
            with self._lock:
                self._skip_in_progress = False
                start = self._promote_locked(float(self._clock()))
                self._schedule_locked(float(self._clock()))
            self._emit_state()
            self._run_start(start)
        return True

    def dismiss_responses(self) -> bool:
        """Dismiss the active response and flush every queued passive Stop."""

        interactions: list[ClaudeInteraction] = []
        terminal_items: list[_ResponseQueueItem] = []
        stop_speech = False
        with self._lock:
            if self._active is None and not self._waiting:
                return False
            active = self._active
            if active is not None:
                stop_speech = self._phase == "active_speaking"
                if active.request_id is not None:
                    closed = self._broker.close(active.request_id)
                    if closed is not None:
                        interactions.append(closed)
                    self._resolve_waiter_locked(active, closed)
                terminal_items.append(active)

            for item in self._waiting:
                if item.request_id is not None:
                    closed = self._broker.close(item.request_id)
                    if closed is not None:
                        interactions.append(closed)
                    self._resolve_waiter_locked(item, closed)
                terminal_items.append(item)

            self._active = None
            self._waiting.clear()
            self._phase = "idle"
            self._quiet_deadline = None
            self._capture_active = False
            self._compose_hold = False
            self._revision = self._next_revision()
            self._schedule_locked(float(self._clock()))

        if stop_speech and self._speech_stop is not None:
            try:
                self._speech_stop()
            except Exception:
                pass
        for interaction in interactions:
            self._interaction_changed_callback(interaction)
        for item in terminal_items:
            self._completion_terminal(item.completion_id)
        self._emit_state()
        return True

    def set_capture_active(self, active: bool) -> None:
        """Hold promotion while a routed capture is active."""

        self._set_hold("_capture_active", active)

    def set_compose_hold(self, active: bool) -> None:
        """Hold promotion while the user writes in the open console."""

        self._set_hold("_compose_hold", active)

    def _held(self) -> bool:
        return self._capture_active or self._compose_hold

    def _set_hold(self, name: str, active: bool) -> None:
        if not isinstance(active, bool):
            return
        start: _ResponseQueueItem | None = None
        with self._lock:
            if getattr(self, name) == active:
                return
            setattr(self, name, active)
            if not self._held():
                now = float(self._clock())
                if self._phase == "quiet_delay":
                    start = self._maybe_complete_quiet_locked(now)
                elif self._phase == "idle":
                    # A response that arrived while held was never promoted.
                    start = self._promote_locked(now)
            self._revision = self._next_revision()
            self._schedule_locked(float(self._clock()))
        self._emit_state()
        self._run_start(start)

    def snapshot(self) -> dict[str, Any]:
        """Return the privacy-safe v1 lifecycle snapshot."""

        with self._lock:
            now = float(self._clock())
            active = self._active
            ordered = (
                ([] if active is None else [active.completion_id])
                + [item.completion_id for item in self._waiting]
            )
            active_payload: dict[str, object] | None = None
            if active is not None and self._phase in {
                "active_speaking",
                "active_grace",
            }:
                interaction = (
                    self._broker.get(active.request_id)
                    if active.request_id is not None
                    else None
                )
                passive_remaining = (
                    max(0.0, interaction.passive_deadline - now)
                    if interaction is not None
                    and interaction.active
                    and interaction.status == "pending"
                    else max(0.0, (active.phase_deadline or now) - now)
                    if self._phase == "active_grace"
                    else 0.0
                )
                hard_remaining = (
                    max(0.0, active.hard_deadline - now)
                    if active.hard_deadline is not None
                    else 0.0
                )
                active_payload = {
                    "completion_id": active.completion_id,
                    "phase": self._phase,
                    "request_id": active.request_id,
                    "session_handle": active.session_handle,
                    "generation": active.generation,
                    "engaged": bool(
                        interaction is not None and interaction.engaged
                    ),
                    "routable": bool(
                        interaction is not None
                        and interaction.status == "pending"
                        and interaction.active
                        and not active.read_only
                    ),
                    "passive_total_seconds": active.grace_seconds,
                    "passive_remaining_seconds": passive_remaining,
                    "hard_remaining_seconds": hard_remaining,
                    "speech_completed": active.speech_completed,
                }
                if active.agent != "claude":
                    active_payload["agent"] = active.agent
            quiet_remaining = (
                max(0.0, self._quiet_deadline - now)
                if self._phase == "quiet_delay"
                and self._quiet_deadline is not None
                else 0.0
            )
            return {
                "version": 1,
                "revision": self._revision,
                "phase": self._phase,
                "ordered_completion_ids": ordered,
                "active": active_payload,
                "quiet_delay_remaining_seconds": quiet_remaining,
            }

    def _promote_locked(
        self, now: float
    ) -> _ResponseQueueItem | None:
        if (
            self._active is not None
            or self._skip_in_progress
            or self._phase != "idle"
            or self._held()
            or not self._waiting
        ):
            return None
        item = self._waiting.popleft()
        self._active = item

        interaction_event: ClaudeInteraction | None = None
        if item.request_id is not None:
            interaction = self._broker.get(item.request_id)
            if interaction is None or interaction.status != "pending":
                item.read_only = True
                self._resolve_waiter_locked(item, interaction)
            elif (
                item.hard_deadline is None
                or item.hard_deadline - now < item.grace_seconds
            ):
                item.read_only = True
                interaction_event = self._broker.close(item.request_id)
                self._resolve_waiter_locked(item, interaction_event)

        if item.speech_start is not None:
            self._phase = "active_speaking"
            item.phase_deadline = None
            if interaction_event is not None:
                self._interaction_changed_callback(interaction_event)
            return item

        interaction_event, terminal_item = self._begin_grace_locked(item, now)
        if interaction_event is not None:
            self._interaction_changed_callback(interaction_event)
        if terminal_item is not None:
            self._completion_terminal(terminal_item.completion_id)
        return None

    def _begin_grace_locked(
        self,
        item: _ResponseQueueItem,
        now: float,
    ) -> tuple[ClaudeInteraction | None, _ResponseQueueItem | None]:
        interaction_event: ClaudeInteraction | None = None
        duration = self.VISUAL_DWELL_SECONDS
        if item.request_id is not None and not item.read_only:
            interaction = self._broker.get(item.request_id)
            if interaction is not None and interaction.status == "pending":
                interaction_event = self._broker.activate_stop(
                    item.request_id,
                    passive_seconds=item.grace_seconds,
                )
                if (
                    interaction_event is not None
                    and interaction_event.status == "pending"
                    and interaction_event.active
                ):
                    duration = max(
                        0.0,
                        interaction_event.passive_deadline - now,
                    )
                else:
                    item.read_only = True
                    self._resolve_waiter_locked(item, interaction_event)

        self._phase = "active_grace"
        item.phase_deadline = now + (
            duration if duration > 0 else self.VISUAL_DWELL_SECONDS
        )
        return interaction_event, None

    def _finish_active_locked(
        self, now: float
    ) -> _ResponseQueueItem | None:
        item = self._active
        if item is None:
            return None
        self._active = None
        item.phase_deadline = None
        delay = self._quiet_delay()
        if delay > 0 or self._held():
            self._phase = "quiet_delay"
            self._quiet_deadline = now + delay
        else:
            self._phase = "idle"
            self._quiet_deadline = None
        return item

    def _maybe_complete_quiet_locked(
        self, now: float
    ) -> _ResponseQueueItem | None:
        if (
            self._phase != "quiet_delay"
            or self._held()
            or (
                self._quiet_deadline is not None
                and now < self._quiet_deadline
            )
        ):
            return None
        self._phase = "idle"
        self._quiet_deadline = None
        return self._promote_locked(now)

    def _on_timer(self, generation: int) -> None:
        interactions: list[ClaudeInteraction] = []
        terminal_items: list[_ResponseQueueItem] = []
        start: _ResponseQueueItem | None = None
        with self._lock:
            if self._closed or generation != self._timer_generation:
                return
            self._timer = None
            now = float(self._clock())

            for item in list(self._waiting):
                if (
                    item.request_id is None
                    or item.read_only
                    or item.hard_deadline is None
                    or now < item.hard_deadline
                ):
                    continue
                item.read_only = True
                closed = self._broker.close(item.request_id)
                if closed is not None:
                    interactions.append(closed)
                self._resolve_waiter_locked(item, closed)

            active = self._active
            if (
                active is not None
                and active.request_id is not None
                and not active.read_only
                and active.hard_deadline is not None
                and now >= active.hard_deadline
            ):
                active.read_only = True
                closed = self._broker.close(active.request_id)
                if closed is not None:
                    interactions.append(closed)
                self._resolve_waiter_locked(active, closed)
                # Unsent text with no other route: keep the card up read-only
                # (bounded) so the user can copy it, instead of vanishing.
                if (
                    self._phase == "active_grace"
                    and active.session_handle is not None
                    and self._draft_present is not None
                ):
                    try:
                        keep = bool(self._draft_present(active.session_handle))
                    except Exception:
                        keep = False
                    if keep:
                        active.phase_deadline = now + self.RETAINED_DRAFT_TTL_SECONDS

            if (
                active is not None
                and self._phase == "active_grace"
                and active.phase_deadline is not None
                and now >= active.phase_deadline
            ):
                if active.request_id is not None and not active.read_only:
                    closed = self._broker.close(active.request_id)
                    active.read_only = True
                    if closed is not None:
                        interactions.append(closed)
                    self._resolve_waiter_locked(active, closed)
                finished = self._finish_active_locked(now)
                if finished is not None:
                    terminal_items.append(finished)
                if self._phase == "idle":
                    start = self._promote_locked(now)

            if self._phase == "quiet_delay" and start is None:
                start = self._maybe_complete_quiet_locked(now)

            self._revision = self._next_revision()
            self._schedule_locked(now)

        for interaction in interactions:
            self._interaction_changed_callback(interaction)
        for item in terminal_items:
            self._completion_terminal(item.completion_id)
        self._emit_state()
        self._run_start(start)

    def _schedule_locked(self, now: float) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._timer_generation += 1
        if self._closed:
            return
        # Schedule only deadlines that _on_timer can act on. An expired deadline
        # that is held (a read-only response still speaking, or quiet delay
        # held by a capture or compose hold) would otherwise re-arm at the 1 ms
        # floor on every callback. Held states keep the periodic snapshot tick,
        # and the release paths (speech_terminal, _set_hold) reschedule.
        due: list[float] = []
        if self._active is not None or self._waiting or self._phase != "idle":
            due.append(now + self.SNAPSHOT_TICK_SECONDS)
        if self._active is not None:
            if (
                self._active.hard_deadline is not None
                and self._active.request_id is not None
                and not self._active.read_only
            ):
                due.append(self._active.hard_deadline)
            if (
                self._phase == "active_grace"
                and self._active.phase_deadline is not None
            ):
                due.append(self._active.phase_deadline)
        due.extend(
            item.hard_deadline
            for item in self._waiting
            if item.hard_deadline is not None
            and item.request_id is not None
            and not item.read_only
        )
        if (
            self._phase == "quiet_delay"
            and self._quiet_deadline is not None
            and not self._held()
        ):
            due.append(self._quiet_deadline)
        if not due:
            return
        generation = self._timer_generation
        delay = max(0.001, min(due) - now)
        try:
            self._timer = self._scheduler.schedule(delay, lambda: self._on_timer(generation))
        except RuntimeError:
            self._timer = None

    def _run_start(self, item: _ResponseQueueItem | None) -> None:
        # Serialize the scheduling edge with skip/typed engagement. Otherwise
        # a delayed enqueue could start a response after Escape retired it.
        with self._lock:
            if self._closed or item is None or item is not self._active or self._phase != "active_speaking":
                return
            callback = lambda reason: self.speech_terminal(item.completion_id, item.generation, reason)
            try:
                scheduled = item.speech_start(callback) is True if item.speech_start else False
            except Exception:
                scheduled = False
            if not scheduled:
                try:
                    self._chime()
                except Exception:
                    pass
                finally:
                    callback("unavailable")

    def _find_request_locked(
        self, request_id: str
    ) -> _ResponseQueueItem | None:
        if self._active is not None and self._active.request_id == request_id:
            return self._active
        return next(
            (
                item
                for item in self._waiting
                if item.request_id == request_id
            ),
            None,
        )

    @staticmethod
    def _set_future_result(
        future: Future[ClaudeInteraction | None],
        interaction: ClaudeInteraction | None,
    ) -> None:
        if not future.done():
            future.set_result(interaction)

    def _resolve_waiter_locked(
        self,
        item: _ResponseQueueItem,
        interaction: ClaudeInteraction | None,
    ) -> None:
        if item.waiter is None or item.waiter_loop is None or item.waiter.done():
            return
        try:
            item.waiter_loop.call_soon_threadsafe(
                self._set_future_result,
                item.waiter,
                interaction,
            )
        except RuntimeError:
            # Persistent Stop requests can finish before their independent HUD
            # response expires. A closed request loop has no live waiter left.
            if not item.waiter_loop.is_closed():
                raise
            item.waiter = None
            item.waiter_loop = None

    def _quiet_delay(self) -> float:
        try:
            raw = self._quiet_delay_provider()
        except Exception:
            return 2.0
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(raw)
            or not 0 <= raw <= 10
        ):
            return 2.0
        return float(raw)

    def _emit_state(self) -> None:
        self._publisher.submit()

    def close(self) -> None:
        """Retire workers and release held replies without waiting for publication."""
        with self._lock:
            self._closed = True
            self._timer_generation += 1
            self._scheduler.close()
            self._publisher.close()
        self.clear()


def _require_nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_kind(kind: object) -> InteractionKind:
    if kind not in _KINDS:
        raise ValueError(f"unsupported interaction kind: {kind!r}")
    return kind  # type: ignore[return-value]


def _validate_agent(agent: object) -> AgentProvider:
    if agent not in _AGENTS:
        raise ValueError(f"unsupported interaction agent: {agent!r}")
    return agent  # type: ignore[return-value]


def _validate_passive_seconds(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        or value > MAX_PASSIVE_DEADLINE_SECONDS
    ):
        raise ValueError(
            "passive_seconds must be in "
            f"(0, {MAX_PASSIVE_DEADLINE_SECONDS}]"
        )
    return float(value)


def _validate_decision(
    kind: InteractionKind,
    decision: object,
) -> str | dict[str, str]:
    if kind == "stop":
        if not isinstance(decision, str) or not decision.strip():
            raise ValueError("Stop reply must be a non-empty string")
        return decision

    if kind == "permission":
        if decision not in _PERMISSION_DECISIONS:
            raise ValueError("permission decision must be 'allow' or 'deny'")
        return decision  # type: ignore[return-value]

    if not isinstance(decision, Mapping) or not decision:
        raise ValueError("question answers must be a non-empty mapping")
    answers: dict[str, str] = {}
    for question, answer in decision.items():
        if not isinstance(question, str) or not question:
            raise ValueError("question answer keys must be non-empty strings")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("question answers must be non-empty strings")
        answers[question] = answer
    return answers


def _validate_answers_for_questions(
    questions: list[dict[str, Any]],
    answers: object,
) -> dict[str, str]:
    """Validate one complete map against Claude's verbatim question keys."""

    validated = _validate_decision("question", answers)
    assert isinstance(validated, dict)
    expected = [
        item.get("question")
        for item in questions
        if isinstance(item, dict) and isinstance(item.get("question"), str)
    ]
    if len(expected) != len(questions) or set(validated) != set(expected):
        raise ValueError("question answers must match every verbatim question")
    return validated


# Provider-neutral names for new integrations. The Claude-prefixed exports stay
# available so existing imports and tests remain compatible.
AgentInteraction = ClaudeInteraction
AgentInteractionBroker = ClaudeInteractionBroker
AgentQuestionCoordinator = ClaudeQuestionCoordinator
AgentResponseCoordinator = ClaudeResponseCoordinator
