"""Serializable Phase 5 capability inventory for settings and diagnostics.

The report describes implementation support only. Live devices, permissions,
processes, endpoints, hooks, and model availability stay in their adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


ADVANCED_CAPABILITY_SCHEMA_VERSION = 1


class AdvancedCapabilityState(StrEnum):
    AVAILABLE = "available"
    DEVELOPMENT_ONLY = "development-only"
    NOT_IMPLEMENTED = "not-implemented"
    UNAVAILABLE = "unavailable"
    EXTERNAL_DEPENDENCY = "external-dependency"


class AdvancedCapabilityPresentation(StrEnum):
    ENABLE = "enable"
    DISABLE = "disable"
    EXPLAIN = "explain"


@dataclass(frozen=True, slots=True)
class AdvancedCapability:
    capability_id: str
    state: AdvancedCapabilityState
    reason_code: str
    presentation: AdvancedCapabilityPresentation

    def __post_init__(self) -> None:
        if not self.capability_id.strip() or not self.reason_code.strip():
            raise ValueError("capability_id and reason_code must be non-empty")
        if self.state is AdvancedCapabilityState.AVAILABLE:
            if self.presentation is not AdvancedCapabilityPresentation.ENABLE:
                raise ValueError("available capability must be enabled")
        elif self.presentation is AdvancedCapabilityPresentation.ENABLE:
            raise ValueError("absent capability cannot present an enabled action")

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.capability_id,
            "state": self.state.value,
            "reason": self.reason_code,
            "presentation": self.presentation.value,
        }


@dataclass(frozen=True, slots=True)
class AdvancedCapabilityReport:
    platform: str
    capabilities: tuple[AdvancedCapability, ...]
    schema_version: int = ADVANCED_CAPABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        ids = [item.capability_id for item in self.capabilities]
        if not self.platform.strip():
            raise ValueError("platform must be non-empty")
        if self.schema_version != ADVANCED_CAPABILITY_SCHEMA_VERSION:
            raise ValueError("unsupported capability schema version")
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("capabilities must be unique and sorted")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "capabilities": [item.as_dict() for item in self.capabilities],
        }


_CAPABILITY_IDS = (
    "agent-hook-lifecycle",
    "agent-hud-integration",
    "app-identity",
    "app-path-browse",
    "browser-page-identity",
    "finetune-registry-evaluation",
    "local-llm-runtime",
    "tts-synthesis-playback",
)


def _presentation(state: AdvancedCapabilityState) -> AdvancedCapabilityPresentation:
    if state is AdvancedCapabilityState.AVAILABLE:
        return AdvancedCapabilityPresentation.ENABLE
    if state in {
        AdvancedCapabilityState.DEVELOPMENT_ONLY,
        AdvancedCapabilityState.EXTERNAL_DEPENDENCY,
    }:
        return AdvancedCapabilityPresentation.EXPLAIN
    return AdvancedCapabilityPresentation.DISABLE


def create_advanced_capability_report(platform_name: str) -> AdvancedCapabilityReport:
    """Return static implementation support without importing native owners."""

    normalized = platform_name.strip().lower()
    if normalized == "windows":
        states = {
            capability_id: (AdvancedCapabilityState.AVAILABLE, "implemented")
            for capability_id in _CAPABILITY_IDS
        }
        states["finetune-registry-evaluation"] = (
            AdvancedCapabilityState.DEVELOPMENT_ONLY,
            "dev-tools-only",
        )
        platform_value = "Windows"
    elif normalized in {"darwin", "macos"}:
        states = {
            capability_id: (
                AdvancedCapabilityState.NOT_IMPLEMENTED,
                "phase5-pending",
            )
            for capability_id in _CAPABILITY_IDS
        }
        states["local-llm-runtime"] = (
            AdvancedCapabilityState.AVAILABLE,
            "metal-runtime",
        )
        states["finetune-registry-evaluation"] = (
            AdvancedCapabilityState.DEVELOPMENT_ONLY,
            "dev-tools-only",
        )
        states["app-identity"] = (
            AdvancedCapabilityState.AVAILABLE,
            "appkit-context",
        )
        states["browser-page-identity"] = (
            AdvancedCapabilityState.AVAILABLE,
            "accessibility-host-only",
        )
        states["app-path-browse"] = (
            AdvancedCapabilityState.UNAVAILABLE,
            "raw-path-boundary-unavailable",
        )
        states["tts-synthesis-playback"] = (
            AdvancedCapabilityState.AVAILABLE,
            "darwin-playback",
        )
        states["agent-hook-lifecycle"] = (
            AdvancedCapabilityState.AVAILABLE,
            "claude-cli-plugin",
        )
        states["agent-hud-integration"] = (
            AdvancedCapabilityState.AVAILABLE,
            "native-hud-owner",
        )
        platform_value = "Darwin"
    else:
        states = {
            capability_id: (
                AdvancedCapabilityState.UNAVAILABLE,
                "unsupported-platform",
            )
            for capability_id in _CAPABILITY_IDS
        }
        platform_value = platform_name.strip() or "Unknown"

    return AdvancedCapabilityReport(
        platform=platform_value,
        capabilities=tuple(
            AdvancedCapability(
                capability_id,
                states[capability_id][0],
                states[capability_id][1],
                _presentation(states[capability_id][0]),
            )
            for capability_id in _CAPABILITY_IDS
        ),
    )


__all__ = [
    "ADVANCED_CAPABILITY_SCHEMA_VERSION",
    "AdvancedCapability",
    "AdvancedCapabilityPresentation",
    "AdvancedCapabilityReport",
    "AdvancedCapabilityState",
    "create_advanced_capability_report",
]
