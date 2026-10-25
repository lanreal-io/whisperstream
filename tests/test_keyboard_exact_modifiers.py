"""Regression coverage for exact modifier matching in global shortcuts."""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock

import pytest
if sys.platform != "win32":
    pytest.skip("Windows exact-modifier owner tests", allow_module_level=True)

from pynput.keyboard import Key, KeyCode

from whisperstream.keyboard_shortcuts import KeyboardShortcutManager


def _manager() -> KeyboardShortcutManager:
    manager = KeyboardShortcutManager(
        logger=logging.getLogger("test.keyboard_exact_modifiers")
    )
    manager._start_listener = MagicMock()
    manager._queue = MagicMock()
    return manager


def _run_only_queued_action(manager: KeyboardShortcutManager) -> None:
    """Exercise the Mac-parity adapter's generation-guarded dispatch wrapper."""

    manager._queue.put.assert_called_once()
    manager._queue.put.call_args.args[0]()


def test_shift_then_right_ctrl_does_not_fire_standalone_toggle() -> None:
    manager = _manager()
    on_start = MagicMock()
    manager.register_toggle("right ctrl", on_start, MagicMock())

    manager._on_press(Key.shift)
    manager._on_press(Key.ctrl_r)

    manager._queue.put.assert_not_called()
    assert manager._toggle_active is False
    assert manager._toggle_debounce is False


def test_right_ctrl_then_shift_fires_standalone_toggle_once() -> None:
    manager = _manager()
    on_start = MagicMock()
    manager.register_toggle("right ctrl", on_start, MagicMock())

    manager._on_press(Key.ctrl_r)
    manager._on_press(Key.shift)

    _run_only_queued_action(manager)
    on_start.assert_called_once_with()
    assert manager._toggle_active is True


def test_multi_key_ptt_is_blocked_by_extra_modifier() -> None:
    manager = _manager()
    on_press = MagicMock()
    manager.register_push_to_talk("ctrl+k", on_press, MagicMock())

    manager._on_press(Key.shift)
    manager._on_press(Key.ctrl_l)
    manager._on_press(KeyCode.from_char("k"))

    manager._queue.put.assert_not_called()
    assert manager._ptt_pressed is False


def test_multi_key_ptt_fires_with_exact_modifier_set() -> None:
    manager = _manager()
    on_press = MagicMock()
    manager.register_push_to_talk("ctrl+k", on_press, MagicMock())

    manager._on_press(Key.ctrl_l)
    manager._on_press(KeyCode.from_char("k"))

    _run_only_queued_action(manager)
    on_press.assert_called_once_with()
    assert manager._ptt_pressed is True


@pytest.mark.parametrize("slot", ["extra", "profile"])
def test_tap_style_slots_are_blocked_by_extra_modifier(slot: str) -> None:
    manager = _manager()
    on_press = MagicMock()
    if slot == "extra":
        manager.register_extra_hotkey("ctrl+k", on_press)
    else:
        manager.register_profile_hotkeys(
            {"chat": "ctrl+k"}, lambda profile_id: on_press(profile_id)
        )

    manager._on_press(Key.shift)
    manager._on_press(Key.ctrl_l)
    manager._on_press(KeyCode.from_char("k"))

    manager._queue.put.assert_not_called()


def test_shortcut_without_modifier_target_ignores_held_modifiers() -> None:
    manager = _manager()
    on_press = MagicMock()
    manager.register_extra_hotkey("f9", on_press)

    manager._on_press(Key.shift)
    manager._on_press(Key.f9)

    _run_only_queued_action(manager)
    on_press.assert_called_once_with()
