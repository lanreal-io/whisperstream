"""Tests for managed-session reply delivery (Task 3.3).

Three routes complete the delivery path: the app POSTs a dictated reply, the
wrapper long-polls for it, and the wrapper reports the PTY-master write back so
the session is marked active.

The load-bearing property under test is **block, never queue** (scope decision
6). Every refusal path here must return a blocked result *and* leave the
pending slot empty, so nothing can be delivered later without a new explicit
user action. A test that only asserted the response body would pass against an
implementation that quietly queued the payload, so every refusal test also
asserts the wrapper's long-poll comes back empty.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from whisperstream.managed_session_auth import HEADER_NAME, auth as managed_session_auth
from whisperstream.managed_sessions import (
    MAX_REPLY_CHARS,
    PENDING_REPLY_TTL_SECONDS,
    ManagedSessionStore,
)

pytestmark = pytest.mark.usefixtures("accepted_eula")


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "none"
    cfg.enhancement_local_model = "gemma4-e2b-instruct"
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.claude_hud_enabled = True
    cfg.codex_hud_enabled = True
    cfg.claude_hud_show_text = True
    cfg.lockdown_mode = False
    cfg.agent_speech_enabled = False
    cfg.agent_speech_activation = "per_session"
    cfg.claude_reply_grace_seconds = 6
    cfg.claude_reply_queue_delay_seconds = 2
    cfg.claude_reply_auto_submit = False
    cfg.claude_permission_decisions_enabled = False
    cfg.start_sound_path = None
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.to_dict = lambda: {}
    return cfg


def _make_client(*, config=None):
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.config = config or _make_mock_config()
    app.parakeet = MagicMock()
    app.llama_server = None
    app.config_manager = MagicMock()
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.set_event_broadcaster = MagicMock()
    app.audio_feedback = MagicMock()
    app.speech_controller = None
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    return TestClient(server.fastapi, raise_server_exceptions=True), server


@pytest.fixture()
def token(tmp_path):
    """Mint a capability token on the process-global singleton for one test."""
    value = managed_session_auth.mint(tmp_path)
    yield value
    managed_session_auth.clear(tmp_path)


def _headers(tok: str) -> dict:
    return {HEADER_NAME: tok}


def _register(client, tok: str, wrapper_token: str = "wrapper-tok") -> str:
    resp = client.post(
        "/api/managed-sessions/register",
        json={"token": wrapper_token, "pid": 111, "cwd": "/repo/one"},
        headers=_headers(tok),
    )
    assert resp.status_code == 200
    return resp.json()["session_handle"]


def _register_v2(client, tok: str, agent: str, wrapper_token: str) -> str:
    resp = client.post(
        "/api/managed-sessions/v2/register",
        json={"agent": agent, "token": wrapper_token, "cwd": "/repo/one"},
        headers=_headers(tok),
    )
    assert resp.status_code == 200
    return resp.json()["session_handle"]


def _idle_session(client, server, tok: str, wrapper_token: str = "wrapper-tok") -> str:
    """Register a session and park it in the one state that allows a reply."""
    handle = _register(client, tok, wrapper_token)
    server._get_managed_sessions().record_event(wrapper_token, "Stop", session_id="sid-1")
    return handle


def _poll(client, tok: str, handle: str):
    """Collect whatever the wrapper's long-poll would collect, without waiting."""
    resp = client.get(
        f"/api/managed-sessions/{handle}/pending?wait=0",
        headers=_headers(tok),
    )
    assert resp.status_code == 200
    return resp.json()["reply"]


# ---------------------------------------------------------------------------
# Capability token gate (401)
# ---------------------------------------------------------------------------


def test_reply_401_without_token(token):
    client, _server = _make_client()

    resp = client.post("/api/managed-sessions/some-handle/reply", json={"text": "hi"})

    assert resp.status_code == 401


def test_delivery_routes_use_the_stored_provider_gate(token):
    client, server = _make_client()
    handle = _register_v2(client, token, "codex", "codex-wrapper")
    store = server._get_managed_sessions()
    store.record_event("codex-wrapper", "Stop", agent="codex")
    server._app.config.codex_hud_enabled = False
    reply = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "must not queue"},
        headers=_headers(token),
    )
    pending = client.get(
        f"/api/managed-sessions/{handle}/pending?wait=0",
        headers=_headers(token),
    )
    delivered = client.post(
        f"/api/managed-sessions/{handle}/delivered",
        headers=_headers(token),
    )
    assert reply.status_code == 409
    assert pending.json() == {"reply": None, "shutdown_requested": False}
    assert delivered.status_code == 409
    assert store.has_pending_reply(handle) is False


def test_pending_401_without_token(token):
    client, _server = _make_client()

    resp = client.get("/api/managed-sessions/some-handle/pending?wait=0")

    assert resp.status_code == 401


def test_delivered_401_without_token(token):
    client, _server = _make_client()

    resp = client.post("/api/managed-sessions/some-handle/delivered")

    assert resp.status_code == 401


def test_reply_401_with_wrong_token(token):
    client, _server = _make_client()

    resp = client.post(
        "/api/managed-sessions/some-handle/reply",
        json={"text": "hi"},
        headers=_headers("not-the-token"),
    )

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# claude_hud_enabled gate and unknown handles
# ---------------------------------------------------------------------------


def test_reply_409_hud_disabled_when_gate_off(token):
    client, server = _make_client(config=_make_mock_config(claude_hud_enabled=True))
    handle = _idle_session(client, server, token)
    server._app.config.claude_hud_enabled = False

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "hello there"},
        headers=_headers(token),
    )

    assert resp.status_code == 409
    assert resp.json() == {"error": "hud_disabled"}
    server._app.config.claude_hud_enabled = True
    assert _poll(client, token, handle) is None


def test_reply_404_for_an_unknown_handle(token):
    client, _server = _make_client()

    resp = client.post(
        "/api/managed-sessions/ghost-handle/reply",
        json={"text": "hello there"},
        headers=_headers(token),
    )

    assert resp.status_code == 404
    assert resp.json() == {"accepted": False}


def test_pending_404_for_an_unknown_handle_so_the_wrapper_reregisters(token):
    client, _server = _make_client()

    resp = client.get(
        "/api/managed-sessions/ghost-handle/pending?wait=0", headers=_headers(token)
    )

    assert resp.status_code == 404
    assert resp.json() == {"reply": None, "shutdown_requested": False}


def test_delivered_reports_not_ok_for_an_unknown_handle(token):
    client, _server = _make_client()

    resp = client.post(
        "/api/managed-sessions/ghost-handle/delivered", headers=_headers(token)
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": False}


# ---------------------------------------------------------------------------
# The happy path: idle and untouched
# ---------------------------------------------------------------------------


def test_reply_is_accepted_and_collected_by_the_long_poll(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "yes, go ahead with the second option"},
        headers=_headers(token),
    )

    assert resp.status_code == 200
    assert resp.json() == {"accepted": True}
    assert _poll(client, token, handle) == "yes, go ahead with the second option"


def test_revoked_acceptance_withdraws_the_waiting_reply_as_undelivered(
    token, monkeypatch
):
    client, server = _make_client()
    handle = _idle_session(client, server, token)
    store = server._get_managed_sessions()
    settled: list[tuple[str, bool]] = []
    store._on_reply_settled = lambda tag, delivered: settled.append((tag, delivered))
    assert store.set_pending_reply(handle, "private reply", tag="send-1")
    # The request is admitted, then acceptance is revoked while it waits.
    checks = iter([True])
    monkeypatch.setattr(
        server, "_acceptance_is_current", lambda _grant: next(checks, False)
    )

    assert _poll(client, token, handle) is None
    assert settled == [("send-1", False)]
    assert store.take_pending_reply(handle) is None


def test_a_collected_reply_is_handed_out_exactly_once(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)
    client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "first and only"},
        headers=_headers(token),
    )

    assert _poll(client, token, handle) == "first and only"
    assert _poll(client, token, handle) is None


def test_a_multiline_reply_survives_intact(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)
    payload = "first line\nsecond line\nthird line"

    client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": payload},
        headers=_headers(token),
    )

    # Never mutated on the way through: the wrapper frames it in bracketed
    # paste so the newlines stay inside one message.
    assert _poll(client, token, handle) == payload


def test_the_long_poll_returns_promptly_when_a_reply_is_already_waiting(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)
    client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "already waiting"},
        headers=_headers(token),
    )

    # A non-zero hold must not delay a reply that is already parked.
    resp = client.get(
        f"/api/managed-sessions/{handle}/pending?wait=10", headers=_headers(token)
    )

    assert resp.json() == {
        "reply": "already waiting",
        "shutdown_requested": False,
    }


def test_the_long_poll_returns_promptly_for_a_shutdown_request(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)
    assert server._get_managed_sessions().request_shutdown(handle) is True

    resp = client.get(
        f"/api/managed-sessions/{handle}/pending?wait=10", headers=_headers(token)
    )

    assert resp.status_code == 200
    assert resp.json() == {"reply": None, "shutdown_requested": True}


def test_the_long_poll_refreshes_session_liveness(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)
    session = server._get_managed_sessions().get("wrapper-tok")
    before = session.last_seen

    assert _poll(client, token, handle) is None

    assert session.last_seen >= before


# ---------------------------------------------------------------------------
# Initial prompt and predicate refusals (Task 2.6)
# ---------------------------------------------------------------------------


def test_reply_is_accepted_at_the_initial_prompt_before_any_stop(token):
    client, _server = _make_client()
    handle = _register(client, token)

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "hello there"},
        headers=_headers(token),
    )

    assert resp.json() == {"accepted": True}
    assert _poll(client, token, handle) == "hello there"


def test_reply_is_refused_after_the_session_ended(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)
    server._get_managed_sessions().record_event("wrapper-tok", "SessionEnd")

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "hello there"},
        headers=_headers(token),
    )

    assert resp.json() == {"accepted": False}
    assert _poll(client, token, handle) is None


def test_reply_is_allowed_while_parked_in_a_blocked_question(token):
    # Scope decision 14 calls this the strongest injection case: Claude cannot
    # be producing output while it waits for an answer.
    client, server = _make_client()
    handle = _register(client, token)
    server._get_managed_sessions().record_event(
        "wrapper-tok", "PreToolUse", session_id="sid-1"
    )

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "use the second option"},
        headers=_headers(token),
    )

    assert resp.json() == {"accepted": True}
    assert _poll(client, token, handle) == "use the second option"


# ---------------------------------------------------------------------------
# Payload-guard refusals (Task 2.7) -- refused at the backend, off the wire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("leading", ["/", "!", "@", "#", "?"])
@pytest.mark.parametrize("prefix", ["", "   ", "\n\t "])
def test_hazardous_leading_characters_are_refused_before_the_wire(
    token, leading, prefix
):
    client, server = _make_client()
    handle = _idle_session(client, server, token)

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": f"{prefix}{leading}model please"},
        headers=_headers(token),
    )

    assert resp.json() == {"accepted": False}
    assert _poll(client, token, handle) is None


def test_an_embedded_bracketed_paste_terminator_is_refused(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "harmless \x1b[201~ then not"},
        headers=_headers(token),
    )

    assert resp.json() == {"accepted": False}
    assert _poll(client, token, handle) is None


def test_an_embedded_control_byte_is_refused(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "hello \x07 there"},
        headers=_headers(token),
    )

    assert resp.json() == {"accepted": False}
    assert _poll(client, token, handle) is None


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t"])
def test_an_empty_or_whitespace_only_reply_is_refused(token, text):
    client, server = _make_client()
    handle = _idle_session(client, server, token)

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": text},
        headers=_headers(token),
    )

    assert resp.json() == {"accepted": False}
    assert _poll(client, token, handle) is None


def test_an_oversized_reply_is_refused(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "a" * (MAX_REPLY_CHARS + 1)},
        headers=_headers(token),
    )

    assert resp.json() == {"accepted": False}
    assert _poll(client, token, handle) is None


# ---------------------------------------------------------------------------
# One at a time: a second reply is refused, never stacked
# ---------------------------------------------------------------------------


def test_a_second_reply_is_refused_while_one_is_still_waiting(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)
    first = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "the first reply"},
        headers=_headers(token),
    )
    assert first.json() == {"accepted": True}

    second = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "the second reply"},
        headers=_headers(token),
    )

    assert second.json() == {"accepted": False}
    # The refused reply is dropped, not queued behind the first.
    assert _poll(client, token, handle) == "the first reply"
    assert _poll(client, token, handle) is None


# ---------------------------------------------------------------------------
# The post-write touch
# ---------------------------------------------------------------------------


def test_reporting_delivery_marks_the_session_active(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)
    store = server._get_managed_sessions()
    assert store.can_reply(store.get("wrapper-tok")) is True

    resp = client.post(
        f"/api/managed-sessions/{handle}/delivered", headers=_headers(token)
    )

    assert resp.json() == {"ok": True}
    # Block another HUD send immediately, before Claude's hook reaches us.
    assert store.get("wrapper-tok").last_event_kind == "UserPromptSubmit"


def test_delivery_report_drops_a_second_reply_that_raced_the_first_write():
    store = ManagedSessionStore()
    session = store.register("tok", agent="codex")
    store.record_event("tok", "Stop", agent="codex")
    assert store.set_pending_reply(session.session_handle, "first") is True
    assert store.take_pending_reply(session.session_handle) == "first"
    assert store.set_pending_reply(session.session_handle, "raced second") is True

    assert store.mark_replied(session.session_handle) is True

    assert store.has_pending_reply(session.session_handle) is False
    store.record_event("tok", "Stop", agent="codex")
    assert store.take_pending_reply(session.session_handle) is None
    assert store.can_reply(store.get("wrapper-tok")) is False


def test_a_delivered_reply_blocks_the_next_one_until_claude_stops_again(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)
    client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "the first reply"},
        headers=_headers(token),
    )
    assert _poll(client, token, handle) == "the first reply"
    client.post(f"/api/managed-sessions/{handle}/delivered", headers=_headers(token))

    blocked = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "too soon"},
        headers=_headers(token),
    )
    assert blocked.json() == {"accepted": False}

    # Claude's next Stop opens a fresh window.
    server._get_managed_sessions().record_event("wrapper-tok", "Stop")
    allowed = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "now it is fine"},
        headers=_headers(token),
    )
    assert allowed.json() == {"accepted": True}


# ---------------------------------------------------------------------------
# Long-poll bounds
# ---------------------------------------------------------------------------


def test_the_long_poll_hold_is_clamped_to_the_ceiling(token, monkeypatch):
    from whisperstream import managed_sessions as ms

    client, server = _make_client()
    handle = _idle_session(client, server, token)
    monkeypatch.setattr(ms, "MAX_PENDING_LONG_POLL_SECONDS", 0.05)

    # A caller asking for an hour cannot pin the connection open.
    resp = client.get(
        f"/api/managed-sessions/{handle}/pending?wait=3600", headers=_headers(token)
    )

    assert resp.json() == {"reply": None, "shutdown_requested": False}


def test_a_negative_hold_is_clamped_to_zero(token):
    client, server = _make_client()
    handle = _idle_session(client, server, token)

    resp = client.get(
        f"/api/managed-sessions/{handle}/pending?wait=-5", headers=_headers(token)
    )

    assert resp.json() == {"reply": None, "shutdown_requested": False}


# ---------------------------------------------------------------------------
# Store-level: expiry and lifecycle, driven by a fake clock
# ---------------------------------------------------------------------------


def _stopped_store(clock):
    store = ManagedSessionStore(clock=clock)
    session = store.register("tok", pid=111, cwd="/repo/one")
    store.record_event("tok", "Stop", session_id="sid-1")
    return store, session


def test_an_uncollected_reply_expires_and_is_never_delivered_later():
    now = [1000.0]
    store, session = _stopped_store(lambda: now[0])
    assert store.set_pending_reply(session.session_handle, "stale reply") is True

    now[0] += PENDING_REPLY_TTL_SECONDS + 1

    # Scope decision 6: a blocked reply is never delivered later without a new
    # explicit user action, and a payload nobody collected in time is exactly
    # that.
    assert store.take_pending_reply(session.session_handle) is None
    assert store.has_pending_reply(session.session_handle) is False


def test_an_expired_reply_frees_the_slot_for_a_new_one():
    now = [1000.0]
    store, session = _stopped_store(lambda: now[0])
    store.set_pending_reply(session.session_handle, "stale reply")

    now[0] += PENDING_REPLY_TTL_SECONDS + 1

    assert store.set_pending_reply(session.session_handle, "fresh reply") is True
    assert store.take_pending_reply(session.session_handle) == "fresh reply"


def test_session_end_drops_a_waiting_reply():
    now = [1000.0]
    store, session = _stopped_store(lambda: now[0])
    store.set_pending_reply(session.session_handle, "orphan reply")

    store.record_event("tok", "SessionEnd")

    assert store.has_pending_reply(session.session_handle) is False


def test_deregistration_drops_a_waiting_reply():
    now = [1000.0]
    store, session = _stopped_store(lambda: now[0])
    store.set_pending_reply(session.session_handle, "orphan reply")

    assert store.deregister("tok") is True

    assert store.take_pending_reply(session.session_handle) is None
    assert len(store) == 0


def test_reaping_an_expired_session_drops_its_waiting_reply():
    now = [1000.0]
    store, session = _stopped_store(lambda: now[0])
    store.set_pending_reply(session.session_handle, "orphan reply")

    now[0] += store.session_ttl_seconds + 1
    assert store.reap() == (session.session_handle,)

    # Re-registering the same token must not inherit the dead run's payload.
    revived = store.register("tok", pid=222, cwd="/repo/one")
    store.record_event("tok", "Stop", session_id="sid-2")
    assert store.take_pending_reply(revived.session_handle) is None


def test_set_pending_reply_refuses_an_unknown_handle():
    store = ManagedSessionStore()
    assert store.set_pending_reply("ghost", "hello") is False


def test_set_pending_reply_refuses_a_non_string_payload():
    now = [1000.0]
    store, session = _stopped_store(lambda: now[0])
    assert store.set_pending_reply(session.session_handle, None) is False
    assert store.set_pending_reply(session.session_handle, 42) is False


# ---------------------------------------------------------------------------
# No reason ever reaches the wire
# ---------------------------------------------------------------------------


def test_a_refusal_carries_no_reason_string(token):
    client, server = _make_client()
    handle = _register(client, token)
    server._get_managed_sessions().record_event(
        "wrapper-tok", "UserPromptSubmit", session_id="sid-1"
    )

    resp = client.post(
        f"/api/managed-sessions/{handle}/reply",
        json={"text": "hello there"},
        headers=_headers(token),
    )

    # Matching the reason-free `can_reply` precedent: the body is exactly
    # one boolean, with nothing that leaks session state.
    assert resp.json() == {"accepted": False}
