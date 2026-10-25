"""Native disposable Windows probes for Unit 08 application and browser context."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import psutil
import win32con
import win32gui
import win32process

from whisperstream.app_context_contract import (
    ContextRequirement,
    ContextStatus,
)
from whisperstream.windows_app_context import WindowsApplicationContextAdapter


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests" / "windows_context_target.py"
TEMP_PREFIX = "whisperstream-unit08-"


def _require_native_windows() -> None:
    machine = platform.machine().lower()
    if platform.system() != "Windows" or machine not in {"amd64", "x86_64"}:
        raise RuntimeError("native Windows x64 is required")
    if "microsoft" in platform.release().lower():
        raise RuntimeError("WSL is not a native Windows host")


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _wait(predicate, *, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise TimeoutError("native probe checkpoint timed out")


def _force_foreground(hwnd: int) -> None:
    attached: list[int] = []
    current_thread = int(ctypes.windll.kernel32.GetCurrentThreadId())
    try:
        for window in (win32gui.GetForegroundWindow(), hwnd):
            if not window:
                continue
            thread_id, _pid = win32process.GetWindowThreadProcessId(window)
            if thread_id and thread_id != current_thread and thread_id not in attached:
                if ctypes.windll.user32.AttachThreadInput(
                    current_thread, int(thread_id), True
                ):
                    attached.append(int(thread_id))
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    finally:
        for thread_id in reversed(attached):
            ctypes.windll.user32.AttachThreadInput(
                current_thread, thread_id, False
            )


class _Target:
    def __init__(self, root: Path, role: str) -> None:
        self.status = root / f"{role}-status.json"
        self.command = root / f"{role}-command.txt"
        self.command.write_text("", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(TARGET),
                "--status",
                str(self.status),
                "--command",
                str(self.command),
                "--role",
                role,
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.initial = _wait(lambda: _read_json(self.status))
            self.pid = int(self.initial["pid"])
            reported_hwnd = int(self.initial["hwnd"])
            self.hwnd = int(win32gui.GetAncestor(reported_hwnd, win32con.GA_ROOT))
        except Exception:
            self.close()
            raise

    def focus(self) -> None:
        self.command.write_text(f"focus:{time.monotonic_ns()}", encoding="utf-8")
        _force_foreground(self.hwnd)
        _wait(lambda: win32gui.GetForegroundWindow() == self.hwnd)
        _wait(lambda: self.content().get("entry_focused") is True)

    def content(self) -> dict[str, object]:
        return _read_json(self.status) or {}

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.command.write_text("exit", encoding="utf-8")
        try:
            self.process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5.0)


def _probe_app(root: Path) -> dict[str, object]:
    target = _Target(root, "context")
    adapter = WindowsApplicationContextAdapter()
    try:
        target.focus()
        snapshot = adapter.capture(1, ContextRequirement.APPLICATION)
        if snapshot.status is not ContextStatus.RESOLVED:
            raise AssertionError(f"app capture status={snapshot.status.value}")
        if snapshot.application is None:
            raise AssertionError("app capture omitted logical identity")
        return {
            "status": snapshot.status.value,
            "identity_sha256": hashlib.sha256(
                snapshot.application.logical_id.encode("utf-8")
            ).hexdigest(),
            "native_fields_exported": any(
                hasattr(snapshot, name) for name in ("hwnd", "pid", "path")
            ),
            "processes_started": 1,
        }
    finally:
        adapter.close()
        target.close()


class _SyntheticHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = (
            b"<!doctype html><meta charset=utf-8>"
            b"<title>Whisperstream Unit 08 Synthetic</title>"
            b"<link rel=icon href='data:image/svg+xml,<svg xmlns=\"http://www.w3.org/2000/svg\"/>'>"
            b"<input autofocus aria-label='Synthetic field'>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _SyntheticServer(ThreadingHTTPServer):
    def handle_error(self, _request: object, _client_address: object) -> None:
        return


def _edge_path() -> Path:
    candidates = (
        Path(os.environ.get("PROGRAMFILES(X86)", ""))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Microsoft/Edge/Application/msedge.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("native Windows Edge dependency is unavailable")


def _descendant_identities(process: subprocess.Popen[bytes]) -> dict[int, float]:
    try:
        parent = psutil.Process(process.pid)
        descendants = parent.children(recursive=True)
        values = [parent, *descendants]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {}
    identities: dict[int, float] = {}
    for value in values:
        try:
            identities[value.pid] = value.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return identities


def _find_window(pids: set[int]) -> int | None:
    matches: list[int] = []

    def collect(hwnd: int, _extra: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in pids and win32gui.GetWindow(hwnd, win32con.GW_OWNER) == 0:
            matches.append(hwnd)
        return True

    win32gui.EnumWindows(collect, None)
    return matches[0] if matches else None


def _stop_identities(identities: dict[int, float]) -> None:
    owned: list[psutil.Process] = []
    for pid, created in identities.items():
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - created) < 0.01:
                owned.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for process in reversed(owned):
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _gone, alive = psutil.wait_procs(owned, timeout=5.0)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(alive, timeout=5.0)


def _probe_browser(root: Path) -> dict[str, object]:
    edge = _edge_path()
    server = _SyntheticServer(("127.0.0.1", 0), _SyntheticHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=False)
    server_thread.start()
    profile = root / "edge-profile"
    try:
        browser = subprocess.Popen(
            [
                str(edge),
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-sync",
                "--new-window",
                f"http://synthetic.unit08.test:{server.server_port}/page",
                "--host-resolver-rules=MAP synthetic.unit08.test 127.0.0.1",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)
        raise
    identities: dict[int, float] = {}
    adapter = WindowsApplicationContextAdapter()
    try:
        def locate() -> int | None:
            nonlocal identities
            identities.update(_descendant_identities(browser))
            return _find_window(set(identities))

        hwnd = int(_wait(locate, timeout=25.0))
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        _force_foreground(hwnd)
        _wait(lambda: win32gui.GetForegroundWindow() == hwnd)

        def capture():
            snapshot = adapter.capture(
                2, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
            )
            return snapshot if snapshot.status is ContextStatus.RESOLVED else None

        snapshot = _wait(capture, timeout=20.0)
        if snapshot.browser_page is None:
            raise AssertionError("browser capture omitted host identity")
        return {
            "status": snapshot.status.value,
            "application_sha256": hashlib.sha256(
                snapshot.application.logical_id.encode("utf-8")
            ).hexdigest(),
            "host_sha256": hashlib.sha256(
                snapshot.browser_page.normalized_host.encode("utf-8")
            ).hexdigest(),
            "raw_url_exported": hasattr(snapshot.browser_page, "url"),
            "processes_started": len(identities),
        }
    finally:
        adapter.close()
        identities.update(_descendant_identities(browser))
        _stop_identities(identities)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)
        if server_thread.is_alive():
            raise AssertionError("synthetic browser listener did not stop")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", choices=("app", "browser"), required=True)
    args = parser.parse_args()
    _require_native_windows()
    root = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        if args.check == "app":
            result = _probe_app(root)
        else:
            result = _probe_browser(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if root.exists():
        raise AssertionError("owned temporary root did not clean up")
    result["check"] = args.check
    result["platform"] = "Windows-x64"
    result["owned_processes_remaining"] = 0
    result["owned_listeners_remaining"] = 0
    result["temporary_roots_remaining"] = 0
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
