"""Tests for the in-memory managed-session store (managed_sessions.py).

Every hard invariant from the plan has a named test here:

* the ``token -> session_id`` binding is refreshable, not one-shot;
* Claude registration opens the initial prompt, while Codex waits for a hook;
* arrival order is stable and removal never reorders survivors;
* reaping is inspection-only and can never signal, kill, or terminate.
"""
from __future__ import annotations

import ast
import itertools
from pathlib import Path

import psutil
import pytest

from whisperstream import managed_sessions
from whisperstream.agent_activity import SourceKey
from whisperstream.managed_sessions import (
    DEFAULT_SESSION_TTL_SECONDS,
    MAX_MANAGED_SESSIONS,
    ManagedSession,
    ManagedSessionStore,
)


class _FakeClock:
    """Monotonic-shaped clock the tests drive by hand."""

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


# ---------------------------------------------------------------------------
# Registration and identity
# ---------------------------------------------------------------------------


def test_registration_records_the_planned_fields():
    store, clock = _make_store()

    session = store.register("tok-a", pid=4321, cwd=r"C:\Users\dev\whisperstream")

    assert isinstance(session, ManagedSession)
    assert session.session_handle == "handle-1"
    assert session.agent == "claude"
    assert session.token == "tok-a"
    assert session.pid == 4321
    assert session.source_name == "whisperstream"
    assert session.session_id is None
    assert session.last_event_kind is None
    assert session.last_seen == clock.now


def test_registration_never_retains_cwd():
    store, _clock = _make_store()

    session = store.register("tok-a", pid=1, cwd=r"C:\Users\dev\secret-client-repo")

    # Only the derived, sanitized basename survives; the path itself is not a
    # field of the record (matching agent_activity's cwd handling).
    assert not hasattr(session, "cwd")
    assert "Users" not in repr(session)


def test_registration_is_idempotent_on_token():
    store, clock = _make_store()

    first = store.register("tok-a", pid=10, cwd="/repo/one")
    clock.advance(5)
    second = store.register("tok-a", pid=11, cwd="/repo/one")

    assert len(store) == 1
    assert second is first
    assert second.session_handle == "handle-1"
    # Re-registration refreshes liveness and the PID without minting a handle.
    assert second.pid == 11
    assert second.last_seen == clock.now


def test_registration_records_an_explicit_codex_provider():
    store, _clock = _make_store()

    session = store.register("tok-a", agent="codex", cwd=None)

    assert session.agent == "codex"
    assert session.source_name == "Codex"


def test_a_token_cannot_be_rebound_to_another_provider():
    store, _clock = _make_store()
    original = store.register("tok-a", agent="claude", pid=10, cwd="/repo/one")

    with pytest.raises(ValueError, match="another agent"):
        store.register("tok-a", agent="codex", pid=11, cwd="/repo/two")

    assert store.get("tok-a") is original
    assert original.agent == "claude"
    assert original.pid == 10


@pytest.mark.parametrize("agent", ["", "cursor", None, 42])
def test_registration_rejects_an_unknown_provider(agent):
    store, _clock = _make_store()

    with pytest.raises(ValueError, match="agent"):
        store.register("tok-a", agent=agent)


def test_handle_is_opaque_and_not_derived_from_token_or_pid():
    store = ManagedSessionStore(pid_is_alive=lambda pid: True)

    session = store.register("tok-secret-value", pid=13579, cwd="/repo/one")

    assert session.session_handle
    assert "tok-secret-value" not in session.session_handle
    assert "13579" not in session.session_handle
    assert session.session_handle != session.token


def test_lookup_by_token_and_by_handle():
    store, _clock = _make_store()
    session = store.register("tok-a", pid=1, cwd="/repo/one")

    assert store.get("tok-a") is session
    assert store.get_by_handle(session.session_handle) is session
    assert store.get("nope") is None
    assert store.get_by_handle("nope") is None


def test_unknown_token_operations_return_none_and_never_create_records():
    store, _clock = _make_store()

    assert store.record_event("ghost", "Stop") is None
    assert store.refresh_poll("ghost") is None
    assert store.deregister("ghost") is False
    assert len(store) == 0


def test_oversized_token_is_bounded_into_a_stable_key():
    store, _clock = _make_store()
    huge = "t" * 5000

    first = store.register(huge, pid=1, cwd="/repo/one")
    second = store.register(huge, pid=1, cwd="/repo/one")

    assert second is first
    assert len(first.token) < len(huge)
    assert store.get(huge) is first


def test_store_caps_session_count_by_dropping_the_oldest():
    store, _clock = _make_store(max_sessions=3)

    for index in range(4):
        store.register(f"tok-{index}", pid=index, cwd="/repo/one")

    assert len(store) == 3
    assert [s.token for s in store.sessions()] == ["tok-1", "tok-2", "tok-3"]
    assert MAX_MANAGED_SESSIONS >= 3


# ---------------------------------------------------------------------------
# Invariant: the token -> session_id binding is refreshable, not one-shot
# ---------------------------------------------------------------------------


def test_event_binds_session_id_for_a_known_token():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")

    session = store.record_event("tok-a", "SessionStart", session_id="sid-1")

    assert session is not None
    assert session.session_id == "sid-1"
    assert session.last_event_kind == "SessionStart"


def test_event_with_a_new_session_id_rebinds_without_duplicating_the_record():
    store, _clock = _make_store()
    original = store.register("tok-a", pid=1, cwd="/repo/one")
    store.record_event("tok-a", "SessionStart", session_id="sid-1")

    rebound = store.record_event("tok-a", "SessionStart", session_id="sid-2")

    assert len(store) == 1
    assert rebound is original
    assert rebound.session_id == "sid-2"
    assert rebound.session_handle == original.session_handle


def test_rebinding_is_not_one_shot_across_resume_compact_and_new_conversation():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")

    for session_id in ("sid-start", "sid-resume", "sid-compact", "sid-new"):
        store.record_event("tok-a", "SessionStart", session_id=session_id)

    assert len(store) == 1
    assert store.get("tok-a").session_id == "sid-new"


def test_rebinding_preserves_arrival_position():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")
    store.register("tok-b", pid=2, cwd="/repo/two")
    store.register("tok-c", pid=3, cwd="/repo/three")

    store.record_event("tok-a", "SessionStart", session_id="sid-new")

    assert [s.token for s in store.sessions()] == ["tok-a", "tok-b", "tok-c"]


def test_event_without_a_session_id_keeps_the_existing_binding():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")
    store.record_event("tok-a", "SessionStart", session_id="sid-1")

    session = store.record_event("tok-a", "Stop")

    assert session.session_id == "sid-1"
    assert session.last_event_kind == "Stop"


def test_source_key_reuses_agent_activity_source_key():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")
    session = store.record_event("tok-a", "SessionStart", session_id="sid-1")

    assert session.source_key == SourceKey("claude", "sid-1")


def test_mixed_providers_with_the_same_session_id_have_distinct_source_keys():
    store, _clock = _make_store()
    store.register("tok-claude", agent="claude")
    store.register("tok-codex", agent="codex")

    claude = store.record_event(
        "tok-claude", "SessionStart", agent="claude", session_id="sid-1"
    )
    codex = store.record_event(
        "tok-codex", "SessionStart", agent="codex", session_id="sid-1"
    )

    assert claude.source_key == SourceKey("claude", "sid-1")
    assert codex.source_key == SourceKey("codex", "sid-1")
    assert claude.source_key != codex.source_key


def test_an_event_from_the_wrong_provider_cannot_mutate_a_session():
    store, _clock = _make_store()
    session = store.register("tok-a", agent="claude")

    result = store.record_event(
        "tok-a", "Stop", agent="codex", session_id="wrong-provider-sid"
    )

    assert result is None
    assert session.session_id is None
    assert session.last_event_kind is None


def test_oversized_session_id_is_bounded_like_agent_activity():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")

    session = store.record_event("tok-a", "SessionStart", session_id="s" * 5000)

    assert session.session_id.startswith("sha256:")
    assert len(session.session_id) < 100


# ---------------------------------------------------------------------------
# Invariant: registration opens the initial prompt
# ---------------------------------------------------------------------------


def test_a_freshly_registered_session_has_initial_prompt_state():
    store, _clock = _make_store()

    session = store.register("tok-a", pid=1, cwd="/repo/one")

    # No throwaway Claude exchange is required before the HUD can be used.
    assert session.last_event_kind is None
    assert session.state_is_known is True
    assert store.can_reply(session) is True


def test_a_freshly_registered_codex_session_waits_for_a_correlated_hook():
    store, _clock = _make_store()

    session = store.register("tok-a", agent="codex", cwd="/repo/one")

    assert session.last_event_kind is None
    assert session.state_is_known is False
    assert store.can_reply(session) is False

    observed = store.record_event("tok-a", "SessionStart", agent="codex")

    assert observed is session
    assert session.state_is_known is True
    assert store.can_reply(session) is True


def test_a_session_with_only_a_stop_has_known_state():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")

    session = store.record_event("tok-a", "Stop", session_id="sid-1")

    assert session.state_is_known is True


def test_delivery_poll_freshness_expires():
    store, clock = _make_store(session_ttl_seconds=90.0)
    session = store.register("tok-a", pid=1, cwd="/repo/one")

    assert session.is_fresh(clock.now, 90.0) is True
    clock.advance(89.0)
    assert session.is_fresh(clock.now, 90.0) is True
    clock.advance(2.0)
    assert session.is_fresh(clock.now, 90.0) is False
    assert store.session_ttl_seconds == 90.0


def test_delivery_poll_refreshes_liveness():
    store, clock = _make_store(session_ttl_seconds=90.0)
    registered = store.register("tok-a", pid=1, cwd="/repo/one")
    clock.advance(80.0)

    session = store.refresh_poll(registered.session_handle)

    assert session.last_seen == clock.now
    assert session.is_fresh(clock.now, 90.0) is True


def test_session_end_marks_the_session_ended_and_unknown():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")
    store.record_event("tok-a", "Stop", session_id="sid-1")

    session = store.record_event("tok-a", "SessionEnd")

    assert session.ended is True
    assert session.state_is_known is False


# ---------------------------------------------------------------------------
# Invariant: arrival order is stable and is the tab order
# ---------------------------------------------------------------------------


def test_arrival_order_is_stable_and_is_the_tab_order():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")
    store.register("tok-b", pid=2, cwd="/repo/two")
    store.register("tok-c", pid=3, cwd="/repo/three")

    assert [s.token for s in store.sessions()] == ["tok-a", "tok-b", "tok-c"]
    assert [s.arrival_index for s in store.sessions()] == [0, 1, 2]


def test_removal_never_reorders_survivors():
    store, _clock = _make_store()
    for index, token in enumerate(["tok-a", "tok-b", "tok-c", "tok-d"]):
        store.register(token, pid=index, cwd="/repo/one")

    store.deregister("tok-b")

    assert [s.token for s in store.sessions()] == ["tok-a", "tok-c", "tok-d"]

    store.deregister("tok-a")

    assert [s.token for s in store.sessions()] == ["tok-c", "tok-d"]


def test_remove_agent_drops_only_that_providers_sessions_and_private_state():
    store, _clock = _make_store()
    claude = store.register("tok-claude", agent="claude")
    codex = store.register("tok-codex", agent="codex")
    store.record_event("tok-claude", "Stop", agent="claude")
    store.record_event("tok-codex", "Stop", agent="codex")
    assert store.record_response("tok-claude", "claude response") is True
    assert store.record_response("tok-codex", "codex response") is True
    assert store.set_pending_reply(claude.session_handle, "claude reply") is True
    assert store.set_pending_reply(codex.session_handle, "codex reply") is True

    removed = store.remove_agent("codex")

    assert removed == (codex.session_handle,)
    assert store.sessions() == (claude,)
    assert store.get_by_handle(codex.session_handle) is None
    assert store.view(codex.session_handle) is None
    assert store.take_pending_reply(codex.session_handle) is None
    assert store.view(claude.session_handle).latest_response == "claude response"
    assert store.take_pending_reply(claude.session_handle) == "claude reply"


def test_reregistration_never_reorders():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")
    store.register("tok-b", pid=2, cwd="/repo/two")

    store.register("tok-a", pid=1, cwd="/repo/one")

    assert [s.token for s in store.sessions()] == ["tok-a", "tok-b"]


def test_a_readded_token_after_removal_arrives_at_the_end():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")
    store.register("tok-b", pid=2, cwd="/repo/two")
    store.deregister("tok-a")

    store.register("tok-a", pid=3, cwd="/repo/one")

    assert [s.token for s in store.sessions()] == ["tok-b", "tok-a"]


# ---------------------------------------------------------------------------
# Invariant: reaping is inspection-only
# ---------------------------------------------------------------------------


def test_deregister_removes_the_record():
    store, _clock = _make_store()
    session = store.register("tok-a", pid=1, cwd="/repo/one")

    assert store.deregister("tok-a") is True
    assert len(store) == 0
    assert store.get_by_handle(session.session_handle) is None


def test_reap_removes_sessions_with_expired_delivery_polls():
    store, clock = _make_store(session_ttl_seconds=90.0)
    store.register("tok-a", pid=1, cwd="/repo/one")
    clock.advance(30.0)
    store.register("tok-b", pid=2, cwd="/repo/two")
    clock.advance(70.0)

    removed = store.reap()

    assert len(removed) == 1
    assert [s.token for s in store.sessions()] == ["tok-b"]


def test_reap_removes_sessions_whose_pid_is_gone():
    probed: list[int] = []

    def probe(pid: int) -> bool:
        probed.append(pid)
        return pid != 2

    store, _clock = _make_store(pid_is_alive=probe)
    store.register("tok-a", pid=1, cwd="/repo/one")
    store.register("tok-b", pid=2, cwd="/repo/two")

    removed = store.reap()

    assert probed == [1, 2]
    assert len(removed) == 1
    assert [s.token for s in store.sessions()] == ["tok-a"]


def test_reap_keeps_a_session_whose_pid_cannot_be_probed():
    def probe(pid: int) -> bool:
        raise OSError("probe failed")

    store, _clock = _make_store(pid_is_alive=probe)
    store.register("tok-a", pid=1, cwd="/repo/one")

    assert store.reap() == ()
    assert len(store) == 1


def test_reap_is_inspection_only_and_never_signals_the_process(monkeypatch):
    """The default liveness probe must not construct a killable handle."""

    def explode(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("the store must never construct psutil.Process")

    monkeypatch.setattr(psutil, "Process", explode)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)

    # No pid_is_alive override: exercise the module's real default probe.
    store = ManagedSessionStore(handle_factory=_handle_factory())
    store.register("tok-a", pid=424242, cwd="/repo/one")

    removed = store.reap()

    assert len(removed) == 1
    assert len(store) == 0


def test_no_kill_signal_or_terminate_call_is_reachable_from_the_store():
    """Static guard: scope decision 8 forbids the app ever killing a wrapper."""
    source = Path(managed_sessions.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden = {
        "kill",
        "killpg",
        "terminate",
        "send_signal",
        "TerminateProcess",
        "taskkill",
        "SIGKILL",
        "SIGTERM",
    }
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Import):
            referenced.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            referenced.add((node.module or "").split(".")[0])
            referenced.update(alias.name for alias in node.names)

    assert not (referenced & forbidden)
    assert "signal" not in referenced
    assert "subprocess" not in referenced


def test_deregistration_only_removes_the_record():
    """Deregistering must not touch the process, only the bookkeeping."""
    probed: list[int] = []
    store, _clock = _make_store(pid_is_alive=lambda pid: probed.append(pid) or True)
    store.register("tok-a", pid=99, cwd="/repo/one")

    store.deregister("tok-a")

    assert probed == []


# ---------------------------------------------------------------------------
# Naming: reuse the existing agent_activity / agent_session_names path
# ---------------------------------------------------------------------------


def test_source_name_uses_the_project_basename_like_agent_activity():
    store, _clock = _make_store()

    windows = store.register("tok-a", pid=1, cwd=r"C:\Users\USERNAME\Documents\my-repo")
    posix = store.register("tok-b", pid=2, cwd="/home/dev/other-repo")

    assert windows.source_name == "my-repo"
    assert posix.source_name == "other-repo"


def test_source_name_falls_back_to_the_agent_label():
    store, _clock = _make_store()

    session = store.register("tok-a", pid=1, cwd=None)

    assert session.source_name == "Claude"


def test_source_name_is_sanitized_and_capped():
    store, _clock = _make_store()

    session = store.register("tok-a", pid=1, cwd="/repo/" + "n" * 200)

    assert len(session.source_name) <= 48
    assert "/" not in session.source_name


def test_an_explicit_custom_name_wins_over_the_basename():
    store, _clock = _make_store()

    session = store.register(
        "tok-a", pid=1, cwd="/repo/one", custom_name="Refactor pass"
    )

    assert session.source_name == "Refactor pass"


def test_a_hostile_custom_name_is_sanitized():
    store, _clock = _make_store()

    session = store.register("tok-a", pid=1, cwd="/repo/one", custom_name="a\nb/c")

    assert "\n" not in session.source_name
    assert "/" not in session.source_name


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_sessions_returns_a_detached_tuple():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")

    snapshot = store.sessions()
    store.register("tok-b", pid=2, cwd="/repo/two")

    assert len(snapshot) == 1
    assert isinstance(snapshot, tuple)


def test_clear_empties_the_store():
    store, _clock = _make_store()
    store.register("tok-a", pid=1, cwd="/repo/one")

    store.clear()

    assert len(store) == 0
    assert store.sessions() == ()


def test_default_session_ttl_is_exposed():
    store = ManagedSessionStore()

    assert store.session_ttl_seconds == DEFAULT_SESSION_TTL_SECONDS
    assert DEFAULT_SESSION_TTL_SECONDS > 0


@pytest.mark.parametrize("bad", [0, -1, "many"])
def test_max_sessions_must_be_a_positive_int(bad):
    with pytest.raises(ValueError):
        ManagedSessionStore(max_sessions=bad)
