from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import numpy as np
import psutil
import pytest

import whisperstream.stt_supervisor as stt_supervisor_module
from whisperstream.stt_supervisor import (
    NativeSttWorkerCrashed,
    SttWorkerSupervisor,
    default_worker_command,
)
from whisperstream.stt_worker_process import (
    STARTUP_TIMEOUT_SECONDS,
    source_worker_command,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMAND = source_worker_command(REPO_ROOT)


def test_frozen_default_worker_command_uses_original_onefile_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_engine = tmp_path / "portable" / "whisperstream-engine.exe"
    extracted_engine = tmp_path / "runtime" / "whisperstream-engine.exe"
    monkeypatch.setitem(vars(stt_supervisor_module), "__compiled__", object())
    monkeypatch.setenv("NUITKA_ONEFILE_BINARY", str(original_engine))
    monkeypatch.setattr(stt_supervisor_module.sys, "executable", str(extracted_engine))

    assert default_worker_command() == (str(original_engine), "--stt-worker")


def _supervisor(
    tmp_path: Path,
    *,
    environment: dict[str, str] | None = None,
    inference_timeout: float = 3.0,
) -> SttWorkerSupervisor:
    return SttWorkerSupervisor(
        backend="prototype",
        model_identifier="prototype-v1",
        diagnostic_dir=tmp_path,
        logger=logging.getLogger("tests.stt-supervisor"),
        command=SOURCE_COMMAND,
        cwd=REPO_ROOT,
        environment=environment,
        # Cold interpreter startup slows under parallel load; it is not the
        # bound under test, so allow the production startup budget.
        startup_timeout=STARTUP_TIMEOUT_SECONDS,
        load_timeout=3.0,
        inference_timeout=inference_timeout,
        shutdown_timeout=3.0,
        suppression_seconds=5.0,
    )


def test_supervisor_normal_load_transcribe_unload_and_cleanup(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    parent_pid = os.getpid()
    supervisor.load({})
    worker_pid = supervisor.pid
    try:
        assert worker_pid is not None
        assert worker_pid != parent_pid
        assert psutil.pid_exists(worker_pid)
        body = supervisor.request_audio(
            "transcribe_chunk",
            np.zeros(1600, dtype=np.float32),
            sample_rate=16_000,
        )
        assert body["text"] == ""
        assert body["sample_count"] == 1600
        assert supervisor.unload() == {"state": "idle", "loaded": False}
        assert supervisor.health()["state"] == "idle"
    finally:
        report = supervisor.shutdown()
    assert report is not None and report.expected is True
    assert not psutil.pid_exists(worker_pid)
    assert supervisor.is_running is False


def test_stop_worker_releases_process_and_next_load_recreates_one(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.load({})
    first_pid = supervisor.pid
    assert first_pid is not None and psutil.pid_exists(first_pid)

    report = supervisor.stop_worker()
    assert report is not None and report.expected is True
    assert not psutil.pid_exists(first_pid)
    assert supervisor.pid is None
    assert supervisor.loaded is False
    assert supervisor.health() == {
        "engine_id": "prototype",
        "state": "idle",
        "loaded": False,
    }

    supervisor.load({})
    second_pid = supervisor.pid
    try:
        assert second_pid is not None and second_pid != first_pid
        assert psutil.pid_exists(second_pid)
    finally:
        supervisor.shutdown()
    assert not psutil.pid_exists(second_pid)


def test_supervisor_serializes_concurrent_requests(tmp_path: Path) -> None:
    supervisor = _supervisor(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION": "transcribe_chunk",
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_MS": "100",
        },
    )
    supervisor.load({})
    results: list[str] = []
    failures: list[BaseException] = []

    def transcribe() -> None:
        try:
            body = supervisor.request_audio(
                "transcribe_chunk",
                np.zeros(1600, dtype=np.float32),
                sample_rate=16_000,
            )
            results.append(body["text"])
        except BaseException as exc:  # noqa: BLE001 - asserted below
            failures.append(exc)

    started = time.monotonic()
    threads = [threading.Thread(target=transcribe) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        elapsed = time.monotonic() - started
        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        assert results == ["", ""]
        assert elapsed >= 0.18
    finally:
        supervisor.shutdown()


def test_malformed_reply_recovers_once_then_suppresses_and_cleans_up(
    tmp_path: Path,
) -> None:
    supervisor = _supervisor(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_MALFORMED_REPLY_OPERATION": "health"
        },
    )
    supervisor.load({})
    with pytest.raises(NativeSttWorkerCrashed) as captured:
        supervisor.health()

    failure = captured.value
    assert failure.operation == "health"
    assert failure.recovery_attempted is True
    assert failure.suppressed is True
    assert supervisor.recovery_count == 1
    assert supervisor.is_running is False
    if failure.report is not None:
        assert not psutil.pid_exists(failure.report.pid)
    supervisor.shutdown()


def test_timeout_recovers_once_then_suppresses_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _supervisor(
        tmp_path,
        environment={
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION": "transcribe_chunk",
            "WHISPERSTREAM_STT_WORKER_TEST_DELAY_MS": "500",
        },
        inference_timeout=0.05,
    )
    supervisor.load({})
    # Recovery starts a cold interpreter under the separate startup timeout.
    # Measure request/cleanup latency without imposing a faster startup limit.
    recovered_processes = []
    new_process = supervisor._new_process

    def track_recovery_process():
        process = new_process()
        recovered_processes.append(process)
        return process

    monkeypatch.setattr(supervisor, "_new_process", track_recovery_process)
    started = time.monotonic()
    with pytest.raises(NativeSttWorkerCrashed) as captured:
        supervisor.request_audio(
            "transcribe_chunk",
            np.zeros(1600, dtype=np.float32),
            sample_rate=16_000,
        )
    elapsed = time.monotonic() - started

    failure = captured.value
    assert len(recovered_processes) == 1
    spawn_ms = recovered_processes[0].spawn_ms
    assert spawn_ms is not None and 0 <= spawn_ms < 10_000
    assert elapsed - spawn_ms / 1_000 < 3.0
    assert failure.recovery_attempted is True
    assert failure.suppressed is True
    assert supervisor.recovery_count == 1
    assert supervisor.is_running is False
    supervisor.shutdown()
