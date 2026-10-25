"""Explicit macOS permission request and System Settings recovery adapters.

This module is loaded only after an explicit backend action.  Its native bridge
contains prompt-capable calls, so passive status, startup, hydration, and device
enumeration must never import or construct it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
import threading
from collections.abc import Callable
from typing import Protocol

from .permission_actions import PermissionActionProviders
from .permission_contract import (
    PermissionAction,
    PermissionActionFailure,
    PermissionActionOutcome,
    PermissionActionStatus,
    PermissionIntent,
    PermissionIntentRegistry,
    PermissionIntentUse,
    PermissionKind,
    PermissionState,
)
from .macos_permission_status import (
    MACOS_PERMISSION_API_MINIMUMS,
    MacosPermissionStatusProvider,
    _runtime_macos_version,
)


MICROPHONE_REQUEST_TIMEOUT_SECONDS = 120.0
SYSTEM_SETTINGS_BUNDLE_IDENTIFIER = "com.apple.systempreferences"
MACOS_SYSTEM_SETTINGS_API_MINIMUM = (10, 6)


class _MacosPermissionActionBridge(Protocol):
    def request_microphone_access(self) -> bool: ...

    def request_accessibility_access(self) -> bool: ...

    def request_input_monitoring_access(self) -> bool: ...

    def open_system_settings(self) -> bool: ...


class _BlockDescriptor(ctypes.Structure):
    _fields_ = [
        ("reserved", ctypes.c_ulong),
        ("size", ctypes.c_ulong),
        ("signature", ctypes.c_char_p),
    ]


class _BlockLiteral(ctypes.Structure):
    _fields_ = [
        ("isa", ctypes.c_void_p),
        ("flags", ctypes.c_int32),
        ("reserved", ctypes.c_int32),
        ("invoke", ctypes.c_void_p),
        ("descriptor", ctypes.POINTER(_BlockDescriptor)),
    ]


_BlockInvoke = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool)
_BLOCK_IS_GLOBAL = 1 << 28
_BLOCK_HAS_SIGNATURE = 1 << 30


class _NativeMacosPermissionActionBridge:
    """Minimal ctypes bindings for explicit prompt and recovery operations."""

    def __init__(self) -> None:
        self._libraries: dict[str, ctypes.CDLL] = {}
        # A timed-out microphone prompt may complete later. Retain its Python
        # callback/block storage for the process lifetime so native code never
        # calls a released function pointer.
        self._retained_blocks: list[tuple[object, ...]] = []

    def _library(self, name: str) -> ctypes.CDLL:
        cached = self._libraries.get(name)
        if cached is not None:
            return cached
        path = ctypes.util.find_library(name)
        if not path:
            raise RuntimeError("native permission action framework unavailable")
        library = ctypes.CDLL(path)
        self._libraries[name] = library
        return library

    def _objc_class(self, name: bytes) -> int:
        objc = self._library("objc")
        get_class = objc.objc_getClass
        get_class.argtypes = [ctypes.c_char_p]
        get_class.restype = ctypes.c_void_p
        value = int(get_class(name) or 0)
        if not value:
            raise RuntimeError("native permission action class unavailable")
        return value

    def _selector(self, name: bytes) -> int:
        objc = self._library("objc")
        register = objc.sel_registerName
        register.argtypes = [ctypes.c_char_p]
        register.restype = ctypes.c_void_p
        value = int(register(name) or 0)
        if not value:
            raise RuntimeError("native permission action selector unavailable")
        return value

    def _send(
        self,
        restype,
        argtypes: tuple[type[ctypes._SimpleCData], ...],
        *args,
    ):
        objc = self._library("objc")
        function_type = ctypes.CFUNCTYPE(restype, *argtypes)
        sender = ctypes.cast(objc.objc_msgSend, function_type)
        return sender(*args)

    def _class_responds(self, class_name: bytes, selector_name: bytes) -> bool:
        return self._object_responds(
            self._objc_class(class_name), selector_name
        )

    def _object_responds(self, object_pointer: int, selector_name: bytes) -> bool:
        target = self._selector(selector_name)
        responds = self._selector(b"respondsToSelector:")
        return bool(
            self._send(
                ctypes.c_bool,
                (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p),
                object_pointer,
                responds,
                target,
            )
        )

    def probe_support(self) -> dict[str, bool]:
        """Resolve real symbols/selectors without invoking any action API."""

        if platform.system() != "Darwin":
            raise RuntimeError("macOS permission action adapter unavailable")
        application_services = self._library("ApplicationServices")
        core_foundation = self._library("CoreFoundation")
        core_graphics = self._library("CoreGraphics")
        avfoundation = self._library("AVFoundation")
        system = self._library("System")
        self._library("AppKit")
        workspace_class = self._objc_class(b"NSWorkspace")
        workspace = self._send(
            ctypes.c_void_p,
            (ctypes.c_void_p, ctypes.c_void_p),
            workspace_class,
            self._selector(b"sharedWorkspace"),
        )
        return {
            "microphone_request": bool(
                ctypes.c_void_p.in_dll(avfoundation, "AVMediaTypeAudio").value
                and ctypes.c_void_p.in_dll(system, "_NSConcreteGlobalBlock").value
                and self._class_responds(
                    b"AVCaptureDevice",
                    b"requestAccessForMediaType:completionHandler:",
                )
            ),
            "accessibility_request": bool(
                hasattr(application_services, "AXIsProcessTrustedWithOptions")
                and ctypes.c_void_p.in_dll(
                    application_services, "kAXTrustedCheckOptionPrompt"
                ).value
                and ctypes.c_void_p.in_dll(
                    core_foundation, "kCFBooleanTrue"
                ).value
                and hasattr(core_foundation, "CFDictionaryCreateMutable")
                and hasattr(core_foundation, "CFDictionarySetValue")
            ),
            "input_monitoring_request": hasattr(
                core_graphics, "CGRequestListenEventAccess"
            ),
            "system_settings_recovery": bool(
                workspace
                and self._class_responds(
                    b"NSWorkspace", b"sharedWorkspace"
                )
                and self._class_responds(
                    b"NSString", b"stringWithUTF8String:"
                )
                and self._object_responds(
                    workspace, b"URLForApplicationWithBundleIdentifier:"
                )
                and self._object_responds(workspace, b"openURL:")
            ),
        }

    def request_microphone_access(self) -> bool:
        avfoundation = self._library("AVFoundation")
        system = self._library("System")
        capture_device = self._objc_class(b"AVCaptureDevice")
        selector = self._selector(
            b"requestAccessForMediaType:completionHandler:"
        )
        media_type = ctypes.c_void_p.in_dll(
            avfoundation, "AVMediaTypeAudio"
        ).value
        block_isa = ctypes.c_void_p.in_dll(
            system, "_NSConcreteGlobalBlock"
        ).value
        if not media_type or not block_isa:
            raise RuntimeError("native microphone request symbol unavailable")

        finished = threading.Event()
        result: dict[str, bool] = {}

        @_BlockInvoke
        def completion(_block, granted) -> None:
            result["granted"] = bool(granted)
            finished.set()

        descriptor = _BlockDescriptor(
            reserved=0,
            size=ctypes.sizeof(_BlockLiteral),
            signature=b"v@?B",
        )
        block = _BlockLiteral(
            isa=block_isa,
            flags=_BLOCK_IS_GLOBAL | _BLOCK_HAS_SIGNATURE,
            reserved=0,
            invoke=ctypes.cast(completion, ctypes.c_void_p).value,
            descriptor=ctypes.pointer(descriptor),
        )
        retained = (completion, descriptor, block)
        self._retained_blocks.append(retained)
        self._send(
            None,
            (
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ),
            capture_device,
            selector,
            media_type,
            ctypes.addressof(block),
        )
        if not finished.wait(MICROPHONE_REQUEST_TIMEOUT_SECONDS):
            raise RuntimeError("native microphone request timed out")
        # The system invokes this completion exactly once. Removing the entry
        # after completion prevents unbounded retention on normal user actions.
        self._retained_blocks.remove(retained)
        return result["granted"]

    def request_accessibility_access(self) -> bool:
        application_services = self._library("ApplicationServices")
        core_foundation = self._library("CoreFoundation")
        prompt_key = ctypes.c_void_p.in_dll(
            application_services, "kAXTrustedCheckOptionPrompt"
        ).value
        true_value = ctypes.c_void_p.in_dll(
            core_foundation, "kCFBooleanTrue"
        ).value
        if not prompt_key or not true_value:
            raise RuntimeError("native accessibility request symbol unavailable")

        create = core_foundation.CFDictionaryCreateMutable
        create.argtypes = [
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        create.restype = ctypes.c_void_p
        set_value = core_foundation.CFDictionarySetValue
        set_value.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        set_value.restype = None
        key_callbacks = ctypes.addressof(
            ctypes.c_byte.in_dll(
                core_foundation, "kCFTypeDictionaryKeyCallBacks"
            )
        )
        value_callbacks = ctypes.addressof(
            ctypes.c_byte.in_dll(
                core_foundation, "kCFTypeDictionaryValueCallBacks"
            )
        )
        options = create(None, 0, key_callbacks, value_callbacks)
        if not options:
            raise RuntimeError("native accessibility options unavailable")
        try:
            set_value(options, prompt_key, true_value)
            request = application_services.AXIsProcessTrustedWithOptions
            request.argtypes = [ctypes.c_void_p]
            request.restype = ctypes.c_ubyte
            return bool(request(options))
        finally:
            release = core_foundation.CFRelease
            release.argtypes = [ctypes.c_void_p]
            release.restype = None
            release(options)

    def request_input_monitoring_access(self) -> bool:
        core_graphics = self._library("CoreGraphics")
        request = core_graphics.CGRequestListenEventAccess
        request.argtypes = []
        request.restype = ctypes.c_bool
        return bool(request())

    def open_system_settings(self) -> bool:
        self._library("AppKit")
        workspace_class = self._objc_class(b"NSWorkspace")
        shared_selector = self._selector(b"sharedWorkspace")
        workspace = self._send(
            ctypes.c_void_p,
            (ctypes.c_void_p, ctypes.c_void_p),
            workspace_class,
            shared_selector,
        )
        string_class = self._objc_class(b"NSString")
        identifier = self._send(
            ctypes.c_void_p,
            (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p),
            string_class,
            self._selector(b"stringWithUTF8String:"),
            SYSTEM_SETTINGS_BUNDLE_IDENTIFIER.encode("ascii"),
        )
        application_url = self._send(
            ctypes.c_void_p,
            (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p),
            workspace,
            self._selector(b"URLForApplicationWithBundleIdentifier:"),
            identifier,
        )
        if not workspace or not identifier or not application_url:
            return False
        return bool(
            self._send(
                ctypes.c_bool,
                (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p),
                workspace,
                self._selector(b"openURL:"),
                application_url,
            )
        )


def _invalid_intent_outcome(
    intent: PermissionIntent,
    use: PermissionIntentUse,
    expected_action: PermissionAction,
) -> PermissionActionOutcome | None:
    if use is PermissionIntentUse.ACCEPTED and intent.action is expected_action:
        return None
    failure = (
        PermissionActionFailure.INTENT_REUSED
        if use is PermissionIntentUse.REUSED
        else PermissionActionFailure.INTENT_REQUIRED
    )
    return PermissionActionOutcome(
        kind=intent.kind,
        action=expected_action,
        status=PermissionActionStatus.FAILED,
        before=PermissionState.UNKNOWN,
        after=PermissionState.UNKNOWN,
        failure=failure,
    )


def _state_for_kind(kind: PermissionKind) -> PermissionState:
    report = MacosPermissionStatusProvider().query_permissions()
    return next(item.state for item in report.permissions if item.kind is kind)


def _unsupported_outcome(
    kind: PermissionKind,
    action: PermissionAction,
    state: PermissionState,
) -> PermissionActionOutcome:
    failure = (
        PermissionActionFailure.NOT_REQUESTABLE
        if action is PermissionAction.REQUEST
        else PermissionActionFailure.RECOVERY_UNAVAILABLE
    )
    return PermissionActionOutcome(
        kind=kind,
        action=action,
        status=PermissionActionStatus.UNSUPPORTED,
        before=state,
        after=state,
        failure=failure,
    )


class MacosPermissionRequester:
    """Consume one request intent and invoke at most one prompt per capability."""

    def __init__(
        self,
        registry: PermissionIntentRegistry,
        *,
        state_query: Callable[[PermissionKind], PermissionState] = _state_for_kind,
        bridge_factory: Callable[[], _MacosPermissionActionBridge]
        | None = None,
        runtime_macos: tuple[int, int] | None = None,
        attempted: set[PermissionKind] | None = None,
        attempt_lock: threading.Lock | None = None,
    ) -> None:
        self._registry = registry
        self._state_query = state_query
        self._bridge_factory = bridge_factory or _NativeMacosPermissionActionBridge
        self._runtime_macos = (
            _runtime_macos_version() if runtime_macos is None else runtime_macos
        )
        self._bridge: _MacosPermissionActionBridge | None = None
        self._attempted = attempted if attempted is not None else set()
        self._attempt_lock = attempt_lock or threading.Lock()

    def _native_bridge(self) -> _MacosPermissionActionBridge:
        if self._bridge is None:
            self._bridge = self._bridge_factory()
        return self._bridge

    def request_permission(
        self, intent: PermissionIntent
    ) -> PermissionActionOutcome:
        use = self._registry.consume(intent)
        invalid = _invalid_intent_outcome(
            intent, use, PermissionAction.REQUEST
        )
        if invalid is not None:
            return invalid
        if platform.system() != "Darwin":
            return _unsupported_outcome(
                intent.kind, PermissionAction.REQUEST, PermissionState.UNSUPPORTED
            )
        minimum = MACOS_PERMISSION_API_MINIMUMS[intent.kind]
        if self._runtime_macos is not None and self._runtime_macos < minimum:
            return _unsupported_outcome(
                intent.kind, PermissionAction.REQUEST, PermissionState.UNSUPPORTED
            )

        try:
            before = self._state_query(intent.kind)
        except Exception:  # noqa: BLE001 - native state remains content-free
            before = PermissionState.UNKNOWN
        if before is PermissionState.GRANTED:
            return PermissionActionOutcome(
                kind=intent.kind,
                action=PermissionAction.REQUEST,
                status=PermissionActionStatus.UNCHANGED,
                before=before,
                after=before,
            )
        if before is PermissionState.DENIED:
            return PermissionActionOutcome(
                kind=intent.kind,
                action=PermissionAction.REQUEST,
                status=PermissionActionStatus.DECLINED,
                before=before,
                after=before,
            )
        if before in {
            PermissionState.RESTRICTED,
            PermissionState.NOT_REQUIRED,
            PermissionState.UNSUPPORTED,
        }:
            return _unsupported_outcome(
                intent.kind, PermissionAction.REQUEST, before
            )

        with self._attempt_lock:
            if intent.kind in self._attempted:
                return PermissionActionOutcome(
                    kind=intent.kind,
                    action=PermissionAction.REQUEST,
                    status=PermissionActionStatus.UNCHANGED,
                    before=before,
                    after=before,
                )
            self._attempted.add(intent.kind)

        try:
            bridge = self._native_bridge()
            if intent.kind is PermissionKind.MICROPHONE:
                native_granted = bridge.request_microphone_access()
            elif intent.kind is PermissionKind.ACCESSIBILITY:
                native_granted = bridge.request_accessibility_access()
            else:
                native_granted = bridge.request_input_monitoring_access()
            after = self._state_query(intent.kind)
        except Exception:  # noqa: BLE001 - native failures never expose detail
            return PermissionActionOutcome(
                kind=intent.kind,
                action=PermissionAction.REQUEST,
                status=PermissionActionStatus.FAILED,
                before=before,
                after=PermissionState.UNKNOWN,
                failure=PermissionActionFailure.NATIVE_ERROR,
            )

        if native_granted and after in {
            PermissionState.UNKNOWN,
            PermissionState.NOT_DETERMINED,
        }:
            after = PermissionState.GRANTED
        elif (
            intent.kind is PermissionKind.MICROPHONE
            and not native_granted
            and after is PermissionState.NOT_DETERMINED
        ):
            # AVFoundation's completion Boolean explicitly distinguishes grant
            # from denial even if the immediate status read has not settled.
            after = PermissionState.DENIED

        if after is PermissionState.GRANTED:
            status = PermissionActionStatus.COMPLETED
        elif after is PermissionState.DENIED:
            status = PermissionActionStatus.DECLINED
        else:
            # Accessibility and Input Monitoring Boolean false values do not
            # distinguish denial from a pending/not-yet-reflected decision.
            status = PermissionActionStatus.UNCHANGED
        return PermissionActionOutcome(
            kind=intent.kind,
            action=PermissionAction.REQUEST,
            status=status,
            before=before,
            after=after,
        )


class MacosPermissionRecoveryProvider:
    """Open top-level System Settings after one explicit recovery intent."""

    def __init__(
        self,
        registry: PermissionIntentRegistry,
        *,
        state_query: Callable[[PermissionKind], PermissionState] = _state_for_kind,
        bridge_factory: Callable[[], _MacosPermissionActionBridge]
        | None = None,
        runtime_macos: tuple[int, int] | None = None,
    ) -> None:
        self._registry = registry
        self._state_query = state_query
        self._bridge_factory = bridge_factory or _NativeMacosPermissionActionBridge
        self._runtime_macos = (
            _runtime_macos_version() if runtime_macos is None else runtime_macos
        )
        self._bridge: _MacosPermissionActionBridge | None = None

    def _native_bridge(self) -> _MacosPermissionActionBridge:
        if self._bridge is None:
            self._bridge = self._bridge_factory()
        return self._bridge

    def open_permission_settings(
        self, intent: PermissionIntent
    ) -> PermissionActionOutcome:
        use = self._registry.consume(intent)
        invalid = _invalid_intent_outcome(
            intent, use, PermissionAction.OPEN_SETTINGS
        )
        if invalid is not None:
            return invalid
        if (
            platform.system() != "Darwin"
            or (
                self._runtime_macos is not None
                and self._runtime_macos < MACOS_SYSTEM_SETTINGS_API_MINIMUM
            )
        ):
            return _unsupported_outcome(
                intent.kind,
                PermissionAction.OPEN_SETTINGS,
                PermissionState.UNSUPPORTED,
            )
        try:
            before = self._state_query(intent.kind)
        except Exception:  # noqa: BLE001 - native state remains content-free
            before = PermissionState.UNKNOWN
        if before is PermissionState.GRANTED:
            return PermissionActionOutcome(
                kind=intent.kind,
                action=PermissionAction.OPEN_SETTINGS,
                status=PermissionActionStatus.UNCHANGED,
                before=before,
                after=before,
            )
        if before in {PermissionState.NOT_REQUIRED, PermissionState.UNSUPPORTED}:
            return _unsupported_outcome(
                intent.kind, PermissionAction.OPEN_SETTINGS, before
            )
        try:
            opened = self._native_bridge().open_system_settings()
            after = self._state_query(intent.kind)
        except Exception:  # noqa: BLE001 - native failures never expose detail
            return PermissionActionOutcome(
                kind=intent.kind,
                action=PermissionAction.OPEN_SETTINGS,
                status=PermissionActionStatus.FAILED,
                before=before,
                after=PermissionState.UNKNOWN,
                failure=PermissionActionFailure.NATIVE_ERROR,
            )
        if not opened:
            return PermissionActionOutcome(
                kind=intent.kind,
                action=PermissionAction.OPEN_SETTINGS,
                status=PermissionActionStatus.UNSUPPORTED,
                before=before,
                after=after,
                failure=PermissionActionFailure.RECOVERY_UNAVAILABLE,
            )
        return PermissionActionOutcome(
            kind=intent.kind,
            action=PermissionAction.OPEN_SETTINGS,
            status=PermissionActionStatus.COMPLETED,
            before=before,
            after=after,
            relaunch_required=after is not PermissionState.GRANTED,
        )


def make_macos_permission_action_providers(
    registry: PermissionIntentRegistry,
) -> PermissionActionProviders:
    attempted: set[PermissionKind] = set()
    attempt_lock = threading.Lock()
    bridge = _NativeMacosPermissionActionBridge()
    bridge_factory = lambda: bridge
    return PermissionActionProviders(
        requester=MacosPermissionRequester(
            registry,
            bridge_factory=bridge_factory,
            attempted=attempted,
            attempt_lock=attempt_lock,
        ),
        recovery=MacosPermissionRecoveryProvider(
            registry,
            bridge_factory=bridge_factory,
        ),
    )


__all__ = [
    "MACOS_SYSTEM_SETTINGS_API_MINIMUM",
    "MICROPHONE_REQUEST_TIMEOUT_SECONDS",
    "MacosPermissionRecoveryProvider",
    "MacosPermissionRequester",
    "SYSTEM_SETTINGS_BUNDLE_IDENTIFIER",
    "make_macos_permission_action_providers",
]
