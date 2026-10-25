"""Generate a zero-preroll numbered-speech fixture with the Kokoro voice model.

Each word is synthesized in a separate Kokoro call, onset/offset trimmed, and
then concatenated with a caller-selected silence gap. The manifest records the
trim points so the fixture can be audited without relying on listening alone.
No real voice is involved, so the clips can be published.

Install the Kokoro voice model in an isolated WHISPERSTREAM_DATA_DIR first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


WORDS = ("one", "two", "three", "four", "five")
TARGET_SAMPLE_RATE = 16_000


def _synthesize_word(engine, word: str, path: Path, voice: str) -> None:
    """Synthesize one word in its own Kokoro call and keep the raw output."""
    samples = np.asarray(engine.generate(word, sid=voice, speed=1.0), dtype=np.float32)
    if samples.ndim != 1 or not np.isfinite(samples).all() or not np.any(samples):
        raise RuntimeError(f"Kokoro produced invalid audio for {word!r}")
    sf.write(path, samples, int(engine.sample_rate), subtype="PCM_16")


def _to_mono_float(audio: np.ndarray) -> np.ndarray:
    values = np.asarray(audio, dtype=np.float32)
    if values.ndim == 2:
        values = values.mean(axis=1)
    return values.reshape(-1)


def _active_bounds(audio: np.ndarray, *, relative_db: float) -> tuple[int, int, float]:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak <= 0:
        raise RuntimeError("synthesis produced silent audio")
    threshold = max(peak * math.pow(10.0, relative_db / 20.0), 2.0 / 32768.0)
    active = np.flatnonzero(np.abs(audio) >= threshold)
    if active.size == 0:
        raise RuntimeError("no speech onset found")
    return int(active[0]), int(active[-1]) + 1, threshold


def _resample(audio: np.ndarray, source_rate: int) -> np.ndarray:
    if source_rate == TARGET_SAMPLE_RATE:
        return audio.astype(np.float32, copy=False)
    divisor = math.gcd(source_rate, TARGET_SAMPLE_RATE)
    return resample_poly(
        audio,
        TARGET_SAMPLE_RATE // divisor,
        source_rate // divisor,
    ).astype(np.float32, copy=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def generate_fixture(
    output_dir: Path,
    *,
    gap_ms: int,
    voice: str,
    relative_db: float,
) -> None:
    from whisperstream.speech.engines import make_engine

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    words_dir = output_dir / "words"
    raw_dir.mkdir(exist_ok=True)
    words_dir.mkdir(exist_ok=True)

    segments: list[np.ndarray] = []
    records: list[dict[str, object]] = []
    engine = make_engine("kokoro")
    engine.load()
    try:
        for word in WORDS:
            _synthesize_word(engine, word, raw_dir / f"{word}.wav", voice)
    finally:
        engine.unload()
    for word in WORDS:
        raw_path = raw_dir / f"{word}.wav"
        raw_audio, source_rate = sf.read(raw_path, dtype="float32", always_2d=False)
        mono = _to_mono_float(raw_audio)
        start, end, threshold = _active_bounds(mono, relative_db=relative_db)
        trimmed = _resample(mono[start:end], int(source_rate))
        trimmed_path = words_dir / f"{word}.wav"
        sf.write(trimmed_path, trimmed, TARGET_SAMPLE_RATE, subtype="PCM_16")
        segments.append(trimmed)
        records.append(
            {
                "word": word,
                "source_sample_rate": int(source_rate),
                "source_samples": int(mono.size),
                "trim_start_sample": start,
                "trim_end_sample": end,
                "threshold": threshold,
                "trimmed_samples_16khz": int(trimmed.size),
                "sha256": _sha256(trimmed_path),
            }
        )

    gap = np.zeros(round(TARGET_SAMPLE_RATE * gap_ms / 1000.0), dtype=np.float32)
    combined_parts: list[np.ndarray] = []
    for index, segment in enumerate(segments):
        if index:
            combined_parts.append(gap)
        combined_parts.append(segment)
    combined = np.concatenate(combined_parts)
    combined_path = output_dir / "numbers.wav"
    sf.write(combined_path, combined, TARGET_SAMPLE_RATE, subtype="PCM_16")

    manifest = {
        "generator": Path(__file__).name,
        "engine": "kokoro",
        "voice": voice,
        "words": list(WORDS),
        "expected": " ".join(WORDS),
        "sample_rate": TARGET_SAMPLE_RATE,
        "gap_ms": gap_ms,
        "relative_threshold_db": relative_db,
        "leading_silence_samples": 0,
        "combined_samples": int(combined.size),
        "combined_sha256": _sha256(combined_path),
        "segments": records,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/fixtures/startup-capture"),
    )
    parser.add_argument("--gap-ms", type=int, required=True)
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--relative-db", type=float, default=-42.0)
    args = parser.parse_args()
    if args.gap_ms < 0:
        parser.error("--gap-ms must be non-negative")
    generate_fixture(
        args.output,
        gap_ms=args.gap_ms,
        voice=args.voice,
        relative_db=args.relative_db,
    )


if __name__ == "__main__":
    main()
