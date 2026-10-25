"""Native-free lifecycle contract for local transcript enhancement runtimes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable


LOCAL_LLM_RUNTIME_SCHEMA_VERSION = 1


class LocalRuntimeKind(StrEnum):
    MANAGED_LLAMA = "managed-llama"
    EXTERNAL_OLLAMA = "external-ollama"


class RuntimeOwnership(StrEnum):
    APPLICATION = "application"
    EXTERNAL = "external"


class RuntimeAcceleration(StrEnum):
    CPU = "cpu"
    VULKAN = "vulkan"
    METAL = "metal"


class RuntimeStage(StrEnum):
    STOPPED = "stopped"
    SPAWNING = "spawning"
    LOADING = "loading"
    READY = "ready"
    INFERENCING = "inferencing"
    IDLE = "idle"
    STOPPING = "stopping"
    FAILED = "failed"


class RuntimeFailure(StrEnum):
    UNAVAILABLE = "unavailable"
    MODEL_MISSING = "model-missing"
    SPAWN_FAILED = "spawn-failed"
    START_TIMEOUT = "start-timeout"
    HEALTH_FAILED = "health-failed"
    INFERENCE_FAILED = "inference-failed"
    INFERENCE_TIMEOUT = "inference-timeout"
    INVALID_OUTPUT = "invalid-output"
    CANCELLED = "cancelled"
    STALE_GENERATION = "stale-generation"
    UNSUPPORTED_ACCELERATION = "unsupported-acceleration"
    CLEANUP_FAILED = "cleanup-failed"
    NATIVE_ERROR = "native-error"


@dataclass(frozen=True, slots=True)
class LocalModelRef:
    catalog_id: str
    storage_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.catalog_id.strip() or not self.storage_token.strip():
            raise ValueError("catalog_id and storage_token must be non-empty")


@dataclass(frozen=True, slots=True)
class RuntimeStartRequest:
    generation: int
    kind: LocalRuntimeKind
    ownership: RuntimeOwnership
    model: LocalModelRef
    acceleration: RuntimeAcceleration
    start_timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if self.start_timeout_seconds <= 0:
            raise ValueError("start timeout must be positive")
        if self.kind is LocalRuntimeKind.MANAGED_LLAMA:
            if self.ownership is not RuntimeOwnership.APPLICATION:
                raise ValueError("managed llama runtime must be application-owned")
        elif self.ownership is not RuntimeOwnership.EXTERNAL:
            raise ValueError("external Ollama runtime must remain externally owned")


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    generation: int
    kind: LocalRuntimeKind
    ownership: RuntimeOwnership
    stage: RuntimeStage
    healthy: bool
    failure: RuntimeFailure | None = None
    schema_version: int = LOCAL_LLM_RUNTIME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if self.schema_version != LOCAL_LLM_RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported runtime schema version")
        failed = self.stage is RuntimeStage.FAILED
        if failed != (self.failure is not None):
            raise ValueError("failure must match failed runtime stage")
        if self.stage is RuntimeStage.READY and not self.healthy:
            raise ValueError("ready runtime must be healthy")
        if self.ownership is RuntimeOwnership.EXTERNAL and (
            self.kind is not LocalRuntimeKind.EXTERNAL_OLLAMA
        ):
            raise ValueError("external ownership is reserved for Ollama")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "kind": self.kind.value,
            "ownership": self.ownership.value,
            "stage": self.stage.value,
            "healthy": self.healthy,
            "failure": self.failure.value if self.failure else None,
        }


@dataclass(frozen=True, slots=True)
class LocalInferenceRequest:
    request_id: str
    generation: int
    timeout_seconds: float
    raw_text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if not isinstance(self.raw_text, str):
            raise ValueError("raw_text must be a string")


@dataclass(frozen=True, slots=True)
class LocalInferenceResult:
    request_id: str
    generation: int
    use_raw: bool
    enhanced_text: str | None = field(default=None, repr=False)
    failure: RuntimeFailure | None = None
    schema_version: int = LOCAL_LLM_RUNTIME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        if self.schema_version != LOCAL_LLM_RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported runtime schema version")
        if self.failure is None:
            if self.use_raw or not isinstance(self.enhanced_text, str):
                raise ValueError("success requires one enhanced result")
        elif not self.use_raw or self.enhanced_text is not None:
            raise ValueError("failure requires raw fallback and no enhanced text")


@runtime_checkable
class LocalRuntimeAdapter(Protocol):
    """Own one logical runtime generation; native resources stay private."""

    async def start(self, request: RuntimeStartRequest) -> RuntimeSnapshot:
        """Start or attach to the requested generation."""

    async def warm(self, generation: int) -> RuntimeSnapshot:
        """Prove model readiness before enhanced output is authoritative."""

    async def infer(
        self,
        request: LocalInferenceRequest,
        transport: Callable[[LocalInferenceRequest], Awaitable[str]],
    ) -> LocalInferenceResult:
        """Return exactly one enhancement result or raw fallback."""

    async def cancel(
        self, request_id: str, generation: int
    ) -> LocalInferenceResult:
        """Cancel only the matching in-flight request."""

    async def stop(self, generation: int) -> RuntimeSnapshot:
        """Stop only an application-owned matching generation."""

    async def close(self) -> None:
        """Idempotently stop owned runtimes and timers."""


@runtime_checkable
class LocalRuntimeFactory(Protocol):
    def create(self, kind: LocalRuntimeKind) -> LocalRuntimeAdapter:
        """Create the selected platform adapter without starting it."""


__all__ = [
    "LOCAL_LLM_RUNTIME_SCHEMA_VERSION",
    "LocalInferenceRequest",
    "LocalInferenceResult",
    "LocalModelRef",
    "LocalRuntimeAdapter",
    "LocalRuntimeFactory",
    "LocalRuntimeKind",
    "RuntimeAcceleration",
    "RuntimeFailure",
    "RuntimeOwnership",
    "RuntimeSnapshot",
    "RuntimeStage",
    "RuntimeStartRequest",
]
