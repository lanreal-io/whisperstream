"""Negative-disclosure tests for inbound Claude hook diagnostics."""

from __future__ import annotations

import logging

import whisperstream.claude_hud as claude_hud


def test_payload_logging_escape_hatch_and_fingerprint_are_removed() -> None:
    assert not hasattr(claude_hud, "_RAW_HOOK_LOG_ENV")
    assert not hasattr(claude_hud, "encoding_fingerprint")


def test_known_hook_emits_no_payload_derived_diagnostic(
    caplog, monkeypatch
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_LOG_RAW_CLAUDE_HOOKS", "1")
    canaries = [
        "PRIVATE_RESPONSE_CANARY",
        "C:\\Users\\Secret Person\\project\\response.md",
        "session_id=session-secret-91",
        "record_id=record-secret-92",
        "process=Private Editor.exe",
        "domain=private.example",
        "title=Acquisition Notes",
        "device=Conference Microphone",
        "unicode=\U0001f512\u79d8\u5bc6",
    ]
    payload = {
        "hook_event_name": "Stop",
        "last_assistant_message": " ".join(canaries),
        "session_id": canaries[2],
        "cwd": canaries[1],
    }

    with caplog.at_level(logging.DEBUG, logger="whisperstream.claude_hud"):
        result = claude_hud.parse_claude_event(payload)

    assert result is not None
    for canary in canaries:
        assert canary not in caplog.text
    assert "hook inbound" not in caplog.text
    assert "raw=" not in caplog.text


def test_unknown_hook_logs_only_allowlisted_status(caplog) -> None:
    unknown_value = "PRIVATE_UNKNOWN_HOOK_\U0001f512"

    with caplog.at_level(logging.DEBUG, logger="whisperstream.claude_hud"):
        result = claude_hud.parse_claude_event(
            {"hook_event_name": unknown_value, "junk": "PRIVATE_BODY_CANARY"}
        )

    assert result is None
    assert "[ClaudeHUD] hook ignored reason=unknown_event" in caplog.text
    assert unknown_value not in caplog.text
    assert "PRIVATE_BODY_CANARY" not in caplog.text
