"""Synthetic Windows resource overlays; no binaries, signing or builds run."""
from pathlib import Path
import json

import pytest

from scripts import build
from scripts import fetch_llama_server


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    tauri = tmp_path / "src-tauri"
    tauri.mkdir()
    (tauri / "tauri.conf.json").write_text(json.dumps({
        "bundle": {"resources": ["common.txt", "base-only.dll"],
                   "createUpdaterArtifacts": False},
    }))
    resources = ["common.txt", *[
        f"binaries/{name}" for name in fetch_llama_server.REQUIRED_DLLS
    ], "binaries/LICENSE-LLVM-OpenMP", "runtime/windows-only.dll"]
    (tauri / "tauri.windows.conf.json").write_text(json.dumps({
        "bundle": {"resources": resources},
    }))
    (tauri / "tauri.macos.conf.json").write_text(json.dumps({
        "bundle": {"resources": ["mac-only.dylib"]},
    }))
    store = tauri / "tauri.microsoft-store.conf.json"
    store.write_text(json.dumps({"bundle": {
        "createUpdaterArtifacts": False,
        "windows": {"webviewInstallMode": {"type": "offlineInstaller"}},
    }}))
    monkeypatch.setattr(build.b, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build, "MICROSOFT_STORE_CONFIG", store)
    # The source edition: no official Tauri overlay ahead of these configs.
    monkeypatch.delenv(build.b.TAURI_OVERLAY_ENV, raising=False)
    return tmp_path, resources


def test_cli_overlay_preserves_exact_windows_resources_once(checkout):
    root, expected = checkout
    overlay = json.loads(build._cli_resources_overlay_json())
    assert overlay == {"bundle": {"resources": [*expected, "bin/whisperstream.exe"]}}
    windows = root / "src-tauri/tauri.windows.conf.json"
    windows.write_text(json.dumps({"bundle": {"resources": [*expected, "bin/whisperstream.exe"]}}))
    assert json.loads(build._cli_resources_overlay_json()) == overlay


@pytest.mark.parametrize("windows", [None, {"bundle": {"targets": ["nsis"]}}])
def test_absent_windows_resource_override_retains_base(checkout, windows):
    root, _ = checkout
    path = root / "src-tauri/tauri.windows.conf.json"
    if windows is None:
        path.unlink()
    else:
        path.write_text(json.dumps(windows))
    assert build._windows_bundle_resources() == ["common.txt", "base-only.dll"]


@pytest.mark.parametrize("resources", [None, {}, [1]])
def test_invalid_windows_resources_fail_before_build(checkout, resources):
    root, _ = checkout
    (root / "src-tauri/tauri.windows.conf.json").write_text(json.dumps({
        "bundle": {"resources": resources},
    }))
    with pytest.raises(ValueError, match="list of paths"):
        build._cli_resources_overlay_json()


@pytest.mark.parametrize("unsigned", [False, True])
@pytest.mark.parametrize("store", [False, True])
def test_build_command_preserves_windows_closure_and_signing_policy(checkout, monkeypatch, unsigned, store):
    root, expected = checkout
    signing = root / "signing.json"
    signing.write_text(json.dumps({"bundle": {"windows": {"signCommand": "synthetic signer %1"}}}))
    calls = []
    monkeypatch.setattr(build.b, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    if unsigned:
        build._step_tauri_nsis_unsigned(12, microsoft_store=store)
    else:
        build._step_tauri_nsis(signing, 12, microsoft_store=store)
    assert len(calls) == (1 if unsigned else 2)
    argv, kwargs = calls[0]
    assert argv[:3] == ["cargo", "tauri", "build"]
    configs = [argv[index + 1] for index, value in enumerate(argv) if value == "--config"]
    if not unsigned:
        assert configs.pop(0) == str(signing)
    overlay = json.loads(configs.pop(0))
    assert overlay["bundle"]["resources"] == [*expected, "bin/whisperstream.exe"]
    if unsigned:
        assert overlay["bundle"]["createUpdaterArtifacts"] is False
        assert "windows" not in overlay["bundle"]
    else:
        assert set(overlay["bundle"]) == {"resources"}
        assert json.loads(signing.read_text())["bundle"]["windows"]["signCommand"] == "synthetic signer %1"
    if store:
        assert configs.pop(0) == str(build.MICROSOFT_STORE_CONFIG)
        store_config = json.loads(build.MICROSOFT_STORE_CONFIG.read_text())
        assert store_config["bundle"]["createUpdaterArtifacts"] is False
        assert store_config["bundle"]["windows"]["webviewInstallMode"]["type"] == "offlineInstaller"
    if not unsigned:
        assert json.loads(configs.pop(0)) == {"bundle": {"windows": {"signCommand": None}}}
        assert "--no-bundle" in argv
        assert not kwargs.get("permitted_secrets")
        bundle_argv, bundle_kwargs = calls[1]
        assert bundle_argv[:3] == ["cargo", "tauri", "bundle"]
        bundle_configs = [bundle_argv[index + 1] for index, value in enumerate(bundle_argv) if value == "--config"]
        assert bundle_configs[0] == str(signing)
        assert json.loads(bundle_configs[1])["bundle"]["resources"] == [*expected, "bin/whisperstream.exe"]
        if store:
            assert bundle_configs[2] == str(build.MICROSOFT_STORE_CONFIG)
        assert json.loads(bundle_configs[-1]) == {"build": {"beforeBundleCommand": None}}
        assert "AZURE_CLIENT_SECRET" in bundle_kwargs["permitted_secrets"]
        assert bundle_kwargs["cwd"] == root / "src-tauri"
    assert configs == []
    assert kwargs["cwd"] == root / "src-tauri"


def test_store_enumerates_windows_pe_resources_outside_binaries(checkout):
    root, _ = checkout
    installer = root / "installer.exe"
    paths = build._microsoft_store_pe_paths(installer)
    assert root / "src-tauri/runtime/windows-only.dll" in paths
    assert root / "src-tauri/base-only.dll" not in paths
    assert root / "src-tauri/mac-only.dylib" not in paths
    assert root / "src-tauri/common.txt" not in paths
    assert installer in paths


def test_missing_windows_overlay_pe_remains_a_store_signature_failure(checkout, monkeypatch):
    root, _ = checkout
    # No files are created and the real signature or payload tools never run.
    monkeypatch.setattr(build.b, "verify_signature", lambda _: pytest.fail("missing PE reached signer"))
    with pytest.raises(build.MicrosoftStorePackageError, match="windows-only.dll: missing"):
        build._verify_microsoft_store_pe_signatures(root / "installer.exe")


def test_committed_windows_closure_survives_cli_overlay(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(build.b, "REPO_ROOT", root)
    expected = json.loads((root / "src-tauri/tauri.windows.conf.json").read_text())["bundle"]["resources"]
    actual = json.loads(build._cli_resources_overlay_json())["bundle"]["resources"]
    assert actual == [*expected, "bin/whisperstream.exe"]
    for name in [*fetch_llama_server.REQUIRED_DLLS, "LICENSE-LLVM-OpenMP"]:
        assert f"binaries/{name}" in actual
