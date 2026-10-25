from __future__ import annotations

import os
import sys
from pathlib import Path

import psutil
import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows Qwen supervisor integration requires the installed Windows model and worker",
        allow_module_level=True,
    )

from whisperstream.qwen_manager import QwenManager

from tests._speech_fixture import requires_speech_fixture, speech_audio


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_QWEN_E2E = os.environ.get("WHISPERSTREAM_RUN_QWEN_E2E") == "1"
QWEN_MODEL_DIR = (
    Path(os.environ["LOCALAPPDATA"])
    / "Whisperstream-dev"
    / "models"
    / "qwen3-asr-0.6b-int8"
)
REQUIRED_MODEL_FILES = (
    "conv_frontend.onnx",
    "encoder.int8.onnx",
    "decoder.int8.onnx",
    "tokenizer/vocab.json",
    "tokenizer/merges.txt",
    "tokenizer/tokenizer_config.json",
)


_fixture_audio = speech_audio


@requires_speech_fixture
@pytest.mark.skipif(
    not RUN_QWEN_E2E,
    reason="set WHISPERSTREAM_RUN_QWEN_E2E=1 for the installed-model smoke",
)
def test_real_qwen_worker_transcribe_preview_offload_and_reload() -> None:
    missing = [name for name in REQUIRED_MODEL_FILES if not (QWEN_MODEL_DIR / name).is_file()]
    assert not missing, "Qwen integration gate requested but model files are missing"

    import logging

    manager = QwenManager(
        model_name="qwen3-asr-0.6b-int8",
        threads=4,
        logger=logging.getLogger("tests.qwen-integration"),
        model_dir=QWEN_MODEL_DIR,
        timeout=0,
        forced_language="English",
    )
    first_pid = manager.worker_pid
    audio, sample_rate = _fixture_audio()
    try:
        assert first_pid is not None and first_pid != os.getpid()
        assert psutil.pid_exists(first_pid)
        final_text = manager.transcribe(audio, sample_rate=sample_rate)
        assert final_text.strip()

        preview = manager.try_transcribe_preview(audio, sample_rate=sample_rate)
        assert preview is not None and preview.text.strip()
        assert preview.tokens is None and preview.timestamps is None

        manager.unload()
        assert not psutil.pid_exists(first_pid)
        assert manager.worker_pid is None

        manager.ensure_loaded()
        replacement_pid = manager.worker_pid
        assert replacement_pid is not None and replacement_pid != first_pid
        assert psutil.pid_exists(replacement_pid)
    finally:
        final_pid = manager.worker_pid
        manager.shutdown()
    if final_pid is not None:
        assert not psutil.pid_exists(final_pid)
