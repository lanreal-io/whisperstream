"""Tests for parse_claude_event — pure function, no I/O, no side effects.

Covers all cases from Task 1.2:
  - Stop with a real message -> kind=response, empty=False
  - Stop with empty/whitespace/missing last_assistant_message -> kind=response, empty=True
  - Stop with stop_hook_active truthy -> response eligible for another reply
  - SubagentStop -> None
  - Notification with permission_prompt matcher -> kind=needs_input
  - Notification with idle_prompt matcher -> None
  - Notification with unrecognized matcher -> None
  - garbage/missing hook_event_name -> None
  - empty dict -> None
"""

import json
from pathlib import Path

import pytest


AGENT_EVENT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_events"


def load_agent_event_fixture(name: str) -> dict:
    return json.loads((AGENT_EVENT_FIXTURES / name).read_text(encoding="utf-8"))


def parse_claude_event(payload, **kwargs):
    """Import from the module under test."""
    from whisperstream.claude_hud import parse_claude_event as _fn
    return _fn(payload, **kwargs)


# ---------------------------------------------------------------------------
# Stop event — real message
# ---------------------------------------------------------------------------

class TestStopWithMessage:
    def test_returns_response_kind(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": "Here is the answer to your question.",
        })
        assert result is not None
        assert result["kind"] == "response"

    def test_text_field_matches_message(self):
        msg = "Here is the answer to your question."
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": msg,
        })
        assert result["text"] == msg

    def test_empty_flag_is_false(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": "Some content here.",
        })
        assert result["empty"] is False

    def test_multiline_message_preserved(self):
        msg = "Line one.\nLine two.\nLine three."
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": msg,
        })
        assert result["text"] == msg
        assert result["empty"] is False

    def test_final_spoken_line_is_extracted_without_changing_visual_response(self):
        msg = "Finished the settings flow.\n\n🔊 Added coverage for the failure cases."
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": msg,
        })

        assert result["text"] == msg
        assert result["spoken_summary"] == "Added coverage for the failure cases."

    def test_trailing_blank_lines_do_not_hide_final_spoken_line(self):
        msg = "Done.\r\n🔊 Human-readable summary.  \r\n\r\n"
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": msg,
        })

        assert result["text"] == msg
        assert result["spoken_summary"] == "Human-readable summary."

    @pytest.mark.parametrize(
        "summary",
        (
            "Unicode stays exact: café, 漢字, 🧊",
            "Long authored summary " + ("word " * 2_000).strip(),
        ),
        ids=("unicode", "long"),
    )
    def test_valid_authored_summary_is_extracted_exactly(self, summary):
        msg = f"Completed response.\n🔊 {summary}"

        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": msg,
        })

        assert result["text"] == msg
        assert result["spoken_summary"] == summary

    @pytest.mark.parametrize(
        "msg",
        (
            "🔊summary without a space",
            "   🔊 leading whitespace",
            "🔊 trailing whitespace   ",
            "🔊️ variation selector",
            # cp1252 and latin-1 mis-decodes of the marker's UTF-8 bytes.
            b"\xf0\x9f\x94\x8a".decode("cp1252") + " mangled marker",
            b"\xf0\x9f\x94\x8a".decode("latin-1") + " mangled marker",
        ),
        ids=(
            "no-space",
            "leading-ws",
            "trailing-ws",
            "variation-selector",
            "cp1252-mojibake",
            "latin1-mojibake",
        ),
    )
    def test_tolerated_marker_renderings_still_speak(self, msg):
        """Iteration 03: a mangled or unspaced marker speaks instead of failing silently."""
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": f"Completed response.\n{msg}",
        })

        assert result["spoken_summary"] is not None
        assert result["spoken_summary"].endswith(msg.strip().split()[-1])

    @pytest.mark.parametrize(
        "msg",
        (
            "🔊",
            "🔊 ",
            "🔊 valid summary\nordinary final line",
            "ordinary final line\n🔊 \n\n",
            "ordinary response without a marker",
            "ordinary response\nd??? corrupted marker",
        ),
    )
    def test_missing_or_invalid_final_spoken_line_is_silent(self, msg):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": msg,
        })

        assert result["text"] == msg
        assert result["spoken_summary"] is None

    @pytest.mark.parametrize(
        "msg",
        (
            "Plain text with trailing whitespace.  \n",
            "# Result\n\n- **Passed** `pytest`\n",
            "Unicode stays exact: café, 漢字, 🧊",
            '{"status":"ok","result":{"count":2}}',
        ),
    )
    def test_direct_response_strings_are_preserved_exactly(self, msg):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": msg,
        })
        assert result["text"] == msg


class TestStopResponseNormalization:
    def test_synthetic_stop_fixture_keeps_legitimate_json_literal_and_logs_no_content(
        self, caplog
    ):
        payload = load_agent_event_fixture("claude_stop.json")

        with caplog.at_level("DEBUG", logger="whisperstream.claude_hud"):
            result = parse_claude_event(payload)

        assert result == {
            "kind": "response",
            "text": '{"fixture":"LEGITIMATE_JSON_LITERAL"}',
            "empty": False,
            "spoken_summary": None,
            "park_eligible": True,
            "agent": "claude",
            "session_id": "fixture-claude-session",
            "prompt_id": "fixture-claude-prompt",
            "cwd": r"C:\synthetic-root\synthetic-project",
        }
        assert "transcript_path" not in result
        for private_value in (
            payload["last_assistant_message"],
            payload["session_id"],
            payload["prompt_id"],
            payload["cwd"],
            payload["transcript_path"],
        ):
            assert private_value not in caplog.text

    def test_direct_stop_preserves_identity_without_transcript_path(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": "Done.",
            "session_id": "claude-session",
            "prompt_id": "prompt-3",
            "cwd": "C:/work/whisperstream",
            "transcript_path": "C:/private/transcript.jsonl",
        })
        assert result == {
            "kind": "response",
            "text": "Done.",
            "empty": False,
            "spoken_summary": None,
            "park_eligible": True,
            "agent": "claude",
            "session_id": "claude-session",
            "prompt_id": "prompt-3",
            "cwd": "C:/work/whisperstream",
        }
        assert "transcript_path" not in result

    def test_known_stop_envelope_unwraps_and_supplies_identity(self):
        envelope = {
            "hook_event_name": "Stop",
            "last_assistant_message": "Nested response.",
            "session_id": "claude-session",
            "prompt_id": "prompt-7",
            "cwd": "C:/work/whisperstream",
            "transcript_path": "C:/private/transcript.jsonl",
            "stop_hook_active": False,
        }
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": json.dumps(envelope),
        })
        assert result == {
            "kind": "response",
            "text": "Nested response.",
            "empty": False,
            "spoken_summary": None,
            "park_eligible": True,
            "agent": "claude",
            "session_id": "claude-session",
            "prompt_id": "prompt-7",
            "cwd": "C:/work/whisperstream",
        }
        assert "transcript_path" not in result
        assert "C:/private/transcript.jsonl" not in str(result)

    def test_two_nested_stop_envelopes_unwrap(self):
        inner = json.dumps({
            "hook_event_name": "Stop",
            "last_assistant_message": "Human-facing answer.",
        })
        outer = json.dumps({
            "hook_event_name": "Stop",
            "last_assistant_message": inner,
        })
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": outer,
        })
        assert result["text"] == "Human-facing answer."

    def test_unwrap_stops_after_two_envelopes(self):
        third = json.dumps({
            "hook_event_name": "Stop",
            "last_assistant_message": "Must remain wrapped.",
        })
        second = json.dumps({
            "hook_event_name": "Stop",
            "last_assistant_message": third,
        })
        first = json.dumps({
            "hook_event_name": "Stop",
            "last_assistant_message": second,
        })
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": first,
        })
        assert result["text"] == third

    def test_json_lookalike_with_unknown_field_remains_literal(self):
        msg = json.dumps({
            "hook_event_name": "Stop",
            "last_assistant_message": "Example output",
            "explanation": "This is response content, not a transport envelope.",
        })
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": msg,
        })
        assert result["text"] == msg

    @pytest.mark.parametrize("bad_message", (None, {"secret": "do-not-log"}, 42))
    def test_malformed_known_envelope_becomes_empty_without_content_logging(
        self, bad_message, caplog
    ):
        envelope = {
            "hook_event_name": "Stop",
            "last_assistant_message": bad_message,
        }
        with caplog.at_level("DEBUG", logger="whisperstream.claude_hud"):
            result = parse_claude_event({
                "hook_event_name": "Stop",
                "last_assistant_message": json.dumps(envelope),
            })
        assert result["text"] == ""
        assert result["empty"] is True
        assert "do-not-log" not in caplog.text


# ---------------------------------------------------------------------------
# Stop event — empty / whitespace / missing last_assistant_message
# ---------------------------------------------------------------------------

class TestStopEmptyMessage:
    def test_empty_string_sets_empty_flag(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": "",
        })
        assert result is not None
        assert result["kind"] == "response"
        assert result["empty"] is True

    def test_whitespace_only_sets_empty_flag(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": "   \n\t  ",
        })
        assert result is not None
        assert result["kind"] == "response"
        assert result["empty"] is True

    def test_missing_key_sets_empty_flag(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            # no last_assistant_message key
        })
        assert result is not None
        assert result["kind"] == "response"
        assert result["empty"] is True

    def test_none_value_sets_empty_flag(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": None,
        })
        assert result is not None
        assert result["kind"] == "response"
        assert result["empty"] is True

    def test_empty_text_field_is_empty_string(self):
        """HUD owns the fallback copy; parser returns empty string, not a placeholder."""
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": "",
        })
        assert result["text"] == ""
        assert result["spoken_summary"] is None


# ---------------------------------------------------------------------------
# Stop event — stop_hook_active continuation
# ---------------------------------------------------------------------------

class TestStopHookActiveContinuation:
    def test_true_preserves_followup_and_allows_second_park(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "stop_hook_active": True,
            "last_assistant_message": "Would have fired twice.",
        })
        assert result["text"] == "Would have fired twice."
        assert result["park_eligible"] is True

    def test_truthy_integer_allows_second_park(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "stop_hook_active": 1,
            "last_assistant_message": "Integer-marked continuation.",
        })
        assert result["text"] == "Integer-marked continuation."
        assert result["park_eligible"] is True

    def test_truthy_string_allows_second_park(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "stop_hook_active": "yes",
            "last_assistant_message": "String-marked continuation.",
        })
        assert result["text"] == "String-marked continuation."
        assert result["park_eligible"] is True

    def test_followup_keeps_marked_summary_separate(self):
        msg = "Follow-up response.\n🔊 Follow-up finished."
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "stop_hook_active": True,
            "last_assistant_message": msg,
        })

        assert result["text"] == msg
        assert result["spoken_summary"] == "Follow-up finished."
        assert result["park_eligible"] is True

    def test_nested_active_stop_envelope_remains_parkable(self):
        envelope = {
            "hook_event_name": "Stop",
            "stop_hook_active": True,
            "last_assistant_message": "Nested follow-up.",
        }
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": json.dumps(envelope),
        })

        assert result["text"] == "Nested follow-up."
        assert result["park_eligible"] is True

    def test_false_does_not_skip(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": "Normal completion.",
        })
        assert result is not None
        assert result["kind"] == "response"
        assert result["park_eligible"] is True

    def test_missing_does_not_skip(self):
        """Absent stop_hook_active should not trigger the guard."""
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": "Normal completion.",
        })
        assert result is not None
        assert result["kind"] == "response"
        assert result["park_eligible"] is True

    def test_zero_does_not_skip(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "stop_hook_active": 0,
            "last_assistant_message": "Normal completion.",
        })
        assert result is not None
        assert result["kind"] == "response"
        assert result["park_eligible"] is True

    def test_none_does_not_skip(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "stop_hook_active": None,
            "last_assistant_message": "Normal completion.",
        })
        assert result is not None
        assert result["kind"] == "response"
        assert result["park_eligible"] is True


# ---------------------------------------------------------------------------
# SubagentStop — always None
# ---------------------------------------------------------------------------

class TestSubagentStop:
    """Phase 2 contract: SubagentStop is a progress milestone. It still never
    fires the HUD (the endpoint routes progress to template speech only, and
    never copies the subagent message forward)."""

    def test_returns_progress_kind(self):
        result = parse_claude_event({
            "hook_event_name": "SubagentStop",
            "last_assistant_message": "Subagent finished.",
            "session_id": "s-1",
        })
        assert result == {
            "kind": "progress",
            "detail": "subagent_done",
            "session_id": "s-1",
        }

    def test_never_carries_message_text(self):
        result = parse_claude_event({
            "hook_event_name": "SubagentStop",
            "last_assistant_message": "secret subagent output",
        })
        assert result is not None
        assert "text" not in result
        assert "secret subagent output" not in str(result)

    def test_progress_without_session_id(self):
        result = parse_claude_event({"hook_event_name": "SubagentStop"})
        assert result == {
            "kind": "progress",
            "detail": "subagent_done",
            "session_id": None,
        }


# ---------------------------------------------------------------------------
# Notification — permission_prompt
# ---------------------------------------------------------------------------

class TestNotificationPermissionPrompt:
    def test_exact_notification_type_is_non_authoritative_working(self):
        result = parse_claude_event({
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "message": "Claude needs permission.",
            "session_id": "session-a",
        })
        assert result == {"kind": "working", "session_id": "session-a"}

    @pytest.mark.parametrize("field", ("message", "title", "matcher"))
    def test_free_text_permission_markers_are_ignored(self, field):
        assert parse_claude_event({
            "hook_event_name": "Notification",
            field: "permission_prompt",
        }) is None

    def test_notification_type_requires_exact_match(self):
        assert parse_claude_event({
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt extra",
        }) is None


# ---------------------------------------------------------------------------
# Notification — idle_prompt and other matchers -> None
# ---------------------------------------------------------------------------

class TestNotificationIdlePrompt:
    def test_idle_prompt_returns_none(self):
        result = parse_claude_event({
            "hook_event_name": "Notification",
            "notification_type": "idle_prompt",
        })
        assert result is None

    def test_unrecognized_notification_returns_none(self):
        result = parse_claude_event({
            "hook_event_name": "Notification",
            "message": "some_other_event",
        })
        assert result is None

    def test_notification_with_no_matcher_fields_returns_none(self):
        result = parse_claude_event({"hook_event_name": "Notification"})
        assert result is None


# ---------------------------------------------------------------------------
# SessionStart / SessionEnd — Task 2.5. Recognized, but managed-session
# lifecycle only: neither ever reaches the HUD or speech. The actual
# rebind-on-SessionStart / mark-unsafe-on-SessionEnd behavior lives in
# managed_sessions.record_event and is correlated independently in
# server._handle_claude_hook_body (Task 2.4), before parse_claude_event runs
# at all -- see tests/test_managed_sessions.py and
# tests/test_managed_session_correlation.py for that behavior's tests.
# ---------------------------------------------------------------------------

class TestSessionStart:
    def test_returns_none(self):
        assert parse_claude_event({
            "hook_event_name": "SessionStart",
            "session_id": "s-1",
        }) is None

    def test_recognized_distinctly_from_unknown_event(self, caplog):
        with caplog.at_level("DEBUG", logger="whisperstream.claude_hud"):
            parse_claude_event({
                "hook_event_name": "SessionStart",
                "session_id": "s-1",
            })
        assert not any("Unknown hook_event_name" in r.message for r in caplog.records)


class TestSessionEnd:
    def test_returns_none(self):
        assert parse_claude_event({
            "hook_event_name": "SessionEnd",
            "session_id": "s-1",
        }) is None

    def test_recognized_distinctly_from_unknown_event(self, caplog):
        with caplog.at_level("DEBUG", logger="whisperstream.claude_hud"):
            parse_claude_event({
                "hook_event_name": "SessionEnd",
                "session_id": "s-1",
            })
        assert not any("Unknown hook_event_name" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Unknown / malformed / missing hook_event_name
# ---------------------------------------------------------------------------

class TestGarbageInput:
    def test_empty_dict_returns_none(self):
        assert parse_claude_event({}) is None

    def test_unknown_event_name_returns_none(self):
        assert parse_claude_event({"hook_event_name": "SomeOtherEvent"}) is None

    def test_missing_hook_event_name_returns_none(self):
        assert parse_claude_event({"last_assistant_message": "hello"}) is None

    def test_none_hook_event_name_returns_none(self):
        assert parse_claude_event({"hook_event_name": None}) is None

    def test_integer_hook_event_name_returns_none(self):
        assert parse_claude_event({"hook_event_name": 42}) is None

    def test_unrelated_pre_op_event_returns_none(self):
        assert parse_claude_event({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo private"},
        }) is None

    def test_post_op_event_returns_none(self):
        assert parse_claude_event({"hook_event_name": "PostToolUse"}) is None


# ---------------------------------------------------------------------------
# Phase 2 — session_id, UserPromptSubmit, error marker
# ---------------------------------------------------------------------------


class TestSessionIdExtraction:
    def test_stop_carries_session_id(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": "Done.",
            "session_id": "sess-42",
        })
        assert result["session_id"] == "sess-42"

    def test_missing_session_id_is_none(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": "Done.",
        })
        assert result["session_id"] is None

    def test_non_string_session_id_is_none(self):
        result = parse_claude_event({
            "hook_event_name": "Stop",
            "last_assistant_message": "Done.",
            "session_id": 42,
        })
        assert result["session_id"] is None


class TestUserPromptSubmit:
    def test_maps_to_prompt_submitted(self):
        result = parse_claude_event({
            "hook_event_name": "UserPromptSubmit",
            "prompt": "fix the flaky test",
            "session_id": "sess-1",
        })
        assert result == {"kind": "prompt_submitted", "session_id": "sess-1"}

    def test_prompt_text_never_copied_forward(self):
        result = parse_claude_event({
            "hook_event_name": "UserPromptSubmit",
            "prompt": "very secret prompt text",
        })
        assert "very secret prompt text" not in str(result)


class TestSpeechActivation:
    def _registry(self):
        from whisperstream.agent_events import SessionActivationRegistry

        return SessionActivationRegistry()

    def _names(self):
        from whisperstream.agent_events import SessionNameRegistry

        return SessionNameRegistry()

    def test_exact_plugin_command_activates_only_its_session(self):
        registry = self._registry()
        names = self._names()
        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_source": "plugin",
                "command_name": "whisperstream:speak",
                "prompt": "/whisperstream:speak",
                "session_id": "session-a",
                "transcript_path": "C:/private/transcript.jsonl",
            },
            activation_registry=registry,
            name_registry=names,
        )

        assert result == {
            "kind": "speech_activation",
            "session_id": "session-a",
            "named": False,
        }
        assert registry.is_active("session-a") is True
        assert registry.is_active("session-b") is False
        assert "transcript_path" not in result
        assert names.get("session-a") is None

    def test_exact_namespaced_prompt_token_is_supported_fallback(self):
        registry = self._registry()
        names = self._names()
        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_source": "plugin",
                "command_name": "speak",
                "prompt": "/whisperstream:speak softly",
                "session_id": "session-a",
            },
            activation_registry=registry,
            name_registry=names,
        )

        assert result["kind"] == "speech_activation"
        assert registry.is_active("session-a") is True
        # Argument parsing (Task 2.1): a trailing argument on /whisperstream:speak
        # also sets the session name, so this now activates AND names.
        assert result["named"] is True
        assert names.get("session-a") == "softly"

    def test_bare_speak_activates_without_touching_an_existing_name(self):
        registry = self._registry()
        names = self._names()
        names.set("session-a", "previously chosen")

        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_source": "plugin",
                "command_name": "whisperstream:speak",
                "prompt": "/whisperstream:speak",
                "session_id": "session-a",
            },
            activation_registry=registry,
            name_registry=names,
        )

        assert result["kind"] == "speech_activation"
        assert result["named"] is False
        assert registry.is_active("session-a") is True
        assert names.get("session-a") == "previously chosen"

    @pytest.mark.parametrize(
        "overrides",
        (
            {"command_source": "user"},
            {"command_name": "other:speak", "prompt": "/other:speak"},
            {"command_name": "speak", "prompt": "/whisperstream:speaker"},
            {"expansion_type": "mcp_prompt"},
            {"session_id": ""},
        ),
    )
    def test_near_matches_do_not_activate(self, overrides):
        registry = self._registry()
        names = self._names()
        payload = {
            "hook_event_name": "UserPromptExpansion",
            "expansion_type": "slash_command",
            "command_source": "plugin",
            "command_name": "whisperstream:speak",
            "prompt": "/whisperstream:speak",
            "session_id": "session-a",
            **overrides,
        }

        assert parse_claude_event(
            payload, activation_registry=registry, name_registry=names
        ) is None
        assert registry.is_active("session-a") is False
        assert names.get("session-a") is None


class TestSessionNameCommand:
    def _names(self):
        from whisperstream.agent_events import SessionNameRegistry

        return SessionNameRegistry()

    def _registry(self):
        from whisperstream.agent_events import SessionActivationRegistry

        return SessionActivationRegistry()

    def test_exact_command_name_sets_the_session_name(self):
        names = self._names()
        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_source": "plugin",
                "command_name": "whisperstream:name",
                "prompt": "/whisperstream:name speak-harness",
                "session_id": "session-a",
            },
            name_registry=names,
        )

        assert result == {
            "kind": "session_name",
            "session_id": "session-a",
            "named": True,
        }
        assert names.get("session-a") == "speak-harness"

    def test_prompt_token_fallback_sets_the_session_name(self):
        names = self._names()
        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_source": "plugin",
                "command_name": "name",
                "prompt": "/whisperstream:name pay kit",
                "session_id": "session-a",
            },
            name_registry=names,
        )

        assert result["kind"] == "session_name"
        assert result["named"] is True
        assert names.get("session-a") == "pay kit"

    def test_bare_invocation_clears_the_name(self):
        names = self._names()
        names.set("session-a", "old name")

        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_source": "plugin",
                "command_name": "whisperstream:name",
                "prompt": "/whisperstream:name",
                "session_id": "session-a",
            },
            name_registry=names,
        )

        assert result == {
            "kind": "session_name",
            "session_id": "session-a",
            "named": False,
        }
        assert names.get("session-a") is None

    def test_argument_that_sanitizes_to_nothing_is_treated_as_bare(self):
        names = self._names()
        names.set("session-a", "old name")

        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_source": "plugin",
                "command_name": "whisperstream:name",
                "prompt": "/whisperstream:name \x00\x00\x00",
                "session_id": "session-a",
            },
            name_registry=names,
        )

        assert result["named"] is False
        assert names.get("session-a") is None

    def test_name_command_does_not_activate_speech(self):
        registry = self._registry()
        names = self._names()

        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_source": "plugin",
                "command_name": "whisperstream:name",
                "prompt": "/whisperstream:name harness",
                "session_id": "session-a",
            },
            activation_registry=registry,
            name_registry=names,
        )

        assert result["kind"] == "session_name"
        assert registry.is_active("session-a") is False

    @pytest.mark.parametrize(
        "overrides",
        (
            {"command_source": "user"},
            {"command_name": "other:name", "prompt": "/other:name"},
            # /whisperstreamname (missing the colon) is a distinct command.
            {"command_name": "name", "prompt": "/whisperstreamname"},
            # /whisperstream:names is a near-miss, not the name command.
            {"command_name": "names", "prompt": "/whisperstream:names"},
            # /whisperstream:name-thing is a near-miss too.
            {
                "command_name": "name-thing",
                "prompt": "/whisperstream:name-thing",
            },
            {"expansion_type": "mcp_prompt"},
            {"session_id": ""},
        ),
    )
    def test_near_matches_do_not_write_the_registry(self, overrides):
        names = self._names()
        names.set("session-a", "untouched")
        payload = {
            "hook_event_name": "UserPromptExpansion",
            "expansion_type": "slash_command",
            "command_source": "plugin",
            "command_name": "whisperstream:name",
            "prompt": "/whisperstream:name",
            "session_id": "session-a",
            **overrides,
        }

        assert parse_claude_event(payload, name_registry=names) is None
        assert names.get("session-a") == "untouched"

    def test_activated_session_gets_marked_summary_context_on_prompt(self):
        from whisperstream.claude_hud import SPOKEN_SUMMARY_CONTEXT

        registry = self._registry()
        registry.activate("session-a")

        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-a",
                "prompt": "private prompt",
            },
            activation_registry=registry,
            speech_activation="per_session",
        )

        assert result == {
            "kind": "prompt_submitted",
            "session_id": "session-a",
            "additional_context": SPOKEN_SUMMARY_CONTEXT,
        }
        assert "private prompt" not in str(result)

    def test_inactive_session_gets_no_context_in_per_session_mode(self):
        registry = self._registry()
        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-b",
                "prompt": "private prompt",
            },
            activation_registry=registry,
            speech_activation="per_session",
        )

        assert result == {
            "kind": "prompt_submitted",
            "session_id": "session-b",
        }

    def test_always_on_adds_context_without_registry_state(self):
        from whisperstream.claude_hud import SPOKEN_SUMMARY_CONTEXT

        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-b",
            },
            activation_registry=self._registry(),
            speech_activation="always_on",
        )

        assert result["additional_context"] == SPOKEN_SUMMARY_CONTEXT

    def test_invalid_activation_mode_fails_closed(self):
        registry = self._registry()
        registry.activate("session-a")

        result = parse_claude_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-a",
            },
            activation_registry=registry,
            speech_activation="global",
        )

        assert "additional_context" not in result


class TestAskUserQuestion:
    def test_question_shape_round_trips_with_exact_question_key(self):
        exact_question = "  Which checks should run?  "
        questions = [
            {
                "question": exact_question,
                "header": "Checks",
                "options": [
                    {"label": "Unit", "description": "Fast local tests"},
                    {"label": "E2E"},
                ],
                "multiSelect": True,
            }
        ]
        result = parse_claude_event({
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_use_id": "tool-use-private",
            "tool_input": {
                "questions": questions,
                "untrusted_extra": "do not forward",
            },
            "session_id": "session-a",
            "transcript_path": "C:/private/transcript.jsonl",
        })

        assert result == {
            "kind": "question",
            "phase": "surfaced",
            "session_id": "session-a",
            "questions": questions,
        }
        assert result["questions"][0]["question"] == exact_question
        assert "tool_use_id" not in result
        assert "transcript_path" not in result
        assert "untrusted_extra" not in str(result)

    def test_question_defaults_optional_multi_select_to_false(self):
        result = parse_claude_event({
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [
                    {
                        "question": "Choose one?",
                        "header": "Choice",
                        "options": [{"label": "A"}],
                    }
                ]
            },
        })

        assert result["questions"][0]["multiSelect"] is False

    @pytest.mark.parametrize(
        "tool_input",
        (
            {},
            {"questions": []},
            {"questions": "not-a-list"},
            {
                "questions": [
                    {
                        "question": "",
                        "header": "Choice",
                        "options": [{"label": "A"}],
                    }
                ]
            },
            {
                "questions": [
                    {
                        "question": "Choose?",
                        "header": "Choice",
                        "options": [{"label": ""}],
                    }
                ]
            },
        ),
    )
    def test_malformed_questions_fail_open_to_claude(self, tool_input):
        assert parse_claude_event({
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": tool_input,
        }) is None

    def test_question_content_never_enters_logs(self, caplog):
        private = "QUESTION-CONTENT-MUST-NOT-LOG"
        with caplog.at_level("DEBUG", logger="whisperstream.claude_hud"):
            parse_claude_event({
                "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {
                            "question": private,
                            "header": "Private",
                            "options": [{"label": "Secret"}],
                        }
                    ]
                },
                "session_id": "private-session",
            })

        assert private not in caplog.text
        assert "private-session" not in caplog.text


class TestPermissionRequest:
    def test_ask_user_question_branches_before_generic_permission(self):
        questions = [
            {
                "question": "Choose one?",
                "header": "Choice",
                "options": [{"label": "A"}],
                "multiSelect": False,
            }
        ]
        result = parse_claude_event({
            "hook_event_name": "PermissionRequest",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": questions},
            "session_id": "session-a",
        })

        assert result == {
            "kind": "question",
            "phase": "answerable",
            "session_id": "session-a",
            "questions": questions,
        }

    def test_permission_keeps_only_bounded_safe_metadata(self):
        private_command = "rm -rf C:/private/customer"
        result = parse_claude_event({
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {
                "command": private_command,
                "description": "Remove generated build files",
            },
            "permission_suggestions": [
                {"behavior": "allow", "destination": "localSettings"}
            ],
            "session_id": "session-a",
        })

        assert result == {
            "kind": "permission",
            "session_id": "session-a",
            "tool_name": "Bash",
            "description": "Remove generated build files",
        }
        assert private_command not in str(result)
        assert "permission_suggestions" not in result
        assert "tool_input" not in result

    def test_permission_description_and_tool_name_are_bounded(self):
        result = parse_claude_event({
            "hook_event_name": "PermissionRequest",
            "tool_name": "Tool" * 100,
            "tool_input": {"description": "x" * 1000},
        })

        assert len(result["tool_name"]) <= 128
        assert len(result["description"]) <= 240

    def test_missing_description_uses_privacy_safe_fallback(self):
        result = parse_claude_event({
            "hook_event_name": "PermissionRequest",
            "tool_name": "Write",
            "tool_input": {"file_path": "C:/private/customer.txt"},
        })

        assert result["description"] == "Write a file"
        assert "customer" not in str(result)

    @pytest.mark.parametrize(
        "payload",
        (
            {"tool_name": None, "tool_input": {}},
            {"tool_name": "", "tool_input": {}},
            {"tool_name": "Bash", "tool_input": "not-an-object"},
        ),
    )
    def test_malformed_permission_fails_open_to_claude(self, payload):
        assert parse_claude_event({
            "hook_event_name": "PermissionRequest",
            **payload,
        }) is None


class TestNotificationErrorMarker:
    def test_exact_error_notification_type_maps_to_error(self):
        result = parse_claude_event({
            "hook_event_name": "Notification",
            "notification_type": "error",
            "session_id": "sess-1",
        })
        assert result == {"kind": "error", "session_id": "sess-1"}

    def test_error_in_free_text_message_is_ignored(self):
        # "error" in message/title must NOT map (an unrelated permission
        # message could contain the substring). Matcher field only.
        result = parse_claude_event({
            "hook_event_name": "Notification",
            "message": "Claude needs to run error_analyzer",
        })
        assert result is None

    def test_mixed_matcher_text_is_ignored(self):
        assert parse_claude_event({
            "hook_event_name": "Notification",
            "matcher": "permission_prompt error",
        }) is None


# ---------------------------------------------------------------------------
# Task 1.6 — session name resolved at the hook seam
# ---------------------------------------------------------------------------

class TestSessionNameResolution:
    """The name is resolved here and the transcript path is dropped here.

    A session name is transcript-class: these tests assert on structure and on
    the sanitized name the resolver returns, never on transcript content.
    """

    @staticmethod
    def _name_registry():
        from whisperstream.agent_events import SessionNameRegistry
        return SessionNameRegistry()

    @staticmethod
    def _transcript(tmp_path, title: str, name: str = "transcript.jsonl"):
        path = tmp_path / name
        lines = [
            json.dumps({"type": "user", "message": "opaque"}),
            json.dumps({"type": "custom-title", "customTitle": title}),
            json.dumps({"type": "assistant", "message": "opaque"}),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_registry_name_wins_and_is_marked_custom(self, tmp_path):
        registry = self._name_registry()
        registry.set("sess-name-1", "pay kit")
        transcript = self._transcript(tmp_path, "renamed title")

        result = parse_claude_event(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "Done.",
                "session_id": "sess-name-1",
                "transcript_path": str(transcript),
            },
            name_registry=registry,
        )

        assert result["source_name"] == "pay kit"
        assert result["source_name_is_custom"] is True

    def test_custom_title_is_used_when_no_registry_name(
        self, tmp_path, monkeypatch
    ):
        import whisperstream.claude_hud as hud_module
        from whisperstream.agent_session_names import TranscriptTitleCache

        monkeypatch.setattr(
            hud_module,
            "_DEFAULT_TITLE_CACHE",
            TranscriptTitleCache(projects_root=tmp_path),
        )
        registry = self._name_registry()
        transcript = self._transcript(tmp_path, "speak-harness")

        result = parse_claude_event(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "Done.",
                "session_id": "sess-name-2",
                "transcript_path": str(transcript),
            },
            name_registry=registry,
        )

        assert result["source_name"] == "speak-harness"
        assert result["source_name_is_custom"] is True

    def test_custom_title_is_sanitized(self, tmp_path, monkeypatch):
        import whisperstream.claude_hud as hud_module
        from whisperstream.agent_session_names import TranscriptTitleCache

        monkeypatch.setattr(
            hud_module,
            "_DEFAULT_TITLE_CACHE",
            TranscriptTitleCache(projects_root=tmp_path),
        )
        registry = self._name_registry()
        transcript = self._transcript(tmp_path, "app/pty-proxy")

        result = parse_claude_event(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "Done.",
                "session_id": "sess-name-3",
                "transcript_path": str(transcript),
            },
            name_registry=registry,
        )

        assert result["source_name"] == "app pty-proxy"

    def test_no_name_emits_no_source_name_key(self, tmp_path):
        registry = self._name_registry()
        transcript = self._transcript(tmp_path, "  ", name="untitled.jsonl")

        result = parse_claude_event(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "Done.",
                "session_id": "sess-name-4",
                "transcript_path": str(transcript),
                "cwd": "C:/Users/USERNAME/whisperstream",
            },
            name_registry=registry,
        )

        assert "source_name" not in result
        assert "source_name_is_custom" not in result

    def test_missing_transcript_path_is_not_an_error(self):
        registry = self._name_registry()

        result = parse_claude_event(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "Done.",
                "session_id": "sess-name-5",
            },
            name_registry=registry,
        )

        assert result["kind"] == "response"
        assert "source_name" not in result

    def test_transcript_path_never_reaches_the_parsed_event(self, tmp_path):
        registry = self._name_registry()
        registry.set("sess-name-6", "pay kit")
        transcript = self._transcript(tmp_path, "renamed title")

        result = parse_claude_event(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "Done.",
                "session_id": "sess-name-6",
                "transcript_path": str(transcript),
            },
            name_registry=registry,
        )

        assert "transcript_path" not in result
        assert str(transcript) not in str(result)

    def test_empty_stop_turn_still_carries_the_name(self, tmp_path):
        registry = self._name_registry()
        registry.set("sess-name-7", "pay kit")

        result = parse_claude_event(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "",
                "session_id": "sess-name-7",
            },
            name_registry=registry,
        )

        assert result["empty"] is True
        assert result["source_name"] == "pay kit"

    def test_question_carries_the_resolved_name(self, tmp_path):
        registry = self._name_registry()
        registry.set("sess-name-8", "pay kit")

        result = parse_claude_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {
                            "question": "Choose one?",
                            "header": "Choice",
                            "options": [{"label": "A"}],
                        }
                    ]
                },
                "session_id": "sess-name-8",
                "transcript_path": "C:/private/transcript.jsonl",
            },
            name_registry=registry,
        )

        assert result["source_name"] == "pay kit"
        assert "transcript_path" not in result

    def test_question_omits_the_name_when_unresolved(self):
        registry = self._name_registry()

        result = parse_claude_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {
                            "question": "Choose one?",
                            "header": "Choice",
                            "options": [{"label": "A"}],
                        }
                    ]
                },
                "session_id": "sess-name-9",
            },
            name_registry=registry,
        )

        assert "source_name" not in result

    def test_name_is_never_logged(self, tmp_path, caplog):
        import logging

        registry = self._name_registry()
        registry.set("sess-name-10", "pay kit")
        caplog.set_level(logging.DEBUG, logger="whisperstream.claude_hud")

        parse_claude_event(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "Done.",
                "session_id": "sess-name-10",
            },
            name_registry=registry,
        )

        assert "pay kit" not in caplog.text
        assert "named=true" in caplog.text

    def test_permission_carries_the_resolved_name(self, tmp_path):
        registry = self._name_registry()
        registry.set("sess-name-11", "pay kit")

        result = parse_claude_event(
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"description": "Run the focused test suite"},
                "session_id": "sess-name-11",
                "transcript_path": "C:/private/transcript.jsonl",
            },
            name_registry=registry,
        )

        assert result["source_name"] == "pay kit"
        assert "transcript_path" not in result
        # Permissions never speak; the custom flag would gate nothing.
        assert "source_name_is_custom" not in result

    def test_permission_omits_the_name_when_unresolved(self):
        registry = self._name_registry()

        result = parse_claude_event(
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"description": "Run the focused test suite"},
                "session_id": "sess-name-12",
            },
            name_registry=registry,
        )

        assert "source_name" not in result
