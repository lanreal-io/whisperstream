"""Prevent unit-test regressions that attach hooks to the user's desktop."""

import logging
import sys

import pytest

guarded_desktop = pytest.mark.skipif(
    sys.platform not in ("win32", "darwin"), reason="Windows/macOS desktop input isolation"
)


@guarded_desktop
@pytest.mark.parametrize("device", ["keyboard", "mouse"])
def test_native_listener_start_is_rejected_before_creating_a_thread(device):
    from pynput import keyboard, mouse

    listener = {"keyboard": keyboard, "mouse": mouse}[device].Listener()
    with pytest.raises(RuntimeError, match="Unit tests must not start global input"):
        listener.start()
    assert listener.ident is None
    assert not listener.is_alive()


@guarded_desktop
def test_macos_event_tap_start_is_rejected_before_creating_a_thread():
    from whisperstream.macos_shortcuts import _CoreGraphicsEventListener

    def unexpected_api():
        raise AssertionError("event tap API must not be created")

    listener = _CoreGraphicsEventListener(lambda event: False, api_factory=unexpected_api)
    with pytest.raises(RuntimeError, match="Unit tests must not start global input"):
        listener.start()
    assert listener._thread.ident is None
    assert not listener.is_alive()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcut adapter listener isolation")
def test_listener_doubles_preserve_registration_and_cleanup(mock_shortcut_listeners):
    from whisperstream.keyboard_shortcuts import KeyboardShortcutManager

    manager = KeyboardShortcutManager(logger=logging.getLogger("test.input_isolation"))
    manager.register_extra_hotkey("ctrl+alt+v", lambda: None)
    assert manager._listener in mock_shortcut_listeners
    assert mock_shortcut_listeners
    for listener in mock_shortcut_listeners:
        listener.start.assert_called_once_with()
    manager.unhook_all()
    for listener in mock_shortcut_listeners:
        listener.stop.assert_called_once_with()
