"""Tests for the Agent Speak setup flow's synthetic hook payload constants.

The payload shapes mirror ``hud_simulator.py`` (dev-only, not shipped). This module must stay importable in a frozen Nuitka build, so it
must not import ``hud_simulator`` or ``dev_endpoints``.
"""

from __future__ import annotations

from whisperstream.agent_setup_payloads import (
    SETUP_SESSION_ID,
    setup_question_payloads,
    setup_response_payloads,
)


def test_session_id_is_distinctive() -> None:
    assert isinstance(SETUP_SESSION_ID, str)
    assert SETUP_SESSION_ID
    assert not SETUP_SESSION_ID.startswith("hud-sim-")


def test_question_payload_order_and_event_names() -> None:
    payloads = setup_question_payloads()
    assert len(payloads) == 2
    assert payloads[0]["hook_event_name"] == "PreToolUse"
    assert payloads[1]["hook_event_name"] == "PermissionRequest"


def test_question_payloads_carry_ask_user_question_tool() -> None:
    payloads = setup_question_payloads()
    for payload in payloads:
        assert payload["tool_name"] == "AskUserQuestion"


def test_question_has_exactly_two_locked_options() -> None:
    payloads = setup_question_payloads()
    for payload in payloads:
        questions = payload["tool_input"]["questions"]
        assert len(questions) == 1
        question = questions[0]
        assert question["question"] == "This is a practice question. Which sounds better?"
        options = question["options"]
        assert len(options) == 2
        assert options[0]["label"] == "A quieter voice"
        assert options[1]["label"] == "A louder voice"


def test_response_payload_order_and_event_names() -> None:
    payloads = setup_response_payloads()
    assert len(payloads) == 2
    assert payloads[0]["hook_event_name"] == "UserPromptExpansion"
    assert payloads[1]["hook_event_name"] == "Stop"


def test_response_expansion_activates_speak_via_registry() -> None:
    payloads = setup_response_payloads()
    expansion = payloads[0]
    assert expansion["expansion_type"] == "slash_command"
    assert expansion["command_source"] == "plugin"
    assert expansion["command_name"] == "whisperstream:speak"


def test_response_stop_message_carries_marker_on_final_line() -> None:
    payloads = setup_response_payloads()
    stop = payloads[1]
    final_line = stop["last_assistant_message"].splitlines()[-1]
    assert "\U0001f50a " in final_line


def test_no_payload_carries_prompt_id() -> None:
    for payload in (*setup_question_payloads(), *setup_response_payloads()):
        assert "prompt_id" not in payload


def test_every_payload_carries_setup_session_id() -> None:
    for payload in (*setup_question_payloads(), *setup_response_payloads()):
        assert payload["session_id"] == SETUP_SESSION_ID


def test_builders_return_fresh_objects_each_call() -> None:
    first = setup_question_payloads()
    second = setup_question_payloads()
    assert first == second
    assert first[0] is not second[0]
    assert first[0]["tool_input"] is not second[0]["tool_input"]

    first_r = setup_response_payloads()
    second_r = setup_response_payloads()
    assert first_r == second_r
    assert first_r[0] is not second_r[0]
