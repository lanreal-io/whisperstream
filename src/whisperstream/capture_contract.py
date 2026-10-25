"""Platform-neutral microphone device and capture contract.

PortAudio indices, CoreAudio identifiers, WASAPI/MME details, stream objects,
and native errors remain adapter-private. Shared orchestration sees only opaque
logical device IDs, normalized audio, generations, and content-free outcomes.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Protocol, runtime_checkable

from whisperstream.stt_contracts import Float32Audio, normalize_stt_audio


CAPTURE_SCHEMA_VERSION = 1
CAPTURE_SAMPLE_RATE = 16_000
CAPTURE_CHANNELS = 1
CAPTURE_DTYPE = "float32"
CAPTURE_START_TIMEOUT_SECONDS = 5.0
CAPTURE_STOP_TIMEOUT_SECONDS = 5.0

_capture_owner_epoch_lock = threading.Lock()
_capture_owner_epochs = itertools.count(1)


def allocate_capture_owner_epoch() -> int:
    """Return one process-unique adapter-instance epoch."""
    with _capture_owner_epoch_lock:
        return next(_capture_owner_epochs)


class CaptureSelectionResolution(StrEnum):
    SYSTEM_DEFAULT = "system-default"
    SELECTED = "selected"
    FALLBACK_MISSING = "fallback-missing"
    FALLBACK_OPEN_FAILED = "fallback-open-failed"


class CaptureCompletionStatus(StrEnum):
    STOPPED = "stopped"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CaptureFailure(StrEnum):
    NO_INPUT_DEVICE = "no-input-device"
    OPEN_FAILED = "open-failed"
    DEVICE_LOST = "device-lost"
    STREAM_FAILED = "stream-failed"
    TIMEOUT = "timeout"
    STALE_GENERATION = "stale-generation"
    CLEANUP_FAILED = "cleanup-failed"
    NATIVE_ERROR = "native-error"


@dataclass(frozen=True, slots=True)
class CaptureFormat:
    sample_rate: int = CAPTURE_SAMPLE_RATE
    channels: int = CAPTURE_CHANNELS
    dtype: str = CAPTURE_DTYPE

    def __post_init__(self) -> None:
        if self.sample_rate != CAPTURE_SAMPLE_RATE:
            raise ValueError("capture sample_rate must be 16000")
        if self.channels != CAPTURE_CHANNELS:
            raise ValueError("capture channels must be mono")
        if self.dtype != CAPTURE_DTYPE:
            raise ValueError("capture dtype must be float32")


@dataclass(frozen=True, slots=True)
class CaptureDevice:
    logical_id: str = field(repr=False)
    display_name: str
    max_input_channels: int
    default_sample_rate: float
    is_default: bool

    def __post_init__(self) -> None:
        if not isinstance(self.logical_id, str) or not self.logical_id.strip():
            raise ValueError("logical_id must be non-empty")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ValueError("display_name must be non-empty")
        if (
            isinstance(self.max_input_channels, bool)
            or not isinstance(self.max_input_channels, int)
            or self.max_input_channels < 1
        ):
            raise ValueError("max_input_channels must be positive")
        if (
            isinstance(self.default_sample_rate, bool)
            or not isinstance(self.default_sample_rate, (int, float))
            or self.default_sample_rate <= 0
        ):
            raise ValueError("default_sample_rate must be positive")
        if type(self.is_default) is not bool:  # noqa: E721
            raise ValueError("is_default must be boolean")


@dataclass(frozen=True, slots=True)
class CaptureInventory:
    devices: tuple[CaptureDevice, ...]
    schema_version: int = CAPTURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_SCHEMA_VERSION:
            raise ValueError("unsupported capture schema version")
        if not all(isinstance(item, CaptureDevice) for item in self.devices):
            raise ValueError("devices must contain CaptureDevice values")
        ids = tuple(item.logical_id for item in self.devices)
        if len(ids) != len(set(ids)):
            raise ValueError("logical device IDs must be unique")
        if sum(1 for item in self.devices if item.is_default) > 1:
            raise ValueError("at most one input device may be the default")


@dataclass(frozen=True, slots=True)
class CaptureSelection:
    """Requested device, retaining the Windows name-based compatibility seam."""

    preferred_id: str | None = field(default=None, repr=False)
    legacy_name: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.preferred_id, "preferred_id"),
            (self.legacy_name, "legacy_name"),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{label} must be non-empty when present")

    @property
    def uses_system_default(self) -> bool:
        return self.preferred_id is None and self.legacy_name is None


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    request_id: str
    generation: int
    selection: CaptureSelection = CaptureSelection()
    audio_format: CaptureFormat = CaptureFormat()

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if not isinstance(self.selection, CaptureSelection):
            raise ValueError("selection must be a CaptureSelection")
        if not isinstance(self.audio_format, CaptureFormat):
            raise ValueError("audio_format must be a CaptureFormat")


@dataclass(frozen=True, slots=True)
class CaptureSession:
    request_id: str
    generation: int
    device_id: str = field(repr=False)
    selection_resolution: CaptureSelectionResolution

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if not isinstance(self.device_id, str) or not self.device_id.strip():
            raise ValueError("device_id must be non-empty")
        if not isinstance(self.selection_resolution, CaptureSelectionResolution):
            raise ValueError("invalid selection resolution")


@dataclass(frozen=True, slots=True)
class CaptureTerminalEvent:
    """One content-free terminal request from an exact capture generation."""

    request_id: str
    generation: int
    failure: CaptureFailure
    owner_epoch: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if not isinstance(self.failure, CaptureFailure):
            raise ValueError("failure must be a CaptureFailure")
        if (
            isinstance(self.owner_epoch, bool)
            or not isinstance(self.owner_epoch, int)
            or self.owner_epoch < 1
        ):
            raise ValueError("owner_epoch must be a positive integer")


@dataclass(frozen=True, slots=True)
class CaptureStartOutcome:
    session: CaptureSession | None = None
    failure: CaptureFailure | None = None

    def __post_init__(self) -> None:
        if (self.session is None) == (self.failure is None):
            raise ValueError("capture start requires exactly one session or failure")
        if self.session is not None and not isinstance(self.session, CaptureSession):
            raise ValueError("session must be a CaptureSession")
        if self.failure is not None and not isinstance(self.failure, CaptureFailure):
            raise ValueError("failure must be a CaptureFailure")


@dataclass(frozen=True, slots=True)
class CaptureCompletion:
    request_id: str
    generation: int
    status: CaptureCompletionStatus
    audio: Float32Audio | None = field(default=None, repr=False)
    failure: CaptureFailure | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if not isinstance(self.status, CaptureCompletionStatus):
            raise ValueError("invalid capture completion status")
        failed = self.status is CaptureCompletionStatus.FAILED
        if failed != (self.failure is not None):
            raise ValueError("failure must match failed completion status")
        if self.status is CaptureCompletionStatus.CANCELLED and self.audio is not None:
            raise ValueError("cancelled capture must discard audio")
        if self.status is CaptureCompletionStatus.STOPPED and self.audio is None:
            raise ValueError("stopped capture must return normalized audio")
        if self.audio is not None:
            normalized = normalize_stt_audio(
                self.audio,
                sample_rate=CAPTURE_SAMPLE_RATE,
            )
            object.__setattr__(self, "audio", normalized)


AudioLevelCallback = Callable[[float], None]
AudioChunkCallback = Callable[[Float32Audio], None]
CaptureTerminalCallback = Callable[[CaptureTerminalEvent], None]


@runtime_checkable
class CaptureAdapter(Protocol):
    """One generation-bound input stream owned by a platform adapter."""

    def enumerate_input_devices(self) -> CaptureInventory:
        """Return logical input devices without persisting native indices."""

    def start_capture(
        self,
        request: CaptureRequest,
        *,
        on_level: AudioLevelCallback | None = None,
        on_chunk: AudioChunkCallback | None = None,
    ) -> CaptureStartOutcome:
        """Start one bounded generation, with selected-device fallback explicit."""

    def stop_capture(self, session: CaptureSession) -> CaptureCompletion:
        """Stop the exact active generation and return flat float32 audio."""

    def cancel_capture(self, session: CaptureSession) -> CaptureCompletion:
        """Stop the exact active generation and discard its audio."""

    def close(self) -> bool:
        """Idempotently release the adapter; report complete bounded teardown."""


__all__ = [
    "CAPTURE_CHANNELS",
    "CAPTURE_DTYPE",
    "CAPTURE_SAMPLE_RATE",
    "CAPTURE_SCHEMA_VERSION",
    "CAPTURE_START_TIMEOUT_SECONDS",
    "CAPTURE_STOP_TIMEOUT_SECONDS",
    "AudioChunkCallback",
    "AudioLevelCallback",
    "CaptureAdapter",
    "CaptureCompletion",
    "CaptureCompletionStatus",
    "CaptureDevice",
    "CaptureFailure",
    "CaptureFormat",
    "CaptureInventory",
    "CaptureRequest",
    "CaptureSelection",
    "CaptureSelectionResolution",
    "CaptureSession",
    "CaptureStartOutcome",
    "CaptureTerminalCallback",
    "CaptureTerminalEvent",
    "allocate_capture_owner_epoch",
]
