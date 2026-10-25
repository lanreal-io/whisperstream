"""Display payloads cannot accumulate with changing application icons."""
from whisperstream.app_context_contract import ApplicationInventoryScope
from whisperstream import windows_app_context, macos_app_context
from tests.test_macos_app_context_conformance import _Workspace, _observation


def test_windows_icons_are_lru_bounded_and_favicons_have_no_second_owner(monkeypatch):
    monkeypatch.setattr(windows_app_context, "_MAX_ICON_ASSETS", 2)
    rows = []
    adapter = windows_app_context.WindowsApplicationContextAdapter(
        running_inventory=lambda: rows,
        favicon_resolver=lambda domain: "data:image/png;base64,synthetic",
    )
    tokens = []
    try:
        for index in range(4):
            rows[:] = [{"process_name": "synthetic.exe", "display_name": "Synthetic",
                        "icon": f"data:image/png;base64,{index}"}]
            tokens.append(adapter.inventory(ApplicationInventoryScope.RUNNING)[0].icon_token)
        assert len(adapter._assets) == 2
        assert adapter.icon_payload(tokens[0]) is None
        assert adapter.icon_payload(tokens[-1]) is not None
        adapter.resolve_favicons(["synthetic.example"])
        assert len(adapter._assets) == 2
    finally:
        adapter.close()
    assert not adapter._assets


def test_macos_icons_are_bounded_across_inventory_refreshes(monkeypatch):
    monkeypatch.setattr(macos_app_context, "_MAX_ICON_ASSETS", 2)
    workspace = _Workspace(_observation())
    adapter = macos_app_context.MacosApplicationContextAdapter(workspace=workspace)
    tokens = []
    try:
        for index in range(4):
            workspace.running = [_observation(icon=f"data:image/png;base64,{index}")]
            tokens.append(adapter.inventory(ApplicationInventoryScope.RUNNING)[0].icon_token)
        assert len(adapter._assets) == 2
        assert adapter.icon_payload(tokens[0]) is None
        assert adapter.icon_payload(tokens[-1]) is not None
    finally:
        adapter.close()
    assert not adapter._assets


def test_large_macos_response_keeps_its_icons_when_reuse_cache_evicts(monkeypatch):
    monkeypatch.setattr(macos_app_context, "_MAX_ICON_ASSETS", 2)
    workspace = _Workspace(_observation())
    workspace.running = [
        _observation(bundle_id=f"org.example.app{index}", icon=f"data:image/png;base64,{index}")
        for index in range(4)
    ]
    adapter = macos_app_context.MacosApplicationContextAdapter(workspace=workspace)
    try:
        rows = adapter._rows(ApplicationInventoryScope.RUNNING)
        assert len(rows) == 4
        assert all(row["icon"] for row in rows)
        assert len(adapter._assets) == 2
    finally:
        adapter.close()


def test_macos_negative_favicon_result_recovers_after_short_miss_window():
    now = [0.]
    icon = [None]
    adapter = macos_app_context.MacosApplicationContextAdapter(
        workspace=_Workspace(None),
        favicon_resolver=lambda host: icon[0],
        monotonic=lambda: now[0],
    )
    try:
        assert adapter.resolve_favicons(["fresh.example"])[0]["icon"] is None
        icon[0] = "data:image/png;base64,synthetic"
        now[0] = 16.
        assert adapter.resolve_favicons(["fresh.example"])[0]["icon"] == icon[0]
    finally:
        adapter.close()
