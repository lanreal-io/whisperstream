"""Tests for the content-free transcript access logger (Task 3.2).

Verifies:
- One log line per call, containing only timestamp + action + id.
- Allowed actions are the TranscriptStore CRUD verbs.
- Unknown/invalid actions are rejected safely (no write occurs).
- record_id is validated/truncated so long content-shaped strings are never written verbatim.
- Fuzz: no transcript-content-shaped argument is ever accepted/written.
- The access logger is INDEPENDENT of get_logger() — tearing down get_logger's handlers
  does not affect the access logger.
- WHISPERSTREAM_DATA_DIR is honoured via get_log_dir().
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_access_log(log_dir: Path) -> list[str]:
    """Return non-empty lines from the access.log under log_dir."""
    log_file = log_dir / "access.log"
    if not log_file.exists():
        return []
    return [line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect WHISPERSTREAM_DATA_DIR to a temp directory for each test."""
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    # Reset the access_log module so it picks up the new env var.
    import importlib
    import sys
    for mod_name in list(sys.modules.keys()):
        if "access_log" in mod_name:
            del sys.modules[mod_name]
    yield tmp_path
    # Clean up module state after test.
    for mod_name in list(sys.modules.keys()):
        if "access_log" in mod_name:
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# Basic write tests
# ---------------------------------------------------------------------------

class TestLogAccessBasicWrites:
    def test_disabled_snapshot_does_not_create_audit_file(self, tmp_data_dir: Path):
        from whisperstream.access_log import log_access

        log_access("get", "ordinary-record", enabled=False)

        assert not (tmp_data_dir / "logs" / "access.log").exists()

    def test_writes_one_line_per_call(self, tmp_data_dir: Path):
        from whisperstream.access_log import log_access
        log_access("get", "abc123", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 1

    def test_line_contains_action_and_id(self, tmp_data_dir: Path):
        from whisperstream.access_log import log_access
        log_access("get", "abc123", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 1
        line = lines[0]
        assert "action=get" in line
        assert "id=abc123" in line

    def test_multiple_calls_produce_multiple_lines(self, tmp_data_dir: Path):
        from whisperstream.access_log import log_access
        log_access("list", None, enabled=True)
        log_access("get", "id1", enabled=True)
        log_access("delete", "id2", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 3

    def test_line_contains_timestamp(self, tmp_data_dir: Path):
        from whisperstream.access_log import log_access
        log_access("save", "rec99", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 1
        # Timestamp should look like an ISO-8601 or similar date string.
        # At minimum a 4-digit year should appear.
        assert re.search(r"\d{4}", lines[0]), f"No timestamp found in: {lines[0]}"

    def test_none_id_written_as_none_marker(self, tmp_data_dir: Path):
        from whisperstream.access_log import log_access
        log_access("list", None, enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 1
        # id field should appear and be either "id=-" or "id=None" or similar sentinel.
        assert "id=" in lines[0]


# ---------------------------------------------------------------------------
# Allowed-action enumeration
# ---------------------------------------------------------------------------

ALLOWED_ACTIONS = ("open", "list", "get", "get_audio", "delete", "clear", "purge", "save")


class TestAllowedActions:
    @pytest.mark.parametrize("action", ALLOWED_ACTIONS)
    def test_allowed_action_writes(self, tmp_data_dir: Path, action: str):
        from whisperstream.access_log import log_access
        log_access(action, "someid", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 1
        assert f"action={action}" in lines[0]

    def test_unknown_action_does_not_write(self, tmp_data_dir: Path):
        from whisperstream.access_log import log_access
        log_access("drop_table", "some-id", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 0, "Unknown action must not produce a log line."

    def test_empty_action_does_not_write(self, tmp_data_dir: Path):
        from whisperstream.access_log import log_access
        log_access("", "some-id", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 0

    def test_action_with_injected_newline_does_not_write(self, tmp_data_dir: Path):
        from whisperstream.access_log import log_access
        log_access("get\nevil_action", "some-id", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 0, "Newline-injected action must be rejected."


# ---------------------------------------------------------------------------
# record_id validation / truncation
# ---------------------------------------------------------------------------

class TestRecordIdValidation:
    def test_normal_id_passes_through(self, tmp_data_dir: Path):
        from whisperstream.access_log import log_access
        log_access("get", "01HXYZ-ABCD", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert "01HXYZ-ABCD" in lines[0]

    def test_long_id_is_truncated(self, tmp_data_dir: Path):
        """A record_id longer than a reasonable ID limit is truncated, never written verbatim."""
        from whisperstream.access_log import log_access
        # A realistic transcript ID is <= 128 chars; passing 1000 chars should truncate.
        long_id = "x" * 1000
        log_access("get", long_id, enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 1
        # The full 1000-char string must NOT appear verbatim.
        assert long_id not in lines[0], "Long record_id must be truncated before writing."

    def test_id_with_newline_is_sanitized(self, tmp_data_dir: Path):
        """Newlines in record_id must not produce multiple log lines."""
        from whisperstream.access_log import log_access
        log_access("get", "abc\nevil_line content=sensitive", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        # Must be at most one non-empty line.
        assert len(lines) <= 1
        # The injected content must not appear.
        if lines:
            assert "sensitive" not in lines[0]

    def test_id_with_spaces_is_sanitized(self, tmp_data_dir: Path):
        """Spaces in record_id could look like extra key=value pairs; must be sanitized."""
        from whisperstream.access_log import log_access
        log_access("get", "abc def injected=true", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 1
        # The injected key=value must not appear as a separate field after the id.
        assert "injected=true" not in lines[0]


# ---------------------------------------------------------------------------
# Fuzz: no transcript-content-shaped argument is ever written
# ---------------------------------------------------------------------------

class TestNoContentWritten:
    def test_long_natural_language_as_record_id_not_written_verbatim(self, tmp_data_dir: Path):
        """Passing long natural-language text as record_id must not write it verbatim."""
        from whisperstream.access_log import log_access
        # Simulate a caller accidentally passing transcript text as record_id.
        transcript_text = "The patient was seen on Monday and was prescribed 500mg of ibuprofen."
        log_access("get", transcript_text, enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        # The full transcript string must not appear.
        full_text_in_log = any(transcript_text in line for line in lines)
        assert not full_text_in_log, (
            "Transcript-content-shaped record_id must not be written verbatim to access log."
        )

    def test_sentence_length_id_triggers_truncation(self, tmp_data_dir: Path):
        """Any ID > 128 chars (sentence territory) is truncated."""
        from whisperstream.access_log import log_access
        sentence = "A" * 200
        log_access("save", sentence, enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 1
        assert sentence not in lines[0]

    def test_log_line_format_is_metadata_only(self, tmp_data_dir: Path):
        """Each log line must ONLY contain: timestamp, action=..., id=... fields."""
        from whisperstream.access_log import log_access
        log_access("delete", "myrecord123", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) == 1
        line = lines[0]
        # Strip timestamp prefix (anything before 'action='), then check remaining fields.
        assert "action=" in line
        assert "id=" in line
        # Should not contain any field keys that look like content fields.
        for forbidden_key in ("text=", "transcript=", "content=", "audio=", "data="):
            assert forbidden_key not in line, (
                f"Forbidden field '{forbidden_key}' found in access log line: {line}"
            )


# ---------------------------------------------------------------------------
# Independence from get_logger()
# ---------------------------------------------------------------------------

class TestLoggerIndependence:
    def test_access_logger_not_child_of_whisperstream_logger(self, tmp_data_dir: Path):
        """The access logger must be independent of the main whisperstream logger.

        Its name must NOT be 'whisperstream' (which would make it a parent) and
        must NOT be a child of 'whisperstream' via the dotted-hierarchy convention
        (e.g. 'whisperstream.something').  It may share a common prefix fragment
        as long as it is not a true hierarchy child.
        """
        from whisperstream.access_log import get_access_logger, _ACCESS_LOGGER_NAME
        access_logger = get_access_logger()
        # Must not be the root 'whisperstream' logger.
        assert access_logger.name != "whisperstream", (
            "Access logger must not be the 'whisperstream' diagnostic logger."
        )
        # Must not be a dotted child 'whisperstream.*'.
        assert not access_logger.name.startswith("whisperstream."), (
            f"Access logger '{access_logger.name}' must not be a hierarchy child "
            "of the 'whisperstream' diagnostic logger."
        )

    def test_tearing_down_whisperstream_logger_does_not_affect_access_log(
        self, tmp_data_dir: Path
    ):
        """Removing all handlers from the 'whisperstream' diagnostic logger must not
        silence the access logger."""
        import logging
        from whisperstream.access_log import log_access

        # Tear down the whisperstream diagnostic logger.
        ws_logger = logging.getLogger("whisperstream")
        for handler in list(ws_logger.handlers):
            ws_logger.removeHandler(handler)
            handler.close()
        ws_logger.addHandler(logging.NullHandler())

        # Access log must still write.
        log_access("get", "independence-test-id", enabled=True)
        lines = _read_access_log(tmp_data_dir / "logs")
        assert len(lines) >= 1
        assert any("independence-test-id" in line for line in lines)

    def test_access_logger_has_its_own_rotating_file_handler(self, tmp_data_dir: Path):
        """The access logger must have a RotatingFileHandler attached."""
        from logging.handlers import RotatingFileHandler
        from whisperstream.access_log import get_access_logger
        access_logger = get_access_logger()
        handler_types = [type(h) for h in access_logger.handlers]
        assert RotatingFileHandler in handler_types, (
            "Access logger must have its own RotatingFileHandler."
        )

    def test_access_log_file_is_named_access_log(self, tmp_data_dir: Path):
        """The rotating file must be named 'access.log' under the log dir."""
        from logging.handlers import RotatingFileHandler
        from whisperstream.access_log import get_access_logger
        access_logger = get_access_logger()
        file_names = [
            Path(h.baseFilename).name
            for h in access_logger.handlers
            if isinstance(h, RotatingFileHandler)
        ]
        assert "access.log" in file_names, (
            f"Expected 'access.log'; got handler files: {file_names}"
        )

    def test_access_logger_does_not_propagate_to_root(self, tmp_data_dir: Path):
        """The access logger must not propagate to the root logger (diagnostic leak guard)."""
        from whisperstream.access_log import get_access_logger
        access_logger = get_access_logger()
        assert not access_logger.propagate, (
            "Access logger must not propagate to root logger."
        )


# ---------------------------------------------------------------------------
# WHISPERSTREAM_DATA_DIR honoured
# ---------------------------------------------------------------------------

class TestDataDirEnvVar:
    def test_access_log_written_under_data_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """access.log should appear under the WHISPERSTREAM_DATA_DIR/logs/ path."""
        custom_dir = tmp_path / "custom_data"
        custom_dir.mkdir()
        monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(custom_dir))

        import sys
        for mod_name in list(sys.modules.keys()):
            if "access_log" in mod_name:
                del sys.modules[mod_name]

        from whisperstream.access_log import log_access
        log_access("list", None, enabled=True)

        log_file = custom_dir / "logs" / "access.log"
        assert log_file.exists(), f"Expected access.log at {log_file}"
        lines = [l for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1

        # Cleanup module for subsequent tests.
        for mod_name in list(sys.modules.keys()):
            if "access_log" in mod_name:
                del sys.modules[mod_name]
