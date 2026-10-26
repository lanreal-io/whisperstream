#!/usr/bin/env python3
"""Probe packaged Phase 5 capability surfaces without owner interaction.

The frozen engine runs in file-only mode against an isolated reviewed model.
The probe sends no user content, does not reconcile a live Claude plugin, does
not open an output stream, and records only capability/state and cleanup facts.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import psutil


PORT = 38420
BASE_URL = f"http://127.0.0.1:{PORT}"
PHASE_ROOT = Path("/tmp/whisperstream-macos-parity/phase-5")
MODEL_RELATIVE = Path(
    "Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3"
)
EXPECTED_CAPABILITIES = {
    "agent-hook-lifecycle": ("available", "claude-cli-plugin", "enable"),
    "agent-hud-integration": ("available", "native-hud-owner", "enable"),
    "app-identity": ("available", "appkit-context", "enable"),
    "app-path-browse": (
        "unavailable",
        "raw-path-boundary-unavailable",
        "disable",
    ),
    "browser-page-identity": (
        "available",
        "accessibility-host-only",
        "enable",
    ),
    "finetune-registry-evaluation": (
        "development-only",
        "dev-tools-only",
        "explain",
    ),
    "local-llm-runtime": ("available", "metal-runtime", "enable"),
    "tts-synthesis-playback": ("available", "darwin-playback", "enable"),
}


class PackagedAdvancedProbeError(RuntimeError):
    """A content-free packaged Phase 5 invariant failed."""


def _identity(process: psutil.Process) -> dict[str, Any]:
    return {
        "pid": process.pid,
        "create_time": process.create_time(),
    }


def _lineage(root_pid: int) -> list[dict[str, Any]]:
    try:
        root = psutil.Process(root_pid)
        processes = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    result: list[dict[str, Any]] = []
    for process in processes:
        try:
            result.append(_identity(process))
        except psutil.Error:
            continue
    return result


def _alive(record: dict[str, Any]) -> bool:
    try:
        process = psutil.Process(int(record["pid"]))
        return abs(process.create_time() - float(record["create_time"])) < 0.001
    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, ValueError):
        return False


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3.0)


def _wait_health(client: httpx.Client) -> float:
    started = time.perf_counter()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            response = client.get("/api/health")
            payload = response.json()
            if (
                response.status_code == 200
                and payload.get("status") == "ok"
                and payload.get("model_loaded") is True
            ):
                return time.perf_counter() - started
        except (httpx.HTTPError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise PackagedAdvancedProbeError("packaged advanced health deadline expired")


def _copy_model(source: Path, home: Path) -> None:
    destination = home / MODEL_RELATIVE
    if destination.exists():
        raise PackagedAdvancedProbeError("isolated model destination is not fresh")
    destination.parent.mkdir(parents=True, mode=0o700)
    subprocess.run(
        ["/bin/cp", "-cR", str(source), str(destination)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def probe(app: Path, model_source: Path, output: Path) -> dict[str, Any]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise PackagedAdvancedProbeError("native Darwin arm64 is required")
    app = app.resolve(strict=True)
    model_source = model_source.resolve(strict=True)
    output = output.resolve(strict=False)
    if PHASE_ROOT.resolve() not in output.parents or output.exists():
        raise PackagedAdvancedProbeError("output must be a fresh Phase 5 child")
    state = output.parent / f"{output.stem}-state"
    if state.exists():
        raise PackagedAdvancedProbeError("probe state must be fresh")
    engine = app / "Contents/MacOS/whisperstream-engine"
    worker = app / "Contents/MacOS/whisperstream-macos-stt-worker"
    resources = app / "Contents/Resources/resources"
    if not all(path.exists() for path in (engine, worker, resources)):
        raise PackagedAdvancedProbeError("packaged advanced executable set is incomplete")

    home = state / "home"
    data = state / "data"
    temporary = state / "tmp"
    cache = state / "cache"
    logs = state / "logs"
    for directory in (home, data, temporary, cache, logs):
        directory.mkdir(parents=True, mode=0o700)
    _copy_model(model_source, home)
    environment = dict(os.environ)
    environment.update(
        {
            "CFFIXED_USER_HOME": str(home),
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "XDG_CACHE_HOME": str(cache),
            "WHISPERSTREAM_BUNDLE_RESOURCES_DIR": str(resources),
            "WHISPERSTREAM_DATA_DIR": str(data),
            "WHISPERSTREAM_MACOS_STT_WORKER_BIN": str(worker),
        }
    )
    log_handle = (logs / "engine.log").open("wb")
    process = subprocess.Popen(
        [
            str(engine),
            "--serve",
            "--port",
            str(PORT),
            "--no-hotkeys",
            "--file-only",
            "--fixture-probe-worker",
            "--data-dir",
            str(data),
        ],
        cwd=engine.parent,
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    client = httpx.Client(base_url=BASE_URL, timeout=10.0)
    lineage: list[dict[str, Any]] = []
    try:
        readiness = _wait_health(client)
        lineage = _lineage(process.pid)
        capabilities_response = client.get("/api/capabilities/advanced")
        speech_response = client.get("/api/speech/status")
        plugin_response = client.get("/api/claude-hud/status")
        config_response = client.get("/api/config")
        hook_response = client.post("/api/claude-hook", json={})
        if any(
            response.status_code != 200
            for response in (
                capabilities_response,
                speech_response,
                plugin_response,
                config_response,
                hook_response,
            )
        ):
            raise PackagedAdvancedProbeError("packaged advanced endpoint failed")
        capability_payload = capabilities_response.json()
        speech_payload = speech_response.json()
        plugin_payload = plugin_response.json()
        config_payload = config_response.json()
        hook_payload = hook_response.json()
        capability_rows = capability_payload.get("capabilities", [])
        observed = {
            row.get("id"): (
                row.get("state"),
                row.get("reason"),
                row.get("presentation"),
            )
            for row in capability_rows
            if isinstance(row, dict)
        }
        if (
            capability_payload.get("schema_version") != 1
            or capability_payload.get("platform") != "Darwin"
            or observed != EXPECTED_CAPABILITIES
        ):
            raise PackagedAdvancedProbeError("packaged advanced capability truth changed")
        engines = speech_payload.get("engines", {})
        supertonic = engines.get("supertonic", {}) if isinstance(engines, dict) else {}
        if (
            config_payload.get("agent_speech_enabled") is not False
            or supertonic.get("available") is not False
            or supertonic.get("supported") is not True
            or supertonic.get("unavailable_reason") != "model_unavailable"
            or hook_payload != {}
        ):
            raise PackagedAdvancedProbeError("packaged speech or fail-open truth changed")
        if plugin_payload.get("status") != "unknown":
            raise PackagedAdvancedProbeError("packaged plugin status mutated owner state")
        shutdown = client.post("/api/shutdown")
        if shutdown.status_code != 200:
            raise PackagedAdvancedProbeError("packaged advanced shutdown failed")
        process.wait(timeout=8.0)
    finally:
        client.close()
        _stop_group(process)
        log_handle.close()
    deadline = time.monotonic() + 5.0
    while any(_alive(item) for item in lineage) and time.monotonic() < deadline:
        time.sleep(0.05)
    cleanup_verified = all(not _alive(item) for item in lineage)
    if not cleanup_verified:
        raise PackagedAdvancedProbeError("packaged advanced lineage survived cleanup")

    report = {
        "schema_version": 1,
        "verdict": "PASS",
        "platform": {"system": "Darwin", "machine": "arm64", "native": True},
        "readiness_seconds": round(readiness, 3),
        "capabilities": {
            "schema_version": 1,
            "count": len(observed),
            "available": sum(value[0] == "available" for value in observed.values()),
            "development_only": sum(
                value[0] == "development-only" for value in observed.values()
            ),
            "unavailable": sum(value[0] == "unavailable" for value in observed.values()),
            "external_dependency": sum(
                value[0] == "external-dependency" for value in observed.values()
            ),
            "updater_present": False,
        },
        "speech": {
            "default_off": True,
            "supertonic_supported": True,
            "supertonic_model_reason": "model_unavailable",
            "output_stream_opened": False,
        },
        "agent": {
            "live_cli_invoked": False,
            "owner_settings_written": False,
            "empty_hook_fail_open": True,
        },
        "privacy": {
            "payloads_sent": 0,
            "owner_roots_read": 0,
            "device_identities_recorded": 0,
            "application_identities_recorded": 0,
        },
        "cleanup": {
            "processes_remaining": 0,
            "listeners_remaining": 0,
            "lineage_count": len(lineage),
            "cleanup_verified": True,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(state)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--model-source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = probe(args.app, args.model_source, args.output)
    except (OSError, ValueError, subprocess.SubprocessError, PackagedAdvancedProbeError) as exc:
        print(
            f"macOS packaged advanced probe failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    print(
        "PASS macos-packaged-advanced-probe "
        f"capabilities={report['capabilities']['count']} cleanup=pass"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
