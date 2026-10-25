"""Adversarial contract for Lockdown-safe local diagnostics."""

from __future__ import annotations

import io
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import zipfile

import pytest

from whisperstream.safe_diagnostics import (
    MAX_BYTES,
    MAX_EVENTS,
    SafeDiagnosticRejected,
    SafeDiagnosticSink,
)


CANARIES = (
    "Secret transcript words",
    "private-audio.wav",
    "C:\\Users\\Patient\\notes.txt",
    "https://private.example/upload",
    "Private Editor",
    "Conference Microphone",
    "patient@example.com",
    "license-secret-123",
    "record-secret-456",
    "raw provider failure with secret details",
)


def _all_sink_text(sink: SafeDiagnosticSink) -> str:
    memory = "\n".join(
        json.dumps(record, sort_keys=True) for record in sink.snapshot()
    )
    disk = sink.path.read_text(encoding="utf-8") if sink.path.exists() else ""
    return f"{memory}\n{disk}"


def test_safe_sink_accepts_only_closed_structured_schema(tmp_path: Path) -> None:
    sink = SafeDiagnosticSink(tmp_path)

    assert sink.emit(
        "app_started",
        app_version="1.4.0",
        managed=True,
        policy_state="enabled",
        history_enabled=False,
    )
    assert sink.emit(
        "lockdown_transition",
        phase="entry_complete",
        managed=True,
        history_enabled=False,
    )

    with pytest.raises(SafeDiagnosticRejected, match="unknown event"):
        sink.emit("user_message", value="Secret transcript words")
    with pytest.raises(SafeDiagnosticRejected, match="unknown field"):
        sink.emit(
            "app_started",
            app_version="1.4.0",
            managed=True,
            policy_state="enabled",
            history_enabled=False,
            transcript="Secret transcript words",
        )
    with pytest.raises(SafeDiagnosticRejected, match="invalid value"):
        sink.emit(
            "lockdown_transition",
            phase="Secret transcript words",
            managed=True,
            history_enabled=False,
        )
    with pytest.raises(SafeDiagnosticRejected, match="invalid value"):
        sink.emit(
            "app_started",
            app_version="Secret transcript words",
            managed=True,
            policy_state="enabled",
            history_enabled=False,
        )

    text = _all_sink_text(sink)
    assert '"event": "app_started"' in text
    assert '"policy_state": "enabled"' in text
    assert "Secret transcript words" not in text


@pytest.mark.parametrize(
    ("event", "fields"),
    [
        (
            "app_started",
            {
                "app_version": "{canary}",
                "managed": True,
                "policy_state": "enabled",
                "history_enabled": False,
            },
        ),
        (
            "lockdown_transition",
            {
                "phase": "entry_failed",
                "managed": True,
                "history_enabled": False,
                "error_type": "{canary}",
            },
        ),
        (
            "support_export",
            {"status": "{canary}", "event_count": 1},
        ),
    ],
)
def test_content_like_strings_are_rejected_before_memory_or_disk(
    tmp_path: Path,
    event: str,
    fields: dict[str, object],
) -> None:
    sink = SafeDiagnosticSink(tmp_path)

    for canary in CANARIES:
        candidate = {
            name: (canary if value == "{canary}" else value)
            for name, value in fields.items()
        }
        with pytest.raises(SafeDiagnosticRejected):
            sink.emit(event, **candidate)

    text = _all_sink_text(sink)
    for canary in CANARIES:
        assert canary not in text


def test_safe_sink_enforces_joint_event_and_byte_retention(tmp_path: Path) -> None:
    sink = SafeDiagnosticSink(tmp_path)

    for index in range(MAX_EVENTS * 3):
        assert sink.emit(
            "support_export",
            status="prepared",
            event_count=index % (MAX_EVENTS + 1),
        )

    snapshot = sink.snapshot()
    assert len(snapshot) <= MAX_EVENTS
    assert sink.path.stat().st_size <= MAX_BYTES
    assert len(sink.serialized().encode("utf-8")) <= MAX_BYTES


def test_tampered_disk_rows_are_never_loaded_or_exported(tmp_path: Path) -> None:
    path = tmp_path / "lockdown-safe-diagnostics.jsonl"
    path.write_text(
        '{"timestamp":"2026-08-13T12:00:00Z","event":"app_started",'
        '"fields":{"app_version":"1.4.0","managed":true,'
        '"policy_state":"enabled","history_enabled":false}}\n'
        '{"timestamp":"2026-08-13T12:00:01Z","event":"user_message",'
        '"fields":{"value":"Secret transcript words"}}\n',
        encoding="utf-8",
    )

    sink = SafeDiagnosticSink(tmp_path)

    assert len(sink.snapshot()) == 1
    assert "Secret transcript words" not in _all_sink_text(sink)


def test_clear_removes_pre_lockdown_memory_and_disk(tmp_path: Path) -> None:
    sink = SafeDiagnosticSink(tmp_path)
    sink.emit(
        "app_started",
        app_version="1.4.0",
        managed=False,
        policy_state="absent",
        history_enabled=False,
    )

    sink.clear()

    assert sink.snapshot() == []
    assert not sink.path.exists()


def test_lockdown_entry_preserves_prior_safe_ring_and_adds_transition(
    tmp_path: Path,
) -> None:
    from whisperstream.server import WhisperstreamServer

    sink = SafeDiagnosticSink(tmp_path)
    sink.emit(
        "app_started",
        app_version="1.4.0",
        managed=False,
        policy_state="absent",
        history_enabled=False,
    )
    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._safe_diagnostics = sink
    server._app = SimpleNamespace(
        config=SimpleNamespace(history_enabled=False),
        managed_lockdown_policy=SimpleNamespace(enabled=True),
        begin_persistence_transition=MagicMock(),
    )
    server._imports = MagicMock()
    server._revoke_bakeoffs = MagicMock()
    server._clear_agent_speak_state = MagicMock()
    server._begin_lockdown_entry()

    server._revoke_bakeoffs.assert_called_once_with("lockdown_locked")
    records = sink.snapshot()
    assert [record["event"] for record in records] == [
        "app_started",
        "lockdown_transition",
    ]
    assert records[1]["fields"] == {
        "phase": "entry_started",
        "managed": True,
        "history_enabled": False,
    }


def test_failed_entry_records_bounded_failure_without_raw_error(
    tmp_path: Path,
) -> None:
    from whisperstream.server import WhisperstreamServer

    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._safe_diagnostics = SafeDiagnosticSink(tmp_path)
    server._app = SimpleNamespace(
        config=SimpleNamespace(lockdown_mode=True, history_enabled=False),
        managed_lockdown_policy=SimpleNamespace(enabled=False),
        end_persistence_transition=MagicMock(),
    )
    server._diagnostics_locked = MagicMock(return_value=True)
    server._restore_diagnostics_after_failed_entry()

    assert server._safe_diagnostics.snapshot()[0]["fields"] == {
        "phase": "entry_failed",
        "managed": False,
        "history_enabled": False,
    }


def test_local_support_zip_has_only_safe_files_and_never_connects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = SafeDiagnosticSink(tmp_path)
    sink.emit(
        "app_started",
        app_version="1.4.0",
        managed=True,
        policy_state="enabled",
        history_enabled=False,
    )
    (tmp_path / "whisperstream.log").write_text(
        "Secret transcript words", encoding="utf-8"
    )
    (tmp_path / "access.log").write_text("record-secret-456", encoding="utf-8")

    def refuse_network(*_args, **_kwargs):
        raise AssertionError("support export must not make a network request")

    monkeypatch.setattr(socket, "create_connection", refuse_network)
    bundle = sink.build_support_zip(
        app_version="1.4.0",
        os_family="Windows",
        runtime_version="3.12.11",
        model_loaded=True,
    )

    with zipfile.ZipFile(io.BytesIO(bundle.getvalue())) as archive:
        assert set(archive.namelist()) == {
            "safe-diagnostics.jsonl",
            "system-info.json",
        }
        exported = "\n".join(
            archive.read(name).decode("utf-8") for name in archive.namelist()
        )
    for canary in CANARIES:
        assert canary not in exported
    assert "access.log" not in exported
    assert "whisperstream.log" not in exported
