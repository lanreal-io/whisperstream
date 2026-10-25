"""Isolated official sherpa-onnx Kokoro with stable voice/callback contracts.

Existing eSpeak-free caches remain usable through the legacy adapter. New
models use upstream's English lexicon/eSpeak frontend, without a custom export.
"""
from __future__ import annotations

import gc
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from ..model_downloader import DOWNLOAD_REGISTRY, ModelId, is_present, model_dir
from .engine_specs import DEFAULT_TTS_THREADS, KOKORO_SPEC, SpeechInputError

KOKORO_SAMPLE_RATE = 24_000
# Verified against the pinned graph's speaker2id metadata, not positional UI order.
UPSTREAM_VOICE_IDS = {
    "af_heart": 3, "af_bella": 2, "af_nicole": 6, "af_aoede": 1,
    "af_kore": 5, "af_sarah": 9, "am_fenrir": 14,
    "am_michael": 16, "am_puck": 18,
}


@dataclass(frozen=True)
class KokoroModelFiles:
    model: Path
    voices: Path
    tokens: Path
    lexicon: Path
    data_dir: Path


def resolve_model_files(directory: Path) -> KokoroModelFiles:
    missing = [name for name in DOWNLOAD_REGISTRY[ModelId.KOKORO_82M_FP32].files
               if not (directory / name).is_file() or (directory / name).stat().st_size == 0]
    if missing:
        raise FileNotFoundError("Kokoro bundle is incomplete: " + ", ".join(missing))
    return KokoroModelFiles(directory / "model.onnx", directory / "voices.bin",
                            directory / "tokens.txt", directory / "lexicon-us-en.txt",
                            directory / "espeak-ng-data")


class KokoroEngine:
    """Lazy Kokoro adapter; playback retains generation and output ownership."""

    def __init__(self, *, logger: logging.Logger | None = None,
                 num_threads: int = DEFAULT_TTS_THREADS) -> None:
        from .availability import require_engine_available
        require_engine_available("kokoro")
        if isinstance(num_threads, bool) or num_threads < 1:
            raise ValueError("num_threads must be at least 1")
        self._logger = logger or logging.getLogger(__name__)
        self._num_threads = int(num_threads)
        self._lock = threading.Lock()
        self._engine = None
        self._legacy = None
        # Idle unload keeps the in-process legacy adapter so its immutable
        # frontend data stays resident; rebuilding it per load fragments
        # CPython arenas. The isolated upstream worker frees all of its memory
        # when its process exits, so it needs no retention.
        self._retained_legacy = None

    @staticmethod
    def is_installed() -> bool:
        from .availability import legacy_kokoro_cached
        return is_present(ModelId.KOKORO_82M_FP32) or legacy_kokoro_cached()

    def load(self) -> None:
        with self._lock:
            if self._engine is not None and not self._engine.alive:
                self._engine.close()
                self._engine = None
            if self._engine is not None or self._legacy is not None:
                return
            from .availability import legacy_kokoro_cached
            if not is_present(ModelId.KOKORO_82M_FP32) and legacy_kokoro_cached():
                legacy = self._retained_legacy
                if legacy is None:
                    from .legacy_kokoro_engine import KokoroEngine as LegacyKokoroEngine
                    legacy = LegacyKokoroEngine(logger=self._logger, num_threads=self._num_threads)
                legacy.load()
                self._legacy = legacy
                self._retained_legacy = legacy
                return
            # The upstream bundle superseded the legacy cache; drop its frontend.
            self._release_retained_legacy_locked()
            files = resolve_model_files(model_dir(ModelId.KOKORO_82M_FP32))
            from .kokoro_process import KokoroProcess
            started = time.monotonic()
            engine = KokoroProcess(files.model.parent, self._num_threads)
            if engine.sample_rate != KOKORO_SAMPLE_RATE or engine.num_speakers != 54:
                engine.close()
                raise RuntimeError("Kokoro runtime does not match the pinned graph")
            self._engine = engine
            self._logger.info("speech engine loaded voice=kokoro-upstream speakers=%d latency=%.2fs",
                              len(UPSTREAM_VOICE_IDS), time.monotonic() - started)

    def _loaded(self):
        with self._lock:
            engine, legacy = self._engine, self._legacy
        if engine is None and legacy is None:
            raise RuntimeError("KokoroEngine.load() must be called first")
        return engine, legacy

    @property
    def sample_rate(self) -> int:
        self._loaded()
        return KOKORO_SAMPLE_RATE

    @property
    def num_speakers(self) -> int:
        self._loaded()
        return len(UPSTREAM_VOICE_IDS)

    @property
    def voice_ids(self) -> tuple[str, ...]:
        self._loaded()
        return tuple(str(voice.id) for voice in KOKORO_SPEC.voices)

    def generate(self, text: str, *, sid: str = "af_heart", speed: float = 1.0,
                 callback: Callable[[np.ndarray, float], int] | None = None) -> np.ndarray:
        # Agent response content and derived phonemes must never be logged.
        engine, legacy = self._loaded()
        if not isinstance(text, str) or not text.strip():
            raise SpeechInputError("text must be a non-empty string")
        if not isinstance(sid, str) or sid not in UPSTREAM_VOICE_IDS:
            raise ValueError("unknown Kokoro voice")
        if (isinstance(speed, bool) or not isinstance(speed, (int, float))
                or not math.isfinite(float(speed)) or not 0.5 <= float(speed) <= 2.0):
            raise ValueError("speed must be finite and between 0.5 and 2.0")
        if legacy is not None:
            return legacy.generate(text, sid=sid, speed=speed, callback=callback)
        # Reject content without English text before unnecessary model work.
        # Other native error output is discarded within the owned child.
        if not re.search(r"[A-Za-z0-9]", text):
            raise SpeechInputError("Kokoro needs pronounceable English text")
        stopped = False
        def native_callback(samples, progress):
            nonlocal stopped
            frames = np.asarray(samples, dtype=np.float32).reshape(-1).copy()
            if frames.size == 0 or not np.isfinite(frames).all():
                raise RuntimeError("Kokoro returned invalid audio")
            stopped = callback is not None and bool(callback(frames, progress))
            # Native 1.13.8 uses 1=continue, 0=stop, despite its Python docstring.
            return 0 if stopped else 1
        started = time.perf_counter()
        audio = engine.generate(text, sid=UPSTREAM_VOICE_IDS[sid], speed=float(speed),
                                callback=native_callback)
        frames = np.asarray(audio.samples, dtype=np.float32).reshape(-1)
        if not np.isfinite(frames).all() or (frames.size == 0 and not stopped):
            raise RuntimeError("Kokoro returned invalid audio")
        self._logger.debug("speech synth done engine=kokoro char_count=%d duration_ms=%d voice=%s speed=%.2f",
                           len(text), int((time.perf_counter() - started) * 1000), sid, float(speed))
        return frames

    def unload(self) -> None:
        """Idle release. A legacy adapter keeps its frontend resident."""
        with self._lock:
            engine, legacy = self._engine, self._legacy
            self._engine = self._legacy = None
        if engine is not None:
            engine.close()
        if legacy is not None:
            legacy.unload()
        gc.collect()

    def dispose(self) -> None:
        """Final release (shutdown), including retained legacy frontend data."""
        with self._lock:
            engine = self._engine
            self._engine = self._legacy = None
            retained, self._retained_legacy = self._retained_legacy, None
        if engine is not None:
            engine.close()
        if retained is not None:
            retained.dispose()
        gc.collect()

    def _release_retained_legacy_locked(self) -> None:
        retained, self._retained_legacy = self._retained_legacy, None
        if retained is not None:
            retained.dispose()
