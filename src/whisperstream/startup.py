"""Windows startup registry helpers (HKCU — no admin required).

NOTE: In the packaged app, "Run on Startup" is driven by tauri-plugin-autostart
on the Tauri side (it registers the install-dir Whisperstream.exe via
current_exe()). These helpers and the `/api/startup` endpoint remain the
Windows dev/web fallback only. The endpoint reports `supported=false` and does
not mutate on other platforms. Do not rely on `_get_exe_path()` for the
production Run key — Nuitka does not set ``sys.frozen``, so the source branch
below would register the engine sidecar from its transient extraction cache.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_logger = logging.getLogger(__name__)

_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "Whisperstream"


def is_startup_supported() -> bool:
    """Return whether the dev/web fallback can own startup registration."""
    return sys.platform == "win32"


def _get_exe_path() -> str:
    """Return the path to the running executable."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    # When running from source, use the Python interpreter + module
    return f'"{sys.executable}" -m whisperstream.main --tray'


def is_startup_enabled() -> bool:
    """Check whether Whisperstream is registered to run on Windows startup."""
    if not is_startup_supported():
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, _APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_startup_enabled(enabled: bool) -> None:
    """Add or remove Whisperstream from the Windows startup registry."""
    if not is_startup_supported():
        _logger.info(
            "startup toggle enabled=%s persisted=false supported=false",
            str(enabled).lower(),
        )
        return
    import winreg

    persisted = True
    try:
        if enabled:
            exe = _get_exe_path()
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, exe)
        else:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.DeleteValue(key, _APP_NAME)
            except FileNotFoundError:
                pass  # Already removed
    except OSError as exc:
        persisted = False
        _logger.error(
            "startup toggle write failed reason=%s",
            type(exc).__name__,
        )
        raise
    finally:
        _logger.info(
            "startup toggle enabled=%s persisted=%s",
            str(enabled).lower(),
            str(persisted).lower(),
        )
