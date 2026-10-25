"""Build the pinned espeak-free sherpa-onnx Windows wheel set.

The build starts from the exact upstream commit in a detached checkout,
applies the tracked downstream patch, injects the downstream PEP 440 version,
and creates one normal-ABI x64 wheel for each supported CPython.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_URL = "https://github.com/k2-fsa/sherpa-onnx.git"
UPSTREAM_COMMIT = "330609dab49be6ee8b30702918ca7abbbad1286a"
UPSTREAM_VERSION = "1.13.3"
DOWNSTREAM_VERSION = "1.13.3+whisperstream.2"
SUPPORTED_PYTHONS = ("3.12", "3.13", "3.14")
BUILD_REQUIREMENTS = (
    "setuptools==80.9.0",
    "wheel==0.45.1",
    "cmake==3.31.6",
)
PATCH_PATH = REPO_ROOT / "vendor" / "sherpa-onnx" / "espeak-free-supertonic.patch"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "dist" / "sherpa-onnx-wheels"
WORK_ROOT_ENV = "WHISPERSTREAM_SHERPA_WORK_ROOT"
DEFAULT_WORK_ROOT_NAME = "ws-build"
# VS2019's MSBuild FileTracker still uses legacy MAX_PATH handling for its
# generated .tlog files. Keeping source at 80 characters or fewer leaves
# substantial room for CMake's approximately 150-character try-compile suffix.
MAX_SOURCE_DIR_LENGTH = 80
MANIFEST_NAME = "sha256-manifest.json"
CMAKE_ARGS = (
    "-DCMAKE_BUILD_TYPE=Release",
    "-DSHERPA_ONNX_ENABLE_WHISPERSTREAM_SUPERTONIC_ONLY=ON",
    "-DSHERPA_ONNX_ENABLE_TTS=ON",
    "-DSHERPA_ONNX_ENABLE_BINARY=OFF",
    "-DSHERPA_ONNX_ENABLE_TESTS=OFF",
    "-DSHERPA_ONNX_ENABLE_GPU=OFF",
    "-DSHERPA_ONNX_ENABLE_PORTAUDIO=OFF",
    "-DSHERPA_ONNX_ENABLE_WEBSOCKET=OFF",
)


class BuildError(RuntimeError):
    """The pinned source or wheel build did not meet its contract."""


def _remove_tree(path: Path) -> None:
    """Remove a disposable tree, retrying read-only Git files on Windows."""
    if not path.exists():
        return

    def make_writable_and_retry(
        function: Callable[[str], object],
        target: str,
        exc_info: tuple[type[BaseException], BaseException, object],
    ) -> None:
        del exc_info
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        function(target)

    shutil.rmtree(path, onerror=make_writable_and_retry)


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+", subprocess.list2cmdline(list(command)))
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        check=True,
        text=True,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_exact(path: Path, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    if content.count(old) != 1:
        raise BuildError(
            f"expected exactly one version declaration {old!r} in {path}"
        )
    path.write_text(content.replace(old, new), encoding="utf-8", newline="\n")


def _validate_short_work_dir(work_dir: Path) -> None:
    source_dir = work_dir / "source"
    if len(str(source_dir)) > MAX_SOURCE_DIR_LENGTH:
        raise BuildError(
            "sherpa source path is too long for Visual Studio 2019 MSBuild "
            f"FileTracker ({len(str(source_dir))} > {MAX_SOURCE_DIR_LENGTH}): "
            f"{source_dir}"
        )


def _automatic_work_roots() -> tuple[Path, ...]:
    configured = os.environ.get(WORK_ROOT_ENV)
    if configured:
        return (Path(configured),)

    system_drive = os.environ.get("SystemDrive", "C:")
    primary = Path(f"{system_drive}\\{DEFAULT_WORK_ROOT_NAME}")
    fallback = Path(tempfile.gettempdir()) / DEFAULT_WORK_ROOT_NAME
    if primary.resolve() == fallback.resolve():
        return (primary,)
    return (primary, fallback)


def resolve_work_dir(requested: Path | None) -> tuple[Path, bool]:
    """Resolve an explicit work directory or allocate unique short scratch."""
    if requested is not None:
        work_dir = requested.resolve()
        _validate_short_work_dir(work_dir)
        return work_dir, False

    failures: list[str] = []
    for root in _automatic_work_roots():
        resolved_root = root.resolve()
        candidate: Path | None = None
        try:
            resolved_root.mkdir(parents=True, exist_ok=True)
            candidate = Path(
                tempfile.mkdtemp(prefix="s-", dir=resolved_root)
            ).resolve()
            _validate_short_work_dir(candidate)
            return candidate, True
        except (BuildError, OSError) as exc:
            failures.append(f"{resolved_root}: {exc}")
            if candidate is not None:
                try:
                    _remove_tree(candidate)
                except OSError:
                    pass

    raise BuildError(
        "could not allocate a short sherpa build directory: "
        + "; ".join(failures)
    )


def _require_disjoint_paths(work_dir: Path, output_dir: Path) -> None:
    if (
        work_dir == output_dir
        or work_dir.is_relative_to(output_dir)
        or output_dir.is_relative_to(work_dir)
    ):
        raise BuildError(
            "sherpa work and output directories must not contain one another: "
            f"work={work_dir}, output={output_dir}"
        )


def prepare_source(
    source_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> None:
    """Create an exact detached patched source tree."""
    if source_dir.exists():
        _remove_tree(source_dir)
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    if not PATCH_PATH.is_file():
        raise BuildError(f"downstream patch not found: {PATCH_PATH}")

    runner(["git", "init", str(source_dir)])
    # The upstream Android examples contain paths beyond Win32's legacy
    # MAX_PATH limit. Enable Git's Windows long-path support inside this
    # disposable checkout before materializing the pinned tree.
    runner(["git", "config", "core.longpaths", "true"], cwd=source_dir)
    runner(["git", "remote", "add", "origin", UPSTREAM_URL], cwd=source_dir)
    runner(
        ["git", "fetch", "--depth", "1", "origin", UPSTREAM_COMMIT],
        cwd=source_dir,
    )
    runner(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=source_dir)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_dir,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if head != UPSTREAM_COMMIT:
        raise BuildError(
            f"upstream commit mismatch: expected {UPSTREAM_COMMIT}, got {head}"
        )
    runner(["git", "apply", "--check", str(PATCH_PATH)], cwd=source_dir)
    runner(["git", "apply", str(PATCH_PATH)], cwd=source_dir)
    _replace_exact(
        source_dir / "CMakeLists.txt",
        f'set(SHERPA_ONNX_VERSION "{UPSTREAM_VERSION}")',
        f'set(SHERPA_ONNX_VERSION "{DOWNSTREAM_VERSION}")',
    )


def _load_verifier():
    path = REPO_ROOT / "scripts" / "verify-sherpa-binary.py"
    spec = importlib.util.spec_from_file_location("verify_sherpa_binary", path)
    if spec is None or spec.loader is None:
        raise BuildError(f"could not load verifier: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_wheel(
    source_dir: Path,
    python_version: str,
    abi_output_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> Path:
    if python_version not in SUPPORTED_PYTHONS:
        raise BuildError(f"unsupported Python version: {python_version}")
    if abi_output_dir.exists():
        _remove_tree(abi_output_dir)
    abi_output_dir.mkdir(parents=True)

    # Upstream's Windows build extension copies runtime DLLs through this
    # generated staging directory even when SHERPA_ONNX_ENABLE_BINARY=OFF.
    # setup.py normally creates it only when command-line binaries are enabled.
    (source_dir / "build" / "sherpa_onnx" / "bin").mkdir(
        parents=True,
        exist_ok=True,
    )

    env = os.environ.copy()
    env.pop("SHERPA_ONNX_SPLIT_PYTHON_PACKAGE", None)
    env.pop("SHERPA_ONNX_IS_FOR_PYPI", None)
    env["SHERPA_ONNX_CMAKE_ARGS"] = " ".join(CMAKE_ARGS)
    runner(
        [
            "uv",
            "run",
            "--no-project",
            "--isolated",
            "--python",
            python_version,
            "--with",
            BUILD_REQUIREMENTS[0],
            "--with",
            BUILD_REQUIREMENTS[1],
            "--with",
            BUILD_REQUIREMENTS[2],
            "python",
            "setup.py",
            "bdist_wheel",
            "--dist-dir",
            str(abi_output_dir),
        ],
        cwd=source_dir,
        env=env,
    )
    wheels = sorted(abi_output_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise BuildError(
            f"Python {python_version} produced {len(wheels)} wheels, expected one"
        )
    return wheels[0]


def create_manifest(wheels: Sequence[Path]) -> dict[str, object]:
    patch_hash = _sha256(PATCH_PATH)
    return {
        "schema_version": 1,
        "distribution": "sherpa-onnx",
        "version": DOWNSTREAM_VERSION,
        "upstream": {
            "url": UPSTREAM_URL,
            "commit": UPSTREAM_COMMIT,
            "version": UPSTREAM_VERSION,
        },
        "patch": {
            "path": PATCH_PATH.relative_to(REPO_ROOT).as_posix(),
            "sha256": patch_hash,
        },
        "wheels": [
            {
                "filename": wheel.name,
                "size": wheel.stat().st_size,
                "sha256": _sha256(wheel),
            }
            for wheel in sorted(wheels, key=lambda item: item.name)
        ],
    }


def write_manifest(output_dir: Path, wheels: Sequence[Path]) -> Path:
    manifest_path = output_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(create_manifest(wheels), indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest_path


def publish_verified_outputs(
    output_dir: Path,
    wheels: Sequence[tuple[Path, str]],
    *,
    verifier: object,
) -> Path:
    """Re-verify copied wheels and promote a complete output set at once."""
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists() and not output_dir.is_dir():
        raise BuildError(f"output path is not a directory: {output_dir}")

    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}-stage-",
            dir=output_dir.parent,
        )
    ).resolve()
    backup_dir: Path | None = None
    try:
        published: list[Path] = []
        for wheel, abi in wheels:
            destination = staging_dir / wheel.name
            shutil.copy2(wheel, destination)
            if _sha256(destination) != _sha256(wheel):
                raise BuildError(f"wheel hash changed while copying: {wheel.name}")
            verifier.verify_wheel(destination, expected_abi=abi)
            verifier.verify_wheel_runtime(destination, expected_abi=abi)
            published.append(destination)

        manifest_path = write_manifest(staging_dir, published)

        if output_dir.exists():
            backup_dir = output_dir.with_name(
                f".{output_dir.name}-backup-{uuid.uuid4().hex}"
            )
            os.replace(output_dir, backup_dir)
        try:
            os.replace(staging_dir, output_dir)
        except OSError:
            if backup_dir is not None and backup_dir.exists():
                os.replace(backup_dir, output_dir)
                backup_dir = None
            raise

        if backup_dir is not None:
            _remove_tree(backup_dir)
        return output_dir / manifest_path.name
    finally:
        if staging_dir.exists():
            _remove_tree(staging_dir)


def _validate_host() -> None:
    if sys.platform != "win32":
        raise BuildError("sherpa wheel builds are supported only on Windows")
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64"}:
        raise BuildError(f"sherpa wheel builds require Windows x64, got {machine}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build pinned espeak-free sherpa-onnx Windows wheels."
    )
    parser.add_argument(
        "--python",
        dest="pythons",
        action="append",
        choices=SUPPORTED_PYTHONS,
        help="Python version to build (repeatable; defaults to all supported ABIs).",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help=(
            "Short absolute scratch directory. By default a unique directory "
            r"under C:\ws-build is used, with %%TEMP%%\ws-build as fallback."
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    work_dir: Path | None = None
    managed_work_dir = False
    succeeded = False
    try:
        _validate_host()
        pythons = tuple(args.pythons or SUPPORTED_PYTHONS)
        if len(set(pythons)) != len(pythons):
            raise BuildError("each Python version may be requested only once")

        work_dir, managed_work_dir = resolve_work_dir(args.work_dir)
        output_dir = args.output_dir.resolve()
        _require_disjoint_paths(work_dir, output_dir)
        source_dir = work_dir / "source"
        print(f"using short build directory: {work_dir}")
        prepare_source(source_dir)

        verifier = _load_verifier()
        built: list[tuple[Path, str]] = []
        for python_version in pythons:
            abi = f"cp{python_version.replace('.', '')}"
            staged = build_wheel(
                source_dir,
                python_version,
                work_dir / f"wheel-{abi}",
            )
            verifier.verify_wheel(staged, expected_abi=abi)
            verifier.verify_wheel_runtime(staged, expected_abi=abi)
            built.append((staged, abi))

        manifest_path = publish_verified_outputs(
            output_dir,
            built,
            verifier=verifier,
        )
        succeeded = True
        print(f"wrote {manifest_path}")
        for wheel, _abi in built:
            print(f"{_sha256(wheel)}  {wheel.name}")
    except (BuildError, subprocess.CalledProcessError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if managed_work_dir and work_dir is not None:
            if succeeded:
                try:
                    _remove_tree(work_dir)
                except OSError as exc:
                    print(
                        f"WARNING: could not remove scratch directory "
                        f"{work_dir}: {exc}",
                        file=sys.stderr,
                    )
            else:
                print(
                    f"preserved failed build directory: {work_dir}",
                    file=sys.stderr,
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
