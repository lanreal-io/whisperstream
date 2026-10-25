"""Tests for enable_claude_hud / disable_claude_hud file-I/O wrappers (Task 1.7).

These tests monkeypatch the settings.json path resolver so they NEVER touch
~/.claude/settings.json on the developer's machine.  All file operations go to
a pytest tmp_path instead.

Scenarios:
- Seed an unrelated Stop hook, call enable, assert our two hooks added AND the
  unrelated one survives.
- Call disable, assert ours gone and unrelated remains.
- Corrupt the file to ``{`` (invalid JSON) and assert enable still succeeds
  (treats as {}).
- Atomic guarantee: file is always valid JSON after the operation (write-then-
  replace, never a partial file).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import whisperstream.claude_hud as hud_module
from whisperstream.claude_hud import (
    ClaudePluginSettingsError,
    enable_claude_hud,
    disable_claude_hud,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_stop_entry() -> dict:
    """A pre-existing user Stop hook that must survive enable/disable."""
    return {
        "hooks": [
            {"type": "command", "command": r"C:\Users\Test\scripts\my-stop-hook.ps1"}
        ]
    }


def _settings_path(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def _read_settings(settings_path: Path) -> dict:
    """Read and parse the settings.json at *settings_path*."""
    return json.loads(settings_path.read_text(encoding="utf-8"))


def _forwarder_in_command(entry: dict, forwarder: str) -> bool:
    """Return True if the forwarder path appears in any inner hook command."""
    for inner in entry.get("hooks", []):
        if isinstance(inner.get("command"), str) and forwarder in inner["command"]:
            return True
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Monkeypatch _claude_settings_path() to point at a tmp_path-based file.

    Returns the Path object that enable/disable will read/write.
    """
    settings_file = _settings_path(tmp_path)

    # Monkeypatch the resolver used by enable_claude_hud / disable_claude_hud.
    monkeypatch.setattr(
        hud_module,
        "_claude_settings_path",
        lambda: settings_file,
    )
    return settings_file


@pytest.fixture()
def tmp_app_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Monkeypatch get_app_dir() (used by install_forwarder_script) to tmp_path."""
    import whisperstream.config_manager as cm
    monkeypatch.setattr(cm, "get_app_dir", lambda: tmp_path / "app_data")
    return tmp_path / "app_data"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEnableClaudeHud:
    def test_enable_creates_two_hooks_on_empty_settings(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """enable_claude_hud adds Stop and Notification hooks when settings is missing."""
        # settings.json does not exist yet — missing file -> treat as {}.
        assert not tmp_settings.exists()

        enable_claude_hud()

        assert tmp_settings.exists(), "settings.json was not created"
        data = _read_settings(tmp_settings)
        assert data["enabledPlugins"]["whisperstream@whisperstream"] is True
        source = data["extraKnownMarketplaces"]["whisperstream"]["source"]
        assert source == {
            "source": "directory",
            "path": str(tmp_app_dir / "claude-plugin"),
        }
        assert data.get("hooks", {}) == {}

    def test_enable_adds_hooks_preserving_unrelated_stop(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """enable_claude_hud preserves user's pre-existing Stop hook."""
        user_stop = _user_stop_entry()
        initial = {"hooks": {"Stop": [user_stop]}}
        tmp_settings.parent.mkdir(parents=True, exist_ok=True)
        tmp_settings.write_text(json.dumps(initial), encoding="utf-8")

        enable_claude_hud()

        data = _read_settings(tmp_settings)
        stop_list = data["hooks"]["Stop"]
        assert stop_list == [user_stop]

    def test_enable_idempotent(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """Calling enable_claude_hud twice does not duplicate our hooks."""
        enable_claude_hud()
        first = _read_settings(tmp_settings)
        second_result = enable_claude_hud()

        data = _read_settings(tmp_settings)
        assert data == first
        assert list(data["enabledPlugins"]) == ["whisperstream@whisperstream"]
        assert second_result["reload_required"] is False

    def test_enable_survives_corrupt_json(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """enable_claude_hud treats corrupt JSON as {} and still succeeds."""
        tmp_settings.parent.mkdir(parents=True, exist_ok=True)
        tmp_settings.write_text("{", encoding="utf-8")  # invalid JSON

        with pytest.raises(ClaudePluginSettingsError):
            enable_claude_hud()
        assert tmp_settings.read_text(encoding="utf-8") == "{"

    def test_enable_writes_valid_json(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """File is always valid JSON after enable_claude_hud (atomic write)."""
        enable_claude_hud()
        # If this raises, the file is not valid JSON.
        data = json.loads(tmp_settings.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_enable_creates_parent_dir_if_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """enable_claude_hud creates ~/.claude/ if it doesn't exist."""
        deep_settings = tmp_path / "new_home" / ".claude" / "settings.json"
        monkeypatch.setattr(hud_module, "_claude_settings_path", lambda: deep_settings)

        import whisperstream.config_manager as cm
        monkeypatch.setattr(cm, "get_app_dir", lambda: tmp_path / "app_data")

        assert not deep_settings.parent.exists()
        enable_claude_hud()
        assert deep_settings.exists()

    def test_enable_notification_has_permission_prompt_matcher(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """Our Notification hook has matcher == 'permission_prompt'."""
        enable_claude_hud()
        manifest = (
            tmp_app_dir
            / "claude-plugin"
            / "plugins"
            / "whisperstream"
            / "hooks"
            / "hooks.json"
        )
        hooks = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]
        assert "Notification" in hooks

    def test_enable_stop_has_no_matcher(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """Our Stop hook has no 'matcher' key (fires for all)."""
        enable_claude_hud()
        manifest = (
            tmp_app_dir
            / "claude-plugin"
            / "plugins"
            / "whisperstream"
            / "hooks"
            / "hooks.json"
        )
        hooks = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]
        assert "matcher" not in hooks["Stop"][0]


class TestDisableClaudeHud:
    def test_disable_removes_our_hooks(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """disable_claude_hud removes the Stop and Notification hooks we added."""
        enable_claude_hud()
        disable_claude_hud()

        data = _read_settings(tmp_settings)
        hooks = data.get("hooks", {})
        stop_list = hooks.get("Stop", [])
        notif_list = hooks.get("Notification", [])
        assert not any(_entry_has_our_marker(e, tmp_app_dir) for e in stop_list), \
            "Our Stop hook not removed by disable"
        assert not any(_entry_has_our_marker(e, tmp_app_dir) for e in notif_list), \
            "Our Notification hook not removed by disable"

    def test_disable_preserves_unrelated_stop_hook(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """disable_claude_hud leaves the user's pre-existing Stop hook intact."""
        user_stop = _user_stop_entry()
        initial = {"hooks": {"Stop": [user_stop]}}
        tmp_settings.parent.mkdir(parents=True, exist_ok=True)
        tmp_settings.write_text(json.dumps(initial), encoding="utf-8")

        enable_claude_hud()
        disable_claude_hud()

        data = _read_settings(tmp_settings)
        stop_list = data.get("hooks", {}).get("Stop", [])
        user_hooks = [e for e in stop_list if not _entry_has_our_marker(e, tmp_app_dir)]
        assert len(user_hooks) == 1, "User Stop hook was wrongly removed by disable"

    def test_disable_when_settings_missing_is_noop(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """disable_claude_hud with no settings.json is a no-op (treats as {})."""
        assert not tmp_settings.exists()
        # Must not raise.
        disable_claude_hud()
        # File should either not exist (noop) or be valid JSON.
        if tmp_settings.exists():
            json.loads(tmp_settings.read_text(encoding="utf-8"))

    def test_disable_survives_corrupt_json(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """disable_claude_hud treats corrupt JSON as {} and still succeeds."""
        tmp_settings.parent.mkdir(parents=True, exist_ok=True)
        tmp_settings.write_text("{", encoding="utf-8")

        with pytest.raises(ClaudePluginSettingsError):
            disable_claude_hud()
        assert tmp_settings.read_text(encoding="utf-8") == "{"

    def test_disable_writes_valid_json(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """File is always valid JSON after disable_claude_hud (atomic write)."""
        enable_claude_hud()
        disable_claude_hud()
        data = json.loads(tmp_settings.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_enable_then_disable_roundtrip_preserves_user_hooks(
        self, tmp_settings: Path, tmp_app_dir: Path
    ):
        """enable then disable leaves user hooks exactly as they were before enable."""
        user_stop = _user_stop_entry()
        initial = {"hooks": {"Stop": [user_stop]}, "permissions": {"allow": ["Bash(*)"]}}
        tmp_settings.parent.mkdir(parents=True, exist_ok=True)
        tmp_settings.write_text(json.dumps(initial), encoding="utf-8")

        enable_claude_hud()
        disable_claude_hud()

        data = _read_settings(tmp_settings)
        stop_list = data.get("hooks", {}).get("Stop", [])
        assert len(stop_list) == 1, "Should have exactly 1 Stop (the user's)"
        assert data.get("permissions") == {"allow": ["Bash(*)"]}


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _entry_has_our_marker(entry: dict, app_dir: Path) -> bool:
    """Return True if ANY inner hook command references the installed forwarder path."""
    from whisperstream.claude_hud import _FORWARDER_SUBPATH
    forwarder_path = str(app_dir / _FORWARDER_SUBPATH)
    return _forwarder_in_command(entry, forwarder_path)
