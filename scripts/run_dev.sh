#!/usr/bin/env bash
# Start the source backend and Tauri shell on native Apple Silicon.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="$ROOT/frontend"
TAURI_DIR="$ROOT/src-tauri"
WORKER_DIR="$ROOT/native/macos-stt-worker"
PORT=38420

NO_TAURI=0
NO_HOTKEYS=0
BOOTSTRAP_ONLY=0
DATA_DIR=""

usage() {
  cat <<'EOF'
Usage: scripts/run_dev.sh [options]

Options:
  --no-tauri                  Start the backend and Vite without the native shell
  --no-hotkeys                Disable global hotkeys in the source backend
  --bootstrap-only            Install and stage dependencies, then exit
  --experimental              Enable experimental development surfaces
  --data-dir PATH             Override isolated development state
  -h, --help                  Show this help
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-tauri) NO_TAURI=1 ;;
    --no-hotkeys) NO_HOTKEYS=1 ;;
    --bootstrap-only) BOOTSTRAP_ONLY=1 ;;
    --experimental) export WHISPERSTREAM_EXPERIMENTAL=1 ;;
    --data-dir)
      shift
      [ "$#" -gt 0 ] || { echo "--data-dir requires a path" >&2; exit 2; }
      DATA_DIR="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "scripts/run_dev.sh requires native Apple Silicon macOS." >&2
  exit 1
fi

for tool in cargo curl node npm uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Required command is missing: $tool" >&2
    exit 1
  fi
done

export WHISPERSTREAM_AGENT_PLUGIN_VARIANT=dev
if [ -n "$DATA_DIR" ]; then
  export WHISPERSTREAM_DATA_DIR="$DATA_DIR"
elif [ -z "${WHISPERSTREAM_DATA_DIR:-}" ]; then
  export WHISPERSTREAM_DATA_DIR="$HOME/Library/Application Support/Whisperstream-dev"
fi

# Never use installed application state for development.
DATA_PATH="$WHISPERSTREAM_DATA_DIR" python3 <<'PYDATA'
import os
from pathlib import Path
candidate = Path(os.environ["DATA_PATH"]).expanduser().resolve()
installed = (Path.home() / "Library/Application Support/Whisperstream").resolve()
if candidate == installed or installed in candidate.parents:
    raise SystemExit("Use a separate development data directory.")
PYDATA

native_node_bindings_valid() {
  FRONTEND_DIR="$FRONTEND" node <<'JS'
const fs = require("node:fs");
const path = require("node:path");

const root = process.env.FRONTEND_DIR;
const lock = JSON.parse(fs.readFileSync(path.join(root, "package-lock.json"), "utf8"));
const packages = [
  "@tauri-apps/cli-darwin-arm64",
  "@rollup/rollup-darwin-arm64",
  "@esbuild/darwin-arm64",
  "@tailwindcss/oxide-darwin-arm64",
  "lightningcss-darwin-arm64",
];
let valid = true;
for (const name of packages) {
  const expected = lock.packages?.[`node_modules/${name}`]?.version;
  const installedPath = path.join(root, "node_modules", name, "package.json");
  let installed;
  if (fs.existsSync(installedPath)) {
    installed = JSON.parse(fs.readFileSync(installedPath, "utf8")).version;
  }
  if (!expected || installed !== expected) {
    console.error(`[mac-dev] native Node binding mismatch package=${name} expected=${expected || "missing-lock"} installed=${installed || "missing"}`);
    valid = false;
  }
}
process.exit(valid ? 0 : 1);
JS
}

install_native_node_bindings() {
  local specs
  specs="$(FRONTEND_DIR="$FRONTEND" node <<'JS'
const fs = require("node:fs");
const path = require("node:path");
const root = process.env.FRONTEND_DIR;
const lock = JSON.parse(fs.readFileSync(path.join(root, "package-lock.json"), "utf8"));
const packages = [
  "@tauri-apps/cli-darwin-arm64",
  "@rollup/rollup-darwin-arm64",
  "@esbuild/darwin-arm64",
  "@tailwindcss/oxide-darwin-arm64",
  "lightningcss-darwin-arm64",
];
for (const name of packages) {
  const version = lock.packages?.[`node_modules/${name}`]?.version;
  if (!version) throw new Error(`native package missing from lockfile: ${name}`);
  console.log(`${name}@${version}`);
}
JS
)"
  echo "[mac-dev] repairing pinned Apple Silicon Node bindings"
  # Package specs contain no whitespace. Intentional splitting passes each
  # lock-derived name@version as one npm argument on macOS Bash 3.2.
  # shellcheck disable=SC2086
  (cd "$FRONTEND" && npm install --no-save --package-lock=false --os=darwin --cpu=arm64 $specs)
}

if [ ! -f "$FRONTEND/node_modules/vite/package.json" ] || [ ! -f "$FRONTEND/node_modules/@tauri-apps/cli/package.json" ]; then
  echo "[mac-dev] frontend dependencies missing, running npm ci"
  (cd "$FRONTEND" && npm ci --include=optional --os=darwin --cpu=arm64)
fi
if ! native_node_bindings_valid; then
  install_native_node_bindings
  native_node_bindings_valid
fi

# The locked official sherpa-onnx wheels serve every sherpa STT family and
# Supertonic, so an ordinary `uv run` leaves this environment unchanged.
(cd "$ROOT" && uv sync --locked --extra dev)
(cd "$ROOT" && uv run --no-sync python scripts/check-sherpa-source.py)

WORKER="$WORKER_DIR/target/release/whisperstream-macos-stt-worker"
WORKER_NEEDS_BUILD=0
if [ ! -x "$WORKER" ]; then
  WORKER_NEEDS_BUILD=1
elif find "$WORKER_DIR/src" "$WORKER_DIR/Cargo.toml" "$WORKER_DIR/Cargo.lock" -type f -newer "$WORKER" -print -quit | grep -q .; then
  WORKER_NEEDS_BUILD=1
fi
if [ "$WORKER_NEEDS_BUILD" -eq 1 ]; then
  echo "[mac-dev] building the native macOS STT worker"
  MACOSX_DEPLOYMENT_TARGET=14.0 cargo build --release --locked \
    --manifest-path "$WORKER_DIR/Cargo.toml"
fi

BINARIES="$TAURI_DIR/binaries"
mkdir -p "$BINARIES"
WORKER_SIDECAR="$BINARIES/whisperstream-macos-stt-worker-aarch64-apple-darwin"
if [ ! -f "$WORKER_SIDECAR" ] || ! cmp -s "$WORKER" "$WORKER_SIDECAR"; then
  cp "$WORKER" "$WORKER_SIDECAR"
  chmod u+x "$WORKER_SIDECAR"
fi

# Debug builds never spawn the packaged engine, but Tauri validates every
# externalBin path before compiling.
ENGINE_STUB="$BINARIES/whisperstream-engine-aarch64-apple-darwin"
if [ ! -e "$ENGINE_STUB" ]; then
  : > "$ENGINE_STUB"
  chmod u+x "$ENGINE_STUB"
fi

LLAMA_SIDECAR="$BINARIES/llama-server-aarch64-apple-darwin"
LLAMA_LIBRARY="$TAURI_DIR/resources/llama-cpp/libllama.dylib"
if [ ! -x "$LLAMA_SIDECAR" ] || [ ! -f "$LLAMA_LIBRARY" ]; then
  echo "[mac-dev] staging the pinned Metal llama-server and dylibs"
  (cd "$ROOT" && uv run --no-sync python scripts/fetch_macos_llama_server.py)
fi

# This is the standalone terminal wrapper, not a packaged application. Tauri
# validates every staged externalBin above before compiling this crate.
(cd "$TAURI_DIR" && MACOSX_DEPLOYMENT_TARGET=14.0 cargo build --locked --bin whisperstream-cli)
mkdir -p "$TAURI_DIR/bin"
cp "$TAURI_DIR/target/debug/whisperstream-cli" "$TAURI_DIR/bin/whisperstream"

if [ "$BOOTSTRAP_ONLY" -eq 1 ]; then
  echo "[mac-dev] bootstrap complete"
  exit 0
fi

if command -v lsof >/dev/null 2>&1 && lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Stop the existing backend first." >&2
  exit 1
fi
if command -v lsof >/dev/null 2>&1 && lsof -tiTCP:1420 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 1420 is already in use. Stop the existing Vite/Tauri session first." >&2
  exit 1
fi

BACKEND_ARGS=(uv run --no-sync python -m whisperstream.main --serve --port "$PORT")
if [ "$NO_HOTKEYS" -eq 1 ]; then
  BACKEND_ARGS+=(--no-hotkeys)
fi

BACKEND_PID=""
BACKEND_CREATED=""
PROCESS_OWNER="$ROOT/scripts/macos_dev_process.py"
PROCESS_PYTHON="$ROOT/.venv/bin/python"
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [ -n "$BACKEND_PID" ] && [ -n "$BACKEND_CREATED" ]; then
    "$PROCESS_PYTHON" "$PROCESS_OWNER" stop "$BACKEND_PID" --created "$BACKEND_CREATED" || true
    wait "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

echo "[mac-dev] starting backend with data dir: $WHISPERSTREAM_DATA_DIR"
(cd "$ROOT" && exec "${BACKEND_ARGS[@]}") &
BACKEND_PID=$!
BACKEND_CREATED="$("$PROCESS_PYTHON" "$PROCESS_OWNER" identity "$BACKEND_PID" --parent "$$")"

BACKEND_READY=0
for ((attempt = 0; attempt < 120; attempt++)); do
  if "$PROCESS_PYTHON" "$PROCESS_OWNER" ready "$BACKEND_PID" --created "$BACKEND_CREATED" --port "$PORT" && curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    BACKEND_READY=1
    break
  fi
  if ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    wait "$BACKEND_PID"
    exit 1
  fi
  sleep 0.25
done
if [ "$BACKEND_READY" -ne 1 ]; then
  echo "Backend did not become ready on port $PORT." >&2
  exit 1
fi

if [ "$NO_TAURI" -eq 1 ]; then
  echo "[mac-dev] starting Vite at http://localhost:1420"
  (cd "$FRONTEND" && npm run dev)
else
  echo "[mac-dev] starting Tauri with the repo-local pinned CLI"
  # Match the CLI build's deployment target so the shared target directory
  # keeps its native dependencies instead of rebuilding them every launch.
  (cd "$TAURI_DIR" && MACOSX_DEPLOYMENT_TARGET=14.0 "$FRONTEND/node_modules/.bin/tauri" dev)
fi
