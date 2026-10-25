"""Preview manifest integrity and immutable generation-input guardrails."""
from __future__ import annotations
import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
import pytest

REPO = Path(__file__).resolve().parents[1]

def generator():
    spec = importlib.util.spec_from_file_location("voice_sample_generator", REPO / "scripts/generate-voice-samples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_samples_fully_decode_and_match_manifest():
    assert generator().verify(REPO / "frontend/public/voice-samples") == 0


@pytest.mark.parametrize("change", ["text", "hash", "speaker", "model", "missing", "extra"])
def test_verify_rejects_misbound_or_incomplete_assets(tmp_path, change):
    directory = tmp_path / "samples"
    shutil.copytree(REPO / "frontend/public/voice-samples", directory)
    path = directory / "manifest.json"
    data = json.loads(path.read_text())
    if change == "text": data["text"] = "A stale sample sentence."
    elif change == "hash": data["clips"][0]["sha256"] = "0" * 64
    elif change == "speaker": data["clips"][0]["native_speaker_id"] = 999
    elif change == "model": data["models"]["kokoro"]["revision"] = "0" * 40
    elif change == "missing": (directory / data["clips"][0]["file"]).unlink()
    elif change == "extra": shutil.copyfile(directory / data["clips"][0]["file"], directory / "extra.ogg")
    path.write_text(json.dumps(data))
    assert generator().verify(directory) == 1


def test_generation_rejects_altered_or_missing_model_inputs(tmp_path, monkeypatch):
    from whisperstream import model_downloader, model_sources
    module = generator()
    path = tmp_path / "input.bin"
    path.write_bytes(b"synthetic model bytes")
    pinned = {"repo": "example/model", "revision": "1" * 40,
              "files": {"input.bin": {"size": path.stat().st_size, "sha256": module._digest(path)}}}
    monkeypatch.setattr(model_downloader, "model_dir", lambda _: tmp_path)
    monkeypatch.setattr(model_sources, "MODEL_SOURCES", {"synthetic": pinned})
    spec = SimpleNamespace(key="test", model_id=SimpleNamespace(value="synthetic"))
    assert module._model_inputs(spec) == pinned
    path.write_bytes(b"altered")
    with pytest.raises(ValueError, match="mismatch"): module._model_inputs(spec)
    path.unlink()
    with pytest.raises(ValueError, match="Missing"): module._model_inputs(spec)
