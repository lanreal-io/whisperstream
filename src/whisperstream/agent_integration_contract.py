"""Agent plugin, hook transport, interaction, and HUD lifecycle contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


AGENT_INTEGRATION_SCHEMA_VERSION = 1


class AgentProvider(StrEnum):
    CLAUDE_CODE = "claude-code"


class AgentHookEvent(StrEnum):
    NOTIFICATION = "notification"
    PERMISSION_REQUEST = "permission-request"
    PRE_TOOL_USE = "pre-tool-use"
    STOP = "stop"
    SUBAGENT_STOP = "subagent-stop"
    USER_PROMPT_SUBMIT = "user-prompt-submit"


class AgentLifecycleAction(StrEnum):
    RECONCILE = "reconcile"
    REMOVE = "remove"
    # Pause without uninstalling (the Agents master switch).
    DISABLE = "disable"
    FORWARD = "forward"
    SHUTDOWN = "shutdown"


class AgentLifecycleState(StrEnum):
    DISABLED = "disabled"
    INSTALLING = "installing"
    CURRENT = "current"
    UPDATED = "updated"
    REMOVED = "removed"
    FAILED = "failed"


class AgentInteractionKind(StrEnum):
    ACTIVITY = "activity"
    RESPONSE = "response"
    QUESTION = "question"
    PERMISSION = "permission"
    COMPOSE = "compose"


class AgentHookDisposition(StrEnum):
    FORWARDED = "forwarded"
    RESOLVED = "resolved"
    FAILED_OPEN = "failed-open"
    IGNORED = "ignored"
    EXPIRED = "expired"


class AgentFailure(StrEnum):
    CLI_UNAVAILABLE = "cli-unavailable"
    PLUGIN_UNAVAILABLE = "plugin-unavailable"
    VERSION_MISMATCH = "version-mismatch"
    INVALID_EVENT = "invalid-event"
    ROUTE_UNAVAILABLE = "route-unavailable"
    TIMEOUT = "timeout"
    STALE_GENERATION = "stale-generation"
    CLEANUP_FAILED = "cleanup-failed"
    NATIVE_ERROR = "native-error"


@dataclass(frozen=True, slots=True)
class AgentSessionRef:
    provider: AgentProvider
    session_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.session_token.strip():
            raise ValueError("session_token must be non-empty")


@dataclass(frozen=True, slots=True)
class AgentLifecycleRequest:
    request_id: str
    generation: int
    provider: AgentProvider
    action: AgentLifecycleAction
    # RECONCILE only: reinstall even an identical version (Connect recovery).
    force: bool = False
    # RECONCILE only: background refresh of an existing install. Never
    # installs, adds a marketplace, or re-enables a paused plugin.
    keep_disabled: bool = False

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")


@dataclass(frozen=True, slots=True)
class AgentLifecycleResult:
    request_id: str
    generation: int
    state: AgentLifecycleState
    failure: AgentFailure | None = None
    schema_version: int = AGENT_INTEGRATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if self.schema_version != AGENT_INTEGRATION_SCHEMA_VERSION:
            raise ValueError("unsupported agent schema version")
        if (self.state is AgentLifecycleState.FAILED) != (self.failure is not None):
            raise ValueError("failure must match failed lifecycle state")


@dataclass(frozen=True, slots=True)
class AgentHookRequest:
    request_id: str
    generation: int
    session: AgentSessionRef
    event: AgentHookEvent
    deadline_seconds: float
    body_json: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline must be positive")
        if not isinstance(self.body_json, str):
            raise ValueError("body_json must be a string")


@dataclass(frozen=True, slots=True)
class AgentHookResult:
    request_id: str
    generation: int
    disposition: AgentHookDisposition
    interaction: AgentInteractionKind | None = None
    response_json: str = field(default="{}", repr=False)
    failure: AgentFailure | None = None
    schema_version: int = AGENT_INTEGRATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if self.schema_version != AGENT_INTEGRATION_SCHEMA_VERSION:
            raise ValueError("unsupported agent schema version")
        if not isinstance(self.response_json, str):
            raise ValueError("response_json must be a string")
        failed_open = self.disposition is AgentHookDisposition.FAILED_OPEN
        if failed_open != (self.failure is not None):
            raise ValueError("failure is reported only for fail-open results")
        if failed_open and self.response_json != "{}":
            raise ValueError("fail-open hook result must return an empty decision")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "generation": self.generation,
            "disposition": self.disposition.value,
            "interaction": self.interaction.value if self.interaction else None,
            "failure": self.failure.value if self.failure else None,
        }


@runtime_checkable
class AgentPluginAdapter(Protocol):
    """Use the provider-supported CLI while keeping install paths private."""

    def apply(self, request: AgentLifecycleRequest) -> AgentLifecycleResult:
        """Reconcile or remove the exact packaged plugin version."""

    def close(self) -> None:
        """Idempotently stop owned CLI workers and lifecycle tasks."""


@runtime_checkable
class AgentHookAdapter(Protocol):
    """Forward bounded hook events without exposing body or route identity."""

    def forward(self, request: AgentHookRequest) -> AgentHookResult:
        """Return a typed result; transport failures fail open with empty JSON."""

    def cancel(self, request_id: str, generation: int) -> AgentHookResult:
        """Expire only the matching parked interaction."""

    def cancel_all(self) -> None:
        """Scrub every owned route without retaining session/body content."""

    def close(self) -> None:
        """Idempotently release routes, timers, HTTP work, and HUD leases."""


__all__ = [
    "AGENT_INTEGRATION_SCHEMA_VERSION",
    "AgentFailure",
    "AgentHookAdapter",
    "AgentHookDisposition",
    "AgentHookEvent",
    "AgentHookRequest",
    "AgentHookResult",
    "AgentInteractionKind",
    "AgentLifecycleAction",
    "AgentLifecycleRequest",
    "AgentLifecycleResult",
    "AgentLifecycleState",
    "AgentPluginAdapter",
    "AgentProvider",
    "AgentSessionRef",
]
