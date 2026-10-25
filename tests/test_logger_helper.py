"""Tests for log_transcription_event() and lockdown logging gate in whisperstream.logger."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from whisperstream.logger import get_logger, log_transcription_event, reconfigure_logging


def test_emits_stage_and_counts_correctly(caplog):
    """Test 1: correct format — stage, duration, chars, words, truncated all appear."""
    logger = logging.getLogger("test.transcription.format")
    with caplog.at_level(logging.INFO, logger="test.transcription.format"):
        log_transcription_event(
            logger,
            "inference",
            duration_s=1.23,
            char_count=127,
            word_count=21,
            truncated=False,
        )
    assert len(caplog.records) == 1
    msg = caplog.records[0].message
    assert "transcription" in msg
    assert "stage=inference" in msg
    assert "duration=1.23s" in msg
    assert "chars=127" in msg
    assert "words=21" in msg
    assert "truncated=false" in msg


def test_omits_none_fields(caplog):
    """Test 2: None fields (duration_s, char_count, word_count) must not appear in output."""
    logger = logging.getLogger("test.transcription.none_omit")
    with caplog.at_level(logging.INFO, logger="test.transcription.none_omit"):
        log_transcription_event(
            logger,
            "pre_process",
            duration_s=None,
            char_count=None,
            word_count=None,
        )
    assert len(caplog.records) == 1
    msg = caplog.records[0].message
    assert "stage=pre_process" in msg
    # None fields must not appear
    assert "duration=" not in msg
    assert "chars=" not in msg
    assert "words=" not in msg
    # truncated should still appear (it has a default of False)
    assert "truncated=false" in msg


def test_level_routing(caplog):
    """Test 3: level argument controls which log level is used."""
    logger = logging.getLogger("test.transcription.level")
    # Emit at DEBUG
    with caplog.at_level(logging.DEBUG, logger="test.transcription.level"):
        log_transcription_event(
            logger,
            "chunk_in",
            level=logging.DEBUG,
        )
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG

    caplog.clear()

    # Emit at WARNING
    with caplog.at_level(logging.WARNING, logger="test.transcription.level"):
        log_transcription_event(
            logger,
            "chunk_out",
            level=logging.WARNING,
        )
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


def test_rejects_text_kwarg():
    """Test 4: passing text=, transcript=, transcription=, processed=, or chunk= raises TypeError."""
    logger = logging.getLogger("test.transcription.reject")

    with pytest.raises(TypeError):
        log_transcription_event(logger, "post_process", text="hello world")

    with pytest.raises(TypeError):
        log_transcription_event(logger, "post_process", transcript="hello world")

    with pytest.raises(TypeError):
        log_transcription_event(logger, "post_process", transcription="hello world")

    with pytest.raises(TypeError):
        log_transcription_event(logger, "post_process", processed="hello world")

    with pytest.raises(TypeError):
        log_transcription_event(logger, "post_process", chunk="hello world")


def test_truncated_true_in_output(caplog):
    """Test 5: truncated=True must appear as truncated=true in the log line."""
    logger = logging.getLogger("test.transcription.truncated")
    with caplog.at_level(logging.INFO, logger="test.transcription.truncated"):
        log_transcription_event(
            logger,
            "final",
            char_count=500,
            word_count=85,
            truncated=True,
        )
    assert len(caplog.records) == 1
    msg = caplog.records[0].message
    assert "truncated=true" in msg
    # Ensure it is not false
    assert "truncated=false" not in msg


def test_extra_kwargs_appear_in_output(caplog):
    """Bonus: extra key=value pairs passed via **extra appear in the log line."""
    logger = logging.getLogger("test.transcription.extra")
    with caplog.at_level(logging.INFO, logger="test.transcription.extra"):
        log_transcription_event(
            logger,
            "inference",
            char_count=50,
            word_count=8,
            model="parakeet-fp16",
            attempt=2,
        )
    assert len(caplog.records) == 1
    msg = caplog.records[0].message
    assert "model=parakeet-fp16" in msg
    assert "attempt=2" in msg


# ---------------------------------------------------------------------------
# Task 3.3 — Lockdown logging gate tests
# ---------------------------------------------------------------------------

def _fresh_logger_name(suffix: str) -> str:
    """Return a unique logger name so each test gets an uncached logger."""
    import uuid
    return f"whisperstream.test.lockdown.{suffix}.{uuid.uuid4().hex[:8]}"


class TestGetLoggerLockdown:
    """The compatibility flag never suppresses safe diagnostics."""

    def test_lockdown_on_keeps_file_handler(self, tmp_path):
        """Voluntary Lockdown retains the rotating support log."""
        from logging.handlers import RotatingFileHandler
        name = _fresh_logger_name("null_only")
        logger = get_logger(name, level=logging.INFO, log_dir=tmp_path, lockdown=True)
        assert any(isinstance(h, RotatingFileHandler) for h in logger.handlers)

    def test_lockdown_on_writes_safe_log_file(self, tmp_path):
        name = _fresh_logger_name("no_file")
        logger = get_logger(name, level=logging.INFO, log_dir=tmp_path, lockdown=True)
        logger.info("managed diagnostic heartbeat")
        assert "managed diagnostic heartbeat" in (tmp_path / "whisperstream.log").read_text(encoding="utf-8")

    def test_lockdown_off_default_path_unchanged(self, tmp_path):
        """With lockdown=False (default), get_logger() behaviour is unchanged — file handler added."""
        from logging.handlers import RotatingFileHandler
        name = _fresh_logger_name("off_default")
        logger = get_logger(name, level=logging.INFO, log_dir=tmp_path, lockdown=False)
        file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1, (
            "Lockdown-off path must still attach a RotatingFileHandler"
        )

    def test_lockdown_default_is_off(self, tmp_path):
        """Calling get_logger() without lockdown= defaults to lockdown=False (no NullHandler-only)."""
        from logging.handlers import RotatingFileHandler
        name = _fresh_logger_name("default_off")
        logger = get_logger(name, level=logging.INFO, log_dir=tmp_path)
        # At least one file handler expected (lockdown is off by default)
        file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1


class TestReconfigureLogging:
    """reconfigure_logging() tears down existing handlers and rebuilds per lockdown flag."""

    def test_reconfigure_off_to_on_keeps_file_handler(self, tmp_path):
        from logging.handlers import RotatingFileHandler
        name = _fresh_logger_name("off_to_on")
        # Start unlocked — file handler attached
        get_logger(name, level=logging.INFO, log_dir=tmp_path, lockdown=False)
        # Reconfigure to locked
        reconfigure_logging(True, name=name, level=logging.INFO, log_dir=tmp_path)
        logger = logging.getLogger(name)
        file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1

    def test_lockdown_log_filter_redacts_recognizable_secrets(self, tmp_path):
        name = _fresh_logger_name("secret_filter")
        logger = get_logger(name, log_dir=tmp_path, lockdown=True)
        logger.info(
            "support failure email=person@example.com password=hunter2 "
            "recovery_code=ABCD-EFGH-IJKL-MNOP-QRST api_key=sk-private"
        )
        text = (tmp_path / "whisperstream.log").read_text(encoding="utf-8")
        for secret in (
            "person@example.com",
            "hunter2",
            "ABCD-EFGH-IJKL-MNOP-QRST",
            "sk-private",
        ):
            assert secret not in text
        assert "<redacted-email>" in text

    def test_reconfigure_on_to_off_reattaches_file_handler(self, tmp_path):
        """Switching from lockdown=True to lockdown=False must re-attach a RotatingFileHandler."""
        from logging.handlers import RotatingFileHandler
        name = _fresh_logger_name("on_to_off")
        # Start locked
        get_logger(name, level=logging.INFO, log_dir=tmp_path, lockdown=True)
        # Reconfigure to unlocked
        reconfigure_logging(False, name=name, level=logging.INFO, log_dir=tmp_path)
        logger = logging.getLogger(name)
        file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1, (
            "After reconfigure(lockdown=False), exactly one RotatingFileHandler must be present"
        )

    def test_reconfigure_twice_does_not_duplicate_handlers(self, tmp_path):
        """Calling reconfigure twice with the same lockdown flag must not duplicate handlers."""
        from logging.handlers import RotatingFileHandler
        name = _fresh_logger_name("no_dup")
        get_logger(name, level=logging.INFO, log_dir=tmp_path, lockdown=False)
        # Two consecutive reconfigure calls with lockdown=False
        reconfigure_logging(False, name=name, level=logging.INFO, log_dir=tmp_path)
        reconfigure_logging(False, name=name, level=logging.INFO, log_dir=tmp_path)
        logger = logging.getLogger(name)
        file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1, (
            f"Duplicate reconfigure must not add duplicate handlers, got {len(file_handlers)}"
        )

    def test_reconfigure_on_keeps_file_handler(self, tmp_path):
        from logging.handlers import RotatingFileHandler
        name = _fresh_logger_name("only_null")
        get_logger(name, level=logging.INFO, log_dir=tmp_path, lockdown=False)
        reconfigure_logging(True, name=name, level=logging.INFO, log_dir=tmp_path)
        logger = logging.getLogger(name)
        assert any(isinstance(h, RotatingFileHandler) for h in logger.handlers)

    def test_reconfigure_on_writes_safe_records_to_file(self, tmp_path):
        name = _fresh_logger_name("no_write")
        # Start unlocked so the file is created
        logger = get_logger(name, level=logging.INFO, log_dir=tmp_path, lockdown=False)
        # Reconfigure to locked
        reconfigure_logging(True, name=name, level=logging.INFO, log_dir=tmp_path)
        # Capture file size before logging
        log_file = tmp_path / "whisperstream.log"
        logger.info("lockdown support heartbeat")
        assert "lockdown support heartbeat" in log_file.read_text(encoding="utf-8")


class TestAccessLoggerIsolation:
    """reconfigure_logging() and lockdown must NOT touch the ws_access_audit logger."""

    def test_access_logger_handler_survives_reconfigure_lockdown(self, tmp_path, monkeypatch):
        """reconfigure_logging(lockdown=True) must leave the access logger's handler intact."""
        from logging.handlers import RotatingFileHandler
        from whisperstream.access_log import get_access_logger

        # monkeypatch.setenv restores the PREVIOUS value (the session-scoped
        # isolated tmp dir set by tests/conftest.py) on teardown, unlike a raw
        # os.environ[...] = ... / del os.environ[...] pair, which unconditionally
        # deletes the var and so -- in a full-suite run -- clobbers that session
        # isolation for every test that runs after this one.
        monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))

        # Ensure the access logger is initialised (creates its RotatingFileHandler)
        access_logger = get_access_logger()
        access_handlers_before = [
            h for h in access_logger.handlers if isinstance(h, RotatingFileHandler)
        ]
        assert len(access_handlers_before) == 1, (
            "Access logger must have exactly one RotatingFileHandler before reconfigure"
        )

        # Now reconfigure the 'whisperstream' diagnostic logger to lockdown
        name = _fresh_logger_name("access_isolation")
        get_logger(name, level=logging.INFO, log_dir=tmp_path, lockdown=False)
        reconfigure_logging(True, name=name, level=logging.INFO, log_dir=tmp_path)

        # Access logger's handlers must be untouched
        access_handlers_after = [
            h for h in access_logger.handlers if isinstance(h, RotatingFileHandler)
        ]
        assert len(access_handlers_after) == 1, (
            "reconfigure_logging(lockdown=True) must NOT remove the access logger's handler"
        )
        # Must be the same handler object (not replaced)
        assert access_handlers_before[0] is access_handlers_after[0], (
            "Access logger's RotatingFileHandler must not be replaced by reconfigure_logging"
        )

    def test_access_logger_name_is_independent(self):
        """ws_access_audit is NOT a child of 'whisperstream' — manipulating 'whisperstream'
        logger must not propagate to ws_access_audit."""
        from whisperstream.access_log import _ACCESS_LOGGER_NAME
        # The access logger name must NOT start with 'whisperstream.'
        assert not _ACCESS_LOGGER_NAME.startswith("whisperstream"), (
            f"Access logger name '{_ACCESS_LOGGER_NAME}' must be outside the "
            "'whisperstream' hierarchy so diagnostic teardown can never silence it"
        )


# ---------------------------------------------------------------------------
# F4 (security-hardening) — harden diagnostic logging on startup under lockdown
# ---------------------------------------------------------------------------


def test_peek_lockdown_mode_reads_config_flag(tmp_path, monkeypatch):
    """peek_lockdown_mode() reflects the lockdown_mode flag in config.toml."""
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    from whisperstream.config_manager import peek_lockdown_mode

    # No config file yet -> False (fresh install).
    assert peek_lockdown_mode() is False

    (tmp_path / "config.toml").write_text("lockdown_mode = true\n", encoding="utf-8")
    assert peek_lockdown_mode() is True

    (tmp_path / "config.toml").write_text("lockdown_mode = false\n", encoding="utf-8")
    assert peek_lockdown_mode() is False

    # Garbage TOML -> False (fail safe, never crash).
    (tmp_path / "config.toml").write_text("=not valid toml=", encoding="utf-8")
    assert peek_lockdown_mode() is False


def test_app_init_under_lockdown_keeps_safe_diagnostic_logging(tmp_path, monkeypatch):
    """Voluntary Lockdown keeps the support log and independent access log."""
    from logging.handlers import RotatingFileHandler
    from unittest import mock as _mock

    from rich.logging import RichHandler

    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    # A VALID lockdown config: local provider + history off (no envelope needed).
    (tmp_path / "config.toml").write_text(
        'lockdown_mode = true\n'
        'enhancement_provider = "local"\n'
        'history_enabled = false\n',
        encoding="utf-8",
    )

    wlogger = logging.getLogger("whisperstream")
    # Detach any pre-existing handlers WITHOUT closing them so we can restore the
    # shared logger exactly for later tests.
    saved_handlers = wlogger.handlers[:]
    saved_level = wlogger.level
    saved_propagate = wlogger.propagate
    wlogger.handlers[:] = []

    try:
        # Simulate main()'s crash logger having already attached diagnostic
        # handlers before the app is constructed.
        reconfigure_logging(False, log_dir=tmp_path)
        assert any(
            isinstance(h, RotatingFileHandler) for h in wlogger.handlers
        ), "precondition: a diagnostic file handler must be attached before app init"

        from whisperstream.main import WhisperstreamApp

        with _mock.patch("whisperstream.main.AudioCapture"), _mock.patch(
            "whisperstream.main.AudioFeedback"
        ), _mock.patch("whisperstream.main.KeyboardShortcutManager"), _mock.patch(
            "whisperstream.speech.controller.SpeechController"
        ):
            WhisperstreamApp(headless=True, defer_model_load=True)

        handler_types = {type(h) for h in wlogger.handlers}
        assert RotatingFileHandler in handler_types
        from whisperstream.logger import _has_console
        assert (RichHandler in handler_types) == _has_console()

        # The content-free access logger must NOT have been silenced.
        from whisperstream.access_log import get_access_logger

        access_logger = get_access_logger()
        assert any(
            isinstance(h, RotatingFileHandler) for h in access_logger.handlers
        ), "F4: the ws_access_audit access logger must stay active under lockdown"
    finally:
        # Close whatever remains, then restore the original (unclosed) handlers.
        for h in wlogger.handlers[:]:
            wlogger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        wlogger.handlers[:] = saved_handlers
        wlogger.setLevel(saved_level)
        wlogger.propagate = saved_propagate


def test_app_init_with_envelope_only_keeps_safe_diagnostics(
    tmp_path, monkeypatch
):
    from logging.handlers import RotatingFileHandler
    from unittest import mock as _mock

    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'lockdown_mode = false\nenhancement_provider = "local"\n',
        encoding="utf-8",
    )
    (tmp_path / "lockdown.key.json").write_text("{}", encoding="utf-8")
    wlogger = logging.getLogger("whisperstream")
    saved_handlers = wlogger.handlers[:]
    saved_level = wlogger.level
    saved_propagate = wlogger.propagate
    wlogger.handlers[:] = []
    try:
        reconfigure_logging(False, log_dir=tmp_path)
        assert any(isinstance(h, RotatingFileHandler) for h in wlogger.handlers)

        from whisperstream.main import WhisperstreamApp

        with _mock.patch("whisperstream.main.AudioCapture"), _mock.patch(
            "whisperstream.main.AudioFeedback"
        ), _mock.patch("whisperstream.main.KeyboardShortcutManager"):
            WhisperstreamApp(headless=True, defer_model_load=True)

        assert any(isinstance(handler, RotatingFileHandler) for handler in wlogger.handlers)
    finally:
        for handler in wlogger.handlers[:]:
            wlogger.removeHandler(handler)
            handler.close()
        wlogger.handlers[:] = saved_handlers
        wlogger.setLevel(saved_level)
        wlogger.propagate = saved_propagate


def test_f4_app_init_without_lockdown_keeps_diagnostic_logging(tmp_path, monkeypatch):
    """Sanity: with lockdown OFF, app construction leaves a diagnostic file handler
    attached (the F4 hardening must NOT fire outside lockdown)."""
    from logging.handlers import RotatingFileHandler
    from unittest import mock as _mock

    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'lockdown_mode = false\n', encoding="utf-8"
    )

    wlogger = logging.getLogger("whisperstream")
    saved_handlers = wlogger.handlers[:]
    saved_level = wlogger.level
    saved_propagate = wlogger.propagate
    wlogger.handlers[:] = []

    try:
        from whisperstream.main import WhisperstreamApp

        with _mock.patch("whisperstream.main.AudioCapture"), _mock.patch(
            "whisperstream.main.AudioFeedback"
        ), _mock.patch("whisperstream.main.KeyboardShortcutManager"), _mock.patch(
            "whisperstream.speech.controller.SpeechController"
        ), _mock.patch(
            "whisperstream.main.sweep_stale_owned_temp_files"
        ) as stale_temp_sweep:
            app = WhisperstreamApp(headless=True, defer_model_load=True)

        stale_temp_sweep.assert_called_once_with()
        app.cleanup()

        assert any(
            isinstance(h, RotatingFileHandler) for h in wlogger.handlers
        ), "outside lockdown, the diagnostic file handler must be present"
    finally:
        for h in wlogger.handlers[:]:
            wlogger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        wlogger.handlers[:] = saved_handlers
        wlogger.setLevel(saved_level)
        wlogger.propagate = saved_propagate
