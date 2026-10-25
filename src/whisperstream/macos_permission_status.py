"""Query-only macOS permission capability mapping.

Native frameworks are loaded only after an explicit status query. This module
contains no permission-request operation and does not open capture devices,
create event taps, observe input, or navigate to System Settings.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
import threading
from collections.abc import Callable
from typing import Protocol

from .permission_contract import (
    PermissionKind,
    PermissionReport,
    PermissionSnapshot,
    PermissionState,
)


MACOS_PERMISSION_API_MINIMUMS = {
    PermissionKind.MICROPHONE: (10, 14),
    PermissionKind.ACCESSIBILITY: (10, 9),
    PermissionKind.INPUT_MONITORING: (10, 15),
}

_run_loop_state = threading.local()


_MICROPHONE_STATE_MAP = {
    0: PermissionState.NOT_DETERMINED,
    1: PermissionState.RESTRICTED,
    2: PermissionState.DENIED,
    3: PermissionState.GRANTED,
}


class _MacosPermissionBridge(Protocol):
    def microphone_authorization_status(self) -> int: ...

    def accessibility_is_trusted(self) -> bool: ...

    def input_monitoring_is_trusted(self) -> bool: ...


class _NativeMacosPermissionBridge:
    """Minimal in-process bindings for three status/preflight calls."""

    def __init__(self) -> None:
        self._libraries: dict[str, ctypes.CDLL] = {}

    def _library(self, name: str) -> ctypes.CDLL:
        cached = self._libraries.get(name)
        if cached is not None:
            return cached
        path = ctypes.util.find_library(name)
        if not path:
            raise RuntimeError("native permission framework unavailable")
        library = ctypes.CDLL(path)
        self._libraries[name] = library
        return library

    def microphone_authorization_status(self) -> int:
        avfoundation = self._library("AVFoundation")
        objc = self._library("objc")

        objc_get_class = objc.objc_getClass
        objc_get_class.argtypes = [ctypes.c_char_p]
        objc_get_class.restype = ctypes.c_void_p
        capture_device = objc_get_class(b"AVCaptureDevice")

        selector_register = objc.sel_registerName
        selector_register.argtypes = [ctypes.c_char_p]
        selector_register.restype = ctypes.c_void_p
        selector = selector_register(b"authorizationStatusForMediaType:")

        media_type = ctypes.c_void_p.in_dll(avfoundation, "AVMediaTypeAudio").value
        if not capture_device or not selector or not media_type:
            raise RuntimeError("native microphone status symbol unavailable")

        message_send = objc.objc_msgSend
        message_send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        message_send.restype = ctypes.c_long
        return int(message_send(capture_device, selector, media_type))

    def _drain_main_run_loop(self) -> None:
        # The backend's asyncio loop does not service CFRunLoop notifications.
        # AX trust can therefore stay stale after a Settings change until this
        # process restarts. The status endpoint runs on the Python main thread;
        # pump pending native work there before reading trust. Worker queries
        # must never try to service another thread's run loop.
        if threading.current_thread() is not threading.main_thread():
            return
        if getattr(_run_loop_state, "draining", False):
            return
        cf = self._library("CoreFoundation")
        run = cf.CFRunLoopRunInMode
        run.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_ubyte]
        run.restype = ctypes.c_int32
        mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode").value
        if not mode:
            raise RuntimeError("native run loop mode unavailable")
        _run_loop_state.draining = True
        try:
            # A bounded slice, including queued main-dispatch work. Keep this
            # separate from the shortcut listener's dedicated native run loop.
            run(mode, 0.001, False)
        finally:
            _run_loop_state.draining = False

    def accessibility_is_trusted(self) -> bool:
        application_services = self._library("ApplicationServices")
        query = application_services.AXIsProcessTrustedWithOptions
        query.argtypes = [ctypes.c_void_p]
        query.restype = ctypes.c_ubyte
        self._drain_main_run_loop()
        # NULL means no options. In particular, no prompt option is supplied.
        return bool(query(None))

    def input_monitoring_is_trusted(self) -> bool:
        core_graphics = self._library("CoreGraphics")
        preflight = core_graphics.CGPreflightListenEventAccess
        preflight.argtypes = []
        preflight.restype = ctypes.c_bool
        return bool(preflight())


def _runtime_macos_version() -> tuple[int, int] | None:
    raw = platform.mac_ver()[0]
    try:
        major, minor, *_rest = (int(part) for part in raw.split("."))
    except (TypeError, ValueError):
        return None
    return major, minor


def _snapshot(
    *,
    kind: PermissionKind,
    minimum_macos: tuple[int, int],
    runtime_macos: tuple[int, int] | None,
    query: Callable[[], object],
    map_state: Callable[[object], PermissionState],
    request_supported: bool,
) -> PermissionSnapshot:
    if runtime_macos is not None and runtime_macos < minimum_macos:
        return PermissionSnapshot(
            kind=kind,
            state=PermissionState.UNSUPPORTED,
            query_supported=False,
            request_supported=False,
        )
    try:
        raw_state = query()
    except Exception:  # noqa: BLE001 - native status failure is content-free
        return PermissionSnapshot(
            kind=kind,
            state=PermissionState.UNKNOWN,
            query_supported=False,
            request_supported=False,
        )
    return PermissionSnapshot(
        kind=kind,
        state=map_state(raw_state),
        query_supported=True,
        # Capability metadata only. The prompt-capable implementation remains
        # in the separately loaded explicit-action adapter.
        request_supported=request_supported,
    )


def _microphone_state(raw_state: object) -> PermissionState:
    if type(raw_state) is not int:  # noqa: E721 - bool is not a native enum
        return PermissionState.UNKNOWN
    return _MICROPHONE_STATE_MAP.get(raw_state, PermissionState.UNKNOWN)


def _boolean_preflight_state(raw_state: object) -> PermissionState:
    if raw_state is True:
        return PermissionState.GRANTED
    # These APIs expose no denial/not-determined/restricted distinction.
    return PermissionState.UNKNOWN


class MacosPermissionStatusProvider:
    """Report current macOS capability state without requesting access."""

    def __init__(
        self,
        *,
        bridge_factory: Callable[[], _MacosPermissionBridge] | None = None,
        runtime_macos: tuple[int, int] | None = None,
    ) -> None:
        self._bridge_factory = bridge_factory or _NativeMacosPermissionBridge
        self._runtime_macos = (
            _runtime_macos_version() if runtime_macos is None else runtime_macos
        )
        self._bridge: _MacosPermissionBridge | None = None

    def _native_bridge(self) -> _MacosPermissionBridge:
        if self._bridge is None:
            self._bridge = self._bridge_factory()
        return self._bridge

    def query_permissions(self) -> PermissionReport:
        if platform.system() != "Darwin":
            raise RuntimeError("macOS permission adapter unavailable")

        return PermissionReport(
            platform="macos",
            permissions=(
                _snapshot(
                    kind=PermissionKind.MICROPHONE,
                    minimum_macos=MACOS_PERMISSION_API_MINIMUMS[
                        PermissionKind.MICROPHONE
                    ],
                    runtime_macos=self._runtime_macos,
                    query=lambda: self._native_bridge().microphone_authorization_status(),
                    map_state=_microphone_state,
                    request_supported=True,
                ),
                _snapshot(
                    kind=PermissionKind.ACCESSIBILITY,
                    minimum_macos=MACOS_PERMISSION_API_MINIMUMS[
                        PermissionKind.ACCESSIBILITY
                    ],
                    runtime_macos=self._runtime_macos,
                    query=lambda: self._native_bridge().accessibility_is_trusted(),
                    map_state=_boolean_preflight_state,
                    request_supported=True,
                ),
                _snapshot(
                    kind=PermissionKind.INPUT_MONITORING,
                    minimum_macos=MACOS_PERMISSION_API_MINIMUMS[
                        PermissionKind.INPUT_MONITORING
                    ],
                    runtime_macos=self._runtime_macos,
                    query=lambda: self._native_bridge().input_monitoring_is_trusted(),
                    map_state=_boolean_preflight_state,
                    request_supported=True,
                ),
            ),
        )


__all__ = ["MACOS_PERMISSION_API_MINIMUMS", "MacosPermissionStatusProvider"]
