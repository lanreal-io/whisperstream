"""Shared macOS signing: local signing and the Developer ID hook with no provider."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import audit_macos_internal_bundle as auditor
from scripts import build_macos_internal as builder
from scripts import macos_signing

TEAM = "ABCDE12345"
SHA = "A" * 40


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def no_provider(monkeypatch):
    """Behave like a build that was given no Developer ID provider."""
    monkeypatch.setattr(builder, "developer_id", None)
    monkeypatch.setattr(auditor, "developer_id", None)


def test_hook_resolves_to_none_without_a_provider():
    assert macos_signing.load_developer_id(None) is None


def test_a_named_but_missing_provider_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError, match="Developer ID provider not found"):
        macos_signing.load_developer_id(tmp_path / "absent.py")


def test_provider_defaults_to_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv(macos_signing.DEVELOPER_ID_PROVIDER_ENV, raising=False)
    assert macos_signing.default_developer_id_provider() is None
    assert builder._parse_args([]).developer_id_provider is None
    assert auditor._parse_args(["--app", "W.app"]).developer_id_provider is None
    provider = tmp_path / "provider.py"
    monkeypatch.setenv(macos_signing.DEVELOPER_ID_PROVIDER_ENV, str(provider))
    assert builder._parse_args([]).developer_id_provider == provider
    assert builder._parse_args(["--developer-id-provider", "x.py"]).developer_id_provider == Path("x.py")
    assert auditor._parse_args(["--app", "W.app"]).developer_id_provider == provider


def test_main_loads_the_named_provider(monkeypatch, tmp_path):
    provider = tmp_path / "provider.py"
    provider.write_text("class DeveloperIdError(Exception):\n    pass\n")
    monkeypatch.setattr(builder, "developer_id", None)
    monkeypatch.setattr(auditor, "developer_id", None)
    builder.use_developer_id_provider(provider)
    auditor.use_developer_id_provider(provider)
    assert builder.developer_id is auditor.developer_id is macos_signing.load_developer_id(provider)
    builder.use_developer_id_provider(None)
    assert builder.developer_id is None


def test_hook_loads_a_provider_once_from_its_path(tmp_path):
    provider = tmp_path / "provider.py"
    provider.write_text("LOADS = []\nLOADS.append(1)\n")
    first = macos_signing.load_developer_id(provider)
    assert first is macos_signing.load_developer_id(provider)
    assert first.LOADS == [1]


def test_codesign_argv_requests_timestamp_runtime_and_entitlements():
    argv = macos_signing.codesign_argv(SHA, Path("/x/engine"), entitlements=Path("/e.plist"), identifier="id")
    assert argv[:5] == ["/usr/bin/codesign", "--force", "--sign", SHA, "--timestamp"]
    assert argv[argv.index("--options") + 1] == "runtime"
    assert argv[argv.index("--entitlements") + 1] == str(Path("/e.plist"))
    assert argv[-1] == str(Path("/x/engine"))
    assert "--options" not in macos_signing.codesign_argv(SHA, Path("x.dmg"), runtime=False)


def test_builder_defaults_to_local_signing():
    args = builder._parse_args([])
    assert (args.signing, args.team_id, args.signing_sha1) == ("local", None, None)
    assert builder._parse_args(["--signing", "developer-id", "--team-id", TEAM]).signing == "developer-id"


def test_local_tauri_config_has_no_hardened_runtime():
    argv = builder._tauri_build_argv(Path("tauri"), "both", "PINNED")
    assert json.loads(argv[argv.index("--config") + 1])["bundle"]["macOS"] == {"signingIdentity": "PINNED"}


def test_local_signing_needs_no_provider(no_provider, monkeypatch):
    assert builder._nuitka_signing_args("local") == []
    assert len(set(builder.ONEFILE_CACHE_NAMES.values())) == 2
    path = Path("/s/src-tauri/binaries/whisperstream-engine-aarch64-apple-darwin")
    local = builder._staged_signing_argv(
        {"WHISPERSTREAM_BUILD_SIGNING_IDENTITY": "PINNED"}, path, "binaries", "id")
    assert local == ["/usr/bin/codesign", "--force", "--sign", "PINNED", "--identifier", "id", str(path)]
    monkeypatch.setattr(builder, "local_signing_identity", lambda: "PINNED")
    assert builder._resolve_signing("local", None, None) == ("PINNED", None)
    with pytest.raises(builder.MacInternalBuildError, match="require --signing developer-id"):
        builder._resolve_signing("local", TEAM, None)
    report = builder._signing_report("PINNED", None, None)
    assert report["kind"] == "self-signed" and report["team_id"] is None and report["notarization"] is None


def test_developer_id_build_fails_before_staging_without_provider(no_provider, monkeypatch, tmp_path):
    for name in (*builder.FORBIDDEN_CREDENTIAL_ENV, *builder.UPDATER_HOOK_ENVS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(builder, "_assert_native_host", lambda: None)
    with pytest.raises(builder.MacInternalBuildError, match="use --signing local"):
        builder.build(tmp_path / "root", tmp_path / "tools", bundles="both", signing="developer-id")
    assert not (tmp_path / "root").exists()


def test_developer_id_audit_fails_without_provider(no_provider, tmp_path):
    with pytest.raises(auditor.BundleAuditError, match="was not given"):
        auditor.audit(tmp_path / "W.app", signing="developer-id", team_id=TEAM)


def test_audit_team_id_rules_without_provider(no_provider, tmp_path):
    with pytest.raises(auditor.BundleAuditError, match="requires --signing developer-id"):
        auditor.audit(tmp_path / "W.app", team_id=TEAM)
    report = tmp_path / "build-report.json"
    report.write_text(json.dumps({"signing": {"kind": "developer-id", "team_id": TEAM}}))
    assert auditor._report_team_id(report) == TEAM
    report.write_text(json.dumps({"signing": {"kind": "self-signed"}}))
    with pytest.raises(auditor.BundleAuditError, match="does not describe a Developer ID"):
        auditor._report_team_id(report)


def test_local_audit_still_rejects_developer_id_signatures(no_provider, monkeypatch):
    details = f"Authority=Developer ID Application: X ({TEAM})\nTeamIdentifier={TEAM}"
    monkeypatch.setattr(auditor, "_run", lambda argv, check=True: result(stderr=details if "-dv" in argv else ""))
    with pytest.raises(auditor.BundleAuditError, match="distribution signing identity"):
        auditor._signature(Path("W.app"))


def test_mac_build_children_never_inherit_updater_hooks(monkeypatch, tmp_path):
    """A locally signed build ships no updater; a provider path must not reach its staged build."""
    from scripts import _build_lib

    assert set(builder.UPDATER_HOOK_ENVS) == set(_build_lib.UPDATER_HOOK_ENVS)
    for name in builder.UPDATER_HOOK_ENVS:
        monkeypatch.setenv(name, str(tmp_path / "provider"))
    monkeypatch.setenv(macos_signing.DEVELOPER_ID_PROVIDER_ENV, str(tmp_path / "devid.py"))
    for name in builder.FORBIDDEN_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in ("_assert_native_host", "_assert_non_distribution_environment",
                 "_assert_internal_legal_resources", "_assert_tauri_api_compatibility"):
        monkeypatch.setattr(builder, name, lambda **_: None)
    monkeypatch.setattr(builder, "_resolve_signing", lambda *_: ("PINNED", None))
    monkeypatch.setattr(builder, "_output", lambda *_a, **_k: "0")
    monkeypatch.setattr(builder, "_stage_sources", lambda *_a, **_k: {})
    monkeypatch.setattr(builder, "stage_macos_llama_server", lambda *_a, **_k: {})
    monkeypatch.setattr(builder, "_bind_llama_bundle_rpath", lambda _root, stage: stage)
    monkeypatch.setattr(builder, "_tree_measurement", lambda *_a, **_k: {})
    seen = {}

    class Captured(Exception):
        pass

    def capture(_root, _tools, _source, env, *_rest):
        seen.update(env)
        raise Captured

    monkeypatch.setattr(builder, "_build_engine", capture)
    with pytest.raises(Captured):
        builder.build(tmp_path / "root", tmp_path / "tools", bundles="app", signing="local")
    assert not set(builder.UPDATER_HOOK_ENVS) & set(seen)
    assert seen[macos_signing.DEVELOPER_ID_PROVIDER_ENV] == str(tmp_path / "devid.py")
    assert seen["WHISPERSTREAM_BUILD_SIGNING_KIND"] == "local"
