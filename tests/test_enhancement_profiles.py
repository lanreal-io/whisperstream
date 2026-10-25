"""Tests for the enhancement-profiles config layer (PR-A).

Covers the new ``profiles`` / ``default_profile_id`` fields, the behavior-
preserving (idempotent) migration from the legacy ``enhancement_mode`` +
``enhancement_custom_prompt`` pair, the ``[[profiles]]`` TOML round-trip, the
new-install seeding in ``load()``, and the ``validate()`` rules.

Exact-prompt assertions compare against the imported ``CHAT_PROMPT`` /
``CODE_PROMPT`` constants (never hardcoded copies) so a prompt re-tune keeps
these tests honest.
"""

import tempfile
import tomllib
import unittest
from pathlib import Path

from whisperstream.config_manager import ConfigManager, WhisperstreamConfig
from whisperstream.llm_enhancer import (
    CHAT_PROMPT,
    CODE_PROMPT,
    EMAIL_PROMPT,
    GEMMA_LOCAL_CHAT_PROMPT,
    LOCAL_EMAIL_PROMPT,
    LLMEnhancer,
)


def _make_manager(tmpdir: str) -> ConfigManager:
    config_path = Path(tmpdir) / "config.toml"
    models_path = Path(tmpdir) / "models"
    return ConfigManager(config_path=config_path, models_root=models_path)


class TestProfileFields(unittest.TestCase):
    def test_default_constructed_config_has_empty_profiles(self):
        """A bare WhisperstreamConfig() has no seeded profiles (seeding is
        load()-only, migration is from_dict-only)."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.profiles, [])
        self.assertEqual(conf.default_profile_id, "")

    def test_default_constructed_config_validates(self):
        """The pre-seed empty state (no profiles, empty default) is valid."""
        conf = WhisperstreamConfig()
        conf.validate()  # Should not raise

    def test_legacy_fields_untouched(self):
        """The legacy enhancement_mode / enhancement_custom_prompt fields
        survive the dataclass addition unchanged."""
        conf = WhisperstreamConfig()
        self.assertEqual(conf.enhancement_mode, "default")
        self.assertEqual(conf.enhancement_custom_prompt, "")


class TestProfileTomlRoundTrip(unittest.TestCase):
    def test_every_curated_custom_icon_round_trips(self):
        """Every bounded custom-profile icon survives save -> load."""
        icon_keys = (
            "chat",
            "code",
            "mail",
            "document",
            "note",
            "terminal",
            "checklist",
            "calendar",
            "people",
            "sparkle",
            "search",
            "pen",
            "book",
            "globe",
        )
        profiles = [
            {
                "id": f"custom-{icon}",
                "name": icon.title(),
                "prompt": "Clean this up.",
                "icon": icon,
            }
            for icon in icon_keys
        ]
        cfg = WhisperstreamConfig(
            profiles=profiles,
            default_profile_id=profiles[0]["id"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)
            with mgr.config_path.open("rb") as handle:
                data = tomllib.load(handle)
            reloaded = mgr.load()

        persisted = {
            profile["id"]: profile.get("icon")
            for profile in data["profiles"]
        }
        loaded = {
            profile["id"]: profile.get("icon")
            for profile in reloaded.profiles
            if profile["id"].startswith("custom-")
        }
        expected = {f"custom-{icon}": icon for icon in icon_keys}
        self.assertEqual(persisted, expected)
        self.assertEqual(loaded, expected)

    def test_builtin_profiles_never_persist_icon_overrides(self):
        """Built-in glyphs stay derived from id, even with valid icon input."""
        cfg = WhisperstreamConfig(
            profiles=[
                {
                    "id": "chat",
                    "name": "Chat",
                    "prompt": CHAT_PROMPT,
                    "icon": "globe",
                },
                {
                    "id": "code",
                    "name": "Code",
                    "prompt": CODE_PROMPT,
                    "icon": "mail",
                },
                {
                    "id": "email",
                    "name": "Email",
                    "prompt": EMAIL_PROMPT,
                    "icon": "chat",
                },
            ],
            default_profile_id="chat",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)
            with mgr.config_path.open("rb") as handle:
                data = tomllib.load(handle)

        self.assertTrue(all("icon" not in profile for profile in data["profiles"]))

    def test_profiles_round_trip_through_save_and_load(self):
        """A config with bundled chat/code + a custom multi-line prompt
        (containing newlines, a lone " and a \"\"\" run) survives save() ->
        tomllib.load() -> from_dict() byte-identical."""
        nasty_prompt = (
            'Clean it up.\n'
            'Keep a lone " quote here.\n'
            'And a """ triple-quote run that would terminate a basic string.\n'
            'Tabs\tand trailing text.'
        )
        profiles = [
            {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
            {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
            {"id": "custom", "name": "Custom", "prompt": nasty_prompt},
        ]
        cfg = WhisperstreamConfig(
            profiles=[dict(p) for p in profiles],
            default_profile_id="custom",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)

            with mgr.config_path.open("rb") as handle:
                data = tomllib.load(handle)

            # tomllib parses [[profiles]] natively into a list-of-dicts.
            self.assertIsInstance(data["profiles"], list)
            self.assertEqual(len(data["profiles"]), 3)
            self.assertEqual(data["default_profile_id"], "custom")

            reloaded = WhisperstreamConfig.from_dict(data)

        # The original three profiles survive the TOML round-trip byte-identical
        # (in order). The email built-in backfill (Task 1.4) appends an email
        # row on reload because this saved config predates it.
        self.assertEqual(reloaded.profiles[:3], profiles)
        self.assertEqual(
            [p["id"] for p in reloaded.profiles], ["chat", "code", "custom", "email"]
        )
        self.assertEqual(reloaded.profiles[-1]["prompt"], EMAIL_PROMPT)
        self.assertEqual(reloaded.default_profile_id, "custom")
        # The nasty custom prompt survives the """...""" escaping exactly.
        self.assertEqual(reloaded.profiles[2]["prompt"], nasty_prompt)
        # Bundled prompts survive byte-identical.
        self.assertEqual(reloaded.profiles[0]["prompt"], CHAT_PROMPT)
        self.assertEqual(reloaded.profiles[1]["prompt"], CODE_PROMPT)

    def test_save_emits_only_id_name_prompt_per_profile(self):
        """The serializer drops any extra key (e.g. a frontend-only `builtin`
        flag); only id/name/prompt land on disk."""
        cfg = WhisperstreamConfig(
            profiles=[
                {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT, "builtin": True},
            ],
            default_profile_id="chat",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)
            with mgr.config_path.open("rb") as handle:
                data = tomllib.load(handle)
        self.assertEqual(list(data["profiles"][0].keys()), ["id", "name", "prompt"])

    def test_mode_field_round_trips_and_only_valid_values_persist(self):
        """A profile's fine-tune `mode` ("chat"|"code") survives save -> load;
        an invalid mode value is dropped so the backend default (chat) applies,
        and an absent mode stays absent."""
        cfg = WhisperstreamConfig(
            profiles=[
                {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
                {"id": "c1", "name": "Notes", "prompt": "free text", "mode": "code"},
                {"id": "c2", "name": "Bad", "prompt": "free text", "mode": "bogus"},
            ],
            default_profile_id="c1",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(cfg)
            with mgr.config_path.open("rb") as handle:
                data = tomllib.load(handle)
            reloaded = WhisperstreamConfig.from_dict(data)
        by_id = {p["id"]: p for p in reloaded.profiles}
        self.assertEqual(by_id["c1"].get("mode"), "code")  # valid mode persists
        self.assertNotIn("mode", by_id["c2"])  # invalid value dropped on save
        self.assertNotIn("mode", by_id["chat"])  # absent stays absent


class TestMigration(unittest.TestCase):
    """Behavior-preserving migration from each legacy mode.

    The first-migration path (no `profiles` key) seeds the email built-in
    alongside chat/code (Task 1.4), so a legacy `enhancement_mode`-only config
    migrated for the first time also gains email.
    """

    def test_migration_default_mode_maps_to_chat(self):
        conf = WhisperstreamConfig.from_dict(
            {"enhancement_mode": "default", "enhancement_custom_prompt": "MYPROMPT"}
        )
        self.assertEqual(conf.default_profile_id, "chat")
        self.assertEqual([p["id"] for p in conf.profiles], ["chat", "code", "email"])
        self.assertEqual(conf.profiles[0]["prompt"], CHAT_PROMPT)
        self.assertEqual(conf.profiles[1]["prompt"], CODE_PROMPT)
        self.assertEqual(conf.profiles[2]["prompt"], EMAIL_PROMPT)

    def test_migration_chat_mode_maps_to_chat(self):
        conf = WhisperstreamConfig.from_dict(
            {"enhancement_mode": "chat", "enhancement_custom_prompt": "MYPROMPT"}
        )
        self.assertEqual(conf.default_profile_id, "chat")
        self.assertEqual([p["id"] for p in conf.profiles], ["chat", "code", "email"])
        self.assertEqual(conf.profiles[0]["prompt"], CHAT_PROMPT)
        self.assertEqual(conf.profiles[1]["prompt"], CODE_PROMPT)
        self.assertEqual(conf.profiles[2]["prompt"], EMAIL_PROMPT)

    def test_migration_code_mode_maps_to_code(self):
        conf = WhisperstreamConfig.from_dict(
            {"enhancement_mode": "code", "enhancement_custom_prompt": "MYPROMPT"}
        )
        self.assertEqual(conf.default_profile_id, "code")
        self.assertEqual([p["id"] for p in conf.profiles], ["chat", "code", "email"])
        self.assertEqual(conf.profiles[1]["prompt"], CODE_PROMPT)
        self.assertEqual(conf.profiles[2]["prompt"], EMAIL_PROMPT)

    def test_migration_custom_mode_appends_custom_profile(self):
        conf = WhisperstreamConfig.from_dict(
            {"enhancement_mode": "custom", "enhancement_custom_prompt": "MYPROMPT"}
        )
        self.assertEqual(conf.default_profile_id, "custom")
        self.assertEqual(
            [p["id"] for p in conf.profiles], ["chat", "code", "email", "custom"]
        )
        self.assertEqual(conf.profiles[0]["prompt"], CHAT_PROMPT)
        self.assertEqual(conf.profiles[1]["prompt"], CODE_PROMPT)
        self.assertEqual(conf.profiles[2]["prompt"], EMAIL_PROMPT)
        # The custom profile is appended after the built-ins.
        custom = next(p for p in conf.profiles if p["id"] == "custom")
        self.assertEqual(custom["name"], "Custom")
        self.assertEqual(custom["prompt"], "MYPROMPT")

    def test_migration_custom_mode_empty_prompt_falls_back_to_chat(self):
        """An empty custom prompt behaves like chat (mirrors _get_system_prompt)."""
        conf = WhisperstreamConfig.from_dict(
            {"enhancement_mode": "custom", "enhancement_custom_prompt": ""}
        )
        self.assertEqual(conf.default_profile_id, "custom")
        self.assertEqual(
            [p["id"] for p in conf.profiles], ["chat", "code", "email", "custom"]
        )
        custom = next(p for p in conf.profiles if p["id"] == "custom")
        self.assertEqual(custom["prompt"], CHAT_PROMPT)

    def test_migration_unrecognized_mode_defaults_to_chat(self):
        """Any unrecognized legacy mode maps the default pointer to chat."""
        conf = WhisperstreamConfig.from_dict({"enhancement_mode": "bogus"})
        self.assertEqual(conf.default_profile_id, "chat")
        self.assertEqual([p["id"] for p in conf.profiles], ["chat", "code", "email"])

    def test_migration_empty_dict_seeds_chat_default(self):
        """A config dict with no enhancement_mode key migrates as 'default' -> chat."""
        conf = WhisperstreamConfig.from_dict({})
        self.assertEqual(conf.default_profile_id, "chat")
        self.assertEqual([p["id"] for p in conf.profiles], ["chat", "code", "email"])

    def test_legacy_migration_seeds_email_built_in(self):
        """Task 1.4: a legacy `enhancement_mode`-only config (no `profiles` key)
        migrated for the first time seeds chat + code + email, with email
        carrying the exact EMAIL_PROMPT and default_profile_id untouched."""
        conf = WhisperstreamConfig.from_dict({"enhancement_mode": "chat"})
        ids = [p["id"] for p in conf.profiles]
        self.assertEqual(ids, ["chat", "code", "email"])
        email_profile = next(p for p in conf.profiles if p["id"] == "email")
        self.assertEqual(email_profile["name"], "Email")
        self.assertEqual(email_profile["prompt"], EMAIL_PROMPT)
        # default_profile_id is not touched by the email seed.
        self.assertEqual(conf.default_profile_id, "chat")

    def test_backfill_appends_email_to_populated_profiles(self):
        """Task 1.4: an existing user's populated profiles=[chat, code] (no
        email) gains the email built-in via the backfill, appended last."""
        conf = WhisperstreamConfig.from_dict(
            {
                "profiles": [
                    {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
                    {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
                ],
                "default_profile_id": "chat",
            }
        )
        ids = [p["id"] for p in conf.profiles]
        self.assertEqual(ids, ["chat", "code", "email"])
        email_profile = conf.profiles[-1]
        self.assertEqual(email_profile["id"], "email")
        self.assertEqual(email_profile["name"], "Email")
        self.assertEqual(email_profile["prompt"], EMAIL_PROMPT)
        # The backfill does not touch the default pointer.
        self.assertEqual(conf.default_profile_id, "chat")
        # Pre-existing built-ins are untouched.
        self.assertEqual(conf.profiles[0]["prompt"], CHAT_PROMPT)
        self.assertEqual(conf.profiles[1]["prompt"], CODE_PROMPT)

    def test_backfill_appends_email_after_custom_profiles(self):
        """The backfill appends email even when the populated array carries a
        user's custom profile; email lands at the end."""
        conf = WhisperstreamConfig.from_dict(
            {
                "profiles": [
                    {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
                    {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
                    {"id": "mine", "name": "Mine", "prompt": "my own prompt"},
                ],
                "default_profile_id": "mine",
            }
        )
        ids = [p["id"] for p in conf.profiles]
        self.assertEqual(ids, ["chat", "code", "mine", "email"])
        self.assertEqual(conf.profiles[-1]["prompt"], EMAIL_PROMPT)
        self.assertEqual(conf.default_profile_id, "mine")
        # The user's custom profile is preserved.
        self.assertEqual(conf.profiles[2]["prompt"], "my own prompt")


class TestMigrationIdempotency(unittest.TestCase):
    def test_present_profiles_key_is_not_regenerated(self):
        """When the incoming dict already carries a `profiles` key, migration
        does NOTHING (present means already migrated)."""
        user_edited = [
            {"id": "chat", "name": "Chat", "prompt": "USER EDITED CHAT"},
            {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
            {"id": "mine", "name": "Mine", "prompt": "my own prompt"},
        ]
        conf = WhisperstreamConfig.from_dict(
            {
                "enhancement_mode": "code",  # legacy mode present but must be ignored
                "profiles": [dict(p) for p in user_edited],
                "default_profile_id": "mine",
            }
        )
        # The legacy mode does NOT regenerate the array: the user's three
        # profiles are preserved exactly (in order, edits intact). The email
        # built-in backfill (Task 1.4) appends email because none was present,
        # but it never re-seeds or clobbers the existing entries.
        self.assertEqual(conf.profiles[:3], user_edited)
        self.assertEqual([p["id"] for p in conf.profiles], ["chat", "code", "mine", "email"])
        self.assertEqual(conf.profiles[-1]["prompt"], EMAIL_PROMPT)
        self.assertEqual(conf.default_profile_id, "mine")
        self.assertEqual(conf.profiles[0]["prompt"], "USER EDITED CHAT")

    def test_user_edit_survives_save_reload_round_trip(self):
        """A user-edited bundled profile survives a save -> reload cycle; the
        migration does not clobber it on the second load."""
        # First load: migrate a fresh chat user.
        migrated = WhisperstreamConfig.from_dict({"enhancement_mode": "chat"})
        # User edits the Chat profile's prompt.
        migrated.profiles[0]["prompt"] = "EDITED BY USER"

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            mgr.save(migrated)
            reloaded = mgr.load()

        self.assertEqual(reloaded.profiles[0]["id"], "chat")
        self.assertEqual(reloaded.profiles[0]["prompt"], "EDITED BY USER")
        # Re-feeding the reloaded dict through from_dict is still idempotent.
        again = WhisperstreamConfig.from_dict(reloaded.to_dict())
        self.assertEqual(again.profiles[0]["prompt"], "EDITED BY USER")
        self.assertEqual(again.profiles, reloaded.profiles)

    def test_email_backfill_is_idempotent_for_present_email(self):
        """Task 1.4: a config that ALREADY has an email profile is not
        re-seeded; the email profile (and the rest) round-trips unchanged."""
        profiles = [
            {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
            {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
            {"id": "email", "name": "Email", "prompt": EMAIL_PROMPT},
        ]
        conf = WhisperstreamConfig.from_dict(
            {
                "profiles": [dict(p) for p in profiles],
                "default_profile_id": "chat",
            }
        )
        # Exactly one email profile, no duplication, profiles preserved.
        self.assertEqual([p["id"] for p in conf.profiles], ["chat", "code", "email"])
        self.assertEqual(conf.profiles, profiles)

    def test_email_backfill_preserves_user_edited_email_prompt(self):
        """Task 1.4: a user who has EDITED the email profile's prompt is never
        clobbered; the custom email prompt round-trips byte-identical and the
        backfill does not re-seed the canonical EMAIL_PROMPT over it."""
        custom_email_prompt = "MY CUSTOM EMAIL CLEANUP PROMPT\nWith two lines."
        self.assertNotEqual(custom_email_prompt, EMAIL_PROMPT)
        profiles = [
            {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
            {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
            {"id": "email", "name": "Email", "prompt": custom_email_prompt},
        ]
        conf = WhisperstreamConfig.from_dict(
            {
                "profiles": [dict(p) for p in profiles],
                "default_profile_id": "email",
            }
        )
        # Still exactly one email profile (no duplicate appended).
        email_profiles = [p for p in conf.profiles if p["id"] == "email"]
        self.assertEqual(len(email_profiles), 1)
        # The user's edited prompt is preserved byte-identical.
        self.assertEqual(email_profiles[0]["prompt"], custom_email_prompt)
        # The whole profiles array round-trips unchanged.
        self.assertEqual(conf.profiles, profiles)
        self.assertEqual(conf.default_profile_id, "email")

    def test_email_backfill_does_not_reseed_when_email_renamed(self):
        """The gate is strictly on 'a profile with id email exists', not name:
        a user who renamed their email profile is never re-seeded."""
        profiles = [
            {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
            {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
            {"id": "email", "name": "My Mail", "prompt": "renamed and edited"},
        ]
        conf = WhisperstreamConfig.from_dict(
            {
                "profiles": [dict(p) for p in profiles],
                "default_profile_id": "chat",
            }
        )
        email_profiles = [p for p in conf.profiles if p["id"] == "email"]
        self.assertEqual(len(email_profiles), 1)
        self.assertEqual(email_profiles[0]["name"], "My Mail")
        self.assertEqual(email_profiles[0]["prompt"], "renamed and edited")
        self.assertEqual(conf.profiles, profiles)


class TestNewInstallSeeding(unittest.TestCase):
    def test_load_no_file_seeds_bundled_profiles(self):
        """A first run (no config.toml) yields all three bundled profiles
        (chat, code, email) + default_profile_id='chat'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            # Sanity: the path does not exist yet.
            self.assertFalse(mgr.config_path.exists())
            config = mgr.load()

        self.assertEqual(
            config.profiles,
            [
                {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
                {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
                {"id": "email", "name": "Email", "prompt": EMAIL_PROMPT},
            ],
        )
        self.assertEqual(config.default_profile_id, "chat")
        config.validate()  # The seeded config passes the new profile validation.

    def test_load_no_file_seeds_email_built_in_profile(self):
        """A first run (no config.toml) yields chat, code, AND email built-in
        profiles; default_profile_id stays 'chat'; validate() passes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            self.assertFalse(mgr.config_path.exists())
            config = mgr.load()

        # Email profile is the third entry.
        self.assertEqual(len(config.profiles), 3)
        email_profile = config.profiles[2]
        self.assertEqual(email_profile["id"], "email")
        self.assertEqual(email_profile["name"], "Email")
        self.assertEqual(email_profile["prompt"], EMAIL_PROMPT)

        # Default stays chat (email is selected by routing / manual pick).
        self.assertEqual(config.default_profile_id, "chat")

        # The full set is exactly chat, code, email in order.
        self.assertEqual(
            config.profiles,
            [
                {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
                {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
                {"id": "email", "name": "Email", "prompt": EMAIL_PROMPT},
            ],
        )
        config.validate()  # The seeded config with three built-ins must validate.


class TestProfileValidation(unittest.TestCase):
    def test_empty_profile_id_fails(self):
        conf = WhisperstreamConfig(
            profiles=[{"id": "", "name": "Blank", "prompt": "x"}],
            default_profile_id="",
        )
        with self.assertRaisesRegex(ValueError, "profile id must be non-empty"):
            conf.validate()

    def test_missing_profile_id_fails(self):
        conf = WhisperstreamConfig(
            profiles=[{"name": "NoId", "prompt": "x"}],
            default_profile_id="",
        )
        with self.assertRaisesRegex(ValueError, "profile id must be non-empty"):
            conf.validate()

    def test_duplicate_profile_ids_fail(self):
        conf = WhisperstreamConfig(
            profiles=[
                {"id": "dup", "name": "One", "prompt": "x"},
                {"id": "dup", "name": "Two", "prompt": "y"},
            ],
            default_profile_id="dup",
        )
        with self.assertRaisesRegex(ValueError, "profile id must be unique"):
            conf.validate()

    def test_dangling_default_profile_id_fails(self):
        conf = WhisperstreamConfig(
            profiles=[{"id": "chat", "name": "Chat", "prompt": "x"}],
            default_profile_id="nope",
        )
        with self.assertRaisesRegex(
            ValueError, "default_profile_id must reference an existing profile"
        ):
            conf.validate()

    def test_bundled_config_passes(self):
        conf = WhisperstreamConfig(
            profiles=[
                {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
                {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
            ],
            default_profile_id="chat",
        )
        conf.validate()  # Should not raise

    def test_default_constructed_empty_config_passes(self):
        """Empty profiles + empty default_profile_id (pre-seed state) is valid."""
        conf = WhisperstreamConfig()
        conf.validate()  # Should not raise

    def test_legacy_enhancement_mode_check_still_lenient(self):
        """The legacy enhancement_mode validation is unchanged: a config
        carrying a valid legacy mode + valid profiles still validates."""
        conf = WhisperstreamConfig(
            enhancement_mode="code",
            profiles=[{"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT}],
            default_profile_id="chat",
        )
        conf.validate()  # Should not raise


class TestSystemPromptResolution(unittest.TestCase):
    """Engine resolution: `_get_system_prompt(profile_id)` resolves the active
    profile's prompt by `profile_id or default_profile_id`, falling back to
    CHAT_PROMPT when the resolved id matches no profile (Task 1.7/1.8)."""

    CUSTOM_PROMPT = "MY CUSTOM CLEANUP PROMPT"

    def _make_enhancer(self, default_profile_id: str = "chat") -> LLMEnhancer:
        """Build an enhancer over a config carrying bundled chat/code + a
        custom profile. No network/provider/logger needed for prompt
        resolution; the default no-op logger is fine."""
        config = WhisperstreamConfig(
            profiles=[
                {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
                {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
                {"id": "custom", "name": "Custom", "prompt": self.CUSTOM_PROMPT},
            ],
            default_profile_id=default_profile_id,
        )
        return LLMEnhancer(config)

    def test_no_arg_resolves_default_profile_prompt(self):
        """With no profile_id, resolve the default_profile_id's prompt."""
        enhancer = self._make_enhancer(default_profile_id="code")
        self.assertEqual(enhancer._get_system_prompt(), CODE_PROMPT)

    def test_code_profile_id_returns_code_prompt(self):
        enhancer = self._make_enhancer()
        self.assertEqual(enhancer._get_system_prompt("code"), CODE_PROMPT)

    def test_custom_profile_id_returns_custom_prompt(self):
        enhancer = self._make_enhancer()
        self.assertEqual(enhancer._get_system_prompt("custom"), self.CUSTOM_PROMPT)

    def test_unknown_profile_id_falls_back_to_chat(self):
        enhancer = self._make_enhancer()
        self.assertEqual(enhancer._get_system_prompt("does-not-exist"), CHAT_PROMPT)

    def test_dangling_default_profile_id_falls_back_to_chat(self):
        """default_profile_id points at a missing id and no arg given:
        fall back to CHAT_PROMPT."""
        enhancer = self._make_enhancer(default_profile_id="missing")
        self.assertEqual(enhancer._get_system_prompt(), CHAT_PROMPT)

    # ------------------------------------------------------------------
    # Provider-aware email built-in (Task 1.2): the email profile stores the
    # cloud v6 prompt (EMAIL_PROMPT); _get_system_prompt swaps to the local v14
    # prompt (LOCAL_EMAIL_PROMPT) ONLY when the resolved profile id is "email"
    # AND enhancement_provider lowercased == "local". The branch must not fire
    # for any other profile or provider.
    # ------------------------------------------------------------------

    def _make_email_enhancer(
        self,
        enhancement_provider: str,
        default_profile_id: str = "chat",
    ) -> LLMEnhancer:
        """Build an enhancer whose profiles include the email built-in (storing
        the cloud v6 EMAIL_PROMPT) plus chat/code, with a chosen provider."""
        config = WhisperstreamConfig(
            profiles=[
                {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
                {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
                {"id": "email", "name": "Email", "prompt": EMAIL_PROMPT},
            ],
            default_profile_id=default_profile_id,
            enhancement_provider=enhancement_provider,
            # Customizable (Gemma) is the editable-prompt local model.
            enhancement_local_model="gemma4-e2b-instruct",
        )
        return LLMEnhancer(config)

    def test_email_profile_local_provider_returns_local_v14(self):
        """email profile + provider=local -> LOCAL_EMAIL_PROMPT (v14)."""
        enhancer = self._make_email_enhancer(enhancement_provider="local")
        self.assertEqual(enhancer._get_system_prompt("email"), LOCAL_EMAIL_PROMPT)

    def test_email_profile_gemini_provider_returns_cloud_v6(self):
        """email profile + provider=gemini -> EMAIL_PROMPT (v6, the stored prompt)."""
        enhancer = self._make_email_enhancer(enhancement_provider="gemini")
        self.assertEqual(enhancer._get_system_prompt("email"), EMAIL_PROMPT)

    def test_email_profile_ollama_provider_returns_cloud_v6(self):
        """ollama is NOT local: email profile + provider=ollama -> EMAIL_PROMPT (v6)."""
        enhancer = self._make_email_enhancer(enhancement_provider="ollama")
        self.assertEqual(enhancer._get_system_prompt("email"), EMAIL_PROMPT)

    def test_local_provider_non_email_profile_unaffected(self):
        """provider=local + chat: the email swap must NOT fire; with the default
        gemma local model (prompt_key "gemma") chat resolves to the frozen
        GEMMA_LOCAL_CHAT_PROMPT, not the cloud c5 CHAT_PROMPT (the #267 split).
        Distinct from CHAT_PROMPT (cloud) and LOCAL_EMAIL_PROMPT (email)."""
        enhancer = self._make_email_enhancer(enhancement_provider="local")
        self.assertEqual(enhancer._get_system_prompt("chat"), GEMMA_LOCAL_CHAT_PROMPT)
        self.assertNotEqual(enhancer._get_system_prompt("chat"), CHAT_PROMPT)

    def test_email_via_default_pointer_local_provider_returns_local_v14(self):
        """The branch resolves the id the same way the function does
        (profile_id or default_profile_id): a local provider whose default
        profile IS email (no explicit arg) still gets v14."""
        enhancer = self._make_email_enhancer(
            enhancement_provider="local", default_profile_id="email"
        )
        self.assertEqual(enhancer._get_system_prompt(), LOCAL_EMAIL_PROMPT)


if __name__ == "__main__":
    unittest.main()
