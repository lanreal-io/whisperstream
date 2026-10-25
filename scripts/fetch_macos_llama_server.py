#!/usr/bin/env python3
"""Fetch, verify, and stage the pinned Apple Silicon llama.cpp sidecar."""

from __future__ import annotations

import hashlib
import shutil
import stat
import tarfile
import tempfile
import urllib.request
from pathlib import Path


LLAMA_CPP_RELEASE_TAG = "b9204"
LLAMA_CPP_ARTIFACT = f"llama-{LLAMA_CPP_RELEASE_TAG}-bin-macos-arm64.tar.gz"
LLAMA_CPP_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{LLAMA_CPP_RELEASE_TAG}/{LLAMA_CPP_ARTIFACT}"
)
LLAMA_CPP_SHA256 = (
    "3182ca57962a18b21f459f74d51970456dc9e6b0255d6fa28c46dc272789b26d"
)
TARGET_TRIPLE = "aarch64-apple-darwin"
REQUIRED_DYLIBS = (
    "libggml-base.0.12.0.dylib",
    "libggml-base.0.dylib",
    "libggml-base.dylib",
    "libggml-blas.0.12.0.dylib",
    "libggml-blas.0.dylib",
    "libggml-blas.dylib",
    "libggml-cpu.0.12.0.dylib",
    "libggml-cpu.0.dylib",
    "libggml-cpu.dylib",
    "libggml-metal.0.12.0.dylib",
    "libggml-metal.0.dylib",
    "libggml-metal.dylib",
    "libggml-rpc.0.12.0.dylib",
    "libggml-rpc.0.dylib",
    "libggml-rpc.dylib",
    "libggml.0.12.0.dylib",
    "libggml.0.dylib",
    "libggml.dylib",
    "libllama-common.0.0.9204.dylib",
    "libllama-common.0.dylib",
    "libllama-common.dylib",
    "libllama.0.0.9204.dylib",
    "libllama.0.dylib",
    "libllama.dylib",
    "libmtmd.0.0.9204.dylib",
    "libmtmd.0.dylib",
    "libmtmd.dylib",
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class MacosLlamaStageError(RuntimeError):
    """Raised when the pinned sidecar cannot be staged exactly."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    partial = archive.with_suffix(archive.suffix + ".part")
    try:
        with urllib.request.urlopen(LLAMA_CPP_URL, timeout=120) as response:
            with partial.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        partial.replace(archive)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _find_payload(root: Path) -> Path:
    matches = [path.parent for path in root.rglob("llama-server") if path.is_file()]
    if len(matches) != 1:
        raise MacosLlamaStageError(
            f"expected one llama-server payload, found count={len(matches)}"
        )
    return matches[0]


def _stage_archive(archive: Path, repo_root: Path) -> dict[str, object]:
    if _sha256(archive) != LLAMA_CPP_SHA256:
        raise MacosLlamaStageError("pinned macOS llama.cpp archive digest mismatch")

    binaries = repo_root / "src-tauri" / "binaries"
    resources = repo_root / "src-tauri" / "resources" / "llama-cpp"
    binaries.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="whisperstream-llama-stage-") as value:
        extracted = Path(value)
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(extracted, filter="data")
        payload = _find_payload(extracted)
        required = ("llama-server", "LICENSE", *REQUIRED_DYLIBS)
        missing = [name for name in required if not (payload / name).is_file()]
        if missing:
            raise MacosLlamaStageError(
                f"pinned macOS llama.cpp payload missing count={len(missing)}"
            )

        sidecar = binaries / f"llama-server-{TARGET_TRIPLE}"
        shutil.copy2(payload / "llama-server", sidecar)
        sidecar.chmod(sidecar.stat().st_mode | stat.S_IXUSR)
        for name in ("LICENSE", *REQUIRED_DYLIBS):
            shutil.copy2(payload / name, resources / name, follow_symlinks=True)

    return {
        "release": LLAMA_CPP_RELEASE_TAG,
        "archive": LLAMA_CPP_ARTIFACT,
        "archive_sha256": LLAMA_CPP_SHA256,
        "sidecar_sha256": _sha256(sidecar),
        "dylib_count": len(REQUIRED_DYLIBS),
    }


def stage_macos_llama_server(
    repo_root: Path | None = None,
    *,
    cache_root: Path | None = None,
) -> dict[str, object]:
    """Stage the verified sidecar and dylibs into a source/build tree."""

    root = (repo_root or REPO_ROOT).resolve()
    cache = (
        cache_root.resolve()
        if cache_root is not None
        else root / "dist" / "cache" / "llama-cpp"
    )
    archive = cache / LLAMA_CPP_RELEASE_TAG / LLAMA_CPP_ARTIFACT
    if not archive.is_file():
        _download(archive)
    return _stage_archive(archive, root)


def main() -> int:
    result = stage_macos_llama_server()
    print(
        "[llama-macos] staged "
        f"release={result['release']} dylibs={result['dylib_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
