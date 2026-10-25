"""Synthetic supervisor and manager construction for final-result contracts."""
import logging
import os
from pathlib import Path
import numpy as np
from whisperstream.supervised_stt_manager import SupervisedSttManager
from whisperstream.qwen_manager import QwenManager
from whisperstream.cohere_manager import CohereManager
from whisperstream.parakeet_manager import ParakeetManager
from whisperstream.macos_stt_manager import MacosFluidAudioManager

class _FakeSupervisor:
    def __init__(self) -> None:
        self.pid: int | None = None
        self.capabilities = {
            "preview": {"metadata": "text", "nonblocking_admission": True},
            "language_hint": {"dynamic_updates": True},
        }
        self.recovery_count = 0
        self.loaded = False
        self.load_calls: list[dict[str, object]] = []
        self.audio_calls: list[tuple[str, np.ndarray, int]] = []
        self.language_calls: list[str | None] = []
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.started_pids: list[int] = []
        self.audio_result: dict[str, object] = {"text": "hello world"}

    def load(self, body):
        if self.pid is None:
            self.pid = os.getpid() + 2000 + len(self.started_pids)
            self.started_pids.append(self.pid)
        self.load_calls.append(dict(body))
        self.loaded = True
        return {"state": "ready", "loaded": True}

    def stop_worker(self):
        self.stop_calls += 1
        self.loaded = False
        self.pid = None
        return None

    def health(self):
        return {
            "engine_id": "qwen",
            "state": "ready" if self.loaded else "idle",
            "loaded": self.loaded,
        }

    def request_audio(self, operation, audio, *, sample_rate, body=None):
        self.audio_calls.append((operation, np.array(audio, copy=True), sample_rate))
        return dict(self.audio_result)

    def set_language_hint(self, language):
        self.language_calls.append(language)
        return {"updated": True, "loaded": self.loaded}

    def shutdown(self):
        self.shutdown_calls += 1
        self.loaded = False
        self.pid = None
        return None


class FluidHarness(MacosFluidAudioManager):
    def __init__(self, supervisor):
        SupervisedSttManager.__init__(self, model_name='parakeet-tdt-0.6b-v3',
            threads=None, logger=logging.getLogger(__name__), model_dir=Path('.'),
            timeout=0, _supervisor=supervisor)


def manager_for(route, monkeypatch):
    supervisor = _FakeSupervisor()
    cls = {'qwen': QwenManager, 'cohere': CohereManager,
           'sherpa': ParakeetManager, 'fluid': FluidHarness}[route]
    if route == 'fluid':
        supervisor.capabilities = {}
        manager = cls(supervisor)
    else:
        supervisor.capabilities = {'preview': {'metadata': cls.capabilities.preview.metadata.value,
                                               'nonblocking_admission': True}}
        if cls.capabilities.hotwords:
            supervisor.capabilities['hotwords'] = {'update_mode': 'reload'}
        if cls.capabilities.language_hint:
            supervisor.capabilities['language_hint'] = {'dynamic_updates': True}
        args = dict(model_name='parakeet-tdt-0.6b-v2' if route == 'sherpa' else 'test',
            model_dir=Path('.'), logger=logging.getLogger(__name__), timeout=0,
            _supervisor=supervisor)
        if route == 'sherpa':
            args.update(quantization='int8', threads=None)
        if route == 'cohere':
            args['language'] = 'en'
        manager = cls(**args)
    return manager, supervisor
