#!/usr/bin/env python3
"""Exercise real macOS hotkey and delivery adapters safely.

The probe creates two disposable, ad-hoc-signed AppKit applications and reads
only their synthetic focused controls through Accessibility. It posts F18/F19
events through Core Graphics, uses the production delivery adapters, restores
the general pasteboard through the production clipboard transaction, and
removes every exact process and temporary artifact it creates.

The active event tap and delivery require Accessibility. Input Monitoring is
observed in the report but is not a prerequisite. This source-runner F18/F19
check does not prove installed-shell identity or physical Right Shift behavior.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import psutil

from whisperstream.delivery_contract import (
    DeliveryRequest,
    DeliveryRoute,
    DeliveryStatus,
    DeliveryStyle,
)
from whisperstream.macos_delivery_adapter import MacosDeliveryAdapter
from whisperstream.macos_permission_status import MacosPermissionStatusProvider
from whisperstream.macos_shortcuts import MacosShortcutAdapter
from whisperstream.permission_contract import (
    PermissionState,
    required_permissions_for_operation,
)


PHASE_ROOT = Path("/tmp/whisperstream-macos-owner-validation")
REPORT_ROOT = PHASE_ROOT / "reports"
RUN_ROOT = PHASE_ROOT / "runs"
TARGET_TEXT = "NativeTyping42 ClipboardPaste43"
_CF_STRING_ENCODING_UTF8 = 0x08000100


class NativeInteractionProbeError(RuntimeError):
    """A content-safe native interaction failure."""


class NativeInteractionTccBlocked(NativeInteractionProbeError):
    """The public grant is present but macOS rejects cross-app event delivery."""


def _wait(predicate, *, timeout: float, label: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise NativeInteractionProbeError(f"timed out waiting for {label}")


def _require_native_permissions() -> dict[str, str]:
    translated = subprocess.run(
        ["sysctl", "-in", "sysctl.proc_translated"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if (
        platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or translated not in {"", "0"}
    ):
        raise NativeInteractionProbeError("native Darwin arm64 is required")
    report = MacosPermissionStatusProvider().query_permissions()
    states = {item.kind: item.state for item in report.permissions}
    # Match the production active/default tap. Preserve every observed state
    # below, including unknown or granted Input Monitoring, so a passing run
    # cannot silently claim evidence of an absent separate grant.
    for kind in required_permissions_for_operation("global-shortcuts"):
        if states.get(kind) is not PermissionState.GRANTED:
            raise NativeInteractionProbeError(
                f"required permission is not granted kind={kind.value}"
            )
    return {kind.value: state.value for kind, state in states.items()}


def _write_synthetic_app(root: Path, name: str, bundle_id: str) -> Path:
    app = root / f"{name}.app"
    contents = app / "Contents"
    executable = contents / "MacOS" / name
    executable.parent.mkdir(parents=True)
    source = root / f"{name}.swift"
    source.write_text(
        r'''
import AppKit

final class Delegate: NSObject, NSApplicationDelegate {
    var window: NSWindow?
    var field: NSTextField?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let mode = CommandLine.arguments[3]
        let window = NSWindow(
            contentRect: NSRect(x: 180, y: 180, width: 480, height: 180),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Synthetic Native Target"
        if mode == "target" {
            let field = NSTextField(frame: NSRect(x: 30, y: 75, width: 420, height: 32))
            field.identifier = NSUserInterfaceItemIdentifier("synthetic-target-field")
            field.setAccessibilityIdentifier("synthetic-target-field")
            field.stringValue = ""
            window.contentView?.addSubview(field)
            window.makeFirstResponder(field)
            self.field = field
        } else {
            let label = NSTextField(labelWithString: "Synthetic focus sentinel")
            label.frame = NSRect(x: 30, y: 75, width: 420, height: 32)
            window.contentView?.addSubview(label)
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        for attempt in 1...20 {
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(attempt * 100)) {
                window.makeKeyAndOrderFront(nil)
                if mode == "target", let field = self.field {
                    window.makeFirstResponder(field)
                }
                NSApp.activate(ignoringOtherApps: true)
            }
        }
        self.window = window
        writeReceipt("started")
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        writeReceipt("active")
    }

    func writeReceipt(_ state: String) {
        let receipt = CommandLine.arguments[1]
        let nonce = CommandLine.arguments[2]
        let value = "\(state)\n\(ProcessInfo.processInfo.processIdentifier)\n\(nonce)\n"
        try? value.write(toFile: receipt, atomically: true, encoding: .utf8)
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
'''.strip()
        + "\n",
        encoding="utf-8",
    )
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": bundle_id,
                "CFBundleName": name,
                "CFBundleExecutable": name,
                "CFBundlePackageType": "APPL",
                "LSMinimumSystemVersion": "14.0",
                "NSPrincipalClass": "NSApplication",
            },
            handle,
        )
    subprocess.run(
        ["swiftc", str(source), "-o", str(executable)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(app)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return app


def _read_receipt(path: Path, nonce: str) -> tuple[str, int] | None:
    try:
        state, raw_pid, actual_nonce = path.read_text(encoding="utf-8").splitlines()
        pid = int(raw_pid)
    except (OSError, ValueError):
        return None
    if state not in {"started", "active"} or actual_nonce != nonce or pid <= 0:
        return None
    return state, pid


def _launch_app(
    app: Path,
    *,
    receipt: Path,
    nonce: str,
    mode: str,
) -> tuple[subprocess.Popen[bytes], int, float]:
    broker = subprocess.Popen(
        [
            "/usr/bin/open",
            "-W",
            "-n",
            str(app),
            "--args",
            str(receipt),
            nonce,
            mode,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    owned_identity: tuple[int, float] | None = None

    def activation_receipt() -> tuple[str, int] | None:
        nonlocal owned_identity
        value = _read_receipt(receipt, nonce)
        if value is None:
            return None
        _, pid = value
        if owned_identity is None:
            owned_identity = (pid, psutil.Process(pid).create_time())
        if pid != owned_identity[0] or not _identity_alive(*owned_identity):
            raise NativeInteractionProbeError("synthetic app identity changed")
        return value if value[0] == "active" else None

    try:
        _, pid = _wait(
            activation_receipt,
            timeout=15.0,
            label=f"{mode} app activation",
        )
        # Let the synthetic app's bounded activation retries drain before a
        # second disposable app participates in focus-restoration checks.
        time.sleep(2.1)
        assert owned_identity is not None
        return broker, pid, owned_identity[1]
    except Exception:
        try:
            if owned_identity is not None:
                _stop_identity(*owned_identity)
        finally:
            if broker.poll() is None:
                broker.terminate()
                try:
                    broker.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    broker.kill()
                    broker.wait(timeout=5.0)
        raise


def _identity_alive(pid: int, created: float) -> bool:
    try:
        return abs(psutil.Process(pid).create_time() - created) < 0.001
    except (psutil.Error, ProcessLookupError):
        return False


def _stop_identity(pid: int, created: float) -> None:
    if not _identity_alive(pid, created):
        return
    process = psutil.Process(pid)
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


class _AccessibilityReader:
    def __init__(self) -> None:
        ax_path = ctypes.util.find_library("ApplicationServices")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ax_path or not cf_path:
            raise NativeInteractionProbeError("Accessibility framework unavailable")
        self.ax = ctypes.CDLL(ax_path)
        self.cf = ctypes.CDLL(cf_path)
        self.ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
        self.ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
        self.ax.AXUIElementCopyAttributeValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        self.cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self.cf.CFStringGetLength.restype = ctypes.c_long
        self.cf.CFStringGetMaximumSizeForEncoding.argtypes = [
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self.cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        self.cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cf.CFRelease.restype = None

    def _attribute(self, element: int, name: str) -> int:
        key = int(
            self.cf.CFStringCreateWithCString(
                None, name.encode("utf-8"), _CF_STRING_ENCODING_UTF8
            )
            or 0
        )
        if not key:
            raise NativeInteractionProbeError("Accessibility key creation failed")
        value = ctypes.c_void_p()
        try:
            status = self.ax.AXUIElementCopyAttributeValue(
                element, key, ctypes.byref(value)
            )
        finally:
            self.cf.CFRelease(key)
        if status != 0 or not value.value:
            raise NativeInteractionProbeError(
                f"Accessibility attribute unavailable name={name}"
            )
        return int(value.value)

    def _string(self, value: int) -> str:
        length = int(self.cf.CFStringGetLength(value))
        maximum = int(
            self.cf.CFStringGetMaximumSizeForEncoding(
                length, _CF_STRING_ENCODING_UTF8
            )
        )
        buffer = ctypes.create_string_buffer(maximum + 1)
        if not self.cf.CFStringGetCString(
            value, buffer, len(buffer), _CF_STRING_ENCODING_UTF8
        ):
            raise NativeInteractionProbeError("Accessibility string conversion failed")
        return buffer.value.decode("utf-8")

    def focused(self, pid: int) -> tuple[str, str]:
        application = int(self.ax.AXUIElementCreateApplication(pid) or 0)
        if not application:
            raise NativeInteractionProbeError("Accessibility application unavailable")
        element = role = value = 0
        try:
            element = self._attribute(application, "AXFocusedUIElement")
            role = self._attribute(element, "AXRole")
            value = self._attribute(element, "AXValue")
            return self._string(role), self._string(value)
        finally:
            for item in (value, role, element, application):
                if item:
                    self.cf.CFRelease(item)


class _CoreGraphicsPoster:
    def __init__(self) -> None:
        cg_path = ctypes.util.find_library("CoreGraphics")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not cg_path or not cf_path:
            raise NativeInteractionProbeError("Core Graphics unavailable")
        self.cg = ctypes.CDLL(cg_path)
        self.cf = ctypes.CDLL(cf_path)
        self.cg.CGEventCreateKeyboardEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_bool,
        ]
        self.cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        self.cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        self.cg.CGEventPost.restype = None
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cf.CFRelease.restype = None

    def post(self, keycode: int, pressed: bool) -> None:
        event = int(self.cg.CGEventCreateKeyboardEvent(None, keycode, pressed) or 0)
        if not event:
            raise NativeInteractionProbeError("Core Graphics event creation failed")
        try:
            self.cg.CGEventPost(0, event)
        finally:
            self.cf.CFRelease(event)


def _frontmost_pid() -> int:
    from whisperstream.macos_delivery_adapter import _NativeMacosDeliveryBridge

    target = _NativeMacosDeliveryBridge().frontmost_target()
    return 0 if target is None else int(target.pid)


def _run_hotkeys(logger: logging.Logger) -> dict[str, object]:
    callbacks: list[str] = []
    adapter = MacosShortcutAdapter(logger=logger)
    poster = _CoreGraphicsPoster()
    try:
        adapter.register_push_to_talk(
            "f18",
            lambda: callbacks.append("hold-begin"),
            lambda: callbacks.append("hold-end"),
        )
        adapter.register_toggle(
            "f19",
            lambda: callbacks.append("toggle-start"),
            lambda: callbacks.append("toggle-stop"),
        )
        for keycode, pressed in (
            (79, True),
            (79, True),
            (79, False),
            (79, False),
            (80, True),
            (80, False),
            (80, True),
            (80, False),
        ):
            poster.post(keycode, pressed)
            time.sleep(0.04)
        _wait(
            lambda: callbacks
            == ["hold-begin", "hold-end", "toggle-start", "toggle-stop"],
            timeout=5.0,
            label="native shortcut callbacks",
        )
    finally:
        adapter.close()
    return {
        "posted_events": 8,
        "callbacks": len(callbacks),
        "ptt_exact_once": callbacks[:2] == ["hold-begin", "hold-end"],
        "toggle_exact_once": callbacks[2:] == ["toggle-start", "toggle-stop"],
        "listener_failure": (
            None
            if adapter.last_listener_failure is None
            else adapter.last_listener_failure.value
        ),
        "cleanup_failure": (
            None
            if adapter.last_cleanup_failure is None
            else adapter.last_cleanup_failure.value
        ),
    }


def _run_delivery(
    logger: logging.Logger,
    reader: _AccessibilityReader,
    target_pid: int,
) -> dict[str, object]:
    delivery = MacosDeliveryAdapter(
        keyboard_manager=None,
        logger=logger,
        clipboard_behavior=True,
        clipboard_clear_delay=0.1,
        keystroke_delay_ms=0,
    )
    receipts = []
    try:
        _wait(
            lambda: _frontmost_pid() == target_pid,
            timeout=5.0,
            label="target focus",
        )
        role, value = reader.focused(target_pid)
        if role != "AXTextField" or value:
            raise NativeInteractionProbeError("synthetic target did not start empty")
        poster = _CoreGraphicsPoster()
        poster.post(0, True)
        poster.post(0, False)
        try:
            _wait(
                lambda: len(reader.focused(target_pid)[1]) == 1,
                timeout=2.0,
                label="physical keycode delivery preflight",
            )
        except NativeInteractionProbeError as exc:
            raise NativeInteractionTccBlocked(
                "Core Graphics input did not reach the focused synthetic target"
            ) from exc
        prefix = reader.focused(target_pid)[1]

        target = delivery.resolve_target("native-type", 1, DeliveryRoute.FOCUSED).target
        if target is None:
            raise NativeInteractionProbeError("focused delivery target was not resolved")
        typed_receipt = delivery.deliver(
            DeliveryRequest(
                "native-type",
                1,
                target,
                DeliveryStyle.KEYSTROKE,
                "NativeTyping42",
            )
        )
        receipts.append(typed_receipt)
        if typed_receipt.status is not DeliveryStatus.FOCUSED_ATTEMPTED:
            failure = None if typed_receipt.failure is None else typed_receipt.failure.value
            raise NativeInteractionProbeError(
                f"direct typing failed before input failure={failure}"
            )
        try:
            _wait(
                lambda: reader.focused(target_pid)[1] == prefix + "NativeTyping42",
                timeout=2.0,
                label="direct typing assertion",
            )
            typed_ok = True
        except NativeInteractionProbeError:
            typed_ok = False

        target = delivery.resolve_target("native-paste", 2, DeliveryRoute.FOCUSED).target
        if target is None:
            raise NativeInteractionProbeError("clipboard delivery target was not resolved")
        receipts.append(
            delivery.deliver(
                DeliveryRequest(
                    "native-paste",
                    2,
                    target,
                    DeliveryStyle.CLIPBOARD,
                    " ClipboardPaste43",
                )
            )
        )
        expected_after_paste = (
            "NativeTyping42 ClipboardPaste43"
            if typed_ok
            else prefix + " ClipboardPaste43"
        )
        if typed_ok:
            expected_after_paste = prefix + expected_after_paste
        try:
            _wait(
                lambda: reader.focused(target_pid)[1] == expected_after_paste,
                timeout=2.0,
                label="clipboard delivery assertion",
            )
            pasted_ok = True
        except NativeInteractionProbeError:
            pasted_ok = False
        if not typed_ok:
            actual_role, actual_value = reader.focused(target_pid)
            digest = hashlib.sha256(actual_value.encode()).hexdigest()
            raise NativeInteractionProbeError(
                "direct typing assertion failed "
                f"role={actual_role} length={len(actual_value)} sha256={digest} "
                f"clipboard_inserted={pasted_ok}"
            )
        if not pasted_ok:
            raise NativeInteractionProbeError("clipboard delivery assertion failed")
        _wait(
            lambda: delivery.pending_restore_count == 0,
            timeout=5.0,
            label="clipboard restoration",
        )

        return {
            "accessibility_role": role,
            "focused_delivery": receipts[0].status.value,
            "clipboard_delivery": receipts[1].status.value,
            "all_units_completed": all(item.units_completed == 1 for item in receipts),
            "clipboard_restored": delivery.pending_restore_count == 0,
            "asserted_value_sha256": hashlib.sha256(
                (prefix + TARGET_TEXT).encode()
            ).hexdigest(),
        }
    finally:
        delivery.close()


def run_probe(output: Path) -> dict[str, object]:
    permissions = _require_native_permissions()
    output = output.resolve()
    report_root = REPORT_ROOT.resolve()
    if output.exists() or report_root not in output.parents:
        raise NativeInteractionProbeError(
            "output must be fresh and under the owner-validation report root"
        )
    report_root.mkdir(parents=True, exist_ok=True)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="native-interaction-", dir=RUN_ROOT))
    logger = logging.getLogger("macos-native-interaction-probe")
    logger.addHandler(logging.NullHandler())
    target_broker = focus_broker = None
    target_pid = focus_pid = 0
    target_created = focus_created = 0.0
    hotkeys: dict[str, object] | None = None
    report: dict[str, object] | None = None
    try:
        bundle_nonce = uuid.uuid4().hex
        target_app = _write_synthetic_app(
            root,
            "SyntheticNativeTarget",
            f"test.whisperstream.native-target.{bundle_nonce}",
        )
        focus_app = _write_synthetic_app(
            root,
            "SyntheticFocusSentinel",
            f"test.whisperstream.focus-sentinel.{bundle_nonce}",
        )
        target_broker, target_pid, target_created = _launch_app(
            target_app,
            receipt=root / "target.receipt",
            nonce=uuid.uuid4().hex,
            mode="target",
        )
        reader = _AccessibilityReader()
        hotkeys = _run_hotkeys(logger)
        focus_broker, focus_pid, focus_created = _launch_app(
            focus_app,
            receipt=root / "focus.receipt",
            nonce=uuid.uuid4().hex,
            mode="focus",
        )
        subprocess.run(
            ["/usr/bin/open", "-a", str(target_app)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait(
            lambda: _frontmost_pid() == target_pid,
            timeout=5.0,
            label="target reactivation",
        )
        # Bounce focus through the sentinel so delivery proves it targets the
        # reactivated synthetic app rather than whichever app launched last.
        subprocess.run(
            ["/usr/bin/open", "-a", str(focus_app)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait(
            lambda: _frontmost_pid() == focus_pid,
            timeout=5.0,
            label="focus sentinel preflight",
        )
        subprocess.run(
            ["/usr/bin/open", "-a", str(target_app)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait(
            lambda: _frontmost_pid() == target_pid,
            timeout=5.0,
            label="target delivery activation",
        )

        try:
            delivery = _run_delivery(logger, reader, target_pid)
        except NativeInteractionTccBlocked:
            report = {
                "schema_version": 1,
                "verdict": "BLOCKED-TCC",
                "platform": {
                    "system": "Darwin",
                    "machine": "arm64",
                    "native": True,
                },
                "permissions": permissions,
                "hotkeys": hotkeys,
                "delivery": {
                    "reason": "core_graphics_cross_app_delivery_rejected",
                    "accessibility_assertion_attempted": True,
                    "delivery_completed": False,
                },
                "privacy": {
                    "synthetic_targets_only": True,
                    "accessibility_values_recorded": 0,
                    "clipboard_values_recorded": 0,
                    "native_identities_recorded": 0,
                },
            }
        else:
            report = {
                "schema_version": 1,
                "verdict": "PASS",
                "platform": {
                    "system": "Darwin",
                    "machine": "arm64",
                    "native": True,
                },
                "permissions": permissions,
                "hotkeys": hotkeys,
                "delivery": delivery,
                "privacy": {
                    "synthetic_targets_only": True,
                    "accessibility_values_recorded": 0,
                    "clipboard_values_recorded": 0,
                    "native_identities_recorded": 0,
                },
            }
    finally:
        if target_pid and target_created:
            _stop_identity(target_pid, target_created)
        if focus_pid and focus_created:
            _stop_identity(focus_pid, focus_created)
        for broker in (target_broker, focus_broker):
            if broker is not None and broker.poll() is None:
                broker.terminate()
                try:
                    broker.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    broker.kill()
                    broker.wait(timeout=5.0)
        shutil.rmtree(root)

    if report is None or hotkeys is None:
        raise NativeInteractionProbeError("native interaction proof produced no report")
    report["cleanup"] = {
        "probe_root_removed": not root.exists(),
        "target_process_alive": bool(
            target_pid and _identity_alive(target_pid, target_created)
        ),
        "focus_process_alive": bool(
            focus_pid and _identity_alive(focus_pid, focus_created)
        ),
        "native_event_tap_threads": sum(
            thread.name == "MacEventTap" for thread in threading.enumerate()
        ),
        "dispatch_threads": sum(
            thread.name == "MacKbdDispatch" for thread in threading.enumerate()
        ),
    }
    common_checks = (
        report["hotkeys"]["ptt_exact_once"],
        report["hotkeys"]["toggle_exact_once"],
        report["hotkeys"]["listener_failure"] is None,
        report["hotkeys"]["cleanup_failure"] is None,
        all(value == 0 for value in report["privacy"].values() if type(value) is int),
        report["cleanup"]["probe_root_removed"],
        not report["cleanup"]["target_process_alive"],
        not report["cleanup"]["focus_process_alive"],
        report["cleanup"]["native_event_tap_threads"] == 0,
        report["cleanup"]["dispatch_threads"] == 0,
    )
    if report["verdict"] == "PASS":
        checks = common_checks + (
            report["delivery"]["all_units_completed"],
            report["delivery"]["clipboard_restored"],
        )
    else:
        checks = common_checks + (
            report["delivery"]["accessibility_assertion_attempted"],
            not report["delivery"]["delivery_completed"],
        )
    if not all(checks):
        raise NativeInteractionProbeError("native interaction proof failed")
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if TARGET_TEXT in serialized:
        raise NativeInteractionProbeError("synthetic content entered report")
    output.write_text(serialized, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = run_probe(args.output)
    except NativeInteractionProbeError as exc:
        print(f"macOS native interaction probe failed: {exc}")
        return 1
    if report["verdict"] == "PASS":
        print(
            "macOS native interaction probe "
            "verdict=PASS hotkeys=pass delivery=pass"
        )
        return 0
    print(
        "macOS native interaction probe "
        "verdict=BLOCKED-TCC hotkeys=pass delivery=blocked"
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
