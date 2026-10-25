"""Best-effort extraction of an executable's icon as a small PNG data-URI.

Exposes:
    icon_data_uri_for_exe(exe_path) -> str | None
        Returns a ``data:image/png;base64,...`` string for the exe's icon, or
        ``None`` when extraction is unavailable or fails for any reason.

The running-app picker (Power Mode routing) calls this once per unique exe to
render a real app icon next to each process name. It must never raise and must
never block the app list: every failure path collapses to ``None``.

Implementation notes:
- Uses ``win32gui.ExtractIconEx`` to pull the exe's HICON, then rasterizes it
  to a 32x32 BGRA buffer via a top-down 32bpp DIB section (ctypes GDI) and
  ``DrawIconEx`` with ``DI_NORMAL`` (alpha-composited). The BGRA buffer is
  converted to an RGBA PNG using only the standard library (``zlib`` +
  ``struct``), so no Pillow dependency is added.
- Results are cached by exe path for at most five minutes and 128 entries so
  repeated picker calls avoid immediate re-rasterization without retaining an
  unbounded application-identity catalog for the process lifetime.

Privacy: never log icon bytes or exe paths. The caller already surfaces the
process name; this module logs nothing.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
import struct
import sys
import time
import zlib
from typing import Optional

# ---------------------------------------------------------------------------
# Top-level guarded imports (Nuitka-friendly static signal). Mirror
# foreground_window.py / system_info.py: a missing lib or non-Windows degrades
# the whole module to "icons unavailable" (every public call returns None).
# ---------------------------------------------------------------------------
try:
    import ctypes
    from ctypes import wintypes

    import win32con
    import win32gui

    _ICONS_AVAILABLE = sys.platform == "win32"
except Exception:
    _ICONS_AVAILABLE = False

# Icon bytes can identify installed applications, so keep only a short-lived,
# count-bounded process-memory cache. A cached None still means "tried and
# failed" during the same five-minute window.
_ICON_CACHE_TTL_SECONDS = 5 * 60.0
_ICON_CACHE_MAX_ENTRIES = 128
_ICON_CACHE: OrderedDict[str, tuple[float, Optional[str]]] = OrderedDict()

# Rasterization target. 32px is a good middle ground: large enough to look crisp
# at the picker's 16-20px render size, small enough that the base64 PNG stays a
# few hundred bytes.
_ICON_SIZE = 32


def _prune_icon_cache(now: float) -> None:
    """Drop expired and oldest-overflow icon results from process memory."""
    for path, (expires_at, _result) in list(_ICON_CACHE.items()):
        if expires_at <= now:
            _ICON_CACHE.pop(path, None)
    while len(_ICON_CACHE) > max(0, _ICON_CACHE_MAX_ENTRIES):
        _ICON_CACHE.popitem(last=False)


def _get_cached_icon(exe_path: str, now: float) -> tuple[bool, Optional[str]]:
    _prune_icon_cache(now)
    entry = _ICON_CACHE.pop(exe_path, None)
    if entry is None:
        return False, None
    expires_at, result = entry
    if expires_at <= now:
        return False, None
    _ICON_CACHE[exe_path] = entry
    return True, result


def _cache_icon(exe_path: str, result: Optional[str]) -> None:
    now = time.monotonic()
    _prune_icon_cache(now)
    _ICON_CACHE.pop(exe_path, None)
    _ICON_CACHE[exe_path] = (now + _ICON_CACHE_TTL_SECONDS, result)
    _prune_icon_cache(now)


if _ICONS_AVAILABLE:

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]


def _png_from_rgba(rgba: bytes, width: int, height: int) -> bytes:
    """Encode a raw RGBA pixel buffer as PNG bytes (stdlib only).

    ``rgba`` is ``width * height * 4`` bytes, row-major, top row first.
    """
    # Prepend the per-scanline filter byte (0 = None) required by the PNG spec.
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(rgba[y * stride : (y + 1) * stride])

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    # IHDR: 8-bit, color type 6 (truecolor + alpha).
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _rasterize_hicon(hicon: int) -> Optional[bytes]:
    """Draw ``hicon`` onto a 32bpp top-down DIB and return PNG bytes, or None."""
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    size = _ICON_SIZE
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth = size
    # Negative height -> top-down DIB so row 0 is the top scanline.
    bmi.biHeight = -size
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0  # BI_RGB

    screen_dc = user32.GetDC(0)
    if not screen_dc:
        return None
    mem_dc = 0
    dib = 0
    old_obj = 0
    try:
        bits_ptr = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(
            screen_dc,
            ctypes.byref(bmi),
            0,  # DIB_RGB_COLORS
            ctypes.byref(bits_ptr),
            None,
            0,
        )
        if not dib or not bits_ptr:
            return None

        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not mem_dc:
            return None
        old_obj = gdi32.SelectObject(mem_dc, dib)

        # DI_NORMAL alpha-composites the icon (incl. its alpha channel) onto the
        # zero-initialized (transparent) DIB.
        ok = user32.DrawIconEx(
            mem_dc, 0, 0, hicon, size, size, 0, None, win32con.DI_NORMAL
        )
        if not ok:
            return None

        # Pull the BGRA bytes out of the DIB section.
        buf = (ctypes.c_char * (size * size * 4))()
        ctypes.memmove(buf, bits_ptr, size * size * 4)
        bgra = bytes(buf)

        # DrawIconEx writes straight (non-premultiplied) alpha into a BI_RGB DIB
        # in BGRA byte order. Convert to RGBA by swapping the B and R channels.
        rgba = bytearray(bgra)
        rgba[0::4], rgba[2::4] = bgra[2::4], bgra[0::4]

        # If the icon has no alpha information at all (every alpha byte 0), the
        # PNG would be fully transparent. Detect that and force opaque so the
        # icon is still visible.
        if not any(rgba[3::4]):
            rgba[3::4] = bytes([255]) * (size * size)

        return _png_from_rgba(bytes(rgba), size, size)
    finally:
        if old_obj and mem_dc:
            gdi32.SelectObject(mem_dc, old_obj)
        if dib:
            gdi32.DeleteObject(dib)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)


def icon_data_uri_for_exe(exe_path: Optional[str]) -> Optional[str]:
    """Return a PNG data-URI for ``exe_path``'s icon, or None.

    Best-effort and never raises. Results are cached briefly by exe path
    (including a cached None for exes with no usable icon) so repeated picker
    calls don't immediately re-rasterize. Returns None on non-Windows, missing
    libs, a falsy path, or any extraction failure.
    """
    if not _ICONS_AVAILABLE or not exe_path:
        return None

    cached, cached_result = _get_cached_icon(exe_path, time.monotonic())
    if cached:
        return cached_result

    result: Optional[str] = None
    large = None
    small = None
    try:
        # ExtractIconEx returns (large_icons, small_icons) lists. Prefer a large
        # icon for crisper downscaling; fall back to the small one.
        large, small = win32gui.ExtractIconEx(exe_path, 0, 1)
        hicon = None
        if large:
            hicon = large[0]
        elif small:
            hicon = small[0]
        if hicon:
            png = _rasterize_hicon(hicon)
            if png:
                result = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    except Exception:
        result = None
    finally:
        # ExtractIconEx hands ownership of every returned HICON to us; destroy
        # them all so we don't leak GDI handles across repeated picker opens.
        for handle_list in (large, small):
            if handle_list:
                for handle in handle_list:
                    try:
                        win32gui.DestroyIcon(handle)
                    except Exception:
                        pass

    _cache_icon(exe_path, result)
    return result
