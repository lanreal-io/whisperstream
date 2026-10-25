#!/usr/bin/env python3
"""Sanitized source/frozen smoke and measurement probe for STT worker v1.

The output contains only timings, byte/sample counts, PIDs, RSS, exit metadata,
hashes, and boolean checks.  It never records audio values, transcript text,
hotwords, exception messages, or traceback contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import psutil

from whisperstream.stt_worker_process import (
    SttWorkerExited,
    SttWorkerProcess,
    SttWorkerRemoteError,
    frozen_worker_command,
    source_worker_command,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = "prototype"
REPRESENTATIVE_SAMPLES = 20 * 16_000
REPRESENTATIVE_BYTES = REPRESENTATIVE_SAMPLES * 4


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def _timing_summary(values: list[float]) -> dict[str, float | int]:
    return {
        "rounds": len(values),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
    }


def _measure_call(call: object) -> tuple[Any, float]:
    started = time.perf_counter()
    result = call()  # type: ignore[operator]
    return result, (time.perf_counter() - started) * 1_000


def _new_client(
    command: Sequence[str],
    diagnostic_dir: Path,
    *,
    environment: dict[str, str] | None = None,
) -> SttWorkerProcess:
    return SttWorkerProcess(
        command,
        backend=BACKEND,
        diagnostic_dir=diagnostic_dir,
        cwd=REPO_ROOT,
        environment=environment,
    )


def _lifecycle_probe(command: Sequence[str], root: Path) -> dict[str, Any]:
    client = _new_client(command, root / "lifecycle")
    audio = np.zeros(REPRESENTATIVE_SAMPLES, dtype=np.float32)
    health_ms: list[float] = []
    audio_ms: list[float] = []
    try:
        hello = client.start(timeout=30)
        if hello.get("selected_version") != 1:
            raise AssertionError("worker did not negotiate protocol v1")
        memory = client.memory_snapshot()
        for _ in range(25):
            _, elapsed = _measure_call(
                lambda: client.request("health", timeout=5)
            )
            health_ms.append(elapsed)
        client.request("load", timeout=10)
        for _ in range(10):
            result, elapsed = _measure_call(
                lambda: client.request_audio(
                    "transcribe_chunk",
                    audio,
                    timeout=10,
                )
            )
            if result.get("sample_count") != REPRESENTATIVE_SAMPLES:
                raise AssertionError("worker audio count mismatch")
            audio_ms.append(elapsed)
        shutdown_started = time.perf_counter()
        report = client.shutdown(timeout=5)
        shutdown_ms = (time.perf_counter() - shutdown_started) * 1_000
        if report is None or report.exit_code_signed != 0 or client.is_running:
            raise AssertionError("worker did not shut down cleanly")
        audio_summary = _timing_summary(audio_ms)
        return {
            "spawn_hello_ms": round(float(client.spawn_ms or 0.0), 3),
            "request_roundtrip": _timing_summary(health_ms),
            "audio_copy_roundtrip": {
                **audio_summary,
                "sample_count": REPRESENTATIVE_SAMPLES,
                "payload_bytes": REPRESENTATIVE_BYTES,
                "shared_memory_blocked": bool(audio_summary["p95_ms"] > 25.0),
                "decision_threshold_p95_ms": 25.0,
            },
            "shutdown_ms": round(shutdown_ms, 3),
            "memory": {
                "parent_pid": memory.parent_pid,
                "parent_rss_bytes": memory.parent_rss_bytes,
                "worker_pid": memory.worker_pid,
                "worker_rss_bytes": memory.worker_rss_bytes,
                "distinct_process": memory.parent_pid != memory.worker_pid,
            },
            "expected_exit": {
                "pid": report.pid,
                "operation": report.operation,
                "exit_code_signed": report.exit_code_signed,
                "exit_code_hex": report.exit_code_hex,
                "worker_alive_after": client.is_running,
            },
        }
    finally:
        if client.is_running:
            client.terminate_owned_worker(expected=False)


def _python_exception_probe(command: Sequence[str], root: Path) -> dict[str, Any]:
    client = _new_client(
        command,
        root / "python-exception",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION": "health"
        },
    )
    try:
        client.start(timeout=30)
        try:
            client.request("health", timeout=5)
        except SttWorkerRemoteError as exc:
            error = exc.error
        else:
            raise AssertionError("worker did not return synthetic Python exception")
        traceback_text = error.get("traceback")
        if not isinstance(traceback_text, str) or not traceback_text.startswith(
            "Traceback (most recent call last)"
        ):
            raise AssertionError("worker Python error did not retain traceback")
        if not client.is_running:
            raise AssertionError("ordinary Python exception killed worker")
        diagnostic_path = client.diagnostic_path
        if diagnostic_path is None or not diagnostic_path.is_file():
            raise AssertionError("worker diagnostic file missing")
        diagnostic_bytes = diagnostic_path.stat().st_size
        report = client.shutdown(timeout=5)
        return {
            "category": error.get("category"),
            "code": error.get("code"),
            "exception_type": error.get("exception_type"),
            "traceback_present": True,
            "traceback_bytes": len(traceback_text.encode("utf-8")),
            "diagnostic_bytes": diagnostic_bytes,
            "diagnostic_bounded": diagnostic_bytes <= 512 * 1024,
            "worker_survived": report is not None and report.exit_code_signed == 0,
        }
    finally:
        if client.is_running:
            client.terminate_owned_worker(expected=False)


def _hard_exit_probe(
    command: Sequence[str],
    root: Path,
    operation: str,
) -> dict[str, Any]:
    client = _new_client(
        command,
        root / f"hard-exit-{operation}",
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION": operation,
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_MS": "2500",
        },
    )
    parent_pid = os.getpid()
    failures: list[BaseException] = []
    try:
        client.start(timeout=30)
        if operation == "transcribe":
            client.request("load", timeout=5)

        def in_flight() -> None:
            try:
                if operation == "transcribe":
                    client.request_audio(
                        "transcribe",
                        np.zeros(REPRESENTATIVE_SAMPLES, dtype=np.float32),
                        timeout=10,
                    )
                else:
                    client.request("load", timeout=10)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                failures.append(exc)

        thread = threading.Thread(target=in_flight, daemon=True)
        thread.start()
        marker = client.wait_for_marker(
            operation=operation,
            phase="active",
            timeout=5,
        )
        worker_pid = client.pid
        report = client.terminate_owned_worker(expected=False)
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("hard-exit request thread did not finish")
        if not failures or not isinstance(failures[0], SttWorkerExited):
            raise AssertionError("hard exit was not classified as worker exit")
        if report is None or report.operation != operation:
            raise AssertionError("hard exit did not preserve last operation")
        if worker_pid is None or marker.get("pid") != worker_pid:
            raise AssertionError("hard exit marker PID mismatch")
        if not psutil.pid_exists(parent_pid) or os.getpid() != parent_pid:
            raise AssertionError("parent did not survive worker exit")

        replacement = _new_client(command, root / f"replacement-{operation}")
        try:
            replacement.start(timeout=30)
            replacement.request("health", timeout=5)
            replacement_report = replacement.shutdown(timeout=5)
            restart_ok = (
                replacement_report is not None
                and replacement_report.exit_code_signed == 0
                and not replacement.is_running
            )
        finally:
            if replacement.is_running:
                replacement.terminate_owned_worker(expected=False)
        if not restart_ok:
            raise AssertionError("replacement worker did not shut down cleanly")
        return {
            "operation": operation,
            "parent_pid": parent_pid,
            "parent_survived": True,
            "worker_pid": worker_pid,
            "marker_phase": report.marker_phase,
            "exit_code_signed": report.exit_code_signed,
            "exit_code_hex": report.exit_code_hex,
            "restart_ok": restart_ok,
            "worker_alive_after": client.is_running,
        }
    finally:
        if client.is_running:
            client.terminate_owned_worker(expected=False)


def run_probe(mode: str, executable: Path | None) -> dict[str, Any]:
    if mode == "source":
        artifact = REPO_ROOT / "whisperstream_entry.py"
        command = source_worker_command(REPO_ROOT)
        artifact_path = "whisperstream_entry.py"
    else:
        if executable is None or not executable.is_file():
            raise FileNotFoundError("frozen worker executable is required")
        artifact = executable.resolve()
        command = frozen_worker_command(artifact)
        try:
            artifact_path = artifact.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            artifact_path = artifact.name

    with tempfile.TemporaryDirectory(prefix=f"whisperstream-stt-{mode}-") as temp:
        root = Path(temp)
        lifecycle = _lifecycle_probe(command, root)
        python_exception = _python_exception_probe(command, root)
        hard_exits = [
            _hard_exit_probe(command, root, "load"),
            _hard_exit_probe(command, root, "transcribe"),
        ]

    return {
        "schema_version": 1,
        "mode": mode,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "artifact": {
            "path": artifact_path,
            "sha256": _sha256(artifact),
            "size_bytes": artifact.stat().st_size,
        },
        "lifecycle": lifecycle,
        "python_exception": python_exception,
        "hard_exits": hard_exits,
        "checks": {
            "parent_survived_all_hard_exits": all(
                item["parent_survived"] for item in hard_exits
            ),
            "replacement_started_after_all_hard_exits": all(
                item["restart_ok"] for item in hard_exits
            ),
            "no_worker_alive_after": not lifecycle["expected_exit"][
                "worker_alive_after"
            ]
            and all(not item["worker_alive_after"] for item in hard_exits),
            "python_traceback_retained": python_exception["traceback_present"],
            "shared_memory_required": lifecycle["audio_copy_roundtrip"][
                "shared_memory_blocked"
            ],
        },
        "recorded_at": datetime.now().astimezone().isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("source", "frozen"), required=True)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_probe(args.mode, args.executable)
    if result["platform"]["system"] != "Windows":
        raise RuntimeError("worker boundary probe requires native Windows")
    required_true = (
        "parent_survived_all_hard_exits",
        "replacement_started_after_all_hard_exits",
        "no_worker_alive_after",
        "python_traceback_retained",
    )
    if not all(result["checks"][key] for key in required_true):
        raise AssertionError("one or more worker boundary checks failed")
    if result["checks"]["shared_memory_required"]:
        raise AssertionError("audio copy p95 exceeds the 25 ms acceptance threshold")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "spawn_hello_ms": result["lifecycle"]["spawn_hello_ms"],
                "request_p95_ms": result["lifecycle"]["request_roundtrip"][
                    "p95_ms"
                ],
                "audio_copy_p95_ms": result["lifecycle"][
                    "audio_copy_roundtrip"
                ]["p95_ms"],
                "shutdown_ms": result["lifecycle"]["shutdown_ms"],
                "hard_exits": len(result["hard_exits"]),
                "traceback_present": result["python_exception"][
                    "traceback_present"
                ],
                "no_worker_alive_after": result["checks"][
                    "no_worker_alive_after"
                ],
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
