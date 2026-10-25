"""Platform-local tests for the dev/web startup fallback."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from whisperstream import startup


def test_non_windows_startup_helpers_are_fail_soft_without_winreg() -> None:
    with (
        patch.object(startup.sys, "platform", "darwin"),
        patch.dict(sys.modules, {"winreg": None}),
    ):
        assert startup.is_startup_supported() is False
        assert startup.is_startup_enabled() is False
        assert startup.set_startup_enabled(True) is None
        assert startup.set_startup_enabled(False) is None


def test_windows_startup_read_keeps_registry_behavior() -> None:
    winreg = MagicMock()
    winreg.HKEY_CURRENT_USER = object()
    winreg.KEY_READ = 17
    key = winreg.OpenKey.return_value.__enter__.return_value

    with (
        patch.object(startup.sys, "platform", "win32"),
        patch.dict(sys.modules, {"winreg": winreg}),
    ):
        assert startup.is_startup_supported() is True
        assert startup.is_startup_enabled() is True

    winreg.OpenKey.assert_called_once_with(
        winreg.HKEY_CURRENT_USER,
        startup._REG_KEY,
        0,
        winreg.KEY_READ,
    )
    winreg.QueryValueEx.assert_called_once_with(key, startup._APP_NAME)


def test_windows_startup_write_keeps_registry_behavior() -> None:
    winreg = MagicMock()
    winreg.HKEY_CURRENT_USER = object()
    winreg.KEY_SET_VALUE = 23
    winreg.REG_SZ = 31
    key = winreg.OpenKey.return_value.__enter__.return_value

    with (
        patch.object(startup.sys, "platform", "win32"),
        patch.dict(sys.modules, {"winreg": winreg}),
        patch.object(startup, "_get_exe_path", return_value="app-command"),
    ):
        startup.set_startup_enabled(True)

    winreg.OpenKey.assert_called_once_with(
        winreg.HKEY_CURRENT_USER,
        startup._REG_KEY,
        0,
        winreg.KEY_SET_VALUE,
    )
    winreg.SetValueEx.assert_called_once_with(
        key,
        startup._APP_NAME,
        0,
        winreg.REG_SZ,
        "app-command",
    )
