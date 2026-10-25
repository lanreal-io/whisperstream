"""Native macOS global shortcuts backed by a Core Graphics event tap.

The event tap, run-loop objects, virtual key codes, and native event pointers
stay in this module.  :class:`MacosShortcutAdapter` translates them into the
schema-v1 logical shortcut reducer and retains the legacy app-facing methods
used by ``main.py`` and shortcut capture.

Passive permission queries never create an event tap. Registration constructs
the active/default tap only after Accessibility is granted. Input Monitoring
is not required for that operation. No request or System Settings action exists
in this module.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import queue
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol

from .permission_contract import (
    PermissionReport, PermissionState, required_permissions_for_operation,
)
from .shortcut_contract import (
    CanonicalShortcut,
    ShortcutAction,
    ShortcutBinding,
    ShortcutCaptureCallback,
    ShortcutCaptureOutcome,
    ShortcutCaptureStatus,
    ShortcutFailure,
    ShortcutGesture,
    ShortcutGestureState,
    ShortcutRegistrationOutcome,
    ShortcutRegistrationStatus,
    ShortcutTransition,
)


_LISTENER_JOIN_TIMEOUT_S = 2.0
_LISTENER_START_TIMEOUT_S = 2.0
_SUPPRESS_GRACE_S = 0.05
_DISPATCH_STOP = object()

# Core Graphics constants from the macOS SDK's CGEventTypes.h.
_CG_EVENT_KEY_DOWN = 10
_CG_EVENT_KEY_UP = 11
_CG_EVENT_FLAGS_CHANGED = 12
_CG_EVENT_OTHER_MOUSE_DOWN = 25
_CG_EVENT_OTHER_MOUSE_UP = 26
_CG_EVENT_TAP_DISABLED_BY_TIMEOUT = 0xFFFFFFFE
_CG_EVENT_TAP_DISABLED_BY_USER_INPUT = 0xFFFFFFFF
_CG_KEYBOARD_EVENT_KEYCODE = 9
_CG_MOUSE_EVENT_BUTTON_NUMBER = 3
_CG_EVENT_SOURCE_USER_DATA = 42
_CG_SESSION_EVENT_TAP = 1
_CG_HEAD_INSERT_EVENT_TAP = 0
_CG_EVENT_TAP_OPTION_DEFAULT = 0
_CG_COMBINED_SESSION_STATE = 0
_SYNTHETIC_EVENT_MARKER = 0x5753485354524D31  # ``WSHSTRM1``

# Modifier state carried by a ``flagsChanged`` event. These are the public
# ``CGEventFlags`` values from CGEventTypes.h. Reading the event itself matters:
# at a head-insert tap, the accumulated event-source key state can still reflect
# the preceding event rather than the modifier transition being delivered.
_CG_EVENT_FLAG_MASK_ALPHA_SHIFT = 0x00010000
_CG_EVENT_FLAG_MASK_SHIFT = 0x00020000
_CG_EVENT_FLAG_MASK_CONTROL = 0x00040000
_CG_EVENT_FLAG_MASK_ALTERNATE = 0x00080000
_CG_EVENT_FLAG_MASK_COMMAND = 0x00100000
_CG_EVENT_FLAG_MASK_SECONDARY_FN = 0x00800000
_MODIFIER_FLAG_BY_KEYCODE = {
    54: _CG_EVENT_FLAG_MASK_COMMAND,
    55: _CG_EVENT_FLAG_MASK_COMMAND,
    56: _CG_EVENT_FLAG_MASK_SHIFT,
    57: _CG_EVENT_FLAG_MASK_ALPHA_SHIFT,
    58: _CG_EVENT_FLAG_MASK_ALTERNATE,
    59: _CG_EVENT_FLAG_MASK_CONTROL,
    60: _CG_EVENT_FLAG_MASK_SHIFT,
    61: _CG_EVENT_FLAG_MASK_ALTERNATE,
    62: _CG_EVENT_FLAG_MASK_CONTROL,
    63: _CG_EVENT_FLAG_MASK_SECONDARY_FN,
}
_MODIFIER_NAME_BY_FLAG = {
    "cmd": _CG_EVENT_FLAG_MASK_COMMAND,
    "shift": _CG_EVENT_FLAG_MASK_SHIFT,
    "option": _CG_EVENT_FLAG_MASK_ALTERNATE,
    "ctrl": _CG_EVENT_FLAG_MASK_CONTROL,
    "caps lock": _CG_EVENT_FLAG_MASK_ALPHA_SHIFT,
    "fn": _CG_EVENT_FLAG_MASK_SECONDARY_FN,
}


class _ListenerDisabledError(RuntimeError):
    """Raised when an event tap cannot remain active."""


class _NativeShortcutUnavailableError(RuntimeError):
    """Raised when native capability is unavailable but not known denied."""


class _CleanupFailedError(RuntimeError):
    """Raised when an exact native listener owner survives bounded teardown."""


_KeyIdentity = tuple[str, int]


@dataclass(frozen=True, slots=True)
class MacKeyEvent:
    """Adapter-private normalized event emitted by an injected/native source."""

    identity: _KeyIdentity | None = None
    transition: ShortcutTransition | None = None
    modifiers: frozenset[str] = frozenset()
    synthetic: bool = False
    disabled_reason: ShortcutFailure | None = None
    reenabled: bool = False

    def __post_init__(self) -> None:
        disabled = self.disabled_reason is not None
        if disabled:
            if (
                self.identity is not None
                or self.transition is not None
                or self.modifiers
            ):
                raise ValueError("disabled event cannot carry a key transition")
            if self.disabled_reason is not ShortcutFailure.LISTENER_DISABLED:
                raise ValueError("disabled event requires listener-disabled")
        elif self.identity is None or self.transition is None:
            raise ValueError("key event requires identity and transition")


class _EventListener(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


class _CoreGraphicsApi:
    """Lazy ctypes bindings for the documented CoreGraphics/CoreFoundation API."""

    _CALLBACK = ctypes.CFUNCTYPE(
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )

    def __init__(self) -> None:
        cg_path = ctypes.util.find_library("CoreGraphics")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not cg_path or not cf_path:
            raise _NativeShortcutUnavailableError("native shortcut framework unavailable")
        self.cg = ctypes.CDLL(cg_path)
        self.cf = ctypes.CDLL(cf_path)

        self.cg.CGEventTapCreate.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint64,
            self._CALLBACK,
            ctypes.c_void_p,
        ]
        self.cg.CGEventTapCreate.restype = ctypes.c_void_p
        self.cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        self.cg.CGEventTapEnable.restype = None
        self.cg.CGEventTapIsEnabled.argtypes = [ctypes.c_void_p]
        self.cg.CGEventTapIsEnabled.restype = ctypes.c_bool
        self.cg.CGEventGetIntegerValueField.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.cg.CGEventGetIntegerValueField.restype = ctypes.c_int64
        self.cg.CGEventGetFlags.argtypes = [ctypes.c_void_p]
        self.cg.CGEventGetFlags.restype = ctypes.c_uint64
        self.cg.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_uint16]
        self.cg.CGEventSourceKeyState.restype = ctypes.c_bool

        self.cf.CFMachPortCreateRunLoopSource.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
        ]
        self.cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
        self.cf.CFMachPortInvalidate.argtypes = [ctypes.c_void_p]
        self.cf.CFMachPortInvalidate.restype = None
        self.cf.CFRunLoopGetCurrent.argtypes = []
        self.cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        self.cf.CFRunLoopAddSource.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.cf.CFRunLoopAddSource.restype = None
        self.cf.CFRunLoopRemoveSource.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.cf.CFRunLoopRemoveSource.restype = None
        self.cf.CFRunLoopRun.argtypes = []
        self.cf.CFRunLoopRun.restype = None
        self.cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]
        self.cf.CFRunLoopStop.restype = None
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cf.CFRelease.restype = None

        self.common_modes = ctypes.c_void_p.in_dll(
            self.cf, "kCFRunLoopCommonModes"
        ).value
        if not self.common_modes:
            raise _NativeShortcutUnavailableError("native run-loop mode unavailable")

    def key_down(self, keycode: int) -> bool:
        return bool(self.cg.CGEventSourceKeyState(_CG_COMBINED_SESSION_STATE, keycode))


class _CoreGraphicsEventListener:
    """One exact active session event tap owned by a dedicated CFRunLoop."""

    def __init__(
        self,
        callback: Callable[[MacKeyEvent], bool],
        *,
        api_factory: Callable[[], _CoreGraphicsApi] = _CoreGraphicsApi,
    ) -> None:
        self._callback = callback
        self._api_factory = api_factory
        self._api: _CoreGraphicsApi | None = None
        self._tap: int | None = None
        self._source: int | None = None
        self._run_loop: int | None = None
        self._native_callback: object | None = None
        self._modifier_keys_down: set[int] = set()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._stop_requested = threading.Event()
        self._start_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="MacEventTap",
        )

    @staticmethod
    def _mask() -> int:
        return sum(
            1 << event_type
            for event_type in (
                _CG_EVENT_KEY_DOWN,
                _CG_EVENT_KEY_UP,
                _CG_EVENT_FLAGS_CHANGED,
                _CG_EVENT_OTHER_MOUSE_DOWN,
                _CG_EVENT_OTHER_MOUSE_UP,
            )
        )

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(_LISTENER_START_TIMEOUT_S):
            self.stop()
            self.join(_LISTENER_JOIN_TIMEOUT_S)
            raise _ListenerDisabledError("event tap start timed out")
        if self._start_error is not None:
            raise self._start_error
        if not self.is_alive():
            raise _ListenerDisabledError("event tap disabled during start")

    def _run(self) -> None:
        api: _CoreGraphicsApi | None = None
        tap: int | None = None
        source: int | None = None
        run_loop: int | None = None
        try:
            api = self._api_factory()
            native_callback = api._CALLBACK(self._on_native_event)
            tap = api.cg.CGEventTapCreate(
                _CG_SESSION_EVENT_TAP,
                _CG_HEAD_INSERT_EVENT_TAP,
                _CG_EVENT_TAP_OPTION_DEFAULT,
                self._mask(),
                native_callback,
                None,
            )
            if not tap:
                raise _NativeShortcutUnavailableError("event tap creation failed")
            source = api.cf.CFMachPortCreateRunLoopSource(None, tap, 0)
            if not source:
                raise _NativeShortcutUnavailableError("event tap run-loop source failed")
            run_loop = api.cf.CFRunLoopGetCurrent()
            if not run_loop:
                raise _NativeShortcutUnavailableError("event tap run loop unavailable")

            self._api = api
            self._tap = int(tap)
            self._source = int(source)
            self._run_loop = int(run_loop)
            self._native_callback = native_callback
            api.cf.CFRunLoopAddSource(run_loop, source, api.common_modes)
            api.cg.CGEventTapEnable(tap, True)
            if not api.cg.CGEventTapIsEnabled(tap):
                raise _ListenerDisabledError("event tap did not enable")
            self._ready.set()
            if not self._stop_requested.is_set():
                api.cf.CFRunLoopRun()
        except BaseException as exc:  # noqa: BLE001 - transfer startup failure
            self._start_error = exc
            self._ready.set()
        finally:
            if api is not None and run_loop and source:
                try:
                    api.cf.CFRunLoopRemoveSource(run_loop, source, api.common_modes)
                except Exception:
                    pass
            if api is not None and tap:
                try:
                    api.cf.CFMachPortInvalidate(tap)
                except Exception:
                    pass
            if api is not None and source:
                try:
                    api.cf.CFRelease(source)
                except Exception:
                    pass
            if api is not None and tap:
                try:
                    api.cf.CFRelease(tap)
                except Exception:
                    pass
            self._tap = None
            self._source = None
            self._run_loop = None
            self._native_callback = None
            self._stopped.set()

    def _on_native_event(
        self,
        _proxy: int,
        event_type: int,
        event: int,
        _user_info: int,
    ) -> int | None:
        api = self._api
        tap = self._tap
        if api is None:
            return event
        if event_type in {
            _CG_EVENT_TAP_DISABLED_BY_TIMEOUT,
            _CG_EVENT_TAP_DISABLED_BY_USER_INPUT,
        }:
            reenabled = False
            if tap:
                try:
                    api.cg.CGEventTapEnable(tap, True)
                    reenabled = bool(api.cg.CGEventTapIsEnabled(tap))
                except Exception:
                    reenabled = False
            self._callback(
                MacKeyEvent(
                    disabled_reason=ShortcutFailure.LISTENER_DISABLED,
                    reenabled=reenabled,
                )
            )
            return event

        field = (
            _CG_MOUSE_EVENT_BUTTON_NUMBER
            if event_type in {_CG_EVENT_OTHER_MOUSE_DOWN, _CG_EVENT_OTHER_MOUSE_UP}
            else _CG_KEYBOARD_EVENT_KEYCODE
        )
        raw_code = int(api.cg.CGEventGetIntegerValueField(event, field))
        synthetic = (
            int(
                api.cg.CGEventGetIntegerValueField(
                    event, _CG_EVENT_SOURCE_USER_DATA
                )
            )
            == _SYNTHETIC_EVENT_MARKER
        )
        event_flags = int(api.cg.CGEventGetFlags(event))
        modifiers = frozenset(
            name for name, flag in _MODIFIER_NAME_BY_FLAG.items() if event_flags & flag
        )
        if event_type in {_CG_EVENT_OTHER_MOUSE_DOWN, _CG_EVENT_OTHER_MOUSE_UP}:
            # Quartz button 2 is the middle button, exposed as mouse3.
            if raw_code not in {2, 3, 4}:
                return event
            identity = ("mouse", raw_code)
            pressed = event_type == _CG_EVENT_OTHER_MOUSE_DOWN
        else:
            identity = ("key", raw_code)
            pressed = event_type == _CG_EVENT_KEY_DOWN
            if event_type == _CG_EVENT_FLAGS_CHANGED:
                flag = _MODIFIER_FLAG_BY_KEYCODE.get(raw_code)
                if flag is None:
                    return event
                if raw_code in self._modifier_keys_down:
                    pressed = False
                    if not synthetic:
                        self._modifier_keys_down.discard(raw_code)
                else:
                    pressed = bool(event_flags & flag)
                    if pressed and not synthetic:
                        self._modifier_keys_down.add(raw_code)
        suppress = self._callback(
            MacKeyEvent(
                identity=identity,
                transition=(
                    ShortcutTransition.PRESSED
                    if pressed
                    else ShortcutTransition.RELEASED
                ),
                modifiers=modifiers,
                synthetic=synthetic,
            )
        )
        return None if suppress else event

    def stop(self) -> None:
        self._stop_requested.set()
        run_loop = self._run_loop
        api = self._api
        if run_loop and api is not None:
            api.cf.CFRunLoopStop(run_loop)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        if not self._thread.is_alive() or self._stopped.is_set():
            return False
        api = self._api
        tap = self._tap
        if api is None or tap is None:
            return False
        try:
            return bool(api.cg.CGEventTapIsEnabled(tap))
        except Exception:
            return False


# ANSI virtual-key positions plus Mac special keys. Config parsing accepts the
# stable names used by existing Windows configs; captured Mac shortcuts emit
# Mac-native ``cmd``/``option`` names.
_NAME_TO_CODES: dict[str, frozenset[int]] = {
    "a": frozenset({0}), "s": frozenset({1}), "d": frozenset({2}),
    "f": frozenset({3}), "h": frozenset({4}), "g": frozenset({5}),
    "z": frozenset({6}), "x": frozenset({7}), "c": frozenset({8}),
    "v": frozenset({9}), "b": frozenset({11}), "q": frozenset({12}),
    "w": frozenset({13}), "e": frozenset({14}), "r": frozenset({15}),
    "y": frozenset({16}), "t": frozenset({17}), "1": frozenset({18}),
    "2": frozenset({19}), "3": frozenset({20}), "4": frozenset({21}),
    "6": frozenset({22}), "5": frozenset({23}), "=": frozenset({24}),
    "9": frozenset({25}), "7": frozenset({26}), "-": frozenset({27}),
    "8": frozenset({28}), "0": frozenset({29}), "]": frozenset({30}),
    "o": frozenset({31}), "u": frozenset({32}), "[": frozenset({33}),
    "i": frozenset({34}), "p": frozenset({35}), "l": frozenset({37}),
    "j": frozenset({38}), "'": frozenset({39}), "k": frozenset({40}),
    ";": frozenset({41}), "\\": frozenset({42}), ",": frozenset({43}),
    "/": frozenset({44}), "n": frozenset({45}), "m": frozenset({46}),
    ".": frozenset({47}), "`": frozenset({50}),
    "enter": frozenset({36, 76}),
    "tab": frozenset({48}),
    "space": frozenset({49}),
    "backspace": frozenset({51}),
    "esc": frozenset({53}),
    "cmd": frozenset({54, 55}),
    "left cmd": frozenset({55}),
    "right cmd": frozenset({54}),
    "shift": frozenset({56, 60}),
    "left shift": frozenset({56}),
    "right shift": frozenset({60}),
    "caps lock": frozenset({57}),
    "option": frozenset({58, 61}),
    "left option": frozenset({58}),
    "right option": frozenset({61}),
    "ctrl": frozenset({59, 62}),
    "left ctrl": frozenset({59}),
    "right ctrl": frozenset({62}),
    "fn": frozenset({63}),
    "delete": frozenset({117}),
    "home": frozenset({115}),
    "end": frozenset({119}),
    "page up": frozenset({116}),
    "page down": frozenset({121}),
    "left": frozenset({123}),
    "right": frozenset({124}),
    "down": frozenset({125}),
    "up": frozenset({126}),
    "f1": frozenset({122}), "f2": frozenset({120}),
    "f3": frozenset({99}), "f4": frozenset({118}),
    "f5": frozenset({96}), "f6": frozenset({97}),
    "f7": frozenset({98}), "f8": frozenset({100}),
    "f9": frozenset({101}), "f10": frozenset({109}),
    "f11": frozenset({103}), "f12": frozenset({111}),
    "f13": frozenset({105}), "f14": frozenset({107}),
    "f15": frozenset({113}), "f16": frozenset({106}),
    "f17": frozenset({64}), "f18": frozenset({79}),
    "f19": frozenset({80}), "f20": frozenset({90}),
}
_ALIASES = {
    "command": "cmd",
    "win": "cmd",
    "super": "cmd",
    "meta": "cmd",
    "left win": "left cmd",
    "right win": "right cmd",
    "alt": "option",
    "left alt": "left option",
    "right alt": "right option",
    "control": "ctrl",
    "left control": "left ctrl",
    "right control": "right ctrl",
    "return": "enter",
    "escape": "esc",
    "grave": "`",
    "tilde": "`",
    "mouse 3": "mouse3",
    "mouse_3": "mouse3",
    "middle": "mouse3",
    "middle click": "mouse3",
    "middle mouse": "mouse3",
    "mouse 4": "mouse4",
    "mouse_4": "mouse4",
    "mouse 5": "mouse5",
    "mouse_5": "mouse5",
}
_MOUSE_NAMES = {"mouse3": 2, "mouse4": 3, "mouse5": 4}
_CODE_TO_NAME = {
    code: name
    for name, codes in _NAME_TO_CODES.items()
    if name not in {"cmd", "shift", "option", "ctrl", "enter"}
    for code in codes
}
_CODE_TO_NAME.update(
    {
        36: "enter", 76: "enter",
        55: "cmd", 54: "right cmd",
        56: "shift", 60: "right shift",
        58: "option", 61: "right option",
        59: "ctrl", 62: "right ctrl",
    }
)
_MOUSE_CODE_TO_NAME = {value: key for key, value in _MOUSE_NAMES.items()}
_MODIFIER_CODES = frozenset({54, 55, 56, 57, 58, 59, 60, 61, 62, 63})
_FUNCTION_CODES = frozenset(
    code for number in range(1, 21) for code in _NAME_TO_CODES[f"f{number}"]
)
_SUPPRESSIBLE_STANDALONE_CODES = _MODIFIER_CODES | _FUNCTION_CODES | {50}
_CAPTURE_ORDER = {
    "ctrl": 0, "left ctrl": 0, "right ctrl": 0,
    "option": 1, "left option": 1, "right option": 1,
    "shift": 2, "left shift": 2, "right shift": 2,
    "cmd": 3, "left cmd": 3, "right cmd": 3,
    "fn": 4,
}
_CAPTURE_MODIFIER_FAMILIES = {
    "ctrl": frozenset({"ctrl", "left ctrl", "right ctrl"}),
    "option": frozenset({"option", "left option", "right option"}),
    "shift": frozenset({"shift", "left shift", "right shift"}),
    "cmd": frozenset({"cmd", "left cmd", "right cmd"}),
    "caps lock": frozenset({"caps lock"}),
    "fn": frozenset({"fn"}),
}
_MAC_GLYPHS = {
    "ctrl": "⌃", "left ctrl": "⌃", "right ctrl": "⌃",
    "option": "⌥", "left option": "⌥", "right option": "⌥",
    "shift": "⇧", "left shift": "⇧", "right shift": "⇧",
    "cmd": "⌘", "left cmd": "⌘", "right cmd": "⌘",
    "caps lock": "⇪", "enter": "↩", "tab": "⇥",
    "backspace": "⌫", "delete": "⌦", "esc": "⎋",
    "left": "←", "right": "→", "up": "↑", "down": "↓",
    "page up": "⇞", "page down": "⇟", "home": "↖", "end": "↘",
}


def _normalize_name(name: str) -> str | None:
    normalized = _ALIASES.get(name.strip().lower(), name.strip().lower())
    if normalized in _NAME_TO_CODES or normalized in _MOUSE_NAMES:
        return normalized
    return None


def _identity_name(identity: _KeyIdentity) -> str | None:
    kind, code = identity
    if kind == "mouse":
        return _MOUSE_CODE_TO_NAME.get(code)
    return _CODE_TO_NAME.get(code)


def _ordered_names(names: set[str] | list[str]) -> list[str]:
    return sorted(
        set(names),
        key=lambda item: (_CAPTURE_ORDER.get(item, 100), item),
    )


def macos_shortcut_glyphs(configured: str) -> str:
    """Return canonical compact Mac notation without native key objects."""

    names: list[str] = []
    for raw in configured.split("+"):
        name = _normalize_name(raw)
        if name is None:
            continue
        names.append(name)
    rendered: list[str] = []
    for name in _ordered_names(names):
        if name in _MAC_GLYPHS:
            rendered.append(_MAC_GLYPHS[name])
        elif len(name) == 1:
            rendered.append(name.upper())
        elif name == "space":
            rendered.append("Space")
        else:
            rendered.append(name.upper() if name.startswith("f") else name.title())
    return "".join(rendered)


@dataclass(slots=True)
class _RegisteredBinding:
    binding: ShortcutBinding
    targets: tuple[frozenset[_KeyIdentity], ...]
    callback: Callable[[str, ShortcutAction], None]
    state: ShortcutGestureState
    hold_begin_delivered: bool = False


class MacosShortcutAdapter:
    """CoreGraphics adapter for the frozen shortcut contract and legacy app API."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        listener_factory: Callable[[Callable[[MacKeyEvent], bool]], _EventListener] | None = None,
        permission_reporter: Callable[[], PermissionReport] | None = None,
        physical_key_state: Callable[[int], bool] | None = None,
        suppress_timer_factory: Callable[[float, Callable[[], None]], threading.Timer] | None = None,
    ) -> None:
        self._logger = logger
        self._listener_factory = listener_factory or _CoreGraphicsEventListener
        self._permission_reporter = permission_reporter or self._query_permissions
        self._physical_key_state = physical_key_state or self._native_key_state
        self._suppress_timer_factory = suppress_timer_factory or threading.Timer
        self._listener: _EventListener | None = None
        self._listener_generation: object | None = None
        self._lock = threading.RLock()
        self._listener_lifecycle_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._wait_event = threading.Event()
        self._pressed: set[_KeyIdentity] = set()
        self._bindings: dict[str, _RegisteredBinding] = {}
        self._toggle_active = False
        self._mode = "none"
        self._capture_active = False
        self._capture_names: set[str] = set()
        self._capture_identities: set[_KeyIdentity] = set()
        self._capture_on_changed: Callable[[str], None] | None = None
        self._capture_on_captured: Callable[[str], None] | None = None
        self._capture_contract_callback: ShortcutCaptureCallback | None = None
        self._early_handlers: dict[
            str, tuple[Callable[[], bool], Callable[[], None]]
        ] = {}
        self._early_suppressed: set[_KeyIdentity] = set()
        self._standalone_suppressed: set[_KeyIdentity] = set()
        self._passthrough_until_release: set[_KeyIdentity] = set()
        self._suppress_depth = 0
        self._suppress_lock = threading.Lock()
        self._suppress_timers: set[threading.Timer] = set()
        self._last_cleanup_failure: ShortcutFailure | None = None
        self._last_listener_failure: ShortcutFailure | None = None
        self._listener_reenable_count = 0
        self._queue: queue.Queue[object] = queue.Queue()
        self._worker = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="MacKbdDispatch",
        )
        self._worker.start()

    @staticmethod
    def _query_permissions() -> PermissionReport:
        from .macos_permission_status import MacosPermissionStatusProvider

        return MacosPermissionStatusProvider().query_permissions()

    @staticmethod
    def _native_key_state(keycode: int) -> bool:
        return _CoreGraphicsApi().key_down(keycode)

    @staticmethod
    def native_symbols_available() -> bool:
        """Resolve the real native adapter symbols without registering a tap."""

        try:
            _CoreGraphicsApi()
        except Exception:
            return False
        return True

    @property
    def last_cleanup_failure(self) -> ShortcutFailure | None:
        return self._last_cleanup_failure

    @property
    def last_listener_failure(self) -> ShortcutFailure | None:
        return self._last_listener_failure

    @property
    def listener_reenable_count(self) -> int:
        return self._listener_reenable_count

    @property
    def capture_active(self) -> bool:
        with self._lock:
            return self._capture_active or self._mode.startswith("capture")

    @property
    def listener_ready(self) -> bool:
        """Observe operational health without creating a tap or requesting access."""
        with self._lock:
            listener = self._listener
            if (
                self._closed
                or self._last_listener_failure is not None
                or self._last_cleanup_failure is not None
                or listener is None
            ):
                return False
            try:
                return bool(listener.is_alive())
            except Exception:
                return False

    def ensure_ready(self) -> bool:
        """Reconcile an idle listener, including before any shortcut is bound.

        App orchestration serializes this with capture and configuration changes.
        A held gesture or capture is deferred rather than interrupted. Permission
        checks are passive; only an explicit permission action may prompt.
        """
        with self._close_lock:
            if self._closed:
                raise _ListenerDisabledError("shortcut adapter is closed")
            self.reconcile_lost_releases()
            with self._lock:
                if self.capture_active or self._pressed or any(
                    binding.state.active for binding in self._bindings.values()
                ):
                    return False
            try:
                self._permission_gate()
                if not self.listener_ready:
                    self._disable_listener_after_failure()
                    if self._last_cleanup_failure is not None:
                        raise _CleanupFailedError("event tap cleanup failed")
                    self._start_listener()
            except Exception as exc:
                self._last_listener_failure = self._registration_failure(exc)
                self._disable_listener_after_failure()
                raise
            return self.listener_ready

    def reconcile_lost_releases(self) -> None:
        """Settle physically released keys after an unhealthy tap misses them.

        This never starts/stops a listener or synthesizes a press. The app may
        call it while recording so a queued balancing hold-end can settle that
        recording before an idle recovery attempt. Healthy listeners retain
        sole ownership of their live gestures.
        """
        with self._lock:
            if self._closed or self.capture_active or self.listener_ready:
                return
            observed = set(self._pressed)
            for registered in self._bindings.values():
                if registered.state.active:
                    for options in registered.targets:
                        observed.update(options)
            for identity in observed:
                # This adapter's physical-state seam can prove key releases.
                # An unknown key or mouse state cannot authorize a release.
                down = True
                if identity[0] == "key":
                    try:
                        down = bool(self._physical_key_state(identity[1]))
                    except Exception:
                        pass
                if down:
                    self._pressed.add(identity)
                else:
                    self._pressed.discard(identity)
            for registered in self._bindings.values():
                if registered.state.active and not self._targets_active(
                    registered.targets, self._pressed, allow_extra_modifiers=True
                ):
                    for action in registered.state.apply(ShortcutTransition.RELEASED):
                        self._dispatch_action(registered, action)

    def _dispatch_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _DISPATCH_STOP:
                return
            try:
                assert callable(item)
                item()
            except Exception:
                self._logger.exception("hotkey callback raised")

    def _queue_callback(
        self,
        callback: Callable[[], None],
        *,
        allow_when_closed: bool = False,
    ) -> None:
        def guarded() -> None:
            if self._closed and not allow_when_closed:
                return
            callback()

        self._queue.put(guarded)

    def _permission_gate(self) -> None:
        try:
            report = self._permission_reporter()
        except Exception as exc:
            raise _NativeShortcutUnavailableError(
                "shortcut permission status unavailable"
            ) from exc
        states = {snapshot.kind: snapshot.state for snapshot in report.permissions}
        required = tuple(
            states.get(kind, PermissionState.UNKNOWN)
            for kind in required_permissions_for_operation("global-shortcuts")
        )
        if all(state is PermissionState.GRANTED for state in required):
            return
        if any(
            state in {
                PermissionState.NOT_DETERMINED,
                PermissionState.DENIED,
                PermissionState.RESTRICTED,
            }
            for state in required
        ):
            raise PermissionError("global shortcut permission is not granted")
        raise _NativeShortcutUnavailableError("global shortcut permission is unknown")

    def _start_listener(self) -> None:
        # Capture completion and application recovery can arrive on different
        # threads. Retiring and replacing the exact tap is one operation.
        with self._listener_lifecycle_lock:
            if self._closed:
                raise _ListenerDisabledError("shortcut adapter is closed")
            if self._stop_listener() is not None:
                raise _CleanupFailedError("event tap cleanup failed")
            self._permission_gate()
            listener: _EventListener | None = None
            generation = object()
            with self._lock:
                self._listener_generation = generation
            try:
                listener = self._listener_factory(
                    lambda event: self._handle_listener_event(generation, event)
                )
                listener.start()
                if not listener.is_alive():
                    raise _ListenerDisabledError("event tap disabled")
            except Exception:
                with self._lock:
                    self._listener_generation = None
                cleanup_ok = self._stop_native_listener(listener)
                with self._lock:
                    self._pressed.clear()
                    # A partially started tap can survive stop/join too. Keep
                    # its exact owner so retry/close can finish its teardown.
                    self._listener = None if cleanup_ok else listener
                if not cleanup_ok:
                    self._last_cleanup_failure = ShortcutFailure.CLEANUP_FAILED
                    raise _CleanupFailedError("event tap cleanup failed")
                raise
            with self._lock:
                self._listener = listener
                self._pressed.clear()
                self._last_listener_failure = None

    @staticmethod
    def _stop_native_listener(listener: _EventListener | None) -> bool:
        if listener is None:
            return True
        ok = True
        try:
            listener.stop()
        except Exception:
            ok = False
        try:
            listener.join(_LISTENER_JOIN_TIMEOUT_S)
        except Exception:
            ok = False
        try:
            if listener.is_alive():
                ok = False
        except Exception:
            ok = False
        return ok

    def _stop_listener(self) -> ShortcutFailure | None:
        with self._listener_lifecycle_lock:
            with self._lock:
                listener = self._listener
                self._listener_generation = None
                self._pressed.clear()
                self._early_suppressed.clear()
                self._standalone_suppressed.clear()
                self._passthrough_until_release.clear()
            if listener is None:
                # No owned tap is not evidence that an earlier failed cleanup
                # succeeded. Only verified teardown can clear its latch.
                return self._last_cleanup_failure
            ok = self._stop_native_listener(listener)
            failure = None if ok else ShortcutFailure.CLEANUP_FAILED
            with self._lock:
                self._listener = None if ok else listener
                self._last_cleanup_failure = failure
            if failure is not None:
                self._logger.warning("hotkey listener cleanup failed")
            return failure

    def _has_bindings(self) -> bool:
        return bool(self._bindings or self._early_handlers)

    def _ensure_listener(self) -> None:
        if self._has_bindings() and not self.listener_ready:
            self._start_listener()

    @staticmethod
    def _registration_failure(exc: BaseException) -> ShortcutFailure:
        if isinstance(exc, PermissionError):
            return ShortcutFailure.PERMISSION_DENIED
        if isinstance(exc, _ListenerDisabledError):
            return ShortcutFailure.LISTENER_DISABLED
        if isinstance(exc, _CleanupFailedError):
            return ShortcutFailure.CLEANUP_FAILED
        return ShortcutFailure.NATIVE_ERROR

    @staticmethod
    def _parse_shortcut(configured: str) -> tuple[frozenset[_KeyIdentity], ...]:
        if not configured.strip():
            return ()
        targets: list[frozenset[_KeyIdentity]] = []
        seen_names: set[str] = set()
        for raw in configured.split("+"):
            name = _normalize_name(raw)
            if name is None or name in seen_names:
                return ()
            seen_names.add(name)
            if name in _MOUSE_NAMES:
                targets.append(frozenset({("mouse", _MOUSE_NAMES[name])}))
            else:
                targets.append(
                    frozenset(("key", code) for code in _NAME_TO_CODES[name])
                )
        return tuple(targets)

    @staticmethod
    def _binding_from_config(
        action_id: str,
        gesture: ShortcutGesture,
        configured: str,
    ) -> ShortcutBinding:
        names: list[str] = []
        if configured.strip():
            for raw in configured.split("+"):
                name = _normalize_name(raw)
                if name is not None and name not in names:
                    names.append(name)
        return ShortcutBinding(
            action_id=action_id,
            gesture=gesture,
            configured=configured,
            shortcut=CanonicalShortcut(tuple(names)) if names else None,
        )

    @staticmethod
    def _binding_names(binding: ShortcutBinding) -> set[str] | None:
        if binding.shortcut is None:
            return None
        names: set[str] = set()
        for raw in binding.shortcut.keys:
            name = _normalize_name(raw)
            if name is None or name in names:
                return None
            names.add(name)
        return names

    def _remove_binding(self, action_id: str, *, allow_closed: bool = False) -> None:
        registered = self._bindings.pop(action_id, None)
        if registered is None:
            return
        for action in registered.state.reset():
            self._dispatch_action(registered, action, allow_closed=allow_closed)

    def register(
        self,
        binding: ShortcutBinding,
        callback: Callable[[str, ShortcutAction], None],
    ) -> ShortcutRegistrationOutcome:
        if not isinstance(binding, ShortcutBinding) or not callable(callback):
            return ShortcutRegistrationOutcome(
                action_id=getattr(binding, "action_id", "invalid"),
                status=ShortcutRegistrationStatus.FAILED,
                failure=ShortcutFailure.INVALID_BINDING,
            )
        if self._closed:
            return ShortcutRegistrationOutcome(
                action_id=binding.action_id,
                status=ShortcutRegistrationStatus.FAILED,
                failure=ShortcutFailure.LISTENER_DISABLED,
            )
        if not binding.configured.strip():
            self._remove_binding(binding.action_id)
            return ShortcutRegistrationOutcome(
                action_id=binding.action_id,
                status=ShortcutRegistrationStatus.UNBOUND,
            )
        targets = self._parse_shortcut(binding.configured)
        parsed = self._binding_from_config(
            binding.action_id, binding.gesture, binding.configured
        )
        if (
            not targets
            or parsed.shortcut is None
            or self._binding_names(binding) != set(parsed.shortcut.keys)
        ):
            return ShortcutRegistrationOutcome(
                action_id=binding.action_id,
                status=ShortcutRegistrationStatus.FAILED,
                failure=ShortcutFailure.INVALID_BINDING,
            )
        for existing_id, registered in self._bindings.items():
            if (
                existing_id != binding.action_id
                and binding.gesture in {ShortcutGesture.HOLD, ShortcutGesture.TOGGLE}
                and registered.binding.gesture is binding.gesture
            ):
                return ShortcutRegistrationOutcome(
                    action_id=binding.action_id,
                    status=ShortcutRegistrationStatus.FAILED,
                    failure=ShortcutFailure.CONFLICT,
                )
        self._remove_binding(binding.action_id)
        self._bindings[binding.action_id] = _RegisteredBinding(
            binding=binding,
            targets=targets,
            callback=callback,
            state=ShortcutGestureState(binding.gesture),
        )
        try:
            self._ensure_listener()
        except Exception as exc:
            self._remove_binding(binding.action_id)
            return ShortcutRegistrationOutcome(
                action_id=binding.action_id,
                status=ShortcutRegistrationStatus.FAILED,
                failure=self._registration_failure(exc),
            )
        return ShortcutRegistrationOutcome(
            action_id=binding.action_id,
            status=ShortcutRegistrationStatus.REGISTERED,
        )

    @staticmethod
    def _raise_legacy_registration_failure(
        configured: str,
        outcome: ShortcutRegistrationOutcome,
    ) -> None:
        if outcome.status is not ShortcutRegistrationStatus.FAILED:
            return
        if outcome.failure is ShortcutFailure.INVALID_BINDING:
            raise ValueError(f"Could not parse shortcut: {configured!r}")
        if outcome.failure is ShortcutFailure.PERMISSION_DENIED:
            raise PermissionError("global shortcut registration denied")
        raise RuntimeError(f"global shortcut registration failed: {outcome.failure}")

    def register_push_to_talk(
        self,
        shortcut: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        binding = self._binding_from_config(
            "push-to-talk", ShortcutGesture.HOLD, shortcut
        )

        def callback(_action_id: str, action: ShortcutAction) -> None:
            if action is ShortcutAction.HOLD_BEGIN:
                on_press()
            elif action is ShortcutAction.HOLD_END:
                on_release()

        outcome = self.register(binding, callback)
        self._raise_legacy_registration_failure(shortcut, outcome)

    def register_toggle(
        self,
        shortcut: str,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
    ) -> None:
        binding = self._binding_from_config(
            "toggle-recording", ShortcutGesture.TOGGLE, shortcut
        )

        def callback(_action_id: str, action: ShortcutAction) -> None:
            if action is not ShortcutAction.TOGGLE:
                return
            if self._toggle_active:
                self._toggle_active = False
                on_stop()
            else:
                self._toggle_active = True
                on_start()

        outcome = self.register(binding, callback)
        self._raise_legacy_registration_failure(shortcut, outcome)
        self._toggle_active = False

    def reset_toggle_state(self) -> None:
        self._toggle_active = False

    def _register_tap(
        self,
        action_id: str,
        shortcut: str,
        callback: Callable[[], None],
    ) -> None:
        binding = self._binding_from_config(action_id, ShortcutGesture.TAP, shortcut)
        outcome = self.register(
            binding,
            lambda _action_id, action: callback()
            if action is ShortcutAction.FIRE
            else None,
        )
        self._raise_legacy_registration_failure(shortcut, outcome)

    def register_extra_hotkey(
        self,
        shortcut: str,
        on_press: Callable[[], None],
        *,
        key: str = "paste_last",
    ) -> None:
        self._register_tap(key, shortcut, on_press)

    def unregister_extra_hotkey(self, *, key: str | None = None) -> None:
        if key is None:
            for action_id in tuple(self._bindings):
                if action_id not in {
                    "push-to-talk",
                    "toggle-recording",
                } and not action_id.startswith("profile:"):
                    self._remove_binding(action_id)
            return
        self._remove_binding(key)

    def register_profile_hotkeys(
        self,
        hotkeys: dict[str, str],
        on_press: Callable[[str], None],
    ) -> None:
        self.unregister_profile_hotkeys()
        for profile_id, shortcut in hotkeys.items():
            if not shortcut.strip():
                continue
            try:
                self._register_tap(
                    f"profile:{profile_id}",
                    shortcut,
                    lambda profile=profile_id: on_press(profile),
                )
            except ValueError:
                self._logger.warning(
                    "profile hotkey registration skipped reason=unparseable profile=%s",
                    profile_id,
                )

    def unregister_profile_hotkeys(self) -> None:
        for action_id in tuple(self._bindings):
            if action_id.startswith("profile:"):
                self._remove_binding(action_id)

    def register_early_key_handler(
        self,
        key: str,
        predicate: Callable[[], bool],
        on_press: Callable[[], None],
    ) -> None:
        """Store a selective handler without starting the native event tap.

        The app installs these during ordinary hotkey registration. The normal
        binding/capture lifecycle remains responsible for listener startup.
        """

        name = _normalize_name(key)
        if name not in {"enter", "esc"}:
            raise ValueError("early key must be Enter or Escape")
        if not callable(predicate) or not callable(on_press):
            raise TypeError("early key handlers must be callable")
        self._early_handlers[name] = (predicate, on_press)

    @staticmethod
    def _targets_active(
        targets: tuple[frozenset[_KeyIdentity], ...],
        pressed: set[_KeyIdentity],
        *,
        allow_extra_modifiers: bool = False,
    ) -> bool:
        if not targets or not all(options & pressed for options in targets):
            return False
        if not allow_extra_modifiers:
            allowed_modifiers = {
                identity for options in targets for identity in options
                if identity[0] == "key" and identity[1] in _MODIFIER_CODES
            }
            if allowed_modifiers:
                held_modifiers = {
                    identity for identity in pressed
                    if identity[0] == "key" and identity[1] in _MODIFIER_CODES
                }
                if not held_modifiers <= allowed_modifiers:
                    return False
        return True

    def _dispatch_action(
        self,
        registered: _RegisteredBinding,
        action: ShortcutAction,
        *,
        allow_closed: bool = False,
    ) -> None:
        def invoke() -> None:
            if action is ShortcutAction.HOLD_BEGIN:
                registered.hold_begin_delivered = True
            elif action is ShortcutAction.HOLD_END:
                if not registered.hold_begin_delivered:
                    return
                registered.hold_begin_delivered = False
            registered.callback(registered.binding.action_id, action)

        self._queue_callback(
            invoke,
            allow_when_closed=allow_closed,
        )

    def _handle_listener_event(self, generation: object, event: MacKeyEvent) -> bool:
        with self._lock:
            if self._listener_generation is not generation:
                return False
            return self._handle_event(event)

    def _handle_event(self, event: MacKeyEvent) -> bool:
        if event.disabled_reason is not None:
            with self._lock:
                # A timed-out tap may already have let a claimed down through.
                # Keep its repeats and balancing up visible until full release.
                self._passthrough_until_release.update(self._early_suppressed)
                self._passthrough_until_release.update(self._standalone_suppressed)
                self._early_suppressed.clear()
                self._standalone_suppressed.clear()
            if event.reenabled:
                self._listener_reenable_count += 1
                self._last_listener_failure = None
            else:
                self._last_listener_failure = ShortcutFailure.LISTENER_DISABLED
                generation = self._listener_generation
                self._queue_callback(
                    lambda: self._disable_listener_after_failure(generation=generation)
                )
            return False
        assert event.identity is not None and event.transition is not None
        if self._closed or event.synthetic:
            return False
        with self._suppress_lock:
            if self._suppress_depth > 0:
                return False
        with self._lock:
            identity = event.identity
            name = _identity_name(identity)
            if name is None:
                return False

            if event.transition is ShortcutTransition.RELEASED:
                self._pressed.discard(identity)
            else:
                self._pressed.add(identity)

            passthrough = identity in self._passthrough_until_release
            if event.transition is ShortcutTransition.RELEASED:
                self._passthrough_until_release.discard(identity)

            if self._mode == "capture":
                if event.transition is ShortcutTransition.PRESSED:
                    self._capture_names.add(name)
                    for modifier in event.modifiers:
                        family = _CAPTURE_MODIFIER_FAMILIES[modifier]
                        if not self._capture_names.intersection(family):
                            self._capture_names.add(modifier)
                    self._capture_identities.add(identity)
                    combo = "+".join(_ordered_names(self._capture_names))
                    callback = self._capture_on_changed
                    if callback is not None:
                        self._queue_callback(lambda value=combo, cb=callback: cb(value))
                else:
                    self._capture_identities.discard(identity)
                    if (
                        self._capture_names
                        and not self._capture_identities
                        and not event.modifiers
                    ):
                        combo = "+".join(_ordered_names(self._capture_names))
                        self._capture_active = False
                        self._mode = "capture-pending"
                        self._queue_callback(lambda value=combo: self._complete_capture(value))
                return False
            if self._mode != "none":
                return False

            early = self._early_handlers.get(name)
            if early is not None and not passthrough:
                if event.transition is ShortcutTransition.RELEASED:
                    if identity in self._early_suppressed:
                        self._early_suppressed.discard(identity)
                        return True
                elif identity not in self._early_suppressed:
                    predicate, callback = early
                    try:
                        # A modified Enter/Escape is never a routed reply key
                        # (Shift+Return is a newline). The HUD's own text box
                        # owns IME composition while it has focus.
                        should_consume = not event.modifiers and bool(predicate())
                    except Exception:
                        should_consume = False
                    if should_consume:
                        self._early_suppressed.add(identity)
                        self._queue_callback(callback)
                        return True
                elif identity in self._early_suppressed:
                    return True

            bindings = tuple(self._bindings.values())
            for registered in bindings:
                active = self._targets_active(
                    registered.targets,
                    self._pressed,
                    # Extra modifiers cannot initiate a chord, but cannot
                    # cancel or rearm a gesture that already owns its press.
                    allow_extra_modifiers=registered.state.active,
                )
                if event.transition is ShortcutTransition.PRESSED and active:
                    for action in registered.state.apply(ShortcutTransition.PRESSED):
                        self._dispatch_action(registered, action)
                elif event.transition is ShortcutTransition.RELEASED and not active:
                    for action in registered.state.apply(ShortcutTransition.RELEASED):
                        self._dispatch_action(registered, action)

            if passthrough:
                return False
            if event.transition is ShortcutTransition.RELEASED:
                if identity in self._standalone_suppressed:
                    self._standalone_suppressed.discard(identity)
                    return True
                return False
            if identity in self._standalone_suppressed:
                return True
            if self._is_suppressible_recording_identity(identity):
                self._standalone_suppressed.add(identity)
                return True
            return False

    def _is_suppressible_recording_identity(self, identity: _KeyIdentity) -> bool:
        kind, code = identity
        if kind != "key" or code not in _SUPPRESSIBLE_STANDALONE_CODES:
            return False
        for registered in self._bindings.values():
            if (
                registered.binding.gesture in {
                    ShortcutGesture.HOLD,
                    ShortcutGesture.TOGGLE,
                }
                and len(registered.targets) == 1
                and identity in registered.targets[0]
                and registered.state.active
            ):
                return True
        return False

    def _disable_listener_after_failure(self, *, generation: object | None = None) -> None:
        with self._listener_lifecycle_lock:
            with self._lock:
                if generation is not None and self._listener_generation is not generation:
                    return
            self._stop_listener()
            with self._lock:
                for registered in self._bindings.values():
                    for action in registered.state.reset():
                        self._dispatch_action(registered, action)

    def capture_shortcut(
        self,
        on_keys_changed: Callable[[str], None],
        on_captured: Callable[[str], None],
    ) -> None:
        if self._capture_active:
            return
        self._begin_capture(on_keys_changed, on_captured, None)

    def begin_capture(self, callback: ShortcutCaptureCallback) -> None:
        if not callable(callback):
            raise TypeError("capture callback must be callable")
        if self._capture_active or self._mode != "none":
            self._queue_callback(
                lambda cb=callback: cb(
                    ShortcutCaptureOutcome(
                        status=ShortcutCaptureStatus.FAILED,
                        failure=ShortcutFailure.CONFLICT,
                    )
                )
            )
            return
        self._begin_capture(None, None, callback)

    def _begin_capture(
        self,
        on_keys_changed: Callable[[str], None] | None,
        on_captured: Callable[[str], None] | None,
        contract_callback: ShortcutCaptureCallback | None,
    ) -> None:
        if self._closed:
            self._last_listener_failure = ShortcutFailure.LISTENER_DISABLED
            raise _ListenerDisabledError("shortcut adapter is closed")
        for registered in tuple(self._bindings.values()):
            for action in registered.state.reset():
                self._dispatch_action(registered, action)
        self._stop_listener()
        self._capture_active = True
        self._capture_names.clear()
        self._capture_identities.clear()
        self._capture_on_changed = on_keys_changed
        self._capture_on_captured = on_captured
        self._capture_contract_callback = contract_callback
        self._mode = "capture"
        try:
            self._start_listener()
        except Exception as exc:
            self._last_listener_failure = self._registration_failure(exc)
            self._capture_active = False
            self._mode = "none"
            self._capture_on_changed = None
            self._capture_on_captured = None
            self._capture_contract_callback = None
            if contract_callback is not None:
                failure = self._registration_failure(exc)
                self._queue_callback(
                    lambda cb=contract_callback, value=failure: cb(
                        ShortcutCaptureOutcome(
                            status=ShortcutCaptureStatus.FAILED,
                            failure=value,
                        )
                    )
                )
                return
            raise

    def _complete_capture(self, configured: str) -> None:
        legacy_callback = self._capture_on_captured
        contract_callback = self._capture_contract_callback
        self._capture_on_changed = None
        self._capture_on_captured = None
        self._capture_contract_callback = None
        self._capture_names.clear()
        self._capture_identities.clear()
        self._stop_listener()
        self._mode = "none"
        try:
            self._ensure_listener()
        except Exception as exc:
            self._last_listener_failure = self._registration_failure(exc)
            if legacy_callback is not None:
                legacy_callback(configured)
            if contract_callback is not None:
                contract_callback(
                    ShortcutCaptureOutcome(
                        status=ShortcutCaptureStatus.FAILED,
                        failure=ShortcutFailure.CLEANUP_FAILED,
                    )
                )
            return
        if contract_callback is not None:
            binding = self._binding_from_config(
                "captured", ShortcutGesture.TAP, configured
            )
            if binding.shortcut is None:
                contract_callback(
                    ShortcutCaptureOutcome(
                        status=ShortcutCaptureStatus.FAILED,
                        failure=ShortcutFailure.INVALID_BINDING,
                    )
                )
            else:
                contract_callback(
                    ShortcutCaptureOutcome(
                        status=ShortcutCaptureStatus.CAPTURED,
                        shortcut=binding.shortcut,
                    )
                )
        if legacy_callback is not None:
            legacy_callback(configured)

    def stop_capture(self, *, timeout: bool = False) -> None:
        del timeout
        was_capturing = self._capture_active or self._mode.startswith("capture")
        callback = self._capture_contract_callback
        self._capture_active = False
        self._mode = "none"
        self._capture_names.clear()
        self._capture_identities.clear()
        self._capture_on_changed = None
        self._capture_on_captured = None
        self._capture_contract_callback = None
        self._stop_listener()
        try:
            self._ensure_listener()
        except Exception:
            if callback is not None:
                self._queue_callback(
                    lambda cb=callback: cb(
                        ShortcutCaptureOutcome(
                            status=ShortcutCaptureStatus.FAILED,
                            failure=ShortcutFailure.CLEANUP_FAILED,
                        )
                    )
                )
            return
        if was_capturing and callback is not None:
            self._queue_callback(
                lambda cb=callback: cb(
                    ShortcutCaptureOutcome(status=ShortcutCaptureStatus.CANCELLED)
                )
            )

    def cancel_capture(self) -> None:
        self.stop_capture()

    def unhook_all(self) -> None:
        self._stop_listener()
        for action_id in tuple(self._bindings):
            self._remove_binding(action_id, allow_closed=self._closed)
        self._toggle_active = False
        self._capture_active = False
        self._mode = "none"
        self._capture_names.clear()
        self._capture_identities.clear()
        self._capture_on_changed = None
        self._capture_on_captured = None
        self._capture_contract_callback = None
        self._pressed.clear()

    @contextmanager
    def suppress_self_events(self) -> Iterator[None]:
        with self._suppress_lock:
            self._suppress_depth += 1
        try:
            yield
        finally:
            timer: threading.Timer | None = None

            def release() -> None:
                try:
                    self._suppress_release_one()
                finally:
                    if timer is not None:
                        self._suppress_timers.discard(timer)

            timer = self._suppress_timer_factory(_SUPPRESS_GRACE_S, release)
            timer.daemon = True
            self._suppress_timers.add(timer)
            timer.start()

    def _suppress_release_one(self) -> None:
        # Native listener callbacks already own _lock before checking suppression.
        with self._lock, self._suppress_lock:
            if self._suppress_depth > 0:
                self._suppress_depth -= 1
            if self._suppress_depth != 0 or self._closed:
                return
            # Keep expiry serialized with a new injection entering suppression.
            self._reconcile_suppressed_releases()
            self._reconcile_modifiers()

    def _reconcile_suppressed_releases(self) -> None:
        """Rearm gestures whose physical release was hidden by injected input.

        Reconcile only keys already observed before suppression. Never synthesize
        a press, release a physically held key, or infer an unknown key state.
        """
        with self._lock:
            if self._closed or self._mode != "none":
                return
            released: set[_KeyIdentity] = set()
            for identity in self._pressed:
                if identity[0] != "key":
                    continue
                try:
                    down = bool(self._physical_key_state(identity[1]))
                except Exception:
                    continue
                if not down:
                    released.add(identity)
            self._pressed.difference_update(released)
            self._early_suppressed.difference_update(released)
            self._standalone_suppressed.difference_update(released)
            self._passthrough_until_release.difference_update(released)
            for registered in self._bindings.values():
                if registered.state.active and not self._targets_active(
                    registered.targets, self._pressed, allow_extra_modifiers=True
                ):
                    for action in registered.state.apply(ShortcutTransition.RELEASED):
                        self._dispatch_action(registered, action)

    def _reconcile_modifiers(self) -> None:
        with self._lock:
            self._pressed.difference_update(
                identity
                for identity in self._pressed
                if identity[0] == "key" and identity[1] in _MODIFIER_CODES
            )
            for code in _MODIFIER_CODES:
                try:
                    down = bool(self._physical_key_state(code))
                except Exception:
                    down = False
                if down:
                    self._pressed.add(("key", code))

    def snapshot_held_modifiers(self) -> list[str]:
        held: list[str] = []
        for code in sorted(_MODIFIER_CODES):
            try:
                down = bool(self._physical_key_state(code))
            except Exception:
                down = False
            if down:
                name = _CODE_TO_NAME.get(code)
                if name is not None:
                    held.append(name)
        return held

    def modifier_physically_down(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        name = _normalize_name(key)
        if name is None or name not in _NAME_TO_CODES:
            return False
        try:
            return any(self._physical_key_state(code) for code in _NAME_TO_CODES[name])
        except Exception:
            return False

    def send(self, _combination: str) -> None:
        raise RuntimeError("macOS shortcut adapter does not own text delivery")

    def type_text(self, _text: str, *, delay_ms: int = 0) -> None:
        del delay_ms
        raise RuntimeError("macOS shortcut adapter does not own text delivery")

    def wait(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            self._wait_event.wait()
            return
        from .macos_native_query_owner import service_main_queries
        while not self._wait_event.wait(0.01):
            service_main_queries()

    def close(self) -> None:
        with self._close_lock:
            already_closed = self._closed
            self._closed = True
        if already_closed:
            # Closing remains idempotent, but a previous bounded teardown may
            # have retained a tap that now permits its exact cleanup to finish.
            self._stop_listener()
            return
        for timer in tuple(self._suppress_timers):
            timer.cancel()
        self._suppress_timers.clear()
        with self._suppress_lock:
            self._suppress_depth = 0
        self.unhook_all()
        self._queue.put(_DISPATCH_STOP)
        if self._worker is not threading.current_thread():
            self._worker.join(_LISTENER_JOIN_TIMEOUT_S)
            if self._worker.is_alive():
                self._last_cleanup_failure = ShortcutFailure.CLEANUP_FAILED
                self._logger.warning("hotkey dispatch cleanup failed")
        self._wait_event.set()


KeyboardShortcutManager = MacosShortcutAdapter


__all__ = [
    "KeyboardShortcutManager",
    "MacKeyEvent",
    "MacosShortcutAdapter",
    "macos_shortcut_glyphs",
]
