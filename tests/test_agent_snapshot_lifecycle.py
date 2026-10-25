"""Server-lifetime ordering survives content-owner teardown and recreation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from tests.test_claude_hook_endpoint import QUESTION, _make_client

pytestmark = pytest.mark.usefixtures("accepted_eula")


def test_interaction_replacement_removes_terminal_and_survives_owner_recreation():
    client, server = _make_client()
    events = []
    server.broadcast = events.append
    try:
        broker = server._get_agent_interactions()
        item = broker.create("opaque-session", "permission")
        pending = server._agent_interactions_snapshot_event()
        assert pending["interactions"][0]["interaction"]["request_id"] == item.request_id
        server._clear_agent_speak_state(reason="test")
        cleared = [e for e in events if e["type"] == "agent.interactions.snapshot"][-1]
        assert cleared["interactions"] == []
        assert cleared["revision"] > pending["revision"]
        server._get_agent_interactions().create("new-session", "permission")
        current = server._agent_interactions_snapshot_event()
        assert current["revision"] > cleared["revision"]
        assert len(current["interactions"]) == 1
    finally:
        server._clear_agent_speak_state(reason="test-cleanup")
        client.close()


def test_delayed_interaction_snapshot_cannot_revive_retired_broker():
    client, server = _make_client()
    broker = server._get_agent_interactions()
    broker.create("opaque-session", "permission")
    entered, release = Event(), Event()
    original = broker.versioned_snapshot

    def delayed(next_revision):
        result = original(next_revision)
        entered.set()
        assert release.wait(5)
        return result

    broker.versioned_snapshot = delayed
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(server._agent_interactions_snapshot_event)
            assert entered.wait(5)
            server._clear_agent_speak_state(reason="test")
            server._get_agent_interactions().create("new-session", "permission")
            current = server._agent_interactions_snapshot_event()
            release.set()
            replay = pending.result(timeout=5)
            assert replay["interactions"] == []
            assert replay["revision"] < current["revision"]
    finally:
        release.set()
        server._clear_agent_speak_state(reason="test-cleanup")
        client.close()


def _activate(coordinator, family, index):
    if family == "question":
        coordinator.surface(f"session-{index}", QUESTION["tool_input"]["questions"])
    else:
        coordinator.enqueue(
            completion_id=f"completion-{index}", sequence=index,
            grace_seconds=0, speech_start=lambda callback: True,
        )
        # Publication is asynchronous and coalesced. Observe the completed
        # snapshot before testing a later lifecycle's ordering boundary.
        worker = coordinator._publisher._thread
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive()


@pytest.mark.parametrize("family", ["question", "response"])
def test_absent_owner_read_cannot_borrow_recreated_owner_revision(family):
    client, server = _make_client()
    getter = getattr(server, f"_get_agent_{family}_coordinator")
    builder = (
        server._agent_question_snapshot_event if family == "question"
        else server._agent_response_queue_snapshot_event
    )
    armed = True

    class RecreateAfterAbsentRead(type(server)):
        def __getattribute__(self, name):
            nonlocal armed
            value = super().__getattribute__(name)
            if name == f"_claude_{family}_coordinator" and armed:
                armed = False
                # The pending builder observed no owner, but a hook creates
                # one before it assembles the empty snapshot's revision.
                _activate(getter(), family, 1)
            return value

    server.__class__ = RecreateAfterAbsentRead
    try:
        empty = builder()
        active = builder()
        assert empty["active"] is None
        assert active["active"] is not None
        assert empty["revision"] < active["revision"]
    finally:
        server._clear_agent_speak_state(reason="test-cleanup")
        client.close()


@pytest.mark.parametrize("family", ["question", "response"])
def test_snapshots_remain_ordered_across_repeated_owner_recreation(family):
    client, server = _make_client()
    events = []
    server.broadcast = events.append
    getter = getattr(server, f"_get_agent_{family}_coordinator")
    event_type = f"agent.{'response_queue' if family == 'response' else family}.snapshot"
    previous_revision = -1
    try:
        for cycle in range(3):
            coordinator = getter()
            for index in range(4):
                _activate(coordinator, family, cycle * 10 + index)
            active = [event for event in events if event["type"] == event_type][-1]
            assert active["active"] is not None
            assert active["revision"] > previous_revision
            server._clear_agent_speak_state(reason="test-toggle")
            empty = [event for event in events if event["type"] == event_type][-1]
            assert empty["active"] is None
            assert empty["revision"] > active["revision"]
            previous_revision = empty["revision"]
            assert getattr(server, f"_agent_{family}_coordinator") is None
    finally:
        server._clear_agent_speak_state(reason="test-cleanup")
        client.close()


@pytest.mark.parametrize("family", ["question", "response"])
def test_retired_state_callback_cannot_publish_after_recreation(family):
    client, server = _make_client()
    events = []
    server.broadcast = events.append
    getter = getattr(server, f"_get_agent_{family}_coordinator")
    old = getter()
    try:
        _activate(old, family, 1)
        server._clear_agent_speak_state(reason="test-toggle")
        _activate(getter(), family, 2)
        events.clear()
        # Even an old owner changing after retirement cannot stamp its content
        # into the new lifecycle, despite sharing the monotonic allocator.
        _activate(old, family, 3)
        old._state_changed()
        assert events == []
    finally:
        old.clear()
        server._clear_agent_speak_state(reason="test-cleanup")
        client.close()


@pytest.mark.parametrize("family", ["question", "response"])
@pytest.mark.parametrize("publication", [True, False])
def test_snapshot_pending_during_clear_cannot_publish_into_new_lifecycle(family, publication):
    client, server = _make_client()
    events = []
    server.broadcast = events.append
    getter = getattr(server, f"_get_agent_{family}_coordinator")
    builder = (
        server._agent_question_snapshot_event if family == "question"
        else server._agent_response_queue_snapshot_event
    )
    old = getter()
    _activate(old, family, 1)
    entered, release = Event(), Event()
    original_snapshot = old.snapshot

    def delayed_snapshot():
        payload = original_snapshot()
        entered.set()
        assert release.wait(5)
        return payload

    old.snapshot = delayed_snapshot
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(old._state_changed if publication else builder)
            assert entered.wait(5)
            # Clear itself can emit a callback. Only the already-pending read
            # waits, so restore the real snapshot before performing teardown.
            old.snapshot = original_snapshot
            server._clear_agent_speak_state(reason="test-toggle")
            _activate(getter(), family, 2)
            current = builder()
            events.clear()
            release.set()
            replay = pending.result(timeout=5)
            if not publication:
                assert replay["active"] is None
                assert replay["revision"] < current["revision"]
        assert events == []
    finally:
        release.set()
        server._clear_agent_speak_state(reason="test-cleanup")
        client.close()
