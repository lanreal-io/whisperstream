#!/usr/bin/env python3
"""Shared macOS code-signing helpers and the Developer ID signing hook.

Developer ID signing, hardened runtime and notarization belong to the official
installer, which supplies them as a provider module. Builds name it with
``--developer-id-provider`` (default: the ``WHISPERSTREAM_DEVELOPER_ID_PROVIDER``
environment variable); without one the hook resolves to nothing, so a source
tree builds and audits with ``--signing local`` only. Shared code never imports
the provider by name; it loads it from the path the build supplies.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

DEVELOPER_ID_PROVIDER_ENV = "WHISPERSTREAM_DEVELOPER_ID_PROVIDER"
_loaded: dict[Path, ModuleType] = {}


def codesign_argv(
    identity: str,
    path: Path,
    *,
    entitlements: Path | None = None,
    identifier: str | None = None,
    runtime: bool = True,
    timestamp: bool = True,
) -> list[str]:
    """codesign argv; Developer ID callers keep the secure timestamp and runtime."""
    argv = ["/usr/bin/codesign", "--force", "--sign", identity]
    if timestamp:
        argv.append("--timestamp")
    if runtime:
        argv += ["--options", "runtime"]
    if entitlements is not None:
        argv += ["--entitlements", str(entitlements)]
    if identifier is not None:
        argv += ["--identifier", identifier]
    argv.append(str(path))
    return argv


def _module_name(provider: Path) -> str:
    # Unique per path, so a test provider never replaces the real one.
    return f"whisperstream_signing_provider_{abs(hash(str(provider))):x}"


def default_developer_id_provider() -> Path | None:
    """The provider path from the environment, or None when it names none."""
    value = os.environ.get(DEVELOPER_ID_PROVIDER_ENV)
    return Path(value) if value else None


def load_developer_id(provider: Path | None) -> ModuleType | None:
    """Return the Developer ID provider at this path, or None without one.

    A path that names no file is a broken official build, so it fails here
    instead of silently falling back to a build that cannot sign.
    """
    if provider is None:
        return None
    provider = provider.resolve()
    if not provider.is_file():
        raise FileNotFoundError(f"Developer ID provider not found: {provider}")
    if provider not in _loaded:
        spec = importlib.util.spec_from_file_location(_module_name(provider), provider)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load the Developer ID provider at {provider}")
        module = importlib.util.module_from_spec(spec)
        # Dataclasses resolve their module through sys.modules while executing.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
        _loaded[provider] = module
    return _loaded[provider]
