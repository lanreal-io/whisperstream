"""Tests for keyboard_shortcuts.py name maps and _parse_key parser.

Covers:
- Win modifier and left/right variants
- Aliases: cmd, super, meta -> win
- Casing / whitespace tolerance
- _KEY_TO_NAME reverse mapping for cmd keys
- Regression: existing ctrl modifier still works
- Mouse middle button mouse3 (cross-platform via Button.middle)
- Mouse side buttons mouse4/mouse5 (skipped when pynput backend lacks x1/x2)
- _parse_shortcut acceptance: mouse-only and keyboard+mouse combos
"""

import logging
import sys
from unittest.mock import MagicMock

import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows shortcut parser tests require the pynput Windows owner",
        allow_module_level=True,
    )

from pynput.keyboard import Key, KeyCode

from whisperstream.keyboard_shortcuts import (
    _GENERIC_MODIFIERS,
    _KEY_TO_NAME,
    _NAME_TO_KEY,
    _MouseButton,
    _MOUSE_MIDDLE,
    _MOUSE_X1,
    _MOUSE_X2,
    _canonical,
    _key_to_display,
    _keycode_ordinal,
    _parse_key,
    KeyboardShortcutManager,
)


def _make_manager() -> KeyboardShortcutManager:
    return KeyboardShortcutManager(logger=logging.getLogger("test"))

# The canonical frozenset that all "win" variants must resolve to.
# Note: on Windows pynput, Key.cmd and Key.cmd_l are the same object, so
# frozenset({Key.cmd, Key.cmd_l, Key.cmd_r}) == frozenset({Key.cmd, Key.cmd_r}).
_WIN_SET = frozenset({Key.cmd, Key.cmd_l, Key.cmd_r})


# ---------------------------------------------------------------------------
# Regression: existing ctrl modifier is unchanged
# ---------------------------------------------------------------------------

def test_parse_ctrl_regression():
    result = _parse_key("ctrl")
    assert result == frozenset({Key.ctrl_l, Key.ctrl_r}), (
        f"Expected ctrl frozenset, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Win modifier (canonical name)
# ---------------------------------------------------------------------------

def test_parse_win_generic():
    assert _parse_key("win") == _WIN_SET

def test_win_in_generic_modifiers():
    assert "win" in _GENERIC_MODIFIERS
    assert _GENERIC_MODIFIERS["win"] == _WIN_SET


# ---------------------------------------------------------------------------
# Aliases: cmd, super, meta all resolve to the win set
# ---------------------------------------------------------------------------

def test_parse_cmd_alias():
    assert _parse_key("cmd") == _WIN_SET

def test_parse_super_alias():
    assert _parse_key("super") == _WIN_SET

def test_parse_meta_alias():
    assert _parse_key("meta") == _WIN_SET


# ---------------------------------------------------------------------------
# Side-specific Win keys
# ---------------------------------------------------------------------------

def test_parse_left_win():
    result = _parse_key("left win")
    assert result == frozenset({Key.cmd_l}), f"Expected {{Key.cmd_l}}, got {result!r}"

def test_parse_right_win():
    result = _parse_key("right win")
    assert result == frozenset({Key.cmd_r}), f"Expected {{Key.cmd_r}}, got {result!r}"

def test_left_win_in_name_to_key():
    assert "left win" in _NAME_TO_KEY
    assert _NAME_TO_KEY["left win"] is Key.cmd_l

def test_right_win_in_name_to_key():
    assert "right win" in _NAME_TO_KEY
    assert _NAME_TO_KEY["right win"] is Key.cmd_r


# ---------------------------------------------------------------------------
# Casing and whitespace tolerance
# ---------------------------------------------------------------------------

def test_parse_win_uppercase():
    assert _parse_key("WIN") == _WIN_SET

def test_parse_win_mixed_case_with_spaces():
    assert _parse_key("  Win  ") == _WIN_SET

def test_parse_cmd_mixed_case():
    assert _parse_key("Cmd") == _WIN_SET


# ---------------------------------------------------------------------------
# _KEY_TO_NAME reverse mapping
# ---------------------------------------------------------------------------

def test_key_to_name_cmd_generic():
    assert _KEY_TO_NAME[Key.cmd] == "win"

def test_key_to_name_cmd_l():
    assert _KEY_TO_NAME[Key.cmd_l] == "win"

def test_key_to_name_cmd_r():
    assert _KEY_TO_NAME[Key.cmd_r] == "right win"


# ---------------------------------------------------------------------------
# Mouse middle button: mouse3 (Button.middle)
#
# Button.middle is on the cross-platform base Button enum (pynput), so these
# tests run on every platform — no skip required.
# ---------------------------------------------------------------------------

def test_parse_mouse3_returns_middle():
    result = _parse_key("mouse3")
    assert result is not None
    assert len(result) == 1
    assert next(iter(result)) is _MOUSE_MIDDLE


def test_parse_mouse3_space_alias():
    """'mouse 3' (space) is an alias for 'mouse3'."""
    assert _parse_key("mouse 3") == _parse_key("mouse3")


def test_parse_mouse3_underscore_alias():
    """'mouse_3' (underscore) is an alias for 'mouse3'."""
    assert _parse_key("mouse_3") == _parse_key("mouse3")


def test_parse_middle_word_alias():
    """'middle' as a word is an alias for 'mouse3'."""
    assert _parse_key("middle") == _parse_key("mouse3")


def test_parse_middle_click_phrase_alias():
    """'middle click' (two words) is an alias for 'mouse3'."""
    assert _parse_key("middle click") == _parse_key("mouse3")


def test_key_to_display_mouse3():
    assert _key_to_display(_MOUSE_MIDDLE) == "mouse3"


# ---------------------------------------------------------------------------
# Mouse side buttons: mouse4 (Button.x1) and mouse5 (Button.x2)
#
# These tests are skipped when the pynput backend does not expose x1/x2
# (macOS/Linux CI runners). On Windows, Button.x1 and Button.x2 are always
# present, so these tests run and must pass.
# ---------------------------------------------------------------------------

_MOUSE_SKIP = pytest.mark.skipif(
    _MOUSE_X1 is None,
    reason="mouse side buttons not available on this pynput backend",
)


@_MOUSE_SKIP
def test_parse_mouse4_returns_x1():
    result = _parse_key("mouse4")
    assert result is not None
    assert len(result) == 1
    assert next(iter(result)) is _MOUSE_X1


@_MOUSE_SKIP
def test_parse_mouse5_returns_x2():
    result = _parse_key("mouse5")
    assert result is not None
    assert len(result) == 1
    assert next(iter(result)) is _MOUSE_X2


@_MOUSE_SKIP
def test_parse_mouse4_space_alias():
    """'mouse 4' (space) is an alias for 'mouse4'."""
    assert _parse_key("mouse 4") == _parse_key("mouse4")


@_MOUSE_SKIP
def test_parse_mouse4_underscore_alias():
    """'mouse_4' (underscore) is an alias for 'mouse4'."""
    assert _parse_key("mouse_4") == _parse_key("mouse4")


@_MOUSE_SKIP
def test_parse_mouse5_underscore_alias():
    """'mouse_5' (underscore) is an alias for 'mouse5'."""
    assert _parse_key("mouse_5") == _parse_key("mouse5")


@_MOUSE_SKIP
def test_key_to_display_mouse4():
    assert _key_to_display(_MOUSE_X1) == "mouse4"


@_MOUSE_SKIP
def test_key_to_display_mouse5():
    assert _key_to_display(_MOUSE_X2) == "mouse5"


@_MOUSE_SKIP
def test_canonical_mouse_button_returns_itself():
    """_canonical on a mouse Button returns the Button itself (already canonical)."""
    assert _canonical(_MOUSE_X1) is _MOUSE_X1
    assert _canonical(_MOUSE_X2) is _MOUSE_X2


# ---------------------------------------------------------------------------
# Task 1.5: _parse_shortcut acceptance tests
#
# _parse_shortcut is an instance method on KeyboardShortcutManager. These
# tests verify that the public-facing multi-key entry point correctly composes
# the per-key parses for mouse-only and keyboard+mouse combos, and that the
# keyboard path is not regressed by mouse changes.
# ---------------------------------------------------------------------------

def test_parse_shortcut_mouse3_only():
    """mouse3 (middle) alone produces a single-frozenset list whose element is Button.middle."""
    m = _make_manager()
    result = m._parse_shortcut("mouse3")
    assert len(result) == 1, f"Expected 1 target set, got {len(result)}: {result!r}"
    assert len(result[0]) == 1, f"Expected 1 element in frozenset, got {result[0]!r}"
    assert next(iter(result[0])) is _MOUSE_MIDDLE


def test_parse_shortcut_ctrl_plus_mouse3():
    """ctrl+mouse3 produces a 2-element list: ctrl-generic set first, then {Button.middle}."""
    m = _make_manager()
    result = m._parse_shortcut("ctrl+mouse3")
    assert len(result) == 2, f"Expected 2 target sets, got {len(result)}: {result!r}"
    ctrl_set = frozenset({Key.ctrl_l, Key.ctrl_r})
    assert result[0] == ctrl_set, (
        f"First frozenset should be ctrl-generic {ctrl_set!r}, got {result[0]!r}"
    )
    assert len(result[1]) == 1, f"Second frozenset should have 1 element, got {result[1]!r}"
    assert next(iter(result[1])) is _MOUSE_MIDDLE


@_MOUSE_SKIP
def test_parse_shortcut_mouse4_only():
    """mouse4 alone produces a single-frozenset list whose element is Button.x1."""
    m = _make_manager()
    result = m._parse_shortcut("mouse4")
    assert len(result) == 1, f"Expected 1 target set, got {len(result)}: {result!r}"
    assert len(result[0]) == 1, f"Expected 1 element in frozenset, got {result[0]!r}"
    assert next(iter(result[0])) is _MOUSE_X1


@_MOUSE_SKIP
def test_parse_shortcut_mouse5_only():
    """mouse5 alone produces a single-frozenset list whose element is Button.x2."""
    m = _make_manager()
    result = m._parse_shortcut("mouse5")
    assert len(result) == 1, f"Expected 1 target set, got {len(result)}: {result!r}"
    assert len(result[0]) == 1, f"Expected 1 element in frozenset, got {result[0]!r}"
    assert next(iter(result[0])) is _MOUSE_X2


@_MOUSE_SKIP
def test_parse_shortcut_ctrl_plus_mouse4():
    """ctrl+mouse4 produces a 2-element list: ctrl-generic set first, then {Button.x1}.

    Order mirrors input order — keyboard part (ctrl) appears before the mouse part
    (mouse4) because ctrl is first in the combo string.
    """
    m = _make_manager()
    result = m._parse_shortcut("ctrl+mouse4")
    assert len(result) == 2, f"Expected 2 target sets, got {len(result)}: {result!r}"
    ctrl_set = frozenset({Key.ctrl_l, Key.ctrl_r})
    assert result[0] == ctrl_set, (
        f"First frozenset should be ctrl-generic {ctrl_set!r}, got {result[0]!r}"
    )
    assert len(result[1]) == 1, f"Second frozenset should have 1 element, got {result[1]!r}"
    assert next(iter(result[1])) is _MOUSE_X1


@_MOUSE_SKIP
def test_parse_shortcut_win_plus_mouse4():
    """win+mouse4 produces a 2-element list: win-generic set first, then {Button.x1}."""
    m = _make_manager()
    result = m._parse_shortcut("win+mouse4")
    assert len(result) == 2, f"Expected 2 target sets, got {len(result)}: {result!r}"
    win_set = frozenset({Key.cmd, Key.cmd_l, Key.cmd_r})
    assert result[0] == win_set, (
        f"First frozenset should be win-generic {win_set!r}, got {result[0]!r}"
    )
    assert len(result[1]) == 1, f"Second frozenset should have 1 element, got {result[1]!r}"
    assert next(iter(result[1])) is _MOUSE_X1


def test_parse_shortcut_right_shift_regression():
    """Regression: 'right shift' still parses to a single-frozenset list [{Key.shift_r}].

    Proves mouse changes did not break the existing keyboard-only path.
    """
    m = _make_manager()
    result = m._parse_shortcut("right shift")
    assert result == [frozenset({Key.shift_r})], (
        f"Expected [frozenset({{Key.shift_r}})], got {result!r}"
    )


def test_parse_shortcut_ctrl_plus_v_regression():
    """Regression: 'ctrl+v' still parses to [ctrl_set, frozenset({ord('v')})].

    Proves the keyboard combo path is still intact after mouse changes.
    """
    m = _make_manager()
    result = m._parse_shortcut("ctrl+v")
    ctrl_set = frozenset({Key.ctrl_l, Key.ctrl_r})
    assert len(result) == 2, f"Expected 2 target sets, got {len(result)}: {result!r}"
    assert result[0] == ctrl_set, (
        f"First frozenset should be ctrl-generic {ctrl_set!r}, got {result[0]!r}"
    )
    assert result[1] == frozenset({ord('v')}), (
        f"Second frozenset should be {{ord('v')}}={frozenset({ord('v')})!r}, got {result[1]!r}"
    )


# ---------------------------------------------------------------------------
# Physical-key canonical is stable across char representations
#
# pynput derives KeyCode.char through the OS layout, so the SAME physical key
# reports a different char depending on which modifiers are held: 'c' alone is
# 'c', under Ctrl it is the control char '\x03', and under Ctrl+Alt it may be
# None. _keycode_ordinal / _canonical key off the stable vk so press and
# release of one key hash identically — otherwise a released letter is never
# discarded from _pressed and an N-key combo degrades into firing on its
# modifiers alone (the "ctrl+alt fires ctrl+alt+c" bug). _key_to_display shares
# the same helper so the stored shortcut name can never disagree with the
# runtime match key.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("char", ["c", "\x03", None])
def test_keycode_ordinal_letter_stable_across_char(char):
    """A letter's ordinal is its vk-derived value regardless of char."""
    assert _keycode_ordinal(KeyCode(vk=67, char=char)) == ord("c")


@pytest.mark.parametrize("char", ["c", "\x03", None])
def test_canonical_letter_stable_across_char(char):
    assert _canonical(KeyCode(vk=67, char=char)) == ord("c")


def test_canonical_press_release_char_mismatch_agree():
    """Press reports char='c', release reports the Ctrl control char for the
    same physical key — both must canonicalize identically so discard() works."""
    press = _canonical(KeyCode(vk=67, char="c"))
    release = _canonical(KeyCode(vk=67, char="\x03"))
    assert press == release == ord("c")


def test_keycode_ordinal_matches_parse_key_letter():
    """_keycode_ordinal of a letter equals the target _parse_key builds for it."""
    assert frozenset({_keycode_ordinal(KeyCode(vk=67, char="c"))}) == _parse_key("c")


def test_keycode_ordinal_digit_uses_vk_not_shifted_symbol():
    """Shift+5 reports char='%' but vk stays 53 → ordinal must be the digit."""
    assert _keycode_ordinal(KeyCode(vk=53, char="%")) == ord("5")


@pytest.mark.parametrize(
    ("vk", "layout_char", "physical_name"),
    [
        (0xBA, "ö", ";"),
        (0xBB, "´", "="),
        (0xBC, ";", ","),
        (0xBD, "+", "-"),
        (0xBE, ":", "."),
        (0xBF, "½", "/"),
        (0xC0, "à", "`"),
        (0xDB, "å", "["),
        (0xDC, "»", "\\"),
        (0xDD, "^", "]"),
        (0xDE, "ä", "'"),
    ],
)
def test_windows_oem_punctuation_uses_stable_physical_name(
    vk: int, layout_char: str, physical_name: str,
):
    key = KeyCode(vk=vk, char=layout_char)

    assert _keycode_ordinal(key) == ord(physical_name)
    assert _canonical(key) == ord(physical_name)
    assert _key_to_display(key) == physical_name
    assert _canonical(key) in _parse_key(physical_name)


def test_keycode_ordinal_falls_back_to_char_outside_known_vk_rows():
    assert _keycode_ordinal(KeyCode(vk=0xE1, char="§")) == ord("§")


def test_keycode_ordinal_none_when_no_vk_and_no_char():
    assert _keycode_ordinal(KeyCode(vk=None, char=None)) is None


@pytest.mark.parametrize("char", ["c", "\x03", None])
def test_key_to_display_letter_stable_across_char(char):
    """Display name and match key share _keycode_ordinal, so a Ctrl control
    char never leaks into the displayed shortcut string."""
    assert _key_to_display(KeyCode(vk=67, char=char)) == "c"


@pytest.mark.parametrize(
    ("vk", "press_char", "release_char", "physical_name"),
    [
        (0xBF, "½", "/", "/"),
        (0xDC, "»", "\\", "\\"),
        (0xC0, "à", None, "`"),
    ],
)
def test_capture_normalizes_layout_characters_to_physical_punctuation(
    vk: int,
    press_char: str,
    release_char: str | None,
    physical_name: str,
):
    manager = _make_manager()
    manager._queue = MagicMock()
    changed = MagicMock()
    captured = MagicMock()
    manager._capture_active = True
    manager._capture_on_changed = changed
    manager._capture_on_captured = captured
    manager._mode = "capture"

    manager._on_press(Key.ctrl_l)
    manager._on_press(Key.alt_l)
    manager._on_press(KeyCode(vk=vk, char=press_char))
    manager._on_release(KeyCode(vk=vk, char=release_char))
    manager._on_release(Key.alt_l)
    manager._on_release(Key.ctrl_l)

    queued = [call.args[0] for call in manager._queue.put.call_args_list]
    for callback in queued:
        callback()

    changed.assert_any_call(f"ctrl+alt+{physical_name}")
    captured.assert_called_once_with(f"ctrl+alt+{physical_name}")
