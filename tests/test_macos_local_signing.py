"""Local identity selection and build propagation, without touching Keychain."""
import json
from pathlib import Path

import pytest

from scripts import macos_local_signing as signing
from scripts import build_macos_internal as builder
from scripts import audit_macos_internal_bundle as auditor
from types import SimpleNamespace


def test_missing_identity_requires_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(signing, "certificate_path", lambda: tmp_path / "missing.pem")
    with pytest.raises(RuntimeError, match="setup once"):
        signing.identity()


@pytest.mark.parametrize("listing", ['1) OTHER "Whisperstream Local Development"', '1) PINNED "Other App"', '0 valid identities found'])
def test_identity_does_not_fall_back_to_another_key(tmp_path, monkeypatch, listing):
    certificate = tmp_path / "cert.pem"
    certificate.touch()
    monkeypatch.setattr(signing, "certificate_path", lambda: certificate)
    monkeypatch.setattr(signing, "fingerprint", lambda _: "PINNED")
    monkeypatch.setattr(signing, "run", lambda *args: listing)
    with pytest.raises(RuntimeError, match="unavailable or untrusted"):
        signing.identity()


def test_setup_reuses_existing_valid_identity_without_mutation(tmp_path, monkeypatch):
    certificate = tmp_path / "cert.pem"
    certificate.touch()
    monkeypatch.setattr(signing.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(signing, "certificate_path", lambda: certificate)
    monkeypatch.setattr(signing, "identity", lambda: "PINNED")
    monkeypatch.setattr(signing, "run", lambda *args: pytest.fail("must not recreate or import a key"))
    assert signing.setup() == "PINNED"


def test_tauri_receives_pinned_identity_before_bundling():
    argv = builder._tauri_build_argv(Path("tauri"), "both", "PINNED")
    config = json.loads(argv[argv.index("--config") + 1])
    assert config["bundle"]["macOS"]["signingIdentity"] == "PINNED"
    assert argv[argv.index("--bundles") + 1] == "app,dmg"


def test_only_signing_process_gets_login_keychain_home():
    original = {"HOME": "/isolated", "CFFIXED_USER_HOME": "/isolated", "WHISPERSTREAM_DATA_DIR": "/isolated/data", "WHISPERSTREAM_BUILD_SIGNING_HOME": "/owner"}
    result = builder._signing_environment(original)
    assert result["HOME"] == result["CFFIXED_USER_HOME"] == "/owner"
    assert result["WHISPERSTREAM_DATA_DIR"] == "/isolated/data"
    assert original["HOME"] == "/isolated"


def test_native_resource_signing_uses_copies_not_symlink_targets(tmp_path, monkeypatch):
    directory = tmp_path / "src-tauri/resources/llama-cpp"
    directory.mkdir(parents=True)
    library = directory / "libtest.dylib"
    library.write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic")
    (directory / "LICENSE").write_text("license")
    (directory / "alias.dylib").symlink_to(library)
    calls = []
    monkeypatch.setattr(builder, "_run", lambda argv, **kwargs: calls.append(argv))
    builder._sign_staged_binaries(tmp_path, {"WHISPERSTREAM_BUILD_SIGNING_IDENTITY": "PINNED"}, tmp_path)
    assert len(calls) == 1
    assert calls[0][-1] == str(library)
    assert calls[0][calls[0].index("--sign") + 1] == "PINNED"


@pytest.mark.parametrize("helper_matches", [True, False])
def test_audit_checks_helper_certificate_not_only_app(monkeypatch, helper_matches):
    monkeypatch.setattr(auditor, "local_signing_identity", lambda: "PINNED")
    monkeypatch.setattr(auditor, "_mach_o_candidates", lambda _: [Path("helper")])
    checked = []
    def run(argv, **kwargs):
        stderr = "Authority=Whisperstream Local Development\nTeamIdentifier=not set" if "-dv" in argv else ""
        result = 0
        if "-R" in argv:
            checked.append(argv[-1])
            assert argv[argv.index("-R") + 1] == '=certificate leaf = H"PINNED"'
            if argv[-1] == "helper" and not helper_matches:
                result = 1
        return SimpleNamespace(returncode=result, stdout="", stderr=stderr)
    monkeypatch.setattr(auditor, "_run", run)
    if helper_matches:
        assert auditor._signature(Path("app"))["kind"] == "self-signed"
    else:
        with pytest.raises(auditor.BundleAuditError, match="pinned local"):
            auditor._signature(Path("app"))
    assert checked == ["app", "helper"]
