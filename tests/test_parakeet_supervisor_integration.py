from __future__ import annotations

import os
import sys
from pathlib import Path

import psutil
import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows Parakeet supervisor integration requires the installed Windows model and worker",
        allow_module_level=True,
    )

from whisperstream.parakeet_manager import ParakeetManager
from whisperstream.stt_supervisor import NativeSttWorkerCrashed

from tests._speech_fixture import requires_speech_fixture, speech_audio, speech_text


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_MODEL_BASE = (
    Path(os.environ["WHISPERSTREAM_TEST_MODEL_BASE"])
    if os.environ.get("WHISPERSTREAM_TEST_MODEL_BASE")
    else Path(os.environ["LOCALAPPDATA"]) / "Whisperstream-dev"
)
REQUIRED_MODEL_FILES = (
    "encoder.int8.onnx",
    "decoder.int8.onnx",
    "joiner.int8.onnx",
    "tokens.txt",
)


@pytest.fixture(params=["nemo-parakeet-tdt-0.6b-v2", "nemo-parakeet-tdt-0.6b-v3"])
def parakeet_model(request):
    return request.param


def _assert_model_present(model_dir: Path) -> None:
    missing = [name for name in REQUIRED_MODEL_FILES if not (model_dir / name).is_file()]
    if missing:
        pytest.skip(
            "Parakeet supervisor integration requires the installed isolated "
            "exact Parakeet model; missing " + ", ".join(missing)
        )


def _manager(model_name: str) -> ParakeetManager:
    model_dir = TEST_MODEL_BASE / "models" / f"{model_name}-int8"
    _assert_model_present(model_dir)
    import logging

    return ParakeetManager(
        model_name=model_name,
        quantization="int8",
        threads=4,
        logger=logging.getLogger("tests.parakeet-crash"),
        model_dir=model_dir,
        timeout=0,
    )


_fixture_audio = speech_audio


def test_load_hard_exit_recovers_once_and_parent_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parakeet_model: str,
) -> None:
    sentinel = tmp_path / "load-crashed-once"
    monkeypatch.setenv("WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION", "load")
    monkeypatch.setenv(
        "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL",
        str(sentinel),
    )
    parent_pid = os.getpid()
    manager = _manager(parakeet_model)
    worker_pid = manager.worker_pid
    try:
        assert psutil.pid_exists(parent_pid)
        assert worker_pid is not None and worker_pid != parent_pid
        assert manager.recovery_count == 1
        failure = manager._supervisor.last_failure_report
        assert failure is not None
        assert failure.operation == "load"
        assert failure.pid != worker_pid
        assert not psutil.pid_exists(failure.pid)
        assert manager.health().loaded is True
    finally:
        manager.shutdown()
    assert not psutil.pid_exists(worker_pid)


@requires_speech_fixture
def test_inference_hard_exit_recovers_once_without_duplicate_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parakeet_model: str,
) -> None:
    sentinel = tmp_path / "inference-crashed-once"
    monkeypatch.setenv(
        "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION",
        "transcribe_chunk",
    )
    monkeypatch.setenv(
        "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL",
        str(sentinel),
    )
    manager = _manager(parakeet_model)
    first_worker_pid = manager.worker_pid
    audio, sample_rate = _fixture_audio()
    expected = speech_text()
    try:
        result = manager.transcribe(audio, sample_rate=sample_rate)
        replacement_pid = manager.worker_pid
        assert result.casefold() == expected.casefold()
        assert manager.recovery_count == 1
        assert replacement_pid is not None
        assert replacement_pid != os.getpid()
        assert replacement_pid != first_worker_pid
        assert first_worker_pid is not None and not psutil.pid_exists(first_worker_pid)
    finally:
        final_pid = manager.worker_pid
        manager.shutdown()
    assert final_pid is not None and not psutil.pid_exists(final_pid)


def test_repeated_load_hard_exit_is_suppressed_after_one_recovery(
    monkeypatch: pytest.MonkeyPatch,
    parakeet_model: str,
) -> None:
    monkeypatch.setenv("WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION", "load")
    monkeypatch.delenv(
        "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL",
        raising=False,
    )
    parent_pid = os.getpid()

    with pytest.raises(NativeSttWorkerCrashed) as captured:
        _manager(parakeet_model)

    failure = captured.value
    assert failure.operation == "load"
    assert failure.recovery_attempted is True
    assert failure.suppressed is True
    assert failure.report is not None
    assert not psutil.pid_exists(failure.report.pid)
    assert psutil.pid_exists(parent_pid)
