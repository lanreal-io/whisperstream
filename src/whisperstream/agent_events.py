"""Shared vocabulary for normalized coding-agent events.

Provider adapters map raw hook payloads onto the normalized event shape
consumed by the Agent Speak runtime:

    {"kind": <str>, "session_id": <str | None>, ...kind-specific fields}

Kinds (``EVENT_KINDS``):

- ``response``          — the agent finished a turn; carries ``text`` (transcript
                          data, see banner rules in the adapters) and ``empty``.
- ``needs_input``       — the agent is waiting on a permission/approval prompt.
- ``error``             — the agent hit a terminal error for this turn.
- ``progress``          — a milestone inside a long task (e.g. a subagent
                          finished); carries ``detail`` selecting the template.
- ``prompt_submitted``  — the user submitted a prompt to the agent (typed or
                          injected). Drives speech focus and typed-prompt
                          barge-in. Never spoken; the raw prompt text is NEVER
                          copied into the normalized event.
- ``speech_activation`` — a validated per-session Agent Speech skill
                          invocation. Carries no command or prompt content.
- ``question``          — a structured AskUserQuestion request with exact,
                          bounded question keys and options.
- ``permission``        — a manual permission request with bounded
                          human-facing metadata and no raw tool input.

This module also owns the two speech policies that must be identical across
agents:

- Deterministic spoken templates (:func:`template_for`) — conversational events
  are spoken from this fixed table ONLY. Raw tool payloads, commands, paths,
  code, or any other payload-derived text never reach the TTS engine through
  this path.
- :class:`SpeechDebounce` — minimum interval between utterances of the same
  kind, collapsing rapid repeats (plan.md Phase 2 item 2).
- :class:`SpeechFocus` — most-recent-prompt source wins routine speech
  (plan.md §7 Q4, decision (b)). This is
  only an attention policy. It does not represent a globally active session
  and never controls visual completion delivery.

Gate hygiene: this is an agent-speech experimental module. It is imported only
lazily from gated code paths and is excluded from production builds via
``--nofollow-import-to=whisperstream.agent_events`` in ``scripts/build.py``.
It deliberately has no heavy imports (stdlib only).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional, Tuple

from .agent_session_names import bounded_session_key, sanitize_session_name

__all__ = [
    "AGENTS",
    "EVENT_KINDS",
    "SessionActivationRegistry",
    "SessionNameRegistry",
    "SpeechDebounce",
    "SpeechFocus",
    "SPOKEN_SUMMARY_CONTEXT",
    "SPOKEN_SUMMARY_PREFIX",
    "extract_spoken_summary",
    "normalize_permission_event",
    "template_for",
]

AGENTS = ("claude", "codex")

EVENT_KINDS = (
    "response",
    "working",
    "needs_input",
    "error",
    "progress",
    "prompt_submitted",
    "speech_activation",
    "question",
    "permission",
)

_MAX_ACTIVATED_SESSIONS = 400
_MAX_NAMED_SESSIONS = 400

# Display names used inside spoken templates. Unknown agents fall back to a
# neutral label so a future adapter cannot crash template resolution.
_AGENT_LABEL = {"claude": "Claude", "codex": "Codex"}
_FALLBACK_LABEL = "The agent"

SPOKEN_SUMMARY_PREFIX = "🔊 "
SPOKEN_SUMMARY_CONTEXT = (
    "Always end your final response with one concise, human-readable spoken "
    "summary on its own final line. Begin that line with `🔊 ` (the speaker "
    "emoji followed by one space). This line is required on every response, "
    "including short confirmations and follow-ups. Keep it visible in the "
    "response."
)
_SPOKEN_SUMMARY_MARKERS = (
    SPOKEN_SUMMARY_PREFIX.strip(),
    b"\xf0\x9f\x94\x8a".decode("cp1252"),
    b"\xf0\x9f\x94\x8a".decode("latin-1"),
)
_VARIATION_SELECTOR = "\ufe0f"

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
    "apply_patch": "Edit a file",
}

# Deterministic spoken templates, keyed by (kind, detail). ``detail=None`` is
# the kind's default template. ``{agent}`` is the display label above. These
# strings are fixed product copy (NOT transcript data) — safe to keep verbatim
# here and safe to log. No em dashes (user-visible copy rule).
_TEMPLATES: Dict[Tuple[str, Optional[str]], str] = {
    ("needs_input", None): "{agent} needs your approval to continue.",
    ("error", None): "{agent} hit an error.",
    ("progress", None): "{agent} finished a step.",
    ("progress", "subagent_done"): "A subtask finished.",
}


def template_for(agent: str, kind: str, detail: Optional[str] = None) -> Optional[str]:
    """Return the fixed spoken line for a conversational event, or None.

    ``None`` means "this kind has no spoken template" (e.g. ``response``,
    which speaks the summarizer output instead, and ``prompt_submitted``,
    which is never spoken).
    """
    template = _TEMPLATES.get((kind, detail))
    if template is None and detail is not None:
        template = _TEMPLATES.get((kind, None))
    if template is None:
        return None
    label = _AGENT_LABEL.get(agent, _FALLBACK_LABEL)
    return template.format(agent=label)


def extract_spoken_summary(text: str) -> str | None:
    """Return marker-free text from the final non-empty physical line.

    The complete response remains opaque visual content. Only the fixed marker
    is interpreted, including the two Windows mojibake renderings accepted for
    compatibility with older PowerShell forwarders.

    # ========================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # ``text`` and the returned summary are agent response content.
    # ========================================================================
    """

    final_line = next(
        (line for line in reversed(text.splitlines()) if line.strip()),
        None,
    )
    if final_line is None:
        return None
    candidate = final_line.strip()
    for marker in _SPOKEN_SUMMARY_MARKERS:
        if not candidate.startswith(marker):
            continue
        body = candidate[len(marker):]
        if body.startswith(_VARIATION_SELECTOR):
            body = body[len(_VARIATION_SELECTOR):]
        spoken_summary = body.strip()
        return spoken_summary or None
    return None


def _bounded_display_text(value: object, *, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    if not compact or not all(character.isprintable() for character in compact):
        return None
    return compact[:max_chars]


def normalize_permission_event(payload: dict, *, agent: str) -> dict | None:
    """Keep only bounded human-facing metadata from a permission request.

    # ========================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # Tool descriptions and inputs can contain commands, paths, and user data.
    # Raw tool input is never copied into the normalized event.
    # ========================================================================
    """

    if agent not in AGENTS:
        return None
    raw_tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if (
        not isinstance(raw_tool_name, str)
        or not raw_tool_name.strip()
        or not isinstance(tool_input, dict)
    ):
        return None
    tool_name = _bounded_display_text(raw_tool_name, max_chars=_MAX_TOOL_NAME_CHARS)
    if tool_name is None:
        return None
    description = _bounded_display_text(
        tool_input.get("description"),
        max_chars=_MAX_PERMISSION_DESCRIPTION_CHARS,
    )
    if description is None:
        description = _PERMISSION_FALLBACK_DESCRIPTIONS.get(
            tool_name,
            "Use this tool",
        )
    session_id = payload.get("session_id")
    return {
        "kind": "permission",
        "agent": agent,
        "session_id": (
            session_id if isinstance(session_id, str) and session_id else None
        ),
        "tool_name": tool_name,
        "description": description,
    }


# Minimum seconds between spoken utterances of the same kind. Chosen so a
# permission prompt is never buried (short window, only collapses double-fires)
# while progress milestones from a busy session aggregate instead of chattering.
DEBOUNCE_INTERVALS: Dict[str, float] = {
    "needs_input": 5.0,
    "error": 10.0,
    "progress": 20.0,
}


class SessionActivationRegistry:
    """Bounded, memory-only set of sessions that invoked Agent Speech.

    Raw provider session IDs never leave this process through this class. Very
    large IDs become stable hashes before storage, and the oldest activation is
    evicted when the fixed bound is reached. Restarting Whisperstream creates a
    fresh registry and therefore clears every activation.
    """

    def __init__(self, *, max_sessions: int = _MAX_ACTIVATED_SESSIONS) -> None:
        if (
            not isinstance(max_sessions, int)
            or isinstance(max_sessions, bool)
            or max_sessions < 1
        ):
            raise ValueError("max_sessions must be a positive integer")
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        self._active: dict[str, None] = {}

    def activate(self, session_id: object) -> bool:
        """Activate one session; return ``True`` only for a new activation."""
        key = self._session_key(session_id)
        if key is None:
            return False
        with self._lock:
            if key in self._active:
                return False
            self._active[key] = None
            while len(self._active) > self._max_sessions:
                oldest = next(iter(self._active))
                del self._active[oldest]
            return True

    def is_active(self, session_id: object) -> bool:
        key = self._session_key(session_id)
        if key is None:
            return False
        with self._lock:
            return key in self._active

    def clear(self) -> None:
        with self._lock:
            self._active.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._active)

    @staticmethod
    def _session_key(session_id: object) -> str | None:
        return bounded_session_key(session_id)


class SessionNameRegistry:
    """Bounded, memory-only map of sessions to an explicitly chosen name.

    Mirrors :class:`SessionActivationRegistry` in shape and guarantees: raw
    provider session IDs never leave this process (oversized IDs become
    stable hashes via :func:`bounded_session_key`), the oldest entry is
    evicted once the fixed bound is reached, and a Whisperstream restart
    creates a fresh registry, clearing every name (explicit names are
    memory-only).

    A session name is transcript-class: never log the value returned by
    :meth:`get`, only whether a lookup or set succeeded.
    """

    def __init__(self, *, max_sessions: int = _MAX_NAMED_SESSIONS) -> None:
        if (
            not isinstance(max_sessions, int)
            or isinstance(max_sessions, bool)
            or max_sessions < 1
        ):
            raise ValueError("max_sessions must be a positive integer")
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        self._names: dict[str, str] = {}

    def set(self, session_id: object, name: object) -> bool:
        """Set (or replace) one session's name; return ``True`` iff stored.

        The candidate is sanitized via :func:`sanitize_session_name` first;
        a ``None`` sanitize result clears any existing entry for the session
        and returns ``False`` (an unsanitizable name is treated the same as
        no name at all). Re-setting an existing session replaces its value
        and moves it to newest, so it is not the next eviction candidate.
        """
        key = bounded_session_key(session_id)
        if key is None:
            return False
        sanitized = sanitize_session_name(name)
        if sanitized is None:
            with self._lock:
                self._names.pop(key, None)
            return False
        with self._lock:
            self._names.pop(key, None)
            self._names[key] = sanitized
            while len(self._names) > self._max_sessions:
                oldest = next(iter(self._names))
                del self._names[oldest]
        return True

    def clear_name(self, session_id: object) -> None:
        """Clear one session's explicit name (the bare-command clear)."""
        key = bounded_session_key(session_id)
        if key is None:
            return
        with self._lock:
            self._names.pop(key, None)

    def get(self, session_id: object) -> str | None:
        key = bounded_session_key(session_id)
        if key is None:
            return None
        with self._lock:
            return self._names.get(key)

    def clear(self) -> None:
        with self._lock:
            self._names.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._names)


class SpeechDebounce:
    """Per-kind minimum-interval gate for template speech.

    ``allow(kind)`` returns True and records the utterance time when the kind
    has not fired within its interval; rapid repeats collapse to the first
    one. Kinds without a configured interval are always allowed. Thread-safe
    (events arrive on the server loop, but the policy is cheap to lock).

    The clock is injectable for tests (monotonic seconds).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._last_fire: Dict[str, float] = {}

    def allow(self, kind: str) -> bool:
        interval = DEBOUNCE_INTERVALS.get(kind)
        if interval is None:
            return True
        with self._lock:
            now = self._clock()
            last = self._last_fire.get(kind)
            if last is not None and (now - last) < interval:
                return False
            self._last_fire[kind] = now
            return True


class SpeechFocus:
    """Most-recent-prompt source policy for routine speech.

    ``note_prompt(agent, session_id)`` focuses routine speech on the semantic
    coding-agent session the user most recently prompted. ``should_speak`` then
    answers whether that source may produce routine speech (responses,
    progress, errors).

    - No focused source yet (no prompt seen since backend start) -> speak.
      Single-session users keep exactly the Phase 1 behavior even with an
      old forwarder that never sends prompt events.
    - Event has no session id (older hook payloads, malformed) -> speak.
      Speech focus is a filter, never a silencer of last resort.
    - Otherwise -> speak only when the event's source is focused.

    This class is deliberately narrower than session bookkeeping. It tracks
    one speech preference only; all sources remain independently visible.

    ``needs_input`` deliberately bypasses this policy at the call site:
    permission prompts are always announced (plan.md Phase 2 done-when).

    Known limitation (recorded in decisions.md): headless ``-p`` sessions also
    fire prompt-submitted hooks, so a scripted background session claims
    attribution just like an interactive one. Acceptable while experimental;
    revisit if dogfooding shows noise.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._focused_source: Optional[Tuple[str, str]] = None

    def note_prompt(self, agent: str, session_id: Optional[str]) -> None:
        if (
            not isinstance(agent, str)
            or not agent
            or not isinstance(session_id, str)
            or not session_id
        ):
            return
        with self._lock:
            self._focused_source = (agent, session_id)

    def should_speak(self, agent: str, session_id: Optional[str]) -> bool:
        if (
            not isinstance(agent, str)
            or not agent
            or not isinstance(session_id, str)
            or not session_id
        ):
            return True
        with self._lock:
            if self._focused_source is None:
                return True
            return (agent, session_id) == self._focused_source

    def clear(self) -> None:
        """Release the last raw session association."""

        with self._lock:
            self._focused_source = None
