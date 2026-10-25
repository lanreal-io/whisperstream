"""Event-driven, preference-gated screen-reader state for the idle control.

The persistent recording control is the only idle consumer of screen-reader
state, so the server runs this monitor only while ``show_recording_control``
is enabled. It never polls. One thread owns two independent event sources and
re-checks only when one fires:

1. ``spi``: ``WM_SETTINGCHANGE`` with ``wParam == SPI_SETSCREENREADER`` (the
   flag NVDA and JAWS set), received by a hidden top-level window. Broadcast
   messages reach top-level windows only, so this is deliberately not a
   message-only (``HWND_MESSAGE``) window.
2. ``narrator``: ``RegNotifyChangeKeyValue`` on Narrator's
   ``HKCU\\Software\\Microsoft\\Narrator\\NoRoam`` key, whose ``RunningState``
   is 1 while Narrator runs (Narrator never sets the SPI flag). Until the key
   exists, the deepest existing ancestor is watched (not its subtree). After
   every arm the deepest path is resolved again and re-armed if it moved, so a
   key created or deleted between selection and registration is not missed.
   The value is always read after the watch is armed.

A check reads the SPI flag and ``RunningState``; it never scans processes, so
NVDA without its SPI flag and JAWS-family process matching are covered only by
the live dictation-start check in main.

Publication is generation-checked: a check carries its session through the
probe, and the cache update plus notification happen under the monitor lock
only if that session is still current, so ``stop()``/``start()`` can never be
overwritten by a check that was already probing.

If one source cannot register, the other keeps running and the monitor reports
``degraded``; reads then fall back to a live check. If both fail, monitoring
stops. There is no polling fallback. ``WHISPERSTREAM_FORCE_SCREEN_READER`` is
applied on every read. Diagnostics carry only source names, stages, states and
exception classes, never key paths or process names.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable
from threading import Thread

FORCE_ENV = "WHISPERSTREAM_FORCE_SCREEN_READER"
NARRATOR_KEY_PATH = r"Software\Microsoft\Narrator\NoRoam"
NARRATOR_RUNNING_VALUE = "RunningState"

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_SETTINGCHANGE = 0x001A
SPI_SETSCREENREADER = 0x0047

SOURCE_SPI = "spi"
SOURCE_NARRATOR = "narrator"
_SOURCES = frozenset({SOURCE_SPI, SOURCE_NARRATOR})

STATUS_OFF = "off"
STATUS_EVENTS = "events"
STATUS_DEGRADED = "degraded"


def forced_screen_reader_state() -> bool | None:
    """Return the dev/test override, or ``None`` when detection should run."""

    force = os.environ.get(FORCE_ENV, "").strip().lower()
    if force in ("1", "true"):
        return True
    if force in ("0", "false"):
        return False
    return None


def read_narrator_running(key_path: str = NARRATOR_KEY_PATH) -> bool:
    """True when ``HKCU\\<key_path>\\RunningState`` is 1. Never raises."""

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _kind = winreg.QueryValueEx(key, NARRATOR_RUNNING_VALUE)
        return value == 1
    except Exception:  # noqa: BLE001 - absent key or non-Windows reads as off
        return False


def _safe_probe(probe: Callable[[], bool]) -> bool:
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - detection fails open to "no reader"
        return False


class ScreenReaderMonitor:
    """Cache screen-reader state from OS change events while running."""

    def __init__(
        self,
        *,
        flag_probe: Callable[[], bool],
        on_change: Callable[[bool], None] | None = None,
        on_degraded: Callable[[str], None] | None = None,
        narrator_key_path: str = NARRATOR_KEY_PATH,
        narrator_probe: Callable[[], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._flag_probe = flag_probe
        self._on_change = on_change
        self._on_degraded = on_degraded
        self._narrator_key_path = narrator_key_path
        self._narrator_probe = narrator_probe or (
            lambda: read_narrator_running(self._narrator_key_path)
        )
        self._logger = logger or logging.getLogger(__name__)
        # Guards session identity, cache, degraded sources, and publication.
        # Notification runs under it so stop/start cannot interleave with an
        # in-flight publish; probes never run under it.
        # Reentrant so on_change may read status() from the publishing thread.
        self._lock = threading.RLock()
        self._cached: bool | None = None
        self._reported: bool | None = None
        self._degraded: set[str] = set()
        self._thread: Thread | None = None
        self._session: _WindowsWatch | None = None

    @property
    def hwnd(self) -> int | None:
        """The hidden window handle of the running session (tests target it)."""

        session = self._session
        return None if session is None else session.hwnd

    @property
    def running(self) -> bool:
        """True while a session is current and has published a value."""

        with self._lock:
            return self._session is not None and self._cached is not None

    def status(self) -> str:
        """Content-free monitor state: ``off``, ``events`` or ``degraded``."""

        with self._lock:
            if self._session is None or self._cached is None:
                return STATUS_OFF
            return STATUS_DEGRADED if self._degraded else STATUS_EVENTS

    def degraded_sources(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._degraded)

    def cached(self) -> bool | None:
        """Effective cached state, or ``None`` when callers must check live.

        ``None`` when the monitor is off, has not published yet, or lost an
        event source, since a degraded cache can miss changes.
        """

        forced = forced_screen_reader_state()
        with self._lock:
            if self._session is None or self._degraded:
                return None
            cached = self._cached
        if cached is None:
            return None
        return forced if forced is not None else cached

    def _detect(self) -> bool:
        return _safe_probe(self._flag_probe) or _safe_probe(self._narrator_probe)

    def _session_check(self, session: object) -> None:
        """Probe for ``session`` and publish only if it is still current."""

        with self._lock:
            if self._session is not session:
                return
        detected = self._detect()
        forced = forced_screen_reader_state()
        effective = forced if forced is not None else detected
        with self._lock:
            if self._session is not session:
                return  # stopped or replaced while probing
            initial = self._cached is None
            self._cached = detected
            previous = self._reported
            self._reported = effective
            if initial or previous != effective:
                self._logger.info(
                    "[Accessibility] screen_reader state=%s",
                    "active" if effective else "inactive",
                )
                self._notify(effective)

    def _notify(self, effective: bool) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(effective)
        except Exception as exc:  # noqa: BLE001 - keep monitoring
            self._logger.warning(
                "[Accessibility] screen_reader notify result=failed error_type=%s",
                type(exc).__name__,
            )

    def start(self) -> bool:
        """Start event monitoring. Idempotent and non-blocking.

        Returns False when the platform cannot provide change events.
        """

        if sys.platform != "win32":
            return False
        with self._lock:
            if self._session is not None:
                return True
            session = _WindowsWatch(self)
            thread = Thread(
                target=session.run, name="ScreenReaderMonitor", daemon=True
            )
            self._session = session
            self._thread = thread
            self._cached = None
            self._reported = None
            self._degraded = set()
        thread.start()
        return True

    def stop(self) -> None:
        """Stop monitoring without joining; the cache becomes unavailable.

        Waits only for an in-flight publish (never for a probe or the thread).
        """

        with self._lock:
            session = self._session
            self._session = None
            self._thread = None
            self._cached = None
            self._reported = None
            self._degraded = set()
        if session is not None:
            session.request_stop()

    def wait_started(self, timeout: float) -> bool:
        """Test/diagnostic helper: wait until the first check has completed."""

        session = self._session
        return session is not None and session.ready.wait(timeout) and self.running

    def _source_failed(
        self,
        session: object,
        source: str | frozenset[str],
        stage: str,
        exc: BaseException | None,
    ) -> bool:
        """Record lost event source(s). Returns True if the session ends."""

        sources = {source} if isinstance(source, str) else set(source)
        self._logger.warning(
            "[Accessibility] screen_reader monitor source=%s result=unavailable stage=%s error_type=%s",
            "+".join(sorted(sources)),
            stage,
            type(exc).__name__ if exc is not None else "none",
        )
        with self._lock:
            if self._session is not session:
                return True
            self._degraded |= sources
            ended = self._degraded >= _SOURCES
            if ended:
                self._session = None
                self._thread = None
                self._cached = None
                self._reported = None
                self._degraded = set()
        status = STATUS_OFF if ended else STATUS_DEGRADED
        self._logger.warning("[Accessibility] screen_reader monitor status=%s", status)
        if self._on_degraded is not None:
            # A refresh hint for clients; the server answers with a live check.
            try:
                self._on_degraded(status)
            except Exception as exc2:  # noqa: BLE001
                self._logger.warning(
                    "[Accessibility] screen_reader degraded notify result=failed error_type=%s",
                    type(exc2).__name__,
                )
        return ended

    def _session_failed(
        self, session: object, stage: str, exc: BaseException | None
    ) -> None:
        """Every source is lost (setup or wait failure)."""

        self._source_failed(session, _SOURCES, stage, exc)


# ---------------------------------------------------------------------------
# Windows event plumbing
# ---------------------------------------------------------------------------

_win32 = None
_win32_lock = threading.Lock()


def _load_win32():
    """Private ctypes bindings; never mutates the shared ``ctypes.windll``."""

    with _win32_lock:
        if _win32 is None:
            _bind_win32()
        return _win32


def _bind_win32() -> None:
    global _win32
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SendMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = LRESULT
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
        wintypes.UINT, wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.MsgWaitForMultipleObjects.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
        wintypes.DWORD, wintypes.DWORD,
    ]
    user32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.CreateEventW.argtypes = [
        wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.RegNotifyChangeKeyValue.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD, wintypes.HANDLE,
        wintypes.BOOL,
    ]
    advapi32.RegNotifyChangeKeyValue.restype = wintypes.LONG

    windows: dict[int, _WindowsWatch] = {}

    @WNDPROC
    def wndproc(hwnd, msg, wparam, lparam):
        session = windows.get(hwnd or 0)
        if session is not None:
            if msg == WM_SETTINGCHANGE and wparam == SPI_SETSCREENREADER:
                session.on_setting_change()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                windows.pop(hwnd, None)
                user32.PostQuitMessage(0)
                return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    class_name = f"WhisperstreamScreenReaderMonitor{os.getpid()}"
    hinstance = kernel32.GetModuleHandleW(None)
    wndclass = WNDCLASSW()
    wndclass.lpfnWndProc = wndproc
    wndclass.hInstance = hinstance
    wndclass.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(wndclass)):
        raise OSError(ctypes.get_last_error(), "RegisterClassW failed")

    _win32 = type("Win32", (), {})()
    _win32.ctypes = ctypes
    _win32.wintypes = wintypes
    _win32.user32 = user32
    _win32.kernel32 = kernel32
    _win32.advapi32 = advapi32
    _win32.windows = windows
    _win32.class_name = class_name
    _win32.hinstance = hinstance
    # Keep the callback and class alive for the process lifetime.
    _win32.wndproc = wndproc
    _win32.wndclass = wndclass


class _WindowsWatch:
    """One monitor session: hidden window, registry wait, and stop event.

    The window (``spi``) and registry watch (``narrator``) are independent; a
    failure of one leaves the other running.
    """

    _REG_FILTER = 0x00000001 | 0x00000004  # NAME | LAST_SET
    _INFINITE = 0xFFFFFFFF
    _QS_ALLINPUT = 0x04FF
    _PM_REMOVE = 0x0001
    _WM_QUIT = 0x0012
    _ERROR_KEY_DELETED = 1018
    _MAX_ARM_ATTEMPTS = 8

    def __init__(self, monitor: ScreenReaderMonitor) -> None:
        self._monitor = monitor
        self._stop_requested = threading.Event()
        self.ready = threading.Event()
        # Guards handles shared with request_stop() on other threads.
        self._handles_lock = threading.Lock()
        self._hwnd: int | None = None
        self._stop_event = None
        self._reg_event = None
        self._reg_key = None
        self._reg_path: str | None = None
        self.registrations = 0

    # -- called from other threads ------------------------------------------

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    def request_stop(self) -> None:
        """Wake the session thread; never waits for it."""

        self._stop_requested.set()
        win = _win32
        if win is None:
            return
        with self._handles_lock:
            if self._stop_event:
                win.kernel32.SetEvent(self._stop_event)
            if self._hwnd:
                win.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    # -- monitor thread ------------------------------------------------------

    def on_setting_change(self) -> None:
        self._monitor._session_check(self)

    def run(self) -> None:
        try:
            win = _load_win32()
            stop_event = win.kernel32.CreateEventW(None, True, False, None)
            if not stop_event:
                raise OSError(win.ctypes.get_last_error(), "CreateEventW failed")
            with self._handles_lock:
                self._stop_event = stop_event
        except Exception as exc:  # noqa: BLE001 - no source can run
            self._monitor._session_failed(self, "setup", exc)
            self.ready.set()
            self._cleanup()
            return

        window_ok = self._start_window(win)
        registry_ok = self._start_registry(win)
        if not (window_ok or registry_ok) or self._stop_requested.is_set():
            self._cleanup()
            self.ready.set()
            return
        self._monitor._session_check(self)
        self.ready.set()
        try:
            self._loop(win, registry_ok)
        except Exception as exc:  # noqa: BLE001 - stop rather than spin
            self._monitor._session_failed(self, "loop", exc)
        finally:
            self._cleanup()

    def _start_window(self, win) -> bool:
        try:
            hwnd = win.user32.CreateWindowExW(
                0, win.class_name, None, 0, 0, 0, 0, 0,
                None, None, win.hinstance, None,
            )
            if not hwnd:
                raise OSError(win.ctypes.get_last_error(), "CreateWindowExW failed")
            with self._handles_lock:
                self._hwnd = hwnd
            win.windows[hwnd] = self
            return True
        except Exception as exc:  # noqa: BLE001 - keep the registry source
            self._monitor._source_failed(self, SOURCE_SPI, "window", exc)
            return False

    def _start_registry(self, win) -> bool:
        try:
            reg_event = win.kernel32.CreateEventW(None, False, False, None)
            if not reg_event:
                raise OSError(win.ctypes.get_last_error(), "CreateEventW failed")
            self._reg_event = reg_event
            self._arm_registry()
            return True
        except Exception as exc:  # noqa: BLE001 - keep the window source
            self._close_registry()
            self._monitor._source_failed(self, SOURCE_NARRATOR, "registry", exc)
            return False

    def _loop(self, win, registry_ok: bool) -> None:
        msg = win.wintypes.MSG()
        while True:
            handles = [self._stop_event]
            if registry_ok:
                handles.append(self._reg_event)
            array = (win.wintypes.HANDLE * len(handles))(*handles)
            result = win.user32.MsgWaitForMultipleObjects(
                len(handles), array, False, self._INFINITE, self._QS_ALLINPUT
            )
            if result == 0 or self._stop_requested.is_set():
                return
            if registry_ok and result == 1:
                try:
                    self._arm_registry()
                except Exception as exc:  # noqa: BLE001 - keep the window
                    self._close_registry()
                    registry_ok = False
                    if self._monitor._source_failed(
                        self, SOURCE_NARRATOR, "registry", exc
                    ):
                        return
                self._monitor._session_check(self)
            elif result == len(handles):
                while win.user32.PeekMessageW(
                    win.ctypes.byref(msg), None, 0, 0, self._PM_REMOVE
                ):
                    if msg.message == self._WM_QUIT:
                        return
                    win.user32.TranslateMessage(win.ctypes.byref(msg))
                    win.user32.DispatchMessageW(win.ctypes.byref(msg))
            else:
                raise OSError(win.ctypes.get_last_error(), "wait failed")

    def _deepest_existing(self) -> str:
        """Deepest existing prefix of the Narrator key path ("" means HKCU)."""

        import winreg

        path = self._monitor._narrator_key_path
        while path:
            try:
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_NOTIFY).Close()
                return path
            except FileNotFoundError:
                path = path.rsplit("\\", 1)[0] if "\\" in path else ""
        return ""

    def _arm_registry(self) -> None:
        """Watch the deepest existing key, re-resolving until it is stable.

        Selecting a key and arming its watch is not atomic: a deeper key can be
        created, or the chosen key deleted, in between. After each arm the path
        is resolved again; if it moved, the watch is re-armed there. Any change
        after the final arm signals the event, so the subsequent read is safe.
        """

        import winreg

        win = _win32
        access = winreg.KEY_NOTIFY | winreg.KEY_QUERY_VALUE
        for _attempt in range(self._MAX_ARM_ATTEMPTS):
            path = self._deepest_existing()
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, access)
            except FileNotFoundError:
                continue  # deleted after resolution; resolve again
            status = win.advapi32.RegNotifyChangeKeyValue(
                key.handle, False, self._REG_FILTER, self._reg_event, True
            )
            if status == self._ERROR_KEY_DELETED:
                key.Close()
                continue
            if status != 0:
                key.Close()
                raise OSError(status, "RegNotifyChangeKeyValue failed")
            self.registrations += 1
            previous = self._reg_key
            self._reg_key = key
            self._reg_path = path
            if previous is not None:
                try:
                    previous.Close()
                except OSError:
                    pass
            if self._deepest_existing() == path:
                return
        # Still armed on the last key; its own change events drive the next
        # descent. Never spin here.

    def _close_registry(self) -> None:
        if self._reg_key is not None:
            try:
                self._reg_key.Close()
            except OSError:
                pass
            self._reg_key = None
            self._reg_path = None

    def _cleanup(self) -> None:
        with self._handles_lock:
            self._release_handles()

    def _release_handles(self) -> None:
        win = _win32
        self._close_registry()
        if win is None:
            return
        hwnd = self._hwnd
        self._hwnd = None
        if hwnd and win.windows.pop(hwnd, None) is not None:
            win.user32.DestroyWindow(hwnd)
        for name in ("_reg_event", "_stop_event"):
            handle = getattr(self, name)
            if handle:
                win.kernel32.CloseHandle(handle)
            setattr(self, name, None)
