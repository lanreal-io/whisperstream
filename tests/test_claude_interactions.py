"""Tests for the condition-backed Claude interaction broker."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from whisperstream.claude_interactions import (
    ClaudeQuestionCoordinator,
    ClaudeResponseCoordinator,
    ENGAGED_CEILING_SECONDS,
    MAX_PASSIVE_DEADLINE_SECONDS,
    PASSIVE_DEADLINE_SECONDS,
    ClaudeInteractionBroker,
)


class ManualClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.value = initial

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _ids(*values: str):
    iterator = iter(values)
    return lambda: next(iterator)


QUESTION_BATCH = [
    {
        "question": "Which release channel?",
        "header": "Channel",
        "options": [
            {"label": "Stable", "description": "Ship broadly"},
            {"label": "Beta", "description": "Test first"},
        ],
        "multiSelect": False,
    }
]


def _question_coordinator(clock: ManualClock):
    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=_ids("question-request"),
    )
    states: list[dict] = []
    interactions = []
    coordinator = ClaudeQuestionCoordinator(
        broker=broker,
        state_changed=lambda: states.append(coordinator.snapshot()),
        interaction_changed=interactions.append,
        clock=clock,
    )
    return broker, coordinator, states, interactions


def test_question_grace_starts_only_after_exact_speech_terminal() -> None:
    clock = ManualClock()
    broker, coordinator, _, interactions = _question_coordinator(clock)
    generation = coordinator.surface("session", QUESTION_BATCH)
    interaction = broker.create(
        "session",
        "question",
        passive_seconds=6,
        active=False,
    )
    terminals = []

    coordinator.attach(
        interaction,
        QUESTION_BATCH,
        grace_seconds=6,
        speech_start=lambda index, callback: (
            terminals.append((index, callback)) or True
        ),
    )

    assert generation == 1
    assert coordinator.snapshot()["active"]["phase"] == "speaking"
    assert broker.get(interaction.request_id).active is False
    clock.advance(20)
    assert broker.get(interaction.request_id).status == "pending"

    terminals[0][1]("completed")
    active = broker.get(interaction.request_id)
    assert active.active is True
    assert active.passive_deadline == pytest.approx(clock.value + 6)
    assert coordinator.snapshot()["active"]["phase"] == "grace"
    assert interactions[-1].request_id == interaction.request_id


def test_early_question_answer_is_held_for_five_seconds_and_maps_exact_keys() -> None:
    clock = ManualClock()
    broker, coordinator, _, _ = _question_coordinator(clock)
    generation = coordinator.surface("session", QUESTION_BATCH)
    assert coordinator.answer(
        "session",
        generation,
        {"Which release channel?": "Beta"},
    ) is None
    clock.advance(4.9)
    interaction = broker.create(
        "session",
        "question",
        passive_seconds=6,
        active=False,
    )

    attached = coordinator.attach(
        interaction,
        QUESTION_BATCH,
        grace_seconds=6,
        speech_start=None,
    )

    assert attached.status == "resolved"
    assert attached.decision == {"Which release channel?": "Beta"}
    assert coordinator.snapshot()["active"]["phase"] == "answered"


def test_answered_snapshot_records_what_this_surface_sent() -> None:
    # The snapshot's `answers` map is the only wire-visible record that an
    # answer came from the HUD, and it is what the sent confirmation reads to
    # decide a receipt is owed. The auto-submit path already wrote it; the
    # explicit answer path did not, so an explicitly submitted answer looked
    # identical to one typed straight into Claude Code. A question answered
    # natively still leaves it empty, which is the distinction worth keeping.
    clock = ManualClock()
    broker, coordinator, _, _ = _question_coordinator(clock)
    coordinator.surface("session", QUESTION_BATCH)
    interaction = broker.create(
        "session",
        "question",
        passive_seconds=6,
        active=False,
    )
    coordinator.attach(
        interaction,
        QUESTION_BATCH,
        grace_seconds=6,
        speech_start=None,
    )
    active = coordinator.snapshot()["active"]

    coordinator.answer(
        active["session_handle"],
        active["generation"],
        {"Which release channel?": "Beta"},
    )

    answered = coordinator.snapshot()["active"]
    assert answered["phase"] == "answered"
    assert answered["answers"] == {"Which release channel?": "Beta"}


def test_expired_early_answer_and_sticky_defer_fail_open() -> None:
    clock = ManualClock()
    broker, coordinator, _, _ = _question_coordinator(clock)
    generation = coordinator.surface("session", QUESTION_BATCH)
    coordinator.answer(
        "session",
        generation,
        {"Which release channel?": "Beta"},
    )
    clock.advance(5.1)
    coordinator._expire_early_hold(generation)
    assert coordinator.snapshot()["active"]["phase"] == "held_failed"
    interaction = broker.create(
        "session",
        "question",
        passive_seconds=6,
        active=False,
    )
    failed = coordinator.attach(
        interaction,
        QUESTION_BATCH,
        grace_seconds=6,
        speech_start=None,
    )
    assert failed.status == "expired"
    assert coordinator.snapshot()["active"]["phase"] == "held_failed"

    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=_ids("deferred-request"),
    )
    coordinator = ClaudeQuestionCoordinator(
        broker=broker,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        clock=clock,
    )
    generation = coordinator.surface("session", QUESTION_BATCH)
    assert coordinator.defer("session", generation) is None
    parked = broker.create(
        "session",
        "question",
        passive_seconds=6,
        active=False,
    )
    deferred = coordinator.attach(
        parked,
        QUESTION_BATCH,
        grace_seconds=6,
        speech_start=None,
    )
    assert deferred.status == "resolved"
    assert deferred.decision is None


def test_terminal_question_ack_is_exact_and_scrubs_snapshot() -> None:
    clock = ManualClock()
    _, coordinator, _, _ = _question_coordinator(clock)
    generation = coordinator.surface("session", QUESTION_BATCH)
    coordinator.defer("session", generation)

    assert coordinator.acknowledge("other-session", generation) is False
    assert coordinator.acknowledge("session", generation + 1) is False
    assert coordinator.snapshot()["active"]["phase"] == "deferred"
    assert coordinator.acknowledge("session", generation) is True
    assert coordinator.snapshot()["active"] is None


def test_terminal_question_ttl_and_clear_scrub_all_content() -> None:
    clock = ManualClock()
    _, coordinator, states, _ = _question_coordinator(clock)
    generation = coordinator.surface("session", QUESTION_BATCH)
    coordinator.defer("session", generation)
    assert coordinator._terminal_timer is not None

    clock.advance(coordinator.TERMINAL_TTL_SECONDS)
    coordinator._expire_terminal(generation)
    assert coordinator.snapshot()["active"] is None

    next_generation = coordinator.surface("session-two", QUESTION_BATCH)
    coordinator.answer(
        "session-two",
        next_generation,
        {"Which release channel?": "Beta"},
    )
    coordinator.clear()
    assert coordinator.snapshot()["active"] is None
    assert coordinator._early_timer is None
    assert coordinator._terminal_timer is None
    assert states[-1]["active"] is None


def test_question_capture_engages_and_answers_with_owner_text() -> None:
    clock = ManualClock()
    broker, coordinator, _, _ = _question_coordinator(clock)
    generation = coordinator.surface("session", QUESTION_BATCH)
    interaction = broker.create(
        "session",
        "question",
        passive_seconds=6,
        active=False,
    )
    coordinator.attach(
        interaction,
        QUESTION_BATCH,
        grace_seconds=6,
        speech_start=None,
    )
    # The capture itself is the reply owner's; the question only engages.
    remaining = coordinator.begin_capture()
    assert remaining is not None and remaining > 0
    assert broker.get(interaction.request_id).engaged is True
    assert coordinator.snapshot()["active"]["phase"] == "grace"
    assert "compose_generation" not in coordinator.snapshot()["active"]
    # A stale generation is refused.
    assert coordinator.submit_text("session", generation + 1, "Beta") is None

    resolved = coordinator.submit_text("session", generation, "Beta")

    assert resolved.status == "resolved"
    assert resolved.decision == {"Which release channel?": "Beta"}


def test_question_capture_before_the_park_attaches_without_speech() -> None:
    """An answer started before PermissionRequest goes straight to grace."""
    clock = ManualClock()
    broker, coordinator, _, _ = _question_coordinator(clock)
    coordinator.surface("session", QUESTION_BATCH)
    assert coordinator.begin_capture() is not None
    interaction = broker.create(
        "session",
        "question",
        passive_seconds=6,
        active=False,
    )
    spoken = []
    coordinator.attach(
        interaction,
        QUESTION_BATCH,
        grace_seconds=6,
        speech_start=lambda index, callback: spoken.append(index) or True,
    )
    assert spoken == []
    assert broker.get(interaction.request_id).active is True


def test_multi_question_voice_advances_and_restarts_grace_after_each_speech() -> None:
    clock = ManualClock()
    questions = QUESTION_BATCH + [
        {
            "question": "Which verification group?",
            "header": "Checks",
            "options": [{"label": "Backend", "description": "Run pytest"}],
            "multiSelect": False,
        }
    ]
    broker, coordinator, _, _ = _question_coordinator(clock)
    coordinator.surface("session", questions)
    interaction = broker.create(
        "session",
        "question",
        passive_seconds=6,
        active=False,
    )
    speech_callbacks = []
    coordinator.attach(
        interaction,
        questions,
        grace_seconds=6,
        speech_start=lambda index, callback: (
            speech_callbacks.append((index, callback)) or True
        ),
    )
    speech_callbacks.pop(0)[1]("completed")
    assert coordinator.begin_capture() is not None
    session, generation = coordinator.current_target()

    assert coordinator.submit_text(session, generation, "Beta") is True
    assert coordinator.snapshot()["active"]["current_index"] == 1
    assert coordinator.snapshot()["active"]["phase"] == "speaking"
    assert broker.get(interaction.request_id).active is False
    assert speech_callbacks[0][0] == 1

    clock.advance(3)
    speech_callbacks.pop(0)[1]("completed")
    active = broker.get(interaction.request_id)
    assert active.passive_deadline == pytest.approx(clock.value + 6)
    assert coordinator.begin_capture() is not None
    resolved = coordinator.submit_text(session, generation, "Backend")

    assert resolved.decision == {
        "Which release channel?": "Beta",
        "Which verification group?": "Backend",
    }


def test_create_uses_locked_deadlines_and_privacy_safe_wire() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=_ids("opaque-request"),
    )

    interaction = broker.create(
        "opaque-session",
        "stop",
        raw_session_id="raw-claude-session",
    )

    assert interaction.request_id == "opaque-request"
    assert interaction.created_at == 100.0
    assert interaction.passive_deadline == 106.0
    assert interaction.hard_deadline == 220.0
    assert interaction.status == "pending"
    assert interaction.engaged is False
    wire = interaction.to_dict(now=clock())
    assert wire == {
        "request_id": "opaque-request",
        "session_handle": "opaque-session",
        "kind": "stop",
            "sequence": 1,
            "status": "pending",
            "active": True,
            "engaged": False,
        "passive_seconds": 6.0,
        "remaining_seconds": 6.0,
        "hard_remaining_seconds": 120.0,
        "source_name": None,
    }
    assert "decision" not in wire
    assert "raw_session_id" not in wire
    assert "raw-claude-session" not in repr(wire)
    assert (
        broker.raw_session_id_for(interaction.request_id)
        == "raw-claude-session"
    )


def test_engagement_before_passive_deadline_uses_absolute_hard_ceiling() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock)
    interaction = broker.create("session", "stop")

    clock.advance(5.5)
    engaged = broker.engage(interaction.request_id)
    assert engaged is not None
    assert engaged.engaged is True
    assert engaged.effective_deadline == interaction.created_at + 120.0

    clock.advance(114.4)
    assert broker.get(interaction.request_id).status == "pending"
    clock.advance(0.1)
    assert broker.expire(interaction.request_id).status == "expired"


@pytest.mark.parametrize("passive_seconds", range(1, 31))
def test_create_accepts_every_configured_reply_grace(
    passive_seconds: int,
) -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock)

    interaction = broker.create(
        "session",
        "stop",
        passive_seconds=passive_seconds,
    )

    assert interaction.passive_seconds == float(passive_seconds)
    assert interaction.passive_deadline == 100.0 + passive_seconds
    assert interaction.hard_deadline == 220.0


def test_create_and_enqueue_reject_passive_grace_exceeding_ceiling() -> None:
    broker = ClaudeInteractionBroker()
    with pytest.raises(ValueError, match=r"passive_seconds must be in \(0, 30\.0\]"):
        broker.create("session", "stop", passive_seconds=31)

    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 2.0,
        state_changed=lambda: None,
        interaction_changed=lambda _item: None,
        completion_terminal=lambda _id: None,
        chime=lambda: None,
    )
    with pytest.raises(ValueError, match=r"grace_seconds must be in \[0, 30\.0\]"):
        coordinator.enqueue(
            completion_id="comp-1",
            sequence=1,
            speech_start=lambda _cb: True,
            interaction=broker.create("session", "stop", passive_seconds=6),
            waiter_loop=None,
            grace_seconds=31,
        )


def test_late_engagement_expires_instead_of_extending() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock)
    interaction = broker.create("session", "question")

    clock.advance(6.0)
    late = broker.engage(interaction.request_id)

    assert late is not None
    assert late.status == "expired"
    assert late.engaged is False


def test_first_resolve_wins_and_duplicate_resolves_return_original_result() -> None:
    broker = ClaudeInteractionBroker()
    interaction = broker.create("session", "stop")

    first = broker.resolve_reply(interaction.request_id, "first reply")
    duplicate = broker.resolve_reply(interaction.request_id, "different reply")

    assert first is not None
    assert duplicate == first
    assert first.status == "resolved"
    assert first.decision == "first reply"


def test_permission_lives_to_the_hard_ceiling_not_the_passive_grace() -> None:
    """A permission ask has no passive phase: it is the ask itself.

    Nothing in the frontend ever engages an interaction, so before this the
    permission pill expired at the shared six second passive default and
    vanished from the HUD while Claude was still blocked on it.
    """

    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock)
    permission = broker.create("session", "permission")
    stop = broker.create("session", "stop")

    assert permission.effective_deadline == permission.hard_deadline
    assert permission.to_dict(now=clock.value)["remaining_seconds"] == pytest.approx(
        ENGAGED_CEILING_SECONDS
    )

    clock.advance(PASSIVE_DEADLINE_SECONDS + 1.0)

    still_pending = broker.get(permission.request_id)
    assert still_pending is not None
    assert still_pending.status == "pending"
    # The shared passive default is untouched for every other kind.
    assert broker.get(stop.request_id).status == "expired"

    clock.advance(ENGAGED_CEILING_SECONDS)

    expired = broker.get(permission.request_id)
    assert expired is not None
    assert expired.status == "expired"
    assert expired.decision is None


def test_permission_interaction_carries_resolved_source_name() -> None:
    """The permission pill shows the name (scope decision 3); the broker

    stores whatever the hook seam already resolved and sanitized, and
    ``to_dict`` mirrors the question snapshot's always-present field.
    """

    broker = ClaudeInteractionBroker()
    permission = broker.create("session", "permission", source_name="pay kit")

    assert permission.source_name == "pay kit"
    assert permission.to_dict(now=0.0)["source_name"] == "pay kit"


def test_interaction_source_name_defaults_to_none() -> None:
    broker = ClaudeInteractionBroker()
    permission = broker.create("session", "permission")

    assert permission.source_name is None
    assert permission.to_dict(now=0.0)["source_name"] is None


def test_interaction_source_name_ignores_non_string_and_empty() -> None:
    broker = ClaudeInteractionBroker()

    empty = broker.create("session-a", "permission", source_name="")
    assert empty.source_name is None

    non_string = broker.create("session-b", "permission", source_name=123)
    assert non_string.source_name is None


def test_question_and_permission_decisions_are_kind_checked_and_immutable() -> None:
    broker = ClaudeInteractionBroker()
    question = broker.create("session", "question")
    answers = {"Exact question?": "A, B"}
    question_result = broker.resolve_question(question.request_id, answers)
    answers["Exact question?"] = "mutated"

    assert question_result is not None
    assert question_result.decision == {"Exact question?": "A, B"}
    question_result.decision["Exact question?"] = "snapshot mutation"
    assert broker.get(question.request_id).decision == {
        "Exact question?": "A, B"
    }

    permission = broker.create("session", "permission")
    permission_result = broker.resolve_permission(permission.request_id, "deny")
    assert permission_result is not None
    assert permission_result.decision == "deny"

    with pytest.raises(ValueError):
        broker.resolve_permission(
            broker.create("session", "permission").request_id,
            "always_allow",
        )
    with pytest.raises(ValueError):
        broker.resolve_reply(question.request_id, "wrong kind")


def test_defer_is_resolved_terminal_idempotent_and_has_no_decision() -> None:
    broker = ClaudeInteractionBroker()
    interaction = broker.create("session", "permission")

    deferred = broker.defer(interaction.request_id)
    duplicate = broker.defer(interaction.request_id)

    assert deferred is not None
    assert deferred == duplicate
    assert deferred.status == "resolved"
    assert deferred.decision is None


def test_wait_uses_condition_notification_and_returns_resolution() -> None:
    broker = ClaudeInteractionBroker(
        passive_seconds=1.0,
        hard_seconds=2.0,
    )
    interaction = broker.create("session", "stop")
    entered = threading.Event()

    def wait_for_result():
        entered.set()
        return broker.wait(interaction.request_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(wait_for_result)
        assert entered.wait(timeout=1.0)
        result = broker.resolve_reply(interaction.request_id, "continue")
        assert future.result(timeout=1.0) == result


def test_terminal_discard_scrubs_raw_session_and_rejects_pending() -> None:
    broker = ClaudeInteractionBroker()
    interaction = broker.create(
        "opaque-session",
        "stop",
        raw_session_id="private-raw-session",
    )

    assert broker.discard_terminal(interaction.request_id) is False
    assert broker.raw_session_id_for(interaction.request_id) == (
        "private-raw-session"
    )
    broker.resolve_reply(interaction.request_id, "private decision")

    assert broker.discard_terminal(interaction.request_id) is True
    assert broker.get(interaction.request_id) is None
    assert broker.raw_session_id_for(interaction.request_id) is None
    assert broker.discard_terminal(interaction.request_id) is False


def test_broker_clear_scrubs_rows_and_wakes_pending_waiters() -> None:
    broker = ClaudeInteractionBroker()
    interaction = broker.create(
        "opaque-session",
        "stop",
        raw_session_id="private-raw-session",
    )
    entered = threading.Event()

    def wait_for_clear():
        entered.set()
        return broker.wait(interaction.request_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(wait_for_clear)
        assert entered.wait(timeout=1.0)
        broker.clear()
        assert future.result(timeout=1.0) is None

    assert broker.snapshot() == ()
    assert broker.raw_session_id_for(interaction.request_id) is None


def test_wait_falls_through_as_expired_without_busy_polling() -> None:
    broker = ClaudeInteractionBroker(
        passive_seconds=0.03,
        hard_seconds=0.1,
    )
    interaction = broker.create("session", "stop")

    started = time.perf_counter()
    result = broker.wait(interaction.request_id)
    elapsed = time.perf_counter() - started

    assert result is not None
    assert result.status == "expired"
    assert result.decision is None
    assert elapsed >= 0.02
    assert elapsed < 0.5


def test_concurrent_resolvers_cannot_overwrite_the_winner() -> None:
    broker = ClaudeInteractionBroker()
    interaction = broker.create("session", "permission")

    def decide(value: str):
        return broker.resolve_permission(interaction.request_id, value)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(decide, ["allow", "deny", "allow", "deny"] * 2)
        )

    assert all(result == results[0] for result in results)
    assert results[0].decision in {"allow", "deny"}


def test_unknown_requests_are_safe_noops() -> None:
    broker = ClaudeInteractionBroker()

    assert broker.get("missing") is None
    assert broker.engage("missing") is None
    assert broker.resolve_reply("missing", "reply") is None
    assert broker.defer("missing") is None
    assert broker.expire("missing") is None
    assert broker.wait("missing") is None


def test_explicit_request_id_retries_are_idempotent() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock)

    original = broker.create(
        "opaque-session",
        "question",
        raw_session_id="raw-session",
        request_id="opaque-request",
    )
    clock.advance(1.0)
    retry = broker.create(
        "opaque-session",
        "question",
        raw_session_id="raw-session",
        request_id="opaque-request",
    )

    assert retry == original
    assert retry.sequence == 1

    with pytest.raises(ValueError):
        broker.create(
            "different-session",
            "question",
            request_id="opaque-request",
        )


def test_invalid_decisions_do_not_resolve_requests() -> None:
    broker = ClaudeInteractionBroker()
    stop = broker.create("session", "stop")
    question = broker.create("session", "question")

    with pytest.raises(ValueError):
        broker.resolve_reply(stop.request_id, "   ")
    with pytest.raises(ValueError):
        broker.resolve_question(question.request_id, {"Question?": ""})

    assert broker.get(stop.request_id).status == "pending"
    assert broker.get(question.request_id).status == "pending"


def test_wait_with_manual_clock_is_released_by_deterministic_expiry() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock)
    interaction = broker.create("session", "stop")
    entered = threading.Event()

    def wait_for_result():
        entered.set()
        return broker.wait(interaction.request_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(wait_for_result)
        assert entered.wait(timeout=1.0)
        clock.advance(PASSIVE_DEADLINE_SECONDS)
        expired = broker.expire(interaction.request_id)
        assert expired is not None
        assert expired.status == "expired"
        assert future.result(timeout=1.0) == expired


def test_production_timing_constants_are_locked() -> None:
    assert PASSIVE_DEADLINE_SECONDS == 6.0
    assert MAX_PASSIVE_DEADLINE_SECONDS == 30.0
    assert ENGAGED_CEILING_SECONDS == 120.0


def _fire_coordinator_timer(coordinator: ClaudeResponseCoordinator) -> None:
    timer = coordinator._timer
    if timer is not None:
        timer.cancel()
    coordinator._on_timer(coordinator._timer_generation)


def test_response_coordinator_serializes_speech_grace_quiet_and_promotion() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=_ids("request-one", "request-two"),
    )
    first = broker.create(
        "session-one",
        "stop",
        passive_seconds=6,
        active=False,
    )
    second = broker.create(
        "session-two",
        "stop",
        passive_seconds=6,
        active=False,
    )
    speech_callbacks = {}
    interactions = []
    terminals: list[str] = []
    chimes: list[bool] = []
    holder: dict[str, ClaudeResponseCoordinator] = {}
    snapshots: list[dict] = []
    snapshot_ready = threading.Event()

    def changed() -> None:
        snapshots.append(holder["coordinator"].snapshot())
        snapshot_ready.set()

    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 2,
        state_changed=changed,
        interaction_changed=interactions.append,
        completion_terminal=terminals.append,
        chime=lambda: chimes.append(True),
        clock=clock,
    )
    holder["coordinator"] = coordinator
    loop = asyncio.new_event_loop()
    try:
        coordinator.enqueue(
            completion_id="completion-one",
            sequence=1,
            grace_seconds=6,
            speech_start=lambda callback: (
                speech_callbacks.__setitem__("one", callback) or True
            ),
            interaction=first,
            waiter_loop=loop,
        )
        coordinator.enqueue(
            completion_id="completion-two",
            sequence=2,
            grace_seconds=6,
            speech_start=lambda callback: (
                speech_callbacks.__setitem__("two", callback) or True
            ),
            interaction=second,
            waiter_loop=loop,
        )

        queued = coordinator.snapshot()
        assert queued["phase"] == "active_speaking"
        assert queued["ordered_completion_ids"] == [
            "completion-one",
            "completion-two",
        ]
        assert queued["active"] == {
            "completion_id": "completion-one",
            "speech_completed": False,
            "phase": "active_speaking",
            "request_id": "request-one",
            "session_handle": "session-one",
            "generation": 1,
            "engaged": False,
            "routable": False,
            "passive_total_seconds": 6.0,
            "passive_remaining_seconds": 0.0,
            "hard_remaining_seconds": 120.0,
        }
        assert broker.get(second.request_id).active is False

        clock.advance(20)
        speech_callbacks["one"]("completed")
        grace = coordinator.snapshot()
        assert grace["phase"] == "active_grace"
        assert grace["active"]["passive_remaining_seconds"] == pytest.approx(6)
        assert grace["active"]["hard_remaining_seconds"] == pytest.approx(100)
        assert grace["active"]["routable"] is True
        assert broker.get(first.request_id).passive_deadline == pytest.approx(
            clock() + 6
        )

        clock.advance(6)
        _fire_coordinator_timer(coordinator)
        quiet = coordinator.snapshot()
        assert quiet["phase"] == "quiet_delay"
        assert quiet["active"] is None
        assert quiet["ordered_completion_ids"] == ["completion-two"]
        assert quiet["quiet_delay_remaining_seconds"] == pytest.approx(2)
        assert terminals == ["completion-one"]
        assert broker.get(first.request_id).status == "expired"

        clock.advance(2)
        _fire_coordinator_timer(coordinator)
        promoted = coordinator.snapshot()
        assert promoted["phase"] == "active_speaking"
        assert promoted["active"]["completion_id"] == "completion-two"
        assert "two" in speech_callbacks
        assert chimes == []
        assert snapshot_ready.wait(5)
        assert snapshots
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def test_response_reply_engages_and_resolves_only_its_conversation() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=lambda: "request",
    )
    interaction = broker.create(
        "session",
        "stop",
        passive_seconds=6,
        active=False,
    )
    speech_callbacks = []
    speech_stops: list[bool] = []
    terminals: list[str] = []
    order: list[str] = []
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 0,
        state_changed=lambda: order.append("state"),
        interaction_changed=lambda _interaction: order.append("interaction"),
        completion_terminal=terminals.append,
        chime=lambda: None,
        speech_stop=lambda: speech_stops.append(True),
        clock=clock,
    )
    loop = asyncio.new_event_loop()
    try:
        coordinator.enqueue(
            completion_id="completion",
            sequence=1,
            grace_seconds=6,
            speech_start=lambda callback: (
                speech_callbacks.append(callback) or True
            ),
            interaction=interaction,
            waiter_loop=loop,
        )

        # Engaging stops speech and holds the Stop to its hard ceiling. Only
        # the exact conversation's open Stop qualifies.
        assert coordinator.engage("other-session") is None
        assert coordinator.engage("session") == pytest.approx(120)
        assert speech_stops == [True]
        assert broker.get("request").engaged is True
        assert coordinator.snapshot()["phase"] == "active_grace"
        assert coordinator.live_reply_remaining("session") == pytest.approx(120)

        assert coordinator.resolve_reply("other-session", "reply words") is None
        assert coordinator.resolve_reply("session", "   ") is None
        order.clear()
        resolved = coordinator.resolve_reply(
            "session", "reply words", on_resolved=lambda _i: order.append("receipt")
        )
        # The reply owner's receipt goes out before any terminal publish, so
        # the HUD still has the card when it confirms the send.
        assert order and order[0] == "receipt"
        assert resolved is not None
        assert resolved.status == "resolved"
        assert resolved.decision == "reply words"
        assert coordinator.snapshot()["phase"] == "idle"
        assert terminals == ["completion"]
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def test_visible_unparkable_response_has_no_reply_route() -> None:
    """A displayed response without hook authority has nothing to resolve."""
    clock = ManualClock()
    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=lambda: "unused",
    )
    speech_callbacks = []
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 0,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        completion_terminal=lambda _completion_id: None,
        chime=lambda: None,
        clock=clock,
    )
    try:
        coordinator.enqueue(
            completion_id="recursive-completion",
            sequence=1,
            grace_seconds=6,
            speech_start=lambda callback: (
                speech_callbacks.append(callback) or True
            ),
        )

        # No Stop is open: there is nothing to engage or resolve. The server
        # still lets a painted card take the dictation into its draft.
        assert coordinator.live_reply_remaining(None) is None
        assert coordinator.engage(None) is None
        assert coordinator.resolve_reply(None, "text") is None
        assert coordinator.snapshot()["active"]["request_id"] is None
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()


def test_response_cleanup_flushes_only_response_routes() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=_ids(
            "request-one",
            "request-two",
            "question-request",
        ),
    )
    first = broker.create(
        "session-one", "stop", passive_seconds=6, active=False
    )
    second = broker.create(
        "session-two", "stop", passive_seconds=6, active=False
    )
    question = broker.create("session-question", "question")
    terminals: list[str] = []
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 2,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        completion_terminal=terminals.append,
        chime=lambda: None,
        clock=clock,
    )
    loop = asyncio.new_event_loop()
    try:
        coordinator.enqueue(
            completion_id="completion-one",
            sequence=1,
            grace_seconds=6,
            speech_start=None,
            interaction=first,
            waiter_loop=loop,
        )
        coordinator.enqueue(
            completion_id="completion-two",
            sequence=2,
            grace_seconds=6,
            speech_start=None,
            interaction=second,
            waiter_loop=loop,
        )
        assert coordinator.dismiss_responses() is True
        assert coordinator.snapshot()["phase"] == "idle"
        assert coordinator.snapshot()["ordered_completion_ids"] == []
        assert broker.get(first.request_id).status == "expired"
        assert broker.get(second.request_id).status == "expired"
        assert broker.get(question.request_id).status == "pending"
        assert terminals == ["completion-one", "completion-two"]
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def test_skip_current_preserves_waiters_and_ignores_late_speech_terminal() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock, request_id_factory=_ids("a", "b", "c"))
    callbacks = []
    calls = []
    coordinator = ClaudeResponseCoordinator(
        broker=broker, quiet_delay_provider=lambda: 20,
        state_changed=lambda: None, interaction_changed=lambda _: None,
        completion_terminal=lambda value: calls.append(("terminal", value)),
        speech_stop=lambda: calls.append(("stop", None)), chime=lambda: None, clock=clock,
    )
    loop = asyncio.new_event_loop()
    try:
        for index, name in enumerate(("a", "b", "c"), 1):
            interaction = broker.create(name, "stop", passive_seconds=6, active=False)
            coordinator.enqueue(
                completion_id=name, sequence=index, grace_seconds=6,
                speech_start=lambda callback: callbacks.append(callback) or True,
                interaction=interaction, waiter_loop=loop,
            )
        assert len(callbacks) == 1
        assert coordinator.skip_current() is True
        snapshot = coordinator.snapshot()
        assert snapshot["active"]["completion_id"] == "b"
        assert snapshot["ordered_completion_ids"] == ["b", "c"]
        assert snapshot["phase"] == "active_speaking"
        assert len(callbacks) == 2
        assert broker.get("a").status == "expired"
        assert broker.get("b").status == "pending"
        assert broker.get("c").status == "pending"
        assert calls == [("stop", None), ("terminal", "a")]
        callbacks[0]("stopped")
        assert coordinator.snapshot()["phase"] == "active_speaking"
        callbacks[1]("completed")
        assert coordinator.snapshot()["active"]["passive_remaining_seconds"] == 6
    finally:
        coordinator.dismiss_responses()
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def _ceiling_coordinator(clock, draft_present):
    broker = ClaudeInteractionBroker(clock=clock, request_id_factory=lambda: "request")
    terminals: list[str] = []
    coordinator = ClaudeResponseCoordinator(
        broker=broker, quiet_delay_provider=lambda: 0,
        state_changed=lambda: None, interaction_changed=lambda _: None,
        completion_terminal=terminals.append, chime=lambda: None, clock=clock,
        draft_present=draft_present,
    )
    return broker, coordinator, terminals


@pytest.mark.parametrize("unsent", [False, True])
def test_hook_ceiling_keeps_card_only_while_owner_holds_unsent_text(unsent) -> None:
    """At the hard ceiling the Stop always fails open. A card with unsent text
    and no other route stays up read-only (bounded) so the text can be copied."""
    clock = ManualClock()
    seen: list[str] = []
    broker, coordinator, terminals = _ceiling_coordinator(
        clock, lambda conversation: seen.append(conversation) or unsent
    )
    loop = asyncio.new_event_loop()
    try:
        interaction = broker.create("session", "stop", passive_seconds=6, active=False)
        coordinator.enqueue(
            completion_id="completion", sequence=1, grace_seconds=6,
            speech_start=None, interaction=interaction, waiter_loop=loop,
        )
        assert coordinator.engage("session") == pytest.approx(120)
        clock.advance(120)
        _fire_coordinator_timer(coordinator)

        assert broker.get("request").status == "expired"
        assert seen == ["session"]
        assert coordinator.live_reply_remaining("session") is None
        if unsent:
            snapshot = coordinator.snapshot()
            assert snapshot["active"]["completion_id"] == "completion"
            assert snapshot["active"]["routable"] is False
            assert terminals == []
            clock.advance(coordinator.RETAINED_DRAFT_TTL_SECONDS)
            _fire_coordinator_timer(coordinator)
        assert coordinator.snapshot()["active"] is None
        assert terminals == ["completion"]
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def test_response_coordinator_starts_visual_grace_when_audio_is_unavailable() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock)
    chimes: list[bool] = []
    holder: dict[str, ClaudeResponseCoordinator] = {}
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 0,
        state_changed=lambda: holder["coordinator"].snapshot(),
        interaction_changed=lambda _interaction: None,
        completion_terminal=lambda _completion_id: None,
        chime=lambda: chimes.append(True),
        clock=clock,
    )
    holder["coordinator"] = coordinator
    try:
        coordinator.enqueue(
            completion_id="visual-only",
            sequence=1,
            grace_seconds=0,
            speech_start=lambda _callback: False,
        )

        snapshot = coordinator.snapshot()
        assert snapshot["phase"] == "active_grace"
        assert snapshot["active"]["routable"] is False
        assert snapshot["active"]["passive_remaining_seconds"] == pytest.approx(
            6
        )
        assert chimes == [True]
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()


def test_response_clear_fails_open_active_work() -> None:
    clock = ManualClock()
    terminals: list[str] = []
    coordinator = ClaudeResponseCoordinator(
        broker=ClaudeInteractionBroker(clock=clock),
        quiet_delay_provider=lambda: 0,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        completion_terminal=terminals.append,
        chime=lambda: None,
        clock=clock,
    )
    coordinator.enqueue(
        completion_id="active-completion",
        sequence=1,
        grace_seconds=6,
        speech_start=lambda _callback: False,
    )
    coordinator.clear()

    snapshot = coordinator.snapshot()
    assert snapshot["phase"] == "idle"
    assert snapshot["active"] is None
    assert snapshot["ordered_completion_ids"] == []
    assert terminals == ["active-completion"]
    assert coordinator._timer is None


def test_response_coordinator_engagement_holds_until_absolute_ceiling() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=lambda: "request",
    )
    interaction = broker.create(
        "session",
        "stop",
        passive_seconds=6,
        active=False,
    )
    speech_callbacks = []
    terminals: list[str] = []
    holder: dict[str, ClaudeResponseCoordinator] = {}
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 0,
        state_changed=lambda: holder["coordinator"].snapshot(),
        interaction_changed=lambda _interaction: None,
        completion_terminal=terminals.append,
        chime=lambda: None,
        clock=clock,
    )
    holder["coordinator"] = coordinator
    loop = asyncio.new_event_loop()
    try:
        coordinator.enqueue(
            completion_id="completion",
            sequence=1,
            grace_seconds=6,
            speech_start=lambda callback: (
                speech_callbacks.append(callback) or True
            ),
            interaction=interaction,
            waiter_loop=loop,
        )
        speech_callbacks[0]("completed")
        engaged = broker.engage(interaction.request_id)
        assert engaged is not None and engaged.engaged
        coordinator.interaction_changed(engaged)

        clock.advance(6)
        _fire_coordinator_timer(coordinator)
        assert coordinator.snapshot()["phase"] == "active_grace"
        assert coordinator.snapshot()["active"]["engaged"] is True

        clock.advance(114)
        _fire_coordinator_timer(coordinator)
        assert broker.get(interaction.request_id).status == "expired"
        assert terminals == ["completion"]
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def test_question_snapshot_carries_optional_source_name() -> None:
    clock = ManualClock()
    _, coordinator, _, _ = _question_coordinator(clock)

    coordinator.surface("session", QUESTION_BATCH, source_name="pay kit")
    assert coordinator.snapshot()["active"]["source_name"] == "pay kit"


def test_question_snapshot_source_name_is_none_when_unresolved() -> None:
    clock = ManualClock()
    _, coordinator, _, _ = _question_coordinator(clock)

    coordinator.surface("session", QUESTION_BATCH)
    assert coordinator.snapshot()["active"]["source_name"] is None


def test_typing_stop_cannot_cancel_concurrent_escape_successor() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock, request_id_factory=_ids("a", "b"))
    stopping, release_stop, skip_attempted, next_started = Event(), Event(), Event(), Event()

    def stop():
        stopping.set()
        assert release_stop.wait(2)

    coordinator = ClaudeResponseCoordinator(
        broker=broker, quiet_delay_provider=lambda: 0,
        state_changed=lambda: None, interaction_changed=lambda _: None,
        completion_terminal=lambda _: None, speech_stop=stop,
        chime=lambda: None, clock=clock,
    )
    loop = asyncio.new_event_loop()
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        for index, name in enumerate(("a", "b"), 1):
            interaction = broker.create(name, "stop", passive_seconds=6, active=False)
            coordinator.enqueue(
                completion_id=name, sequence=index, grace_seconds=6,
                speech_start=lambda _, name=name: (next_started.set() if name == "b" else None) or True,
                interaction=interaction, waiter_loop=loop,
            )
        typing = pool.submit(coordinator.engage, "a")
        assert stopping.wait(1)

        def skip():
            skip_attempted.set()
            return coordinator.skip_current()

        skipping = pool.submit(skip)
        assert skip_attempted.wait(1)
        assert not next_started.wait(0.1)
        release_stop.set()
        assert typing.result(timeout=2) is not None
        assert skipping.result(timeout=2)
        assert next_started.is_set()
        assert coordinator.snapshot()["active"]["completion_id"] == "b"
    finally:
        release_stop.set()
        pool.shutdown(wait=True)
        coordinator.clear()
        loop.close()


def test_detached_response_waiter_cleanup_tolerates_closed_request_loop():
    loop = asyncio.new_event_loop()
    waiter = loop.create_future()
    loop.close()
    item = SimpleNamespace(waiter=waiter, waiter_loop=loop)
    coordinator = ClaudeResponseCoordinator(
        broker=ClaudeInteractionBroker(), quiet_delay_provider=lambda: 0,
        state_changed=lambda: None, interaction_changed=lambda _: None,
        completion_terminal=lambda _: None, chime=lambda: None,
    )
    coordinator._resolve_waiter_locked(item, None)
    assert item.waiter is None
    assert item.waiter_loop is None


def _scheduled_delay(coordinator: ClaudeResponseCoordinator) -> float | None:
    timer = coordinator._timer
    return None if timer is None else timer.interval


def test_expired_hard_deadline_held_by_speech_does_not_spin_timer() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=lambda: "request",
    )
    interaction = broker.create(
        "session",
        "stop",
        passive_seconds=6,
        active=False,
    )
    speech_callbacks = {}
    terminals: list[str] = []
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 2,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        completion_terminal=terminals.append,
        chime=lambda: None,
        clock=clock,
    )
    loop = asyncio.new_event_loop()
    try:
        coordinator.enqueue(
            completion_id="completion",
            sequence=1,
            grace_seconds=6,
            speech_start=lambda callback: (
                speech_callbacks.__setitem__("speech", callback) or True
            ),
            interaction=interaction,
            waiter_loop=loop,
        )
        assert coordinator.snapshot()["phase"] == "active_speaking"

        # Speech outlives the absolute ceiling: the response becomes read-only
        # but stays speaking. The expired deadline must not be re-armed.
        clock.advance(121)
        _fire_coordinator_timer(coordinator)
        assert broker.get(interaction.request_id).status == "expired"
        assert coordinator.snapshot()["phase"] == "active_speaking"
        for _ in range(5):
            assert _scheduled_delay(coordinator) == pytest.approx(
                ClaudeResponseCoordinator.SNAPSHOT_TICK_SECONDS
            )
            clock.advance(1)
            _fire_coordinator_timer(coordinator)

        # Releasing the hold resumes the ordinary lifecycle to idle.
        speech_callbacks["speech"]("completed")
        assert coordinator.snapshot()["phase"] == "active_grace"
        clock.advance(ClaudeResponseCoordinator.VISUAL_DWELL_SECONDS)
        _fire_coordinator_timer(coordinator)
        assert coordinator.snapshot()["phase"] == "quiet_delay"
        clock.advance(2)
        _fire_coordinator_timer(coordinator)
        assert coordinator.snapshot()["phase"] == "idle"
        assert terminals == ["completion"]
        assert coordinator._timer is None
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def test_quiet_delay_held_by_capture_does_not_spin_and_rearms_on_release() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock)
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 2,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        completion_terminal=lambda _completion: None,
        chime=lambda: None,
        clock=clock,
    )
    try:
        for sequence, completion_id in enumerate(("first", "second"), start=1):
            coordinator.enqueue(
                completion_id=completion_id,
                sequence=sequence,
                grace_seconds=6,
                speech_start=None,
            )
        assert coordinator.snapshot()["phase"] == "active_grace"
        coordinator.set_capture_active(True)

        clock.advance(ClaudeResponseCoordinator.VISUAL_DWELL_SECONDS)
        _fire_coordinator_timer(coordinator)
        assert coordinator.snapshot()["phase"] == "quiet_delay"

        # The quiet deadline passes while capture holds it.
        clock.advance(5)
        for _ in range(5):
            _fire_coordinator_timer(coordinator)
            assert coordinator.snapshot()["phase"] == "quiet_delay"
            assert _scheduled_delay(coordinator) == pytest.approx(
                ClaudeResponseCoordinator.SNAPSHOT_TICK_SECONDS
            )

        coordinator.set_capture_active(False)
        promoted = coordinator.snapshot()
        assert promoted["phase"] == "active_grace"
        assert promoted["active"]["completion_id"] == "second"
        assert _scheduled_delay(coordinator) == pytest.approx(
            ClaudeResponseCoordinator.SNAPSHOT_TICK_SECONDS
        )
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()


@pytest.mark.parametrize("hold", ["set_capture_active", "set_compose_hold"])
def test_a_response_arriving_while_held_and_idle_plays_on_release(hold) -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock)
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 0,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        completion_terminal=lambda _completion: None,
        chime=lambda: None,
        clock=clock,
    )
    try:
        getattr(coordinator, hold)(True)
        coordinator.enqueue(
            completion_id="waiting",
            sequence=1,
            grace_seconds=6,
            speech_start=None,
        )
        assert coordinator.snapshot()["phase"] == "idle"

        getattr(coordinator, hold)(False)
        promoted = coordinator.snapshot()
        assert promoted["phase"] == "active_grace"
        assert promoted["active"]["completion_id"] == "waiting"
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()


def test_either_hold_alone_keeps_the_queue_held() -> None:
    clock = ManualClock()
    coordinator = ClaudeResponseCoordinator(
        broker=ClaudeInteractionBroker(clock=clock),
        quiet_delay_provider=lambda: 0,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        completion_terminal=lambda _completion: None,
        chime=lambda: None,
        clock=clock,
    )
    try:
        coordinator.set_capture_active(True)
        coordinator.set_compose_hold(True)
        coordinator.enqueue(completion_id="c", sequence=1, grace_seconds=6, speech_start=None)
        coordinator.set_capture_active(False)
        assert coordinator.snapshot()["phase"] == "idle"
        coordinator.set_compose_hold(False)
        assert coordinator.snapshot()["phase"] == "active_grace"
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()


def test_blocked_publication_does_not_hold_deadline_or_survive_close():
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock, request_id_factory=lambda: "request")
    interaction = broker.create("session", "stop", passive_seconds=6, active=False)
    entered, release = threading.Event(), threading.Event()
    publications = []

    def publish():
        publications.append(1)
        entered.set()
        assert release.wait(5)

    coordinator = ClaudeResponseCoordinator(
        broker=broker, quiet_delay_provider=lambda: 2, state_changed=publish,
        interaction_changed=lambda _: None, completion_terminal=lambda _: None,
        chime=lambda: None, clock=clock,
    )
    loop = asyncio.new_event_loop()
    try:
        waiter = coordinator.enqueue(
            completion_id="completion", sequence=1, grace_seconds=6,
            speech_start=lambda callback: True, interaction=interaction, waiter_loop=loop,
        )
        assert entered.wait(5)
        worker = coordinator._publisher._thread
        clock.advance(121)
        # Run the actual timer path on another thread: it must finish while
        # publication remains blocked, preserving the broker's hard deadline.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_fire_coordinator_timer, coordinator).result(timeout=5)
        assert broker.get(interaction.request_id).status == "expired"
        assert coordinator._timer.interval == pytest.approx(1.0)
        coordinator.close()
        coordinator.enqueue(completion_id="late", sequence=2, grace_seconds=0, speech_start=None)
        assert coordinator.snapshot()["active"] is None
        assert coordinator._timer is None
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert publications == [1]
        loop.run_until_complete(asyncio.sleep(0))
        assert waiter.done()
    finally:
        release.set()
        coordinator.close()
        loop.close()


def _draft_coordinator(clock):
    broker = ClaudeInteractionBroker(clock=clock, request_id_factory=lambda: "request")
    interaction = broker.create("session", "stop", passive_seconds=6, active=False)
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 0,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        completion_terminal=lambda _completion_id: None,
        chime=lambda: None,
        clock=clock,
    )
    return broker, interaction, coordinator


def test_incoming_response_queues_behind_an_engaged_reply() -> None:
    clock = ManualClock()
    broker, interaction, coordinator = _draft_coordinator(clock)
    loop = asyncio.new_event_loop()
    try:
        coordinator.enqueue(
            completion_id="first", sequence=1, grace_seconds=6, speech_start=None,
            interaction=interaction, waiter_loop=loop,
        )
        assert coordinator.engage("session") is not None

        coordinator.enqueue(
            completion_id="second", sequence=2, grace_seconds=6, speech_start=None,
        )
        clock.advance(30)
        _fire_coordinator_timer(coordinator)

        assert coordinator.snapshot()["active"]["completion_id"] == "first"
    finally:
        coordinator.dismiss_responses()
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def test_response_escape_flushes_only_response_routes() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=_ids(
            "request-one",
            "request-two",
            "question-request",
        ),
    )
    first = broker.create(
        "session-one", "stop", passive_seconds=6, active=False
    )
    second = broker.create(
        "session-two", "stop", passive_seconds=6, active=False
    )
    question = broker.create("session-question", "question")
    terminals: list[str] = []
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 2,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        completion_terminal=terminals.append,
        chime=lambda: None,
        clock=clock,
    )
    loop = asyncio.new_event_loop()
    try:
        coordinator.enqueue(
            completion_id="completion-one",
            sequence=1,
            grace_seconds=6,
            speech_start=None,
            interaction=first,
            waiter_loop=loop,
        )
        coordinator.enqueue(
            completion_id="completion-two",
            sequence=2,
            grace_seconds=6,
            speech_start=None,
            interaction=second,
            waiter_loop=loop,
        )
        assert coordinator.dismiss_responses() is True
        assert coordinator.snapshot()["phase"] == "idle"
        assert coordinator.snapshot()["ordered_completion_ids"] == []
        assert broker.get(first.request_id).status == "expired"
        assert broker.get(second.request_id).status == "expired"
        assert broker.get(question.request_id).status == "pending"
        assert terminals == ["completion-one", "completion-two"]
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def test_response_absolute_ceiling_expires_the_hook_and_refuses_late_delivery() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(
        clock=clock,
        request_id_factory=lambda: "request",
    )
    interaction = broker.create(
        "session",
        "stop",
        passive_seconds=6,
        active=False,
    )
    coordinator = ClaudeResponseCoordinator(
        broker=broker,
        quiet_delay_provider=lambda: 0,
        state_changed=lambda: None,
        interaction_changed=lambda _interaction: None,
        completion_terminal=lambda _completion_id: None,
        chime=lambda: None,
        clock=clock,
    )
    loop = asyncio.new_event_loop()
    try:
        coordinator.enqueue(
            completion_id="completion",
            sequence=1,
            grace_seconds=6,
            speech_start=None,
            interaction=interaction,
            waiter_loop=loop,
        )
        assert coordinator.engage("session") is not None

        clock.advance(120)
        _fire_coordinator_timer(coordinator)

        # Draft text and its read-only retention belong to the reply desk;
        # the coordinator only expires the hook and refuses late delivery.
        assert coordinator.resolve_reply("session", "keep me") is None
        assert broker.get("request").status == "expired"
    finally:
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def test_typed_reply_engages_only_the_exact_live_conversation() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock, request_id_factory=lambda: "typed")
    callbacks = []
    stops = []
    coordinator = ClaudeResponseCoordinator(
        broker=broker, quiet_delay_provider=lambda: 0,
        state_changed=lambda: None, interaction_changed=lambda _: None,
        completion_terminal=lambda _: None, speech_stop=lambda: stops.append(True),
        chime=lambda: None, clock=clock,
    )
    loop = asyncio.new_event_loop()
    try:
        interaction = broker.create("session", "stop", passive_seconds=6, active=False)
        coordinator.enqueue(
            completion_id="completion", sequence=1, grace_seconds=6,
            speech_start=lambda callback: callbacks.append(callback) or True,
            interaction=interaction, waiter_loop=loop,
        )
        assert coordinator.engage("wrong") is None
        assert coordinator.engage("session") is not None
        snapshot = coordinator.snapshot()
        assert snapshot["phase"] == "active_grace"
        assert snapshot["active"]["completion_id"] == "completion"
        assert broker.get("typed").engaged
        assert stops == [True]
        callbacks[0]("stopped")
        assert coordinator.snapshot()["phase"] == "active_grace"
        # Draft text and its retention belong to the reply desk; the
        # coordinator only refuses delivery once the hook's ceiling passes.
        clock.advance(121)
        coordinator._on_timer(coordinator._timer_generation)
        assert coordinator.resolve_reply("session", "Keep these words") is None
    finally:
        coordinator.dismiss_responses()
        if coordinator._timer is not None:
            coordinator._timer.cancel()
        loop.close()


def test_is_speaking_tracks_the_foreground_voice() -> None:
    clock = ManualClock()
    broker = ClaudeInteractionBroker(clock=clock, request_id_factory=_ids("a"))
    callbacks = []
    coordinator = ClaudeResponseCoordinator(
        broker=broker, quiet_delay_provider=lambda: 20,
        state_changed=lambda: None, interaction_changed=lambda _: None,
        completion_terminal=lambda _value: None,
        speech_stop=lambda: None, chime=lambda: None, clock=clock,
    )
    loop = asyncio.new_event_loop()
    try:
        assert coordinator.is_speaking() is False
        interaction = broker.create("a", "stop", passive_seconds=6, active=False)
        coordinator.enqueue(
            completion_id="a", sequence=1, grace_seconds=6,
            speech_start=lambda callback: callbacks.append(callback) or True,
            interaction=interaction, waiter_loop=loop,
        )
        assert coordinator.is_speaking() is True
        # A stopped voice leaves the response up for its reply window.
        callbacks[0]("stopped")
        assert coordinator.is_speaking() is False
        assert coordinator.snapshot()["active"]["completion_id"] == "a"
    finally:
        coordinator.dismiss_responses()
        loop.close()
