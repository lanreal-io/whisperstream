"""Windows application enumeration; imported only by an explicit native query."""
from __future__ import annotations
import sys
from typing import Dict, List, Optional
import psutil
from .app_directory import friendly_app_name
from .app_icons import icon_data_uri_for_exe

# ---------------------------------------------------------------------------
# Top-level guarded imports (Nuitka-friendly static signal).
# NOT inside-function imports — Nuitka needs to see these at module scope to
# include the extensions in the frozen binary.  Mirror foreground_window.py.
# ---------------------------------------------------------------------------
try:
    import win32gui
    import win32process
    _WIN32_AVAILABLE = True
except Exception:
    _WIN32_AVAILABLE = False


def list_running_apps() -> List[Dict[str, Optional[str]]]:
    """Return a deduplicated list of running GUI applications.

    Each entry is ``{"process_name": <lowercased>, "display_name": <str>,
    "icon": <str|None>, "path": <str|None>}`` where ``icon`` is a small base64
    PNG data-URI (``"data:image/png;base64,..."``) extracted from the app's exe
    icon, or ``None`` when the icon could not be extracted, and ``path`` is the
    full exe path (or ``None``), recorded when a rule is added so install state
    can later be checked by path existence. ``path`` is never logged.

    Logic:
    - Enumerate top-level windows via ``win32gui.EnumWindows``.
    - Keep only visible windows (``IsWindowVisible``) with a non-empty title
      (``GetWindowText``).  The title is used solely as a visibility filter
      and is never included in the output (PII risk).
    - Resolve hwnd -> pid via ``win32process.GetWindowThreadProcessId``, then
      ``psutil.Process(pid).name()`` and ``.exe()``.
    - Build ``display_name`` via ``app_directory.friendly_app_name`` (the exe's
      system ``FileDescription``, e.g. ``Code.exe`` -> ``Visual Studio Code``,
      with a strip-``.exe`` + title-case fallback). Window titles are NOT used
      as the display name.
    - Extract a best-effort icon from the exe path (cached by path in
      ``app_icons``); any failure yields ``icon: None``, never raises.
    - Deduplicate by lowercased ``process_name``.
    - Skip-not-fail per process on ``AccessDenied`` / ``NoSuchProcess`` /
      generic ``Error``.
    - Wrap the whole body in try/except returning ``[]`` (graceful-degrade).
    - Returns ``[]`` on non-win32 or when the win32 libraries are unavailable.

    Never logs a window title, exe path, or icon bytes.  If logging is added,
    log only ``count=``.
    """
    if sys.platform != "win32" or not _WIN32_AVAILABLE:
        return []

    try:
        # lowercased process_name -> (display_name, icon_data_uri | None, exe_path | None)
        seen: dict[str, tuple[str, Optional[str], Optional[str]]] = {}

        def _visit(hwnd: int, _lparam: object) -> bool:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                # Use the title only as a visibility filter — never log or return it.
                title = win32gui.GetWindowText(hwnd)
                if not title:
                    return True

                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    proc = psutil.Process(pid)
                    proc_name = proc.name()
                except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.Error):
                    return True

                key = proc_name.lower()
                if key not in seen:
                    # Best-effort exe path FIRST: it feeds both the system
                    # friendly name (FileDescription) and the icon. proc.exe()
                    # can raise on protected/elevated processes; treat any
                    # failure as no path.
                    try:
                        exe_path = proc.exe()
                    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.Error):
                        exe_path = None

                    # System-provided display name (FileDescription -> override
                    # -> strip-.exe+title-case fallback). No hardcoded name map.
                    display = friendly_app_name(proc_name, exe_path)

                    icon: Optional[str] = None
                    if exe_path:
                        # icon_data_uri_for_exe is itself best-effort and
                        # caches by path; belt-and-suspenders guard anyway.
                        try:
                            icon = icon_data_uri_for_exe(exe_path)
                        except Exception:
                            icon = None

                    seen[key] = (display, icon, exe_path)
            except Exception:
                pass
            return True  # always continue enumeration

        win32gui.EnumWindows(_visit, None)
        return [
            {"process_name": k, "display_name": display, "icon": icon, "path": path}
            for k, (display, icon, path) in seen.items()
        ]

    except Exception:
        return []
