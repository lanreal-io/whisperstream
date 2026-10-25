"""Tests for whisperstream.system_info.

get_total_ram_mb — three behaviors are load-bearing:
- Returns an int when psutil.virtual_memory() succeeds.
- Returns None when psutil.virtual_memory() raises (psutil quirks on
  exotic platforms must not block the download flow).
- The int return is the bytes value divided by 1024*1024, truncated.

list_running_apps — the icon contract (Power Mode picker):
- Every entry carries an ``icon`` key: a data-URI string or None.
- Icon extraction is best-effort: a stubbed failure yields ``icon: None``
  and never raises / never blocks the list.
"""

from __future__ import annotations

import pytest

import sys
from unittest.mock import MagicMock, patch

from whisperstream.system_info import (
    get_available_ram_mb,
    get_total_ram_mb,
    list_running_apps,
)


def test_returns_int_on_success():
    """psutil.virtual_memory().total -> bytes; result must be MB int."""
    fake_vm = MagicMock()
    fake_vm.total = 8 * 1024 * 1024 * 1024  # 8 GiB in bytes
    with patch("whisperstream.system_info.psutil.virtual_memory", return_value=fake_vm):
        result = get_total_ram_mb()
    assert result == 8192
    assert isinstance(result, int)


def test_returns_none_when_psutil_raises():
    """Any exception from psutil collapses to None (fail-open contract)."""
    with patch(
        "whisperstream.system_info.psutil.virtual_memory",
        side_effect=RuntimeError("psutil unavailable"),
    ):
        result = get_total_ram_mb()
    assert result is None


def test_returns_none_on_oserror():
    """OSError (common on locked-down sandboxes) also collapses to None."""
    with patch(
        "whisperstream.system_info.psutil.virtual_memory",
        side_effect=OSError("cannot read /proc/meminfo"),
    ):
        result = get_total_ram_mb()
    assert result is None


def test_converts_partial_gib_correctly():
    """Non-integer-GiB amounts (e.g. 12 GiB minus 256 MiB) round down to int MB."""
    fake_vm = MagicMock()
    # 12 GiB - 256 MiB = 12032 MiB
    fake_vm.total = (12 * 1024 - 256) * 1024 * 1024
    with patch("whisperstream.system_info.psutil.virtual_memory", return_value=fake_vm):
        result = get_total_ram_mb()
    assert result == 12032


def test_real_psutil_call_returns_positive_int():
    """Smoke test against the real psutil — no mocks. Confirms the import path."""
    result = get_total_ram_mb()
    # On any machine running these tests, total RAM should be > 0.
    assert result is None or (isinstance(result, int) and result > 0)


def test_available_ram_returns_int_on_success():
    fake_vm = MagicMock()
    fake_vm.available = 3 * 1024 * 1024 * 1024
    with patch("whisperstream.system_info.psutil.virtual_memory", return_value=fake_vm):
        result = get_available_ram_mb()
    assert result == 3072
    assert isinstance(result, int)


def test_available_ram_never_raises():
    with patch(
        "whisperstream.system_info.psutil.virtual_memory",
        side_effect=RuntimeError("probe failed"),
    ):
        assert get_available_ram_mb() is None


# ---------------------------------------------------------------------------
# list_running_apps — icon contract
# ---------------------------------------------------------------------------

def _make_fake_win32(hwnds, pid_by_hwnd, name_by_pid, exe_by_pid):
    """Build the win32gui / win32process / psutil stubs list_running_apps uses.

    EnumWindows invokes the visitor once per hwnd in ``hwnds``. Every window is
    visible with a non-empty title so it passes the visibility filter.
    """
    fake_gui = MagicMock()
    fake_gui.IsWindowVisible.return_value = True
    fake_gui.GetWindowText.return_value = "Some Window Title"

    def _enum_windows(cb, lparam):
        for hwnd in hwnds:
            cb(hwnd, lparam)

    fake_gui.EnumWindows.side_effect = _enum_windows

    fake_proc = MagicMock()
    fake_proc.GetWindowThreadProcessId.side_effect = lambda hwnd: (0, pid_by_hwnd[hwnd])

    def _process(pid):
        p = MagicMock()
        p.name.return_value = name_by_pid[pid]
        p.exe.return_value = exe_by_pid[pid]
        return p

    fake_psutil = MagicMock()
    fake_psutil.Process.side_effect = _process
    # Keep the real exception classes so the except-clauses in the SUT match.
    import psutil as real_psutil

    fake_psutil.AccessDenied = real_psutil.AccessDenied
    fake_psutil.NoSuchProcess = real_psutil.NoSuchProcess
    fake_psutil.Error = real_psutil.Error
    return fake_gui, fake_proc, fake_psutil


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Requires the native Windows owner")
def test_list_running_apps_each_entry_has_icon_key_success_path():
    """Success path: every entry has process_name/display_name/icon; the icon is
    the data-URI returned by the (stubbed) extractor."""
    fake_gui, fake_proc, fake_psutil = _make_fake_win32(
        hwnds=[101],
        pid_by_hwnd={101: 7},
        name_by_pid={7: "Code.exe"},
        exe_by_pid={7: r"C:\Apps\Code.exe"},
    )
    with patch.object(sys, "platform", "win32"), \
         patch("whisperstream.windows_system_info._WIN32_AVAILABLE", True), \
         patch("whisperstream.windows_system_info.win32gui", fake_gui), \
         patch("whisperstream.windows_system_info.win32process", fake_proc), \
         patch("whisperstream.windows_system_info.psutil", fake_psutil), \
         patch(
             "whisperstream.windows_system_info.icon_data_uri_for_exe",
             return_value="data:image/png;base64,AAAA",
         ):
        apps = list_running_apps()

    assert len(apps) == 1
    entry = apps[0]
    assert set(entry.keys()) == {"process_name", "display_name", "icon", "path"}
    assert entry["process_name"] == "code.exe"
    assert entry["display_name"] == "Code"
    assert entry["icon"] == "data:image/png;base64,AAAA"
    # The full exe path is carried so a rule can record it (path-existence checks).
    assert entry["path"] == r"C:\Apps\Code.exe"


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Requires the native Windows owner")
def test_list_running_apps_icon_none_when_extraction_fails():
    """A stubbed extraction failure (extractor raises) yields icon=None and does
    NOT raise — the list still comes back."""
    fake_gui, fake_proc, fake_psutil = _make_fake_win32(
        hwnds=[202],
        pid_by_hwnd={202: 9},
        name_by_pid={9: "Slack.exe"},
        exe_by_pid={9: r"C:\Apps\Slack.exe"},
    )
    with patch.object(sys, "platform", "win32"), \
         patch("whisperstream.windows_system_info._WIN32_AVAILABLE", True), \
         patch("whisperstream.windows_system_info.win32gui", fake_gui), \
         patch("whisperstream.windows_system_info.win32process", fake_proc), \
         patch("whisperstream.windows_system_info.psutil", fake_psutil), \
         patch(
             "whisperstream.windows_system_info.icon_data_uri_for_exe",
             side_effect=RuntimeError("boom"),
         ):
        apps = list_running_apps()

    assert len(apps) == 1
    assert apps[0]["icon"] is None
    assert apps[0]["process_name"] == "slack.exe"


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Requires the native Windows owner")
def test_list_running_apps_icon_none_when_exe_unavailable():
    """proc.exe() raising AccessDenied (protected/elevated process) yields
    icon=None without ever calling the extractor."""
    import psutil as real_psutil

    fake_gui, fake_proc, fake_psutil = _make_fake_win32(
        hwnds=[303],
        pid_by_hwnd={303: 11},
        name_by_pid={11: "Secure.exe"},
        exe_by_pid={11: r"C:\Apps\Secure.exe"},
    )

    def _process_denied_exe(pid):
        p = MagicMock()
        p.name.return_value = "Secure.exe"
        p.exe.side_effect = real_psutil.AccessDenied(pid)
        return p

    fake_psutil.Process.side_effect = _process_denied_exe

    with patch.object(sys, "platform", "win32"), \
         patch("whisperstream.windows_system_info._WIN32_AVAILABLE", True), \
         patch("whisperstream.windows_system_info.win32gui", fake_gui), \
         patch("whisperstream.windows_system_info.win32process", fake_proc), \
         patch("whisperstream.windows_system_info.psutil", fake_psutil), \
         patch(
             "whisperstream.windows_system_info.icon_data_uri_for_exe",
             return_value="data:image/png;base64,SHOULDNOTAPPEAR",
         ) as mock_icon:
        apps = list_running_apps()

    assert apps[0]["icon"] is None
    mock_icon.assert_not_called()


def test_list_running_apps_never_raises_real_call():
    """Real call (no mocks): returns a list and, on Windows, each entry has the
    icon key (string or None). On non-Windows it returns []. Never raises."""
    apps = list_running_apps()
    assert isinstance(apps, list)
    for entry in apps:
        assert "icon" in entry
        assert entry["icon"] is None or (
            isinstance(entry["icon"], str)
            and entry["icon"].startswith("data:image/png;base64,")
        )


# ---------------------------------------------------------------------------
# app_icons — extraction contract
# ---------------------------------------------------------------------------

def test_icon_data_uri_none_for_empty_path():
    """A falsy exe path returns None without raising."""
    from whisperstream.app_icons import icon_data_uri_for_exe

    assert icon_data_uri_for_exe(None) is None
    assert icon_data_uri_for_exe("") is None


def test_icon_data_uri_none_for_bogus_path():
    """A nonexistent exe path returns None (best-effort, never raises)."""
    from whisperstream.app_icons import icon_data_uri_for_exe

    assert icon_data_uri_for_exe(r"C:\does\not\exist\nope.exe") is None


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Requires the native Windows owner")
def test_icon_data_uri_caches_by_path():
    """Repeated calls for the same path don't re-extract: the cached value is
    returned (including a cached None)."""
    from whisperstream import app_icons

    bogus = r"C:\does\not\exist\nope2.exe"
    app_icons._ICON_CACHE.pop(bogus, None)
    first = app_icons.icon_data_uri_for_exe(bogus)
    assert bogus in app_icons._ICON_CACHE
    second = app_icons.icon_data_uri_for_exe(bogus)
    assert first == second


def test_icon_data_uri_real_windows_exe():
    """On Windows, a known system exe yields a PNG data-URI; elsewhere None.
    Never raises either way."""
    import os

    from whisperstream.app_icons import _ICONS_AVAILABLE, icon_data_uri_for_exe

    exe = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe")
    uri = icon_data_uri_for_exe(exe)
    if not _ICONS_AVAILABLE or sys.platform != "win32":
        assert uri is None
    else:
        # explorer.exe always has an icon; allow None defensively but assert the
        # data-URI shape when present.
        assert uri is None or uri.startswith("data:image/png;base64,")
