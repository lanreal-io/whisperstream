from __future__ import annotations

from pathlib import Path

from whisperstream.daily_use_capabilities import (
    CapabilityState,
    create_daily_use_capability_report,
)


ROOT = Path(__file__).resolve().parents[1]
RUST_ADAPTER = ROOT / "src-tauri" / "src" / "macos_window_lifecycle.rs"


def test_darwin_capabilities_enable_window_login_and_update_lifecycle() -> None:
    report = create_daily_use_capability_report("Darwin")
    capabilities = {item.capability_id: item for item in report.capabilities}

    assert capabilities["window-lifecycle"].state is CapabilityState.AVAILABLE
    assert capabilities["launch-at-login"].state is CapabilityState.AVAILABLE
    # Only the official build carries an updater; the capability says the
    # platform supports it, as on Windows.
    assert capabilities["auto-update"].state is CapabilityState.AVAILABLE


def test_macos_adapter_owns_appkit_roles_dock_menu_and_reopen() -> None:
    source = RUST_ADAPTER.read_text(encoding="utf-8")
    parent = (ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")

    for marker in (
        'WindowRole::Main => "main"',
        'WindowRole::Hud => "hud"',
        'WindowRole::Snap => "snap-guide"',
        'WindowRole::Review => "popup-review"',
        'WindowRole::Update => "popup-update"',
        'WindowRole::Eula => "popup-eula"',
        'WindowRole::Error => "popup-error"',
        "set_activation_policy",
        "set_dock_visibility",
        "LaunchPresentation::Minimized",
        "CloseRequested",
        "show_main_window",
        "install",
    ):
        assert marker in source
    assert "macos_window_lifecycle::install" in parent
    assert "RunEvent::Reopen" in parent
    assert "agent-console" not in source


def test_macos_menu_bar_uses_a_native_template_icon() -> None:
    source = RUST_ADAPTER.read_text(encoding="utf-8")
    vector = (ROOT / "src-tauri/icons/macos-menu-bar-template.svg").read_text(
        encoding="utf-8"
    )

    assert 'include_bytes!("../icons/macos-menu-bar-template.png")' in source
    assert ".icon_as_template(true)" in source
    assert 'include_bytes!("../icons/32x32.png")' not in source
    assert "<path" in vector
    assert "<rect" not in vector
    assert "background" not in vector.lower()


def test_macos_login_item_uses_typed_smappservice_status_without_approval_automation() -> None:
    source = RUST_ADAPTER.read_text(encoding="utf-8")
    cargo = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    capabilities = (ROOT / "src-tauri" / "capabilities" / "default.json").read_text(
        encoding="utf-8"
    )

    for marker in (
        "SMAppService::mainAppService",
        "SMAppServiceStatus::NotRegistered",
        "SMAppServiceStatus::Enabled",
        "SMAppServiceStatus::RequiresApproval",
        "SMAppServiceStatus::NotFound",
        "registerAndReturnError",
        "unregisterAndReturnError",
    ):
        assert marker in source
    assert 'objc2-service-management = "0.3.2"' in cargo
    assert "openSystemSettingsLoginItems" not in source
    assert "tauri-plugin-autostart" not in source
    assert "autostart:allow-enable" in capabilities  # retained for Windows only


def test_macos_geometry_is_bounded_inside_the_selected_display() -> None:
    source = RUST_ADAPTER.read_text(encoding="utf-8")
    for marker in (
        "bounded_geometry",
        "available_monitors",
        "display_token",
        ".clamp(",
        "LogicalPosition",
        "LogicalSize",
    ):
        assert marker in source


def test_shared_window_roles_match_startup_static_roots() -> None:
    python_contract = (
        ROOT / "src" / "whisperstream" / "window_lifecycle_contract.py"
    ).read_text(encoding="utf-8")
    rust_contract = (
        ROOT / "src-tauri" / "src" / "window_lifecycle_contract.rs"
    ).read_text(encoding="utf-8")

    for role in ("review", "error"):
        assert f'"{role}"' in python_contract
    for role in ("Review", "Error"):
        assert role in rust_contract
    assert "trial-expired" not in python_contract
    assert "TrialExpired" not in rust_contract
    assert "agent-console" not in python_contract


def test_windows_adapter_and_update_policy_remain_owned_and_unchanged() -> None:
    adapter = (ROOT / "src-tauri" / "src" / "windows_window_lifecycle.rs").read_text(
        encoding="utf-8"
    )
    parent = (ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    frontend = (ROOT / "frontend" / "src" / "lib" / "windowLifecycle.ts").read_text(
        encoding="utf-8"
    )
    for marker in (
        "tauri_plugin_autostart::ManagerExt",
        "prepare_for_update",
        "apply_main_window_icon",
        "TrayIconBuilder",
        "set_window_launch_at_login",
    ):
        assert marker in adapter
    assert '#[cfg(target_os = "windows")]\n    let builder = builder.plugin(tauri_plugin_autostart::init' in parent
    assert "navigator.userAgent" not in frontend
    assert 'invoke("apply_window_lifecycle"' in frontend


def test_internal_bundle_is_app_only_adhoc_and_updater_free_by_default() -> None:
    config = (ROOT / "src-tauri" / "tauri.macos.conf.json").read_text(
        encoding="utf-8"
    )
    builder = (ROOT / "scripts" / "build_macos_internal.py").read_text(
        encoding="utf-8"
    )

    assert __import__("json").loads(config)["bundle"]["targets"] == ["app"]
    assert '"signingIdentity": "-"' in config
    assert '"createUpdaterArtifacts": false' in config
    assert '"endpoints": []' in config
    assert '"src-tauri/",' in builder
    assert '"src-tauri/src/macos_window_lifecycle.rs"' not in builder.split(
        "EXCLUDED_EXACT", 1
    )[1].split("}", 1)[0]
    assert "FORBIDDEN_CREDENTIAL_ENV" in builder
    # Only a Developer ID build given the updater provider admits the updater key.
    assert "_assert_non_distribution_environment(updater=providers is not None)" in builder
    assert '"createUpdaterArtifacts": False' in builder


def test_native_status_probe_is_passive_and_content_free() -> None:
    source = RUST_ADAPTER.read_text(encoding="utf-8")
    start = source.index("fn macos_window_lifecycle_passive_status()")
    body = source[start : source.index("\n    }", start)]
    assert "SmAppServiceBridge::status()" in body
    assert "register" not in body
    assert "unregister" not in body
