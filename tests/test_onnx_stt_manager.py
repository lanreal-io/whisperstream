import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import numpy as np

from whisperstream.onnx_stt_manager import (
    ModelNotPreparedError,
    model_dir_for_sherpa,
)


from whisperstream.supervised_stt_manager import SupervisedSttManager


class _TestManager(SupervisedSttManager):
    def __init__(self, *, timeout):
        self.supervisor = MagicMock()
        self.supervisor.capabilities = {}
        self.supervisor.pid = None
        super().__init__(model_name="test", threads=None, logger=logging.getLogger(__name__),
                         model_dir=Path("unused"), timeout=timeout, _supervisor=self.supervisor)

    def _load_body(self):
        return {"model_name": "test"}


def test_sherpa_model_dir_keeps_ascii_windows_path(tmp_path: Path) -> None:
    model_dir = tmp_path / "ascii-model"
    model_dir.mkdir()

    with (
        patch("whisperstream.onnx_stt_manager.sys.platform", "win32"),
        patch("whisperstream.onnx_stt_manager._windows_short_path") as short_path,
    ):
        assert model_dir_for_sherpa(model_dir) == model_dir

    short_path.assert_not_called()


def test_sherpa_model_dir_keeps_non_windows_path(tmp_path: Path) -> None:
    model_dir = tmp_path / "Exämple" / "model"

    with (
        patch("whisperstream.onnx_stt_manager.sys.platform", "linux"),
        patch("whisperstream.onnx_stt_manager._windows_short_path") as short_path,
    ):
        assert model_dir_for_sherpa(model_dir) == model_dir

    short_path.assert_not_called()


def test_sherpa_model_dir_uses_ascii_short_path_on_windows(tmp_path: Path) -> None:
    model_dir = tmp_path / "Exämple" / "model"
    model_dir.mkdir(parents=True)
    short_model_dir = Path(r"C:\USERS\EXAMPL~1\MODEL")

    with (
        patch("whisperstream.onnx_stt_manager.sys.platform", "win32"),
        patch(
            "whisperstream.onnx_stt_manager._windows_short_path",
            return_value=short_model_dir,
        ) as short_path,
    ):
        assert model_dir_for_sherpa(model_dir) == short_model_dir

    short_path.assert_called_once_with(model_dir)


def test_sherpa_model_dir_wraps_short_path_api_failure(tmp_path: Path) -> None:
    model_dir = tmp_path / "Exämple" / "model"
    model_dir.mkdir(parents=True)

    with (
        patch("whisperstream.onnx_stt_manager.sys.platform", "win32"),
        patch(
            "whisperstream.onnx_stt_manager._windows_short_path",
            side_effect=OSError("8.3 names unavailable"),
        ),
        pytest.raises(ModelNotPreparedError, match="ASCII-only folder") as exc_info,
    ):
        model_dir_for_sherpa(model_dir)

    assert str(model_dir) in str(exc_info.value)


def test_sherpa_model_dir_rejects_non_ascii_short_path(tmp_path: Path) -> None:
    model_dir = tmp_path / "Exämple" / "model"
    model_dir.mkdir(parents=True)

    with (
        patch("whisperstream.onnx_stt_manager.sys.platform", "win32"),
        patch(
            "whisperstream.onnx_stt_manager._windows_short_path",
            return_value=model_dir,
        ),
        pytest.raises(ModelNotPreparedError, match="ASCII-compatible short path"),
    ):
        model_dir_for_sherpa(model_dir)


def test_shutdown_stops_monitor_and_releases_worker_once():
    manager = _TestManager(timeout=300)
    monitor = manager._monitor_thread
    assert monitor.is_alive()
    manager.shutdown()
    manager.shutdown()
    assert not monitor.is_alive()
    assert not manager._loaded
    manager.supervisor.shutdown.assert_called_once_with()


def test_shutdown_without_monitor_releases_worker():
    manager = _TestManager(timeout=0)
    manager.shutdown()
    assert manager._monitor_thread is None
    manager.supervisor.shutdown.assert_called_once_with()


def test_shutdown_after_idle_unload_prevents_lazy_reload():
    manager = _TestManager(timeout=0)
    manager.unload()
    manager.shutdown()
    with pytest.raises(RuntimeError, match="shut down"):
        manager.ensure_loaded()
    manager.supervisor.load.assert_called_once()


@pytest.fixture(autouse=True)
def _bypass_optional_detector(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_VAD_ENABLED", "0")
