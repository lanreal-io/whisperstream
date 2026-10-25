"""Tests for the ``profile_hotkeys`` config layer (round 04, issue 14).

Covers the new ``profile_hotkeys`` dataclass field (empty default, NO seeding),
from_dict normalization (coerce values to lowercase strings, drop blanks, no
seed-on-absence — mirrors site_map), validate() pruning (drop entries whose
profile id is gone, drop a hotkey colliding with primary_shortcut /
paste_last_shortcut, drop a hotkey duplicated across profiles — drop the offender
and warn, never raise), and the ``[profile_hotkeys]`` TOML round-trip.

Mirrors tests/test_config_power_mode.py.
"""

import tempfile
import tomllib
import unittest
from pathlib import Path

from whisperstream.config_manager import ConfigManager, WhisperstreamConfig


def _make_manager(tmpdir: str) -> ConfigManager:
    config_path = Path(tmpdir) / "config.toml"
    models_path = Path(tmpdir) / "models"
    return ConfigManager(config_path=config_path, models_root=models_path)


def _profiles():
    return [
        {"id": "chat", "name": "Chat", "prompt": "c"},
        {"id": "code", "name": "Code", "prompt": "k"},
    ]


class TestProfileHotkeysDefaultAndAbsence(unittest.TestCase):
    def test_default_constructed_config_has_empty_map(self):
        """A bare WhisperstreamConfig() has an empty profile_hotkeys."""
        self.assertEqual(WhisperstreamConfig().profile_hotkeys, {})

    def test_default_constructed_config_validates(self):
        """The empty state is valid (validate must not raise)."""
        WhisperstreamConfig().validate()

    def test_from_dict_without_key_stays_empty(self):
        """No seed-on-absence (unlike power_mode_map): an absent key -> {}."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertEqual(conf.profile_hotkeys, {})

    def test_from_dict_with_present_empty_map_stays_empty(self):
        conf = WhisperstreamConfig.from_dict({"profile_hotkeys": {}})
        self.assertEqual(conf.profile_hotkeys, {})


class TestProfileHotkeysNormalize(unittest.TestCase):
    def test_from_dict_lowercases_values_and_drops_blanks(self):
        """from_dict lowercases the shortcut value, coerces to str, and drops
        entries with a blank key or blank value. Profile-id membership /
        conflicts are NOT checked here (that is validate()'s job)."""
        conf = WhisperstreamConfig.from_dict(
            {
                "profile_hotkeys": {"chat": "CTRL+ALT+1", "x": "", "": "ctrl+alt+2"},
                "profiles": _profiles(),
                "default_profile_id": "chat",
            }
        )
        self.assertEqual(conf.profile_hotkeys, {"chat": "ctrl+alt+1"})

    def test_from_dict_populated_round_trips(self):
        conf = WhisperstreamConfig.from_dict(
            {
                "profile_hotkeys": {"chat": "ctrl+alt+1", "code": "ctrl+alt+2"},
                "profiles": _profiles(),
                "default_profile_id": "chat",
            }
        )
        self.assertEqual(
            conf.profile_hotkeys, {"chat": "ctrl+alt+1", "code": "ctrl+alt+2"}
        )


class TestProfileHotkeysValidate(unittest.TestCase):
    def test_validate_drops_unknown_profile_id_without_raising(self):
        """A hotkey keyed by a non-existent profile id is dropped in place."""
        conf = WhisperstreamConfig(
            profile_hotkeys={"ghost": "ctrl+alt+1"},
            profiles=_profiles(),
            default_profile_id="chat",
        )
        conf.validate()
        self.assertEqual(conf.profile_hotkeys, {})

    def test_validate_keeps_known_id(self):
        conf = WhisperstreamConfig(
            profile_hotkeys={"chat": "ctrl+alt+1", "ghost": "ctrl+alt+2"},
            profiles=_profiles(),
            default_profile_id="chat",
        )
        conf.validate()
        self.assertEqual(conf.profile_hotkeys, {"chat": "ctrl+alt+1"})

    def test_validate_drops_collision_with_primary_shortcut(self):
        """A hotkey equal to primary_shortcut is dropped (case-insensitive)."""
        conf = WhisperstreamConfig(
            primary_shortcut="right shift",
            profile_hotkeys={"chat": "Right Shift"},
            profiles=_profiles(),
            default_profile_id="chat",
        )
        conf.validate()
        self.assertEqual(conf.profile_hotkeys, {})

    def test_validate_drops_collision_with_paste_last_shortcut(self):
        conf = WhisperstreamConfig(
            paste_last_shortcut="ctrl+alt+v",
            profile_hotkeys={"chat": "ctrl+alt+v"},
            profiles=_profiles(),
            default_profile_id="chat",
        )
        conf.validate()
        self.assertEqual(conf.profile_hotkeys, {})

    def test_validate_drops_duplicate_across_profiles(self):
        """Two profiles claiming the same shortcut: the LATER one is dropped."""
        conf = WhisperstreamConfig(
            profile_hotkeys={"chat": "ctrl+alt+1", "code": "ctrl+alt+1"},
            profiles=_profiles(),
            default_profile_id="chat",
        )
        conf.validate()
        self.assertEqual(conf.profile_hotkeys, {"chat": "ctrl+alt+1"})

    def test_validate_keeps_distinct_non_conflicting(self):
        conf = WhisperstreamConfig(
            primary_shortcut="right shift",
            paste_last_shortcut="ctrl+alt+v",
            profile_hotkeys={"chat": "ctrl+alt+1", "code": "ctrl+alt+2"},
            profiles=_profiles(),
            default_profile_id="chat",
        )
        before = dict(conf.profile_hotkeys)
        conf.validate()
        self.assertEqual(conf.profile_hotkeys, before)


class TestProfileHotkeysTomlRoundTrip(unittest.TestCase):
    def test_round_trip_through_save_and_load(self):
        """A non-empty profile_hotkeys survives save() -> tomllib.load() ->
        from_dict() and the on-disk text carries a [profile_hotkeys] header."""
        hotkeys = {"chat": "ctrl+alt+1", "code": "ctrl+alt+2"}
        cfg = WhisperstreamConfig(
            primary_shortcut="right shift",
            paste_last_shortcut="ctrl+alt+v",
            profiles=_profiles(),
            default_profile_id="chat",
            profile_hotkeys=dict(hotkeys),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)

            on_disk_text = mgr.config_path.read_text(encoding="utf-8")
            self.assertIn("[profile_hotkeys]", on_disk_text)

            with mgr.config_path.open("rb") as handle:
                data = tomllib.load(handle)
            self.assertEqual(data["profile_hotkeys"], hotkeys)

            reloaded = WhisperstreamConfig.from_dict(data)
        self.assertEqual(reloaded.profile_hotkeys, hotkeys)

    def test_empty_map_emits_no_table_header(self):
        cfg = WhisperstreamConfig(
            profiles=_profiles(),
            default_profile_id="chat",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)
            on_disk_text = mgr.config_path.read_text(encoding="utf-8")
        self.assertNotIn("[profile_hotkeys]", on_disk_text)


class TestProfileHotkeysNewInstallSeed(unittest.TestCase):
    def test_load_no_file_does_not_seed_hotkeys(self):
        """A first run (no config.toml) leaves profile_hotkeys empty (no seed)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            self.assertFalse(mgr.config_path.exists())
            config = mgr.load()
        self.assertEqual(config.profile_hotkeys, {})


if __name__ == "__main__":
    unittest.main()
