"""Native-free contract for user-facing windows and app lifecycle actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class WindowRole(str, Enum):
    MAIN = "main"
    HUD = "hud"
    SNAP = "snap"
    REVIEW = "review"
    UPDATE = "update"
    EULA = "eula"
    ERROR = "error"


class WindowAction(str, Enum):
    SHOW = "show"
    HIDE = "hide"
    FOCUS = "focus"
    MINIMIZE = "minimize"
    RESTORE = "restore"
    CLOSE_REQUEST = "close-request"
    QUIT = "quit"


class WindowCloseDisposition(str, Enum):
    HIDE = "hide"
    CLOSE = "close"
    QUIT = "quit"


class LaunchPresentation(str, Enum):
    NORMAL = "normal"
    MINIMIZED = "minimized"


class LaunchAtLoginState(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    REQUIRES_APPROVAL = "requires-approval"
    NOT_FOUND = "not-found"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


class WindowFailure(str, Enum):
    NONE = "none"
    UNSUPPORTED = "unsupported"
    NOT_FOUND = "not-found"
    INVALID_STATE = "invalid-state"
    DISPLAY_UNAVAILABLE = "display-unavailable"
    STALE_GENERATION = "stale-generation"
    NATIVE_FAILURE = "native-failure"


@dataclass(frozen=True)
class WindowRef:
    role: WindowRole
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError("window token must be nonempty")


@dataclass(frozen=True)
class LogicalGeometry:
    x: float
    y: float
    width: float
    height: float
    display_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or not self.display_token.strip():
            raise ValueError("geometry requires positive size and display token")


@dataclass(frozen=True)
class WindowPolicy:
    role: WindowRole
    close_disposition: WindowCloseDisposition
    focusable: bool
    resizable: bool
    visible_on_normal_launch: bool
    visible_on_minimized_launch: bool


@dataclass(frozen=True)
class WindowLifecycleRequest:
    generation: int
    window: WindowRef
    action: WindowAction
    geometry: LogicalGeometry | None = None

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")


@dataclass(frozen=True)
class WindowLifecycleResult:
    generation: int
    role: WindowRole
    action: WindowAction
    visible: bool
    focused: bool
    failure: WindowFailure = WindowFailure.NONE


@dataclass(frozen=True)
class LaunchAtLoginResult:
    state: LaunchAtLoginState
    failure: WindowFailure = WindowFailure.NONE

    def __post_init__(self) -> None:
        failure_states = {
            LaunchAtLoginState.NOT_FOUND,
            LaunchAtLoginState.UNSUPPORTED,
            LaunchAtLoginState.UNAVAILABLE,
        }
        if (self.state in failure_states) is (self.failure is WindowFailure.NONE):
            raise ValueError("launch-at-login state and failure disagree")


@runtime_checkable
class WindowLifecycleAdapter(Protocol):
    """Adapter owns native window IDs, monitor objects, tray/menu, and Dock."""

    def launch(self, presentation: LaunchPresentation) -> tuple[WindowLifecycleResult, ...]: ...

    def apply(self, request: WindowLifecycleRequest) -> WindowLifecycleResult: ...

    def set_launch_at_login(self, enabled: bool) -> LaunchAtLoginResult: ...

    def launch_at_login_state(self) -> LaunchAtLoginResult: ...


@runtime_checkable
class WindowLifecycleFactory(Protocol):
    """Select the platform window owner without constructing shared handles."""

    def create(self) -> WindowLifecycleAdapter: ...


WINDOW_LIFECYCLE_SCHEMA_VERSION = 1
