"""Tests for WhisperstreamApp._effective_input_style resolution (Task 1.5 + K2).

Task 1.5 assertions:
(a) A profile with input_style="keystroke" wins over global "clipboard".
(b) A profile without the key inherits the global default.
(c) profile_id=None with default_profile_id pointing at a keystroke profile
    resolves to "keystroke" (proves do_enhance-independence).
(d) An unknown per-profile value (not in {"clipboard","keystroke"}) falls back
    to the global default.

Task K2 assertions (screen-reader auto-default + dirty flag):
(e) screen_reader_active() never raises (try/except).
(f) screen_reader_active() honors WHISPERSTREAM_FORCE_SCREEN_READER=1/0.
(g) not-user-set + SR forced True -> effective global = "keystroke".
(h) not-user-set + SR False -> effective global = "clipboard".
(i) user_set=True + input_style="clipboard" + SR True -> "clipboard" (explicit wins).
(j) per-profile keystroke wins regardless of dirty flag + SR state.
(k) /api/config response includes screen_reader_detected (bool).
"""

import os
import unittest
from unittest.mock import MagicMock, patch


def _make_config(
    *,
    global_input_style: str = "clipboard",
    profiles: list | None = None,
    default_profile_id: str = "",
) -> MagicMock:
    """Build a minimal stub WhisperstreamConfig for resolution tests."""
    cfg = MagicMock()
    cfg.input_style = global_input_style
    cfg.profiles = profiles or []
    cfg.default_profile_id = default_profile_id
    # Fields required by WhisperstreamApp.__init__ that we stub out:
    cfg.word_filters = []
    cfg.word_replacements = {}
    cfg.post_processing = ""
    cfg.clipboard_behavior = False
    cfg.clipboard_clear_delay = 0.0
    cfg.keystroke_delay_ms = 0
    cfg.parakeet_model = "parakeet-tdt-0.6b-v2"
    cfg.parakeet_quantization = ""
    cfg.hotword_score = 20.0
    cfg.beam_width = 4
    cfg.word_boosts = []
    cfg.profile_hotkeys = {}
    cfg.power_mode_map = {}
    cfg.site_map = {}
    cfg.enhancement_enabled = False
    cfg.enhancement_provider = "cloud"
    cfg.enhancement_chunked_cleanup = False
    cfg.language = "en"
    cfg.primary_shortcut = ""
    cfg.secondary_shortcut = ""
    cfg.toggle_shortcut = ""
    cfg.extra_shortcut = ""
    cfg.recording_mode = "push_to_talk"
    return cfg


def _make_app(cfg: MagicMock) -> object:
    """Construct a WhisperstreamApp with heavy dependencies mocked out.

    We patch out the pieces that hit hardware or disk so the constructor
    completes in unit-test environments.
    """
    from whisperstream.main import WhisperstreamApp

    with (
        patch("whisperstream.main.ConfigManager") as mock_cm_cls,
        patch("whisperstream.main.get_logger", return_value=MagicMock()),
        patch("whisperstream.main.get_log_dir", return_value="/tmp"),
        patch("whisperstream.main.KeyboardShortcutManager", return_value=MagicMock()),
        patch("whisperstream.main.LLMEnhancer", return_value=MagicMock()),
        patch("whisperstream.main.LicenseManager", return_value=MagicMock()),
        patch("whisperstream.main.ReviewState", return_value=MagicMock()),
        patch("whisperstream.main.TranscriptStore", return_value=MagicMock()),
        patch("whisperstream.main.AudioFeedback", return_value=MagicMock()),
        patch("whisperstream.main.TextInjector", return_value=MagicMock()),
    ):
        mock_cm = MagicMock()
        mock_cm.load.return_value = cfg
        mock_cm.model_dir.return_value = "/tmp/models"
        mock_cm_cls.return_value = mock_cm

        app = WhisperstreamApp.__new__(WhisperstreamApp)
        app.logger = MagicMock()
        app.config = cfg
        app.config_manager = mock_cm
        app._model_dir = "/tmp/models"
        app._headless = True
        app._manual_profile_id = None
        app._override_anchor = None
        app._dictation_app = None
        return app


class TestEffectiveInputStyle(unittest.TestCase):
    """Resolution logic for _effective_input_style."""

    def _resolve(self, app, profile_id):
        """Call the method under test."""
        return app._effective_input_style(profile_id)

    def test_profile_keystroke_beats_global_clipboard(self):
        """(a) A profile whose input_style="keystroke" overrides global "clipboard"."""
        cfg = _make_config(
            global_input_style="clipboard",
            profiles=[{"id": "p1", "name": "P1", "prompt": "", "input_style": "keystroke"}],
        )
        app = _make_app(cfg)
        result = self._resolve(app, "p1")
        self.assertEqual(result, "keystroke")

    def test_profile_without_input_style_inherits_global(self):
        """(b) A profile with no input_style key falls back to the global default."""
        cfg = _make_config(
            global_input_style="keystroke",
            profiles=[{"id": "p2", "name": "P2", "prompt": ""}],
        )
        app = _make_app(cfg)
        result = self._resolve(app, "p2")
        self.assertEqual(result, "keystroke")

    def test_none_profile_id_resolves_via_default_profile_id(self):
        """(c) profile_id=None resolves via default_profile_id (do_enhance-independence).

        The raw inject path sets profile_id=None (no enhancement), but the
        effective input style must still honour the default profile's override.
        """
        cfg = _make_config(
            global_input_style="clipboard",
            profiles=[{"id": "dflt", "name": "Default", "prompt": "", "input_style": "keystroke"}],
            default_profile_id="dflt",
        )
        app = _make_app(cfg)
        # Pass None as profile_id — mimics the do_enhance=False path in main.py
        result = self._resolve(app, None)
        self.assertEqual(result, "keystroke")

    def test_unknown_per_profile_value_falls_back_to_global(self):
        """(d) An unknown per-profile input_style (e.g. "paste") is treated as absent
        and the global default is returned instead."""
        cfg = _make_config(
            global_input_style="clipboard",
            profiles=[{"id": "p3", "name": "P3", "prompt": "", "input_style": "paste"}],
        )
        app = _make_app(cfg)
        result = self._resolve(app, "p3")
        self.assertEqual(result, "clipboard")


class _FakeApp:
    """Minimal ForegroundApp stand-in for routing tests. Plain object (not a
    MagicMock) so ``domain`` is a real falsy value, not an auto-created truthy
    attribute that would spuriously trigger the site-routing branch."""

    def __init__(self, process_name: str, domain: str | None = None):
        self.process_name = process_name
        self.domain = domain


class TestEffectiveInputStyleNoEnhanceRouting(unittest.TestCase):
    """profile_id=None (the do_enhance=False path) must still honour a per-app or
    per-site profile's input_style override, by re-running the pure
    site -> app -> default routing against the focused app. This is the behaviour
    that makes a per-app Input Style override apply even when AI cleanup is OFF."""

    def test_none_profile_id_honors_per_app_override(self):
        """Focused app maps (power_mode_map) to a keystroke profile; profile_id=None
        (cleanup off) -> resolves "keystroke" via app routing, no default needed."""
        cfg = _make_config(
            global_input_style="clipboard",
            profiles=[{"id": "citrix", "name": "Citrix", "prompt": "", "input_style": "keystroke"}],
        )
        cfg.power_mode_map = {"wfica32.exe": "citrix"}
        app = _make_app(cfg)
        app._dictation_app = _FakeApp("wfica32.exe")
        result = app._effective_input_style(None)
        self.assertEqual(result, "keystroke")

    def test_none_profile_id_honors_per_site_override(self):
        """Focused browser carries a domain mapped (site_map) to a keystroke profile;
        profile_id=None -> resolves "keystroke" via site routing."""
        cfg = _make_config(
            global_input_style="clipboard",
            profiles=[{"id": "remote", "name": "Remote", "prompt": "", "input_style": "keystroke"}],
        )
        cfg.site_map = {"portal.example.com": "remote"}
        app = _make_app(cfg)
        app._dictation_app = _FakeApp("chrome.exe", domain="portal.example.com")
        result = app._effective_input_style(None)
        self.assertEqual(result, "keystroke")

    def test_none_profile_id_unmapped_app_falls_back_to_global(self):
        """Focused app has no mapping and there is no default profile -> the effective
        global (clipboard) is returned (regression: routing must not invent a style)."""
        cfg = _make_config(
            global_input_style="clipboard",
            profiles=[{"id": "citrix", "name": "Citrix", "prompt": "", "input_style": "keystroke"}],
        )
        cfg.power_mode_map = {"wfica32.exe": "citrix"}
        app = _make_app(cfg)
        app._dictation_app = _FakeApp("notepad.exe")  # not in the map
        result = app._effective_input_style(None)
        self.assertEqual(result, "clipboard")

    def test_explicit_profile_id_unaffected_by_focused_app(self):
        """When an explicit profile_id is passed (the enhance path), it wins and the
        focused-app routing is NOT consulted (no surprise re-routing)."""
        cfg = _make_config(
            global_input_style="clipboard",
            profiles=[
                {"id": "chat", "name": "Chat", "prompt": "", "input_style": "clipboard"},
                {"id": "citrix", "name": "Citrix", "prompt": "", "input_style": "keystroke"},
            ],
        )
        cfg.power_mode_map = {"wfica32.exe": "citrix"}
        app = _make_app(cfg)
        app._dictation_app = _FakeApp("wfica32.exe")  # would route to citrix/keystroke
        result = app._effective_input_style("chat")   # but the explicit pick wins
        self.assertEqual(result, "clipboard")


class TestScreenReaderActive(unittest.TestCase):
    """Unit tests for the screen_reader_active() helper (Task K2)."""

    def _import_fn(self):
        from whisperstream.main import screen_reader_active
        return screen_reader_active

    def test_never_raises(self):
        """screen_reader_active() must not raise under any circumstances."""
        fn = self._import_fn()
        # Call without any env override — should return bool, never raise.
        result = fn()
        self.assertIsInstance(result, bool)

    def test_force_env_true_returns_true(self):
        """WHISPERSTREAM_FORCE_SCREEN_READER=1 forces return True."""
        fn = self._import_fn()
        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "1"}):
            self.assertIs(fn(), True)

    def test_force_env_true_string_returns_true(self):
        """WHISPERSTREAM_FORCE_SCREEN_READER=true (string) forces return True."""
        fn = self._import_fn()
        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "true"}):
            self.assertIs(fn(), True)

    def test_force_env_false_returns_false(self):
        """WHISPERSTREAM_FORCE_SCREEN_READER=0 forces return False."""
        fn = self._import_fn()
        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "0"}):
            self.assertIs(fn(), False)

    def test_force_env_false_string_returns_false(self):
        """WHISPERSTREAM_FORCE_SCREEN_READER=false (string) forces return False."""
        fn = self._import_fn()
        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "false"}):
            self.assertIs(fn(), False)

    def test_force_env_absent_returns_bool(self):
        """Without the force env var, the function still returns bool (never raises)."""
        fn = self._import_fn()
        env = {k: v for k, v in os.environ.items() if k != "WHISPERSTREAM_FORCE_SCREEN_READER"}
        with patch.dict(os.environ, env, clear=True):
            result = fn()
        self.assertIsInstance(result, bool)


class TestEffectiveInputStyleK2(unittest.TestCase):
    """K2: Screen-reader auto-default + dirty flag integration into _effective_input_style."""

    def _make_cfg_k2(
        self,
        *,
        global_input_style: str = "clipboard",
        input_style_user_set: bool = False,
        profiles: list | None = None,
        default_profile_id: str = "",
    ) -> MagicMock:
        """Build a stub config with the K2 dirty-flag field included."""
        cfg = _make_config(
            global_input_style=global_input_style,
            profiles=profiles,
            default_profile_id=default_profile_id,
        )
        cfg.input_style_user_set = input_style_user_set
        return cfg

    def test_not_user_set_sr_true_global_resolves_keystroke(self):
        """(g) not-user-set + SR forced True -> effective global "keystroke"."""
        cfg = self._make_cfg_k2(global_input_style="clipboard", input_style_user_set=False)
        app = _make_app(cfg)
        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "1"}):
            result = app._effective_input_style(None)
        self.assertEqual(result, "keystroke")

    def test_not_user_set_sr_false_global_resolves_clipboard(self):
        """(h) not-user-set + SR forced False -> effective global "clipboard"."""
        cfg = self._make_cfg_k2(global_input_style="keystroke", input_style_user_set=False)
        app = _make_app(cfg)
        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "0"}):
            result = app._effective_input_style(None)
        self.assertEqual(result, "clipboard")

    def test_user_set_true_clipboard_sr_true_clipboard_wins(self):
        """(i) user_set=True + input_style='clipboard' + SR True -> 'clipboard' (explicit wins)."""
        cfg = self._make_cfg_k2(
            global_input_style="clipboard",
            input_style_user_set=True,
        )
        app = _make_app(cfg)
        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "1"}):
            result = app._effective_input_style(None)
        self.assertEqual(result, "clipboard")

    def test_per_profile_keystroke_wins_over_sr_auto_clipboard(self):
        """(j) per-profile keystroke wins regardless of SR state / dirty flag."""
        cfg = self._make_cfg_k2(
            global_input_style="clipboard",
            input_style_user_set=False,
            profiles=[{"id": "ks", "name": "KS", "prompt": "", "input_style": "keystroke"}],
        )
        app = _make_app(cfg)
        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "0"}):
            # SR is off, global would be clipboard; but profile overrides to keystroke
            result = app._effective_input_style("ks")
        self.assertEqual(result, "keystroke")

    def test_user_set_true_keystroke_no_sr_keystroke_wins(self):
        """user_set=True + input_style='keystroke' + SR False -> 'keystroke' (user choice)."""
        cfg = self._make_cfg_k2(
            global_input_style="keystroke",
            input_style_user_set=True,
        )
        app = _make_app(cfg)
        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "0"}):
            result = app._effective_input_style(None)
        self.assertEqual(result, "keystroke")


class TestApiConfigScreenReaderDetected(unittest.TestCase):
    """K2: /api/config response includes screen_reader_detected (bool)."""

    def _make_server_and_client(self, fake_config):
        """Build a WhisperstreamServer with a mock app around fake_config."""
        from fastapi.testclient import TestClient
        from whisperstream.server import WhisperstreamServer

        mock_app = MagicMock()
        mock_app.config = fake_config
        mock_app.stt_engine = MagicMock()
        mock_app.license_manager = MagicMock()
        mock_app.license_manager.status.return_value = {"is_pro": True, "trial_remaining_seconds": 0}
        from unittest.mock import AsyncMock
        mock_app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
        mock_app.load_stt_engine_async = MagicMock()
        mock_app.set_event_broadcaster = MagicMock()

        server = WhisperstreamServer(mock_app, port=0, logger=None, register_hotkeys=False)
        client = TestClient(server.fastapi, raise_server_exceptions=True)
        return client

    def test_get_config_includes_screen_reader_detected(self):
        """GET /api/config response dict must contain 'screen_reader_detected' as bool."""
        from whisperstream.config_manager import WhisperstreamConfig

        cfg = WhisperstreamConfig()
        client = self._make_server_and_client(cfg)

        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "1"}):
            resp = client.get("/api/config")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("screen_reader_detected", body,
                      "GET /api/config must include 'screen_reader_detected' key")
        self.assertIsInstance(body["screen_reader_detected"], bool,
                              "'screen_reader_detected' must be a bool")
        # With FORCE=1 it must be True
        self.assertIs(body["screen_reader_detected"], True)

    def test_get_config_screen_reader_detected_false_when_forced_off(self):
        """GET /api/config returns screen_reader_detected=False when SR forced off."""
        from whisperstream.config_manager import WhisperstreamConfig

        cfg = WhisperstreamConfig()
        client = self._make_server_and_client(cfg)

        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "0"}):
            resp = client.get("/api/config")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIs(body["screen_reader_detected"], False)


class TestEffectiveInputStyleLogging(unittest.TestCase):
    """The resolver emits a diagnostic trace so 'why paste vs keystroke' is
    answerable from the logs alone (no code-spelunking)."""

    def test_resolution_emits_inputstyle_debug_trace(self):
        """_effective_input_style logs the resolved style + screen_reader +
        user_set so a non-switching SR session is diagnosable from the log."""
        cfg = _make_config(global_input_style="clipboard")
        cfg.input_style_user_set = False
        app = _make_app(cfg)
        with patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "1"}):
            result = app._effective_input_style(None)
        self.assertEqual(result, "keystroke")

        traces = [
            c for c in app.logger.debug.call_args_list
            if c.args and "[InputStyle] resolved" in str(c.args[0])
        ]
        self.assertTrue(traces, "resolver must emit an [InputStyle] resolved debug log")
        fmt, *vals = traces[-1].args
        self.assertIn("screen_reader=", fmt)
        self.assertIn("user_set=", fmt)
        # Positional values: resolved, int(sr), str(user_set).lower(), ...
        self.assertEqual(vals[0], "keystroke")  # resolved style
        self.assertEqual(vals[1], 1)            # screen reader detected
        self.assertEqual(vals[2], "false")      # dirty flag not set


class TestScreenReaderProcessFallback(unittest.TestCase):
    """Process-name fallback for screen readers that don't set the
    SPI_GETSCREENREADER system flag (notably Narrator)."""

    def _proc(self, name):
        p = MagicMock()
        p.info = {"name": name}
        return p

    def test_process_running_detects_narrator(self):
        from whisperstream.main import _screen_reader_process_running
        procs = [self._proc("explorer.exe"), self._proc("Narrator.exe"), self._proc("code.exe")]
        with patch("psutil.process_iter", return_value=procs):
            self.assertTrue(_screen_reader_process_running())

    def test_process_running_detects_nvda_and_jaws(self):
        from whisperstream.main import _screen_reader_process_running
        for reader in ("nvda.exe", "jfw.exe", "fsreader.exe"):
            with patch("psutil.process_iter",
                       return_value=[self._proc("explorer.exe"), self._proc(reader)]):
                self.assertTrue(_screen_reader_process_running(), f"{reader} should match")

    def test_process_running_false_when_no_reader(self):
        from whisperstream.main import _screen_reader_process_running
        procs = [self._proc("explorer.exe"), self._proc("code.exe"), self._proc("chrome.exe")]
        with patch("psutil.process_iter", return_value=procs):
            self.assertFalse(_screen_reader_process_running())

    def test_process_running_never_raises(self):
        from whisperstream.main import _screen_reader_process_running
        with patch("psutil.process_iter", side_effect=RuntimeError("boom")):
            self.assertFalse(_screen_reader_process_running())

    def test_active_true_via_process_when_spi_false(self):
        """Narrator case: SPI flag False but a reader process is running -> True."""
        from whisperstream.main import screen_reader_active
        env = {k: v for k, v in os.environ.items() if k != "WHISPERSTREAM_FORCE_SCREEN_READER"}
        with patch("whisperstream.main._spi_screen_reader_flag", return_value=False), \
             patch("whisperstream.main._screen_reader_process_running", return_value=True), \
             patch.dict(os.environ, env, clear=True):
            self.assertTrue(screen_reader_active())

    def test_active_false_when_both_probes_false(self):
        from whisperstream.main import screen_reader_active
        env = {k: v for k, v in os.environ.items() if k != "WHISPERSTREAM_FORCE_SCREEN_READER"}
        with patch("whisperstream.main._spi_screen_reader_flag", return_value=False), \
             patch("whisperstream.main._screen_reader_process_running", return_value=False), \
             patch.dict(os.environ, env, clear=True):
            self.assertFalse(screen_reader_active())

    def test_force_env_still_wins_over_process_check(self):
        """Force env=0 short-circuits before the process check even if a reader runs."""
        from whisperstream.main import screen_reader_active
        with patch("whisperstream.main._screen_reader_process_running", return_value=True), \
             patch.dict(os.environ, {"WHISPERSTREAM_FORCE_SCREEN_READER": "0"}):
            self.assertFalse(screen_reader_active())


if __name__ == "__main__":
    unittest.main()
