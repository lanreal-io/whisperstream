"""Bounded, metadata-only diagnostics for effective Lockdown.

This sink is intentionally separate from both the ordinary ``whisperstream``
logger and the transcript ``access.log``. Its public API accepts only a closed
set of event names, field names, and value domains. It never redacts arbitrary
input: a value outside the reviewed schema is rejected before it reaches
memory, disk, or a support archive.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import threading
from typing import Callable, Mapping
import zipfile

from .atomic_io import atomic_write_text


FILENAME = "lockdown-safe-diagnostics.jsonl"
MAX_EVENTS = 256
MAX_BYTES = 128 * 1024

_VERSION_RE = re.compile(
    r"^(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){1,3}"
    r"(?:[-+][0-9A-Za-z.-]{1,32})?$"
)
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)

_POLICY_STATES = frozenset(
    {"absent", "disabled", "enabled", "malformed", "inaccessible", "unsupported"}
)
_TRANSITION_PHASES = frozenset(
    {"entry_started", "entry_complete", "entry_failed", "exit_complete"}
)
_EXPORT_STATUSES = frozenset({"prepared"})
_OS_FAMILIES = frozenset({"Windows", "Linux", "Darwin", "Other"})
_ERROR_TYPES = frozenset(
    {
        "ArithmeticError",
        "AssertionError",
        "AttributeError",
        "ConnectionError",
        "EOFError",
        "FileNotFoundError",
        "ImportError",
        "IndexError",
        "KeyError",
        "MemoryError",
        "NotImplementedError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "ValueError",
    }
)


class SafeDiagnosticRejected(ValueError):
    """Raised when a caller tries to leave the reviewed metadata schema."""


Validator = Callable[[object], bool]


def _is_bool(value: object) -> bool:
    return type(value) is bool


def _bounded_int(maximum: int) -> Validator:
    return lambda value: type(value) is int and 0 <= value <= maximum


def _enum(values: frozenset[str]) -> Validator:
    return lambda value: type(value) is str and value in values


def _version(value: object) -> bool:
    return type(value) is str and _VERSION_RE.fullmatch(value) is not None


_SCHEMAS: Mapping[str, tuple[Mapping[str, Validator], frozenset[str]]] = {
    "app_started": (
        {
            "app_version": _version,
            "managed": _is_bool,
            "policy_state": _enum(_POLICY_STATES),
            "history_enabled": _is_bool,
        },
        frozenset({"app_version", "managed", "policy_state", "history_enabled"}),
    ),
    "lockdown_transition": (
        {
            "phase": _enum(_TRANSITION_PHASES),
            "managed": _is_bool,
            "history_enabled": _is_bool,
            "error_type": _enum(_ERROR_TYPES),
        },
        frozenset({"phase", "managed", "history_enabled"}),
    ),
    "support_export": (
        {
            "status": _enum(_EXPORT_STATUSES),
            "event_count": _bounded_int(MAX_EVENTS),
        },
        frozenset({"status", "event_count"}),
    ),
}


def _serialize(records: list[dict[str, object]]) -> str:
    if not records:
        return ""
    return "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n"


def _validate_fields(event: str, fields: Mapping[str, object]) -> dict[str, object]:
    schema_entry = _SCHEMAS.get(event)
    if schema_entry is None:
        raise SafeDiagnosticRejected(f"unknown event: {event!r}")
    validators, required = schema_entry
    unknown = fields.keys() - validators.keys()
    if unknown:
        raise SafeDiagnosticRejected(f"unknown field: {sorted(unknown)!r}")
    missing = required - fields.keys()
    if missing:
        raise SafeDiagnosticRejected(f"missing field: {sorted(missing)!r}")
    for field, value in fields.items():
        if not validators[field](value):
            raise SafeDiagnosticRejected(f"invalid value for {event}.{field}")
    return dict(fields)


def _validate_record(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise SafeDiagnosticRejected("record must be an object")
    if set(value) != {"timestamp", "event", "fields"}:
        raise SafeDiagnosticRejected("record has unknown fields")
    timestamp = value["timestamp"]
    event = value["event"]
    fields = value["fields"]
    if type(timestamp) is not str or _TIMESTAMP_RE.fullmatch(timestamp) is None:
        raise SafeDiagnosticRejected("record timestamp is invalid")
    if type(event) is not str or type(fields) is not dict:
        raise SafeDiagnosticRejected("record shape is invalid")
    return {
        "timestamp": timestamp,
        "event": event,
        "fields": _validate_fields(event, fields),
    }


class SafeDiagnosticSink:
    """Thread-safe, disk-backed ring of reviewed Lockdown metadata events."""

    def __init__(self, log_dir: Path) -> None:
        self.path = Path(log_dir) / FILENAME
        self._lock = threading.Lock()
        self._records: deque[dict[str, object]] = deque()
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > MAX_BYTES:
                self.path.unlink(missing_ok=True)
                return
            raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return

        valid: list[dict[str, object]] = []
        rejected = False
        for line in raw_lines:
            try:
                valid.append(_validate_record(json.loads(line)))
            except (json.JSONDecodeError, SafeDiagnosticRejected):
                rejected = True
        self._records.extend(valid[-MAX_EVENTS:])
        self._enforce_bounds()
        if rejected or len(valid) > len(self._records):
            self._persist()

    def _enforce_bounds(self) -> None:
        while len(self._records) > MAX_EVENTS:
            self._records.popleft()
        while self._records and len(
            _serialize(list(self._records)).encode("utf-8")
        ) > MAX_BYTES:
            self._records.popleft()

    def _persist(self) -> bool:
        try:
            if not self._records:
                self.path.unlink(missing_ok=True)
                return True
            atomic_write_text(self.path, _serialize(list(self._records)))
            return True
        except OSError:
            return False

    def emit(self, event: str, **fields: object) -> bool:
        """Validate and retain one event, rejecting all schema drift."""
        if type(event) is not str:
            raise SafeDiagnosticRejected("event name must be a string")
        validated = _validate_fields(event, fields)
        record: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "event": event,
            "fields": validated,
        }
        with self._lock:
            self._records.append(record)
            self._enforce_bounds()
            return self._persist()

    def clear(self) -> None:
        """Clear safe events at the Lockdown-entry boundary."""
        with self._lock:
            self._records.clear()
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                {
                    "timestamp": record["timestamp"],
                    "event": record["event"],
                    "fields": dict(record["fields"]),
                }
                for record in self._records
            ]

    def serialized(self) -> str:
        return _serialize(self.snapshot())

    def build_support_zip(
        self,
        *,
        app_version: str,
        os_family: str,
        runtime_version: str,
        model_loaded: bool,
    ) -> io.BytesIO:
        """Build the local-only Lockdown support archive entirely in memory."""
        if not _version(app_version):
            raise SafeDiagnosticRejected("invalid app version")
        if os_family not in _OS_FAMILIES:
            raise SafeDiagnosticRejected("invalid OS family")
        if not _version(runtime_version):
            raise SafeDiagnosticRejected("invalid runtime version")
        if type(model_loaded) is not bool:
            raise SafeDiagnosticRejected("invalid model-loaded value")

        self.emit(
            "support_export",
            status="prepared",
            event_count=min(len(self.snapshot()) + 1, MAX_EVENTS),
        )
        system_info = {
            "app_version": app_version,
            "os_family": os_family,
            "runtime_version": runtime_version,
            "model_loaded": model_loaded,
            "diagnostic_kind": "lockdown_safe_metadata",
            "automatic_upload": False,
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("safe-diagnostics.jsonl", self.serialized())
            archive.writestr(
                "system-info.json",
                json.dumps(system_info, indent=2, sort_keys=True),
            )
        buf.seek(0)
        return buf
