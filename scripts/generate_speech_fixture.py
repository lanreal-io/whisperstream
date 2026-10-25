"""Generate the Kokoro speech clips that tests and fixture tools use.

Every spoken test clip in the repository is synthetic, so the tests can be
published and run without anyone's voice. The clips are spoken by the bundled
Kokoro model (Apache-2.0) and resampled to 16 kHz mono 16-bit PCM, the format
the STT engines take.

Two sets are generated:

- ``tests/fixtures/synthetic/speech.wav`` (with ``speech.txt`` and its own
  ``manifest.json``), the shared clip public tests use.
- The clips that replaced real recordings: ``tests/fixtures/test.wav`` and each
  enhancement fixture's ``audio.wav``. ``tests/fixtures/kokoro-clips.json``
  records their spoken text and hashes. The text is what a person would have
  said; an enhancement fixture's ``input.txt`` keeps the transcript the
  enhancement tests are tuned against and is not rewritten.

Install the Kokoro voice model in an isolated WHISPERSTREAM_DATA_DIR first:
    uv run python scripts/generate_speech_fixture.py
    uv run python scripts/generate_speech_fixture.py --verify

Generation is deterministic: a rerun reproduces the same bytes. No microphone,
network request or personal text is used. Commit the audio and both manifests
together.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
OUTPUT_DIR = FIXTURES_DIR / "synthetic"
WAV_NAME = "speech.wav"
TEXT_NAME = "speech.txt"
MANIFEST_NAME = "manifest.json"
TEXT = "Hello, this is a generated test clip for speech recognition."
VOICE = "af_heart"
TARGET_RATE = 16_000
CLIPS_MANIFEST = FIXTURES_DIR / "kokoro-clips.json"

# Replacement clips: path relative to tests/fixtures -> spoken text. Numbers,
# symbols and acronyms are spelled the way a person dictates them, so Kokoro
# reads them predictably.
CLIPS: dict[str, str] = {
    "test.wav": "Hello. I'm speaking and this is a test audio file.",
    "enhancement/chat/02_self_correction_time/audio.wav": "Meet at five, no wait, six at the coffee place.",
    "enhancement/chat/03_slang_preserved/audio.wav": "Lol, that's, that's actually pretty funny, N. G. L.",
    "enhancement/chat/04_contractions_preserved/audio.wav": "I'm gonna grab lunch, you wanna come?",
    "enhancement/chat/05_filler_heavy/audio.wav": "Okay, so the thing is, I can't make it tonight, sorry.",
    "enhancement/chat/06_repeated_word/audio.wav": "Yeah, yeah, yeah, totally agree.",
    "enhancement/chat/07_meta_command_preserved/audio.wav": "Can you send this to John saying I'll be late?",
    "enhancement/chat/08_correction_multiple/audio.wav": "The meeting tomorrow is moved to Tuesday, no, Wednesday at two PM.",
    "enhancement/chat/09_emotion_preserved/audio.wav": "Haha, that's wild, did you see the, the email from Sarah?",
    "enhancement/chat/10_actually_emphasis/audio.wav": "I think it's fine, actually I love it.",
    "enhancement/chat/11_slang_stack/audio.wav": "O M W, B R B, gotta grab my keys.",
    "enhancement/chat/12_short_passthrough/audio.wav": "Sure.",
    "enhancement/chat/13_sorry_correction/audio.wav": "Meeting is on Friday, sorry, Saturday.",
    "enhancement/chat/14_list_numbered_explicit/audio.wav": "Okay, here's what I need to grab. One, milk. Two, eggs. Three, bread.",
    "enhancement/chat/15_list_first_second_third/audio.wav": "First, I'll call the dentist, second, I'll book the flight, third, I'll email mom.",
    "enhancement/chat/16_numbers_not_list/audio.wav": "I have two questions and three concerns about the proposal.",
    "enhancement/chat/17_date_spoken/audio.wav": "Let's do March third at noon.",
    "enhancement/chat/18_time_spoken/audio.wav": "The call is at nine thirty in the morning.",
    "enhancement/chat/19_i_mean_correction/audio.wav": "Send it to John, I mean James.",
    "enhancement/chat/20_scratch_that/audio.wav": "Buy milk, scratch that, buy oat milk.",
    "enhancement/chat/21_make_that/audio.wav": "Two coffees, make that three.",
    "enhancement/chat/22_or_rather/audio.wav": "The client is Acme, or rather, Innotech.",
    "enhancement/code/01_async_refactor/audio.wav": "Refactor the user auth middleware to use async await instead of callbacks.",
    "enhancement/code/02_line_and_filename/audio.wav": "The bug is in line forty two of user service dot py, where we're destructuring response data before checking if response is null.",
    "enhancement/code/03_acronyms_and_status/audio.wav": "Add a JSON schema validator to the REST endpoint and return a four oh four if the user ID is missing.",
    "enhancement/code/04_camel_case/audio.wav": "Camel case user profile equals await fetch user by ID.",
    "enhancement/code/05_snake_case/audio.wav": "Snake case auth middleware should wrap every handler in the API router.",
    "enhancement/code/06_operator_dot_then/audio.wav": "Use await instead of dot then and remove the nested callback.",
    "enhancement/code/07_version_numbers/audio.wav": "Bumping Next JS to fifteen point two and migrating the app router to React nineteen.",
    "enhancement/code/08_cicd_eslint/audio.wav": "The CI slash CD pipeline fails on the lint step because E S lint can't find the config.",
    "enhancement/code/09_npm_scoped_package/audio.wav": "N P M install at TanStack slash React Query and add the provider to app dot TSX.",
    "enhancement/code/10_pascal_case_class/audio.wav": "Pascal case user service extends base service and overrides the create method.",
    "enhancement/code/11_env_constant/audio.wav": "The API key is stored in dot env and we read it via process dot env dot API key.",
    "enhancement/code/12_no_casing_guess/audio.wav": "The username column has an index on it.",
    "enhancement/code/13_commit_terseness/audio.wav": "Fix null check in auth handler.",
    "enhancement/code/14_that_should_be/audio.wav": "Set timeout to thirty, that should be sixty seconds.",
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resample(samples, source_rate: int):
    import numpy as np

    duration = len(samples) / source_rate
    count = int(round(duration * TARGET_RATE))
    source_times = np.arange(len(samples)) / source_rate
    target_times = np.arange(count) / TARGET_RATE
    return np.interp(target_times, source_times, samples)


def _render(engine, text: str):
    """Speak ``text`` and return peak-normalized 16 kHz PCM16 samples."""
    import numpy as np

    samples = np.asarray(engine.generate(text, sid=VOICE, speed=1.0), dtype=np.float64)
    if samples.ndim != 1 or not np.isfinite(samples).all() or not np.any(samples):
        raise RuntimeError(f"Kokoro returned invalid audio for {text!r}")
    resampled = _resample(samples, engine.sample_rate)
    peak = float(np.max(np.abs(resampled))) or 1.0
    pcm = np.clip(resampled / peak * 0.8, -1.0, 1.0)
    return (pcm * 32767.0).astype("<i2")


def _write_wav(path: Path, pcm16) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(TARGET_RATE)
        writer.writeframes(pcm16.tobytes())


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")


def _wav_shape(path: Path) -> tuple[int, int, int, int]:
    with wave.open(str(path), "rb") as reader:
        return (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getnframes())


def generate(output_dir: Path, fixtures_dir: Path) -> int:
    from whisperstream.speech.engines import make_engine

    engine = make_engine("kokoro")
    engine.load()
    try:
        speech = _render(engine, TEXT)
        clips = {name: _render(engine, text) for name, text in CLIPS.items()}
    finally:
        engine.unload()

    wav_path = output_dir / WAV_NAME
    _write_wav(wav_path, speech)
    text_path = output_dir / TEXT_NAME
    text_path.write_text(TEXT + "\n", encoding="utf-8", newline="\n")
    _write_json(output_dir / MANIFEST_NAME, {
        "schema_version": 1,
        "generator": "scripts/generate_speech_fixture.py",
        "engine": "kokoro",
        "voice": VOICE,
        "text": TEXT,
        "sample_rate": TARGET_RATE,
        "channels": 1,
        "frames": int(len(speech)),
        "files": {WAV_NAME: _digest(wav_path), TEXT_NAME: _digest(text_path)},
    })

    records = []
    for name, pcm16 in clips.items():
        path = fixtures_dir / name
        _write_wav(path, pcm16)
        records.append({"path": name, "text": CLIPS[name], "frames": int(len(pcm16)), "sha256": _digest(path)})
    _write_json(fixtures_dir / CLIPS_MANIFEST.name, {
        "schema_version": 1,
        "generator": "scripts/generate_speech_fixture.py",
        "engine": "kokoro",
        "voice": VOICE,
        "sample_rate": TARGET_RATE,
        "channels": 1,
        "clips": records,
    })
    print(f"generated {1 + len(records)} clips")
    return verify(output_dir, fixtures_dir)


def verify(output_dir: Path, fixtures_dir: Path) -> int:
    problems: list[str] = []
    try:
        manifest = json.loads((output_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        problems += [
            name for name, digest in manifest["files"].items()
            if _digest(output_dir / name) != digest
        ]
        if _wav_shape(output_dir / WAV_NAME) != (1, 2, TARGET_RATE, manifest["frames"]):
            problems.append("WAV format differs from the manifest")
        if manifest["text"] != TEXT:
            problems.append("Manifest text differs from the generator")

        clips = json.loads((fixtures_dir / CLIPS_MANIFEST.name).read_text(encoding="utf-8"))["clips"]
        if {c["path"]: c["text"] for c in clips} != CLIPS:
            problems.append("kokoro-clips.json differs from the generator's clip table")
        for clip in clips:
            path = fixtures_dir / clip["path"]
            if _digest(path) != clip["sha256"]:
                problems.append(clip["path"])
            elif _wav_shape(path) != (1, 2, TARGET_RATE, clip["frames"]):
                problems.append(f"{clip['path']}: WAV format differs from the manifest")
    except (OSError, ValueError, KeyError, wave.Error) as exc:
        print(f"Invalid speech fixture: {exc}", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"Speech fixture mismatch: {problem}", file=sys.stderr)
    if not problems:
        print("Verified the Kokoro speech fixtures")
    return int(bool(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="synthetic clip directory")
    parser.add_argument("--fixtures-dir", type=Path, default=FIXTURES_DIR, help="root for the replacement clips")
    args = parser.parse_args()
    if args.verify:
        return verify(args.output_dir, args.fixtures_dir)
    return generate(args.output_dir, args.fixtures_dir)


if __name__ == "__main__":
    raise SystemExit(main())
