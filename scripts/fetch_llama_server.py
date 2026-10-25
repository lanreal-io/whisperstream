"""Fetch, verify, and stage the llama-server.exe sidecar and its runtime DLLs.

Usage (standalone):
    uv run python scripts/fetch_llama_server.py

Called from scripts/build.py Step 7 via stage_llama_server(). Safe to run
standalone for development staging.

Pinning strategy
----------------
Pin to a specific release tag (e.g. b10537) rather than tracking "latest".
Before changing the tag, verify the new zip exists:
    https://github.com/ggml-org/llama.cpp/releases/download/<TAG>/llama-<TAG>-bin-win-vulkan-x64.zip
Then update LLAMA_CPP_RELEASE_TAG, LLAMA_CPP_ARTIFACT, LLAMA_CPP_BASE_URL, and LLAMA_CPP_SHA256.

DLLs shipped
------------
The Vulkan build ships the Vulkan backend (ggml-vulkan.dll), one base GGML
DLL, per-uarch CPU backends (kept in the Vulkan zip as fallback compute),
the llama runtime, the common/mtmd helpers, and the OpenMP runtime. All are
required at load time; missing any one causes llama-server to abort with a
DLL not found error. The llama-server.exe binary is renamed to the Tauri
sidecar convention (llama-server-x86_64-pc-windows-msvc.exe); DLLs are NOT
renamed. SHA256 verified against llama-b10537-bin-win-vulkan-x64.zip on
2026-09-18.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Pinned release constants -- update all together when upgrading.
# b10537 is exactly upstream PR #26678 (bf0040e15fd5b716262658f4d652c9cee959cf91),
# replacing the restricted Microsoft OpenMP runtime with LLVM OpenMP 20.1.8.
# ---------------------------------------------------------------------------
LLAMA_CPP_RELEASE_TAG = "b10537"
# The artifact name embeds the build number, so it changes with every tag bump.
LLAMA_CPP_ARTIFACT = f"llama-{LLAMA_CPP_RELEASE_TAG}-bin-win-vulkan-x64.zip"
LLAMA_CPP_BASE_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_CPP_RELEASE_TAG}"
# SHA256 of llama-b10537-bin-win-vulkan-x64.zip downloaded on 2026-09-18. Re-verify when bumping the tag.
LLAMA_CPP_SHA256 = "36fb9809433edc17364955880b5a14a7a0b4b967f465e10723119c150418de8c"

# ---------------------------------------------------------------------------
# DLLs to copy alongside llama-server.exe.
# Enumerated from llama-b10537-bin-win-vulkan-x64.zip on 2026-09-18.
# All are present in the Vulkan zip and required at runtime.
# ---------------------------------------------------------------------------
REQUIRED_DLLS = [
    "ggml-base.dll",
    "ggml-cpu-alderlake.dll",
    "ggml-cpu-cannonlake.dll",
    "ggml-cpu-cascadelake.dll",
    "ggml-cpu-cooperlake.dll",
    "ggml-cpu-haswell.dll",
    "ggml-cpu-icelake.dll",
    "ggml-cpu-ivybridge.dll",
    "ggml-cpu-piledriver.dll",
    "ggml-cpu-sandybridge.dll",
    "ggml-cpu-sapphirerapids.dll",
    "ggml-cpu-skylakex.dll",
    "ggml-cpu-sse42.dll",
    "ggml-cpu-x64.dll",
    "ggml-cpu-zen4.dll",
    "ggml-rpc.dll",
    "ggml-vulkan.dll",
    "ggml.dll",
    "libomp.dll",
    "llama-server-impl.dll",
    "llama-common.dll",
    "llama.dll",
    "mtmd.dll",
]

OPENMP_LICENSE = "LICENSE-LLVM-OpenMP"
OPENMP_LICENSE_SHA256 = "fdad1758a9e1f9d5a81e18879b3406772115edc92c24bfa36b70c654f325e8e4"
OBSOLETE_DLLS = ("libomp140.x86_64.dll",)

# Resolve repo root from this file's location (scripts/ -> repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _size_str(path: Path) -> str:
    """Return a short human-readable size string (replaces du -h)."""
    try:
        size = path.stat().st_size
    except OSError:
        return "?"
    units = ["B", "K", "M", "G"]
    value = float(size)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{int(size)}B"


def _sha256_file(path: Path) -> str:
    """Compute the SHA256 hex digest of a file using Python hashlib (no sha256sum spawn)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def stage_llama_server(repo_root: Path | None = None) -> None:
    """Fetch (or cache-hit), verify, and stage the llama-server sidecar and DLLs.

    Cache layout: dist/cache/llama-cpp/<tag>/<artifact>.zip
    Staged exe:   src-tauri/binaries/llama-server-x86_64-pc-windows-msvc.exe
    Staged DLLs:  src-tauri/binaries/<dll> (not renamed)

    Raises SystemExit(1) on integrity or required-payload validation failure.
    """
    root = repo_root or _REPO_ROOT

    cache_dir = root / "dist" / "cache" / "llama-cpp" / LLAMA_CPP_RELEASE_TAG
    zip_path = cache_dir / LLAMA_CPP_ARTIFACT
    binaries_dir = root / "src-tauri" / "binaries"
    staged_exe = binaries_dir / "llama-server-x86_64-pc-windows-msvc.exe"

    print(f"[llama] tag={LLAMA_CPP_RELEASE_TAG} artifact={LLAMA_CPP_ARTIFACT}")

    # --- Step 1: Download (cached) ---
    if zip_path.is_file():
        print(f"[llama] Cache hit: {zip_path} -- skipping download.")
    else:
        url = f"{LLAMA_CPP_BASE_URL}/{LLAMA_CPP_ARTIFACT}"
        print(f"[llama] Downloading {url} ...")
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Download with progress reporting via urllib (no curl spawn).
        def _report_hook(block_num: int, block_size: int, total_size: int) -> None:
            if total_size > 0:
                pct = min(100, int(block_num * block_size * 100 / total_size))
                print(f"\r[llama] Progress: {pct}%", end="", flush=True)
        urllib.request.urlretrieve(url, zip_path, reporthook=_report_hook)
        print()  # newline after progress line
        print(f"[llama] Download complete: {_size_str(zip_path)}")

    # --- Step 2: SHA256 verification ---
    print("[llama] Verifying SHA256...")
    actual_sha = _sha256_file(zip_path)
    if actual_sha != LLAMA_CPP_SHA256:
        print(f"ERROR: SHA256 mismatch for {LLAMA_CPP_ARTIFACT}", file=sys.stderr)
        print(f"  expected: {LLAMA_CPP_SHA256}", file=sys.stderr)
        print(f"  actual:   {actual_sha}", file=sys.stderr)
        print(
            f"  Delete {zip_path} and re-run, or update the SHA256 constant if you changed the tag.",
            file=sys.stderr,
        )
        sys.exit(1)
    print("[llama] SHA256 OK")

    # Validate the archive itself before changing any staged file. Never reuse an
    # extracted directory: stale DLLs could mask an incomplete new payload.
    required = ["llama-server.exe", *REQUIRED_DLLS, OPENMP_LICENSE]
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if any(Path(name).name.lower() in OBSOLETE_DLLS for name in names):
            sys.exit("ERROR: Restricted Microsoft OpenMP runtime in llama archive.")
        for name in required:
            if names.count(name) != 1 or zf.getinfo(name).file_size == 0:
                sys.exit(f"ERROR: Missing, empty, or duplicate required llama payload: {name}")
        payload = {name: zf.read(name) for name in required}
    if hashlib.sha256(payload[OPENMP_LICENSE]).hexdigest() != OPENMP_LICENSE_SHA256:
        sys.exit("ERROR: LLVM OpenMP license hash mismatch.")

    binaries_dir.mkdir(parents=True, exist_ok=True)
    for name, data in payload.items():
        target = staged_exe if name == "llama-server.exe" else binaries_dir / name
        target.write_bytes(data)
        print(f"[llama] Staged {target.name}")
    # Remove only retired files owned by this fetcher, never unrelated sidecars.
    for name in OBSOLETE_DLLS:
        (binaries_dir / name).unlink(missing_ok=True)

    print("[llama] Licenses: llama.cpp (MIT), LLVM OpenMP (Apache-2.0 WITH LLVM-exception).")

    # --- Done: print summary ---
    print()
    print("[llama] Done. Staged files in src-tauri/binaries/:")
    print(f"  {_size_str(staged_exe)}  llama-server-x86_64-pc-windows-msvc.exe")
    for dll in REQUIRED_DLLS:
        dll_path = binaries_dir / dll
        if dll_path.is_file():
            print(f"  {_size_str(dll_path)}  {dll}")


def main() -> int:
    """Entrypoint for standalone use: uv run python scripts/fetch_llama_server.py"""
    stage_llama_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())
