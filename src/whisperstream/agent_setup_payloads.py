"""Synthetic Claude hook payload constants for the Agent Speak setup flow.

Step 4 of the setup flow
fires deterministic, locally-generated hook payloads through the real
``_handle_claude_hook_body`` path so the exercise never depends on a live
Claude session asking a question. The payload *shapes* mirror the dev-only
HUD scenario simulator's ``_stop``/``_name``/``_surface``/``_park`` helpers
(lines 55 to 140 of that module), but that module is excluded from the
Nuitka build. This module is shipped, so the setup routes registered in
``server.py`` (always-on, not dev-gated) can import it in every build.

Every payload omits ``prompt_id``: the response-dedupe digest is only computed
when ``prompt_id`` is present (``claude_hud.py``), and omitting it is what
lets the setup flow be fired and re-fired without tripping dedupe.

Never log the contents of these payloads' rendered text elsewhere; the
question prompt and option labels are user-visible copy, not confidential,
but the routes that fire these must stay content-free in their own logging
per project convention.
"""

from __future__ import annotations

from typing import Any

# A distinctive fixed session id for the setup flow's synthetic hook traffic.
# Deliberately outside the `hud-sim-*` namespace (dev simulator sessions) and
# unlikely to collide with a real Claude session id, since a fixed id could
# otherwise collide with a live session's broker/activation state.
SETUP_SESSION_ID = "whisperstream-setup-flow"

# The marker the speak skill contracts for, written as an escape so this file
# cannot be corrupted by an editor or a codepage-confused tool.
_MARKER = "\U0001f50a "

_QUESTION_PROMPT = "This is a practice question. Which sounds better?"
_QUESTION_OPTIONS = (
    {"label": "A quieter voice"},
    {"label": "A louder voice"},
)


def _questions_payload() -> list[dict[str, Any]]:
    return [
        {
            "question": _QUESTION_PROMPT,
            "options": [dict(option) for option in _QUESTION_OPTIONS],
        }
    ]


def setup_question_payloads() -> list[dict[str, Any]]:
    """The two-payload AskUserQuestion sequence for step 4's question half.

    Order matters: the ``PreToolUse`` half surfaces the card, and the
    ``PermissionRequest`` half is what makes it answerable. Without the
    second payload's ``request_id``-bearing park, the answer only holds
    briefly before going ``held_failed``.
    """
    return [
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "session_id": SETUP_SESSION_ID,
            "tool_input": {"questions": _questions_payload()},
        },
        {
            "hook_event_name": "PermissionRequest",
            "tool_name": "AskUserQuestion",
            "session_id": SETUP_SESSION_ID,
            "tool_input": {"questions": _questions_payload()},
        },
    ]


def setup_response_payloads() -> list[dict[str, Any]]:
    """The two-payload response sequence for step 4's response half.

    Order matters: the ``UserPromptExpansion`` activates
    ``whisperstream:speak`` via the registry activate path (so the user never
    has to type the command), then the ``Stop`` carries the speaker marker on
    the final line of its message so the response is marked for speech.
    """
    return [
        {
            "hook_event_name": "UserPromptExpansion",
            "session_id": SETUP_SESSION_ID,
            "expansion_type": "slash_command",
            "command_source": "plugin",
            "command_name": "whisperstream:speak",
        },
        {
            "hook_event_name": "Stop",
            "session_id": SETUP_SESSION_ID,
            "last_assistant_message": _MARKER,
        },
    ]
