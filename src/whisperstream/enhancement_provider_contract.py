"""Native-free enhancement provider and fallback contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class EnhancementProviderKind(str, Enum):
    CLOUD = "cloud"
    OLLAMA = "ollama"


class EnhancementDestination(str, Enum):
    CLOUD_SERVICE = "cloud-service"
    CONFIGURED_ENDPOINT = "configured-endpoint"


class EnhancementFailure(str, Enum):
    NONE = "none"
    UNSUPPORTED = "unsupported"
    BLOCKED_BY_POLICY = "blocked-by-policy"
    CREDENTIAL_UNAVAILABLE = "credential-unavailable"
    ENDPOINT_UNAVAILABLE = "endpoint-unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    HTTP = "http"
    INVALID_RESPONSE = "invalid-response"
    EMPTY_RESPONSE = "empty-response"


@dataclass(frozen=True)
class EnhancementProviderPolicy:
    kind: EnhancementProviderKind
    network_bound: bool
    destination: EnhancementDestination
    requires_protected_secret: bool
    requires_endpoint: bool
    allowed_in_lockdown: bool
    retries: int
    raw_fallback: bool

    def __post_init__(self) -> None:
        if self.retries != 0:
            raise ValueError("daily-use enhancement providers do not retry")
        if not self.raw_fallback:
            raise ValueError("daily-use enhancement must preserve raw fallback")


def provider_policy(kind: EnhancementProviderKind) -> EnhancementProviderPolicy:
    """Return the frozen shared policy without inspecting the host or network."""

    if kind is EnhancementProviderKind.CLOUD:
        return EnhancementProviderPolicy(
            kind=kind,
            network_bound=True,
            destination=EnhancementDestination.CLOUD_SERVICE,
            requires_protected_secret=True,
            requires_endpoint=True,
            allowed_in_lockdown=False,
            retries=0,
            raw_fallback=True,
        )
    return EnhancementProviderPolicy(
        kind=kind,
        network_bound=True,
        destination=EnhancementDestination.CONFIGURED_ENDPOINT,
        requires_protected_secret=False,
        requires_endpoint=True,
        allowed_in_lockdown=False,
        retries=0,
        raw_fallback=True,
    )


@dataclass(frozen=True)
class EnhancementRequest:
    request_id: str
    provider_id: str
    provider_kind: EnhancementProviderKind
    timeout_seconds: float
    transcript: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.provider_id.strip():
            raise ValueError("request_id and provider_id must be nonempty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class EnhancementOutcome:
    request_id: str
    failure: EnhancementFailure
    use_raw: bool
    enhanced_text: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must be nonempty")
        if self.failure is EnhancementFailure.NONE:
            if self.use_raw or not self.enhanced_text:
                raise ValueError("successful enhancement requires nonempty output")
        elif not self.use_raw or self.enhanced_text is not None:
            raise ValueError("provider failure must select raw fallback only")


@runtime_checkable
class EnhancementProviderAdapter(Protocol):
    """Adapter owns endpoint, HTTP client, secret lookup, and request cleanup."""

    @property
    def policy(self) -> EnhancementProviderPolicy: ...

    async def enhance(self, request: EnhancementRequest) -> EnhancementOutcome: ...

    async def cancel(self, request_id: str) -> EnhancementOutcome: ...


ENHANCEMENT_PROVIDER_SCHEMA_VERSION = 1
