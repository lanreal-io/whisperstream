"""Synthetic adapter and REST test doubles. No native owners are started."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import numpy as np

def _make_mock_config(**overrides) -> MagicMock:
    """Return a WhisperstreamConfig-like mock with sensible defaults."""
    cfg = MagicMock()
    cfg.enhancement_provider = "local"
    cfg.enhancement_local_model = "gemma4-e2b-instruct"
    # Real dict (not an auto-speccing MagicMock) so get_local_gpu_verdict()'s
    # .get() and the worker's [model_id] = verdict write behave like the real
    # per-model map. Overrides can pre-seed it.
    cfg.enhancement_local_gpu_verdicts = {}
    cfg.enhancement_local_gpu_artifacts = {}
    cfg.enhancement_local_gpu_devices = {}
    cfg.enhancement_local_gpu_device_names = {}
    cfg.enhancement_local_endpoint = ""
    cfg.enhancement_api_key = ""
    cfg.parakeet_model = "nemo-parakeet-tdt-0.6b-v3"
    cfg.parakeet_quantization = "int8"
    cfg.word_boosts = []
    cfg.threads = None
    cfg.model_timeout = 0.0
    cfg.hotword_score = 1.5
    cfg.beam_width = 4
    cfg.lockdown_mode = False
    for key, value in overrides.items():
        setattr(cfg, key, value)
    # effective_provider() is the single dispatch chokepoint (Task 4.6).
    # Without lockdown, it mirrors the stored enhancement_provider.
    cfg.effective_provider = MagicMock(side_effect=lambda: cfg.enhancement_provider)
    # to_dict() is called by PUT /api/config; return the overrides plus defaults
    def _to_dict():
        d: dict[str, Any] = {
            "enhancement_provider": cfg.enhancement_provider,
            "enhancement_local_model": cfg.enhancement_local_model,
            "enhancement_local_endpoint": cfg.enhancement_local_endpoint,
        }
        return d
    cfg.to_dict = _to_dict
    return cfg

def _make_mock_app(*, config=None, llama_server=None) -> MagicMock:
    """Return a WhisperstreamApp-like mock."""
    app = MagicMock()
    app.config = config or _make_mock_config()
    app.stt_engine = MagicMock()  # non-None so /api/health reports model_loaded=True
    app.llama_server = llama_server
    app.resident_llama_pool = None

    # config_manager.save() and reload_config() are called on download success
    app.config_manager = MagicMock()
    app.config_manager.save = MagicMock()
    app.config_manager.protected_secret_store = None

    from whisperstream.config_manager import ConfigManager, WhisperstreamConfig

    app.config_manager.client_dict.side_effect = ConfigManager.client_dict
    app.config_manager.config_from_client_dict.side_effect = (
        lambda data, *, current: WhisperstreamConfig.from_dict(data)
    )
    app.reload_config = MagicMock()

    # license_manager for /api/health and on_startup hook
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True, "trial_remaining_seconds": 0}
    from unittest.mock import AsyncMock
    app.license_manager.maybe_revalidate = AsyncMock(return_value=None)

    app.load_stt_engine_async = MagicMock()
    app.set_event_broadcaster = MagicMock()
    app.dictation_state_snapshot.return_value = None
    app.start_hud_dictation.return_value = "started"
    app.complete_hud_dictation.return_value = "completed"
    app.cancel_hud_dictation.return_value = "cancelled"
    return app

def _make_server(*, config=None, llama_server=None, engine_generation=None) -> Any:
    """Construct a server with the Windows endpoint owners these tests exercise."""
    from whisperstream.agent_integration import create_agent_integration_adapters
    from whisperstream.server import WhisperstreamServer
    from whisperstream.windows_model_lifecycle import WindowsModelLifecycleAdapter

    mock_app = _make_mock_app(config=config, llama_server=llama_server)
    server = WhisperstreamServer(
        mock_app,
        port=0,
        logger=None,
        register_hotkeys=False,
        engine_generation=engine_generation,
    )
    server._model_lifecycle = WindowsModelLifecycleAdapter()
    (
        server._agent_plugin_adapter,
        server._agent_hook_adapter,
    ) = create_agent_integration_adapters(system="Windows", machine="x86_64")
    return server

class _FakeSupervisor:
    def __init__(self) -> None:
        self.pid = None
        self.capabilities = {
            "preview": {
                "metadata": "timed_tokens",
                "nonblocking_admission": True,
            },
            "hotwords": {"update_mode": "reload"},
        }
        self.recovery_count = 0
        self.loaded = False
        self.load_calls: list[dict[str, object]] = []
        self.unload_calls = 0
        self.audio_calls: list[tuple[str, np.ndarray, int]] = []
        self.hotword_calls: list[tuple[list[str], float]] = []
        self.shutdown_calls = 0
        self.stop_calls = 0
        self.started_pids: list[int] = []
        self.audio_result: dict[str, object] = {"text": "hello"}
        self.audio_side_effect = None

    def load(self, body):
        if self.pid is None:
            self.pid = os.getpid() + 1000 + len(self.started_pids)
            self.started_pids.append(self.pid)
        self.load_calls.append(dict(body))
        self.loaded = True
        return {"state": "ready", "loaded": True}

    def unload(self):
        self.unload_calls += 1
        self.loaded = False
        return {"state": "idle", "loaded": False}

    def stop_worker(self):
        self.stop_calls += 1
        self.unload_calls += int(self.loaded)
        self.loaded = False
        self.pid = None
        return None

    def health(self):
        return {
            "engine_id": "parakeet",
            "state": "ready" if self.loaded else "idle",
            "loaded": self.loaded,
        }

    def request_audio(self, operation, audio, *, sample_rate, body=None):
        self.audio_calls.append((operation, np.array(audio, copy=True), sample_rate))
        if self.audio_side_effect is not None:
            return self.audio_side_effect(operation, audio, sample_rate)
        return dict(self.audio_result)

    def update_hotwords(self, hotwords, score):
        self.hotword_calls.append((list(hotwords), float(score)))
        self.loaded = True
        return {"updated_count": len(hotwords), "loaded": True}

    def shutdown(self):
        self.shutdown_calls += 1
        self.loaded = False
        self.pid = None
        return None

class StopAfterBargeIn(Exception):
    """Ends _start_recording at the first capture-setup step after barge-in."""


def _bare_app(*, controller, order=None):
    """A bare WhisperstreamApp (no __init__) with just what _start_recording's
    barge-in path reads. The first capture-setup step records "capture" in
    ``order`` and raises ``StopAfterBargeIn`` so no audio machinery runs.
    """
    from whisperstream.main import WhisperstreamApp

    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.speech_controller = controller
    app.logger = MagicMock()
    app._broadcast = MagicMock()
    app.license_manager = MagicMock()
    app.license_manager.status.return_value = {"is_pro": True, "trial_remaining_seconds": 0}

    def _next_step():
        if order is not None:
            order.append("capture")
        raise StopAfterBargeIn

    app._invalidate_live_preview = MagicMock(side_effect=_next_step)
    return app
