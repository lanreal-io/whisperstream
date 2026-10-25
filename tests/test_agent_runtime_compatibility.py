"""Version evidence must not overstate a surface's delivery capability."""

import subprocess

import pytest

from whisperstream import agent_runtime_compatibility as runtime


@pytest.mark.parametrize("text, expected", [
    ("2.1.287 (Claude Code)", "2.1.287"),
    ("codex-cli 0.160.0\n", "0.160.0"),
    ("0.160.0-alpha.2", None),
    ("error from C:/private/2.1.287/file", None),
    ("", None),
])
def test_only_release_version_output_is_evidence(text, expected):
    assert runtime.release_version(text) == expected


def cli_snapshot(monkeypatch, agent, version, help_text="--thread <THREAD> --message <TEXT>"):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "C:/fixture/provider.exe")
    calls = []

    def run(argv):
        calls.append(argv)
        return version if argv[-1] == "--version" else help_text

    monkeypatch.setattr(runtime, "_run", run)
    result = runtime.inspect_cli(agent)
    return result, calls


def test_old_claude_requests_update_without_assuming_plugin_ready(monkeypatch):
    result, _ = cli_snapshot(monkeypatch, "claude", "2.1.286 (Claude Code)")
    assert result["state"] == "update_required"
    assert result["minimum_version"] == "2.1.287"
    assert result["reply_verified"] is False


def test_supported_version_still_needs_live_transport(monkeypatch):
    result, _ = cli_snapshot(monkeypatch, "claude", "2.1.287 (Claude Code)")
    assert result["state"] == "supported"
    assert result["reply_verified"] is False


def test_codex_command_missing_is_distinct_from_old_version(monkeypatch):
    result, _ = cli_snapshot(monkeypatch, "codex", "codex-cli 0.160.0", "unrecognized subcommand")
    assert result["state"] == "capability_unavailable"


def test_codex_probe_never_sends_a_message(monkeypatch):
    result, calls = cli_snapshot(monkeypatch, "codex", "codex-cli 0.160.0")
    assert result["state"] == "legacy"
    assert calls == [["C:/fixture/provider.exe", "--version"], ["C:/fixture/provider.exe", "queue", "--help"]]
    assert "C:/fixture" not in str(result)


def test_missing_and_failed_cli_are_different(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    assert runtime.inspect_cli("claude")["state"] == "not_installed"
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "provider.exe")
    monkeypatch.setattr(runtime, "_run", lambda _: None)
    assert runtime.inspect_cli("claude")["state"] == "check_failed"


def test_desktop_package_is_not_an_engine_version(monkeypatch):
    monkeypatch.setattr(runtime, "_desktop_package", lambda _: {"version": "1.24012.1.0", "location": "C:/private/app"})
    result = runtime.inspect_desktop("claude")
    assert result["app_version"] == "1.24012.1.0"
    assert result["engine_version"] is None
    assert result["state"] == "unknown_engine"
    assert "C:/private" not in str(result)


def test_desktop_check_failure_does_not_claim_absence(monkeypatch):
    monkeypatch.setattr(runtime, "_desktop_package", lambda _: None)
    assert runtime.inspect_desktop("claude")["state"] == "check_failed"
    monkeypatch.setattr(runtime, "_desktop_package", lambda _: {})
    assert runtime.inspect_desktop("claude")["state"] == "not_installed"


def test_probe_is_hidden_bounded_and_no_shell(monkeypatch):
    def run(argv, **kwargs):
        assert kwargs["timeout"] <= 6
        assert kwargs.get("shell", False) is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 0, "codex-cli 0.160.0", "")
    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert runtime._run(["codex", "--version"]) == "codex-cli 0.160.0"


def test_mac_bundle_identity_and_engine_evidence_are_separate(monkeypatch, tmp_path):
    import plistlib
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.Path, "home", lambda: tmp_path)
    bundle = tmp_path / "Applications/Codex.app"
    contents = bundle / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.openai.codex",
        "CFBundleShortVersionString": "26.930.31730",
    }))
    engine = contents / "Resources/codex-cli/bin/codex"
    engine.parent.mkdir(parents=True)
    engine.touch()
    calls = []
    monkeypatch.setattr(runtime, "_run", lambda argv: calls.append(argv) or "codex-cli 0.160.0")
    result = runtime.inspect_desktop("codex")
    assert result["app_version"] == "26.930.31730"
    assert result["engine_version"] == "0.160.0"
    assert result["state"] == "legacy"
    assert result["reply_verified"] is False
    assert calls == [[str(engine), "--version"]]
    assert runtime.desktop_codex_executable() == str(engine)
    assert str(tmp_path) not in str(result)


def test_mac_malformed_installed_bundle_is_failed_check(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.Path, "home", lambda: tmp_path)
    contents = tmp_path / "Applications/Codex.app/Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(b"invalid plist")
    # Ensure the fixture does not fall through to the host's real app bundle.
    original_exists = runtime.Path.exists
    monkeypatch.setattr(runtime.Path, "exists", lambda path: False if str(path).startswith('/Applications/') else original_exists(path))
    assert runtime.inspect_desktop("codex")["state"] == "check_failed"
