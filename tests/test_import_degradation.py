"""Tests for the no-ffmpeg import-degradation contract in `_load_audio_file`.

The dev/CI box typically HAS ffmpeg on PATH, so the missing-ffmpeg failure
cannot be observed directly. These tests SIMULATE ffmpeg absence (the way a
missing binary actually manifests: `subprocess.run` raises `FileNotFoundError`)
and assert that `_load_audio_file` surfaces a friendly, human-readable
`RuntimeError` for a non-WAV path, while the native WAV path never touches
ffmpeg at all.

This proves the contract Task 3.5's `/api/import` endpoint relies on to turn a
missing-ffmpeg failure into a per-file "convert to WAV" reason instead of a raw
exception or traceback.

These tests touch only audio bytes and file paths, never transcript text, so the
DO-NOT-LOG banner is informational here.
"""
from __future__ import annotations

import logging
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from whisperstream.waveform_peaks import wav_bytes_from_float32


# --- Test 1: no-ffmpeg path raises a friendly RuntimeError -----------------


def test_non_wav_without_ffmpeg_raises_friendly_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-.wav file with ffmpeg absent raises a friendly, actionable RuntimeError.

    `_load_audio_file` does `import subprocess` then `subprocess.run(...)`; a
    missing ffmpeg binary surfaces as `FileNotFoundError` from that call. We
    monkeypatch `subprocess.run` in the module namespace the function resolves
    `run` through (the stdlib `subprocess` module) to raise `FileNotFoundError`,
    then assert the resulting message names converting to WAV / installing
    ffmpeg and reads as human-readable guidance (no raw traceback string).
    """
    from whisperstream.main import _load_audio_file

    # A real file must exist (the function checks existence before branching),
    # but its bytes are irrelevant because we intercept the ffmpeg call.
    mp3_path = tmp_path / "foo.mp3"
    mp3_path.write_bytes(b"not really an mp3, ffmpeg would handle it")

    def _fake_run(*_args, **_kwargs):
        # Exactly how a missing ffmpeg binary manifests from subprocess.run.
        raise FileNotFoundError(2, "No such file or directory", "ffmpeg")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        _load_audio_file(str(mp3_path), logging.getLogger("test"))

    message = str(excinfo.value)
    lowered = message.lower()

    # The message must name ffmpeg and tell the user how to recover: install
    # ffmpeg or convert to a .wav file (matches main.py's wording).
    assert "ffmpeg" in lowered
    assert "install" in lowered
    assert ".wav" in lowered

    # Human-readable guidance, not a raw traceback / exception dump.
    assert "Traceback" not in message
    assert "FileNotFoundError" not in message
    assert "\n  File \"" not in message  # no stack-frame lines


# --- Test 2: native WAV path returns audio without invoking ffmpeg ---------


def test_wav_path_returns_float32_without_invoking_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real .wav loads via the native `wave` path and never calls subprocess.run.

    We make `subprocess.run` raise if it is ever invoked, then load a genuine
    16-bit PCM mono WAV. A clean return proves the .wav branch bypasses ffmpeg
    entirely (so WAV import works even when ffmpeg is absent).
    """
    from whisperstream.main import _load_audio_file

    # Generate a tiny real WAV (16 kHz, mono, 16-bit) via the stdlib helper.
    sr = 16000
    t = np.linspace(0.0, 0.25, sr // 4, endpoint=False, dtype=np.float32)
    original = (0.5 * np.sin(2 * np.pi * 330.0 * t)).astype(np.float32)

    wav_path = tmp_path / "clip.wav"
    wav_path.write_bytes(wav_bytes_from_float32(original, sample_rate=sr))

    # Sanity-check the generated file really is a 16-bit mono WAV.
    with wave.open(str(wav_path), "r") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == sr

    called = {"ran": False}

    def _fail_if_called(*_args, **_kwargs):
        called["ran"] = True
        raise AssertionError("ffmpeg (subprocess.run) must not be called for a .wav file")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)

    loaded, loaded_sr = _load_audio_file(str(wav_path), logging.getLogger("test"))

    assert called["ran"] is False  # ffmpeg was never invoked
    assert loaded_sr == sr
    assert isinstance(loaded, np.ndarray)
    assert loaded.dtype == np.float32
    assert loaded.shape == original.shape
    # 16-bit quantization tolerance (~1 LSB / 32768).
    assert np.max(np.abs(loaded - original)) < 2e-4
