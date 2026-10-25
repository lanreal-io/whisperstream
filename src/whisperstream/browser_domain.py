"""Read the active browser tab's registrable domain via Windows UI Automation.

Per-site routing layers domain-aware profile selection on top of per-app routing.
At recording-start we read the focused browser's address bar through UIA (no
browser extension, no focus steal) and reduce it to a registrable-ish domain that
the resolver matches against the user's `site_map`.

Exports:
    extract_registrable_domain(raw) -- pure helper, the omnibox guard.
    read_active_browser_domain(hwnd, process_name) -- best-effort live UIA read.

Privacy (locked invariant): the captured URL/domain is IN MEMORY ONLY. It is
NEVER logged, broadcast over the WebSocket, or written to disk. The raw address
bar value stays inside the UIA helper and only its normalized host is returned.
This module emits no log line that contains a URL or domain.

----------------------------------------------------------------------------
UIA dependency decision (load-bearing)
----------------------------------------------------------------------------
We call **raw `comtypes` UI Automation** (`CUIAutomation` / `IUIAutomation`),
NOT the high-level `uiautomation` wrapper the spike used, and NOT pywin32's
`UIAutomationClient`.

Why raw comtypes:
  * `comtypes` is ALREADY a dependency (transitive via `pycaw`, which ships in the
    frozen installer and already drives COM successfully for system-volume control).
    So this adds ZERO new packages -- the same win the task wanted from pywin32 (c).
  * pywin32's UIA path goes through `win32com.client` gencache, which has the SAME
    runtime-typelib-generation concern as comtypes, with no offsetting benefit. comtypes
    is the COM stack this app has already proven survives the Nuitka onefile freeze
    (via pycaw), so it is the lowest-risk choice.
  * The spike's `uiautomation` wrapper would add a new package AND still pull
    `comtypes`. Dropping the wrapper and calling `IUIAutomation` directly keeps the
    proven mechanism with no extra dependency.

The Nuitka-onefile freeze risk: `comtypes` generates its typelib wrappers
(`comtypes.gen.UIAutomationClient`) at runtime. We trigger that generation at
module import via `comtypes.client.GetModule("UIAutomationCore.dll")` (top-level,
guarded -- a static signal for Nuitka, mirroring foreground_window.py's win32
imports). **Task 3.1 (frozen Nuitka build) is the gate that proves the generated
wrappers survive the freeze.** If the freeze breaks runtime codegen, the fallbacks
in the task's decision list (pre-generated `comtypes.gen`, then pywin32) apply.
"""

from __future__ import annotations

import sys
from urllib.parse import urlparse

# Top-level guarded imports (Nuitka-friendly static signal). Mirrors
# foreground_window.py: import at module scope so Nuitka includes the
# extensions / generated wrappers in the frozen binary, and degrade to an
# availability flag (never raise at import) so non-win32 / missing-COM
# environments stay importable.
try:
    if sys.platform != "win32":  # pragma: no cover - non-win32 short-circuit
        raise ImportError("UIA is win32-only")
    import comtypes  # noqa: F401  (kept for the static include signal)
    import comtypes.client as _cc

    # Frozen-build safety (Task 3.1 gate). comtypes writes its generated typelib
    # wrappers into `comtypes/gen/` by default; in a Nuitka onefile that package
    # lives inside the read-only extracted runtime dir, so the write fails and the
    # `from comtypes.gen import ...` below raises -- UIA then silently goes
    # unavailable and per-site routing never fires. `gen_dir = None` forces
    # comtypes to build the wrapper module IN MEMORY (no disk write), which
    # survives the freeze. Harmless in dev (in-memory codegen works there too).
    _cc.gen_dir = None

    # Generate the IUIAutomation wrappers from the system typelib. GetModule
    # returns the generated wrapper module directly; capture its return value
    # rather than re-importing `comtypes.gen.UIAutomationClient`, which may not
    # exist on disk under `gen_dir = None`.
    _UIA = _cc.GetModule("UIAutomationCore.dll")

    import win32gui  # toolbar-rect lookup for the Firefox cold-start wake

    _UIA_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import/codegen failure degrades cleanly
    _UIA_AVAILABLE = False


def uia_available() -> bool:
    """True if the UIA stack imported and generated its wrappers successfully.

    The frozen-build gate (Task 3.1): when False, `read_active_browser_domain`
    can never return a domain, so per-site routing degrades to per-app/default.
    Exposed so the caller can log this state at startup (the module itself
    deliberately emits no log line, per the privacy invariant above)."""
    return _UIA_AVAILABLE


# Process names whose address bar we know how to read.
_CHROMIUM_BROWSERS = {"chrome.exe", "msedge.exe"}
_FIREFOX_BROWSERS = {"firefox.exe"}
_KNOWN_BROWSERS = _CHROMIUM_BROWSERS | _FIREFOX_BROWSERS


def is_known_browser(process_name: str | None) -> bool:
    """True if `process_name` is a browser we can read a domain from."""
    return bool(process_name) and process_name.lower() in _KNOWN_BROWSERS


def extract_registrable_domain(raw: str | None) -> str | None:
    """Reduce an address-bar value to a registrable-ish domain, or None.

    Ported verbatim from the verified spikes
    (the uia_probe and firefox_combobox
    to_domain helpers): prepend `http://` if there is no scheme,
    `urlparse`, take `netloc`, strip `user@`, strip `:port`, strip a leading
    `www.`, lowercase. Algorithm = "www-strip + host only"; subdomains are
    PRESERVED (so `news.ycombinator.com` keeps its three labels -- a last-two-
    labels / eTLD+1 reduction would corrupt it). The frontend `normalizeDomain`
    mirrors this for instant validation; the backend stays authoritative.

    Omnibox guard (the value this whole feature trusts): return None unless the
    result is a host containing a dot, and reject inputs with interior whitespace
    (search text), so a mid-typed query, a bare word (`localhost`), an empty
    value, or a new-tab page never produces a spurious "domain".
    """
    if not raw:
        return None
    t = raw.strip()
    if not t:
        return None
    # Interior whitespace => search text / multi-word query, not a URL.
    if " " in t:
        return None
    if "://" not in t:
        t = "http://" + t
    try:
        netloc = urlparse(t).netloc
    except Exception:  # noqa: BLE001 - malformed input never raises out of here
        return None
    host = netloc.split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    # Must be a real host (contains a dot). Rejects bare words and new-tab values.
    if not host or "." not in host:
        return None
    return host


def _read_domain(uia, element, name_needle: str) -> str | None:
    """Return a normalized host from the named address control.

    ``element`` is already scoped to one browser window. The raw ValuePattern
    value never crosses this helper's return boundary. Controls whose names do
    not identify the address bar are ignored rather than treated as a fallback.

    comtypes `FindFirst` matches exact property conditions only (no regex), so we
    fetch all descendants of the target control type and substring-match the Name
    in Python -- the equivalent of the spike's `RegexName="(?i).*address.*"`.
    """
    try:
        type_cond = uia.CreatePropertyCondition(
            _UIA.UIA_ControlTypePropertyId, _UIA.UIA_EditControlTypeId
        )
        combo_cond = uia.CreatePropertyCondition(
            _UIA.UIA_ControlTypePropertyId, _UIA.UIA_ComboBoxControlTypeId
        )
        cond = uia.CreateOrCondition(type_cond, combo_cond)
        found = element.FindAll(_UIA.TreeScope_Descendants, cond)
    except Exception:  # noqa: BLE001
        return None
    if not found:
        return None

    try:
        count = found.Length
    except Exception:  # noqa: BLE001
        return None
    for i in range(count):
        try:
            el = found.GetElement(i)
            name = (el.CurrentName or "").lower()
        except Exception:  # noqa: BLE001
            continue
        if name_needle in name:
            try:
                pattern = el.GetCurrentPattern(_UIA.UIA_ValuePatternId)
                if not pattern:
                    return None
                value_pattern = pattern.QueryInterface(
                    _UIA.IUIAutomationValuePattern
                )
                return extract_registrable_domain(value_pattern.CurrentValue)
            except Exception:  # noqa: BLE001
                return None
    return None


def _wake_firefox_a11y(uia, hwnd) -> None:
    """Cold-start guard: sweep ElementFromPoint across the Firefox toolbar to
    instantiate its lazy accessibility tree. Ported from firefox_combobox.py
    wake_via_point. No-op on any failure.
    """
    try:
        left, top, right, _bottom = win32gui.GetWindowRect(hwnd)
        x = left + int((right - left) * 0.45)
        for y in range(top + 40, top + 95, 8):
            try:
                uia.ElementFromPoint(_make_point(x, y))
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def _make_point(x: int, y: int):
    """Build the tagPOINT struct ElementFromPoint expects."""
    return _UIA.tagPOINT(x, y)


def read_active_browser_domain(hwnd: int, process_name: str | None) -> str | None:
    """Read the registrable domain of the active tab in a browser window.

    Best-effort like the foreground routing probe: returns None
    on a non-browser process, when UIA is unavailable, or on ANY COM/UIA error.
    NEVER raises -- this runs on the recording-start path and must not crash it.

    Does NOT set SPI_SETSCREENREADER (research rejects it: global side effect,
    not the activation trigger). For Firefox it wakes the lazy a11y tree with a
    one-off ElementFromPoint sweep and retries once.

    Privacy: returns the domain to the caller for in-memory routing only; emits
    no log line and writes nothing to disk.
    """
    if not _UIA_AVAILABLE or not is_known_browser(process_name):
        return None
    if not hwnd:
        return None

    pn = process_name.lower()
    try:
        uia = _cc.CreateObject(_UIA.CUIAutomation, interface=_UIA.IUIAutomation)
        # ElementFromHandle on the browser HWND is the call that wakes Firefox's
        # lazy a11y tree (sends WM_GETOBJECT/UiaRootObjectId); harmless for
        # Chromium, which is always populated.
        element = uia.ElementFromHandle(hwnd)
        if not element:
            return None

        if pn in _CHROMIUM_BROWSERS:
            # Chromium: "Address and search bar" Edit control.
            return _read_domain(uia, element, "address")

        # Firefox: "Search with ... or enter address" ComboBox.
        domain = _read_domain(uia, element, "address")
        if domain:
            return domain
        # Cold start: a11y tree not yet instantiated. Wake it, then retry once.
        _wake_firefox_a11y(uia, hwnd)
        return _read_domain(uia, element, "address")
    except Exception:  # noqa: BLE001 - any COM/UIA error degrades to None
        return None
