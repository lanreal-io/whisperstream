"""Lazy platform selection for interactive microphone capture.

The selector imports no native audio module until an interactive constructor or
device probe is called.  Platform adapters keep PortAudio indices, CoreAudio
identifiers, and stream objects behind the schema in :mod:`capture_contract`.
"""

from __future__ import annotations

import platform
from types import ModuleType
from typing import Any


def _adapter_module(system: str | None = None) -> ModuleType:
    selected = platform.system() if system is None else system
    if selected == "Darwin":
        from . import macos_capture as capture_module

        return capture_module
    if selected == "Windows":
        from . import audio_capture as capture_module

        return capture_module
    raise RuntimeError("native capture adapter is unavailable")


def AudioCapture(*args: Any, **kwargs: Any):
    """Construct the current platform capture adapter lazily."""
    return _adapter_module().AudioCapture(*args, **kwargs)


def enumerate_input_devices():
    """Return the current platform's typed, prompt-free input inventory."""
    return _adapter_module().enumerate_input_devices()


def list_input_devices() -> list[dict[str, Any]]:
    """Return the UI-facing input inventory without native identifiers."""
    try:
        return _adapter_module().list_input_devices()
    except RuntimeError:
        return []


def has_input_device() -> bool:
    """Return whether a supported platform currently exposes an input."""
    try:
        return bool(_adapter_module().has_input_device())
    except RuntimeError:
        return False


__all__ = [
    "AudioCapture",
    "enumerate_input_devices",
    "has_input_device",
    "list_input_devices",
]
