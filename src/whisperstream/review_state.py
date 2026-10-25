"""ReviewState — thread-safe persistence for in-app review prompt state.

Modeled on the UsageStats pattern in server.py. Persists to review_state.json
alongside stats.json in the app data directory.
"""

import json
import threading
from datetime import date
from typing import Any, Dict

from whisperstream.atomic_io import atomic_write_json
from whisperstream.config_manager import get_app_dir

# ── defaults ────────────────────────────────────────────────────────────────

_DEFAULTS: Dict[str, Any] = {
    "session_count": 0,
    "distinct_days": [],
    "prompt_count": 0,
    "last_prompt_date": "",
    "last_dismiss_date": "",
    "permanently_dismissed": False,
    "last_submitted_date": "",
}


# Closing the prompt without a GitHub click counts as one ask. It may return
# after ASK_INTERVAL_DAYS and stops after MAX_ASKS.
MAX_ASKS = 3
ASK_INTERVAL_DAYS = 30


class ReviewState:
    """Tracks review-prompt eligibility state. Thread-safe, file-backed."""

    def __init__(self) -> None:
        self._path = get_app_dir() / "review_state.json"
        self._lock = threading.Lock()
        self._data = self._load()

    # ── I/O ─────────────────────────────────────────────────────────────────

    def _load(self) -> Dict[str, Any]:
        try:
            if self._path.exists():
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                # Merge with defaults so new fields are always present.
                # Use json round-trip for a clean deep copy of defaults.
                result = json.loads(json.dumps(_DEFAULTS))
                result.update(loaded)
                return result
        except Exception:
            pass
        return json.loads(json.dumps(_DEFAULTS))

    def _save(self) -> None:
        try:
            atomic_write_json(self._path, self._data)
        except Exception:
            pass

    # ── mutators ─────────────────────────────────────────────────────────────

    def record_session(self) -> None:
        """Increment session_count and record today in distinct_days."""
        today = date.today().isoformat()
        with self._lock:
            self._data["session_count"] += 1
            if today not in self._data["distinct_days"]:
                self._data["distinct_days"].append(today)
            self._save()

    def record_dismissal(self) -> None:
        """Increment prompt_count; permanently dismiss after 2 dismissals."""
        today = date.today().isoformat()
        with self._lock:
            self._data["prompt_count"] += 1
            self._data["last_dismiss_date"] = today
            if self._data["prompt_count"] >= 2:
                self._data["permanently_dismissed"] = True
            self._save()

    def record_permanent_dismissal(self) -> None:
        """Permanently suppress future prompts after an explicit opt-out."""
        today = date.today().isoformat()
        with self._lock:
            self._data["prompt_count"] += 1
            self._data["last_dismiss_date"] = today
            self._data["permanently_dismissed"] = True
            self._save()

    def record_prompt_shown(self) -> None:
        """Count one ask and start the interval before the next one."""
        today = date.today().isoformat()
        with self._lock:
            self._data["prompt_count"] += 1
            self._data["last_prompt_date"] = today
            self._save()

    def record_later(self) -> None:
        """Snooze: restart the ask interval without ending future asks."""
        today = date.today().isoformat()
        with self._lock:
            self._data["last_dismiss_date"] = today
            self._save()

    def record_submission(self) -> None:
        """Mark that the prompt handed off to GitHub; permanently suppress future prompts."""
        today = date.today().isoformat()
        with self._lock:
            self._data["last_submitted_date"] = today
            self._data["permanently_dismissed"] = True
            self._save()

    # ── query ─────────────────────────────────────────────────────────────────

    def should_show_prompt(self) -> bool:
        """Evaluate all trigger conditions and return True if the prompt should appear.

        Conditions checked here (recency, lull detection and Lockdown are
        handled upstream by the idle timer in main.py):
        1. session_count >= 20
        2. len(distinct_days) >= 5
        3. not permanently_dismissed
        4. last_submitted_date is empty (never opened GitHub from the prompt)
        5. Fewer than MAX_ASKS asks so far
        6. At least ASK_INTERVAL_DAYS since the last ask or snooze

        Entitlement is unrestricted, so no license state is consulted.
        """
        with self._lock:
            # 1. Enough sessions
            if self._data["session_count"] < 20:
                return False

            # 2. Enough distinct usage days
            if len(self._data["distinct_days"]) < 5:
                return False

            # 3. Not permanently dismissed
            if self._data["permanently_dismissed"]:
                return False

            # 4. Never opened GitHub from the prompt
            if self._data["last_submitted_date"]:
                return False

            # 5. Ask limit
            if self._data["prompt_count"] >= MAX_ASKS:
                return False

            # 6. Interval since the most recent ask or snooze
            for key in ("last_prompt_date", "last_dismiss_date"):
                try:
                    last = date.fromisoformat(self._data[key])
                except (TypeError, ValueError):
                    continue
                if (date.today() - last).days < ASK_INTERVAL_DAYS:
                    return False

            return True

    # ── read ──────────────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)
