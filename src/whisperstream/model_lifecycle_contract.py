"""Native-free contract for downloadable model assets.

The shared layer names logical assets and operation generations only. Platform
paths, file handles, archive implementations, and volume identities belong to
the concrete storage adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, runtime_checkable


class ModelFamily(str, Enum):
    STT = "stt"
    ENHANCEMENT = "enhancement"
    SPEECH = "speech"


class ModelOperation(str, Enum):
    ACQUIRE = "acquire"
    RELOCATE = "relocate"
    DELETE = "delete"
    VERIFY = "verify"
    CANCEL = "cancel"


class ModelState(str, Enum):
    ABSENT = "absent"
    PARTIAL = "partial"
    READY = "ready"
    BUSY = "busy"
    CORRUPT = "corrupt"
    UNAVAILABLE = "unavailable"


class ModelFailure(str, Enum):
    NONE = "none"
    UNSUPPORTED = "unsupported"
    BUSY = "busy"
    CANCELLED = "cancelled"
    NO_SPACE = "no-space"
    NETWORK = "network"
    INTEGRITY = "integrity"
    SOURCE_UNAVAILABLE = "source-unavailable"
    DESTINATION_UNAVAILABLE = "destination-unavailable"
    PERMISSION = "permission"
    IO = "io"
    CLEANUP = "cleanup"


@dataclass(frozen=True)
class ModelAssetRef:
    asset_id: str
    family: ModelFamily

    def __post_init__(self) -> None:
        if not self.asset_id.strip():
            raise ValueError("asset_id must be nonempty")


@dataclass(frozen=True)
class ModelSnapshot:
    asset: ModelAssetRef
    state: ModelState
    location_token: str | None = None
    verified: bool = False
    operation_generation: int | None = None

    def __post_init__(self) -> None:
        if self.operation_generation is not None and self.operation_generation < 1:
            raise ValueError("operation_generation must be positive")
        if self.state is ModelState.READY and not self.location_token:
            raise ValueError("ready assets require an opaque location token")


@dataclass(frozen=True)
class ModelProgress:
    generation: int
    asset: ModelAssetRef
    completed_bytes: int
    total_bytes: int | None

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.completed_bytes < 0:
            raise ValueError("completed_bytes cannot be negative")
        if self.total_bytes is not None:
            if self.total_bytes < 0 or self.completed_bytes > self.total_bytes:
                raise ValueError("invalid progress bounds")


@dataclass(frozen=True)
class ModelOperationRequest:
    generation: int
    operation: ModelOperation
    asset: ModelAssetRef
    destination_token: str | None = None

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.operation is ModelOperation.RELOCATE and not self.destination_token:
            raise ValueError("relocation requires an opaque destination token")
        if self.operation is not ModelOperation.RELOCATE and self.destination_token:
            raise ValueError("only relocation accepts a destination token")


@dataclass(frozen=True)
class ModelOperationResult:
    generation: int
    operation: ModelOperation
    asset: ModelAssetRef
    state: ModelState
    failure: ModelFailure = ModelFailure.NONE
    changed: bool = False

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.failure is ModelFailure.NONE and self.state is ModelState.UNAVAILABLE:
            raise ValueError("unavailable result requires an explicit failure")


ProgressCallback = Callable[[ModelProgress], None]


@runtime_checkable
class ModelLifecycleAdapter(Protocol):
    """One adapter owns storage paths and every operation for its assets."""

    def snapshot(self, asset: ModelAssetRef) -> ModelSnapshot: ...

    def execute(
        self,
        request: ModelOperationRequest,
        on_progress: ProgressCallback | None = None,
    ) -> ModelOperationResult: ...

    def cancel(self, generation: int) -> ModelOperationResult: ...


@runtime_checkable
class ModelLifecycleFactory(Protocol):
    """Select one platform storage owner without exposing its native state."""

    def create(self) -> ModelLifecycleAdapter: ...


MODEL_LIFECYCLE_SCHEMA_VERSION = 1
