"""Unit tests for direct, eSpeak-free Kokoro ONNX inference.

All ONNX Runtime and English-frontend construction is mocked. The model-backed
quality and roundtrip proof belongs to the packaged evidence suite.

TRANSCRIPT DATA note: synthesis calls use fixed, non-sensitive test strings.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

import whisperstream.speech.legacy_kokoro_engine as kokoro_module
from whisperstream.model_downloader import ModelId
from whisperstream.speech.legacy_kokoro_engine import (
    KOKORO_SAMPLE_RATE,
    MAX_PHONEME_IDS,
    KokoroEngine,
    resolve_model_files,
)


MODEL_FILES = (
    "kokoro-v1.0.onnx",
    "voices-v1.0.npz",
    "config.json",
)
CURATED_VOICES = (
    "af_heart",
    "af_bella",
    "af_nicole",
    "af_aoede",
    "af_kore",
    "af_sarah",
    "am_fenrir",
    "am_michael",
    "am_puck",
)


def _make_bundle(root: Path, *, missing: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        if name == missing:
            continue
        if name == "config.json":
            (root / name).write_text(
                json.dumps({"vocab": {"a": 1, "b": 2, " ": 3, ".": 4}}),
                encoding="utf-8",
            )
        elif name == "voices-v1.0.npz":
            np.savez(
                root / name,
                **{
                    voice: np.full((510, 1, 256), index, dtype=np.float32)
                    for index, voice in enumerate(CURATED_VOICES, start=1)
                },
            )
        else:
            (root / name).write_bytes(b"test")
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
    phonemes = "ab."
    instances: list["_FakeFrontend"] = []

    def __init__(self, model_dir: Path, *, data: _FakeData | None = None) -> None:
        self.model_dir = model_dir
        self.data = data
        self.calls: list[str] = []
        self.__class__.instances.append(self)

    def phonemize(self, text: str) -> str:
        self.calls.append(text)
        return self.phonemes


class _FakeSessionOptions:
    def __init__(self) -> None:
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0
        self.execution_mode: object | None = None


class _FakeSession:
    instances: list["_FakeSession"] = []
    output: np.ndarray = np.array([[0.25, -0.5, 0.75]], dtype=np.float32)
    error: Exception | None = None

    def __init__(
        self,
        path: str,
        *,
        sess_options: _FakeSessionOptions,
        providers: list[str],
    ) -> None:
        if self.error is not None:
            raise self.error
        self.path = path
        self.sess_options = sess_options
        self.providers = providers
        self.run_calls: list[dict[str, np.ndarray]] = []
        self.__class__.instances.append(self)

    def run(
        self,
        output_names: object,
        inputs: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        assert output_names is None
        self.run_calls.append(inputs)
        return [self.output.copy()]


@pytest.fixture
def mocked_runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    _FakeFrontend.instances = []
    _FakeData.instances = []
    _FakeFrontend.phonemes = "ab."
    _FakeSession.instances = []
    _FakeSession.output = np.array([[0.25, -0.5, 0.75]], dtype=np.float32)
    _FakeSession.error = None
    runtime = SimpleNamespace(
        SessionOptions=_FakeSessionOptions,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=_FakeSession,
    )
    monkeypatch.setattr(kokoro_module, "_load_onnxruntime", lambda: runtime)
    monkeypatch.setattr(kokoro_module, "KokoroEnglishFrontend", _FakeFrontend)
    monkeypatch.setattr(kokoro_module, "KokoroFrontendData", _FakeData)
    return runtime


def test_resolve_model_files_requires_exact_relative_names(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")

    files = resolve_model_files(bundle)

    assert files.model == bundle / "kokoro-v1.0.onnx"
    assert files.voices == bundle / "voices-v1.0.npz"
    assert files.config == bundle / "config.json"


@pytest.mark.parametrize("missing", MODEL_FILES)
def test_resolve_model_files_rejects_incomplete_bundle(
    tmp_path: Path,
    missing: str,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro", missing=missing)

    with pytest.raises(FileNotFoundError, match=missing):
        resolve_model_files(bundle)


def test_construction_is_lazy_and_unloaded(
    mocked_runtime: SimpleNamespace,
) -> None:
    engine = KokoroEngine()

    assert engine._session is None  # noqa: SLF001 - lazy state is the contract
    assert _FakeSession.instances == []
    assert _FakeFrontend.instances == []


def test_properties_and_generate_require_load() -> None:
    engine = KokoroEngine()

    with pytest.raises(RuntimeError, match="load"):
        _ = engine.sample_rate
    with pytest.raises(RuntimeError, match="load"):
        _ = engine.num_speakers
    with pytest.raises(RuntimeError, match="load"):
        engine.generate("Fixed test sentence.")


def test_unload_before_load_is_noop() -> None:
    KokoroEngine().unload()


def test_is_installed_uses_kokoro_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ModelId] = []
    monkeypatch.setattr(
        kokoro_module,
        "legacy_kokoro_cached",
        lambda: seen.append(True) or True,
    )

    assert KokoroEngine.is_installed() is True
    assert seen == [True]


def test_load_wires_cpu_session_threads_voices_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    original_np_load = np.load
    loads: list[tuple[Path, bool]] = []

    def tracked_load(path: Path, *, allow_pickle: bool):
        loads.append((path, allow_pickle))
        return original_np_load(path, allow_pickle=allow_pickle)

    monkeypatch.setattr(kokoro_module.np, "load", tracked_load)
    engine = KokoroEngine(num_threads=7)

    engine.load()
    engine.load()

    assert loads == [(bundle / "voices-v1.0.npz", False)]
    assert len(_FakeSession.instances) == 1
    session = _FakeSession.instances[0]
    assert session.path == str(bundle / "kokoro-v1.0.onnx")
    assert session.providers == ["CPUExecutionProvider"]
    assert session.sess_options.intra_op_num_threads == 7
    assert session.sess_options.inter_op_num_threads == 1
    assert session.sess_options.execution_mode == "sequential"
    assert _FakeFrontend.instances[0].model_dir == bundle
    assert engine.sample_rate == KOKORO_SAMPLE_RATE == 24_000
    assert engine.num_speakers == len(CURATED_VOICES)
    assert engine.voice_ids == CURATED_VOICES


def test_default_thread_count_prioritizes_desktop_responsiveness() -> None:
    assert KokoroEngine()._num_threads == 2


def test_load_rejects_missing_curated_voice_and_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    np.savez(
        bundle / "voices-v1.0.npz",
        **{
            voice: np.ones((510, 1, 256), dtype=np.float32)
            for voice in CURATED_VOICES
            if voice != "am_puck"
        },
    )
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine()

    with pytest.raises(ValueError, match="am_puck"):
        engine.load()

    assert engine._session is None  # noqa: SLF001 - failed load is retryable


@pytest.mark.parametrize(
    "shape",
    [
        (509, 1, 256),
        (510, 256),
        (510, 1, 255),
    ],
)
def test_load_rejects_invalid_voice_shape(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
    shape: tuple[int, ...],
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    archive = {
        voice: np.ones((510, 1, 256), dtype=np.float32)
        for voice in CURATED_VOICES
    }
    archive["af_heart"] = np.ones(shape, dtype=np.float32)
    np.savez(bundle / "voices-v1.0.npz", **archive)
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)

    with pytest.raises(ValueError, match="af_heart"):
        KokoroEngine().load()


def test_native_load_error_leaves_engine_unloaded(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    _FakeSession.error = RuntimeError("native load failed")
    engine = KokoroEngine()

    with pytest.raises(RuntimeError, match="native load failed"):
        engine.load()

    assert engine._session is None  # noqa: SLF001 - no partial native handle


def test_generate_forwards_string_voice_speed_and_validated_tokens(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    logger = Mock()
    callback = Mock(return_value=0)
    engine = KokoroEngine(logger=logger)
    engine.load()

    samples = engine.generate(
        "Fixed test sentence.",
        sid="af_bella",
        speed=1.25,
        callback=callback,
    )

    np.testing.assert_array_equal(
        samples,
        np.array([0.25, -0.5, 0.75], dtype=np.float32),
    )
    assert _FakeFrontend.instances[0].calls == ["Fixed test sentence."]
    inputs = _FakeSession.instances[0].run_calls[0]
    np.testing.assert_array_equal(
        inputs["tokens"],
        np.array([[0, 1, 2, 4, 0]], dtype=np.int64),
    )
    assert inputs["style"].shape == (1, 256)
    assert inputs["style"].dtype == np.float32
    assert float(inputs["style"][0, 0]) == 2.0
    np.testing.assert_array_equal(
        inputs["speed"],
        np.array([1.25], dtype=np.float32),
    )
    callback.assert_called_once()
    assert callback.call_args.args[1] == pytest.approx(1.0)
    logger.debug.assert_called_once()
    assert "Fixed test sentence." not in repr(logger.debug.call_args)


def test_generate_normalizes_frontend_whitespace_to_vocab_space(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    _FakeFrontend.phonemes = "a\n\tb"
    engine = KokoroEngine()
    engine.load()

    engine.generate("Fixed multiline test sentence.")

    np.testing.assert_array_equal(
        _FakeSession.instances[0].run_calls[0]["tokens"],
        np.array([[0, 1, 3, 3, 2, 0]], dtype=np.int64),
    )


def test_generate_chunks_at_509_ids_and_checks_cancellation_between_chunks(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    _FakeFrontend.phonemes = "a" * (MAX_PHONEME_IDS + 11)
    callback = Mock(return_value=1)
    engine = KokoroEngine()
    engine.load()

    samples = engine.generate(
        "Fixed long test sentence.",
        sid="af_heart",
        callback=callback,
    )

    assert samples.size == 3
    assert len(_FakeSession.instances[0].run_calls) == 1
    assert _FakeSession.instances[0].run_calls[0]["tokens"].shape == (
        1,
        MAX_PHONEME_IDS + 2,
    )
    callback.assert_called_once()
    assert callback.call_args.args[1] == pytest.approx(
        MAX_PHONEME_IDS / (MAX_PHONEME_IDS + 11)
    )


def test_generate_runs_all_bounded_chunks_without_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    _FakeFrontend.phonemes = "a" * (MAX_PHONEME_IDS + 11)
    engine = KokoroEngine()
    engine.load()

    samples = engine.generate("Fixed long test sentence.", sid="af_heart")

    assert samples.size == 6
    calls = _FakeSession.instances[0].run_calls
    assert [call["tokens"].shape[1] - 2 for call in calls] == [
        MAX_PHONEME_IDS,
        11,
    ]


@pytest.mark.parametrize(
    ("phonemes", "message"),
    [
        ("az", "unknown phoneme"),
        ("", "phonemes"),
    ],
)
def test_generate_rejects_invalid_frontend_output(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
    phonemes: str,
    message: str,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    _FakeFrontend.phonemes = phonemes
    engine = KokoroEngine()
    engine.load()

    with pytest.raises(ValueError, match=message):
        engine.generate("Fixed test sentence.")

    assert _FakeSession.instances[0].run_calls == []


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (np.array([], dtype=np.float32), "empty"),
        (np.array([np.nan], dtype=np.float32), "non-finite"),
        (np.array([np.inf], dtype=np.float32), "non-finite"),
    ],
)
def test_generate_rejects_invalid_model_output(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
    output: np.ndarray,
    message: str,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    _FakeSession.output = output
    engine = KokoroEngine()
    engine.load()

    with pytest.raises(RuntimeError, match=message):
        engine.generate("Fixed test sentence.")


@pytest.mark.parametrize("sid", ["missing", 1, None])
def test_generate_rejects_unknown_or_non_string_voice(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
    sid: object,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine()
    engine.load()

    with pytest.raises(ValueError, match="voice"):
        engine.generate("Fixed test sentence.", sid=sid)  # type: ignore[arg-type]


@pytest.mark.parametrize("speed", [0.0, 0.49, 2.01, np.nan, np.inf])
def test_generate_rejects_invalid_speed(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
    speed: float,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine()
    engine.load()

    with pytest.raises(ValueError, match="speed"):
        engine.generate("Fixed test sentence.", speed=speed)


def test_idle_unload_releases_session_and_voices_but_keeps_frontend(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="full")
    engine.load()
    first_session = engine._session  # noqa: SLF001 - reload identity is contract
    frontend = engine._frontend  # noqa: SLF001

    engine.unload()
    engine.unload()

    assert engine._session is None  # noqa: SLF001
    assert engine._voices == {}  # noqa: SLF001
    assert engine._vocabulary == {}  # noqa: SLF001
    assert engine._frontend is frontend  # noqa: SLF001 - retained while idle
    with pytest.raises(RuntimeError, match="load"):
        engine.generate("Hello.", sid="af_heart")
    engine.load()
    assert engine._session is not first_session  # noqa: SLF001
    assert len(_FakeSession.instances) == 2


def test_repeated_idle_reloads_reuse_one_frontend_with_identical_output(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="full")
    engine.load()
    baseline = engine.generate("Hello there.", sid="af_heart")
    baseline_inputs = _FakeSession.instances[-1].run_calls[-1]

    for _ in range(5):
        engine.unload()
        engine.load()
        output = engine.generate("Hello there.", sid="af_heart")
        np.testing.assert_array_equal(output, baseline)
        inputs = _FakeSession.instances[-1].run_calls[-1]
        assert inputs.keys() == baseline_inputs.keys()
        for name in inputs:
            np.testing.assert_array_equal(inputs[name], baseline_inputs[name])

    assert len(_FakeFrontend.instances) == 1
    assert len(_FakeSession.instances) == 6
    assert _FakeFrontend.instances[0].calls == ["Hello there."] * 6


def test_bundle_replacement_rebuilds_the_frontend(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="full")
    engine.load()
    engine.unload()

    (bundle / "config.json").write_text(
        json.dumps({"vocab": {"a": 1, "b": 2, " ": 3, ".": 4, "c": 5}}),
        encoding="utf-8",
    )
    engine.load()

    assert len(_FakeFrontend.instances) == 2
    assert engine._frontend is _FakeFrontend.instances[1]  # noqa: SLF001


def test_model_root_change_rebuilds_the_frontend(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    roots = [_make_bundle(tmp_path / "a" / "kokoro")]
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: roots[0])
    engine = KokoroEngine(frontend_retention="full")
    engine.load()
    engine.unload()

    roots[0] = _make_bundle(tmp_path / "b" / "kokoro")
    engine.load()

    assert len(_FakeFrontend.instances) == 2
    assert _FakeFrontend.instances[1].model_dir == roots[0]


def test_bundle_removal_releases_the_retained_frontend_at_idle_unload(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    import shutil

    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="full")
    engine.load()
    shutil.rmtree(bundle)

    engine.unload()

    assert engine._frontend is None  # noqa: SLF001
    with pytest.raises(FileNotFoundError):
        engine.load()
    assert engine._frontend is None  # noqa: SLF001
    assert engine._session is None  # noqa: SLF001


def test_load_failure_after_reuse_keeps_engine_unloaded_and_frontend_valid(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="full")
    engine.load()
    frontend = engine._frontend  # noqa: SLF001
    engine.unload()

    _FakeSession.error = RuntimeError("native load failed")
    with pytest.raises(RuntimeError, match="native load failed"):
        engine.load()
    assert engine._session is None  # noqa: SLF001 - no partial publish
    assert engine._voices == {}  # noqa: SLF001
    assert engine._frontend is frontend  # noqa: SLF001 - still the same bundle

    _FakeSession.error = None
    engine.load()
    assert len(_FakeFrontend.instances) == 1


def test_first_load_failure_does_not_publish_a_new_frontend(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    _FakeSession.error = RuntimeError("native load failed")
    engine = KokoroEngine(frontend_retention="full")
    with pytest.raises(RuntimeError):
        engine.load()
    assert engine._frontend is None  # noqa: SLF001
    assert engine._session is None  # noqa: SLF001


def test_dispose_releases_everything_including_the_frontend(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="full")
    engine.load()

    engine.dispose()
    engine.dispose()

    assert engine._session is None  # noqa: SLF001
    assert engine._frontend is None  # noqa: SLF001
    assert engine._voices == {}  # noqa: SLF001
    engine.load()
    assert len(_FakeFrontend.instances) == 2

    engine.unload()
    engine.dispose()  # disposing an idle engine drops the retained frontend
    assert engine._frontend is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Light retention ("data") and no retention ("none")
# ---------------------------------------------------------------------------


def test_data_retention_keeps_language_data_and_rebuilds_g2p_each_load(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="data")
    engine.load()
    baseline = engine.generate("Hello there.", sid="af_heart")

    for _ in range(4):
        engine.unload()
        assert engine._frontend is None  # noqa: SLF001 - G2P session released
        assert engine._frontend_data is _FakeData.instances[0]  # noqa: SLF001
        engine.load()
        np.testing.assert_array_equal(
            engine.generate("Hello there.", sid="af_heart"), baseline
        )

    assert len(_FakeData.instances) == 1
    assert len(_FakeFrontend.instances) == 5
    assert all(item.data is _FakeData.instances[0] for item in _FakeFrontend.instances)


def test_data_retention_rebuilds_data_when_the_bundle_changes(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="data")
    engine.load()
    engine.unload()
    (bundle / "config.json").write_text(
        json.dumps({"vocab": {"a": 1, "b": 2, " ": 3, ".": 4, "c": 5}}),
        encoding="utf-8",
    )
    engine.load()
    assert len(_FakeData.instances) == 2
    assert engine._frontend.data is _FakeData.instances[1]  # noqa: SLF001


def test_data_retention_dispose_and_removal_release_the_data(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    import shutil

    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="data")
    engine.load()
    engine.unload()
    engine.dispose()
    assert engine._frontend_data is None  # noqa: SLF001

    engine.load()
    shutil.rmtree(bundle)
    engine.unload()
    assert engine._frontend_data is None  # noqa: SLF001
    assert engine._frontend is None  # noqa: SLF001


def test_data_retention_load_failure_keeps_valid_data_and_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="data")
    engine.load()
    data = engine._frontend_data  # noqa: SLF001
    engine.unload()
    _FakeSession.error = RuntimeError("native load failed")
    with pytest.raises(RuntimeError):
        engine.load()
    assert engine._session is None  # noqa: SLF001
    assert engine._frontend is None  # noqa: SLF001
    assert engine._frontend_data is data  # noqa: SLF001


def test_no_retention_rebuilds_everything_each_load(
    monkeypatch: pytest.MonkeyPatch,
    mocked_runtime: SimpleNamespace,
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "kokoro")
    monkeypatch.setattr(kokoro_module, "legacy_kokoro_dir", lambda: bundle)
    engine = KokoroEngine(frontend_retention="none")
    for _ in range(3):
        engine.load()
        engine.unload()
        assert engine._frontend is None  # noqa: SLF001
        assert engine._frontend_data is None  # noqa: SLF001
    assert len(_FakeFrontend.instances) == 3


def test_unknown_retention_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="frontend_retention"):
        KokoroEngine(frontend_retention="sometimes")


@pytest.fixture(autouse=True)
def experimental_voice_runtime(monkeypatch):
    """These cases exercise the retained experimental Kokoro implementation."""
    monkeypatch.setenv("WHISPERSTREAM_EXPERIMENTAL", "1")


def test_default_retention_keeps_the_whole_frontend() -> None:
    assert kokoro_module.DEFAULT_FRONTEND_RETENTION == "full"
    assert KokoroEngine()._retention == "full"  # noqa: SLF001
