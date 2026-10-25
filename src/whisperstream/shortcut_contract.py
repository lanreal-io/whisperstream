"""Platform-neutral global-shortcut and shortcut-capture contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, ContextManager, Protocol, runtime_checkable


SHORTCUT_SCHEMA_VERSION = 1


class ShortcutGesture(StrEnum):
    HOLD = "hold"
    TOGGLE = "toggle"
    TAP = "tap"


class ShortcutTransition(StrEnum):
    PRESSED = "pressed"
    RELEASED = "released"


class ShortcutAction(StrEnum):
    HOLD_BEGIN = "hold-begin"
    HOLD_END = "hold-end"
    TOGGLE = "toggle"
    FIRE = "fire"


class ShortcutRegistrationStatus(StrEnum):
    REGISTERED = "registered"
    UNBOUND = "unbound"
    FAILED = "failed"


class ShortcutFailure(StrEnum):
    INVALID_BINDING = "invalid-binding"
    CONFLICT = "conflict"
    PERMISSION_DENIED = "permission-denied"
    LISTENER_DISABLED = "listener-disabled"
    NATIVE_ERROR = "native-error"
    CLEANUP_FAILED = "cleanup-failed"


class ShortcutCaptureStatus(StrEnum):
    CAPTURED = "captured"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CanonicalShortcut:
    """Canonical logical keys with all native key objects kept private."""

    keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("shortcut must contain at least one key")
        if not all(isinstance(key, str) and key.strip() for key in self.keys):
            raise ValueError("shortcut keys must be non-empty strings")
        normalized = tuple(key.strip().lower() for key in self.keys)
        if len(normalized) != len(set(normalized)):
            raise ValueError("shortcut keys must be unique")
        if normalized != self.keys:
            object.__setattr__(self, "keys", normalized)


@dataclass(frozen=True, slots=True)
class ShortcutBinding:
    action_id: str
    gesture: ShortcutGesture
    configured: str
    shortcut: CanonicalShortcut | None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("action_id must be non-empty")
        if not isinstance(self.gesture, ShortcutGesture):
            raise ValueError("invalid shortcut gesture")
        if not isinstance(self.configured, str):
            raise ValueError("configured shortcut must be a string")
        if self.shortcut is not None and not isinstance(
            self.shortcut, CanonicalShortcut
        ):
            raise ValueError("shortcut must be canonical when bound")
        if not self.configured.strip() and self.shortcut is not None:
            raise ValueError("empty configured shortcut must be unbound")


@dataclass(frozen=True, slots=True)
class ShortcutRegistrationOutcome:
    action_id: str
    status: ShortcutRegistrationStatus
    failure: ShortcutFailure | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("action_id must be non-empty")
        if not isinstance(self.status, ShortcutRegistrationStatus):
            raise ValueError("invalid shortcut registration status")
        failed = self.status is ShortcutRegistrationStatus.FAILED
        if failed != (self.failure is not None):
            raise ValueError("failure must match failed registration status")
        if self.failure is not None and not isinstance(self.failure, ShortcutFailure):
            raise ValueError("failure must be a ShortcutFailure")


@dataclass(frozen=True, slots=True)
class ShortcutCaptureOutcome:
    status: ShortcutCaptureStatus
    shortcut: CanonicalShortcut | None = None
    failure: ShortcutFailure | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ShortcutCaptureStatus):
            raise ValueError("invalid shortcut capture status")
        if self.status is ShortcutCaptureStatus.CAPTURED:
            if self.shortcut is None or self.failure is not None:
                raise ValueError("captured status requires only a shortcut")
        elif self.status is ShortcutCaptureStatus.FAILED:
            if self.failure is None or self.shortcut is not None:
                raise ValueError("failed status requires only a failure")
        elif self.shortcut is not None or self.failure is not None:
            raise ValueError("cancelled status carries no shortcut or failure")
        if self.failure is not None and not isinstance(self.failure, ShortcutFailure):
            raise ValueError("failure must be a ShortcutFailure")


class ShortcutGestureState:
    """Pure press/release reducer shared by every native listener."""

    def __init__(self, gesture: ShortcutGesture) -> None:
        if not isinstance(gesture, ShortcutGesture):
            raise ValueError("invalid shortcut gesture")
        self._gesture = gesture
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def apply(self, transition: ShortcutTransition) -> tuple[ShortcutAction, ...]:
        if not isinstance(transition, ShortcutTransition):
            raise ValueError("invalid shortcut transition")
        if transition is ShortcutTransition.PRESSED:
            if self._active:
                return ()
            self._active = True
            if self._gesture is ShortcutGesture.HOLD:
                return (ShortcutAction.HOLD_BEGIN,)
            if self._gesture is ShortcutGesture.TOGGLE:
                return (ShortcutAction.TOGGLE,)
            return (ShortcutAction.FIRE,)
        if not self._active:
            return ()
        self._active = False
        if self._gesture is ShortcutGesture.HOLD:
            return (ShortcutAction.HOLD_END,)
        return ()

    def reset(self) -> tuple[ShortcutAction, ...]:
        """Clear state; active hold teardown emits one matching end action."""

        if self._active and self._gesture is ShortcutGesture.HOLD:
            self._active = False
            return (ShortcutAction.HOLD_END,)
        self._active = False
        return ()


ShortcutActionCallback = Callable[[str, ShortcutAction], None]
ShortcutCaptureCallback = Callable[[ShortcutCaptureOutcome], None]


@runtime_checkable
class ShortcutAdapter(Protocol):
    """Register co-resident bindings and own deterministic listener teardown."""

    def register(
        self,
        binding: ShortcutBinding,
        callback: ShortcutActionCallback,
    ) -> ShortcutRegistrationOutcome:
        """Register or explicitly unbind one logical action."""

    def begin_capture(self, callback: ShortcutCaptureCallback) -> None:
        """Suspend normal activation and capture one released logical combo."""

    def cancel_capture(self) -> None:
        """Cancel capture and restore the prior registration set."""

    def suppress_self_events(self) -> ContextManager[None]:
        """Fence adapter-generated input from shortcut activation."""

    def close(self) -> None:
        """Idempotently stop listeners and clear held logical state."""


__all__ = [
    "SHORTCUT_SCHEMA_VERSION",
    "CanonicalShortcut",
    "ShortcutAction",
    "ShortcutActionCallback",
    "ShortcutAdapter",
    "ShortcutBinding",
    "ShortcutCaptureCallback",
    "ShortcutCaptureOutcome",
    "ShortcutCaptureStatus",
    "ShortcutFailure",
    "ShortcutGesture",
    "ShortcutGestureState",
    "ShortcutRegistrationOutcome",
    "ShortcutRegistrationStatus",
    "ShortcutTransition",
]
