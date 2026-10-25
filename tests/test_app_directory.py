"""Tests for whisperstream.app_directory (install-aware app routing).

Three public surfaces, all best-effort / never-raise:

friendly_app_name
- explicit override wins (windowsterminal.exe -> "Windows PowerShell");
- otherwise the exe FileDescription (monkeypatched here, since the version
  resource is system-specific);
- otherwise strip-.exe + title-case fallback.

resolve_apps
- a running app -> installed=True with the live display_name + icon;
- a non-running but installed app (resolve_installed_exe hits) -> installed=True
  with the system friendly name + an icon extracted from the on-disk exe;
- neither -> installed=False, no icon;
- de-dup + empty/whitespace names dropped; empty input -> [].

resolve_installed_exe
- a bogus name returns None and never raises (no real registry/COM dependence).
"""

from __future__ import annotations

from whisperstream import app_directory
from whisperstream.app_directory import (
    friendly_app_name,
    resolve_apps,
    resolve_installed_exe,
)


# ---------------------------------------------------------------------------
# friendly_app_name
# ---------------------------------------------------------------------------

def test_friendly_name_override():
    """The one explicit override is returned regardless of any exe path."""
    assert friendly_app_name("WindowsTerminal.exe") == "Windows PowerShell"
    # Case-insensitive on the process name.
    assert friendly_app_name("windowsterminal.exe", r"C:\x\wt.exe") == "Windows PowerShell"


def test_friendly_name_file_description(monkeypatch):
    """When no override, the exe FileDescription is used verbatim."""
    monkeypatch.setattr(
        app_directory, "_file_description", lambda path: "Microsoft Visual Studio"
    )
    assert friendly_app_name("devenv.exe", r"C:\VS\devenv.exe") == "Microsoft Visual Studio"


def test_friendly_name_title_case_fallback(monkeypatch):
    """No override and no FileDescription -> strip .exe + title-case."""
    monkeypatch.setattr(app_directory, "_file_description", lambda path: None)
    assert friendly_app_name("sublime_text.exe", r"C:\x\sublime_text.exe") == "Sublime_Text"
    # No path at all skips the description lookup and title-cases the stem.
    assert friendly_app_name("notepad.exe") == "Notepad"


def test_friendly_name_empty():
    """An empty process name never raises and returns the (empty) input."""
    assert friendly_app_name("") == ""


# ---------------------------------------------------------------------------
# resolve_apps
# ---------------------------------------------------------------------------

def _running(process_name, display_name, icon):
    return {"process_name": process_name, "display_name": display_name, "icon": icon}


def test_resolve_apps_running_hit():
    """A currently-running app uses its live display_name + icon, installed=True.
    resolve_installed_exe is NOT consulted for a running app."""
    running = [_running("code.exe", "Visual Studio Code", "data:image/png;base64,AAA")]
    out = resolve_apps(["Code.exe"], running)
    assert out == [
        {
            "process_name": "code.exe",
            "installed": True,
            "display_name": "Visual Studio Code",
            "icon": "data:image/png;base64,AAA",
        }
    ]


def test_resolve_apps_installed_hit(monkeypatch):
    """Not running but installed: resolve_installed_exe returns a path, the icon
    is extracted from that on-disk exe, and the friendly name is used. This is
    the installed-but-closed case (issue 4 / Discord)."""
    monkeypatch.setattr(
        app_directory, "resolve_installed_exe", lambda name: r"C:\Apps\Discord.exe"
    )
    monkeypatch.setattr(
        app_directory, "icon_data_uri_for_exe", lambda path: "data:image/png;base64,DIS"
    )
    monkeypatch.setattr(app_directory, "_file_description", lambda path: "Discord")
    out = resolve_apps(["Discord.exe"], [])
    assert out == [
        {
            "process_name": "discord.exe",
            "installed": True,
            "display_name": "Discord",
            "icon": "data:image/png;base64,DIS",
        }
    ]


def test_resolve_apps_bundled_default_unresolvable_hidden(monkeypatch):
    """A bundled DEFAULT that is neither running nor resolvable -> installed=False
    (the user may not have that default app installed)."""
    monkeypatch.setattr(app_directory, "resolve_installed_exe", lambda name: None)
    monkeypatch.setattr(app_directory, "_file_description", lambda path: None)
    out = resolve_apps(["slack.exe"], [], default_names={"slack.exe"})
    assert out == [
        {
            "process_name": "slack.exe",
            "installed": False,
            "display_name": "Slack",
            "icon": None,
        }
    ]


def test_resolve_apps_user_added_unresolvable_shown_on_trust(monkeypatch):
    """A USER-ADDED (non-default) app with no recorded path that we can't
    auto-detect -> installed=True on trust (hiding a working route the user
    explicitly added is worse than showing a stale one). No icon (no path)."""
    monkeypatch.setattr(app_directory, "resolve_installed_exe", lambda name: None)
    monkeypatch.setattr(app_directory, "_file_description", lambda path: None)
    out = resolve_apps(["dota2.exe"], [], default_names={"slack.exe"})
    assert out[0]["process_name"] == "dota2.exe"
    assert out[0]["installed"] is True
    assert out[0]["icon"] is None


def test_resolve_apps_recorded_path_present_installed(monkeypatch, tmp_path):
    """A recorded exe path that still exists -> installed=True with an icon, even
    when no registry source resolves the name (Steam game / portable exe)."""
    exe = tmp_path / "dota2.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(app_directory, "resolve_installed_exe", lambda name: None)
    monkeypatch.setattr(app_directory, "_file_description", lambda path: None)
    monkeypatch.setattr(
        app_directory, "icon_data_uri_for_exe", lambda p: "data:image/png;base64,IC"
    )
    out = resolve_apps(["dota2.exe"], [], known_paths={"dota2.exe": str(exe)})
    assert out[0]["installed"] is True
    assert out[0]["icon"] == "data:image/png;base64,IC"


def test_resolve_apps_recorded_path_gone_uninstalled(monkeypatch):
    """A recorded path that has VANISHED and nothing else resolves -> installed=
    False (the app the user added was genuinely uninstalled), even though it is
    a non-default user-added rule."""
    monkeypatch.setattr(app_directory, "resolve_installed_exe", lambda name: None)
    monkeypatch.setattr(app_directory, "_file_description", lambda path: None)
    out = resolve_apps(["dota2.exe"], [], known_paths={"dota2.exe": "C:/nope/gone.exe"})
    assert out[0]["installed"] is False
    assert out[0]["icon"] is None


def test_resolve_apps_dedup_and_empty(monkeypatch):
    """Duplicate (case-insensitive) and empty/whitespace names collapse; the
    output keeps the first occurrence only."""
    monkeypatch.setattr(app_directory, "resolve_installed_exe", lambda name: None)
    monkeypatch.setattr(app_directory, "_file_description", lambda path: None)
    out = resolve_apps(["Slack.exe", "slack.exe", "", "   ", "SLACK.EXE"], [])
    assert len(out) == 1
    assert out[0]["process_name"] == "slack.exe"


def test_resolve_apps_empty_input():
    """No names in -> [] out (and a None list never raises)."""
    assert resolve_apps([], []) == []
    assert resolve_apps(None, None) == []  # type: ignore[arg-type]


def test_resolve_apps_running_index_dedups_running(monkeypatch):
    """A running app whose icon extraction is irrelevant (it's not re-extracted)
    still resolves from the running record even if resolve_installed_exe would
    have returned a path."""
    monkeypatch.setattr(
        app_directory, "resolve_installed_exe", lambda name: r"C:\should\not\be\used.exe"
    )
    running = [_running("code.exe", "Visual Studio Code", None)]
    out = resolve_apps(["code.exe"], running)
    assert out[0]["installed"] is True
    assert out[0]["display_name"] == "Visual Studio Code"
    # icon comes from the running record (None here), NOT a fresh extraction.
    assert out[0]["icon"] is None


# ---------------------------------------------------------------------------
# resolve_installed_exe
# ---------------------------------------------------------------------------

def test_resolve_installed_exe_bogus_returns_none():
    """A name that is not running, not in App Paths, not a Store alias, and not a
    Start Menu shortcut returns None and never raises."""
    result = resolve_installed_exe("definitely_not_real_xyz_zzz.exe")
    assert result is None


def test_resolve_installed_exe_empty_returns_none():
    """Empty / whitespace names short-circuit to None."""
    assert resolve_installed_exe("") is None
    assert resolve_installed_exe("   ") is None


def test_resolve_installed_exe_never_raises_on_stage_failure(monkeypatch):
    """Even if every resolution stage raises, the function swallows it -> None."""
    def _boom(*_a, **_k):
        raise RuntimeError("stage exploded")

    monkeypatch.setattr(app_directory, "_resolve_via_app_paths", _boom)
    monkeypatch.setattr(app_directory, "_resolve_via_store_alias", _boom)
    monkeypatch.setattr(app_directory, "_resolve_via_start_menu", _boom)
    assert resolve_installed_exe("anything.exe") is None
