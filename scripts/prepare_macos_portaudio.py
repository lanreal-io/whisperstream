#!/usr/bin/env python3
"""Build the pinned CoreAudio deadlock fix into this virtual environment.

No app bundle or installer is produced. See vendor/portaudio/README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

REVISION = "1ca0eeedb678c3cca80f3306c474d69a7a4b5278"
BASE_REVISION = "147dd722548358763a8b649b3e4b41dfffbcfbb6"  # v19.7.0
SOURCE_TREE = "aac4dc5b8eb81d25ff0f1cd9be344601db2a3bf3"
SOURCE_URL = "https://github.com/PortAudio/portaudio.git"
CMAKE_VERSION = "3.31.6"


RECEIPT_NAME = "whisperstream-portaudio.json"
SOUNDDEVICE_SHA256 = "db3bfdb3b71c934a6fb169aab41e61a90fd1d1491aa03fccc97e8e895d3c2b58"
LOADER_ORIGINAL = b"try:\n    for _libname in (\n"
LOADER_PATCHED = (
    b'try:\n    # Whisperstream: use the reviewed bundled CoreAudio fix on macOS.\n'
    b'    if _platform.system() == "Darwin":\n'
    b'        raise OSError("Use bundled PortAudio")\n'
    b'    for _libname in (\n'
)


def source_identity() -> dict:
    return {
        "revision": REVISION, "base_revision": BASE_REVISION,
        "source_tree": SOURCE_TREE, "source_url": SOURCE_URL,
        "cmake_version": CMAKE_VERSION, "build_type": "Release",
        "assertions": False, "architecture": "arm64", "deployment_target": "13.0",
    }


def prepare_loader(source: bytes) -> bytes:
    original = source.replace(LOADER_PATCHED, LOADER_ORIGINAL)
    if hashlib.sha256(original).hexdigest() != SOUNDDEVICE_SHA256:
        raise RuntimeError("Unsupported sounddevice loader source; review the dependency update")
    return original.replace(LOADER_ORIGINAL, LOADER_PATCHED, 1)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_directory() -> Path:
    spec = importlib.util.find_spec("_sounddevice_data")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("Install the locked macOS sounddevice wheel first")
    directory = Path(next(iter(spec.submodule_search_locations))).resolve()
    # Never patch a system install or a uv shared cache hardlink in place.
    if sys.prefix == sys.base_prefix or not directory.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Run with the target virtual environment's Python")
    return directory


def verify(directory: Path) -> dict:
    receipt = json.loads((directory / RECEIPT_NAME).read_text())
    library = directory / "portaudio-binaries/libportaudio.dylib"
    loader = directory.parent / "sounddevice.py"
    if (any(receipt.get(key) != value for key, value in source_identity().items())
            or receipt.get("library_sha256") != digest(library)):
        raise RuntimeError("PortAudio provenance or library hash mismatch")
    if loader.read_bytes() != prepare_loader(loader.read_bytes()):
        raise RuntimeError("sounddevice loader can bypass the bundled CoreAudio fix")
    if receipt.get("loader_sha256") != digest(loader):
        raise RuntimeError("sounddevice loader checksum mismatch")
    assert_no_native_assertions(library)
    # sounddevice prefers a system library over its wheel. Verify the actual
    # loader choice in a fresh interpreter, not just the file we replaced.
    loaded = subprocess.check_output(
        [sys.executable, "-c", "import sounddevice; print(sounddevice._libname)"],
        text=True,
    ).strip()
    if Path(loaded).resolve() != library.resolve():
        raise RuntimeError("sounddevice selected another PortAudio library; remove the system-library override")
    return receipt


def assert_no_native_assertions(library: Path) -> None:
    symbols = subprocess.check_output(["nm", "-u", str(library)], text=True)
    if "___assert_rtn" in symbols.split():
        raise RuntimeError("PortAudio contains enabled native assertions")


def prepare_source(root: Path) -> Path:
    source_root = root / "source"
    subprocess.run(["git", "init", "--quiet", str(source_root)], check=True)

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(source_root), *args],
            text=True,
        ).strip()

    git("fetch", "--depth=2", SOURCE_URL, BASE_REVISION, REVISION)
    git("checkout", "--detach", BASE_REVISION)
    if git("rev-parse", "HEAD") != BASE_REVISION:
        raise RuntimeError("PortAudio base revision mismatch")
    # Apply only the fix's delta, never the intervening master history.
    git("cherry-pick", "--no-commit", REVISION)
    if git("write-tree") != SOURCE_TREE:
        raise RuntimeError("PortAudio patched source tree mismatch")
    return source_root


def build_library(root: Path) -> Path:
    source_root = prepare_source(root)
    build_root = root / "build"
    env = os.environ.copy()
    # Do not let caller flags silently turn release assertions back on.
    for key in ("CFLAGS", "CPPFLAGS", "CXXFLAGS", "LDFLAGS"):
        env.pop(key, None)
    cmake = ["uv", "tool", "run", "--from", f"cmake=={CMAKE_VERSION}", "cmake"]
    for command in (
        [*cmake, "-S", str(source_root), "-B", str(build_root),
         "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_C_COMPILER=clang",
         "-DCMAKE_C_FLAGS_RELEASE=-O3 -DNDEBUG", "-DCMAKE_OSX_ARCHITECTURES=arm64",
         "-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0", "-DPA_BUILD_SHARED=ON",
         "-DPA_BUILD_STATIC=OFF", "-DPA_BUILD_TESTS=OFF", "-DPA_BUILD_EXAMPLES=OFF"],
        [*cmake, "--build", str(build_root), "--config", "Release", "--parallel", "2"],
    ):
        subprocess.run(command, env=env, check=True)
    library = build_root / "libportaudio.dylib"
    assert_no_native_assertions(library)
    subprocess.run(["install_name_tool", "-id", "@rpath/libportaudio.dylib", str(library)], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(library)], check=True)
    arches = subprocess.check_output(["lipo", "-archs", str(library)], text=True).strip()
    if arches != "arm64":
        raise RuntimeError("PortAudio build architecture mismatch")
    return library


def install(library: Path, directory: Path) -> dict:
    destination = directory / "portaudio-binaries/libportaudio.dylib"
    loader = directory.parent / "sounddevice.py"
    patched_loader = prepare_loader(loader.read_bytes())
    receipt = {
        **source_identity(), "library_sha256": digest(library),
        "sounddevice_original_sha256": SOUNDDEVICE_SHA256,
        "loader_sha256": hashlib.sha256(patched_loader).hexdigest(),
    }
    # Replace, never overwrite: uv may have hardlinked the original wheel.
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
    try:
        shutil.copyfile(library, temporary)
        temporary.chmod(0o755)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile(dir=loader.parent, delete=False) as output:
        loader_temporary = Path(output.name)
        output.write(patched_loader)
    try:
        loader_temporary.chmod(0o644)
        os.replace(loader_temporary, loader)
    finally:
        loader_temporary.unlink(missing_ok=True)
    (directory / RECEIPT_NAME).write_text(json.dumps(receipt, indent=2) + "\n")
    return verify(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--receipt", type=Path, help="Copy verified provenance into build logs")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This preparation step requires native Apple Silicon")
    directory = package_directory()
    prepare_loader((directory.parent / "sounddevice.py").read_bytes())
    try:
        receipt = verify(directory)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
        if args.check:
            raise
        with tempfile.TemporaryDirectory(prefix="whisperstream-portaudio-") as temporary:
            receipt = install(build_library(Path(temporary)), directory)
    if args.receipt:
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"PortAudio CoreAudio fix verified: {REVISION}")


if __name__ == "__main__":
    main()
