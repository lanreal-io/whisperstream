"""Codex hook adapter for the shared Agent Speak runtime.

Only the current official ``Stop``, ``PermissionRequest``, and
``UserPromptSubmit`` contracts are accepted. Provider-specific response JSON
is encoded by ``server.py`` at the HTTP boundary.

# ============================================================================
# TRANSCRIPT DATA - DO NOT LOG
# Hook bodies contain assistant responses, prompts, tool input, paths, and raw
# provider identifiers. Never log bodies, normalized events, returned decisions,
# or any of those values. Fixed state names and exception classes are safe.
# ============================================================================
"""

from __future__ import annotations

from .agent_plugin_runtime import plugin_identity

import json
import logging

from .agent_events import (
    SPOKEN_SUMMARY_CONTEXT,
    SPOKEN_SUMMARY_PREFIX,
    SessionActivationRegistry,
    SessionNameRegistry,
    extract_spoken_summary,
    normalize_permission_event,
)

logger = logging.getLogger(__name__)

_CODEX_STOP_ENVELOPE_KEYS = frozenset(
    {
        "cwd",
        "hook_event_name",
        "last_assistant_message",
        "model",
        "permission_mode",
        "session_id",
        "stop_hook_active",
        "transcript_path",
        "turn_id",
    }
)
_MAX_RESPONSE_ENVELOPE_LAYERS = 2
_MAX_RAW_SKILL_ARGUMENT_CHARS = 200
_DEFAULT_ACTIVATION_REGISTRY = SessionActivationRegistry()
_DEFAULT_NAME_REGISTRY = SessionNameRegistry()

__all__ = [
    "SPOKEN_SUMMARY_CONTEXT",
    "SPOKEN_SUMMARY_PREFIX",
    "clear_codex_session_state",
    "parse_codex_event",
]


def clear_codex_session_state() -> None:
    """Scrub module-owned activation and explicit-name state."""

    _DEFAULT_ACTIVATION_REGISTRY.clear()
    _DEFAULT_NAME_REGISTRY.clear()


def _optional_string(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _response_identity(payload: dict) -> dict:
    return {
        "agent": "codex",
        "session_id": _optional_string(payload, "session_id"),
        "turn_id": _optional_string(payload, "turn_id"),
        "cwd": _optional_string(payload, "cwd"),
    }


def _merge_missing_identity(identity: dict, payload: dict) -> None:
    nested = _response_identity(payload)
    for key in ("session_id", "turn_id", "cwd"):
        if identity[key] is None:
            identity[key] = nested[key]


def _decode_stop_envelope(value: str) -> dict | None:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    if decoded.get("hook_event_name") != "Stop":
        return None
    if not set(decoded).issubset(_CODEX_STOP_ENVELOPE_KEYS):
        return None
    return decoded


def _normalize_response(raw_message: object, payload: dict) -> dict:
    """Build one response event without interpreting arbitrary response JSON.

    # ========================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # ``raw_message`` and nested assistant messages are response content.
    # ========================================================================
    """

    identity = _response_identity(payload)
    if not isinstance(raw_message, str):
        return {
            "kind": "response",
            **identity,
            "text": "",
            "empty": True,
            "spoken_summary": None,
            "park_eligible": True,
        }

    text = raw_message
    for _layer in range(_MAX_RESPONSE_ENVELOPE_LAYERS):
        envelope = _decode_stop_envelope(text)
        if envelope is None:
            break
        _merge_missing_identity(identity, envelope)
        nested = envelope.get("last_assistant_message")
        if not isinstance(nested, str):
            logger.debug("[CodexHUD] Stop envelope status=malformed")
            return {
                "kind": "response",
                **identity,
                "text": "",
                "empty": True,
                "spoken_summary": None,
                "park_eligible": True,
            }
        text = nested

    if not text.strip():
        return {
            "kind": "response",
            **identity,
            "text": "",
            "empty": True,
            "spoken_summary": None,
            "park_eligible": True,
        }
    return {
        "kind": "response",
        **identity,
        "text": text,
        "empty": False,
        "spoken_summary": extract_spoken_summary(text),
        "park_eligible": True,
    }


def _speak_skill_argument(prompt: object) -> tuple[bool, str | None]:
    """Recognize only the exact Codex plugin skill token, then drop prompt."""

    if not isinstance(prompt, str):
        return False, None
    tokens = prompt.split(maxsplit=1)
    if not tokens or tokens[0] != f"${plugin_identity().codex_skill}":
        return False, None
    return True, tokens[1] if len(tokens) == 2 else None


def correlation_fields(payload: dict) -> "tuple[str, str | None] | None":
    """Return root lifecycle identity for an exact managed Codex route."""
    if not isinstance(payload, dict):
        return None
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str) or not event_name:
        return None
    # Codex subagents reuse the root session id but are not the PTY prompt the
    # wrapper owns. Their lifecycle must never open or block the root route.
    for key in ("agent_id", "agent_type"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return None
    return event_name, _optional_string(payload, "session_id")


def parse_codex_event(
    payload: dict,
    *,
    activation_registry: SessionActivationRegistry | None = None,
    name_registry: SessionNameRegistry | None = None,
    speech_activation: str = "per_session",
) -> dict | None:
    """Normalize one current official Codex hook event.

    Prompt text and raw tool input are inspected only at this edge and are
    never copied into the returned shared event.
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
    names = _DEFAULT_NAME_REGISTRY if name_registry is None else name_registry
    session_id = _optional_string(payload, "session_id")

    if event_name == "Stop":
        result = _normalize_response(payload.get("last_assistant_message"), payload)
        name = names.get(result.get("session_id"))
        if name is not None:
            result["source_name"] = name
            result["source_name_is_custom"] = True
        logger.debug(
            "[CodexHUD] Stop normalized empty=%s named=%s",
            result["empty"],
            name is not None,
        )
        return result

    if event_name == "PermissionRequest":
        result = normalize_permission_event(payload, agent="codex")
        if result is None:
            logger.debug("[CodexHUD] PermissionRequest status=malformed")
            return None
        name = names.get(session_id)
        if name is not None:
            result["source_name"] = name
        logger.debug(
            "[CodexHUD] PermissionRequest status=normalized named=%s",
            name is not None,
        )
        return result

    if event_name == "UserPromptSubmit":
        matched, argument = _speak_skill_argument(payload.get("prompt"))
        if matched and session_id is not None:
            registry.activate(session_id)
            if argument is not None:
                names.set(
                    session_id,
                    argument[:_MAX_RAW_SKILL_ARGUMENT_CHARS],
                )
        result = {
            "kind": "prompt_submitted",
            "agent": "codex",
            "session_id": session_id,
        }
        if (
            speech_activation == "always_on"
            or (
                speech_activation == "per_session"
                and registry.is_active(session_id)
            )
        ):
            result["additional_context"] = SPOKEN_SUMMARY_CONTEXT
        logger.debug(
            "[CodexHUD] UserPromptSubmit normalized activated=%s",
            matched,
        )
        return result

    logger.debug("[CodexHUD] hook ignored reason=unknown_event")
    return None
