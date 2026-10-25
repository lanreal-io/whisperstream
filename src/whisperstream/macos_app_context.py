"""Darwin owner for opaque application and browser-page Power Mode context.

AppKit application objects, bundle identifiers, process identifiers, bundle
paths, Accessibility elements, raw address values, and image bytes stay in this
module.  Shared callers receive only hashed application keys, opaque instance
tokens, lowercase host-only page identity, and cosmetic data-URI assets.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import base64
import hashlib
import inspect
import os
import plistlib
import queue
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .app_context_contract import (
    ApplicationContextAdapter,
    ApplicationDescriptor,
    ApplicationInventoryScope,
    ApplicationRef,
    BrowserPageRef,
    ContextFailure,
    ContextRequirement,
    ContextSnapshot,
    ContextStatus,
)
from .browser_favicons import (
    _FAVICON_MISS_TTL_SECONDS,
    _query_chromium,
    _query_firefox,
    normalize_favicon_domain,
)

_MAX_ICON_ASSETS = 512


_BROWSER_BUNDLE_IDS = frozenset(
    {
        "com.apple.safari",
        "com.google.chrome",
        "com.microsoft.edgemac",
        "com.brave.browser",
        "org.chromium.chromium",
        "org.mozilla.firefox",
    }
)


def _opaque(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()
    return f"macos-{kind}:sha256:{digest}"


@dataclass(frozen=True, slots=True)
class _MacosApplicationObservation:
    """Adapter-private AppKit observation used only during one operation."""

    bundle_identifier: str = field(repr=False)
    process_identifier: int = field(repr=False)
    display_name: str = field(repr=False)
    icon_data_uri: str | None = field(default=None, repr=False)


class _MacosWorkspace(Protocol):
    def frontmost_application(self) -> _MacosApplicationObservation | None: ...

    def running_applications(self) -> list[_MacosApplicationObservation]: ...

    def installed_applications(
        self, logical_ids: set[str] | None = None
    ) -> list[_MacosApplicationObservation]: ...

    def close(self) -> None: ...


def _objc_callable(
    objc: ctypes.CDLL,
    restype: object,
    *argtypes: object,
) -> object:
    address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    if not address:
        raise RuntimeError("Objective-C message dispatch unavailable")
    return ctypes.CFUNCTYPE(
        restype,  # type: ignore[arg-type]
        ctypes.c_void_p,
        ctypes.c_void_p,
        *argtypes,  # type: ignore[arg-type]
    )(address)


class _NSRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("width", ctypes.c_double), ("height", ctypes.c_double)]


class _NativeMacosWorkspace:
    """Small ctypes AppKit bridge with no retained application objects."""

    _INSTALL_ROOTS = (
        Path("/Applications"),
        Path("/System/Applications"),
        Path("/System/Applications/Utilities"),
    )

    def __init__(self) -> None:
        appkit_path = ctypes.util.find_library("AppKit")
        core_foundation_path = ctypes.util.find_library("CoreFoundation")
        objc_path = ctypes.util.find_library("objc")
        if not all((appkit_path, core_foundation_path, objc_path)):
            raise RuntimeError("native application-context framework unavailable")
        self._appkit = ctypes.CDLL(appkit_path)
        self._cf = ctypes.CDLL(core_foundation_path)
        self._objc = ctypes.CDLL(objc_path)
        self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self._objc.objc_getClass.restype = ctypes.c_void_p
        self._objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self._objc.sel_registerName.restype = ctypes.c_void_p
        self._msg_id0 = _objc_callable(self._objc, ctypes.c_void_p)
        self._msg_void0 = _objc_callable(self._objc, None)
        self._msg_i32_0 = _objc_callable(self._objc, ctypes.c_int32)
        self._msg_ulong_0 = _objc_callable(self._objc, ctypes.c_ulong)
        self._msg_id_ulong = _objc_callable(
            self._objc, ctypes.c_void_p, ctypes.c_ulong
        )
        self._msg_id_id = _objc_callable(
            self._objc, ctypes.c_void_p, ctypes.c_void_p
        )
        self._msg_id_ulong_id = _objc_callable(
            self._objc, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p
        )
        self._msg_ptr0 = _objc_callable(self._objc, ctypes.c_void_p)
        # NSWorkspace keeps its running/frontmost application state in a cache
        # that is only refreshed from notifications delivered on the calling
        # thread's run loop. This process never runs one, so without draining
        # pending sources first every query returns the value observed when the
        # workspace was first touched. Bind CFRunLoopRunInMode so each read can
        # pump the already-pending sources before asking AppKit.
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
        except ValueError:  # pragma: no cover - mode symbol always present
            self._cf_default_mode = None
        self._sel_new = self._selector(b"new")
        self._sel_drain = self._selector(b"drain")
        self._sel_shared_workspace = self._selector(b"sharedWorkspace")
        self._sel_frontmost_application = self._selector(b"frontmostApplication")
        self._sel_running_applications = self._selector(b"runningApplications")
        self._sel_bundle_identifier = self._selector(b"bundleIdentifier")
        self._sel_localized_name = self._selector(b"localizedName")
        self._sel_process_identifier = self._selector(b"processIdentifier")
        self._sel_bundle_url = self._selector(b"bundleURL")
        self._sel_path = self._selector(b"path")
        self._sel_icon = self._selector(b"icon")
        self._sel_icon_for_file = self._selector(b"iconForFile:")
        self._msg_cg_image = _objc_callable(
            self._objc, ctypes.c_void_p, ctypes.POINTER(_NSRect),
            ctypes.c_void_p, ctypes.c_void_p,
        )
        self._sel_cg_image = self._selector(b"CGImageForProposedRect:context:hints:")
        self._sel_init_cg_image = self._selector(b"initWithCGImage:")
        self._sel_alloc = self._selector(b"alloc")
        self._sel_autorelease = self._selector(b"autorelease")
        self._sel_representation_png = self._selector(
            b"representationUsingType:properties:"
        )
        self._sel_bytes = self._selector(b"bytes")
        self._sel_length = self._selector(b"length")
        self._sel_dictionary = self._selector(b"dictionary")
        self._sel_count = self._selector(b"count")
        self._sel_object_at_index = self._selector(b"objectAtIndex:")
        self._pool_class = self._class(b"NSAutoreleasePool")
        self._bitmap_rep_class = self._class(b"NSBitmapImageRep")
        self._dictionary_class = self._class(b"NSDictionary")
        workspace_class = self._class(b"NSWorkspace")
        self._workspace = int(
            self._msg_id0(workspace_class, self._sel_shared_workspace) or 0
        )
        if not self._workspace:
            raise RuntimeError("native application workspace unavailable")
        self._cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFStringGetLength.restype = ctypes.c_long
        self._cf.CFStringGetMaximumSizeForEncoding.argtypes = [
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        self._cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetCString.restype = ctypes.c_bool
        self._cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self._cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None
        self._closed = False

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

    def _string(self, value: int | None) -> str:
        if not value:
            return ""
        length = self._cf.CFStringGetLength(value)
        capacity = (
            self._cf.CFStringGetMaximumSizeForEncoding(length, _CF_UTF8) + 1
        )
        buffer = ctypes.create_string_buffer(capacity)
        if not self._cf.CFStringGetCString(value, buffer, capacity, _CF_UTF8):
            return ""
        return buffer.value.decode("utf-8")

    def _pool(self) -> int:
        return int(self._msg_id0(self._pool_class, self._sel_new) or 0)

    def _drain(self, pool: int) -> None:
        if pool:
            self._msg_void0(pool, self._sel_drain)

    def _pump_run_loop(self, *, passes: int = 32) -> None:
        from .macos_native_query_owner import pump_main_native_events
        pump_main_native_events()

    def _running_observation(
        self, application: object | None, *, include_icon: bool = True
    ) -> _MacosApplicationObservation | None:
        if application is None or self._closed:
            return None
        try:
            bundle_id = self._string(
                int(self._msg_id0(application, self._sel_bundle_identifier) or 0)
            ).strip().lower()
            display_name = self._string(
                int(self._msg_id0(application, self._sel_localized_name) or 0)
            ).strip()
            process_id = int(
                self._msg_i32_0(application, self._sel_process_identifier)
            )
            if not bundle_id or not display_name or process_id <= 0:
                return None
            icon = int(self._msg_id0(application, self._sel_icon) or 0) if include_icon else 0
            icon_data_uri = self._image_data_uri(icon) if include_icon else None
            if include_icon and icon_data_uri is None:
                bundle_url = int(
                    self._msg_id0(application, self._sel_bundle_url) or 0
                )
                bundle_path = self._string(
                    int(self._msg_id0(bundle_url, self._sel_path) or 0)
                )
                icon_data_uri = self._icon_data_uri(bundle_path)
            return _MacosApplicationObservation(
                bundle_id,
                process_id,
                display_name,
                icon_data_uri,
            )
        except Exception:  # noqa: BLE001 - inventory/capture are best effort
            return None

    def _icon_data_uri(self, bundle_path: str) -> str | None:
        from .macos_native_query_owner import query_on_main
        return query_on_main(lambda: self._icon_data_uri_main(bundle_path))

    def _icon_data_uri_main(self, bundle_path: str) -> str | None:
        """Resolve one local app icon to PNG while retaining no path or image."""

        if not bundle_path:
            return None
        path_value = self._cf.CFStringCreateWithCString(
            None,
            bundle_path.encode("utf-8"),
            _CF_UTF8,
        )
        if not path_value:
            return None
        pool = self._pool()
        try:
            image = int(
                self._msg_id_id(
                    self._workspace,
                    self._sel_icon_for_file,
                    path_value,
                )
                or 0
            )
            return self._image_data_uri(image)
        except Exception:  # noqa: BLE001 - cosmetic local icon is best effort
            return None
        finally:
            self._cf.CFRelease(path_value)
            self._drain(pool)

    def _image_data_uri(self, image: int) -> str | None:
        if not image:
            return None
        try:
            # Ask AppKit for a small picker representation instead of expanding
            # every high-resolution icon frame into a multi-megabyte TIFF.
            rect = _NSRect(0, 0, 64, 64)
            cg_image = self._msg_cg_image(
                image, self._sel_cg_image, ctypes.byref(rect), None, None
            )
            if not cg_image:
                return None
            representation = self._msg_id_id(
                self._msg_id0(self._bitmap_rep_class, self._sel_alloc),
                self._sel_init_cg_image, cg_image,
            )
            if not representation:
                return None
            self._msg_id0(representation, self._sel_autorelease)
            properties = int(
                self._msg_id0(self._dictionary_class, self._sel_dictionary)
                or 0
            )
            png = int(
                self._msg_id_ulong_id(
                    representation,
                    self._sel_representation_png,
                    4,
                    properties,
                )
                or 0
            )
            length = int(self._msg_ulong_0(png, self._sel_length)) if png else 0
            pointer = int(self._msg_ptr0(png, self._sel_bytes) or 0) if png else 0
            if pointer <= 0 or length <= 0 or length > 8 * 1024 * 1024:
                return None
            payload = ctypes.string_at(pointer, length)
            return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
        except Exception:  # noqa: BLE001 - cosmetic local icon is best effort
            return None

    def frontmost_application(self) -> _MacosApplicationObservation | None:
        from .macos_native_query_owner import query_on_main
        return query_on_main(self._frontmost_application_main)

    def _frontmost_application_main(self) -> _MacosApplicationObservation | None:
        if self._closed:
            return None
        self._pump_run_loop()
        pool = self._pool()
        try:
            application = int(
                self._msg_id0(
                    self._workspace, self._sel_frontmost_application
                )
                or 0
            )
            return self._running_observation(application)
        finally:
            self._drain(pool)

    def running_applications(self) -> list[_MacosApplicationObservation]:
        from .macos_native_query_owner import query_on_main
        observations = query_on_main(self._running_applications_main)
        decorated = []
        for observation in observations:
            # Each cosmetic conversion is a separate bounded owner request;
            # the coarse identity snapshot never converts hundreds of icons.
            try:
                icon = query_on_main(lambda: self._running_icon_main(observation))
            except Exception:
                icon = None
            decorated.append(replace(observation, icon_data_uri=icon))
        return decorated

    def _running_icon_main(self, observation: _MacosApplicationObservation) -> str | None:
        if self._closed:
            return None
        message = _objc_callable(self._objc, ctypes.c_void_p, ctypes.c_int32)
        pool = self._pool()
        try:
            application = int(message(
                self._class(b"NSRunningApplication"),
                self._selector(b"runningApplicationWithProcessIdentifier:"),
                observation.process_identifier,
            ) or 0)
            current = self._running_observation(application or None)
            if current is not None and current.bundle_identifier == observation.bundle_identifier:
                return current.icon_data_uri
            return None
        finally:
            self._drain(pool)

    def _running_applications_main(self) -> list[_MacosApplicationObservation]:
        if self._closed:
            return []
        self._pump_run_loop()
        observations: list[_MacosApplicationObservation] = []
        seen: set[str] = set()
        pool = self._pool()
        try:
            frontmost_application = int(
                self._msg_id0(
                    self._workspace,
                    self._sel_frontmost_application,
                )
                or 0
            )
            frontmost = self._running_observation(frontmost_application, include_icon=False)
            if frontmost is not None:
                seen.add(frontmost.bundle_identifier)
                observations.append(frontmost)
            applications = int(
                self._msg_id0(
                    self._workspace, self._sel_running_applications
                )
                or 0
            )
            if not applications:
                return []
            count = min(
                int(self._msg_ulong_0(applications, self._sel_count)),
                512,
            )
            for index in range(count):
                application = int(
                    self._msg_id_ulong(
                        applications,
                        self._sel_object_at_index,
                        index,
                    )
                    or 0
                )
                observation = self._running_observation(application, include_icon=False)
                if observation is None or observation.bundle_identifier in seen:
                    continue
                seen.add(observation.bundle_identifier)
                observations.append(observation)
        except Exception:  # noqa: BLE001 - picker inventory is best effort
            return []
        finally:
            self._drain(pool)
        return observations

    def installed_applications(
        self, logical_ids: set[str] | None = None
    ) -> list[_MacosApplicationObservation]:
        if self._closed:
            return []
        roots = list(self._INSTALL_ROOTS)
        home = os.environ.get("CFFIXED_USER_HOME", "").strip() or os.environ.get(
            "HOME", ""
        ).strip()
        if home:
            roots.append(Path(home) / "Applications")
        applications: list[_MacosApplicationObservation] = []
        seen: set[str] = set()
        for root in roots:
            try:
                paths = sorted(root.glob("*.app"))
            except OSError:
                continue
            for path in paths:
                if len(applications) >= 512:
                    return applications
                try:
                    with (path / "Contents/Info.plist").open("rb") as handle:
                        info = plistlib.load(handle)
                    if not isinstance(info, dict):
                        continue
                    bundle_id = str(
                        info.get("CFBundleIdentifier") or ""
                    ).strip().lower()
                    display_name = str(
                        info.get("CFBundleDisplayName")
                        or info.get("CFBundleName")
                        or path.stem
                    ).strip()
                    if not bundle_id or not display_name or bundle_id in seen:
                        continue
                    seen.add(bundle_id)
                    if logical_ids is not None and _opaque("app", bundle_id) not in logical_ids:
                        continue
                    applications.append(
                        _MacosApplicationObservation(
                            bundle_id,
                            0,
                            display_name,
                            self._icon_data_uri(str(path)),
                        )
                    )
                except Exception:  # noqa: BLE001 - one broken bundle is skipped
                    continue
        return applications

    def close(self) -> None:
        self._closed = True
        self._workspace = 0


_APPLICATION_SERVICES = Path(
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)
_CORE_FOUNDATION = Path(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_CF_UTF8 = 0x08000100
_AX_MESSAGE_TIMEOUT_SECONDS = 0.25
_AX_READ_DEADLINE_SECONDS = 1.5
_BROWSER_CAPTURE_TIMEOUT_SECONDS = 2.0
_AX_PAGE_CONTENT_ROLES = frozenset({"AXDocument", "AXWebArea"})


def _may_descend_browser_accessibility(role: str) -> bool:
    return role not in _AX_PAGE_CONTENT_ROLES


def _is_browser_address_control(
    browser: str,
    role: str,
    identifier: str,
    description: str,
) -> bool:
    """Match only known browser-chrome controls, never an arbitrary field."""

    if role not in {"AXTextField", "AXComboBox"}:
        return False
    if browser == "com.apple.safari":
        return identifier == "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD"
    if browser == "org.mozilla.firefox":
        return identifier in {"urlbar-input", "urlbar"}
    return (
        identifier in {"Address and search bar", "address-and-search-bar"}
        or description == "Address and search bar"
    )


def _read_preferred_browser_window(
    root: int,
    *,
    read_attribute: Callable[[int, str], int | None],
    release: Callable[[int], None],
    walk: Callable[[int, int], str | None],
) -> str | None:
    """Read focused browser chrome, using main only when focused is absent."""

    focused = read_attribute(root, "AXFocusedWindow")
    if focused:
        try:
            return walk(focused, 0)
        finally:
            release(focused)
    main = read_attribute(root, "AXMainWindow")
    try:
        return walk(main, 0) if main else None
    finally:
        if main:
            release(main)


class _NativeAccessibilityReader:
    """Minimal ctypes AX reader that owns and releases every copied CF value."""

    def __init__(self) -> None:
        self._ax = ctypes.CDLL(str(_APPLICATION_SERVICES))
        self._cf = ctypes.CDLL(str(_CORE_FOUNDATION))
        self._ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        self._ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
        self._ax.AXUIElementCopyAttributeValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        self._ax.AXUIElementSetMessagingTimeout.argtypes = [
            ctypes.c_void_p,
            ctypes.c_float,
        ]
        self._ax.AXUIElementSetMessagingTimeout.restype = ctypes.c_int32
        self._cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self._cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self._cf.CFStringGetTypeID.restype = ctypes.c_ulong
        self._cf.CFArrayGetTypeID.restype = ctypes.c_ulong
        self._cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        self._cf.CFGetTypeID.restype = ctypes.c_ulong
        self._cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFStringGetLength.restype = ctypes.c_long
        self._cf.CFStringGetMaximumSizeForEncoding.argtypes = [
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        self._cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetCString.restype = ctypes.c_bool
        self._cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        self._cf.CFArrayGetCount.restype = ctypes.c_long
        self._cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        self._cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None
        self._deadline = 0.0
        self._cancel_event: threading.Event | None = None

    def _attribute(self, element: int, name: str) -> int | None:
        if (
            (self._cancel_event is not None and self._cancel_event.is_set())
            or (self._deadline and time.monotonic() >= self._deadline)
        ):
            return None
        key = self._cf.CFStringCreateWithCString(
            None, name.encode("ascii"), _CF_UTF8
        )
        if not key:
            return None
        value = ctypes.c_void_p()
        try:
            self._ax.AXUIElementSetMessagingTimeout(
                element,
                ctypes.c_float(_AX_MESSAGE_TIMEOUT_SECONDS),
            )
            status = self._ax.AXUIElementCopyAttributeValue(
                element,
                key,
                ctypes.byref(value),
            )
        finally:
            self._cf.CFRelease(key)
        return int(value.value) if status == 0 and value.value else None

    def _string(self, value: int | None) -> str | None:
        if not value or self._cf.CFGetTypeID(value) != self._cf.CFStringGetTypeID():
            return None
        length = self._cf.CFStringGetLength(value)
        capacity = (
            self._cf.CFStringGetMaximumSizeForEncoding(length, _CF_UTF8) + 1
        )
        buffer = ctypes.create_string_buffer(capacity)
        if not self._cf.CFStringGetCString(value, buffer, capacity, _CF_UTF8):
            return None
        return buffer.value.decode("utf-8")

    def browser_location(
        self,
        application: _MacosApplicationObservation,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> str | None:
        root = self._ax.AXUIElementCreateApplication(
            application.process_identifier
        )
        if not root:
            return None
        self._ax.AXUIElementSetMessagingTimeout(
            root,
            ctypes.c_float(_AX_MESSAGE_TIMEOUT_SECONDS),
        )
        self._cancel_event = cancel_event
        self._deadline = min(
            deadline or float("inf"),
            time.monotonic() + _AX_READ_DEADLINE_SECONDS,
        )
        visited: set[int] = set()
        count = 0

        def walk(element: int, depth: int) -> str | None:
            nonlocal count
            if (
                element in visited
                or count >= 512
                or depth > 12
                or (cancel_event is not None and cancel_event.is_set())
                or time.monotonic() >= self._deadline
            ):
                return None
            visited.add(element)
            count += 1
            role_value = self._attribute(element, "AXRole")
            try:
                role = self._string(role_value) or ""
            finally:
                if role_value:
                    self._cf.CFRelease(role_value)
            if not _may_descend_browser_accessibility(role):
                return None
            strings: dict[str, str] = {}
            for attribute in ("AXIdentifier", "AXDescription"):
                value = self._attribute(element, attribute)
                try:
                    strings[attribute] = self._string(value) or ""
                finally:
                    if value:
                        self._cf.CFRelease(value)
            identifier = strings["AXIdentifier"]
            description = strings["AXDescription"]
            browser = application.bundle_identifier
            if _is_browser_address_control(
                browser,
                role,
                identifier,
                description,
            ):
                value = self._attribute(element, "AXValue")
                try:
                    return self._string(value)
                finally:
                    if value:
                        self._cf.CFRelease(value)
            children = self._attribute(element, "AXChildren")
            try:
                if (
                    not children
                    or self._cf.CFGetTypeID(children)
                    != self._cf.CFArrayGetTypeID()
                ):
                    return None
                child_count = min(self._cf.CFArrayGetCount(children), 128)
                for index in range(child_count):
                    child = self._cf.CFArrayGetValueAtIndex(children, index)
                    if child:
                        found = walk(int(child), depth + 1)
                        if found is not None:
                            return found
            finally:
                if children:
                    self._cf.CFRelease(children)
            return None

        try:
            return _read_preferred_browser_window(
                root,
                read_attribute=self._attribute,
                release=self._cf.CFRelease,
                walk=walk,
            )
        finally:
            self._deadline = 0.0
            self._cancel_event = None
            self._cf.CFRelease(root)


def _native_browser_location(application: _MacosApplicationObservation) -> str | None:
    """Read only a recognized browser's address control through Accessibility.

    The walk is bounded and browser-specific. It never asks for window titles,
    selected text, page content, history, or an arbitrary edit-control value.
    A localized/unrecognized surface therefore returns ``None``.
    """

    try:
        return _NativeAccessibilityReader().browser_location(application)
    except Exception:  # noqa: BLE001 - browser discovery never breaks dictation
        return None


@dataclass(slots=True)
class _BrowserLocationRequest:
    application: _MacosApplicationObservation
    cancel_event: threading.Event
    done_event: threading.Event = field(default_factory=threading.Event)
    result: str | None = None


class _BrowserLocationWorker:
    """One adapter-owned bounded worker for synchronous Accessibility reads."""

    def __init__(
        self,
        reader: Callable[[_MacosApplicationObservation], str | None],
        *,
        timeout_seconds: float,
    ) -> None:
        self._reader = reader
        try:
            parameters = inspect.signature(reader).parameters
        except (TypeError, ValueError):
            parameters = {}
        self._reader_accepts_bounds = {
            "cancel_event",
            "deadline",
        }.issubset(parameters)
        self._timeout_seconds = timeout_seconds
        self._requests: queue.Queue[_BrowserLocationRequest | None] = queue.Queue(1)
        self._lock = threading.Lock()
        self._active: _BrowserLocationRequest | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="whisperstream-macos-context",
            daemon=True,
        )
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _call_reader(self, request: _BrowserLocationRequest) -> str | None:
        deadline = time.monotonic() + self._timeout_seconds
        if self._reader is _native_browser_location:
            try:
                return _NativeAccessibilityReader().browser_location(
                    request.application,
                    cancel_event=request.cancel_event,
                    deadline=deadline,
                )
            except Exception:  # noqa: BLE001 - native discovery is best effort
                return None
        if self._reader_accepts_bounds:
            return self._reader(  # type: ignore[call-arg]
                request.application,
                cancel_event=request.cancel_event,
                deadline=deadline,
            )
        return self._reader(request.application)

    def _run(self) -> None:
        while True:
            request = self._requests.get()
            if request is None:
                return
            try:
                value = self._call_reader(request)
                if not request.cancel_event.is_set():
                    request.result = value
            except Exception:  # noqa: BLE001 - application-only is fallback
                request.result = None
            finally:
                request.done_event.set()
                with self._lock:
                    if self._active is request:
                        self._active = None

    def read(self, application: _MacosApplicationObservation) -> str | None:
        request = _BrowserLocationRequest(application, threading.Event())
        with self._lock:
            if self._closed or self._active is not None:
                return None
            self._active = request
        try:
            self._requests.put_nowait(request)
        except queue.Full:
            with self._lock:
                if self._active is request:
                    self._active = None
            return None
        if request.done_event.wait(self._timeout_seconds):
            return request.result
        request.cancel_event.set()
        return None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = self._active
        if active is not None:
            active.cancel_event.set()
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            # The worker observes the active cancellation before returning.
            pass
        self._thread.join(_BROWSER_CAPTURE_TIMEOUT_SECONDS + 0.5)
        if self._thread.is_alive():
            try:
                self._requests.put_nowait(None)
            except queue.Full:
                pass
            self._thread.join(_AX_READ_DEADLINE_SECONDS + 0.5)


def _host_from_location(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        host = parsed.hostname
        normalized = normalize_favicon_domain(host)
        if normalized and normalized.startswith("www."):
            normalized = normalized[4:]
        return normalized or None
    except (TypeError, ValueError, UnicodeError):
        return None


def _local_favicon(home: Path, host: str) -> str | None:
    """Best-effort local browser-cache lookup with no network or retained path."""

    chromium_roots = (
        home / "Library/Application Support/Google/Chrome",
        home / "Library/Application Support/Microsoft Edge",
        home / "Library/Application Support/BraveSoftware/Brave-Browser",
        home / "Library/Application Support/Chromium",
    )
    for root in chromium_roots:
        try:
            profiles = sorted(root.glob("*/Favicons"))[:64]
        except OSError:
            continue
        for database in profiles:
            icon = _query_chromium(str(database), host)
            if icon:
                return icon
    firefox = home / "Library/Application Support/Firefox/Profiles"
    try:
        profiles = sorted(firefox.glob("*/favicons.sqlite"))[:64]
    except OSError:
        profiles = []
    for database in profiles:
        icon = _query_firefox(str(database), host)
        if icon:
            return icon
    return None


class MacosApplicationContextAdapter:
    """Capture one logical Darwin context and own all cosmetic identity state."""

    def __init__(
        self,
        *,
        workspace: _MacosWorkspace | None = None,
        browser_location_reader: Callable[[_MacosApplicationObservation], str | None]
        = _native_browser_location,
        browser_bundle_ids: Iterable[str] = _BROWSER_BUNDLE_IDS,
        favicon_resolver: Callable[[str], str | None] | None = None,
        token_factory: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        browser_capture_timeout: float = _BROWSER_CAPTURE_TIMEOUT_SECONDS,
    ) -> None:
        self._workspace = workspace or _NativeMacosWorkspace()
        self._browser_location_reader = browser_location_reader
        self._browser_capture_timeout = browser_capture_timeout
        self._browser_worker: _BrowserLocationWorker | None = None
        self._browser_bundle_ids = {
            value.strip().lower() for value in browser_bundle_ids if value.strip()
        }
        self._browser_application_ids = {
            _opaque("app", value) for value in self._browser_bundle_ids
        }
        self._token_factory = token_factory or (lambda: uuid.uuid4().hex)
        if favicon_resolver is None:
            home = os.environ.get("CFFIXED_USER_HOME", "").strip() or os.environ.get(
                "HOME", ""
            ).strip()
            root = Path(home) if home else Path("/")
            favicon_resolver = lambda host: _local_favicon(root, host)
        self._favicon_resolver = favicon_resolver
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._assets: OrderedDict[str, str] = OrderedDict()
        self._favicon_cache: dict[str, tuple[float, str | None]] = {}
        self._closed = False

    def _read_browser_location(
        self,
        observation: _MacosApplicationObservation,
    ) -> str | None:
        """Create the bounded AX worker only when browser context is requested."""

        with self._lock:
            if self._closed:
                return None
            if self._browser_worker is None:
                self._browser_worker = _BrowserLocationWorker(
                    self._browser_location_reader,
                    timeout_seconds=self._browser_capture_timeout,
                )
            worker = self._browser_worker
        return worker.read(observation)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def __del__(self) -> None:
        # Keep partially constructed or short-lived adapters exact as well as
        # the explicit application shutdown path.
        try:
            if hasattr(self, "_lock"):
                self.close()
        except Exception:  # noqa: BLE001 - destructors must never escape
            pass

    def _application_ref(
        self,
        observation: _MacosApplicationObservation,
    ) -> ApplicationRef:
        return ApplicationRef(
            _opaque("app", observation.bundle_identifier),
            f"macos-app-instance:{self._token_factory()}",
        )

    def capture(
        self,
        generation: int,
        requirement: ContextRequirement,
    ) -> ContextSnapshot:
        if self.closed:
            return ContextSnapshot(
                generation,
                requirement,
                ContextStatus.FAILED,
                failure=ContextFailure.NATIVE_ERROR,
            )
        try:
            observation = self._workspace.frontmost_application()
        except Exception:  # noqa: BLE001 - foreground capture never breaks dictation
            observation = None
        if observation is None:
            return ContextSnapshot(
                generation,
                requirement,
                ContextStatus.UNAVAILABLE,
                failure=ContextFailure.NO_FOREGROUND_APPLICATION,
            )
        application = self._application_ref(observation)
        if (
            requirement is ContextRequirement.APPLICATION_AND_BROWSER_PAGE
            and observation.bundle_identifier in self._browser_bundle_ids
        ):
            try:
                host = _host_from_location(
                    self._read_browser_location(observation)
                )
            except Exception:  # noqa: BLE001 - application-only is required fallback
                host = None
            if host:
                return ContextSnapshot(
                    generation,
                    requirement,
                    ContextStatus.RESOLVED,
                    application,
                    BrowserPageRef(application, host),
                )
        return ContextSnapshot(
            generation,
            requirement,
            (
                ContextStatus.APPLICATION_ONLY
                if requirement is ContextRequirement.APPLICATION_AND_BROWSER_PAGE
                else ContextStatus.RESOLVED
            ),
            application,
        )

    def _descriptors(
        self, scope: ApplicationInventoryScope, logical_ids: set[str] | None = None,
        *, payloads: dict[str, str] | None = None,
    ) -> tuple[ApplicationDescriptor, ...]:
        if self.closed:
            return ()
        try:
            observations = (
                self._workspace.running_applications()
                if scope is ApplicationInventoryScope.RUNNING
                else self._workspace.installed_applications(logical_ids)
            )
        except Exception:  # noqa: BLE001 - cosmetic inventory is best effort
            return ()
        descriptors: list[ApplicationDescriptor] = []
        for observation in observations:
            try:
                icon_token = None
                if observation.icon_data_uri:
                    icon_token = _opaque("icon", observation.icon_data_uri)
                    if payloads is not None:
                        payloads[icon_token] = observation.icon_data_uri
                    with self._lock:
                        if not self._closed:
                            self._assets[icon_token] = observation.icon_data_uri
                            self._assets.move_to_end(icon_token)
                            while len(self._assets) > _MAX_ICON_ASSETS:
                                self._assets.popitem(last=False)
                descriptors.append(
                    ApplicationDescriptor(
                        self._application_ref(observation),
                        observation.display_name,
                        icon_token,
                    )
                )
            except (TypeError, ValueError):
                continue
        return tuple(descriptors)

    def inventory(
        self, scope: ApplicationInventoryScope
    ) -> tuple[ApplicationDescriptor, ...]:
        return self._descriptors(scope)

    def icon_payload(self, token: str) -> str | None:
        with self._lock:
            payload = None if self._closed else self._assets.get(token)
            if payload is not None:
                self._assets.move_to_end(token)
            return payload

    def _rows(
        self, scope: ApplicationInventoryScope, logical_ids: set[str] | None = None
    ) -> list[dict[str, object]]:
        # A large single response owns its icons until serialization, even
        # when it exceeds the process-wide reuse cache.
        payloads: dict[str, str] = {}
        descriptors = self._descriptors(scope, logical_ids, payloads=payloads)
        return [
            {
                "process_name": descriptor.application.logical_id,
                "display_name": descriptor.display_name,
                "icon": payloads.get(descriptor.icon_token or ""),
                **(
                    {"installed": True}
                    if scope is ApplicationInventoryScope.INSTALLED
                    else {}
                ),
            }
            for descriptor in descriptors
        ]

    def running_apps(self) -> list[dict[str, object]]:
        return self._rows(ApplicationInventoryScope.RUNNING)

    def installed_apps(self) -> list[dict[str, object]]:
        return self._rows(ApplicationInventoryScope.INSTALLED)

    def resolve_apps(
        self,
        names: Iterable[object] | None,
        *,
        known_paths: Mapping[str, str] | None = None,
        default_names: set[str] | None = None,
    ) -> list[dict[str, object]]:
        del known_paths, default_names
        if self.closed:
            return []
        names = [
            value.strip().lower() for value in names or ()
            if isinstance(value, str) and value.strip()
        ]
        # Legacy Windows rules and an empty map need no native icon inventory.
        logical_ids = {name for name in names if name.startswith("macos-app:sha256:")}
        installed = {
            str(row["process_name"]): row
            for row in self._rows(ApplicationInventoryScope.INSTALLED, logical_ids)
        } if logical_ids else {}
        results: list[dict[str, object]] = []
        for value in names or ():
            if not isinstance(value, str) or not value.strip():
                continue
            key = value.strip().lower()
            row = installed.get(key)
            results.append(
                dict(row)
                if row is not None
                else {
                    "process_name": key,
                    "display_name": key,
                    "icon": None,
                    "installed": False,
                }
            )
        return results

    def resolve_path(self, path: str) -> None:
        # Raw filesystem paths never cross the shared Mac routing boundary.
        del path
        return None

    def resolve_favicons(self, domains: Iterable[object]) -> list[dict[str, object]]:
        if self.closed:
            return []
        results: list[dict[str, object]] = []
        for value in domains:
            host = normalize_favicon_domain(value)
            if host is None:
                continue
            if host.startswith("www."):
                host = host[4:]
            try:
                now = self._monotonic()
                with self._lock:
                    cached = self._favicon_cache.get(host, ...)
                    ttl = (
                        _FAVICON_MISS_TTL_SECONDS
                        if cached is not ... and cached[1] is None else 300.0
                    )
                    if cached is not ... and now - cached[0] >= ttl:
                        self._favicon_cache.pop(host, None)
                        cached = ...
                if cached is ...:
                    resolved = self._favicon_resolver(host)
                    with self._lock:
                        if not self._closed:
                            if len(self._favicon_cache) >= 128:
                                oldest = next(iter(self._favicon_cache), None)
                                if oldest is not None:
                                    self._favicon_cache.pop(oldest, None)
                            self._favicon_cache[host] = (now, resolved)
                    icon = resolved
                else:
                    icon = cached[1]
                results.append({"domain": host, "icon": icon})
            except Exception:  # noqa: BLE001 - local cosmetic lookup is best effort
                results.append({"domain": host, "icon": None})
        return results

    def safe_capture_metadata(self, snapshot: ContextSnapshot) -> dict[str, object]:
        browser = bool(
            snapshot.application is not None
            and snapshot.application.logical_id in self._browser_application_ids
        )
        return {
            "browser": browser,
            "domain_read": (
                "n/a"
                if not browser
                else "hit" if snapshot.browser_page is not None else "miss"
            ),
        }

    def uia_available(self) -> bool:
        try:
            library = ctypes.CDLL(str(_APPLICATION_SERVICES))
            library.AXIsProcessTrusted.restype = ctypes.c_bool
            return bool(library.AXIsProcessTrusted())
        except Exception:  # noqa: BLE001 - passive status only
            return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._assets.clear()
            self._favicon_cache.clear()
        try:
            self._workspace.close()
        except Exception:  # noqa: BLE001 - close remains idempotent
            pass
        worker = self._browser_worker
        if worker is not None:
            worker.close()


assert isinstance(
    _protocol_check := MacosApplicationContextAdapter(workspace=type(
        "_ProtocolCheckWorkspace",
        (),
        {
            "frontmost_application": lambda self: None,
            "running_applications": lambda self: [],
            "installed_applications": lambda self: [],
            "close": lambda self: None,
        },
    )()),
    ApplicationContextAdapter,
)
_protocol_check.close()
del _protocol_check


__all__ = ["MacosApplicationContextAdapter"]
