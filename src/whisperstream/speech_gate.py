"""Local no-speech admission for independent, bounded recognition windows.

The caller serializes access. Only the ONNX session is retained; recurrent state
and audio context belong to one call, never to a preview or dictation session.
Inference contract adapted from Silero's MIT-licensed OnnxWrapper (v6.2.3).
See assets/silero-vad-LICENSE.txt and vendor/silero-vad/README.md.
"""
from __future__ import annotations

import hashlib
import logging
import math
from pathlib import Path

import numpy as np
import onnxruntime as ort


logger = logging.getLogger(__name__)
MODEL_SHA256 = "7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49"
MODEL_PATH = Path(__file__).parent / "assets" / "silero_vad.onnx"
SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64


from .speech_gate_settings import SpeechGateSettings


class SpeechGate:
    """Speech-preserving gate: uncertainty or detector failure permits ASR.

    Use from a worker, not the microphone callback. A manager's existing decode
    lock serializes this instance. Loading is lazy, local, hash-checked, CPU-only.
    """

    def __init__(self, settings: SpeechGateSettings) -> None:
        self.settings = settings
        self.failed = False
        self._session: ort.InferenceSession | None = None

    def _load_session(self) -> ort.InferenceSession:
        if self._session is None:
            model = MODEL_PATH.read_bytes()
            if hashlib.sha256(model).hexdigest() != MODEL_SHA256:
                raise ValueError("Speech detector checksum mismatch")
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            self._session = ort.InferenceSession(
                model, sess_options=options, providers=["CPUExecutionProvider"]
            )
        return self._session

    def close(self) -> None:
        self._session = None

    def has_speech(self, audio: np.ndarray, *, sample_rate: int = SAMPLE_RATE) -> bool:
        if not self.settings.enabled or self.failed or sample_rate != SAMPLE_RATE:
            return True
        waveform = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not waveform.size or not np.any(waveform):
            return False
        try:
            session = self._load_session()
            state = np.zeros((2, 1, 128), dtype=np.float32)
            context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)
            required_samples = math.ceil(self.settings.min_speech_ms * SAMPLE_RATE / 1000)
            speech_samples = 0
            for offset in range(0, waveform.size, FRAME_SAMPLES):
                tail = waveform[offset:offset + FRAME_SAMPLES]
                frame = np.zeros((1, FRAME_SAMPLES), dtype=np.float32)
                frame[0, :tail.size] = tail
                inputs = np.concatenate((context, frame), axis=1)
                probability, state = session.run(None, {
                    "input": inputs,
                    "state": state,
                    "sr": np.array(SAMPLE_RATE, dtype=np.int64),
                })
                score = float(probability[0, 0])
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise ValueError("Invalid speech probability")
                context = inputs[:, -CONTEXT_SAMPLES:]
                speech_samples = (
                    speech_samples + tail.size
                    if score >= self.settings.threshold else 0
                )
                if speech_samples >= required_samples:
                    return True
            return False
        except Exception as exc:
            # Never turn a broken optional detector into lost dictation. Retry
            # only with a new manager so repeated chunks cannot flood logs.
            self.failed = True
            self.close()
            logger.warning("speech_gate unavailable action=bypass error_type=%s", type(exc).__name__)
            return True
