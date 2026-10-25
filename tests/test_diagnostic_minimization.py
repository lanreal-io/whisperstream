"""Adversarial boundaries for minimized diagnostic retention and export."""

from __future__ import annotations

import io
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import zipfile

from fastapi.testclient import TestClient

from whisperstream.claude_hud import parse_claude_event
from whisperstream.config_manager import _log_validation_warning
from whisperstream.server.middleware import _FRONTEND_LOG_MAX_REQUEST_BYTES, _safe_rest_log_path
from whisperstream.server.diagnostics import (
    DiagnosticLogs,
    _FRONTEND_LOG_MAX_ENTRIES,
    _FRONTEND_LOG_MAX_LINE_BYTES,
    _FRONTEND_LOG_MAX_TOTAL_BYTES,
    _sanitize_frontend_logs,
)

from whisperstream.server import WhisperstreamServer


CANARY_FRAGMENTS = (
    "SecretRecording",
    "Secret Person",
    "PrivateEditor",
    "private.example",
    "Acquisition Notes",
    "Conference Microphone",
    "session-secret-91",
    "record-secret-92",
    "PRIVATE_EXCEPTION_MESSAGE",
)


def _mock_app() -> MagicMock:
    config = MagicMock()
    config.lockdown_mode = False
    config.enhancement_provider = "local"
    config.enhancement_local_model = "gemma4-e2b-instruct"
    config.enhancement_local_gpu_verdicts = {}
    config.effective_provider.return_value = "local"
    config.to_dict.return_value = {}

    app = MagicMock()
    app.config = config
    app.parakeet = MagicMock()
    app.llama_server = None
    app.license_manager.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
    app.dictation_state_snapshot.return_value = None
    app.start_hud_dictation.return_value = "started"
    app.complete_hud_dictation.return_value = "completed"
    app.cancel_hud_dictation.return_value = "cancelled"
    return app


def _all_text(values: Any) -> str:
    if isinstance(values, dict):
        return "\n".join(_all_text(value) for value in values.values())
    if isinstance(values, (list, tuple)):
        return "\n".join(_all_text(value) for value in values)
    return str(values)


def _assert_canaries_absent(text: str) -> None:
    for canary in CANARY_FRAGMENTS:
        assert canary not in text
    assert "\U0001f512\u79d8\u5bc6" not in text


def test_frontend_ingest_is_finite_bounded_and_resettable(tmp_path) -> None:
    from whisperstream.safe_diagnostics import SafeDiagnosticSink

    logs = DiagnosticLogs(
        logger=logging.getLogger(__name__),
        safe_diagnostics=SafeDiagnosticSink(tmp_path),
        software_version="test",
        model_loaded=lambda: False,
    )

    payload = [
        (
            f"[2026-08-05T00:00:{index:02d}Z] [ERROR] [App] "
            f"reason=PRIVATE_EXCEPTION_MESSAGE_{index} "
            f"path=C:\\Users\\Secret Person\\SecretRecording-{index}.wav "
            f"session_id=session-secret-91 device=Conference Microphone "
            + "x" * 4_096
        )
        for index in range(600)
    ]
    for index in range(50):
        logs.store(f"attacker-source-{index}", payload)
    logs.store("main", payload)
    logs.store("hud", payload)

    assert set(logs._frontend_logs) == {"unknown", "main", "hud"}
    assert all(
        len(lines) <= _FRONTEND_LOG_MAX_ENTRIES
        for lines in logs._frontend_logs.values()
    )
    assert all(
        len(line.encode("utf-8")) <= _FRONTEND_LOG_MAX_LINE_BYTES
        for lines in logs._frontend_logs.values()
        for line in lines
    )
    assert sum(
        len(line.encode("utf-8"))
        for lines in logs._frontend_logs.values()
        for line in lines
    ) <= _FRONTEND_LOG_MAX_TOTAL_BYTES
    _assert_canaries_absent(_all_text(logs._frontend_logs))

    logs.clear()
    logs.clear()
    assert logs._frontend_logs == {}


def test_frontend_sanitizer_drops_objects_and_redacts_complete_values() -> None:
    sanitized = _sanitize_frontend_logs(
        [
            object(),
            "[App] title=Acquisition Notes device=Conference Microphone status=failed",
            "[App] reason=PRIVATE_EXCEPTION_MESSAGE with extra private words",
            "[App] url=https://private.example/a path=/Users/Secret Person/private.txt",
            "[App] PRIVATE_UNICODE=\U0001f512\u79d8\u5bc6 status=ok",
        ]
    )

    text = "\n".join(sanitized)
    _assert_canaries_absent(text)
    assert "status=failed" in text
    assert "reason=<redacted>" in text


def test_rest_diagnostic_paths_template_transcript_ids() -> None:
    assert _safe_rest_log_path("/api/transcripts/record-secret-92") == (
        "/api/transcripts/{record_id}"
    )
    assert _safe_rest_log_path("/api/transcripts/record-secret-92/audio") == (
        "/api/transcripts/{record_id}/audio"
    )
    assert _safe_rest_log_path(
        "/api/dev/transcripts/record-secret-92/enhance"
    ) == "/api/dev/transcripts/{record_id}/enhance"
    assert _safe_rest_log_path(
        "/api/experimental/transcripts/record-secret-92/reenhance"
    ) == "/api/experimental/transcripts/{record_id}/reenhance"
    assert _safe_rest_log_path(
        "/api/experimental/transcripts/record-secret-92/bakeoff"
    ) == "/api/experimental/transcripts/{record_id}/bakeoff"
    assert _safe_rest_log_path(
        "/api/experimental/bakeoffs/run-secret-91/cancel"
    ) == "/api/experimental/bakeoffs/{run_id}/cancel"
    assert _safe_rest_log_path(
        "/api/experimental/bakeoffs/run-secret-91/retry"
    ) == "/api/experimental/bakeoffs/{run_id}/retry"
    for suffix in ("reply", "pending", "delivered"):
        assert _safe_rest_log_path(
            f"/api/managed-sessions/private-handle/{suffix}"
        ) == f"/api/managed-sessions/{{session_handle}}/{suffix}"


def test_canaries_never_reach_rotating_logs_synced_buffers_or_support_zip(
    tmp_path: Path, monkeypatch
) -> None:
    import whisperstream.claude_hud as claude_hud_module
    import whisperstream.config_manager as config_module
    import whisperstream.server.diagnostics as diagnostic_module

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    handler = RotatingFileHandler(
        log_dir / "whisperstream.log",
        maxBytes=300,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    diagnostic_logger = logging.getLogger("diagnostic-canary-test")
    diagnostic_logger.handlers = [handler]
    diagnostic_logger.propagate = False
    diagnostic_logger.setLevel(logging.DEBUG)

    for module_logger in (claude_hud_module.logger, config_module.logger):
        module_logger.addHandler(handler)
        module_logger.setLevel(logging.DEBUG)

    try:
        for index in range(20):
            diagnostic_logger.info("diagnostic heartbeat ordinal=%d", index)
        parse_claude_event(
            {
                "hook_event_name": "PRIVATE_UNICODE_\U0001f512\u79d8\u5bc6",
                "session_id": "session-secret-91",
                "last_assistant_message": "Acquisition Notes",
            }
        )
        _log_validation_warning(
            ValueError("PRIVATE_EXCEPTION_MESSAGE at C:\\Users\\Secret Person")
        )
        handler.flush()

        rotating_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(log_dir.glob("whisperstream.log*"))
        )
        _assert_canaries_absent(rotating_text)
        assert "error_type=ValueError" in rotating_text

        monkeypatch.setattr(diagnostic_module, "get_log_dir", lambda: log_dir)
        server = WhisperstreamServer(
            _mock_app(), port=0, logger=diagnostic_logger, register_hotkeys=False
        )
        with TestClient(server.fastapi, raise_server_exceptions=True) as client:
            response = client.post(
                "/api/logs/frontend",
                json={
                    "source": "attacker-source-private.example",
                    "frontend_logs": [
                        "[2026-08-05T00:00:00Z] [ERROR] [App] "
                        "filename=SecretRecording.wav "
                        "path=C:\\Users\\Secret Person\\private.txt "
                        "process=PrivateEditor.exe domain=private.example "
                        "title=Acquisition Notes device=Conference Microphone "
                        "session_id=session-secret-91 record_id=record-secret-92 "
                        "reason=PRIVATE_EXCEPTION_MESSAGE \U0001f512\u79d8\u5bc6"
                    ],
                },
            )
            assert response.status_code == 200
            assert response.json()["source"] == "unknown"
            _assert_canaries_absent(_all_text(server._diagnostic_logs._frontend_logs))

            oversized = client.post(
                "/api/logs/frontend",
                json={
                    "source": "main",
                    "frontend_logs": ["x" * _FRONTEND_LOG_MAX_REQUEST_BYTES],
                },
            )
            assert oversized.status_code == 413
            assert "main" not in server._diagnostic_logs._frontend_logs

            export = client.get("/api/logs/export")
            assert export.status_code == 200

        with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
            assert set(archive.namelist()) == {
                "backend.log",
                "frontend.log",
                "system-info.json",
            }
            for member in archive.namelist():
                _assert_canaries_absent(
                    archive.read(member).decode("utf-8", errors="replace")
                )
    finally:
        for module_logger in (claude_hud_module.logger, config_module.logger):
            module_logger.removeHandler(handler)
        handler.close()
