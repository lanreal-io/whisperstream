"""Source-level contracts for Microsoft Store EXE distribution."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build as build_script


STORE_CONFIG = ROOT / "src-tauri" / "tauri.microsoft-store.conf.json"


@pytest.mark.parametrize("failure", [None, "unsigned", "missing"])
def test_payload_audit_checks_bundled_main_not_restored_build(monkeypatch, tmp_path, failure):
    monkeypatch.setenv("SEVEN_ZIP", "7z-test")
    seen = []
    def extract(argv):
        folder = Path(next(arg[2:] for arg in argv if arg.startswith("-o")))
        for name in ("whisperstream.exe", "whisperstream-engine.exe", "llama-server.exe", "bin/whisperstream.exe"):
            if failure == "missing" and name == "whisperstream.exe":
                continue
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"bundled")
    def verify(path):
        seen.append(path)
        return build_script.b.SignatureResult(
            trusted=not (failure == "unsigned" and path.name == "whisperstream.exe"),
            tool="test", detail="not signed")
    monkeypatch.setattr(build_script.b, "run", extract)
    monkeypatch.setattr(build_script.b, "verify_signature", verify)
    if failure:
        with pytest.raises(build_script.MicrosoftStorePackageError):
            build_script._verify_microsoft_store_payload(tmp_path / "installer.exe")
    else:
        result = build_script._verify_microsoft_store_payload(tmp_path / "installer.exe")
        assert Path("installer-payload/whisperstream.exe") in result
        assert len(seen) == 4
        assert all(not path.exists() for path in seen)


def test_store_overlay_is_offline_and_does_not_replace_base_resources() -> None:
    config = json.loads(STORE_CONFIG.read_text(encoding="utf-8"))

    assert config == {
        "bundle": {
            "publisher": "Lanreal Technologies Inc.",
            "createUpdaterArtifacts": False,
            "windows": {"webviewInstallMode": {"type": "offlineInstaller"}},
        }
    }


def test_store_nsis_build_preserves_overlay_in_compile_and_bundle(monkeypatch) -> None:
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.delenv(build_script.b.TAURI_OVERLAY_ENV, raising=False)
    monkeypatch.setattr(
        build_script.b,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs["cwd"])),
    )
    monkeypatch.setattr(
        build_script,
        "_cli_resources_overlay_json",
        lambda: '{"bundle":{"resources":["base","bin/whisperstream.exe"]}}',
    )

    signing_overlay = Path("signing.json")
    build_script._step_tauri_nsis(
        signing_overlay,
        12,
        microsoft_store=True,
    )

    argv, cwd = calls[0]
    assert argv == [
        "cargo",
        "tauri",
        "build",
        "--config",
        "signing.json",
        "--config",
        '{"bundle":{"resources":["base","bin/whisperstream.exe"]}}',
        "--config",
        str(build_script.MICROSOFT_STORE_CONFIG),
        "--no-bundle", "--config", '{"bundle":{"windows":{"signCommand":null}}}',
    ]
    bundle_argv, bundle_cwd = calls[1]
    assert bundle_argv[:3] == ["cargo", "tauri", "bundle"]
    assert str(build_script.MICROSOFT_STORE_CONFIG) in bundle_argv
    assert json.loads(bundle_argv[-1]) == {"build": {"beforeBundleCommand": None}}
    assert bundle_cwd == cwd
    assert cwd == build_script.b.REPO_ROOT / "src-tauri"


@pytest.mark.parametrize(
    "argv",
    [
        ["--microsoft-store", "--portable"],
        ["--microsoft-store", "--experimental"],
    ],
)
def test_store_mode_rejects_incompatible_build_modes(argv) -> None:
    with pytest.raises(SystemExit, match="2"):
        build_script.main(argv)


def test_store_manifest_records_exact_artifact(monkeypatch, tmp_path) -> None:
    version = "1.5.0"
    installer_dir = tmp_path / "src-tauri" / "target" / "release" / "bundle" / "nsis"
    installer_dir.mkdir(parents=True)
    installer = installer_dir / f"Whisperstream_{version}_x64-setup.exe"
    installer.write_bytes(b"exact signed store installer")
    monkeypatch.setattr(build_script.b, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_script.b, "iso_utc_now", lambda: "2026-09-02T05:00:00Z")
    monkeypatch.setattr(
        build_script,
        "_verify_microsoft_store_pe_signatures",
        lambda exact_installer: [exact_installer],
    )

    output, manifest_path = build_script._stage_microsoft_store_artifact(
        version,
        12,
        signed=True,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert output.read_bytes() == installer.read_bytes()
    assert manifest == {
        "schema_version": 1,
        "app_version": version,
        "package": {
            "filename": installer.name,
            "sha256": build_script.b.file_sha256(installer),
            "size_bytes": installer.stat().st_size,
            "architecture": "x64",
            "installer_type": "exe",
            "silent_install_arguments": "/S",
        },
        "tauri_config": "src-tauri/tauri.microsoft-store.conf.json",
        "signature_ready": True,
        "verified_pe_files": [
            "src-tauri/target/release/bundle/nsis/Whisperstream_1.5.0_x64-setup.exe"
        ],
        "generated_at": "2026-09-02T05:00:00Z",
    }


def test_unsigned_store_artifact_is_quarantined_from_submission_name(
    monkeypatch, tmp_path
) -> None:
    version = "1.5.0"
    installer_dir = tmp_path / "src-tauri" / "target" / "release" / "bundle" / "nsis"
    installer_dir.mkdir(parents=True)
    installer = installer_dir / f"Whisperstream_{version}_x64-setup.exe"
    installer.write_bytes(b"unsigned store installer")
    monkeypatch.setattr(build_script.b, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_script.b, "iso_utc_now", lambda: "2026-09-02T05:00:00Z")

    output, manifest_path = build_script._stage_microsoft_store_artifact(
        version,
        12,
        signed=False,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert output.name.startswith("UNSIGNED-")
    assert manifest_path.name == "submission-UNSIGNED.json"
    assert manifest["signature_ready"] is False
    assert manifest["verified_pe_files"] == []


def test_store_signature_audit_reports_all_missing_and_untrusted_files(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(build_script.b, "REPO_ROOT", tmp_path)
    installer = tmp_path / "store.exe"
    installer.write_bytes(b"installer")
    binaries = tmp_path / "src-tauri" / "binaries"
    binaries.mkdir(parents=True)
    untrusted = binaries / "llama.dll"
    untrusted.write_bytes(b"dll")

    class Result:
        def __init__(self, trusted: bool, detail: str):
            self.trusted = trusted
            self.detail = detail

    monkeypatch.setattr(
        build_script.b,
        "verify_signature",
        lambda path: Result(Path(path) != untrusted, "not trusted"),
    )

    with pytest.raises(build_script.MicrosoftStorePackageError) as exc_info:
        build_script._verify_microsoft_store_pe_signatures(installer)

    message = str(exc_info.value)
    assert "llama.dll: not trusted" in message
    assert "whisperstream-engine-x86_64-pc-windows-msvc.exe: missing" in message
    assert "llama-server-x86_64-pc-windows-msvc.exe: missing" in message
    assert "bin\\whisperstream.exe: missing" in message or "bin/whisperstream.exe: missing" in message


def test_store_signature_audit_returns_sorted_verified_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(build_script, "_verify_microsoft_store_payload", lambda _: [])
    monkeypatch.setattr(build_script.b, "REPO_ROOT", tmp_path)
    installer = tmp_path / "store.exe"
    required = [
        installer,
        tmp_path / "src-tauri" / "binaries" / "whisperstream-engine-x86_64-pc-windows-msvc.exe",
        tmp_path / "src-tauri" / "binaries" / "llama-server-x86_64-pc-windows-msvc.exe",
        tmp_path / "src-tauri" / "bin" / "whisperstream.exe",
        tmp_path / "src-tauri" / "binaries" / "llama.dll",
    ]
    for path in required:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"signed")

    class Result:
        trusted = True
        detail = "Valid"

    monkeypatch.setattr(build_script.b, "verify_signature", lambda _path: Result())

    verified = build_script._verify_microsoft_store_pe_signatures(installer)

    assert verified == sorted(required, key=lambda path: str(path).lower())


def test_store_pipeline_never_generates_normal_update_manifest() -> None:
    source = Path(build_script.__file__).read_text(encoding="utf-8")
    signed_branch = source.split("# Default always-signed path", 1)[1]
    assert "if microsoft_store:" in signed_branch
    store_branch = signed_branch.split("if microsoft_store:", 1)[1].split("else:", 1)[0]
    assert "_stage_microsoft_store_artifact" in store_branch
    assert "_generate_latest_json" not in store_branch
