"""Bounded reading history, entirely in RAM. Never log transcript content.

This store grants no reply authority. Opaque session handles only group display
turns; the broker and managed-session delivery guards remain authoritative.
"""

from collections import OrderedDict
import threading


class AgentConversationStore:
    def __init__(self, *, max_sessions=12, max_turns=20, max_chars=200_000):
        self._sessions = OrderedDict()
        self._lock = threading.RLock()
        self._max_sessions = max_sessions
        self._max_turns = max_turns
        self._max_chars = max_chars

    def _session(self, handle):
        session = self._sessions.setdefault(handle, {"session_handle": handle, "turns": [], "activity": "finished"})
        self._sessions.move_to_end(handle)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        return session

    def append(self, handle, turn_id, role, text):
        if not handle or role not in {"user", "assistant"} or not isinstance(text, str) or not text.strip():
            return
        with self._lock:
            session = self._session(handle)
            if any(turn["id"] == turn_id for turn in session["turns"]):
                return
            session["turns"].append({"id": turn_id, "role": role, "text": text[:32_000]})
            session["turns"] = session["turns"][-self._max_turns:]
            session["activity"] = "finished" if role == "assistant" else "working"
            while sum(len(turn["text"]) for item in self._sessions.values() for turn in item["turns"]) > self._max_chars:
                oldest = next(item for item in self._sessions.values() if item["turns"])
                oldest["turns"].pop(0)

    def activity(self, handle, active):
        if handle:
            with self._lock:
                self._session(handle)["activity"] = "working" if active else "finished"

    def snapshot(self):
        with self._lock:
            return [{**item, "turns": [dict(turn) for turn in item["turns"]]} for item in self._sessions.values()]

    def clear(self):
        with self._lock:
            self._sessions.clear()
