"""Native-free recording chime and output-volume transaction contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class AudioPolicyMode(str, Enum):
    OFF = "off"
    REDUCE = "reduce"
    MUTE = "mute"


class AudioPolicyStage(str, Enum):
    IDLE = "idle"
    START_CHIME = "start-chime"
    CAPTURE = "capture"
    MUTATED = "mutated"
    RESTORED = "restored"
    STOP_CHIME = "stop-chime"
    CLOSED = "closed"


class ChimeKind(str, Enum):
    START = "start"
    STOP = "stop"


class AudioPolicyFailure(str, Enum):
    NONE = "none"
    UNSUPPORTED = "unsupported"
    ENDPOINT_UNAVAILABLE = "endpoint-unavailable"
    OBSERVATION_FAILED = "observation-failed"
    MUTATION_FAILED = "mutation-failed"
    RESTORE_FAILED = "restore-failed"
    STALE_GENERATION = "stale-generation"
    CLOSED = "closed"


@dataclass(frozen=True)
class OutputEndpointRef:
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError("endpoint token must be nonempty")


@dataclass(frozen=True)
class AudioOutputSnapshot:
    endpoint: OutputEndpointRef
    scalar_volume: float | None
    muted: bool | None

    def __post_init__(self) -> None:
        if self.scalar_volume is not None and not 0.0 <= self.scalar_volume <= 1.0:
            raise ValueError("scalar_volume must be between zero and one")


@dataclass(frozen=True)
class AudioPolicyTransaction:
    generation: int
    mode: AudioPolicyMode
    snapshot: AudioOutputSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")


@dataclass(frozen=True)
class AudioPolicyResult:
    generation: int
    stage: AudioPolicyStage
    failure: AudioPolicyFailure = AudioPolicyFailure.NONE
    changed: bool = False

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.failure is not AudioPolicyFailure.NONE and self.changed:
            raise ValueError("failed audio operation cannot claim a change")


@runtime_checkable
class RecordingAudioPolicyAdapter(Protocol):
    """Adapter privately owns output-device resolution and native controls."""

    def play_chime(self, generation: int, kind: ChimeKind) -> AudioPolicyResult: ...

    def begin(
        self, generation: int, mode: AudioPolicyMode
    ) -> tuple[AudioPolicyTransaction | None, AudioPolicyResult]: ...

    def restore(self, transaction: AudioPolicyTransaction) -> AudioPolicyResult: ...

    def close(self, generation: int) -> AudioPolicyResult: ...


@runtime_checkable
class RecordingAudioPolicyFactory(Protocol):
    """Select an output-policy owner without resolving an endpoint eagerly."""

    def create(self) -> RecordingAudioPolicyAdapter: ...


AUDIO_REDUCE_RATIO = 0.20
# Compatibility export retained for the frozen Windows conformance suite.
WINDOWS_REDUCE_TARGET_SCALAR = AUDIO_REDUCE_RATIO
AUDIO_POLICY_SCHEMA_VERSION = 1
