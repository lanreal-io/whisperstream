"""Waveform peak precompute + in-memory WAV encoding helpers.

These helpers turn a float32 mono @ 16 kHz recording (as produced by the mic
path's `_last_waveform` and by `_load_audio_file`) into:

  * `compute_peaks` - a small, JSON-serializable list of normalized 0..1 peaks
    for rendering a scrubbable waveform without re-reading the audio.
  * `wav_bytes_from_float32` - 16-bit PCM mono WAV bytes suitable for handing to
    `TranscriptStore.save(audio_bytes=...)`.

This module never receives, returns, or logs transcript text, so the DO-NOT-LOG
banner is informational only here; keep it if transcript text is ever threaded
through.
"""
from __future__ import annotations

import io
import wave

import numpy as np


def compute_peaks(samples: np.ndarray, buckets: int = 180) -> list[float]:
    """Reduce a mono float32 waveform to `buckets` normalized 0..1 peaks.

    The waveform is split into `buckets` equal segments (the tail is padded with
    zeros so the reshape is exact). Each bucket's value is the maximum absolute
    sample in that segment, normalized by the global maximum so the loudest
    bucket is 1.0. A silent or empty input yields all zeros (no divide-by-zero).

    Returns a plain Python ``list[float]`` so it is directly JSON-serializable.
    """
    if buckets <= 0:
        return []

    flat = np.asarray(samples, dtype=np.float32).reshape(-1)
    magnitudes = np.abs(flat)

    if magnitudes.size == 0:
        return [0.0] * buckets

    # Pad the tail with zeros so the sample count is a clean multiple of
    # `buckets`, then take the per-bucket max-abs.
    pad = (-magnitudes.size) % buckets
    if pad:
        magnitudes = np.concatenate([magnitudes, np.zeros(pad, dtype=np.float32)])

    bucketed = magnitudes.reshape(buckets, -1).max(axis=1)

    global_max = float(bucketed.max())
    if global_max <= 0.0:
        return [0.0] * buckets

    normalized = bucketed / global_max
    return [float(v) for v in normalized]


def wav_bytes_from_float32(samples: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Encode a mono float32 waveform as 16-bit PCM mono WAV bytes in memory.

    Float samples are clipped to [-1, 1] and scaled to int16. The result parses
    back through the stdlib `wave` module (and `_load_audio_file`'s native WAV
    path) as a comparable waveform.
    """
    flat = np.asarray(samples, dtype=np.float32).reshape(-1)
    clipped = np.clip(flat, -1.0, 1.0)
    int16 = np.round(clipped * 32767.0).astype(np.int16)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(int(sample_rate))
        wf.writeframes(int16.tobytes())

    return buffer.getvalue()
