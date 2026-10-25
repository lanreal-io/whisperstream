"""Tests for KeyboardShortcutManager.suppress_self_events() — the guard
that prevents synthetic keystrokes (from send() or direct Controller
calls) from re-entering the hotkey logic.

Bug fixed: with PTT bound to ``shift`` / ``left shift`` / ``insert``, the
shift+insert paste combo we send during text injection was firing the
push-to-talk handler a second time (phantom recording right after the
real one ends). With the guard in place, those self-injected events are
dropped at _on_press / _on_release.
"""

import logging
import sys
import threading
from collections.abc import Callable

import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows pynput self-event suppression tests",
        allow_module_level=True,
    )

from pynput.keyboard import Key

from whisperstream import keyboard_shortcuts
from whisperstream.keyboard_shortcuts import (
    KeyboardShortcutManager,
    _SUPPRESS_GRACE_S,
)


def _make_manager() -> KeyboardShortcutManager:
    return KeyboardShortcutManager(logger=logging.getLogger("test"))


class _ControlledTimer:
    """Timer double whose callback runs only when the test explicitly fires it."""

    def __init__(self, delay_s: float, callback: Callable[[], None]) -> None:
        self.delay_s = delay_s
        self.callback = callback
        self.daemon = False
        self.started = False
        self.fired = False

    def start(self) -> None:
        self.started = True

    def fire(self) -> None:
        assert self.started
        assert not self.fired
        self.fired = True
        self.callback()


@pytest.fixture(autouse=True)
def controlled_timers(monkeypatch) -> list[_ControlledTimer]:
    """Capture suppression timers without starting uncontrolled Timer threads."""
    timers: list[_ControlledTimer] = []
    timers_lock = threading.Lock()

    def make_timer(
        delay_s: float, callback: Callable[[], None],
    ) -> _ControlledTimer:
        timer = _ControlledTimer(delay_s, callback)
        with timers_lock:
            timers.append(timer)
        return timer

    monkeypatch.setattr(keyboard_shortcuts, "_new_suppress_timer", make_timer)
    return timers


# ---------------------------------------------------------------------------
# Depth counter mechanics
# ---------------------------------------------------------------------------

def test_initial_suppress_depth_is_zero():
    m = _make_manager()
    assert m._suppress_depth == 0


def test_enter_increments_depth_immediately():
    m = _make_manager()
    with m.suppress_self_events():
        assert m._suppress_depth == 1


def test_exit_decrements_depth_after_grace_window(controlled_timers):
    m = _make_manager()
    with m.suppress_self_events():
        pass
    # Depth must NOT drop synchronously — the grace window keeps it at 1
    # until the Timer fires, so trailing-edge pynput events still get
    # filtered.
    assert m._suppress_depth == 1
    assert len(controlled_timers) == 1
    timer = controlled_timers[0]
    assert _SUPPRESS_GRACE_S == 0.05
    assert timer.delay_s == _SUPPRESS_GRACE_S
    assert timer.daemon is True
    assert timer.started is True

    timer.fire()

    assert m._suppress_depth == 0


def test_nested_blocks_stack_depths(controlled_timers):
    m = _make_manager()
    with m.suppress_self_events():
        assert m._suppress_depth == 1
        with m.suppress_self_events():
            assert m._suppress_depth == 2
        # Inner exit schedules a delayed dec; depth still 2 immediately.
        assert m._suppress_depth == 2
        assert len(controlled_timers) == 1
    assert len(controlled_timers) == 2

    controlled_timers[0].fire()
    assert m._suppress_depth == 1
    controlled_timers[1].fire()

    assert m._suppress_depth == 0


def test_release_one_handles_underflow():
    m = _make_manager()
    # Direct call when depth is already 0 should not go negative.
    m._suppress_release_one()
    assert m._suppress_depth == 0


# ---------------------------------------------------------------------------
# _on_press / _on_release behavior under suppression
# ---------------------------------------------------------------------------

def test_on_press_drops_events_during_suppression():
    m = _make_manager()
    with m.suppress_self_events():
        m._on_press(Key.shift)
        m._on_press(Key.insert)
        # Both events must be ignored — _pressed stays empty.
        assert Key.shift not in m._pressed
        assert Key.insert not in m._pressed


def test_on_release_drops_events_during_suppression():
    m = _make_manager()
    # Pre-populate as if user is holding shift before the guard opens.
    m._pressed.add(Key.shift)
    with m.suppress_self_events():
        m._on_release(Key.shift)
        # Synthetic release must not pop a key the user is still holding.
        assert Key.shift in m._pressed


def test_press_works_normally_after_grace_window(controlled_timers):
    m = _make_manager()
    with m.suppress_self_events():
        pass
    controlled_timers[0].fire()
    m._on_press(Key.shift)
    assert Key.shift in m._pressed


def test_press_during_grace_window_is_still_dropped():
    """Trailing-edge events delivered after the with-block exits but
    before the grace timer fires must still be suppressed — that is the
    whole point of the grace window."""
    m = _make_manager()
    with m.suppress_self_events():
        pass
    # Inside the grace window: the OS thread might deliver a lagged event.
    m._on_press(Key.insert)
    assert Key.insert not in m._pressed


# ---------------------------------------------------------------------------
# Integration with send()
# ---------------------------------------------------------------------------

def test_send_does_not_re_enter_hotkey_logic_for_shift_insert(monkeypatch):
    """The original bug case: send('shift+insert') used to flip
    _pressed via the keyboard listener and fire registered hotkeys.
    With the guard in send(), the synthetic press/release sequence
    must not touch _pressed even though our Controller fakes still
    invoke pynput's listener path conceptually.

    We can't drive the OS listener in a unit test, so we simulate the
    re-entry by calling _on_press / _on_release ourselves while inside
    the same call (which is what the OS would do via the hook thread).
    """
    m = _make_manager()

    # Replace Controller calls with a stub that also synthesizes the
    # listener callback (modeling pynput's behavior of routing synthetic
    # presses through the same low-level hook on Windows).
    pressed_via_controller: list = []
    released_via_controller: list = []

    class _StubController:
        def press(self, k):
            pressed_via_controller.append(k)
            m._on_press(k)  # simulate pynput hook re-entry

        def release(self, k):
            released_via_controller.append(k)
            m._on_release(k)

    monkeypatch.setattr(m, "_controller", _StubController())

    m.send("shift+insert")

    # Controller saw the calls...
    assert len(pressed_via_controller) == 2
    assert len(released_via_controller) == 2
    # ...but the listener path was suppressed, so _pressed is clean.
    assert Key.shift not in m._pressed
    assert Key.insert not in m._pressed


def test_text_injector_outer_block_protects_direct_controller_calls(monkeypatch):
    """text_injector wraps both send() AND direct Controller.press /
    Controller.release (used by the modifier-release guard). The outer
    suppress block must protect those direct calls too — otherwise the
    pre-paste modifier release leaks back into _pressed."""
    m = _make_manager()
    # User is holding shift (saved as a "held" modifier in text_injector).
    m._pressed.add(Key.shift)

    class _StubController:
        def press(self, k):
            m._on_press(k)

        def release(self, k):
            m._on_release(k)

    monkeypatch.setattr(m, "_controller", _StubController())

    # Mirror text_injector.inject's structure: outer suppress block,
    # release the held modifier, send the combo, re-press if still held.
    with m.suppress_self_events():
        m._controller.release(Key.shift)
        m.send("shift+insert")
        if Key.shift in m._pressed:
            m._controller.press(Key.shift)

    # The synthetic activity must not have changed user-held state.
    assert Key.shift in m._pressed


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

def test_concurrent_suppress_blocks_do_not_corrupt_depth(controlled_timers):
    """Many threads entering and exiting the guard concurrently should
    leave the depth at 0 once controlled callbacks fire concurrently."""
    m = _make_manager()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(10):
            with m.suppress_self_events():
                pass

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert m._suppress_depth == 80
    assert len(controlled_timers) == 80

    # Eight workers explicitly fire all 80 callbacks. This retains concurrent
    # decrement coverage without creating uncontrolled Timer threads.
    fire_barrier = threading.Barrier(8)

    def fire_timers(timers: list[_ControlledTimer]) -> None:
        fire_barrier.wait()
        for timer in timers:
            timer.fire()

    fire_threads = [
        threading.Thread(target=fire_timers, args=(controlled_timers[i::8],))
        for i in range(8)
    ]
    for thread in fire_threads:
        thread.start()
    for thread in fire_threads:
        thread.join()

    assert m._suppress_depth == 0
    assert all(timer.fired for timer in controlled_timers)
