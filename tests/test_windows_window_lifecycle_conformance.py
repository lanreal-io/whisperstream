"""Source-level Windows conformance for window/application lifecycle schema v1."""

from __future__ import annotations

import json
import platform
from pathlib import Path

import pytest

from whisperstream.window_lifecycle_contract import (
    WINDOW_LIFECYCLE_SCHEMA_VERSION,
    LaunchAtLoginState,
    LaunchPresentation,
    WindowAction,
    WindowCloseDisposition,
    WindowFailure,
    WindowRole,
)


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"},
    reason="native Windows AMD64 conformance only",
)


def test_frozen_contract_vocabulary_matches_rust_owner() -> None:
    rust_contract = (ROOT / "src-tauri/src/window_lifecycle_contract.rs").read_text(
        encoding="utf-8"
    )
    assert WINDOW_LIFECYCLE_SCHEMA_VERSION == 1
    for value in WindowRole:
        assert value.name.title().replace("_", "") in rust_contract
    for value in WindowAction:
        assert value.name.title().replace("_", "") in rust_contract
    for value in WindowCloseDisposition:
        assert value.name.title().replace("_", "") in rust_contract
    for value in LaunchPresentation:
        assert value.name.title().replace("_", "") in rust_contract
    for value in LaunchAtLoginState:
        assert value.name.title().replace("_", "") in rust_contract
    for value in WindowFailure:
        assert value.name.title().replace("_", "") in rust_contract


def test_shared_parent_has_no_windows_native_types() -> None:
    contract = (ROOT / "src-tauri/src/window_lifecycle_contract.rs").read_text(
        encoding="utf-8"
    )
    parent = (ROOT / "src-tauri/src/lib.rs").read_text(encoding="utf-8")
    for forbidden in ("tauri::", "windows::", "HWND", "TrayIconBuilder", "ManagerExt"):
        assert forbidden not in contract
    assert "windows::Win32" not in parent
    assert "windows_window_lifecycle::install" in parent


def test_windows_adapter_owns_hwnd_tray_login_and_update_preparation() -> None:
    adapter = (ROOT / "src-tauri/src/windows_window_lifecycle.rs").read_text(
        encoding="utf-8"
    )
    for required in (
        "WindowLifecycle<WindowsWindowPort>",
        "TrayIconBuilder",
        "ManagerExt",
        "HWND",
        "SetWindowPos",
        "SetWindowRgn",
        "prepare_for_update",
        "set_window_launch_at_login",
        "WindowAction::CloseRequest",
        "WindowAction::Quit",
    ):
        assert required in adapter


def test_tauri_roles_preserve_windows_window_policies() -> None:
    config = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    windows = {item["label"]: item for item in config["app"]["windows"]}
    assert windows["main"]["visible"] is False
    assert windows["main"]["resizable"] is False
    assert windows["hud"]["focusable"] is False
    assert windows["hud"]["alwaysOnTop"] is True
    assert windows["hud"]["skipTaskbar"] is True
    assert windows["snap-guide"]["focusable"] is False
    # The official updater creates its own popup; source builds have none.
    assert "popup-update" not in windows
    for label in ("popup-eula",):
        assert windows[label]["visible"] is False
        assert windows[label]["resizable"] is False


def test_frontend_routes_windows_hud_popup_and_login_actions_through_adapter() -> None:
    lifecycle = (ROOT / "frontend/src/lib/windowLifecycle.ts").read_text(
        encoding="utf-8"
    )
    autostart = (ROOT / "frontend/src/lib/autostart.ts").read_text(encoding="utf-8")
    app = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    hud = (ROOT / "frontend/src/pages/HUD.tsx").read_text(encoding="utf-8")
    assert 'invoke("apply_window_lifecycle"' in lifecycle
    assert 'invoke("window_launch_at_login_state")' in autostart
    assert 'applyWindowLifecycle(contractRole, "show")' in app
    reconciler = (ROOT / "frontend/src/pages/hud/useHudNativeReconciler.ts").read_text(
        encoding="utf-8"
    )
    assert "useHudNativeReconciler({" in hud
    assert 'applyWindowLifecycle("hud", action)' in reconciler
    assert 'applyWindowLifecycle("snap", "show")' in hud
