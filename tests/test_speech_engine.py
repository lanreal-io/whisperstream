"""Unit tests for the Supertonic speech engine.

All sherpa-onnx construction and generation is mocked.

TRANSCRIPT DATA note: synthesis calls use fixed, non-sensitive test strings.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

# Keep the engine's Windows DLL discovery order intact before capability
# collection imports sherpa itself.
if sys.platform == "win32":
    _spec = importlib.util.find_spec("onnxruntime")
    if _spec and _spec.origin:
        _ort_dir = str(Path(_spec.origin).parent)
        os.add_dll_directory(_ort_dir)
        os.environ["PATH"] = _ort_dir + os.pathsep + os.environ.get("PATH", "")
        _capi_dir = Path(_spec.origin).parent / "capi"
        if _capi_dir.exists():
            os.add_dll_directory(str(_capi_dir))
            os.environ["PATH"] = str(_capi_dir) + os.pathsep + os.environ["PATH"]

sherpa_onnx = pytest.importorskip(
    "sherpa_onnx",
    reason="Windows sherpa Supertonic runtime is unavailable on this platform",
)

import whisperstream.speech.supertonic_engine as supertonic_module
from whisperstream.model_downloader import ModelId
from whisperstream.speech.supertonic_engine import SupertonicEngine, resolve_model_files


MODEL_FILES = (
    "duration_predictor.int8.onnx",
    "text_encoder.int8.onnx",
    "vector_estimator.int8.onnx",
    "vocoder.int8.onnx",
    "tts.json",
    "unicode_indexer.bin",
    "voice.bin",
)


def _make_bundle(root: Path, *, missing: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        if name != missing:
            (root / name).write_bytes(b"test")
    return root


class _FakeSupertonicConfig:
    pass


class _FakeModelConfig:
    pass


class _FakeTtsConfig:
    pass


class _FakeOfflineTts:
    instances: list["_FakeOfflineTts"] = []
    error: Exception | None = None

    def __init__(self, config: object) -> None:
        if self.error is not None:
            raise self.error
        self.config = config
        self.sample_rate = 44_100
        self.num_speakers = 10
        self.generate_calls: list[tuple[str, int, float, object]] = []
        self.__class__.instances.append(self)

    def generate(
        self,
        text: str,
        *,
        sid: int,
        speed: float,
        callback: object,
    ) -> SimpleNamespace:
        self.generate_calls.append((text, sid, speed, callback))
        return SimpleNamespace(samples=[0.25, -0.5, 0.75])


@pytest.fixture
def mocked_sherpa(monkeypatch: pytest.MonkeyPatch) -> type[_FakeOfflineTts]:
    _FakeOfflineTts.instances = []
    _FakeOfflineTts.error = None
    monkeypatch.setattr(
        sherpa_onnx,
        "OfflineTtsSupertonicModelConfig",
        _FakeSupertonicConfig,
    )
    monkeypatch.setattr(
        sherpa_onnx,
        "OfflineTtsModelConfig",
        _FakeModelConfig,
    )
    monkeypatch.setattr(sherpa_onnx, "OfflineTtsConfig", _FakeTtsConfig)
    monkeypatch.setattr(sherpa_onnx, "OfflineTts", _FakeOfflineTts)
    return _FakeOfflineTts


def test_resolve_model_files_happy_path(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "supertonic-3-int8")

    resolved = resolve_model_files(bundle)

    assert resolved.duration_predictor.name == "duration_predictor.int8.onnx"
    assert resolved.text_encoder.name == "text_encoder.int8.onnx"
    assert resolved.vector_estimator.name == "vector_estimator.int8.onnx"
    assert resolved.vocoder.name == "vocoder.int8.onnx"
    assert resolved.tts_json == bundle / "tts.json"
    assert resolved.unicode_indexer == bundle / "unicode_indexer.bin"
    assert resolved.voice_style == bundle / "voice.bin"


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("duration_predictor.int8.onnx", r"duration_predictor.*\.onnx"),
        ("text_encoder.int8.onnx", r"text_encoder.*\.onnx"),
        ("vector_estimator.int8.onnx", r"vector_estimator.*\.onnx"),
        ("vocoder.int8.onnx", r"vocoder.*\.onnx"),
        ("tts.json", r"tts\.json"),
        ("unicode_indexer.bin", r"unicode_indexer\.bin"),
        ("voice.bin", r"voice\.bin"),
    ],
)
def test_resolve_model_files_names_missing_path(
    tmp_path: Path,
    missing: str,
    message: str,
) -> None:
    bundle = _make_bundle(tmp_path / "incomplete", missing=missing)

    with pytest.raises(FileNotFoundError, match=message):
        resolve_model_files(bundle)


def test_resolve_model_files_accepts_quantization_suffix(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "supertonic")
    (bundle / "vocoder.int8.onnx").rename(bundle / "vocoder.fp16.onnx")

    assert resolve_model_files(bundle).vocoder.name == "vocoder.fp16.onnx"


def test_construction_is_lazy_and_unloaded(
    mocked_sherpa: type[_FakeOfflineTts],
) -> None:
    engine = SupertonicEngine()

    assert engine._engine is None  # noqa: SLF001 - the lazy state is the contract
    assert mocked_sherpa.instances == []


def test_properties_and_generate_require_load() -> None:
    engine = SupertonicEngine()

    with pytest.raises(RuntimeError, match="load"):
        _ = engine.sample_rate
    with pytest.raises(RuntimeError, match="load"):
        _ = engine.num_speakers
    with pytest.raises(RuntimeError, match="load"):
        engine.generate("Fixed test sentence.")


def test_unload_before_load_is_noop() -> None:
    SupertonicEngine().unload()


def test_is_installed_uses_supertonic_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ModelId] = []
    monkeypatch.setattr(
        supertonic_module,
        "is_present",
        lambda model: seen.append(model) or True,
    )

    assert SupertonicEngine.is_installed() is True
    assert seen == [ModelId.SUPERTONIC_3_INT8]


def test_load_wires_exact_supertonic_config_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    mocked_sherpa: type[_FakeOfflineTts],
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "supertonic")
    monkeypatch.setattr(supertonic_module, "_model_dir", lambda model: bundle)

    engine = SupertonicEngine(num_threads=7)
    engine.load()
    engine.load()

    assert len(mocked_sherpa.instances) == 1
    native = mocked_sherpa.instances[0]
    config = native.config
    model_config = config.model
    st_config = model_config.supertonic
    assert st_config.duration_predictor == str(bundle / "duration_predictor.int8.onnx")
    assert st_config.text_encoder == str(bundle / "text_encoder.int8.onnx")
    assert st_config.vector_estimator == str(bundle / "vector_estimator.int8.onnx")
    assert st_config.vocoder == str(bundle / "vocoder.int8.onnx")
    assert st_config.tts_json == str(bundle / "tts.json")
    assert st_config.unicode_indexer == str(bundle / "unicode_indexer.bin")
    assert st_config.voice_style == str(bundle / "voice.bin")
    assert model_config.num_threads == 7
    assert model_config.provider == "cpu"
    assert engine.sample_rate == 44_100
    assert engine.num_speakers == 10


def test_default_thread_count_prioritizes_desktop_responsiveness() -> None:
    assert SupertonicEngine()._num_threads == 2


def test_failed_model_resolution_leaves_engine_unloaded(
    monkeypatch: pytest.MonkeyPatch,
    mocked_sherpa: type[_FakeOfflineTts],
    tmp_path: Path,
) -> None:
    empty = tmp_path / "missing"
    empty.mkdir()
    monkeypatch.setattr(supertonic_module, "_model_dir", lambda model: empty)
    engine = SupertonicEngine()

    with pytest.raises(FileNotFoundError):
        engine.load()

    assert engine._engine is None  # noqa: SLF001 - failed load must remain retryable
    assert mocked_sherpa.instances == []


def test_native_load_error_leaves_engine_unloaded(
    monkeypatch: pytest.MonkeyPatch,
    mocked_sherpa: type[_FakeOfflineTts],
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "supertonic")
    monkeypatch.setattr(supertonic_module, "_model_dir", lambda model: bundle)
    mocked_sherpa.error = RuntimeError("native load failed")
    engine = SupertonicEngine()

    with pytest.raises(RuntimeError, match="native load failed"):
        engine.load()

    assert engine._engine is None  # noqa: SLF001 - no partial native handle


def test_generate_forwards_voice_speed_and_cancellation_callback(
    monkeypatch: pytest.MonkeyPatch,
    mocked_sherpa: type[_FakeOfflineTts],
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "supertonic")
    monkeypatch.setattr(supertonic_module, "_model_dir", lambda model: bundle)
    logger = Mock()
    callback = Mock(return_value=1)
    engine = SupertonicEngine(logger=logger)
    engine.load()

    samples = engine.generate(
        "Fixed test sentence.",
        sid=6,
        speed=1.25,
        callback=callback,
    )

    assert samples.dtype == np.float32
    np.testing.assert_array_equal(samples, np.array([0.25, -0.5, 0.75], dtype=np.float32))
    native = mocked_sherpa.instances[0]
    assert native.generate_calls == [("Fixed test sentence.", 6, 1.25, callback)]
    assert callback.return_value == 1
    logger.debug.assert_called_once()
    assert "Fixed test sentence." not in repr(logger.debug.call_args)


def test_generate_propagates_native_error(
    monkeypatch: pytest.MonkeyPatch,
    mocked_sherpa: type[_FakeOfflineTts],
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "supertonic")
    monkeypatch.setattr(supertonic_module, "_model_dir", lambda model: bundle)
    engine = SupertonicEngine()
    engine.load()
    native = mocked_sherpa.instances[0]
    native.generate = Mock(side_effect=RuntimeError("synthesis failed"))

    with pytest.raises(RuntimeError, match="synthesis failed"):
        engine.generate("Fixed test sentence.")


def test_unload_releases_native_engine(
    monkeypatch: pytest.MonkeyPatch,
    mocked_sherpa: type[_FakeOfflineTts],
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "supertonic")
    monkeypatch.setattr(supertonic_module, "_model_dir", lambda model: bundle)
    engine = SupertonicEngine()
    engine.load()

    engine.unload()
    engine.unload()

    assert engine._engine is None  # noqa: SLF001 - release is the contract
