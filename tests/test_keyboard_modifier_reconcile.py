"""Regression tests for the keyboard modifier-mirror desync bugs.

Root cause (shared by both bugs): ``suppress_self_events()`` makes
``_on_press`` / ``_on_release`` early-return WITHOUT updating ``_pressed`` so
synthetic injection keystrokes can't re-enter the hotkey logic. That also
swallows genuine user key events that land inside the injection window + its
grace tail, so the ``_pressed`` software mirror drifts from the real keyboard.

- **Bug A (keystroke contamination):** the mirror MISSES a physically-held
  Ctrl/Alt, so the injector's modifier-release guard (which reads
  ``snapshot_held_modifiers``) releases nothing and keystroke-mode typing runs
  with the modifier held -> every char becomes a chord (VSCode opens/creates
  files). Fixed by making ``snapshot_held_modifiers`` authoritative on the real
  OS key state (``GetAsyncKeyState``), unioned with the mirror.

- **Bug B (phantom paste_last):** the mirror RETAINS phantom keys after a
  paste_last (the ctrl+alt+v releases land during suppression), so a bare Left
  Ctrl press completes the stuck ctrl+alt+v and re-fires paste_last "every other
  time." Fixed by reconciling ``_pressed`` to OS truth when the suppression
  window closes.

These tests monkeypatch ``_physical_key_down`` so the OS oracle is deterministic
and the mirror can be driven into divergence on purpose.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock

import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows GetAsyncKeyState modifier reconciliation tests",
        allow_module_level=True,
    )

from pynput.keyboard import Key, KeyCode

from whisperstream import keyboard_shortcuts
from whisperstream.keyboard_shortcuts import (
    KeyboardShortcutManager,
    _canon_to_vk,
    _SUPPRESS_GRACE_S,
)

# One grace cycle plus a small buffer for Timer scheduling jitter (mirrors
# test_self_event_suppression).
_GRACE_WAIT = _SUPPRESS_GRACE_S + 0.05

pytestmark = pytest.mark.usefixtures("mock_shortcut_listeners")

_requires_windows = pytest.mark.skipif(
    sys.platform != "win32",
    reason="GetAsyncKeyState reconciliation is Windows-only",
)


def _make_manager() -> KeyboardShortcutManager:
    manager = KeyboardShortcutManager(logger=logging.getLogger("test.reconcile"))
    manager._start_listener = MagicMock()
    return manager


def _press(m: KeyboardShortcutManager, *keys) -> None:
    for k in keys:
        m._on_press(k)


def _release(m: KeyboardShortcutManager, *keys) -> None:
    for k in keys:
        m._on_release(k)


def _flush_queue(m: KeyboardShortcutManager) -> None:
    """Drain the dispatch queue synchronously (callbacks run on a daemon thread)."""
    done = __import__("threading").Event()
    m._queue.put(done.set)
    done.wait(timeout=2.0)


@pytest.fixture
def controlled_timers(monkeypatch):
    """Capture suppression timers so these tests do not depend on thread timing."""
    timers = []

    def make_timer(delay_s, callback):
        timer = MagicMock(delay_s=delay_s, callback=callback)
        timers.append(timer)
        return timer

    monkeypatch.setattr(keyboard_shortcuts, "_new_suppress_timer", make_timer)
    return timers


def _vk(key: Key) -> int:
    return int(key.value.vk)


# ---------------------------------------------------------------------------
# _canon_to_vk mapping
# ---------------------------------------------------------------------------

class TestCanonToVk:
    def test_key_enum_uses_value_vk(self):
        assert _canon_to_vk(Key.ctrl_l) == _vk(Key.ctrl_l)

    def test_letter_lowercase_ordinal_maps_to_uppercase_vk(self):
        # 'v' is stored as ord('v')=118; the Windows VK for V is 0x56 == 86.
        assert _canon_to_vk(ord("v")) == 0x56

    def test_digit_ordinal_maps_to_digit_vk(self):
        # '5' is stored as ord('5')=53, which equals VK_5.
        assert _canon_to_vk(ord("5")) == 0x35

    @pytest.mark.parametrize(
        ("physical_name", "vk"),
        [("/", 0xBF), ("\\", 0xDC), ("`", 0xC0)],
    )
    def test_oem_punctuation_ordinal_maps_to_stable_vk(self, physical_name, vk):
        assert _canon_to_vk(ord(physical_name)) == vk

    def test_unmappable_entry_returns_none(self):
        assert _canon_to_vk(ord("§")) is None


# ---------------------------------------------------------------------------
# Bug A: snapshot_held_modifiers is authoritative on the OS state
# ---------------------------------------------------------------------------

class TestSnapshotUsesOsState:
    def test_snapshot_includes_physically_held_modifier_missing_from_mirror(self):
        """The held-Ctrl press was swallowed during a suppression window, so the
        mirror is empty -- but snapshot must still report Ctrl as held so the
        injector releases it before keystroke typing."""
        m = _make_manager()
        assert m._pressed == set()
        down = {_vk(Key.ctrl_l)}
        m._physical_key_down = lambda vk: vk in down

        held = m.snapshot_held_modifiers()

        assert Key.ctrl_l in held

    def test_snapshot_unions_mirror_and_physical(self):
        """A stale-mirror modifier (physically up) AND a physically-down modifier
        missing from the mirror both appear -- the union never subtracts the
        mirror, preserving the stale-mirror re-press gate."""
        m = _make_manager()
        m._pressed.add(Key.shift_r)  # mirror says shift_r (e.g. physically up)
        down = {_vk(Key.ctrl_l)}     # OS says ctrl_l is down
        m._physical_key_down = lambda vk: vk in down

        held = set(m.snapshot_held_modifiers())

        assert Key.shift_r in held
        assert Key.ctrl_l in held

    def test_snapshot_empty_when_nothing_held(self):
        m = _make_manager()
        m._physical_key_down = lambda vk: False
        assert m.snapshot_held_modifiers() == []


# ---------------------------------------------------------------------------
# Bug B: _reconcile_pressed_with_os repairs the mirror
# ---------------------------------------------------------------------------

class TestReconcilePressedWithOs:
    def test_drops_phantom_keys(self):
        m = _make_manager()
        m._pressed.update({Key.ctrl_l, Key.alt_l, ord("v")})
        m._physical_key_down = lambda vk: False  # nothing physically down

        m._reconcile_pressed_with_os()

        assert m._pressed == set()

    def test_keeps_genuinely_held_and_drops_phantom(self):
        m = _make_manager()
        m._pressed.update({Key.ctrl_l, Key.alt_l, ord("v")})
        down = {_vk(Key.ctrl_l)}  # user still holds Ctrl; alt+v are phantom
        m._physical_key_down = lambda vk: vk in down

        m._reconcile_pressed_with_os()

        assert Key.ctrl_l in m._pressed
        assert Key.alt_l not in m._pressed
        assert ord("v") not in m._pressed

    def test_adds_held_modifier_missing_from_mirror(self):
        m = _make_manager()
        assert m._pressed == set()
        down = {_vk(Key.ctrl_l)}
        m._physical_key_down = lambda vk: vk in down

        m._reconcile_pressed_with_os()

        assert Key.ctrl_l in m._pressed

    def test_leaves_unmappable_phantom_alone(self):
        """An ordinal with no resolvable VK can't be verified, so it is
        conservatively left in place rather than dropped."""
        m = _make_manager()
        m._pressed.add(ord("§"))
        m._physical_key_down = lambda vk: False

        m._reconcile_pressed_with_os()

        assert ord("§") in m._pressed


# ---------------------------------------------------------------------------
# Wiring: closing the suppression window reconciles the mirror
# ---------------------------------------------------------------------------

class TestSuppressionCloseReconciles:
    def test_grace_timer_reconciles_pressed(self, controlled_timers):
        m = _make_manager()
        m._physical_key_down = lambda vk: False
        # Phantom: user pressed ctrl+alt+v then released them while suppression
        # was active, so the releases were swallowed and the mirror still holds.
        m._pressed.update({Key.ctrl_l, Key.alt_l, ord("v")})

        with m.suppress_self_events():
            _release(m, KeyCode.from_char("v"), Key.alt_l, Key.ctrl_l)

        # Mirror is still phantom until the grace timer fires + reconciles.
        assert m._pressed
        assert len(controlled_timers) == 1
        controlled_timers[0].callback()
        assert m._pressed == set()


# ---------------------------------------------------------------------------
# End-to-end Bug B: no spurious paste_last on a bare Ctrl after a paste_last
# whose key releases were swallowed by the suppression window.
# ---------------------------------------------------------------------------

class TestNoSpuriousPasteLast:
    def test_bare_ctrl_does_not_refire_paste_last(self, controlled_timers):
        m = _make_manager()
        cb = MagicMock()
        m.register_extra_hotkey("ctrl+alt+v", cb, key="paste_last")
        m._physical_key_down = lambda vk: False

        # Genuine paste_last fires once.
        _press(m, Key.ctrl_l, Key.alt_l, KeyCode.from_char("v"))
        _flush_queue(m)
        assert cb.call_count == 1

        # The key releases land inside the injection's suppression window and are
        # swallowed -- without reconciliation, alt+v stay phantom-stuck.
        with m.suppress_self_events():
            _release(m, KeyCode.from_char("v"), Key.alt_l, Key.ctrl_l)
        assert len(controlled_timers) == 1
        controlled_timers[0].callback()
        assert m._pressed == set()  # reconciled away the phantom keys

        # Bare Left Ctrl cycles must NOT re-fire paste_last ("every other time").
        _press(m, Key.ctrl_l)
        _flush_queue(m)
        _release(m, Key.ctrl_l)
        _press(m, Key.ctrl_l)
        _flush_queue(m)

        assert cb.call_count == 1, (
            "paste_last re-fired on a bare Ctrl press -- phantom alt+v are stuck "
            "in _pressed (suppression window swallowed their release)"
        )
