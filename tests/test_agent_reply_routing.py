"""Server routing through the single reply owner.

Push-to-talk, Enter, Escape and delivery all read one foreground and one key
policy. These tests drive the real server, the real reply desk and the real
managed-session store; the hook coordinator is a small fake with the same
surface (open-Stop lookup, engage, resolve).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_claude_hook_endpoint import _make_client, _make_mock_config
from whisperstream.agent_reply_desk import question_target, reply_target

pytestmark = pytest.mark.usefixtures("accepted_eula")


class FakeHook:
    """Response coordinator double: one open Stop per conversation."""

    def __init__(self, live: set[str] | None = None, accept: bool = True):
        self.live = set(live or ())
        self.accept = accept
        self.engaged: list[str] = []
        self.resolved: list[tuple[str, str]] = []
        self.capture_active: list[bool] = []
        self.skipped = 0
        self.speaking = False

    def live_reply_remaining(self, conversation):
        return 30.0 if conversation in self.live else None

    def engage(self, conversation):
        if conversation not in self.live:
            return None
        self.engaged.append(conversation)
        return 30.0

    def resolve_reply(self, conversation, text, *, on_resolved=None):
        if conversation not in self.live or not self.accept:
            return None
        self.resolved.append((conversation, text))
        self.live.discard(conversation)
        interaction = SimpleNamespace(status="resolved", kind="stop", session_handle=conversation, request_id="r1", decision=text)
        if on_resolved is not None:
            on_resolved(interaction)
        return interaction

    def set_capture_active(self, active):
        self.capture_active.append(active)

    def set_compose_hold(self, active):
        self.compose_hold = active

    def is_speaking(self):
        return self.speaking

    def can_dismiss_responses(self):
        return True

    def skip_current(self):
        self.skipped += 1
        return True

    def active_conversation(self):
        return next(iter(self.live), None)

    def active_session_handles(self):
        return set(self.live)


class FakeQuestion:
    def __init__(self, session="qs", generation=4):
        self.target = (session, generation)
        self.answers: list[str] = []
        self.deferred = 0
        self.phases: list[str] = []

    def current_target(self):
        return self.target

    def active_session_handles(self):
        return {self.target[0]}

    def begin_capture(self):
        return 30.0

    def submit_text(self, session, generation, text):
        self.answers.append(text)
        return True

    def can_defer(self):
        return True

    def defer_current(self):
        self.deferred += 1


@pytest.fixture
def server_client():
    client, server = _make_client()
    server._app.start_agent_compose = MagicMock(return_value=True)
    server._app.stop_agent_compose = MagicMock(return_value=True)
    server._app.cancel_agent_compose = MagicMock(return_value=True)
    server._claude_question_coordinator = None
    server._claude_response_coordinator = None
    try:
        yield server
    finally:
        client.close()


def present(server, target, surface="console", *, painted=True, focused=False):
    desk = server._get_reply_desk()
    return desk.present(
        seq=desk.presented().seq + 1,
        target_id=target,
        surface=surface,
        painted=painted,
        focused=focused,
    )


def wrapped(server, token="wrapper-token", sid="claude-session", event="Stop"):
    store = server._get_managed_sessions()
    session = store.register(token, pid=None, cwd="/repo/one")
    server._record_managed_event(token, event, agent="claude", session_id=sid)
    return store, session, server._managed_reply_target(session)


def compose(server, generation, *states, text="hello there"):
    for state in states:
        if state == "final":
            event = {"type": "agent.compose.final", "generation": generation, "text": text}
        else:
            event = {"type": "agent.compose.state", "generation": generation, "state": state}
        server._observe_routed_compose_event(event)


def dictate(server, text="hello there", mode="push_to_talk"):
    assert server.start_routed_reply(mode) is True
    generation = server._get_agent_compose_leases().current().generation
    compose(server, generation, "processing", "final", "idle", text=text)
    return generation


# -- push-to-talk ------------------------------------------------------------


@pytest.mark.parametrize("mode", ["push_to_talk", "toggle"])
def test_nothing_painted_falls_through_to_dictation(server_client, mode):
    wrapped(server_client)
    assert server_client.start_routed_reply(mode) is False
    server_client._app.start_agent_compose.assert_not_called()


def test_console_covered_by_another_surface_falls_through(server_client):
    _store, _session, target = wrapped(server_client)
    present(server_client, target, painted=False)
    assert server_client.start_routed_reply("push_to_talk") is False


def test_painted_console_captures_into_its_draft(server_client):
    store, session, target = wrapped(server_client)
    present(server_client, target)
    dictate(server_client, "hi console")
    draft = server_client._reply_desk.draft(target)
    assert (draft.text, draft.phase) == ("hi console", "ready")


def test_hook_card_and_console_share_one_draft(server_client):
    hook = FakeHook()
    server_client._claude_response_coordinator = hook
    _store, _session, target = wrapped(server_client)
    conversation = target.split(":", 1)[1]
    hook.live.add(conversation)
    present(server_client, target, "card")
    dictate(server_client, "from the card")
    present(server_client, target, "console")
    assert server_client._reply_desk.draft(target).text == "from the card"
    assert hook.engaged == [conversation]


def test_compose_start_failure_falls_through_and_keeps_text(server_client):
    _store, _session, target = wrapped(server_client)
    present(server_client, target)
    server_client._reply_desk.update(target_id=target, base_revision=0, text="typed")
    server_client._app.start_agent_compose.return_value = False
    assert server_client.start_routed_reply("push_to_talk") is False
    assert server_client._get_agent_compose_leases().current() is None
    draft = server_client._reply_desk.draft(target)
    assert (draft.text, draft.phase) == ("typed", "editing")


def test_question_outranks_console_for_voice(server_client):
    question = FakeQuestion()
    server_client._claude_question_coordinator = question
    _store, _session, target = wrapped(server_client)
    present(server_client, target)
    server_client._sync_reply_question()
    # Covered console: the question is foreground but not painted yet.
    assert server_client.start_routed_reply("push_to_talk") is False
    present(server_client, question_target("qs", 4), "question")
    dictate(server_client, "stable please")
    assert server_client._reply_desk.draft(question_target("qs", 4)).text == "stable please"
    assert server_client._reply_desk.draft(target) is None


def test_lockdown_falls_through(server_client):
    _store, _session, target = wrapped(server_client)
    present(server_client, target)
    server_client._diagnostics_locked = lambda: True
    assert server_client.start_routed_reply("push_to_talk") is False


# -- capture lifetime --------------------------------------------------------


def test_capture_idle_never_erases_typed_text(server_client):
    _store, _session, target = wrapped(server_client)
    present(server_client, target)
    desk = server_client._reply_desk
    desk.update(target_id=target, base_revision=0, text="keep this")
    assert server_client.start_routed_reply("toggle") is True
    generation = server_client._get_agent_compose_leases().current().generation
    compose(server_client, generation, "processing", "idle")
    draft = desk.draft(target)
    assert (draft.text, draft.phase) == ("keep this", "editing")


def test_redictation_appends(server_client):
    _store, _session, target = wrapped(server_client)
    present(server_client, target)
    dictate(server_client, "one")
    dictate(server_client, "two")
    assert server_client._reply_desk.draft(target).text == "one two"


# -- Enter -------------------------------------------------------------------


def test_enter_claims_ready_draft_and_delivers_once(server_client):
    store, session, target = wrapped(server_client)
    present(server_client, target)
    dictate(server_client, "ship it")
    assert server_client.should_consume_response_enter() is True
    server_client.submit_ready_response_draft()
    # Handed to the wrapper: kept, read-only, until it is collected.
    assert server_client._reply_desk.draft(target).phase == "sending"
    assert store.take_pending_reply(session.session_handle) == "ship it"
    assert server_client._reply_desk.draft(target) is None
    receipt = server_client._reply_desk.receipts()[-1]
    assert (receipt.target_id, receipt.text) == (target, "ship it")
    # The claim was consumed: a second callback does nothing.
    server_client.submit_ready_response_draft()


def test_enter_takes_a_spoken_draft_even_while_hud_focused(server_client):
    """A spoken draft has no text box to own Enter (owner, 2026-10-25)."""
    _store, _session, target = wrapped(server_client)
    present(server_client, target, focused=True)
    dictate(server_client, "spoken while focused")
    assert server_client.should_consume_response_enter() is True


def test_enter_never_claims_an_editing_draft(server_client):
    _store, _session, target = wrapped(server_client)
    present(server_client, target)
    server_client._reply_desk.update(target_id=target, base_revision=0, text="typed")
    assert server_client.should_consume_response_enter() is False


def test_claimed_enter_is_bound_to_its_draft(server_client):
    store, session, target = wrapped(server_client)
    present(server_client, target)
    dictate(server_client, "first")
    assert server_client.should_consume_response_enter() is True
    # The draft changes between key-down and the callback: nothing is sent.
    desk = server_client._reply_desk
    desk.update(target_id=target, base_revision=desk.draft(target).text_revision, text="changed")
    server_client.submit_ready_response_draft()
    assert store.take_pending_reply(session.session_handle) is None
    assert desk.draft(target).text == "changed"


def test_refused_send_keeps_text_and_releases_enter(server_client):
    store, session, target = wrapped(server_client)
    present(server_client, target)
    dictate(server_client, "hello")
    store.get_by_handle(session.session_handle).ended = True
    assert server_client.should_consume_response_enter() is True
    server_client.submit_ready_response_draft()
    draft = server_client._reply_desk.draft(target)
    assert draft.text == "hello" and draft.outcome[1] == "refused"
    assert server_client.should_consume_response_enter() is False


def test_hook_refusal_keeps_text_and_releases_enter(server_client):
    hook = FakeHook({"conv-x"}, accept=False)
    server_client._claude_response_coordinator = hook
    target = reply_target("conv-x")
    present(server_client, target, "card")
    dictate(server_client, "hi")
    assert server_client.should_consume_response_enter() is True
    server_client.submit_ready_response_draft()
    assert server_client._reply_desk.draft(target).outcome[1] == "refused"
    assert server_client.should_consume_response_enter() is False


# -- delivery ----------------------------------------------------------------


def test_live_hook_wins_over_managed_transport(server_client):
    hook = FakeHook()
    server_client._claude_response_coordinator = hook
    store, session, target = wrapped(server_client)
    conversation = target.split(":", 1)[1]
    hook.live.add(conversation)
    present(server_client, target, "card")
    dictate(server_client, "once")
    server_client.submit_ready_response_draft() if server_client.should_consume_response_enter() else None
    assert hook.resolved == [(conversation, "once")]
    assert store.take_pending_reply(session.session_handle) is None


def test_working_session_holds_reply_for_next_stop(server_client):
    store, session, target = wrapped(server_client, event="UserPromptSubmit")
    present(server_client, target)
    desk = server_client._reply_desk
    desk.update(target_id=target, base_revision=0, text="later")
    draft = desk.draft(target)
    assert server_client._submit_reply(draft.draft_id, draft.text_revision) == "applied"
    assert store.has_held_reply(session.session_handle)
    held = desk.draft(target)
    assert (held.phase, held.held, held.text) == ("sending", True, "later")
    server_client._record_managed_event("wrapper-token", "Stop", agent="claude", session_id="claude-session")
    assert store.take_pending_reply(session.session_handle) == "later"
    assert desk.draft(target) is None
    assert desk.receipts()[-1].text == "later"


def test_dropped_held_reply_comes_back_with_copy(server_client):
    store, session, target = wrapped(server_client, event="UserPromptSubmit")
    desk = server_client._get_reply_desk()
    desk.update(target_id=target, base_revision=0, text="held text")
    draft = desk.draft(target)
    server_client._submit_reply(draft.draft_id, draft.text_revision)
    server_client._record_managed_event("wrapper-token", "SessionEnd", agent="claude", session_id="claude-session")
    back = desk.draft(target)
    assert (back.text, back.phase, back.outcome[1]) == ("held text", "editing", "not_delivered")


def test_uncollected_send_is_withdrawn_and_comes_back(server_client):
    store, session, target = wrapped(server_client)
    desk = server_client._get_reply_desk()
    desk.update(target_id=target, base_revision=0, text="nobody home")
    draft = desk.draft(target)
    server_client._submit_reply(draft.draft_id, draft.text_revision)
    send_id = store.pending_tags(session.session_handle)[0]
    # The pickup window ends: withdrawn, never sent later.
    assert store.withdraw_pending_reply(session.session_handle, send_id) is True
    assert store.take_pending_reply(session.session_handle) is None
    back = desk.draft(target)
    assert (back.text, back.outcome[1]) == ("nobody home", "not_delivered")


def test_second_submit_while_first_is_out_is_refused(server_client):
    store, session, target = wrapped(server_client)
    desk = server_client._get_reply_desk()
    desk.update(target_id=target, base_revision=0, text="once")
    draft = desk.draft(target)
    assert server_client._submit_reply(draft.draft_id, draft.text_revision) == "applied"
    assert server_client._submit_reply(draft.draft_id, draft.text_revision) == "refused"
    assert desk.draft(target).outcome is None  # the first send owns the result
    assert store.take_pending_reply(session.session_handle) == "once"
    assert store.take_pending_reply(session.session_handle) is None


def test_unwrapped_card_without_route_still_takes_dictation(server_client):
    # Owner decision: a painted card takes the dictation even with no route;
    # Enter then refuses and the HUD offers Copy.
    server_client._claude_response_coordinator = FakeHook()
    target = reply_target("closed-hook")
    present(server_client, target, "card")
    dictate(server_client, "keep me")
    draft = server_client._reply_desk.draft(target)
    assert draft.text == "keep me"
    assert server_client._agent_reply_snapshot_event()["drafts"][0]["delivery"] == "none"


def test_typed_submit_sends_exact_hud_text(server_client):
    store, session, target = wrapped(server_client)
    present(server_client, target, focused=True)
    desk = server_client._reply_desk
    desk.update(target_id=target, base_revision=0, text="draf")
    draft = desk.draft(target)
    assert server_client._submit_reply(draft.draft_id, draft.text_revision, "draft!") == "applied"
    assert store.take_pending_reply(session.session_handle) == "draft!"


def test_question_answer_delivers_through_question(server_client):
    question = FakeQuestion()
    server_client._claude_question_coordinator = question
    server_client._sync_reply_question()
    present(server_client, question_target("qs", 4), "question")
    dictate(server_client, "beta")
    assert server_client.should_consume_response_enter() is True
    server_client.submit_ready_response_draft()
    assert question.answers == ["beta"]


def test_auto_submit_delivers_on_final():
    client, server = _make_client(config=_make_mock_config(claude_reply_auto_submit=True))
    try:
        server._app.start_agent_compose = MagicMock(return_value=True)
        server._claude_question_coordinator = None
        server._claude_response_coordinator = None
        store, session, target = wrapped(server)
        present(server, target)
        dictate(server, "auto")
        assert store.take_pending_reply(session.session_handle) == "auto"
    finally:
        client.close()


# -- Escape ------------------------------------------------------------------


def test_escape_while_recording_cancels_only_the_recording(server_client):
    _store, _session, target = wrapped(server_client)
    present(server_client, target)
    desk = server_client._reply_desk
    desk.update(target_id=target, base_revision=0, text="prior")
    assert server_client.start_routed_reply("toggle") is True
    assert server_client.should_consume_response_escape() is True
    server_client.dismiss_response_routes()
    draft = desk.draft(target)
    assert (draft.text, draft.phase) == ("prior", "editing")
    server_client._app.cancel_agent_compose.assert_called_once()


def test_escape_on_console_dismisses_and_discards(server_client):
    _store, _session, target = wrapped(server_client)
    present(server_client, target)
    server_client._reply_desk.update(target_id=target, base_revision=0, text="bye")
    server_client.broadcast = MagicMock()
    assert server_client.should_consume_response_escape() is True
    server_client.dismiss_response_routes()
    assert server_client._reply_desk.draft(target) is None
    closes = [c.args[0] for c in server_client.broadcast.call_args_list if c.args[0].get("type") == "agent.reply.close"]
    assert closes == [{"type": "agent.reply.close", "version": 1, "target_id": target, "surface": "console"}]


def test_escape_on_speaking_console_stops_speech_then_dismisses(server_client):
    hook = FakeHook({"c"})
    hook.speaking = True
    server_client._claude_response_coordinator = hook
    server_client._app.speech_controller = MagicMock()
    target = reply_target("c")
    present(server_client, target)
    server_client._reply_desk.update(target_id=target, base_revision=0, text="keep")
    server_client.broadcast = MagicMock()
    assert server_client.should_consume_response_escape() is True
    assert server_client.dismiss_response_routes() is True
    # First Escape: silence only. The console and its draft stay.
    server_client._app.speech_controller.stop.assert_called_once()
    assert server_client._reply_desk.draft(target).text == "keep"
    assert not [c for c in server_client.broadcast.call_args_list if c.args[0].get("type") == "agent.reply.close"]
    hook.speaking = False
    assert server_client.should_consume_response_escape() is True
    assert server_client.dismiss_response_routes() is True
    assert server_client._reply_desk.draft(target) is None
    assert hook.skipped == 0


def test_escape_on_card_skips_response_and_keeps_draft(server_client):
    hook = FakeHook({"c"})
    server_client._claude_response_coordinator = hook
    target = reply_target("c")
    present(server_client, target, "card")
    server_client._reply_desk.update(target_id=target, base_revision=0, text="for later")
    assert server_client.should_consume_response_escape() is True
    assert server_client.dismiss_response_routes() is True
    assert hook.skipped == 1
    assert server_client._reply_desk.draft(target).text == "for later"


def test_escape_claims_pair_with_their_callbacks(server_client):
    server_client._claude_response_coordinator = FakeHook({"a", "b"})
    present(server_client, reply_target("a"), "card")
    assert server_client.should_consume_response_escape() is True
    present(server_client, reply_target("b"), "console")
    assert server_client.should_consume_response_escape() is True
    server_client.broadcast = MagicMock()
    server_client.dismiss_response_routes()
    server_client.dismiss_response_routes()
    assert server_client.dismiss_response_routes() is False
    closes = [c.args[0]["surface"] for c in server_client.broadcast.call_args_list if c.args[0].get("type") == "agent.reply.close"]
    assert closes == ["card", "console"]


def test_escape_with_question_over_console_defers_question_only(server_client):
    question = FakeQuestion()
    server_client._claude_question_coordinator = question
    _store, _session, target = wrapped(server_client)
    present(server_client, target)
    server_client._reply_desk.update(target_id=target, base_revision=0, text="hidden draft")
    server_client._sync_reply_question()
    # Covered console owns nothing.
    assert server_client.should_consume_response_escape() is False
    present(server_client, question_target("qs", 4), "question")
    assert server_client.should_consume_response_escape() is True
    server_client.dismiss_response_routes()
    assert question.deferred == 1
    assert server_client._reply_desk.draft(target).text == "hidden draft"


def test_escape_unpainted_is_not_consumed(server_client):
    server_client._claude_response_coordinator = FakeHook({"c"})
    assert server_client.should_consume_response_escape() is False


# -- draft lifetime across transports ---------------------------------------


def test_session_id_rebind_keeps_draft(server_client):
    store, session, old_target = wrapped(server_client)
    desk = server_client._get_reply_desk()
    desk.update(target_id=old_target, base_revision=0, text="survive /clear")
    server_client._record_managed_event("wrapper-token", "SessionStart", agent="claude", session_id="claude-new")
    new_target = server_client._managed_reply_target(store.get_by_handle(session.session_handle))
    assert new_target != old_target
    assert desk.draft(new_target).text == "survive /clear"


def test_unbound_wrapped_session_takes_no_reply(server_client):
    store = server_client._get_managed_sessions()
    session = store.register("t2", pid=None, cwd="/repo/two")
    assert server_client._managed_reply_target(session) is None


def test_reap_keeps_draft(server_client):
    store, session, target = wrapped(server_client)
    desk = server_client._get_reply_desk()
    desk.update(target_id=target, base_revision=0, text="asleep")
    store.deregister("wrapper-token")
    assert desk.draft(target).text == "asleep"


def test_session_end_keeps_draft_without_a_route(server_client):
    _store, _session, target = wrapped(server_client)
    desk = server_client._get_reply_desk()
    desk.update(target_id=target, base_revision=0, text="copy me")
    server_client._record_managed_event("wrapper-token", "SessionEnd", agent="claude", session_id="claude-session")
    assert desk.draft(target).text == "copy me"
    assert server_client._reply_delivery(target) == "none"


def test_provider_disable_scrubs_hook_only_drafts_without_a_store(server_client):
    desk = server_client._get_reply_desk()
    target = reply_target(server_client._agent_session_handle("claude-sid", "claude"))
    desk.update(target_id=target, base_revision=0, text="hook only")
    assert server_client._managed_sessions is None
    server_client._remove_managed_sessions_for_agent("claude")
    assert desk.draft(target) is None


def test_question_change_cancels_capture_into_old_question(server_client):
    question = FakeQuestion()
    server_client._claude_question_coordinator = question
    server_client._sync_reply_question()
    old = question_target("qs", 4)
    present(server_client, old, "question")
    assert server_client.start_routed_reply("toggle") is True
    question.target = ("qs", 5)
    server_client._sync_reply_question()
    server_client._app.cancel_agent_compose.assert_called_once()
    assert server_client._reply_desk.draft(old) is None
    assert server_client._reply_desk.capturing() is None


def test_provider_disable_drops_only_that_provider(server_client):
    _store, _session, target = wrapped(server_client)
    desk = server_client._get_reply_desk()
    desk.update(target_id=target, base_revision=0, text="claude text")
    codex_target = reply_target(server_client._agent_session_handle("codex-sid", "codex"))
    desk.update(target_id=codex_target, base_revision=0, text="codex text")
    server_client._remove_managed_sessions_for_agent("claude")
    assert desk.draft(target) is None
    assert desk.draft(codex_target).text == "codex text"


def test_socket_close_revokes_presented(server_client):
    _store, _session, target = wrapped(server_client)
    desk = server_client._get_reply_desk()
    sock = object()
    desk.present(seq=0, target_id=target, surface="console", painted=True, focused=False, socket=sock)
    server_client._release_reply_socket(sock)
    assert desk.key_policy.ptt is None


def test_hook_ceiling_keeps_card_only_without_other_route(server_client):
    _store, _session, target = wrapped(server_client)
    conversation = target.split(":", 1)[1]
    desk = server_client._get_reply_desk()
    desk.update(target_id=target, base_revision=0, text="unsent")
    assert server_client._reply_draft_present(conversation) is False  # console can still send
    desk.update(target_id=reply_target("hook-only"), base_revision=0, text="unsent")
    assert server_client._reply_draft_present("hook-only") is True


# -- wire commands -----------------------------------------------------------


def test_ws_commands_are_correlated(server_client):
    from fastapi.testclient import TestClient

    _store, session, target = wrapped(server_client)
    with TestClient(server_client.fastapi).websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        ws.send_json({"type": "agent.hud.presented", "seq": 1, "target_id": target, "surface": "console", "painted": True, "focused": True})
        ws.send_json({"type": "agent.draft.update", "command_id": 7, "target_id": target, "base_revision": 0, "text": "hi"})
        result = _receive(ws, "agent.reply.result")
        assert (result["command_id"], result["result"], result["text_revision"]) == (7, "applied", 1)
        ws.send_json({"type": "agent.draft.update", "command_id": 8, "target_id": target, "base_revision": 0, "text": "hx"})
        assert _receive(ws, "agent.reply.result")["result"] == "conflict"
        ws.send_json({"type": "agent.draft.submit", "command_id": 9, "draft_id": result["draft_id"], "revision": 1, "text": "hi!"})
        assert _receive(ws, "agent.reply.result")["result"] == "applied"
    assert server_client._get_managed_sessions().take_pending_reply(session.session_handle) == "hi!"


def _receive(ws, event_type):
    for _ in range(50):
        event = ws.receive_json()
        if event.get("type") == event_type:
            return event
    raise AssertionError(f"no {event_type}")


@pytest.mark.parametrize(
    "session_state,hook_live,expected",
    [
        ("idle", False, "now"),
        ("working", False, "hold"),
        ("stale", False, "none"),
        ("ended", False, "none"),
        ("absent", False, "none"),
        ("idle", True, "now"),
        ("working", True, "now"),
        ("absent", True, "now"),
    ],
)
def test_delivery_is_decided_at_submit_from_live_routes(server_client, session_state, hook_live, expected):
    hook = FakeHook()
    server_client._claude_response_coordinator = hook
    if session_state == "absent":
        target = reply_target("hook-only-conv")
    else:
        event = "UserPromptSubmit" if session_state == "working" else "Stop"
        store, session, target = wrapped(server_client, event=event)
        if session_state == "stale":
            store.get_by_handle(session.session_handle).last_seen -= 10_000
        if session_state == "ended":
            store.get_by_handle(session.session_handle).ended = True
    if hook_live:
        hook.live.add(target.split(":", 1)[1])
    assert server_client._reply_delivery(target) == expected


def test_writing_in_the_focused_console_holds_new_responses(server_client):
    hook = FakeHook()
    server_client._claude_response_coordinator = hook
    target = reply_target("c1")
    desk = server_client._get_reply_desk()
    present(server_client, target, "console", focused=True)
    desk.update(target_id=target, base_revision=0, text="half a thought")
    assert hook.compose_hold is True

    # Focus leaves the console: queued responses may play again.
    present(server_client, target, "console", focused=False)
    assert hook.compose_hold is False


def _user_turns(server):
    return [
        turn["text"]
        for session in server._agent_conversations.snapshot()
        for turn in session["turns"]
        if turn["role"] == "user"
    ]


def test_reply_enters_history_only_once_the_wrapper_collects_it(server_client):
    store, session, target = wrapped(server_client)
    desk = server_client._get_reply_desk()
    desk.update(target_id=target, base_revision=0, text="collected")
    draft = desk.draft(target)
    server_client._submit_reply(draft.draft_id, draft.text_revision)
    assert _user_turns(server_client) == []
    assert store.take_pending_reply(session.session_handle) == "collected"
    assert _user_turns(server_client) == ["collected"]


def test_withdrawn_reply_never_enters_history(server_client):
    store, session, target = wrapped(server_client)
    desk = server_client._get_reply_desk()
    desk.update(target_id=target, base_revision=0, text="never arrived")
    draft = desk.draft(target)
    server_client._submit_reply(draft.draft_id, draft.text_revision)
    send_id = store.pending_tags(session.session_handle)[0]
    store.withdraw_pending_reply(session.session_handle, send_id)
    assert _user_turns(server_client) == []



def _reply_ws(server, message):
    import asyncio

    asyncio.run(server._handle_reply_ws_message({"version": 1, **message}, MagicMock()))


def test_explicit_stale_target_never_changes_a_managed_draft(server_client):
    store, session, target = wrapped(server_client)
    present(server_client, target, "console")
    desk = server_client._get_reply_desk()
    assert desk.update(target_id=target, base_revision=0, text="correct route").result == "applied"
    draft = desk.draft(target)
    for wrong in (
        {"draft_id": "stale-draft", "revision": draft.text_revision},
        {"draft_id": draft.draft_id, "revision": draft.text_revision + 1},
        {"target_id": "conv:wrong-session", "revision": draft.text_revision},
    ):
        _reply_ws(server_client, {"type": "agent.draft.submit", "text": "must not be sent", **wrong})
        assert store.take_pending_reply(session.session_handle) is None
        assert desk.draft(target).text == "correct route"
    _reply_ws(server_client, {
        "type": "agent.draft.update", "target_id": target, "draft_id": "stale-draft",
        "base_revision": draft.text_revision, "text": "must not be sent",
    })
    assert desk.draft(target).text == "correct route"
    _reply_ws(server_client, {
        "type": "agent.draft.submit", "draft_id": draft.draft_id,
        "revision": draft.text_revision, "text": "correct route",
    })
    assert store.take_pending_reply(session.session_handle) == "correct route"


# -- reply hint retirement ---------------------------------------------------


def _wait_for_hint_saves() -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name == "reply-hint-save":
            thread.join(timeout=5)


def test_reply_hint_retires_after_three_dictated_replies(server_client, monkeypatch):
    import whisperstream.server.app as app_module

    monkeypatch.setattr(app_module, "replace", lambda config: config)
    server_client._app.config.claude_reply_hint_dictated_replies = 0
    store, session, target = wrapped(server_client)
    present(server_client, target)
    for count in (1, 2, 3):
        assert server_client._agent_reply_snapshot_event()["reply_hint_retired"] is False
        dictate(server_client, f"reply {count}")
        assert server_client.should_consume_response_enter() is True
        server_client.submit_ready_response_draft()
        assert store.take_pending_reply(session.session_handle) == f"reply {count}"
        assert server_client._app.config.claude_reply_hint_dictated_replies == count
    # The snapshot published with the third receipt already says so.
    assert server_client._agent_reply_snapshot_event()["reply_hint_retired"] is True
    _wait_for_hint_saves()
    saved = server_client._app.config_manager.save.call_args.args[0]
    assert saved.claude_reply_hint_dictated_replies == 3


def test_typed_replies_do_not_retire_the_reply_hint(server_client):
    server_client._app.config.claude_reply_hint_dictated_replies = 2
    store, session, target = wrapped(server_client)
    present(server_client, target, focused=True)
    desk = server_client._reply_desk
    desk.update(target_id=target, base_revision=0, text="typed")
    draft = desk.draft(target)
    assert server_client._submit_reply(draft.draft_id, draft.text_revision, "typed") == "applied"
    assert store.take_pending_reply(session.session_handle) == "typed"
    assert server_client._app.config.claude_reply_hint_dictated_replies == 2
    assert server_client._agent_reply_snapshot_event()["reply_hint_retired"] is False
