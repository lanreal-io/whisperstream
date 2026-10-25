"""Build a Whisperstream Windows installer locally (native, no Git Bash).

Two installer profiles share the same frontend and native inputs:

- ``--profile source`` (default): an unsigned installer for anyone compiling
  from source. It needs no signing credentials and no official providers.
- ``--profile official``: the signed installer. It refuses to start without the
  Azure Trusted Signing credentials and the official build's updater provider
  (``require_updater_provider``), and it writes ``dist/latest.json`` plus the
  legal release attestation.

Both profiles validate the committed Tauri configuration and hold an exclusive
checkout lock (``scripts/installer_profile.py``). The script STOPS before any
upload: upload, Cloudflare purge and git tagging belong to a separate release
command, and this script never touches R2, Cloudflare or git tags.

Invocation (the official profile is wrapped by op run for secret injection):

    uv run --locked python scripts/build.py
    op run --env-file=.env.build -- uv run --locked python scripts/build.py --profile official
    op run --env-file=.env.build -- uv run --locked python scripts/build.py --profile official --portable
    op run --env-file=.env.build -- uv run --locked python scripts/build.py --profile official --microsoft-store

Flags:
    --profile       ``source`` (default, unsigned) or ``official`` (signed).
    --portable      Assemble a portable folder (no NSIS installer, no latest.json).
    --microsoft-store
                    Build the offline NSIS package prepared for Partner Center.
    --skip-nuitka   Reuse the existing dist/whisperstream-engine.exe (skips the
                    5-15 min C compile after a post-Nuitka step failed).
    --unsigned      Kept for existing commands: the source profile is already
                    unsigned. The official profile rejects it.

The body runs inside a try/finally that ALWAYS restores the two sentinel files
(`_version.py`, `_build_flags.py`) and removes the signing overlay, even on
exception. That restoration is load-bearing: a crash must not leave a dirty
working tree or ship the dev-tools surface (it replaces build.sh's
`trap cleanup EXIT`).

All stdout/stderr is teed to dist/build-signing.log (overwritten each run).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

# Make the sibling helper module importable when run as `python scripts/build.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _build_lib as b  # noqa: E402  (after sys.path tweak)
from installer_profile import effective_windows_configuration, installer_profile  # noqa: E402


MICROSOFT_STORE_CONFIG = (
    b.REPO_ROOT / "src-tauri" / "tauri.microsoft-store.conf.json"
)


# ---------------------------------------------------------------------------
# Fail-closed sherpa-onnx packaging preflight
# ---------------------------------------------------------------------------

class SherpaPreflightError(RuntimeError):
    """The installed official native graph differs from the reviewed inputs."""


class BuildEnvironmentPreflightError(RuntimeError):
    """The local machine is not ready to start an app package build."""


class MicrosoftStorePackageError(RuntimeError):
    """A Microsoft Store package failed its submission-readiness contract."""


def _verify_installed_sherpa() -> None:
    """Reuse the reviewed binding/core hash inventory before compilation."""
    spec = importlib.util.spec_from_file_location(
        "sherpa_source_preflight", b.REPO_ROOT / "scripts/check-sherpa-source.py"
    )
    if spec is None or spec.loader is None:
        raise SherpaPreflightError("Unable to load the public native verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.verify()
    except (ValueError, OSError, ImportError, AttributeError) as exc:
        raise SherpaPreflightError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Fail-fast local build environment preflight
# ---------------------------------------------------------------------------

def _resolve_selected_stt_model() -> tuple[str, Path, tuple[str, ...]]:
    """Resolve the exact STT artifact the packaged smoke test will load."""
    from whisperstream.config_manager import ConfigManager
    from whisperstream.model_downloader import (
        DOWNLOAD_REGISTRY,
        ModelId,
        model_dir,
        model_id_for_parakeet,
    )
    from whisperstream.stt_factory import _resolve_backend, _selection_from_config

    config = ConfigManager().load()
    selection = _selection_from_config(config)
    backend, _source = _resolve_backend(config)
    model_id = (
        ModelId.QWEN3_ASR_06B_INT8
        if backend == "qwen"
        else model_id_for_parakeet(selection.parakeet_model)
    )
    spec = DOWNLOAD_REGISTRY[model_id]
    return model_id.value, model_dir(model_id), spec.files


def _verify_selected_stt_model(
    *,
    selected: tuple[str, Path, tuple[str, ...]] | None = None,
) -> Path:
    """Require every selected STT file to exist and contain data."""
    model_id, directory, required_files = selected or _resolve_selected_stt_model()
    incomplete: list[str] = []
    for relative in required_files:
        path = directory / relative
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                incomplete.append(relative)
        except OSError:
            incomplete.append(relative)

    if incomplete:
        names = ", ".join(incomplete)
        raise BuildEnvironmentPreflightError(
            "selected STT model is missing or incomplete: "
            f"model={model_id} path={directory} files=[{names}]. "
            "Run `uv run python -m whisperstream.setup`, then retry the build."
        )

    print(
        "[build] STT model preflight passed "
        f"model={model_id} path={directory} files={len(required_files)}"
    )
    return directory


def _path_is_within(path: Path, directory: Path) -> bool:
    """Case-insensitive containment check suitable for native Windows paths."""
    try:
        path_value = os.path.normcase(str(path.resolve(strict=False)))
        directory_value = os.path.normcase(str(directory.resolve(strict=False)))
        return os.path.commonpath((path_value, directory_value)) == directory_value
    except (OSError, ValueError):
        return False


def _verify_same_version_runtime_not_running(
    version: str,
    *,
    platform_name: str | None = None,
    local_app_data: Path | None = None,
    processes: Any | None = None,
) -> Path | None:
    """Reject only a running engine from this build version's Nuitka cache."""
    if (platform_name or sys.platform) != "win32":
        return None

    if local_app_data is None:
        local_app_data_value = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data_value:
            raise BuildEnvironmentPreflightError(
                "LOCALAPPDATA is unavailable; cannot verify the Nuitka runtime cache"
            )
        local_app_data = Path(local_app_data_value)

    runtime_dir = local_app_data / "Whisperstream-runtime" / version
    if processes is None:
        import psutil

        processes = psutil.process_iter(["pid", "name", "exe"])

    collisions: list[tuple[int | str, Path]] = []
    for process in processes:
        try:
            info = process.info if hasattr(process, "info") else process
            name = str(info.get("name") or "")
            executable = info.get("exe")
            if name.casefold() != "whisperstream-engine.exe" or not executable:
                continue
            executable_path = Path(str(executable))
            if _path_is_within(executable_path, runtime_dir):
                collisions.append((info.get("pid", "unknown"), executable_path))
        except (OSError, ValueError, TypeError):
            continue

    if collisions:
        details = ", ".join(f"pid={pid} path={path}" for pid, path in collisions)
        raise BuildEnvironmentPreflightError(
            "the same-version Whisperstream engine is still running from the "
            f"Nuitka cache ({details}). Exit Whisperstream {version} completely "
            "and retry the build. Other installed versions do not need to be closed."
        )

    print(f"[build] runtime cache preflight passed version={version} path={runtime_dir}")
    return runtime_dir


# Nuitka before 4.1 leaks one dict per compiled empty-dict allocation on
# Python 3.14+ (_Nuitka_AllocatePyDictObject allocates twice). The shipped
# 1.6.0 engine was built that way and leaked tens of GB in production.
NUITKA_PY314_DICT_FIX = (4, 1)


def _locked_nuitka_version() -> tuple[int, ...]:
    text = (b.REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    match = re.search(r'\[\[package\]\]\s*name = "nuitka"\s*version = "([^"]+)"', text)
    if match is None:
        raise BuildEnvironmentPreflightError("nuitka is missing from uv.lock")
    return tuple(int(part) for part in re.findall(r"\d+", match.group(1))[:3])


def _verify_compiler_runtime(
    python_version: tuple[int, int] | None = None,
    nuitka_version: tuple[int, ...] | None = None,
) -> None:
    """Refuse interpreter/compiler pairs with the known Nuitka dict leak."""
    python_version = python_version or sys.version_info[:2]
    nuitka_version = nuitka_version or _locked_nuitka_version()
    if python_version >= (3, 14) and nuitka_version < NUITKA_PY314_DICT_FIX:
        raise BuildEnvironmentPreflightError(
            f"Python {python_version[0]}.{python_version[1]} with Nuitka "
            f"{'.'.join(map(str, nuitka_version))} leaks memory in the packaged engine; "
            "build with the pinned Python 3.13 (.python-version) or Nuitka >= 4.1"
        )
    print(
        "[build] compiler runtime preflight passed "
        f"python={python_version[0]}.{python_version[1]} "
        f"nuitka={'.'.join(map(str, nuitka_version))}"
    )


def _verify_build_environment(version: str) -> None:
    """Run cheap local checks before source stamping or compilation."""
    _verify_compiler_runtime()
    _verify_selected_stt_model()
    _verify_same_version_runtime_not_running(version)


# ---------------------------------------------------------------------------
# Step 1: Nuitka onefile compile
# ---------------------------------------------------------------------------

def _nuitka_argv(version: str, numeric: str, experimental: bool = False) -> list[str]:
    """Return the exact Nuitka argv from build.sh lines 152-203.

    The arg list is reproduced verbatim. The two version-dependent values
    (``--windows-file-version`` and ``--onefile-tempdir-spec``) interpolate the
    numeric and human-readable versions respectively, exactly as build.sh did.

    Args:
        version: Human-readable version from tauri.conf.json (used in the
            onefile tempdir spec, the Nuitka runtime-cache key).
        numeric: 4-part numeric version for the Windows VERSIONINFO block.
        experimental: Kept for the retained experimental gate, which gates no
            feature today (Agent Speak shipped in 1.3.0 and came off
            it). No ``--nofollow-import-to`` exclusions are attached to it now;
            add them back here when a new feature goes behind the gate.

    Returns:
        The full argv list for ``uv run --locked --extra build python -m nuitka ...``.
    """
    argv = [
        "uv", "run", "--locked", "--extra", "build", "python", "-m", "nuitka",
        "--onefile",
        "--standalone",
        "--output-dir=dist",
        "--output-filename=whisperstream-engine.exe",
        # Optional dev-tools routes. Source checkouts may not include the module,
        # and excluding an absent module is a no-op.
        "--nofollow-import-to=whisperstream.dev_endpoints",
        # onnxruntime.transformers (included via --include-package=onnxruntime)
        # optionally imports Hugging Face `datasets`, which is not installed.
        # Nuitka >= 4.1 would resolve it to any local folder of that name and
        # abort in its package configuration checks.
        "--nofollow-import-to=datasets",
        # Platform adapters are selected through importlib at runtime, so
        # Nuitka cannot discover them from the static import graph. Keep the
        # Windows adapters in the frozen engine without making shared imports
        # eager or weakening their native-free parent boundaries.
        "--include-module=whisperstream.windows_protected_secret",
        "--include-module=whisperstream.stt_worker_speech_gate",
        "--include-module=whisperstream.speech_gate",
        "--include-module=whisperstream.windows_permission_status",
        "--include-module=whisperstream.windows_protected_storage_probe",
        "--include-module=whisperstream.permission_actions",
        "--include-module=whisperstream.audio_capture",
        "--include-module=whisperstream.windows_audio_policy",
        "--include-module=whisperstream.windows_history_security",
        "--include-module=whisperstream.keyboard_shortcuts",
        "--include-module=whisperstream.windows_delivery_adapter",
        "--include-module=whisperstream.windows_app_context",
        "--nofollow-import-to=whisperstream.macos_keychain_probe",
        "--nofollow-import-to=whisperstream.macos_permission_actions",
        "--nofollow-import-to=whisperstream.macos_permission_status",
        "--nofollow-import-to=whisperstream.macos_capture",
        "--nofollow-import-to=whisperstream.macos_delivery_adapter",
        "--nofollow-import-to=whisperstream.macos_protected_secret",
        "--nofollow-import-to=whisperstream.macos_protected_storage_probe",
        "--nofollow-import-to=whisperstream.macos_shortcuts",
        "--nofollow-import-to=whisperstream.macos_stt_manager",
        "--include-module=whisperstream.speech.kokoro_worker",
        "--include-module=whisperstream.speech.legacy_kokoro_engine",
        "--include-package=sherpa_onnx",
        "--include-package=onnxruntime",
        "--include-module=sounddevice",
        "--include-package=_sounddevice_data",
        "--include-package=pygame",
        "--include-package=pynput",
        "--include-package=fastapi",
        "--include-package=uvicorn",
        "--include-package=starlette",
        "--include-package=websockets",
        "--include-package=anyio",
        "--include-package=rich",
        "--include-package=cryptography",
        "--include-package=comtypes",
        "--include-package=truststore",
        "--include-package-data=sherpa_onnx",
        "--include-package-data=onnxruntime",
        "--include-package-data=_sounddevice_data",
        "--include-package-data=pygame",
        "--include-data-files=src/whisperstream/assets/*.wav=whisperstream/assets/",
        "--include-data-files=src/whisperstream/assets/*.ico=whisperstream/assets/",
        "--include-data-files=src/whisperstream/assets/silero_vad.onnx=whisperstream/assets/silero_vad.onnx",
        "--include-data-files=src/whisperstream/assets/silero-vad-LICENSE.txt=whisperstream/assets/silero-vad-LICENSE.txt",
        "--include-data-files=src/whisperstream/assets/__init__.py=whisperstream/assets/",
        "--include-data-files=src/whisperstream/cloud-models.json=whisperstream/cloud-models.json",
        "--nofollow-import-to=sympy",
        "--nofollow-import-to=scipy",
        "--nofollow-import-to=PIL",
        "--nofollow-import-to=Pillow",
        "--nofollow-import-to=tkinter",
        "--nofollow-import-to=unittest",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=hf_xet",
        "--nofollow-import-to=markdown_it",
        "--nofollow-import-to=pyreadline3",
        "--nofollow-import-to=_pyrepl",
        "--report=dist/compilation-report.xml",
        "--jobs=4",
        "--windows-file-description=Whisperstream Engine",
        "--windows-product-name=Whisperstream",
        "--windows-company-name=Lanreal Technologies Inc.",
        f"--windows-file-version={numeric}",
        "--windows-console-mode=attach",
        f"--onefile-tempdir-spec={{CACHE_DIR}}/Whisperstream-runtime/{version}",
        "--onefile-cache-mode=cached",
        "--windows-icon-from-ico=src/whisperstream/assets/whisperstream.ico",
        "--no-deployment-flag=self-execution",
        "--assume-yes-for-downloads",
    ]
    # Agent Speak ships from 1.3.0, so its modules are compiled in and its
    # non-Python payloads must be staged too. They are loaded via
    # importlib.resources at runtime, so Nuitka does not discover them from the
    # import graph — without these includes the features import fine and then
    # fail only in a packaged install.
    argv += [
        "--include-data-files=src/whisperstream/claude_hook/forwarder.ps1=whisperstream/claude_hook/forwarder.ps1",
        "--include-data-dir=src/whisperstream/claude_plugin=whisperstream/claude_plugin",
        "--include-data-dir=src/whisperstream/codex_plugin=whisperstream/codex_plugin",
    ]
    # Keep Kokoro lazy-loadable in standard builds for already-installed models.
    # The download entry point separately requires experimental opt-in.
    argv.append("whisperstream_entry.py")
    return argv


def _step_nuitka(
    version: str, numeric: str, total: int, skip_nuitka: bool, experimental: bool = False
) -> None:
    """Step 1: compile the Python sidecar with Nuitka (or skip it).

    When ``skip_nuitka`` is set, reuse the existing onefile exe; hard-error if
    it is absent (mirrors build.sh lines 141-149). ``experimental`` is threaded
    to ``_nuitka_argv`` so a ``--experimental`` build keeps the agent-speech
    modules instead of excluding them.
    """
    engine = b.REPO_ROOT / "dist" / "whisperstream-engine.exe"
    if skip_nuitka:
        print(f"=== Step 1/{total}: Build Python sidecar with Nuitka (SKIPPED via --skip-nuitka) ===")
        if not engine.is_file():
            print(
                "ERROR: --skip-nuitka requested but dist/whisperstream-engine.exe is missing.",
                file=sys.stderr,
            )
            print("  Run a full build first, or remove --skip-nuitka.", file=sys.stderr)
            sys.exit(1)
        print(f"Reusing existing dist/whisperstream-engine.exe ({_size_str(engine)})")
        print(
            "Note: the reused binary has its OWN baked-in version. Make sure it matches "
            f"tauri.conf.json ({version}). _version.py was stamped from tauri.conf.json at "
            "the time the binary was built."
        )
        return

    print(f"=== Step 1/{total}: Build Python sidecar with Nuitka ===")
    b.run(_nuitka_argv(version, numeric, experimental))


# ---------------------------------------------------------------------------
# Step 2: smoke test + manual sidecar signing
# ---------------------------------------------------------------------------

def _step_smoke_and_sign(cfg, total: int, unsigned: bool = False) -> Path:
    """Step 2: smoke-test the frozen exe, then manually sign it.

    The sidecar is signed BEFORE staging into src-tauri/binaries/ because
    Tauri's signCommand hook can silently fail on read-only sidecars
    (tauri-apps/tauri#11778), so this artifact is handled manually.

    When ``unsigned`` is True the signing step is skipped entirely (no
    trusted-signing-cli call). The smoke test still runs.

    Returns:
        The path to the smoke-tested (and optionally signed) dist/whisperstream-engine.exe.
    """
    engine = b.REPO_ROOT / "dist" / "whisperstream-engine.exe"
    print("")
    print(f"=== Step 2/{total}: Smoke test sidecar ===")
    b.run([str(engine), "--check"])

    if unsigned:
        print("Signing whisperstream-engine.exe SKIPPED (--unsigned).")
        return engine

    print("")
    print("Signing whisperstream-engine.exe...")
    b.sign_file(cfg, engine, "Whisperstream Engine")
    return engine


# ---------------------------------------------------------------------------
# Steps 3-8: legal docs, deps, licenses, sidecars, frontend build
# ---------------------------------------------------------------------------

def _step_derive_eula(total: int) -> None:
    """Step 3: derive both bundled legal documents directly (no bash)."""
    print("")
    print(f"=== Step 3/{total}: Derive bundled legal documents ===")
    b.run([sys.executable, str(b.REPO_ROOT / "scripts" / "derive-eula-txt.py")])
    b.run([sys.executable, str(b.REPO_ROOT / "scripts" / "derive-privacy-txt.py")])
    b.validate_packaged_legal_resources()


def _step_frontend_deps(total: int) -> None:
    """Step 4: install frontend dependencies via npm ci in frontend/."""
    print("")
    print(f"=== Step 4/{total}: Install frontend dependencies ===")
    b.run(["npm", "ci"], cwd=b.REPO_ROOT / "frontend")


def _step_generate_licenses(total: int) -> None:
    """Step 5: generate THIRD_PARTY_LICENSES.txt + the audit report.

    Calls the native Python license generator (scripts/generate_licenses.py).
    No bash spawn on the build path here.
    """
    print("")
    print(f"=== Step 5/{total}: Generate THIRD_PARTY_LICENSES.txt ===")
    b.run([sys.executable, str(b.REPO_ROOT / "scripts" / "generate_licenses.py")])


def _step_stage_sidecar(total: int) -> Path:
    """Step 6: stage the signed sidecar into src-tauri/binaries/ (shutil.copy, no cp)."""
    print("")
    print(f"=== Step 6/{total}: Stage sidecar for Tauri ===")
    binaries = b.REPO_ROOT / "src-tauri" / "binaries"
    binaries.mkdir(parents=True, exist_ok=True)
    staged = binaries / "whisperstream-engine-x86_64-pc-windows-msvc.exe"
    shutil.copy(b.REPO_ROOT / "dist" / "whisperstream-engine.exe", staged)
    print(f"Staged whisperstream-engine sidecar ({_size_str(staged)})")
    return staged


def _step_stage_llama(total: int) -> Path:
    """Step 7: fetch + stage the llama-server sidecar; assert the exe exists."""
    print("")
    print(f"=== Step 7/{total}: Stage llama-server sidecar ===")
    import fetch_llama_server
    fetch_llama_server.stage_llama_server()

    staged = b.REPO_ROOT / "src-tauri" / "binaries" / "llama-server-x86_64-pc-windows-msvc.exe"
    if not staged.is_file():
        print(
            "ERROR: llama-server binary was not staged by fetch_llama_server.py",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Staged llama-server sidecar ({_size_str(staged)})")
    return staged


# ---------------------------------------------------------------------------
# Steps 8-9: build, sign, and stage whisperstream-cli (Task 5.1)
# ---------------------------------------------------------------------------

def _step_build_cli(total: int) -> Path:
    """Step 8: compile the whisperstream-cli binary in release mode.

    The crate binary is named ``whisperstream-cli`` (src-tauri/Cargo.toml): it
    cannot be named ``whisperstream`` because that cargo output name already
    belongs to the Tauri shell binary (src/main.rs).
    """
    print("")
    print(f"=== Step 8/{total}: Build whisperstream-cli ===")
    b.run(
        ["cargo", "build", "--release", "--bin", "whisperstream-cli"],
        cwd=b.REPO_ROOT / "src-tauri",
    )
    compiled = b.REPO_ROOT / "src-tauri" / "target" / "release" / "whisperstream-cli.exe"
    print(f"Compiled whisperstream-cli.exe ({_size_str(compiled)})")
    return compiled


def _step_sign_and_stage_cli(
    cfg, total: int, compiled: Path, unsigned: bool = False
) -> Path:
    """Step 9: sign whisperstream-cli.exe, THEN stage it as bin/whisperstream.exe.

    Signing happens manually and before staging, mirroring the sidecar
    workaround for tauri-apps/tauri#11778 (Tauri's signCommand hook can
    silently fail on a bundled resource). The installed filename is
    ``whisperstream.exe`` inside a ``bin\\`` subdirectory: it is shipped as a
    plain bundled resource (``bin/whisperstream.exe`` in tauri.conf.json), NOT
    ``externalBin`` — externalBin flattens into the install root, which would
    put every bundled sidecar on PATH instead of just this one directory.
    """
    print("")
    print(f"=== Step 9/{total}: Sign and stage whisperstream-cli ===")
    if unsigned:
        print("Signing whisperstream-cli.exe SKIPPED (--unsigned).")
    else:
        print("Signing whisperstream-cli.exe...")
        b.sign_file(cfg, compiled, "Whisperstream CLI")

    bin_dir = b.REPO_ROOT / "src-tauri" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    staged = bin_dir / "whisperstream.exe"
    shutil.copy(compiled, staged)
    print(f"Staged whisperstream-cli sidecar ({_size_str(staged)})")
    return staged


def _frontend_build_env(experimental: bool) -> dict[str, str] | None:
    """Return the environment required to keep experimental frontend code."""
    if not experimental:
        return None
    env = os.environ.copy()
    env["VITE_EXPERIMENTAL"] = "1"
    return env


def _step_build_frontend(total: int, experimental: bool = False) -> None:
    """Step 8: build the frontend bundle via npm run build in frontend/.

    On a ``--experimental`` build, ``VITE_EXPERIMENTAL=1`` is exported to the Vite
    build so the frontend's compile-time gate (``import.meta.env.VITE_EXPERIMENTAL``)
    stays live. Default builds leave it unset. Since 1.3.0 it gates no shipped UI;
    Agent Speak ships in a default bundle gated only by default-off config.
    """
    print("")
    print(f"=== Step 10/{total}: Build frontend ===")
    env = _frontend_build_env(experimental)
    if env is not None:
        # Vite exposes VITE_-prefixed process-env vars on import.meta.env at build
        # time, so this reaches the bundle without a .env file.
        print("[build] --experimental: VITE_EXPERIMENTAL=1 passed to the frontend build")
    b.run(["npm", "run", "build"], cwd=b.REPO_ROOT / "frontend", env=env)


# ---------------------------------------------------------------------------
# Step 9 (NSIS): cargo tauri build + latest.json, stop before upload
# ---------------------------------------------------------------------------

def _windows_bundle_resources() -> list[str]:
    """Read the effective resource array after Tauri's Windows config merge.

    JSON Merge Patch replaces arrays. A Windows resource list therefore
    supersedes the base list; omitting it retains the base resources.
    """
    resources = effective_windows_configuration(b.REPO_ROOT).get("bundle", {}).get("resources")
    if not isinstance(resources, list) or any(
        not isinstance(resource, str) for resource in resources
    ):
        raise ValueError("Windows bundle resources must be a list of paths")
    return list(resources)


def _cli_resources_overlay_json() -> str:
    """Build a `--config` overlay that adds bin/whisperstream.exe as a bundled resource.

    Task 5.1 could not add "bin/whisperstream.exe" to tauri.conf.json's own
    bundle.resources: Tauri's build.rs validates every resource path exists at
    *every* cargo build of this crate, including the plain
    `cargo build --bin whisperstream-cli` (step 8 above, and every Phase 1-3
    verify command) that produces the file in the first place, so a
    committed resource entry would break those builds on a fresh checkout.

    Instead this overlay is applied only to the `cargo tauri build`
    invocations (step 11), which always run after step 9 has already signed
    and staged the real bin/whisperstream.exe. `--config` overlays merge via
    RFC 7386 JSON Merge Patch (json_patch::merge), which REPLACES arrays
    wholesale rather than concatenating them, so this must carry the full
    effective Windows resources list plus the CLI entry, not
    just the new entry.
    """
    resources = _windows_bundle_resources()
    cli_resource = "bin/whisperstream.exe"
    if cli_resource not in resources:
        resources.append(cli_resource)
    return json.dumps(
        {"bundle": {"resources": resources}}, separators=(",", ":")
    )


def _step_tauri_nsis(
    overlay: Path,
    total: int,
    experimental: bool = False,
    microsoft_store: bool = False,
) -> None:
    """Step 9 (NSIS): build the signed Tauri NSIS installer.

    With signCommand configured (via the --config overlay), Tauri signs both
    the inner whisperstream.exe (before bundling) and the final NSIS installer
    (after bundling). Mirrors build.sh lines 318-327.

    A second --config carries the CLI resources overlay (see
    _cli_resources_overlay_json); `cargo tauri build` merges repeated
    --config values in the order given. The build's Tauri overlay (the
    official updater's configuration, see b.tauri_overlay_args) comes first,
    so the Microsoft Store overlay still turns updater artifacts off.
    """
    print(f"=== Step 11/{total}: Build Tauri app (NSIS installer) ===")
    argv = [
        "cargo", "tauri", "build",
        *b.tauri_overlay_args(),
        "--config", str(overlay),
        "--config", _cli_resources_overlay_json(),
    ]
    if microsoft_store:
        argv.extend(["--config", str(MICROSOFT_STORE_CONFIG)])
    # Compile and all beforeBuild/dependency hooks run without credentials.
    compile_argv = [*argv, "--no-bundle", "--config", '{"bundle":{"windows":{"signCommand":null}}}']
    b.run(
        compile_argv,
        cwd=b.REPO_ROOT / "src-tauri",
        env=_frontend_build_env(experimental),
    )
    # Tauri's separate bundle command does not compile dependencies. Disable
    # beforeBundleCommand so no project hook inherits signing authority.
    bundle_argv = [*argv]
    bundle_argv[2] = "bundle"
    bundle_argv.extend(["--config", '{"build":{"beforeBundleCommand":null}}'])
    b.run(bundle_argv, cwd=b.REPO_ROOT / "src-tauri",
          env=_frontend_build_env(experimental),
          permitted_secrets=(*b.AZURE_SIGNING_SECRETS, *b.TAURI_SIGNING_SECRETS))


def _step_tauri_nsis_unsigned(
    total: int,
    experimental: bool = False,
    microsoft_store: bool = False,
) -> None:
    """Step 9 (NSIS, unsigned): build the Tauri NSIS installer WITHOUT the signing overlay.

    Used only when --unsigned is set. The installer is unsigned and NOT
    release-ready. No trusted-signing-cli calls are made. The resources
    overlay (bin/whisperstream.exe) is merged into this same --config value;
    see _cli_resources_overlay_json.
    """
    print(f"=== Step 11/{total}: Build Tauri app (NSIS installer, unsigned) ===")
    resources_overlay = json.loads(_cli_resources_overlay_json())
    unsigned_config = json.dumps(
        {
            "bundle": {
                "createUpdaterArtifacts": False,
                "resources": resources_overlay["bundle"]["resources"],
            }
        },
        separators=(",", ":"),
    )
    argv = ["cargo", "tauri", "build", *b.tauri_overlay_args(), "--config", unsigned_config]
    if microsoft_store:
        argv.extend(["--config", str(MICROSOFT_STORE_CONFIG)])
    b.run(
        argv,
        cwd=b.REPO_ROOT / "src-tauri",
        env=_frontend_build_env(experimental),
    )


def _generate_latest_json(version: str, total: int) -> None:
    """Step 10 (NSIS): locate the updater signature, write latest.json.

    The signed installer is the update payload (Tauri v2 updater artifacts):
    it hard-errors if the installer or its ``.exe.sig`` is missing, reads the
    signature, extracts CHANGELOG notes, and writes dist/latest.json pointing
    at the release's versioned folder. It then STOPS (the build never uploads).
    """
    print("")
    print(f"=== Step 12/{total}: Generate update manifest (no upload) ===")
    print(f"Version: {version}")

    nsis_dir = b.REPO_ROOT / "src-tauri" / "target" / "release" / "bundle" / "nsis"
    nsis_exe = nsis_dir / f"Whisperstream_{version}_x64-setup.exe"
    nsis_sig = nsis_dir / f"Whisperstream_{version}_x64-setup.exe.sig"

    if not nsis_exe.is_file():
        print(f"ERROR: {nsis_exe} not found.")
        print("Ensure cargo tauri build produced the NSIS .exe installer.")
        sys.exit(1)
    if not nsis_sig.is_file():
        print(f"ERROR: {nsis_sig} not found.")
        print("Ensure TAURI_SIGNING_PRIVATE_KEY is set and createUpdaterArtifacts is configured.")
        sys.exit(1)

    signature = nsis_sig.read_text(encoding="utf-8")
    print(f"Signature: {signature[:20]}...")

    # Extract notes from CHANGELOG.md (same fallback + warnings as build.sh).
    changelog = b.REPO_ROOT / "CHANGELOG.md"
    notes = None
    if changelog.exists():
        notes = b.extract_changelog_notes(version, changelog.read_text(encoding="utf-8"))
        if notes is not None:
            print(f"Using CHANGELOG.md notes for {version} ({len(notes)} chars)")
        else:
            print(
                f"WARNING: CHANGELOG.md has no entry for {version}, using fallback notes",
                file=sys.stderr,
            )
    else:
        print("WARNING: CHANGELOG.md not found, using fallback notes", file=sys.stderr)

    pub_date = b.iso_utc_now()
    manifest = b.build_latest_manifest(
        version, pub_date, signature, notes, download_base=b.update_download_base()
    )

    import json
    dist = b.REPO_ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "latest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Generated dist/latest.json")
    legal_attestation = b.write_legal_release_attestation(
        version,
        nsis_exe,
        nsis_sig,
    )
    print(f"Generated {legal_attestation}")

    # STOP. The build command never uploads. Just print artifact paths.
    print("")
    print("=== Build complete! ===")
    print("Installer: src-tauri/target/release/bundle/nsis/")
    print(f"  {nsis_exe}")
    print(f"  {nsis_sig}")
    print("Manifests: dist/latest.json, dist/legal-release.json")


def _copy_windows_installer_to_builds(version: str, *, signed: bool, microsoft_store: bool) -> Path:
    """Copy the finished NSIS installer to builds/windows with a descriptive name."""
    installer = (
        b.REPO_ROOT / "src-tauri" / "target" / "release" / "bundle" / "nsis"
        / f"Whisperstream_{version}_x64-setup.exe"
    )
    variant = "-microsoft-store" if microsoft_store else ""
    suffix = "" if signed else "-UNSIGNED"
    return b.copy_to_builds(installer, "windows", f"Whisperstream_{version}_x64{variant}-setup{suffix}.exe")


def _microsoft_store_pe_paths(installer: Path) -> list[Path]:
    """Enumerate every PE input the Store installer is expected to install."""
    root = b.REPO_ROOT
    paths = {
        installer,
        root
        / "src-tauri"
        / "binaries"
        / "whisperstream-engine-x86_64-pc-windows-msvc.exe",
        root
        / "src-tauri"
        / "binaries"
        / "llama-server-x86_64-pc-windows-msvc.exe",
        root / "src-tauri" / "bin" / "whisperstream.exe",
    }

    binaries_dir = root / "src-tauri" / "binaries"
    if binaries_dir.is_dir():
        paths.update(
            path
            for path in binaries_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".exe", ".dll"}
        )

    config_path = root / "src-tauri" / "tauri.conf.json"
    if config_path.is_file():
        try:
            for resource in _windows_bundle_resources():
                resource_path = Path(resource)
                if resource_path.suffix.lower() in {".exe", ".dll"}:
                    paths.add(root / "src-tauri" / resource_path)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise MicrosoftStorePackageError(
                f"cannot enumerate Store PE resources from {config_path}: {exc}"
            ) from exc

    return sorted(paths, key=lambda path: str(path).lower())


def _verify_microsoft_store_pe_signatures(installer: Path) -> list[Path]:
    """Fail unless the installer and every installed PE input are trusted."""
    verified: list[Path] = []
    failures: list[str] = []
    for path in _microsoft_store_pe_paths(installer):
        if not path.is_file():
            failures.append(f"{path}: missing")
            continue
        result = b.verify_signature(path)
        if not result.trusted:
            failures.append(f"{path}: {result.detail}")
            continue
        verified.append(path)

    if failures:
        details = "\n".join(f"  - {failure}" for failure in failures)
        raise MicrosoftStorePackageError(
            "Microsoft Store PE signature audit failed:\n" + details
        )
    return verified + _verify_microsoft_store_payload(installer)


def _store_extractor() -> str:
    extractor = os.environ.get("SEVEN_ZIP") or shutil.which("7z")
    if not extractor:
        raise MicrosoftStorePackageError("Set SEVEN_ZIP to a 7-Zip executable for the Store payload audit")
    return extractor


def _verify_microsoft_store_payload(installer: Path) -> list[Path]:
    """Audit installed bytes, since Tauri restores its unsigned main binary."""
    extractor = _store_extractor()
    with tempfile.TemporaryDirectory(prefix="whisperstream-store-audit-") as folder:
        b.run([extractor, "x", str(installer.resolve()), f"-o{folder}", "-y", "-bso0"])
        root = Path(folder)
        required = [root / name for name in (
            "whisperstream.exe", "whisperstream-engine.exe", "llama-server.exe",
            "bin/whisperstream.exe",
        )]
        paths = set(required)
        paths.update(path for path in root.rglob("*") if path.is_file()
                     and path.suffix.lower() in {".exe", ".dll"}
                     and not path.relative_to(root).parts[0].startswith("$"))
        verified = []
        for path in sorted(paths):
            if not path.is_file():
                raise MicrosoftStorePackageError(f"Store payload missing: {path.relative_to(root)}")
            result = b.verify_signature(path)
            if not result.trusted:
                raise MicrosoftStorePackageError(f"Store payload {path.relative_to(root)}: {result.detail}")
            verified.append(Path("installer-payload") / path.relative_to(root))
        return verified


def _stage_microsoft_store_artifact(
    version: str,
    total: int,
    *,
    signed: bool,
) -> tuple[Path, Path]:
    """Copy the exact Store installer and write isolated submission facts."""
    print("")
    print(f"=== Step 12/{total}: Stage Microsoft Store submission package ===")
    installer = (
        b.REPO_ROOT
        / "src-tauri"
        / "target"
        / "release"
        / "bundle"
        / "nsis"
        / f"Whisperstream_{version}_x64-setup.exe"
    )
    if not installer.is_file():
        raise MicrosoftStorePackageError(f"Store installer is missing: {installer}")

    verified = _verify_microsoft_store_pe_signatures(installer) if signed else []
    output_dir = b.REPO_ROOT / "dist" / "microsoft-store" / version
    output_dir.mkdir(parents=True, exist_ok=True)
    output_name = installer.name if signed else f"UNSIGNED-{installer.name}"
    output = output_dir / output_name
    shutil.copy2(installer, output)

    def display_path(path: Path) -> str:
        try:
            return path.relative_to(b.REPO_ROOT).as_posix()
        except ValueError:
            return str(path)

    manifest = {
        "schema_version": 1,
        "app_version": version,
        "package": {
            "filename": output.name,
            "sha256": b.file_sha256(output),
            "size_bytes": output.stat().st_size,
            "architecture": "x64",
            "installer_type": "exe",
            "silent_install_arguments": "/S",
        },
        "tauri_config": "src-tauri/tauri.microsoft-store.conf.json",
        "signature_ready": signed,
        "verified_pe_files": [display_path(path) for path in verified],
        "generated_at": b.iso_utc_now(),
    }
    manifest_name = "submission.json" if signed else "submission-UNSIGNED.json"
    manifest_path = output_dir / manifest_name
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Store package: {output}")
    print(f"Submission metadata: {manifest_path}")
    if not signed:
        print("[build] WARNING Store package is unsigned and not submission-ready")
    return output, manifest_path
    print("Signed:    whisperstream-engine.exe, whisperstream.exe, NSIS installer")
    print("  Verify:  right-click the .exe, Properties, Digital Signatures tab")
    print("Upload:    skipped (use the release script to upload this build).")


# ---------------------------------------------------------------------------
# Step 9 (portable): cargo tauri build --no-bundle + folder assembly
# ---------------------------------------------------------------------------

_PORTABLE_RESOURCE_FILES = (
    "THIRD_PARTY_LICENSES.txt",
    "PRIVACY.txt",
    "EULA.txt",
    "eula-policy.json",
    "eula/2.1.txt",
    "gemma-4-e2b-chat-template.jinja",
)

_PORTABLE_LLAMA_DLLS = (
    "ggml.dll",
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
    "libomp.dll",
    "llama-server-impl.dll",
    "llama.dll",
    "llama-common.dll",
    "mtmd.dll",
)
_PORTABLE_LLAMA_NOTICES = ("LICENSE-LLVM-OpenMP",)

_PORTABLE_BIN_README = """This folder holds whisperstream.exe, the CLI wrapper for Claude Code and Codex.

A portable build never mutates system state, so it does not add this folder
to PATH for you. To use `whisperstream claude` or `whisperstream codex` from any terminal, add this
bin folder to your PATH yourself:

    setx PATH "%PATH%;<this folder>"

Then open a new terminal and run one of:

    whisperstream claude
    whisperstream codex
"""


def _step_tauri_portable(
    cfg,
    overlay: Path,
    total: int,
    experimental: bool = False,
) -> None:
    """Step 9 (portable): build the unbundled Tauri app and assemble the folder.

    Mirrors build.sh lines 262-316:
    - cargo tauri build --no-bundle --config <overlay>
    - manually sign the raw whisperstream.exe (the --no-bundle signCommand hook
      does not always run on it)
    - assemble dist/Whisperstream/ with the Windows sidecars, resources, and DLLs
    - create a Start-Menu shortcut via a native powershell.exe COM call

    Windows paths are computed in Python (str(Path)) instead of cygpath; on
    Windows, str(Path(...)) already yields backslash native paths.
    """
    print(f"=== Step 11/{total}: Build Tauri app (portable) ===")
    src_tauri = b.REPO_ROOT / "src-tauri"
    b.run(
        ["cargo", "tauri", "build", "--no-bundle", *b.tauri_overlay_args(),
         "--config", str(overlay),
         "--config", '{"bundle":{"windows":{"signCommand":null}}}'],
        cwd=src_tauri,
        env=_frontend_build_env(experimental),
    )

    release_exe = src_tauri / "target" / "release" / "whisperstream.exe"

    # With --no-bundle, Tauri's signCommand hook does not always run on the raw
    # whisperstream.exe. Sign it manually so the portable folder ships a signed
    # main executable.
    print("")
    print("Signing whisperstream.exe (portable)...")
    b.sign_file(cfg, release_exe, "Whisperstream")

    _assemble_portable_folder(release_exe)


def _step_tauri_portable_unsigned(
    total: int,
    experimental: bool = False,
) -> None:
    """Step 9 (portable, unsigned): build the unbundled Tauri app without signing.

    Used only when --unsigned is set. Runs cargo tauri build --no-bundle WITHOUT
    the signing overlay and skips the manual exe signing step.
    """
    print(f"=== Step 11/{total}: Build Tauri app (portable, unsigned) ===")
    src_tauri = b.REPO_ROOT / "src-tauri"
    b.run(
        ["cargo", "tauri", "build", "--no-bundle", *b.tauri_overlay_args()],
        cwd=src_tauri,
        env=_frontend_build_env(experimental),
    )

    release_exe = src_tauri / "target" / "release" / "whisperstream.exe"
    print("Signing whisperstream.exe (portable) SKIPPED (--unsigned).")

    _assemble_portable_folder(release_exe)


def _assemble_portable_folder(release_exe: Path) -> None:
    """Assemble the dist/Whisperstream/ portable folder and create the Start-Menu shortcut.

    Shared by the signed and unsigned portable paths. ``release_exe`` is the
    path to whisperstream.exe produced by ``cargo tauri build --no-bundle``.
    """
    portable_dir = b.REPO_ROOT / "dist" / "Whisperstream"
    if portable_dir.exists():
        shutil.rmtree(portable_dir)
    portable_dir.mkdir(parents=True, exist_ok=True)

    resources_source = b.REPO_ROOT / "src-tauri" / "resources"
    binaries_source = b.REPO_ROOT / "src-tauri" / "binaries"
    resources_dir = portable_dir / "resources"
    binaries_dir = portable_dir / "binaries"

    shutil.copy(release_exe, portable_dir / "Whisperstream.exe")
    shutil.copy(
        b.REPO_ROOT / "dist" / "whisperstream-engine.exe",
        portable_dir / "whisperstream-engine.exe",
    )
    shutil.copy(
        binaries_source / "llama-server-x86_64-pc-windows-msvc.exe",
        portable_dir / "llama-server.exe",
    )

    for relative_name in _PORTABLE_RESOURCE_FILES:
        destination = resources_dir / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(resources_source / relative_name, destination)
    for filename in (*_PORTABLE_LLAMA_DLLS, *_PORTABLE_LLAMA_NOTICES):
        binaries_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(binaries_source / filename, binaries_dir / filename)

    # Keep the longstanding human-readable root copies while also mirroring
    # Tauri's resources/ layout used by the frozen engine at runtime.
    for filename in (
        "THIRD_PARTY_LICENSES.txt",
        "PRIVACY.txt",
        "EULA.txt",
        "gemma-4-e2b-chat-template.jinja",
    ):
        shutil.copy(resources_source / filename, portable_dir / filename)
    shutil.copy(b.REPO_ROOT / "LICENSE", portable_dir / "LICENSE")
    shutil.copy(b.REPO_ROOT / "NOTICE", portable_dir / "NOTICE")
    (portable_dir / "LICENSES").mkdir()
    shutil.copy(
        b.REPO_ROOT / "LICENSES" / "Chirp-MIT.txt",
        portable_dir / "LICENSES" / "Chirp-MIT.txt",
    )
    # The CLI wrapper (Task 5.1) rides along in bin\, same as the installed
    # layout, but --portable never mutates system state (no NSIS, no PATH
    # write), so the folder ships a README telling the user to add it
    # themselves instead.
    bin_dir = portable_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        b.REPO_ROOT / "src-tauri" / "bin" / "whisperstream.exe",
        bin_dir / "whisperstream.exe",
    )
    (bin_dir / "README.txt").write_text(
        _PORTABLE_BIN_README, encoding="utf-8", newline="\r\n"
    )

    _create_start_menu_shortcut(portable_dir)

    print("")
    print("=== Build complete! ===")
    print("Portable folder: dist/Whisperstream/")
    print(f"  Whisperstream.exe   ({_size_str(portable_dir / 'Whisperstream.exe')})")
    print(f"  whisperstream-engine.exe   ({_size_str(portable_dir / 'whisperstream-engine.exe')})")
    print(f"  llama-server.exe   ({_size_str(portable_dir / 'llama-server.exe')})")
    print(f"  THIRD_PARTY_LICENSES.txt   ({_size_str(portable_dir / 'THIRD_PARTY_LICENSES.txt')})")
    print(f"  EULA.txt   ({_size_str(portable_dir / 'EULA.txt')})")
    print(
        "  gemma-4-e2b-chat-template.jinja   "
        f"({_size_str(portable_dir / 'gemma-4-e2b-chat-template.jinja')})"
    )
    print(f"  bin\\whisperstream.exe   ({_size_str(bin_dir / 'whisperstream.exe')})")
    print("")
    print("Start Menu shortcut created. Search 'Whisperstream' or pin it.")


def _create_start_menu_shortcut(portable_dir: Path) -> None:
    """Create a Start-Menu .lnk via a native powershell.exe WScript.Shell COM call.

    Replaces build.sh's cygpath-fed PowerShell block (lines 292-306). The
    PowerShell call is native (not bash), so it is kept; the only change is
    computing the Windows paths in Python. On Windows, ``str(Path(...))`` is
    already the backslash native form WScript.Shell expects.
    """
    appdata = os.environ.get("APPDATA", "")
    shortcut_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    shortcut_path = shortcut_dir / "Whisperstream.lnk"

    win_exe = str(portable_dir / "Whisperstream.exe")
    win_icon = str(b.REPO_ROOT / "src-tauri" / "icons" / "icon.ico")
    win_dir = str(portable_dir)
    win_shortcut = str(shortcut_path)

    ps_script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{win_shortcut}'); "
        f"$s.TargetPath = '{win_exe}'; "
        f"$s.WorkingDirectory = '{win_dir}'; "
        f"$s.IconLocation = '{win_icon}'; "
        "$s.Description = 'Whisperstream dictation app'; "
        "$s.Save()"
    )
    b.run(["powershell.exe", "-NoProfile", "-Command", ps_script])
    print(f"Start Menu shortcut: {shortcut_path}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _size_str(path: Path) -> str:
    """Return a short human-readable size string (replaces ``du -h``)."""
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


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _run_build(
    portable: bool,
    skip_nuitka: bool,
    unsigned: bool = False,
    experimental: bool = False,
    microsoft_store: bool = False,
) -> None:
    """Run the build pipeline (Steps 1-9 + latest.json).

    By default (unsigned=False) ALWAYS signs: resolves signing config (hard-error
    if missing) and wraps the body in a signing overlay context manager. When
    unsigned=True, skips signing entirely and prints a prominent warning.

    When ``experimental`` is False (default, a production/release build)
    ``EXPERIMENTAL_AVAILABLE`` is flipped off. That flag gates no feature today, so
    the flip is a no-op for shipped functionality; it stays because it is the correct
    default for the next feature placed behind the gate. Agent Speak ships in a
    default release build as of 1.3.0.

    In both paths, the body runs in try/finally so the two sentinel files
    (_version.py, _build_flags.py) and any signing overlay are ALWAYS
    restored/removed on exit or exception. Stops before upload.
    """
    if microsoft_store and not MICROSOFT_STORE_CONFIG.is_file():
        raise MicrosoftStorePackageError(
            f"Microsoft Store Tauri overlay is missing: {MICROSOFT_STORE_CONFIG}"
        )
    if microsoft_store and not unsigned:
        _store_extractor()

    # Every installer needs usable terms, even when signing is disabled.
    b.validate_packaged_legal_resources()

    # Unreleased source may retain publication-date placeholders, and unsigned
    # local builds remain available in that state. A signed build must fail
    # before setup or compilation if the canonical policies are not final.
    if not unsigned:
        b.validate_legal_release_documents(source_only=True)
        b.require_updater_provider()
        b.validate_updater_release_policy()

    version = b.read_tauri_version()

    # These checks depend only on the local runtime/config and must happen before
    # source stamping or the long Nuitka compile. The frozen sidecar smoke test
    # remains Step 2 because it verifies the actual packaged executable.
    _verify_build_environment(version)

    # This must run before signing setup, source stamping, and especially
    # Nuitka. A stock/wrong-ABI/contaminated sherpa payload must never enter a
    # newly packaged or reused sidecar build.
    _verify_installed_sherpa()

    if unsigned:
        print("[build] WARNING unsigned build, not release-ready")
        print("[build] Signing credentials NOT required (source profile).")
        cfg = None
    else:
        # --- Always-sign: resolve signing config or hard-error before any work. ---
        cfg = b.signing_config_from_env()
        if cfg is None:
            print(
                "ERROR: signing credentials missing. The official profile always signs.\n"
                "  Set AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, and AZURE_TENANT_ID\n"
                "  (typically via: op run --env-file=.env.build -- "
                "uv run --locked python scripts/build.py --profile official).\n"
                "  For an unsigned source installer use: "
                "uv run --locked python scripts/build.py",
                file=sys.stderr,
            )
            sys.exit(1)
        b.require_trusted_signing_cli()
        print(
            f"Code signing ENABLED (endpoint={cfg.endpoint} account={cfg.account} profile={cfg.profile})"
        )

    numeric = b.numeric_version(version)
    total = 11 if portable else 12

    print(f"Stamping src/whisperstream/_version.py with version={version}")
    b.stamp_version(version, numeric)
    print("Setting DEV_TOOLS_AVAILABLE=False (dev tools excluded from prod build)")
    b.disable_dev_tools()
    if experimental:
        print("[build] --experimental: keeping EXPERIMENTAL_AVAILABLE=True")
    else:
        print("Setting EXPERIMENTAL_AVAILABLE=False (gates no shipped feature; correct default for future ones)")
        b.disable_experimental()

    # The sentinel restores are always done in finally (replacing build.sh's
    # `trap cleanup EXIT`). This MUST run even on exception.
    try:
        if unsigned:
            # --unsigned: run the whole pipeline without a signing overlay.
            # Step 1: Nuitka onefile compile (or skip).
            _step_nuitka(version, numeric, total, skip_nuitka, experimental)
            # Step 2: smoke test only (no signing).
            _step_smoke_and_sign(None, total, unsigned=True)
            # Step 3: bundled legal documents.
            _step_derive_eula(total)
            # Step 4: frontend deps.
            _step_frontend_deps(total)
            # Step 5: licenses.
            _step_generate_licenses(total)
            # Step 6: stage sidecar.
            _step_stage_sidecar(total)
            # Step 7: stage llama-server.
            _step_stage_llama(total)
            # Step 8: build whisperstream-cli.
            cli_compiled = _step_build_cli(total)
            # Step 9: sign (skipped) and stage whisperstream-cli.
            _step_sign_and_stage_cli(None, total, cli_compiled, unsigned=True)
            # Step 10: build frontend.
            _step_build_frontend(total, experimental)
            # Step 11: Tauri build WITHOUT the signing overlay.
            print("")
            if portable:
                _step_tauri_portable_unsigned(total, experimental)
            else:
                _step_tauri_nsis_unsigned(
                    total,
                    experimental,
                    microsoft_store=microsoft_store,
                )
                if microsoft_store:
                    _stage_microsoft_store_artifact(version, total, signed=False)
                else:
                    print(
                        f"=== Step 12/{total}: Generate update manifest "
                        "(SKIPPED for unsigned build) ==="
                    )
                _copy_windows_installer_to_builds(
                    version, signed=False, microsoft_store=microsoft_store
                )
            print("[build] WARNING unsigned build, not release-ready")
        else:
            # Default always-signed path: wrap in signing overlay context manager.
            with b.signing_overlay(cfg.endpoint, cfg.account, cfg.profile) as overlay:
                # Step 1: Nuitka onefile compile (or skip).
                _step_nuitka(version, numeric, total, skip_nuitka, experimental)
                # Step 2: smoke test + manual sidecar sign.
                _step_smoke_and_sign(cfg, total)
                # Step 3: bundled legal documents.
                _step_derive_eula(total)
                b.validate_legal_release_documents()
                # Step 4: frontend deps.
                _step_frontend_deps(total)
                # Step 5: licenses.
                _step_generate_licenses(total)
                # Step 6: stage sidecar.
                _step_stage_sidecar(total)
                # Step 7: stage llama-server.
                _step_stage_llama(total)
                # Step 8: build whisperstream-cli.
                cli_compiled = _step_build_cli(total)
                # Step 9: sign and stage whisperstream-cli.
                _step_sign_and_stage_cli(cfg, total, cli_compiled)
                # Step 10: build frontend.
                _step_build_frontend(total)
                # Step 11: Tauri build (+ latest.json for NSIS).
                print("")
                if portable:
                    _step_tauri_portable(cfg, overlay, total, experimental)
                else:
                    _step_tauri_nsis(
                        overlay,
                        total,
                        experimental,
                        microsoft_store=microsoft_store,
                    )
                    if microsoft_store:
                        _stage_microsoft_store_artifact(version, total, signed=True)
                    else:
                        _generate_latest_json(version, total)
                    _copy_windows_installer_to_builds(
                        version, signed=True, microsoft_store=microsoft_store
                    )
    finally:
        # Restore all sentinels and remove the overlay no matter what. The
        # signing_overlay context manager already removed its file on exit; we
        # restore the version + dev-tools + experimental sentinels here so a
        # crash mid-build never leaves a dirty working tree. restore_experimental
        # is a no-op on an --experimental build (the flag was never flipped).
        b.restore_version_sentinel()
        b.restore_dev_tools()
        b.restore_experimental()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description=(
            "Build an unsigned source installer (default), or explicitly select "
            "the signed official installer with --profile official. Never uploads."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("source", "official"),
        default="source",
        help=(
            "source: unsigned installer, no credentials needed. official: signed "
            "installer; needs the signing credentials and the official build's providers."
        ),
    )
    parser.add_argument(
        "--portable",
        action="store_true",
        help="Assemble a portable folder instead of the NSIS installer (no upload artifacts).",
    )
    parser.add_argument(
        "--microsoft-store",
        action="store_true",
        help=(
            "Build the signed offline NSIS package and isolated Partner Center "
            "submission metadata. Does not upload or submit."
        ),
    )
    parser.add_argument(
        "--skip-nuitka",
        action="store_true",
        help="Reuse the existing dist/whisperstream-engine.exe (skips the 5-15 min C compile).",
    )
    parser.add_argument(
        "--unsigned",
        action="store_true",
        help=(
            "Accepted for existing commands; the source profile is already "
            "unsigned. NOT release-ready. Rejected by the official profile."
        ),
    )
    parser.add_argument(
        "--experimental",
        action="store_true",
        help=(
            "Keep EXPERIMENTAL_AVAILABLE=True and pass VITE_EXPERIMENTAL=1 to the "
            "frontend build. The gate holds no feature today (Agent Speak ships "
            "in a normal release build since 1.3.0), so this only "
            "matters once a new feature is placed behind it."
        ),
    )
    parser.add_argument(
        "--verify-sherpa-only",
        action="store_true",
        help=(
            "Run the fail-closed installed sherpa provenance, ABI, and native "
            "payload checks, then exit without starting a package build."
        ),
    )
    args = parser.parse_args(argv)
    if args.microsoft_store and args.portable:
        parser.error("--microsoft-store cannot be combined with --portable")
    if args.microsoft_store and args.experimental:
        parser.error("--microsoft-store cannot be combined with --experimental")
    if args.profile == "official" and args.unsigned:
        parser.error("--unsigned cannot be combined with --profile official")

    log_path = b.REPO_ROOT / "dist" / "build-signing.log"
    # Capture the real stdout/stderr BEFORE constructing the Tee and pass it as
    # the Tee's primary. Otherwise Tee.write falls back to the live sys.stdout,
    # which we reassign to the Tee below, producing infinite write recursion.
    original_stdout, original_stderr = sys.stdout, sys.stderr
    with b.Tee(log_path, primary=original_stdout) as tee:
        sys.stdout = tee
        sys.stderr = tee
        try:
            try:
                if args.verify_sherpa_only:
                    _verify_installed_sherpa()
                else:
                    with installer_profile(b.REPO_ROOT, args.profile):
                        _run_build(
                            args.portable,
                            args.skip_nuitka,
                            args.profile == "source",
                            args.experimental,
                            args.microsoft_store,
                        )
            except (
                BuildEnvironmentPreflightError,
                SherpaPreflightError,
                MicrosoftStorePackageError,
                b.LegalReleaseGateError,
                ValueError,
                FileExistsError,
            ) as exc:
                if isinstance(exc, FileExistsError):
                    prefix = (
                        "another build holds .installer-profile.lock (remove it "
                        "only after an interrupted build)"
                    )
                elif isinstance(exc, ValueError):
                    prefix = "installer configuration check failed"
                elif isinstance(exc, BuildEnvironmentPreflightError):
                    prefix = "build environment preflight failed"
                elif isinstance(exc, SherpaPreflightError):
                    prefix = "sherpa preflight failed"
                elif isinstance(exc, MicrosoftStorePackageError):
                    prefix = "Microsoft Store package failed"
                else:
                    prefix = "legal release gate failed"
                print(f"ERROR: {prefix}: {exc}", file=sys.stderr)
                return 1
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr
    return 0


if __name__ == "__main__":
    sys.exit(main())
