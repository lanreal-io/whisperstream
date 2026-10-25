"""Authenticated local transport state. Content stays in the managed RAM store.

Packets are sequenced per mod instance. Reconnect, replacement and session changes
mint a new opaque handle; a delayed poll cannot collect another instance's reply.
No packet body, session identifier or reply may be logged.
"""

from __future__ import annotations

import re

from .agent_runtime_compatibility import CLAUDE_MOD_MINIMUM, meets_minimum, release_version
from .agent_session_names import bounded_session_key


def accept_claude_packet(store, body: object):
    """Return (session, response, event) after validating one mod packet.

    The caller owns provider/consent/auth guards and rechecks them before output.
    Only connect may create state; all other actions require the exact handle.
    """
    if not isinstance(body, dict):
        return None, {}, None
    instance = body.get("instance")
    sid = body.get("session_id")
    sequence = body.get("sequence")
    revision = body.get("revision")
    surface = body.get("surface")
    version = release_version(body.get("engine_version"))
    action = body.get("action")
    state = body.get("state")
    if (not isinstance(instance, str) or not re.fullmatch(r"[a-f0-9]{32}", instance)
        or not isinstance(sid, str) or bounded_session_key(sid) != sid
        or type(sequence) is not int or not 0 <= sequence <= 2**53 - 1
        or type(revision) is not int or not 0 <= revision <= 2**53 - 1
        or surface not in {"cli", "desktop"} or version is None
        or not meets_minimum(version, CLAUDE_MOD_MINIMUM)
        or action not in {"connect", "poll", "end"}
        or state not in {"idle", "busy"}):
        return None, {}, None
    token = "claude-mod:" + instance
    session = store.get(token)
    if action != "connect":
        if session is None or session.session_handle != body.get("handle"):
            return None, {"reset": True}, None
    elif session is None:
        # One live route per provider session. Reload replaces its exact handle
        # and drops the old instance's offered reply. The user's draft is keyed
        # by conversation in the reply owner, so it survives the reconnect.
        for old in store.sessions():
            if old.agent == "claude" and old.session_id == sid and old.transport == "claude_mod":
                store.deregister(old.token)
        cwd = body.get("cwd")
        session = store.register(token, agent="claude", cwd=cwd if isinstance(cwd, str) and len(cwd) <= 4096 else None)
        session.transport = "claude_mod"
        session.surface = surface
        session.runtime_version = version
    if session.session_id is not None and session.session_id != sid:
        return None, {"reset": True}, None
    if sequence <= session.transport_sequence:
        return None, {}, None
    session.transport_sequence = sequence
    if revision < session.transport_revision:
        return None, {}, None
    if revision != session.transport_revision:
        store.invalidate_reply(session.session_handle)
        session.transport_revision = revision
    if action == "end":
        store.deregister(token)
        return None, {"ok": True}, None
    # A busy packet withdraws any reply already offered for the previous idle
    # turn. Replies accepted while busy are held by the store and offered again
    # at the next Stop. Pickup also tests freshness and state under the lock.
    store.record_event(token, "Stop" if state == "idle" else "UserPromptSubmit", agent="claude", session_id=sid)
    store.refresh_poll(session.session_handle)
    event = body.get("completion")
    if not isinstance(event, dict) or state != "idle":
        event = None
    else:
        text = event.get("text")
        turn_id = event.get("turn_id")
        event = ({"hook_event_name": "Stop", "session_id": sid,
                  "last_assistant_message": text, "prompt_id": turn_id}
                 if isinstance(text, str) and 0 < len(text) <= 200000
                 and isinstance(turn_id, str) and 0 < len(turn_id) <= 256 else None)
    # Process a new completion before offering any reply to its hook. The next
    # heartbeat collects the exact idle generation's reply independently.
    reply = store.take_pending_reply(session.session_handle) if event is None else None
    if reply is not None:
        store.mark_replied(session.session_handle)
    return session, {"ok": True, "handle": session.session_handle, "reply": reply}, event
