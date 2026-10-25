"""Portable parent-facing speech-to-text contracts.

The authoritative contract is deliberately narrower than any one native
engine. Optional preview, hotword, and language-hint behavior is exposed
through explicit capability accessors so callers never have to infer support
from a method that silently does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from numbers import Integral
from typing import Optional, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray


Float32Audio = NDArray[np.float32]


class InvalidSttAudio(ValueError):
    """The parent supplied audio that is outside the shared STT contract."""


class SttLifecycleState(StrEnum):
    """Content-free lifecycle state returned by :meth:`SttEngine.health`."""

    READY = "ready"
    IDLE = "idle"
    UNAVAILABLE = "unavailable"
    SHUTDOWN = "shutdown"


class SttPreviewMetadata(StrEnum):
    """Strongest preview metadata an engine promises to return safely."""

    TEXT = "text"
    TIMED_TOKENS = "timed_tokens"


class SttHotwordUpdateMode(StrEnum):
    """How a supported engine applies a hotword update."""

    RELOAD = "reload"


@dataclass(frozen=True, slots=True)
class DetailedTranscriptionResult:
    """Immutable fresh decode result shared by every native adapter.

    ``text`` is always usable. Timed-token metadata is all-or-nothing for
    ``tokens`` and ``timestamps``; optional parallel arrays may remain absent.
    Consumers must continue to validate metadata before using it for visual
    agreement because a native engine can still return malformed values.
    """

    text: str
    tokens: tuple[str, ...] | None = None
    timestamps: tuple[float, ...] | None = None
    durations: tuple[float, ...] | None = None
    ys_log_probs: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class SttPreviewSupport:
    """Declared behavior of the optional preview capability."""

    metadata: SttPreviewMetadata
    nonblocking_admission: bool


@dataclass(frozen=True, slots=True)
class SttHotwordSupport:
    """Declared behavior of the optional hotword capability."""

    update_mode: SttHotwordUpdateMode


@dataclass(frozen=True, slots=True)
class SttLanguageHintSupport:
    """Declared behavior of the optional mutable language-hint capability."""

    dynamic_updates: bool


@dataclass(frozen=True, slots=True)
class SttCapabilities:
    """Authoritative capability data for non-universal STT behavior.

    ``None`` means unsupported. A non-``None`` entry must have a matching
    operational capability object on the engine.

    ``final_alignment`` is the parent adapter's audited declaration that its
    pinned decoder can supply native alignment on fresh final decodes. It is
    independent of preview support and is not a worker handshake field. True
    does not guarantee metadata on every result; consumers must check each
    result, and malformed or unavailable alignment remains absent.
    """

    preview: SttPreviewSupport | None = None
    hotwords: SttHotwordSupport | None = None
    language_hint: SttLanguageHintSupport | None = None
    final_alignment: bool = False


@dataclass(frozen=True, slots=True)
class SttHealth:
    """Content-free health returned by an STT engine."""

    engine_id: str
    state: SttLifecycleState
    loaded: bool


@runtime_checkable
class SttPreviewCapability(Protocol):
    """Optional, best-effort preview recognition."""

    def try_transcribe_preview(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = 16_000,
    ) -> DetailedTranscriptionResult | None:
        """Return a preview result, or ``None`` when admission is busy."""


@runtime_checkable
class SttHotwordCapability(Protocol):
    """Optional hotword update behavior."""

    def update_hotwords(self, hotwords: list[str], score: float) -> None:
        """Apply the complete hotword set using the advertised update mode."""


@runtime_checkable
class SttLanguageHintCapability(Protocol):
    """Optional mutable language-hint behavior."""

    def set_language_hint(self, name: Optional[str]) -> None:
        """Set or clear a backend-supported language hint."""


@runtime_checkable
class SttEngine(Protocol):
    """Authoritative lifecycle and final-transcription contract."""

    engine_id: str
    capabilities: SttCapabilities

    @property
    def preview_capability(self) -> SttPreviewCapability | None:
        """Return the preview implementation only when advertised."""

    @property
    def hotword_capability(self) -> SttHotwordCapability | None:
        """Return the hotword implementation only when advertised."""

    @property
    def language_hint_capability(self) -> SttLanguageHintCapability | None:
        """Return the language-hint implementation only when advertised."""

    def load(self) -> None:
        """Idempotently make the engine ready for transcription."""

    def unload(self) -> None:
        """Idempotently release the loaded model while keeping the engine usable."""

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = 16_000,
        language: Optional[str] = None,
    ) -> str:
        """Return the authoritative final transcript for a complete waveform."""

    def transcribe_chunk(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = 16_000,
    ) -> str:
        """Return one authoritative parent-planned chunk transcript."""

    def transcribe_chunk_detailed(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = 16_000,
    ) -> DetailedTranscriptionResult:
        """Decode an authoritative chunk with native alignment when available.

        This must run a fresh final decode, independently of preview admission
        and preview caches. Absent alignment remains absent.
        """

    def health(self) -> SttHealth:
        """Return content-free lifecycle health."""

    def shutdown(self) -> None:
        """Idempotently stop the engine and release owned resources."""


def normalize_stt_audio(audio: np.ndarray, *, sample_rate: int) -> Float32Audio:
    """Validate and normalize audio at the portable STT boundary.

    Whisperstream's authoritative representation is a flat float32 waveform
    with a positive integer sample rate. One- and two-dimensional numeric
    arrays remain accepted because the existing Windows managers historically
    flattened two-dimensional inputs. Scalars, arrays with more than two axes,
    complex/non-numeric data, and invalid sample rates are rejected before a
    native engine sees them.
    """

    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, Integral)
        or sample_rate <= 0
    ):
        raise InvalidSttAudio("sample_rate must be a positive integer")
    if not isinstance(audio, np.ndarray):
        raise InvalidSttAudio("audio must be a numpy array")
    if audio.ndim not in (1, 2):
        raise InvalidSttAudio("audio must have one or two dimensions")
    if not np.issubdtype(audio.dtype, np.number) or np.issubdtype(
        audio.dtype, np.complexfloating
    ):
        raise InvalidSttAudio("audio dtype must contain real numeric samples")
    return audio.reshape(-1).astype(np.float32, copy=False)


__all__ = [
    "DetailedTranscriptionResult",
    "Float32Audio",
    "InvalidSttAudio",
    "SttCapabilities",
    "SttEngine",
    "SttHealth",
    "SttHotwordCapability",
    "SttHotwordSupport",
    "SttHotwordUpdateMode",
    "SttLanguageHintCapability",
    "SttLanguageHintSupport",
    "SttLifecycleState",
    "SttPreviewCapability",
    "SttPreviewMetadata",
    "SttPreviewSupport",
    "normalize_stt_audio",
]
