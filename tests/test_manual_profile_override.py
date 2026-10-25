"""Tests for the per-profile manual-switch override state machine (issue 14).

Covers the ``WhisperstreamApp`` override logic without spinning up the full app
(no model load, no audio, no hotkey listener). The three override methods
(``set_manual_profile``, ``clear_manual_profile``, ``_resolve_active_profile``)
plus ``_profile_name`` are pure w.r.t. a small attribute surface
(``config``, ``logger``, ``_dictation_app``, ``_manual_profile_id``,
``_override_anchor``, ``_broadcast``), so they are exercised by binding the
unbound class methods to a lightweight stand-in object.

State machine asserted:
- set -> same-app dictation uses the override (overrides auto-routing);
- set -> different-app dictation clears the override and auto-routes;
- a cleared override falls back to ``resolve_profile_for_target``;
- setting an unknown profile id is ignored;
- a deleted override profile clears at resolution time;
- set / clear broadcast the right ``active_profile_override`` payloads.
"""

import logging
import types
import unittest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.foreground_window import RoutingTarget
from whisperstream.main import WhisperstreamApp


def _app_window(process_name: str | None, domain: str | None = None):
    """Build a minimum routing target (the focused app) or None."""
    if process_name is None:
        return None
    return RoutingTarget(process_name=process_name, domain=domain)


def _config():
    return WhisperstreamConfig(
        profiles=[
            {"id": "chat", "name": "Chat", "prompt": "c"},
            {"id": "code", "name": "Code", "prompt": "k"},
        ],
        default_profile_id="chat",
        power_mode_map={"code.exe": "code"},
        site_map={},
    )


class _Stub:
    """Minimal object exposing the attribute surface the override methods touch.

    The override methods are bound from WhisperstreamApp so the tested logic is
    the real code, not a reimplementation.
    """

    def __init__(self, config, focused):
        self.config = config
        self.logger = logging.getLogger("test.override")
        self._dictation_app = focused
        self._manual_profile_id = None
        self._override_anchor = None
        self.events = []

    def _broadcast(self, event):
        self.events.append(event)

    # Bind the real methods onto the stub.
    _profile_name = WhisperstreamApp._profile_name
    set_manual_profile = WhisperstreamApp.set_manual_profile
    clear_manual_profile = WhisperstreamApp.clear_manual_profile
    _resolve_active_profile = WhisperstreamApp._resolve_active_profile


def _make(focused_at_record, *, monkeypatch_focus=None):
    """Build a stub whose recording-time foreground app is ``focused_at_record``.

    ``monkeypatch_focus`` (a RoutingTarget or None) overrides what
    ``detect_foreground_routing_target`` returns at hotkey-press time. When omitted, the
    press-time focus equals ``focused_at_record`` (the common single-app case).
    """
    stub = _Stub(_config(), focused_at_record)
    return stub


class TestSetAndResolve(unittest.TestCase):
    def setUp(self):
        # Patch the process-only routing probe so manual anchoring is deterministic.
        # is deterministic. Restored in tearDown.
        import whisperstream.main as main_mod

        self._main_mod = main_mod
        self._orig_detect = main_mod.detect_foreground_routing_target

    def tearDown(self):
        self._main_mod.detect_foreground_routing_target = self._orig_detect

    def _set_focus_at_press(self, app):
        self._main_mod.detect_foreground_routing_target = (
            lambda *, include_browser_domain=False: app
        )

    def test_set_then_same_app_uses_override(self):
        """Press the hotkey while Slack is focused; dictating in Slack uses the
        override profile, overriding the auto-route (slack.exe is unmapped, so
        auto would return the default)."""
        slack = _app_window("slack.exe")
        stub = _make(slack)
        self._set_focus_at_press(slack)

        stub.set_manual_profile("code")  # anchor = slack.exe
        self.assertEqual(stub._manual_profile_id, "code")
        self.assertEqual(stub._override_anchor, "slack.exe")

        # Dictation fires while Slack is still focused -> override wins.
        self.assertEqual(stub._resolve_active_profile(), "code")
        # Override still active (same-app keeps it sticky).
        self.assertEqual(stub._manual_profile_id, "code")

    def test_set_then_different_app_clears_and_auto_routes(self):
        """Press while Slack focused, but dictate in Code: the override clears and
        auto-routing resumes (code.exe -> code via power_mode_map)."""
        slack = _app_window("slack.exe")
        stub = _make(_app_window("code.exe"))
        self._set_focus_at_press(slack)

        stub.set_manual_profile("chat")  # anchor = slack.exe
        # At record time the focus is code.exe (!= anchor) -> override drops.
        self.assertEqual(stub._resolve_active_profile(), "code")
        self.assertIsNone(stub._manual_profile_id)
        self.assertIsNone(stub._override_anchor)
        # A cleared broadcast was emitted.
        self.assertIn(
            {"type": "active_profile_override", "profile_id": None}, stub.events
        )

    def test_cleared_override_falls_back_to_resolver(self):
        """With no override set, resolution is pure resolve_profile_for_target."""
        stub = _make(_app_window("code.exe"))
        self.assertEqual(stub._resolve_active_profile(), "code")
        stub2 = _make(_app_window("unmapped.exe"))
        # Unmapped app -> None (caller uses the global default).
        self.assertIsNone(stub2._resolve_active_profile())

    def test_set_unknown_profile_id_is_ignored(self):
        """Setting a hotkey to a deleted/unknown profile id is a no-op."""
        slack = _app_window("slack.exe")
        stub = _make(slack)
        self._set_focus_at_press(slack)

        stub.set_manual_profile("ghost")
        self.assertIsNone(stub._manual_profile_id)
        self.assertEqual(stub.events, [])  # no broadcast for an ignored set

    def test_override_profile_deleted_clears_at_resolution(self):
        """If the override profile vanishes from config before the next dictation
        (same app still focused), resolution drops the override and auto-routes."""
        slack = _app_window("slack.exe")
        stub = _make(slack)
        self._set_focus_at_press(slack)
        stub.set_manual_profile("code")  # anchor slack.exe

        # Profile "code" deleted from config.
        stub.config.profiles = [{"id": "chat", "name": "Chat", "prompt": "c"}]
        # slack.exe is unmapped -> auto returns None (global default).
        self.assertIsNone(stub._resolve_active_profile())
        self.assertIsNone(stub._manual_profile_id)

    def test_set_broadcasts_active_payload(self):
        slack = _app_window("slack.exe")
        stub = _make(slack)
        self._set_focus_at_press(slack)
        stub.set_manual_profile("code")
        self.assertEqual(
            stub.events[-1],
            {
                "type": "active_profile_override",
                "profile_id": "code",
                "name": "Code",
                "source": "hotkey",
            },
        )

    def test_clear_broadcasts_null_payload(self):
        slack = _app_window("slack.exe")
        stub = _make(slack)
        self._set_focus_at_press(slack)
        stub.set_manual_profile("code")
        stub.events.clear()
        stub.clear_manual_profile()
        self.assertEqual(
            stub.events, [{"type": "active_profile_override", "profile_id": None}]
        )

    def test_clear_when_nothing_set_is_noop(self):
        stub = _make(_app_window("code.exe"))
        stub.clear_manual_profile()
        self.assertEqual(stub.events, [])

    def test_no_foreground_app_at_press_never_sticks(self):
        """If no app is focused at press (anchor None), a later dictation in any
        app differs from the None anchor, so the override never applies."""
        stub = _make(_app_window("code.exe"))
        self._set_focus_at_press(None)  # no foreground app at press
        stub.set_manual_profile("chat")
        self.assertIsNone(stub._override_anchor)
        # code.exe != None anchor -> override drops, auto-route to code.
        self.assertEqual(stub._resolve_active_profile(), "code")


if __name__ == "__main__":
    unittest.main()
