"""TTS synthesis, playback, interruption, and output ownership contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Protocol, runtime_checkable


TTS_PLAYBACK_SCHEMA_VERSION = 1


class SpeechEngineChoice(StrEnum):
    AUTOMATIC = "automatic"
    KOKORO = "kokoro"
    SUPERTONIC = "supertonic"


class SpeechStage(StrEnum):
    QUEUED = "queued"
    SYNTHESIZING = "synthesizing"
    BUFFERED = "buffered"
    PLAYING = "playing"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class SpeechTerminalReason(StrEnum):
    COMPLETED = "completed"
    EXPLICIT_STOP = "explicit-stop"
    SUPERSEDED = "superseded"
    PREPLAY_FAILURE = "preplay-failure"
    UNAVAILABLE_OUTPUT = "unavailable-output"
    SYNTHESIS_FAILURE = "synthesis-failure"
    PLAYBACK_FAILURE = "playback-failure"
    SHUTDOWN = "shutdown"


class SpeechFailure(StrEnum):
    ENGINE_UNAVAILABLE = "engine-unavailable"
    MODEL_UNAVAILABLE = "model-unavailable"
    INVALID_REQUEST = "invalid-request"
    SYNTHESIS_FAILED = "synthesis-failed"
    OUTPUT_UNAVAILABLE = "output-unavailable"
    OUTPUT_CHANGED = "output-changed"
    PLAYBACK_FAILED = "playback-failed"
    STALE_GENERATION = "stale-generation"
    CLEANUP_FAILED = "cleanup-failed"
    NATIVE_ERROR = "native-error"


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    completion_id: str
    generation: int
    engine: SpeechEngineChoice
    voice_token: str
    speed: float
    volume: float
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.completion_id.strip() or not self.voice_token.strip():
            raise ValueError("completion_id and voice_token must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text must be a non-empty string")
        if self.speed <= 0 or not 0 <= self.volume <= 1:
            raise ValueError("speech speed or volume is invalid")


@dataclass(frozen=True, slots=True)
class SpeechBuffer:
    generation: int
    ordinal: int
    sample_rate_hz: int
    channels: int
    pcm_f32le: bytes = field(repr=False)
    final: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal < 0
        ):
            raise ValueError("ordinal must be nonnegative")
        if self.sample_rate_hz < 1 or self.channels < 1:
            raise ValueError("audio format must be positive")
        if not isinstance(self.pcm_f32le, bytes) or not self.pcm_f32le:
            raise ValueError("pcm_f32le must contain audio bytes")
        if len(self.pcm_f32le) % (4 * self.channels):
            raise ValueError("pcm_f32le must contain complete float32 frames")


@dataclass(frozen=True, slots=True)
class SpeechReceipt:
    completion_id: str
    generation: int
    stage: SpeechStage
    terminal_reason: SpeechTerminalReason | None = None
    failure: SpeechFailure | None = None
    buffers_completed: int = 0
    schema_version: int = TTS_PLAYBACK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.completion_id.strip():
            raise ValueError("completion_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if self.schema_version != TTS_PLAYBACK_SCHEMA_VERSION:
            raise ValueError("unsupported TTS schema version")
        terminal = self.stage in {
            SpeechStage.COMPLETED,
            SpeechStage.INTERRUPTED,
            SpeechStage.FAILED,
        }
        if terminal != (self.terminal_reason is not None):
            raise ValueError("terminal stage and reason must match")
        if (self.stage is SpeechStage.FAILED) != (self.failure is not None):
            raise ValueError("failure must match failed stage")
        if self.stage is SpeechStage.COMPLETED and (
            self.terminal_reason is not SpeechTerminalReason.COMPLETED
        ):
            raise ValueError("completed speech requires completed reason")
        if isinstance(self.buffers_completed, bool) or self.buffers_completed < 0:
            raise ValueError("buffers_completed must be nonnegative")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "completion_id": self.completion_id,
            "generation": self.generation,
            "stage": self.stage.value,
            "terminal_reason": (
                self.terminal_reason.value if self.terminal_reason else None
            ),
            "failure": self.failure.value if self.failure else None,
            "buffers_completed": self.buffers_completed,
        }


@runtime_checkable
class SpeechSynthesisAdapter(Protocol):
    def synthesize(self, request: SpeechRequest) -> Iterable[SpeechBuffer]:
        """Yield ordered logical PCM buffers without owning an output device."""

    def close(self) -> None:
        """Idempotently release model and worker resources."""


@runtime_checkable
class SpeechPlaybackAdapter(Protocol):
    """Own current-output resolution and one newest-wins playback generation."""

    def play(
        self,
        request: SpeechRequest,
        buffers: Iterable[SpeechBuffer],
    ) -> SpeechReceipt:
        """Resolve the current output for this utterance and play once."""

    def interrupt(
        self,
        generation: int,
        reason: SpeechTerminalReason,
    ) -> None:
        """Abort matching output without blocking the caller or joining workers."""

    def close(self) -> None:
        """Idempotently abort output and release streams, timers, and workers."""


__all__ = [
    "TTS_PLAYBACK_SCHEMA_VERSION",
    "SpeechBuffer",
    "SpeechEngineChoice",
    "SpeechFailure",
    "SpeechPlaybackAdapter",
    "SpeechReceipt",
    "SpeechRequest",
    "SpeechStage",
    "SpeechSynthesisAdapter",
    "SpeechTerminalReason",
]
