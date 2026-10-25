"""Windows early-key and recording-key suppression contracts."""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows low-level keyboard suppression contracts",
        allow_module_level=True,
    )

from whisperstream.keyboard_shortcuts import (
    KeyboardShortcutManager,
    Key,
    KeyCode,
    _LLKHF_INJECTED_MASK,
    _VK_ENTER,
    _VK_ESCAPE,
    _VK_GRAVE,
    _WM_KEYDOWN,
    _WM_KEYUP,
    _canonical,
    _is_suppressible_standalone_recording_targets,
    _suppression_key_for_vk,
)

pytestmark = pytest.mark.usefixtures("accepted_eula", "mock_shortcut_listeners")


@pytest.fixture(autouse=True)
def _no_physical_modifiers_or_ime(monkeypatch):
    """Keep the real keyboard and the focused app's layout out of these tests."""
    import whisperstream.keyboard_shortcuts as keyboard_shortcuts

    monkeypatch.setattr(keyboard_shortcuts, "_routed_key_modifier_down", lambda: False)
    monkeypatch.setattr(keyboard_shortcuts, "_foreground_uses_ime_layout", lambda: False)
    monkeypatch.setattr(KeyboardShortcutManager, "_physical_key_down", lambda self, vk: False)
    return keyboard_shortcuts


@pytest.mark.parametrize("key", ["enter", "escape"])
def test_modified_routed_key_is_never_consumed(_no_physical_modifiers_or_ime, monkeypatch, key) -> None:
    # Shift+Enter is a newline and Ctrl/Alt+Enter belong to the focused app.
    monkeypatch.setattr(_no_physical_modifiers_or_ime, "_routed_key_modifier_down", lambda: True)
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.early"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    predicate = MagicMock(return_value=True)
    manager.register_early_key_handler(key, predicate, MagicMock())
    vk = _VK_ENTER if key == "enter" else _VK_ESCAPE

    assert manager._win32_event_filter(_WM_KEYDOWN, SimpleNamespace(vkCode=vk, flags=0)) is True
    predicate.assert_not_called()
    manager._queue.put.assert_not_called()
    manager._listener.suppress_event.assert_not_called()


def test_enter_is_never_consumed_under_an_ime_layout(_no_physical_modifiers_or_ime, monkeypatch) -> None:
    monkeypatch.setattr(_no_physical_modifiers_or_ime, "_foreground_uses_ime_layout", lambda: True)
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.early"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    enter = MagicMock(return_value=True)
    escape = MagicMock(return_value=True)
    manager.register_early_key_handler("enter", enter, MagicMock())
    manager.register_early_key_handler("escape", escape, MagicMock())

    assert manager._win32_event_filter(_WM_KEYDOWN, SimpleNamespace(vkCode=_VK_ENTER, flags=0)) is True
    enter.assert_not_called()
    # Escape is not an IME commit key; it still belongs to the painted reply.
    assert manager._win32_event_filter(_WM_KEYDOWN, SimpleNamespace(vkCode=_VK_ESCAPE, flags=0)) is False


def test_early_enter_suppression_consumes_exact_down_repeat_and_up() -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.early"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    predicate = MagicMock(return_value=True)
    callback = MagicMock()
    manager.register_early_key_handler("enter", predicate, callback)
    data = SimpleNamespace(vkCode=_VK_ENTER, flags=0)

    assert manager._win32_event_filter(_WM_KEYDOWN, data) is False
    assert manager._win32_event_filter(_WM_KEYDOWN, data) is False
    assert manager._win32_event_filter(_WM_KEYUP, data) is False

    predicate.assert_called_once_with()
    manager._queue.put.assert_called_once()
    manager._queue.put.call_args.args[0]()
    callback.assert_called_once_with()
    assert manager._listener.suppress_event.call_count == 3
    assert manager._early_suppressed_vks == set()


def test_early_escape_falls_through_when_no_response_route_exists() -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.early"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    predicate = MagicMock(return_value=False)
    callback = MagicMock()
    manager.register_early_key_handler("escape", predicate, callback)
    data = SimpleNamespace(vkCode=_VK_ESCAPE, flags=0)

    assert manager._win32_event_filter(_WM_KEYDOWN, data) is True
    assert manager._win32_event_filter(_WM_KEYUP, data) is True
    predicate.assert_called_once_with()
    manager._queue.put.assert_not_called()
    manager._listener.suppress_event.assert_not_called()


def test_early_key_filter_never_consumes_injected_enter() -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.early"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    predicate = MagicMock(return_value=True)
    manager.register_early_key_handler("enter", predicate, MagicMock())

    assert manager._win32_event_filter(
        _WM_KEYDOWN,
        SimpleNamespace(vkCode=_VK_ENTER, flags=_LLKHF_INJECTED_MASK),
    ) is True
    predicate.assert_not_called()
    manager._listener.suppress_event.assert_not_called()


def test_early_key_registration_is_fixed_to_enter_and_escape() -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.early"))

    with pytest.raises(ValueError, match="Enter or Escape"):
        manager.register_early_key_handler(
            "space", lambda: True, lambda: None
        )


def test_reset_toggle_state_makes_next_press_start_again() -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.toggle"))
    manager._queue = MagicMock()
    on_start = MagicMock()
    on_stop = MagicMock()
    manager.register_toggle("f8", on_start, on_stop)

    manager._on_press(Key.f8)
    manager._on_release(Key.f8)
    manager.reset_toggle_state()
    manager._on_press(Key.f8)
    manager._on_release(Key.f8)

    assert manager._queue.put.call_count == 2
    for queued in manager._queue.put.call_args_list:
        queued.args[0]()
    assert on_start.call_count == 2
    on_stop.assert_not_called()


@pytest.mark.parametrize(
    "shortcut",
    [
        "`",
        "~",
        "caps lock",
        "f1",
        "f24",
        "ctrl",
        "right ctrl",
        "shift",
        "right shift",
        "alt",
        "right alt",
        "win",
        "right win",
    ],
)
def test_recording_suppression_eligibility_accepts_dedicated_standalone_keys(
    shortcut: str,
) -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.suppress"))

    assert _is_suppressible_standalone_recording_targets(
        manager._parse_shortcut(shortcut)
    )


@pytest.mark.parametrize(
    "shortcut",
    [
        "a",
        "1",
        ";",
        "space",
        "insert",
        "page up",
        "ctrl+f8",
    ],
)
def test_recording_suppression_eligibility_rejects_typing_navigation_and_combos(
    shortcut: str,
) -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.suppress"))

    assert not _is_suppressible_standalone_recording_targets(
        manager._parse_shortcut(shortcut)
    )


@pytest.mark.parametrize("shortcut", ["`", "~"])
def test_grave_raw_vk_maps_to_the_stored_physical_key_name(shortcut: str) -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.suppress"))
    targets = manager._parse_shortcut(shortcut)

    key = _suppression_key_for_vk(targets, _VK_GRAVE)

    assert key is not None
    assert _canonical(key) in targets[0]


def test_physical_grave_vk_matches_modifier_combo() -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.grave-combo"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    callback = MagicMock()
    manager.register_extra_hotkey("ctrl+alt+`", callback, key="claude_view")

    manager._on_press(Key.ctrl_l)
    manager._on_press(Key.alt_l)
    manager._on_press(KeyCode(vk=_VK_GRAVE))

    manager._queue.put.assert_called_once()
    manager._queue.put.call_args.args[0]()
    callback.assert_called_once_with()


def test_recording_filter_consumes_down_repeats_and_up_with_ptt_callbacks() -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.suppress"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    on_press = MagicMock()
    on_release = MagicMock()
    manager._ptt_targets = manager._parse_shortcut("`")
    manager._ptt_on_press = on_press
    manager._ptt_on_release = on_release
    data = SimpleNamespace(vkCode=_VK_GRAVE, flags=0)
    original_on_press = manager._on_press
    original_on_release = manager._on_release
    manager._on_press = MagicMock(wraps=original_on_press)
    manager._on_release = MagicMock(wraps=original_on_release)

    assert manager._win32_event_filter(_WM_KEYDOWN, data) is False
    assert manager._win32_event_filter(_WM_KEYDOWN, data) is False
    assert manager._win32_event_filter(_WM_KEYUP, data) is False

    assert manager._on_press.call_count == 2
    manager._on_release.assert_called_once()
    assert manager._queue.put.call_count == 2
    for queued in manager._queue.put.call_args_list:
        queued.args[0]()
    on_press.assert_called_once_with()
    on_release.assert_called_once_with()
    assert manager._listener.suppress_event.call_count == 3
    assert manager._recording_suppressed_keys == {}
    assert manager._pressed == set()


@pytest.mark.parametrize("typing_vk", [0xBF, 0x41])
def test_reserved_shift_stays_consumed_when_a_typing_key_joins(typing_vk: int) -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.suppress"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    manager._ptt_targets = manager._parse_shortcut("right shift")
    manager._ptt_on_press = MagicMock()
    manager._ptt_on_release = MagicMock()
    shift = SimpleNamespace(vkCode=0xA1, flags=0)
    typing = SimpleNamespace(vkCode=typing_vk, flags=0)

    assert manager._win32_event_filter(_WM_KEYDOWN, shift) is False
    assert manager._win32_event_filter(_WM_KEYDOWN, typing) is True
    assert manager._win32_event_filter(_WM_KEYUP, typing) is True
    # Joining a typing key must not end/replay the reserved Shift gesture.
    assert manager._queue.put.call_count == 1
    assert manager._recording_suppressed_keys == {0xA1: Key.shift_r}
    assert manager._win32_event_filter(_WM_KEYUP, shift) is False
    assert manager._listener.suppress_event.call_count == 2
    assert manager._queue.put.call_count == 2


def test_recording_filter_supports_eligible_standalone_toggle_binding() -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.suppress"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    on_start = MagicMock()
    manager._toggle_targets = manager._parse_shortcut("f8")
    manager._toggle_on_start = on_start
    data = SimpleNamespace(vkCode=0x77, flags=0)

    assert manager._win32_event_filter(_WM_KEYDOWN, data) is False
    assert manager._win32_event_filter(_WM_KEYUP, data) is False

    manager._queue.put.assert_called_once()
    manager._queue.put.call_args.args[0]()
    on_start.assert_called_once_with()


@pytest.mark.parametrize(
    ("shortcut", "vk"),
    [
        ("right shift", 0xA1),
        ("left ctrl", 0xA2),
        ("left alt", 0xA4),
        ("caps lock", 0x14),
        ("f8", 0x77),
    ],
)
def test_recording_release_reaches_windows_when_suppressed_down_leaked(
    monkeypatch, shortcut: str, vk: int,
) -> None:
    """A timed-out down must not become a permanently held Windows modifier."""
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.suppress"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    manager._ptt_targets = manager._parse_shortcut(shortcut)
    on_press = MagicMock()
    on_release = MagicMock()
    manager._ptt_on_press = on_press
    manager._ptt_on_release = on_release
    data = SimpleNamespace(vkCode=vk, flags=0)
    windows_down = [False]
    monkeypatch.setattr(manager, "_physical_key_down", lambda code: code == vk and windows_down[0])

    assert manager._win32_event_filter(_WM_KEYDOWN, data) is False
    # Model Windows passing a hook callback that exceeded its deadline.
    windows_down[0] = True
    before_release = manager._listener.suppress_event.call_count
    assert manager._win32_event_filter(_WM_KEYUP, data) is False

    # Returning False skips duplicate pynput callbacks; only suppress_event()
    # hides the event from Windows. The leaked down needs its physical up.
    assert manager._listener.suppress_event.call_count == before_release
    for queued in manager._queue.put.call_args_list:
        queued.args[0]()
    on_press.assert_called_once_with()
    on_release.assert_called_once_with()
    assert manager._recording_suppressed_keys == {}
    assert manager._pressed == set()

    # Recovery also rearms the next press, whose down stays hidden normally.
    manager._queue.reset_mock()
    windows_down[0] = False
    assert manager._win32_event_filter(_WM_KEYDOWN, data) is False
    before_release = manager._listener.suppress_event.call_count
    assert manager._win32_event_filter(_WM_KEYUP, data) is False
    assert manager._listener.suppress_event.call_count == before_release + 1
    for queued in manager._queue.put.call_args_list:
        queued.args[0]()
    assert on_press.call_count == on_release.call_count == 2


def test_recording_filter_bypasses_injected_eligible_key() -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.suppress"))
    manager._listener = MagicMock()
    manager._on_press = MagicMock()
    manager._ptt_targets = manager._parse_shortcut("`")

    assert manager._win32_event_filter(
        _WM_KEYDOWN,
        SimpleNamespace(vkCode=_VK_GRAVE, flags=_LLKHF_INJECTED_MASK),
    ) is True

    manager._on_press.assert_not_called()
    manager._listener.suppress_event.assert_not_called()
    assert manager._recording_suppressed_keys == {}


@pytest.mark.parametrize(
    ("shortcut", "vk"),
    [
        ("a", 0x41),
        ("1", 0x31),
        (";", 0xBA),
        ("space", 0x20),
        ("page up", 0x21),
    ],
)
def test_recording_filter_leaves_noneligible_standalone_bindings_alone(
    shortcut: str, vk: int,
) -> None:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.suppress"))
    manager._listener = MagicMock()
    manager._on_press = MagicMock()
    manager._ptt_targets = manager._parse_shortcut(shortcut)

    assert manager._win32_event_filter(
        _WM_KEYDOWN, SimpleNamespace(vkCode=vk, flags=0)
    ) is True

    manager._on_press.assert_not_called()
    manager._listener.suppress_event.assert_not_called()


def test_early_enter_lost_keyup_does_not_swallow_the_next_press(monkeypatch) -> None:
    import whisperstream.keyboard_shortcuts as ks

    clock = [100.0]
    monkeypatch.setattr(ks.time, "monotonic", lambda: clock[0])
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.early"))
    manager._listener = MagicMock()
    manager._queue = MagicMock()
    owned = [True]
    manager.register_early_key_handler("enter", lambda: owned[0], MagicMock())
    data = SimpleNamespace(vkCode=_VK_ENTER, flags=0)

    assert manager._win32_event_filter(_WM_KEYDOWN, data) is False
    # Key-up lost. Auto-repeat inside the window stays hidden.
    clock[0] += 0.03
    assert manager._win32_event_filter(_WM_KEYDOWN, data) is False

    # Much later the draft is gone; the next Enter must reach the focused app.
    owned[0] = False
    clock[0] += 5.0
    assert manager._win32_event_filter(_WM_KEYDOWN, data) is True
    assert manager._early_suppressed_vks == set()
