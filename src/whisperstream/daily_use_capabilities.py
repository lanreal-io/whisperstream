"""Serializable daily-use capability report for onboarding and settings.

This module reports implementation support. Callers may compose a passive
boolean from an owning adapter, but native identity and detailed volatile state
never cross this schema-v1 boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CapabilityState(str, Enum):
    AVAILABLE = "available"
    NOT_IMPLEMENTED = "not-implemented"
    UNAVAILABLE = "unavailable"
    EXTERNAL_DEPENDENCY = "external-dependency"
    REQUIRES_USER_ACTION = "requires-user-action"


class CapabilityPresentation(str, Enum):
    ENABLE = "enable"
    DISABLE = "disable"
    EXPLAIN = "explain"


@dataclass(frozen=True)
class DailyUseCapability:
    capability_id: str
    state: CapabilityState
    reason_code: str
    presentation: CapabilityPresentation

    def __post_init__(self) -> None:
        if not self.capability_id.strip() or not self.reason_code.strip():
            raise ValueError("capability_id and reason_code must be nonempty")
        if self.state is CapabilityState.AVAILABLE:
            if self.presentation is not CapabilityPresentation.ENABLE:
                raise ValueError("available capability must be enabled")
        elif self.presentation is CapabilityPresentation.ENABLE:
            raise ValueError("absent capability cannot present an enabled action")

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.capability_id,
            "state": self.state.value,
            "reason": self.reason_code,
            "presentation": self.presentation.value,
        }


@dataclass(frozen=True)
class DailyUseCapabilityReport:
    platform: str
    capabilities: tuple[DailyUseCapability, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        ids = [item.capability_id for item in self.capabilities]
        if not self.platform.strip():
            raise ValueError("platform must be nonempty")
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("capabilities must be unique and sorted")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "capabilities": [item.as_dict() for item in self.capabilities],
        }


_CAPABILITY_IDS = (
    "audio-policy",
    "auto-update",
    "cloud-enhancement",
    "file-workflows",
    "history-security",
    "launch-at-login",
    "licensing",
    "managed-local-enhancement",
    "model-lifecycle",
    "ollama-enhancement",
    "protected-storage",
    "qwen-stt",
    "window-lifecycle",
)


def _capability(
    capability_id: str,
    state: CapabilityState,
    reason: str,
) -> DailyUseCapability:
    if state is CapabilityState.AVAILABLE:
        presentation = CapabilityPresentation.ENABLE
    elif state in {
        CapabilityState.EXTERNAL_DEPENDENCY,
        CapabilityState.REQUIRES_USER_ACTION,
    }:
        presentation = CapabilityPresentation.EXPLAIN
    else:
        presentation = CapabilityPresentation.DISABLE
    return DailyUseCapability(capability_id, state, reason, presentation)


def create_daily_use_capability_report(
    platform_name: str,
    *,
    audio_policy_available: bool | None = None,
) -> DailyUseCapabilityReport:
    """Build implementation support without importing native owners."""

    normalized = platform_name.strip().lower()
    if normalized == "windows":
        states = {
            capability_id: (CapabilityState.AVAILABLE, "implemented")
            for capability_id in _CAPABILITY_IDS
        }
        states["ollama-enhancement"] = (
            CapabilityState.EXTERNAL_DEPENDENCY,
            "endpoint-required",
        )
        platform_value = "Windows"
    elif normalized in {"darwin", "macos"}:
        states = {
            capability_id: (CapabilityState.NOT_IMPLEMENTED, "phase4-pending")
            for capability_id in _CAPABILITY_IDS
        }
        states["cloud-enhancement"] = (CapabilityState.AVAILABLE, "shared-transport")
        # The official Developer ID build ships the updater on its own Mac
        # channel; source builds have none, as on Windows.
        states["auto-update"] = (CapabilityState.AVAILABLE, "implemented")
        states["managed-local-enhancement"] = (
            CapabilityState.AVAILABLE,
            "metal-runtime",
        )
        states["model-lifecycle"] = (
            CapabilityState.AVAILABLE,
            "fluidaudio-verified",
        )
        states["file-workflows"] = (
            CapabilityState.AVAILABLE,
            "native-panel-file-sessions",
        )
        states["history-security"] = (
            CapabilityState.AVAILABLE,
            "keychain-aes-gcm",
        )
        states["licensing"] = (
            CapabilityState.AVAILABLE,
            "unrestricted-source-entitlement",
        )
        if audio_policy_available is True:
            states["audio-policy"] = (
                CapabilityState.AVAILABLE,
                "coreaudio-output-policy",
            )
        else:
            states["audio-policy"] = (
                CapabilityState.UNAVAILABLE,
                (
                    "software-output-control-unsupported"
                    if audio_policy_available is False
                    else "output-control-unconfirmed"
                ),
            )
        states["qwen-stt"] = (
            CapabilityState.AVAILABLE,
            "supervised-sherpa-worker",
        )
        states["ollama-enhancement"] = (
            CapabilityState.EXTERNAL_DEPENDENCY,
            "endpoint-required",
        )
        states["protected-storage"] = (
            CapabilityState.AVAILABLE,
            "keychain-adapter",
        )
        states["window-lifecycle"] = (
            CapabilityState.AVAILABLE,
            "appkit-window-lifecycle",
        )
        states["launch-at-login"] = (
            CapabilityState.AVAILABLE,
            "smappservice-main-app",
        )
        platform_value = "Darwin"
    else:
        states = {
            capability_id: (CapabilityState.UNAVAILABLE, "unsupported-platform")
            for capability_id in _CAPABILITY_IDS
        }
        platform_value = platform_name.strip() or "Unknown"

    return DailyUseCapabilityReport(
        platform=platform_value,
        capabilities=tuple(
            _capability(capability_id, *states[capability_id])
            for capability_id in _CAPABILITY_IDS
        ),
    )


DAILY_USE_CAPABILITY_SCHEMA_VERSION = 1
