"""EULA policy, exact-identity acceptance authority, and compatibility helpers.

No side effects at import time. Pure helpers are side-effect-free; I/O helpers
(``read_*``, ``write_*``, ``opt_out_is_active``) interact only with the
application data directory returned by ``_eula_dir()``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_EULA_FILENAME = "EULA.txt"
EULA_POLICY_FILENAME = "eula-policy.json"
_BUNDLE_RESOURCES_ENV = "WHISPERSTREAM_BUNDLE_RESOURCES_DIR"


def _find_bundled_resource_path(relative_path: str | Path) -> Path:
    """Locate one bundled legal resource in dev and frozen environments.

    ``relative_path`` is always interpreted beneath the resource directory.
    Absolute paths and parent traversal are rejected before any filesystem
    lookup so policy metadata cannot escape the installed resource boundary.
    """
    relative = Path(relative_path)
    if (
        not str(relative_path)
        or relative.is_absolute()
        or relative.drive
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"Invalid bundled resource path: {relative_path!s}")

    candidates: list[Path] = []

    # The macOS shell passes the exact Contents/Resources root. Nuitka may run
    # the extracted process from a cache, so this project-owned contract must
    # take precedence over executable-derived fallbacks.
    configured_resources = os.environ.get(_BUNDLE_RESOURCES_ENV, "").strip()
    if configured_resources:
        candidates.append(Path(configured_resources).expanduser().resolve() / relative)

    # Nuitka exposes the original onefile launcher on platforms where the
    # compiled containing directory points at an extraction cache.
    onefile_binary = os.environ.get("NUITKA_ONEFILE_BINARY", "").strip()
    if onefile_binary:
        binary_dir = Path(onefile_binary).expanduser().resolve().parent
        candidates.append(binary_dir.parent / "Resources" / relative)
        candidates.append(binary_dir / "resources" / relative)
        candidates.append(binary_dir / relative)

    try:
        launched = Path(sys.argv[0]).expanduser().resolve()
        if launched.is_file() and launched.name.lower() in {
            "whisperstream-engine",
            "whisperstream-engine.exe",
        }:
            binary_dir = launched.parent
            candidates.append(binary_dir.parent / "Resources" / relative)
            candidates.append(binary_dir / "resources" / relative)
            candidates.append(binary_dir / relative)
    except (IndexError, OSError):
        pass

    compiled = globals().get("__compiled__")
    containing_dir = getattr(compiled, "containing_dir", None) if compiled else None
    if containing_dir:
        binary_dir = Path(containing_dir)
        candidates.append(binary_dir.parent / "Resources" / relative)
        candidates.append(binary_dir / "resources" / relative)
        candidates.append(binary_dir / relative)

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        candidates.append(exe_dir.parent / "Resources" / relative)
        candidates.append(exe_dir / "resources" / relative)
        candidates.append(exe_dir / relative)

    try:
        repo_root = Path(__file__).resolve().parents[2]
        candidates.append(repo_root / "src-tauri" / "resources" / relative)
    except IndexError:
        pass

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(f"Cannot locate bundled resource {relative.as_posix()}")


def _find_eula_path() -> Path:
    """Locate ``EULA.txt`` in both dev and frozen-sidecar environments.

    Search order (first match wins):
    1. Nuitka onefile (``__compiled__.containing_dir``) plus ``resources/`` subdir.
       In onefile mode ``sys.executable`` points at the temp extraction dir, NOT
       the original binary, so we must use Nuitka's ``__compiled__`` constant to
       find the dir Tauri installed the resources into.
    2. PyInstaller / Nuitka standalone (``sys.frozen``): sibling of
       ``sys.executable`` plus ``resources/`` subdir.
    3. Dev / installed package: walk up from ``__file__`` to the repo root and
       look inside ``src-tauri/resources/``.

    Returns the first ``Path`` that exists. Raises ``FileNotFoundError`` if
    the file cannot be located.
    """
    try:
        return _find_bundled_resource_path(_EULA_FILENAME)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Cannot locate {_EULA_FILENAME}. "
            "Run 'python scripts/derive-eula-txt.py' to generate it."
        ) from exc


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def read_bundled_eula_text() -> bytes:
    """Read and return the bundled EULA text as raw bytes.

    The SHA-256 is computed over bytes so that CRLF normalisation baked in
    by the derive script is preserved exactly — no line-ending surprises
    from text-mode decoding.

    Raises ``FileNotFoundError`` if ``EULA.txt`` cannot be located.
    """
    return _find_eula_path().read_bytes()


def read_bundled_eula_policy() -> bytes:
    """Read the bundled release policy as raw UTF-8 JSON bytes."""
    return _find_bundled_resource_path(EULA_POLICY_FILENAME).read_bytes()


def read_bundled_eula_terms(terms_path: str) -> bytes:
    """Read exact policy-selected terms beneath the bundled resource root."""
    return _find_bundled_resource_path(terms_path).read_bytes()


class EulaPolicyReason(str, Enum):
    """Content-free outcome of loading the bundled acceptance policy."""

    READY = "ready"
    POLICY_UNAVAILABLE = "policy_unavailable"
    POLICY_INVALID = "policy_invalid"
    TERMS_UNAVAILABLE = "terms_unavailable"
    TERMS_INVALID = "terms_invalid"


@dataclass(frozen=True, slots=True)
class EulaPolicyStatus:
    """Immutable, content-free policy identity plus the exact bundled terms."""

    valid: bool
    reason: EulaPolicyReason
    notice_id: str | None = None
    requirement_kind: str | None = None
    terms_version: str | None = None
    terms_sha256: str | None = None
    terms_path: str | None = None
    terms_text: str | None = None
    terms_bytes: bytes | None = None
    summary: tuple[str, ...] = ()


_POLICY_BASE_KEYS = frozenset(
    {
        "schema_version",
        "notice_id",
        "requirement_kind",
        "terms_version",
        "terms_sha256",
        "terms_path",
    }
)
_POLICY_OPTIONAL_KEYS = frozenset({"summary"})
_REQUIREMENT_KINDS = frozenset({"reacknowledgement", "substantive_update"})


def _invalid_policy_status(reason: EulaPolicyReason) -> EulaPolicyStatus:
    return EulaPolicyStatus(valid=False, reason=reason)


def load_eula_policy_status() -> EulaPolicyStatus:
    """Load and strictly verify the active policy and its exact terms.

    The result is safe to expose to processing callers: failures contain only
    a stable category, never filesystem paths or document content. The policy
    and terms are deliberately reread on every call so source tests and a
    damaged installation fail closed immediately.
    """
    try:
        raw_policy = read_bundled_eula_policy()
    except (FileNotFoundError, OSError, ValueError):
        return _invalid_policy_status(EulaPolicyReason.POLICY_UNAVAILABLE)

    try:
        policy = json.loads(raw_policy.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _invalid_policy_status(EulaPolicyReason.POLICY_INVALID)

    if not isinstance(policy, dict):
        return _invalid_policy_status(EulaPolicyReason.POLICY_INVALID)
    keys = frozenset(policy)
    if not _POLICY_BASE_KEYS <= keys or not keys <= (_POLICY_BASE_KEYS | _POLICY_OPTIONAL_KEYS):
        return _invalid_policy_status(EulaPolicyReason.POLICY_INVALID)
    if policy.get("schema_version") != 1:
        return _invalid_policy_status(EulaPolicyReason.POLICY_INVALID)

    notice_id = policy.get("notice_id")
    requirement_kind = policy.get("requirement_kind")
    terms_version = policy.get("terms_version")
    terms_sha256 = policy.get("terms_sha256")
    terms_path = policy.get("terms_path")
    if (
        not isinstance(notice_id, str)
        or not notice_id.strip()
        or not isinstance(requirement_kind, str)
        or requirement_kind not in _REQUIREMENT_KINDS
        or not isinstance(terms_version, str)
        or _VERSION_RE.fullmatch(terms_version) is None
        or not isinstance(terms_sha256, str)
        or _SHA256_RE.fullmatch(terms_sha256) is None
        or not isinstance(terms_path, str)
        or not terms_path
    ):
        return _invalid_policy_status(EulaPolicyReason.POLICY_INVALID)

    summary_value = policy.get("summary", [])
    if (
        not isinstance(summary_value, list)
        or any(not isinstance(item, str) or not item.strip() for item in summary_value)
        or (requirement_kind == "substantive_update" and not summary_value)
    ):
        return _invalid_policy_status(EulaPolicyReason.POLICY_INVALID)

    try:
        terms = read_bundled_eula_terms(terms_path)
    except (FileNotFoundError, OSError, ValueError):
        return _invalid_policy_status(EulaPolicyReason.TERMS_UNAVAILABLE)

    try:
        parsed_version = parse_eula_version(terms)
        terms_text = terms.decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return _invalid_policy_status(EulaPolicyReason.TERMS_INVALID)
    if parsed_version != terms_version or compute_eula_sha256(terms) != terms_sha256:
        return _invalid_policy_status(EulaPolicyReason.TERMS_INVALID)

    return EulaPolicyStatus(
        valid=True,
        reason=EulaPolicyReason.READY,
        notice_id=notice_id,
        requirement_kind=requirement_kind,
        terms_version=terms_version,
        terms_sha256=terms_sha256,
        terms_path=terms_path,
        terms_text=terms_text,
        terms_bytes=terms,
        summary=tuple(summary_value),
    )


def compute_eula_sha256(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of *data*.

    Args:
        data: Raw bytes to hash (typically the output of
            ``read_bundled_eula_text()``).

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(data).hexdigest()


def parse_eula_version(data: bytes) -> str:
    """Extract the EULA version string from *data*.

    Looks for ``Version X.Y`` followed by ``Effective`` in the
    ASCII-decoded text, where X.Y is one or more dot-separated numeric
    components. Anything between the two tokens is tolerated (whitespace,
    blank lines, markdown bold delimiters, dashes), so the same parser
    handles both the bundled plaintext ``EULA.txt`` and the markdown
    source ``legal/EULA.md``.

    Args:
        data: Raw bytes of the EULA text (ASCII-encoded).

    Returns:
        Version string, e.g. ``"1.0"`` or ``"2.3"``.

    Raises:
        ValueError: If no matching version header is found.
    """
    text = data.decode("ascii", errors="replace")
    match = re.search(r"Version\s+([\d.]+)\b.*?Effective", text, re.DOTALL)
    if not match:
        raise ValueError(
            "EULA text does not contain a recognisable version header. "
            "Expected: 'Version X.Y' followed by 'Effective ...'"
        )
    return match.group(1)


def is_major_version_bump(old: str, new: str) -> bool:
    """Return True iff the major (integer) version component changed.

    A "material amendment" is defined as a bump in the major version
    (the integer portion before the first ``.``). Minor bumps (e.g.
    1.0 -> 1.1) are non-material per scope.md locked decisions.

    Args:
        old: Previous EULA version string, e.g. ``"1.0"``.
        new: New (bundled) EULA version string, e.g. ``"2.0"``.

    Returns:
        True if the major version increased; False otherwise.

    Examples:
        >>> is_major_version_bump("1.0", "1.1")
        False
        >>> is_major_version_bump("1.0", "2.0")
        True
    """
    old_major = int(old.split(".")[0])
    new_major = int(new.split(".")[0])
    return new_major != old_major


# ---------------------------------------------------------------------------
# Re-acknowledgement floor (non-material affirmative re-consent)
# ---------------------------------------------------------------------------

# The lowest EULA version whose acceptance is still treated as current. When a
# user's recorded acceptance is BELOW this floor, the Software asks them to
# affirmatively re-acknowledge the current terms once (EULA Section 20,
# "Re-acknowledgement"). This is DELIBERATELY decoupled from the material-
# amendment flow (``is_major_version_bump`` / ``detect_amendment_state``): the
# doc version mirrors the app version, and re-acknowledgement is armed by
# RAISING this floor, not by which version digit changed. Raise it only when a
# change warrants a fresh affirmative acknowledgement; a non-material
# clarification that does not warrant re-consent leaves the floor untouched.
REACK_FLOOR_VERSION = "1.7"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints for ordering.

    Only the leading numeric component of each dot-separated token is used, so
    a value like ``"1.2.0-dev"`` still orders on its numeric prefix. An empty
    or unparseable string yields ``(0,)`` so comparisons never raise.
    """
    parts: list[int] = []
    for token in version.split("."):
        match = re.match(r"\d+", token.strip())
        if match is None:
            break
        parts.append(int(match.group(0)))
    return tuple(parts) or (0,)


def version_lt(a: str, b: str) -> bool:
    """Return True iff version ``a`` is strictly older than version ``b``.

    Component-wise numeric comparison (so ``1.2`` < ``1.10``), unlike a naive
    string compare. Used to test an accepted version against
    ``REACK_FLOOR_VERSION``.
    """
    return _version_tuple(a) < _version_tuple(b)


def utc_now_iso8601() -> str:
    """Return the current UTC time as an ISO 8601 string with a capital ``Z`` suffix.

    Format: ``YYYY-MM-DDTHH:MM:SSZ`` (no fractional seconds).

    Legal Constraint #5 (research.md): timestamps MUST use capital ``Z``,
    not ``+00:00``, and MUST NOT include fractional seconds.

    Returns:
        e.g. ``"2026-04-18T14:22:17Z"``
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Acceptance / opt-out record filenames and directory
# ---------------------------------------------------------------------------

ACCEPTANCE_FILENAME = "acceptance.json"
OPT_OUT_FILENAME = "update-opt-out.json"


def _eula_dir() -> Path:
    """Return the application data directory used for EULA record files.

    Delegates to ``config_manager.get_app_dir()`` — the single canonical
    ``platformdirs.user_data_dir("Whisperstream")`` location. Do NOT call
    this at module import time; call it inside I/O helpers only.

    Returns:
        Path to ``%LOCALAPPDATA%\\Whisperstream\\`` (or platform equivalent).
    """
    from whisperstream.config_manager import get_app_dir  # local import to avoid circular deps

    return get_app_dir()


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write *data* as JSON to *path* using an atomic write-then-fsync-then-replace.

    Write-before-hide durability (research.md Legal Constraint #1): the data
    must be fsync'd to disk before the caller proceeds, so a crash or power
    loss between write and rename cannot leave a partial file at *path*.

    Steps:
    1. Write JSON to a sibling temp file in the same directory.
    2. Call ``os.fsync`` on the file descriptor to flush OS buffers.
    3. Call ``os.replace`` (atomic rename on POSIX; best-effort on Windows)
       to move the temp file to *path*.

    Args:
        path: Destination file path.
        data: Dictionary to serialise as UTF-8 JSON with 2-space indentation.

    Raises:
        OSError: On any filesystem error (caller surfaces as HTTP 500).
    """
    # Delegates to the shared writer (whisperstream.atomic_io); kept as a thin
    # wrapper so write_acceptance / write_opt_out keep their signature and the
    # indent=2 + trailing-newline record format stays byte-identical.
    from whisperstream.atomic_io import atomic_write_json

    atomic_write_json(path, data, indent=2, trailing_newline=True)


# ---------------------------------------------------------------------------
# acceptance.json helpers
# ---------------------------------------------------------------------------


ACCEPTANCE_SCHEMA_VERSION = 2
_ACCEPTANCE_FIELDS = (
    "eula_version",
    "eula_accepted_at",
    "software_version",
    "eula_sha256",
)
_ACCEPTANCE_FIELD_SET = frozenset(_ACCEPTANCE_FIELDS)
_EVENT_FIELD_SET = _ACCEPTANCE_FIELD_SET | {"event_type"}
_LEDGER_FIELD_SET = _ACCEPTANCE_FIELD_SET | {"schema_version", "records"}
_EVENT_TYPES = frozenset(
    {
        "legacy_migrated",
        "first_run_acceptance",
        "reacknowledgement",
        "updated_terms_acceptance",
    }
)
_acceptance_lock = threading.RLock()


class AcceptanceGrantReason(str, Enum):
    """Stable, content-free acceptance decision reasons."""

    ACCEPTED = "accepted"
    POLICY_INVALID = "policy_invalid"
    ACCEPTANCE_MISSING = "acceptance_missing"
    ACCEPTANCE_INVALID = "acceptance_invalid"
    ACCEPTANCE_UNAVAILABLE = "acceptance_unavailable"
    ACCEPTANCE_MISMATCH = "acceptance_mismatch"


@dataclass(frozen=True, slots=True)
class AcceptanceGrant:
    """Immutable exact-identity decision safe to pass across content stages."""

    allowed: bool
    reason: AcceptanceGrantReason
    target_version: str | None = None
    target_sha256: str | None = None
    evidence_sha256: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True, slots=True)
class _LedgerRead:
    ledger: dict[str, Any] | None
    reason: AcceptanceGrantReason | None = None


class AcceptanceLedgerError(ValueError):
    """Raised when explicit acceptance cannot preserve prior evidence."""


class AcceptancePolicyError(ValueError):
    """Raised when explicit acceptance does not match a valid target."""


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _valid_acceptance_fields(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    return bool(
        isinstance(record.get("eula_version"), str)
        and _VERSION_RE.fullmatch(record["eula_version"])
        and _valid_timestamp(record.get("eula_accepted_at"))
        and isinstance(record.get("software_version"), str)
        and record["software_version"].strip()
        and isinstance(record.get("eula_sha256"), str)
        and _SHA256_RE.fullmatch(record["eula_sha256"])
    )


def _compatibility_projection(record: dict[str, Any]) -> dict[str, str]:
    return {field: record[field] for field in _ACCEPTANCE_FIELDS}


def _normalise_acceptance_payload(payload: object) -> dict[str, Any] | None:
    """Strictly normalize legacy/schema-2 data without writing evidence."""
    if not isinstance(payload, dict):
        return None
    if "schema_version" not in payload:
        if frozenset(payload) != _ACCEPTANCE_FIELD_SET or not _valid_acceptance_fields(payload):
            return None
        event = {**_compatibility_projection(payload), "event_type": "legacy_migrated"}
        return {
            "schema_version": ACCEPTANCE_SCHEMA_VERSION,
            **_compatibility_projection(payload),
            "records": [event],
        }
    if payload.get("schema_version") != ACCEPTANCE_SCHEMA_VERSION:
        return None
    if frozenset(payload) != _LEDGER_FIELD_SET or not _valid_acceptance_fields(payload):
        return None
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        return None
    normalized: list[dict[str, str]] = []
    migrated_count = 0
    for index, raw_record in enumerate(records):
        if (
            not isinstance(raw_record, dict)
            or frozenset(raw_record) != _EVENT_FIELD_SET
            or not _valid_acceptance_fields(raw_record)
            or raw_record.get("event_type") not in _EVENT_TYPES
        ):
            return None
        if raw_record["event_type"] == "legacy_migrated":
            migrated_count += 1
            if index != 0 or migrated_count > 1:
                return None
        normalized.append(
            {**_compatibility_projection(raw_record), "event_type": raw_record["event_type"]}
        )
    if _compatibility_projection(payload) != _compatibility_projection(normalized[-1]):
        return None
    return {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        **_compatibility_projection(normalized[-1]),
        "records": normalized,
    }


def _read_acceptance_ledger_unlocked() -> _LedgerRead:
    try:
        payload = json.loads((_eula_dir() / ACCEPTANCE_FILENAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _LedgerRead(None, AcceptanceGrantReason.ACCEPTANCE_MISSING)
    except json.JSONDecodeError:
        return _LedgerRead(None, AcceptanceGrantReason.ACCEPTANCE_INVALID)
    except OSError:
        return _LedgerRead(None, AcceptanceGrantReason.ACCEPTANCE_UNAVAILABLE)
    ledger = _normalise_acceptance_payload(payload)
    if ledger is None:
        return _LedgerRead(None, AcceptanceGrantReason.ACCEPTANCE_INVALID)
    return _LedgerRead(ledger)


def read_acceptance_ledger() -> dict[str, Any] | None:
    """Return a strict normalized schema-2 ledger without changing the file."""
    with _acceptance_lock:
        result = _read_acceptance_ledger_unlocked()
        return json.loads(json.dumps(result.ledger)) if result.ledger is not None else None


def read_acceptance() -> dict[str, str] | None:
    """Return the latest strict acceptance in the legacy four-field shape."""
    with _acceptance_lock:
        result = _read_acceptance_ledger_unlocked()
        return _compatibility_projection(result.ledger) if result.ledger is not None else None


def _ledger_sha256(ledger: dict[str, Any]) -> str:
    payload = json.dumps(ledger, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _grant_from_ledger(policy: EulaPolicyStatus, result: _LedgerRead) -> AcceptanceGrant:
    """Derive the startup decision from already-read, validated evidence."""
    if not policy.valid:
        return AcceptanceGrant(False, AcceptanceGrantReason.POLICY_INVALID)
    if result.ledger is None:
        return AcceptanceGrant(
            False,
            result.reason or AcceptanceGrantReason.ACCEPTANCE_INVALID,
            policy.terms_version,
            policy.terms_sha256,
        )
    allowed = bool(
        result.ledger["eula_version"] == policy.terms_version
        and result.ledger["eula_sha256"] == policy.terms_sha256
    )
    return AcceptanceGrant(
        allowed,
        AcceptanceGrantReason.ACCEPTED if allowed else AcceptanceGrantReason.ACCEPTANCE_MISMATCH,
        policy.terms_version,
        policy.terms_sha256,
        _ledger_sha256(result.ledger),
    )


class AcceptanceSession:
    """One backend startup gate. Admission is immutable until process restart.

    A blocked startup may accept its displayed policy once, after durable evidence
    verification. Once admitted, neither external file edits nor repeated API
    requests can replace its grant. Lockdown and provider consent are separate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._policy = load_eula_policy_status()
        with _acceptance_lock:
            result = _read_acceptance_ledger_unlocked()
        self._acceptance = (
            _compatibility_projection(result.ledger) if result.ledger is not None else None
        )
        self._grant = _grant_from_ledger(self.policy, result)
        self._summary = self.policy.summary

    @property
    def policy(self) -> EulaPolicyStatus:
        return self._policy

    @property
    def grant(self) -> AcceptanceGrant:
        return self._grant

    @property
    def summary(self) -> tuple[str, ...]:
        return self._summary

    @property
    def acceptance(self) -> dict[str, str] | None:
        return dict(self._acceptance) if self._acceptance is not None else None

    def accept(self, version: str, sha256: str, software_version: str) -> dict:
        with self._lock:
            if version != self.policy.terms_version or sha256 != self.policy.terms_sha256:
                raise AcceptancePolicyError("Acceptance does not match this startup policy")
            if self.grant.allowed:
                # Retry-safe without rewriting evidence or changing the live grant.
                return self.acceptance
            record = _write_acceptance(version, sha256, software_version, policy=self.policy)
            with _acceptance_lock:
                result = _read_acceptance_ledger_unlocked()
            grant = _grant_from_ledger(self.policy, result)
            if not grant.allowed:
                raise OSError("Acceptance evidence could not be verified after writing")
            self._acceptance = dict(record)
            self._summary = self.policy.summary
            # Publish the allowed grant last. Runtime reads need no disk I/O/lock.
            self._grant = grant
            return dict(record)


_acceptance_session: AcceptanceSession | None = None
_acceptance_session_lock = threading.Lock()


def get_acceptance_session() -> AcceptanceSession:
    """Initialize once, before any backend content services are admitted."""
    global _acceptance_session
    if _acceptance_session is None:
        with _acceptance_session_lock:
            if _acceptance_session is None:
                _acceptance_session = AcceptanceSession()
    return _acceptance_session


def get_acceptance_grant() -> AcceptanceGrant:
    """Return the process startup grant without rereading files after startup."""
    return get_acceptance_session().grant


def acceptance_grant_is_current(grant: AcceptanceGrant) -> bool:
    """Check admission against the immutable process session, with no I/O."""
    return grant.allowed and get_acceptance_grant() == grant


def write_acceptance(
    eula_version: str,
    eula_sha256: str,
    software_version: str,
) -> dict:
    """Accept the startup policy; an admitted session cannot change agreement."""
    return get_acceptance_session().accept(eula_version, eula_sha256, software_version)


def _write_acceptance(
    eula_version: str,
    eula_sha256: str,
    software_version: str,
    *,
    policy: EulaPolicyStatus,
) -> dict:
    """Write an ``acceptance.json`` record for a successful first-run or amendment accept.

    Computes ``eula_accepted_at`` as the current UTC time, writes the four-field
    record atomically (write → fsync → replace), then removes any existing
    ``update-opt-out.json`` (a prior opt-out is superseded by the new acceptance,
    per tasks.md Task 3.1 note).

    Args:
        eula_version: EULA version string parsed from the bundled EULA text.
        eula_sha256: Hex SHA-256 digest of the bundled EULA bytes.
        software_version: Application version string at the time of acceptance.

    Returns:
        The dict that was written to disk.

    Raises:
        OSError: On any I/O error — the caller (REST endpoint) turns this into
            an HTTP 500 so the frontend can show an inline retry error and keep
            the popup open. A failed write must never leave a partial record.
    """
    persisted = False
    with _acceptance_lock:
        if not policy.valid:
            raise AcceptancePolicyError("The bundled EULA policy is invalid")
        if eula_version != policy.terms_version or eula_sha256 != policy.terms_sha256:
            raise AcceptancePolicyError("The requested EULA identity is not the active target")
        if not isinstance(software_version, str) or not software_version.strip():
            raise AcceptancePolicyError("The software version is invalid")

        existing = _read_acceptance_ledger_unlocked()
        if existing.ledger is None and existing.reason is not AcceptanceGrantReason.ACCEPTANCE_MISSING:
            raise AcceptanceLedgerError("Existing acceptance evidence is invalid")
        records = list(existing.ledger["records"]) if existing.ledger is not None else []
        if not records:
            event_type = "first_run_acceptance"
        elif policy.requirement_kind == "substantive_update":
            event_type = "updated_terms_acceptance"
        else:
            event_type = "reacknowledgement"

        record: dict[str, str] = {
            "eula_version": policy.terms_version,
            "eula_accepted_at": utc_now_iso8601(),
            "software_version": software_version,
            "eula_sha256": policy.terms_sha256,
        }
        records.append({**record, "event_type": event_type})
        ledger: dict[str, Any] = {
            "schema_version": ACCEPTANCE_SCHEMA_VERSION,
            **record,
            "records": records,
        }
        eula_dir = _eula_dir()
        try:
            _atomic_write_json(eula_dir / ACCEPTANCE_FILENAME, ledger)
            persisted = True
        except OSError as exc:
            _logger.error("eula write failed reason=%s", type(exc).__name__)
            raise
        finally:
            _logger.info(
                "eula accepted version=%s persisted=%s",
                eula_version,
                str(persisted).lower(),
            )

        reread = _read_acceptance_ledger_unlocked()
        if (
            reread.ledger is None
            or reread.ledger["eula_version"] != policy.terms_version
            or reread.ledger["eula_sha256"] != policy.terms_sha256
        ):
            raise OSError("Acceptance evidence could not be verified after writing")

    # Remove any prior opt-out file — the new acceptance supersedes it.
    with _acceptance_lock:
        opt_out_path = eula_dir / OPT_OUT_FILENAME
        try:
            opt_out_path.unlink(missing_ok=True)
        except OSError:
            pass  # Best-effort; not load-bearing.

    return record


# ---------------------------------------------------------------------------
# update-opt-out.json helpers
# ---------------------------------------------------------------------------


def write_opt_out(
    declined_version: str,
    pinned_version: str,
    software_version: str,
    expires_at: str,
) -> dict:
    """Write one non-authoritative opt-out record under the ledger lock."""
    with _acceptance_lock:
        return _write_opt_out_unlocked(
            declined_version,
            pinned_version,
            software_version,
            expires_at,
        )


def _write_opt_out_unlocked(
    declined_version: str,
    pinned_version: str,
    software_version: str,
    expires_at: str,
) -> dict:
    """Write an ``update-opt-out.json`` record when the user declines a material amendment.

    The record captures the declined EULA version, the pinned (previously
    accepted) version, and the 30-day expiry window, using the same atomic
    write-before-hide pattern as ``write_acceptance``.

    Args:
        declined_version: The new EULA version the user declined.
        pinned_version: The EULA version the user previously accepted and
            will continue to operate under during the opt-out window.
        software_version: Application version string at the time of decline.
        expires_at: ISO 8601 UTC timestamp (``Z`` suffix) 30 days after
            ``declined_at``. The caller computes this; see tasks.md Task 1.4
            for the ``declined_at + timedelta(days=30)`` pattern.

    Returns:
        The dict that was written to disk.

    Raises:
        OSError: On any I/O error.
    """
    declined_at = utc_now_iso8601()
    # Re-compute expires_at from declined_at to ensure consistency (the caller
    # may pass a pre-computed value, but the canonical source of truth is
    # declined_at + 30 days).
    dt = datetime.strptime(declined_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    computed_expires_at = (dt + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    record: dict = {
        "declined_version": declined_version,
        "pinned_version": pinned_version,
        "declined_at": declined_at,
        "expires_at": computed_expires_at,
        "software_version": software_version,
    }
    eula_dir = _eula_dir()
    eula_dir.mkdir(parents=True, exist_ok=True)
    persisted = True
    try:
        _atomic_write_json(eula_dir / OPT_OUT_FILENAME, record)
    except OSError as exc:
        persisted = False
        _logger.error("eula write failed reason=%s", type(exc).__name__)
        raise
    finally:
        _logger.info(
            "eula declined version=%s persisted=%s",
            declined_version,
            str(persisted).lower(),
        )
    return record


def read_opt_out() -> dict | None:
    """Read and parse ``update-opt-out.json`` from the EULA data directory.

    Returns the parsed dict if the file exists and contains valid JSON;
    returns ``None`` if the file is missing or malformed.

    Returns:
        Parsed dict with keys ``declined_version``, ``pinned_version``,
        ``declined_at``, ``expires_at``, ``software_version``, or ``None``.
    """
    path = _eula_dir() / OPT_OUT_FILENAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def opt_out_is_active(now_iso: str | None = None) -> bool:
    """Return True iff the update opt-out window is currently active.

    The window is active when:
    - ``update-opt-out.json`` exists and is valid JSON, AND
    - ``now_iso < expires_at`` (ISO 8601 lexicographic comparison is valid
      when both timestamps share the same ``Z``-suffix format and UTC timezone).

    Args:
        now_iso: Override for the current time in ISO 8601 ``Z`` format.
            Defaults to ``utc_now_iso8601()`` when ``None``. Useful for
            testing expiry scenarios.

    Returns:
        ``True`` if the opt-out is active (updater should be suppressed);
        ``False`` if the file is missing, malformed, or the window has expired.
    """
    record = read_opt_out()
    if record is None:
        return False
    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    current = now_iso if now_iso is not None else utc_now_iso8601()
    return current < expires_at


def detect_amendment_state(bundled_version: str, acceptance: dict | None) -> dict:
    """Detect whether the user must accept a material EULA amendment.

    A re-acceptance is required when ALL of the following hold:
    - A prior acceptance record exists (``acceptance is not None``).
    - The bundled EULA has a higher major version than the accepted version.
    - The user has not actively opted out of the re-ask (i.e.
      ``opt_out_is_active()`` is ``False``).

    The 30-day opt-out window re-ask cadence is handled automatically: once
    the ``update-opt-out.json`` expires (``expires_at`` is in the past),
    ``opt_out_is_active()`` returns ``False`` and this function returns
    ``needs_amendment = True`` again, so the popup re-fires.

    Args:
        bundled_version: EULA version string parsed from the bundled
            ``EULA.txt``, e.g. ``"2.0"``.
        acceptance: Parsed ``acceptance.json`` dict (from ``read_acceptance()``)
            or ``None`` if no acceptance record exists.

    Returns:
        A dict with two keys:

        - ``needs_amendment`` (``bool``): ``True`` iff all three conditions
          above hold; ``False`` otherwise.
        - ``pinned_version`` (``str | None``): The previously accepted EULA
          version (``acceptance["eula_version"]``) when ``needs_amendment``
          is ``True``; ``None`` otherwise.
    """
    if acceptance is None:
        return {"needs_amendment": False, "pinned_version": None}

    if not is_major_version_bump(acceptance["eula_version"], bundled_version):
        return {"needs_amendment": False, "pinned_version": None}

    if opt_out_is_active():
        return {"needs_amendment": False, "pinned_version": None}

    # All three conditions met: amendment re-acceptance required.
    pinned = acceptance["eula_version"]
    _logger.info("eula amendment detected from=%s to=%s", pinned, bundled_version)
    return {"needs_amendment": True, "pinned_version": pinned}


def detect_reacknowledgement_state(bundled_version: str, acceptance: dict | None) -> dict:
    """Detect whether the user should affirmatively re-acknowledge the terms.

    This is the NON-material re-consent path (EULA Section 20,
    "Re-acknowledgement"), deliberately separate from the material-amendment
    flow in ``detect_amendment_state``. It returns ``needs_reacknowledgement =
    True`` when ALL of the following hold:

    - A prior acceptance record exists (``acceptance is not None``); the
      first-run flow handles a missing record.
    - The change is NOT a material major-version bump. A major bump is the
      stronger material-amendment flow (30-day notice + opt-out) and takes
      precedence, so this path stands down for it.
    - The accepted EULA version is below ``REACK_FLOOR_VERSION`` (component-wise
      numeric comparison).

    Because acceptance records the bundled version on accept, the accepted
    version rises to at least the floor once the user acknowledges, so this is a
    one-shot prompt per floor bump. There is no opt-out / pinned-version branch:
    the prompt is accept-or-exit, because a non-material change already binds on
    publication.

    Args:
        bundled_version: EULA version parsed from the bundled ``EULA.txt``.
        acceptance: Parsed ``acceptance.json`` dict, or ``None``.

    Returns:
        A dict with one key, ``needs_reacknowledgement`` (``bool``).
    """
    if acceptance is None:
        return {"needs_reacknowledgement": False}

    accepted_version = acceptance.get("eula_version")
    if not accepted_version:
        # Malformed record (no version): do not prompt, to avoid a crash loop.
        return {"needs_reacknowledgement": False}

    # A material major bump is handled by detect_amendment_state; defer to it.
    if is_major_version_bump(accepted_version, bundled_version):
        return {"needs_reacknowledgement": False}

    if version_lt(accepted_version, REACK_FLOOR_VERSION):
        _logger.info(
            "eula reacknowledgement required accepted=%s floor=%s",
            accepted_version,
            REACK_FLOOR_VERSION,
        )
        return {"needs_reacknowledgement": True}

    return {"needs_reacknowledgement": False}
