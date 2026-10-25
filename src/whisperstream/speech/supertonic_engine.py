"""sherpa-onnx Supertonic 3 wrapper (agent-speech voice engine).

Supertonic is the **espeak-free** voice engine: it phonemizes with a self-contained
character-level Unicode tokenizer (`unicode_indexer.bin`), so it needs no external
phonemizer at inference. It ships 10 fixed preset speakers
selected by ``sid`` (no voice cloning, no reference audio). Weights are OpenRAIL-M
(redistribution cleared with an EULA use-restriction flow-down).

Provides cheap construction, lazy thread-safe :meth:`load`, and the engine interface
(``load`` / ``sample_rate`` / ``generate(text, *, sid, speed, callback)`` / ``unload``)
that :class:`~whisperstream.speech.playback.PlaybackOwner` drives. Its sherpa-onnx
model config uses ``OfflineTtsSupertonicModelConfig`` with 4 ONNX graphs,
``tts.json``, ``unicode_indexer.bin``, and ``voice.bin``.

This is a plain always-importable module inside the ``whisperstream.speech`` package
(Nuitka-excluded from prod via ``--nofollow-import-to``); callers import it lazily and
only behind the experimental gate.
"""
from __future__ import annotations

import gc
import importlib.util
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# --- sherpa-onnx Windows DLL shim (mirrors parakeet_manager.py) -------------------------
if sys.platform == "win32":
    _spec = importlib.util.find_spec("onnxruntime")
    if _spec and _spec.origin:
        _ort_dir = str(Path(_spec.origin).parent)
        os.add_dll_directory(_ort_dir)
        os.environ["PATH"] = _ort_dir + os.pathsep + os.environ.get("PATH", "")
        _capi_dir = Path(_spec.origin).parent / "capi"
        if _capi_dir.exists():
            os.add_dll_directory(str(_capi_dir))
            os.environ["PATH"] = str(_capi_dir) + os.pathsep + os.environ["PATH"]

from ..model_downloader import ModelId, is_present
from ..model_downloader import model_dir as _model_dir
from .engine_specs import DEFAULT_TTS_THREADS

__all__ = ["SupertonicEngine", "SupertonicModelFiles", "resolve_model_files"]


@dataclass(frozen=True)
class SupertonicModelFiles:
    """Resolved on-disk paths for one Supertonic 3 bundle."""

    duration_predictor: Path
    text_encoder: Path
    vector_estimator: Path
    vocoder: Path
    tts_json: Path
    unicode_indexer: Path
    voice_style: Path


def resolve_model_files(model_dir: Path) -> SupertonicModelFiles:
    """Locate the Supertonic files inside *model_dir*.

    Pure and side-effect free (no sherpa-onnx construction, no file-content reads) so it
    is unit-testable against a plain tmp dir without the real ~120 MB voice model. The
    four ONNX graphs carry a quantization suffix (``.int8.onnx`` today) resolved by glob
    rather than hardcoded. Raises :class:`FileNotFoundError` naming exactly what is
    missing.
    """

    def _one(prefix: str) -> Path:
        hits = sorted(model_dir.glob(f"{prefix}*.onnx"))
        if not hits:
            raise FileNotFoundError(
                f"no {prefix}*.onnx in {model_dir} "
                "(Supertonic voice model not installed or extraction incomplete)"
            )
        return hits[0]

    duration_predictor = _one("duration_predictor")
    text_encoder = _one("text_encoder")
    vector_estimator = _one("vector_estimator")
    vocoder = _one("vocoder")
    tts_json = model_dir / "tts.json"
    unicode_indexer = model_dir / "unicode_indexer.bin"
    voice_style = model_dir / "voice.bin"
    missing = [str(p) for p in (tts_json, unicode_indexer, voice_style) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"Supertonic bundle at {model_dir} is missing required path(s): "
            + ", ".join(missing)
        )
    return SupertonicModelFiles(
        duration_predictor=duration_predictor,
        text_encoder=text_encoder,
        vector_estimator=vector_estimator,
        vocoder=vocoder,
        tts_json=tts_json,
        unicode_indexer=unicode_indexer,
        voice_style=voice_style,
    )


class SupertonicEngine:
    """sherpa-onnx ``OfflineTts`` wrapper for the Supertonic 3 voice.

    Construction is cheap: no sherpa-onnx model load, no disk I/O. :meth:`load` is the
    lazy, idempotent, thread-safe entry point. Public methods are safe to call from any
    thread; internal state is guarded by a single lock.
    """

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        num_threads: int = DEFAULT_TTS_THREADS,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._num_threads = num_threads
        self._lock = threading.Lock()
        self._engine: Optional[object] = None

    @staticmethod
    def is_installed() -> bool:
        """Whether the Supertonic voice model is present on disk."""
        return is_present(ModelId.SUPERTONIC_3_INT8)

    def load(self) -> None:
        """Load the sherpa-onnx engine if not already loaded.

        Idempotent and thread-safe. Raises :class:`FileNotFoundError` (propagated from
        :func:`resolve_model_files`) if the voice model files are missing -- callers
        should check :meth:`is_installed` first for a clean UX path.
        """
        with self._lock:
            if self._engine is not None:
                return
            files = resolve_model_files(_model_dir(ModelId.SUPERTONIC_3_INT8))

            # The reviewed clean downstream runtime is imported only at model
            # load. This keeps construction/default-off capability probes cheap
            # and lets Darwin report the missing reviewed wheel truthfully at
            # the synthesis boundary instead of failing during controller setup.
            import sherpa_onnx  # noqa: PLC0415

            st_config = sherpa_onnx.OfflineTtsSupertonicModelConfig()
            st_config.duration_predictor = str(files.duration_predictor)
            st_config.text_encoder = str(files.text_encoder)
            st_config.vector_estimator = str(files.vector_estimator)
            st_config.vocoder = str(files.vocoder)
            st_config.tts_json = str(files.tts_json)
            st_config.unicode_indexer = str(files.unicode_indexer)
            st_config.voice_style = str(files.voice_style)

            model_config = sherpa_onnx.OfflineTtsModelConfig()
            model_config.supertonic = st_config
            model_config.num_threads = self._num_threads
            model_config.provider = "cpu"

            config = sherpa_onnx.OfflineTtsConfig()
            config.model = model_config

            t0 = time.monotonic()
            engine = sherpa_onnx.OfflineTts(config)
            elapsed = time.monotonic() - t0
            self._engine = engine
            self._logger.info(
                "speech engine loaded voice=supertonic-3-int8 speakers=%d latency=%.2fs",
                engine.num_speakers,
                elapsed,
            )

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz (44100 for Supertonic). Requires :meth:`load`."""
        with self._lock:
            engine = self._engine
        if engine is None:
            raise RuntimeError("SupertonicEngine.load() must be called before sample_rate")
        return engine.sample_rate

    @property
    def num_speakers(self) -> int:
        """Number of preset speakers (10). Requires :meth:`load`."""
        with self._lock:
            engine = self._engine
        if engine is None:
            raise RuntimeError("SupertonicEngine.load() must be called before num_speakers")
        return engine.num_speakers

    def generate(
        self,
        text: str,
        *,
        sid: int = 0,
        speed: float = 1.0,
        callback: Optional[Callable[[np.ndarray, float], int]] = None,
    ) -> np.ndarray:
        """Synthesize *text* into float32 mono samples at :attr:`sample_rate`.

        *callback*, when given, is invoked during generation with
        ``(samples: np.ndarray, progress: float) -> int``; returning non-zero stops
        generation early (used by ``speech/playback.py`` for cancellation).
        """
        # ============================================================================
        # TRANSCRIPT DATA -- DO NOT LOG
        # `text` is Claude Code agent response content (transcript-class data). Never log it or any derived string. Metadata
        # only (char_count, duration_ms) is fine. See text_injector.py's banner.
        # ============================================================================
        with self._lock:
            engine = self._engine
        if engine is None:
            raise RuntimeError("SupertonicEngine.load() must be called before generate()")
        t0 = time.perf_counter()
        audio = engine.generate(text, sid=sid, speed=speed, callback=callback)
        samples = np.asarray(audio.samples, dtype=np.float32)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        self._logger.debug(
            "speech synth done char_count=%d duration_ms=%d sid=%d speed=%.2f",
            len(text),
            elapsed_ms,
            sid,
            speed,
        )
        return samples

    def unload(self) -> None:
        """Release the sherpa-onnx engine. Idempotent; safe to call when unloaded."""
        with self._lock:
            if self._engine is None:
                return
            self._engine = None
        gc.collect()
