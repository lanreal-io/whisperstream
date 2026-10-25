#!/usr/bin/env python3
"""Run the internal bundle's no-interaction frontend and backend health probe.

The default launch stays minimized for query-only capability evidence. Opt in
to a visible main-WebView hydration proof with ``--prove-main-webview``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Iterable

import psutil


PORT = 38420
REPO_ROOT = Path(__file__).resolve().parents[1]
MACOS_ENGINE_SPAWN_MODE_ENV = "WHISPERSTREAM_MACOS_ENGINE_SPAWN_MODE"
MACOS_ENGINE_LIFECYCLE_PROBE_MODE = "lifecycle-probe"
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/health"
PERMISSIONS_URL = f"http://127.0.0.1:{PORT}/api/permissions"
SHUTDOWN_URL = f"http://127.0.0.1:{PORT}/api/shutdown"
LOG_EXPORT_URL = f"http://127.0.0.1:{PORT}/api/logs/export"
EULA_STATUS_URL = f"http://127.0.0.1:{PORT}/api/eula/status"
PERMISSION_KINDS = ("microphone", "accessibility", "input-monitoring")
PERMISSION_STATES = {
    "not-determined",
    "granted",
    "denied",
    "restricted",
    "not-required",
    "unsupported",
    "unknown",
}
MACOS_STT_MODEL_NAME = "parakeet-tdt-0.6b-v3"
MACOS_STT_REQUIRED_ASSETS = (
    "Preprocessor.mlmodelc/model.mil",
    "Encoder.mlmodelc/model.mil",
    "Decoder.mlmodelc/model.mil",
    "JointDecisionv3.mlmodelc/model.mil",
    "parakeet_v3_vocab.json",
)
REAL_ENGINE_ARGUMENTS = (
    "--serve",
    "--port",
    str(PORT),
    "--owner-pid",
    "<owner-pid>",
    "--engine-generation",
    "<generation-token>",
)
PROBE_ONLY_ENGINE_FLAGS = (
    "--no-hotkeys",
    "--file-only",
    "--lifecycle-probe-worker",
)


class LaunchProbeError(RuntimeError):
    """A content-safe launch probe failure."""


def _probe_spawn_environment() -> dict[str, str]:
    return {
        MACOS_ENGINE_SPAWN_MODE_ENV: MACOS_ENGINE_LIFECYCLE_PROBE_MODE,
    }


def _spawn_environment(
    inherited: dict[str, str], *, real_dictation_runtime: bool
) -> dict[str, str]:
    """Return a launch environment with one unambiguous spawn-mode contract."""

    environment = dict(inherited)
    if real_dictation_runtime:
        environment.pop(MACOS_ENGINE_SPAWN_MODE_ENV, None)
    else:
        environment.update(_probe_spawn_environment())
    return environment


def _validate_real_engine_arguments(
    arguments: list[str], *, owner_pid: int
) -> dict[str, Any]:
    """Validate and sanitize the selector-absent packaged engine vector."""

    if len(arguments) != len(REAL_ENGINE_ARGUMENTS):
        raise LaunchProbeError("real packaged engine argument count changed")
    if arguments[:4] != ["--serve", "--port", str(PORT), "--owner-pid"]:
        raise LaunchProbeError("real packaged engine argument prefix changed")
    if arguments[4] != str(owner_pid):
        raise LaunchProbeError("real packaged engine owner binding changed")
    if arguments[5] != "--engine-generation" or not arguments[6]:
        raise LaunchProbeError("real packaged engine generation binding changed")
    if any(flag in arguments for flag in PROBE_ONLY_ENGINE_FLAGS):
        raise LaunchProbeError("real packaged engine inherited a probe-only flag")
    return {
        "arguments": list(REAL_ENGINE_ARGUMENTS),
        "owner_pid_matches_shell": True,
        "generation_token_present": True,
        "probe_flags_absent": True,
    }


def _real_engine_spawn_contract(shell_pid: int) -> dict[str, Any]:
    """Observe the exact shell-owned engine argv without recording its token."""

    try:
        root = psutil.Process(shell_pid)
        processes = root.children(recursive=True)
    except psutil.Error as exc:
        raise LaunchProbeError("real packaged engine lineage was unavailable") from exc
    for process in processes:
        try:
            command = process.cmdline()
        except psutil.Error:
            continue
        arguments = command[1:]
        if arguments[:3] == ["--serve", "--port", str(PORT)]:
            return _validate_real_engine_arguments(arguments, owner_pid=shell_pid)
    raise LaunchProbeError("real packaged engine argv was not observed")


def _real_worker_identity(shell_pid: int) -> dict[str, Any]:
    """Return content-free identity for the native FluidAudio worker."""

    try:
        root = psutil.Process(shell_pid)
        processes = root.children(recursive=True)
    except psutil.Error as exc:
        raise LaunchProbeError("real packaged worker lineage was unavailable") from exc
    for process in processes:
        try:
            executable = Path(process.exe()).name
            if executable != "whisperstream-macos-stt-worker":
                continue
            return {
                "pid": process.pid,
                "ppid": process.ppid(),
                "pgid": os.getpgid(process.pid),
                "create_time": process.create_time(),
                "role": "native-stt-worker",
            }
        except (OSError, psutil.Error):
            continue
    raise LaunchProbeError("real packaged native worker was not observed")


def _prepare_real_runtime(model_source: Path, home: Path, data: Path) -> Path:
    """Prepare model and fail-closed persistence state without native secrets."""

    source = model_source.resolve()
    if not source.is_dir() or any(path.is_symlink() for path in source.rglob("*")):
        raise LaunchProbeError("real runtime model source is absent or contains symlinks")
    if any(not (source / relative).is_file() for relative in MACOS_STT_REQUIRED_ASSETS):
        raise LaunchProbeError("real runtime model source is incomplete")
    model_root = (
        home
        / "Library"
        / "Application Support"
        / "FluidAudio"
        / "Models"
        / MACOS_STT_MODEL_NAME
    )
    model_root.parent.mkdir(parents=True, mode=0o700)
    subprocess.run(
        ["/bin/cp", "-cR", str(source), str(model_root)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if any(not (model_root / relative).is_file() for relative in MACOS_STT_REQUIRED_ASSETS):
        raise LaunchProbeError("real runtime model clone is incomplete")

    # Presence of the lockdown envelope is the production TranscriptStore gate:
    # it starts history locked with no platform-provider key lookup or creation.
    # The deliberately empty marker is never unlocked and is discarded with this
    # disposable launch root. Config keeps every native key binding unbound so
    # this automated launch cannot open an event tap or request TCC access.
    (data / "lockdown.key.json").touch(mode=0o600)
    (data / "config.toml").write_text(
        "\n".join(
            (
                'primary_shortcut = ""',
                'push_to_talk_shortcut = ""',
                'toggle_shortcut = ""',
                'paste_last_shortcut = ""',
                'paste_last_original_shortcut = ""',
                'claude_view_shortcut = ""',
                "history_enabled = false",
                "",
                "[profile_hotkeys]",
                "",
            )
        ),
        encoding="utf-8",
    )
    return model_root


def _assert_protected_storage_untouched(data: Path) -> None:
    """Reject evidence that the isolated real launch selected protected storage."""

    if (data / "transcript.key").exists():
        raise LaunchProbeError("real launch created a protected transcript key")
    for path in sorted(item for item in data.rglob("*") if item.is_file()):
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        if b"secret.v1" in payload:
            raise LaunchProbeError("real launch persisted a protected-secret reference")


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
    raise LaunchProbeError("packaged engine health deadline expired")


def _post_shutdown() -> None:
    request = urllib.request.Request(SHUTDOWN_URL, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=1.0) as response:
            if response.status != 200:
                raise LaunchProbeError("packaged engine shutdown request failed")
    except ConnectionResetError:
        # The server can close its loopback socket immediately after committing
        # the successful response. The caller still requires the exact engine
        # process to exit, so a reset without shutdown cannot pass the probe.
        return


def _shutdown_exact_engine(process: subprocess.Popen[bytes]) -> None:
    exited = process.poll() is not None
    if not exited:
        try:
            _post_shutdown()
        except (OSError, urllib.error.URLError):
            # The packaged frontend also requests shutdown when the exact shell
            # quits. A refused follow-up is valid only after this exact engine
            # process independently reaches exit.
            exited = _wait_exit(process, 1.0)
            if not exited:
                raise
    if not exited and not _wait_exit(process, 5.0):
        raise LaunchProbeError("packaged engine did not exit after graceful shutdown")


def _permission_report() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(PERMISSIONS_URL, timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise LaunchProbeError("packaged permission query failed") from exc
    permissions = payload.get("permissions")
    if (
        payload.get("schema_version") != 1
        or payload.get("platform") != "macos"
        or not isinstance(permissions, list)
        or [item.get("kind") for item in permissions] != list(PERMISSION_KINDS)
        or any(item.get("state") not in PERMISSION_STATES for item in permissions)
        or any(type(item.get("query_supported")) is not bool for item in permissions)
        or any(type(item.get("request_supported")) is not bool for item in permissions)
    ):
        raise LaunchProbeError("packaged permission report violated schema")
    return payload


def _repeated_permission_report(repetitions: int = 3) -> dict[str, Any]:
    reports = [_permission_report() for _ in range(repetitions)]
    if any(report != reports[0] for report in reports[1:]):
        raise LaunchProbeError("packaged permission report changed during repeated query")
    return reports[0]


def _snapshot_lineage(root_pid: int) -> list[dict[str, Any]]:
    try:
        root = psutil.Process(root_pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        return []
    lineage: list[dict[str, Any]] = []
    for process in processes:
        try:
            if not process.is_running():
                continue
            lineage.append(
                {
                    "pid": process.pid,
                    "ppid": process.ppid(),
                    "pgid": os.getpgid(process.pid),
                    "create_time": process.create_time(),
                }
            )
        except (OSError, psutil.Error):
            # WebKit helpers can exit between children(), is_running(), and the
            # identity reads. An already-gone descendant is cleaned by
            # definition and must not invalidate the surviving lineage proof.
            continue
    return lineage


def _identity_present(record: dict[str, Any]) -> bool:
    try:
        process = psutil.Process(record["pid"])
        return abs(process.create_time() - record["create_time"]) < 0.001
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _wait_exit(process: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _launch_agent_snapshot() -> list[dict[str, Any]]:
    root = Path.home() / "Library/LaunchAgents"
    if not root.is_dir():
        return []
    return [
        {"name": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in sorted(root.iterdir())
        if path.is_file() and "whisperstream" in path.name.lower()
    ]


def _wait_frontend_connection(
    engine_log_path: Path,
    shell_process: subprocess.Popen[bytes],
    timeout: float,
) -> float:
    started = time.perf_counter()
    deadline = started + timeout
    # The privacy-minimized server log deliberately omits the client address.
    # This content-free marker is emitted only after an accepted frontend
    # WebSocket connection.
    marker = "ws connect status=accepted"
    while time.perf_counter() < deadline:
        if shell_process.poll() is not None:
            raise LaunchProbeError("Tauri shell exited before frontend connection")
        if engine_log_path.is_file() and marker in engine_log_path.read_text(
            encoding="utf-8", errors="replace"
        ):
            return time.perf_counter() - started
        time.sleep(0.1)
    raise LaunchProbeError("packaged frontend did not connect to the backend")


def _maybe_wait_frontend_connection(
    engine_log_path: Path,
    shell_process: subprocess.Popen[bytes],
    *,
    real_dictation_runtime: bool,
    waiter: Any = _wait_frontend_connection,
) -> float | None:
    """Keep the frontend proof separate from selector-absent runtime proof."""

    if real_dictation_runtime:
        return None
    return waiter(engine_log_path, shell_process, 10.0)


def _wait_shell_engine_ready(
    shell_log_path: Path,
    shell_process: subprocess.Popen[bytes],
    timeout: float,
) -> float:
    """Wait for Tauri to accept its exact generation-bound engine."""

    started = time.perf_counter()
    deadline = started + timeout
    marker = "[tauri] engine ready generation=1"
    while time.perf_counter() < deadline:
        if shell_process.poll() is not None:
            raise LaunchProbeError("Tauri shell exited before owned engine readiness")
        if shell_log_path.is_file() and marker in shell_log_path.read_text(
            encoding="utf-8", errors="replace"
        ):
            return time.perf_counter() - started
        time.sleep(0.1)
    raise LaunchProbeError("packaged shell-owned engine readiness deadline expired")


def _assert_frontend_requests_clean(engine_log_path: Path) -> None:
    """Reject a shell connection that leaves frontend-triggered API failures."""

    # Give the initial page queries a bounded opportunity to complete after the
    # WebSocket establishes. This is still a content-free log check.
    time.sleep(0.5)
    log_text = engine_log_path.read_text(encoding="utf-8", errors="replace")
    if "api unhandled_exception" in log_text or "Exception in ASGI application" in log_text:
        raise LaunchProbeError("packaged frontend triggered a backend exception")


def _first_run_inline_contract(source: str) -> bool:
    """Bind first-run status to the guarded inline main-window surface."""

    start = source.find("export function EulaConsentGate()")
    end = source.find("export default EulaConsentGate;", start)
    if start < 0 or end < 0:
        return False
    gate = source[start:end]
    required = (
        'console.log(`[EULA] status loaded state=${next.state}`);',
        'resolveEulaRequirementState(status) === "ready"',
        "<ReadyApp",
        'data-testid="eula-consent-gate"',
        "<EulaPopup",
        "inline",
        "initialPayload={payloadForEulaStatus(status)}",
    )
    return all(marker in gate for marker in required) and "getConfig(" not in gate


def _fetch_eula_status() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(EULA_STATUS_URL, timeout=1.0) as response:
            if response.status != 200:
                raise LaunchProbeError("packaged EULA status query failed")
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise LaunchProbeError("packaged EULA status was unreadable") from exc
    if not isinstance(payload, dict):
        raise LaunchProbeError("packaged EULA status was unreadable")
    return payload


def _valid_first_run_status(status: dict[str, Any]) -> bool:
    return (
        status.get("state") == "first_run"
        and status.get("needs_first_run") is True
        and isinstance(status.get("bundled_version"), str)
        and bool(status["bundled_version"])
        and isinstance(status.get("bundled_sha256"), str)
        and len(status["bundled_sha256"]) == 64
        and isinstance(status.get("bundled_text"), str)
        and bool(status["bundled_text"])
    )


def _main_webview_ready_state(
    frontend_log: str,
    engine_log: str,
    eula_status: dict[str, Any],
    *,
    inline_contract: bool,
) -> str | None:
    """Return the legally guarded first-run surface proven for the main WebView."""

    main_lines = [
        line
        for line in frontend_log.splitlines()
        if line.startswith("[main] ")
    ]
    if any(
        marker in line
        for line in main_lines
        for marker in (
            "Unhandled error:",
            "Unhandled rejection:",
            "[App] unhandled error",
            "[App] unhandled rejection",
        )
    ):
        raise LaunchProbeError("packaged main WebView reported a hydration failure")

    if any(
        marker in line
        for line in main_lines
        for marker in ("[EULA] status load failed", "[App] config load failed")
    ):
        raise LaunchProbeError("packaged main WebView reported a hydration failure")
    config_requested = (
        "api request method=GET path=/api/config" in engine_log
    )
    config_loaded = any("[App] config loaded" in line for line in main_lines)
    if config_requested or config_loaded:
        raise LaunchProbeError(
            "packaged main WebView crossed the first-run legal guard"
        )
    eula_loaded = (
        "api response method=GET path=/api/eula/status status=200" in engine_log
    )
    if eula_loaded and _valid_first_run_status(eula_status) and inline_contract:
        return "first-run-eula-guarded"
    return None


def _fetch_frontend_log() -> str:
    try:
        with urllib.request.urlopen(LOG_EXPORT_URL, timeout=1.0) as response:
            if response.status != 200:
                raise LaunchProbeError("packaged frontend log export failed")
            archive_bytes = response.read()
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            return archive.read("frontend.log").decode("utf-8", errors="replace")
    except (KeyError, OSError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        raise LaunchProbeError("packaged frontend log export was unreadable") from exc


def _wait_main_webview_ready(
    shell_process: subprocess.Popen[bytes],
    engine_log_path: Path,
    timeout: float,
) -> tuple[str, float]:
    started = time.perf_counter()
    deadline = started + timeout
    source = (REPO_ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    inline_contract = _first_run_inline_contract(source)
    if not inline_contract:
        raise LaunchProbeError("packaged first-run inline source contract changed")
    while time.perf_counter() < deadline:
        if shell_process.poll() is not None:
            raise LaunchProbeError("Tauri shell exited before main WebView readiness")
        engine_log = engine_log_path.read_text(encoding="utf-8", errors="replace")
        if "api response method=GET path=/api/eula/status status=200" not in engine_log:
            time.sleep(0.5)
            continue
        state = _main_webview_ready_state(
            _fetch_frontend_log(),
            engine_log,
            _fetch_eula_status(),
            inline_contract=inline_contract,
        )
        if state is not None:
            return state, time.perf_counter() - started
        time.sleep(0.5)
    raise LaunchProbeError(
        "packaged main WebView did not exit the settings loading gate"
    )


def _shell_launch_command(shell: Path, *, prove_main_webview: bool) -> list[str]:
    if prove_main_webview:
        return [str(shell)]
    return [str(shell), "--minimized"]


def _maybe_wait_main_webview_ready(
    shell_process: subprocess.Popen[bytes],
    engine_log_path: Path,
    *,
    prove_main_webview: bool,
) -> tuple[str, float | None]:
    if not prove_main_webview:
        return "not-requested", None
    return _wait_main_webview_ready(shell_process, engine_log_path, 20.0)


def _terminate_exact_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is not None:
            return
        raise
    if not _wait_exit(process, 3.0):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            if process.poll() is None:
                raise
        process.wait(timeout=3.0)


def probe(
    app: Path,
    output: Path,
    *,
    query_permissions: bool = False,
    prove_main_webview: bool = False,
    real_dictation_runtime: bool = False,
    model_source: Path | None = None,
) -> dict[str, Any]:
    app = app.resolve()
    executable_dir = app / "Contents/MacOS"
    shell = executable_dir / "whisperstream"
    engine = executable_dir / "whisperstream-engine"
    worker = executable_dir / "whisperstream-macos-stt-worker"
    if not all(path.is_file() and os.access(path, os.X_OK) for path in (shell, engine, worker)):
        raise LaunchProbeError("required bundle executable is missing")
    if not _port_is_free():
        raise LaunchProbeError("required isolated health port is already in use")
    if real_dictation_runtime and (query_permissions or prove_main_webview):
        raise LaunchProbeError("real runtime proof cannot request probe-only modes")
    if bool(model_source) != real_dictation_runtime:
        raise LaunchProbeError("real runtime proof requires exactly one model source")

    launch_root = output.resolve().parent / "launch-state"
    if launch_root.exists():
        raise LaunchProbeError("launch-state path already exists; use a fresh output directory")
    home = launch_root / "home"
    data = launch_root / "data"
    logs = launch_root / "logs"
    for directory in (home, data, logs):
        directory.mkdir(parents=True, mode=0o700)
    model_root: Path | None = None
    if real_dictation_runtime:
        assert model_source is not None
        model_root = _prepare_real_runtime(model_source, home, data)
    env = _spawn_environment(
        dict(os.environ), real_dictation_runtime=real_dictation_runtime
    )
    env.update(
        {
            "CFFIXED_USER_HOME": str(home),
            "HOME": str(home),
            "WHISPERSTREAM_BUNDLE_RESOURCES_DIR": str(
                app / "Contents/Resources/resources"
            ),
            "WHISPERSTREAM_DATA_DIR": str(data),
            "WHISPERSTREAM_MACOS_STT_WORKER_BIN": str(worker),
        }
    )

    engine_log_path = data / "logs/whisperstream.log"
    shell_log_path = logs / "shell.log"
    shell_log = shell_log_path.open("wb")
    shell_process: subprocess.Popen[bytes] | None = None
    engine_lineage: list[dict[str, Any]] = []
    shell_lineage: list[dict[str, Any]] = []
    health_payload: dict[str, Any] = {}
    health_seconds = 0.0
    generation_ready_seconds = 0.0
    shell_alive_seconds = 0.0
    frontend_connected_seconds: float | None = None
    main_webview_ready_seconds: float | None = None
    main_webview_state = "not-requested"
    permission_report: dict[str, Any] | None = None
    real_runtime_ready_seconds: float | None = None
    real_spawn_contract: dict[str, Any] | None = None
    real_worker: dict[str, Any] | None = None
    cleanup = False
    launch_agents_before = _launch_agent_snapshot()
    launch_agents_after: list[dict[str, Any]] = []
    try:
        shell_started = time.perf_counter()
        shell_process = subprocess.Popen(
            _shell_launch_command(
                shell,
                prove_main_webview=prove_main_webview,
            ),
            env=env,
            cwd=executable_dir,
            stdout=shell_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        generation_ready_seconds = _wait_shell_engine_ready(
            shell_log_path, shell_process, 30.0
        )
        health_payload, health_seconds = _health(
            30.0 if real_dictation_runtime else 5.0
        )
        frontend_connected_seconds = _maybe_wait_frontend_connection(
            engine_log_path,
            shell_process,
            real_dictation_runtime=real_dictation_runtime,
        )
        if frontend_connected_seconds is not None:
            _assert_frontend_requests_clean(engine_log_path)
        if real_dictation_runtime:
            # Health is generation-bound and returns model_loaded only after the
            # ordinary manager exists. Pair it with the observed native worker
            # and selector-absent argv instead of requiring ReadyApp's WebSocket,
            # which the fresh-profile EULA guard intentionally blocks.
            real_runtime_ready_seconds = health_seconds
            real_spawn_contract = _real_engine_spawn_contract(shell_process.pid)
            real_worker = _real_worker_identity(shell_process.pid)
            _assert_protected_storage_untouched(data)
        main_webview_state, main_webview_ready_seconds = (
            _maybe_wait_main_webview_ready(
                shell_process,
                engine_log_path,
                prove_main_webview=prove_main_webview,
            )
        )
        if query_permissions:
            permission_report = _repeated_permission_report()
        shell_alive_seconds = time.perf_counter() - shell_started
        if shell_process.poll() is not None:
            raise LaunchProbeError("Tauri shell exited before frontend load proof")
        shell_lineage = _snapshot_lineage(shell_process.pid)
        engine_lineage = [
            item for item in shell_lineage if item["pid"] != shell_process.pid
        ]

        quit_process = subprocess.run(
            [str(shell), "--quit"],
            env=env,
            cwd=executable_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
        if quit_process.returncode != 0:
            raise LaunchProbeError("single-instance app quit request failed")
        if not _wait_exit(shell_process, 5.0):
            raise LaunchProbeError("Tauri shell did not exit after exact quit request")

        cleanup = all(
            not _identity_present(item) for item in shell_lineage
        )
        if not cleanup:
            raise LaunchProbeError("recorded launch descendant survived cleanup")
        launch_agents_after = _launch_agent_snapshot()
        if launch_agents_after != launch_agents_before:
            raise LaunchProbeError("minimal launch changed a Whisperstream login item")
        if real_dictation_runtime:
            _assert_protected_storage_untouched(data)
    finally:
        if shell_process is not None:
            _terminate_exact_group(shell_process)
        shell_log.close()

    result = {
        "schema_version": 1,
        "health": {
            "status": health_payload.get("status"),
            "model_loaded": health_payload.get("model_loaded"),
            "ready_seconds": round(health_seconds, 3),
            "generation_bound_ready": True,
            "generation_ready_seconds": round(generation_ready_seconds, 3),
            "real_runtime_ready_seconds": (
                round(real_runtime_ready_seconds, 3)
                if real_runtime_ready_seconds is not None
                else None
            ),
        },
        "frontend": {
            "tauri_shell_alive_seconds": round(shell_alive_seconds, 3),
            "backend_connection_seconds": (
                round(frontend_connected_seconds, 3)
                if frontend_connected_seconds is not None
                else None
            ),
            "backend_connection_required": not real_dictation_runtime,
            "launch_mode": "visible" if prove_main_webview else "minimized",
            "main_webview_proof_requested": prove_main_webview,
            "main_webview_ready_seconds": (
                round(main_webview_ready_seconds, 3)
                if main_webview_ready_seconds is not None
                else None
            ),
            "packaged_assets_loaded_by_shell": True,
            "loading_settings_gate_exited": True if prove_main_webview else None,
            "ready_surface": main_webview_state,
            "first_run_legal_guard": (
                {
                    "eula_status_loaded": True,
                    "inline_main_surface_source_bound": True,
                    "config_hydrated_before_acceptance": False,
                }
                if prove_main_webview
                else None
            ),
            "visible_pixel_observation": "owner-required",
        },
        "interaction": {
            "file_only": not real_dictation_runtime,
            "microphone_opened": False,
            "output_stream_opened": False,
            "hotkey_setup_entered": real_dictation_runtime,
            "hotkeys_registered": False,
            "hotkey_bindings": (
                "intentionally-unbound" if real_dictation_runtime else "disabled"
            ),
            "hotkey_setup_evidence": (
                "selector-absent-serve-argv-with-empty-isolated-bindings"
                if real_dictation_runtime
                else None
            ),
            "keys_observed": False,
            "text_injected": False,
            "clipboard_mutated": False,
            "permission_query_called": query_permissions,
            "permission_request_called": False,
            "visible_prompt_observation": "owner-required",
        },
        "permission_capabilities": {
            "query_repetitions": 3 if query_permissions else 0,
            "report": permission_report,
        },
        "process_lineage": {
            "owner": "packaged-shell",
            "engine": engine_lineage,
            "shell": shell_lineage,
            "cleanup_verified": cleanup,
        },
        "real_dictation_runtime": {
            "requested": real_dictation_runtime,
            "selector_absent": real_dictation_runtime,
            "spawn_contract": real_spawn_contract,
            "worker": real_worker,
            "model_root": (
                {
                    "isolated": True,
                    "required_assets_present": len(MACOS_STT_REQUIRED_ASSETS),
                }
                if model_root is not None
                else None
            ),
            "model_loaded": health_payload.get("model_loaded") is True,
        },
        "protected_storage": {
            "lockdown_marker_preseeded": real_dictation_runtime,
            "history_enabled": False if real_dictation_runtime else None,
            "native_store_query_requested": False,
            "native_store_probe_run": False,
            "transcript_key_created": False if real_dictation_runtime else None,
            "protected_reference_persisted": False if real_dictation_runtime else None,
        },
        "system_configuration": {
            "whisperstream_launch_agents_before": launch_agents_before,
            "whisperstream_launch_agents_after": launch_agents_after,
            "login_item_changed": False,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return result


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--query-permissions",
        action="store_true",
        help="query the packaged schema-v1 permission endpoint three times",
    )
    parser.add_argument(
        "--prove-main-webview",
        action="store_true",
        help=(
            "launch visibly and require valid first-run EULA status, the "
            "source-bound inline main surface, and an unbroken legal guard"
        ),
    )
    parser.add_argument(
        "--prove-real-dictation-runtime",
        action="store_true",
        help=(
            "launch with the spawn selector absent under locked disposable "
            "storage and require the real engine/worker readiness contract"
        ),
    )
    parser.add_argument(
        "--model-source",
        type=Path,
        help="complete FluidAudio model root cloned into the disposable home",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        probe(
            args.app,
            args.output,
            query_permissions=args.query_permissions,
            prove_main_webview=args.prove_main_webview,
            real_dictation_runtime=args.prove_real_dictation_runtime,
            model_source=args.model_source,
        )
    except (LaunchProbeError, OSError, psutil.Error, subprocess.SubprocessError) as exc:
        print(f"internal launch probe failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
