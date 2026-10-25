"""Exact Codex hook adapter contracts and privacy boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whisperstream.agent_events import SessionActivationRegistry, SessionNameRegistry
from whisperstream.codex_hud import SPOKEN_SUMMARY_CONTEXT, parse_codex_event


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_events"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_stop_maps_exact_official_fields_without_transcript_path() -> None:
    payload = _fixture("codex_stop.json")

    result = parse_codex_event(payload)

    assert result == {
        "kind": "response",
        "agent": "codex",
        "session_id": "fixture-codex-session",
        "turn_id": "fixture-codex-turn",
        "cwd": "C:/Users/USERNAME/Documents/Dev/whisperstream",
        "text": "Codex finished the change.\n🔊 Codex finished the change.",
        "empty": False,
        "spoken_summary": "Codex finished the change.",
        "park_eligible": True,
    }
    assert "transcript_path" not in result


def test_stop_preserves_recursive_completion_and_parks_again() -> None:
    payload = {**_fixture("codex_stop.json"), "stop_hook_active": True}

    result = parse_codex_event(payload)

    assert result is not None
    assert result["kind"] == "response"
    assert result["park_eligible"] is True


def test_permission_keeps_only_bounded_human_metadata(caplog) -> None:
    payload = _fixture("codex_permission.json")

    with caplog.at_level("DEBUG", logger="whisperstream.codex_hud"):
        result = parse_codex_event(payload)

    assert result == {
        "kind": "permission",
        "agent": "codex",
        "session_id": "fixture-codex-permission",
        "tool_name": "Bash",
        "description": "Run the focused test suite",
    }
    assert "tool_input" not in result
    assert "PRIVATE_CODEX_TOOL_INPUT" not in caplog.text
    assert "fixture-codex-permission" not in caplog.text


def test_prompt_submit_drops_prompt_and_focuses_without_activating(caplog) -> None:
    payload = _fixture("codex_prompt_submit.json")
    activation = SessionActivationRegistry()

    with caplog.at_level("DEBUG", logger="whisperstream.codex_hud"):
        result = parse_codex_event(
            payload,
            activation_registry=activation,
            speech_activation="per_session",
        )

    assert result == {
        "kind": "prompt_submitted",
        "agent": "codex",
        "session_id": "fixture-codex-session",
    }
    assert "prompt" not in result
    assert "PRIVATE_CODEX_PROMPT" not in caplog.text
    assert len(activation) == 0


def test_exact_speak_skill_activates_session_and_adds_context() -> None:
    activation = SessionActivationRegistry()
    names = SessionNameRegistry()
    payload = {
        **_fixture("codex_prompt_submit.json"),
        "prompt": "$speak api bridge",
    }

    result = parse_codex_event(
        payload,
        activation_registry=activation,
        name_registry=names,
        speech_activation="per_session",
    )

    assert result == {
        "kind": "prompt_submitted",
        "agent": "codex",
        "session_id": "fixture-codex-session",
        "additional_context": SPOKEN_SUMMARY_CONTEXT,
    }
    assert activation.is_active("fixture-codex-session") is True
    assert names.get("fixture-codex-session") == "api bridge"


def test_activated_session_gets_context_and_stop_name() -> None:
    activation = SessionActivationRegistry()
    names = SessionNameRegistry()
    activation.activate("fixture-codex-session")
    names.set("fixture-codex-session", "api bridge")

    prompt = parse_codex_event(
        _fixture("codex_prompt_submit.json"),
        activation_registry=activation,
        name_registry=names,
        speech_activation="per_session",
    )
    stop = parse_codex_event(
        _fixture("codex_stop.json"),
        activation_registry=activation,
        name_registry=names,
        speech_activation="per_session",
    )

    assert prompt["additional_context"] == SPOKEN_SUMMARY_CONTEXT
    assert stop["source_name"] == "api bridge"
    assert stop["source_name_is_custom"] is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"hook_event_name": "PreToolUse", "tool_input": "PRIVATE"},
        {"hook_event_name": "PermissionRequest", "tool_input": "PRIVATE"},
    ],
)
def test_malformed_or_unsupported_payloads_fail_open(payload) -> None:
    assert parse_codex_event(payload) is None
