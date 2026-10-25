"""Tests for add_hooks / remove_hooks pure helpers (Task 1.6).

These functions operate on in-memory dicts only — no disk I/O.
Schema reference (Claude Code settings.json):

    {
      "hooks": {
        "Stop": [
          {
            "hooks": [{"type": "command", "command": "..."}]
          }
        ],
        "Notification": [
          {
            "matcher": "permission_prompt",
            "hooks": [{"type": "command", "command": "..."}]
          }
        ]
      }
    }

Our entries are identified by the forwarder_path appearing in
the hook command string.  All other entries are preserved.
"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from whisperstream.claude_hud import (
    _PLUGIN_TEMPLATE_FILES,
    ClaudePluginConflictError,
    _numeric_version,
    _plugin_version,
    add_hooks,
    add_plugin_settings,
    install_claude_plugin,
    reconcile_claude_plugin,
    remove_claude_plugin,
    remove_hooks,
    remove_plugin_settings,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[1]

FORWARDER = r"C:\Users\Test\AppData\Local\Whisperstream\claude-hud\forwarder.ps1"
OTHER_FORWARDER = r"C:\Users\Test\AppData\Local\Whisperstream\claude-hud\other.ps1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _our_stop_entry(forwarder: str) -> dict:
    """The Stop entry we expect add_hooks to produce."""
    return {
        "hooks": [
            {"type": "command", "command": f'powershell -NoProfile -File "{forwarder}"'}
        ]
    }


def _our_notification_entry(forwarder: str) -> dict:
    """The Notification entry we expect add_hooks to produce."""
    return {
        "matcher": "permission_prompt",
        "hooks": [
            {"type": "command", "command": f'powershell -NoProfile -File "{forwarder}"'}
        ]
    }


def _user_stop_entry() -> dict:
    """A pre-existing user Stop hook that must survive add/remove."""
    return {
        "hooks": [
            {"type": "command", "command": "C:\\Users\\Test\\scripts\\my-stop-hook.ps1"}
        ]
    }


def _user_notification_entry() -> dict:
    """A pre-existing user Notification hook that must survive add/remove."""
    return {
        "matcher": "some_other_event",
        "hooks": [
            {"type": "command", "command": "C:\\Users\\Test\\scripts\\my-notif-hook.ps1"}
        ]
    }


# ---------------------------------------------------------------------------
# add_hooks
# ---------------------------------------------------------------------------

class TestAddHooks:
    def test_empty_settings_adds_both_hooks(self):
        """add_hooks({}, ...) produces Stop and Notification entries."""
        result = add_hooks({}, FORWARDER)

        assert "hooks" in result
        hooks = result["hooks"]
        assert "Stop" in hooks
        assert "Notification" in hooks

        stop_entries = hooks["Stop"]
        assert isinstance(stop_entries, list)
        assert len(stop_entries) == 1
        assert stop_entries[0] == _our_stop_entry(FORWARDER)

        notif_entries = hooks["Notification"]
        assert isinstance(notif_entries, list)
        assert len(notif_entries) == 1
        assert notif_entries[0] == _our_notification_entry(FORWARDER)

    def test_stop_has_no_matcher(self):
        """Our Stop entry must NOT have a 'matcher' key (Stop fires for all)."""
        result = add_hooks({}, FORWARDER)
        stop_entry = result["hooks"]["Stop"][0]
        assert "matcher" not in stop_entry

    def test_notification_has_permission_prompt_matcher(self):
        """Our Notification entry must have matcher == 'permission_prompt'."""
        result = add_hooks({}, FORWARDER)
        notif_entry = result["hooks"]["Notification"][0]
        assert notif_entry.get("matcher") == "permission_prompt"

    def test_does_not_mutate_input(self):
        """add_hooks must return a new dict and leave the input unchanged."""
        original = {}
        result = add_hooks(original, FORWARDER)
        assert result is not original
        assert original == {}

    def test_preserves_existing_unrelated_stop_hook(self):
        """Existing user Stop hooks survive add_hooks."""
        user_stop = _user_stop_entry()
        settings = {"hooks": {"Stop": [copy.deepcopy(user_stop)]}}
        result = add_hooks(settings, FORWARDER)

        stop_list = result["hooks"]["Stop"]
        # Both ours and theirs must be present.
        assert any(
            forwarder_in_command(e, FORWARDER) for e in stop_list
        ), "Our Stop entry not found"
        assert any(
            not forwarder_in_command(e, FORWARDER) for e in stop_list
        ), "User Stop entry was removed"

    def test_preserves_existing_unrelated_notification_hook(self):
        """Existing user Notification hooks survive add_hooks."""
        user_notif = _user_notification_entry()
        settings = {"hooks": {"Notification": [copy.deepcopy(user_notif)]}}
        result = add_hooks(settings, FORWARDER)

        notif_list = result["hooks"]["Notification"]
        assert any(
            forwarder_in_command(e, FORWARDER) for e in notif_list
        ), "Our Notification entry not found"
        assert any(
            not forwarder_in_command(e, FORWARDER) for e in notif_list
        ), "User Notification entry was removed"

    def test_preserves_other_top_level_keys(self):
        """Non-hooks keys in settings are left untouched."""
        settings = {"permissions": {"allow": ["Bash(*)"]}, "theme": "dark"}
        result = add_hooks(settings, FORWARDER)
        assert result["permissions"] == {"allow": ["Bash(*)"]}
        assert result["theme"] == "dark"

    def test_idempotent_double_add(self):
        """Calling add_hooks twice does not duplicate our entries."""
        once = add_hooks({}, FORWARDER)
        twice = add_hooks(once, FORWARDER)

        stop_ours = [e for e in twice["hooks"]["Stop"] if forwarder_in_command(e, FORWARDER)]
        notif_ours = [e for e in twice["hooks"]["Notification"] if forwarder_in_command(e, FORWARDER)]
        assert len(stop_ours) == 1, f"Expected 1 Stop entry, got {len(stop_ours)}"
        assert len(notif_ours) == 1, f"Expected 1 Notification entry, got {len(notif_ours)}"

    def test_idempotent_with_user_hooks_present(self):
        """Double-add is idempotent even when user hooks exist alongside ours."""
        user_stop = _user_stop_entry()
        settings = {"hooks": {"Stop": [copy.deepcopy(user_stop)]}}
        once = add_hooks(settings, FORWARDER)
        twice = add_hooks(once, FORWARDER)

        stop_ours = [e for e in twice["hooks"]["Stop"] if forwarder_in_command(e, FORWARDER)]
        assert len(stop_ours) == 1

    def test_missing_hooks_key_is_created(self):
        """settings without a 'hooks' key at all is handled gracefully."""
        settings = {"theme": "dark"}
        result = add_hooks(settings, FORWARDER)
        assert "hooks" in result
        assert "Stop" in result["hooks"]
        assert "Notification" in result["hooks"]

    def test_command_embeds_forwarder_path(self):
        """The hook command string contains the forwarder_path as the stable marker."""
        result = add_hooks({}, FORWARDER)
        stop_cmd = result["hooks"]["Stop"][0]["hooks"][0]["command"]
        notif_cmd = result["hooks"]["Notification"][0]["hooks"][0]["command"]
        assert FORWARDER in stop_cmd
        assert FORWARDER in notif_cmd

    def test_each_entry_has_type_command(self):
        """Inner hook items must have type='command'."""
        result = add_hooks({}, FORWARDER)
        for event in ("Stop", "Notification"):
            for outer_entry in result["hooks"][event]:
                for inner in outer_entry["hooks"]:
                    assert inner.get("type") == "command"


# ---------------------------------------------------------------------------
# remove_hooks
# ---------------------------------------------------------------------------

class TestRemoveHooks:
    def test_empty_settings_no_op(self):
        """remove_hooks({}, ...) returns an empty dict (or equivalent) cleanly."""
        result = remove_hooks({}, FORWARDER)
        # No 'hooks' key because there is nothing to keep.
        assert result == {} or result.get("hooks") in (None, {})

    def test_does_not_mutate_input(self):
        """remove_hooks must return a new dict and leave the input unchanged."""
        settings = add_hooks({}, FORWARDER)
        original_copy = copy.deepcopy(settings)
        remove_hooks(settings, FORWARDER)
        assert settings == original_copy

    def test_removes_our_stop_entry(self):
        """Our Stop entry is removed by remove_hooks."""
        settings = add_hooks({}, FORWARDER)
        result = remove_hooks(settings, FORWARDER)
        stop_list = result.get("hooks", {}).get("Stop", [])
        assert not any(forwarder_in_command(e, FORWARDER) for e in stop_list)

    def test_removes_our_notification_entry(self):
        """Our Notification entry is removed by remove_hooks."""
        settings = add_hooks({}, FORWARDER)
        result = remove_hooks(settings, FORWARDER)
        notif_list = result.get("hooks", {}).get("Notification", [])
        assert not any(forwarder_in_command(e, FORWARDER) for e in notif_list)

    def test_user_stop_hook_survives_remove(self):
        """User's Stop hook is intact after remove_hooks prunes ours."""
        user_stop = _user_stop_entry()
        settings = {"hooks": {"Stop": [copy.deepcopy(user_stop)]}}
        settings = add_hooks(settings, FORWARDER)
        result = remove_hooks(settings, FORWARDER)

        stop_list = result.get("hooks", {}).get("Stop", [])
        assert any(
            not forwarder_in_command(e, FORWARDER) for e in stop_list
        ), "User Stop hook was wrongly removed"

    def test_user_notification_hook_survives_remove(self):
        """User's Notification hook is intact after remove_hooks prunes ours."""
        user_notif = _user_notification_entry()
        settings = {"hooks": {"Notification": [copy.deepcopy(user_notif)]}}
        settings = add_hooks(settings, FORWARDER)
        result = remove_hooks(settings, FORWARDER)

        notif_list = result.get("hooks", {}).get("Notification", [])
        assert any(
            not forwarder_in_command(e, FORWARDER) for e in notif_list
        ), "User Notification hook was wrongly removed"

    def test_remove_when_never_present_is_noop(self):
        """remove_hooks on a settings that never had our hooks is a no-op."""
        user_stop = _user_stop_entry()
        settings = {"hooks": {"Stop": [copy.deepcopy(user_stop)]}}
        result = remove_hooks(settings, FORWARDER)
        # User stop hook must still be there.
        stop_list = result.get("hooks", {}).get("Stop", [])
        assert len(stop_list) == 1
        assert not forwarder_in_command(stop_list[0], FORWARDER)

    def test_remove_when_hooks_key_absent(self):
        """remove_hooks when settings has no 'hooks' key is a no-op."""
        settings = {"theme": "dark"}
        result = remove_hooks(settings, FORWARDER)
        assert result.get("theme") == "dark"

    def test_empty_event_array_cleaned_up(self):
        """After removing our only Stop entry, Stop key is absent or empty list (clean)."""
        settings = add_hooks({}, FORWARDER)
        result = remove_hooks(settings, FORWARDER)
        # After removing all our hooks, Stop / Notification should not have empty arrays
        # polluting the dict (implementation may either remove the key or leave []).
        # Either is acceptable, but empty lists for BOTH means the hooks key may be empty too.
        hooks = result.get("hooks", {})
        stop_list = hooks.get("Stop", [])
        notif_list = hooks.get("Notification", [])
        assert stop_list == [], f"Stop list should be empty or absent, got {stop_list}"
        assert notif_list == [], f"Notification list should be empty or absent, got {notif_list}"

    def test_empty_hooks_dict_cleaned_up(self):
        """After removing all our hooks from an otherwise-empty dict, 'hooks' is cleaned up."""
        settings = add_hooks({}, FORWARDER)
        result = remove_hooks(settings, FORWARDER)
        # Either 'hooks' is absent or is an empty dict — both are acceptable.
        hooks = result.get("hooks", {})
        # All event arrays under hooks should be empty.
        for event_list in hooks.values():
            assert event_list == [], f"Expected empty list, got {event_list}"

    def test_preserves_other_top_level_keys_on_remove(self):
        """Non-hooks keys in settings survive remove_hooks."""
        settings = {"permissions": {"allow": ["Bash(*)"]}, "hooks": {}}
        settings = add_hooks(settings, FORWARDER)
        result = remove_hooks(settings, FORWARDER)
        assert result["permissions"] == {"allow": ["Bash(*)"]}

    def test_add_then_remove_roundtrip(self):
        """add_hooks then remove_hooks leaves other user hooks exactly as they were."""
        user_stop = _user_stop_entry()
        user_notif = _user_notification_entry()
        original = {
            "hooks": {
                "Stop": [copy.deepcopy(user_stop)],
                "Notification": [copy.deepcopy(user_notif)],
            },
            "permissions": {"allow": ["Bash(*)"]},
        }
        added = add_hooks(original, FORWARDER)
        restored = remove_hooks(added, FORWARDER)

        # Stop: only the user's entry remains.
        stop_list = restored["hooks"]["Stop"]
        assert len(stop_list) == 1
        assert stop_list[0] == user_stop

        # Notification: only the user's entry remains.
        notif_list = restored["hooks"]["Notification"]
        assert len(notif_list) == 1
        assert notif_list[0] == user_notif

        # Non-hooks key preserved.
        assert restored["permissions"] == {"allow": ["Bash(*)"]}

    def test_remove_only_removes_matching_forwarder(self):
        """remove_hooks with FORWARDER_A leaves a hook installed by FORWARDER_B untouched."""
        # Install hooks for two different forwarder paths.
        settings_a = add_hooks({}, FORWARDER)
        settings_ab = add_hooks(settings_a, OTHER_FORWARDER)

        # Remove only FORWARDER's hooks.
        result = remove_hooks(settings_ab, FORWARDER)

        # OTHER_FORWARDER's hooks must still be present.
        stop_list = result.get("hooks", {}).get("Stop", [])
        notif_list = result.get("hooks", {}).get("Notification", [])
        assert any(forwarder_in_command(e, OTHER_FORWARDER) for e in stop_list), \
            "OTHER_FORWARDER Stop entry was wrongly removed"
        assert any(forwarder_in_command(e, OTHER_FORWARDER) for e in notif_list), \
            "OTHER_FORWARDER Notification entry was wrongly removed"
        # FORWARDER's hooks must be gone.
        assert not any(forwarder_in_command(e, FORWARDER) for e in stop_list)
        assert not any(forwarder_in_command(e, FORWARDER) for e in notif_list)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

class TestPhase2HookEvents:
    """Phase 2 registers UserPromptSubmit (attribution + typed barge-in) and
    SubagentStop (conversational progress) alongside Stop + Notification."""

    def test_add_registers_all_four_events(self):
        from whisperstream.claude_hud import add_hooks

        result = add_hooks({}, FORWARDER)
        assert set(result["hooks"].keys()) == {
            "Stop",
            "Notification",
            "UserPromptSubmit",
            "SubagentStop",
        }
        for event_list in result["hooks"].values():
            assert len(event_list) == 1
            assert FORWARDER in event_list[0]["hooks"][0]["command"]

    def test_remove_prunes_all_four_events(self):
        from whisperstream.claude_hud import add_hooks, remove_hooks

        merged = add_hooks({}, FORWARDER)
        result = remove_hooks(merged, FORWARDER)
        assert result == {}

    def test_remove_handles_old_two_event_install(self):
        """Disabling after an upgrade from the original Stop+Notification
        install prunes what exists and leaves nothing dangling."""
        from whisperstream.claude_hud import remove_hooks

        old_install = {
            "hooks": {
                "Stop": [_our_stop_entry(FORWARDER)],
                "Notification": [_our_notification_entry(FORWARDER)],
            }
        }
        result = remove_hooks(old_install, FORWARDER)
        assert result == {}


def forwarder_in_command(entry: dict, forwarder: str) -> bool:
    """Return True if the forwarder path appears in any inner hook command."""
    for inner in entry.get("hooks", []):
        if isinstance(inner.get("command"), str) and forwarder in inner["command"]:
            return True
    return False


class TestClaudePluginSettings:
    def test_add_declares_local_marketplace_and_enables_plugin(self):
        original = {"theme": "dark"}
        result = add_plugin_settings(original, r"C:\Whisperstream\claude-plugin")

        assert original == {"theme": "dark"}
        assert result["theme"] == "dark"
        assert result["extraKnownMarketplaces"]["whisperstream"] == {
            "source": {
                "source": "directory",
                "path": r"C:\Whisperstream\claude-plugin",
            }
        }
        assert result["enabledPlugins"]["whisperstream@whisperstream"] is True

    def test_add_is_idempotent_and_preserves_unrelated_entries(self):
        settings = {
            "extraKnownMarketplaces": {
                "other": {"source": {"source": "github", "repo": "owner/repo"}}
            },
            "enabledPlugins": {"other@other": False},
        }
        once = add_plugin_settings(settings, "C:/Whisperstream/claude-plugin")
        twice = add_plugin_settings(once, "C:/Whisperstream/claude-plugin")

        assert twice == once
        assert twice["extraKnownMarketplaces"]["other"] == settings[
            "extraKnownMarketplaces"
        ]["other"]
        assert twice["enabledPlugins"]["other@other"] is False

    def test_add_keeps_a_paused_plugin_disabled_only_when_asked(self):
        paused = {"enabledPlugins": {"whisperstream@whisperstream": False}}
        kept = add_plugin_settings(paused, "C:/Whisperstream/claude-plugin", keep_disabled=True)
        resumed = add_plugin_settings(paused, "C:/Whisperstream/claude-plugin")
        fresh = add_plugin_settings({}, "C:/Whisperstream/claude-plugin", keep_disabled=True)

        assert kept["enabledPlugins"]["whisperstream@whisperstream"] is False
        assert resumed["enabledPlugins"]["whisperstream@whisperstream"] is True
        assert fresh["enabledPlugins"]["whisperstream@whisperstream"] is True

    def test_add_refuses_to_clobber_same_named_user_marketplace(self):
        settings = {
            "extraKnownMarketplaces": {
                "whisperstream": {
                    "source": {"source": "github", "repo": "someone/whisperstream"}
                }
            }
        }

        with pytest.raises(ClaudePluginConflictError):
            add_plugin_settings(settings, "C:/Whisperstream/claude-plugin")

        assert settings["extraKnownMarketplaces"]["whisperstream"]["source"][
            "source"
        ] == "github"

    def test_remove_only_prunes_exact_owned_registration(self):
        marketplace = "C:/Whisperstream/claude-plugin"
        settings = add_plugin_settings(
            {
                "extraKnownMarketplaces": {
                    "other": {"source": {"source": "github", "repo": "owner/repo"}}
                },
                "enabledPlugins": {"other@other": True},
            },
            marketplace,
        )

        result = remove_plugin_settings(settings, marketplace)

        assert result == {
            "extraKnownMarketplaces": {
                "other": {"source": {"source": "github", "repo": "owner/repo"}}
            },
            "enabledPlugins": {"other@other": True},
        }

    def test_remove_leaves_same_named_user_marketplace_untouched(self):
        settings = {
            "extraKnownMarketplaces": {
                "whisperstream": {
                    "source": {"source": "github", "repo": "someone/whisperstream"}
                }
            },
            "enabledPlugins": {"whisperstream@whisperstream": True},
        }

        assert (
            remove_plugin_settings(settings, "C:/Whisperstream/claude-plugin")
            == settings
        )


class TestClaudePluginFiles:
    REQUIRED = {
        ".claude-plugin/plugin.json",
        "skills/speak/SKILL.md",
        "skills/name/SKILL.md",
        "hooks/hooks.json",
        "scripts/forward-hook.ps1",
    }

    def test_install_materializes_complete_plugin_and_marketplace(self, tmp_path: Path):
        result = install_claude_plugin(tmp_path)
        plugin_dir = Path(result["plugin_dir"])

        assert result["status"] == "installed"
        assert plugin_dir == tmp_path / "claude-plugin" / "plugins" / "whisperstream"
        assert all((plugin_dir / relative).is_file() for relative in self.REQUIRED)

        manifest = json.loads(
            (plugin_dir / ".claude-plugin" / "plugin.json").read_text("utf-8")
        )
        assert manifest["name"] == "whisperstream"
        marketplace = json.loads(
            (
                tmp_path
                / "claude-plugin"
                / ".claude-plugin"
                / "marketplace.json"
            ).read_text("utf-8")
        )
        assert marketplace["plugins"][0]["source"] == "./plugins/whisperstream"
        assert marketplace["plugins"][0]["version"] == manifest["version"]

    def test_install_is_idempotent_updates_managed_files_and_preserves_extra(
        self, tmp_path: Path
    ):
        first = install_claude_plugin(tmp_path)
        plugin_dir = Path(first["plugin_dir"])
        extra = plugin_dir / "notes.txt"
        extra.write_text("user-owned", encoding="utf-8")
        hook_manifest = plugin_dir / "hooks" / "hooks.json"
        hook_manifest.write_text("stale", encoding="utf-8")

        second = install_claude_plugin(tmp_path)
        third = install_claude_plugin(tmp_path)

        assert second["status"] == "updated"
        assert third["status"] == "current"
        assert json.loads(hook_manifest.read_text("utf-8"))["hooks"]
        assert extra.read_text("utf-8") == "user-owned"

    def test_install_refuses_to_overwrite_unowned_managed_path(self, tmp_path: Path):
        plugin_dir = tmp_path / "claude-plugin" / "plugins" / "whisperstream"
        manifest = plugin_dir / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name":"user-plugin"}', encoding="utf-8")

        with pytest.raises(ClaudePluginConflictError):
            install_claude_plugin(tmp_path)

        assert manifest.read_text("utf-8") == '{"name":"user-plugin"}'

    def test_remove_deletes_only_owned_files(self, tmp_path: Path):
        result = install_claude_plugin(tmp_path)
        plugin_dir = Path(result["plugin_dir"])
        extra = plugin_dir / "notes.txt"
        extra.write_text("keep", encoding="utf-8")

        removed = remove_claude_plugin(tmp_path)

        assert removed["status"] == "removed"
        assert extra.read_text("utf-8") == "keep"
        assert all(not (plugin_dir / relative).exists() for relative in self.REQUIRED)

    def test_remove_deletes_complete_owned_tree_when_no_extra_files(self, tmp_path: Path):
        result = install_claude_plugin(tmp_path)
        plugin_dir = Path(result["plugin_dir"])

        removed = remove_claude_plugin(tmp_path)

        assert removed["status"] == "removed"
        assert not plugin_dir.exists()

    def test_remove_does_not_touch_unowned_directory(self, tmp_path: Path):
        plugin_dir = tmp_path / "claude-plugin" / "plugins" / "whisperstream"
        manifest = plugin_dir / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name":"user-plugin"}', encoding="utf-8")

        result = remove_claude_plugin(tmp_path)

        assert result["status"] == "not_owned"
        assert manifest.is_file()

    def test_template_files_exist_on_disk_and_in_package_data(self):
        package_dir = REPO_ROOT / "src" / "whisperstream" / "claude_plugin"
        for relative in _PLUGIN_TEMPLATE_FILES:
            assert (package_dir / relative).is_file(), (
                f"{relative} listed in _PLUGIN_TEMPLATE_FILES but missing on disk"
            )

        pyproject = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        package_data = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"][
            "package-data"
        ]["whisperstream.claude_plugin"]
        for relative in _PLUGIN_TEMPLATE_FILES:
            posix_relative = relative.as_posix()
            assert posix_relative in package_data, (
                f"{posix_relative} missing from pyproject.toml package-data"
            )

    def test_plugin_version_bumped_past_1_3_0(self):
        assert _numeric_version(_plugin_version()) > _numeric_version("1.3.0")

    def test_name_and_speak_skill_descriptions_are_locked_verbatim(self):
        skills_dir = (
            REPO_ROOT / "src" / "whisperstream" / "claude_plugin" / "skills"
        )
        name_skill = (skills_dir / "name" / "SKILL.md").read_text(encoding="utf-8")
        speak_skill = (skills_dir / "speak" / "SKILL.md").read_text(encoding="utf-8")

        assert (
            "description: Set or replace this Claude session's name in the "
            "Whisperstream HUD. Run it with no name to clear."
        ) in name_skill
        assert (
            "description: Activate Whisperstream Agent Speech for this Claude "
            "session. Add a name to label it."
        ) in speak_skill

    def test_install_materializes_name_skill(self, tmp_path: Path):
        result = install_claude_plugin(tmp_path)
        plugin_dir = Path(result["plugin_dir"])

        assert (plugin_dir / "skills" / "name" / "SKILL.md").is_file()


class TestClaudePluginReconciliation:
    @staticmethod
    def _install_record(version: str) -> dict:
        return {
            "id": "whisperstream@whisperstream",
            "scope": "user",
            "version": version,
            "enabled": True,
        }

    @staticmethod
    def _fake_cli(
        monkeypatch,
        installed,
        *,
        marketplace_path: "Path | None" = None,
        marketplace_add_returncode: int = 0,
        change_returncode: int = 0,
    ):
        import whisperstream.claude_hud as hud_module

        calls: list[list[str]] = []

        def run(_executable: str, arguments: list[str]):
            calls.append(arguments)
            if arguments == ["marketplace", "list", "--json"]:
                records = (
                    []
                    if marketplace_path is None
                    else [
                        {
                            "name": "whisperstream",
                            "source": "directory",
                            "path": str(marketplace_path),
                        }
                    ]
                )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(records),
                    stderr="",
                )
            if arguments[:2] == ["marketplace", "add"]:
                return SimpleNamespace(
                    returncode=marketplace_add_returncode,
                    stdout="",
                    stderr="failed" if marketplace_add_returncode else "",
                )
            if arguments == ["list", "--json"]:
                records = [] if installed is None else [installed]
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(records),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=change_returncode,
                stdout="",
                stderr="failed" if change_returncode else "",
            )

        monkeypatch.setattr(hud_module, "_claude_cli_path", lambda: "claude")
        monkeypatch.setattr(hud_module, "_run_claude_plugin_cli", run)
        return calls

    def test_current_version_does_not_mutate_cache(self, tmp_path, monkeypatch):
        from whisperstream.agent_plugin_runtime import mark_payload_synced
        materialized = install_claude_plugin(tmp_path)
        mark_payload_synced(materialized)
        materialized = install_claude_plugin(tmp_path)
        calls = self._fake_cli(
            monkeypatch,
            self._install_record(materialized["version"]),
            marketplace_path=Path(materialized["marketplace_dir"]),
        )

        result = reconcile_claude_plugin(
            tmp_path,
            install_result=materialized,
        )

        assert result["ok"] is True
        assert result["status"] == "current"
        assert result["reload_required"] is False
        assert calls == [
            ["marketplace", "list", "--json"],
            ["list", "--json"],
        ]

    def test_force_reinstalls_an_identical_version(self, tmp_path, monkeypatch):
        from whisperstream.agent_plugin_runtime import mark_payload_synced
        materialized = install_claude_plugin(tmp_path)
        mark_payload_synced(materialized)
        materialized = install_claude_plugin(tmp_path)
        calls = self._fake_cli(
            monkeypatch,
            self._install_record(materialized["version"]),
            marketplace_path=Path(materialized["marketplace_dir"]),
        )

        result = reconcile_claude_plugin(tmp_path, install_result=materialized, force=True)

        assert result["status"] == "installed"
        assert result["reload_required"] is True
        assert calls == [
            ["marketplace", "list", "--json"],
            ["list", "--json"],
            ["uninstall", "whisperstream@whisperstream", "--scope", "user"],
            ["install", "whisperstream@whisperstream", "--scope", "user"],
        ]

    def test_paused_plugin_is_re_enabled_unless_kept_paused(self, tmp_path, monkeypatch):
        from whisperstream.agent_plugin_runtime import mark_payload_synced
        materialized = install_claude_plugin(tmp_path)
        mark_payload_synced(materialized)
        materialized = install_claude_plugin(tmp_path)
        record = {**self._install_record(materialized["version"]), "enabled": False}
        calls = self._fake_cli(
            monkeypatch, record, marketplace_path=Path(materialized["marketplace_dir"])
        )

        kept = reconcile_claude_plugin(tmp_path, install_result=materialized, keep_disabled=True)
        assert kept["status"] == "current"
        assert ["enable", "whisperstream@whisperstream", "--scope", "user"] not in calls

        resumed = reconcile_claude_plugin(tmp_path, install_result=materialized)
        assert resumed["ok"] is True
        assert resumed["reload_required"] is True
        assert calls[-1] == ["enable", "whisperstream@whisperstream", "--scope", "user"]

    def test_cli_pause_disables_without_uninstalling(self, tmp_path, monkeypatch):
        from whisperstream.claude_hud import disable_claude_plugin_via_cli
        calls = self._fake_cli(monkeypatch, self._install_record("2.0.0"))

        result = disable_claude_plugin_via_cli()

        assert result == {"ok": True, "status": "paused", "error": None}
        assert calls == [
            ["list", "--json"],
            ["disable", "whisperstream@whisperstream", "--scope", "user"],
        ]

    def test_cli_pause_is_idempotent_and_missing_plugin_is_fine(self, tmp_path, monkeypatch):
        from whisperstream.claude_hud import disable_claude_plugin_via_cli
        calls = self._fake_cli(monkeypatch, {**self._install_record("2.0.0"), "enabled": False})
        assert disable_claude_plugin_via_cli()["status"] == "paused"
        assert calls == [["list", "--json"]]
        self._fake_cli(monkeypatch, None)
        assert disable_claude_plugin_via_cli()["status"] == "not_installed"

    def test_missing_plugin_is_installed_with_supported_cli(
        self, tmp_path, monkeypatch
    ):
        calls = self._fake_cli(monkeypatch, None)

        result = reconcile_claude_plugin(tmp_path)

        assert result["ok"] is True
        assert result["status"] == "installed"
        assert result["reload_required"] is True
        assert calls == [
            ["marketplace", "list", "--json"],
            ["marketplace", "add", str(tmp_path / "claude-plugin")],
            ["list", "--json"],
            [
                "install",
                "whisperstream@whisperstream",
                "--scope",
                "user",
            ],
        ]

    def test_failed_marketplace_add_stops_before_plugin_inspection(
        self, tmp_path, monkeypatch
    ):
        calls = self._fake_cli(
            monkeypatch,
            None,
            marketplace_add_returncode=1,
        )

        result = reconcile_claude_plugin(tmp_path)

        assert result["ok"] is False
        assert result["status"] == "failed"
        assert result["error"] == "marketplace_add_failed"
        assert result["reload_required"] is False
        assert calls == [
            ["marketplace", "list", "--json"],
            ["marketplace", "add", str(tmp_path / "claude-plugin")],
        ]

    def test_failed_clean_install_reports_structured_plugin_failure(
        self, tmp_path, monkeypatch
    ):
        calls = self._fake_cli(
            monkeypatch,
            None,
            change_returncode=1,
        )

        result = reconcile_claude_plugin(tmp_path)

        assert result["ok"] is False
        assert result["status"] == "failed"
        assert result["error"] == "plugin_install_failed"
        assert result["installed_version"] is None
        assert result["reload_required"] is False
        assert calls == [
            ["marketplace", "list", "--json"],
            ["marketplace", "add", str(tmp_path / "claude-plugin")],
            ["list", "--json"],
            [
                "install",
                "whisperstream@whisperstream",
                "--scope",
                "user",
            ],
        ]

    def test_older_plugin_is_updated_with_supported_cli(
        self, tmp_path, monkeypatch
    ):
        materialized = install_claude_plugin(tmp_path)
        calls = self._fake_cli(
            monkeypatch,
            self._install_record("1.0.0"),
            marketplace_path=Path(materialized["marketplace_dir"]),
        )

        result = reconcile_claude_plugin(
            tmp_path,
            install_result=materialized,
        )

        assert result["ok"] is True
        assert result["status"] == "updated"
        # Read the bundled manifest rather than hardcoding, so a plugin
        # version bump does not spuriously fail this reconciliation test.
        bundled = _plugin_version()
        assert result["version"] == bundled
        assert result["installed_version"] == bundled
        assert calls[-1] == [
            "update",
            "whisperstream@whisperstream",
            "--scope",
            "user",
        ]

    def test_failed_update_preserves_last_working_version(
        self, tmp_path, monkeypatch
    ):
        materialized = install_claude_plugin(tmp_path)
        calls = self._fake_cli(
            monkeypatch,
            self._install_record("1.0.0"),
            marketplace_path=Path(materialized["marketplace_dir"]),
            change_returncode=1,
        )

        result = reconcile_claude_plugin(
            tmp_path,
            install_result=materialized,
        )

        assert result["ok"] is False
        assert result["status"] == "failed"
        assert result["error"] == "plugin_update_failed"
        assert result["installed_version"] == "1.0.0"
        assert result["reload_required"] is False
        assert calls[-1][0] == "update"
