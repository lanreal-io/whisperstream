"""Tests for whisperstream.browser_favicons (local-cache favicon extraction).

Only the pure / guardable bits are unit-tested here. The module reads the
installed browsers' favicon caches, which are machine-specific, so no test
asserts on real machine state. We assert:

- best-effort never-raise contract: non-win32 / missing-DB / bogus domain all
  return None without raising;
- the per-domain cache returns the SAME object (including a cached None);
- the Chromium schema path works when pointed at a tiny in-memory-on-disk SQLite
  DB built with the real ``icon_mapping`` + ``favicon_bitmaps`` schema and one
  PNG row -> a ``data:image/png;base64,...`` URI comes back.
- the Firefox schema path works against a synthesized ``favicons.sqlite``.

Privacy: the domain and the icon bytes are never logged; no test asserts a log
line, because nothing must ever emit one.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import sys
import time
from unittest.mock import MagicMock

import pytest

from whisperstream import browser_favicons, temp_lifecycle
from whisperstream.browser_favicons import favicon_data_uri_for_domain

# A 1x1 transparent PNG (smallest valid PNG); stand-in for a cached favicon blob.
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with a clean module cache so cached Nones don't leak."""
    browser_favicons._FAVICON_CACHE.clear()
    yield
    browser_favicons._FAVICON_CACHE.clear()


# ---------------------------------------------------------------------------
# Never-raise contract
# ---------------------------------------------------------------------------

def test_non_win32_returns_none(monkeypatch):
    """On a non-Windows platform the whole module degrades to None."""
    monkeypatch.setattr(browser_favicons, "_FAVICONS_AVAILABLE", False)
    assert favicon_data_uri_for_domain("github.com") is None


def test_falsy_domain_returns_none():
    """A None / empty / whitespace domain returns None without raising."""
    assert favicon_data_uri_for_domain(None) is None
    assert favicon_data_uri_for_domain("") is None
    assert favicon_data_uri_for_domain("   ") is None


@pytest.mark.skipif(sys.platform != "win32", reason="win32-only path")
def test_bogus_domain_no_cache_returns_none(monkeypatch):
    """A domain with no cached favicon (no browser DBs found) returns None.

    Point the browser-path resolvers at nothing so the scan finds no DB and the
    function falls through to None — exercising the win32 branch without
    depending on what favicons the test machine actually has cached.
    """
    monkeypatch.setattr(browser_favicons, "_chromium_favicon_paths", lambda: [])
    monkeypatch.setattr(browser_favicons, "_firefox_favicons_paths", lambda: [])
    assert favicon_data_uri_for_domain("no-such-domain-xyz.example") is None


def test_query_chromium_missing_file_returns_none():
    """Pointing the Chromium query at a non-existent path returns None."""
    assert browser_favicons._query_chromium(r"C:\nope\Favicons", "github.com") is None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_cache_returns_same_object(monkeypatch):
    """A second call for the same domain returns the cached object, not a re-scan."""
    monkeypatch.setattr(browser_favicons, "_FAVICONS_AVAILABLE", True)

    calls = {"n": 0}

    def _fake_paths():
        calls["n"] += 1
        return []

    monkeypatch.setattr(browser_favicons, "_chromium_favicon_paths", _fake_paths)
    monkeypatch.setattr(browser_favicons, "_firefox_favicons_paths", lambda: [])

    first = favicon_data_uri_for_domain("example.com")
    second = favicon_data_uri_for_domain("example.com")
    # Cached None is returned without re-running the scan.
    assert first is None and second is None
    assert calls["n"] == 1


def test_cache_entry_expires(monkeypatch):
    """Expired domain identities are discarded and re-read on demand."""
    monkeypatch.setattr(browser_favicons, "_FAVICONS_AVAILABLE", True)
    monkeypatch.setattr(browser_favicons, "_FAVICON_CACHE_TTL_SECONDS", 5)
    moments = iter((10.0, 16.0))
    monkeypatch.setattr(browser_favicons.time, "monotonic", lambda: next(moments))

    calls = {"n": 0}

    def _fake_paths():
        calls["n"] += 1
        return []

    monkeypatch.setattr(browser_favicons, "_chromium_favicon_paths", _fake_paths)
    monkeypatch.setattr(browser_favicons, "_firefox_favicons_paths", lambda: [])

    assert favicon_data_uri_for_domain("example.com") is None
    assert favicon_data_uri_for_domain("example.com") is None
    assert calls["n"] == 2


def test_cache_evicts_least_recently_used_domain(monkeypatch):
    """The cache cannot retain more domain identities than its hard cap."""
    monkeypatch.setattr(browser_favicons, "_FAVICONS_AVAILABLE", True)
    monkeypatch.setattr(browser_favicons, "_FAVICON_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(browser_favicons, "_chromium_favicon_paths", lambda: [])
    monkeypatch.setattr(browser_favicons, "_firefox_favicons_paths", lambda: [])

    for domain in ("one.example", "two.example", "three.example"):
        assert favicon_data_uri_for_domain(domain) is None

    assert list(browser_favicons._FAVICON_CACHE) == [
        "two.example",
        "three.example",
    ]


# ---------------------------------------------------------------------------
# Chromium schema round-trip
# ---------------------------------------------------------------------------

def _build_chromium_db(path: str, page_url: str, png: bytes) -> None:
    """Create a minimal Chromium Favicons DB with one mapped PNG row."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE icon_mapping (id INTEGER PRIMARY KEY, page_url TEXT, icon_id INTEGER)")
        conn.execute(
            "CREATE TABLE favicon_bitmaps (id INTEGER PRIMARY KEY, icon_id INTEGER, image_data BLOB, width INTEGER)"
        )
        conn.execute("INSERT INTO icon_mapping (page_url, icon_id) VALUES (?, ?)", (page_url, 1))
        # Two bitmaps for the same icon; the larger width must win.
        conn.execute(
            "INSERT INTO favicon_bitmaps (icon_id, image_data, width) VALUES (?, ?, ?)",
            (1, b"small-junk", 16),
        )
        conn.execute(
            "INSERT INTO favicon_bitmaps (icon_id, image_data, width) VALUES (?, ?, ?)",
            (1, sqlite3.Binary(png), 32),
        )
        conn.commit()
    finally:
        conn.close()


def test_chromium_query_returns_largest_png(tmp_path):
    """The Chromium query joins mapping->bitmaps and returns the largest PNG as a data-URI."""
    db = tmp_path / "Favicons"
    _build_chromium_db(str(db), "https://github.com/explore", _PNG_1PX)

    uri = browser_favicons._query_chromium(str(db), "github.com")
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")
    # The 32px row (the real PNG) wins over the 16px junk row.
    decoded = base64.b64decode(uri.split(",", 1)[1])
    assert decoded == _PNG_1PX


def test_live_wal_icon_is_visible_without_checkpoint(tmp_path):
    db = tmp_path / "Favicons"
    _build_chromium_db(str(db), "https://other.example/", _PNG_1PX)
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO icon_mapping(page_url, icon_id) VALUES (?, 1)",
                       ("https://fresh.example/",))
        writer.commit()
        assert (tmp_path / "Favicons-wal").exists()
        assert browser_favicons._query_chromium(str(db), "fresh.example") is not None
    finally:
        writer.close()


def test_numbered_chromium_profile_is_searched(tmp_path, monkeypatch):
    profile = tmp_path / "Google" / "Chrome" / "User Data" / "Profile 2"
    profile.mkdir(parents=True)
    _build_chromium_db(str(profile / "Favicons"), "https://work.example/", _PNG_1PX)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(browser_favicons, "_FAVICONS_AVAILABLE", True)
    monkeypatch.setattr(browser_favicons, "_firefox_favicons_paths", lambda: [])
    assert favicon_data_uri_for_domain("work.example") is not None


def test_secondary_firefox_profile_is_searched(tmp_path, monkeypatch):
    root = tmp_path / "Mozilla" / "Firefox"
    profile = root / "Profiles" / "work"
    profile.mkdir(parents=True)
    (root / "profiles.ini").write_text(
        "[Profile1]\nName=work\nIsRelative=1\nPath=Profiles/work\n",
        encoding="utf-8",
    )
    _build_firefox_db(str(profile / "favicons.sqlite"), "https://work.example/", _PNG_1PX)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(browser_favicons, "_FAVICONS_AVAILABLE", True)
    monkeypatch.setattr(browser_favicons, "_chromium_favicon_paths", lambda: [])
    assert favicon_data_uri_for_domain("work.example") is not None


def test_negative_cache_recovers_sooner_than_success_cache(tmp_path, monkeypatch):
    db = tmp_path / "Favicons"
    monkeypatch.setattr(browser_favicons, "_FAVICONS_AVAILABLE", True)
    monkeypatch.setattr(browser_favicons, "_chromium_favicon_paths", lambda: [str(db)])
    monkeypatch.setattr(browser_favicons, "_firefox_favicons_paths", lambda: [])
    now = [100.]
    monkeypatch.setattr(browser_favicons.time, "monotonic", lambda: now[0])
    assert favicon_data_uri_for_domain("fresh.example") is None
    _build_chromium_db(str(db), "https://fresh.example/", _PNG_1PX)
    assert favicon_data_uri_for_domain("fresh.example") is None
    now[0] += browser_favicons._FAVICON_MISS_TTL_SECONDS + 1
    assert favicon_data_uri_for_domain("fresh.example") is not None


def test_chromium_query_domain_miss_returns_none(tmp_path):
    """A domain that isn't in the DB returns None (no spurious match)."""
    db = tmp_path / "Favicons"
    _build_chromium_db(str(db), "https://github.com/explore", _PNG_1PX)
    assert browser_favicons._query_chromium(str(db), "gitlab.com") is None


@pytest.mark.parametrize(
    "page_url",
    [
        "https://notgithub.com/path",
        "https://sub.github.com/path",
        "https://github.com.evil/path",
    ],
)
def test_chromium_query_requires_exact_host_boundary(tmp_path, page_url):
    """Host-only routes must not match sibling, subdomain, or suffix hosts."""
    db = tmp_path / "Favicons"
    _build_chromium_db(str(db), page_url, _PNG_1PX)
    assert browser_favicons._query_chromium(str(db), "github.com") is None


def test_chromium_query_matches_www_variant_and_host_without_path(tmp_path):
    """The normalized host still finds a cached root URL carrying ``www``."""
    db = tmp_path / "Favicons"
    _build_chromium_db(str(db), "https://www.github.com", _PNG_1PX)
    assert browser_favicons._query_chromium(str(db), "github.com") is not None


def test_domain_like_params_escape_sql_wildcards():
    params = browser_favicons._domain_like_params(r"exa_mple%5\\test.com")
    assert params
    assert all(r"\_" in value and r"\%" in value and r"\\" in value for value in params)


def test_favicon_for_domain_uses_chromium_path(tmp_path, monkeypatch):
    """End-to-end: favicon_data_uri_for_domain picks up the Chromium DB hit."""
    db = tmp_path / "Favicons"
    _build_chromium_db(str(db), "https://example.org/", _PNG_1PX)

    monkeypatch.setattr(browser_favicons, "_FAVICONS_AVAILABLE", True)
    monkeypatch.setattr(browser_favicons, "_chromium_favicon_paths", lambda: [str(db)])
    monkeypatch.setattr(browser_favicons, "_firefox_favicons_paths", lambda: [])

    uri = favicon_data_uri_for_domain("example.org")
    assert uri is not None and uri.startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# Firefox schema round-trip
# ---------------------------------------------------------------------------

def _build_firefox_db(path: str, page_url: str, png: bytes) -> None:
    """Create a minimal Firefox favicons.sqlite with one mapped PNG row."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE moz_pages_w_icons (id INTEGER PRIMARY KEY, page_url TEXT)")
        conn.execute("CREATE TABLE moz_icons_to_pages (page_id INTEGER, icon_id INTEGER)")
        conn.execute("CREATE TABLE moz_icons (id INTEGER PRIMARY KEY, data BLOB, width INTEGER)")
        conn.execute("INSERT INTO moz_pages_w_icons (id, page_url) VALUES (?, ?)", (1, page_url))
        conn.execute("INSERT INTO moz_icons (id, data, width) VALUES (?, ?, ?)", (1, sqlite3.Binary(png), 32))
        conn.execute("INSERT INTO moz_icons_to_pages (page_id, icon_id) VALUES (?, ?)", (1, 1))
        conn.commit()
    finally:
        conn.close()


def test_firefox_query_returns_png(tmp_path):
    """The Firefox query joins pages->icons and returns the PNG with a sniffed mime."""
    db = tmp_path / "favicons.sqlite"
    _build_firefox_db(str(db), "https://mozilla.org/about", _PNG_1PX)

    uri = browser_favicons._query_firefox(str(db), "mozilla.org")
    assert uri is not None
    # PNG magic bytes -> image/png mime.
    assert uri.startswith("data:image/png;base64,")
    decoded = base64.b64decode(uri.split(",", 1)[1])
    assert decoded == _PNG_1PX


def test_firefox_falls_back_when_no_chromium(tmp_path, monkeypatch):
    """When Chromium has no hit, the Firefox cache is consulted next."""
    db = tmp_path / "favicons.sqlite"
    _build_firefox_db(str(db), "https://mozilla.org/", _PNG_1PX)

    monkeypatch.setattr(browser_favicons, "_FAVICONS_AVAILABLE", True)
    monkeypatch.setattr(browser_favicons, "_chromium_favicon_paths", lambda: [])
    monkeypatch.setattr(browser_favicons, "_firefox_favicons_paths", lambda: [str(db)])

    uri = favicon_data_uri_for_domain("mozilla.org")
    assert uri is not None and uri.startswith("data:image/png;base64,")


def test_firefox_mime_sniffing():
    """_firefox_mime sniffs png/ico/svg and defaults to png."""
    assert browser_favicons._firefox_mime(32, _PNG_1PX) == "image/png"
    assert browser_favicons._firefox_mime(16, b"\x00\x00\x01\x00rest") == "image/x-icon"
    assert browser_favicons._firefox_mime(16, b"<svg xmlns=") == "image/svg+xml"
    assert browser_favicons._firefox_mime(16, b"<?xml version") == "image/svg+xml"
    assert browser_favicons._firefox_mime(16, b"unknown-bytes") == "image/png"


# ---------------------------------------------------------------------------
# Owned fallback-copy lifecycle
# ---------------------------------------------------------------------------


def _force_fallback_sqlite_open(monkeypatch):
    real_connect = sqlite3.connect

    def _connect(path, *args, **kwargs):
        if kwargs.get("uri"):
            raise sqlite3.OperationalError("force immutable-open fallback")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(browser_favicons.sqlite3, "connect", _connect)


def _owned_temp_paths(tmp_path):
    return list(tmp_path.glob(f"{temp_lifecycle.OWNED_TEMP_PREFIX}*"))


def test_fallback_copy_deleted_after_hit(tmp_path, monkeypatch):
    db = tmp_path / "Favicons"
    _build_chromium_db(str(db), "https://github.com/", _PNG_1PX)
    monkeypatch.setattr(temp_lifecycle, "_temp_root", lambda: tmp_path)
    _force_fallback_sqlite_open(monkeypatch)

    assert browser_favicons._query_chromium(str(db), "github.com") is not None
    assert _owned_temp_paths(tmp_path) == []


def test_fallback_copy_deleted_after_miss(tmp_path, monkeypatch):
    db = tmp_path / "Favicons"
    _build_chromium_db(str(db), "https://github.com/", _PNG_1PX)
    monkeypatch.setattr(temp_lifecycle, "_temp_root", lambda: tmp_path)
    _force_fallback_sqlite_open(monkeypatch)

    assert browser_favicons._query_chromium(str(db), "gitlab.com") is None
    assert _owned_temp_paths(tmp_path) == []


def test_fallback_copy_deleted_after_query_failure(tmp_path, monkeypatch):
    db = tmp_path / "Favicons"
    db.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(temp_lifecycle, "_temp_root", lambda: tmp_path)
    _force_fallback_sqlite_open(monkeypatch)

    assert browser_favicons._query_chromium(str(db), "github.com") is None
    assert _owned_temp_paths(tmp_path) == []


def test_fallback_copy_deleted_after_encoding_failure(tmp_path, monkeypatch):
    db = tmp_path / "Favicons"
    _build_chromium_db(str(db), "https://github.com/", _PNG_1PX)
    monkeypatch.setattr(temp_lifecycle, "_temp_root", lambda: tmp_path)
    monkeypatch.setattr(
        browser_favicons,
        "_to_data_uri",
        lambda *_args: (_ for _ in ()).throw(ValueError("encoding failed")),
    )
    _force_fallback_sqlite_open(monkeypatch)

    assert browser_favicons._query_chromium(str(db), "github.com") is None
    assert _owned_temp_paths(tmp_path) == []


def test_cleanup_closes_connection_before_unlink_even_when_close_raises(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(temp_lifecycle, "_temp_root", lambda: tmp_path)
    owned_path = tmp_path / f"{temp_lifecycle.OWNED_TEMP_PREFIX}favicon-db-test.favicons"
    owned_path.write_bytes(b"copy")
    state = {"close_attempted": False}

    class _Connection:
        def execute(self, *_args, **_kwargs):
            return [(_PNG_1PX, 32)]

        def close(self):
            state["close_attempted"] = True
            raise sqlite3.OperationalError("simulated close failure")

    monkeypatch.setattr(
        browser_favicons,
        "_connect_readonly",
        lambda _path: browser_favicons._ReadonlyConnection(
            _Connection(), owned_path
        ),
    )
    real_unlink = temp_lifecycle._unlink

    def _assert_close_first(path):
        assert state["close_attempted"] is True
        real_unlink(path)

    monkeypatch.setattr(temp_lifecycle, "_unlink", _assert_close_first)

    assert browser_favicons._query_chromium("unused", "github.com") is not None
    assert not owned_path.exists()


def test_stale_sweep_removes_only_owned_old_regular_files(tmp_path, monkeypatch):
    monkeypatch.setattr(temp_lifecycle, "_temp_root", lambda: tmp_path)
    now = time.time()
    stale_owned = tmp_path / f"{temp_lifecycle.OWNED_TEMP_PREFIX}stale.favicons"
    fresh_owned = tmp_path / f"{temp_lifecycle.OWNED_TEMP_PREFIX}fresh.favicons"
    legacy_generic = tmp_path / "legacy-random-name.favicons"
    unrelated = tmp_path / "unrelated.tmp"
    owned_directory = tmp_path / f"{temp_lifecycle.OWNED_TEMP_PREFIX}directory"
    for path in (stale_owned, fresh_owned, legacy_generic, unrelated):
        path.write_bytes(b"x")
    owned_directory.mkdir()
    os.utime(stale_owned, (now - 7200, now - 7200))
    os.utime(legacy_generic, (now - 7200, now - 7200))
    os.utime(unrelated, (now - 7200, now - 7200))

    removed = temp_lifecycle.sweep_stale_owned_temp_files(
        older_than_seconds=3600,
        now=now,
    )

    assert removed == 1
    assert not stale_owned.exists()
    assert fresh_owned.exists()
    assert legacy_generic.exists()
    assert unrelated.exists()
    assert owned_directory.is_dir()
    assert temp_lifecycle.close_then_unlink(legacy_generic) is False
    assert legacy_generic.exists()


def test_site_icon_request_limits_are_validated(tmp_path, monkeypatch):
    """Oversized domain batches/hosts are rejected before any browser DB read."""
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    from starlette.testclient import TestClient
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    client = TestClient(server.fastapi)

    too_many = [f"site-{i}.example" for i in range(129)]
    assert client.post("/api/sites/icons", json={"domains": too_many}).status_code == 422
    assert client.post(
        "/api/sites/icons",
        json={"domains": ["a" * 254]},
    ).status_code == 422


def test_site_icon_endpoint_skips_malformed_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    from starlette.testclient import TestClient
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    client = TestClient(server.fastapi)

    response = client.post(
        "/api/sites/icons",
        json={"domains": ["https://github.com/path", "github.com.evil/%_"]},
    )
    assert response.status_code == 200
    assert response.json() == []
