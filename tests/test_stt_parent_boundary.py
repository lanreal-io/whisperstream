from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PARENT_MODULES = (
    REPO_ROOT / "src/whisperstream/speech_gate_client.py",
    REPO_ROOT / "src/whisperstream/speech_gate_settings.py",
    REPO_ROOT / "src/whisperstream/parakeet_manager.py",
    REPO_ROOT / "src/whisperstream/qwen_manager.py",
    REPO_ROOT / "src/whisperstream/cohere_manager.py",
    REPO_ROOT / "src/whisperstream/onnx_stt_manager.py",
    REPO_ROOT / "src/whisperstream/macos_stt_manager.py",
    REPO_ROOT / "src/whisperstream/supervised_stt_manager.py",
    REPO_ROOT / "src/whisperstream/stt_factory.py",
    REPO_ROOT / "src/whisperstream/stt_supervisor.py",
    REPO_ROOT / "src/whisperstream/stt_worker_process.py",
    REPO_ROOT / "src/whisperstream/stt_worker_protocol.py",
)


def test_parent_side_stt_modules_have_no_native_engine_imports() -> None:
    forbidden = {"sherpa_onnx", "onnxruntime"}
    for path in PARENT_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        assert imported.isdisjoint(forbidden), f"native import in parent module {path}"


def test_parent_side_stt_modules_have_no_recognizer_construction() -> None:
    forbidden_calls = {"OfflineRecognizer", "from_transducer", "from_qwen3_asr", "from_cohere_transcribe"}
    for path in PARENT_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Name):
                used.add(node.id)
        assert used.isdisjoint(forbidden_calls), (
            f"native recognizer construction in parent module {path}"
        )


def test_long_lived_backend_import_does_not_load_sherpa() -> None:
    probe = (
        "import json, sys; "
        "import whisperstream.main; "
        "import whisperstream.parakeet_manager; "
        "import whisperstream.qwen_manager; "
        "import whisperstream.cohere_manager; "
        "import whisperstream.onnx_stt_manager; "
        "import whisperstream.stt_supervisor; "
        "print(json.dumps({'sherpa': 'sherpa_onnx' in sys.modules, "
        "'onnxruntime': 'onnxruntime' in sys.modules, "
        "'sounddevice': 'sounddevice' in sys.modules, "
        "'pynput': 'pynput' in sys.modules, "
        "'winsound': 'winsound' in sys.modules, "
        "'win32api': 'win32api' in sys.modules, "
        "'win32gui': 'win32gui' in sys.modules, "
        "'mac_worker': 'whisperstream.macos_stt_worker' in sys.modules, "
        "'parakeet_worker': 'whisperstream.stt_worker_parakeet' in sys.modules, "
        "'qwen_worker': 'whisperstream.stt_worker_qwen' in sys.modules}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    state = json.loads(completed.stdout.strip())
    assert state == {
        "sherpa": False,
        "onnxruntime": False,
        "sounddevice": False,
        "pynput": False,
        "winsound": False,
        "win32api": False,
        "win32gui": False,
        "mac_worker": False,
        "parakeet_worker": False,
        "qwen_worker": False,
    }


def test_file_only_backend_constructs_no_permission_sensitive_surface() -> None:
    probe = r'''
import json
import sys

from whisperstream.main import WhisperstreamApp
from whisperstream.server import WhisperstreamServer

app = WhisperstreamApp(
    verbose=False,
    headless=True,
    defer_model_load=True,
    file_only=True,
)
server = WhisperstreamServer(app, port=0, register_hotkeys=False)
try:
    state = {
        "keyboard": app.keyboard is None,
        "audio_capture": app.audio_capture is None,
        "audio_feedback": app.audio_feedback is None,
        "health_route": any(
            getattr(route, "path", None) == "/api/health"
            for route in server.fastapi.routes
        ),
        "forbidden_modules": sorted(
            name for name in (
                "sounddevice",
                "pynput",
                "pynput.keyboard",
                "winsound",
                "win32api",
                "win32gui",
                "sherpa_onnx",
                "onnxruntime",
                "whisperstream.audio_capture",
                "whisperstream.keyboard_shortcuts",
                "whisperstream.stt_worker_parakeet",
                "whisperstream.stt_worker_qwen",
            ) if name in sys.modules
        ),
    }
    print(json.dumps(state))
finally:
    app.cleanup()
'''
    with tempfile.TemporaryDirectory(
        prefix="whisperstream-file-only-"
    ) as data_dir:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env={
                **os.environ,
                "WHISPERSTREAM_DATA_DIR": data_dir,
            },
        )
    assert not Path(data_dir).exists()
    state = json.loads(completed.stdout.strip().splitlines()[-1])
    assert state == {
        "keyboard": True,
        "audio_capture": True,
        "audio_feedback": True,
        "health_route": True,
        "forbidden_modules": [],
    }


def test_qwen_parent_manager_operations_do_not_load_native_modules() -> None:
    probe = r'''
import json
import logging
import sys
from pathlib import Path
import numpy as np
from whisperstream.qwen_manager import QwenManager

class FakeSupervisor:
    pid = 991001
    recovery_count = 0
    capabilities = {
        "preview": {"metadata": "text", "nonblocking_admission": True},
        "language_hint": {"dynamic_updates": True},
    }
    def __init__(self): self.loaded = False
    def load(self, body): self.loaded = True; return {"loaded": True}
    def request_audio(self, operation, audio, *, sample_rate, body=None): return {"text": ""}
    def set_language_hint(self, language): return {"updated": True, "loaded": self.loaded}
    def stop_worker(self): self.loaded = False; self.pid = None
    def health(self): return {"engine_id": "qwen", "state": "ready" if self.loaded else "idle", "loaded": self.loaded}
    def shutdown(self): self.loaded = False; self.pid = None

supervisor = FakeSupervisor()
manager = QwenManager(
    model_name="qwen3-asr-0.6b-int8",
    logger=logging.getLogger("parent-boundary"),
    model_dir=Path("."),
    timeout=0,
    _supervisor=supervisor,
)
manager.set_language_hint("Japanese")
manager.try_transcribe_preview(np.zeros(16, dtype=np.float32))
manager.unload()
manager.transcribe_chunk(np.zeros(16, dtype=np.float32))
manager.shutdown()
print(json.dumps({
    "sherpa": "sherpa_onnx" in sys.modules,
    "onnxruntime": "onnxruntime" in sys.modules,
    "qwen_worker": "whisperstream.stt_worker_qwen" in sys.modules,
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(completed.stdout.strip()) == {
        "sherpa": False,
        "onnxruntime": False,
        "qwen_worker": False,
    }


def test_worker_crash_controls_are_absent_from_production_api_surfaces() -> None:
    surface = "\n".join(
        (REPO_ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "src/whisperstream/main.py",
            "src/whisperstream/server/app.py",
            # Development endpoints, when present (source releases omit them).
            "src/whisperstream/dev_endpoints.py",
        )
        if (REPO_ROOT / relative).is_file()
    )
    for forbidden in (
        "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION",
        "WHISPERSTREAM_STT_WORKER_TEST_MALFORMED_REPLY_OPERATION",
        "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION",
        "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION",
    ):
        assert forbidden not in surface
