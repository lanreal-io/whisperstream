"""Deterministic Windows conformance for app/browser context."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

from whisperstream.app_context_contract import (
    ApplicationInventoryScope,
    ContextFailure,
    ContextRequirement,
    ContextStatus,
    ProfileRouteTier,
)
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.foreground_window import RoutingTarget
from whisperstream.power_mode import resolve_enhancement_route
from whisperstream.windows_app_context import WindowsApplicationContextAdapter


def _config() -> WhisperstreamConfig:
    return replace(
        WhisperstreamConfig(),
        profiles=[
            {"id": "default", "name": "Default"},
            {"id": "app", "name": "App"},
            {"id": "site", "name": "Site"},
            {"id": "manual", "name": "Manual"},
        ],
        default_profile_id="default",
        power_mode_map={"browser.exe": "app"},
        site_map={"synthetic.example": "site"},
    )


def test_windows_context_captures_only_logical_app_and_host() -> None:
    adapter = WindowsApplicationContextAdapter(
        routing_probe=lambda **_kwargs: RoutingTarget(
            "Browser.EXE", "synthetic.example"
        ),
        token_factory=lambda: "opaque",
    )

    snapshot = adapter.capture(
        7, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
    )

    assert snapshot.status is ContextStatus.RESOLVED
    assert snapshot.application is not None
    assert snapshot.application.logical_id == "browser.exe"
    assert snapshot.application.instance_token == "windows-app-instance:opaque"
    assert snapshot.browser_page is not None
    assert snapshot.browser_page.normalized_host == "synthetic.example"
    assert not hasattr(snapshot, "hwnd")
    assert not hasattr(snapshot, "pid")
    assert not hasattr(snapshot, "url")


def test_windows_context_browser_failure_preserves_application_fallback() -> None:
    adapter = WindowsApplicationContextAdapter(
        routing_probe=lambda **_kwargs: RoutingTarget("browser.exe", None),
        browser_classifier=lambda _name: True,
    )

    snapshot = adapter.capture(
        3, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
    )

    assert snapshot.status is ContextStatus.APPLICATION_ONLY
    assert snapshot.application is not None
    assert snapshot.browser_page is None
    assert snapshot.failure is None
    assert adapter.safe_capture_metadata(snapshot) == {
        "browser": True,
        "domain_read": "miss",
    }


def test_windows_context_unavailable_never_raises() -> None:
    adapter = WindowsApplicationContextAdapter(
        routing_probe=MagicMock(side_effect=PermissionError("denied"))
    )
    snapshot = adapter.capture(2, ContextRequirement.APPLICATION)
    assert snapshot.status is ContextStatus.UNAVAILABLE
    assert snapshot.failure is ContextFailure.NO_FOREGROUND_APPLICATION


def test_windows_inventory_and_asset_resolution_are_adapter_owned() -> None:
    running = [
        {
            "process_name": "synthetic.exe",
            "display_name": "Synthetic",
            "icon": "data:image/png;base64,c3ludGhldGlj",
        }
    ]
    installed = [{**running[0], "installed": True}]
    adapter = WindowsApplicationContextAdapter(
        running_inventory=lambda: list(running),
        installed_inventory=lambda: list(installed),
        app_resolver=lambda names, rows, **_kwargs: [
            {**rows[0], "installed": names == ["synthetic.exe"]}
        ],
        path_resolver=lambda path: installed[0] if path.endswith(".exe") else None,
        favicon_resolver=lambda _domain: "data:image/png;base64,ZmF2aWNvbg==",
        domain_normalizer=lambda value: (
            str(value).lower() if value == "Synthetic.Example" else None
        ),
        token_factory=lambda: "inventory",
    )

    descriptors = adapter.inventory(ApplicationInventoryScope.RUNNING)
    assert len(descriptors) == 1
    assert descriptors[0].application.logical_id == "synthetic.exe"
    assert descriptors[0].icon_token is not None
    assert adapter.icon_payload(descriptors[0].icon_token or "") == running[0]["icon"]
    assert adapter.running_apps() == running
    assert adapter.installed_apps() == installed
    assert adapter.resolve_apps(["synthetic.exe"])[0]["installed"] is True
    assert adapter.resolve_path("synthetic.exe") == installed[0]
    assert adapter.resolve_favicons(["Synthetic.Example", "invalid"]) == [
        {
            "domain": "synthetic.example",
            "icon": "data:image/png;base64,ZmF2aWNvbg==",
        }
    ]

    adapter.close()
    assert adapter.closed is True
    assert adapter.icon_payload(descriptors[0].icon_token or "") is None
    assert adapter.running_apps() == []


def test_power_mode_contract_precedence_and_stale_fallback() -> None:
    adapter = WindowsApplicationContextAdapter(
        routing_probe=lambda **_kwargs: RoutingTarget(
            "browser.exe", "synthetic.example"
        ),
        token_factory=lambda: "route",
    )
    snapshot = adapter.capture(
        8, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
    )
    config = _config()

    manual = resolve_enhancement_route(
        snapshot,
        config,
        manual_profile_id="manual",
        manual_anchor="browser.exe",
    )
    assert manual is not None and manual.tier is ProfileRouteTier.MANUAL
    site = resolve_enhancement_route(snapshot, config)
    assert site is not None and site.tier is ProfileRouteTier.SITE
    app_only = adapter.capture(9, ContextRequirement.APPLICATION)
    app_route = resolve_enhancement_route(app_only, config)
    assert app_route is not None and app_route.tier is ProfileRouteTier.APPLICATION
    stale = replace(config, power_mode_map={"browser.exe": "deleted"}, site_map={})
    default = resolve_enhancement_route(app_only, stale)
    assert default is not None and default.tier is ProfileRouteTier.DEFAULT


def test_contract_modules_contain_no_windows_native_types() -> None:
    import whisperstream.app_context_contract as app_contract

    names = set(vars(app_contract))
    lowered = {name.lower() for name in names}
    assert "hwnd" not in lowered
    assert "pid" not in lowered
    assert "iuiautomationelement" not in lowered
