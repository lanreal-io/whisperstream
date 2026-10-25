"""Thread-safe local usage counters and their best-effort persistence."""

import json
import threading

from ..atomic_io import atomic_write_json
from ..config_manager import get_app_dir


class UsageStats:
    """Simple usage statistics tracker. Persists to a JSON file."""

    def __init__(self) -> None:
        self._path = get_app_dir() / "stats.json"
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, int]:
        counters = {"total_transcriptions": 0, "total_words": 0, "total_recording_ms": 0}
        try:
            if self._path.exists():
                saved = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    for key in counters:
                        value = saved.get(key)
                        # Counts and elapsed milliseconds are nonnegative integers.
                        # bool is an int subclass, but not a valid saved counter.
                        if type(value) is int and value >= 0:
                            counters[key] = value
        except Exception:
            pass
        return counters

    def _save(self) -> None:
        try:
            atomic_write_json(self._path, self._data)
        except Exception:
            pass

    def record_transcription(self, text: str, duration_ms: int) -> None:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        with self._lock:
            self._data["total_transcriptions"] += 1
            self._data["total_words"] += len(text.split())
            self._data["total_recording_ms"] += duration_ms
            self._save()

    def to_dict(self) -> dict[str, int]:
        with self._lock:
            return dict(self._data)
