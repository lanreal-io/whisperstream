"""Identify non-interactive coding-agent sessions at the hook edge.

Agent Speak serves the person at the keyboard. Subagents, ``codex exec`` runs,
``claude -p`` runs and background/ephemeral provider sessions must never reach
the HUD, speech, completions or Stop holds. Callers return the provider's
fail-open response immediately when a classifier returns a reason.

# ============================================================================
# TRANSCRIPT DATA - DO NOT LOG
# Payloads and rollout files contain prompts and replies. Only the returned
# content-free reason may be logged. Transcript paths are never logged.
# ============================================================================

Markers were verified against real provider runs (October 2026):
- Claude: hooks fired inside a subagent carry a non-empty ``agent_id``. Main
  sessions started with ``claude --agent`` also set ``agent_type``, so it is
  not a subagent marker. Unattended ``claude -p`` runs are filtered by the
  forwarder scripts (``CLAUDE_CODE_SESSION_ATTENDED=0``), not here.
- Codex: subagent events carry ``agent_id``/``agent_type``; ephemeral and
  background sessions send no ``transcript_path``; saved rollouts start with
  a ``session_meta`` line whose ``payload.source`` is ``"exec"`` for
  ``codex exec`` or an object with ``"subagent"`` for spawned worker threads.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path

_MAX_FIRST_LINE_BYTES = 64 * 1024
_MAX_CACHE_ENTRIES = 256

_lock = threading.Lock()
_verdicts: "OrderedDict[str, str | None]" = OrderedDict()


def clear_session_filter_cache() -> None:
    """Drop cached rollout verdicts (tests and privacy teardown)."""

    with _lock:
        _verdicts.clear()


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _rollout_source_reason(path: str) -> "str | None":
    """Classify a Codex rollout from its first ``session_meta`` line only."""

    try:
        with Path(path).open("rb") as handle:
            first = handle.readline(_MAX_FIRST_LINE_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(first) > _MAX_FIRST_LINE_BYTES:
        return None
    try:
        record = json.loads(first.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    source = payload.get("source") if isinstance(payload, dict) else None
    if source == "exec":
        return "exec"
    if isinstance(source, dict) and "subagent" in source:
        return "subagent"
    return None


def codex_session_excluded(payload: object) -> "str | None":
    """Return a content-free reason when a Codex hook is not interactive."""

    if not isinstance(payload, dict):
        return None
    if _non_empty_string(payload.get("agent_id")) or _non_empty_string(payload.get("agent_type")):
        return "subagent"
    if "transcript_path" not in payload:
        # Current Codex always sends the key (null when ephemeral). An absent
        # key is an older or synthesized payload; keep normal behavior.
        return None
    transcript = payload.get("transcript_path")
    if not _non_empty_string(transcript):
        return "ephemeral"
    key = str(transcript)
    with _lock:
        if key in _verdicts:
            _verdicts.move_to_end(key)
            return _verdicts[key]
    reason = _rollout_source_reason(key)
    # Only positive verdicts and readable interactive rollouts are stable.
    # An unreadable file may simply not exist yet, so it is re-checked later.
    if reason is not None or Path(key).is_file():
        with _lock:
            _verdicts[key] = reason
            _verdicts.move_to_end(key)
            while len(_verdicts) > _MAX_CACHE_ENTRIES:
                _verdicts.popitem(last=False)
    return reason


def claude_session_excluded(payload: object) -> "str | None":
    """Return ``"subagent"`` for hooks fired inside a Claude subagent."""

    if isinstance(payload, dict) and _non_empty_string(payload.get("agent_id")):
        return "subagent"
    return None


__all__ = [
    "claude_session_excluded",
    "clear_session_filter_cache",
    "codex_session_excluded",
]
