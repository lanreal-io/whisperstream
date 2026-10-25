"""Synthetic Kokoro frontend retention across idle unloads.

ONNX Runtime and the English frontend are mocked; no model weights, language
data or recorded speech are used. Synthesis inputs are fixed test strings.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest.mock import Mock

import numpy as np
import pytest

import whisperstream.speech.legacy_kokoro_engine as legacy_module
from whisperstream.speech import availability, kokoro_frontend
from whisperstream.speech.kokoro_engine import KokoroEngine
from whisperstream.speech.selection import EngineManager

CURATED_VOICES = (
    "af_heart", "af_bella", "af_nicole", "af_aoede", "af_kore",
    "af_sarah", "am_fenrir", "am_michael", "am_puck",
)


def _make_bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        json.dumps({"vocab": {"a": 1, "b": 2, " ": 3, ".": 4}}), encoding="utf-8"
    )
    np.savez(
        root / "voices-v1.0.npz",
        **{
            voice: np.full((510, 1, 256), index, dtype=np.float32)
            for index, voice in enumerate(CURATED_VOICES, start=1)
        },
    )
    (root / "kokoro-v1.0.onnx").write_bytes(b"test")
    return root


class _FakeData:
    instances: list["_FakeData"] = []

    def __init__(self, model_dir: Path) -> None:
        self.model_dir = model_dir
        self.__class__.instances.append(self)

    @classmethod
    def from_model_dir(cls, model_dir: Path) -> "_FakeData":
        return cls(model_dir)


class _FakeFrontend:
    instances: list["_FakeFrontend"] = []

    def __init__(self, model_dir: Path, *, data: _FakeData | None = None) -> None:
        self.model_dir = model_dir
        self.data = data
        self.calls: list[str] = []
        self.__class__.instances.append(self)

    def phonemize(self, text: str) -> str:
        self.calls.append(text)
        return "ab."


class _FakeSessionOptions:
    def __init__(self) -> None:
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0
        self.execution_mode: object | None = None


class _FakeSession:
    instances: list["_FakeSession"] = []
    error: Exception | None = None

    def __init__(self, path: str, *, sess_options, providers) -> None:
        if self.error is not None:
            raise self.error
        self.__class__.instances.append(self)

    def run(self, output_names, inputs):
        return [np.array([[0.25, -0.5, 0.75]], dtype=np.float32)]


@pytest.fixture
def legacy_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    _FakeData.instances = []
    _FakeFrontend.instances = []
    _FakeSession.instances = []
    _FakeSession.error = None
    runtime = SimpleNamespace(
        SessionOptions=_FakeSessionOptions,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=_FakeSession,
    )
    roots = [_make_bundle(tmp_path / "legacy-kokoro")]
    monkeypatch.setattr(availability, "require_engine_available", lambda _engine: None)
    monkeypatch.setattr(legacy_module, "_load_onnxruntime", lambda: runtime)
    monkeypatch.setattr(legacy_module, "KokoroEnglishFrontend", _FakeFrontend)
    monkeypatch.setattr(legacy_module, "KokoroFrontendData", _FakeData)
    monkeypatch.setattr(legacy_module, "legacy_kokoro_dir", lambda: roots[0])
    return SimpleNamespace(roots=roots, tmp_path=tmp_path)


def _legacy(retention: str = "full") -> legacy_module.KokoroEngine:
    return legacy_module.KokoroEngine(frontend_retention=retention)


def test_full_retention_reuses_one_frontend_across_idle_reloads(legacy_runtime) -> None:
    engine = _legacy()
    engine.load()
    baseline = engine.generate("Hello there.", sid="af_heart")
    frontend = engine._frontend

    for _ in range(5):
        engine.unload()
        assert engine._session is None
        assert engine._voices == {}
        assert engine._frontend is frontend
        engine.load()
        np.testing.assert_array_equal(engine.generate("Hello there.", sid="af_heart"), baseline)

    assert len(_FakeFrontend.instances) == 1
    assert len(_FakeSession.instances) == 6


def test_bundle_replacement_or_root_change_rebuilds_the_frontend(legacy_runtime) -> None:
    engine = _legacy()
    engine.load()
    engine.unload()
    (legacy_runtime.roots[0] / "config.json").write_text(
        json.dumps({"vocab": {"a": 1, "b": 2, " ": 3, ".": 4, "c": 5}}), encoding="utf-8"
    )
    engine.load()
    assert len(_FakeFrontend.instances) == 2

    engine.unload()
    legacy_runtime.roots[0] = _make_bundle(legacy_runtime.tmp_path / "moved" / "kokoro")
    engine.load()
    assert len(_FakeFrontend.instances) == 3
    assert _FakeFrontend.instances[-1].model_dir == legacy_runtime.roots[0]


def test_bundle_removal_releases_the_frontend_at_idle_unload(legacy_runtime) -> None:
    engine = _legacy()
    engine.load()
    shutil.rmtree(legacy_runtime.roots[0])
    engine.unload()
    assert engine._frontend is None
    with pytest.raises(FileNotFoundError):
        engine.load()
    assert engine._session is None


def test_load_failure_after_reuse_keeps_the_valid_frontend(legacy_runtime) -> None:
    engine = _legacy()
    engine.load()
    frontend = engine._frontend
    engine.unload()
    _FakeSession.error = RuntimeError("native load failed")
    with pytest.raises(RuntimeError, match="native load failed"):
        engine.load()
    assert engine._session is None
    assert engine._frontend is frontend
    _FakeSession.error = None
    engine.load()
    assert len(_FakeFrontend.instances) == 1


def test_dispose_releases_the_retained_frontend(legacy_runtime) -> None:
    engine = _legacy()
    engine.load()
    engine.unload()
    engine.dispose()
    engine.dispose()
    assert engine._frontend is None
    engine.load()
    assert len(_FakeFrontend.instances) == 2


def test_data_retention_rebuilds_only_the_g2p_wrapper(legacy_runtime) -> None:
    engine = _legacy("data")
    for _ in range(3):
        engine.load()
        engine.unload()
        assert engine._frontend is None
        assert engine._frontend_data is _FakeData.instances[0]
    assert len(_FakeData.instances) == 1
    assert len(_FakeFrontend.instances) == 3
    assert all(item.data is _FakeData.instances[0] for item in _FakeFrontend.instances)


def test_no_retention_rebuilds_everything(legacy_runtime) -> None:
    engine = _legacy("none")
    for _ in range(3):
        engine.load()
        engine.unload()
        assert engine._frontend is None
        assert engine._frontend_data is None
    assert len(_FakeFrontend.instances) == 3


def test_unknown_retention_mode_is_rejected(legacy_runtime) -> None:
    with pytest.raises(ValueError, match="frontend_retention"):
        _legacy("sometimes")


# ---------------------------------------------------------------------------
# Source adapter: legacy reuse, upstream worker unchanged
# ---------------------------------------------------------------------------


def _fill(directory: Path, names) -> None:
    for name in names:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")


@pytest.fixture
def legacy_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Mock]:
    monkeypatch.setattr("whisperstream.config_manager.get_models_root", lambda: tmp_path)
    monkeypatch.setattr(availability, "require_engine_available", lambda _engine: None)
    _fill(tmp_path / availability.LEGACY_KOKORO_DIR, availability.LEGACY_KOKORO_FILES)
    created: list[Mock] = []

    def factory(**_kwargs) -> Mock:
        created.append(Mock())
        return created[-1]

    monkeypatch.setattr(legacy_module, "KokoroEngine", factory)
    return created


def test_adapter_keeps_one_legacy_engine_across_idle_unloads(legacy_cache) -> None:
    engine = KokoroEngine()
    for _ in range(3):
        engine.load()
        engine.unload()
    assert len(legacy_cache) == 1
    legacy = legacy_cache[0]
    assert legacy.load.call_count == 3
    assert legacy.unload.call_count == 3
    legacy.dispose.assert_not_called()

    engine.dispose()
    legacy.dispose.assert_called_once()
    engine.load()
    assert len(legacy_cache) == 2


def test_upstream_bundle_releases_the_retained_legacy_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, legacy_cache
) -> None:
    from whisperstream import model_downloader as downloader
    from whisperstream.speech import kokoro_process

    engine = KokoroEngine()
    engine.load()
    engine.unload()
    spec = downloader.DOWNLOAD_REGISTRY[downloader.ModelId.KOKORO_82M_FP32]
    _fill(tmp_path / spec.dir_name, spec.files)
    native = Mock(sample_rate=24000, num_speakers=54, alive=True)
    monkeypatch.setattr(kokoro_process, "KokoroProcess", lambda *_: native)

    engine.load()
    assert engine._engine is native
    legacy_cache[0].dispose.assert_called_once()
    engine.dispose()
    native.close.assert_called_once()


# ---------------------------------------------------------------------------
# Shared frontend data
# ---------------------------------------------------------------------------


def test_frontends_share_language_data_with_separate_g2p(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lexicon, tagger = object(), object()
    monkeypatch.setattr(
        kokoro_frontend._EnglishLexicon, "from_model_dir", staticmethod(lambda _root: lexicon)
    )
    monkeypatch.setattr(
        kokoro_frontend.AveragedPerceptronTagger,
        "from_model_dir",
        staticmethod(lambda _root: tagger),
    )
    (tmp_path / "config.json").write_text(json.dumps({"vocab": {"a": 1}}), encoding="utf-8")
    data = kokoro_frontend.KokoroFrontendData.from_model_dir(tmp_path)
    first = kokoro_frontend.KokoroEnglishFrontend(tmp_path, fallback=Mock(), data=data)
    second = kokoro_frontend.KokoroEnglishFrontend(tmp_path, fallback=Mock(), data=data)
    assert first.data is second.data is data
    assert first._lexicon is lexicon and first._tagger is tagger
    assert first._vocabulary == frozenset({"a"})
    assert first._fallback is not second._fallback


# ---------------------------------------------------------------------------
# EngineManager: idle release unloads, shutdown disposes
# ---------------------------------------------------------------------------


class _Engine:
    def __init__(self) -> None:
        self.unload_calls = 0
        self.dispose_calls = 0

    def is_installed(self) -> bool:
        return True

    def unload(self) -> None:
        self.unload_calls += 1

    def dispose(self) -> None:
        self.dispose_calls += 1


class _Scheduled:
    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback

    def cancel(self) -> None:
        pass

    def fire(self) -> None:
        self.callback()


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[_Scheduled] = []

    def __call__(self, _delay: float, callback: Callable[[], None]) -> _Scheduled:
        self.calls.append(_Scheduled(callback))
        return self.calls[-1]


@pytest.fixture
def kokoro_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(availability, "require_engine_available", lambda _engine: None)


def test_idle_release_unloads_but_shutdown_disposes(kokoro_available) -> None:
    scheduler = _Scheduler()
    engine = _Engine()
    manager = EngineManager(
        engine_factory=lambda _key: engine, scheduler=scheduler, idle_timeout=5.0
    )
    manager.acquire("kokoro").release()
    scheduler.calls[-1].fire()
    assert (engine.unload_calls, engine.dispose_calls) == (1, 0)
    assert manager.shutdown(timeout=0.0) is True
    assert (engine.unload_calls, engine.dispose_calls) == (1, 1)


def test_shutdown_disposes_an_engine_released_after_the_wait(kokoro_available) -> None:
    engine = _Engine()
    manager = EngineManager(engine_factory=lambda _key: engine)
    lease = manager.acquire("kokoro")
    assert manager.shutdown(timeout=0.0) is False
    assert engine.dispose_calls == 0
    lease.release()
    assert (engine.unload_calls, engine.dispose_calls) == (0, 1)
