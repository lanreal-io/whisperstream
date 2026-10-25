"""Tests for the safe-to-inject predicate (``managed_sessions.can_reply``).

Injection is allowed at the freshly registered initial prompt, after ``Stop``,
or while the session is parked in a blocked question interaction, provided the
delivery poll is fresh and the session is not ended. Terminal draft ownership
belongs to the user and stdin bytes are deliberately not inspected.

Every allow case and every named blocking case from the plan has a test here,
including the ones that are easy to get wrong:

* a ``Notification`` as the most recent event (non-authoritative by our own
  documentation, and version-fragile);
* working / producing output (inferred - no start-of-output event exists).

The predicate is a **boolean with no reason string**. A test asserts no reason field exists on the return
value or anywhere in the session record.
"""
from __future__ import annotations

import ast
import itertools
from dataclasses import fields as dataclass_fields
from pathlib import Path

import pytest

from whisperstream import managed_sessions
from whisperstream.managed_sessions import (
    DEFAULT_SESSION_TTL_SECONDS,
    ManagedSession,
    ManagedSessionStore,
    QUESTION_EVENT_KINDS,
    SESSION_END_EVENT_KIND,
    SESSION_START_EVENT_KIND,
    STOP_EVENT_KIND,
    can_reply,
)


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _handle_factory():
    counter = itertools.count(1)
    return lambda: f"handle-{next(counter)}"


def _make_store(**kwargs) -> tuple[ManagedSessionStore, _FakeClock]:
    clock = _FakeClock()
    kwargs.setdefault("clock", clock)
    kwargs.setdefault("handle_factory", _handle_factory())
    kwargs.setdefault("pid_is_alive", lambda pid: True)
    return ManagedSessionStore(**kwargs), clock


def _idle_session() -> tuple[ManagedSessionStore, _FakeClock, ManagedSession]:
    """The allowed shape: registered with an authoritative idle event."""
    store, clock = _make_store()
    store.register("tok", pid=4321, cwd=r"C:\dev\whisperstream")
    session = store.record_event("tok", STOP_EVENT_KIND, session_id="s-1")
    assert session is not None
    return store, clock, session


# ---------------------------------------------------------------------------
# The allow cases
# ---------------------------------------------------------------------------


def test_idle_session_may_receive_a_reply():
    store, clock, session = _idle_session()

    assert store.can_reply(session) is True
    assert can_reply(session, now=clock.now) is True


def test_fresh_registration_may_receive_a_reply_without_a_first_stop():
    store, _clock = _make_store()
    session = store.register("tok", pid=4321)

    assert session.last_event_kind is None
    assert store.can_reply(session) is True


def test_the_store_judges_freshness_against_its_own_clock(monkeypatch):
    """A store with a custom clock must not be judged by the module default."""
    store, clock, session = _idle_session()
    monkeypatch.setattr(
        managed_sessions.time,
        "monotonic",
        lambda: clock.now + DEFAULT_SESSION_TTL_SECONDS + 1.0,
    )

    # Pin the module clock beyond the TTL. A fresh CI runner may otherwise have
    # less uptime than the fake clock's 1000-second epoch, making this assertion
    # depend on the host rather than the predicate.
    assert can_reply(session) is False
    assert store.can_reply(session) is True


@pytest.mark.parametrize("kind", sorted(QUESTION_EVENT_KINDS))
def test_a_session_parked_in_a_blocked_question_may_receive_a_reply(kind):
    """The strongest case: Claude is blocked waiting on the user's answer."""
    store, _clock, session = _idle_session()
    store.record_event("tok", kind, session_id="s-1")

    assert store.can_reply(session) is True


# ---------------------------------------------------------------------------
# The named blocking cases
# ---------------------------------------------------------------------------


def test_terminal_input_is_not_part_of_the_session_record():
    """An explicit HUD send makes the user responsible for terminal drafts."""
    names = {field.name for field in dataclass_fields(ManagedSession)}

    assert "bytes_typed_since_stop" not in names
    assert "modal_byte_seen" not in names


def test_a_notification_as_the_most_recent_event_blocks():
    """Notifications are compatibility signals, not authoritative idleness."""
    store, _clock, session = _idle_session()
    store.record_event("tok", "Notification", session_id="s-1")

    assert store.can_reply(session) is False


def test_a_permission_request_blocks():
    """Permissions stay outside voice routing, as everywhere else in the HUD."""
    store, _clock, session = _idle_session()
    store.record_event("tok", "PermissionRequest", session_id="s-1")

    assert store.can_reply(session) is False


def test_the_initial_prompt_remains_available_while_the_wrapper_is_fresh():
    store, clock = _make_store()
    session = store.register("tok", pid=4321)
    clock.advance(DEFAULT_SESSION_TTL_SECONDS / 3)

    assert session.last_event_kind is None
    assert store.can_reply(session) is True


def test_codex_initial_prompt_blocks_until_the_plugin_proves_it_is_current():
    store, _clock = _make_store()
    session = store.register("tok", agent="codex", pid=4321)

    assert store.can_reply(session) is False

    store.record_event(
        "tok", SESSION_START_EVENT_KIND, agent="codex", session_id="s-1"
    )

    assert store.can_reply(session) is True


def test_a_working_session_blocks():
    """Working / producing output is inferred: no start-of-output event exists."""
    store, _clock, session = _idle_session()
    store.record_event("tok", "UserPromptSubmit", session_id="s-1")
    assert store.can_reply(session) is False

    store.record_event("tok", "SubagentStop", session_id="s-1")

    assert store.can_reply(session) is False


def test_session_start_keeps_the_initial_prompt_available():
    store, _clock, session = _idle_session()
    store.record_event("tok", SESSION_START_EVENT_KIND, session_id="s-2")

    assert store.can_reply(session) is True


def test_an_ended_session_blocks():
    store, _clock, session = _idle_session()
    store.record_event("tok", SESSION_END_EVENT_KIND, session_id="s-1")

    assert store.can_reply(session) is False


def test_a_stale_delivery_poll_blocks():
    store, clock, session = _idle_session()
    clock.advance(DEFAULT_SESSION_TTL_SECONDS + 1.0)

    assert store.can_reply(session) is False


def test_an_unknown_event_kind_blocks():
    """Unknown-state-is-unsafe-by-construction, for events we never taught it."""
    store, _clock, session = _idle_session()
    store.record_event("tok", "SomeFutureHook", session_id="s-1")

    assert store.can_reply(session) is False


@pytest.mark.parametrize("value", [None, "handle-1", 42, object()])
def test_a_non_session_argument_blocks(value):
    assert can_reply(value) is False


# ---------------------------------------------------------------------------
# The shape of the answer
# ---------------------------------------------------------------------------


def test_the_predicate_returns_a_plain_bool_with_no_reason():
    store, _clock, session = _idle_session()

    allowed = store.can_reply(session)
    blocked = can_reply(ManagedSession(
        session_handle="h",
        token="t",
        source_name="n",
        arrival_index=0,
        last_seen=0.0,
    ))

    assert type(allowed) is bool
    assert type(blocked) is bool
    for result in (allowed, blocked):
        assert not hasattr(result, "reason")


def test_no_reason_field_exists_on_the_session_record():
    """A reason string added 'for debugging' would reach the wire."""
    names = {field.name for field in dataclass_fields(ManagedSession)}

    assert not any("reason" in name for name in names)


def test_can_reply_has_exactly_one_return_path_that_yields_true():
    """One allow path, so every future blocking case has to be added to it."""
    source = Path(managed_sessions.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "can_reply"
    )
    truthy_returns = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
    ]

    assert len(truthy_returns) == 1
