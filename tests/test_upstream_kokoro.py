"""Synthetic adapter/cache/delivery contracts, without recorded speech fixtures."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import threading

import numpy as np
import pytest

from whisperstream import model_downloader as downloader
from whisperstream.speech import availability
from whisperstream.speech.kokoro_engine import KokoroEngine, UPSTREAM_VOICE_IDS, resolve_model_files
from whisperstream.speech.engine_specs import KOKORO_SPEC, SpeechInputError


@pytest.fixture
def wrapper(monkeypatch):
    monkeypatch.setattr(availability, "require_engine_available", lambda _: None)
    engine = KokoroEngine()
    calls = []
    def generate(text, *, sid, speed, callback):
        output = []
        for progress in (0.5, 1.0):
            frames = np.array([0.1, -0.1], dtype=np.float32)
            output.extend(frames)
            calls.append((sid, speed, progress))
            keep_going = callback(frames, progress)
            frames[:] = 0  # Native callback buffers are temporary.
            if not keep_going:
                break
        return SimpleNamespace(samples=output)
    engine._engine = SimpleNamespace(generate=generate, alive=True)
    return engine, calls


@pytest.mark.parametrize("voice", [v.id for v in KOKORO_SPEC.voices])
def test_preserves_voices_and_continues_each_chunk(wrapper, voice):
    engine, calls = wrapper
    received = []
    frames = engine.generate("Synthetic first sentence. Another sentence.", sid=voice,
                             speed=1.5, callback=lambda samples, _: received.append(samples) or 0)
    assert calls == [(UPSTREAM_VOICE_IDS[voice], 1.5, 0.5), (UPSTREAM_VOICE_IDS[voice], 1.5, 1.0)]
    assert frames.size == 4
    assert all(np.any(samples) for samples in received)
    assert engine.num_speakers == 9
    assert engine.voice_ids == tuple(v.id for v in KOKORO_SPEC.voices)


def test_stop_callback_interrupts_native_generation(wrapper):
    engine, calls = wrapper
    frames = engine.generate("Synthetic sentence. Another sentence.", callback=lambda *_: 1)
    assert len(calls) == 1
    assert frames.size == 2


@pytest.mark.parametrize("speed", [True, 0, 3, float("nan"), float("inf"), "1"])
def test_rejects_invalid_speed_before_native(wrapper, speed):
    engine, calls = wrapper
    with pytest.raises(ValueError):
        engine.generate("Synthetic sentence.", speed=speed)
    assert calls == []


@pytest.mark.parametrize("text", ["", " ", "!?", "🙂", "中文"])
def test_rejects_unpronounceable_text_without_native_diagnostic(wrapper, text, caplog):
    engine, calls = wrapper
    with pytest.raises(SpeechInputError):
        engine.generate(text)
    assert calls == []
    assert caplog.text == ""


def fill(directory, files):
    for name in files:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")


def test_required_english_data_and_partial_cache(tmp_path):
    spec = downloader.DOWNLOAD_REGISTRY[downloader.ModelId.KOKORO_82M_FP32]
    directory = tmp_path / spec.dir_name
    fill(directory, spec.files)
    assert availability.kokoro_cached(models_root=tmp_path)
    assert resolve_model_files(directory).data_dir == directory / "espeak-ng-data"
    (directory / "espeak-ng-data/lang/gmw/en-US").unlink()
    assert not availability.kokoro_cached(models_root=tmp_path)
    with pytest.raises(FileNotFoundError, match="en-US"):
        resolve_model_files(directory)


def test_legacy_cache_load_is_non_destructive(monkeypatch, tmp_path):
    from whisperstream.speech import legacy_kokoro_engine
    monkeypatch.setattr("whisperstream.config_manager.get_models_root", lambda: tmp_path)
    legacy_dir = tmp_path / availability.LEGACY_KOKORO_DIR
    fill(legacy_dir, availability.LEGACY_KOKORO_FILES)
    assert availability.kokoro_cached(models_root=tmp_path)
    assert KokoroEngine.is_installed()
    legacy = Mock()
    monkeypatch.setattr(legacy_kokoro_engine, "KokoroEngine", lambda **_: legacy)
    engine = KokoroEngine()
    engine.load()
    engine.generate("Synthetic test.", sid="am_puck")
    legacy.load.assert_called_once()
    legacy.generate.assert_called_once_with("Synthetic test.", sid="am_puck", speed=1.0, callback=None)
    engine.unload()
    legacy.unload.assert_called_once()
    assert availability.legacy_kokoro_cached(models_root=tmp_path)


def test_complete_upstream_cache_wins_and_partial_cache_keeps_legacy(monkeypatch, tmp_path):
    from whisperstream.speech import legacy_kokoro_engine, kokoro_process
    monkeypatch.setattr("whisperstream.config_manager.get_models_root",lambda: tmp_path)
    model=downloader.ModelId.KOKORO_82M_FP32
    spec=downloader.DOWNLOAD_REGISTRY[model]
    upstream_dir=tmp_path/spec.dir_name
    fill(upstream_dir,spec.files)
    fill(tmp_path/availability.LEGACY_KOKORO_DIR,availability.LEGACY_KOKORO_FILES)
    legacy=Mock()
    native=Mock(sample_rate=24000,num_speakers=54,alive=True)
    monkeypatch.setattr(legacy_kokoro_engine,"KokoroEngine",lambda **_: legacy)
    monkeypatch.setattr(kokoro_process,"KokoroProcess",lambda *_: native)
    engine=KokoroEngine()
    engine.load()
    assert engine._engine is native
    assert engine._legacy is None
    legacy.load.assert_not_called()
    engine.unload()
    (upstream_dir/"espeak-ng-data/lang/gmw/en-US").unlink()
    engine.load()
    assert engine._engine is None
    assert engine._legacy is legacy
    engine.unload()


def test_upstream_pins_match_reviewed_manifest():
    manifest = json.loads((Path(__file__).resolve().parents[1] / "vendor/kokoro/upstream-files.json").read_text())
    source = downloader.MODEL_SOURCES[downloader.ModelId.KOKORO_82M_FP32.value]
    assert source == manifest
    assert set(source["files"]) == set(downloader.DOWNLOAD_REGISTRY[downloader.ModelId.KOKORO_82M_FP32].files)
    assert all(len(pin["sha256"]) == 64 for pin in source["files"].values())


def prepare_download(monkeypatch, tmp_path):
    monkeypatch.setattr("whisperstream.config_manager.get_models_root", lambda: tmp_path)
    monkeypatch.setattr(availability, "kokoro_download_enabled", lambda: True)
    return downloader.ModelId.KOKORO_82M_FP32


def test_failed_upstream_download_preserves_legacy_cache(monkeypatch, tmp_path):
    model = prepare_download(monkeypatch, tmp_path)
    fill(tmp_path / availability.LEGACY_KOKORO_DIR, availability.LEGACY_KOKORO_FILES)
    def fetch(*_):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(downloader, "_download_from_hf", fetch)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        downloader.download_archive(model)
    assert availability.legacy_kokoro_cached(models_root=tmp_path)
    assert not downloader.model_dir(model).exists()
    assert not (tmp_path / (downloader.DOWNLOAD_REGISTRY[model].dir_name + ".extract.tmp")).exists()


def test_new_pinned_files_are_staged_before_install(monkeypatch, tmp_path):
    model = prepare_download(monkeypatch, tmp_path)
    def fetch(spec, dest, pending, *_):
        assert not downloader.model_dir(model).exists()
        fill(dest, pending)
    monkeypatch.setattr(downloader, "_download_from_hf", fetch)
    downloader.download_archive(model)
    assert downloader.is_present(model)


def test_cancelled_staging_preserves_legacy_cache(monkeypatch, tmp_path):
    model = prepare_download(monkeypatch, tmp_path)
    fill(tmp_path / availability.LEGACY_KOKORO_DIR, availability.LEGACY_KOKORO_FILES)
    cancelled = threading.Event()
    def fetch(spec, dest, pending, *_):
        fill(dest, pending)
        cancelled.set()
    monkeypatch.setattr(downloader, "_download_from_hf", fetch)
    with pytest.raises(downloader.DownloadCancelled):
        downloader.download_archive(model, cancel_event=cancelled)
    assert availability.legacy_kokoro_cached(models_root=tmp_path)
    assert not downloader.is_present(model)
