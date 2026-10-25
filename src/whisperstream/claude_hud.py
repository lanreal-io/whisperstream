"""claude_hud.py — Claude HUD bridge layer.

Phase 1 (Tasks 1.2, 1.5): Pure event-parsing helper + forwarder script writer.
Later tasks in this file will add: settings.json merge/prune helpers and
enable/disable I/O wrappers (Tasks 1.6–1.7).

Logging discipline: this module handles last_assistant_message content from
Claude Code. Never log that content. Log metadata only (char count is fine).
See text_injector.py's TRANSCRIPT DATA banner for the project-wide rule.
"""

from __future__ import annotations

from .agent_plugin_runtime import (
    plugin_identity, payload_files, payload_fingerprint, payload_needs_sync,
    mark_payload_synced, remove_sync_receipt, RUNTIME_TARGET,
    legacy_dev_marketplace_owned,
)


import copy
import importlib.resources
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable

from .agent_events import (
    SPOKEN_SUMMARY_CONTEXT,
    SPOKEN_SUMMARY_PREFIX,
    SessionActivationRegistry,
    SessionNameRegistry,
    extract_spoken_summary,
    normalize_permission_event,
)
from .agent_session_names import TranscriptTitleCache, sanitize_session_name
from .macos_user_path import ensure_user_path

logger = logging.getLogger(__name__)

# Documented Notification discriminators. Notifications are compatibility
# activity signals only: they never create an answer-bearing interaction.
_PERMISSION_PROMPT_MARKER = "permission_prompt"
_IDLE_PROMPT_MARKER = "idle_prompt"

# Forward-compat error Notification discriminator. Claude Code does not emit
# this type today. Never infer it from free-text message/title fields.
_ERROR_MARKER = "error"

# A response string is normally opaque human-facing text, even when it parses
# as JSON. Only these documented Claude Stop transport fields identify an
# embedded envelope that may be unwrapped. Unknown keys fail closed and leave
# the original response string untouched.
_CLAUDE_STOP_ENVELOPE_KEYS = frozenset(
    {
        "cwd",
        "hook_event_name",
        "last_assistant_message",
        "permission_mode",
        "prompt_id",
        "session_id",
        "stop_hook_active",
        "transcript_path",
    }
)
_MAX_RESPONSE_ENVELOPE_LAYERS = 2
# Human-readable marker emitted by an activated Claude session. The prefix is
# exact: no leading whitespace, and one literal space after the emoji.
SPOKEN_SUMMARY_PREFIX = "🔊 "
SPOKEN_SUMMARY_CONTEXT = (
    "Always end your final response with one concise, human-readable spoken "
    "summary on its own final line. Begin that line with `🔊 ` (the speaker "
    "emoji followed by one space). This line is required on every response, "
    "including short confirmations and follow-ups. Keep it visible in the "
    "response."
)

# Marker renderings accepted by ``_extract_spoken_summary``. The speaker emoji
# is the contract; the other two are what its UTF-8 bytes look like after a
# mis-decode, so a mangled marker still speaks instead of failing silently.
_SPOKEN_SUMMARY_MARKERS = (
    SPOKEN_SUMMARY_PREFIX.strip(),
    b"\xf0\x9f\x94\x8a".decode("cp1252"),
    b"\xf0\x9f\x94\x8a".decode("latin-1"),
)
_VARIATION_SELECTOR = "️"

# Bound the raw trailing argument before it is ever sanitized, so a
# pathological prompt cannot be walked character by character. The argument
# is prompt text, i.e. transcript-class: never log it.
_MAX_RAW_SKILL_ARGUMENT_CHARS = 200
_DEFAULT_ACTIVATION_REGISTRY = SessionActivationRegistry()
# Explicitly chosen session names (memory-only, bounded) and the stat-keyed
# cache of `/rename` titles read off the session transcript. Both are process
# local and cleared by a restart; neither ever holds a transcript path.
_DEFAULT_NAME_REGISTRY = SessionNameRegistry()
_DEFAULT_TITLE_CACHE = TranscriptTitleCache()

_MAX_QUESTIONS = 4
_MAX_QUESTION_CHARS = 4_096
_MAX_QUESTION_HEADER_CHARS = 128
_MAX_QUESTION_OPTIONS = 20
_MAX_OPTION_LABEL_CHARS = 512
_MAX_OPTION_DESCRIPTION_CHARS = 2_048
_MAX_QUESTION_PAYLOAD_CHARS = 32_000
_MAX_TOOL_NAME_CHARS = 128
_MAX_PERMISSION_DESCRIPTION_CHARS = 240

_PERMISSION_FALLBACK_DESCRIPTIONS = {
    "Agent": "Start an agent task",
    "Bash": "Run a shell command",
    "Edit": "Edit a file",
    "Glob": "Find matching files",
    "Grep": "Search file contents",
    "Read": "Read a file",
    "WebFetch": "Fetch a web page",
    "WebSearch": "Search the web",
    "Write": "Write a file",
}


def clear_claude_session_state() -> None:
    """Scrub module-owned activation, explicit-name, and title caches."""

    _DEFAULT_ACTIVATION_REGISTRY.clear()
    _DEFAULT_NAME_REGISTRY.clear()
    _DEFAULT_TITLE_CACHE.clear()


def _session_id(payload: dict) -> "str | None":
    """Best-effort session id from a Claude Code hook payload (or None)."""
    sid = payload.get("session_id")
    return sid if isinstance(sid, str) and sid else None


# ---------------------------------------------------------------------------
# Task 2.4 — hook-to-managed-session correlation
# ---------------------------------------------------------------------------


def correlation_fields(payload: dict) -> "tuple[str, str | None] | None":
    """Return ``(raw hook_event_name, session_id)`` for session correlation.

    Called by ``server._handle_claude_hook_body`` only when the wrapper's
    correlation token header is present (Task 2.4). Returns ``None`` for a
    payload with no usable event name, so the caller can skip correlation for
    a malformed body without special-casing it.

    The raw, un-normalized ``hook_event_name`` is returned rather than
    ``parse_claude_event``'s normalized ``kind``: the managed-session store's
    event vocabulary (``Stop``, ``SessionEnd``, ...) matches the raw hook
    name, not the HUD's internal event kinds.
    """
    if not isinstance(payload, dict):
        return None
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str) or not event_name:
        return None
    return event_name, _session_id(payload)


def _optional_string(payload: dict, key: str) -> "str | None":
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _claude_response_identity(payload: dict) -> dict:
    """Return only the source metadata needed by the completion pipeline."""
    return {
        "agent": "claude",
        "session_id": _session_id(payload),
        "prompt_id": _optional_string(payload, "prompt_id"),
        "cwd": _optional_string(payload, "cwd"),
    }


def _merge_missing_claude_identity(identity: dict, payload: dict) -> None:
    nested = _claude_response_identity(payload)
    for key in ("session_id", "prompt_id", "cwd"):
        if identity[key] is None:
            identity[key] = nested[key]


def _decode_claude_stop_envelope(value: str) -> "dict | None":
    """Decode one exact, allowlisted Claude Stop envelope from ``value``."""
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    if decoded.get("hook_event_name") != "Stop":
        return None
    if not set(decoded).issubset(_CLAUDE_STOP_ENVELOPE_KEYS):
        return None
    return decoded


def _extract_spoken_summary(text: str) -> "str | None":
    """Return marker-free text from the final non-empty physical line.

    The visual response remains opaque and unchanged. The marker must still open
    the final non-empty line, and an empty marker body is still silent, but
    matching is deliberately tolerant of the ways the marker survives a round
    trip through a model and a Windows console (iteration 03):

    - surrounding whitespace on the line,
    - an optional U+FE0F variation selector after the emoji,
    - no space between the marker and the body, and
    - the cp1252/latin-1 mojibake renderings of the marker's UTF-8 bytes.

    A line with no recognizable marker stays silent. Whisperstream never selects
    or rewrites a summary locally.

    # ============================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # ``text`` and the returned summary are Claude response content. Never log
    # either value. Metadata only.
    # ============================================================================
    """
    return extract_spoken_summary(text)


def _normalize_claude_response(
    raw_msg: object,
    payload: dict,
    *,
    park_eligible: bool,
) -> dict:
    """Build a privacy-safe response event without interpreting arbitrary JSON.

    # ============================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # ``raw_msg`` and nested ``last_assistant_message`` values are user content.
    # Log only lengths, field types, and normalization stage names.
    # ============================================================================
    """
    identity = _claude_response_identity(payload)

    if not isinstance(raw_msg, str):
        return {
            "kind": "response",
            "text": "",
            "empty": True,
            "spoken_summary": None,
            "park_eligible": park_eligible,
            **identity,
        }

    text = raw_msg
    for layer in range(_MAX_RESPONSE_ENVELOPE_LAYERS):
        envelope = _decode_claude_stop_envelope(text)
        if envelope is None:
            break
        _merge_missing_claude_identity(identity, envelope)
        nested = envelope.get("last_assistant_message")
        if not isinstance(nested, str):
            logger.debug(
                "[ClaudeHUD] recognized Stop envelope malformed layer=%d "
                "message_type=%s",
                layer + 1,
                type(nested).__name__,
            )
            return {
                "kind": "response",
                "text": "",
                "empty": True,
                "spoken_summary": None,
                "park_eligible": park_eligible,
                **identity,
            }
        text = nested

    if text.strip():
        return {
            "kind": "response",
            "text": text,
            "empty": False,
            "spoken_summary": _extract_spoken_summary(text),
            "park_eligible": park_eligible,
            **identity,
        }

    return {
        "kind": "response",
        "text": "",
        "empty": True,
        "spoken_summary": None,
        "park_eligible": park_eligible,
        **identity,
    }


def _resolve_session_name(
    payload: dict,
    session_id: "str | None",
    name_registry: SessionNameRegistry,
) -> "str | None":
    """Resolve steps 1 and 2 of the session-name chain at the hook seam.

    Explicit registry name first, then the `/rename` ``custom-title`` read off
    the session transcript. Returns ``None`` when neither yields a name, in
    which case ``agent_activity`` falls back to the project basename and then
    to the literal agent label.

    ``transcript_path`` is consumed here and goes out of scope: it is never
    attached to the parsed event, never broadcast, and never logged. Only the
    resolved name travels onward.

    # ========================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # The returned name and the transcript path are both transcript-class.
    # Log ``named=true|false`` and nothing more.
    # ========================================================================
    """
    if session_id is None:
        return None
    explicit = name_registry.get(session_id)
    if explicit is not None:
        return explicit
    return _DEFAULT_TITLE_CACHE.get(session_id, payload.get("transcript_path"))


def _attach_session_name(
    event: dict,
    name: "str | None",
    *,
    with_custom_flag: bool = True,
) -> dict:
    """Attach a resolved name to a parsed event, or leave the event alone.

    ``with_custom_flag`` is False for question events: the question snapshot
    carries only the name (the custom flag gates speech, which questions opt
    out of), and its wire allowlist has no ``source_name_is_custom`` key.
    """
    if name is None:
        logger.debug("[ClaudeHUD] session_name resolved named=false")
        return event
    event["source_name"] = name
    if with_custom_flag:
        event["source_name_is_custom"] = True
    logger.debug("[ClaudeHUD] session_name resolved named=true")
    return event


def _match_skill_expansion(
    payload: dict, command: str, prompt_token: str
) -> "tuple[bool, str | None]":
    """Validate an exact Whisperstream plugin command and return its argument.

    Mirrors the dual `command_name` + prompt-token check the original
    `/whisperstream:speak` matcher used, because `command_name` normalization
    is unreliable. Returns ``(matched, argument)``; ``argument`` is the raw
    trailing token after the command, or ``None`` when the command was
    invoked bare. The argument is prompt text, i.e. transcript-class: never
    log it.
    """
    if (
        payload.get("expansion_type") != "slash_command"
        or payload.get("command_source") != "plugin"
    ):
        return False, None
    prompt = payload.get("prompt")
    argument = None
    if isinstance(prompt, str):
        tokens = prompt.split(maxsplit=1)
        if len(tokens) > 1:
            argument = tokens[1]
    command_name = payload.get("command_name")
    if command_name == command:
        return True, argument
    if isinstance(prompt, str):
        tokens = prompt.split(maxsplit=1)
        if tokens and tokens[0] == prompt_token:
            return True, argument
    return False, None


def _is_speech_skill_expansion(payload: dict) -> bool:
    """Validate the exact Whisperstream plugin activation command."""
    matched, _argument = _match_skill_expansion(
        payload, f"{plugin_identity().name}:speak", f"/{plugin_identity().name}:speak"
    )
    return matched


def _normalize_questions(tool_input: object) -> "list[dict] | None":
    """Validate and detach the structured AskUserQuestion input.

    # ============================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # Questions, option labels, and descriptions are user/agent content. Never
    # log their values. Metadata counts only.
    # ============================================================================
    """
    if not isinstance(tool_input, dict):
        return None
    raw_questions = tool_input.get("questions")
    if (
        not isinstance(raw_questions, list)
        or not 1 <= len(raw_questions) <= _MAX_QUESTIONS
    ):
        return None

    normalized: list[dict] = []
    seen_question_keys: set[str] = set()
    total_chars = 0
    for raw_question in raw_questions:
        if not isinstance(raw_question, dict):
            return None
        question = raw_question.get("question")
        header = raw_question.get("header")
        options = raw_question.get("options")
        multi_select = raw_question.get("multiSelect", False)
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > _MAX_QUESTION_CHARS
            or question in seen_question_keys
            or not isinstance(header, str)
            or not header.strip()
            or len(header) > _MAX_QUESTION_HEADER_CHARS
            or not isinstance(options, list)
            or not 1 <= len(options) <= _MAX_QUESTION_OPTIONS
            or not isinstance(multi_select, bool)
        ):
            return None

        normalized_options: list[dict] = []
        total_chars += len(question) + len(header)
        for raw_option in options:
            if not isinstance(raw_option, dict):
                return None
            label = raw_option.get("label")
            if (
                not isinstance(label, str)
                or not label.strip()
                or len(label) > _MAX_OPTION_LABEL_CHARS
            ):
                return None
            option: dict[str, str] = {"label": label}
            total_chars += len(label)
            if "description" in raw_option:
                description = raw_option.get("description")
                if (
                    not isinstance(description, str)
                    or len(description) > _MAX_OPTION_DESCRIPTION_CHARS
                ):
                    return None
                option["description"] = description
                total_chars += len(description)
            normalized_options.append(option)

        if total_chars > _MAX_QUESTION_PAYLOAD_CHARS:
            return None
        seen_question_keys.add(question)
        normalized.append(
            {
                "question": question,
                "header": header,
                "options": normalized_options,
                "multiSelect": multi_select,
            }
        )
    return normalized


def _normalize_permission(payload: dict) -> "dict | None":
    """Keep only bounded human-facing metadata from a permission request.

    # ============================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # Tool descriptions and inputs can contain commands, paths, and user data.
    # Never log them. Raw tool input and permission suggestions are never copied
    # into the normalized event.
    # ============================================================================
    """
    result = normalize_permission_event(payload, agent="claude")
    if result is not None:
        # Preserve the shipped Claude normalized shape byte-for-byte. The
        # endpoint pins the provider before parsing.
        result.pop("agent", None)
    return result


def _bounded_display_text(value: object, *, max_chars: int) -> "str | None":
    """Return compact printable UI text or ``None`` for unsafe input."""
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    if not compact or not all(character.isprintable() for character in compact):
        return None
    return compact[:max_chars]


def parse_claude_event(
    payload: dict,
    *,
    activation_registry: "SessionActivationRegistry | None" = None,
    name_registry: "SessionNameRegistry | None" = None,
    speech_activation: str = "per_session",
) -> dict | None:
    """Map a raw Claude Code hook JSON body to a normalized agent event.

    Parameters
    ----------
    payload:
        The raw JSON dict posted by the PowerShell forwarder from a Claude
        Code hook (Stop, SubagentStop, UserPromptSubmit, or Notification).

    Returns
    -------
    dict | None
        Normalized events (shared vocabulary: ``agent_events.EVENT_KINDS``).
        Every returned dict carries ``session_id`` (str or None) so the
        ``/api/agent-event`` handler can run session attribution.

        - ``{"kind": "response", "text": <str>, "empty": <bool>,
          "spoken_summary": <str | None>, ...}`` for a Stop event that should
          expand the HUD. ``empty=True`` when
          ``last_assistant_message`` is absent/empty/whitespace (pure-tool
          turn); the HUD owns the fallback copy ("Claude finished.").
          ``text`` is always the exact visual response (``""`` for the empty
          case). ``spoken_summary`` contains only a valid final ``🔊 `` line
          with the marker removed, otherwise ``None``. Response events also
          carry ``agent``, ``session_id``, ``prompt_id``, and ``cwd`` for source
          attribution. Transcript paths and raw payloads are never copied
          forward.
        - ``{"kind": "working", ...}`` for an exact ``permission_prompt``
          Notification compatibility signal. Notifications never prove that an
          answer hook exists.
        - ``{"kind": "error", ...}`` for an exact forward-compatible error
          Notification discriminator.
        - ``{"kind": "progress", "detail": "subagent_done", ...}`` for
          SubagentStop.  Progress events NEVER fire the HUD (the endpoint
          only routes them to conversational-mode template speech), which
          preserves the original "SubagentStop never fires the HUD" contract.
        - ``{"kind": "prompt_submitted", ...}`` for UserPromptSubmit (session
          attribution + typed-prompt barge-in).  The submitted prompt text is
          transcript-class data and is deliberately NOT copied into the
          normalized event.
        - ``{"kind": "speech_activation", "named": <bool>, ...}`` for an exact
          invocation of the Whisperstream plugin speech skill. ``named`` is
          ``True`` only when a trailing argument set the session name.
        - ``{"kind": "session_name", "named": <bool>, ...}`` for an exact
          invocation of the Whisperstream plugin name skill. A trailing
          argument sets the name (``named=True``); a bare invocation clears
          it (``named=False``). Never touches speech activation.
        - ``{"kind": "question", "phase": "surfaced" | "answerable",
          "questions": [...], ...}`` for a validated AskUserQuestion tool call.
        - ``{"kind": "permission", "tool_name": <str>,
          "description": <str>, ...}`` for a bounded PermissionRequest.
        - ``None`` for anything else that must NOT reach the HUD or speech:
          idle_prompt Notifications, unknown events, malformed payloads, or
          ``SessionStart``/``SessionEnd`` (recognized, but managed-session
          lifecycle only -- see ``managed_sessions.py`` and Task 2.4's
          correlation path in ``server.py``).

    Logging note
    ------------
    # ============================================================================
    # TRANSCRIPT DATA — DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use metadata only (char count, word count, duration, stage name).
    # Never pass last_assistant_message content as a logging arg.
    # ============================================================================
    """
    if not isinstance(payload, dict):
        return None

    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str):
        return None

    registry = (
        _DEFAULT_ACTIVATION_REGISTRY
        if activation_registry is None
        else activation_registry
    )
    names = (
        _DEFAULT_NAME_REGISTRY if name_registry is None else name_registry
    )

    # ------------------------------------------------------------------
    # UserPromptExpansion — exact per-session Agent Speech activation.
    # Prompt/command arguments are transcript-class and never copied.
    # ------------------------------------------------------------------
    if event_name == "UserPromptExpansion":
        session_id = _session_id(payload)
        if session_id is None:
            return None

        # /whisperstream:name — sets or clears the session name. Does not
        # touch speech activation (scope decision 6).
        name_matched, name_argument = _match_skill_expansion(
            payload, f"{plugin_identity().name}:name", f"/{plugin_identity().name}:name"
        )
        if name_matched:
            if name_argument is not None:
                bounded_argument = name_argument[:_MAX_RAW_SKILL_ARGUMENT_CHARS]
                named = names.set(session_id, bounded_argument)
            else:
                names.clear_name(session_id)
                named = False
            logger.debug(
                "[ClaudeHUD] UserPromptExpansion -> session_name named=%s",
                named,
            )
            return {
                "kind": "session_name",
                "session_id": session_id,
                "named": named,
            }

        # /whisperstream:speak [name] — activates speech and, if a trailing
        # name is present, also sets the session name.
        speech_matched, speech_argument = _match_skill_expansion(
            payload, f"{plugin_identity().name}:speak", f"/{plugin_identity().name}:speak"
        )
        if not speech_matched:
            return None
        named = False
        if speech_argument is not None:
            bounded_argument = speech_argument[:_MAX_RAW_SKILL_ARGUMENT_CHARS]
            named = names.set(session_id, bounded_argument)
        registry.activate(session_id)
        if not registry.is_active(session_id):
            return None
        logger.debug("[ClaudeHUD] UserPromptExpansion -> speech_activation")
        return {
            "kind": "speech_activation",
            "session_id": session_id,
            "named": named,
        }

    # ------------------------------------------------------------------
    # SubagentStop — progress milestone (Phase 2, conversational mode).
    # Never fires the HUD; the endpoint routes it to template speech only.
    # ------------------------------------------------------------------
    if event_name == "SubagentStop":
        logger.debug("[ClaudeHUD] SubagentStop -> progress subagent_done")
        return {
            "kind": "progress",
            "detail": "subagent_done",
            "session_id": _session_id(payload),
        }

    # ------------------------------------------------------------------
    # UserPromptSubmit — session attribution + typed-prompt barge-in.
    # The prompt text itself is transcript data; never copy it forward.
    # ------------------------------------------------------------------
    if event_name == "UserPromptSubmit":
        logger.debug("[ClaudeHUD] UserPromptSubmit -> prompt_submitted")
        session_id = _session_id(payload)
        result = {"kind": "prompt_submitted", "session_id": session_id}
        if (
            speech_activation == "always_on"
            or (
                speech_activation == "per_session"
                and registry.is_active(session_id)
            )
        ):
            result["additional_context"] = SPOKEN_SUMMARY_CONTEXT
        return result

    # ------------------------------------------------------------------
    # Stop — the primary HUD trigger.
    # ------------------------------------------------------------------
    if event_name == "Stop":
        # Extract the response text.
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # last_assistant_message is user content from Claude Code. Never log
        # either the value or content-derived metadata about it.
        # ============================================================================
        raw_msg = payload.get("last_assistant_message")
        result = _normalize_claude_response(
            raw_msg,
            payload,
            park_eligible=True,
        )
        if result["empty"]:
            logger.debug("[ClaudeHUD] Stop response empty turn (pure-tool or no message)")
        return _attach_session_name(
            result,
            _resolve_session_name(payload, result.get("session_id"), names),
        )

    # ------------------------------------------------------------------
    # PreToolUse AskUserQuestion — structured question interaction.
    # ------------------------------------------------------------------
    if event_name == "PreToolUse":
        if payload.get("tool_name") != "AskUserQuestion":
            return None
        questions = _normalize_questions(payload.get("tool_input"))
        if questions is None:
            logger.debug("[ClaudeHUD] AskUserQuestion malformed, deferred")
            return None
        logger.debug(
            "[ClaudeHUD] AskUserQuestion normalized count=%d",
            len(questions),
        )
        session_id = _session_id(payload)
        return _attach_session_name(
            {
                "kind": "question",
                "phase": "surfaced",
                "session_id": session_id,
                "questions": questions,
            },
            _resolve_session_name(payload, session_id, names),
            with_custom_flag=False,
        )

    # ------------------------------------------------------------------
    # PermissionRequest — AskUserQuestion answer park, then generic approval.
    # ------------------------------------------------------------------
    if event_name == "PermissionRequest":
        if payload.get("tool_name") == "AskUserQuestion":
            questions = _normalize_questions(payload.get("tool_input"))
            if questions is None:
                logger.debug(
                    "[ClaudeHUD] AskUserQuestion permission malformed, deferred"
                )
                return None
            logger.debug(
                "[ClaudeHUD] AskUserQuestion answer park normalized count=%d",
                len(questions),
            )
            return {
                "kind": "question",
                "phase": "answerable",
                "session_id": _session_id(payload),
                "questions": questions,
            }
        result = _normalize_permission(payload)
        if result is None:
            logger.debug("[ClaudeHUD] PermissionRequest malformed, deferred")
            return None
        logger.debug("[ClaudeHUD] PermissionRequest normalized")
        # Permissions never route through voice (see claude_interactions.py's
        # broker docstring), so source_name_is_custom would gate nothing here.
        # Mirror the question branch and omit it.
        return _attach_session_name(
            result,
            _resolve_session_name(payload, result.get("session_id"), names),
            with_custom_flag=False,
        )

    # ------------------------------------------------------------------
    # Notification — exact, non-authoritative compatibility activity.
    # ------------------------------------------------------------------
    if event_name == "Notification":
        notification_type = payload.get("notification_type")
        if notification_type == _PERMISSION_PROMPT_MARKER:
            logger.debug(
                "[ClaudeHUD] Notification permission_prompt -> working compatibility"
            )
            return {"kind": "working", "session_id": _session_id(payload)}
        if notification_type == _IDLE_PROMPT_MARKER:
            logger.debug("[ClaudeHUD] Notification idle_prompt, ignored")
            return None
        if notification_type == _ERROR_MARKER:
            logger.debug("[ClaudeHUD] Notification error marker detected")
            return {"kind": "error", "session_id": _session_id(payload)}

        logger.debug("[ClaudeHUD] Notification no recognized type, ignored")
        return None

    # ------------------------------------------------------------------
    # SessionStart / SessionEnd — managed-session lifecycle signals only.
    # Neither ever reaches the HUD or speech: the managed-session store
    # correlates on the raw hook_event_name independently, in
    # server._handle_claude_hook_body, before parse_claude_event is even
    # called (Task 2.4). Recognizing them here just keeps them out of the
    # "Unknown hook_event_name" log bucket below; the two events carry no
    # HUD-normalized kind because they never should.
    # ------------------------------------------------------------------
    if event_name in ("SessionStart", "SessionEnd"):
        logger.debug(
            "[ClaudeHUD] %s -> managed-session lifecycle only, no HUD kind",
            event_name,
        )
        return None

    # ------------------------------------------------------------------
    # Anything else (PostToolUse, unknown events, etc.)
    # ------------------------------------------------------------------
    logger.debug("[ClaudeHUD] hook ignored reason=unknown_event")
    return None


# ---------------------------------------------------------------------------
# Task 1.5 — Forwarder script writer
# ---------------------------------------------------------------------------

# Stable relative path of the forwarder script inside the installed data dir.
_FORWARDER_SUBPATH = Path("claude-hud") / "forwarder.ps1"

# Source of the packaged template (installed alongside this module).
_TEMPLATE_PACKAGE = "whisperstream.claude_hook"
_TEMPLATE_FILE = "forwarder.ps1"


def install_forwarder_script(dest_dir: Path) -> Path:
    """Materialize the PowerShell forwarder script to a stable absolute path.

    Reads the packaged ``claude_hook/forwarder.ps1`` template and writes it to
    ``<dest_dir>/claude-hud/forwarder.ps1``, creating parent directories as
    needed.  Returns the destination :class:`~pathlib.Path` so the caller
    (Tasks 1.6/1.7) can embed that absolute path as the marker in
    ``~/.claude/settings.json``.

    This function is idempotent: calling it multiple times with the same
    ``dest_dir`` overwrites the script with the current template (ensuring the
    installed copy is always up-to-date).

    Parameters
    ----------
    dest_dir:
        The Whisperstream application data directory, typically the return
        value of :func:`whisperstream.config_manager.get_app_dir`.

    Returns
    -------
    Path
        Absolute path to the installed forwarder script.
    """
    dest = dest_dir / _FORWARDER_SUBPATH
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Read the template from the installed package so this works in both
    # editable dev installs (where the .ps1 lives in the source tree) and
    # frozen/wheel installs (where hatchling bundles it into the wheel).
    try:
        template_text = (
            importlib.resources.files(_TEMPLATE_PACKAGE)
            .joinpath(_TEMPLATE_FILE)
            .read_text(encoding="utf-8")
        )
    except Exception:
        # Fallback: resolve relative to this module's own file — works in all
        # editable install layouts where importlib.resources might struggle.
        _here = Path(__file__).parent
        template_text = (_here / "claude_hook" / _TEMPLATE_FILE).read_text(
            encoding="utf-8"
        )

    dest.write_text(template_text, encoding="utf-8")
    logger.debug("[ClaudeHUD] forwarder script status=installed")
    return dest


_PLUGIN_TEMPLATE_PACKAGE = "whisperstream.claude_plugin"
_PLUGIN_MARKETPLACE_SUBPATH = Path("claude-plugin")
_PLUGIN_OWNER_MARKER = Path(".whisperstream-owned")
_PLUGIN_OWNER_MARKER_BYTES = b"whisperstream-claude-plugin-v1\n"
_PLUGIN_RELOAD_MESSAGE = "Start a new Claude Code session, or run /reload-plugins."
_PLUGIN_CLI_TIMEOUT_SECONDS = 20.0
_PLUGIN_TEMPLATE_FILES = (
    Path(".claude-plugin") / "plugin.json",
    Path("skills") / "speak" / "SKILL.md",
    Path("skills") / "name" / "SKILL.md",
    Path("hooks") / "hooks.json",
    Path("hooks") / "register.js",
    Path("scripts") / "forward-hook.ps1",
)
_PLUGIN_DARWIN_TEMPLATE_FILES = (
    Path(".claude-plugin") / "plugin.json",
    Path("skills") / "speak" / "SKILL.md",
    Path("skills") / "name" / "SKILL.md",
    Path("hooks") / "hooks.json",
    Path("hooks") / "register.js",
    Path("scripts") / "forward-hook.sh",
)
_PLUGIN_ALL_MANAGED_FILES = tuple(
    dict.fromkeys(_PLUGIN_TEMPLATE_FILES + _PLUGIN_DARWIN_TEMPLATE_FILES)
)


class ClaudePluginConflictError(RuntimeError):
    """Raised rather than overwriting a same-named, user-owned plugin."""


class ClaudePluginSettingsError(RuntimeError):
    """Raised when Claude settings cannot be updated without data loss."""


def _plugin_template_bytes(relative_path: Path) -> bytes:
    try:
        resource = importlib.resources.files(_PLUGIN_TEMPLATE_PACKAGE)
        return resource.joinpath(*relative_path.parts).read_bytes()
    except Exception:
        return (Path(__file__).parent / "claude_plugin" / relative_path).read_bytes()


def _plugin_manifest() -> dict:
    """Return the packaged manifest, which is the plugin version source of truth."""
    try:
        manifest = json.loads(
            _plugin_template_bytes(
                Path(".claude-plugin") / "plugin.json"
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClaudePluginSettingsError(
            "The packaged Claude plugin manifest is invalid"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("name") != "whisperstream":
        raise ClaudePluginSettingsError(
            "The packaged Claude plugin manifest has an unexpected identity"
        )
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ClaudePluginSettingsError(
            "The packaged Claude plugin manifest has no version"
        )
    return manifest


def _plugin_version() -> str:
    return str(_plugin_manifest()["version"])


def _marketplace_bytes(version: str | None = None) -> bytes:
    version = version or _plugin_version()
    manifest = {
        "name": plugin_identity().name,
        "owner": {"name": "Whisperstream"},
        "description": "Local Whisperstream integration for Claude Code.",
        "plugins": [
            {
                "name": plugin_identity().name,
                "source": f"./plugins/{plugin_identity().name}",
                "description": (
                    "Connects Claude Code sessions to the local Whisperstream "
                    "HUD."
                ),
                "version": version,
            }
        ],
    }
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _owned_plugin_paths(app_dir: Path) -> tuple[Path, Path]:
    subpath = str(_PLUGIN_MARKETPLACE_SUBPATH) + ("-dev" if plugin_identity().dev else "")
    marketplace_dir = app_dir / subpath
    return marketplace_dir, marketplace_dir / "plugins" / plugin_identity().name


def _has_owner_marker(plugin_dir: Path) -> bool:
    try:
        return (
            (plugin_dir / _PLUGIN_OWNER_MARKER).read_bytes()
            == _PLUGIN_OWNER_MARKER_BYTES
        )
    except OSError:
        return False


def _plugin_template_files(hook_platform: str) -> tuple[Path, ...]:
    return (
        _PLUGIN_DARWIN_TEMPLATE_FILES
        if hook_platform == "darwin"
        else _PLUGIN_TEMPLATE_FILES
    )


def _plugin_hooks_bytes(hook_platform: str) -> bytes:
    source = _plugin_template_bytes(Path("hooks") / "hooks.json")
    if hook_platform != "darwin":
        return source
    manifest = json.loads(source.decode("utf-8"))
    for registrations in manifest["hooks"].values():
        for registration in registrations:
            for hook in registration.get("hooks", []):
                hook["command"] = "/bin/zsh"
                hook["args"] = [
                    "${CLAUDE_PLUGIN_ROOT}/scripts/forward-hook.sh"
                ]
    return (json.dumps(manifest, indent=2) + "\n").encode("utf-8")


def _generated_plugin_payload(app_dir: Path, hook_platform: str) -> dict[Path, bytes]:
    template_files = _plugin_template_files(hook_platform)
    return payload_files("claude", app_dir, {
        relative: (
            _plugin_hooks_bytes(hook_platform)
            if relative == Path("hooks") / "hooks.json"
            else _plugin_template_bytes(relative)
        )
        for relative in template_files
    })


def bundled_plugin_version(app_dir: Path, *, hook_platform: str = "windows") -> str:
    """Return the exact plugin version this app installs, without writing files."""
    _plugin_manifest()
    generated = _generated_plugin_payload(app_dir, hook_platform)
    return str(json.loads(generated[Path(".claude-plugin/plugin.json")])["version"])


def install_claude_plugin(app_dir: Path, *, hook_platform: str = "windows") -> dict:
    """Install or update the owned local Claude plugin marketplace."""
    _plugin_manifest()  # Validate the packaged identity before generating variants.
    marketplace_dir, plugin_dir = _owned_plugin_paths(app_dir)
    owned_before = _has_owner_marker(plugin_dir)
    marketplace_path = marketplace_dir / ".claude-plugin" / "marketplace.json"
    template_files = _plugin_template_files(hook_platform)
    generated = _generated_plugin_payload(app_dir, hook_platform)
    managed_targets = {plugin_dir / relative: data for relative, data in generated.items()}
    version = json.loads(generated[Path('.claude-plugin/plugin.json')])['version']
    managed_targets[marketplace_path] = _marketplace_bytes(version)

    if not owned_before and any(path.exists() for path in managed_targets):
        raise ClaudePluginConflictError(
            "A same-named Claude plugin already exists and is not owned "
            "by Whisperstream"
        )

    changed = not owned_before
    marker_path = plugin_dir / _PLUGIN_OWNER_MARKER
    if not owned_before:
        _atomic_write_bytes(marker_path, _PLUGIN_OWNER_MARKER_BYTES)

    for target, expected in managed_targets.items():
        try:
            current = target.read_bytes()
        except OSError:
            current = None
        if current != expected:
            _atomic_write_bytes(target, expected)
            changed = True

    # A platform switch in an owned integration must not retain the other
    # platform's executable forwarder. Only exact packaged names are touched.
    for relative in set(_PLUGIN_ALL_MANAGED_FILES) - set(template_files):
        try:
            (plugin_dir / relative).unlink()
            changed = True
        except FileNotFoundError:
            pass

    status = "installed" if not owned_before else ("updated" if changed else "current")
    logger.info("[ClaudeHUD] plugin files status=%s", status)
    return {
        "status": status,
        "plugin_dir": str(plugin_dir),
        "marketplace_dir": str(marketplace_dir),
        "version": version,
        "claude_cli_available": _claude_cli_available(),
        "payload_fingerprint": payload_fingerprint(generated),
        "refresh_required": payload_needs_sync(plugin_dir, payload_fingerprint(generated)),
    }


def _claude_cli_available() -> bool:
    """Best-effort Claude CLI discovery that never blocks installation."""
    try:
        ensure_user_path()
        return shutil.which("claude") is not None
    except (OSError, TypeError):
        return False


def _claude_cli_path() -> "str | None":
    """Resolve the supported Claude executable without mutating plugin state."""
    try:
        ensure_user_path()
        return shutil.which("claude")
    except (OSError, TypeError):
        return None


def _run_claude_plugin_cli(executable: str, arguments: list[str]):
    """Run one bounded, non-interactive Claude plugin command."""
    return subprocess.run(
        [executable, "plugin", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_PLUGIN_CLI_TIMEOUT_SECONDS,
        check=False,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0
        ),
    )


def _installed_whisperstream_plugin(value: object) -> "dict | None":
    if not isinstance(value, list):
        return None
    for item in value:
        if (
            isinstance(item, dict)
            and item.get("id") == plugin_identity().selector
            and item.get("scope") == "user"
        ):
            return item
    return None


def _configured_whisperstream_marketplace(value: list[object]) -> "dict | None":
    for item in value:
        if (
            isinstance(item, dict)
            and item.get("name") == plugin_identity().name
        ):
            return item
    return None


def _same_directory_path(value: object, expected: Path) -> bool:
    if not isinstance(value, str):
        return False
    try:
        actual_path = os.path.normcase(str(Path(value).resolve(strict=False)))
        expected_path = os.path.normcase(str(expected.resolve(strict=False)))
    except (OSError, RuntimeError):
        return False
    return actual_path == expected_path


def _numeric_version(value: str) -> "tuple[int, ...] | None":
    parts = value.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _plugin_not_installed(materialized: dict) -> dict:
    """A background refresh found nothing to update; nothing was changed."""
    return {
        "ok": True,
        **materialized,
        "source_status": materialized["status"],
        "status": "not_installed",
        "installed_version": None,
        "reload_required": False,
        "reload_message": None,
        "error": None,
    }


def _plugin_failure(
    install_result: dict,
    error: str,
    *,
    installed_version: "str | None" = None,
) -> dict:
    logger.warning("[ClaudeHUD] plugin reconcile failed reason=%s", error)
    return {
        "ok": False,
        **install_result,
        "source_status": install_result["status"],
        "status": "failed",
        "installed_version": installed_version,
        "reload_required": False,
        "reload_message": None,
        "error": error,
    }


def reconcile_claude_plugin(
    app_dir: Path,
    *,
    install_result: "dict | None" = None,
    hook_platform: str = "windows",
    cli_runner: "Callable[[str, list[str]], object] | None" = None,
    force: bool = False,
    keep_disabled: bool = False,
) -> dict:
    """Reconcile the bundled plugin into Claude's supported user-scope cache.

    The local marketplace is materialized first. Claude's own CLI exclusively
    owns the versioned cache. Inspection or update failures leave the last
    working cached plugin untouched and return structured, content-free status.

    ``force`` reinstalls even an identical version (Connect is the recovery
    path). A plugin the user's Agents pause disabled is re-enabled unless
    ``keep_disabled`` marks a background refresh, which only updates an
    existing install: it never adds the marketplace, installs, or enables.
    """
    materialized = install_result or install_claude_plugin(app_dir)
    executable = _claude_cli_path()
    if executable is None:
        return _plugin_failure(materialized, "claude_cli_unavailable")

    run_cli = cli_runner or _run_claude_plugin_cli
    marketplace_dir = Path(materialized["marketplace_dir"])
    try:
        marketplaces_result = run_cli(
            executable,
            ["marketplace", "list", "--json"],
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _plugin_failure(materialized, type(exc).__name__)
    if marketplaces_result.returncode != 0:
        return _plugin_failure(materialized, "marketplace_list_failed")
    try:
        marketplaces = json.loads(marketplaces_result.stdout)
    except json.JSONDecodeError:
        return _plugin_failure(materialized, "marketplace_list_invalid_json")
    if not isinstance(marketplaces, list):
        return _plugin_failure(materialized, "marketplace_list_invalid_json")

    legacy = next((item for item in marketplaces if isinstance(item, dict)
                   and item.get("name") == "whisperstream"
                   and item.get("source") == "directory"
                   and legacy_dev_marketplace_owned(item.get("path"), app_dir, "claude")), None)
    if legacy is not None:
        try:
            listed_legacy = _run_claude_plugin_cli(executable, ["list", "--json"])
            if listed_legacy.returncode != 0:
                return _plugin_failure(materialized, "plugin_list_failed")
            records = json.loads(listed_legacy.stdout)
            if not isinstance(records, list):
                return _plugin_failure(materialized, "plugin_list_invalid_json")
            if any(isinstance(item, dict) and item.get("id") == "whisperstream@whisperstream"
                   and item.get("scope") == "user" for item in records):
                removed = _run_claude_plugin_cli(executable, ["uninstall", "whisperstream@whisperstream", "--scope", "user"])
                if removed.returncode != 0:
                    return _plugin_failure(materialized, "legacy_plugin_remove_failed")
            removed = _run_claude_plugin_cli(executable, ["marketplace", "remove", "whisperstream"])
            if removed.returncode != 0:
                return _plugin_failure(materialized, "legacy_marketplace_remove_failed")
            marketplaces = [item for item in marketplaces if item is not legacy]
        except (OSError, subprocess.SubprocessError, ValueError):
            return _plugin_failure(materialized, "legacy_migration_failed")

    configured_marketplace = _configured_whisperstream_marketplace(marketplaces)
    if keep_disabled and configured_marketplace is None:
        return _plugin_not_installed(materialized)
    marketplace_added = configured_marketplace is None
    if configured_marketplace is not None and (
        configured_marketplace.get("source") != "directory"
        or not _same_directory_path(
            configured_marketplace.get("path"),
            marketplace_dir,
        )
    ):
        return _plugin_failure(materialized, "marketplace_conflict")

    if marketplace_added:
        marketplace_add_args = ["marketplace", "add", str(marketplace_dir)]
        if hook_platform == "darwin":
            marketplace_add_args.extend(["--scope", "user"])
        try:
            added = run_cli(
                executable,
                marketplace_add_args,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _plugin_failure(materialized, type(exc).__name__)
        if added.returncode != 0:
            return _plugin_failure(materialized, "marketplace_add_failed")

    try:
        listed = run_cli(executable, ["list", "--json"])
    except (OSError, subprocess.SubprocessError) as exc:
        return _plugin_failure(materialized, type(exc).__name__)
    if listed.returncode != 0:
        return _plugin_failure(materialized, "plugin_list_failed")
    try:
        installed = _installed_whisperstream_plugin(json.loads(listed.stdout))
    except json.JSONDecodeError:
        return _plugin_failure(materialized, "plugin_list_invalid_json")

    bundled_version = str(materialized["version"])
    installed_version = (
        str(installed.get("version"))
        if installed is not None and isinstance(installed.get("version"), str)
        else None
    )
    was_disabled = installed is not None and installed.get("enabled") is False
    if keep_disabled and installed is None:
        return _plugin_not_installed(materialized)

    if force and installed is not None:
        try:
            removed = run_cli(
                executable,
                ["uninstall", plugin_identity().selector, "--scope", "user"],
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _plugin_failure(
                materialized, type(exc).__name__, installed_version=installed_version
            )
        if removed.returncode != 0:
            return _plugin_failure(
                materialized, "plugin_uninstall_failed", installed_version=installed_version
            )
        installed = None
        was_disabled = False

    if installed is None:
        action = "installed"
        arguments = ["install", plugin_identity().selector, "--scope", "user"]
    elif installed_version == bundled_version and not materialized.get("refresh_required"):
        action = "current"
        arguments = []
    else:
        installed_key = (
            _numeric_version(installed_version)
            if installed_version is not None
            else None
        )
        bundled_key = _numeric_version(bundled_version)
        if (
            installed_key is not None
            and bundled_key is not None
            and installed_key > bundled_key
        ):
            action = "current"
            arguments = []
        else:
            action = "updated"
            arguments = ["update", plugin_identity().selector, "--scope", "user"]

    if arguments:
        try:
            changed = run_cli(executable, arguments)
        except (OSError, subprocess.SubprocessError) as exc:
            return _plugin_failure(
                materialized,
                type(exc).__name__,
                installed_version=installed_version,
            )
        if changed.returncode != 0:
            return _plugin_failure(
                materialized,
                f"plugin_{arguments[0]}_failed",
                installed_version=installed_version,
            )

    if was_disabled and not keep_disabled:
        try:
            enabled = run_cli(
                executable,
                ["enable", plugin_identity().selector, "--scope", "user"],
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _plugin_failure(
                materialized, type(exc).__name__, installed_version=installed_version
            )
        if enabled.returncode != 0:
            return _plugin_failure(
                materialized, "plugin_enable_failed", installed_version=installed_version
            )

    mark_payload_synced(materialized)
    reload_required = (
        marketplace_added
        or action in {"installed", "updated"}
        or (was_disabled and not keep_disabled)
    )
    logger.info(
        "[ClaudeHUD] plugin reconcile status=%s version=%s reload_required=%s",
        action,
        bundled_version,
        reload_required,
    )
    return {
        "ok": True,
        **materialized,
        "source_status": materialized["status"],
        "status": action,
        "installed_version": (
            bundled_version if reload_required else installed_version
        ),
        "reload_required": reload_required,
        "reload_message": _PLUGIN_RELOAD_MESSAGE if reload_required else None,
        "error": None,
    }


def _remove_empty_directories(paths: tuple[Path, ...]) -> None:
    for path in paths:
        try:
            path.rmdir()
        except OSError:
            pass


def remove_claude_plugin(app_dir: Path, *, hook_platform: str = "windows") -> dict:
    """Remove only files managed by Whisperstream from its local marketplace."""
    marketplace_dir, plugin_dir = _owned_plugin_paths(app_dir)
    if not plugin_dir.exists():
        return {"status": "not_installed", "plugin_dir": str(plugin_dir)}
    if not _has_owner_marker(plugin_dir):
        return {"status": "not_owned", "plugin_dir": str(plugin_dir)}

    del hook_platform  # cleanup removes only the exact owned cross-platform set
    remove_sync_receipt(plugin_dir)
    (plugin_dir / RUNTIME_TARGET).unlink(missing_ok=True)
    for relative in _PLUGIN_ALL_MANAGED_FILES:
        try:
            (plugin_dir / relative).unlink()
        except FileNotFoundError:
            pass
    try:
        (plugin_dir / _PLUGIN_OWNER_MARKER).unlink()
    except FileNotFoundError:
        pass
    try:
        (marketplace_dir / ".claude-plugin" / "marketplace.json").unlink()
    except FileNotFoundError:
        pass

    _remove_empty_directories(
        (
            plugin_dir / ".claude-plugin",
            plugin_dir / "skills" / "speak",
            plugin_dir / "skills" / "name",
            plugin_dir / "skills",
            plugin_dir / "hooks",
            plugin_dir / "scripts",
            plugin_dir,
            marketplace_dir / "plugins",
            marketplace_dir / ".claude-plugin",
            marketplace_dir,
        )
    )
    logger.info("[ClaudeHUD] plugin files status=removed")
    return {"status": "removed", "plugin_dir": str(plugin_dir)}


def remove_claude_plugin_via_cli(
    app_dir: Path,
    *,
    hook_platform: str = "darwin",
    cli_runner: "Callable[[str, list[str]], object] | None" = None,
) -> dict:
    """Remove only the owned user plugin and marketplace through Claude's CLI.

    The provider CLI owns its settings and cache. Whisperstream removes its
    materialized source tree only after the supported uninstall/remove calls
    succeed, and never reads or rewrites the user's settings file directly.
    """

    executable = _claude_cli_path()
    if executable is None:
        return {
            "ok": False,
            "status": "failed",
            "reload_required": False,
            "error": "claude_cli_unavailable",
        }
    run_cli = cli_runner or _run_claude_plugin_cli
    marketplace_dir, _plugin_dir = _owned_plugin_paths(app_dir)
    try:
        plugins_result = run_cli(executable, ["list", "--json"])
        marketplaces_result = run_cli(
            executable, ["marketplace", "list", "--json"]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "status": "failed",
            "reload_required": False,
            "error": type(exc).__name__,
        }
    if plugins_result.returncode != 0 or marketplaces_result.returncode != 0:
        return {
            "ok": False,
            "status": "failed",
            "reload_required": False,
            "error": "plugin_remove_inspection_failed",
        }
    try:
        installed = _installed_whisperstream_plugin(
            json.loads(plugins_result.stdout)
        )
        marketplaces = json.loads(marketplaces_result.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "status": "failed",
            "reload_required": False,
            "error": "plugin_remove_invalid_json",
        }
    if not isinstance(marketplaces, list):
        return {
            "ok": False,
            "status": "failed",
            "reload_required": False,
            "error": "plugin_remove_invalid_json",
        }
    configured = _configured_whisperstream_marketplace(marketplaces)
    if configured is not None and (
        configured.get("source") != "directory"
        or not _same_directory_path(configured.get("path"), marketplace_dir)
    ):
        return {
            "ok": False,
            "status": "failed",
            "reload_required": False,
            "error": "marketplace_conflict",
        }

    changed = False
    for arguments in (
        ["uninstall", plugin_identity().selector, "--scope", "user"] if installed else None,
        ["marketplace", "remove", plugin_identity().name, "--scope", "user"]
        if configured is not None
        else None,
    ):
        if arguments is None:
            continue
        try:
            result = run_cli(executable, arguments)
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "ok": False,
                "status": "failed",
                "reload_required": False,
                "error": type(exc).__name__,
            }
        if result.returncode != 0:
            return {
                "ok": False,
                "status": "failed",
                "reload_required": False,
                "error": "plugin_remove_failed",
            }
        changed = True

    local = remove_claude_plugin(app_dir, hook_platform=hook_platform)
    changed = changed or local.get("status") == "removed"
    return {
        "ok": True,
        "status": "removed" if changed else "not_installed",
        "reload_required": changed,
        "reload_message": _PLUGIN_RELOAD_MESSAGE if changed else None,
        "error": None,
    }


def disconnect_claude_plugin(*, hook_platform: str = "windows") -> dict:
    """Fully uninstall: Claude's own cache entry first, then owned settings/files.

    Removing only settings would leave Claude's cached copy registered, so a
    later Connect could keep loading a stale plugin.
    """
    from whisperstream.config_manager import get_app_dir

    if _claude_cli_path() is not None:
        removed = remove_claude_plugin_via_cli(get_app_dir(), hook_platform=hook_platform)
        if removed.get("ok") is not True:
            return removed
    return disable_claude_hud()


def disable_claude_plugin_via_cli(
    *,
    cli_runner: "Callable[[str, list[str]], object] | None" = None,
) -> dict:
    """Pause the owned plugin through Claude's CLI without uninstalling it.

    Agents' master switch uses this so turning it back on resumes the same
    install. A missing CLI or plugin is not a failure: nothing can run.
    """
    executable = _claude_cli_path()
    if executable is None:
        return {"ok": True, "status": "not_installed", "error": None}
    run_cli = cli_runner or _run_claude_plugin_cli
    try:
        listed = run_cli(executable, ["list", "--json"])
        if listed.returncode != 0:
            return {"ok": False, "status": "failed", "error": "plugin_list_failed"}
        installed = _installed_whisperstream_plugin(json.loads(listed.stdout))
        if installed is None:
            return {"ok": True, "status": "not_installed", "error": None}
        if installed.get("enabled") is not False:
            disabled = run_cli(
                executable,
                ["disable", plugin_identity().selector, "--scope", "user"],
            )
            if disabled.returncode != 0:
                return {"ok": False, "status": "failed", "error": "plugin_disable_failed"}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"ok": False, "status": "failed", "error": type(exc).__name__}
    logger.info("[ClaudeHUD] plugin paused")
    return {"ok": True, "status": "paused", "error": None}


def _plugin_marketplace_setting(marketplace_path: str) -> dict:
    return {
        "source": {
            "source": "directory",
            "path": marketplace_path,
        }
    }


def add_plugin_settings(
    settings: dict, marketplace_path: str, *, keep_disabled: bool = False
) -> dict:
    """Declare and enable the owned local marketplace without touching peers.

    ``keep_disabled`` preserves an explicit pause (``false``) so a background
    refresh never resumes Agents behind the user's back.
    """
    result = copy.deepcopy(settings)
    marketplaces = result.setdefault("extraKnownMarketplaces", {})
    enabled = result.setdefault("enabledPlugins", {})
    if not isinstance(marketplaces, dict) or not isinstance(enabled, dict):
        raise ClaudePluginConflictError(
            "Claude plugin settings have an unsupported shape"
        )

    expected = _plugin_marketplace_setting(marketplace_path)
    existing = marketplaces.get(plugin_identity().name)
    if existing is not None and existing != expected:
        raise ClaudePluginConflictError(
            "A different marketplace already uses the Whisperstream name"
        )
    marketplaces[plugin_identity().name] = expected
    if not (keep_disabled and enabled.get(plugin_identity().selector) is False):
        enabled[plugin_identity().selector] = True
    return result


def remove_plugin_settings(settings: dict, marketplace_path: str) -> dict:
    """Remove our registration only when its exact local source still matches."""
    result = copy.deepcopy(settings)
    marketplaces = result.get("extraKnownMarketplaces")
    if not isinstance(marketplaces, dict):
        return result
    expected = _plugin_marketplace_setting(marketplace_path)
    if marketplaces.get(plugin_identity().name) != expected:
        return result

    del marketplaces[plugin_identity().name]
    enabled = result.get("enabledPlugins")
    if isinstance(enabled, dict):
        enabled.pop(plugin_identity().selector, None)
        if not enabled:
            result.pop("enabledPlugins", None)
    if not marketplaces:
        result.pop("extraKnownMarketplaces", None)
    return result


# ---------------------------------------------------------------------------
# Task 1.6 — settings.json merge/prune helpers (pure functions, no disk I/O)
# ---------------------------------------------------------------------------

# Claude Code hooks schema (settings.json):
#
#   {
#     "hooks": {
#       "<EventName>": [
#         {
#           "matcher": "<optional-string>",   # absent means match all
#           "hooks": [
#             {"type": "command", "command": "<shell-command>"}
#           ]
#         }
#       ]
#     }
#   }
#
# Multiple layers (user / project / local) are merged additively by Claude Code.
# We operate only on the user layer (~/.claude/settings.json).
#
# Identification strategy: "our" entries are identified by the forwarder
# script's absolute path appearing in the inner hook command string.  This is
# the stable marker referenced across Tasks 1.6 and 1.7.

def _our_stop_entry(forwarder_path: str) -> dict:
    """Return the outer hook-group dict we install under 'Stop'."""
    return {
        "hooks": [
            {"type": "command", "command": f'powershell -NoProfile -File "{forwarder_path}"'}
        ]
    }


def _our_notification_entry(forwarder_path: str) -> dict:
    """Return the outer hook-group dict we install under 'Notification'."""
    return {
        "matcher": "permission_prompt",
        "hooks": [
            {"type": "command", "command": f'powershell -NoProfile -File "{forwarder_path}"'}
        ]
    }


# Events we register the forwarder for. Stop + Notification shipped with the
# original HUD; UserPromptSubmit (session attribution + typed-prompt barge-in)
# and SubagentStop (conversational progress) were added in Phase 2. The
# forwarder always exits 0 and prints nothing, so a UserPromptSubmit hook can
# never block a prompt or inject context. remove_hooks prunes ALL of these, so
# disabling also cleans up entries left by an older install (missing event
# lists are skipped harmlessly).
_HOOK_EVENTS: "tuple[tuple[str, Callable[[str], dict]], ...]" = (
    ("Stop", _our_stop_entry),
    ("Notification", _our_notification_entry),
    ("UserPromptSubmit", _our_stop_entry),
    ("SubagentStop", _our_stop_entry),
)


def _entry_has_marker(entry: dict, forwarder_path: str) -> bool:
    """Return True if any inner hook command contains the forwarder_path marker."""
    for inner in entry.get("hooks", []):
        cmd = inner.get("command")
        if isinstance(cmd, str) and forwarder_path in cmd:
            return True
    return False


def add_hooks(settings: dict, forwarder_path: str) -> dict:
    """Return a new settings dict with our Stop and Notification hook entries added.

    Operates on in-memory dicts only (no disk I/O).  The caller (Task 1.7) is
    responsible for reading and writing ``~/.claude/settings.json``.

    Parameters
    ----------
    settings:
        The parsed contents of ``~/.claude/settings.json`` (may be ``{}``).
    forwarder_path:
        Absolute path to the installed forwarder script.  Embedded in the hook
        command and used as the stable marker for idempotency / removal.

    Returns
    -------
    dict
        A new deep-copied dict with our entries appended to the ``Stop`` and
        ``Notification`` event arrays under ``settings["hooks"]``.  If entries
        whose inner command already contains ``forwarder_path`` are present,
        they are left untouched (idempotent).  All other user hooks are
        preserved.
    """
    result = copy.deepcopy(settings)

    hooks_map = result.setdefault("hooks", {})

    for event_name, our_entry_fn in _HOOK_EVENTS:
        event_list = hooks_map.setdefault(event_name, [])
        # Check if our entry is already present (idempotency).
        already_present = any(_entry_has_marker(e, forwarder_path) for e in event_list)
        if not already_present:
            event_list.append(our_entry_fn(forwarder_path))

    return result


def remove_hooks(settings: dict, forwarder_path: str) -> dict:
    """Return a new settings dict with our hook entries pruned.

    Operates on in-memory dicts only (no disk I/O).  Removes only entries
    whose inner hook command contains ``forwarder_path`` from the ``Stop``
    and ``Notification`` event arrays.  All other entries and top-level keys
    are preserved unchanged.  Empty event arrays and an empty ``hooks`` dict
    are cleaned up so the resulting file stays tidy.

    Parameters
    ----------
    settings:
        The parsed contents of ``~/.claude/settings.json`` (may be ``{}``).
    forwarder_path:
        Absolute path used as the stable marker to identify our entries.

    Returns
    -------
    dict
        A new deep-copied dict with our entries removed.  If our entries were
        never present, the result is effectively identical to the input.
    """
    result = copy.deepcopy(settings)

    hooks_map = result.get("hooks")
    if not isinstance(hooks_map, dict):
        # No hooks key or not a dict — nothing to remove.
        return result

    for event_name, _entry_fn in _HOOK_EVENTS:
        event_list = hooks_map.get(event_name)
        if not isinstance(event_list, list):
            continue
        # Keep only entries that do NOT carry our marker.
        filtered = [e for e in event_list if not _entry_has_marker(e, forwarder_path)]
        hooks_map[event_name] = filtered

    # Clean up empty event arrays to keep settings.json tidy.
    # (Empty lists are removed; other events are untouched.)
    for event_name, _entry_fn in _HOOK_EVENTS:
        if event_name in hooks_map and hooks_map[event_name] == []:
            del hooks_map[event_name]

    # If the hooks dict itself is now empty, leave it in place but empty —
    # removing it entirely would drop user intent if they had manually added
    # an empty section.  However, if it was only there because of us and is
    # now empty, we clean it up for a clean no-hooks state.
    if hooks_map == {}:
        del result["hooks"]

    return result


# ---------------------------------------------------------------------------
# Task 1.7 — File-I/O wrappers: enable_claude_hud / disable_claude_hud
#
# These functions read, modify, and atomically write ~/.claude/settings.json.
# They use a SINGLE resolver function (_claude_settings_path) so tests can
# monkeypatch the path without touching the developer's real Claude config.
#
# Key invariants:
# - NEVER touch the Whisperstream app data dir for the Claude config.  Claude's
#   config is global: ~/.claude/settings.json (user home, not LOCALAPPDATA).
# - Missing or corrupt JSON -> treat as {}; never raise on a bad file.
# - Atomic write (mkstemp -> fsync -> os.replace) so a crash cannot clobber
#   the user's other hooks.
# - Never log settings.json content.
# ---------------------------------------------------------------------------


def _claude_settings_path() -> Path:
    """Return the absolute path to ~/.claude/settings.json.

    This is the SINGLE resolver function.  Tests monkeypatch this attribute on
    the module (``monkeypatch.setattr(hud_module, "_claude_settings_path", ...)``).
    Production always uses Path.home() / ".claude" / "settings.json".

    Do NOT use the app data dir here -- Claude's config is global to the user
    account, independent of WHISPERSTREAM_DATA_DIR.
    """
    return Path.home() / ".claude" / "settings.json"


def _read_settings_safe(path: Path) -> dict:
    """Read and parse settings.json.  Returns {} on missing file or invalid JSON.

    Never raises.  A missing or corrupt settings.json is treated as an empty
    dict so enable/disable always make forward progress.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}


def _read_plugin_settings(path: Path) -> dict:
    """Read user settings without replacing unreadable user-owned content."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ClaudePluginSettingsError(
            "Claude settings could not be read safely"
        ) from exc
    if not isinstance(parsed, dict):
        raise ClaudePluginSettingsError("Claude settings must contain a JSON object")
    return parsed


def enable_claude_hud(
    *,
    reconcile_cli: bool = False,
    hook_platform: str = "windows",
    force: bool = False,
    keep_disabled: bool = False,
) -> dict:
    """Install/update the plugin and prune legacy global hooks.

    Steps:
    1. Materialize the owned local marketplace under the app data directory.
    2. Resolve the Claude settings.json path via _claude_settings_path()
       (monkeypatched in tests; real path is ~/.claude/settings.json).
    3. Read settings.json without replacing unreadable user content.
    4. Enable the plugin and remove only our legacy hook entries.
    5. Atomic write via _atomic_write_json.

    Raises:
        OSError: If the atomic write fails after the read-modify-write window.
            The caller (REST endpoint) converts this to an HTTP 500.
    """
    # Import here to avoid circular imports; both are local whisperstream modules.
    from whisperstream.config_manager import get_app_dir
    from whisperstream.eula import _atomic_write_json

    # Step 1: Ensure the forwarder script exists at its stable absolute path.
    app_dir = get_app_dir()
    install_result = install_claude_plugin(app_dir, hook_platform=hook_platform)
    marketplace_dir, _plugin_dir = _owned_plugin_paths(app_dir)

    # Let the provider retire a proven legacy registration before adding settings
    # for the selected channel. A failure must not overwrite that registration.
    reconcile_result = None
    if reconcile_cli:
        reconcile_result = reconcile_claude_plugin(
            app_dir,
            install_result=install_result,
            hook_platform=hook_platform,
            force=force,
            keep_disabled=keep_disabled,
        )
        if not reconcile_result.get("ok"):
            return reconcile_result
        if reconcile_result.get("status") == "not_installed":
            # A background refresh never registers a plugin the user removed.
            return reconcile_result

    # Step 2: Resolve the target settings path.
    settings_path = _claude_settings_path()

    # Step 3: Ensure the parent directory exists (create ~/.claude/ if needed).
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Step 4: Read-modify-write window — keep it as tight as possible.
    settings = _read_plugin_settings(settings_path)
    without_legacy = remove_hooks(
        settings,
        str(app_dir / _FORWARDER_SUBPATH),
    )
    updated = add_plugin_settings(
        without_legacy, str(marketplace_dir), keep_disabled=keep_disabled
    )
    settings_changed = updated != settings

    # Step 5: Atomic write.
    if settings_changed:
        _atomic_write_json(settings_path, updated)

    if reconcile_cli:
        assert reconcile_result is not None
        reload_required = (
            settings_changed
            or bool(reconcile_result.get("reload_required"))
        )
        result = {
            **reconcile_result,
            "reload_required": reload_required,
            "reload_message": (
                _PLUGIN_RELOAD_MESSAGE if reload_required else None
            ),
        }
    else:
        reload_required = settings_changed or install_result["status"] != "current"
        result = {
            "ok": True,
            **install_result,
            "reload_required": reload_required,
            "reload_message": (
                _PLUGIN_RELOAD_MESSAGE if reload_required else None
            ),
        }
    logger.info(
        "[ClaudeHUD] plugin enabled status=%s reload_required=%s",
        result["status"],
        reload_required,
    )
    return result


def disable_claude_hud() -> dict:
    """Disable the plugin and remove only Whisperstream-owned files.

    Steps:
    1. Resolve the owned marketplace path.
    2. Resolve the Claude settings.json path via _claude_settings_path().
    3. Read settings.json without replacing unreadable user content.
    4. Remove the plugin registration and legacy hook entries.
    5. Atomic write.

    Raises:
        OSError: If the atomic write fails.  The caller (REST endpoint) converts
            this to an HTTP 500 so the frontend can show a disable-failure toast.
    """
    from whisperstream.config_manager import get_app_dir
    from whisperstream.eula import _atomic_write_json

    # Step 1: Resolve the same forwarder path that enable used as the marker.
    app_dir = get_app_dir()
    marketplace_dir, _plugin_dir = _owned_plugin_paths(app_dir)

    # Step 2: Resolve the target settings path.
    settings_path = _claude_settings_path()

    # Step 3: Read-modify-write window.
    settings = _read_plugin_settings(settings_path)
    without_legacy = remove_hooks(
        settings,
        str(app_dir / _FORWARDER_SUBPATH),
    )
    updated = remove_plugin_settings(without_legacy, str(marketplace_dir))
    settings_changed = updated != settings

    # Step 4: Atomic write.
    if settings_changed:
        _atomic_write_json(settings_path, updated)

    remove_result = remove_claude_plugin(app_dir)
    reload_required = settings_changed or remove_result["status"] == "removed"
    logger.info(
        "[ClaudeHUD] plugin disabled status=%s reload_required=%s",
        remove_result["status"],
        reload_required,
    )
    return {
        "ok": True,
        **remove_result,
        "reload_required": reload_required,
        "reload_message": _PLUGIN_RELOAD_MESSAGE if reload_required else None,
    }
