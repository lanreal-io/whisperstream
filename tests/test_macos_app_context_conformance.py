"""Darwin conformance for opaque app/browser context and Power Mode assets."""

from __future__ import annotations

from dataclasses import replace
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from whisperstream.advanced_capabilities import (
    AdvancedCapabilityState,
    create_advanced_capability_report,
)
from whisperstream.app_context import create_application_context_adapter
from whisperstream.app_context_contract import (
    ApplicationInventoryScope,
    ContextFailure,
    ContextRequirement,
    ContextStatus,
    ProfileRouteTier,
)
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.macos_app_context import (
    MacosApplicationContextAdapter,
    _MacosApplicationObservation,
    _is_browser_address_control,
    _may_descend_browser_accessibility,
    _read_preferred_browser_window,
)
from whisperstream.power_mode import resolve_enhancement_route


def _observation(
    *,
    bundle_id: str = "org.example.synthetic-browser",
    pid: int = 101,
    name: str = "Synthetic Browser",
    icon: str | None = "data:image/png;base64,c3ludGhldGlj",
) -> _MacosApplicationObservation:
    return _MacosApplicationObservation(bundle_id, pid, name, icon)


class _Workspace:
    def __init__(self, frontmost: _MacosApplicationObservation | None) -> None:
        self.frontmost = frontmost
        self.running = [frontmost] if frontmost else []
        self.installed = [
            _observation(pid=0),
            _observation(
                bundle_id="org.example.editor",
                pid=0,
                name="Synthetic Editor",
                icon=None,
            ),
        ]
        self.closed = False

    def frontmost_application(self) -> _MacosApplicationObservation | None:
        return self.frontmost

    def running_applications(self) -> list[_MacosApplicationObservation]:
        return list(self.running)

    def installed_applications(self, logical_ids=None) -> list[_MacosApplicationObservation]:
        return list(self.installed)

    def close(self) -> None:
        self.closed = True


def _config(app_id: str) -> WhisperstreamConfig:
    return replace(
        WhisperstreamConfig(),
        profiles=[
            {"id": "default", "name": "Default"},
            {"id": "app", "name": "App"},
            {"id": "site", "name": "Site"},
            {"id": "manual", "name": "Manual"},
        ],
        default_profile_id="default",
        power_mode_map={app_id: "app"},
        site_map={"deep.synthetic.example.test": "site"},
    )


def test_darwin_factory_selects_native_adapter_without_affecting_windows() -> None:
    with patch(
        "whisperstream.macos_app_context._NativeMacosWorkspace",
        return_value=_Workspace(None),
    ):
        mac = create_application_context_adapter(system="Darwin", machine="arm64")
    assert isinstance(mac, MacosApplicationContextAdapter)
    mac.close()

    windows = create_application_context_adapter(system="Windows", machine="AMD64")
    assert windows.__class__.__name__ == "WindowsApplicationContextAdapter"
    windows.close()


def test_capture_uses_stable_opaque_app_id_and_generation_bound_instance() -> None:
    workspace = _Workspace(_observation())
    tokens = iter(("first-opaque", "second-opaque"))
    adapter = MacosApplicationContextAdapter(
        workspace=workspace,
        browser_location_reader=lambda _app: None,
        token_factory=lambda: next(tokens),
    )
    first = adapter.capture(7, ContextRequirement.APPLICATION)
    workspace.frontmost = _observation(pid=202)
    second = adapter.capture(8, ContextRequirement.APPLICATION)

    assert first.status is ContextStatus.RESOLVED
    assert first.application is not None and second.application is not None
    assert first.application.logical_id == second.application.logical_id
    assert first.application.logical_id.startswith("macos-app:sha256:")
    assert "org.example" not in first.application.logical_id
    assert first.application.instance_token != second.application.instance_token
    assert first.application.instance_token == "macos-app-instance:first-opaque"
    assert second.application.instance_token == "macos-app-instance:second-opaque"
    assert "101" not in first.application.instance_token
    assert not hasattr(first, "pid")
    assert not hasattr(first, "bundle_identifier")


def test_browser_reader_keeps_url_private_and_routes_exact_lowercase_host() -> None:
    raw_url = "https://Deep.Synthetic.Example.Test:9443/private/path?q=secret#title"
    adapter = MacosApplicationContextAdapter(
        workspace=_Workspace(_observation()),
        browser_location_reader=lambda _app: raw_url,
        browser_bundle_ids={"org.example.synthetic-browser"},
    )
    snapshot = adapter.capture(
        9, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
    )

    assert snapshot.status is ContextStatus.RESOLVED
    assert snapshot.browser_page is not None
    assert snapshot.browser_page.normalized_host == "deep.synthetic.example.test"
    assert "private/path" not in repr(snapshot)
    assert not hasattr(snapshot.browser_page, "url")
    assert resolve_enhancement_route(
        snapshot, _config(snapshot.application.logical_id)
    ).tier is ProfileRouteTier.SITE


def test_browser_reader_strips_only_leading_www_without_etld_folding() -> None:
    adapter = MacosApplicationContextAdapter(
        workspace=_Workspace(_observation()),
        browser_location_reader=lambda _app: "https://WWW.Deep.Example.Test/private",
        browser_bundle_ids={"org.example.synthetic-browser"},
    )
    snapshot = adapter.capture(
        10, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
    )

    assert snapshot.browser_page is not None
    assert snapshot.browser_page.normalized_host == "deep.example.test"


def test_browser_accessibility_policy_rejects_page_content_and_unknown_fields() -> None:
    assert not _may_descend_browser_accessibility("AXWebArea")
    assert not _may_descend_browser_accessibility("AXDocument")
    assert _may_descend_browser_accessibility("AXGroup")
    assert _is_browser_address_control(
        "com.google.chrome",
        "AXTextField",
        "Address and search bar",
        "",
    )
    assert not _is_browser_address_control(
        "com.google.chrome",
        "AXTextField",
        "synthetic-page-field",
        "Synthetic page content",
    )
    assert not _is_browser_address_control(
        "com.apple.safari",
        "AXWebArea",
        "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD",
        "",
    )
    assert not _is_browser_address_control(
        "org.mozilla.firefox",
        "AXGroup",
        "urlbar-input",
        "",
    )
    # A page-content decoy can reproduce a chrome signature, but its WebArea
    # ancestor is pruned before identifier, description, or value are read.
    assert _is_browser_address_control(
        "com.google.chrome",
        "AXTextField",
        "Address and search bar",
        "",
    )


def test_browser_accessibility_uses_main_only_when_focused_window_is_absent() -> None:
    releases: list[int] = []
    walks: list[int] = []

    def walk(window: int, _depth: int) -> str | None:
        walks.append(window)
        return None if window == 10 else "https://main.example.test"

    focused_unmatched = _read_preferred_browser_window(
        1,
        read_attribute=lambda _root, attribute: {
            "AXFocusedWindow": 10,
            "AXMainWindow": 20,
        }[attribute],
        release=releases.append,
        walk=walk,
    )
    assert focused_unmatched is None
    assert walks == [10]
    assert releases == [10]

    releases.clear()
    walks.clear()
    focused_missing = _read_preferred_browser_window(
        1,
        read_attribute=lambda _root, attribute: {
            "AXFocusedWindow": None,
            "AXMainWindow": 20,
        }[attribute],
        release=releases.append,
        walk=walk,
    )
    assert focused_missing == "https://main.example.test"
    assert walks == [20]
    assert releases == [20]


def test_manual_site_app_default_precedence_uses_one_snapshot() -> None:
    adapter = MacosApplicationContextAdapter(
        workspace=_Workspace(_observation()),
        browser_location_reader=lambda _app: "http://deep.synthetic.example.test/page",
        browser_bundle_ids={"org.example.synthetic-browser"},
    )
    snapshot = adapter.capture(
        11, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
    )
    assert snapshot.application is not None
    config = _config(snapshot.application.logical_id)

    manual = resolve_enhancement_route(
        snapshot,
        config,
        manual_profile_id="manual",
        manual_anchor=snapshot.application.logical_id,
    )
    assert manual is not None and manual.tier is ProfileRouteTier.MANUAL
    assert manual.generation == 11
    assert resolve_enhancement_route(snapshot, config).tier is ProfileRouteTier.SITE

    app_only = replace(snapshot, status=ContextStatus.APPLICATION_ONLY, browser_page=None)
    assert resolve_enhancement_route(app_only, config).tier is ProfileRouteTier.APPLICATION
    default = replace(config, power_mode_map={}, site_map={})
    assert resolve_enhancement_route(app_only, default).tier is ProfileRouteTier.DEFAULT


def test_missing_unrecognized_or_invalid_browser_identity_falls_through() -> None:
    reader = MagicMock(side_effect=PermissionError("private native failure"))
    unrecognized = MacosApplicationContextAdapter(
        workspace=_Workspace(_observation(bundle_id="org.example.editor")),
        browser_location_reader=reader,
    ).capture(2, ContextRequirement.APPLICATION_AND_BROWSER_PAGE)
    assert unrecognized.status is ContextStatus.APPLICATION_ONLY
    reader.assert_not_called()

    for value in (None, "", "localized address text", "file:///private/document"):
        adapter = MacosApplicationContextAdapter(
            workspace=_Workspace(_observation()),
            browser_location_reader=lambda _app, value=value: value,
            browser_bundle_ids={"org.example.synthetic-browser"},
        )
        snapshot = adapter.capture(
            3, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
        )
        assert snapshot.status is ContextStatus.APPLICATION_ONLY
        assert snapshot.failure is None

    failed = MacosApplicationContextAdapter(
        workspace=_Workspace(_observation()),
        browser_location_reader=MagicMock(side_effect=RuntimeError("native")),
        browser_bundle_ids={"org.example.synthetic-browser"},
    ).capture(4, ContextRequirement.APPLICATION_AND_BROWSER_PAGE)
    assert failed.status is ContextStatus.APPLICATION_ONLY


def test_browser_capture_timeout_ignores_late_result_and_close_joins_worker() -> None:
    release = threading.Event()
    cancelled = threading.Event()

    def slow_reader(
        _application: _MacosApplicationObservation,
        *,
        cancel_event: threading.Event,
        deadline: float,
    ) -> str | None:
        while not release.is_set() and time.monotonic() < deadline + 0.5:
            if cancel_event.wait(0.005):
                cancelled.set()
                return "https://late.example.test/private"
        return "https://late.example.test/private"

    adapter = MacosApplicationContextAdapter(
        workspace=_Workspace(_observation()),
        browser_location_reader=slow_reader,
        browser_bundle_ids={"org.example.synthetic-browser"},
        browser_capture_timeout=0.03,
    )
    snapshot = adapter.capture(
        15, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
    )
    adapter.close()
    release.set()

    assert snapshot.status is ContextStatus.APPLICATION_ONLY
    assert snapshot.browser_page is None
    assert cancelled.wait(0.2)
    assert not adapter._browser_worker.alive


def test_close_cancels_an_inflight_browser_read_without_worker_leak() -> None:
    entered = threading.Event()
    cancelled = threading.Event()

    def cooperative_reader(
        _application: _MacosApplicationObservation,
        *,
        cancel_event: threading.Event,
        deadline: float,
    ) -> str | None:
        entered.set()
        while time.monotonic() < deadline:
            if cancel_event.wait(0.005):
                cancelled.set()
                return None
        return None

    adapter = MacosApplicationContextAdapter(
        workspace=_Workspace(_observation()),
        browser_location_reader=cooperative_reader,
        browser_bundle_ids={"org.example.synthetic-browser"},
        browser_capture_timeout=0.5,
    )
    result: list[ContextStatus] = []
    capture = threading.Thread(
        target=lambda: result.append(
            adapter.capture(
                16,
                ContextRequirement.APPLICATION_AND_BROWSER_PAGE,
            ).status
        )
    )
    capture.start()
    assert entered.wait(0.2)
    adapter.close()
    capture.join(1.0)

    assert cancelled.is_set()
    assert result == [ContextStatus.APPLICATION_ONLY]
    assert not adapter._browser_worker.alive


def test_no_foreground_app_is_typed_and_never_raises() -> None:
    adapter = MacosApplicationContextAdapter(workspace=_Workspace(None))
    snapshot = adapter.capture(5, ContextRequirement.APPLICATION)
    assert snapshot.status is ContextStatus.UNAVAILABLE
    assert snapshot.failure is ContextFailure.NO_FOREGROUND_APPLICATION
    assert adapter._browser_worker is None
    adapter.close()


def test_inventory_assets_are_cosmetic_local_only_and_cleanup_is_exact() -> None:
    workspace = _Workspace(_observation())
    mapping = {"sentinel": "profile"}
    adapter = MacosApplicationContextAdapter(
        workspace=workspace,
        favicon_resolver=lambda host: (
            "data:image/png;base64,ZmF2aWNvbg=="
            if host == "synthetic.example.test"
            else None
        ),
    )

    descriptors = adapter.inventory(ApplicationInventoryScope.INSTALLED)
    assert len(descriptors) == 2
    assert mapping == {"sentinel": "profile"}
    rows = adapter.installed_apps()
    assert all(row["installed"] is True for row in rows)
    assert all(str(row["process_name"]).startswith("macos-app:sha256:") for row in rows)
    assert all("path" not in row and "bundle_id" not in row and "pid" not in row for row in rows)
    resolved = adapter.resolve_apps([rows[0]["process_name"], "unknown"])
    assert [row["installed"] for row in resolved] == [True, False]
    assert adapter.resolve_path("/Applications/Private.app") is None
    assert adapter.resolve_favicons(["Synthetic.Example.Test", "bad host"]) == [
        {
            "domain": "synthetic.example.test",
            "icon": "data:image/png;base64,ZmF2aWNvbg==",
        }
    ]

    token = descriptors[0].icon_token
    assert token is not None and adapter.icon_payload(token) is not None
    adapter.close()
    adapter.close()
    assert workspace.closed
    assert adapter.icon_payload(token) is None
    assert adapter.running_apps() == []
    assert adapter.resolve_favicons(["synthetic.example.test"]) == []
    closed = adapter.capture(12, ContextRequirement.APPLICATION)
    assert closed.status is ContextStatus.FAILED
    assert closed.failure is ContextFailure.NATIVE_ERROR


def test_favicon_cache_strips_www_expires_and_evicts_at_128_entries() -> None:
    now = [0.0]
    calls: list[str] = []

    def resolve(host: str) -> str | None:
        calls.append(host)
        return f"data:image/png;base64,{len(calls)}"

    adapter = MacosApplicationContextAdapter(
        workspace=_Workspace(None),
        favicon_resolver=resolve,
        monotonic=lambda: now[0],
    )
    first = adapter.resolve_favicons(["WWW.Example.Test"])
    now[0] = 299.0
    second = adapter.resolve_favicons(["example.test"])
    now[0] = 301.0
    third = adapter.resolve_favicons(["example.test"])

    assert [row["domain"] for row in (*first, *second, *third)] == [
        "example.test",
        "example.test",
        "example.test",
    ]
    assert calls == ["example.test", "example.test"]
    assert first[0]["icon"] == second[0]["icon"]
    assert third[0]["icon"] != second[0]["icon"]

    for index in range(128):
        adapter.resolve_favicons([f"host-{index}.example.test"])
    assert len(adapter._favicon_cache) == 128
    before = len(calls)
    adapter.resolve_favicons(["example.test"])
    assert len(calls) == before + 1


def test_darwin_context_capabilities_are_truthfully_available() -> None:
    report = create_advanced_capability_report("Darwin")
    capabilities = {item.capability_id: item for item in report.capabilities}
    assert capabilities["app-identity"].state is AdvancedCapabilityState.AVAILABLE
    assert (
        capabilities["browser-page-identity"].state
        is AdvancedCapabilityState.AVAILABLE
    )


def test_advanced_capability_endpoint_publishes_typed_mac_context_support() -> None:
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.set_event_broadcaster = MagicMock()
    app.license_manager.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    client = TestClient(server.fastapi, raise_server_exceptions=True)

    with patch("platform.system", return_value="Darwin"):
        response = client.get("/api/capabilities/advanced")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["platform"] == "Darwin"
    assert [item["id"] for item in body["capabilities"]] == sorted(
        item["id"] for item in body["capabilities"]
    )
    by_id = {item["id"]: item for item in body["capabilities"]}
    assert by_id["app-identity"]["presentation"] == "enable"
    assert by_id["browser-page-identity"]["presentation"] == "enable"
    assert by_id["app-path-browse"] == {
        "id": "app-path-browse",
        "state": "unavailable",
        "reason": "raw-path-boundary-unavailable",
        "presentation": "disable",
    }


def test_content_free_metadata_never_exports_identity() -> None:
    adapter = MacosApplicationContextAdapter(
        workspace=_Workspace(_observation()),
        browser_location_reader=lambda _app: "https://synthetic.example.test/private",
        browser_bundle_ids={"org.example.synthetic-browser"},
    )
    snapshot = adapter.capture(
        13, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
    )
    assert adapter.safe_capture_metadata(snapshot) == {
        "browser": True,
        "domain_read": "hit",
    }

    native = MacosApplicationContextAdapter(
        workspace=_Workspace(_observation(bundle_id="org.example.editor")),
    )
    native_snapshot = native.capture(
        14, ContextRequirement.APPLICATION_AND_BROWSER_PAGE
    )
    assert native.safe_capture_metadata(native_snapshot) == {
        "browser": False,
        "domain_read": "n/a",
    }


def test_resolve_legacy_or_empty_rules_does_not_enumerate_native_icons() -> None:
    workspace = _Workspace(None)
    workspace.installed_applications = MagicMock(side_effect=AssertionError("unneeded inventory"))
    adapter = MacosApplicationContextAdapter(workspace=workspace)
    assert adapter.resolve_apps([]) == []
    assert adapter.resolve_apps(["outlook.exe"])[0]["installed"] is False
    workspace.installed_applications.assert_not_called()
    adapter.close()


def test_native_inventory_filters_before_rendering(tmp_path, monkeypatch) -> None:
    import plistlib
    from whisperstream.macos_app_context import _NativeMacosWorkspace, _opaque

    for name in ("one", "two"):
        contents = tmp_path / f"{name}.app" / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": f"org.example.{name}", "CFBundleName": name,
        }))
    workspace = object.__new__(_NativeMacosWorkspace)
    workspace._closed = False
    workspace._INSTALL_ROOTS = (tmp_path,)
    workspace._icon_data_uri = MagicMock(return_value=None)
    monkeypatch.setenv("CFFIXED_USER_HOME", str(tmp_path))
    rows = workspace.installed_applications({_opaque("app", "org.example.two")})
    assert len(rows) == 1
    assert rows[0].bundle_identifier == "org.example.two"
    workspace._icon_data_uri.assert_called_once_with(str(tmp_path / "two.app"))


def test_inventory_request_does_not_block_unrelated_api_requests() -> None:
    import asyncio
    import httpx
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True, "trial_remaining_seconds": 0}
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    entered = threading.Event()
    release = threading.Event()

    def slow_inventory(*args, **kwargs):
        entered.set()
        assert release.wait(3), "cosmetic inventory blocked the API event loop"
        return []

    server._application_context.resolve_apps = slow_inventory

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.fastapi), base_url="http://127.0.0.1") as client:
            pending = asyncio.create_task(client.post("/api/apps/resolve", json={"names": ["synthetic"]}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                response = await asyncio.wait_for(client.get("/api/capabilities/advanced"), 1)
                assert response.status_code == 200
            finally:
                release.set()
                response = await pending
            assert response.status_code == 200

    asyncio.run(exercise())


def test_running_inventory_decorates_icons_outside_coarse_owner_snapshot(monkeypatch):
    from whisperstream.macos_app_context import _NativeMacosWorkspace
    import whisperstream.macos_native_query_owner as native_owner

    workspace = object.__new__(_NativeMacosWorkspace)
    observations = [_observation(pid=101, icon=None), _observation(pid=102, icon=None)]
    workspace._running_applications_main = lambda: observations
    icon_requests = []

    def icon_snapshot(observation):
        icon_requests.append(observation.process_identifier)
        return f'data:image/png;base64,synthetic-{observation.process_identifier}'

    workspace._running_icon_main = icon_snapshot
    requests = []

    def owner_query(operation):
        requests.append(operation)
        return operation()

    monkeypatch.setattr(native_owner, 'query_on_main', owner_query)
    result = workspace.running_applications()
    assert len(requests) == 3
    assert icon_requests == [101, 102]
    assert all(observation.icon_data_uri is not None for observation in result)
    assert all(observation.icon_data_uri is None for observation in observations)
