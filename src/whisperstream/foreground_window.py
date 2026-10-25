"""Capability-specific foreground-window probes.

Routing needs only a process name and, when configured site rules can matter,
the active browser host. The probe never reads window titles or executable
paths.

Every public probe is best-effort and never raises. The guarded Win32 imports
stay at module scope so Nuitka can include them in the frozen sidecar.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .browser_domain import is_known_browser, read_active_browser_domain

# Top-level guarded imports (Nuitka-friendly static signal).
# NOT inside-function imports: Nuitka needs to see these at module scope.
try:
    import psutil
    import win32gui
    import win32process

    _WIN32_AVAILABLE = True
except Exception:
    _WIN32_AVAILABLE = False


@dataclass(frozen=True, slots=True)
class RoutingTarget:
    """Minimum foreground identity retained for app/site profile routing."""

    process_name: str
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class ForegroundWindowTarget:
    """Foreground HWND/PID pair read before resolving the routing identity."""

    hwnd: int
    pid: int


def _foreground_window_target() -> ForegroundWindowTarget | None:
    """Read only the current HWND/PID pair, or ``None`` on any failure."""
    if sys.platform != "win32" or not _WIN32_AVAILABLE:
        return None

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        return ForegroundWindowTarget(hwnd=int(hwnd), pid=pid)
    except Exception:  # noqa: BLE001 - a foreground probe must never break input
        return None


def detect_foreground_routing_target(
    *, include_browser_domain: bool = False
) -> RoutingTarget | None:
    """Return the minimum foreground identity needed for profile routing.

    ``include_browser_domain=False`` is the app/manual-routing path and reads
    only the process name. Callers must opt in to browser UI Automation when a
    configured site rule can affect the dictation.
    """
    window = _foreground_window_target()
    if window is None:
        return None

    try:
        process_name = psutil.Process(window.pid).name()
        if not process_name:
            return None

        domain = None
        if include_browser_domain and is_known_browser(process_name):
            domain = read_active_browser_domain(window.hwnd, process_name)
        return RoutingTarget(process_name=process_name, domain=domain)
    except Exception:  # noqa: BLE001 - routing detection is best effort
        return None
