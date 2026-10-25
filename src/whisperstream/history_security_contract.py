"""Native-free history, protected-state, licensing, and file-workflow contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class SecurityAvailability(str, Enum):
    EMPTY = "empty"
    READY = "ready"
    LOCKED = "locked"
    NOT_DECRYPTABLE = "not-decryptable"
    KEY_ERROR = "key-error"
    # The protected key is gone for good; recovery or Start fresh is required.
    KEY_MISSING = "key-missing"
    UNAVAILABLE = "unavailable"


class LockdownModeState(str, Enum):
    DISABLED = "disabled"
    UNLOCKED = "unlocked"
    LOCKED = "locked"
    RECOVERY_REQUIRED = "recovery-required"
    UNAVAILABLE = "unavailable"


class HistoryFailure(str, Enum):
    NONE = "none"
    UNSUPPORTED = "unsupported"
    BLOCKED_BY_EULA = "blocked-by-eula"
    LOCKED = "locked"
    KEY_UNAVAILABLE = "key-unavailable"
    NOT_FOUND = "not-found"
    INVALID_INPUT = "invalid-input"
    CANCELLED = "cancelled"
    IO = "io"
    CRYPTO = "crypto"
    STORAGE = "storage"


class FileWorkflowKind(str, Enum):
    IMPORT_AUDIO = "import-audio"
    IMPORT_TEXT = "import-text"
    EXPORT_TRANSCRIPT = "export-transcript"
    RETAIN_EULA = "retain-eula"
    EXPORT_LOGS = "export-logs"
    SELECT_APPLICATION = "select-application"
    SELECT_MODEL_STORAGE = "select-model-storage"


class LicenseState(str, Enum):
    PRO = "pro"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class HistoryPolicy:
    cipher: str
    protected_key_required: bool
    zero_retention_means_forever: bool
    ordinary_delete_is_secure_erasure: bool
    max_import_files: int

    def __post_init__(self) -> None:
        if not self.cipher.strip() or self.max_import_files < 1:
            raise ValueError("invalid history policy")
        if not self.protected_key_required:
            raise ValueError("history keys must use protected storage")
        if self.ordinary_delete_is_secure_erasure:
            raise ValueError("ordinary database deletion is not secure erasure")


@dataclass(frozen=True)
class HistoryRecordRef:
    record_id: str

    def __post_init__(self) -> None:
        if not self.record_id.strip():
            raise ValueError("record_id must be nonempty")


@dataclass(frozen=True)
class HistoryResult:
    failure: HistoryFailure
    changed: bool
    record: HistoryRecordRef | None = None

    def __post_init__(self) -> None:
        if self.failure is not HistoryFailure.NONE and self.changed:
            raise ValueError("failed history operation cannot claim a change")


@dataclass(frozen=True)
class FileSelection:
    selection_token: str = field(repr=False)
    kind: FileWorkflowKind
    display_name: str

    def __post_init__(self) -> None:
        if not self.selection_token.strip() or not self.display_name.strip():
            raise ValueError("file selection fields must be nonempty")


@dataclass(frozen=True)
class FileWorkflowResult:
    failure: HistoryFailure
    selection: FileSelection | None = None
    payload: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.failure is HistoryFailure.NONE and self.selection is None:
            raise ValueError("successful file workflow requires a selection")
        if self.failure is not HistoryFailure.NONE and (
            self.selection is not None or self.payload is not None
        ):
            raise ValueError("failed file workflow cannot expose selection or payload")


@dataclass(frozen=True)
class LicenseSnapshot:
    state: LicenseState
    cache_integrity_protected: bool
    cache_confidential: bool
    activation_available: bool
    offline_validation_available: bool = False
    deactivation_available: bool = False


@dataclass(frozen=True)
class SecuritySnapshot:
    protected_storage: SecurityAvailability
    history: SecurityAvailability
    lockdown: LockdownModeState
    network_enhancement_allowed: bool
    diagnostic_export_allowed: bool

    def __post_init__(self) -> None:
        if self.lockdown in {
            LockdownModeState.LOCKED,
            LockdownModeState.RECOVERY_REQUIRED,
        } and (self.network_enhancement_allowed or self.diagnostic_export_allowed):
            raise ValueError("locked state cannot allow network enhancement or diagnostics")


@runtime_checkable
class HistoryStoreAdapter(Protocol):
    @property
    def policy(self) -> HistoryPolicy: ...

    def availability(self) -> SecurityAvailability: ...

    def security_snapshot(self) -> SecuritySnapshot: ...

    def delete(self, record: HistoryRecordRef) -> HistoryResult: ...

    def clear(self) -> HistoryResult: ...

    def enforce_retention(self, retention_days: int) -> HistoryResult: ...


@runtime_checkable
class FileWorkflowAdapter(Protocol):
    """Adapter owns native panels, paths, file descriptors, and grants."""

    async def choose(self, kind: FileWorkflowKind) -> FileWorkflowResult: ...

    async def read(self, selection: FileSelection) -> FileWorkflowResult: ...

    async def write(self, selection: FileSelection, payload: bytes) -> FileWorkflowResult: ...


@runtime_checkable
class LicenseStateAdapter(Protocol):
    """Adapter owns registry/file persistence and remote activation details."""

    def snapshot(self) -> LicenseSnapshot: ...


@runtime_checkable
class HistorySecurityFactory(Protocol):
    """Select platform persistence owners while keeping native state private."""

    def history(self) -> HistoryStoreAdapter: ...

    def files(self) -> FileWorkflowAdapter: ...

    def licensing(self) -> LicenseStateAdapter: ...


WINDOWS_HISTORY_POLICY = HistoryPolicy(
    cipher="AES-256-GCM",
    protected_key_required=True,
    zero_retention_means_forever=True,
    ordinary_delete_is_secure_erasure=False,
    max_import_files=100,
)

HISTORY_SECURITY_SCHEMA_VERSION = 1
