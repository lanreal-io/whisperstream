"""Resolve installed apps: system friendly names + install detection.

Install-aware app routing for the Power Mode profile editor. A mapped app rule
should only render when the app is actually installed (issue 1), its label should
read like a real app name pulled from the exe's ``FileDescription`` (issue 2), and
an installed-but-closed app should still get a real icon (issue 4 — the Discord
case). The user's ``power_mode_map`` routing is never modified here; this gating is
purely cosmetic, so a detection miss never breaks routing and an app installed
later still auto-appears.

Mirrors the guarded-import / never-raise style of ``system_info.py`` and
``browser_domain.py``: a missing lib or non-Windows degrades the whole module to
"best effort returns nothing" and every public function swallows its own errors.

Privacy (locked invariant): never log exe paths, process/app names, or icon bytes.
This module emits no log line that contains any of those.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Dict, List, Optional

from .app_icons import icon_data_uri_for_exe

# ---------------------------------------------------------------------------
# Top-level guarded imports (Nuitka-friendly static signal). Mirror
# system_info.py / browser_domain.py: import at module scope so Nuitka includes
# the extensions in the frozen binary, and degrade to an availability flag
# (never raise at import) on non-win32 / missing-lib environments.
# ---------------------------------------------------------------------------
try:
    if sys.platform != "win32":  # pragma: no cover - non-win32 short-circuit
        raise ImportError("win32-only")
    import win32api
    import winreg

    _WIN_OK = True
except Exception:  # noqa: BLE001 - any import failure degrades cleanly
    _WIN_OK = False

# One explicit user-requested label override; everything else comes from the
# system. Keep this list tiny — it exists for the windowsterminal.exe ->
# "Windows PowerShell" case, not as a hardcoded name map.
_NAME_OVERRIDES: Dict[str, str] = {"windowsterminal.exe": "Windows PowerShell"}

# Store/MSIX exe -> the alias stub(s) that exist in
# %LOCALAPPDATA%\Microsoft\WindowsApps. A Store-installed app exposes a thin
# alias launcher there even though its real package lives under WindowsApps with
# a versioned path we can't easily resolve; the alias existing is our install
# signal.
_STORE_ALIASES: Dict[str, tuple] = {"windowsterminal.exe": ("wt.exe", "windowsterminal.exe")}

_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

# Uninstall registry base path. Read it under both bitness views of HKLM (the
# 32-bit view surfaces WOW6432Node) plus HKCU for per-user installs. Each
# product's subkey may carry a ``DisplayIcon`` pointing at its main exe — our
# broadest install signal for apps with no Start Menu shortcut or App Paths entry.
_UNINSTALL_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

# Installer / updater stub exes that an Uninstall ``DisplayIcon`` sometimes points
# at (Inno Setup ``unins000.exe``, Squirrel ``Update.exe``, MSI ``setup.exe``).
# They are not the routable app process, so never index them as an app. Names
# starting with ``unins`` are also skipped (Inno's numbered uninstallers).
_STUB_EXES = frozenset({
    "uninstall.exe", "uninst.exe", "setup.exe", "update.exe", "updater.exe",
    "install.exe", "installer.exe",
})

# Runtime / redistributable / SDK DisplayName markers. These Uninstall entries
# are developer/runtime components a user never dictates into (and Visual C++ /
# .NET ship many near-duplicate versioned rows), so they are filtered out of the
# picker. Matched as case-insensitive substrings of the DisplayName. Deliberately
# specific (".net runtime", not bare "runtime") so real apps like "Node.js
# JavaScript Runtime" are kept.
_RUNTIME_NAME_MARKERS = (
    "redistributable",
    "visual c++",
    "directx",
    "webview2",
    "desktop runtime",   # Microsoft Windows Desktop Runtime
    ".net runtime",
    ".net sdk",
    ".net core",
    "windows sdk",
    "windows software development kit",
)

# Installed-app paths and labels can identify a user's software inventory.
# Cache enumeration results for only five minutes and at most 512 apps: enough
# to keep one picker session responsive without retaining a process-lifetime
# catalog. A refresh replaces the whole snapshot, evicting removed apps.
_DIRECTORY_CACHE_TTL_SECONDS = 5 * 60.0
_DIRECTORY_CACHE_MAX_ENTRIES = 512

# Module-cached Start Menu index: {exe_basename_lower: target_path}. None until
# built; refreshed after the short directory-cache lifetime.
_START_MENU_INDEX: Optional[Dict[str, str]] = None
_START_MENU_INDEX_EXPIRES_AT = 0.0

# Module-cached unified installed-app index: {exe_basename_lower: exe_path},
# merged across Start Menu .lnk targets + enumerated App Paths + Uninstall
# DisplayIcon. Powers both the installed-apps picker and install detection.
_INSTALLED_INDEX: Optional[Dict[str, str]] = None
_INSTALLED_INDEX_EXPIRES_AT = 0.0

# Module-cached, fully-resolved installed-apps list (name + icon per entry).
# Built from _INSTALLED_INDEX so repeat calls during one short cache window do
# not re-rasterize icons or re-read version resources.
_INSTALLED_APPS: Optional[List[Dict[str, object]]] = None
_INSTALLED_APPS_EXPIRES_AT = 0.0


def _bounded_index(index: Dict[str, str]) -> Dict[str, str]:
    """Return an insertion-ordered snapshot within the directory cache cap."""
    limit = max(0, _DIRECTORY_CACHE_MAX_ENTRIES)
    return dict(list(index.items())[:limit])


def _file_description(path: str) -> Optional[str]:
    """Return the exe's ``FileDescription`` version-resource string, or None.

    Best-effort and never raises. ``devenv.exe`` -> "Microsoft Visual Studio",
    ``Code.exe`` -> "Visual Studio Code", etc. The system supplies the name; we
    keep no hardcoded process-name -> label table.
    """
    if not _WIN_OK or not path:
        return None
    try:
        tr = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")
        if not tr:
            return None
        lang, cp = tr[0]
        key = "\\StringFileInfo\\%04X%04X\\FileDescription" % (lang, cp)
        desc = win32api.GetFileVersionInfo(path, key)
        desc = (desc or "").strip()
        return desc or None
    except Exception:  # noqa: BLE001 - missing resource / malformed exe degrades to None
        return None


def friendly_app_name(process_name: str, exe_path: Optional[str] = None) -> str:
    """Human-friendly display name for a process.

    Resolution order: explicit override -> exe ``FileDescription`` ->
    strip-``.exe`` + title-case fallback. Never raises.
    """
    pn = (process_name or "").strip().lower()
    if pn in _NAME_OVERRIDES:
        return _NAME_OVERRIDES[pn]
    desc = _file_description(exe_path) if exe_path else None
    if desc:
        return desc
    stem = process_name or ""
    if stem.lower().endswith(".exe"):
        stem = stem[:-4]
    return stem.title() if stem else (process_name or "")


def _resolve_via_app_paths(pn: str) -> Optional[str]:
    """App Paths registry lookup across both hives and both bitness views."""
    if not _WIN_OK:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(
                    hive, _APP_PATHS + "\\" + pn, 0, winreg.KEY_READ | view
                ) as k:
                    val = winreg.QueryValueEx(k, None)[0]
            except OSError:
                continue
            except Exception:  # noqa: BLE001 - never raise out of the resolver
                continue
            if not val:
                continue
            path = str(val).strip().strip('"')
            if path and os.path.exists(path):
                return path
    return None


def _resolve_via_store_alias(pn: str) -> Optional[str]:
    """Store/MSIX alias-stub lookup in %LOCALAPPDATA%\\Microsoft\\WindowsApps."""
    try:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            return None
        wa = base + "\\Microsoft\\WindowsApps"
        for cand in _STORE_ALIASES.get(pn, (pn,)):
            candidate = os.path.join(wa, cand)
            if os.path.exists(candidate):
                return candidate
    except Exception:  # noqa: BLE001
        return None
    return None


def _build_start_menu_index_worker(index: Dict[str, str]) -> None:
    """Walk the Start Menu trees and fill ``index`` in place. Runs ONLY on the
    dedicated worker thread spawned by ``_build_start_menu_index`` so it owns a
    clean STA apartment (``WScript.Shell`` is an STA object — calling it from a
    thread the rest of the app has already put into MTA, e.g. via pycaw/comtypes,
    can hard-fault the process). Fully guarded; any failure yields a partial map.
    """
    if not _WIN_OK:
        return

    try:
        import pythoncom  # type: ignore
        import win32com.client as win32com_client  # type: ignore
    except Exception:  # noqa: BLE001 - COM libs unavailable -> empty index
        return

    co_initialized = False
    try:
        try:
            # Explicit STA on this fresh thread (no prior apartment commitment).
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            co_initialized = True
        except Exception:  # noqa: BLE001 - already initialized is fine; don't uninit it
            co_initialized = False

        try:
            shell = win32com_client.Dispatch("WScript.Shell")
        except Exception:  # noqa: BLE001
            return

        roots: List[str] = []
        program_data = os.environ.get("ProgramData")
        appdata = os.environ.get("APPDATA")
        if program_data:
            roots.append(
                os.path.join(program_data, "Microsoft", "Windows", "Start Menu", "Programs")
            )
        if appdata:
            roots.append(
                os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")
            )

        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                for fname in files:
                    if not fname.lower().endswith(".lnk"):
                        continue
                    lnk_path = os.path.join(dirpath, fname)
                    sc = None
                    try:
                        sc = shell.CreateShortcut(lnk_path)
                        target = sc.TargetPath
                    except Exception:  # noqa: BLE001 - skip a bad shortcut
                        continue
                    finally:
                        # Release the per-shortcut COM object NOW, on this owning
                        # STA thread, so its IUnknown::Release never runs later on
                        # a foreign apartment (that path faults the process).
                        sc = None
                    if not target:
                        continue
                    base = os.path.basename(target)
                    if not base.lower().endswith(".exe"):
                        continue
                    if not os.path.exists(target):
                        continue
                    key = base.lower()
                    # First win stays (any working target for the basename is
                    # acceptable for an install signal).
                    if key not in index:
                        index[key] = target
    except Exception:  # noqa: BLE001 - any walk/COM failure yields a partial index
        pass
    finally:
        # Drop the shell ref and force GC so every COM Release runs on THIS
        # apartment thread before CoUninitialize. Deferred releases on another
        # thread are what produce the "releasing IUnknown" fault / access
        # violation seen when comtypes (pycaw/UIA) shares the process.
        shell = None
        try:
            import gc

            gc.collect()
        except Exception:  # noqa: BLE001
            pass
        if co_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


def _build_start_menu_index() -> Dict[str, str]:
    """Build {exe_basename_lower: target_path} from Start Menu .lnk targets.

    Delegates the COM-heavy walk to a dedicated daemon thread with its own STA
    apartment so the calling thread's apartment state (the app initializes COM
    elsewhere via pycaw/comtypes, and ``WScript.Shell`` is STA-only) can never
    hard-fault the resolver. Never raises; a hung/failed walk yields whatever was
    collected before a generous timeout.
    """
    index: Dict[str, str] = {}
    if not _WIN_OK:
        return index

    import threading

    try:
        worker = threading.Thread(
            target=_build_start_menu_index_worker,
            args=(index,),
            name="start-menu-index",
            daemon=True,
        )
        worker.start()
        # Bounded join: a slow/huge Start Menu must not stall the resolver
        # indefinitely. A partial index is acceptable (best-effort gating).
        worker.join(timeout=8.0)
    except Exception:  # noqa: BLE001 - thread machinery failure -> empty index
        return index

    # Copy into a bounded snapshot. If the best-effort worker outlives the join
    # timeout it may continue mutating its private dict, not the retained cache.
    return _bounded_index(index)


def _get_start_menu_index() -> Dict[str, str]:
    """Return the short-lived, count-bounded Start Menu index."""
    global _START_MENU_INDEX, _START_MENU_INDEX_EXPIRES_AT
    now = time.monotonic()
    if _START_MENU_INDEX is None or now >= _START_MENU_INDEX_EXPIRES_AT:
        _START_MENU_INDEX = _bounded_index(_build_start_menu_index())
        _START_MENU_INDEX_EXPIRES_AT = (
            time.monotonic() + _DIRECTORY_CACHE_TTL_SECONDS
        )
    return _START_MENU_INDEX


def _resolve_via_start_menu(pn: str) -> Optional[str]:
    """Start Menu .lnk lookup, building (and caching) the index on first use."""
    try:
        return _get_start_menu_index().get(pn)
    except Exception:  # noqa: BLE001
        return None


def _enumerate_app_paths() -> Dict[str, str]:
    """Enumerate every App Paths subkey -> {exe_basename_lower: exe_path}.

    Walks the App Paths key across both hives and bitness views, reading each
    subkey's default value (the full exe path). Mirrors the per-name resolver but
    lists ALL entries for the installed-apps picker. Never raises; a failure on
    any hive/view degrades to whatever was collected.
    """
    out: Dict[str, str] = {}
    if not _WIN_OK:
        return out
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                root = winreg.OpenKey(hive, _APP_PATHS, 0, winreg.KEY_READ | view)
            except Exception:  # noqa: BLE001 - missing key / access denied
                continue
            try:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(root, i)
                    except OSError:
                        break  # no more subkeys
                    i += 1
                    base = sub.strip().lower()
                    if not base.endswith(".exe") or base in out:
                        continue
                    try:
                        with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | view) as k:
                            val = winreg.QueryValueEx(k, None)[0]
                    except Exception:  # noqa: BLE001
                        continue
                    path = str(val or "").strip().strip('"')
                    if path and os.path.exists(path):
                        out[base] = path
            finally:
                try:
                    winreg.CloseKey(root)
                except Exception:  # noqa: BLE001
                    pass
    return out


def _read_reg_value(key, name: str):
    """Read a registry value by name, or None if absent / unreadable. Never raises."""
    try:
        return winreg.QueryValueEx(key, name)[0]
    except Exception:  # noqa: BLE001 - value missing / wrong type / access
        return None


def _is_listable_uninstall_entry(key) -> bool:
    """Replicate Settings' "Apps & features" filtering: a product is user-facing
    only when it has a ``DisplayName``, is not flagged ``SystemComponent``, is not
    a child/patch of another product (``ParentKeyName``/``ParentDisplayName``),
    and is not an OS update (``ReleaseType``). Best-effort; a read miss is
    non-fatal."""
    name = _read_reg_value(key, "DisplayName")
    if not name or not str(name).strip():
        return False
    low = str(name).lower()
    if any(marker in low for marker in _RUNTIME_NAME_MARKERS):
        return False  # runtime / redistributable / SDK component, not an app
    sysc = _read_reg_value(key, "SystemComponent")
    try:
        if sysc is not None and int(sysc) == 1:
            return False
    except (TypeError, ValueError):
        pass
    if _read_reg_value(key, "ParentKeyName") or _read_reg_value(key, "ParentDisplayName"):
        return False
    rel = str(_read_reg_value(key, "ReleaseType") or "").strip().lower()
    if rel in ("security update", "update", "hotfix"):
        return False
    return True


def _is_stub_exe(base: str) -> bool:
    """True for installer / updater / uninstaller stub exes that are never the
    routable app process. ANY source can surface these (a Start Menu "Uninstall
    X" shortcut, an App Paths setup entry, an Uninstall DisplayIcon), so the
    unified index filters on this centrally. ``unins`` covers Inno Setup's
    ``unins000.exe`` / ``uninstall.exe`` / ``uninstall<app>.exe`` family."""
    return bool(base) and (base in _STUB_EXES or base.startswith("unins"))


def _exe_basename_if_app(display_icon) -> Optional[tuple]:
    """From a ``DisplayIcon`` value, return ``(basename_lower, exe_path)`` when it
    points at a real, existing, non-stub ``.exe``; else None. DisplayIcon is
    ``"<path>[,<index>]"`` and is frequently a ``.ico``/``.dll`` or an
    installer/updater stub, all of which we reject (we can only route a real
    process exe)."""
    path = str(display_icon or "").split(",")[0].strip().strip('"')
    if not path.lower().endswith(".exe") or not os.path.exists(path):
        return None
    base = os.path.basename(path).lower()
    if not base or _is_stub_exe(base):
        return None
    return base, path


def _enumerate_uninstall_exes() -> Dict[str, str]:
    """Enumerate the Uninstall registry -> {exe_basename_lower: exe_path}.

    Walks HKLM (64- and 32-bit views) + HKCU, applies the Settings listability
    filters, then maps each product to its main exe via ``DisplayIcon`` (skipping
    installer/updater stubs). The broadest install source: covers apps with no
    Start Menu shortcut or App Paths entry. Best-effort; never raises.
    """
    out: Dict[str, str] = {}
    if not _WIN_OK:
        return out
    targets = (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, 0),
    )
    for hive, view in targets:
        try:
            root = winreg.OpenKey(hive, _UNINSTALL_PATH, 0, winreg.KEY_READ | view)
        except Exception:  # noqa: BLE001 - missing key / access denied
            continue
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break  # no more subkeys
                i += 1
                try:
                    sk = winreg.OpenKey(root, sub, 0, winreg.KEY_READ | view)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    if not _is_listable_uninstall_entry(sk):
                        continue
                    hit = _exe_basename_if_app(_read_reg_value(sk, "DisplayIcon"))
                    if hit and hit[0] not in out:
                        out[hit[0]] = hit[1]
                finally:
                    try:
                        winreg.CloseKey(sk)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            try:
                winreg.CloseKey(root)
            except Exception:  # noqa: BLE001
                pass
    return out


def _build_installed_index() -> Dict[str, str]:
    """Unified {exe_basename_lower: exe_path} across Start Menu .lnk targets,
    enumerated App Paths, and Uninstall DisplayIcon exes. First source to claim a
    basename wins (Start Menu names are the most user-facing). Never raises; a
    failure in any source contributes nothing rather than aborting the build.
    """
    index: Dict[str, str] = {}
    sources = (_get_start_menu_index, _enumerate_app_paths, _enumerate_uninstall_exes)
    for source in sources:
        try:
            for base, path in source().items():
                if base and path and base not in index and not _is_stub_exe(base):
                    index[base] = path
        except Exception:  # noqa: BLE001 - one bad source never sinks the index
            continue
    return index


def installed_index() -> Dict[str, str]:
    """Return the short-lived, count-bounded unified installed-app index."""
    global _INSTALLED_INDEX, _INSTALLED_INDEX_EXPIRES_AT
    global _INSTALLED_APPS, _INSTALLED_APPS_EXPIRES_AT
    now = time.monotonic()
    if _INSTALLED_INDEX is None or now >= _INSTALLED_INDEX_EXPIRES_AT:
        try:
            _INSTALLED_INDEX = _bounded_index(_build_installed_index())
        except Exception:  # noqa: BLE001
            _INSTALLED_INDEX = {}
        _INSTALLED_INDEX_EXPIRES_AT = (
            time.monotonic() + _DIRECTORY_CACHE_TTL_SECONDS
        )
        # A refreshed index supersedes every fully-resolved list built from the
        # old snapshot, including apps removed since its last enumeration.
        _INSTALLED_APPS = None
        _INSTALLED_APPS_EXPIRES_AT = 0.0
    return _INSTALLED_INDEX


def resolve_installed_exe(process_name: str) -> Optional[str]:
    """Best-effort path to an installed exe for ``process_name``, or None.

    Tries, in order: App Paths registry -> Store/MSIX alias stub -> Start Menu
    .lnk target index -> the unified installed index (which also covers
    Uninstall-only apps). Returns the first hit. NEVER raises — every stage
    swallows its own errors and a total failure collapses to None.
    """
    pn = (process_name or "").strip().lower()
    if not pn:
        return None
    try:
        hit = _resolve_via_app_paths(pn)
        if hit:
            return hit
        hit = _resolve_via_store_alias(pn)
        if hit:
            return hit
        hit = _resolve_via_start_menu(pn)
        if hit:
            return hit
        # Broadest fallback: the unified index (Uninstall-registry apps with no
        # Start Menu shortcut or App Paths entry land here, so an installed-but-
        # closed app of that kind still resolves and renders).
        hit = installed_index().get(pn)
        if hit:
            return hit
    except Exception:  # noqa: BLE001 - belt-and-braces; never raise to the caller
        return None
    return None


def resolve_apps(
    process_names: List[str],
    running_apps: List[Dict[str, Optional[str]]],
    known_paths: Optional[Dict[str, str]] = None,
    default_names: Optional[set] = None,
) -> List[Dict[str, object]]:
    """Resolve install state + display name + icon for each requested process.

    ``running_apps`` is the ``list_running_apps()`` output. ``known_paths`` maps a
    process basename to the full exe path recorded when the user added the rule
    (``config.power_mode_paths``); ``default_names`` is the set of bundled-default
    process names. For each requested name (lowercased, stripped, de-duplicated):

    1. running now -> ``installed: True``, the live display_name + icon.
    2. a recorded path that still exists, OR a registry/Start-Menu resolution ->
       ``installed: True`` with the on-disk friendly name + icon. The recorded
       path is the most reliable signal: it covers apps no registry source lists
       (Steam games, portable exes). A stale recorded path (app moved, e.g. a
       per-version Squirrel exe) falls through to re-resolution before giving up.
    3. otherwise:
       - a recorded path that has VANISHED -> ``installed: False`` (the app the
         user added was genuinely uninstalled);
       - a bundled DEFAULT the user never added -> ``installed: False`` (a default
         the user may not have installed);
       - a USER-ADDED rule with no recorded path -> ``installed: True`` on trust
         (a pre-existing rule from before paths were recorded; hiding a working
         route is worse than showing a stale one). No icon (we have no path).

    Never raises. The user's ``power_mode_map`` is never mutated here.
    """
    paths = {(k or "").strip().lower(): (v or "") for k, v in (known_paths or {}).items()}
    defaults = {(n or "").strip().lower() for n in (default_names or set())}

    by_running: Dict[str, Dict[str, Optional[str]]] = {}
    try:
        for entry in running_apps or []:
            pn = (entry.get("process_name") or "").strip().lower()
            if pn and pn not in by_running:
                by_running[pn] = entry
    except Exception:  # noqa: BLE001 - malformed running list degrades to empty index
        by_running = {}

    out: List[Dict[str, object]] = []
    seen: set = set()
    for raw in process_names or []:
        name = (raw or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)

        running = by_running.get(name)
        if running is not None:
            out.append(
                {
                    "process_name": name,
                    "installed": True,
                    "display_name": running.get("display_name") or name,
                    "icon": running.get("icon"),
                }
            )
            continue

        # Recorded path first (covers apps no registry source lists), then
        # registry/Start-Menu resolution. A recorded path that no longer exists
        # is NOT trusted as installed; we re-resolve before concluding.
        recorded = str(paths.get(name) or "").strip().strip('"')
        path = recorded if (recorded and os.path.exists(recorded)) else resolve_installed_exe(name)
        if path:
            try:
                icon = icon_data_uri_for_exe(path)
            except Exception:  # noqa: BLE001 - icon extraction is best-effort
                icon = None
            out.append(
                {
                    "process_name": name,
                    "installed": True,
                    "display_name": friendly_app_name(name, path),
                    "icon": icon,
                }
            )
            continue

        # Unresolvable and not running. A recorded-but-now-missing path means the
        # app was uninstalled (hide). Otherwise hide bundled defaults but keep a
        # user-added rule we simply can't auto-detect.
        if recorded:
            installed = False
        else:
            installed = name not in defaults
        out.append(
            {
                "process_name": name,
                "installed": installed,
                "display_name": friendly_app_name(name, None),
                "icon": None,
            }
        )

    return out


def list_installed_apps() -> List[Dict[str, object]]:
    """Installed apps for the picker: a sorted, de-duplicated list of
    ``{process_name, display_name, icon, installed: True}``.

    Sourced from the unified installed index (Start Menu + App Paths + Uninstall),
    with a system friendly name and an on-disk icon per entry. Cached for at
    most five minutes and 512 entries so a picker session stays responsive but
    removed apps and full path/icon records do not live until process exit.
    Never raises; degrades to ``[]`` on non-Windows or any failure. The user's
    ``power_mode_map`` is never read or mutated here.
    """
    global _INSTALLED_APPS, _INSTALLED_APPS_EXPIRES_AT
    now = time.monotonic()
    if _INSTALLED_APPS is not None and now < _INSTALLED_APPS_EXPIRES_AT:
        return _INSTALLED_APPS

    out: List[Dict[str, object]] = []
    try:
        for base, path in installed_index().items():
            try:
                icon = icon_data_uri_for_exe(path)
            except Exception:  # noqa: BLE001 - icon extraction is best-effort
                icon = None
            out.append(
                {
                    "process_name": base,
                    "display_name": friendly_app_name(base, path),
                    "icon": icon,
                    "installed": True,
                    "path": path,
                }
            )
        out.sort(
            key=lambda a: (
                str(a.get("display_name") or "").lower(),
                str(a.get("process_name") or ""),
            )
        )
        out = out[: max(0, _DIRECTORY_CACHE_MAX_ENTRIES)]
    except Exception:  # noqa: BLE001 - never raise to the endpoint
        out = []

    _INSTALLED_APPS = out
    _INSTALLED_APPS_EXPIRES_AT = time.monotonic() + _DIRECTORY_CACHE_TTL_SECONDS
    return out


def resolve_app_by_path(path: str) -> Optional[Dict[str, object]]:
    """Resolve a user-browsed executable path to a picker app record.

    Returns ``{process_name, display_name, icon, installed: True}`` for an
    existing ``.exe`` (process_name is the lowercased basename, the routing key),
    or ``None`` for a missing / non-exe / invalid path. Never raises. Used by the
    "Browse..." affordance so a user can route an app we did not auto-detect.
    """
    try:
        p = (path or "").strip().strip('"')
        if not p or not p.lower().endswith(".exe") or not os.path.exists(p):
            return None
        base = os.path.basename(p).lower()
        if not base:
            return None
        try:
            icon = icon_data_uri_for_exe(p)
        except Exception:  # noqa: BLE001
            icon = None
        return {
            "process_name": base,
            "display_name": friendly_app_name(base, p),
            "icon": icon,
            "installed": True,
            "path": p,
        }
    except Exception:  # noqa: BLE001
        return None
