"""Best-effort extraction of a site's favicon as a small image data-URI.

Exposes:
    favicon_data_uri_for_domain(domain) -> str | None
        Returns a ``data:image/<png|x-icon|svg+xml>;base64,...`` string for the
        site's favicon, or ``None`` when no cached favicon is found or extraction
        fails for any reason.

The Power Mode site-routing rows render a real favicon next to each routed
domain. We source it LOCALLY from the installed browsers' favicon caches (the
user already has the favicon on disk because they visited the site) rather than
hitting the network. This mirrors how ``app_icons.py`` extracts an app icon from
the on-disk exe: privacy-preserving, no network call, and never raising.

Why local cache instead of a network fetch:
- Privacy / local-first: a network favicon fetch would leak the user's routed
  domains to a third party (the site, a favicon proxy) on every picker open.
  Reading the browser's own cache leaks nothing.
- On-brand: same posture as ``app_icons.py`` (extract what's already on disk).

Browsers searched, in order: Chrome, Edge (both Chromium ``Favicons`` SQLite
DBs), then Firefox (``favicons.sqlite``). First hit wins.

Live databases are opened read-only with WAL awareness. When no WAL exists,
a locked file can fall back to an attributable Whisperstream-owned temp copy.
Cursors and connections close before that copy is unlinked on every outcome.

Privacy (locked invariant): never log the domain, never log icon bytes. This
module emits no log line at all. Non-win32 -> always ``None``.
"""
from __future__ import annotations

import base64
import configparser
import os
import shutil
import sqlite3
import sys
import time
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .temp_lifecycle import close_then_unlink, create_owned_temp_file

# domain (lowercased) -> (cached_at, data-URI string or None). A cached None
# avoids repeated browser-DB scans while the Power Mode picker is active. The
# short TTL and hard LRU bound prevent configured site identities accumulating
# for the backend process lifetime.
_FAVICON_CACHE_MAX_ENTRIES = 128
_FAVICON_CACHE_TTL_SECONDS = 5 * 60
_FAVICON_MISS_TTL_SECONDS = 15
_FAVICON_CACHE: OrderedDict[str, tuple[float, Optional[str]]] = OrderedDict()
_FAVICON_CACHE_LOCK = threading.Lock()

_FAVICONS_AVAILABLE = sys.platform == "win32"


def normalize_favicon_domain(domain: object) -> Optional[str]:
    """Normalize and validate one host-only favicon lookup key."""
    if not isinstance(domain, str):
        return None
    host = domain.strip().lower().rstrip(".")
    if not host or "." not in host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return None
    if len(host) > 253:
        return None
    labels = host.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in label)
        for label in labels
    ):
        return None
    return host


def _escape_like(value: str) -> str:
    """Escape SQLite LIKE metacharacters using backslash as ESCAPE."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _domain_like_params(domain: str) -> Tuple[str, ...]:
    """Escaped LIKE patterns with an exact browser-host boundary.

    A host must end at the URL end or be followed by a URL separator/port. This
    deliberately rejects sibling prefixes, subdomains, and suffixes such as
    ``notexample.com``, ``sub.example.com``, and ``example.com.evil``. A leading
    ``www.`` variant is included because routing normalizes that prefix away.
    """
    escaped = _escape_like(domain)
    hosts = (escaped, f"www.{escaped}") if not domain.startswith("www.") else (escaped,)
    patterns: list[str] = []
    for host in hosts:
        patterns.extend(
            (
                f"%://{host}",
                f"%://{host}/%",
                f"%://{host}:%",
                f"%://{host}?%",
                f"%://{host}#%",
            )
        )
    return tuple(patterns)


@dataclass
class _ReadonlyConnection:
    connection: sqlite3.Connection
    owned_temp_path: Optional[Path] = None

    def close(self, *dependent_handles: object) -> bool:
        """Close cursors then SQLite before removing an owned fallback copy."""
        return close_then_unlink(
            self.owned_temp_path,
            (*dependent_handles, self.connection),
        )


def _connect_readonly(path: str) -> Optional[_ReadonlyConnection]:
    """Read current committed data, including live WAL pages, with a short wait.

    An immutable open silently ignores a live WAL. Copy fallback is safe only
    without one; copying a changing DB/WAL pair cannot guarantee consistency.
    The wrapper owns the connection and any temporary copy. Never raises.
    """
    if not path or not os.path.exists(path):
        return None
    connection = None
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.05)
        connection.execute("PRAGMA schema_version").close()
        return _ReadonlyConnection(connection)
    except Exception:
        if connection is not None:
            connection.close()
    if os.path.exists(path + "-wal"):
        return None
    # Fallback: copy then open the copy (the original may be exclusively locked).
    try:
        handle, tmp = create_owned_temp_file(
            purpose="favicon-db",
            suffix=".favicons",
        )
        copied_connection: Optional[sqlite3.Connection] = None
        try:
            handle.close()
            shutil.copyfile(path, tmp)
            copied_connection = sqlite3.connect(tmp)
            return _ReadonlyConnection(copied_connection, tmp)
        except Exception:
            close_then_unlink(tmp, (copied_connection, handle))
            return None
    except Exception:
        return None


def _query_chromium(path: str, domain: str) -> Optional[str]:
    """Largest cached PNG favicon for ``domain`` from a Chromium ``Favicons`` DB.

    Schema: ``icon_mapping(page_url, icon_id)`` joins ``favicon_bitmaps(icon_id,
    image_data, width)`` where ``image_data`` is PNG bytes. We pick the largest
    ``width`` row. Best-effort; any failure -> None.
    """
    owned = _connect_readonly(path)
    if owned is None:
        return None
    cur = None
    try:
        like_params = _domain_like_params(domain)
        where = " OR ".join(
            "im.page_url LIKE ? ESCAPE '\\'" for _ in like_params
        )
        cur = owned.connection.execute(
            f"""
            SELECT fb.image_data, fb.width
            FROM icon_mapping im
            JOIN favicon_bitmaps fb ON fb.icon_id = im.icon_id
            WHERE {where}
            ORDER BY fb.width DESC
            """,
            like_params,
        )
        for image_data, _width in cur:
            if image_data:
                return _to_data_uri(bytes(image_data), "image/png")
        return None
    except Exception:
        return None
    finally:
        owned.close(cur)


def _firefox_mime(width: object, data: bytes) -> str:
    """Best-effort mime for a Firefox ``moz_icons.data`` blob.

    Firefox stores png/ico/svg. Sniff the leading bytes; default to ``image/png``.
    """
    try:
        head = data[:16]
    except Exception:
        return "image/png"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    # SVG is text; sniff for an "<svg" / "<?xml" lead-in.
    lowered = head.lower()
    if lowered.startswith(b"<svg") or lowered.startswith(b"<?xml"):
        return "image/svg+xml"
    return "image/png"


def _query_firefox(path: str, domain: str) -> Optional[str]:
    """Largest cached favicon for ``domain`` from a Firefox ``favicons.sqlite``.

    Schema: ``moz_pages_w_icons(page_url, id)`` -> ``moz_icons_to_pages(page_id,
    icon_id)`` -> ``moz_icons(id, data, width)``. We pick the largest ``width``
    row and pass the blob through with a sniffed mime. Best-effort; any failure
    -> None.
    """
    owned = _connect_readonly(path)
    if owned is None:
        return None
    cur = None
    try:
        like_params = _domain_like_params(domain)
        where = " OR ".join(
            "mp.page_url LIKE ? ESCAPE '\\'" for _ in like_params
        )
        cur = owned.connection.execute(
            f"""
            SELECT mi.data, mi.width
            FROM moz_pages_w_icons mp
            JOIN moz_icons_to_pages itp ON itp.page_id = mp.id
            JOIN moz_icons mi ON mi.id = itp.icon_id
            WHERE {where}
            ORDER BY mi.width DESC
            """,
            like_params,
        )
        for data, width in cur:
            if data:
                blob = bytes(data)
                return _to_data_uri(blob, _firefox_mime(width, blob))
        return None
    except Exception:
        return None
    finally:
        owned.close(cur)


def _to_data_uri(blob: bytes, mime: str) -> Optional[str]:
    """Base64-encode ``blob`` as a ``data:<mime>;base64,...`` URI, or None."""
    if not blob:
        return None
    try:
        return "data:" + mime + ";base64," + base64.b64encode(blob).decode("ascii")
    except Exception:
        return None


def _chromium_favicon_paths() -> list[str]:
    """Default and numbered profile databases for Chrome, then Edge."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return []
    paths = []
    for vendor, browser in (("Google", "Chrome"), ("Microsoft", "Edge")):
        root = Path(base) / vendor / browser / "User Data"
        paths.append(str(root / "Default" / "Favicons"))
        try:
            paths.extend(str(p / "Favicons") for p in sorted(root.glob("Profile *")) if p.is_dir())
        except OSError:
            pass
    return paths


def _firefox_favicons_paths() -> list[str]:
    """Default first, followed by other registered Firefox profiles."""
    primary = _firefox_favicons_path()
    paths = [primary] if primary else []
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return paths
    root = Path(appdata) / "Mozilla" / "Firefox"
    try:
        profiles = configparser.ConfigParser(interpolation=None)
        profiles.read(root / "profiles.ini", encoding="utf-8")
        for section in profiles.sections():
            if not section.startswith("Profile"):
                continue
            value = profiles.get(section, "Path", fallback="")
            if not value:
                continue
            profile = Path(value)
            if profiles.get(section, "IsRelative", fallback="1") == "1":
                profile = root / profile
            path = str(profile / "favicons.sqlite")
            if path not in paths and os.path.isfile(path):
                paths.append(path)
    except (OSError, configparser.Error):
        pass
    return paths


def _firefox_favicons_path() -> Optional[str]:
    """``favicons.sqlite`` for the default Firefox profile, or None.

    Finds the profile dir via ``profiles.ini`` (``Default=`` line) and falls back
    to a ``*.default-release`` / ``*.default`` glob. Best-effort; never raises.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    ff_root = os.path.join(appdata, "Mozilla", "Firefox")
    profiles_root = os.path.join(ff_root, "Profiles")

    # Preferred: read the Default= relative path out of profiles.ini.
    try:
        ini = os.path.join(ff_root, "profiles.ini")
        if os.path.exists(ini):
            rel = None
            with open(ini, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    # The Default= key under an [Install...] section points at the
                    # relative profile path (e.g. "Profiles/abc123.default-release").
                    # A bare "Default=1" line under [General] is not a path; skip it
                    # by requiring a path separator in the value.
                    if line.lower().startswith("default=") and (
                        "/" in line or "\\" in line
                    ):
                        rel = line.split("=", 1)[1].strip()
                        break
            if rel:
                cand = os.path.join(ff_root, rel.replace("/", os.sep))
                fav = os.path.join(cand, "favicons.sqlite")
                if os.path.exists(fav):
                    return fav
    except Exception:
        pass

    # Fallback: glob the Profiles dir for a default profile folder.
    try:
        if os.path.isdir(profiles_root):
            entries = sorted(os.listdir(profiles_root))
            for suffix in (".default-release", ".default"):
                for name in entries:
                    if name.lower().endswith(suffix):
                        fav = os.path.join(profiles_root, name, "favicons.sqlite")
                        if os.path.exists(fav):
                            return fav
    except Exception:
        pass
    return None


def favicon_data_uri_for_domain(domain: Optional[str]) -> Optional[str]:
    """Return a favicon data-URI for ``domain`` from a local browser cache, or None.

    Best-effort and NEVER raises. NO network call: the favicon is read only from
    the installed browsers' on-disk caches. Results are cached by domain
    (including a cached None for domains with no usable favicon) so repeated
    picker opens don't re-scan the DBs. Returns None on non-Windows, a falsy /
    malformed domain, no cached favicon, or any read failure.

    Privacy: never logs the domain or the bytes.
    """
    if not _FAVICONS_AVAILABLE or not domain:
        return None

    key = normalize_favicon_domain(domain)
    if key is None:
        return None
    now = time.monotonic()
    with _FAVICON_CACHE_LOCK:
        cached = _FAVICON_CACHE.get(key)
        if cached is not None:
            cached_at, cached_result = cached
            ttl = _FAVICON_CACHE_TTL_SECONDS
            if cached_result is None:
                ttl = min(ttl, _FAVICON_MISS_TTL_SECONDS)
            if now - cached_at <= ttl:
                _FAVICON_CACHE.move_to_end(key)
                return cached_result
            del _FAVICON_CACHE[key]

    result: Optional[str] = None
    try:
        for path in _chromium_favicon_paths():
            hit = _query_chromium(path, key)
            if hit:
                result = hit
                break
        if result is None:
            for ff in _firefox_favicons_paths():
                result = _query_firefox(ff, key)
                if result:
                    break
    except Exception:
        result = None

    with _FAVICON_CACHE_LOCK:
        _FAVICON_CACHE[key] = (now, result)
        _FAVICON_CACHE.move_to_end(key)
        while len(_FAVICON_CACHE) > _FAVICON_CACHE_MAX_ENTRIES:
            _FAVICON_CACHE.popitem(last=False)
    return result
