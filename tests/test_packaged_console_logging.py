from __future__ import annotations

import logging

from rich.logging import RichHandler


class _OutputStream:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty

    def fileno(self) -> int:
        return 1

    def isatty(self) -> bool:
        return self._tty


def test_console_detection_rejects_non_tty_stream_with_file_descriptor(
    monkeypatch,
) -> None:
    from whisperstream import logger as logger_module

    monkeypatch.setattr(logger_module.sys, "stdout", _OutputStream(tty=False))

    assert logger_module._has_console() is False


def test_console_detection_accepts_interactive_terminal(monkeypatch) -> None:
    from whisperstream import logger as logger_module

    monkeypatch.setattr(logger_module.sys, "stdout", _OutputStream(tty=True))

    assert logger_module._has_console() is True


def test_non_tty_logger_uses_file_handler_without_rich_console(
    monkeypatch,
    tmp_path,
) -> None:
    from whisperstream import logger as logger_module

    name = "whisperstream.test.packaged-non-tty"
    target = logging.getLogger(name)
    saved_handlers = target.handlers[:]
    saved_level = target.level
    saved_propagate = target.propagate
    target.handlers[:] = []
    monkeypatch.setattr(logger_module.sys, "stdout", _OutputStream(tty=False))
    try:
        configured = logger_module.get_logger(name, log_dir=tmp_path)
        assert not any(isinstance(handler, RichHandler) for handler in configured.handlers)

        configured.info("packaged sidecar startup")
        for handler in configured.handlers:
            handler.flush()
        assert "packaged sidecar startup" in (
            tmp_path / "whisperstream.log"
        ).read_text(encoding="utf-8")
    finally:
        for handler in target.handlers[:]:
            target.removeHandler(handler)
            handler.close()
        target.handlers[:] = saved_handlers
        target.setLevel(saved_level)
        target.propagate = saved_propagate


def test_model_load_without_rich_console_uses_noop_status(monkeypatch) -> None:
    from whisperstream import main as main_module

    expected_engine = object()
    app = object.__new__(main_module.WhisperstreamApp)
    import threading
    from unittest.mock import Mock
    app._stt_model_operation_lock = threading.Lock()
    app._lock = threading.Lock()
    app.stt_engine = None
    app.install_stt_manager = Mock(side_effect=lambda engine, _config: setattr(app, "stt_engine", engine))
    app._console = None
    app._last_stt_failure = object()
    app.config = object()
    app.config_manager = object()
    app.logger = logging.getLogger("whisperstream.test.packaged-model-load")

    monkeypatch.setattr(
        main_module,
        "make_stt_manager",
        lambda *_args, **_kwargs: expected_engine,
    )

    app._load_stt_engine_blocking()

    assert app.stt_engine is expected_engine
    assert app.last_stt_failure is None
