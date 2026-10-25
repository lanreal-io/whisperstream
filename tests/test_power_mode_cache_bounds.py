"""Privacy boundaries for Power Mode's process-memory identity caches."""

from __future__ import annotations

import pytest

from whisperstream import app_directory, app_icons


@pytest.fixture(autouse=True)
def _clear_icon_cache():
    app_icons._ICON_CACHE.clear()
    yield
    app_icons._ICON_CACHE.clear()


def test_start_menu_index_cache_is_count_bounded_and_expires(monkeypatch):
    now = [100.0]
    builds = [
        {"a.exe": "A", "b.exe": "B", "c.exe": "C"},
        {"new.exe": "N"},
    ]
    monkeypatch.setattr(app_directory.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(app_directory, "_DIRECTORY_CACHE_TTL_SECONDS", 5.0)
    monkeypatch.setattr(app_directory, "_DIRECTORY_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(app_directory, "_START_MENU_INDEX", None)
    monkeypatch.setattr(app_directory, "_START_MENU_INDEX_EXPIRES_AT", 0.0)
    monkeypatch.setattr(
        app_directory,
        "_build_start_menu_index",
        lambda: builds.pop(0),
    )

    first = app_directory._get_start_menu_index()
    assert first == {"a.exe": "A", "b.exe": "B"}
    assert app_directory._get_start_menu_index() is first
    assert len(builds) == 1

    now[0] += 6.0
    refreshed = app_directory._get_start_menu_index()
    assert refreshed == {"new.exe": "N"}
    assert "a.exe" not in refreshed
    assert builds == []


def test_installed_index_cache_is_count_bounded_and_evicts_removed(monkeypatch):
    now = [200.0]
    builds = [
        {"a.exe": "A", "b.exe": "B", "c.exe": "C"},
        {"c.exe": "C"},
    ]
    monkeypatch.setattr(app_directory.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(app_directory, "_DIRECTORY_CACHE_TTL_SECONDS", 5.0)
    monkeypatch.setattr(app_directory, "_DIRECTORY_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(app_directory, "_INSTALLED_INDEX", None)
    monkeypatch.setattr(app_directory, "_INSTALLED_INDEX_EXPIRES_AT", 0.0)
    monkeypatch.setattr(app_directory, "_INSTALLED_APPS", [{"stale": True}])
    monkeypatch.setattr(app_directory, "_INSTALLED_APPS_EXPIRES_AT", 999.0)
    monkeypatch.setattr(
        app_directory,
        "_build_installed_index",
        lambda: builds.pop(0),
    )

    first = app_directory.installed_index()
    assert first == {"a.exe": "A", "b.exe": "B"}
    assert app_directory.installed_index() is first
    assert app_directory._INSTALLED_APPS is None

    now[0] += 6.0
    refreshed = app_directory.installed_index()
    assert refreshed == {"c.exe": "C"}
    assert "a.exe" not in refreshed


def test_installed_app_records_expire_and_drop_removed_entries(monkeypatch):
    now = [300.0]
    current_index = {
        "alpha.exe": "A",
        "beta.exe": "B",
        "gamma.exe": "C",
    }
    monkeypatch.setattr(app_directory.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(app_directory, "_DIRECTORY_CACHE_TTL_SECONDS", 5.0)
    monkeypatch.setattr(app_directory, "_DIRECTORY_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(app_directory, "_INSTALLED_APPS", None)
    monkeypatch.setattr(app_directory, "_INSTALLED_APPS_EXPIRES_AT", 0.0)
    monkeypatch.setattr(app_directory, "installed_index", lambda: current_index)
    monkeypatch.setattr(app_directory, "friendly_app_name", lambda base, _path: base)
    monkeypatch.setattr(app_directory, "icon_data_uri_for_exe", lambda _path: None)

    first = app_directory.list_installed_apps()
    assert [row["process_name"] for row in first] == ["alpha.exe", "beta.exe"]
    assert app_directory.list_installed_apps() is first

    current_index = {"gamma.exe": "C"}
    now[0] += 6.0
    refreshed = app_directory.list_installed_apps()
    assert [row["process_name"] for row in refreshed] == ["gamma.exe"]
    assert all(row["process_name"] != "alpha.exe" for row in refreshed)


def test_icon_cache_is_lru_count_bounded_and_expires(monkeypatch):
    now = [400.0]

    class FakeWin32Gui:
        def __init__(self):
            self.calls: list[str] = []

        def ExtractIconEx(self, path, *_args):
            self.calls.append(path)
            return ([len(self.calls)], [])

        def DestroyIcon(self, _handle):
            return None

    fake = FakeWin32Gui()
    monkeypatch.setattr(app_icons.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(app_icons, "_ICONS_AVAILABLE", True)
    monkeypatch.setattr(app_icons, "_ICON_CACHE_TTL_SECONDS", 5.0)
    monkeypatch.setattr(app_icons, "_ICON_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(app_icons, "win32gui", fake, raising=False)
    monkeypatch.setattr(app_icons, "_rasterize_hicon", lambda handle: bytes([handle]))

    app_icons.icon_data_uri_for_exe("A.exe")
    app_icons.icon_data_uri_for_exe("B.exe")
    app_icons.icon_data_uri_for_exe("C.exe")
    assert list(app_icons._ICON_CACHE) == ["B.exe", "C.exe"]

    app_icons.icon_data_uri_for_exe("B.exe")
    assert fake.calls == ["A.exe", "B.exe", "C.exe"]
    assert list(app_icons._ICON_CACHE) == ["C.exe", "B.exe"]

    now[0] += 6.0
    app_icons.icon_data_uri_for_exe("B.exe")
    assert fake.calls == ["A.exe", "B.exe", "C.exe", "B.exe"]
    assert list(app_icons._ICON_CACHE) == ["B.exe"]
