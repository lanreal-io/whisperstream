"""Tests for the waveform_peaks helper module.

Covers compute_peaks (bucketed, normalized 0..1 peak extraction) and
wav_bytes_from_float32 (in-memory 16-bit PCM WAV encode), including a
round-trip through main._load_audio_file.
"""
from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np


# --- compute_peaks --------------------------------------------------------


def test_compute_peaks_returns_exactly_buckets_in_range_for_sine():
    """A sine wave yields exactly `buckets` floats, all within [0, 1]."""
    from whisperstream.waveform_peaks import compute_peaks

    sr = 16000
    t = np.linspace(0.0, 1.0, sr, endpoint=False, dtype=np.float32)
    samples = (0.8 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)

    peaks = compute_peaks(samples, buckets=180)

    assert isinstance(peaks, list)
    assert len(peaks) == 180
    assert all(isinstance(p, float) for p in peaks)
    assert all(0.0 <= p <= 1.0 for p in peaks)
    # A non-silent input must produce at least one non-zero peak, and the
    # global max normalization should drive the loudest bucket to ~1.0.
    assert max(peaks) == 1.0


def test_compute_peaks_returns_exactly_buckets_in_range_for_ramp():
    """A monotonic ramp also yields `buckets` floats within [0, 1]."""
    from whisperstream.waveform_peaks import compute_peaks

    samples = np.linspace(0.0, 1.0, 5000, dtype=np.float32)

    peaks = compute_peaks(samples, buckets=200)

    assert len(peaks) == 200
    assert all(0.0 <= p <= 1.0 for p in peaks)
    assert max(peaks) == 1.0


def test_compute_peaks_default_bucket_count():
    """Default bucket count is 180 when not specified."""
    from whisperstream.waveform_peaks import compute_peaks

    samples = np.random.default_rng(0).standard_normal(10000).astype(np.float32)
    peaks = compute_peaks(samples)
    assert len(peaks) == 180


def test_compute_peaks_silent_input_all_zeros():
    """An all-zero (silent) input returns all-zero peaks without dividing by zero."""
    from whisperstream.waveform_peaks import compute_peaks

    samples = np.zeros(16000, dtype=np.float32)
    peaks = compute_peaks(samples, buckets=180)

    assert len(peaks) == 180
    assert all(p == 0.0 for p in peaks)


def test_compute_peaks_short_input_under_bucket_count():
    """A short input (< buckets samples) is padded and still returns `buckets` floats."""
    from whisperstream.waveform_peaks import compute_peaks

    samples = np.array([0.1, -0.5, 0.9, -0.2], dtype=np.float32)
    peaks = compute_peaks(samples, buckets=180)

    assert len(peaks) == 180
    assert all(0.0 <= p <= 1.0 for p in peaks)
    # The loudest sample (0.9) should normalize to 1.0 somewhere.
    assert max(peaks) == 1.0


def test_compute_peaks_empty_input_all_zeros():
    """An empty input returns all-zero peaks without crashing."""
    from whisperstream.waveform_peaks import compute_peaks

    samples = np.array([], dtype=np.float32)
    peaks = compute_peaks(samples, buckets=180)

    assert len(peaks) == 180
    assert all(p == 0.0 for p in peaks)


def test_compute_peaks_negative_amplitudes_use_abs():
    """Negative-only samples produce non-zero peaks (max of abs)."""
    from whisperstream.waveform_peaks import compute_peaks

    samples = np.full(2000, -0.4, dtype=np.float32)
    peaks = compute_peaks(samples, buckets=50)

    assert len(peaks) == 50
    assert all(p == 1.0 for p in peaks)


# --- wav_bytes_from_float32 + round-trip ----------------------------------


def test_wav_bytes_from_float32_returns_bytes():
    """wav_bytes_from_float32 returns a non-empty bytes object."""
    from whisperstream.waveform_peaks import wav_bytes_from_float32

    samples = np.linspace(-0.5, 0.5, 8000, dtype=np.float32)
    data = wav_bytes_from_float32(samples, sample_rate=16000)

    assert isinstance(data, (bytes, bytearray))
    assert len(data) > 44  # at least a WAV header + some samples


def test_wav_bytes_from_float32_has_valid_wav_header():
    """The produced bytes parse as a 16-bit mono WAV at the given sample rate."""
    import io

    from whisperstream.waveform_peaks import wav_bytes_from_float32

    samples = np.linspace(-0.5, 0.5, 8000, dtype=np.float32)
    data = wav_bytes_from_float32(samples, sample_rate=16000)

    with wave.open(io.BytesIO(data), "r") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2  # 16-bit PCM
        assert wf.getframerate() == 16000
        assert wf.getnframes() == len(samples)


def test_wav_bytes_roundtrip_through_load_audio_file(tmp_path: Path):
    """Bytes written to a temp .wav read back via _load_audio_file as a comparable waveform."""
    from whisperstream.main import _load_audio_file
    from whisperstream.waveform_peaks import wav_bytes_from_float32

    sr = 16000
    t = np.linspace(0.0, 0.5, sr // 2, endpoint=False, dtype=np.float32)
    original = (0.6 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)

    data = wav_bytes_from_float32(original, sample_rate=sr)

    wav_path = tmp_path / "roundtrip.wav"
    wav_path.write_bytes(data)

    loaded, loaded_sr = _load_audio_file(str(wav_path), logging.getLogger("test"))

    assert loaded_sr == sr
    assert loaded.dtype == np.float32
    assert loaded.shape == original.shape
    # 16-bit quantization introduces small error; tolerance ~1 LSB / 32768.
    assert np.max(np.abs(loaded - original)) < 2e-4
