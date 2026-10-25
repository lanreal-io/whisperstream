"""Native macOS focus, pasteboard, and keyboard delivery adapter.

The portable schema lives in :mod:`whisperstream.delivery_contract`. AppKit
objects, Core Graphics events, process identifiers, launch identities,
pasteboard contents, timers, and modifier key codes remain private to this
module. The adapter accepts only already-processed text and returns
content-free receipts.

Passive Accessibility status is queried before target resolution and again
immediately before input. No request API or System Settings action is reachable
from this module.
"""

from __future__ import annotations

from .macos_process_identity import process_launch_token

import ctypes
import ctypes.util
import logging
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Callable, ContextManager, Protocol

from .delivery_contract import (
    DeliveryClaimLedger,
    DeliveryClaimStatus,
    DeliveryFailure,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryRoute,
    DeliveryStatus,
    DeliveryStyle,
    DeliveryTarget,
    DeliveryTargetOutcome,
    DeliveryWarning,
)
from .permission_contract import PermissionKind, PermissionReport, PermissionState



_COMMAND_KEYCODE = 55
_V_KEYCODE = 9
_CG_COMMAND_FLAG = 1 << 20
_RETURN_KEYCODE = 36
_MODIFIER_KEYCODES = (54, 55, 56, 57, 58, 59, 60, 61, 62)
_CG_HID_EVENT_TAP = 0
_CG_HID_SYSTEM_STATE = 1
_CG_EVENT_SOURCE_USER_DATA = 42
_SYNTHETIC_EVENT_MARKER = 0x5753485354524D31  # ``WSHSTRM1``
_CF_STRING_ENCODING_UTF8 = 0x08000100


@dataclass(frozen=True, slots=True)
class _NativeTarget:
    pid: int = field(repr=False)
    launch_token: int = field(repr=False)


class _MacosDeliveryBridge(Protocol):
    def frontmost_target(self) -> _NativeTarget | None: ...

    def validate_target(self, target: _NativeTarget) -> DeliveryFailure | None: ...

    def read_pasteboard_text(self) -> str | None: ...

    def write_pasteboard_text(self, value: str) -> None: ...

    def held_modifier_keycodes(self) -> tuple[int, ...]: ...

    def modifier_physically_down(self, keycode: int) -> bool: ...

    def post_key(self, keycode: int, pressed: bool, *, flags: int | None = None) -> None: ...

    def post_unicode(self, value: str) -> None: ...


def _objc_callable(
    objc: ctypes.CDLL,
    restype: object,
    *argtypes: object,
) -> object:
    address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    if not address:
        raise RuntimeError("Objective-C message dispatch unavailable")
    prototype = ctypes.CFUNCTYPE(
        restype,  # type: ignore[arg-type]
        ctypes.c_void_p,
        ctypes.c_void_p,
        *argtypes,  # type: ignore[arg-type]
    )
    return prototype(address)


class _NativeMacosDeliveryBridge:
    """Minimal ctypes bindings for documented AppKit/CoreGraphics delivery APIs."""

    def __init__(self) -> None:
        appkit_path = ctypes.util.find_library("AppKit")
        core_graphics_path = ctypes.util.find_library("CoreGraphics")
        core_foundation_path = ctypes.util.find_library("CoreFoundation")
        objc_path = ctypes.util.find_library("objc")
        if not all(
            (appkit_path, core_graphics_path, core_foundation_path, objc_path)
        ):
            raise RuntimeError("native delivery framework unavailable")

        self._appkit = ctypes.CDLL(appkit_path)
        self._cg = ctypes.CDLL(core_graphics_path)
        self._cf = ctypes.CDLL(core_foundation_path)
        self._objc = ctypes.CDLL(objc_path)

        self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self._objc.objc_getClass.restype = ctypes.c_void_p
        self._objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self._objc.sel_registerName.restype = ctypes.c_void_p

        self._msg_id0 = _objc_callable(self._objc, ctypes.c_void_p)
        self._msg_void0 = _objc_callable(self._objc, None)
        self._msg_i32_0 = _objc_callable(self._objc, ctypes.c_int32)
        self._msg_i64_0 = _objc_callable(self._objc, ctypes.c_int64)
        self._msg_f64_0 = _objc_callable(self._objc, ctypes.c_double)
        self._msg_id_i32 = _objc_callable(
            self._objc, ctypes.c_void_p, ctypes.c_int32
        )
        self._msg_id_id = _objc_callable(
            self._objc, ctypes.c_void_p, ctypes.c_void_p
        )
        self._msg_bool_id_id = _objc_callable(
            self._objc,
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        self._workspace_class = self._class(b"NSWorkspace")
        self._running_app_class = self._class(b"NSRunningApplication")
        self._pasteboard_class = self._class(b"NSPasteboard")
        self._autorelease_pool_class = self._class(b"NSAutoreleasePool")
        self._sel_new = self._selector(b"new")
        self._sel_drain = self._selector(b"drain")
        self._sel_shared_workspace = self._selector(b"sharedWorkspace")
        self._sel_frontmost_application = self._selector(b"frontmostApplication")
        self._sel_running_application = self._selector(
            b"runningApplicationWithProcessIdentifier:"
        )
        self._sel_process_identifier = self._selector(b"processIdentifier")
        self._sel_general_pasteboard = self._selector(b"generalPasteboard")
        self._sel_clear_contents = self._selector(b"clearContents")
        self._sel_string_for_type = self._selector(b"stringForType:")
        self._sel_set_string = self._selector(b"setString:forType:")

        self._pasteboard_string_type = ctypes.c_void_p.in_dll(
            self._appkit, "NSPasteboardTypeString"
        ).value
        if not self._pasteboard_string_type:
            raise RuntimeError("pasteboard string type unavailable")

        self._cf.CFStringCreateWithBytes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_long,
            ctypes.c_uint32,
            ctypes.c_bool,
        ]
        self._cf.CFStringCreateWithBytes.restype = ctypes.c_void_p
        self._cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFStringGetLength.restype = ctypes.c_long
        self._cf.CFStringGetMaximumSizeForEncoding.argtypes = [
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        self._cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetCString.restype = ctypes.c_bool
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None
        self._cf.CFRunLoopRunInMode.argtypes = [
            ctypes.c_void_p,
            ctypes.c_double,
            ctypes.c_bool,
        ]
        self._cf.CFRunLoopRunInMode.restype = ctypes.c_int32
        try:
            self._cf_default_mode = ctypes.c_void_p.in_dll(
                self._cf, "kCFRunLoopDefaultMode"
            )
        except ValueError:  # pragma: no cover - present on supported macOS
            self._cf_default_mode = None

        self._cg.CGEventCreateKeyboardEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_bool,
        ]
        self._cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        self._cg.CGEventKeyboardSetUnicodeString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_uint16),
        ]
        self._cg.CGEventKeyboardSetUnicodeString.restype = None
        self._cg.CGEventSetIntegerValueField.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int64,
        ]
        self._cg.CGEventSetIntegerValueField.restype = None
        self._cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self._cg.CGEventSetFlags.restype = None
        self._cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        self._cg.CGEventPost.restype = None
        self._cg.CGEventSourceKeyState.argtypes = [
            ctypes.c_int32,
            ctypes.c_uint16,
        ]
        self._cg.CGEventSourceKeyState.restype = ctypes.c_bool

    def _class(self, name: bytes) -> int:
        value = int(self._objc.objc_getClass(name) or 0)
        if not value:
            raise RuntimeError("Objective-C class unavailable")
        return value

    def _selector(self, name: bytes) -> int:
        value = int(self._objc.sel_registerName(name) or 0)
        if not value:
            raise RuntimeError("Objective-C selector unavailable")
        return value

    def _application_target(self, application: int | None) -> _NativeTarget | None:
        if not application:
            return None
        pid = int(self._msg_i32_0(application, self._sel_process_identifier))
        launch_token = process_launch_token(pid)
        if launch_token is None:
            return None
        return _NativeTarget(pid=pid, launch_token=launch_token)

    def _pump_run_loop(self, *, passes: int = 32) -> None:
        from .macos_native_query_owner import pump_main_native_events
        pump_main_native_events()

    @contextmanager
    def _autorelease_pool(self):
        pool = int(self._msg_id0(self._autorelease_pool_class, self._sel_new) or 0)
        if not pool:
            raise RuntimeError("autorelease pool unavailable")
        try:
            yield
        finally:
            self._msg_void0(pool, self._sel_drain)

    def frontmost_target(self) -> _NativeTarget | None:
        from .macos_native_query_owner import query_on_main
        return query_on_main(self._frontmost_target_main)

    def _frontmost_target_main(self) -> _NativeTarget | None:
        self._pump_run_loop()
        with self._autorelease_pool():
            workspace = int(
                self._msg_id0(self._workspace_class, self._sel_shared_workspace) or 0
            )
            if not workspace:
                raise RuntimeError("workspace unavailable")
            application = int(
                self._msg_id0(workspace, self._sel_frontmost_application) or 0
            )
            return self._application_target(application)

    def validate_target(self, target: _NativeTarget) -> DeliveryFailure | None:
        from .macos_native_query_owner import query_on_main
        return query_on_main(lambda: self._validate_target_main(target))

    def _validate_target_main(self, target: _NativeTarget) -> DeliveryFailure | None:
        self._pump_run_loop()
        with self._autorelease_pool():
            application = int(
                self._msg_id_i32(
                    self._running_app_class,
                    self._sel_running_application,
                    target.pid,
                )
                or 0
            )
            running = self._application_target(application)
            if running is None:
                return DeliveryFailure.DEAD_TARGET
            if running.launch_token != target.launch_token:
                return DeliveryFailure.PROCESS_MISMATCH
            frontmost = self.frontmost_target()
            if frontmost is None:
                return DeliveryFailure.DEAD_TARGET
            if frontmost.pid != target.pid:
                return DeliveryFailure.FOCUS_MISMATCH
            if frontmost.launch_token != target.launch_token:
                return DeliveryFailure.PROCESS_MISMATCH
            return None

    def _general_pasteboard(self) -> int:
        pasteboard = int(
            self._msg_id0(self._pasteboard_class, self._sel_general_pasteboard)
            or 0
        )
        if not pasteboard:
            raise RuntimeError("general pasteboard unavailable")
        return pasteboard

    def _cf_string_to_python(self, value: int) -> str:
        length = int(self._cf.CFStringGetLength(value))
        maximum = int(
            self._cf.CFStringGetMaximumSizeForEncoding(
                length, _CF_STRING_ENCODING_UTF8
            )
        )
        if maximum < 0:
            raise RuntimeError("pasteboard string conversion failed")
        buffer = ctypes.create_string_buffer(maximum + 1)
        if not self._cf.CFStringGetCString(
            value,
            buffer,
            len(buffer),
            _CF_STRING_ENCODING_UTF8,
        ):
            raise RuntimeError("pasteboard string conversion failed")
        return buffer.value.decode("utf-8")

    def _python_to_cf_string(self, value: str) -> int:
        encoded = value.encode("utf-8")
        raw = (ctypes.c_ubyte * max(1, len(encoded)))()
        if encoded:
            ctypes.memmove(raw, encoded, len(encoded))
        result = int(
            self._cf.CFStringCreateWithBytes(
                None,
                raw,
                len(encoded),
                _CF_STRING_ENCODING_UTF8,
                False,
            )
            or 0
        )
        if not result:
            raise RuntimeError("pasteboard string creation failed")
        return result

    def read_pasteboard_text(self) -> str | None:
        with self._autorelease_pool():
            value = int(
                self._msg_id_id(
                    self._general_pasteboard(),
                    self._sel_string_for_type,
                    self._pasteboard_string_type,
                )
                or 0
            )
            return None if not value else self._cf_string_to_python(value)

    def write_pasteboard_text(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("pasteboard value must be text")
        with self._autorelease_pool():
            pasteboard = self._general_pasteboard()
            self._msg_i64_0(pasteboard, self._sel_clear_contents)
            native_string = self._python_to_cf_string(value)
            try:
                written = bool(
                    self._msg_bool_id_id(
                        pasteboard,
                        self._sel_set_string,
                        native_string,
                        self._pasteboard_string_type,
                    )
                )
            finally:
                self._cf.CFRelease(native_string)
            if not written:
                raise RuntimeError("pasteboard write failed")

    def held_modifier_keycodes(self) -> tuple[int, ...]:
        return tuple(
            keycode
            for keycode in _MODIFIER_KEYCODES
            if self.modifier_physically_down(keycode)
        )

    def modifier_physically_down(self, keycode: int) -> bool:
        return bool(
            self._cg.CGEventSourceKeyState(_CG_HID_SYSTEM_STATE, keycode)
        )

    def _post_event(self, keycode: int, pressed: bool, value: str | None, *, flags: int | None = None) -> None:
        event = int(
            self._cg.CGEventCreateKeyboardEvent(None, keycode, pressed) or 0
        )
        if not event:
            raise RuntimeError("keyboard event creation failed")
        try:
            self._cg.CGEventSetIntegerValueField(
                event,
                _CG_EVENT_SOURCE_USER_DATA,
                _SYNTHETIC_EVENT_MARKER,
            )
            if flags is not None:
                self._cg.CGEventSetFlags(event, flags)
            if value is not None:
                encoded = value.encode("utf-16-le")
                units = (ctypes.c_uint16 * (len(encoded) // 2)).from_buffer_copy(
                    encoded
                )
                self._cg.CGEventKeyboardSetUnicodeString(
                    event,
                    len(units),
                    units,
                )
            self._cg.CGEventPost(_CG_HID_EVENT_TAP, event)
        finally:
            self._cf.CFRelease(event)

    def post_key(self, keycode: int, pressed: bool, *, flags: int | None = None) -> None:
        self._post_event(keycode, pressed, None, flags=flags)

    def post_unicode(self, value: str) -> None:
        if not isinstance(value, str) or len(value) != 1:
            raise ValueError("one Unicode scalar is required")
        try:
            self._post_event(0, True, value, flags=0)
        finally:
            self._post_event(0, False, value, flags=0)


@dataclass(frozen=True, slots=True)
class _TargetBinding:
    target: DeliveryTarget
    native_target: object = field(repr=False)


@dataclass(slots=True, eq=False)
class _PendingClipboardRestore:
    saved: str = field(repr=False)
    timer: object | None = field(default=None, repr=False)


@dataclass(slots=True)
class _DeliveryPreparation:
    effective_style: DeliveryStyle
    saved_clipboard: str | None = field(default=None, repr=False)
    snapshot_fallback: bool = False
    warnings: list[DeliveryWarning] = field(default_factory=list)


@dataclass(slots=True)
class _BatchState:
    token: str
    request_id: str
    generation: int
    binding: _TargetBinding
    style: DeliveryStyle
    effective_style: DeliveryStyle
    snapshot_fallback: bool
    saved_clipboard: str | None = field(default=None, repr=False)
    warnings: list[DeliveryWarning] = field(default_factory=list)
    units_attempted: int = 0
    units_completed: int = 0
    modifiers_restored: bool = True


@dataclass(frozen=True, slots=True)
class MacosDeliveryBatchStart:
    """Adapter-private incremental-delivery start result."""

    token: str | None = field(default=None, repr=False)
    receipt: DeliveryReceipt | None = None


def _warnings_tuple(items: list[DeliveryWarning]) -> tuple[DeliveryWarning, ...]:
    return tuple(dict.fromkeys(items))


def _error_name(exc: BaseException) -> str:
    return type(exc).__name__


class MacosDeliveryAdapter:
    """Schema-v1 Mac delivery adapter with exactly-once input claims."""

    supports_incremental_delivery = True
    supports_modifier_wait = True

    def __init__(
        self,
        *,
        keyboard_manager: object | None,
        logger: logging.Logger,
        timer_factory: Callable[[float, Callable[[], None]], object] = threading.Timer,
        sleep: Callable[[float], None] = time.sleep,
        clipboard_behavior: bool,
        clipboard_clear_delay: float,
        keystroke_delay_ms: int = 0,
        permission_reporter: Callable[[], PermissionReport] | None = None,
        bridge_factory: Callable[[], _MacosDeliveryBridge] = _NativeMacosDeliveryBridge,
    ) -> None:
        self._keyboard = keyboard_manager
        self._logger = logger
        self._timer_factory = timer_factory
        self._sleep = sleep
        self._clipboard_behavior = bool(clipboard_behavior)
        self._clipboard_clear_delay = max(0.1, float(clipboard_clear_delay))
        self._keystroke_delay_ms = max(0, int(keystroke_delay_ms))
        self._permission_reporter = permission_reporter or self._query_permissions
        self._bridge_factory = bridge_factory
        self._bridge: _MacosDeliveryBridge | None = None

        self._claim_ledger = DeliveryClaimLedger()
        self._operation_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._targets: dict[str, _TargetBinding] = {}
        self._receipts: dict[tuple[str, int], DeliveryReceipt] = {}
        self._batches: dict[str, _BatchState] = {}
        self._pending_restores: list[_PendingClipboardRestore] = []
        self._synthetic_modifiers: set[int] = set()
        self._cancelled_requests: set[tuple[str, int]] = set()
        self._closing = False
        self._closed = False

    @staticmethod
    def _query_permissions() -> PermissionReport:
        from .macos_permission_status import MacosPermissionStatusProvider

        return MacosPermissionStatusProvider().query_permissions()

    @staticmethod
    def native_symbols_available() -> bool:
        """Resolve real AppKit/CoreGraphics symbols without interacting."""

        try:
            _NativeMacosDeliveryBridge()
        except Exception:
            return False
        return True

    def _native_bridge(self) -> _MacosDeliveryBridge:
        bridge = self._bridge
        if bridge is None:
            bridge = self._bridge_factory()
            self._bridge = bridge
        return bridge

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed or self._closing

    @property
    def pending_restore_count(self) -> int:
        with self._state_lock:
            return len(self._pending_restores)

    @property
    def active_batch_count(self) -> int:
        with self._state_lock:
            return len(self._batches)

    @property
    def target_count(self) -> int:
        with self._state_lock:
            return len(self._targets)

    @property
    def synthetic_modifier_count(self) -> int:
        with self._state_lock:
            return len(self._synthetic_modifiers)

    def _permission_failure(self) -> DeliveryFailure | None:
        try:
            report = self._permission_reporter()
        except Exception as exc:  # noqa: BLE001 - typed, content-free failure
            self._logger.warning(
                "delivery permission query failed reason=%s", _error_name(exc)
            )
            return DeliveryFailure.NATIVE_ERROR
        snapshot = next(
            (
                item
                for item in report.permissions
                if item.kind is PermissionKind.ACCESSIBILITY
            ),
            None,
        )
        if snapshot is not None and snapshot.state is PermissionState.GRANTED:
            return None
        if snapshot is not None and snapshot.state in {
            PermissionState.NOT_DETERMINED,
            PermissionState.DENIED,
            PermissionState.RESTRICTED,
        }:
            return DeliveryFailure.PERMISSION_DENIED
        return DeliveryFailure.NATIVE_ERROR

    def resolve_target(
        self,
        request_id: str,
        generation: int,
        route: DeliveryRoute,
    ) -> DeliveryTargetOutcome:
        """Capture the exact current app identity without changing focus."""

        if not isinstance(route, DeliveryRoute):
            raise ValueError("invalid delivery route")
        if self.closed:
            return DeliveryTargetOutcome(failure=DeliveryFailure.CLEANUP_FAILED)
        permission_failure = self._permission_failure()
        if permission_failure is not None:
            return DeliveryTargetOutcome(failure=permission_failure)
        try:
            native_target = self._native_bridge().frontmost_target()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "delivery target resolve failed reason=%s", _error_name(exc)
            )
            return DeliveryTargetOutcome(failure=DeliveryFailure.NATIVE_ERROR)
        if native_target is None:
            return DeliveryTargetOutcome(failure=DeliveryFailure.DEAD_TARGET)

        opaque_id = f"macos-delivery-{uuid.uuid4().hex}"
        target = DeliveryTarget(
            route=DeliveryRoute.FOCUSED,
            opaque_id=opaque_id,
            generation=generation,
        )
        binding = _TargetBinding(target=target, native_target=native_target)
        with self._state_lock:
            if self._closed:
                return DeliveryTargetOutcome(failure=DeliveryFailure.CLEANUP_FAILED)
            self._targets[opaque_id] = binding
        return DeliveryTargetOutcome(target=target)

    def _take_binding(self, target: DeliveryTarget) -> _TargetBinding | None:
        with self._state_lock:
            binding = self._targets.pop(target.opaque_id, None)
        if binding is None or binding.target != target:
            return None
        return binding

    def _existing_or_reused_receipt(
        self, request: DeliveryRequest
    ) -> DeliveryReceipt | None:
        claim = self._claim_ledger.claim(request.request_id, request.generation)
        if claim is DeliveryClaimStatus.CLAIMED:
            return None
        with self._state_lock:
            receipt = self._receipts.get((request.request_id, request.generation))
        if receipt is not None:
            return receipt
        return self._failed_receipt(
            request,
            DeliveryFailure.STALE_GENERATION,
            route=request.target.route,
        )

    @dataclass(slots=True)
    class _UnitOutcome:
        status: DeliveryStatus
        failure: DeliveryFailure | None
        completed: bool
        warnings: list[DeliveryWarning]
        modifiers_restored: bool

    def _validate_binding(self, binding: _TargetBinding) -> DeliveryFailure | None:
        permission_failure = self._permission_failure()
        if permission_failure is not None:
            return permission_failure
        try:
            return self._native_bridge().validate_target(binding.native_target)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "delivery target validation failed reason=%s", _error_name(exc)
            )
            return DeliveryFailure.NATIVE_ERROR

    def deliver(
        self,
        request: DeliveryRequest,
        *,
        wait_for_modifiers: bool = True,
    ) -> DeliveryReceipt:
        """Attempt one already-processed payload exactly once."""

        # ========================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never log request.processed_text or a native target. Receipts and logs
        # below contain only counts, style, route, status, warnings, and failures.
        # ========================================================================
        with self._operation_lock:
            reused = self._existing_or_reused_receipt(request)
            if reused is not None:
                self._take_binding(request.target)
                return reused
            binding = self._take_binding(request.target)
            if binding is None:
                receipt = self._failed_receipt(
                    request,
                    DeliveryFailure.STALE_GENERATION,
                    route=request.target.route,
                )
            elif self.closed:
                receipt = self._failed_receipt(
                    request,
                    DeliveryFailure.CLEANUP_FAILED,
                    route=binding.target.route,
                )
            else:
                validation_failure = self._validate_binding(binding)
                if validation_failure is not None:
                    receipt = self._failed_receipt(
                        request,
                        validation_failure,
                        route=binding.target.route,
                    )
                else:
                    preparation = self._prepare_delivery(request)
                    unit = self._deliver_unit(
                        binding,
                        style=preparation.effective_style,
                        processed_text=request.processed_text,
                        submit=request.submit,
                        wait_for_modifiers=wait_for_modifiers,
                        warnings=preparation.warnings,
                        snapshot_fallback=preparation.snapshot_fallback,
                    )
                    self._schedule_clipboard_restore(preparation.saved_clipboard)
                    receipt = DeliveryReceipt(
                        request_id=request.request_id,
                        generation=request.generation,
                        route=binding.target.route,
                        status=unit.status,
                        failure=unit.failure,
                        warnings=_warnings_tuple(unit.warnings),
                        units_attempted=1,
                        units_completed=1 if unit.completed else 0,
                        clipboard_restored=(
                            None
                            if preparation.saved_clipboard is not None
                            else (
                                True
                                if request.style is DeliveryStyle.CLIPBOARD
                                else None
                            )
                        ),
                        modifiers_restored=unit.modifiers_restored,
                    )
            with self._state_lock:
                self._receipts[(request.request_id, request.generation)] = receipt
            self._log_receipt(receipt, request.style)
            return receipt

    def begin_batch(self, request: DeliveryRequest) -> MacosDeliveryBatchStart:
        """Claim one incremental delivery and save its pasteboard exactly once."""

        # ========================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # The request payload is used only by the caller's first unit and is not
        # retained in batch state.
        # ========================================================================
        with self._operation_lock:
            reused = self._existing_or_reused_receipt(request)
            if reused is not None:
                self._take_binding(request.target)
                return MacosDeliveryBatchStart(receipt=reused)
            binding = self._take_binding(request.target)
            failure = (
                DeliveryFailure.STALE_GENERATION
                if binding is None
                else (
                    DeliveryFailure.CLEANUP_FAILED
                    if self.closed
                    else self._validate_binding(binding)
                )
            )
            if binding is None or failure is not None:
                receipt = self._failed_receipt(
                    request,
                    failure or DeliveryFailure.NATIVE_ERROR,
                    route=request.target.route if binding is None else binding.target.route,
                )
                with self._state_lock:
                    self._receipts[(request.request_id, request.generation)] = receipt
                return MacosDeliveryBatchStart(receipt=receipt)

            preparation = self._prepare_delivery(request)
            token = uuid.uuid4().hex
            batch = _BatchState(
                token=token,
                request_id=request.request_id,
                generation=request.generation,
                binding=binding,
                style=request.style,
                effective_style=preparation.effective_style,
                snapshot_fallback=preparation.snapshot_fallback,
                saved_clipboard=preparation.saved_clipboard,
                warnings=preparation.warnings,
            )
            with self._state_lock:
                self._batches[token] = batch
            return MacosDeliveryBatchStart(token=token)

    def deliver_batch_unit(
        self,
        token: str,
        processed_text: str,
        *,
        last: bool,
    ) -> DeliveryReceipt:
        """Attempt one incremental unit without re-resolving or re-claiming."""

        # ========================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never retain or log processed_text.
        # ========================================================================
        if not isinstance(processed_text, str):
            raise ValueError("processed_text must be a string")
        with self._operation_lock:
            with self._state_lock:
                batch = self._batches.get(token)
            if batch is None:
                raise ValueError("unknown delivery batch")
            if self.closed:
                return self._finish_batch(
                    batch,
                    status=DeliveryStatus.CANCELLED,
                    failure=None,
                    add_attempt=False,
                )

            batch.units_attempted += 1
            unit = self._deliver_unit(
                batch.binding,
                style=batch.effective_style,
                processed_text=processed_text,
                submit=False,
                wait_for_modifiers=False,
                warnings=batch.warnings,
                snapshot_fallback=batch.snapshot_fallback,
            )
            batch.warnings = unit.warnings
            batch.modifiers_restored = (
                batch.modifiers_restored and unit.modifiers_restored
            )
            if unit.completed:
                batch.units_completed += 1
            receipt = DeliveryReceipt(
                request_id=batch.request_id,
                generation=batch.generation,
                route=batch.binding.target.route,
                status=unit.status,
                failure=unit.failure,
                warnings=_warnings_tuple(batch.warnings),
                units_attempted=batch.units_attempted,
                units_completed=batch.units_completed,
                clipboard_restored=None,
                modifiers_restored=batch.modifiers_restored,
            )
            terminal_failure = unit.failure
            terminal_status = unit.status
            if (
                not unit.completed
                and terminal_failure is None
                and terminal_status is not DeliveryStatus.CANCELLED
            ):
                terminal_failure = DeliveryFailure.INPUT_FAILED
                terminal_status = DeliveryStatus.FAILED
            if (
                terminal_failure is not None
                or terminal_status is DeliveryStatus.CANCELLED
                or last
            ):
                receipt = self._finish_batch(
                    batch,
                    status=terminal_status,
                    failure=terminal_failure,
                    add_attempt=False,
                )
            self._log_receipt(receipt, batch.style)
            return receipt

    def abort_batch(
        self,
        token: str,
        *,
        cancelled: bool = False,
    ) -> DeliveryReceipt | None:
        with self._state_lock:
            batch = self._batches.get(token)
            if batch is None:
                return None
            key = (batch.request_id, batch.generation)
            self._cancelled_requests.add(key)
        with self._operation_lock:
            with self._state_lock:
                batch = self._batches.get(token)
            if batch is None:
                return None
            return self._finish_batch(
                batch,
                status=(
                    DeliveryStatus.CANCELLED if cancelled else DeliveryStatus.FAILED
                ),
                failure=(None if cancelled else DeliveryFailure.NATIVE_ERROR),
                add_attempt=not cancelled,
            )

    def _finish_batch(
        self,
        batch: _BatchState,
        *,
        status: DeliveryStatus,
        failure: DeliveryFailure | None,
        add_attempt: bool,
    ) -> DeliveryReceipt:
        with self._state_lock:
            self._batches.pop(batch.token, None)
        if add_attempt:
            batch.units_attempted = max(
                batch.units_attempted,
                batch.units_completed + 1,
            )
        self._schedule_clipboard_restore(batch.saved_clipboard)
        receipt = DeliveryReceipt(
            request_id=batch.request_id,
            generation=batch.generation,
            route=batch.binding.target.route,
            status=status,
            failure=failure,
            warnings=_warnings_tuple(batch.warnings),
            units_attempted=batch.units_attempted,
            units_completed=batch.units_completed,
            clipboard_restored=(
                None if batch.saved_clipboard is not None else True
            ),
            modifiers_restored=batch.modifiers_restored,
        )
        with self._state_lock:
            self._receipts[(batch.request_id, batch.generation)] = receipt
        return receipt

    def cancel(self, request_id: str, generation: int) -> DeliveryReceipt:
        key = (request_id, generation)
        with self._state_lock:
            self._cancelled_requests.add(key)
        with self._operation_lock:
            with self._state_lock:
                existing = self._receipts.get(key)
                batch = next(
                    (
                        item
                        for item in self._batches.values()
                        if (item.request_id, item.generation) == key
                    ),
                    None,
                )
            if existing is not None:
                return existing
            if batch is not None:
                return self._finish_batch(
                    batch,
                    status=DeliveryStatus.CANCELLED,
                    failure=None,
                    add_attempt=False,
                )
            claim = self._claim_ledger.claim(request_id, generation)
            if claim is DeliveryClaimStatus.REUSED:
                with self._state_lock:
                    existing = self._receipts.get(key)
                if existing is not None:
                    return existing
            receipt = DeliveryReceipt(
                request_id=request_id,
                generation=generation,
                route=DeliveryRoute.FOCUSED,
                status=DeliveryStatus.CANCELLED,
                units_attempted=0,
                units_completed=0,
                clipboard_restored=True,
            )
            with self._state_lock:
                self._receipts[key] = receipt
            return receipt

    def _deliver_unit(
        self,
        binding: _TargetBinding,
        *,
        style: DeliveryStyle,
        processed_text: str,
        submit: bool,
        wait_for_modifiers: bool,
        warnings: list[DeliveryWarning],
        snapshot_fallback: bool,
    ) -> _UnitOutcome:
        # ========================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never log processed_text. It crosses only the pasteboard/direct input
        # calls below.
        # ========================================================================
        warnings = list(warnings)
        failure = self._validate_binding(binding)
        if failure is not None:
            return self._UnitOutcome(
                DeliveryStatus.FAILED,
                failure,
                False,
                warnings,
                True,
            )

        if style is DeliveryStyle.CLIPBOARD:
            try:
                self._native_bridge().write_pasteboard_text(processed_text)
            except Exception as exc:  # noqa: BLE001
                warnings.append(DeliveryWarning.CLIPBOARD_COPY_FAILED)
                self._logger.error(
                    "clipboard copy failed reason=%s", _error_name(exc)
                )
                return self._UnitOutcome(
                    DeliveryStatus.FOCUSED_ATTEMPTED,
                    None,
                    False,
                    warnings,
                    True,
                )
            self._sleep(0.05)
            failure = self._validate_binding(binding)
            if failure is not None:
                return self._UnitOutcome(
                    DeliveryStatus.FAILED,
                    failure,
                    False,
                    warnings,
                    True,
                )

        held, _modifier_snapshot_known = self._snapshot_modifiers(
            wait_for_modifiers
        )
        input_result = self._send_input(
            held,
            style=style,
            processed_text=processed_text,
            submit=submit,
        )
        warnings.extend(input_result.warnings)
        if snapshot_fallback and not input_result.delivered:
            return self._UnitOutcome(
                DeliveryStatus.FAILED,
                DeliveryFailure.INPUT_FAILED,
                False,
                warnings,
                input_result.modifiers_restored,
            )
        return self._UnitOutcome(
            DeliveryStatus.FOCUSED_ATTEMPTED,
            None,
            input_result.delivered,
            warnings,
            input_result.modifiers_restored,
        )

    @dataclass(slots=True)
    class _InputResult:
        delivered: bool = False
        modifiers_restored: bool = True
        warnings: list[DeliveryWarning] = field(default_factory=list)

    def _snapshot_modifiers(
        self, wait_for_modifiers: bool
    ) -> tuple[tuple[int, ...], bool]:
        try:
            held_before = tuple(self._native_bridge().held_modifier_keycodes())
        except Exception:  # noqa: BLE001
            return (), False
        if not wait_for_modifiers or not held_before:
            return held_before, True
        self._sleep(0.25)
        try:
            held = tuple(self._native_bridge().held_modifier_keycodes())
        except Exception:  # noqa: BLE001
            return held_before, False
        for keycode in held_before:
            if keycode not in held:
                self._logger.info("modifier guard released")
        for _keycode in held:
            self._logger.warning("modifier guard timeout")
        return held, True

    def _suppression_context(self) -> ContextManager[None]:
        suppressor = getattr(self._keyboard, "suppress_self_events", None)
        if not callable(suppressor):
            return nullcontext()
        return suppressor()

    def _send_command_v(self) -> None:
        bridge = self._native_bridge()
        command_down = False
        try:
            bridge.post_key(_COMMAND_KEYCODE, True, flags=_CG_COMMAND_FLAG)
            command_down = True
            bridge.post_key(_V_KEYCODE, True, flags=_CG_COMMAND_FLAG)
            bridge.post_key(_V_KEYCODE, False, flags=_CG_COMMAND_FLAG)
        finally:
            if command_down:
                bridge.post_key(_COMMAND_KEYCODE, False, flags=0)

    def _send_direct_text(self, processed_text: str) -> None:
        bridge = self._native_bridge()
        for character in processed_text:
            bridge.post_unicode(character)
            if self._keystroke_delay_ms > 0:
                self._sleep(self._keystroke_delay_ms / 1000.0)

    def _send_input(
        self,
        held: tuple[int, ...],
        *,
        style: DeliveryStyle,
        processed_text: str,
        submit: bool,
    ) -> _InputResult:
        # ========================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never log processed_text. It crosses only direct Unicode events.
        # ========================================================================
        result = self._InputResult()
        bridge = self._native_bridge()
        try:
            with self._suppression_context():
                release_failed = False
                for keycode in held:
                    try:
                        bridge.post_key(keycode, False)
                    except Exception:  # noqa: BLE001
                        release_failed = True
                        result.modifiers_restored = False
                        result.warnings.append(
                            DeliveryWarning.MODIFIER_RESTORE_FAILED
                        )
                try:
                    if not release_failed:
                        if style is DeliveryStyle.CLIPBOARD:
                            self._send_command_v()
                        else:
                            self._send_direct_text(processed_text)
                        result.delivered = True
                        if submit:
                            bridge.post_key(_RETURN_KEYCODE, True)
                            bridge.post_key(_RETURN_KEYCODE, False)
                except Exception as exc:  # noqa: BLE001 - never retry input
                    result.delivered = False
                    self._logger.error(
                        "delivery input failed style=%s reason=%s",
                        style.value,
                        _error_name(exc),
                    )
                finally:
                    for keycode in held:
                        try:
                            physically_down = bool(
                                bridge.modifier_physically_down(keycode)
                            )
                        except Exception:  # noqa: BLE001
                            physically_down = False
                            result.modifiers_restored = False
                            result.warnings.append(
                                DeliveryWarning.MODIFIER_RESTORE_FAILED
                            )
                        if not physically_down:
                            continue
                        try:
                            bridge.post_key(keycode, True)
                            with self._state_lock:
                                self._synthetic_modifiers.add(keycode)
                        except Exception:  # noqa: BLE001
                            result.modifiers_restored = False
                            result.warnings.append(
                                DeliveryWarning.MODIFIER_RESTORE_FAILED
                            )
        except Exception as exc:  # noqa: BLE001
            result.delivered = False
            self._logger.error(
                "delivery suppression failed reason=%s", _error_name(exc)
            )
        result.warnings = list(dict.fromkeys(result.warnings))
        return result

    def _prepare_delivery(self, request: DeliveryRequest) -> _DeliveryPreparation:
        if (
            request.style is not DeliveryStyle.CLIPBOARD
            or not request.restore_clipboard
            or not self._clipboard_behavior
        ):
            return _DeliveryPreparation(effective_style=request.style)
        try:
            saved = self._native_bridge().read_pasteboard_text()
        except Exception:  # noqa: BLE001 - substitute once before input
            self._logger.warning("clipboard snapshot unavailable")
            return _DeliveryPreparation(
                effective_style=DeliveryStyle.KEYSTROKE,
                snapshot_fallback=True,
                warnings=[DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK],
            )
        if saved is None:
            self._logger.warning("clipboard snapshot unavailable")
            return _DeliveryPreparation(
                effective_style=DeliveryStyle.KEYSTROKE,
                snapshot_fallback=True,
                warnings=[DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK],
            )
        return _DeliveryPreparation(
            effective_style=DeliveryStyle.CLIPBOARD,
            saved_clipboard=saved,
        )

    def update_clipboard_settings(self, *, behavior: bool, clear_delay: float) -> None:
        """Update future deliveries without cancelling owned restoration work."""
        with self._state_lock:
            self._clipboard_behavior = bool(behavior)
            self._clipboard_clear_delay = max(0.1, float(clear_delay))

    def _schedule_clipboard_restore(self, saved: str | None) -> None:
        # A saved snapshot belongs to an admitted transaction, even if the
        # preference changes before that transaction settles.
        if saved is None:
            return
        with self._state_lock:
            delay = self._clipboard_clear_delay
        pending = _PendingClipboardRestore(saved=saved)

        def restore() -> None:
            self._restore_pending(pending)

        try:
            pending.timer = self._timer_factory(
                delay,
                restore,
            )
            with self._state_lock:
                self._pending_restores.append(pending)
            pending.timer.daemon = True
            pending.timer.start()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "clipboard restore schedule failed reason=%s", _error_name(exc)
            )
            self._restore_pending(pending)

    def _restore_pending(self, pending: _PendingClipboardRestore) -> None:
        with self._state_lock:
            if pending not in self._pending_restores:
                if pending.timer is not None:
                    return
            else:
                self._pending_restores.remove(pending)
        try:
            self._native_bridge().write_pasteboard_text(pending.saved)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "clipboard restore failed reason=%s", _error_name(exc)
            )

    def _restore_all_pending_clipboards(self) -> None:
        with self._state_lock:
            pending = tuple(self._pending_restores)
            self._pending_restores.clear()
        if not pending:
            return
        for item in pending:
            try:
                if item.timer is not None:
                    item.timer.cancel()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._native_bridge().write_pasteboard_text(pending[0].saved)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "clipboard close restore failed reason=%s", _error_name(exc)
            )

    def _release_synthetic_modifiers(self) -> None:
        with self._state_lock:
            modifiers = tuple(self._synthetic_modifiers)
            self._synthetic_modifiers.clear()
        if not modifiers:
            return
        try:
            with self._suppression_context():
                for keycode in modifiers:
                    try:
                        self._native_bridge().post_key(keycode, False)
                    except Exception:  # noqa: BLE001
                        self._logger.warning("modifier cleanup failed")
        except Exception:  # noqa: BLE001
            self._logger.warning("modifier cleanup suppression failed")

    def close(self) -> None:
        """Synchronously restore adapter-owned state and block later input."""

        with self._state_lock:
            if self._closed or self._closing:
                return
            self._closing = True
        with self._operation_lock:
            with self._state_lock:
                self._closed = True
                batches = tuple(self._batches.values())
            for batch in batches:
                self._finish_batch(
                    batch,
                    status=DeliveryStatus.CANCELLED,
                    failure=None,
                    add_attempt=False,
                )
            self._restore_all_pending_clipboards()
            self._release_synthetic_modifiers()
            with self._state_lock:
                self._targets.clear()
                self._cancelled_requests.clear()

    def _failed_receipt(
        self,
        request: DeliveryRequest,
        failure: DeliveryFailure,
        *,
        route: DeliveryRoute,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            request_id=request.request_id,
            generation=request.generation,
            route=route,
            status=DeliveryStatus.FAILED,
            failure=failure,
            units_attempted=0,
            units_completed=0,
            clipboard_restored=True,
        )

    def _log_receipt(
        self, receipt: DeliveryReceipt, style: DeliveryStyle
    ) -> None:
        self._logger.info(
            "delivery result=%s route=%s style=%s failure=%s warnings=%d attempted=%d completed=%d",
            receipt.status.value,
            receipt.route.value,
            style.value,
            receipt.failure.value if receipt.failure is not None else "none",
            len(receipt.warnings),
            receipt.units_attempted,
            receipt.units_completed,
        )


__all__ = [
    "MacosDeliveryAdapter",
    "MacosDeliveryBatchStart",
]
