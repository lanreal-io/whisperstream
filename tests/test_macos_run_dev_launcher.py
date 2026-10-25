from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "run_dev.sh"
FRONTEND = ROOT / "frontend"


def test_macos_launcher_uses_repo_local_pins() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env bash\n")
    assert '"$FRONTEND/node_modules/.bin/tauri" dev' in source
    assert "npm ci --include=optional" in source
    assert "--os=darwin --cpu=arm64" in source
    assert "package-lock.json" in source
    assert "scripts/fetch_macos_llama_server.py" in source
    assert "native/macos-stt-worker" in source
    assert "cargo install" not in source


def test_macos_launcher_uses_locked_official_sherpa_wheels() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "uv sync --locked --extra dev)" in source
    assert "scripts/check-sherpa-source.py" in source
    assert "build_macos_sherpa" not in source
    assert "--inexact" not in source
    assert ".local-dependencies" not in source


def test_frontend_lock_pins_native_bindings_for_ci_and_supported_hosts() -> None:
    assert not (FRONTEND / ".npmrc").exists()
    lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock["packages"]

    required = {
        "node_modules/@tauri-apps/cli-darwin-arm64",
        "node_modules/@tauri-apps/cli-linux-x64-gnu",
        "node_modules/@tauri-apps/cli-win32-x64-msvc",
        "node_modules/@rollup/rollup-darwin-arm64",
        "node_modules/@rollup/rollup-linux-x64-gnu",
        "node_modules/@rollup/rollup-win32-x64-msvc",
        "node_modules/@esbuild/darwin-arm64",
        "node_modules/@esbuild/linux-x64",
        "node_modules/@esbuild/win32-x64",
        "node_modules/@tailwindcss/oxide-darwin-arm64",
        "node_modules/@tailwindcss/oxide-linux-x64-gnu",
        "node_modules/@tailwindcss/oxide-win32-x64-msvc",
        "node_modules/lightningcss-darwin-arm64",
        "node_modules/lightningcss-linux-x64-gnu",
        "node_modules/lightningcss-win32-x64-msvc",
    }
    assert required <= packages.keys()


def test_launcher_readiness_and_cleanup_are_bound_to_process_generation():
    source = LAUNCHER.read_text(encoding="utf-8")
    assert 'exec "${BACKEND_ARGS[@]}"' in source
    assert 'identity "$BACKEND_PID" --parent "$$"' in source
    assert 'ready "$BACKEND_PID" --created "$BACKEND_CREATED"' in source
    assert 'stop "$BACKEND_PID" --created "$BACKEND_CREATED"' in source
    assert '/api/shutdown' not in source
