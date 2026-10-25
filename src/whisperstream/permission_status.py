"""Lazy platform selection for query-only permission capability status.

Shared consumers may import this module without importing a native permission,
audio, or input framework. Platform adapters are selected only when a caller
explicitly queries status. Request operations live behind the separate
``PermissionRequester`` contract and are intentionally absent here.
"""
from __future__ import annotations

import importlib
import platform

from .permission_contract import (
    PermissionKind,
    PermissionReport,
    PermissionSnapshot,
    PermissionState,
    PermissionStatusProvider,
)


_ADAPTERS = {
    "Windows": (".windows_permission_status", "WindowsPermissionStatusProvider"),
    # Implemented by the paired native Mac loop. Keeping this import lazy leaves
    # ordinary shared and Windows construction free of Apple framework state.
    "Darwin": (".macos_permission_status", "MacosPermissionStatusProvider"),
}


def _platform_label(system: str) -> str:
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    label = system.strip().lower()
    return label or "unknown"


class UnavailablePermissionStatusProvider:
    """Conservative provider used when no truthful adapter can be queried."""

    def __init__(self, platform_name: str) -> None:
        self._platform_name = platform_name

    def query_permissions(self) -> PermissionReport:
        return PermissionReport(
            platform=self._platform_name,
            permissions=tuple(
                PermissionSnapshot(
                    kind=kind,
                    state=PermissionState.UNKNOWN,
                    query_supported=False,
                    request_supported=False,
                )
                for kind in PermissionKind
            ),
        )


def get_permission_status_provider(
    *, system: str | None = None
) -> PermissionStatusProvider:
    """Return a lazy query-only provider for ``system``.

    Unsupported platforms, missing adapters, construction errors, and objects
    that do not implement the frozen status protocol all select the conservative
    provider. No raw native failure crosses this boundary.
    """

    system_name = platform.system() if system is None else system
    platform_name = _platform_label(system_name)
    adapter = _ADAPTERS.get(system_name)
    if adapter is None:
        return UnavailablePermissionStatusProvider(platform_name)

    module_name, class_name = adapter
    try:
        module = importlib.import_module(module_name, package=__package__)
        provider_type = getattr(module, class_name)
        provider = provider_type()
    except Exception:  # noqa: BLE001 - native adapter construction boundary
        return UnavailablePermissionStatusProvider(platform_name)
    if not isinstance(provider, PermissionStatusProvider):
        return UnavailablePermissionStatusProvider(platform_name)
    return provider


def query_permissions(*, system: str | None = None) -> PermissionReport:
    """Return one stable content-free report without requesting access."""

    system_name = platform.system() if system is None else system
    platform_name = _platform_label(system_name)
    fallback = UnavailablePermissionStatusProvider(platform_name)
    try:
        report = get_permission_status_provider(system=system_name).query_permissions()
        if not isinstance(report, PermissionReport) or report.platform != platform_name:
            return fallback.query_permissions()
        by_kind = {snapshot.kind: snapshot for snapshot in report.permissions}
        return PermissionReport(
            platform=platform_name,
            permissions=tuple(by_kind[kind] for kind in PermissionKind),
        )
    except Exception:  # noqa: BLE001 - status queries must be safe and content-free
        return fallback.query_permissions()


__all__ = [
    "UnavailablePermissionStatusProvider",
    "get_permission_status_provider",
    "query_permissions",
]
