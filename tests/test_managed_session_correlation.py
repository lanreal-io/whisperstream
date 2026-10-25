"""Tests for correlating `/api/claude-hook` events to managed sessions (Task 2.4).

Hook payloads carry `session_id`, `cwd`, `prompt_id`, and `transcript_path`
but no PID and no TTY, and `cwd` collides for two sessions in one repo. The
forwarder therefore echoes a wrapper-minted correlation token as a header on
every forwarded hook (`claude_hud.correlation_fields` + `server.py`'s
`_MANAGED_SESSION_TOKEN_HEADER`), and the backend binds or rebinds that
token's `session_id` in the managed-session store.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from whisperstream.claude_hud import correlation_fields
from whisperstream.server.app import _MANAGED_SESSION_TOKEN_HEADER

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
    # None keeps the SubagentStop -> "progress" branch a pure {} return, with
    # no speech-controller side effects to mock.
    app.speech_controller = None
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    return TestClient(server.fastapi, raise_server_exceptions=True, headers={"x-whisperstream-hook-token": server._agent_hook_token}), server


def _subagent_stop_body(session_id: str) -> dict:
    return {"hook_event_name": "SubagentStop", "session_id": session_id}


def _post_hook(client, body, *, token: "str | None" = None):
    headers = {_MANAGED_SESSION_TOKEN_HEADER: token} if token else {}
    return client.post("/api/claude-hook", json=body, headers=headers)


def _post_codex_hook(client, body, *, token: "str | None" = None):
    headers = {_MANAGED_SESSION_TOKEN_HEADER: token} if token else {}
    return client.post("/api/codex-hook", json=body, headers=headers)


# ---------------------------------------------------------------------------
# correlation_fields (pure helper)
# ---------------------------------------------------------------------------


class TestCorrelationFields:
    def test_returns_raw_event_name_and_session_id(self):
        assert correlation_fields(
            {"hook_event_name": "Stop", "session_id": "sid-1"}
        ) == ("Stop", "sid-1")

    def test_returns_raw_event_name_when_session_id_absent(self):
        assert correlation_fields({"hook_event_name": "Stop"}) == ("Stop", None)

    def test_none_for_non_dict_payload(self):
        assert correlation_fields("not-a-dict") is None
        assert correlation_fields(None) is None

    def test_none_when_hook_event_name_missing_or_not_a_string(self):
        assert correlation_fields({}) is None
        assert correlation_fields({"hook_event_name": 5}) is None
        assert correlation_fields({"hook_event_name": ""}) is None


# ---------------------------------------------------------------------------
# Backend correlation via /api/claude-hook
# ---------------------------------------------------------------------------


def test_event_with_token_binds_session_id():
    client, server = _make_client()
    store = server._get_managed_sessions()
    store.register("wrapper-tok", pid=111, cwd="/repo/one")

    resp = _post_hook(
        client, _subagent_stop_body("sid-1"), token="wrapper-tok"
    )

    assert resp.status_code == 200
    session = store.get("wrapper-tok")
    assert session.session_id == "sid-1"
    assert session.last_event_kind == "SubagentStop"


def test_second_event_with_different_session_id_rebinds_not_duplicates():
    client, server = _make_client()
    store = server._get_managed_sessions()
    store.register("wrapper-tok", pid=111, cwd="/repo/one")

    _post_hook(client, _subagent_stop_body("sid-1"), token="wrapper-tok")
    _post_hook(client, _subagent_stop_body("sid-2"), token="wrapper-tok")

    assert len(store) == 1
    assert store.get("wrapper-tok").session_id == "sid-2"


def test_event_with_unknown_token_creates_no_session():
    client, server = _make_client()
    store = server._get_managed_sessions()

    resp = _post_hook(
        client, _subagent_stop_body("sid-1"), token="never-registered"
    )

    assert resp.status_code == 200
    assert len(store) == 0


def test_event_with_no_token_leaves_behavior_byte_identical():
    """Unwrapped sessions (scope decision 10) are completely unaffected."""
    client, server = _make_client()
    store = server._get_managed_sessions()

    with_token = _post_hook(
        client, _subagent_stop_body("sid-1"), token="wrapper-tok"
    )
    without_token_client, without_token_server = _make_client()
    without_token_resp = without_token_client.post(
        "/api/claude-hook", json=_subagent_stop_body("sid-1")
    )

    assert without_token_resp.status_code == 200
    assert without_token_resp.json() == with_token.json() == {}
    assert len(without_token_server._get_managed_sessions()) == 0
    # The token-bearing call above hit an unregistered token, so it also
    # created no session -- confirming the only difference the header makes
    # is *whether a matching record gets updated*, never the hook response.
    assert len(store) == 0


def test_correlation_never_raises_when_the_store_lookup_fails(monkeypatch):
    """A correlation bug must never turn a hook response non-empty or fail closed."""
    client, server = _make_client()
    store = server._get_managed_sessions()
    store.register("wrapper-tok", pid=111, cwd="/repo/one")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "record_event", _boom)

    resp = _post_hook(
        client, _subagent_stop_body("sid-1"), token="wrapper-tok"
    )

    assert resp.status_code == 200
    assert resp.json() == {}


def test_codex_session_start_proves_the_plugin_and_opens_the_initial_prompt():
    client, server = _make_client()
    store = server._get_managed_sessions()
    session = store.register("codex-wrapper", agent="codex", cwd="/repo/one")
    assert store.can_reply(session) is False

    response = _post_codex_hook(
        client,
        {"hook_event_name": "SessionStart", "session_id": "codex-sid"},
        token="codex-wrapper",
    )

    assert response.json() == {}
    assert session.session_id == "codex-sid"
    assert session.last_event_kind == "SessionStart"
    assert store.can_reply(session) is True


def test_codex_stop_retains_the_response_on_the_exact_managed_route():
    client, server = _make_client()
    server._queue_agent_response = MagicMock(return_value=None)
    store = server._get_managed_sessions()
    session = store.register("codex-wrapper", agent="codex")

    response = _post_codex_hook(
        client,
        {
            "hook_event_name": "Stop",
            "session_id": "codex-sid",
            "turn_id": "turn-1",
            "last_assistant_message": "managed Codex response",
        },
        token="codex-wrapper",
    )

    assert response.json() == {}
    assert store.view(session.session_handle).latest_response == "managed Codex response"


def test_provider_mismatched_tokens_are_inert_on_both_hook_endpoints():
    client, server = _make_client()
    store = server._get_managed_sessions()
    claude = store.register("claude-wrapper", agent="claude")
    codex = store.register("codex-wrapper", agent="codex")

    _post_codex_hook(
        client,
        {"hook_event_name": "SessionStart", "session_id": "wrong-codex"},
        token="claude-wrapper",
    )
    _post_hook(
        client,
        {"hook_event_name": "SessionStart", "session_id": "wrong-claude"},
        token="codex-wrapper",
    )

    assert claude.session_id is None
    assert codex.session_id is None


def test_codex_subagent_lifecycle_does_not_mutate_the_root_pty_route():
    client, server = _make_client()
    store = server._get_managed_sessions()
    session = store.register("codex-wrapper", agent="codex")

    _post_codex_hook(
        client,
        {
            "hook_event_name": "Stop",
            "session_id": "root-sid",
            "agent_id": "subagent-1",
            "last_assistant_message": "subagent response",
        },
        token="codex-wrapper",
    )

    assert session.session_id is None
    assert session.last_event_kind is None


@pytest.mark.parametrize(
    "event_name",
    # Neither is recognized by parse_claude_event today (SessionEnd lands in
    # Task 2.5; PostToolUse is never handled), so each falls through to the
    # HUD's "unknown event" branch and the hook response is {}. Correlation
    # runs *before* that branch and must still record the raw kind: the
    # managed-session store's vocabulary is the raw hook name, not something
    # gated on whether the HUD itself understands the event.
    ["SessionEnd", "PostToolUse"],
)
def test_correlation_records_the_raw_hook_event_kind_even_when_hud_ignores_it(
    event_name,
):
    client, server = _make_client()
    store = server._get_managed_sessions()
    store.register("wrapper-tok", pid=111, cwd="/repo/one")

    resp = _post_hook(
        client,
        {"hook_event_name": event_name, "session_id": "sid-1"},
        token="wrapper-tok",
    )

    assert resp.status_code == 200
    assert resp.json() == {}
    assert store.get("wrapper-tok").last_event_kind == event_name
    assert store.get("wrapper-tok").session_id == "sid-1"
