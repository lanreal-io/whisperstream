"""Platform-neutral permission status, request, and recovery contracts.

Status queries remain completely separate from the user-initiated request and
recovery boundaries.  Native adapters receive a one-shot intent issued by
shared orchestration; they never infer consent from a status read or a device
operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Protocol, runtime_checkable
from uuid import uuid4


PERMISSION_SCHEMA_VERSION = 1
PERMISSION_ACTION_SCHEMA_VERSION = 1


class PermissionKind(StrEnum):
    MICROPHONE = "microphone"
    ACCESSIBILITY = "accessibility"
    INPUT_MONITORING = "input-monitoring"


# The current Mac shortcut implementation uses an active/default event tap.
# Input Monitoring belongs to passive/listen-only taps, not that operation.
_MACOS_OPERATION_PERMISSIONS = {
    "microphone-capture": (PermissionKind.MICROPHONE,),
    "global-shortcuts": (PermissionKind.ACCESSIBILITY,),
    "text-injection": (PermissionKind.ACCESSIBILITY,),
    "dictation": (PermissionKind.MICROPHONE, PermissionKind.ACCESSIBILITY),
}


def required_permissions_for_operation(operation: str) -> tuple[PermissionKind, ...]:
    """Return the Mac operation's explicit OS grants; unknown operations fail."""
    return _MACOS_OPERATION_PERMISSIONS[operation]


class PermissionState(StrEnum):
    NOT_DETERMINED = "not-determined"
    GRANTED = "granted"
    DENIED = "denied"
    RESTRICTED = "restricted"
    NOT_REQUIRED = "not-required"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class PermissionAction(StrEnum):
    REQUEST = "request"
    OPEN_SETTINGS = "open-settings"


class PermissionActionStatus(StrEnum):
    COMPLETED = "completed"
    DECLINED = "declined"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class PermissionActionFailure(StrEnum):
    INTENT_REQUIRED = "intent-required"
    INTENT_REUSED = "intent-reused"
    NOT_REQUESTABLE = "not-requestable"
    RECOVERY_UNAVAILABLE = "recovery-unavailable"
    NATIVE_ERROR = "native-error"


class PermissionIntentUse(StrEnum):
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"
    REUSED = "reused"


@dataclass(frozen=True, slots=True)
class PermissionIntent:
    """Opaque proof of one explicit user action.

    The token is process-local coordination data. It is deliberately omitted
    from serialized outcomes and diagnostics.
    """

    intent_id: str
    kind: PermissionKind
    action: PermissionAction

    def __post_init__(self) -> None:
        if not isinstance(self.intent_id, str) or not self.intent_id.strip():
            raise ValueError("intent_id must be non-empty")
        if not isinstance(self.kind, PermissionKind):
            raise ValueError("invalid permission kind")
        if not isinstance(self.action, PermissionAction):
            raise ValueError("invalid permission action")


class PermissionIntentRegistry:
    """Issue and consume one-shot intents without invoking a native API."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._issued: dict[str, PermissionIntent] = {}
        self._consumed: set[str] = set()

    def issue(
        self,
        kind: PermissionKind,
        action: PermissionAction,
    ) -> PermissionIntent:
        intent = PermissionIntent(
            intent_id=uuid4().hex,
            kind=kind,
            action=action,
        )
        with self._lock:
            self._issued[intent.intent_id] = intent
        return intent

    def consume(self, intent: PermissionIntent) -> PermissionIntentUse:
        if not isinstance(intent, PermissionIntent):
            raise ValueError("intent must be a PermissionIntent")
        with self._lock:
            expected = self._issued.get(intent.intent_id)
            if expected != intent:
                return PermissionIntentUse.UNKNOWN
            if intent.intent_id in self._consumed:
                return PermissionIntentUse.REUSED
            self._consumed.add(intent.intent_id)
            return PermissionIntentUse.ACCEPTED


@dataclass(frozen=True, slots=True)
class PermissionActionOutcome:
    """Content-free result of one request or recovery action."""

    kind: PermissionKind
    action: PermissionAction
    status: PermissionActionStatus
    before: PermissionState
    after: PermissionState
    failure: PermissionActionFailure | None = None
    relaunch_required: bool = False
    schema_version: int = PERMISSION_ACTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PermissionKind):
            raise ValueError("invalid permission kind")
        if not isinstance(self.action, PermissionAction):
            raise ValueError("invalid permission action")
        if not isinstance(self.status, PermissionActionStatus):
            raise ValueError("invalid permission action status")
        if not isinstance(self.before, PermissionState) or not isinstance(
            self.after, PermissionState
        ):
            raise ValueError("invalid permission state transition")
        if self.failure is not None and not isinstance(
            self.failure, PermissionActionFailure
        ):
            raise ValueError("invalid permission action failure")
        if type(self.relaunch_required) is not bool:  # noqa: E721
            raise ValueError("relaunch_required must be boolean")
        if self.schema_version != PERMISSION_ACTION_SCHEMA_VERSION:
            raise ValueError("unsupported permission action schema version")
        failed = self.status in {
            PermissionActionStatus.FAILED,
            PermissionActionStatus.UNSUPPORTED,
        }
        if failed != (self.failure is not None):
            raise ValueError("failure must match failed or unsupported status")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "action": self.action.value,
            "status": self.status.value,
            "before": self.before.value,
            "after": self.after.value,
            "failure": self.failure.value if self.failure else None,
            "relaunch_required": self.relaunch_required,
        }


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    kind: PermissionKind
    state: PermissionState
    query_supported: bool
    request_supported: bool

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PermissionKind):
            raise ValueError("invalid permission kind")
        if not isinstance(self.state, PermissionState):
            raise ValueError("invalid permission state")
        if type(self.query_supported) is not bool:  # noqa: E721 - reject integers
            raise ValueError("query_supported must be boolean")
        if type(self.request_supported) is not bool:  # noqa: E721 - reject integers
            raise ValueError("request_supported must be boolean")

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "kind": self.kind.value,
            "state": self.state.value,
            "query_supported": self.query_supported,
            "request_supported": self.request_supported,
        }


@dataclass(frozen=True, slots=True)
class PermissionReport:
    platform: str
    permissions: tuple[PermissionSnapshot, ...]
    schema_version: int = PERMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.platform:
            raise ValueError("platform must be non-empty")
        if not all(
            isinstance(item, PermissionSnapshot) for item in self.permissions
        ):
            raise ValueError("permissions must contain snapshots")
        kinds = tuple(item.kind for item in self.permissions)
        if len(kinds) != len(set(kinds)):
            raise ValueError("permission kinds must be unique")
        if set(kinds) != set(PermissionKind):
            raise ValueError("permission report must contain every permission kind")
        if self.schema_version != PERMISSION_SCHEMA_VERSION:
            raise ValueError("unsupported permission schema version")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "permissions": [item.as_dict() for item in self.permissions],
            **({"required_for_operations": {
                operation: [kind.value for kind in kinds]
                for operation, kinds in _MACOS_OPERATION_PERMISSIONS.items()
            }} if self.platform == "macos" else {}),
        }


@runtime_checkable
class PermissionStatusProvider(Protocol):
    """Query current state only; implementations must not request access."""

    def query_permissions(self) -> PermissionReport:
        """Return a complete content-free snapshot without prompting."""


@runtime_checkable
class PermissionRequester(Protocol):
    """Explicit request boundary, kept separate from status queries."""

    def request_permission(self, intent: PermissionIntent) -> PermissionActionOutcome:
        """Consume one request intent and return the truthful transition."""


@runtime_checkable
class PermissionRecoveryProvider(Protocol):
    """Explicit recovery boundary, kept separate from status and requests."""

    def open_permission_settings(
        self,
        intent: PermissionIntent,
    ) -> PermissionActionOutcome:
        """Consume one recovery intent and report whether navigation occurred."""


__all__ = [
    "PERMISSION_ACTION_SCHEMA_VERSION",
    "PERMISSION_SCHEMA_VERSION",
    "PermissionAction",
    "PermissionActionFailure",
    "PermissionActionOutcome",
    "PermissionActionStatus",
    "PermissionIntent",
    "PermissionIntentRegistry",
    "PermissionIntentUse",
    "PermissionKind",
    "PermissionRecoveryProvider",
    "PermissionReport",
    "PermissionRequester",
    "PermissionSnapshot",
    "PermissionState",
    "PermissionStatusProvider",
    "required_permissions_for_operation",
]
