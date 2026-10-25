"""Lazy platform selection and native-free Power Mode route helpers."""

from __future__ import annotations

import platform

from .app_context_contract import (
    ApplicationContextAdapter,
    ApplicationInventoryScope,
    ContextFailure,
    ContextRequirement,
    ContextSnapshot,
    ContextStatus,
)


class UnavailableApplicationContextAdapter:
    """Truthful fallback while a platform context adapter is not implemented."""

    def capture(
        self,
        generation: int,
        requirement: ContextRequirement,
    ) -> ContextSnapshot:
        return ContextSnapshot(
            generation,
            requirement,
            ContextStatus.UNSUPPORTED,
            failure=ContextFailure.UNSUPPORTED,
        )

    def inventory(
        self,
        scope: ApplicationInventoryScope,
    ) -> tuple[object, ...]:
        del scope
        return ()

    def running_apps(self) -> list[dict[str, object]]:
        return []

    def installed_apps(self) -> list[dict[str, object]]:
        return []

    def resolve_apps(
        self,
        names: object,
        *,
        known_paths: object = None,
        default_names: object = None,
    ) -> list[dict[str, object]]:
        del names, known_paths, default_names
        return []

    def resolve_path(self, path: str) -> None:
        del path
        return None

    def resolve_favicons(self, domains: object) -> list[dict[str, object]]:
        del domains
        return []

    def safe_capture_metadata(self, snapshot: ContextSnapshot) -> dict[str, object]:
        del snapshot
        return {"browser": False, "domain_read": "n/a"}

    def uia_available(self) -> bool:
        return False

    def close(self) -> None:
        return None


def create_application_context_adapter(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> ApplicationContextAdapter:
    """Select one native adapter without importing other platforms eagerly."""

    resolved_system = system or platform.system()
    resolved_machine = (machine or platform.machine()).lower()
    if resolved_system == "Windows" and resolved_machine in {"amd64", "x86_64"}:
        from .windows_app_context import WindowsApplicationContextAdapter

        return WindowsApplicationContextAdapter()
    if resolved_system == "Darwin" and resolved_machine == "arm64":
        from .macos_app_context import MacosApplicationContextAdapter

        return MacosApplicationContextAdapter()
    return UnavailableApplicationContextAdapter()


assert isinstance(UnavailableApplicationContextAdapter(), ApplicationContextAdapter)


__all__ = [
    "UnavailableApplicationContextAdapter",
    "create_application_context_adapter",
]
