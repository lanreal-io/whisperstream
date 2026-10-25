"""Clean-process Darwin selector and native import-boundary regression."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


if sys.platform != "darwin":
    pytest.skip(
        "Darwin runtime selector import boundary",
        allow_module_level=True,
    )


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_darwin_construction_keeps_windows_owners_and_sherpa_unloaded() -> None:
    probe = r'''
import json
import logging
import sys

from whisperstream.capture import AudioCapture
from whisperstream.permission_contract import PermissionState
from whisperstream.shortcuts import KeyboardShortcutManager
from whisperstream.text_injector import TextInjector

logger = logging.getLogger("tests.macos-runtime-import-boundary")
capture = AudioCapture(
    _bridge=object(),
    _permission_state=lambda: PermissionState.NOT_DETERMINED,
)
shortcuts = KeyboardShortcutManager(logger=logger)
injector = TextInjector(
    keyboard_manager=shortcuts,
    logger=logger,
    word_filters=[],
    word_replacements={},
    post_processing="",
    clipboard_behavior=False,
    clipboard_clear_delay=0.1,
)
try:
    forbidden = (
        "sherpa_onnx",
        "onnxruntime",
        "whisperstream.audio_capture",
        "whisperstream.keyboard_shortcuts",
        "whisperstream.windows_delivery_adapter",
        "whisperstream.stt_worker_parakeet",
        "whisperstream.stt_worker_qwen",
        "whisperstream.speech.supertonic_engine",
    )
    print(json.dumps({
        "capture": type(capture).__name__,
        "shortcut": type(shortcuts).__name__,
        "delivery": type(injector._delivery).__name__,
        "forbidden_modules": sorted(name for name in forbidden if name in sys.modules),
    }))
finally:
    injector.close()
    shortcuts.close()
    capture.close()
'''
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )

    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "capture": "MacosCaptureAdapter",
        "shortcut": "MacosShortcutAdapter",
        "delivery": "MacosDeliveryAdapter",
        "forbidden_modules": [],
    }
