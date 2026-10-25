"""Optional PATH exposure for the bundled managed-session CLI.

The app always carries its signed ``bin/whisperstream.exe`` so updates can
replace it in place. This module controls only whether that exact ``bin``
directory is present in the current user's PATH. No system PATH or unrelated
entry is touched.
"""

from __future__ import annotations

import ctypes
import os
import ntpath
from pathlib import Path
import sys
from typing import Any


CLI_EXE_NAME = "whisperstream.exe"
_ENVIRONMENT_KEY = r"Environment"


def _normalized_path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(os.path.expandvars(value.strip())))


def _path_entries(raw_path: str) -> list[str]:
    return [entry.strip() for entry in raw_path.split(";") if entry.strip()]


def path_contains(raw_path: str, target: Path) -> bool:
    wanted = _normalized_path(str(target))
    return any(_normalized_path(entry) == wanted for entry in _path_entries(raw_path))


def add_path_entry(raw_path: str, target: Path) -> str:
    if path_contains(raw_path, target):
        return raw_path
    entries = _path_entries(raw_path)
    entries.append(str(target))
    return ";".join(entries)


def remove_path_entry(raw_path: str, target: Path) -> str:
    wanted = _normalized_path(str(target))
    return ";".join(
        entry for entry in _path_entries(raw_path) if _normalized_path(entry) != wanted
    )


def cli_bin_dir() -> Path:
    override = os.environ.get("WHISPERSTREAM_CLI_BIN_DIR")
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform == "darwin":
        resources = os.environ.get("WHISPERSTREAM_BUNDLE_RESOURCES_DIR")
        if resources:
            return Path(resources).resolve().parent / "bin"
        launcher = os.environ.get("NUITKA_ONEFILE_BINARY")
        if launcher:
            return Path(launcher).resolve().parent.parent / "Resources" / "bin"
        return Path(__file__).resolve().parents[2] / "src-tauri" / "bin"

    # Nuitka onefile executes from its extraction cache, so ``sys.executable``
    # points under %LOCALAPPDATA%\Whisperstream-runtime rather than at the
    # installed sidecar. ``containing_dir`` preserves the directory containing
    # the original executable, beside Tauri's installed ``bin`` directory.
    compiled = globals().get("__compiled__")
    containing_dir = getattr(compiled, "containing_dir", None) if compiled else None
    if containing_dir:
        packaged = Path(containing_dir).resolve() / "bin"
        if (packaged / CLI_EXE_NAME).is_file():
            return packaged

    packaged = Path(sys.executable).resolve().parent / "bin"
    if (packaged / CLI_EXE_NAME).is_file():
        return packaged

    # Source runs use the same staged path that scripts/build.py packages.
    return Path(__file__).resolve().parents[2] / "src-tauri" / "bin"


def _read_user_path() -> tuple[str, int]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENVIRONMENT_KEY) as key:
            value, value_type = winreg.QueryValueEx(key, "Path")
            return str(value), int(value_type)
    except FileNotFoundError:
        return "", winreg.REG_EXPAND_SZ


def _write_user_path(value: str, value_type: int) -> None:
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        _ENVIRONMENT_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        safe_type = value_type if value_type in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} else winreg.REG_EXPAND_SZ
        winreg.SetValueEx(key, "Path", 0, safe_type, value)


def _broadcast_environment_change() -> None:
    if os.name != "nt":
        return
    hwnd_broadcast = 0xFFFF
    wm_settingchange = 0x001A
    smto_abortifhung = 0x0002
    result = ctypes.c_size_t()
    ctypes.windll.user32.SendMessageTimeoutW(
        hwnd_broadcast,
        wm_settingchange,
        0,
        "Environment",
        smto_abortifhung,
        5000,
        ctypes.byref(result),
    )


def status() -> dict[str, Any]:
    if sys.platform == "darwin":
        from . import macos_managed_cli
        return macos_managed_cli.status(cli_bin_dir())
    target = cli_bin_dir()
    available = (target / CLI_EXE_NAME).is_file()
    if os.name != "nt":
        return {
            "ok": True,
            "status": "unavailable",
            "available": False,
            "installed": False,
        }
    raw_path, _ = _read_user_path()
    installed = path_contains(raw_path, target)
    return {
        "ok": True,
        "status": "installed" if installed else "not_installed",
        "available": available,
        "installed": installed,
    }


def install() -> dict[str, Any]:
    if sys.platform == "darwin":
        from . import macos_managed_cli
        return macos_managed_cli.install(cli_bin_dir())
    target = cli_bin_dir()
    if os.name != "nt" or not (target / CLI_EXE_NAME).is_file():
        return {
            "ok": False,
            "status": "unavailable",
            "available": False,
            "installed": False,
        }
    raw_path, value_type = _read_user_path()
    updated = add_path_entry(raw_path, target)
    changed = updated != raw_path
    if changed:
        _write_user_path(updated, value_type)
        _broadcast_environment_change()
    return {
        "ok": True,
        "status": "installed",
        "available": True,
        "installed": True,
        "restart_required": changed,
    }


def uninstall() -> dict[str, Any]:
    if sys.platform == "darwin":
        from . import macos_managed_cli
        return macos_managed_cli.uninstall(cli_bin_dir())
    if os.name != "nt":
        return {
            "ok": True,
            "status": "not_installed",
            "available": False,
            "installed": False,
        }
    target = cli_bin_dir()
    raw_path, value_type = _read_user_path()
    updated = remove_path_entry(raw_path, target)
    changed = updated != raw_path
    if changed:
        _write_user_path(updated, value_type)
        _broadcast_environment_change()
    return {
        "ok": True,
        "status": "not_installed",
        "available": (target / CLI_EXE_NAME).is_file(),
        "installed": False,
        "restart_required": changed,
    }
