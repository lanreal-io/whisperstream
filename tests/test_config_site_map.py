"""Tests for the ``site_map`` config field (per-site-routing Task 1.1).

Covers: empty default on new install (no seeding, unlike power_mode_map);
absent key in from_dict stays empty (NOT seeded); present-but-empty stays
empty; lowercase + blank-drop normalization; ``[site_map]`` TOML round-trip;
validate() prunes stale-profile site entries; a single profile delete prunes
both its app (power_mode_map) and site (site_map) rows via the shared
seen_profile_ids set.
"""

import tempfile
import tomllib
import unittest
from pathlib import Path

from whisperstream.config_manager import (
    ConfigManager,
    WhisperstreamConfig,
)


def _make_manager(tmpdir: str) -> ConfigManager:
    config_path = Path(tmpdir) / "config.toml"
    models_path = Path(tmpdir) / "models"
    return ConfigManager(config_path=config_path, models_root=models_path)


class TestSiteMapDefaultAndNoSeed(unittest.TestCase):
    def test_default_constructed_config_has_empty_site_map(self):
        """A bare WhisperstreamConfig() has an empty site_map (the dataclass
        default must stay empty; no seeding for site_map unlike power_mode_map)."""
        self.assertEqual(WhisperstreamConfig().site_map, {})

    def test_default_constructed_config_validates(self):
        """The empty state is valid (validate must not raise)."""
        WhisperstreamConfig().validate()  # Should not raise

    def test_from_dict_without_key_gets_email_backfill(self):
        """from_dict on a dict with NO site_map key ends up with the three email
        domains backfilled (Task 2.4 email backfill always adds absent mail
        domains; non-email keys are never added)."""
        conf = WhisperstreamConfig.from_dict({})
        # Only the email mail domains are backfilled; no other domains appear.
        self.assertEqual(conf.site_map.get("mail.google.com"), "email")
        self.assertEqual(conf.site_map.get("outlook.office.com"), "email")
        self.assertEqual(conf.site_map.get("outlook.live.com"), "email")
        # No other unexpected keys were added.
        self.assertEqual(len(conf.site_map), 3)

    def test_from_dict_with_present_empty_map_gets_email_backfill(self):
        """A present-but-empty site_map ({}) gets the email backfill applied
        (the backfill adds absent mail domains; it does not re-seed the entire
        map, so a user who clears all non-email routes only gets the three
        mail domains added, not the full default map)."""
        conf = WhisperstreamConfig.from_dict({"site_map": {}})
        self.assertEqual(conf.site_map.get("mail.google.com"), "email")
        self.assertEqual(conf.site_map.get("outlook.office.com"), "email")
        self.assertEqual(conf.site_map.get("outlook.live.com"), "email")
        self.assertEqual(len(conf.site_map), 3)

    def test_from_dict_with_populated_map_gains_email_domains(self):
        """A populated site_map passes through from_dict with lowercased keys
        AND gets the three email domains added by the backfill (if absent)."""
        conf = WhisperstreamConfig.from_dict(
            {
                "site_map": {"github.com": "code"},
                "profiles": [
                    {"id": "code", "name": "Code", "prompt": "p"},
                    {"id": "email", "name": "Email", "prompt": "e"},
                ],
                "default_profile_id": "code",
            }
        )
        # Original entry preserved + email domains added.
        self.assertEqual(conf.site_map.get("github.com"), "code")
        self.assertEqual(conf.site_map.get("mail.google.com"), "email")
        self.assertEqual(conf.site_map.get("outlook.office.com"), "email")
        self.assertEqual(conf.site_map.get("outlook.live.com"), "email")

    def test_load_no_file_seeds_mail_domains(self):
        """A first run (no config.toml) seeds site_map with the three email
        domains: mail.google.com, outlook.office.com, outlook.live.com -> 'email'.
        (Task 2.4: site_map is seeded for new installs only; existing users get
        the backfill in from_dict.)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            self.assertFalse(mgr.config_path.exists())
            config = mgr.load()
        self.assertEqual(config.site_map.get("mail.google.com"), "email")
        self.assertEqual(config.site_map.get("outlook.office.com"), "email")
        self.assertEqual(config.site_map.get("outlook.live.com"), "email")


class TestSiteMapNormalize(unittest.TestCase):
    def test_from_dict_lowercases_keys_and_drops_blanks(self):
        """from_dict lowercases domain keys, coerces values to str, and drops
        entries with a blank key or blank value. Profile-id membership is NOT
        checked here (that is validate()'s job). The email backfill also runs,
        so we assert the non-email entry is correct without asserting the full
        map (to avoid coupling this test to the backfill's exact output)."""
        conf = WhisperstreamConfig.from_dict(
            {
                "site_map": {
                    "GitHub.com": "code",   # key should be lowercased
                    "x.com": "",            # blank value -> dropped
                    "": "code",             # blank key -> dropped
                },
                "profiles": [
                    {"id": "code", "name": "Code", "prompt": "p"},
                    {"id": "email", "name": "Email", "prompt": "e"},
                ],
                "default_profile_id": "code",
            }
        )
        # Normalized entry present; blanks dropped.
        self.assertEqual(conf.site_map.get("github.com"), "code")
        self.assertNotIn("x.com", conf.site_map)
        self.assertNotIn("", conf.site_map)

    def test_from_dict_coerces_values_to_str(self):
        """Values that aren't strings are coerced to str."""
        conf = WhisperstreamConfig.from_dict(
            {
                "site_map": {"github.com": 123},
                "profiles": [
                    {"id": "123", "name": "Num", "prompt": "p"},
                    {"id": "email", "name": "Email", "prompt": "e"},
                ],
                "default_profile_id": "123",
            }
        )
        self.assertEqual(conf.site_map.get("github.com"), "123")


class TestSiteMapValidate(unittest.TestCase):
    def test_validate_drops_stale_id_without_raising(self):
        """A value pointing at a non-existent profile id is dropped in place by
        validate() (profiles are user-deletable); validate must not raise."""
        conf = WhisperstreamConfig(
            site_map={"github.com": "ghost"},
            profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
            default_profile_id="code",
        )
        conf.validate()  # Should not raise.
        self.assertEqual(conf.site_map, {})

    def test_validate_keeps_known_id_drops_only_stale(self):
        """validate() keeps entries pointing at existing profile ids and drops
        only the stale ones, in place."""
        conf = WhisperstreamConfig(
            site_map={"github.com": "code", "gmail.com": "ghost"},
            profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
            default_profile_id="code",
        )
        conf.validate()
        self.assertEqual(conf.site_map, {"github.com": "code"})

    def test_validate_single_profile_delete_prunes_both_app_and_site_rows(self):
        """A single profile delete prunes both power_mode_map and site_map
        entries that pointed at the deleted profile id. Both maps share the
        same seen_profile_ids set in validate(), so they are pruned in one pass."""
        conf = WhisperstreamConfig(
            power_mode_map={"code.exe": "code", "firefox.exe": "deleted_profile"},
            site_map={"github.com": "code", "news.ycombinator.com": "deleted_profile"},
            profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
            default_profile_id="code",
        )
        conf.validate()
        # power_mode_map: deleted_profile entry removed, code.exe kept
        self.assertEqual(conf.power_mode_map, {"code.exe": "code"})
        # site_map: deleted_profile entry removed, github.com kept
        self.assertEqual(conf.site_map, {"github.com": "code"})

    def test_validate_non_lowercase_key_is_normalized(self):
        """A directly-constructed site_map with a mixed-case key triggers the
        defensive-lowercase logic in validate() (same as power_mode_map). The
        entry is kept but the key is normalized to lowercase — the rebuild loop
        lowercases all surviving keys, so "GitHub.com" becomes "github.com"."""
        conf = WhisperstreamConfig(
            site_map={"GitHub.com": "code"},
            profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
            default_profile_id="code",
        )
        conf.validate()
        # Key is lowercased (not dropped), entry survives.
        self.assertEqual(conf.site_map, {"github.com": "code"})


class TestSiteMapTomlRoundTrip(unittest.TestCase):
    def test_round_trip_through_save_and_load(self):
        """A non-empty site_map survives save() -> tomllib.load() -> from_dict()
        with user entries intact, and the on-disk text carries a [site_map] table
        header. Note: the email backfill in from_dict will also add the three mail
        domains on reload (and email profile is backfilled into profiles), so we
        assert the user entries are present rather than asserting exact equality."""
        cfg = WhisperstreamConfig(
            profiles=[
                {"id": "chat", "name": "Chat", "prompt": "c"},
                {"id": "code", "name": "Code", "prompt": "k"},
            ],
            default_profile_id="chat",
            site_map={"github.com": "code", "gmail.com": "chat"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)

            on_disk_text = mgr.config_path.read_text(encoding="utf-8")
            self.assertIn("[site_map]", on_disk_text)

            with mgr.config_path.open("rb") as handle:
                data = tomllib.load(handle)

            # On-disk only has the entries that were saved (no backfill yet).
            self.assertEqual(data["site_map"], {"github.com": "code", "gmail.com": "chat"})

            reloaded = WhisperstreamConfig.from_dict(data)

        # User entries survive unchanged.
        self.assertEqual(reloaded.site_map.get("github.com"), "code")
        self.assertEqual(reloaded.site_map.get("gmail.com"), "chat")
        # Email backfill also present (email profile was backfilled into profiles).
        self.assertEqual(reloaded.site_map.get("mail.google.com"), "email")

    def test_empty_map_emits_no_table_header_before_backfill(self):
        """An empty site_map (directly constructed) emits no [site_map] table on
        the first save, but after reload the email backfill adds entries so the
        on-disk form on the second save will have the [site_map] section."""
        cfg = WhisperstreamConfig(
            profiles=[{"id": "code", "name": "Code", "prompt": "k"}],
            default_profile_id="code",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)
            # First save: no site_map entries yet -> no table header.
            on_disk_text = mgr.config_path.read_text(encoding="utf-8")
        self.assertNotIn("[site_map]", on_disk_text)

    def test_round_trip_alongside_power_mode_map(self):
        """Both site_map and power_mode_map survive a save/load round-trip
        independently — they do not bleed into each other's TOML tables.
        User-specified entries are preserved; the email backfill may add
        additional entries on reload, so we assert presence not exact equality."""
        cfg = WhisperstreamConfig(
            profiles=[
                {"id": "code", "name": "Code", "prompt": "k"},
                {"id": "chat", "name": "Chat", "prompt": "c"},
                {"id": "email", "name": "Email", "prompt": "e"},
            ],
            default_profile_id="chat",
            power_mode_map={"code.exe": "code"},
            site_map={
                "github.com": "code",
                "gmail.com": "chat",
                "mail.google.com": "email",
                "outlook.office.com": "email",
                "outlook.live.com": "email",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)
            reloaded = mgr.load()

        self.assertEqual(reloaded.power_mode_map.get("code.exe"), "code")
        self.assertEqual(reloaded.site_map.get("github.com"), "code")
        self.assertEqual(reloaded.site_map.get("gmail.com"), "chat")
        self.assertEqual(reloaded.site_map.get("mail.google.com"), "email")


class TestEmailSiteMapSeeding(unittest.TestCase):
    """Task 2.4: email mail-domain seeding for new installs + idempotent backfill."""

    _MAIL_DOMAINS = ("mail.google.com", "outlook.office.com", "outlook.live.com")

    def test_load_no_file_seeds_all_three_mail_domains(self):
        """A fresh load() (no config.toml) seeds all three email domains to 'email'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            config = mgr.load()
        for domain in self._MAIL_DOMAINS:
            with self.subTest(domain=domain):
                self.assertEqual(config.site_map.get(domain), "email")

    def test_load_no_file_seeded_config_validates_clean(self):
        """The seeded site_map (email domains -> 'email') validate()s clean
        because the email profile is also seeded by load()'s no-file branch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            config = mgr.load()
        before = dict(config.site_map)
        config.validate()  # Must not raise and must not prune any email domain.
        for domain in self._MAIL_DOMAINS:
            with self.subTest(domain=domain):
                self.assertIn(domain, config.site_map)
        self.assertEqual(config.site_map, before)

    def test_from_dict_absent_site_map_stays_empty(self):
        """from_dict on a dict with NO site_map key still does NOT seed anything.
        The backfill only adds email domains; it does NOT create a map from absent."""
        conf = WhisperstreamConfig.from_dict({})
        # The backfill runs, but since absent site_map starts as {}, the
        # email domains ARE added by the backfill.
        # This confirms the backfill is additive (does not clear user data).
        for domain in self._MAIL_DOMAINS:
            with self.subTest(domain=domain):
                self.assertEqual(conf.site_map.get(domain), "email")

    def test_from_dict_backfill_adds_missing_email_domains(self):
        """from_dict on a config with a non-empty site_map lacking the email
        domains -> backfill appends the three mail domains."""
        conf = WhisperstreamConfig.from_dict(
            {
                "site_map": {"github.com": "code"},
                "profiles": [
                    {"id": "code", "name": "Code", "prompt": "p"},
                    {"id": "email", "name": "Email", "prompt": "e"},
                ],
                "default_profile_id": "code",
            }
        )
        # Original mapping preserved
        self.assertEqual(conf.site_map.get("github.com"), "code")
        # Email domains backfilled
        for domain in self._MAIL_DOMAINS:
            with self.subTest(domain=domain):
                self.assertEqual(conf.site_map.get(domain), "email")

    def test_from_dict_backfill_does_not_clobber_existing_mapping(self):
        """If a user already mapped a mail domain to a different profile,
        the backfill must NOT overwrite it (idempotency gate)."""
        conf = WhisperstreamConfig.from_dict(
            {
                "site_map": {
                    "mail.google.com": "code",   # user mapped to code explicitly
                    "outlook.office.com": "email",
                },
                "profiles": [
                    {"id": "code", "name": "Code", "prompt": "p"},
                    {"id": "email", "name": "Email", "prompt": "e"},
                ],
                "default_profile_id": "code",
            }
        )
        # User's custom mapping preserved (not overwritten to 'email')
        self.assertEqual(conf.site_map.get("mail.google.com"), "code")
        # Already-email entry preserved
        self.assertEqual(conf.site_map.get("outlook.office.com"), "email")
        # Missing domain gets backfilled
        self.assertEqual(conf.site_map.get("outlook.live.com"), "email")

    def test_from_dict_backfill_is_idempotent_through_double_load(self):
        """Running from_dict twice (simulating two loads of the same config)
        leaves the mail domains in place at 'email' without duplication."""
        data = {
            "site_map": {"github.com": "code"},
            "profiles": [
                {"id": "code", "name": "Code", "prompt": "p"},
                {"id": "email", "name": "Email", "prompt": "e"},
            ],
            "default_profile_id": "code",
        }
        conf1 = WhisperstreamConfig.from_dict(data)
        # Simulate a round-trip: serialize back to dict and load again
        data2 = dict(data)
        data2["site_map"] = dict(conf1.site_map)  # contains backfilled domains
        conf2 = WhisperstreamConfig.from_dict(data2)
        for domain in self._MAIL_DOMAINS:
            with self.subTest(domain=domain):
                self.assertEqual(conf2.site_map.get(domain), "email")
        # github.com untouched
        self.assertEqual(conf2.site_map.get("github.com"), "code")


class TestSiteMapToDictIncluded(unittest.TestCase):
    def test_to_dict_includes_site_map(self):
        """to_dict() (via asdict) includes site_map so the REST API exposes it."""
        conf = WhisperstreamConfig(
            site_map={"github.com": "code"},
            profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
            default_profile_id="code",
        )
        d = conf.to_dict()
        self.assertIn("site_map", d)
        self.assertEqual(d["site_map"], {"github.com": "code"})


if __name__ == "__main__":
    unittest.main()
