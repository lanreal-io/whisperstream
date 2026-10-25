"""Lazy platform selection for global shortcuts and shortcut capture.

Shared orchestration imports this module without importing pynput, CoreGraphics,
or native key/event objects.  Each interactive constructor selects exactly one
platform adapter and unsupported platforms fail closed.
"""

from __future__ import annotations

import platform
from types import ModuleType
from typing import Any


def _adapter_module(system: str | None = None) -> ModuleType:
    selected = platform.system() if system is None else system
    if selected == "Darwin":
        from . import macos_shortcuts as shortcut_module

        return shortcut_module
    if selected == "Windows":
        from . import keyboard_shortcuts as shortcut_module

        return shortcut_module
    raise RuntimeError("native shortcut adapter is unavailable")


def KeyboardShortcutManager(*args: Any, **kwargs: Any):
    """Construct the current platform shortcut adapter lazily."""

    return _adapter_module().KeyboardShortcutManager(*args, **kwargs)


__all__ = ["KeyboardShortcutManager"]
