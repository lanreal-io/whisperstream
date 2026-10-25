from __future__ import annotations

import io
import logging
import os
import socket
import stat
import sys
import threading
import time
import wave
from pathlib import Path

import httpx
import psutil
import pytest
from fastapi.testclient import TestClient

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.macos_stt_manager import (
    MacosFluidAudioManager,
    default_macos_stt_worker_path,
)
from whisperstream.stt_errors import (
    SttEngineUnavailableError,
    SttFailure,
    SttWorkerPythonFailure,
    SttWorkerStartFailure,
)
from whisperstream.stt_factory import make_stt_manager, stt_model_status


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the native FluidAudio backend integration is macOS-only",
)


def _worker_binary() -> Path:
    binary = default_macos_stt_worker_path()
    assert binary.is_file(), f"native macOS STT worker is missing: {binary}"
    assert os.access(binary, os.X_OK), f"native macOS STT worker is not executable: {binary}"
    return binary


def _empty_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"")
    return buffer.getvalue()


def _incompatible_worker(tmp_path: Path) -> Path:
    script = tmp_path / "incompatible-worker"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, struct, sys
prefix = sys.stdin.buffer.read(12)
magic, header_len, payload_len = struct.unpack(\">4sII\", prefix)
request = json.loads(sys.stdin.buffer.read(header_len))
sys.stdin.buffer.read(payload_len)
reply = {
    \"protocol\": \"whisperstream-stt\",
    \"version\": 2,
    \"kind\": \"reply\",
    \"request_id\": request[\"request_id\"],
    \"operation\": request[\"operation\"],
    \"backend\": request[\"backend\"],
    \"ok\": True,
    \"body\": {\"selected_version\": 2, \"worker_pid\": os.getpid(), \"engine_id\": \"parakeet\", \"capabilities\": {}},
}
header = json.dumps(reply, separators=(\",\", \":\")).encode()
sys.stdout.buffer.write(struct.pack(\">4sII\", b\"WST1\", len(header), 0) + header)
sys.stdout.buffer.flush()
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _wait_for_failure(app, *, timeout: float = 10.0) -> SttFailure:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        failure = app.last_stt_failure
        if isinstance(failure, SttFailure):
            return failure
        time.sleep(0.02)
    raise AssertionError("background STT load did not publish a typed failure")


def test_darwin_factory_selects_real_native_manager() -> None:
    from whisperstream.config_manager import ConfigManager

    manager = make_stt_manager(
        WhisperstreamConfig(stt_backend="parakeet", model_timeout=0),
        logger=logging.getLogger("tests.macos-backend.factory"),
        config_manager=ConfigManager(),
    )
    try:
        assert isinstance(manager, MacosFluidAudioManager)
        assert manager.worker_pid is not None
        assert manager.worker_pid != os.getpid()
        assert manager.capabilities.preview is None
        assert manager.capabilities.hotwords is None
        assert manager.capabilities.language_hint is None
    finally:
        worker_pid = manager.worker_pid
        manager.shutdown()
    assert worker_pid is not None and not psutil.pid_exists(worker_pid)


def test_missing_executable_keeps_backend_alive_with_typed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.main import WhisperstreamApp
    from whisperstream.server import WhisperstreamServer

    monkeypatch.setenv(
        "WHISPERSTREAM_MACOS_STT_WORKER_BIN",
        str(tmp_path / "missing-worker"),
    )
    app = WhisperstreamApp(
        headless=True,
        defer_model_load=True,
        file_only=True,
    )
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    client = TestClient(server.fastapi)
    try:
        app.load_stt_engine_async()
        failure = _wait_for_failure(app)
        assert isinstance(failure, SttWorkerStartFailure)
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["model_health"] == "degraded"
        assert health.json()["stt_failure"]["code"] == "worker_start_failed"
    finally:
        app.cleanup()


def test_missing_cache_is_typed_and_cleans_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.main import WhisperstreamApp
    from whisperstream.server import WhisperstreamServer

    empty_home = tmp_path / "empty-home"
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("CFFIXED_USER_HOME", str(empty_home))
    monkeypatch.setenv("WHISPERSTREAM_MACOS_STT_WORKER_BIN", str(_worker_binary()))
    app = WhisperstreamApp(headless=True, defer_model_load=True, file_only=True)
    client = TestClient(
        WhisperstreamServer(app, port=0, register_hotkeys=False).fastapi
    )
    try:
        app.load_stt_engine_async()
        failure = _wait_for_failure(app)
        assert isinstance(failure, SttWorkerPythonFailure)
        assert failure.exception_type == "MacSttModelUnavailable"
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["model_health"] == "degraded"
        assert health.json()["stt_failure"]["code"] == "worker_python_exception"
    finally:
        app.cleanup()


def test_incompatible_protocol_is_typed_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whisperstream.main import WhisperstreamApp
    from whisperstream.server import WhisperstreamServer

    monkeypatch.setenv(
        "WHISPERSTREAM_MACOS_STT_WORKER_BIN",
        str(_incompatible_worker(tmp_path)),
    )
    app = WhisperstreamApp(headless=True, defer_model_load=True, file_only=True)
    client = TestClient(
        WhisperstreamServer(app, port=0, register_hotkeys=False).fastapi
    )
    started = time.monotonic()
    try:
        app.load_stt_engine_async()
        failure = _wait_for_failure(app)
        assert isinstance(failure, SttWorkerStartFailure)
        assert time.monotonic() - started < 10
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["model_health"] == "degraded"
        assert health.json()["stt_failure"]["code"] == "worker_start_failed"
    finally:
        app.cleanup()


def test_qwen_is_configuration_visible_and_backend_stays_alive(
    accepted_eula: None,
) -> None:
    from whisperstream.main import WhisperstreamApp
    from whisperstream.server import WhisperstreamServer

    config = WhisperstreamConfig(stt_backend="qwen", stt_model_selection_mode="manual", language="en")
    from whisperstream.stt_errors import ModelNotPreparedError
    with pytest.raises(ModelNotPreparedError):
        make_stt_manager(
            config,
            logger=logging.getLogger("tests.macos-backend.qwen"),
            config_manager=object(),
        )

    status = stt_model_status(config, object())
    assert status["qwen"] == {
        "downloaded": False,
        "active": True,
    }

    app = WhisperstreamApp(
        headless=True,
        defer_model_load=True,
        file_only=True,
    )
    app.license_manager.can_start_recording = lambda: True
    app.config.stt_backend = "qwen"
    app.config.stt_model_selection_mode = "manual"
    app.config.language = "en"
    app._load_stt_engine_blocking()
    assert app.stt_engine is None
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    client = TestClient(server.fastapi)
    try:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "model_loaded": False}
        response = client.post(
            "/api/transcribe",
            files={"file": ("empty.wav", _empty_wav(), "audio/wav")},
        )
        assert response.status_code == 503
        assert response.json() == {"error": "Model not loaded"}
    finally:
        app.cleanup()


def test_real_random_port_headless_health_has_no_os_interaction_construction() -> None:
    from whisperstream.main import WhisperstreamApp
    from whisperstream.server import WhisperstreamServer

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    app = WhisperstreamApp(headless=True, file_only=True)
    assert app.config.enhancement_enabled is False
    assert app.keyboard is None
    assert app.audio_capture is None
    assert app.audio_feedback is None
    server = WhisperstreamServer(app, port=port, register_hotkeys=False)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    worker_pid = app.stt_engine.worker_pid if app.stt_engine is not None else None
    try:
        deadline = time.monotonic() + 15
        response = None
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        assert response is not None and response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["model_loaded"] is True
        shutdown = httpx.post(f"http://127.0.0.1:{port}/api/shutdown", timeout=2)
        assert shutdown.status_code == 200
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        app.cleanup()
    assert worker_pid is not None and not psutil.pid_exists(worker_pid)
