"""Blocking Codex hook endpoint and shared provider-runtime contracts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.usefixtures("accepted_eula")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_events"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_mock_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.enhancement_provider = "none"
    cfg.enhancement_local_model = "gemma4-e2b-instruct"
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.claude_hud_enabled = False
    cfg.codex_hud_enabled = True
    cfg.claude_hud_show_text = True
    cfg.lockdown_mode = False
    cfg.agent_speech_enabled = False
    cfg.agent_summary_text_enabled = False
    cfg.agent_speech_activation = "always_on"
    cfg.claude_reply_grace_seconds = 6
    cfg.claude_reply_queue_delay_seconds = 2
    cfg.claude_reply_auto_submit = False
    cfg.claude_permission_decisions_enabled = False
    cfg.start_sound_path = None
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.to_dict = lambda: {}
    return cfg


def _make_client(*, config=None, speech_controller=None) -> tuple[TestClient, object]:
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
    app.speech_controller = speech_controller
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    return TestClient(server.fastapi, raise_server_exceptions=True, headers={"x-whisperstream-hook-token": server._agent_hook_token}), server


def _resolve_during_wait(server, decision) -> None:
    broker = server._get_agent_interactions()

    def wait(request_id: str):
        broker.resolve(request_id, decision)
        return broker.get(request_id)

    broker.wait = wait

    class ImmediateCoordinator:
        def enqueue(self, **kwargs):
            speech_start = kwargs.get("speech_start")
            if speech_start is not None:
                speech_start(lambda _reason: None)
            interaction = kwargs.get("interaction")
            waiter_loop = kwargs.get("waiter_loop")
            if interaction is None or waiter_loop is None:
                return None
            broker.activate_stop(interaction.request_id)
            broker.resolve(interaction.request_id, decision)
            future = waiter_loop.create_future()
            future.set_result(broker.get(interaction.request_id))
            return future

        @staticmethod
        def snapshot():
            return {
                "version": 1,
                "revision": 1,
                "phase": "idle",
                "ordered_completion_ids": [],
                "active": None,
                "quiet_delay_remaining_seconds": 0.0,
                "retained_drafts": [],
            }

    server._agent_response_coordinator = ImmediateCoordinator()


def test_codex_gate_and_malformed_requests_fail_open() -> None:
    config = _make_mock_config(codex_hud_enabled=False)
    client, _server = _make_client(config=config)
    try:
        assert client.post("/api/codex-hook", json=_fixture("codex_stop.json")).json() == {}
        config.codex_hud_enabled = True
        assert client.post(
            "/api/codex-hook",
            content=b"not-json",
            headers={"content-type": "application/json"},
        ).json() == {}
    finally:
        client.close()


def _codex_rollout(tmp_path: Path, source: object) -> str:
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"source": source}}) + "\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.mark.parametrize(
    "patch",
    [
        {"agent_id": "worker-1"},
        {"transcript_path": None},
        "exec",
        {"subagent": {"thread_spawn": {"depth": 1}}},
    ],
)
def test_codex_noninteractive_stop_fails_open_without_hold(tmp_path, patch) -> None:
    from whisperstream.agent_session_filter import clear_session_filter_cache

    clear_session_filter_cache()
    body = _fixture("codex_stop.json")
    if isinstance(patch, dict) and set(patch) <= {"agent_id", "transcript_path"}:
        body.update(patch)
    else:
        body["transcript_path"] = _codex_rollout(tmp_path, patch)
    client, server = _make_client()
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append
    try:
        response = client.post("/api/codex-hook", json=body)
    finally:
        client.close()

    assert response.json() == {}
    assert broadcasts == []
    assert server._agent_interactions is None


def test_codex_interactive_rollout_stop_still_reaches_the_hud(tmp_path) -> None:
    from whisperstream.agent_session_filter import clear_session_filter_cache

    clear_session_filter_cache()
    client, server = _make_client()
    _resolve_during_wait(server, "Keep going.")
    body = {**_fixture("codex_stop.json"), "transcript_path": _codex_rollout(tmp_path, "vscode")}
    try:
        response = client.post("/api/codex-hook", json=body)
    finally:
        client.close()

    assert response.json() == {"decision": "block", "reason": "Keep going."}


def test_codex_stop_reply_uses_exact_continuation_schema() -> None:
    client, server = _make_client()
    _resolve_during_wait(server, "Run the focused regression test.")
    try:
        response = client.post("/api/codex-hook", json=_fixture("codex_stop.json"))
    finally:
        client.close()

    assert response.json() == {
        "decision": "block",
        "reason": "Run the focused regression test.",
    }


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (
            "allow",
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            },
        ),
        (
            "deny",
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny"},
                }
            },
        ),
        (None, {}),
    ],
)
def test_codex_permission_decisions_use_exact_schema(decision, expected) -> None:
    client, server = _make_client(
        config=_make_mock_config(claude_permission_decisions_enabled=True)
    )
    _resolve_during_wait(server, decision)
    try:
        response = client.post(
            "/api/codex-hook", json=_fixture("codex_permission.json")
        )
    finally:
        client.close()

    assert response.json() == expected


def test_codex_permission_gate_defers_to_native_prompt() -> None:
    client, server = _make_client()
    try:
        response = client.post(
            "/api/codex-hook", json=_fixture("codex_permission.json")
        )
    finally:
        client.close()

    assert response.json() == {}
    assert server._agent_interactions is None


def test_codex_prompt_returns_only_additional_context_and_drops_prompt(caplog) -> None:
    payload = {**_fixture("codex_prompt_submit.json"), "prompt": "PRIVATE_PROMPT"}
    controller = MagicMock()
    client, server = _make_client(speech_controller=controller)
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append
    try:
        with caplog.at_level("DEBUG"):
            response = client.post("/api/codex-hook", json=payload)
    finally:
        client.close()

    assert response.json() == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ANY,
        }
    }
    assert "PRIVATE_PROMPT" not in caplog.text
    assert "PRIVATE_PROMPT" not in json.dumps(broadcasts)
    controller.stop.assert_not_called()
    assert any(
        event.get("type") == "agent.hud.activity"
        and event.get("agent") == "codex"
        and event.get("active") is True
        for event in broadcasts
    )


def test_codex_completion_is_labeled_and_deduped_independently_from_claude() -> None:
    client, server = _make_client(
        config=_make_mock_config(claude_hud_enabled=True, codex_hud_enabled=True)
    )
    broadcasts: list[dict] = []
    server.broadcast = broadcasts.append
    server._queue_agent_response = MagicMock(return_value=None)
    codex = _fixture("codex_stop.json")
    claude = {
        "hook_event_name": "Stop",
        "session_id": codex["session_id"],
        "prompt_id": codex["turn_id"],
        "cwd": codex["cwd"],
        "last_assistant_message": codex["last_assistant_message"],
    }
    try:
        assert client.post("/api/codex-hook", json=codex).json() == {}
        assert client.post("/api/claude-hook", json=claude).json() == {}
    finally:
        client.close()

    completions = [
        event["completion"]
        for event in broadcasts
        if event.get("type") == "agent.completion"
        and event.get("completion", {}).get("kind") == "response"
    ]
    assert [item["agent"] for item in completions] == ["codex", "claude"]
    assert server._queue_agent_response.call_count == 2


def test_codex_timeout_and_internal_failure_fail_open_without_content_logging(caplog) -> None:
    client, server = _make_client()
    payload = {
        **_fixture("codex_stop.json"),
        "session_id": "PRIVATE_TIMEOUT_SESSION",
        "last_assistant_message": "PRIVATE_TIMEOUT_CONTENT",
    }
    server._queue_agent_response = MagicMock(side_effect=RuntimeError("synthetic"))
    try:
        with caplog.at_level("WARNING"):
            response = client.post("/api/codex-hook", json=payload)
    finally:
        client.close()

    assert response.json() == {}
    assert "PRIVATE_TIMEOUT_SESSION" not in caplog.text
    assert "PRIVATE_TIMEOUT_CONTENT" not in caplog.text
