"""Direct ONNX Runtime wrapper for the eSpeak-free Kokoro 82M voice.

The optional model directory owns every model and language-data file. This
module adds no dependency on kokoro-onnx, Misaki, Torch, Transformers, spaCy,
eSpeak, or phonemizer. Construction is cheap; :meth:`KokoroEngine.load`
performs all model I/O and ONNX Runtime imports.
"""

from __future__ import annotations

import gc
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from .availability import legacy_kokoro_dir, legacy_kokoro_cached
from .engine_specs import DEFAULT_TTS_THREADS, KOKORO_SPEC
from .kokoro_frontend import KokoroEnglishFrontend, KokoroFrontendData

__all__ = [
    "KOKORO_SAMPLE_RATE",
    "MAX_PHONEME_IDS",
    "KokoroEngine",
    "KokoroModelFiles",
    "resolve_model_files",
]

KOKORO_SAMPLE_RATE = 24_000
MAX_PHONEME_IDS = 509
_STYLE_DIMENSIONS = 256
_MIN_SPEED = 0.5
_MAX_SPEED = 2.0
_BREAK_CHARS = frozenset(" .,!?:;")


@dataclass(frozen=True)
class KokoroModelFiles:
    """Exact required inference paths inside the optional Kokoro bundle."""

    model: Path
    voices: Path
    config: Path


def resolve_model_files(model_dir: Path) -> KokoroModelFiles:
    """Resolve the exact model-relative Kokoro files and reject partial installs."""

    files = KokoroModelFiles(
        model=model_dir / "kokoro-v1.0.onnx",
        voices=model_dir / "voices-v1.0.npz",
        config=model_dir / "config.json",
    )
    missing = [
        path.name
        for path in (files.model, files.voices, files.config)
        if not path.is_file()
    ]
    if missing:
        raise FileNotFoundError(
            f"Kokoro bundle at {model_dir} is missing required path(s): "
            + ", ".join(missing)
        )
    return files


def _load_onnxruntime() -> Any:
    """Import ONNX Runtime only when the optional engine is loaded."""

    import onnxruntime as ort

    return ort


def _load_vocabulary(path: Path) -> dict[str, int]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        raw_vocab = raw["vocab"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Kokoro vocabulary config: {exc}") from exc
    if not isinstance(raw_vocab, dict) or not raw_vocab:
        raise ValueError("invalid Kokoro vocabulary config: vocab is empty")
    vocabulary: dict[str, int] = {}
    for char, token_id in raw_vocab.items():
        if not isinstance(char, str) or len(char) != 1:
            raise ValueError("invalid Kokoro vocabulary config: keys must be characters")
        if (
            not isinstance(token_id, int)
            or isinstance(token_id, bool)
            or token_id <= 0
        ):
            raise ValueError(
                "invalid Kokoro vocabulary config: token IDs must be positive integers"
            )
        vocabulary[char] = token_id
    return vocabulary


def _load_voices(path: Path) -> dict[str, np.ndarray]:
    voices: dict[str, np.ndarray] = {}
    with np.load(path, allow_pickle=False) as archive:
        for voice in KOKORO_SPEC.voices:
            voice_id = str(voice.id)
            if voice_id not in archive.files:
                raise ValueError(f"Kokoro voice archive is missing {voice_id}")
            value = np.asarray(archive[voice_id], dtype=np.float32)
            if value.shape != (MAX_PHONEME_IDS + 1, 1, _STYLE_DIMENSIONS):
                raise ValueError(
                    f"Kokoro voice {voice_id} has invalid shape {value.shape!r}"
                )
            if not np.all(np.isfinite(value)):
                raise ValueError(f"Kokoro voice {voice_id} contains non-finite values")
            voices[voice_id] = value.copy()
    return voices


def _split_phonemes(
    phonemes: str,
    vocabulary: dict[str, int],
) -> list[tuple[list[int], int]]:
    unknown = sorted({char for char in phonemes if char not in vocabulary})
    if unknown:
        rendered = " ".join(repr(char) for char in unknown)
        raise ValueError(f"unknown phoneme character(s): {rendered}")
    if not phonemes:
        raise ValueError("Kokoro frontend returned no phonemes")

    chunks: list[tuple[list[int], int]] = []
    offset = 0
    total = len(phonemes)
    while offset < total:
        end = min(offset + MAX_PHONEME_IDS, total)
        if end < total:
            # Prefer a nearby natural boundary without producing tiny chunks.
            minimum = offset + (MAX_PHONEME_IDS // 2)
            boundary = next(
                (
                    index + 1
                    for index in range(end - 1, minimum - 1, -1)
                    if phonemes[index] in _BREAK_CHARS
                ),
                end,
            )
            end = boundary
        text_chunk = phonemes[offset:end]
        chunks.append(([vocabulary[char] for char in text_chunk], end))
        offset = end
    return chunks


def _bundle_fingerprint(model_root: Path) -> tuple | None:
    """Identity of the installed bundle: resolved root plus top-level file stats.

    Content-free and cheap (a dozen ``stat`` calls). A replaced, removed or
    relocated bundle produces a different value, which invalidates a retained
    frontend. ``None`` when the bundle directory cannot be read.
    """

    try:
        root = model_root.resolve()
        entries = []
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry.is_file():
                stat = entry.stat()
                entries.append((entry.name, stat.st_size, stat.st_mtime_ns))
        return (str(root), tuple(entries))
    except OSError:
        return None


# What idle unload keeps resident for the next load:
#   "none": nothing (pre-fix behavior; arenas grow with every reload)
#   "full": the whole frontend, including its small G2P ONNX session
#   "data": only the immutable lexicon/tagger/vocabulary; G2P is rebuilt per load
FRONTEND_RETENTION_MODES = ("none", "full", "data")
DEFAULT_FRONTEND_RETENTION = "full"


class KokoroEngine:
    """Lazy, thread-safe direct ORT Kokoro engine.

    Idle :meth:`unload` releases the synthesis session, vocabulary and voice
    arrays. Rebuilding the frontend's large lexicon/tagger dictionaries on every
    reload pinned CPython arenas (type-attribute cache fragmentation), so the
    engine keeps them resident according to ``frontend_retention``. Retained
    frontend state is dropped by :meth:`dispose` (shutdown) and whenever the
    bundle fingerprint changes (replacement, removal or model-root change).
    """

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        num_threads: int = DEFAULT_TTS_THREADS,
        frontend_retention: str = DEFAULT_FRONTEND_RETENTION,
    ) -> None:
        from .availability import require_engine_available
        require_engine_available("kokoro")
        if isinstance(num_threads, bool) or num_threads < 1:
            raise ValueError("num_threads must be at least 1")
        if frontend_retention not in FRONTEND_RETENTION_MODES:
            raise ValueError("unknown frontend_retention")
        self._retention = frontend_retention
        self._logger = logger or logging.getLogger(__name__)
        self._num_threads = int(num_threads)
        self._lock = threading.Lock()
        self._session: Any | None = None
        self._frontend: KokoroEnglishFrontend | None = None
        self._frontend_data: KokoroFrontendData | None = None
        self._frontend_fingerprint: tuple | None = None
        self._vocabulary: dict[str, int] = {}
        self._voices: dict[str, np.ndarray] = {}

    @staticmethod
    def is_installed() -> bool:
        """Whether the verified Kokoro bundle is present on disk."""

        return legacy_kokoro_cached()

    def load(self) -> None:
        """Load the optional bundle once, leaving no partial state on failure."""

        with self._lock:
            if self._session is not None:
                return
            model_root = legacy_kokoro_dir()
            fingerprint = _bundle_fingerprint(model_root)
            if self._has_retained_locked() and (
                fingerprint is None or fingerprint != self._frontend_fingerprint
            ):
                self._release_frontend_locked(reason="bundle_changed")
            files = resolve_model_files(model_root)
            vocabulary = _load_vocabulary(files.config)
            voices = _load_voices(files.voices)
            reused = self._has_retained_locked()
            data = self._frontend_data
            if self._retention == "full" and self._frontend is not None:
                frontend = self._frontend
            elif self._retention == "data":
                if data is None:
                    data = KokoroFrontendData.from_model_dir(model_root)
                frontend = KokoroEnglishFrontend(model_root, data=data)
            else:
                frontend = KokoroEnglishFrontend(model_root)

            ort = _load_onnxruntime()
            options = ort.SessionOptions()
            options.intra_op_num_threads = self._num_threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            started = time.monotonic()
            session = ort.InferenceSession(
                str(files.model),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            elapsed = time.monotonic() - started

            self._session = session
            self._frontend = frontend
            self._frontend_data = data if self._retention == "data" else None
            self._frontend_fingerprint = fingerprint
            self._vocabulary = vocabulary
            self._voices = voices
            self._logger.info(
                "speech engine loaded voice=kokoro-82m-fp32 speakers=%d latency=%.2fs frontend=%s",
                len(voices),
                elapsed,
                "reused" if reused else "built",
            )

    @property
    def sample_rate(self) -> int:
        """Kokoro output sample rate. Requires :meth:`load`."""

        with self._lock:
            loaded = self._session is not None
        if not loaded:
            raise RuntimeError("KokoroEngine.load() must be called before sample_rate")
        return KOKORO_SAMPLE_RATE

    @property
    def num_speakers(self) -> int:
        """Number of curated preset voices. Requires :meth:`load`."""

        with self._lock:
            if self._session is None:
                raise RuntimeError(
                    "KokoroEngine.load() must be called before num_speakers"
                )
            return len(self._voices)

    @property
    def voice_ids(self) -> tuple[str, ...]:
        """Curated string voice identities in stable product order."""

        with self._lock:
            if self._session is None:
                raise RuntimeError(
                    "KokoroEngine.load() must be called before voice_ids"
                )
            installed = self._voices
            return tuple(
                str(voice.id)
                for voice in KOKORO_SPEC.voices
                if str(voice.id) in installed
            )

    def generate(
        self,
        text: str,
        *,
        sid: str = "af_heart",
        speed: float = 1.0,
        callback: Optional[Callable[[np.ndarray, float], int]] = None,
    ) -> np.ndarray:
        """Synthesize *text* into finite float32 mono samples at 24 kHz."""

        # ============================================================================
        # TRANSCRIPT DATA -- DO NOT LOG
        # `text` is agent response content. Never log it or derived phonemes.
        # Content-free metadata (char count, duration, voice ID) is permitted.
        # ============================================================================
        with self._lock:
            session = self._session
            frontend = self._frontend
            vocabulary = self._vocabulary
            voices = self._voices
        if session is None or frontend is None:
            raise RuntimeError("KokoroEngine.load() must be called before generate()")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        if not isinstance(sid, str) or sid not in voices:
            raise ValueError(f"unknown Kokoro voice: {sid!r}")
        if (
            isinstance(speed, bool)
            or not isinstance(speed, (int, float))
            or not math.isfinite(float(speed))
            or not _MIN_SPEED <= float(speed) <= _MAX_SPEED
        ):
            raise ValueError(
                f"speed must be finite and between {_MIN_SPEED} and {_MAX_SPEED}"
            )

        started = time.perf_counter()
        # The frontend preserves source whitespace. Kokoro's vocabulary has a
        # normal space token, not distinct newline/tab tokens.
        phonemes = "".join(
            " " if char.isspace() else char
            for char in frontend.phonemize(text)
        )
        chunks = _split_phonemes(phonemes, vocabulary)
        total_ids = len(phonemes)
        output: list[np.ndarray] = []
        for token_ids, completed_ids in chunks:
            token_count = len(token_ids)
            if not 1 <= token_count <= MAX_PHONEME_IDS:
                raise AssertionError("Kokoro phoneme chunk exceeded model bounds")
            style = np.asarray(voices[sid][token_count], dtype=np.float32)
            inputs = {
                "tokens": np.asarray([[0, *token_ids, 0]], dtype=np.int64),
                "style": style,
                "speed": np.asarray([float(speed)], dtype=np.float32),
            }
            result = session.run(None, inputs)
            if not result:
                raise RuntimeError("Kokoro returned empty output")
            samples = np.asarray(result[0], dtype=np.float32).reshape(-1)
            if samples.size == 0:
                raise RuntimeError("Kokoro returned empty audio")
            if not np.all(np.isfinite(samples)):
                raise RuntimeError("Kokoro returned non-finite audio")
            output.append(samples)
            if callback is not None and callback(
                samples,
                completed_ids / total_ids,
            ):
                break

        combined = np.concatenate(output).astype(np.float32, copy=False)
        if combined.size == 0 or not np.all(np.isfinite(combined)):
            raise RuntimeError("Kokoro returned invalid audio")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._logger.debug(
            "speech synth done engine=kokoro char_count=%d duration_ms=%d "
            "voice=%s speed=%.2f chunks=%d",
            len(text),
            elapsed_ms,
            sid,
            float(speed),
            len(output),
        )
        return combined

    def unload(self) -> None:
        """Idle release: drop the session, vocabulary and voice arrays.

        Retained frontend state (per ``frontend_retention``) stays resident for
        the next load unless the bundle it was built from has changed or
        disappeared. Safe to repeat.
        """

        with self._lock:
            if self._session is None:
                return
            self._session = None
            self._vocabulary = {}
            self._voices = {}
            if self._retention != "full":
                # "data" releases the G2P session with the frontend wrapper.
                self._frontend = None
            if self._retention == "none":
                self._frontend_data = None
                self._frontend_fingerprint = None
            if (
                self._has_retained_locked()
                and _bundle_fingerprint(legacy_kokoro_dir())
                != self._frontend_fingerprint
            ):
                self._release_frontend_locked(reason="bundle_changed")
        gc.collect()

    def _has_retained_locked(self) -> bool:
        return self._frontend is not None or self._frontend_data is not None

    def dispose(self) -> None:
        """Final release (shutdown): idle release plus the retained frontend."""

        with self._lock:
            self._session = None
            self._vocabulary = {}
            self._voices = {}
            self._release_frontend_locked(reason="dispose")
        gc.collect()

    def _release_frontend_locked(self, *, reason: str) -> None:
        if not self._has_retained_locked():
            return
        self._frontend = None
        self._frontend_data = None
        self._frontend_fingerprint = None
        self._logger.info(
            "speech engine frontend released voice=kokoro-82m-fp32 reason=%s",
            reason,
        )
