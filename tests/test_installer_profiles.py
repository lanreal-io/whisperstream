"""Synthetic profile and build-dispatch tests; no package or secrets."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import build
import _build_lib as b
from installer_profile import effective_windows_configuration, installer_profile, validate_source_configuration

_spec = importlib.util.spec_from_file_location("verify_installer", SCRIPTS / "verify-installer.py")
verify_installer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_installer)


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "src-tauri").mkdir()
    (tmp_path / "src-tauri/tauri.conf.json").write_text(json.dumps({
        "bundle": {"resources": ["resources/EULA.txt"]},
    }))
    (tmp_path / "src-tauri/tauri.windows.conf.json").write_text(json.dumps({
        "identifier": "com.whisperstream.app",
        "bundle": {"targets": ["nsis"]},
    }))
    return tmp_path


@pytest.mark.parametrize("profile", ["source", "official"])
@pytest.mark.parametrize("fail", [False, True])
def test_profiles_hold_lock_and_never_modify_inputs(checkout, profile, fail):
    originals = {p: p.read_bytes() for p in (checkout / "src-tauri").glob("*.json")}
    try:
        with installer_profile(checkout, profile):
            with pytest.raises(FileExistsError):
                with installer_profile(checkout, "source"):
                    pytest.fail("Overlapping build was admitted")
            if fail:
                raise RuntimeError("synthetic compiler failure")
    except RuntimeError:
        assert fail
    assert {p: p.read_bytes() for p in (checkout / "src-tauri").glob("*.json")} == originals
    assert not (checkout / ".installer-profile.lock").exists()


def test_unknown_profile_is_rejected_without_a_lock(checkout):
    with pytest.raises(ValueError, match="Unknown installer profile"):
        with installer_profile(checkout, "store"):
            pytest.fail("Unknown profile admitted")
    assert not (checkout / ".installer-profile.lock").exists()


@pytest.mark.parametrize("name,section,value", [
    ("tauri.windows.conf.json", "plugins", {"updater": {"endpoints": ["https://example.invalid/feed.json"]}}),
    ("tauri.conf.json", "bundle", {"createUpdaterArtifacts": "v1Compatible"}),
])
def test_committed_update_feed_or_artifacts_rejected_before_build(checkout, name, section, value):
    path = checkout / "src-tauri" / name
    config = json.loads(path.read_text())
    config.setdefault(section, {}).update(value)
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="no binary update"):
        with installer_profile(checkout, "official"):
            pytest.fail("Update feed admitted")
    assert not (checkout / ".installer-profile.lock").exists()


def test_effective_windows_identity_still_rejects_unreviewed_installer(checkout):
    path = checkout / "src-tauri/tauri.windows.conf.json"
    config = json.loads(path.read_text())
    config["identifier"] = "other.synthetic.identity"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="identity differs"):
        validate_source_configuration(checkout)


def test_windows_merge_replaces_arrays_and_removes_null_keys(checkout):
    path = checkout / "src-tauri/tauri.windows.conf.json"
    config = json.loads(path.read_text())
    config["bundle"]["resources"] = ["resources/PRIVACY.txt"]
    path.write_text(json.dumps(config))
    effective = effective_windows_configuration(checkout)
    assert effective["bundle"]["resources"] == ["resources/PRIVACY.txt"]
    config["bundle"]["resources"] = None
    path.write_text(json.dumps(config))
    assert "resources" not in effective_windows_configuration(checkout)["bundle"]


def _capture_pipeline(monkeypatch, checkout, profile):
    monkeypatch.setattr(b, "REPO_ROOT", checkout)
    calls = []

    def pipeline(portable, skip, unsigned, experimental, store):
        calls.append({"portable": portable, "skip": skip, "unsigned": unsigned,
                      "experimental": experimental, "store": store})
        assert (checkout / ".installer-profile.lock").read_text() == profile

    monkeypatch.setattr(build, "_run_build", pipeline)
    return calls


@pytest.mark.parametrize("argv,profile,unsigned", [
    ([], "source", True),
    (["--unsigned"], "source", True),
    (["--profile", "source"], "source", True),
    (["--profile", "official"], "official", False),
])
def test_cli_selects_pipeline_for_profile(checkout, monkeypatch, argv, profile, unsigned):
    calls = _capture_pipeline(monkeypatch, checkout, profile)
    assert build.main(argv) == 0
    assert [call["unsigned"] for call in calls] == [unsigned]
    assert not (checkout / ".installer-profile.lock").exists()


@pytest.mark.parametrize("flag,field", [
    ("--portable", "portable"), ("--skip-nuitka", "skip"),
    ("--microsoft-store", "store"), ("--experimental", "experimental"),
])
def test_official_keeps_signed_variants(checkout, monkeypatch, flag, field):
    # The official build's recovery, portable and Store aliases stay signed.
    calls = _capture_pipeline(monkeypatch, checkout, "official")
    assert build.main(["--profile", "official", flag]) == 0
    assert calls[0]["unsigned"] is False and calls[0][field] is True


def test_official_rejects_unsigned():
    with pytest.raises(SystemExit) as exc:
        build.main(["--profile", "official", "--unsigned"])
    assert exc.value.code == 2


def test_overlapping_build_fails_without_running_pipeline(checkout, monkeypatch):
    calls = _capture_pipeline(monkeypatch, checkout, "source")
    (checkout / ".installer-profile.lock").write_text("official")
    assert build.main([]) == 1
    assert calls == []
    assert (checkout / ".installer-profile.lock").read_text() == "official"


def test_committed_update_feed_fails_build_before_pipeline(checkout, monkeypatch):
    calls = _capture_pipeline(monkeypatch, checkout, "official")
    path = checkout / "src-tauri/tauri.windows.conf.json"
    config = json.loads(path.read_text())
    config["plugins"] = {"updater": {"endpoints": ["https://example.invalid/feed.json"]}}
    path.write_text(json.dumps(config))
    assert build.main(["--profile", "official"]) == 1
    assert calls == []


@pytest.mark.parametrize("changed,generated,allowed", [
    ("", False, True), ("THIRD_PARTY_LICENSES.txt\0", False, False),
    ("THIRD_PARTY_LICENSES.txt\0", True, True),
    ("src/whisperstream/main.py\0", True, False),
])
def test_clean_revision_allows_only_attested_generated_notices(monkeypatch, tmp_path, changed, generated, allowed):
    monkeypatch.setattr(b, "source_revision", lambda root: "a" * 40)
    monkeypatch.setattr(b.subprocess, "run", lambda argv, **kwargs:
                        SimpleNamespace(stdout=changed if argv[1] == "diff" else ""))
    if allowed:
        assert b.require_clean_source_revision(tmp_path, generated_notices=generated) == "a" * 40
    else:
        with pytest.raises(b.LegalReleaseGateError):
            b.require_clean_source_revision(tmp_path, generated_notices=generated)


def test_verifier_checks_attestation_signature_and_hashes_every_artifact(checkout, monkeypatch):
    nsis = checkout / "src-tauri/target/release/bundle/nsis"
    nsis.mkdir(parents=True)
    stem = nsis / "Whisperstream_2.0.0_x64-setup"
    paths = [Path(f"{stem}.exe"), Path(f"{stem}.exe.sig"),
             checkout / "dist/latest.json", checkout / "dist/legal-release.json"]
    (checkout / "dist").mkdir()
    for path in paths:
        path.write_bytes(path.name.encode())
    attested = []
    monkeypatch.setattr(b, "require_clean_source_revision", lambda root, generated_notices: "a" * 40)
    monkeypatch.setattr(b, "read_tauri_version", lambda root: "2.0.0")
    monkeypatch.setattr(b, "verify_legal_release_attestation",
                        lambda version, exe, sig, root: attested.append((version, exe, sig)))
    monkeypatch.setattr(b, "verify_signature", lambda path: SimpleNamespace(trusted=True))
    report = verify_installer.verify(checkout)
    assert attested == [("2.0.0", paths[0], paths[1])]
    assert [Path(item["path"]) for item in report["artifacts"]] == [p.resolve() for p in paths]
    assert report["profile"] == "official" and report["source_revision"] == "a" * 40
    assert not (checkout / ".installer-profile.lock").exists()

    monkeypatch.setattr(b, "verify_signature", lambda path: SimpleNamespace(trusted=False))
    with pytest.raises(ValueError, match="not trusted"):
        verify_installer.verify(checkout)


@pytest.mark.parametrize("profile", ["source", "official"])
def test_committed_configuration_has_no_update_feed(tmp_path, monkeypatch, profile):
    root = Path(__file__).resolve().parents[1]
    tauri = tmp_path / "src-tauri"
    tauri.mkdir()
    names = ["tauri.conf.json", "tauri.windows.conf.json", "tauri.macos.conf.json",
             "tauri.microsoft-store.conf.json"]
    originals = {}
    for name in names:
        data = (root / "src-tauri" / name).read_bytes()
        path = tauri / name
        path.write_bytes(data)
        originals[path] = data
    assert not (root / "src-tauri/updater-policy.json").exists()
    monkeypatch.setattr(build.b, "REPO_ROOT", tmp_path)
    validate_source_configuration(tmp_path)
    assert "identifier" not in json.loads((tauri / "tauri.conf.json").read_text())
    with installer_profile(tmp_path, profile):
        effective = effective_windows_configuration(tmp_path)
        assert effective["identifier"] == "com.whisperstream.app"
        assert "updater" not in effective.get("plugins", {})
        assert "createUpdaterArtifacts" not in effective["bundle"]
        resources = json.loads(build._cli_resources_overlay_json())
        assert resources["bundle"]["resources"][:-1] == effective["bundle"]["resources"]
        # These may only turn update artifacts off (the Store overlay must win over
        # the official overlay) or carry an empty, feedless updater block.
        for name in ("tauri.macos.conf.json", "tauri.microsoft-store.conf.json"):
            config = json.loads((tauri / name).read_text())
            assert config.get("bundle", {}).get("createUpdaterArtifacts", False) is False
            updater = config.get("plugins", {}).get("updater", {})
            assert not updater.get("endpoints") and not updater.get("pubkey")
    assert {path: path.read_bytes() for path in originals} == originals
