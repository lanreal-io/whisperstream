"""Opaque application and browser-page identity boundary for Power Mode."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


APP_CONTEXT_SCHEMA_VERSION = 1


class ApplicationInventoryScope(StrEnum):
    RUNNING = "running"
    INSTALLED = "installed"


class ContextRequirement(StrEnum):
    APPLICATION = "application"
    APPLICATION_AND_BROWSER_PAGE = "application-and-browser-page"


class ContextStatus(StrEnum):
    RESOLVED = "resolved"
    APPLICATION_ONLY = "application-only"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ContextFailure(StrEnum):
    NO_FOREGROUND_APPLICATION = "no-foreground-application"
    APPLICATION_EXITED = "application-exited"
    PAGE_IDENTITY_UNAVAILABLE = "page-identity-unavailable"
    ACCESS_DENIED = "access-denied"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"
    NATIVE_ERROR = "native-error"


class ProfileRouteTier(StrEnum):
    DEFAULT = "default"
    APPLICATION = "application"
    SITE = "site"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class ApplicationRef:
    logical_id: str = field(repr=False)
    instance_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.logical_id.strip() or not self.instance_token.strip():
            raise ValueError("application identities must be non-empty")


@dataclass(frozen=True, slots=True)
class ApplicationDescriptor:
    application: ApplicationRef
    display_name: str = field(repr=False)
    icon_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("display_name must be non-empty")
        if self.icon_token is not None and not self.icon_token.strip():
            raise ValueError("icon_token must be non-empty when supplied")


@dataclass(frozen=True, slots=True)
class BrowserPageRef:
    application: ApplicationRef
    normalized_host: str = field(repr=False)

    def __post_init__(self) -> None:
        host = self.normalized_host
        if not host or host != host.lower() or "://" in host or "/" in host:
            raise ValueError("normalized_host must be a lowercase host only")


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    generation: int
    requirement: ContextRequirement
    status: ContextStatus
    application: ApplicationRef | None = None
    browser_page: BrowserPageRef | None = None
    failure: ContextFailure | None = None
    schema_version: int = APP_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if self.schema_version != APP_CONTEXT_SCHEMA_VERSION:
            raise ValueError("unsupported app context schema version")
        successful = self.status in {
            ContextStatus.RESOLVED,
            ContextStatus.APPLICATION_ONLY,
        }
        if successful != (self.failure is None):
            raise ValueError("failure must match unsuccessful context status")
        if successful and self.application is None:
            raise ValueError("resolved context requires an application")
        if self.browser_page is not None:
            if self.application is None or (
                self.browser_page.application != self.application
            ):
                raise ValueError("browser page must belong to captured application")
        if self.status is ContextStatus.RESOLVED and (
            self.requirement is ContextRequirement.APPLICATION_AND_BROWSER_PAGE
            and self.browser_page is None
        ):
            raise ValueError("resolved browser requirement needs page identity")
        if self.status is ContextStatus.APPLICATION_ONLY and (
            self.browser_page is not None
        ):
            raise ValueError("application-only context cannot include a page")


@dataclass(frozen=True, slots=True)
class EnhancementProfileRoute:
    generation: int
    tier: ProfileRouteTier
    profile_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if not self.profile_id.strip():
            raise ValueError("profile_id must be non-empty")


@runtime_checkable
class ApplicationContextAdapter(Protocol):
    """Capture logical identity while retaining every native object privately."""

    def capture(
        self,
        generation: int,
        requirement: ContextRequirement,
    ) -> ContextSnapshot:
        """Capture once at recording start without reading titles or page content."""

    def inventory(
        self,
        scope: ApplicationInventoryScope,
    ) -> tuple[ApplicationDescriptor, ...]:
        """Return display metadata for rules without native handles or paths."""

    def close(self) -> None:
        """Idempotently close owned observers, workers, and caches."""


@runtime_checkable
class EnhancementRouteResolver(Protocol):
    def resolve(self, snapshot: ContextSnapshot) -> EnhancementProfileRoute:
        """Apply manual, site, app, then default precedence to one snapshot."""


__all__ = [
    "APP_CONTEXT_SCHEMA_VERSION",
    "ApplicationContextAdapter",
    "ApplicationDescriptor",
    "ApplicationInventoryScope",
    "ApplicationRef",
    "BrowserPageRef",
    "ContextFailure",
    "ContextRequirement",
    "ContextSnapshot",
    "ContextStatus",
    "EnhancementProfileRoute",
    "EnhancementRouteResolver",
    "ProfileRouteTier",
]
