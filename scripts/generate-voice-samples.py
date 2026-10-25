"""Generate reproducible fixed-text voice previews from the pinned native models.

Install both optional voice models in an isolated WHISPERSTREAM_DATA_DIR first.
Run with the project environment, then commit clips and manifest together:
    uv run python scripts/generate-voice-samples.py
    uv run python scripts/generate-voice-samples.py --verify

No microphone, personal text, network request or playback is used by this script.
Input files must match the immutable public catalog, preventing legacy Kokoro
cache fallback from silently generating previews for another frontend.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "frontend" / "public" / "voice-samples"
MANIFEST = "manifest.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clip_path(output_dir: Path, engine_key: str, voice_id: object) -> Path:
    return output_dir / f"{engine_key}-{voice_id}.ogg"


def _model_inputs(spec) -> dict:
    from whisperstream.model_downloader import model_dir
    from whisperstream.model_sources import MODEL_SOURCES

    source = MODEL_SOURCES[spec.model_id.value]
    directory = model_dir(spec.model_id)
    files = {}
    for name, expected in source["files"].items():
        path = directory / name
        if not path.is_file():
            raise ValueError(f"Missing pinned {spec.key} input: {name}")
        actual = {"size": path.stat().st_size, "sha256": _digest(path)}
        if actual != expected:
            raise ValueError(f"Pinned {spec.key} input mismatch: {name}")
        files[name] = actual
    return {"repo": source["repo"], "revision": source["revision"], "files": files}


def generate(output_dir: Path) -> int:
    import numpy as np
    import soundfile
    import sherpa_onnx
    from whisperstream.speech.controller import SAMPLE_TEXT
    from whisperstream.speech.engine_specs import ENGINE_SPECS
    from whisperstream.speech.engines import make_engine
    from whisperstream.speech.kokoro_engine import UPSTREAM_VOICE_IDS

    # Validate both complete bundles before writing any output or loading engines.
    inputs = {key: _model_inputs(spec) for key, spec in ENGINE_SPECS.items()}
    code_paths = ["scripts/generate-voice-samples.py"] + [
        f"src/whisperstream/speech/{name}.py" for name in (
            "controller", "engine_specs", "engines", "kokoro_engine",
            "kokoro_process", "kokoro_worker", "supertonic_engine")]
    native_root = Path(sherpa_onnx.__file__).parent
    native = [{"sha256": _digest(path), "file": path.relative_to(native_root).as_posix()}
              for path in sorted(native_root.rglob("*"))
              if path.suffix.lower() in {".dll", ".pyd", ".so", ".dylib"}]
    manifest = {
        "schema_version": 1, "text": SAMPLE_TEXT, "speed": 1.0,
        "runtime": {key: importlib.metadata.version(key) for key in
                    ("sherpa-onnx", "sherpa-onnx-core", "numpy", "soundfile")},
        "encoder": {"format": "OGG", "subtype": "VORBIS",
                    "libsndfile": soundfile.__libsndfile_version__},
        "generation_code_sha256": {name: _digest(REPO_ROOT / name) for name in code_paths},
        "sherpa_native_sha256": native, "models": inputs, "clips": [],
    }
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="voice-previews-", dir=output_dir.parent) as temp:
        staging = Path(temp)
        for key, spec in ENGINE_SPECS.items():
            engine = make_engine(key)
            engine.load()
            try:
                for voice in spec.voices:
                    samples = np.asarray(engine.generate(SAMPLE_TEXT, sid=voice.id, speed=1.0))
                    if samples.ndim != 1 or not np.isfinite(samples).all() or not np.any(samples):
                        raise ValueError(f"Invalid generated audio: {key}/{voice.id}")
                    path = _clip_path(staging, key, voice.id)
                    soundfile.write(path, samples, spec.sample_rate, format="OGG", subtype="VORBIS")
                    manifest["clips"].append({
                        "file": path.name, "engine": key, "voice_id": voice.id,
                        "native_speaker_id": UPSTREAM_VOICE_IDS[voice.id] if key == "kokoro" else voice.id,
                        "sample_rate": spec.sample_rate, "channels": 1,
                        "frames": len(samples), "size": path.stat().st_size, "sha256": _digest(path),
                    })
                    print(f"generated {path.name}", flush=True)
            finally:
                engine.unload()
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if verify(staging):
            return 1
        output_dir.mkdir(parents=True, exist_ok=True)
        for path in staging.iterdir():
            shutil.copyfile(path, output_dir / path.name)
    return 0


def verify(output_dir: Path) -> int:
    """Fully decode each expected clip and verify its recorded output identity."""
    import numpy as np
    import soundfile
    from whisperstream.speech.controller import SAMPLE_TEXT
    from whisperstream.speech.engine_specs import ENGINE_SPECS
    from whisperstream.speech.kokoro_engine import UPSTREAM_VOICE_IDS
    from whisperstream.model_sources import MODEL_SOURCES

    problems = []
    try:
        manifest = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))
        if manifest["schema_version"] != 1 or manifest["text"] != SAMPLE_TEXT or manifest["speed"] != 1.0:
            problems.append("Manifest text, speed or schema differs from the current generator")
        for key, spec in ENGINE_SPECS.items():
            pinned = MODEL_SOURCES[spec.model_id.value]
            if manifest["models"][key] != {field: pinned[field] for field in ("repo", "revision", "files")}:
                problems.append(f"Manifest model input pins differ: {key}")
        records = {clip["file"]: clip for clip in manifest["clips"]}
        if len(records) != len(manifest["clips"]):
            problems.append("Duplicate clip records")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Invalid preview manifest: {exc}", file=sys.stderr)
        return 1
    expected = set()
    for key, spec in ENGINE_SPECS.items():
        for voice in spec.voices:
            path = _clip_path(output_dir, key, voice.id)
            expected.add(path.name)
            try:
                info = soundfile.info(path)
                if info.format != "OGG" or info.subtype != "VORBIS":
                    raise ValueError("expected Ogg/Vorbis encoding")
                data, rate = soundfile.read(path, always_2d=True)
                record = records[path.name]
                if (rate != spec.sample_rate or data.shape[1] != 1 or not len(data)
                        or not np.isfinite(data).all() or not np.any(data)):
                    raise ValueError("invalid decoded audio")
                if (record["engine"] != key or record["voice_id"] != voice.id
                        or record["frames"] != len(data) or record["sample_rate"] != rate
                        or record["native_speaker_id"] != (UPSTREAM_VOICE_IDS[voice.id] if key == "kokoro" else voice.id)
                        or record["channels"] != data.shape[1] or record["sha256"] != _digest(path)
                        or record["size"] != path.stat().st_size):
                    raise ValueError("manifest output mismatch")
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                problems.append(f"{path.name}: {exc}")
    if expected != set(records) or expected != {p.name for p in output_dir.glob("*.ogg")}:
        problems.append("Clip roster differs from the current engines")
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"Verified {len(expected)} decoded clips and manifest hashes")
    return int(bool(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    return verify(args.output_dir) if args.verify else generate(args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
