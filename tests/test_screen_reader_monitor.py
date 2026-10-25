"""Event-driven, preference-gated screen-reader monitor.

Uses only the monitor's own hidden window and a scratch HKCU key. Never toggles
the real SPI flag or starts Narrator.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import uuid

import pytest

from whisperstream.screen_reader_monitor import (
    FORCE_ENV,
    SPI_SETSCREENREADER,
    WM_SETTINGCHANGE,
    ScreenReaderMonitor,
    _WindowsWatch,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Win32 events")


@pytest.fixture(autouse=True)
def _no_force(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FORCE_ENV, raising=False)


@pytest.fixture
def scratch_key():
    """A unique existing HKCU parent with an absent NoRoam child."""

    import winreg

    parent = rf"Software\WhisperstreamTest-{uuid.uuid4().hex}"
    winreg.CreateKey(winreg.HKEY_CURRENT_USER, parent).Close()
    try:
        yield parent + r"\NoRoam"
    finally:
        for path in (parent + r"\NoRoam", parent):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except FileNotFoundError:
                pass


class Recorder:
    def __init__(self) -> None:
        self.values: list[bool] = []
        self._cond = threading.Condition()

    def __call__(self, value: bool) -> None:
        with self._cond:
            self.values.append(value)
            self._cond.notify_all()

    def wait_for(self, count: int, timeout: float = 5.0) -> list[bool]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while len(self.values) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return list(self.values)


def _send_setting_change(hwnd: int, wparam: int) -> None:
    """Deliver WM_SETTINGCHANGE to the monitor's window only (no broadcast)."""

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    send = user32.SendMessageTimeoutW
    send.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    send.restype = ctypes.c_ssize_t
    result = ctypes.c_size_t()
    assert send(
        hwnd, WM_SETTINGCHANGE, wparam, 0, 0x0002, 5000, ctypes.byref(result)
    )


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _monitor(key_path: str, flag: dict, changes: Recorder) -> ScreenReaderMonitor:
    def read_flag() -> bool:
        flag["calls"] = flag.get("calls", 0) + 1
        return flag.get("value", False)

    return ScreenReaderMonitor(
        flag_probe=read_flag, on_change=changes, narrator_key_path=key_path
    )


@windows_only
def test_start_reports_once_then_rechecks_only_on_screen_reader_setting(scratch_key):
    flag: dict = {}
    changes = Recorder()
    monitor = _monitor(scratch_key, flag, changes)
    try:
        assert monitor.start() is True
        assert monitor.wait_started(5)
        assert changes.wait_for(1) == [False]  # computed and reported at start
        assert monitor.cached() is False
        calls = flag["calls"]

        # Unrelated setting changes do not re-check; there is no timer either.
        _send_setting_change(monitor.hwnd, 0x0072)
        time.sleep(0.2)
        assert flag["calls"] == calls

        flag["value"] = True
        _send_setting_change(monitor.hwnd, SPI_SETSCREENREADER)
        assert changes.wait_for(2) == [False, True]
        assert monitor.cached() is True
        assert flag["calls"] == calls + 1
    finally:
        monitor.stop()


@windows_only
def test_narrator_running_state_change_on_scratch_key(scratch_key):
    import winreg

    changes = Recorder()
    monitor = _monitor(scratch_key, {}, changes)
    try:
        monitor.start()
        assert monitor.wait_started(5)
        assert changes.wait_for(1) == [False]

        # The key appears (watched via its parent), then Narrator stops.
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, scratch_key) as handle:
            winreg.SetValueEx(handle, "RunningState", 0, winreg.REG_DWORD, 1)
        assert changes.wait_for(2) == [False, True]
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, scratch_key, 0, winreg.KEY_SET_VALUE
        ) as handle:
            winreg.SetValueEx(handle, "RunningState", 0, winreg.REG_DWORD, 0)
        assert changes.wait_for(3) == [False, True, False]

        # Deleting the watched key falls back to its parent without failing.
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, scratch_key)
        time.sleep(0.2)
        assert monitor.running is True
    finally:
        monitor.stop()


@windows_only
def test_stop_returns_immediately_and_thread_exits(scratch_key):
    monitor = _monitor(scratch_key, {}, Recorder())
    monitor.start()
    assert monitor.wait_started(5)
    thread = monitor._thread

    started = time.monotonic()
    monitor.stop()
    assert time.monotonic() - started < 0.5
    assert monitor.running is False
    assert monitor.cached() is None
    thread.join(5)
    assert not thread.is_alive()


@windows_only
@pytest.mark.parametrize(("value", "expected"), [("1", True), ("0", False)])
def test_force_override_wins_over_events(
    scratch_key, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
):
    flag = {"value": not expected}
    changes = Recorder()
    monkeypatch.setenv(FORCE_ENV, value)
    monitor = _monitor(scratch_key, flag, changes)
    try:
        monitor.start()
        assert monitor.wait_started(5)
        assert changes.wait_for(1) == [expected]
        _send_setting_change(monitor.hwnd, SPI_SETSCREENREADER)
        assert monitor.cached() is expected
        assert changes.values == [expected]
    finally:
        monitor.stop()


def test_force_override_applies_to_on_demand_check(monkeypatch: pytest.MonkeyPatch):
    from whisperstream.main import screen_reader_active

    monkeypatch.setenv(FORCE_ENV, "true")
    assert screen_reader_active() is True
    monkeypatch.setenv(FORCE_ENV, "0")
    assert screen_reader_active() is False


# ---------------------------------------------------------------------------
# Server gating on show_recording_control
# ---------------------------------------------------------------------------


def _server(show_recording_control: bool):
    from whisperstream.config_manager import WhisperstreamConfig
    from tests._platform_test_support import _make_server

    config = WhisperstreamConfig()
    config.show_recording_control = show_recording_control
    return _make_server(config=config)


def test_no_thread_when_preference_is_off(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    server = _server(False)
    live: list[bool] = []
    monkeypatch.setattr(
        "whisperstream.main.screen_reader_active",
        lambda: live.append(True) or True,
    )
    with TestClient(server.fastapi) as client:
        assert server._screen_reader_monitor._thread is None
        response = client.get("/api/accessibility/screen-reader")
        config = client.get("/api/config")
    assert response.json() == {"active": True, "monitor": "off"}
    assert config.json()["screen_reader_detected"] is True
    assert len(live) == 2  # computed on demand for each read
    assert server._screen_reader_monitor._thread is None


@windows_only
def test_monitor_starts_and_stops_with_the_preference(
    scratch_key, monkeypatch: pytest.MonkeyPatch
):
    server = _server(False)
    monitor = server._screen_reader_monitor
    monitor._narrator_key_path = scratch_key
    monitor._flag_probe = lambda: False
    events: list[dict] = []
    monkeypatch.setattr(server, "broadcast", events.append)
    live: list[bool] = []
    monkeypatch.setattr(
        "whisperstream.main.screen_reader_active",
        lambda: live.append(True) or True,
    )
    try:
        server._sync_screen_reader_monitor()
        assert monitor._thread is None

        server._app.config.show_recording_control = True
        server._publish_config_change(server._app.config, False, False)
        assert monitor.wait_started(5)
        assert _wait_until(lambda: events)
        assert events == [
            {"type": "accessibility.screen_reader", "active": False, "monitor": "events"}
        ]
        assert asyncio.run(server._screen_reader_active()) is False
        assert live == []  # served from the cache

        thread = monitor._thread
        server._app.config.show_recording_control = False
        server._publish_config_change(server._app.config, False, False)
        assert monitor._thread is None
        thread.join(5)
        assert not thread.is_alive()
        assert asyncio.run(server._screen_reader_active()) is True
        assert live == [True]
    finally:
        monitor.stop()


def test_shutdown_helpers_tolerate_a_server_without_a_monitor():
    from whisperstream.server import WhisperstreamServer

    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._stop_screen_reader_monitor()
    server._sync_screen_reader_monitor()


# ---------------------------------------------------------------------------
# Publication races (generation-checked commit)
# ---------------------------------------------------------------------------


class _FakeSession:
    def request_stop(self) -> None:
        pass


def _blocked_monitor(changes: Recorder):
    entered = threading.Event()
    release = threading.Event()

    def probe() -> bool:
        entered.set()
        assert release.wait(5)
        return True

    monitor = ScreenReaderMonitor(
        flag_probe=probe, narrator_probe=lambda: False, on_change=changes
    )
    return monitor, entered, release


def test_stop_during_blocked_probe_discards_the_stale_result():
    changes = Recorder()
    monitor, entered, release = _blocked_monitor(changes)
    session = _FakeSession()
    monitor._session = session
    worker = threading.Thread(target=monitor._session_check, args=(session,))
    worker.start()
    assert entered.wait(5)

    monitor.stop()
    release.set()
    worker.join(5)

    assert monitor.cached() is None
    assert monitor.running is False
    assert monitor.status() == "off"
    assert changes.values == []
    assert monitor._session is None


def test_restart_during_blocked_probe_keeps_the_replacement_session():
    changes = Recorder()
    monitor, entered, release = _blocked_monitor(changes)
    old = _FakeSession()
    monitor._session = old
    worker = threading.Thread(target=monitor._session_check, args=(old,))
    worker.start()
    assert entered.wait(5)

    # Stop, then a replacement session publishes False before the old probe ends.
    monitor.stop()
    new = _FakeSession()
    monitor._session = new
    monitor._flag_probe = lambda: False
    monitor._session_check(new)
    assert changes.values == [False]

    release.set()
    worker.join(5)

    assert monitor._session is new
    assert monitor.cached() is False
    assert changes.values == [False]


# ---------------------------------------------------------------------------
# Registry ancestor races and independent sources (scratch HKCU keys)
# ---------------------------------------------------------------------------


@windows_only
def test_key_created_between_resolution_and_arm_is_rewatched(
    scratch_key, monkeypatch: pytest.MonkeyPatch
):
    import winreg

    from whisperstream.screen_reader_monitor import _load_win32

    win = _load_win32()
    original = win.advapi32.RegNotifyChangeKeyValue
    created = []

    def arm_then_create(*args):
        result = original(*args)
        if not created:
            created.append(True)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, scratch_key) as key:
                winreg.SetValueEx(key, "RunningState", 0, winreg.REG_DWORD, 1)
        return result

    monkeypatch.setattr(win.advapi32, "RegNotifyChangeKeyValue", arm_then_create)
    changes = Recorder()
    monitor = _monitor(scratch_key, {}, changes)
    try:
        monitor.start()
        assert monitor.wait_started(5)
        assert changes.wait_for(1) == [True]
        assert monitor._session._reg_path == scratch_key  # descended after arming

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, scratch_key, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "RunningState", 0, winreg.REG_DWORD, 0)
        assert changes.wait_for(2) == [True, False]
        assert monitor.cached() is False
    finally:
        monitor.stop()


@windows_only
def test_delete_and_recreate_of_the_watched_key(scratch_key):
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, scratch_key) as key:
        winreg.SetValueEx(key, "RunningState", 0, winreg.REG_DWORD, 1)
    changes = Recorder()
    monitor = _monitor(scratch_key, {}, changes)
    try:
        monitor.start()
        assert monitor.wait_started(5)
        assert changes.wait_for(1) == [True]

        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, scratch_key)
        assert changes.wait_for(2) == [True, False]
        assert _wait_until(lambda: monitor._session._reg_path != scratch_key)

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, scratch_key) as key:
            winreg.SetValueEx(key, "RunningState", 0, winreg.REG_DWORD, 1)
        assert changes.wait_for(3) == [True, False, True]
        assert _wait_until(lambda: monitor._session._reg_path == scratch_key)
        assert monitor.status() == "events"
    finally:
        monitor.stop()


@windows_only
def test_registry_failure_keeps_the_spi_window(
    scratch_key, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def fail(_self):
        raise OSError("synthetic")

    monkeypatch.setattr(_WindowsWatch, "_arm_registry", fail)
    flag: dict = {}
    changes = Recorder()
    degraded: list[str] = []
    monitor = _monitor(scratch_key, flag, changes)
    monitor._on_degraded = degraded.append
    try:
        with caplog.at_level(logging.WARNING):
            monitor.start()
            assert monitor.wait_started(5)
        assert monitor.status() == "degraded"
        assert monitor.degraded_sources() == frozenset({"narrator"})
        assert monitor.cached() is None  # callers check live while degraded
        assert degraded == ["degraded"]
        assert "source=narrator result=unavailable stage=registry" in caplog.text
        assert "WhisperstreamTest" not in caplog.text

        flag["value"] = True
        _send_setting_change(monitor.hwnd, SPI_SETSCREENREADER)
        assert changes.wait_for(2) == [False, True]
    finally:
        monitor.stop()


@windows_only
def test_window_failure_keeps_the_registry_watch(
    scratch_key, monkeypatch: pytest.MonkeyPatch
):
    import winreg

    def fail(self, _win):
        self._monitor._source_failed(self, "spi", "window", OSError("synthetic"))
        return False

    monkeypatch.setattr(_WindowsWatch, "_start_window", fail)
    changes = Recorder()
    monitor = _monitor(scratch_key, {}, changes)
    try:
        monitor.start()
        assert monitor.wait_started(5)
        assert monitor.degraded_sources() == frozenset({"spi"})
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, scratch_key) as key:
            winreg.SetValueEx(key, "RunningState", 0, winreg.REG_DWORD, 1)
        assert changes.wait_for(2) == [False, True]
    finally:
        monitor.stop()


@windows_only
def test_both_sources_failing_ends_monitoring(
    scratch_key, monkeypatch: pytest.MonkeyPatch
):
    def fail_window(self, _win):
        self._monitor._source_failed(self, "spi", "window", OSError("synthetic"))
        return False

    def fail_registry(_self):
        raise OSError("synthetic")

    monkeypatch.setattr(_WindowsWatch, "_start_window", fail_window)
    monkeypatch.setattr(_WindowsWatch, "_arm_registry", fail_registry)
    changes = Recorder()
    degraded: list[str] = []
    monitor = _monitor(scratch_key, {}, changes)
    monitor._on_degraded = degraded.append
    monitor.start()
    # The session can fail before this thread reads monitor._thread.
    assert _wait_until(lambda: degraded == ["degraded", "off"])
    assert monitor._thread is None
    assert monitor.status() == "off"
    assert monitor.cached() is None
    assert changes.values == []
    assert degraded == ["degraded", "off"]


def test_server_degraded_hint_pushes_a_live_value(monkeypatch: pytest.MonkeyPatch):
    server = _server(True)
    events: list[dict] = []
    monkeypatch.setattr(server, "broadcast", events.append)
    monkeypatch.setattr("whisperstream.main.screen_reader_active", lambda: True)
    server._screen_reader_monitor_degraded("degraded")
    assert events == [
        {"type": "accessibility.screen_reader", "active": True, "monitor": "degraded"}
    ]
