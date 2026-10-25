"""Tests for capability-specific foreground-window probes."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import psutil
import pytest

from whisperstream.foreground_window import (
    RoutingTarget,
    detect_foreground_routing_target,
)

_WIN32_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="foreground-window detection patches Win32 APIs",
)


@_WIN32_ONLY
def test_process_only_routing_never_reads_title_executable_or_uia():
    mock_proc = MagicMock()
    mock_proc.name.return_value = "notepad.exe"

    with (
        patch(
            "whisperstream.foreground_window.win32gui.GetForegroundWindow",
            return_value=1234,
        ),
        patch(
            "whisperstream.foreground_window.win32process.GetWindowThreadProcessId",
            return_value=(111, 4242),
        ),
        patch(
            "whisperstream.foreground_window.win32gui.GetWindowText"
        ) as mock_get_title,
        patch(
            "whisperstream.foreground_window.psutil.Process",
            return_value=mock_proc,
        ),
        patch(
            "whisperstream.foreground_window.read_active_browser_domain"
        ) as mock_read_domain,
    ):
        result = detect_foreground_routing_target(
            include_browser_domain=False
        )

    assert result == RoutingTarget(process_name="notepad.exe")
    mock_get_title.assert_not_called()
    mock_proc.exe.assert_not_called()
    mock_read_domain.assert_not_called()
    assert not hasattr(result, "window_title")
    assert not hasattr(result, "exe_path")
    assert not hasattr(result, "hwnd")
    assert not hasattr(result, "pid")


@_WIN32_ONLY
def test_site_routing_opts_into_browser_uia_only_for_known_browser():
    mock_proc = MagicMock()
    mock_proc.name.return_value = "chrome.exe"

    with (
        patch(
            "whisperstream.foreground_window.win32gui.GetForegroundWindow",
            return_value=1234,
        ),
        patch(
            "whisperstream.foreground_window.win32process.GetWindowThreadProcessId",
            return_value=(111, 4242),
        ),
        patch(
            "whisperstream.foreground_window.psutil.Process",
            return_value=mock_proc,
        ),
        patch(
            "whisperstream.foreground_window.read_active_browser_domain",
            return_value="mail.google.com",
        ) as mock_read_domain,
    ):
        result = detect_foreground_routing_target(
            include_browser_domain=True
        )

    assert result == RoutingTarget(
        process_name="chrome.exe", domain="mail.google.com"
    )
    mock_read_domain.assert_called_once_with(1234, "chrome.exe")
    mock_proc.exe.assert_not_called()


@_WIN32_ONLY
def test_no_foreground_window_returns_none_without_process_read():
    with (
        patch(
            "whisperstream.foreground_window.win32gui.GetForegroundWindow",
            return_value=0,
        ),
        patch(
            "whisperstream.foreground_window.psutil.Process"
        ) as mock_process,
    ):
        routing = detect_foreground_routing_target(
            include_browser_domain=True
        )

    assert routing is None
    mock_process.assert_not_called()


@_WIN32_ONLY
def test_process_access_denied_returns_none():
    with (
        patch(
            "whisperstream.foreground_window.win32gui.GetForegroundWindow",
            return_value=9999,
        ),
        patch(
            "whisperstream.foreground_window.win32process.GetWindowThreadProcessId",
            return_value=(222, 5555),
        ),
        patch(
            "whisperstream.foreground_window.psutil.Process",
            side_effect=psutil.AccessDenied(pid=5555),
        ),
    ):
        result = detect_foreground_routing_target()

    assert result is None


def test_live_smoke_never_raises():
    routing = detect_foreground_routing_target(include_browser_domain=False)
    assert routing is None or isinstance(routing, RoutingTarget)
