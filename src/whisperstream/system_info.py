"""System hardware introspection for client-side capability checks.

Exposes:
    get_total_ram_mb()  — total physical RAM in MB (used by the Enhancement tab).
    get_available_ram_mb() — currently available physical RAM in MB.
    get_memory_snapshot() — total and available RAM from one system probe.
    list_running_apps() — visible GUI processes for the Power Mode app picker.

psutil is already a transitive dependency via pycaw (see uv.lock); this
module promotes it to a direct import. Wrappers exist so REST consumers can
rely on a None-on-failure / []-on-failure contract without reasoning about
the underlying exception surfaces.

Privacy: window titles may contain PII and must never be logged or returned
to callers. Only process names are surfaced.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

import psutil

@dataclass(frozen=True)
class MemorySnapshot:
    """Physical-memory values captured by one ``virtual_memory`` call."""

    total_mb: int
    available_mb: int


def get_memory_snapshot() -> MemorySnapshot | None:
    """Return one consistent total/available RAM snapshot, or ``None``.

    Resident-process admission needs both values from the same psutil sample so
    its initial budget and live-memory check cannot observe different moments.
    Like the individual probes below, this is best-effort and never raises.
    """

    try:
        memory = psutil.virtual_memory()
        return MemorySnapshot(
            total_mb=int(memory.total / (1024 * 1024)),
            available_mb=int(memory.available / (1024 * 1024)),
        )
    except Exception:
        return None


def get_total_ram_mb() -> int | None:
    """Return total physical RAM in MB, or None if detection fails.

    Detection should not block a download flow: callers treat None as
    "skip the warning" rather than "show an error." Any exception
    (psutil unavailable, virtual_memory() raising on an exotic platform,
    integer conversion edge case) collapses to None.
    """
    try:
        return int(psutil.virtual_memory().total / (1024 * 1024))
    except Exception:
        return None


def get_available_ram_mb() -> int | None:
    """Return currently available physical RAM in MB, or None on any failure."""

    try:
        return int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:
        return None


def list_running_apps() -> List[Dict[str, Optional[str]]]:
    """Lazily enter the Windows application owner for an explicit query."""
    if sys.platform != "win32":
        return []
    from .windows_system_info import list_running_apps as enumerate_apps
    return enumerate_apps()
