"""Tests for the ``power_mode_map`` config layer (PR-B / #160).

Covers the new ``power_mode_map`` dataclass field (empty default), the
from_dict seed-on-absence behavior (an absent key seeds _DEFAULT_POWER_MODE_MAP
for upgrading users; a present-but-empty {} stays empty), the from_dict
normalization (lowercase keys, drop blanks), the defensive validate() drop of
stale (deleted-profile) ids, the ``[power_mode_map]`` TOML round-trip, and the
new-install seeding in ``load()``.

The new-install seed asserts against the imported ``_DEFAULT_POWER_MODE_MAP``
constant (never a hardcoded copy) so a curated-default change keeps these tests
honest.
"""

import tempfile
import tomllib
import unittest
from pathlib import Path

from whisperstream.config_manager import (
    _DEFAULT_POWER_MODE_MAP,
    ConfigManager,
    WhisperstreamConfig,
)


def _make_manager(tmpdir: str) -> ConfigManager:
    config_path = Path(tmpdir) / "config.toml"
    models_path = Path(tmpdir) / "models"
    return ConfigManager(config_path=config_path, models_root=models_path)


class TestPowerModeMapDefaultAndSeedOnAbsence(unittest.TestCase):
    def test_default_constructed_config_has_empty_map(self):
        """A bare WhisperstreamConfig() has an empty power_mode_map (the dataclass
        default must stay empty so from_dict's absence gate can tell an absent key
        from a deliberately-cleared {})."""
        self.assertEqual(WhisperstreamConfig().power_mode_map, {})

    def test_default_constructed_config_validates(self):
        """The empty state is valid (validate must not raise)."""
        WhisperstreamConfig().validate()  # Should not raise

    def test_from_dict_without_key_seeds_default_map(self):
        """from_dict on a dict with NO power_mode_map key seeds the full
        _DEFAULT_POWER_MODE_MAP (upgrading users who predate the field). Asserts
        against the imported constant, never a hardcoded copy."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertEqual(conf.power_mode_map, _DEFAULT_POWER_MODE_MAP)

    def test_from_dict_with_present_empty_map_stays_empty(self):
        """A present-but-empty power_mode_map ({}) is a deliberate user state
        (all routes cleared) and must NOT be re-seeded."""
        conf = WhisperstreamConfig.from_dict({"power_mode_map": {}})
        self.assertEqual(conf.power_mode_map, {})

    def test_from_dict_with_populated_map_round_trips_unchanged(self):
        """A populated power_mode_map's entries pass through from_dict without the
        FULL default map being merged in (contrast: seed-on-absence). The email
        desktop routes (outlook/thunderbird) are backfilled only when absent, so
        a map that already carries them round-trips byte-identical and the other
        default entries (devenv.exe, slack.exe, ...) are NOT merged in."""
        populated = {
            "firefox.exe": "chat",
            "outlook.exe": "email",
            "thunderbird.exe": "email",
        }
        conf = WhisperstreamConfig.from_dict(
            {
                "power_mode_map": dict(populated),
                "profiles": [{"id": "chat", "name": "Chat", "prompt": "p"}],
                "default_profile_id": "chat",
            }
        )
        self.assertEqual(conf.power_mode_map, populated)

    def test_from_dict_seeded_defaults_survive_validate(self):
        """The defaults seeded on absence reference the bundled chat/code profile
        ids, so a config carrying them validate()s clean (nothing pruned), as
        long as those profiles are present."""
        from whisperstream.config_manager import _DEFAULT_POWER_MODE_MAP as defaults

        conf = WhisperstreamConfig.from_dict(
            {
                # No power_mode_map key -> seeded with defaults.
                "profiles": [
                    {"id": "chat", "name": "Chat", "prompt": "c"},
                    {"id": "code", "name": "Code", "prompt": "k"},
                ],
                "default_profile_id": "chat",
            }
        )
        self.assertEqual(conf.power_mode_map, defaults)
        before = dict(conf.power_mode_map)
        conf.validate()  # Must not raise and must not prune known ids.
        self.assertEqual(conf.power_mode_map, before)


class TestPowerModeMapNormalize(unittest.TestCase):
    def test_from_dict_lowercases_keys_and_drops_blanks(self):
        """from_dict lowercases process-name keys, coerces values to str, and
        drops entries with a blank key or blank value. Profile-id membership is
        NOT checked here (that is validate()'s job). The email desktop routes are
        included in the input so the email backfill is a no-op and this stays
        focused on key/value normalization."""
        conf = WhisperstreamConfig.from_dict(
            {
                "power_mode_map": {
                    "Code.EXE": "code",
                    "x": "",
                    "": "code",
                    "outlook.exe": "email",
                    "thunderbird.exe": "email",
                },
                "profiles": [{"id": "code", "name": "Code", "prompt": "p"}],
                "default_profile_id": "code",
            }
        )
        self.assertEqual(
            conf.power_mode_map,
            {"code.exe": "code", "outlook.exe": "email", "thunderbird.exe": "email"},
        )


class TestPowerModeMapValidate(unittest.TestCase):
    def test_validate_drops_stale_id_without_raising(self):
        """A value pointing at a non-existent profile id is dropped in place by
        validate() (profiles are user-deletable); validate must not raise."""
        conf = WhisperstreamConfig(
            power_mode_map={"foo.exe": "ghost"},
            profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
            default_profile_id="code",
        )
        conf.validate()  # Should not raise.
        self.assertEqual(conf.power_mode_map, {})

    def test_validate_keeps_known_id_drops_only_stale(self):
        """validate() keeps entries pointing at existing profile ids and drops
        only the stale ones, in place."""
        conf = WhisperstreamConfig(
            power_mode_map={"code.exe": "code", "foo.exe": "ghost"},
            profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
            default_profile_id="code",
        )
        conf.validate()
        self.assertEqual(conf.power_mode_map, {"code.exe": "code"})


class TestPowerModeMapTomlRoundTrip(unittest.TestCase):
    def test_round_trip_through_save_and_load(self):
        """A non-empty power_mode_map survives save() -> tomllib.load() ->
        from_dict() byte-identical, and the on-disk text carries a
        [power_mode_map] table header. Includes the email desktop routes so the
        from_dict email backfill is a no-op and the round-trip is exact."""
        power_mode_map = {
            "code.exe": "code",
            "slack.exe": "chat",
            "outlook.exe": "email",
            "thunderbird.exe": "email",
        }
        cfg = WhisperstreamConfig(
            profiles=[
                {"id": "chat", "name": "Chat", "prompt": "c"},
                {"id": "code", "name": "Code", "prompt": "k"},
                {"id": "email", "name": "Email", "prompt": "e"},
            ],
            default_profile_id="chat",
            power_mode_map=dict(power_mode_map),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)

            on_disk_text = mgr.config_path.read_text(encoding="utf-8")
            self.assertIn("[power_mode_map]", on_disk_text)

            with mgr.config_path.open("rb") as handle:
                data = tomllib.load(handle)

            # tomllib parses the table natively into a dict.
            self.assertEqual(data["power_mode_map"], power_mode_map)

            reloaded = WhisperstreamConfig.from_dict(data)

        self.assertEqual(reloaded.power_mode_map, power_mode_map)

    def test_empty_map_emits_no_table_header(self):
        """An empty power_mode_map emits no [power_mode_map] table (mirrors the
        word_replacements only-when-non-empty rule)."""
        cfg = WhisperstreamConfig(
            profiles=[{"id": "code", "name": "Code", "prompt": "k"}],
            default_profile_id="code",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)
            on_disk_text = mgr.config_path.read_text(encoding="utf-8")
        self.assertNotIn("[power_mode_map]", on_disk_text)


class TestPowerModeMapNewInstallSeed(unittest.TestCase):
    def test_load_no_file_seeds_default_map(self):
        """A first run (no config.toml) seeds power_mode_map ==
        _DEFAULT_POWER_MODE_MAP (the imported constant, not a hardcoded copy)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            self.assertFalse(mgr.config_path.exists())
            config = mgr.load()
        self.assertEqual(config.power_mode_map, _DEFAULT_POWER_MODE_MAP)

    def test_seeded_map_references_only_seeded_profile_ids(self):
        """Every seeded power_mode_map value references a profile id that the
        same no-file branch seeds, so the seeded config validate()s clean (no
        stale ids dropped)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            config = mgr.load()

        seeded_profile_ids = {p["id"] for p in config.profiles}
        for process_name, profile_id in config.power_mode_map.items():
            with self.subTest(process_name=process_name):
                self.assertIn(profile_id, seeded_profile_ids)

        # validate() is a no-op on a clean seed: the map survives unchanged.
        before = dict(config.power_mode_map)
        config.validate()  # Should not raise and must not drop anything.
        self.assertEqual(config.power_mode_map, before)


class TestEmailRoutingInPowerModeMap(unittest.TestCase):
    """Task 2.4: outlook.exe and thunderbird.exe are seeded with the email profile."""

    def test_default_map_contains_outlook_email(self):
        """_DEFAULT_POWER_MODE_MAP maps outlook.exe to the email profile."""
        self.assertEqual(_DEFAULT_POWER_MODE_MAP.get("outlook.exe"), "email")

    def test_default_map_contains_thunderbird_email(self):
        """_DEFAULT_POWER_MODE_MAP maps thunderbird.exe to the email profile."""
        self.assertEqual(_DEFAULT_POWER_MODE_MAP.get("thunderbird.exe"), "email")

    def test_load_no_file_seeds_outlook_and_thunderbird(self):
        """A fresh load() seeds outlook.exe and thunderbird.exe -> 'email'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            config = mgr.load()
        self.assertEqual(config.power_mode_map.get("outlook.exe"), "email")
        self.assertEqual(config.power_mode_map.get("thunderbird.exe"), "email")

    def test_seeded_config_with_email_routes_validates_clean(self):
        """The seeded config (with outlook/thunderbird -> email) validate()s
        clean because the email profile is also seeded by load()'s no-file branch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            config = mgr.load()
        # Should not raise; outlook/thunderbird -> email must not be pruned.
        before = dict(config.power_mode_map)
        config.validate()
        self.assertIn("outlook.exe", config.power_mode_map)
        self.assertIn("thunderbird.exe", config.power_mode_map)
        self.assertEqual(config.power_mode_map, before)

    def test_from_dict_absent_key_seeds_outlook_thunderbird(self):
        """from_dict without a power_mode_map key seeds the default map which
        now includes outlook.exe and thunderbird.exe -> 'email'."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertEqual(conf.power_mode_map.get("outlook.exe"), "email")
        self.assertEqual(conf.power_mode_map.get("thunderbird.exe"), "email")

    def test_from_dict_customized_map_backfills_email_routes(self):
        """An existing user with a populated power_mode_map that lacks the email
        routes gets outlook.exe/thunderbird.exe -> email backfilled, while their
        existing entries are preserved."""
        conf = WhisperstreamConfig.from_dict(
            {"power_mode_map": {"code.exe": "code", "slack.exe": "chat"}}
        )
        self.assertEqual(conf.power_mode_map.get("outlook.exe"), "email")
        self.assertEqual(conf.power_mode_map.get("thunderbird.exe"), "email")
        # Existing customizations preserved.
        self.assertEqual(conf.power_mode_map.get("code.exe"), "code")
        self.assertEqual(conf.power_mode_map.get("slack.exe"), "chat")

    def test_from_dict_backfill_does_not_clobber_user_outlook_mapping(self):
        """If the user already mapped outlook.exe to a custom profile, the
        backfill must NOT overwrite it (per-key absence gate)."""
        conf = WhisperstreamConfig.from_dict(
            {"power_mode_map": {"outlook.exe": "work-mail", "code.exe": "code"}}
        )
        self.assertEqual(conf.power_mode_map.get("outlook.exe"), "work-mail")
        # thunderbird.exe was absent -> backfilled.
        self.assertEqual(conf.power_mode_map.get("thunderbird.exe"), "email")

    def test_from_dict_present_empty_map_stays_empty(self):
        """A present-but-empty {} power_mode_map is the deliberate 'cleared all
        routes' state and must stay empty -- the email backfill is gated on a
        non-empty map and must NOT re-add routes."""
        conf = WhisperstreamConfig.from_dict({"power_mode_map": {}})
        self.assertEqual(conf.power_mode_map, {})

    def test_from_dict_backfill_idempotent(self):
        """A config that already has the email routes round-trips unchanged
        (no duplication, no clobber)."""
        original = {
            "outlook.exe": "email",
            "thunderbird.exe": "email",
            "code.exe": "code",
        }
        conf = WhisperstreamConfig.from_dict({"power_mode_map": dict(original)})
        self.assertEqual(conf.power_mode_map, original)


class TestPowerModePaths(unittest.TestCase):
    """The power_mode_paths field: empty default, from_dict normalization,
    validate() pruning against power_mode_map (incl. via a stale-profile rule),
    and the [power_mode_paths] TOML round-trip."""

    def test_default_is_empty(self):
        self.assertEqual(WhisperstreamConfig().power_mode_paths, {})

    def test_from_dict_normalizes_keys_and_drops_blanks(self):
        cfg = WhisperstreamConfig.from_dict(
            {
                "power_mode_map": {"dota2.exe": "games"},
                "power_mode_paths": {"DOTA2.EXE": "C:/Games/dota2.exe", "blank": ""},
                "profiles": [{"id": "games", "name": "Games", "prompt": ""}],
                "default_profile_id": "games",
            }
        )
        # Lowercased key; the blank-valued entry is dropped. (Pruning vs
        # power_mode_map is validate()'s job, not from_dict's.)
        self.assertEqual(cfg.power_mode_paths, {"dota2.exe": "C:/Games/dota2.exe"})

    def test_validate_prunes_path_without_a_rule(self):
        cfg = WhisperstreamConfig(
            profiles=[{"id": "games", "name": "Games", "prompt": ""}],
            default_profile_id="games",
            power_mode_map={"dota2.exe": "games"},
            power_mode_paths={
                "dota2.exe": "C:/Games/dota2.exe",
                "gone.exe": "C:/x/gone.exe",
            },
        )
        cfg.validate()
        # gone.exe has no power_mode_map rule -> pruned; dota2 kept.
        self.assertEqual(cfg.power_mode_paths, {"dota2.exe": "C:/Games/dota2.exe"})

    def test_validate_prunes_path_when_rule_dropped_as_stale(self):
        # A rule pointing at a deleted profile is pruned from power_mode_map, and
        # its recorded path is pruned in the SAME validate() pass.
        cfg = WhisperstreamConfig(
            profiles=[{"id": "games", "name": "Games", "prompt": ""}],
            default_profile_id="games",
            power_mode_map={"dota2.exe": "deleted-profile"},
            power_mode_paths={"dota2.exe": "C:/Games/dota2.exe"},
        )
        cfg.validate()
        self.assertEqual(cfg.power_mode_map, {})
        self.assertEqual(cfg.power_mode_paths, {})

    def test_toml_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            cfg = WhisperstreamConfig(
                profiles=[{"id": "games", "name": "Games", "prompt": ""}],
                default_profile_id="games",
                power_mode_map={"dota2.exe": "games"},
                power_mode_paths={"dota2.exe": "C:/Games/dota2.exe"},
            )
            mgr.save(cfg)
            text = (Path(tmp) / "config.toml").read_text(encoding="utf-8")
            self.assertIn("[power_mode_paths]", text)
            reloaded = mgr.load()
            self.assertEqual(
                reloaded.power_mode_paths, {"dota2.exe": "C:/Games/dota2.exe"}
            )


if __name__ == "__main__":
    unittest.main()
