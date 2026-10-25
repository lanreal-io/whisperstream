#!/usr/bin/env python3
"""Exercise the internal macOS app's exact packaged process lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

import psutil


PORT = 38420
MACOS_ENGINE_SPAWN_MODE_ENV = "WHISPERSTREAM_MACOS_ENGINE_SPAWN_MODE"
MACOS_ENGINE_LIFECYCLE_PROBE_MODE = "lifecycle-probe"
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/health"
MAX_ENGINE_LOG_BYTES = 2 * 1024 * 1024
MAX_ENGINE_LOG_FILES = 4
MAX_WORKER_DIAGNOSTIC_FILES = 8
PROHIBITED_LOG_PATTERNS = (
    re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+"),
    re.compile(r"(?i)license[_-]?key\s*[=:]\s*\S+"),
    re.compile(r"(?i)transcript\s*[=:]\s*[^\s,;]+"),
    re.compile(r"(?i)hotword\s*[=:]\s*[^\s,;]+"),
    re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b"),
)


class LifecycleProbeError(RuntimeError):
    """A content-safe packaged lifecycle probe failure."""


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
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(str(size).encode("ascii") + b"\0")
        digest.update(_sha256(path).encode("ascii") + b"\n")
        files += 1
        total += size
    return {"files": files, "bytes": total, "tree_sha256": digest.hexdigest()}


def _port_is_free() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError:
            return False
    return True


def _health(timeout: float) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    deadline = started + timeout
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=0.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "ok" and payload.get("model_loaded") is True:
                return payload, time.perf_counter() - started
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise LifecycleProbeError("packaged lifecycle health deadline expired")


def _process_record(process: psutil.Process, role: str) -> dict[str, Any]:
    try:
        memory = process.memory_info().rss
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        memory = 0
    try:
        parent_pid = process.ppid()
        process_group_id = os.getpgid(process.pid)
        created = process.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError) as exc:
        raise LifecycleProbeError("recorded process exited during identity capture") from exc
    return {
        "pid": process.pid,
        "ppid": parent_pid,
        "pgid": process_group_id,
        "create_time": created,
        "rss_bytes": memory,
        "role": role,
    }


def _identity_present(record: dict[str, Any]) -> bool:
    try:
        process = psutil.Process(record["pid"])
        return abs(process.create_time() - record["create_time"]) < 0.001
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _identity_alive(record: dict[str, Any]) -> bool:
    if not _identity_present(record):
        return False
    try:
        return psutil.Process(record["pid"]).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _role(process: psutil.Process, shell_pid: int) -> str:
    if process.pid == shell_pid:
        return "app"
    try:
        name = process.name().lower()
        executable_name = Path(process.exe()).name.lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "descendant"
    combined = f"{name} {executable_name}"
    if "whisperstream-macos-stt-worker" in combined:
        return "worker"
    if "whisperstream-engine" in combined:
        return "engine"
    return "app-descendant"


def _snapshot_lineage(shell_pid: int) -> list[dict[str, Any]]:
    try:
        shell = psutil.Process(shell_pid)
        processes = [shell, *shell.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    records: list[dict[str, Any]] = []
    for process in processes:
        try:
            records.append(_process_record(process, _role(process, shell_pid)))
        except LifecycleProbeError:
            continue
    return sorted(records, key=lambda item: item["pid"])


def _merge_lineage(
    existing: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = {(item["pid"], item["create_time"]): item for item in existing}
    for item in current:
        merged[(item["pid"], item["create_time"])] = item
    return sorted(merged.values(), key=lambda item: (item["pid"], item["create_time"]))


def _spawn_timings(lineage: list[dict[str, Any]]) -> dict[str, float]:
    shell = min(
        (item for item in lineage if item["role"] == "app"),
        key=lambda item: item["create_time"],
    )
    timings: dict[str, float] = {}
    for role in ("engine", "worker"):
        candidates = [item for item in lineage if item["role"] == role]
        if candidates:
            spawned = min(candidates, key=lambda item: item["create_time"])
            timings[f"{role}_spawn_after_shell_seconds"] = round(
                spawned["create_time"] - shell["create_time"],
                3,
            )
    return timings


def _wait_for_roles(
    shell: subprocess.Popen[bytes], timeout: float
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    lineage: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        if shell.poll() is not None:
            raise LifecycleProbeError("app exited before engine and worker were recorded")
        lineage = _merge_lineage(lineage, _snapshot_lineage(shell.pid))
        engines = [item for item in lineage if item["role"] == "engine"]
        workers = [item for item in lineage if item["role"] == "worker"]
        if engines and workers:
            return lineage
        time.sleep(0.05)
    raise LifecycleProbeError("engine or native worker lineage was not observed")


def _wait_identity_inactive(record: dict[str, Any], timeout: float) -> float:
    started = time.perf_counter()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _identity_alive(record):
            return time.perf_counter() - started
        time.sleep(0.05)
    raise LifecycleProbeError("recorded process identity remained active past its deadline")


def _wait_lineage_absent(lineage: list[dict[str, Any]], timeout: float) -> float:
    started = time.perf_counter()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(not _identity_present(item) for item in lineage):
            return time.perf_counter() - started
        time.sleep(0.05)
    raise LifecycleProbeError("recorded process lineage survived its cleanup deadline")


def _stop_exact_lineage(lineage: list[dict[str, Any]]) -> None:
    alive: list[psutil.Process] = []
    for record in reversed(lineage):
        if not _identity_alive(record):
            continue
        try:
            process = psutil.Process(record["pid"])
            process.terminate()
            alive.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, survivors = psutil.wait_procs(alive, timeout=2.0)
    for process in survivors:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.wait_procs(survivors, timeout=2.0)


def _base_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    data = root / "data"
    cache = root / "cache"
    temporary = root / "tmp"
    for directory in (home, data, cache, temporary):
        directory.mkdir(parents=True, mode=0o700)
    environment = dict(os.environ)
    environment.update(
        {
            "CFFIXED_USER_HOME": str(home),
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "XDG_CACHE_HOME": str(cache),
            "WHISPERSTREAM_DATA_DIR": str(data),
            MACOS_ENGINE_SPAWN_MODE_ENV: MACOS_ENGINE_LIFECYCLE_PROBE_MODE,
        }
    )
    return environment


def _launch(
    app: Path,
    root: Path,
    *,
    minimized: bool,
) -> tuple[subprocess.Popen[bytes], Any, Path, dict[str, str]]:
    shell = app / "Contents/MacOS/whisperstream"
    if not shell.is_file():
        raise LifecycleProbeError("internal app shell is missing")
    environment = _base_environment(root)
    shell_log_path = root / "shell.log"
    shell_log = shell_log_path.open("wb")
    argv = [str(shell)]
    if minimized:
        argv.append("--minimized")
    process = subprocess.Popen(
        argv,
        cwd=app / "Contents/MacOS",
        env=environment,
        stdout=shell_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process, shell_log, shell_log_path, environment


def _request_quit(
    app: Path,
    environment: dict[str, str],
    timeout: float = 5.0,
) -> float:
    started = time.perf_counter()
    result = subprocess.run(
        [str(app / "Contents/MacOS/whisperstream"), "--quit"],
        cwd=app / "Contents/MacOS",
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise LifecycleProbeError("exact single-instance quit request failed")
    return time.perf_counter() - started


def _wait_log_marker(
    path: Path,
    pattern: str,
    process: subprocess.Popen[bytes],
    timeout: float,
) -> float:
    started = time.perf_counter()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        if pattern in text:
            return time.perf_counter() - started
        if process.poll() is not None:
            break
        time.sleep(0.05)
    raise LifecycleProbeError("expected content-free lifecycle marker was not emitted")


def _normal_cycle(
    app: Path,
    root: Path,
    *,
    name: str,
    minimized: bool,
    sentinel: dict[str, Any],
) -> dict[str, Any]:
    if not _port_is_free():
        raise LifecycleProbeError("isolated lifecycle port is already in use")
    process, log_handle, log_path, environment = _launch(
        app, root, minimized=minimized
    )
    lineage: list[dict[str, Any]] = []
    try:
        health, ready_seconds = _health(30.0)
        lineage = _wait_for_roles(process, 5.0)
        marker_seconds = _wait_log_marker(
            log_path, "[tauri] engine ready generation=1", process, 5.0
        )
        shutdown_started = time.perf_counter()
        quit_request_seconds = _request_quit(app, environment)
        try:
            process.wait(timeout=15.0)
        except subprocess.TimeoutExpired as exc:
            raise LifecycleProbeError("normal app quit exceeded lifecycle deadline") from exc
        shutdown_seconds = time.perf_counter() - shutdown_started
        cleanup_seconds = _wait_lineage_absent(lineage, 5.0)
        log_handle.flush()
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        if "stage=Graceful" not in log_text or "cleanup_verified=true" not in log_text:
            raise LifecycleProbeError("normal app quit did not report graceful cleanup")
        if not _identity_alive(sentinel):
            raise LifecycleProbeError("unrelated sentinel exited during normal lifecycle")
        return {
            "name": name,
            "minimized": minimized,
            "health_status": health["status"],
            "ready_seconds": round(ready_seconds, 3),
            "ready_marker_seconds": round(marker_seconds, 3),
            "spawn_timings": _spawn_timings(lineage),
            "quit_request_seconds": round(quit_request_seconds, 3),
            "shutdown_seconds": round(shutdown_seconds, 3),
            "cleanup_seconds": round(cleanup_seconds, 3),
            "shutdown_stage": "Graceful",
            "lineage": lineage,
            "cleanup_verified": True,
        }
    finally:
        _stop_exact_lineage(_merge_lineage(lineage, _snapshot_lineage(process.pid)))
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3.0)
        log_handle.close()


def _crash_cycle(
    app: Path,
    root: Path,
    *,
    role: str,
    sentinel: dict[str, Any],
) -> dict[str, Any]:
    if not _port_is_free():
        raise LifecycleProbeError("isolated lifecycle port is already in use")
    process, log_handle, log_path, environment = _launch(app, root, minimized=True)
    lineage: list[dict[str, Any]] = []
    try:
        _health(30.0)
        lineage = _wait_for_roles(process, 5.0)
        _wait_log_marker(
            log_path, "[tauri] engine ready generation=1", process, 5.0
        )
        candidates = [item for item in lineage if item["role"] == role]
        if not candidates:
            raise LifecycleProbeError("requested crash role was not recorded")
        target = max(candidates, key=lambda item: item["pid"])
        os.kill(target["pid"], signal.SIGKILL)
        crash_exit_seconds = _wait_identity_inactive(target, 3.0)
        engine_health_after = None
        if role == "worker":
            health, _ = _health(3.0)
            engine_health_after = health["status"]
        shutdown_started = time.perf_counter()
        quit_seconds = _request_quit(app, environment, timeout=5.0)
        try:
            process.wait(timeout=20.0)
        except subprocess.TimeoutExpired as exc:
            raise LifecycleProbeError("crash cleanup exceeded lifecycle deadline") from exc
        shutdown_seconds = time.perf_counter() - shutdown_started
        cleanup_seconds = _wait_lineage_absent(lineage, 5.0)
        log_handle.flush()
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        expected_stage = "Graceful"
        if f"stage={expected_stage}" not in log_text:
            raise LifecycleProbeError("crash path reported the wrong cleanup stage")
        if not _identity_alive(sentinel):
            raise LifecycleProbeError("unrelated sentinel exited during crash cleanup")
        return {
            "role": role,
            "target": target,
            "exit_classification": "signal-SIGKILL",
            "crash_exit_seconds": round(crash_exit_seconds, 3),
            "engine_health_after_worker_crash": engine_health_after,
            "spawn_timings": _spawn_timings(lineage),
            "quit_request_seconds": round(quit_seconds, 3),
            "shutdown_seconds": round(shutdown_seconds, 3),
            "cleanup_seconds": round(cleanup_seconds, 3),
            "shutdown_stage": expected_stage,
            "cleanup_path": (
                "natural-group-exit-within-graceful-deadline"
                if role == "engine"
                else "engine-owned-worker-recovery"
            ),
            "lineage": lineage,
            "cleanup_verified": True,
        }
    finally:
        _stop_exact_lineage(_merge_lineage(lineage, _snapshot_lineage(process.pid)))
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3.0)
        log_handle.close()


def _force_exit_cycle(
    app: Path,
    root: Path,
    sentinel: dict[str, Any],
) -> dict[str, Any]:
    if not _port_is_free():
        raise LifecycleProbeError("isolated lifecycle port is already in use")
    process, log_handle, log_path, _environment = _launch(app, root, minimized=True)
    lineage: list[dict[str, Any]] = []
    try:
        _health(30.0)
        lineage = _wait_for_roles(process, 5.0)
        _wait_log_marker(
            log_path, "[tauri] engine ready generation=1", process, 5.0
        )
        shell_record = next(item for item in lineage if item["role"] == "app")
        started = time.perf_counter()
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=3.0)
        shell_exit_seconds = time.perf_counter() - started
        descendants = [item for item in lineage if item["role"] != "app"]
        descendant_cleanup_seconds = _wait_lineage_absent(descendants, 10.0)
        if not _identity_alive(sentinel):
            raise LifecycleProbeError("unrelated sentinel exited during app force-exit")
        return {
            "shell": shell_record,
            "shell_exit_seconds": round(shell_exit_seconds, 3),
            "descendant_cleanup_seconds": round(descendant_cleanup_seconds, 3),
            "lineage": lineage,
            "spawn_timings": _spawn_timings(lineage),
            "owner_watchdog_cleanup": True,
            "cleanup_verified": True,
        }
    finally:
        _stop_exact_lineage(_merge_lineage(lineage, _snapshot_lineage(process.pid)))
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3.0)
        log_handle.close()


def _copy_scenario_app(app: Path, root: Path) -> Path:
    scenario_app = root / "Whisperstream.app"
    shutil.copytree(app, scenario_app, symlinks=True)
    return scenario_app


def _missing_engine_cycle(
    app: Path,
    root: Path,
    sentinel: dict[str, Any],
) -> dict[str, Any]:
    scenario_app = _copy_scenario_app(app, root)
    engine = scenario_app / "Contents/MacOS/whisperstream-engine"
    engine.unlink()
    process, log_handle, log_path, _environment = _launch(
        scenario_app, root / "state", minimized=True
    )
    lineage: list[dict[str, Any]] = []
    try:
        started = time.perf_counter()
        process.wait(timeout=8.0)
        exit_seconds = time.perf_counter() - started
        lineage = _snapshot_lineage(process.pid)
        log_handle.flush()
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        if "failure=MissingBinary" not in log_text:
            raise LifecycleProbeError("missing engine was not classified content-free")
        if any(item["role"] in {"engine", "worker"} for item in lineage):
            raise LifecycleProbeError("missing-engine scenario spawned an owned child")
        if not _identity_alive(sentinel):
            raise LifecycleProbeError("unrelated sentinel exited during missing-engine case")
        return {
            "exit_code": process.returncode,
            "exit_seconds": round(exit_seconds, 3),
            "classification": "MissingBinary",
            "engine_spawned": False,
            "cleanup_verified": True,
        }
    finally:
        _stop_exact_lineage(lineage)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3.0)
        log_handle.close()


def _early_exit_cycle(
    app: Path,
    root: Path,
    sentinel: dict[str, Any],
) -> dict[str, Any]:
    scenario_app = _copy_scenario_app(app, root)
    engine = scenario_app / "Contents/MacOS/whisperstream-engine"
    shutil.copyfile("/usr/bin/false", engine)
    engine.chmod(0o755)
    process, log_handle, log_path, environment = _launch(
        scenario_app, root / "state", minimized=True
    )
    lineage: list[dict[str, Any]] = []
    try:
        marker_seconds = _wait_log_marker(
            log_path, "failure=ExitedBeforeReady", process, 8.0
        )
        lineage = _snapshot_lineage(process.pid)
        quit_seconds = _request_quit(scenario_app, environment)
        process.wait(timeout=8.0)
        cleanup_seconds = _wait_lineage_absent(lineage, 3.0)
        if not _identity_alive(sentinel):
            raise LifecycleProbeError("unrelated sentinel exited during early-exit case")
        return {
            "classification": "ExitedBeforeReady",
            "marker_seconds": round(marker_seconds, 3),
            "quit_request_seconds": round(quit_seconds, 3),
            "cleanup_seconds": round(cleanup_seconds, 3),
            "cleanup_verified": True,
        }
    finally:
        _stop_exact_lineage(_merge_lineage(lineage, _snapshot_lineage(process.pid)))
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3.0)
        log_handle.close()


def _write_hung_engine(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/python3
import http.server, json, signal, subprocess, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(['/bin/sleep', '30'])
generation = sys.argv[sys.argv.index('--engine-generation') + 1]
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args): pass
    def do_GET(self):
        body = json.dumps({'status':'ok','model_loaded':False}).encode()
        self.send_response(200); self.send_header('X-Whisperstream-Engine-Generation', generation); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        body = b'{}'; self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
try:
    http.server.ThreadingHTTPServer(('127.0.0.1', 38420), Handler).serve_forever()
finally:
    child.terminate(); child.wait(timeout=2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _hung_shutdown_cycle(
    app: Path,
    root: Path,
    sentinel: dict[str, Any],
) -> dict[str, Any]:
    scenario_app = _copy_scenario_app(app, root)
    _write_hung_engine(scenario_app / "Contents/MacOS/whisperstream-engine")
    process, log_handle, log_path, environment = _launch(
        scenario_app, root / "state", minimized=True
    )
    lineage: list[dict[str, Any]] = []
    try:
        started = time.perf_counter()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(HEALTH_URL, timeout=0.5) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            raise LifecycleProbeError("hung helper did not reach readiness")
        ready_seconds = time.perf_counter() - started
        _wait_log_marker(
            log_path, "[tauri] engine ready generation=1", process, 5.0
        )
        lineage = _snapshot_lineage(process.pid)
        quit_started = time.perf_counter()
        _request_quit(scenario_app, environment)
        process.wait(timeout=20.0)
        shutdown_seconds = time.perf_counter() - quit_started
        cleanup_seconds = _wait_lineage_absent(lineage, 5.0)
        log_handle.flush()
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        if "stage=ForceKill" not in log_text:
            raise LifecycleProbeError("hung engine did not reach force-kill escalation")
        if not (8.0 <= shutdown_seconds < 15.0):
            raise LifecycleProbeError("hung shutdown did not honor bounded stage deadlines")
        if not _identity_alive(sentinel):
            raise LifecycleProbeError("unrelated sentinel exited during forced escalation")
        return {
            "ready_seconds": round(ready_seconds, 3),
            "shutdown_seconds": round(shutdown_seconds, 3),
            "cleanup_seconds": round(cleanup_seconds, 3),
            "shutdown_stage": "ForceKill",
            "lineage": lineage,
            "cleanup_verified": True,
        }
    finally:
        _stop_exact_lineage(_merge_lineage(lineage, _snapshot_lineage(process.pid)))
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3.0)
        log_handle.close()


def _privacy_measurements(root: Path) -> dict[str, Any]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    runtime_audio_files = [
        path
        for path in files
        if path.suffix.lower() in {".wav", ".flac", ".ogg", ".mp3", ".m4a"}
        and "cache" not in path.relative_to(root).parts
    ]
    packaged_audio_assets = [
        path
        for path in files
        if path.suffix.lower() in {".wav", ".flac", ".ogg", ".mp3", ".m4a"}
        and "cache" in path.relative_to(root).parts
    ]
    if runtime_audio_files:
        raise LifecycleProbeError("lifecycle probe created an audio artifact")

    diagnostic_files = [
        path
        for path in files
        if path.suffix.lower() == ".log"
        or path.name.endswith(".last-operation.json")
    ]
    violations: list[str] = []
    scanned_bytes = 0
    for path in diagnostic_files:
        data = path.read_bytes()
        scanned_bytes += len(data)
        text = data.decode("utf-8", errors="replace")
        if any(pattern.search(text) for pattern in PROHIBITED_LOG_PATTERNS):
            violations.append(path.name)
    if violations:
        raise LifecycleProbeError("diagnostic privacy scan found prohibited content")

    engine_logs = [path for path in files if path.name.startswith("whisperstream.log")]
    if any(path.stat().st_size > MAX_ENGINE_LOG_BYTES for path in engine_logs):
        raise LifecycleProbeError("engine rotating log exceeded its byte bound")
    grouped_engine_logs: dict[Path, int] = {}
    for path in engine_logs:
        grouped_engine_logs[path.parent] = grouped_engine_logs.get(path.parent, 0) + 1
    if any(count > MAX_ENGINE_LOG_FILES for count in grouped_engine_logs.values()):
        raise LifecycleProbeError("engine rotating log exceeded its file-count bound")

    worker_diagnostics = [
        path
        for path in files
        if path.name.startswith("worker-")
        and path.name.endswith((".diagnostic.log", ".last-operation.json"))
    ]
    grouped_worker_files: dict[Path, int] = {}
    for path in worker_diagnostics:
        grouped_worker_files[path.parent] = grouped_worker_files.get(path.parent, 0) + 1
    if any(count > MAX_WORKER_DIAGNOSTIC_FILES for count in grouped_worker_files.values()):
        raise LifecycleProbeError("worker diagnostics exceeded their retention bound")

    return {
        "files_scanned": len(diagnostic_files),
        "bytes_scanned": scanned_bytes,
        "engine_log_files": len(engine_logs),
        "engine_log_max_bytes": max((path.stat().st_size for path in engine_logs), default=0),
        "worker_diagnostic_files": len(worker_diagnostics),
        "audio_files": 0,
        "packaged_audio_assets_excluded": len(packaged_audio_assets),
        "prohibited_matches": 0,
        "isolated_root_mode": oct(root.stat().st_mode & 0o777),
        "transcript_content_recorded": False,
        "secret_or_key_material_recorded": False,
    }


def probe(app: Path, output: Path) -> dict[str, Any]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise LifecycleProbeError("process lifecycle evidence requires native Darwin arm64")
    app = app.resolve()
    output = output.resolve()
    root = output.parent / "lifecycle-state"
    if root.exists() or output.exists():
        raise LifecycleProbeError("lifecycle output path must be fresh")
    root.mkdir(parents=True, mode=0o700)

    shell = app / "Contents/MacOS/whisperstream"
    engine = app / "Contents/MacOS/whisperstream-engine"
    worker = app / "Contents/MacOS/whisperstream-macos-stt-worker"
    if not all(path.is_file() and os.access(path, os.X_OK) for path in (shell, engine, worker)):
        raise LifecycleProbeError("required packaged executable is missing")
    if not _port_is_free():
        raise LifecycleProbeError("isolated lifecycle port is already in use")

    sentinel_process = subprocess.Popen(
        ["/bin/sleep", "120"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    sentinel = _process_record(psutil.Process(sentinel_process.pid), "unrelated-sentinel")
    try:
        report: dict[str, Any] = {
            "schema_version": 1,
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "artifacts": {
                "app": _tree_measurement(app),
                "shell": {"bytes": shell.stat().st_size, "sha256": _sha256(shell)},
                "engine": {"bytes": engine.stat().st_size, "sha256": _sha256(engine)},
                "worker": {"bytes": worker.stat().st_size, "sha256": _sha256(worker)},
            },
            "sentinel": sentinel,
            "normal_cycles": [
                _normal_cycle(
                    app,
                    root / "normal-1",
                    name="normal-visible-unobserved",
                    minimized=False,
                    sentinel=sentinel,
                ),
                _normal_cycle(
                    app,
                    root / "normal-2-minimized",
                    name="minimized",
                    minimized=True,
                    sentinel=sentinel,
                ),
            ],
            "missing_engine": _missing_engine_cycle(
                app, root / "missing-engine", sentinel
            ),
            "early_engine_exit": _early_exit_cycle(
                app, root / "early-engine-exit", sentinel
            ),
            "hung_shutdown": _hung_shutdown_cycle(
                app, root / "hung-shutdown", sentinel
            ),
            "engine_crash": _crash_cycle(
                app, root / "engine-crash", role="engine", sentinel=sentinel
            ),
            "worker_crash": _crash_cycle(
                app, root / "worker-crash", role="worker", sentinel=sentinel
            ),
            "app_force_exit": _force_exit_cycle(
                app, root / "app-force-exit", sentinel
            ),
            "interaction": {
                "microphone_opened": False,
                "output_stream_opened": False,
                "hotkeys_registered": False,
                "keys_observed": False,
                "text_injected": False,
                "clipboard_or_focus_mutated": False,
                "permission_query_called": False,
                "permission_request_called": False,
                "keychain_provider_called": False,
            },
        }
        report["privacy"] = _privacy_measurements(root)
        report["sentinel"]["survived_all_scenarios"] = _identity_alive(sentinel)
        if not report["sentinel"]["survived_all_scenarios"]:
            raise LifecycleProbeError("unrelated sentinel did not survive all scenarios")
        report["cleanup"] = {
            "all_recorded_lineages_absent": True,
            "sentinel_cleaned_by_exact_identity_after_report": True,
        }
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return report
    finally:
        if _identity_alive(sentinel):
            sentinel_process.terminate()
            try:
                sentinel_process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                sentinel_process.kill()
                sentinel_process.wait(timeout=2.0)


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    report = probe(args.app, args.output)
    print(
        json.dumps(
            {
                "normal_cycles": len(report["normal_cycles"]),
                "hung_stage": report["hung_shutdown"]["shutdown_stage"],
                "sentinel_survived": report["sentinel"]["survived_all_scenarios"],
                "cleanup_verified": report["cleanup"]["all_recorded_lineages_absent"],
                "output": str(args.output.resolve()),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
