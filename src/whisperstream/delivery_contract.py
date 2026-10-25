"""Platform-neutral processing-finalization, focus, and delivery contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import Protocol, runtime_checkable


DELIVERY_SCHEMA_VERSION = 1


class DeliveryStyle(StrEnum):
    CLIPBOARD = "clipboard"
    KEYSTROKE = "keystroke"


class DeliveryRoute(StrEnum):
    FOCUSED = "focused"


class DeliveryStatus(StrEnum):
    FOCUSED_ATTEMPTED = "focused-attempted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeliveryFailure(StrEnum):
    TARGET_INVALIDATED = "target-invalidated"
    DEAD_TARGET = "dead-target"
    PROCESS_MISMATCH = "process-mismatch"
    FOREGROUND_FAILED = "foreground-failed"
    STALE_ELEMENT = "stale-element"
    FOCUS_MISMATCH = "focus-mismatch"
    INPUT_FAILED = "input-failed"
    PERMISSION_DENIED = "permission-denied"
    WORKER_UNAVAILABLE = "worker-unavailable"
    WORKER_TIMEOUT = "worker-timeout"
    STALE_GENERATION = "stale-generation"
    UNSUPPORTED = "unsupported"
    CLEANUP_FAILED = "cleanup-failed"
    NATIVE_ERROR = "native-error"


class DeliveryWarning(StrEnum):
    CLIPBOARD_SAVE_FAILED = "clipboard-save-failed"
    CLIPBOARD_SNAPSHOT_FALLBACK = "clipboard-snapshot-fallback"
    CLIPBOARD_COPY_FAILED = "clipboard-copy-failed"
    CLIPBOARD_RESTORE_FAILED = "clipboard-restore-failed"
    MODIFIER_RESTORE_FAILED = "modifier-restore-failed"


class DeliveryClaimStatus(StrEnum):
    CLAIMED = "claimed"
    REUSED = "reused"


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """Opaque, generation-current target returned by a platform adapter."""

    route: DeliveryRoute
    opaque_id: str = field(repr=False)
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.route, DeliveryRoute):
            raise ValueError("invalid delivery route")
        if not isinstance(self.opaque_id, str) or not self.opaque_id.strip():
            raise ValueError("opaque_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")


@dataclass(frozen=True, slots=True)
class DeliveryTargetOutcome:
    target: DeliveryTarget | None = None
    failure: DeliveryFailure | None = None

    def __post_init__(self) -> None:
        if (self.target is None) == (self.failure is None):
            raise ValueError("target outcome requires exactly one target or failure")
        if self.target is not None and not isinstance(self.target, DeliveryTarget):
            raise ValueError("target must be a DeliveryTarget")
        if self.failure is not None and not isinstance(self.failure, DeliveryFailure):
            raise ValueError("failure must be a DeliveryFailure")


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    """One already-processed payload, intentionally hidden from repr."""

    request_id: str
    generation: int
    target: DeliveryTarget
    style: DeliveryStyle
    processed_text: str = field(repr=False)
    restore_clipboard: bool = True
    submit: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if not isinstance(self.target, DeliveryTarget):
            raise ValueError("target must be a DeliveryTarget")
        if self.target.generation != self.generation:
            raise ValueError("target and request generations must match")
        if not isinstance(self.style, DeliveryStyle):
            raise ValueError("invalid delivery style")
        if not isinstance(self.processed_text, str):
            raise ValueError("processed_text must be a string")
        if type(self.restore_clipboard) is not bool:  # noqa: E721
            raise ValueError("restore_clipboard must be boolean")
        if type(self.submit) is not bool:  # noqa: E721
            raise ValueError("submit must be boolean")


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Content-free result; target identity and text never serialize."""

    request_id: str
    generation: int
    route: DeliveryRoute
    status: DeliveryStatus
    failure: DeliveryFailure | None = None
    warnings: tuple[DeliveryWarning, ...] = ()
    units_attempted: int = 1
    units_completed: int = 0
    clipboard_restored: bool | None = None
    modifiers_restored: bool = True
    schema_version: int = DELIVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if not isinstance(self.route, DeliveryRoute):
            raise ValueError("invalid delivery route")
        if not isinstance(self.status, DeliveryStatus):
            raise ValueError("invalid delivery status")
        if self.schema_version != DELIVERY_SCHEMA_VERSION:
            raise ValueError("unsupported delivery schema version")
        failed = self.status is DeliveryStatus.FAILED
        if failed != (self.failure is not None):
            raise ValueError("failure must match failed delivery status")
        if self.failure is not None and not isinstance(self.failure, DeliveryFailure):
            raise ValueError("failure must be a DeliveryFailure")
        if not all(isinstance(item, DeliveryWarning) for item in self.warnings):
            raise ValueError("warnings must contain DeliveryWarning values")
        if len(self.warnings) != len(set(self.warnings)):
            raise ValueError("delivery warnings must be unique")
        if (
            isinstance(self.units_attempted, bool)
            or isinstance(self.units_completed, bool)
            or self.units_attempted < 0
            or self.units_completed < 0
            or self.units_completed > self.units_attempted
        ):
            raise ValueError("delivery unit counts are invalid")
        if self.clipboard_restored is not None and type(
            self.clipboard_restored
        ) is not bool:  # noqa: E721
            raise ValueError("clipboard_restored must be boolean or None")
        if type(self.modifiers_restored) is not bool:  # noqa: E721
            raise ValueError("modifiers_restored must be boolean")
        if (
            self.status is DeliveryStatus.FOCUSED_ATTEMPTED
            and self.route is not DeliveryRoute.FOCUSED
        ):
            raise ValueError("focused status requires focused route")

    @property
    def partial(self) -> bool:
        return 0 < self.units_completed < self.units_attempted

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "generation": self.generation,
            "route": self.route.value,
            "status": self.status.value,
            "failure": self.failure.value if self.failure else None,
            "warnings": [item.value for item in self.warnings],
            "units_attempted": self.units_attempted,
            "units_completed": self.units_completed,
            "partial": self.partial,
            "clipboard_restored": self.clipboard_restored,
            "modifiers_restored": self.modifiers_restored,
        }


class DeliveryClaimLedger:
    """Process-local exactly-once gate for authoritative final delivery."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._claimed: set[tuple[str, int]] = set()

    def claim(self, request_id: str, generation: int) -> DeliveryClaimStatus:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        identity = (request_id, generation)
        with self._lock:
            if identity in self._claimed:
                return DeliveryClaimStatus.REUSED
            self._claimed.add(identity)
            return DeliveryClaimStatus.CLAIMED


@runtime_checkable
class DeliveryAdapter(Protocol):
    """Resolve focus once and deliver one already-processed request."""

    def resolve_target(
        self,
        request_id: str,
        generation: int,
        route: DeliveryRoute,
    ) -> DeliveryTargetOutcome:
        """Return one opaque target or a typed, content-free failure."""

    def deliver(self, request: DeliveryRequest) -> DeliveryReceipt:
        """Attempt exactly one generation with adapter-owned modifier handling."""

    def cancel(self, request_id: str, generation: int) -> DeliveryReceipt:
        """Cancel only the matching generation before input is attempted."""

    def update_clipboard_settings(self, *, behavior: bool, clear_delay: float) -> None:
        """Apply future preferences without abandoning owned restorations."""
        ...

    def close(self) -> None:
        """Idempotently restore owned state and release delivery resources."""


__all__ = [
    "DELIVERY_SCHEMA_VERSION",
    "DeliveryAdapter",
    "DeliveryClaimLedger",
    "DeliveryClaimStatus",
    "DeliveryFailure",
    "DeliveryReceipt",
    "DeliveryRequest",
    "DeliveryRoute",
    "DeliveryStatus",
    "DeliveryStyle",
    "DeliveryTarget",
    "DeliveryTargetOutcome",
    "DeliveryWarning",
]
