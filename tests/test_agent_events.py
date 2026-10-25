"""Tests for agent_events — templates, debounce, and speech focus.

Pure module: no I/O, no audio, no server. The debounce clock is injected so
no test sleeps.
"""

from __future__ import annotations

import pytest

from whisperstream.agent_events import (
    DEBOUNCE_INTERVALS,
    EVENT_KINDS,
    SessionActivationRegistry,
    SessionNameRegistry,
    SpeechDebounce,
    SpeechFocus,
    template_for,
)


# ---------------------------------------------------------------------------
# Deterministic templates
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_actionable_event_kinds_share_the_normalized_vocabulary(self):
        assert {"speech_activation", "question", "permission"}.issubset(EVENT_KINDS)

    def test_needs_input_claude(self):
        assert template_for("claude", "needs_input") == (
            "Claude needs your approval to continue."
        )

    def test_codex_uses_provider_label(self):
        assert template_for("codex", "needs_input") == (
            "Codex needs your approval to continue."
        )

    def test_error_templates(self):
        assert template_for("claude", "error") == "Claude hit an error."

    def test_progress_subagent_done(self):
        assert template_for("claude", "progress", "subagent_done") == "A subtask finished."

    def test_progress_unknown_detail_falls_back_to_kind_default(self):
        assert template_for("claude", "progress", "something_new") == (
            "Claude finished a step."
        )

    def test_unknown_agent_gets_neutral_label(self):
        assert template_for("gemini", "needs_input") == (
            "The agent needs your approval to continue."
        )

    def test_response_and_prompt_submitted_have_no_template(self):
        # Responses speak only their marked summary; prompt_submitted is never spoken.
        assert template_for("claude", "response") is None
        assert template_for("claude", "prompt_submitted") is None

    def test_unknown_kind_returns_none(self):
        assert template_for("claude", "nonsense") is None

    def test_no_em_dashes_in_any_template(self):
        # Copy rule: no em dashes in user-visible copy (spoken copy included).
        for kind in ("needs_input", "error", "progress"):
            for agent in ("claude", "unknown"):
                line = template_for(agent, kind)
                assert line is None or "—" not in line


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


class TestSpeechDebounce:
    def _make(self, start=1000.0):
        state = {"now": start}
        debounce = SpeechDebounce(clock=lambda: state["now"])
        return debounce, state

    def test_first_fire_allowed(self):
        debounce, _ = self._make()
        assert debounce.allow("progress") is True

    def test_repeat_within_interval_collapsed(self):
        debounce, state = self._make()
        assert debounce.allow("progress") is True
        state["now"] += DEBOUNCE_INTERVALS["progress"] - 0.1
        assert debounce.allow("progress") is False

    def test_repeat_after_interval_allowed(self):
        debounce, state = self._make()
        assert debounce.allow("progress") is True
        state["now"] += DEBOUNCE_INTERVALS["progress"] + 0.1
        assert debounce.allow("progress") is True

    def test_kinds_are_independent(self):
        debounce, _ = self._make()
        assert debounce.allow("progress") is True
        assert debounce.allow("needs_input") is True
        assert debounce.allow("error") is True

    def test_suppressed_fire_does_not_reset_window(self):
        # A collapsed repeat must not extend the quiet period.
        debounce, state = self._make()
        interval = DEBOUNCE_INTERVALS["needs_input"]
        assert debounce.allow("needs_input") is True
        state["now"] += interval * 0.9
        assert debounce.allow("needs_input") is False
        state["now"] += interval * 0.2  # past the ORIGINAL fire + interval
        assert debounce.allow("needs_input") is True

    def test_unconfigured_kind_always_allowed(self):
        debounce, _ = self._make()
        assert debounce.allow("response") is True
        assert debounce.allow("response") is True


# ---------------------------------------------------------------------------
# Speech focus (most-recent-prompt source wins routine speech)
# ---------------------------------------------------------------------------


class TestSpeechFocus:
    def test_speaks_everything_before_any_prompt(self):
        focus = SpeechFocus()
        assert focus.should_speak("claude", "session-a") is True
        assert focus.should_speak("claude", None) is True

    def test_last_prompted_source_wins(self):
        focus = SpeechFocus()
        focus.note_prompt("claude", "session-a")
        assert focus.should_speak("claude", "session-a") is True
        assert focus.should_speak("claude", "session-b") is False

    def test_newer_prompt_takes_over(self):
        focus = SpeechFocus()
        focus.note_prompt("claude", "session-a")
        focus.note_prompt("claude", "session-b")
        assert focus.should_speak("claude", "session-a") is False
        assert focus.should_speak("claude", "session-b") is True

    def test_newer_prompt_can_switch_providers_with_same_session_id(self):
        focus = SpeechFocus()
        focus.note_prompt("claude", "session-a")
        focus.note_prompt("codex", "session-a")

        assert focus.should_speak("claude", "session-a") is False
        assert focus.should_speak("codex", "session-a") is True

    def test_event_without_session_id_always_speaks(self):
        # Speech focus is a filter, never a silencer of last resort: an old
        # forwarder that sends no session id keeps Phase 1 behavior.
        focus = SpeechFocus()
        focus.note_prompt("claude", "session-a")
        assert focus.should_speak("claude", None) is True
        assert focus.should_speak("claude", "") is True

    def test_note_prompt_ignores_empty_and_non_string(self):
        focus = SpeechFocus()
        focus.note_prompt("claude", None)
        focus.note_prompt("claude", "")
        focus.note_prompt("claude", 123)  # type: ignore[arg-type]
        focus.note_prompt("", "session-a")
        # Still no focused source -> everything speaks.
        assert focus.should_speak("claude", "session-b") is True

    def test_clear_forgets_focused_session(self):
        focus = SpeechFocus()
        focus.note_prompt("claude", "session-a")
        assert focus.should_speak("claude", "session-b") is False

        focus.clear()

        assert focus.should_speak("claude", "session-b") is True


# ---------------------------------------------------------------------------
# Memory-only per-session speech activation
# ---------------------------------------------------------------------------


class TestSessionActivationRegistry:
    def test_activation_is_session_scoped(self):
        registry = SessionActivationRegistry()

        assert registry.activate("session-a") is True
        assert registry.is_active("session-a") is True
        assert registry.is_active("session-b") is False

    def test_duplicate_activation_is_idempotent(self):
        registry = SessionActivationRegistry()

        assert registry.activate("session-a") is True
        assert registry.activate("session-a") is False
        assert len(registry) == 1

    def test_invalid_session_ids_fail_closed(self):
        registry = SessionActivationRegistry()

        assert registry.activate(None) is False
        assert registry.activate("") is False
        assert registry.activate(123) is False
        assert registry.is_active(None) is False
        assert len(registry) == 0

    def test_oldest_activation_is_evicted_at_bound(self):
        registry = SessionActivationRegistry(max_sessions=2)
        registry.activate("session-a")
        registry.activate("session-b")
        registry.activate("session-c")

        assert registry.is_active("session-a") is False
        assert registry.is_active("session-b") is True
        assert registry.is_active("session-c") is True

    def test_oversized_session_id_is_stably_bounded(self):
        registry = SessionActivationRegistry()
        oversized = "private-session-fragment-" * 100

        registry.activate(oversized)

        assert registry.is_active(oversized) is True
        assert len(registry) == 1

    def test_clear_forgets_current_run_state(self):
        registry = SessionActivationRegistry()
        registry.activate("session-a")

        registry.clear()

        assert registry.is_active("session-a") is False
        assert len(registry) == 0


# ---------------------------------------------------------------------------
# Memory-only per-session explicit name registry
# ---------------------------------------------------------------------------


class TestSessionNameRegistry:
    def test_set_then_get_round_trips_a_sanitized_name(self):
        registry = SessionNameRegistry()

        assert registry.set("session-a", "speak-harness") is True
        assert registry.get("session-a") == "speak-harness"

    def test_name_with_slash_is_stored_sanitized(self):
        registry = SessionNameRegistry()

        assert registry.set("session-a", "app/pty-proxy") is True
        stored = registry.get("session-a")
        assert stored is not None
        assert "/" not in stored

    def test_unsanitizable_name_returns_false_and_leaves_no_entry(self):
        registry = SessionNameRegistry()

        assert registry.set("session-a", "   ") is False
        assert registry.get("session-a") is None
        assert len(registry) == 0

    def test_clear_name_removes_only_that_session(self):
        registry = SessionNameRegistry()
        registry.set("session-a", "alpha")
        registry.set("session-b", "beta")

        registry.clear_name("session-a")

        assert registry.get("session-a") is None
        assert registry.get("session-b") == "beta"

    def test_setting_existing_session_replaces_and_moves_to_newest(self):
        registry = SessionNameRegistry(max_sessions=2)
        registry.set("session-a", "first-name")
        registry.set("session-b", "second-name")

        # Re-set session-a: it should become newest, so session-b (now
        # oldest) is evicted first when the cap is exceeded.
        registry.set("session-a", "renamed")
        registry.set("session-c", "third-name")

        assert registry.get("session-a") == "renamed"
        assert registry.get("session-c") == "third-name"
        assert registry.get("session-b") is None

    def test_oldest_name_is_evicted_at_bound(self):
        registry = SessionNameRegistry(max_sessions=2)
        registry.set("session-a", "alpha")
        registry.set("session-b", "beta")
        registry.set("session-c", "gamma")

        assert registry.get("session-a") is None
        assert registry.get("session-b") == "beta"
        assert registry.get("session-c") == "gamma"

    def test_none_or_empty_session_id_is_a_no_op(self):
        registry = SessionNameRegistry()

        assert registry.set(None, "name") is False
        assert registry.set("", "name") is False
        assert len(registry) == 0

    def test_len_tracks(self):
        registry = SessionNameRegistry()
        assert len(registry) == 0

        registry.set("session-a", "alpha")
        assert len(registry) == 1

        registry.set("session-b", "beta")
        assert len(registry) == 2

        registry.clear_name("session-a")
        assert len(registry) == 1

        registry.clear()
        assert len(registry) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
