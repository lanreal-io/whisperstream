"""Tests for the generalized extra-hotkey registry on KeyboardShortcutManager.

Task 1.3: `_extra_hotkeys: dict[str, dict]` replaces the single-slot
`_extra_hotkey_targets` / `_extra_on_press` / `_extra_pressed` attributes.

Scenarios covered:
- Two independent extra hotkeys registered under different keys both fire their
  own callback when each combo is pressed.
- Re-registering the same key replaces (not duplicates) that slot.
- An empty-string shortcut unbinds a key without raising.
- A non-empty unparseable shortcut raises ValueError.
- `unhook_all` clears all extra hotkeys.

All tests drive `_on_press` / `_on_release` at the unit level. Registration
starts listener doubles so no global OS input hooks are installed.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock

import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows legacy extra-hotkey registry tests require the pynput Windows owner",
        allow_module_level=True,
    )

from pynput.keyboard import Key, KeyCode

from whisperstream.keyboard_shortcuts import KeyboardShortcutManager, _canonical

pytestmark = pytest.mark.usefixtures("mock_shortcut_listeners")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager() -> KeyboardShortcutManager:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.extra_hotkeys"))
    # These tests drive callbacks directly. Never install a real global hook in
    # the owner's desktop session merely to populate a synthetic registry.
    manager._start_listener = MagicMock()
    return manager


def _press(manager: KeyboardShortcutManager, *keys) -> None:
    """Simulate key-presses on *manager* without starting a real listener."""
    for k in keys:
        manager._on_press(k)


def _release(manager: KeyboardShortcutManager, *keys) -> None:
    """Simulate key-releases on *manager* without starting a real listener."""
    for k in keys:
        manager._on_release(k)


def _flush_queue(manager: KeyboardShortcutManager) -> None:
    """Drain all pending callbacks from the dispatch queue synchronously.

    The dispatch worker runs on a daemon thread; for unit tests we drain the
    queue manually so we can assert immediately after _on_press returns.
    """
    # Put a sentinel and block until it's processed to flush anything before it.
    done = __import__("threading").Event()
    manager._queue.put(done.set)
    done.wait(timeout=2.0)


# ---------------------------------------------------------------------------
# Two independent extra hotkeys both fire their own callback
# ---------------------------------------------------------------------------

class TestTwoIndependentExtraHotkeys:
    def test_both_callbacks_fire_independently(self):
        """Two extra hotkeys registered under different keys must each fire
        only their own callback when their respective combo is pressed."""
        m = _make_manager()
        cb_paste_last = MagicMock()
        cb_paste_original = MagicMock()

        # Register two independent extra hotkeys.
        m.register_extra_hotkey("ctrl+alt+v", cb_paste_last, key="paste_last")
        m.register_extra_hotkey("ctrl+alt+o", cb_paste_original, key="paste_last_original")

        assert len(m._extra_hotkeys) == 2

        # Press ctrl+alt+v — only paste_last should fire.
        _press(m, Key.ctrl_l, Key.alt_l, KeyCode.from_char("v"))
        _flush_queue(m)
        cb_paste_last.assert_called_once()
        cb_paste_original.assert_not_called()

        # Release all keys.
        _release(m, KeyCode.from_char("v"), Key.alt_l, Key.ctrl_l)

        # Press ctrl+alt+o — only paste_last_original should fire.
        _press(m, Key.ctrl_l, Key.alt_l, KeyCode.from_char("o"))
        _flush_queue(m)
        cb_paste_last.assert_called_once()  # still only once
        cb_paste_original.assert_called_once()

        _release(m, KeyCode.from_char("o"), Key.alt_l, Key.ctrl_l)

    def test_both_keys_present_in_registry(self):
        """After registering two extra hotkeys, both keys appear in _extra_hotkeys."""
        m = _make_manager()
        m.register_extra_hotkey("ctrl+alt+v", MagicMock(), key="paste_last")
        m.register_extra_hotkey("ctrl+alt+o", MagicMock(), key="paste_last_original")

        assert "paste_last" in m._extra_hotkeys
        assert "paste_last_original" in m._extra_hotkeys

    def test_each_entry_has_expected_shape(self):
        """Each registry entry must contain 'targets', 'on_press', and 'pressed'."""
        m = _make_manager()
        cb = MagicMock()
        m.register_extra_hotkey("ctrl+alt+v", cb, key="paste_last")

        entry = m._extra_hotkeys["paste_last"]
        assert "targets" in entry
        assert "on_press" in entry
        assert "pressed" in entry
        assert isinstance(entry["targets"], list)
        assert entry["on_press"] is cb
        assert entry["pressed"] is False

    def test_pressed_flag_resets_on_release(self):
        """After releasing the combo, the entry's 'pressed' flag resets to False
        so the callback can fire again on the next press."""
        m = _make_manager()
        cb = MagicMock()
        m.register_extra_hotkey("ctrl+alt+v", cb, key="paste_last")

        # Press the combo.
        _press(m, Key.ctrl_l, Key.alt_l, KeyCode.from_char("v"))
        _flush_queue(m)
        assert m._extra_hotkeys["paste_last"]["pressed"] is True

        # Release — flag must reset.
        _release(m, KeyCode.from_char("v"), Key.alt_l, Key.ctrl_l)
        assert m._extra_hotkeys["paste_last"]["pressed"] is False


# ---------------------------------------------------------------------------
# Re-registering the same key replaces (not duplicates) the slot
# ---------------------------------------------------------------------------

class TestReRegistrationReplacesSlot:
    def test_re_registering_same_key_replaces_callback(self):
        """Calling register_extra_hotkey with the same key replaces the entry;
        the previous callback must NOT fire after replacement."""
        m = _make_manager()
        old_cb = MagicMock()
        new_cb = MagicMock()

        m.register_extra_hotkey("ctrl+alt+v", old_cb, key="paste_last")
        m.register_extra_hotkey("ctrl+alt+v", new_cb, key="paste_last")

        # Still exactly one entry for this key.
        assert len([k for k in m._extra_hotkeys if k == "paste_last"]) == 1
        assert len(m._extra_hotkeys) == 1

        _press(m, Key.ctrl_l, Key.alt_l, KeyCode.from_char("v"))
        _flush_queue(m)

        old_cb.assert_not_called()
        new_cb.assert_called_once()

    def test_re_registering_does_not_grow_registry(self):
        """The registry must not accumulate duplicate entries on repeated calls."""
        m = _make_manager()
        for _ in range(5):
            m.register_extra_hotkey("ctrl+alt+v", MagicMock(), key="paste_last")

        assert len(m._extra_hotkeys) == 1

    def test_re_registering_resets_pressed_flag(self):
        """A fresh registration resets the 'pressed' flag to False even if the
        combo was mid-press when re-registered."""
        m = _make_manager()
        cb1 = MagicMock()
        m.register_extra_hotkey("ctrl+alt+v", cb1, key="paste_last")

        # Simulate a mid-press state.
        _press(m, Key.ctrl_l, Key.alt_l, KeyCode.from_char("v"))
        _flush_queue(m)
        assert m._extra_hotkeys["paste_last"]["pressed"] is True

        # Re-register: the slot should reset.
        m.register_extra_hotkey("ctrl+alt+v", MagicMock(), key="paste_last")
        assert m._extra_hotkeys["paste_last"]["pressed"] is False


# ---------------------------------------------------------------------------
# Empty shortcut unbinds without raising
# ---------------------------------------------------------------------------

class TestEmptyShortcutUnbinds:
    def test_empty_string_unbinds_existing_key(self):
        """Passing shortcut='' removes the entry for that key from _extra_hotkeys."""
        m = _make_manager()
        cb = MagicMock()
        m.register_extra_hotkey("ctrl+alt+v", cb, key="paste_last")
        assert "paste_last" in m._extra_hotkeys

        m.register_extra_hotkey("", cb, key="paste_last")
        assert "paste_last" not in m._extra_hotkeys

    def test_empty_string_does_not_raise(self):
        """Passing shortcut='' must not raise ValueError (or any exception)."""
        m = _make_manager()
        # Should not raise even if the key was never registered.
        m.register_extra_hotkey("", MagicMock(), key="paste_last")

    def test_whitespace_only_shortcut_also_unbinds(self):
        """A shortcut that is all whitespace is treated as empty and unbinds."""
        m = _make_manager()
        m.register_extra_hotkey("ctrl+alt+v", MagicMock(), key="paste_last")
        m.register_extra_hotkey("   ", MagicMock(), key="paste_last")
        assert "paste_last" not in m._extra_hotkeys

    def test_empty_unbind_on_unregistered_key_is_silent(self):
        """Unbinding a key that was never registered must not raise."""
        m = _make_manager()
        m.register_extra_hotkey("", MagicMock(), key="nonexistent_key")

    def test_callback_no_longer_fires_after_unbind(self):
        """After unbinding via empty shortcut, pressing the old combo does not
        fire the callback."""
        m = _make_manager()
        cb = MagicMock()
        m.register_extra_hotkey("ctrl+alt+v", cb, key="paste_last")
        m.register_extra_hotkey("", cb, key="paste_last")

        _press(m, Key.ctrl_l, Key.alt_l, KeyCode.from_char("v"))
        _flush_queue(m)
        cb.assert_not_called()


# ---------------------------------------------------------------------------
# Non-empty unparseable shortcut raises ValueError
# ---------------------------------------------------------------------------

class TestUnparseableShortcutRaisesValueError:
    def test_garbage_shortcut_raises_value_error(self):
        """A non-empty shortcut string that cannot be parsed raises ValueError."""
        m = _make_manager()
        with pytest.raises(ValueError):
            m.register_extra_hotkey("not_a_valid_key_at_all_xyz", MagicMock(), key="paste_last")

    def test_registry_unchanged_after_failed_parse(self):
        """A ValueError on parse must leave _extra_hotkeys unchanged."""
        m = _make_manager()
        m.register_extra_hotkey("ctrl+alt+v", MagicMock(), key="paste_last")
        count_before = len(m._extra_hotkeys)

        with pytest.raises(ValueError):
            m.register_extra_hotkey("bad_key_xyz", MagicMock(), key="paste_last")

        # The existing entry must still be there, unchanged.
        assert len(m._extra_hotkeys) == count_before
        assert "paste_last" in m._extra_hotkeys


# ---------------------------------------------------------------------------
# unhook_all clears all extra hotkeys
# ---------------------------------------------------------------------------

class TestUnhookAllClearsExtraHotkeys:
    def test_unhook_all_clears_extra_hotkeys(self):
        """unhook_all() must reset _extra_hotkeys to an empty dict."""
        m = _make_manager()
        m.register_extra_hotkey("ctrl+alt+v", MagicMock(), key="paste_last")
        m.register_extra_hotkey("ctrl+alt+o", MagicMock(), key="paste_last_original")

        assert len(m._extra_hotkeys) == 2
        m.unhook_all()
        assert m._extra_hotkeys == {}

    def test_unhook_all_with_no_extra_hotkeys_is_safe(self):
        """unhook_all() on an empty registry must not raise."""
        m = _make_manager()
        m.unhook_all()
        assert m._extra_hotkeys == {}

    def test_callbacks_do_not_fire_after_unhook_all(self):
        """After unhook_all(), pressing the previously-bound combo must not
        fire any callback."""
        m = _make_manager()
        cb = MagicMock()
        m.register_extra_hotkey("ctrl+alt+v", cb, key="paste_last")
        m.unhook_all()

        _press(m, Key.ctrl_l, Key.alt_l, KeyCode.from_char("v"))
        _flush_queue(m)
        cb.assert_not_called()


# ---------------------------------------------------------------------------
# unregister_extra_hotkey API
# ---------------------------------------------------------------------------

class TestUnregisterExtraHotkey:
    def test_unregister_specific_key_removes_only_that_entry(self):
        """unregister_extra_hotkey(key='paste_last') removes only that key."""
        m = _make_manager()
        m.register_extra_hotkey("ctrl+alt+v", MagicMock(), key="paste_last")
        m.register_extra_hotkey("ctrl+alt+o", MagicMock(), key="paste_last_original")

        m.unregister_extra_hotkey(key="paste_last")

        assert "paste_last" not in m._extra_hotkeys
        assert "paste_last_original" in m._extra_hotkeys

    def test_unregister_none_clears_all(self):
        """unregister_extra_hotkey(key=None) clears the entire registry."""
        m = _make_manager()
        m.register_extra_hotkey("ctrl+alt+v", MagicMock(), key="paste_last")
        m.register_extra_hotkey("ctrl+alt+o", MagicMock(), key="paste_last_original")

        m.unregister_extra_hotkey(key=None)

        assert m._extra_hotkeys == {}

    def test_unregister_nonexistent_key_is_silent(self):
        """unregister_extra_hotkey with a key not in the registry must not raise."""
        m = _make_manager()
        m.unregister_extra_hotkey(key="ghost_key")


# ---------------------------------------------------------------------------
# Default key parameter backward-compat
# ---------------------------------------------------------------------------

class TestDefaultKeyParameter:
    def test_register_without_key_uses_paste_last_default(self):
        """register_extra_hotkey without a key kwarg defaults to key='paste_last'."""
        m = _make_manager()
        cb = MagicMock()
        m.register_extra_hotkey("ctrl+alt+v", cb)

        assert "paste_last" in m._extra_hotkeys
        assert m._extra_hotkeys["paste_last"]["on_press"] is cb


# ---------------------------------------------------------------------------
# Regression: a Ctrl+Alt+letter combo must not degrade into firing on its
# modifiers alone.
#
# pynput reports KeyCode.char for a letter differently depending on the held
# modifiers (the letter, a Ctrl control char like '\x03', or None). If press
# and release of the SAME physical key canonicalize differently, the release
# never removes the letter from _pressed, leaving a phantom key that keeps the
# combo satisfied — so a bare ctrl+alt tap re-fires the hotkey. This is the
# "pasting on just ctrl+alt even though the shortcut is ctrl+alt+c" bug.
# ---------------------------------------------------------------------------

class TestModifierLetterReleaseDoesNotStick:
    def test_letter_release_removes_key_despite_char_mismatch(self):
        """Press reports char='c'; release reports the Ctrl control char for
        the same vk. The letter must still leave _pressed on release."""
        m = _make_manager()
        m.register_extra_hotkey("ctrl+alt+c", MagicMock(), key="paste_last_original")

        _press(m, Key.ctrl_l, Key.alt_l, KeyCode(vk=67, char="c"))
        _flush_queue(m)
        assert ord("c") in m._pressed

        _release(m, KeyCode(vk=67, char="\x03"))
        assert ord("c") not in m._pressed, (
            "released letter stuck in _pressed — press/release canonical mismatch"
        )

    def test_ctrl_alt_does_not_refire_after_letter_release(self):
        """After pressing then releasing ctrl+alt+c, cycling ctrl+alt alone
        (no letter) must NOT re-fire the hotkey."""
        m = _make_manager()
        cb = MagicMock()
        m.register_extra_hotkey("ctrl+alt+c", cb, key="paste_last_original")

        # Genuine ctrl+alt+c press fires exactly once.
        _press(m, Key.ctrl_l, Key.alt_l, KeyCode(vk=67, char="c"))
        _flush_queue(m)
        assert cb.call_count == 1

        # Release the letter (Ctrl control char), then the combo is no longer held.
        _release(m, KeyCode(vk=67, char="\x03"))
        assert m._extra_hotkeys["paste_last_original"]["pressed"] is False

        # Cycle the modifiers only — must not re-fire.
        _release(m, Key.alt_l)
        _press(m, Key.alt_l)
        _flush_queue(m)
        assert cb.call_count == 1, (
            "ctrl+alt with no letter re-fired the hotkey — a phantom letter is "
            "stuck in _pressed"
        )
