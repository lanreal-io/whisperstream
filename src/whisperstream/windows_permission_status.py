"""Query-only Windows permission capability mapping.

Windows has no Accessibility or Input Monitoring prompt equivalent for the
existing non-elevated pynput and injection paths, so those capabilities are
truthfully not required. Whisperstream does not currently have a reliable,
side-effect-free query for the Windows desktop microphone privacy decision, so
that state remains unknown rather than being invented from device availability.
"""
from __future__ import annotations

import platform

from .permission_contract import (
    PermissionKind,
    PermissionReport,
    PermissionSnapshot,
    PermissionState,
)


class WindowsPermissionStatusProvider:
    """Report Windows capability state without opening or registering anything."""

    def query_permissions(self) -> PermissionReport:
        if platform.system() != "Windows":
            raise RuntimeError("windows permission adapter unavailable")
        return PermissionReport(
            platform="windows",
            permissions=(
                PermissionSnapshot(
                    kind=PermissionKind.MICROPHONE,
                    state=PermissionState.UNKNOWN,
                    query_supported=False,
                    request_supported=False,
                ),
                PermissionSnapshot(
                    kind=PermissionKind.ACCESSIBILITY,
                    state=PermissionState.NOT_REQUIRED,
                    query_supported=True,
                    request_supported=False,
                ),
                PermissionSnapshot(
                    kind=PermissionKind.INPUT_MONITORING,
                    state=PermissionState.NOT_REQUIRED,
                    query_supported=True,
                    request_supported=False,
                ),
            ),
        )


__all__ = ["WindowsPermissionStatusProvider"]
