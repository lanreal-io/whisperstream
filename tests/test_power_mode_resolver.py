"""Tests for the pure per-app and per-site profile resolvers (PR-B / #160).

Exercises ``resolve_profile_for_app`` and ``resolve_profile_for_target``
(``src/whisperstream/power_mode.py``):
- ``resolve_profile_for_app``: case-insensitive process-name lookup against
  ``power_mode_map``, stale-mapping (deleted-profile-id) safety, and ``None``
  fallbacks that mean "use the global default profile".
- ``resolve_profile_for_target``: domain-aware resolver implementing the locked
  precedence site -> app -> default. A ``site_map`` hit for an EXISTING profile
  wins over the app rule; a stale site entry (pointing at a deleted profile) falls
  through to the app rule; ``app=None`` or ``app.domain=None`` delegates cleanly to
  ``resolve_profile_for_app``.

``RoutingTarget`` and ``WhisperstreamConfig`` are constructed directly (no
disk) so each case isolates a single resolver branch.
"""

import unittest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.foreground_window import RoutingTarget
from whisperstream.power_mode import (
    resolve_profile_for_app,
    resolve_profile_for_target,
    routing_probe_requirements,
)


def _app(process_name: str, domain: str | None = None) -> RoutingTarget:
    """Build a minimum target with a process name and optional domain."""
    return RoutingTarget(process_name=process_name, domain=domain)


def _config(
    power_mode_map=None,
    site_map=None,
    profiles=None,
) -> WhisperstreamConfig:
    """A config carrying one or two profiles, with optional map overrides.

    Default: a single ``code`` profile, ``power_mode_map={"code.exe": "code"}``,
    ``site_map={}``.
    """
    if profiles is None:
        profiles = [{"id": "code", "name": "Code", "prompt": "p"}]
    if power_mode_map is None:
        power_mode_map = {"code.exe": "code"}
    if site_map is None:
        site_map = {}
    return WhisperstreamConfig(
        profiles=profiles,
        default_profile_id="code",
        power_mode_map=power_mode_map,
        site_map=site_map,
    )


class TestRoutingProbeRequirements(unittest.TestCase):
    def test_no_rules_or_override_need_no_foreground_read(self):
        cfg = _config(power_mode_map={}, site_map={})
        self.assertEqual(routing_probe_requirements(cfg), (False, False))

    def test_app_rule_needs_process_but_not_browser_uia(self):
        cfg = _config(power_mode_map={"code.exe": "code"}, site_map={})
        self.assertEqual(routing_probe_requirements(cfg), (True, False))

    def test_site_rule_needs_process_and_browser_uia_capability(self):
        cfg = _config(power_mode_map={}, site_map={"example.com": "code"})
        self.assertEqual(routing_probe_requirements(cfg), (True, True))

    def test_live_manual_override_needs_process_only(self):
        cfg = _config(power_mode_map={}, site_map={})
        self.assertEqual(
            routing_probe_requirements(cfg, manual_profile_id="code"),
            (True, False),
        )

    def test_stale_rules_and_override_do_not_trigger_reads(self):
        cfg = _config(
            power_mode_map={"code.exe": "deleted"},
            site_map={"example.com": "deleted"},
        )
        self.assertEqual(
            routing_probe_requirements(cfg, manual_profile_id="deleted"),
            (False, False),
        )


class TestResolveProfileForApp(unittest.TestCase):
    def test_none_app_returns_none(self):
        """No foreground app detected -> use the global default (None)."""
        self.assertIsNone(resolve_profile_for_app(None, _config()))

    def test_empty_process_name_returns_none(self):
        """A falsy process name -> None (cannot key the map)."""
        self.assertIsNone(resolve_profile_for_app(_app(""), _config()))

    def test_unmapped_process_name_returns_none(self):
        """A process name with no power_mode_map entry -> None."""
        self.assertIsNone(
            resolve_profile_for_app(_app("notepad.exe"), _config())
        )

    def test_mapped_exact_case_returns_profile_id(self):
        """A mapped process name (stored key matches exactly) pointing at an
        existing profile -> that profile id."""
        self.assertEqual(
            resolve_profile_for_app(_app("code.exe"), _config()),
            "code",
        )

    def test_case_insensitive_lookup(self):
        """Stored key ``code.exe``, app reports ``Code.exe`` -> ``code``
        (lookup lowercases the process name)."""
        self.assertEqual(
            resolve_profile_for_app(_app("Code.exe"), _config()),
            "code",
        )

    def test_mixed_case_process_name_resolves(self):
        """An aggressively mixed-case process name still resolves to the
        lowercased stored key."""
        self.assertEqual(
            resolve_profile_for_app(_app("CoDe.ExE"), _config()),
            "code",
        )

    def test_stale_mapping_returns_none(self):
        """A mapping whose value points at a profile id NOT in
        config.profiles (e.g. the profile was deleted) -> None, so the caller
        falls back to the global default."""
        config = _config(power_mode_map={"code.exe": "ghost"})
        self.assertIsNone(resolve_profile_for_app(_app("code.exe"), config))

    def test_empty_power_mode_map_returns_none(self):
        """An empty map (the existing-user pre-opt-in state) -> None for any
        app."""
        config = _config(power_mode_map={})
        self.assertIsNone(resolve_profile_for_app(_app("code.exe"), config))

    def test_never_raises_across_all_branches(self):
        """The resolver is pure and must never raise, regardless of input
        shape (None app, empty name, unmapped, mapped, stale)."""
        config = _config(power_mode_map={"code.exe": "code", "stale.exe": "ghost"})
        cases = [
            None,
            _app(""),
            _app("notepad.exe"),
            _app("code.exe"),
            _app("Code.exe"),
            _app("stale.exe"),
        ]
        for app in cases:
            with self.subTest(app=app):
                try:
                    resolve_profile_for_app(app, config)
                except Exception as exc:  # noqa: BLE001 - the whole point is no raise
                    self.fail(f"resolve_profile_for_app raised {exc!r}")


class TestResolveProfileForTarget(unittest.TestCase):
    """Tests for ``resolve_profile_for_target`` — the domain-aware entry point.

    Precedence matrix (locked in scope.md): site -> app -> default.
    Each test case isolates one branch of the matrix.
    """

    # ------------------------------------------------------------------
    # Helper: two-profile config with both maps populated
    # ------------------------------------------------------------------

    def _two_profile_config(
        self,
        site_map=None,
        power_mode_map=None,
    ) -> WhisperstreamConfig:
        """Config with ``code`` + ``chat`` profiles.

        Default maps: ``chrome.exe -> code`` (app), ``github.com -> chat`` (site).
        """
        profiles = [
            {"id": "code", "name": "Code", "prompt": "p"},
            {"id": "chat", "name": "Chat", "prompt": "q"},
        ]
        return _config(
            profiles=profiles,
            power_mode_map={"chrome.exe": "code"} if power_mode_map is None else power_mode_map,
            site_map={"github.com": "chat"} if site_map is None else site_map,
        )

    # ------------------------------------------------------------------
    # 1. Site hit: domain maps to an existing profile -> site profile wins
    # ------------------------------------------------------------------

    def test_site_hit_returns_site_profile(self):
        """Domain in site_map pointing at an existing profile -> that profile
        (not the app rule)."""
        cfg = self._two_profile_config()
        app = _app("chrome.exe", domain="github.com")
        self.assertEqual(resolve_profile_for_target(app, cfg), "chat")

    def test_site_hit_wins_over_app_rule(self):
        """When BOTH a site rule and an app rule exist, the site rule wins."""
        # chrome.exe -> code (app rule) AND github.com -> chat (site rule)
        cfg = self._two_profile_config()
        app = _app("chrome.exe", domain="github.com")
        # site should win
        self.assertEqual(resolve_profile_for_target(app, cfg), "chat")
        # cross-check: without the domain, the app rule would kick in
        app_no_domain = _app("chrome.exe", domain=None)
        self.assertEqual(resolve_profile_for_target(app_no_domain, cfg), "code")

    # ------------------------------------------------------------------
    # 2. Site miss -> app hit: domain not in site_map, process_name IS mapped
    # ------------------------------------------------------------------

    def test_site_miss_falls_through_to_app_rule(self):
        """Domain not in site_map -> falls through to the app rule."""
        cfg = self._two_profile_config()
        # stackoverflow.com is not in site_map; chrome.exe -> code
        app = _app("chrome.exe", domain="stackoverflow.com")
        self.assertEqual(resolve_profile_for_target(app, cfg), "code")

    def test_no_domain_falls_through_to_app_rule(self):
        """app.domain is None -> delegates to resolve_profile_for_app."""
        cfg = self._two_profile_config()
        app = _app("chrome.exe", domain=None)
        self.assertEqual(resolve_profile_for_target(app, cfg), "code")

    # ------------------------------------------------------------------
    # 3. Site stale: domain in site_map but profile deleted -> fall through
    # ------------------------------------------------------------------

    def test_stale_site_entry_falls_through_to_app_rule(self):
        """site_map value points at a deleted/nonexistent profile -> falls
        through to the app rule (not to None/default)."""
        cfg = _config(
            profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
            site_map={"github.com": "ghost"},      # ghost profile does not exist
            power_mode_map={"chrome.exe": "code"},
        )
        app = _app("chrome.exe", domain="github.com")
        # stale site entry: ghost doesn't exist -> fall through to app rule -> code
        self.assertEqual(resolve_profile_for_target(app, cfg), "code")

    def test_stale_site_entry_no_app_rule_returns_none(self):
        """site_map value is stale AND no app rule -> None (global default)."""
        cfg = _config(
            profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
            site_map={"github.com": "ghost"},
            power_mode_map={},                     # no app rule either
        )
        app = _app("chrome.exe", domain="github.com")
        self.assertIsNone(resolve_profile_for_target(app, cfg))

    # ------------------------------------------------------------------
    # 4. No domain AND no app rule -> None (global default)
    # ------------------------------------------------------------------

    def test_neither_site_nor_app_returns_none(self):
        """No domain and no app rule -> None (caller uses global default)."""
        cfg = _config(
            site_map={},
            power_mode_map={},
        )
        app = _app("chrome.exe", domain=None)
        self.assertIsNone(resolve_profile_for_target(app, cfg))

    # ------------------------------------------------------------------
    # 5. app=None -> delegates cleanly (same as resolve_profile_for_app(None))
    # ------------------------------------------------------------------

    def test_none_app_delegates_to_app_resolver(self):
        """app=None -> returns None (same as resolve_profile_for_app(None, ...))."""
        cfg = self._two_profile_config()
        self.assertIsNone(resolve_profile_for_target(None, cfg))

    # ------------------------------------------------------------------
    # 6. Domain lookup is case-insensitive
    # ------------------------------------------------------------------

    def test_domain_lookup_is_case_insensitive(self):
        """site_map keys are lowercase; app.domain may arrive mixed-case from
        the UIA read -> lookup lowercases before matching."""
        cfg = _config(
            profiles=[{"id": "code", "name": "Code", "prompt": "p"}],
            site_map={"github.com": "code"},
            power_mode_map={},
        )
        app = _app("chrome.exe", domain="GitHub.COM")
        self.assertEqual(resolve_profile_for_target(app, cfg), "code")

    # ------------------------------------------------------------------
    # 7. Pure / never raises
    # ------------------------------------------------------------------

    def test_never_raises_across_all_branches(self):
        """resolve_profile_for_target is pure and must never raise."""
        cfg = self._two_profile_config()
        cases = [
            None,
            _app("chrome.exe", domain=None),
            _app("chrome.exe", domain="github.com"),
            _app("chrome.exe", domain="stackoverflow.com"),
            _app("notepad.exe", domain=None),
            _app("notepad.exe", domain="github.com"),
            _app("", domain="github.com"),
        ]
        for app in cases:
            with self.subTest(app=app):
                try:
                    resolve_profile_for_target(app, cfg)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"resolve_profile_for_target raised {exc!r}")


class TestEmailAutoActivationRouting(unittest.TestCase):
    """Task 2.4 / Task 2.5: resolver routes email apps and mail sites to 'email'."""

    _EMAIL_PROFILES = [
        {"id": "chat", "name": "Chat", "prompt": "c"},
        {"id": "code", "name": "Code", "prompt": "k"},
        {"id": "email", "name": "Email", "prompt": "e"},
    ]

    def _email_config(self) -> WhisperstreamConfig:
        """Config mirroring the new-install seed: outlook/thunderbird in
        power_mode_map and the three mail domains in site_map, plus all three
        built-in profiles so the stale-mapping guard doesn't prune them."""
        return WhisperstreamConfig(
            profiles=self._EMAIL_PROFILES,
            default_profile_id="chat",
            power_mode_map={
                "code.exe": "code",
                "outlook.exe": "email",
                "thunderbird.exe": "email",
            },
            site_map={
                "mail.google.com": "email",
                "outlook.office.com": "email",
                "outlook.live.com": "email",
            },
        )

    # ------------------------------------------------------------------
    # App-level routing (power_mode_map)
    # ------------------------------------------------------------------

    def test_outlook_exe_routes_to_email(self):
        """outlook.exe -> 'email' via power_mode_map app rule."""
        cfg = self._email_config()
        self.assertEqual(resolve_profile_for_target(_app("outlook.exe"), cfg), "email")

    def test_thunderbird_exe_routes_to_email(self):
        """thunderbird.exe -> 'email' via power_mode_map app rule."""
        cfg = self._email_config()
        self.assertEqual(resolve_profile_for_target(_app("thunderbird.exe"), cfg), "email")

    def test_outlook_exe_case_insensitive(self):
        """Outlook.EXE (mixed case) still resolves to 'email'."""
        cfg = self._email_config()
        self.assertEqual(resolve_profile_for_target(_app("Outlook.EXE"), cfg), "email")

    # ------------------------------------------------------------------
    # Site-level routing (site_map)
    # ------------------------------------------------------------------

    def test_mail_google_com_routes_to_email(self):
        """A foreground app with domain=mail.google.com -> 'email' via site rule."""
        cfg = self._email_config()
        app = _app("chrome.exe", domain="mail.google.com")
        self.assertEqual(resolve_profile_for_target(app, cfg), "email")

    def test_outlook_office_com_routes_to_email(self):
        """domain=outlook.office.com -> 'email' via site rule."""
        cfg = self._email_config()
        app = _app("msedge.exe", domain="outlook.office.com")
        self.assertEqual(resolve_profile_for_target(app, cfg), "email")

    def test_outlook_live_com_routes_to_email(self):
        """domain=outlook.live.com -> 'email' via site rule."""
        cfg = self._email_config()
        app = _app("chrome.exe", domain="outlook.live.com")
        self.assertEqual(resolve_profile_for_target(app, cfg), "email")

    def test_site_rule_wins_over_app_rule_for_email(self):
        """When chrome.exe is also mapped to code (app rule) but domain is
        mail.google.com (site rule), the site rule wins -> 'email'."""
        cfg = WhisperstreamConfig(
            profiles=self._EMAIL_PROFILES,
            default_profile_id="chat",
            power_mode_map={"chrome.exe": "code"},
            site_map={"mail.google.com": "email"},
        )
        app = _app("chrome.exe", domain="mail.google.com")
        self.assertEqual(resolve_profile_for_target(app, cfg), "email")

    # ------------------------------------------------------------------
    # Non-email falls through to default
    # ------------------------------------------------------------------

    def test_non_email_app_falls_through_to_code(self):
        """code.exe -> 'code' (not 'email') — email routing is narrowly scoped."""
        cfg = self._email_config()
        self.assertEqual(resolve_profile_for_target(_app("code.exe"), cfg), "code")

    def test_unknown_domain_falls_through_to_app_rule(self):
        """An unmapped domain (e.g. github.com) in a browser routes via the
        app rule, not the email site rule."""
        cfg = self._email_config()
        app = _app("chrome.exe", domain="github.com")
        # chrome.exe is not in power_mode_map for this config -> None
        self.assertIsNone(resolve_profile_for_target(app, cfg))

    # ------------------------------------------------------------------
    # Validate: seeded email routes survive validate() without pruning
    # ------------------------------------------------------------------

    def test_seeded_email_routes_survive_validate(self):
        """The email config validate()s clean: outlook/thunderbird/mail-domain
        entries reference the existing 'email' profile and are NOT pruned."""
        cfg = self._email_config()
        before_app = dict(cfg.power_mode_map)
        before_site = dict(cfg.site_map)
        cfg.validate()  # Must not raise.
        self.assertEqual(cfg.power_mode_map, before_app)
        self.assertEqual(cfg.site_map, before_site)


if __name__ == "__main__":
    unittest.main()
