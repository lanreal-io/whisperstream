"""Lazy platform selector for local LLM runtime adapters."""

from __future__ import annotations

import platform
from pathlib import Path


def create_managed_runtime(
    supervisor: object,
    *,
    catalog_id: str,
    artifact_path: Path,
    metal_probe=None,
) -> object | None:
    """Return the platform adapter without importing another OS's owner."""

    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows" and machine in {"amd64", "x86_64"}:
        from .windows_local_llm_runtime import WindowsManagedLlamaRuntime

        return WindowsManagedLlamaRuntime(
            supervisor, catalog_id=catalog_id, artifact_path=artifact_path
        )
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        from .macos_local_llm_runtime import MacosManagedLlamaRuntime

        kwargs = {} if metal_probe is None else {"metal_probe": metal_probe}
        return MacosManagedLlamaRuntime(
            supervisor,
            catalog_id=catalog_id,
            artifact_path=artifact_path,
            **kwargs,
        )
    return None


__all__ = ["create_managed_runtime"]
