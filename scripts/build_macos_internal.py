#!/usr/bin/env python3
"""Build Apple Silicon Whisperstream app and DMG bundles."""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts.macos_local_signing import identity as local_signing_identity
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from macos_local_signing import identity as local_signing_identity

try:
    from scripts.macos_signing import codesign_argv, default_developer_id_provider, load_developer_id
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from macos_signing import codesign_argv, default_developer_id_provider, load_developer_id

# The official installer's Developer ID provider, or None in a source tree.
# main() loads it from --developer-id-provider.
developer_id: Any = None


# The updater provider variables (see scripts/_build_lib.py). A Developer ID
# build that names all of them ships the updater: the builder copies each
# provider's directory into the staged source tree and points the variables
# there, because build.rs and frontend/edition.ts refuse providers outside the
# tree being built. Every other build drops them.
UPDATER_HOOK_ENVS = (
    "WHISPERSTREAM_UPDATER_RS",
    "WHISPERSTREAM_FRONTEND_EDITION",
    "WHISPERSTREAM_UPDATER_POLICY",
    "WHISPERSTREAM_TAURI_OVERLAY",
)
TAURI_OVERLAY_ENV = "WHISPERSTREAM_TAURI_OVERLAY"
# The Tauri updater signing key. Only the update-bundle signer receives it.
UPDATER_SIGNING_ENVS = (
    "TAURI_SIGNING_PRIVATE_KEY",
    "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
)
# Tauri's update bundle for a macOS app, and the feed's target key.
UPDATE_BUNDLE_NAME = "Whisperstream.app.tar.gz"
UPDATE_TARGET = "darwin-aarch64"


def _child_environment(base: Any) -> dict[str, str]:
    """The inherited environment without updater hooks or the updater signing key."""
    dropped = (*UPDATER_HOOK_ENVS, *UPDATER_SIGNING_ENVS)
    return {name: value for name, value in base.items() if name not in dropped}


def use_developer_id_provider(provider: Path | None) -> None:
    """Load the Developer ID provider the build names; None keeps local only."""
    global developer_id
    developer_id = load_developer_id(provider)


def _signing_environment(env: dict[str, str]) -> dict[str, str]:
    """Only signing subprocesses need the owner's login-keychain context."""
    result = dict(env)
    if home := env.get("WHISPERSTREAM_BUILD_SIGNING_HOME"):
        result.update(HOME=home, CFFIXED_USER_HOME=home)
    return result

try:
    from scripts._build_lib import (
        LegalReleaseGateError,
        _validate_eula_policy_resources,
        build_update_manifest,
        copy_to_builds,
        extract_changelog_notes,
        iso_utc_now,
        update_download_base,
        versioned_artifact_url,
    )
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from _build_lib import (
        LegalReleaseGateError,
        _validate_eula_policy_resources,
        build_update_manifest,
        copy_to_builds,
        extract_changelog_notes,
        iso_utc_now,
        update_download_base,
        versioned_artifact_url,
    )

try:
    from scripts.fetch_macos_llama_server import (
        LLAMA_CPP_RELEASE_TAG,
        LLAMA_CPP_SHA256,
        stage_macos_llama_server,
    )
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from fetch_macos_llama_server import (
        LLAMA_CPP_RELEASE_TAG,
        LLAMA_CPP_SHA256,
        stage_macos_llama_server,
    )


REPO_ROOT = Path(__file__).resolve().parents[1]
INTERNAL_IDENTIFIER = "io.lanreal.whisperstream"
TARGET_TRIPLE = "aarch64-apple-darwin"
# The phase 4 packaged probe is internal verification tooling. Source releases
# omit it, so the engine and shell include it only when its sources exist.
PHASE4_PROBE_MODULE = "src/whisperstream/macos_phase4_probe.py"
PHASE4_PROBE_SHELL = "src-tauri/src/macos_phase4_probe.rs"
MINIMUM_MACOS = "14.0"
PYTHON_VERSION = "3.12.8"
NUITKA_VERSION = "4.0.3"
ORDERED_SET_VERSION = "4.1.0"
ZSTANDARD_VERSION = "0.25.0"
TAURI_CLI_VERSION = "2.10.1"
ROLLUP_NATIVE_VERSION = "4.60.2"
ESBUILD_NATIVE_VERSION = "0.25.12"
TAILWIND_OXIDE_VERSION = "4.2.4"
LIGHTNINGCSS_NATIVE_VERSION = "1.32.0"
RUST_TOOLCHAIN = "stable-aarch64-apple-darwin"
RUSTC_VERSION = "1.97.1"
# macOS deletes /tmp files unused for a few days but keeps their directories,
# which leaves Cargo crates marked extracted with no sources and a managed
# Python without its binary. ~/Library/Caches is not swept that way.
DEFAULT_TOOL_ROOT = Path.home() / "Library/Caches/Whisperstream/macos-build-tools"
DEFAULT_BUILD_PARENT = REPO_ROOT / "dist/macos"
SIGNING_LOCAL = "local"
SIGNING_DEVELOPER_ID = "developer-id"
SIGNING_MODES = (SIGNING_LOCAL, SIGNING_DEVELOPER_ID)
# Each signing kind extracts its onefile payload to its own cache so a
# Developer ID engine never reuses payload files signed by the local identity.
ONEFILE_CACHE_NAMES = {
    SIGNING_LOCAL: "Whisperstream-macos-internal",
    SIGNING_DEVELOPER_ID: "Whisperstream-macos-developer-id",
}
BUNDLE_SELECTIONS = {
    "app": ("app",),
    "dmg": ("dmg",),
    "both": ("app", "dmg"),
}

BUILD_PACKAGES = (
    f"nuitka=={NUITKA_VERSION}",
    f"ordered-set=={ORDERED_SET_VERSION}",
    f"zstandard=={ZSTANDARD_VERSION}",
)

SOURCE_PREFIXES = (
    "frontend/",
    "native/macos-stt-worker/",
    "src-tauri/",
    "src/whisperstream/",
)
SOURCE_ROOT_FILES = {
    "LICENSE",
    # Bundled notices (tauri.macos.conf.json resources).
    "NOTICE",
    "LICENSES/Chirp-MIT.txt",
    "README.md",  # pyproject readme; uv sync builds the project
    "pyproject.toml",
    "uv.lock",
    "whisperstream_entry.py",
}
# Frontend prebuild verifies committed documentation and generates legal PDFs.
# Its fingerprinted inputs are staged individually, not the whole website.
FRONTEND_BUILD_SCRIPTS = {
    "scripts/check-offline-docs.mjs",
    "scripts/generate-legal-pdfs.py",
    "scripts/local-test-resources.ts",
}
EXACT_UNTRACKED_BUILD_INPUTS = {"src-tauri/Info.macos-internal.plist"}
EXCLUDED_PREFIXES = (
    "src-tauri/binaries/",
    "src-tauri/gen/",
    "src-tauri/installer/",
    "src/whisperstream/__pycache__/",
    "src/whisperstream/claude_hook/",
)
EXCLUDED_EXACT = {
    "src-tauri/icons/icon.ico",
    "src-tauri/tauri.windows.conf.json",
    "src-tauri/src/windows_engine_adapter.rs",
    "src-tauri/src/windows_process_adapter.rs",
    "src/whisperstream/assets/whisperstream.ico",
    "src/whisperstream/claude_plugin/scripts/forward-hook.ps1",
    "src/whisperstream/audio_capture.py",
    "src/whisperstream/keyboard_shortcuts.py",
    "src/whisperstream/windows_delivery_adapter.py",
    "src/whisperstream/windows_permission_status.py",
    "src/whisperstream/windows_protected_secret.py",
    "src/whisperstream/windows_local_llm_runtime.py",
    "src/whisperstream/windows_candidate_evaluator.py",
}
FORBIDDEN_BUNDLE_SUFFIXES = {".dll", ".exe", ".ico", ".msi", ".nsh", ".pyd"}
FORBIDDEN_CREDENTIAL_ENV = (
    "APPLE_API_ISSUER",
    "APPLE_API_KEY",
    "APPLE_API_KEY_PATH",
    "APPLE_CERTIFICATE",
    "APPLE_CERTIFICATE_PASSWORD",
    "APPLE_SIGNING_IDENTITY",
    "TAURI_SIGNING_PRIVATE_KEY",
    "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
)


class MacInternalBuildError(RuntimeError):
    """A content-safe internal build failure."""


LLAMA_BUNDLE_RPATH = "@loader_path/../Resources/resources/llama-cpp"


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> float:
    started = time.perf_counter()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[build] {log_path.stem} (log: {log_path})", flush=True)
    with log_path.open("wb") as log:
        with subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        ) as process:
            assert process.stdout is not None
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                # read1 forwards partial lines immediately, including progress
                # updates using carriage returns. Keep the log's original bytes.
                while chunk := process.stdout.read1(64 * 1024):
                    log.write(chunk)
                    log.flush()
                    sys.stdout.write(decoder.decode(chunk))
                    sys.stdout.flush()
                sys.stdout.write(decoder.decode(b"", final=True))
                sys.stdout.flush()
                process.wait()
            except BaseException:
                # Do not leave the command running if output forwarding fails
                # or the build is interrupted.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
    elapsed = time.perf_counter() - started
    if process.returncode != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        raise MacInternalBuildError(
            f"command failed exit_code={process.returncode} log={log_path.name}\n{tail}"
        )
    return elapsed


def _output(argv: list[str], *, cwd: Path = REPO_ROOT) -> str:
    process = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.returncode != 0:
        raise MacInternalBuildError(f"read-only command failed exit_code={process.returncode}")
    return process.stdout.strip()


def _assert_arm64_macho(path: Path, label: str) -> None:
    process = subprocess.run(
        ["lipo", "-archs", str(path.resolve())],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.returncode != 0 or process.stdout.split() != ["arm64"]:
        raise MacInternalBuildError(f"{label} is not native arm64")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_measurement(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    files = 0
    total = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        file_hash = _sha256(path)
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(str(size).encode("ascii") + b"\0")
        digest.update(file_hash.encode("ascii") + b"\n")
        files += 1
        total += size
    return {"files": files, "bytes": total, "tree_sha256": digest.hexdigest()}


def _macho_rpaths(path: Path) -> list[str]:
    output = _output(["otool", "-l", str(path)])
    result: list[str] = []
    for block in output.split("cmd LC_RPATH")[1:]:
        match = re.search(r"\n\s*path\s+(\S+)\s+\(offset", block)
        if match:
            result.append(match.group(1))
    return result


def _bind_llama_bundle_rpath(
    source_root: Path, stage: dict[str, object]
) -> dict[str, object]:
    """Make the staged sidecar structurally resolve its packaged dylib set."""

    sidecar = (
        source_root
        / "src-tauri/binaries"
        / f"llama-server-{TARGET_TRIPLE}"
    )
    if not sidecar.is_file():
        raise MacInternalBuildError("staged macOS llama sidecar is missing")
    staged_hash = stage.get("sidecar_sha256")
    if not isinstance(staged_hash, str) or len(staged_hash) != 64:
        raise MacInternalBuildError("staged macOS llama sidecar hash is invalid")
    rpaths = _macho_rpaths(sidecar)
    already_bound = LLAMA_BUNDLE_RPATH in rpaths
    if not already_bound:
        process = subprocess.run(
            [
                "install_name_tool", "-add_rpath", LLAMA_BUNDLE_RPATH,
                str(sidecar),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if process.returncode != 0:
            raise MacInternalBuildError("failed to bind packaged llama library rpath")
        rpaths = _macho_rpaths(sidecar)
    if LLAMA_BUNDLE_RPATH not in rpaths:
        raise MacInternalBuildError("packaged llama library rpath is absent")
    packaged_hash = _sha256(sidecar)
    if not already_bound and packaged_hash == staged_hash:
        raise MacInternalBuildError("packaged llama sidecar was not rewritten")
    if already_bound and packaged_hash != staged_hash:
        raise MacInternalBuildError("bound llama sidecar hash changed unexpectedly")
    upstream_hash = stage.get("upstream_sidecar_sha256", staged_hash)
    return {
        **stage,
        "upstream_sidecar_sha256": upstream_hash,
        "sidecar_sha256": packaged_hash,
        "bundle_rpaths": [LLAMA_BUNDLE_RPATH],
    }


def _assert_native_host() -> None:
    translated = subprocess.run(
        ["sysctl", "-in", "sysctl.proc_translated"],
        capture_output=True,
        text=True,
    )
    translated_value = translated.stdout.strip() if translated.returncode == 0 else "0"
    if platform.system() != "Darwin" or platform.machine() != "arm64" or translated_value != "0":
        raise MacInternalBuildError("internal bundle build requires native Darwin arm64")
    _assert_arm64_macho(Path(sys.executable), "builder Python")


def _assert_non_distribution_environment(*, updater: bool = False) -> None:
    """Refuse inherited distribution credentials.

    An updater build may carry the Tauri updater signing key, which only the
    update-bundle signer receives; it requires the key up front so a missing
    one fails before the expensive build.
    """
    allowed = set(UPDATER_SIGNING_ENVS) if updater else set()
    present = sorted(
        name for name in FORBIDDEN_CREDENTIAL_ENV if os.environ.get(name) and name not in allowed
    )
    if present:
        raise MacInternalBuildError(
            "distribution credential environment is not allowed for internal builds"
        )
    if updater and not os.environ.get("TAURI_SIGNING_PRIVATE_KEY"):
        raise MacInternalBuildError(
            "the updater build signs its update bundle and needs TAURI_SIGNING_PRIVATE_KEY"
        )


def _updater_providers(environ: Any, signing: str) -> dict[str, str] | None:
    """The updater provider variables this build ships, as checkout-relative paths.

    None builds without the updater: no provider was named (a source build),
    or the build is locally signed (an internal build must not replace itself
    from the published feed). A partial provider, or one outside this
    checkout, is a broken official build and fails.
    """
    present = {name: environ[name] for name in UPDATER_HOOK_ENVS if environ.get(name)}
    if not present:
        return None
    if signing != SIGNING_DEVELOPER_ID:
        print("[build] Locally signed build: the updater is not included", flush=True)
        return None
    missing = [name for name in UPDATER_HOOK_ENVS if name not in present]
    if missing:
        raise MacInternalBuildError(f"updater provider is incomplete ({', '.join(missing)} unset)")
    checkout = REPO_ROOT.resolve()
    providers: dict[str, str] = {}
    for name, value in present.items():
        path = Path(value)
        if not path.is_absolute():
            path = checkout / path
        path = path.resolve()
        if not path.is_file() or not path.is_relative_to(checkout):
            raise MacInternalBuildError(f"{name} must name a file inside this checkout")
        providers[name] = path.relative_to(checkout).as_posix()
    return providers


def _stage_updater_providers(source_root: Path, providers: dict[str, str]) -> dict[str, str]:
    """Copy each provider's directory into the staged tree; return the staged variables.

    Providers name sibling files (the native module includes its policy and
    Tauri overlay; the frontend edition's modules sit beside its manifest), so
    the whole directory is staged at the same checkout-relative path.
    """
    for directory in sorted({Path(relative).parent.as_posix() for relative in providers.values()}):
        if directory in {"", "."}:
            raise MacInternalBuildError("updater providers must live in a directory")
        shutil.copytree(
            REPO_ROOT / directory, source_root / directory, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "node_modules"),
        )
    return {name: str(source_root / relative) for name, relative in providers.items()}


def _write_update_bundle(
    app: Path,
    version: str,
    *,
    tauri_cli: Path,
    node_path: str,
    overlay: str,
    env: dict[str, str],
    signing_keys: dict[str, str],
    log_dir: Path,
) -> dict[str, Any]:
    """Pack, sign and describe the finished app as a Tauri macOS update.

    Runs after Developer ID signing, notarization and stapling, so the update
    carries the same app as the DMG. Tauri's own updater artifacts stay off
    for the bundle step because they would pack the app before notarization.
    Writes ``Whisperstream.app.tar.gz``, its ``.sig`` and the ``latest.json``
    feed (one ``darwin-aarch64`` target) beside the app.
    """
    bundle = app.parent / UPDATE_BUNDLE_NAME
    signature = app.parent / f"{UPDATE_BUNDLE_NAME}.sig"
    manifest_path = app.parent / "latest.json"
    for stale in (bundle, signature, manifest_path):
        stale.unlink(missing_ok=True)
    # COPYFILE_DISABLE keeps macOS tar from adding AppleDouble (._*) entries.
    _run(["/usr/bin/tar", "-czf", str(bundle), "-C", str(app.parent), app.name],
         cwd=app.parent, env={**env, "COPYFILE_DISABLE": "1"},
         log_path=log_dir / "update-bundle.log")
    _run([str(tauri_cli), "signer", "sign", str(bundle)],
         # The same CLI and native binding that bundled the app.
         cwd=app.parent, env={**env, "NODE_PATH": node_path, **signing_keys},
         log_path=log_dir / "update-signature.log")
    if not signature.is_file():
        raise MacInternalBuildError("the updater signer wrote no signature")
    try:
        base = update_download_base(REPO_ROOT, {TAURI_OVERLAY_ENV: overlay})
        url = versioned_artifact_url(base, version, UPDATE_BUNDLE_NAME)
    except (LegalReleaseGateError, OSError, ValueError) as exc:
        raise MacInternalBuildError(f"cannot derive the update URL: {exc}") from exc
    changelog = REPO_ROOT / "CHANGELOG.md"
    notes = (
        extract_changelog_notes(version, changelog.read_text(encoding="utf-8"), platform="mac")
        if changelog.is_file() else None
    )
    manifest = build_update_manifest(
        version, iso_utc_now(), notes,
        {UPDATE_TARGET: {"signature": signature.read_text(encoding="utf-8"), "url": url}},
    )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "target": UPDATE_TARGET,
        "url": url,
        "bundle": bundle,
        "signature": signature,
        "manifest": manifest_path,
    }


def _assert_internal_legal_resources(checkout: Path = REPO_ROOT) -> None:
    """Fail before staging when the packaged EULA identities do not agree."""

    try:
        _validate_eula_policy_resources(checkout)
    except LegalReleaseGateError as exc:
        raise MacInternalBuildError(
            "internal EULA resource identity is invalid"
        ) from exc


def _assert_tauri_api_compatibility(checkout: Path = REPO_ROOT) -> None:
    """Reject locked or installed API drift before the expensive native build."""
    cargo_lock = (checkout / "src-tauri/Cargo.lock").read_text(encoding="utf-8")
    rust = re.search(r'(?m)^name = "tauri"\nversion = "([^"]+)"$', cargo_lock)
    npm_lock = json.loads((checkout / "frontend/package-lock.json").read_text(encoding="utf-8"))
    api = npm_lock["packages"]["node_modules/@tauri-apps/api"]["version"]
    versions = [api]
    installed = checkout / "frontend/node_modules/@tauri-apps/api/package.json"
    if installed.is_file():
        versions.append(json.loads(installed.read_text(encoding="utf-8"))["version"])
    if rust is None or any(v.split(".")[:2] != rust.group(1).split(".")[:2] for v in versions):
        raise MacInternalBuildError(
            "Tauri API and Rust major/minor versions differ; align the frontend lockfile "
            "with Cargo.lock and run npm ci --prefix frontend"
        )


def _validate_new_build_root(build_root: Path) -> Path:
    root = build_root.resolve()
    if root.exists():
        raise MacInternalBuildError("build root already exists; choose a fresh path")
    root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def _replace_build_output(staged: Path, destination: Path) -> None:
    """Replace one completed output directory, restoring it if promotion fails."""
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise MacInternalBuildError("build output must be a directory, not a file or symlink")
    previous = staged.with_name(staged.name + "-previous")
    had_previous = destination.exists()
    if had_previous:
        destination.rename(previous)
    try:
        staged.rename(destination)
    except BaseException:
        if had_previous:
            previous.rename(destination)
        raise
    if had_previous:
        shutil.rmtree(previous)


# Artifacts the default build keeps in dist/macos/latest, by report key.
OUTPUT_NAMES = (
    ("app", "Whisperstream.app"),
    ("dmg", "Whisperstream.dmg"),
    ("update_bundle", UPDATE_BUNDLE_NAME),
    ("update_signature", f"{UPDATE_BUNDLE_NAME}.sig"),
    ("update_manifest", "latest.json"),
)


def _build_default(tool_root: Path, *, bundles: str, **signing: Any) -> dict[str, Any]:
    """Keep one successful app/DMG and dispose of intermediate build trees."""
    import fcntl  # The default packaged builder runs only on native macOS.

    parent = DEFAULT_BUILD_PARENT.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    with (parent / ".build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MacInternalBuildError("another macOS build is already running") from exc
        with tempfile.TemporaryDirectory(prefix=".build-", dir=parent) as temporary:
            scratch = Path(temporary)
            root = scratch / "work"
            destination = parent / "latest"
            try:
                result = build(root, tool_root, bundles=bundles, **signing)
                staged = scratch / "output"
                staged.mkdir()
                for kind, filename in OUTPUT_NAMES:
                    artifact = result["artifacts"].get(kind)
                    if artifact is not None:
                        Path(artifact["path"]).rename(staged / filename)
                        artifact["path"] = str(destination / filename)
                (root / "logs").rename(staged / "logs")
                (staged / "build-report.json").write_text(
                    json.dumps(result, indent=2) + "\n", encoding="utf-8"
                )
                _replace_build_output(staged, destination)
            except BaseException:
                # Keep one failed run's diagnostics, not its partial installer
                # or compilation tree. The last successful output stays put.
                logs = root / "logs"
                if not logs.is_dir():
                    logs = scratch / "output/logs"
                if logs.is_dir():
                    failed_logs = parent / "last-failed-logs"
                    try:
                        _replace_build_output(logs, failed_logs)
                        print(f"[build] Failure logs: {failed_logs}", file=sys.stderr, flush=True)
                    except (OSError, MacInternalBuildError):
                        print("[build] Could not retain failure logs; see terminal output.", file=sys.stderr)
                raise
    print(f"[build] Output: {destination}", flush=True)
    return result


def _copy_dmg_to_builds(result: dict[str, Any]) -> Path | None:
    """Copy the finished DMG to builds/mac; locally signed builds say so in the name."""
    dmg = result["artifacts"].get("dmg")
    if dmg is None:
        return None
    version = result["identity"]["version"]
    suffix = "" if result["signing"]["kind"] == "developer-id" else "-local-signed"
    return copy_to_builds(dmg["path"], "mac", f"Whisperstream_{version}_aarch64{suffix}.dmg")


def _selected_source_paths() -> list[str]:
    tracked = set(_output(["git", "ls-files"]).splitlines())
    for relative in EXACT_UNTRACKED_BUILD_INPUTS:
        if (REPO_ROOT / relative).is_file():
            tracked.add(relative)

    manifest = json.loads(
        (REPO_ROOT / "frontend/src/assets/documentation/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    frontend_inputs = FRONTEND_BUILD_SCRIPTS | set(manifest["inputs"])
    if any(Path(relative).is_absolute() or ".." in Path(relative).parts for relative in frontend_inputs):
        raise MacInternalBuildError("unsafe frontend build input path")
    selected: list[str] = []
    for relative in sorted(tracked):
        if relative not in SOURCE_ROOT_FILES | frontend_inputs and not relative.startswith(SOURCE_PREFIXES):
            continue
        if relative in EXCLUDED_EXACT or relative.startswith(EXCLUDED_PREFIXES):
            continue
        suffix = Path(relative).suffix.lower()
        if suffix in FORBIDDEN_BUNDLE_SUFFIXES or suffix == ".ps1":
            continue
        source = REPO_ROOT / relative
        if source.is_file():
            selected.append(relative)
    required = SOURCE_ROOT_FILES | EXACT_UNTRACKED_BUILD_INPUTS | frontend_inputs
    missing = sorted(relative for relative in required if relative not in selected)
    if missing:
        raise MacInternalBuildError(f"required staged inputs missing count={len(missing)}")
    return selected


def _stage_sources(source_root: Path, version: str) -> dict[str, Any]:
    for relative in _selected_source_paths():
        source = REPO_ROOT / relative
        destination = source_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)

    version_path = source_root / "src/whisperstream/_version.py"
    version_path.write_text(
        f'"""Build-time version sentinel for the internal macOS bundle."""\n\n__version__ = "{version}"\n',
        encoding="utf-8",
    )

    forbidden = [
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix.lower() in FORBIDDEN_BUNDLE_SUFFIXES
    ]
    if forbidden:
        raise MacInternalBuildError("forbidden platform artifacts entered source stage")
    return _tree_measurement(source_root)


def _managed_python(tool_root: Path, env: dict[str, str], log_dir: Path) -> Path:
    install_root = tool_root / "python"
    python = install_root / f"cpython-{PYTHON_VERSION}-macos-aarch64-none/bin/python3.12"

    def usable() -> bool:
        if not python.is_file():
            return False
        try:
            return _output([
                str(python), "-I", "-c",
                "import ssl, ctypes, venv; import sys; print(sys.version.split()[0])",
            ]) == PYTHON_VERSION
        except (OSError, MacInternalBuildError):
            return False

    if not usable():
        install_env = dict(env)
        install_env["UV_PYTHON_INSTALL_DIR"] = str(install_root)
        # A temporary tool cache can retain its directory/symlinks after its
        # files disappear. Ordinary install treats that as already installed.
        # Repair only this pinned toolchain, without adding user-level commands.
        _run(
            ["uv", "python", "install", "--reinstall", "--no-bin", PYTHON_VERSION],
            cwd=REPO_ROOT,
            env=install_env,
            log_path=log_dir / "python-install.log",
        )
    if not usable():
        raise MacInternalBuildError("exact managed Python toolchain is unavailable")
    if platform.machine() != "arm64":
        raise MacInternalBuildError("managed Python toolchain identity mismatch")
    _assert_arm64_macho(python, "managed Python toolchain")
    return python


def _engine_environment(
    build_root: Path,
    tool_root: Path,
    source_root: Path,
    env: dict[str, str],
    log_dir: Path,
) -> tuple[Path, dict[str, str]]:
    managed_python = _managed_python(tool_root, env, log_dir)
    venv = build_root / "engine-venv"
    _run(
        ["uv", "venv", str(venv), "--python", str(managed_python)],
        cwd=REPO_ROOT,
        env=env,
        log_path=log_dir / "engine-venv.log",
    )
    engine_python = venv / "bin/python"
    dependency_env = dict(env)
    dependency_env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    _run(
        ["uv", "sync", "--locked", "--no-dev", "--project", str(source_root),
         "--python", str(managed_python)],
        cwd=source_root, env=dependency_env,
        log_path=log_dir / "engine-dependencies.log",
    )
    _run(
        ["uv", "pip", "install", "--python", str(engine_python), *BUILD_PACKAGES],
        cwd=REPO_ROOT, env=env, log_path=log_dir / "engine-build-tools.log",
    )
    _run(
        [str(engine_python), str(REPO_ROOT / "scripts/check-sherpa-source.py")],
        cwd=REPO_ROOT, env=env, log_path=log_dir / "macos-sherpa.log",
    )
    engine_env = dict(env)
    engine_env["PYTHONPATH"] = str(source_root / "src")
    return engine_python, engine_env


def _nuitka_signing_args(signing_kind: str) -> list[str]:
    if signing_kind != SIGNING_DEVELOPER_ID:
        return []
    return developer_id.nuitka_args()


def _build_engine(
    build_root: Path,
    tool_root: Path,
    source_root: Path,
    env: dict[str, str],
    log_dir: Path,
    version: str,
) -> tuple[Path, float]:
    engine_python, engine_env = _engine_environment(
        build_root, tool_root, source_root, env, log_dir
    )
    # Import the actual backend and exercise the dynamically selected fuzzy
    # matcher before spending time compiling. This does not start the app.
    _run(
        [str(engine_python), "-c", (
            "import whisperstream.main; from metaphone import doublemetaphone; "
            "from rapidfuzz import process; from rapidfuzz.distance import Levenshtein; "
            "assert doublemetaphone('synthetic'); "
            "assert process.extractOne('synthetic', ['synthetic'])[1] == 100; "
            "assert Levenshtein.distance('synthetic', 'synthetic') == 0"
        )],
        cwd=source_root, env=engine_env,
        log_path=log_dir / "engine-import-smoke.log",
    )
    output_dir = build_root / "engine-output"
    output_dir.mkdir()
    signing_kind = env.get("WHISPERSTREAM_BUILD_SIGNING_KIND", SIGNING_LOCAL)
    argv = [
        str(engine_python),
        "-m",
        "nuitka",
        "--mode=onefile",
        "--static-libpython=no",
        f"--output-dir={output_dir}",
        "--output-filename=whisperstream-engine",
        "--macos-target-arch=arm64",
        f"--macos-sign-identity={env['WHISPERSTREAM_BUILD_SIGNING_IDENTITY']}",
        "--disable-cache=ccache",
        f"--onefile-tempdir-spec={{CACHE_DIR}}/{ONEFILE_CACHE_NAMES[signing_kind]}/{version}",
        "--onefile-cache-mode=cached",
        "--nofollow-import-to=unittest",
        "--nofollow-import-to=pyperclip",
        "--nofollow-import-to=pynput",
        # Optional dev-tools routes; a no-op when the checkout omits them.
        "--nofollow-import-to=whisperstream.dev_endpoints",
        "--nofollow-import-to=whisperstream.audio_capture",
        "--nofollow-import-to=whisperstream.keyboard_shortcuts",
        "--nofollow-import-to=whisperstream.windows_delivery_adapter",
        "--nofollow-import-to=whisperstream.windows_tts_playback",
        # The frozen engine also serves as the supervised sherpa child for
        # English Parakeet, Qwen and Cohere. Inclusion preserves their lazy,
        # child-only imports; excluding them breaks every non-FluidAudio model.
        "--include-module=whisperstream.stt_worker_parakeet",
        "--include-module=whisperstream.stt_worker_qwen",
        "--include-module=whisperstream.stt_worker_cohere",
        "--include-module=whisperstream.stt_worker_speech_gate",
        "--include-module=whisperstream.speech_gate",
        "--nofollow-import-to=whisperstream.windows_permission_status",
        "--nofollow-import-to=whisperstream.windows_protected_secret",
        "--include-module=whisperstream.macos_keychain_probe",
        "--include-module=whisperstream.macos_local_llm_runtime",
        "--include-module=whisperstream.macos_app_context",
        "--include-module=whisperstream.macos_agent_integration",
        "--include-module=whisperstream.macos_audio_policy",
        "--include-module=whisperstream.macos_capture",
        "--include-module=whisperstream.macos_delivery_adapter",
        "--include-module=whisperstream.macos_native_query_owner",
        "--include-module=whisperstream.macos_permission_actions",
        "--include-module=whisperstream.macos_permission_status",
        "--include-module=whisperstream.macos_shortcuts",
        "--include-module=whisperstream.macos_protected_storage_probe",
        *(
            ("--include-module=whisperstream.macos_phase4_probe",)
            if (source_root / PHASE4_PROBE_MODULE).is_file()
            else ()
        ),
        "--include-module=whisperstream.macos_protected_secret",
        "--include-module=whisperstream.macos_stt_manager",
        "--include-module=whisperstream.macos_tts_playback",
        "--include-module=whisperstream.permission_actions",
        "--include-module=whisperstream.speech.kokoro_engine",
        "--include-module=whisperstream.speech.legacy_kokoro_engine",
        "--include-module=whisperstream.speech.kokoro_worker",
        "--include-module=whisperstream.speech.kokoro_frontend",
        "--include-module=sounddevice",
        "--include-package=_sounddevice_data",
        "--include-package-data=_sounddevice_data",
        "--include-package-data=whisperstream",
        "--include-package=anyio",
        "--include-package=cryptography",
        "--include-package=fastapi",
        "--include-package=metaphone",
        "--include-package=rapidfuzz",
        "--include-package=pygame",
        "--include-package-data=pygame",
        "--include-package=starlette",
        "--include-package=sherpa_onnx",
        "--include-package-data=sherpa_onnx",
        "--include-distribution-metadata=sherpa_onnx",
        "--include-package=truststore",
        "--include-package=uvicorn",
        "--include-package=websockets",
        f"--report={output_dir / 'compilation-report.xml'}",
        *_nuitka_signing_args(signing_kind),
        str(source_root / "whisperstream_entry.py"),
    ]
    elapsed = _run(
        argv,
        cwd=source_root,
        env=_signing_environment(engine_env),
        log_path=log_dir / "engine-build.log",
    )
    binary = output_dir / "whisperstream-engine"
    if not binary.is_file():
        raise MacInternalBuildError("Nuitka engine output is missing")
    # A successful compiler exit alone does not prove frozen import closure.
    frozen_env = dict(engine_env)
    frozen_env.pop("PYTHONPATH", None)
    _run(
        [str(binary), "--help"], cwd=output_dir, env=frozen_env,
        log_path=log_dir / "engine-help-smoke.log",
    )
    return binary, elapsed


def _build_worker(
    build_root: Path,
    tool_root: Path,
    source_root: Path,
    env: dict[str, str],
    log_dir: Path,
) -> tuple[Path, float]:
    target_dir = build_root / "worker-target"
    worker_env = dict(env)
    toolchain_bin = Path(
        _output(["rustup", "which", "cargo", "--toolchain", RUST_TOOLCHAIN])
    ).resolve().parent
    rustc_details = _output([str(toolchain_bin / "rustc"), "-Vv"])
    if f"release: {RUSTC_VERSION}" not in rustc_details:
        raise MacInternalBuildError("installed Rust toolchain version mismatch")
    if "host: aarch64-apple-darwin" not in rustc_details:
        raise MacInternalBuildError("installed Rust toolchain is not native arm64")
    _assert_arm64_macho(toolchain_bin / "cargo", "Cargo toolchain")
    _assert_arm64_macho(toolchain_bin / "rustc", "Rust compiler")
    worker_env["CARGO_HOME"] = str(tool_root / "cargo-home")
    worker_env["CARGO_TARGET_DIR"] = str(target_dir)
    worker_env["MACOSX_DEPLOYMENT_TARGET"] = MINIMUM_MACOS
    worker_env["PATH"] = os.pathsep.join(
        [str(toolchain_bin), worker_env.get("PATH", "")]
    )
    elapsed = _run(
        [
            str(toolchain_bin / "cargo"),
            "build",
            "--locked",
            "--release",
            "--target",
            TARGET_TRIPLE,
            "--manifest-path",
            str(source_root / "native/macos-stt-worker/Cargo.toml"),
        ],
        cwd=source_root,
        env=worker_env,
        log_path=log_dir / "worker-build.log",
    )
    binary = target_dir / TARGET_TRIPLE / "release/whisperstream-macos-stt-worker"
    if not binary.is_file():
        raise MacInternalBuildError("native FluidAudio worker output is missing")
    return binary, elapsed


def _provision_node_bindings(tool_root: Path, env: dict[str, str], log_dir: Path) -> Path:
    prefix = tool_root / "tauri-native"
    tauri_package = prefix / "node_modules/@tauri-apps/cli-darwin-arm64/package.json"
    rollup_package = prefix / "node_modules/@rollup/rollup-darwin-arm64/package.json"
    esbuild_package = prefix / "node_modules/@esbuild/darwin-arm64/package.json"
    oxide_package = prefix / "node_modules/@tailwindcss/oxide-darwin-arm64/package.json"
    lightningcss_package = prefix / "node_modules/lightningcss-darwin-arm64/package.json"
    if not all(
        package.is_file()
        for package in (
            tauri_package,
            rollup_package,
            esbuild_package,
            oxide_package,
            lightningcss_package,
        )
    ):
        npm_env = dict(env)
        npm_env["npm_config_cache"] = str(tool_root / "npm-cache")
        _run(
            [
                "npm",
                "install",
                "--prefix",
                str(prefix),
                "--no-save",
                "--package-lock=false",
                f"@tauri-apps/cli-darwin-arm64@{TAURI_CLI_VERSION}",
                f"@rollup/rollup-darwin-arm64@{ROLLUP_NATIVE_VERSION}",
                f"@esbuild/darwin-arm64@{ESBUILD_NATIVE_VERSION}",
                f"@tailwindcss/oxide-darwin-arm64@{TAILWIND_OXIDE_VERSION}",
                f"lightningcss-darwin-arm64@{LIGHTNINGCSS_NATIVE_VERSION}",
            ],
            cwd=REPO_ROOT,
            env=npm_env,
            log_path=log_dir / "tauri-binding-install.log",
        )
    tauri_metadata = json.loads(tauri_package.read_text(encoding="utf-8"))
    rollup_metadata = json.loads(rollup_package.read_text(encoding="utf-8"))
    esbuild_metadata = json.loads(esbuild_package.read_text(encoding="utf-8"))
    oxide_metadata = json.loads(oxide_package.read_text(encoding="utf-8"))
    lightningcss_metadata = json.loads(lightningcss_package.read_text(encoding="utf-8"))
    if tauri_metadata.get("version") != TAURI_CLI_VERSION:
        raise MacInternalBuildError("Tauri native binding version mismatch")
    if rollup_metadata.get("version") != ROLLUP_NATIVE_VERSION:
        raise MacInternalBuildError("Rollup native binding version mismatch")
    if esbuild_metadata.get("version") != ESBUILD_NATIVE_VERSION:
        raise MacInternalBuildError("esbuild native binding version mismatch")
    if oxide_metadata.get("version") != TAILWIND_OXIDE_VERSION:
        raise MacInternalBuildError("Tailwind oxide binding version mismatch")
    if lightningcss_metadata.get("version") != LIGHTNINGCSS_NATIVE_VERSION:
        raise MacInternalBuildError("lightningcss native binding version mismatch")
    native_payloads = []
    for path in sorted((prefix / "node_modules").rglob("*")):
        if not path.is_file():
            continue
        description = _output(["file", "-b", str(path)])
        if "Mach-O" in description:
            _assert_arm64_macho(path, "frontend native binding")
            native_payloads.append(path)
    if len(native_payloads) < 5:
        raise MacInternalBuildError("frontend native binding inventory is incomplete")
    return prefix / "node_modules"


def _tauri_build_argv(
    tauri_cli: Path,
    bundles: str,
    signing_identity: str = "-",
    *,
    entitlements: Path | None = None,
    overlay: str | None = None,
) -> list[str]:
    macos: dict[str, Any] = {"signingIdentity": signing_identity}
    if entitlements is not None:
        # Developer ID: Tauri signs the app and each externalBin sidecar with
        # the hardened runtime and this single reviewed entitlements file.
        macos.update(hardenedRuntime=True, entitlements=str(entitlements))
    argv = [
        str(tauri_cli),
        "build",
        "--target",
        TARGET_TRIPLE,
        "--bundles",
        ",".join(BUNDLE_SELECTIONS[bundles]),
        *(
            ("--features", "macos-phase4-probe")
            if (REPO_ROOT / PHASE4_PROBE_SHELL).is_file()
            else ()
        ),
        # The updater build's Tauri overlay (endpoint and public key) merges
        # before the builder's own settings, which keep Tauri's updater
        # artifacts off: the update bundle is made after notarization.
        *(("--config", overlay) if overlay is not None else ()),
        "--config",
        json.dumps({
            "build": {"beforeBuildCommand": ""},
            "bundle": {"createUpdaterArtifacts": False, "macOS": macos, "resources": [
                *json.loads((REPO_ROOT / "src-tauri/tauri.macos.conf.json").read_text())["bundle"]["resources"],
                "bin/whisperstream",
            ]},
        }),
    ]
    if "dmg" in BUNDLE_SELECTIONS[bundles]:
        # The builder is deliberately unattended. Tauri's CI mode keeps
        # create-dmg from asking Finder to write cosmetic metadata, which
        # would otherwise require a live Automation grant and can leave a
        # writable intermediate image behind when nobody is present.
        argv.append("--ci")
    return argv


def _staged_signing_argv(
    env: dict[str, str], path: Path, directory: str, identifier: str
) -> list[str]:
    identity = env["WHISPERSTREAM_BUILD_SIGNING_IDENTITY"]
    if env.get("WHISPERSTREAM_BUILD_SIGNING_KIND") != SIGNING_DEVELOPER_ID:
        return codesign_argv(identity, path, identifier=identifier,
                             runtime=False, timestamp=False)
    return developer_id.staged_signing_argv(identity, path, directory, identifier)


def _sign_staged_binaries(source_root: Path, env: dict[str, str], log_dir: Path) -> None:
    """Sign copied native resources too, including the managed CLI and dylibs."""
    native_root = source_root / "src-tauri"
    for directory in ("binaries", "bin", "resources/llama-cpp"):
        for path in sorted((native_root / directory).rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            with path.open("rb") as stream:
                if stream.read(4) != b"\xcf\xfa\xed\xfe":
                    continue  # The builder already enforces thin arm64 inputs.
            relative = path.relative_to(native_root).as_posix().replace("/", "-")
            _run(
                _staged_signing_argv(env, path, directory, f"{INTERNAL_IDENTIFIER}.{relative}"),
                cwd=source_root, env=_signing_environment(env),
                log_path=log_dir / f"sign-{relative}.log",
            )


def _build_frontend_and_app(
    build_root: Path,
    tool_root: Path,
    source_root: Path,
    env: dict[str, str],
    log_dir: Path,
    bundles: str,
) -> tuple[Path, Path | None, dict[str, Any], float, float]:
    overlay = env.get(TAURI_OVERLAY_ENV)
    frontend = source_root / "frontend"
    node_modules = frontend / "node_modules"
    # Official builds run in a fresh staging checkout with no packages yet;
    # install them from the lockfile, as the Windows builder does.
    install_env = dict(env)
    install_env["npm_config_cache"] = str(tool_root / "npm-cache")
    _run(["npm", "ci"], cwd=REPO_ROOT / "frontend", env=install_env,
         log_path=log_dir / "frontend-install.log")
    node_modules.symlink_to(REPO_ROOT / "frontend/node_modules", target_is_directory=True)
    native_node_modules = _provision_node_bindings(tool_root, env, log_dir)
    toolchain_bin = Path(
        _output(["rustup", "which", "cargo", "--toolchain", RUST_TOOLCHAIN])
    ).resolve().parent
    node_env = dict(env)
    node_env["NODE_OPTIONS"] = "--no-experimental-webstorage"
    node_env["NODE_PATH"] = os.pathsep.join(
        [str(native_node_modules), str(REPO_ROOT / "frontend/node_modules")]
    )
    node_env["npm_config_cache"] = str(tool_root / "npm-cache")

    try:
        frontend_elapsed = _run(
            ["npm", "run", "build", "--prefix", "frontend"],
            cwd=source_root,
            env=node_env,
            log_path=log_dir / "frontend-build.log",
        )
        frontend_measurement = _tree_measurement(frontend / "dist")

        tauri_env = dict(node_env)
        tauri_env["CARGO_HOME"] = str(tool_root / "cargo-home")
        tauri_env["CARGO_TARGET_DIR"] = str(build_root / "tauri-target")
        tauri_env["MACOSX_DEPLOYMENT_TARGET"] = MINIMUM_MACOS
        # Tauri performs its own rustup target-presence query. Point that
        # read-only query at the already-installed toolchain metadata even
        # though HOME and Cargo's download cache remain phase-isolated.
        tauri_env["RUSTUP_HOME"] = _output(["rustup", "show", "home"])
        tauri_env["RUSTUP_TOOLCHAIN"] = RUST_TOOLCHAIN
        tauri_env["PATH"] = os.pathsep.join(
            [str(toolchain_bin), tauri_env.get("PATH", "")]
        )
        _run(
            ["cargo", "build", "--release", "--locked", "--target", TARGET_TRIPLE,
             "--bin", "whisperstream-cli"],
            cwd=source_root / "src-tauri", env=tauri_env,
            log_path=log_dir / "managed-cli-build.log",
        )
        cli_dir = source_root / "src-tauri" / "bin"
        cli_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build_root / "tauri-target" / TARGET_TRIPLE / "release" / "whisperstream-cli",
                     cli_dir / "whisperstream")
        _sign_staged_binaries(source_root, env, log_dir)
        if "dmg" in BUNDLE_SELECTIONS[bundles]:
            # tauri-cli 2.10.1's --ci option does not propagate to the embedded
            # create-dmg process. That bundler reads CI directly when deciding
            # whether to skip its permission-dependent Finder AppleScript.
            tauri_env["CI"] = "true"
        tauri_cli = REPO_ROOT / "frontend/node_modules/.bin/tauri"
        app_elapsed = _run(
            _tauri_build_argv(
                tauri_cli,
                bundles,
                env["WHISPERSTREAM_BUILD_SIGNING_IDENTITY"],
                entitlements=(
                    developer_id.ENTITLEMENTS_PATH
                    if env.get("WHISPERSTREAM_BUILD_SIGNING_KIND") == SIGNING_DEVELOPER_ID
                    else None
                ),
                overlay=overlay,
            ),
            cwd=source_root,
            env=_signing_environment(tauri_env),
            log_path=log_dir / "tauri-build.log",
        )
    finally:
        node_modules.unlink(missing_ok=True)

    apps = list((build_root / "tauri-target").rglob("Whisperstream.app"))
    if len(apps) != 1:
        raise MacInternalBuildError(f"expected one app bundle, found count={len(apps)}")
    dmgs = list((build_root / "tauri-target").rglob("*.dmg"))
    expects_dmg = "dmg" in BUNDLE_SELECTIONS[bundles]
    if expects_dmg and len(dmgs) != 1:
        raise MacInternalBuildError(f"expected one DMG bundle, found count={len(dmgs)}")
    if not expects_dmg and dmgs:
        raise MacInternalBuildError("unexpected DMG bundle was produced")
    return (
        apps[0],
        dmgs[0] if dmgs else None,
        frontend_measurement,
        frontend_elapsed,
        app_elapsed,
    )


def _stage_external_binaries(source_root: Path, engine: Path, worker: Path) -> None:
    binaries = source_root / "src-tauri/binaries"
    binaries.mkdir(parents=True, exist_ok=True)
    destinations = (
        (engine, binaries / f"whisperstream-engine-{TARGET_TRIPLE}"),
        (worker, binaries / f"whisperstream-macos-stt-worker-{TARGET_TRIPLE}"),
    )
    for source, destination in destinations:
        shutil.copy2(source, destination)
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR)


def _load_version(source_root: Path) -> str:
    config = json.loads((source_root / "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    version = config.get("version")
    if not isinstance(version, str) or not version:
        raise MacInternalBuildError("Tauri version is missing")
    return version


def _resolve_signing(
    signing: str, team_id: str | None, signing_sha1: str | None
) -> tuple[str, Any]:
    """Return the codesign identity, failing before any expensive work."""
    if signing == SIGNING_LOCAL:
        if team_id is not None or signing_sha1 is not None:
            raise MacInternalBuildError("--team-id and --signing-sha1 require --signing developer-id")
        try:
            return local_signing_identity(), None
        except RuntimeError as exc:
            raise MacInternalBuildError(str(exc)) from exc
    if developer_id is None:
        raise MacInternalBuildError(
            "--signing developer-id needs the official installer's Developer ID "
            "provider (--developer-id-provider), which this build was not given; "
            "use --signing local"
        )
    try:
        identity = developer_id.resolve(team_id, signing_sha1)
    except developer_id.DeveloperIdError as exc:
        raise MacInternalBuildError(str(exc)) from exc
    return identity.sha1, identity


def _macho_files(root: Path) -> list[Path]:
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) in {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}:
                result.append(path)
    return result


def _finalize_developer_id(
    app: Path,
    dmg: Path | None,
    identity: Any,
    env: dict[str, str],
    log_dir: Path,
) -> dict[str, Any]:
    try:
        return developer_id.finalize(
            app, dmg, identity,
            macho_paths=_macho_files(app),
            run_logged=_run,
            env=env,
            signing_env=_signing_environment(env),
            log_dir=log_dir,
        )
    except developer_id.DeveloperIdError as exc:
        raise MacInternalBuildError(str(exc)) from exc


def build(
    build_root: Path,
    tool_root: Path,
    *,
    bundles: str = "app",
    signing: str = SIGNING_LOCAL,
    team_id: str | None = None,
    signing_sha1: str | None = None,
) -> dict[str, Any]:
    if bundles not in BUNDLE_SELECTIONS:
        raise MacInternalBuildError("invalid internal bundle selection")
    if signing not in SIGNING_MODES:
        raise MacInternalBuildError("invalid signing mode")
    _assert_native_host()
    providers = _updater_providers(os.environ, signing)
    _assert_non_distribution_environment(updater=providers is not None)
    signing_keys = {
        name: os.environ[name] for name in UPDATER_SIGNING_ENVS
        if providers is not None and os.environ.get(name)
    }
    signing_identity, developer_identity = _resolve_signing(signing, team_id, signing_sha1)
    signing_home = str(Path.home())
    _assert_internal_legal_resources()
    _assert_tauri_api_compatibility()
    root = _validate_new_build_root(build_root)
    tool_root = tool_root.resolve()
    tool_root.mkdir(parents=True, exist_ok=True)
    os.chmod(tool_root, 0o700)
    source_root = root / "source"
    log_dir = root / "logs"
    home = root / "home"
    data = root / "data"
    for directory in (source_root, log_dir, home, data):
        directory.mkdir(mode=0o700)

    env = _child_environment(os.environ)
    env.update(
        {
            "CFFIXED_USER_HOME": str(home),
            "HOME": str(home),
            "MACOSX_DEPLOYMENT_TARGET": MINIMUM_MACOS,
            "SOURCE_DATE_EPOCH": _output(["git", "log", "-1", "--format=%ct"]),
            "UV_CACHE_DIR": str(tool_root / "uv-cache"),
            "WHISPERSTREAM_DATA_DIR": str(data),
            "WHISPERSTREAM_BUILD_SIGNING_IDENTITY": signing_identity,
            "WHISPERSTREAM_BUILD_SIGNING_HOME": signing_home,
            "WHISPERSTREAM_BUILD_SIGNING_KIND": signing,
        }
    )
    if developer_identity is not None:
        env["WHISPERSTREAM_DEVELOPER_ID_ENTITLEMENTS"] = str(developer_id.ENTITLEMENTS_PATH)

    started = time.perf_counter()
    version = json.loads((REPO_ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))["version"]
    source_measurement = _stage_sources(source_root, version)
    if providers is not None:
        env.update(_stage_updater_providers(source_root, providers))
    llama_stage = stage_macos_llama_server(
        source_root,
        cache_root=tool_root / "llama-cpp",
    )
    llama_stage = _bind_llama_bundle_rpath(source_root, llama_stage)
    source_measurement = _tree_measurement(source_root)
    engine, engine_elapsed = _build_engine(
        root, tool_root, source_root, env, log_dir, version
    )
    worker, worker_elapsed = _build_worker(root, tool_root, source_root, env, log_dir)
    _stage_external_binaries(source_root, engine, worker)
    app, dmg, frontend_measurement, frontend_elapsed, app_elapsed = _build_frontend_and_app(
        root, tool_root, source_root, env, log_dir, bundles
    )
    total_elapsed = time.perf_counter() - started

    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if plist.get("CFBundleIdentifier") != INTERNAL_IDENTIFIER:
        raise MacInternalBuildError("built app identifier mismatch")
    if plist.get("CFBundleShortVersionString") != version:
        raise MacInternalBuildError("built app version mismatch")
    developer_id_result: dict[str, Any] | None = None
    if developer_identity is not None:
        developer_id_result = _finalize_developer_id(app, dmg, developer_identity, env, log_dir)
        total_elapsed = time.perf_counter() - started
    update: dict[str, Any] | None = None
    if providers is not None:
        update = _write_update_bundle(
            app, version,
            tauri_cli=REPO_ROOT / "frontend/node_modules/.bin/tauri",
            node_path=os.pathsep.join([
                str(tool_root / "tauri-native/node_modules"), str(REPO_ROOT / "frontend/node_modules"),
            ]),
            overlay=str(REPO_ROOT / providers[TAURI_OVERLAY_ENV]),
            env=env, signing_keys=signing_keys, log_dir=log_dir,
        )
        total_elapsed = time.perf_counter() - started

    result = {
        "schema_version": 1,
        "signing": _signing_report(signing_identity, developer_identity, developer_id_result),
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "identity": {
            "identifier": INTERNAL_IDENTIFIER,
            "version": version,
            "target": TARGET_TRIPLE,
            "minimum_macos": MINIMUM_MACOS,
        },
        "dependency_pins": {
            "python": PYTHON_VERSION,
            "nuitka": NUITKA_VERSION,
            "ordered_set": ORDERED_SET_VERSION,
            "zstandard": ZSTANDARD_VERSION,
            "tauri_cli": TAURI_CLI_VERSION,
            "rollup_native": ROLLUP_NATIVE_VERSION,
            "esbuild_native": ESBUILD_NATIVE_VERSION,
            "tailwind_oxide": TAILWIND_OXIDE_VERSION,
            "lightningcss_native": LIGHTNINGCSS_NATIVE_VERSION,
            "llama_cpp": LLAMA_CPP_RELEASE_TAG,
            "llama_cpp_archive_sha256": LLAMA_CPP_SHA256,
            "rust_toolchain": RUST_TOOLCHAIN,
            "rustc": RUSTC_VERSION,
        },
        "source_stage": source_measurement,
        "llama_cpp_stage": llama_stage,
        "frontend": frontend_measurement,
        "artifacts": {
            "app": {**_tree_measurement(app), "path": str(app)},
            "engine": {"bytes": engine.stat().st_size, "sha256": _sha256(engine)},
            "worker": {"bytes": worker.stat().st_size, "sha256": _sha256(worker)},
        },
        "durations_seconds": {
            "engine": round(engine_elapsed, 3),
            "worker": round(worker_elapsed, 3),
            "frontend": round(frontend_elapsed, 3),
            "tauri_app": round(app_elapsed, 3),
            "total": round(total_elapsed, 3),
        },
        "distribution": {
            "developer_id": developer_identity is not None,
            "notarized": developer_id_result is not None,
            "uploaded": False,
            "published": False,
            "internal_only": developer_identity is None,
        },
    }
    if developer_identity is None:
        result["local_signing"] = {"certificate_sha1": signing_identity, "kind": "self-signed"}
    result["requested_bundles"] = list(BUNDLE_SELECTIONS[bundles])
    result["updater"] = {"enabled": update is not None}
    if update is not None:
        result["updater"].update(target=update["target"], url=update["url"])
        for kind in ("bundle", "signature", "manifest"):
            path = update[kind]
            result["artifacts"][f"update_{kind}"] = {
                "bytes": path.stat().st_size, "sha256": _sha256(path), "path": str(path),
            }
    if dmg is not None:
        result["artifacts"]["dmg"] = {
            "bytes": dmg.stat().st_size,
            "sha256": _sha256(dmg),
            "path": str(dmg),
        }
    report = root / "build-report.json"
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _signing_report(
    signing_identity: str,
    developer_identity: Any,
    developer_id_result: dict[str, Any] | None,
) -> dict[str, Any]:
    if developer_identity is None:
        return {
            "kind": "self-signed",
            "certificate_sha1": signing_identity,
            "team_id": None,
            "hardened_runtime": False,
            "secure_timestamp": False,
            "entitlements": {},
            "notarization": None,
        }
    return developer_id.signing_report(developer_identity, developer_id_result)


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-root",
        type=Path,
        help="retain a fresh full build tree here (default: replace dist/macos/latest)",
    )
    parser.add_argument("--tool-root", type=Path, default=DEFAULT_TOOL_ROOT)
    parser.add_argument(
        "--bundles",
        choices=tuple(BUNDLE_SELECTIONS),
        default="app",
        help="bundle output: app (default), dmg, or both",
    )
    parser.add_argument(
        "--signing",
        choices=SIGNING_MODES,
        default=SIGNING_LOCAL,
        help=(
            "local (default): pinned self-signed internal identity; developer-id: "
            "Developer ID Application identity, hardened runtime, notarization and stapling"
        ),
    )
    parser.add_argument("--team-id", help="Developer ID team to select when several are installed")
    parser.add_argument("--signing-sha1", help="exact Developer ID certificate SHA-1 to use")
    parser.add_argument(
        "--developer-id-provider",
        type=Path,
        default=default_developer_id_provider(),
        help=(
            "Developer ID provider module for --signing developer-id "
            "(default: $WHISPERSTREAM_DEVELOPER_ID_PROVIDER)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        use_developer_id_provider(args.developer_id_provider)
        signing = {
            "signing": args.signing,
            "team_id": args.team_id,
            "signing_sha1": args.signing_sha1,
        }
        if args.build_root is not None:
            result = build(args.build_root, args.tool_root, bundles=args.bundles, **signing)
        else:
            _assert_native_host()
            result = _build_default(args.tool_root, bundles=args.bundles, **signing)
        _copy_dmg_to_builds(result)
        print(json.dumps(result, indent=2))
    except (MacInternalBuildError, OSError) as exc:
        print(f"macOS bundle build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
