"""Qwen forced-language routing across the supervised worker boundary."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from whisperstream.language_routing import qwen_forced_language_value
from whisperstream.qwen_manager import QwenManager


def test_resolver_maps_iso_to_english_name():
    assert qwen_forced_language_value("ja", True) == "Japanese"
    assert qwen_forced_language_value("en", True) == "English"
    assert qwen_forced_language_value("ZH", True) == "Chinese"
    assert qwen_forced_language_value("yue", True) == "Cantonese"


def test_resolver_returns_none_when_not_forcing():
    assert qwen_forced_language_value("ja", False) is None
    assert qwen_forced_language_value("en", False) is None


def test_resolver_returns_none_for_non_qwen_or_unknown():
    assert qwen_forced_language_value("et", True) is None
    assert qwen_forced_language_value("xx", True) is None
    assert qwen_forced_language_value(None, True) is None
    assert qwen_forced_language_value("", True) is None


class _Supervisor:
    def __init__(self) -> None:
        self.pid = 999_001
        self.recovery_count = 0
        self.capabilities = {
            "preview": {"metadata": "text", "nonblocking_admission": True},
            "language_hint": {"dynamic_updates": True},
        }
        self.loaded = False
        self.load_bodies: list[dict] = []
        self.language_calls: list[str | None] = []
        self.audio_calls: list[str] = []

    def load(self, body):
        self.load_bodies.append(dict(body))
        self.loaded = True
        return {"loaded": True, "state": "ready"}

    def request_audio(self, operation, audio, *, sample_rate, body=None):
        self.audio_calls.append(operation)
        return {"text": "hello", "metadata": "text", "busy": False}

    def set_language_hint(self, language):
        self.language_calls.append(language)
        return {"updated": True, "loaded": self.loaded}

    def health(self):
        return {"engine_id": "qwen", "loaded": self.loaded, "state": "ready"}

    def stop_worker(self):
        self.loaded = False
        self.pid = None

    def shutdown(self):
        self.loaded = False
        self.pid = None


def _make_manager(forced_language=None):
    supervisor = _Supervisor()
    manager = QwenManager(
        model_name="qwen3-asr-0.6b-int8",
        logger=MagicMock(),
        model_dir=Path("."),
        timeout=0,
        forced_language=forced_language,
        _supervisor=supervisor,  # type: ignore[arg-type]
    )
    return manager, supervisor


def _audio():
    return np.zeros(1600, dtype=np.float32)


def test_initial_forced_language_is_owned_by_worker_load():
    manager, supervisor = _make_manager(forced_language="Japanese")
    try:
        assert supervisor.load_bodies[-1]["forced_language"] == "Japanese"
    finally:
        manager.shutdown()


def test_auto_language_loads_with_null_hint():
    manager, supervisor = _make_manager(forced_language=None)
    try:
        assert supervisor.load_bodies[-1]["forced_language"] is None
    finally:
        manager.shutdown()


def test_preview_uses_same_worker_language_state():
    manager, supervisor = _make_manager(forced_language="Japanese")
    try:
        result = manager.try_transcribe_preview(_audio(), sample_rate=16_000)
        assert result is not None and result.text == "hello"
        assert supervisor.audio_calls == ["preview"]
        assert supervisor.load_bodies[-1]["forced_language"] == "Japanese"
    finally:
        manager.shutdown()


def test_set_language_hint_updates_worker_without_reload():
    manager, supervisor = _make_manager(forced_language=None)
    try:
        initial_loads = len(supervisor.load_bodies)
        manager.set_language_hint("German")
        manager.transcribe_chunk(_audio(), sample_rate=16_000)
        manager.set_language_hint(None)
        assert supervisor.language_calls == ["German", None]
        assert len(supervisor.load_bodies) == initial_loads
    finally:
        manager.shutdown()


def test_failed_language_update_retires_worker_and_lazy_reload_keeps_new_hint():
    manager, supervisor = _make_manager(forced_language=None)

    def fail(_language):
        raise RuntimeError("content-free language update failure")

    supervisor.set_language_hint = fail  # type: ignore[method-assign]
    manager.set_language_hint("German")
    assert manager.worker_pid is None

    manager.transcribe_chunk(_audio(), sample_rate=16_000)
    try:
        assert supervisor.load_bodies[-1]["forced_language"] == "German"
    finally:
        manager.shutdown()


@pytest.fixture(autouse=True)
def _bypass_optional_detector(monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_VAD_ENABLED", "0")
