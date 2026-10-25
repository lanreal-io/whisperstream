"""Tests for KeyboardShortcutManager.type_text().

Covers:
(a) delay_ms=0 fast path: Controller.type(text) called once, time.sleep NOT called
(b) delay_ms=10 paced path: time.sleep(0.01) called once per char
(c) whole operation runs inside suppress_self_events guard (_suppress_depth > 0 during call)
(d) clipboard untouched: pyperclip patched into the module is never called
(e) empty text is a no-op

Caps Lock fix (K1):
(f) caps ON -> unicode path used, Controller.type NOT called
(g) caps OFF -> Controller.type hybrid used (unicode path NOT taken)
(h) delay pacing applies in the caps-ON path (one sleep per char)
(i) empty text no-op also holds when caps lock is on
"""

import ctypes
import logging
import sys
import unittest.mock as mock

import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows pynput text-delivery tests",
        allow_module_level=True,
    )

from whisperstream.keyboard_shortcuts import KeyboardShortcutManager
import whisperstream.keyboard_shortcuts as ks_module


def _make_manager() -> KeyboardShortcutManager:
    return KeyboardShortcutManager(logger=logging.getLogger("test"))


# ---------------------------------------------------------------------------
# (a) delay_ms=0: Controller.type(text) called once, time.sleep NOT called
# ---------------------------------------------------------------------------

def test_type_text_fast_path_calls_controller_type_once():
    """delay_ms=0 should call Controller.type(text) exactly once (full string, not per-char)."""
    mgr = _make_manager()
    with mock.patch.object(mgr._controller, "type") as mock_type, \
         mock.patch.object(ks_module.time, "sleep") as mock_sleep:
        mgr.type_text("hello world", delay_ms=0)
    mock_type.assert_called_once_with("hello world")
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# (b) delay_ms=10: time.sleep(0.01) called once per char
# ---------------------------------------------------------------------------

def test_type_text_paced_path_sleeps_per_char():
    """delay_ms=10 should call Controller.type(ch) + time.sleep(0.01) for each char."""
    mgr = _make_manager()
    text = "hi"
    sleep_calls = []

    def capture_sleep(s):
        sleep_calls.append(s)

    with mock.patch.object(mgr._controller, "type") as mock_type, \
         mock.patch.object(ks_module.time, "sleep", side_effect=capture_sleep):
        mgr.type_text(text, delay_ms=10)

    # one type call per character
    assert mock_type.call_count == len(text), (
        f"Expected {len(text)} type calls (one per char), got {mock_type.call_count}"
    )
    assert mock_type.call_args_list == [mock.call("h"), mock.call("i")]

    # one sleep per character at delay_ms/1000
    assert len(sleep_calls) == len(text), (
        f"Expected {len(text)} sleep calls, got {len(sleep_calls)}"
    )
    for s in sleep_calls:
        assert abs(s - 0.01) < 1e-9, f"Expected sleep(0.01), got sleep({s})"


# ---------------------------------------------------------------------------
# (c) whole operation runs inside suppress_self_events (_suppress_depth > 0)
# ---------------------------------------------------------------------------

def test_type_text_runs_inside_suppress_guard():
    """The Controller.type call must happen while _suppress_depth > 0."""
    mgr = _make_manager()
    depths_during_call = []

    def capture_depth(text_arg):
        depths_during_call.append(mgr._suppress_depth)

    with mock.patch.object(mgr._controller, "type", side_effect=capture_depth):
        mgr.type_text("abc", delay_ms=0)

    assert depths_during_call, "Controller.type was never called"
    for d in depths_during_call:
        assert d > 0, f"Expected _suppress_depth > 0 during type, got {d}"


# ---------------------------------------------------------------------------
# (d) clipboard untouched: pyperclip patched into module is never called
# ---------------------------------------------------------------------------

def test_type_text_never_touches_clipboard():
    """type_text must never call pyperclip.copy or pyperclip.paste."""
    mgr = _make_manager()
    mock_pyperclip = mock.MagicMock()

    with mock.patch.object(mgr._controller, "type"), \
         mock.patch.dict("sys.modules", {"pyperclip": mock_pyperclip}):
        mgr.type_text("some text", delay_ms=0)

    mock_pyperclip.copy.assert_not_called()
    mock_pyperclip.paste.assert_not_called()


# ---------------------------------------------------------------------------
# (e) empty text is a no-op
# ---------------------------------------------------------------------------

def test_type_text_empty_is_noop():
    """type_text('') must not call Controller.type or time.sleep at all."""
    mgr = _make_manager()
    with mock.patch.object(mgr._controller, "type") as mock_type, \
         mock.patch.object(ks_module.time, "sleep") as mock_sleep:
        mgr.type_text("", delay_ms=0)
        mgr.type_text("", delay_ms=10)

    mock_type.assert_not_called()
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Caps Lock fix (K1)
# ---------------------------------------------------------------------------

def _patch_caps_on(mgr):
    """Patch the caps-lock getter on ks_module to return True, and the
    unicode-inject helper so calls are recorded but no real SendInput fires."""
    caps_patch = mock.patch.object(ks_module, "_caps_lock_on", return_value=True)
    inject_patch = mock.patch.object(ks_module, "_inject_unicode_char")
    return caps_patch, inject_patch


def _patch_caps_off(mgr):
    """Patch the caps-lock getter on ks_module to return False."""
    return mock.patch.object(ks_module, "_caps_lock_on", return_value=False)


# (f) caps ON -> unicode path used, Controller.type NOT called
def test_type_text_caps_on_uses_unicode_path():
    """When Caps Lock is on, type_text must use the unicode-inject path for each
    char and must NOT call Controller.type at all."""
    mgr = _make_manager()
    caps_patch, inject_patch = _patch_caps_on(mgr)
    with caps_patch, inject_patch as mock_inject, \
         mock.patch.object(mgr._controller, "type") as mock_type, \
         mock.patch.object(ks_module.time, "sleep"):
        mgr.type_text("ab", delay_ms=0)

    # unicode injector called once per char
    assert mock_inject.call_count == 2, (
        f"Expected 2 unicode-inject calls (one per char), got {mock_inject.call_count}"
    )
    chars_injected = [call.args[0] for call in mock_inject.call_args_list]
    assert chars_injected == ["a", "b"], f"Expected ['a','b'], got {chars_injected}"

    # Controller.type must NOT be called in the caps-ON path
    mock_type.assert_not_called()


# (g) caps OFF -> Controller.type hybrid used, unicode path NOT taken
def test_type_text_caps_off_uses_controller_type():
    """When Caps Lock is off, type_text must call Controller.type and must NOT
    call the unicode-inject helper."""
    mgr = _make_manager()
    with _patch_caps_off(mgr), \
         mock.patch.object(ks_module, "_inject_unicode_char") as mock_inject, \
         mock.patch.object(mgr._controller, "type") as mock_type, \
         mock.patch.object(ks_module.time, "sleep"):
        mgr.type_text("hello", delay_ms=0)

    mock_type.assert_called_once_with("hello")
    mock_inject.assert_not_called()


# (h) delay pacing applies in the caps-ON path (one sleep per char)
def test_type_text_caps_on_pacing_sleeps_per_char():
    """With delay_ms=10 and Caps Lock on, one sleep(0.01) must fire per char."""
    mgr = _make_manager()
    text = "xy"
    sleep_calls = []

    def capture_sleep(s):
        sleep_calls.append(s)

    caps_patch, inject_patch = _patch_caps_on(mgr)
    with caps_patch, inject_patch, \
         mock.patch.object(mgr._controller, "type"), \
         mock.patch.object(ks_module.time, "sleep", side_effect=capture_sleep):
        mgr.type_text(text, delay_ms=10)

    assert len(sleep_calls) == len(text), (
        f"Expected {len(text)} sleep calls in caps-ON paced path, got {len(sleep_calls)}"
    )
    for s in sleep_calls:
        assert abs(s - 0.01) < 1e-9, f"Expected sleep(0.01), got sleep({s})"


# (i) empty text no-op also holds when caps lock is on
def test_type_text_empty_is_noop_caps_on():
    """type_text('') with Caps Lock on must still be a complete no-op."""
    mgr = _make_manager()
    caps_patch, inject_patch = _patch_caps_on(mgr)
    with caps_patch, inject_patch as mock_inject, \
         mock.patch.object(mgr._controller, "type") as mock_type, \
         mock.patch.object(ks_module.time, "sleep") as mock_sleep:
        mgr.type_text("", delay_ms=0)
        mgr.type_text("", delay_ms=10)

    mock_inject.assert_not_called()
    mock_type.assert_not_called()
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# modifier_physically_down: real OS key state (GetAsyncKeyState), immune to a
# stale _pressed mirror. Gates the modifier-guard re-press so a key the user
# released mid-injection is never re-pressed (the PTT/NVDA stuck-shift bug).
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="GetAsyncKeyState is Windows-only")
def test_modifier_physically_down_true_when_high_bit_set():
    """High-order bit (0x8000) set => key is physically down => True. The VK
    passed to GetAsyncKeyState must be the side-specific code (VK_RSHIFT)."""
    from pynput.keyboard import Key
    mgr = _make_manager()
    with mock.patch.object(
        ks_module.ctypes.windll.user32, "GetAsyncKeyState", return_value=0x8001
    ) as gaks:
        assert mgr.modifier_physically_down(Key.shift_r) is True
    gaks.assert_called_once_with(0xA1)  # VK_RSHIFT


@pytest.mark.skipif(sys.platform != "win32", reason="GetAsyncKeyState is Windows-only")
def test_modifier_physically_down_false_when_high_bit_clear():
    """Only the low 'toggled since last call' bit set (0x0001), high bit clear
    => key is up => False (this is exactly the stale-mirror case)."""
    from pynput.keyboard import Key
    mgr = _make_manager()
    with mock.patch.object(
        ks_module.ctypes.windll.user32, "GetAsyncKeyState", return_value=0x0001
    ):
        assert mgr.modifier_physically_down(Key.shift_r) is False


@pytest.mark.skipif(sys.platform != "win32", reason="GetAsyncKeyState is Windows-only")
def test_modifier_physically_down_never_raises():
    """Any error from the OS call is swallowed and returns False (never crashes
    the inject path)."""
    from pynput.keyboard import Key
    mgr = _make_manager()
    with mock.patch.object(
        ks_module.ctypes.windll.user32, "GetAsyncKeyState", side_effect=OSError("boom")
    ):
        assert mgr.modifier_physically_down(Key.shift_r) is False
