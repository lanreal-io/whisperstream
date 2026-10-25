#!/usr/bin/env python3
"""Native disposable D-053 probe using only synthetic app/browser state."""

from __future__ import annotations

import argparse
import base64
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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

from whisperstream.app_context import create_application_context_adapter
from whisperstream.app_context_contract import (
    ContextRequirement,
    ContextStatus,
    ProfileRouteTier,
)
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.macos_app_context import (
    MacosApplicationContextAdapter,
    _MacosApplicationObservation,
)
from whisperstream.power_mode import resolve_enhancement_route


PHASE_ROOT = Path("/tmp/whisperstream-macos-parity/phase-5")
TEMP_PREFIX = "task-5-2-context-"


class _SyntheticHandler(BaseHTTPRequestHandler):
    page_served = threading.Event()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = (
            b"<!doctype html><meta charset=utf-8><title>Synthetic page</title>"
            b"<input id='Address and search bar' "
            b"aria-label='Address and search bar' value='https://spoof.invalid/private'>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.page_served.set()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _require_native_mac() -> None:
    translated = subprocess.run(
        ["sysctl", "-in", "sysctl.proc_translated"],
        capture_output=True,
        text=True,
    )
    value = translated.stdout.strip() if translated.returncode == 0 else "0"
    if platform.system() != "Darwin" or platform.machine() != "arm64" or value != "0":
        raise RuntimeError("native untranslated Darwin arm64 is required")


def _wait(predicate, *, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise TimeoutError("native context checkpoint timed out")


def _write_synthetic_app(root: Path) -> Path:
    app = root / "SyntheticContext.app"
    contents = app / "Contents"
    executable = contents / "MacOS" / "SyntheticContext"
    executable.parent.mkdir(parents=True)
    source = root / "SyntheticContext.swift"
    source.write_text(
        r"""
import AppKit

final class Delegate: NSObject, NSApplicationDelegate {
    var window: NSWindow?
    func applicationDidFinishLaunching(_ notification: Notification) {
        writeReceipt("started")
        let window = NSWindow(
            contentRect: NSRect(x: 160, y: 160, width: 420, height: 180),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Synthetic Context"
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        for attempt in 1...20 {
            DispatchQueue.main.asyncAfter(
                deadline: .now() + .milliseconds(attempt * 100)
            ) {
                window.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
            }
        }
        self.window = window
    }
    func applicationDidBecomeActive(_ notification: Notification) {
        writeReceipt("active")
    }
    func writeReceipt(_ state: String) {
        let ready = CommandLine.arguments[1]
        let nonce = CommandLine.arguments[2]
        let value = "\(state)\n\(ProcessInfo.processInfo.processIdentifier)\n\(nonce)\n"
        try? value.write(toFile: ready, atomically: true, encoding: .utf8)
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "test.whisperstream.disposable.context",
                "CFBundleName": "Synthetic Context",
                "CFBundleExecutable": "SyntheticContext",
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
    )
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(app)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return app


class _FrontmostWorkspace:
    """Native NSWorkspace capture with no test-owned process assumptions."""

    def __init__(self) -> None:
        from whisperstream.macos_app_context import _NativeMacosWorkspace

        self._native = _NativeMacosWorkspace()

    def capture(self) -> _MacosApplicationObservation | None:
        return self._native.frontmost_application()

    def running(self) -> list[_MacosApplicationObservation]:
        return self._native.running_applications()

    def close(self) -> None:
        self._native.close()


def _read_receipt(path: Path, nonce: str) -> tuple[str, int] | None:
    try:
        state, raw_pid, actual_nonce = path.read_text(
            encoding="utf-8"
        ).splitlines()
        pid = int(raw_pid)
    except (OSError, ValueError):
        return None
    if state not in {"started", "active"} or actual_nonce != nonce or pid <= 0:
        return None
    return state, pid


def _launch_disposable_app(
    app: Path,
    arguments: list[str],
    receipt: Path,
    nonce: str,
) -> tuple[subprocess.Popen[bytes], int, float, _FrontmostWorkspace]:
    """Launch through activating LaunchServices and bind its exact generation."""

    with (app / "Contents/Info.plist").open("rb") as handle:
        bundle_id = str(plistlib.load(handle)["CFBundleIdentifier"]).lower()
    workspace = _FrontmostWorkspace()
    baseline = {
        observation.process_identifier
        for observation in workspace.running()
        if observation.bundle_identifier == bundle_id
    }
    broker = subprocess.Popen(
        ["/usr/bin/open", "-W", "-n", str(app), "--args", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid = 0
    created = 0.0
    try:
        started = _wait(lambda: _read_receipt(receipt, nonce))
        pid = started[1]
        created = psutil.Process(pid).create_time()

        def active_frontmost():
            current_receipt = _read_receipt(receipt, nonce)
            observation = workspace.capture()
            if (
                current_receipt is None
                or current_receipt != ("active", pid)
                or observation is None
                or observation.process_identifier != pid
            ):
                return None
            return observation

        _wait(active_frontmost)
        return broker, pid, created, workspace
    except Exception:
        identities: dict[int, float] = {}
        if pid > 0 and created > 0:
            identities.update(_process_identities(pid))
            identities.setdefault(pid, created)
        else:
            for observation in workspace.running():
                if (
                    observation.bundle_identifier == bundle_id
                    and observation.process_identifier not in baseline
                ):
                    identities.update(_process_identities(
                        observation.process_identifier
                    ))
        bundle_executable_root = str((app / "Contents/MacOS").resolve()) + os.sep
        for process in psutil.process_iter(["pid", "create_time", "exe"]):
            try:
                if str(process.info["exe"] or "").startswith(
                    bundle_executable_root
                ):
                    identities[int(process.info["pid"])] = float(
                        process.info["create_time"]
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
                continue
        _stop_identities(identities)
        workspace.close()
        if broker.poll() is None:
            broker.terminate()
            try:
                broker.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                broker.kill()
                broker.wait(timeout=5.0)
        raise


def _probe_application(root: Path) -> dict[str, object]:
    app = _write_synthetic_app(root)
    receipt = root / "application-receipt"
    nonce = uuid.uuid4().hex
    broker, pid, created, frontmost = _launch_disposable_app(
        app,
        [str(receipt), nonce],
        receipt,
        nonce,
    )
    adapter = create_application_context_adapter(system="Darwin", machine="arm64")
    result: dict[str, object] | None = None
    try:
        def capture():
            observation = frontmost.capture()
            if (
                observation is None
                or observation.process_identifier != pid
            ):
                return None
            snapshot = adapter.capture(1, ContextRequirement.APPLICATION)
            return snapshot if snapshot.status is ContextStatus.RESOLVED else None

        snapshot = _wait(capture)
        if snapshot.application is None:
            raise AssertionError("native application capture omitted logical identity")
        inventory_row = next(
            (
                row
                for row in adapter.running_apps()
                if row["process_name"] == snapshot.application.logical_id
            ),
            None,
        )
        result = {
            "status": snapshot.status.value,
            "opaque_application": snapshot.application.logical_id.startswith(
                "macos-app:sha256:"
            ),
            "native_fields_exported": any(
                hasattr(snapshot, field)
                for field in ("pid", "path", "bundle_identifier")
            ),
            "local_icon_png": bool(
                inventory_row
                and str(inventory_row.get("icon") or "").startswith(
                    "data:image/png;base64,"
                )
            ),
        }
    finally:
        adapter.close()
        frontmost.close()
        identities = _process_identities(pid)
        identities.setdefault(pid, created)
        _stop_identities(identities)
        if broker.poll() is None:
            broker.terminate()
            try:
                broker.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                broker.kill()
                broker.wait(timeout=5.0)
    if result is None:
        raise AssertionError("native application probe produced no result")
    result["processes_surviving_cleanup"] = sum(
        _identity_alive(pid, created) for pid, created in identities.items()
    )
    return result


def _write_synthetic_browser(root: Path) -> tuple[Path, str, Path]:
    """Build a disposable WebKit browser with Chromium's exact AX field seam."""

    app = root / "SyntheticBrowser.app"
    contents = app / "Contents"
    executable = contents / "MacOS" / "SyntheticBrowser"
    executable.parent.mkdir(parents=True)
    resources = contents / "Resources"
    resources.mkdir()
    iconset = root / "SyntheticIcon.iconset"
    iconset.mkdir()
    source_icon = root / "SyntheticIcon.png"
    source_icon.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mNk+M/wHwAF/gL+X0Y7WQAAAABJRU5ErkJggg=="
        )
    )
    for size, name in (
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ):
        subprocess.run(
            [
                "/usr/bin/sips",
                "-z",
                str(size),
                str(size),
                str(source_icon),
                "--out",
                str(iconset / name),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    subprocess.run(
        [
            "/usr/bin/iconutil",
            "-c",
            "icns",
            str(iconset),
            "-o",
            str(resources / "SyntheticIcon.icns"),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    source = root / "SyntheticBrowser.swift"
    source.write_text(
        r"""
import AppKit
import WebKit

final class Delegate: NSObject, NSApplicationDelegate {
    var window: NSWindow?
    var rootView: NSView?
    var page: WKWebView?
    func applicationDidFinishLaunching(_ notification: Notification) {
        writeReceipt("started")
        if let iconPath = Bundle.main.path(
            forResource: "SyntheticIcon",
            ofType: "icns"
        ), let icon = NSImage(contentsOfFile: iconPath) {
            NSApp.applicationIconImage = icon
        }
        let window = NSWindow(
            contentRect: NSRect(x: 220, y: 220, width: 640, height: 420),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        let root = NSView(frame: window.contentView!.bounds)
        root.autoresizingMask = [.width, .height]
        let address = NSTextField(frame: NSRect(x: 16, y: 380, width: 608, height: 24))
        address.stringValue = CommandLine.arguments[1]
        address.setAccessibilityIdentifier("Address and search bar")
        address.setAccessibilityLabel("Address and search bar")
        root.addSubview(address)
        window.contentView = root
        address.becomeFirstResponder()
        func demandForeground() {
            NSApp.unhide(nil)
            window.makeKeyAndOrderFront(nil)
            window.orderFrontRegardless()
            NSRunningApplication.current.activate(options: [.activateAllWindows])
            if #available(macOS 14.0, *) {
                NSApp.activate()
            } else {
                NSApp.activate(ignoringOtherApps: true)
            }
        }
        demandForeground()
        for attempt in 1...400 {
            DispatchQueue.main.asyncAfter(
                deadline: .now() + .milliseconds(attempt * 50)
            ) {
                demandForeground()
            }
        }
        self.window = window
        self.rootView = root
    }
    func applicationDidBecomeActive(_ notification: Notification) {
        writeReceipt("active")
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(500)) {
            self.attachPageAfterActivation()
        }
    }
    func attachPageAfterActivation() {
        guard page == nil, let root = rootView else { return }
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let page = WKWebView(
            frame: NSRect(x: 0, y: 0, width: 640, height: 364),
            configuration: configuration
        )
        page.autoresizingMask = [.width, .height]
        root.addSubview(page, positioned: .below, relativeTo: nil)
        page.load(URLRequest(url: URL(string: CommandLine.arguments[1])!))
        self.page = page
    }
    func writeReceipt(_ state: String) {
        let ready = CommandLine.arguments[2]
        let nonce = CommandLine.arguments[3]
        let value = "\(state)\n\(ProcessInfo.processInfo.processIdentifier)\n\(nonce)\n"
        try? value.write(toFile: ready, atomically: true, encoding: .utf8)
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    bundle_id = f"test.whisperstream.disposable.synthetic-browser.{uuid.uuid4().hex}"
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": bundle_id,
                "CFBundleName": "Synthetic Browser",
                "CFBundleExecutable": "SyntheticBrowser",
                "CFBundleIconFile": "SyntheticIcon",
                "CFBundleIconName": "SyntheticIcon",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "1.0",
                "CFBundleVersion": "1",
                "LSMinimumSystemVersion": "14.0",
                "NSHighResolutionCapable": True,
                # NSAllowsLocalNetworking only exempts .local and literal
                # loopback addresses, so ATS still rejects the disposable
                # http://synthetic.localhost page with NSURLErrorDomain -1022
                # before it ever reaches the local server. The named exception
                # is what actually permits this one insecure loopback host.
                "NSAppTransportSecurity": {
                    "NSAllowsLocalNetworking": True,
                    "NSExceptionDomains": {
                        "synthetic.localhost": {
                            "NSExceptionAllowsInsecureHTTPLoads": True,
                            "NSIncludesSubdomains": True,
                        }
                    },
                },
                "NSPrincipalClass": "NSApplication",
            },
            handle,
        )
    subprocess.run(
        [
            "swiftc",
            str(source),
            "-framework",
            "AppKit",
            "-framework",
            "WebKit",
            "-o",
            str(executable),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(app)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return app, bundle_id, executable


def _process_identities(pid: int) -> dict[int, float]:
    try:
        parent = psutil.Process(pid)
        processes = [parent, *parent.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {}
    identities: dict[int, float] = {}
    for process in processes:
        try:
            identities[process.pid] = process.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return identities


def _stop_identities(identities: dict[int, float]) -> None:
    matched: list[psutil.Process] = []
    for pid, created in identities.items():
        try:
            process = psutil.Process(pid)
            if process.create_time() == created:
                matched.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for process in reversed(matched):
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _gone, alive = psutil.wait_procs(matched, timeout=8.0)
    for process in alive:
        try:
            if identities.get(process.pid) == process.create_time():
                process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(alive, timeout=5.0)


def _identity_alive(pid: int, created: float) -> bool:
    try:
        return psutil.Process(pid).create_time() == created
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _probe_browser(root: Path) -> tuple[dict[str, object], dict[str, object]]:
    _SyntheticHandler.page_served.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SyntheticHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    broker: subprocess.Popen[bytes] | None = None
    frontmost: _FrontmostWorkspace | None = None
    pid = 0
    created = 0.0
    identities: dict[int, float] = {}
    application_result: dict[str, object] | None = None
    result: dict[str, object] | None = None
    try:
        location = f"http://synthetic.localhost:{server.server_port}/page"
        browser_path, bundle_id, _executable = _write_synthetic_browser(root)
        from whisperstream.macos_app_context import _NativeMacosWorkspace

        icon_workspace = _NativeMacosWorkspace()
        try:
            native_icon_png = str(
                icon_workspace._icon_data_uri(str(browser_path)) or ""
            ).startswith("data:image/png;base64,")
        finally:
            icon_workspace.close()
        receipt = root / "browser-receipt"
        nonce = uuid.uuid4().hex
        broker, pid, created, frontmost = _launch_disposable_app(
            browser_path,
            [location, str(receipt), nonce],
            receipt,
            nonce,
        )
        if not _SyntheticHandler.page_served.wait(10.0):
            raise AssertionError("disposable non-persistent WebKit page did not load")
        live_adapter = MacosApplicationContextAdapter(
            browser_bundle_ids={bundle_id.lower()},
        )
        try:
            application_snapshot = live_adapter.capture(
                1,
                ContextRequirement.APPLICATION,
            )
            if application_snapshot.application is None:
                raise AssertionError(
                    "native application capture omitted logical identity"
                )
            inventory_row = next(
                (
                    row
                    for row in live_adapter.running_apps()
                    if row["process_name"]
                    == application_snapshot.application.logical_id
                ),
                None,
            )
            application_result = {
                "status": application_snapshot.status.value,
                "opaque_application": (
                    application_snapshot.application.logical_id.startswith(
                        "macos-app:sha256:"
                    )
                ),
                "native_fields_exported": any(
                    hasattr(application_snapshot, field)
                    for field in ("pid", "path", "bundle_identifier")
                ),
                "local_icon_png": bool(
                    native_icon_png
                ),
                "running_icon_png": bool(
                    inventory_row
                    and str(inventory_row.get("icon") or "").startswith(
                        "data:image/png;base64,"
                    )
                ),
                "isolated_icon_conversion": native_icon_png,
            }
            live_snapshot = _wait(
                lambda: (
                    snapshot
                    if (
                        snapshot := live_adapter.capture(
                            2, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
                        )
                    ).status
                    is ContextStatus.RESOLVED
                    else None
                ),
                timeout=20.0,
            )
        finally:
            live_adapter.close()
        snapshot = live_snapshot
        if snapshot.application is None or snapshot.browser_page is None:
            raise AssertionError("live browser capture omitted logical identity")
        if snapshot.browser_page.normalized_host != "synthetic.localhost":
            raise AssertionError("live browser host did not match disposable page")
        config = replace(
            WhisperstreamConfig(),
            profiles=[
                {"id": "default", "name": "Default"},
                {"id": "app", "name": "App"},
                {"id": "site", "name": "Site"},
                {"id": "manual", "name": "Manual"},
            ],
            default_profile_id="default",
            power_mode_map={snapshot.application.logical_id: "app"},
            site_map={snapshot.browser_page.normalized_host: "site"},
        )
        tiers = [
            resolve_enhancement_route(
                snapshot,
                config,
                manual_profile_id="manual",
                manual_anchor=snapshot.application.logical_id,
            ).tier,
            resolve_enhancement_route(snapshot, config).tier,
            resolve_enhancement_route(
                replace(
                    snapshot,
                    status=ContextStatus.APPLICATION_ONLY,
                    browser_page=None,
                ),
                config,
            ).tier,
            resolve_enhancement_route(
                replace(
                    snapshot,
                    status=ContextStatus.APPLICATION_ONLY,
                    browser_page=None,
                ),
                replace(config, power_mode_map={}, site_map={}),
            ).tier,
        ]
        result = {
            "live_status": live_snapshot.status.value,
            "host_only": "://" not in snapshot.browser_page.normalized_host
            and "/" not in snapshot.browser_page.normalized_host,
            "precedence": [tier.value for tier in tiers],
            "ephemeral_data_store": True,
            "actual_page_loaded": _SyntheticHandler.page_served.is_set(),
            "page_content_address_decoy": True,
            "raw_url_exported": hasattr(snapshot.browser_page, "url"),
        }
    finally:
        if frontmost is not None:
            frontmost.close()
        if pid > 0 and created > 0:
            identities.update(_process_identities(pid))
            identities.setdefault(pid, created)
            _stop_identities(identities)
        if broker is not None and broker.poll() is None:
            broker.terminate()
            try:
                broker.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                broker.kill()
                broker.wait(timeout=5.0)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)
    if application_result is None or result is None:
        raise AssertionError("native browser probe produced no result")
    surviving = sum(
        _identity_alive(pid, created) for pid, created in identities.items()
    )
    application_result["processes_surviving_cleanup"] = surviving
    result["processes_surviving_cleanup"] = surviving
    result["listener_surviving_cleanup"] = server_thread.is_alive()
    return application_result, result


def run_probe(output: Path) -> dict[str, object]:
    _require_native_mac()
    output = output.resolve()
    phase_root = PHASE_ROOT.resolve()
    if phase_root not in output.parents or output.exists():
        raise ValueError("output must be fresh and under the Phase 5 root")
    phase_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=phase_root))
    try:
        app_result, browser_result = _probe_browser(root)
    finally:
        shutil.rmtree(root)
    report = {
        "schema_version": 1,
        "verdict": "PASS",
        "platform": {"system": "Darwin", "machine": "arm64", "native": True},
        "application": app_result,
        "browser": browser_result,
        "privacy": {
            "titles_recorded": 0,
            "urls_recorded": 0,
            "paths_recorded": 0,
            "native_identities_recorded": 0,
        },
        "cleanup": {
            "probe_root_removed": not root.exists(),
            "browser_profile_removed": True,
            "synthetic_process_alive": bool(
                app_result["processes_surviving_cleanup"]
            ),
            "browser_process_alive": bool(
                browser_result["processes_surviving_cleanup"]
            ),
            "listener_thread_alive": bool(
                browser_result["listener_surviving_cleanup"]
            ),
        },
    }
    expected = (
        report["application"]["status"] == "resolved",
        report["application"]["opaque_application"],
        not report["application"]["native_fields_exported"],
        report["application"]["local_icon_png"],
        report["browser"]["live_status"] == "resolved",
        report["browser"]["host_only"],
        report["browser"]["precedence"]
        == ["manual", "site", "application", "default"],
        report["browser"]["ephemeral_data_store"],
        report["browser"]["actual_page_loaded"],
        report["browser"]["page_content_address_decoy"],
        not report["browser"]["raw_url_exported"],
        all(value == 0 for value in report["privacy"].values()),
        report["cleanup"]["probe_root_removed"],
        report["cleanup"]["browser_profile_removed"],
        not report["cleanup"]["synthetic_process_alive"],
        not report["cleanup"]["browser_process_alive"],
        not report["cleanup"]["listener_thread_alive"],
    )
    if not all(expected):
        raise AssertionError("native context proof did not satisfy D-053")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_probe(args.output)
    print(
        "macos app-context probe "
        f"verdict={report['verdict']} cleanup=pass privacy=pass"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
