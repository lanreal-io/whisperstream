"""Unit tests for Task 1.2: latest.json generation + CHANGELOG note extraction.

Tests are written TDD-style: they describe the desired behavior BEFORE the
functions are implemented. Run with:

    uv run pytest tests/test_build_release_lib.py -q
"""

from __future__ import annotations

import json
import hashlib
import importlib.util
import inspect
import re
import sys
from pathlib import Path

import pytest

# Allow importing scripts/_build_lib.py without installing it as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _build_lib as b
import build as build_script

# Fake checkouts carry the updater policy where the official build would name it.
FAKE_UPDATER_POLICY = "edition/updater-policy.json"


@pytest.fixture(autouse=True)
def _updater_policy_named(monkeypatch):
    monkeypatch.setenv(b.UPDATER_POLICY_ENV, FAKE_UPDATER_POLICY)


def test_ordinary_children_do_not_inherit_signing_or_publication_credentials(monkeypatch):
    import subprocess
    for name in ("AZURE_CLIENT_SECRET", "TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "R2_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "synthetic")
    captured = {}
    def execute(*args, **kwargs):
        assert kwargs["env"] is not None
        captured.update(kwargs["env"] or {})
        return subprocess.CompletedProcess(args[0], 0)
    monkeypatch.setattr(b.subprocess, "run", execute)
    b.run(["ordinary-build-child"])
    assert not any(name in captured for name in ("AZURE_CLIENT_SECRET", "TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "R2_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"))


def test_signer_receives_only_its_credential_scope(monkeypatch, tmp_path):
    import subprocess
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "synthetic-updater")
    captured = {}
    def execute(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0)
    monkeypatch.setattr(b.subprocess, "run", execute)
    b.sign_file(b.SigningConfig("synthetic-id", "synthetic-secret", "synthetic-tenant"), tmp_path / "synthetic.exe", "synthetic")
    assert captured["AZURE_CLIENT_SECRET"] == "synthetic-secret"
    assert "TAURI_SIGNING_PRIVATE_KEY" not in captured


def test_signed_nsis_compilation_and_bundling_have_separate_authority(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(b, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    build_script._step_tauri_nsis(tmp_path / "overlay.json", 12)
    assert calls[0][0][:3] == ["cargo", "tauri", "build"]
    assert "--no-bundle" in calls[0][0]
    assert not calls[0][1].get("permitted_secrets")
    assert calls[1][0][:3] == ["cargo", "tauri", "bundle"]
    assert "AZURE_CLIENT_SECRET" in calls[1][1]["permitted_secrets"]


@pytest.mark.parametrize("publisher,expected", [("Lanreal Technologies Inc.", True), ("Other Publisher", False)])
def test_release_requires_expected_publisher(monkeypatch, tmp_path, publisher, expected):
    import subprocess
    monkeypatch.setattr(b, "_find_signtool", lambda: Path("signtool.exe"))
    def execute(command, **kwargs):
        output = SIGNTOOL_SUCCESS_OUTPUT if command[0] == "signtool.exe" else json.dumps({"status": "Valid", "publisher": publisher})
        return subprocess.CompletedProcess(command, 0, output, "")
    monkeypatch.setattr(b.subprocess, "run", execute)
    assert b.verify_signature(tmp_path / "artifact.exe").trusted is expected


# ---------------------------------------------------------------------------
# Signed-release legal document gate
# ---------------------------------------------------------------------------

def _write_legal_headers(
    root: Path,
    *,
    eula_source_date: str = "2026-07-28",
    privacy_source_date: str = "2026-07-28",
    eula_bundle_date: str = "2026-07-28",
    privacy_bundle_date: str = "2026-07-28",
    privacy_source_version: str = "1.3",
) -> None:
    (root / "legal").mkdir(parents=True)
    (root / "src-tauri" / "resources").mkdir(parents=True)
    (root / FAKE_UPDATER_POLICY).parent.mkdir(parents=True, exist_ok=True)
    (root / FAKE_UPDATER_POLICY).write_text(json.dumps({"mode":"customer", "cutoff_utc":None, "download_page":"https://whisperstream.io/download"}))
    eula_source = (
        f"# EULA\n\n**Version 1.3**\n\n**Effective {eula_source_date}**\n"
    )
    privacy_source = (
        f"# Privacy\n\n**Version {privacy_source_version}**\n\n**Effective {privacy_source_date}**\n"
    )
    (root / "legal" / "EULA.md").write_text(eula_source, encoding="utf-8")
    (root / "legal" / "PRIVACY.md").write_text(privacy_source, encoding="utf-8")
    eula_bundle_source = eula_source.replace(
        f"Effective {eula_source_date}",
        f"Effective {eula_bundle_date}",
    )
    privacy_bundle_source = privacy_source.replace(
        f"Effective {privacy_source_date}",
        f"Effective {privacy_bundle_date}",
    )
    eula_bytes = b._render_derived_legal_document(
        eula_bundle_source,
        "derive-eula-txt.py",
    )
    resources = root / "src-tauri" / "resources"
    (resources / "EULA.txt").write_bytes(eula_bytes)
    (resources / "eula").mkdir()
    (resources / "eula" / "1.3.txt").write_bytes(eula_bytes)
    (resources / "eula-policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "notice_id": "eula-1.3-current",
                "requirement_kind": "reacknowledgement",
                "terms_version": "1.3",
                "terms_sha256": hashlib.sha256(eula_bytes).hexdigest(),
                "terms_path": "eula/1.3.txt",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps(
            {
                "bundle": {
                    "resources": [
                        "resources/EULA.txt",
                        "resources/PRIVACY.txt",
                        "resources/eula-policy.json",
                        "resources/eula/1.3.txt",
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "src-tauri" / "resources" / "PRIVACY.txt").write_bytes(
        b._render_derived_legal_document(
            privacy_bundle_source,
            "derive-privacy-txt.py",
        )
    )
    (root / "THIRD_PARTY_LICENSES.txt").write_text(
        "generated licenses\n",
        encoding="utf-8",
    )
    (root / "src-tauri" / "resources" / "THIRD_PARTY_LICENSES.txt").write_text(
        "generated licenses\n",
        encoding="utf-8",
    )
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "latest.json").write_text(
        '{"version":"1.3.0"}\n',
        encoding="utf-8",
    )


def test_installer_licence_page_is_the_gpl_in_nsis_format():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    assert config["bundle"]["licenseFile"] == "resources/GPL-3.0.txt"
    page = (root / "src-tauri" / "resources" / "GPL-3.0.txt").read_bytes()
    assert page == (root / "LICENSE").read_bytes().replace(b"\n", b"\r\n")
    assert page.isascii() and not page.startswith(b"\xef\xbb\xbf")
    assert max(len(line) for line in page.split(b"\r\n")) <= 80


class TestPackagedLegalResources:
    def test_unpublished_dates_allowed_with_complete_resources(self, tmp_path):
        _write_legal_headers(tmp_path, eula_source_date="UNPUBLISHED", eula_bundle_date="UNPUBLISHED")
        b.validate_packaged_legal_resources(tmp_path)

    @pytest.mark.parametrize("resource", [
        "resources/eula/1.3.txt", "resources/eula-policy.json",
        "resources/EULA.txt", "resources/PRIVACY.txt",
    ])
    def test_missing_packaging_entry_rejected(self, tmp_path, resource):
        _write_legal_headers(tmp_path)
        historical = tmp_path / "src-tauri/resources/eula/1.2.txt"
        historical.write_text("Historical agreement")
        path = tmp_path / "src-tauri/tauri.conf.json"
        config = json.loads(path.read_text())
        config["bundle"]["resources"].append("resources/eula/1.2.txt")
        config["bundle"]["resources"].remove(resource)
        path.write_text(json.dumps(config))
        with pytest.raises(b.LegalReleaseGateError, match="bundle.resources"):
            b.validate_packaged_legal_resources(tmp_path)

    @pytest.mark.parametrize("damage", ["missing", "hash", "installer"])
    def test_invalid_terms_rejected(self, tmp_path, damage):
        _write_legal_headers(tmp_path)
        terms = tmp_path / "src-tauri/resources/eula/1.3.txt"
        if damage == "missing":
            terms.unlink()
        elif damage == "hash":
            terms.write_bytes(terms.read_bytes() + b"changed")
        else:
            (tmp_path / "src-tauri/resources/EULA.txt").write_bytes(b"different")
        with pytest.raises(b.LegalReleaseGateError):
            b.validate_packaged_legal_resources(tmp_path)

    def test_missing_declared_historical_terms_rejected(self, tmp_path):
        _write_legal_headers(tmp_path)
        path = tmp_path / "src-tauri/tauri.conf.json"
        config = json.loads(path.read_text())
        config["bundle"]["resources"].append("resources/eula/1.2.txt")
        path.write_text(json.dumps(config))
        with pytest.raises(b.LegalReleaseGateError, match="missing or empty"):
            b.validate_packaged_legal_resources(tmp_path)

    def test_derivation_cannot_leave_installer_terms_mismatched(self, tmp_path, monkeypatch):
        _write_legal_headers(tmp_path)
        monkeypatch.setattr(b, "REPO_ROOT", tmp_path)
        def regenerate(argv):
            if argv[-1].endswith("derive-eula-txt.py"):
                (tmp_path / "src-tauri/resources/EULA.txt").write_bytes(b"changed terms")
        monkeypatch.setattr(b, "run", regenerate)
        with pytest.raises(b.LegalReleaseGateError, match="installer EULA bytes"):
            build_script._step_derive_eula(12)

    @pytest.mark.parametrize("unsigned", [True, False])
    @pytest.mark.parametrize("portable", [True, False])
    @pytest.mark.parametrize("skip_nuitka", [True, False])
    def test_pipeline_rejects_omitted_terms_before_any_build_work(
        self, tmp_path, monkeypatch, unsigned, portable, skip_nuitka,
    ):
        _write_legal_headers(tmp_path)
        path = tmp_path / "src-tauri/tauri.conf.json"
        config = json.loads(path.read_text())
        config["bundle"]["resources"].remove("resources/eula/1.3.txt")
        path.write_text(json.dumps(config))
        monkeypatch.setattr(b, "REPO_ROOT", tmp_path)
        def unexpected(*args, **kwargs):
            pytest.fail("Build work started before resource validation")
        for name in ("stamp_version", "read_tauri_version", "validate_legal_release_documents"):
            monkeypatch.setattr(b, name, unexpected)
        monkeypatch.setattr(build_script, "_step_nuitka", unexpected)
        with pytest.raises(b.LegalReleaseGateError, match="policy-selected terms"):
            build_script._run_build(portable=portable, skip_nuitka=skip_nuitka, unsigned=unsigned)


class TestLegalReleaseDocuments:
    def test_accepts_independent_versions_with_required_publication_date(self, tmp_path):
        _write_legal_headers(tmp_path, privacy_source_version="2.0")
        b.validate_legal_release_documents(tmp_path, required_effective_date="2026-07-28")

    def test_rejects_stale_privacy_version_with_matching_dates(self, tmp_path):
        _write_legal_headers(tmp_path, privacy_source_version="2.0")
        path = tmp_path / "src-tauri/resources/PRIVACY.txt"
        path.write_bytes(path.read_bytes().replace(b"Version 2.0", b"Version 1.3"))
        with pytest.raises(b.LegalReleaseGateError, match="Version/Effective header is stale"):
            b.validate_legal_release_documents(tmp_path)

    def test_accepts_matching_concrete_source_and_bundle_headers(self, tmp_path):
        _write_legal_headers(tmp_path)

        b.validate_legal_release_documents(tmp_path)

    @pytest.mark.parametrize(
        "field",
        [
            "eula_source_date",
            "privacy_source_date",
            "eula_bundle_date",
            "privacy_bundle_date",
        ],
    )
    def test_rejects_placeholder_in_every_required_header(self, tmp_path, field):
        _write_legal_headers(tmp_path, **{field: "YYYY-MM-DD"})

        with pytest.raises(b.LegalReleaseGateError, match="YYYY-MM-DD"):
            b.validate_legal_release_documents(tmp_path)

    def test_rejects_invalid_calendar_date(self, tmp_path):
        _write_legal_headers(tmp_path, privacy_source_date="2026-02-30")

        with pytest.raises(b.LegalReleaseGateError, match="not a valid"):
            b.validate_legal_release_documents(tmp_path, source_only=True)

    def test_rejects_non_hyphenated_date(self, tmp_path):
        _write_legal_headers(tmp_path, privacy_source_date="20260728")

        with pytest.raises(b.LegalReleaseGateError, match="YYYY-MM-DD format"):
            b.validate_legal_release_documents(tmp_path, source_only=True)

    def test_rejects_eula_privacy_source_date_mismatch(self, tmp_path):
        _write_legal_headers(tmp_path, privacy_source_date="2026-07-29")

        with pytest.raises(b.LegalReleaseGateError, match="do not match"):
            b.validate_legal_release_documents(tmp_path, source_only=True)

    def test_rejects_effective_date_other_than_required_publication_date(
        self, tmp_path
    ):
        _write_legal_headers(tmp_path)

        with pytest.raises(b.LegalReleaseGateError, match="required publication"):
            b.validate_legal_release_documents(
                tmp_path,
                required_effective_date="2026-07-29",
            )

    def test_rejects_stale_derived_header(self, tmp_path):
        _write_legal_headers(tmp_path, privacy_bundle_date="2026-07-27")

        with pytest.raises(b.LegalReleaseGateError, match="stale"):
            b.validate_legal_release_documents(tmp_path)

    def test_rejects_stale_derived_content_with_matching_header(self, tmp_path):
        _write_legal_headers(tmp_path)
        privacy_bundle = tmp_path / "src-tauri" / "resources" / "PRIVACY.txt"
        privacy_bundle.write_bytes(
            privacy_bundle.read_bytes().replace(b"PRIVACY", b"PRIVACY CHANGED", 1)
        )

        with pytest.raises(b.LegalReleaseGateError, match="full content is stale"):
            b.validate_legal_release_documents(tmp_path)

    def test_rejects_policy_hash_mismatch(self, tmp_path):
        _write_legal_headers(tmp_path)
        policy_path = tmp_path / "src-tauri" / "resources" / "eula-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["terms_sha256"] = "0" * 64
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        with pytest.raises(b.LegalReleaseGateError, match="SHA-256"):
            b.validate_legal_release_documents(tmp_path)

    def test_rejects_policy_selected_terms_version_mismatch(self, tmp_path):
        _write_legal_headers(tmp_path)
        terms_path = tmp_path / "src-tauri" / "resources" / "eula" / "1.3.txt"
        mismatched = terms_path.read_bytes().replace(b"Version 1.3", b"Version 1.4")
        terms_path.write_bytes(mismatched)
        policy_path = tmp_path / "src-tauri" / "resources" / "eula-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["terms_sha256"] = hashlib.sha256(mismatched).hexdigest()
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        with pytest.raises(b.LegalReleaseGateError, match="Version"):
            b.validate_legal_release_documents(tmp_path)

    def test_rejects_installer_eula_different_from_selected_terms(self, tmp_path):
        _write_legal_headers(tmp_path)
        installer_eula = tmp_path / "src-tauri" / "resources" / "EULA.txt"
        installer_eula.write_bytes(installer_eula.read_bytes() + b"changed\r\n")

        with pytest.raises(b.LegalReleaseGateError, match="installer EULA bytes"):
            b.validate_legal_release_documents(tmp_path)

    def test_rejects_policy_selected_terms_missing_from_tauri_resources(self, tmp_path):
        _write_legal_headers(tmp_path)
        config_path = tmp_path / "src-tauri" / "tauri.conf.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["bundle"]["resources"].remove("resources/eula/1.3.txt")
        config_path.write_text(json.dumps(config), encoding="utf-8")

        with pytest.raises(
            b.LegalReleaseGateError,
            match="policy-selected terms are missing",
        ):
            b.validate_legal_release_documents(tmp_path)

    @pytest.mark.parametrize("privacy_version", ["1.3", "2.0"])
    def test_attestation_binds_documents_and_installer(self, tmp_path, privacy_version):
        _write_legal_headers(tmp_path, privacy_source_version=privacy_version)
        installer = tmp_path / "Whisperstream_1.3.0_x64-setup.exe"
        updater_sig = tmp_path / "Whisperstream_1.3.0_x64-setup.exe.sig"
        installer.write_bytes(b"signed installer bytes")
        updater_sig.write_bytes(b"signed updater bytes")

        output = b.write_legal_release_attestation(
            "1.3.0",
            installer,
            updater_sig,
            root=tmp_path,
        )

        assert output == tmp_path / "dist" / "legal-release.json"
        attestation = json.loads(output.read_text(encoding="utf-8"))
        assert attestation["schema_version"] == 5
        assert set(attestation["artifacts"]) == {
            "installer",
            "updater_signature",
            "latest_json",
        }
        assert {
            "THIRD_PARTY_LICENSES.txt",
            "src-tauri/resources/THIRD_PARTY_LICENSES.txt",
            "src-tauri/resources/eula-policy.json",
            "src-tauri/resources/eula/1.3.txt",
        } <= set(attestation["documents"])
        b.verify_legal_release_attestation(
            "1.3.0",
            installer,
            updater_sig,
            root=tmp_path,
        )

    def test_attestation_rejects_changed_installer(self, tmp_path):
        _write_legal_headers(tmp_path)
        installer = tmp_path / "Whisperstream_1.3.0_x64-setup.exe"
        updater_sig = tmp_path / "Whisperstream_1.3.0_x64-setup.exe.sig"
        installer.write_bytes(b"original")
        updater_sig.write_bytes(b"updater")
        b.write_legal_release_attestation(
            "1.3.0", installer, updater_sig, root=tmp_path
        )
        installer.write_bytes(b"replacement")

        with pytest.raises(b.LegalReleaseGateError, match="does not match"):
            b.verify_legal_release_attestation(
                "1.3.0",
                installer,
                updater_sig,
                root=tmp_path,
            )

    def test_attestation_rejects_changed_effective_date_field(self, tmp_path):
        _write_legal_headers(tmp_path)
        installer = tmp_path / "Whisperstream_1.3.0_x64-setup.exe"
        updater_sig = tmp_path / "Whisperstream_1.3.0_x64-setup.exe.sig"
        installer.write_bytes(b"installer")
        updater_sig.write_bytes(b"updater")
        output = b.write_legal_release_attestation(
            "1.3.0", installer, updater_sig, root=tmp_path
        )
        attestation = json.loads(output.read_text(encoding="utf-8"))
        attestation["effective_date"] = "2026-07-27"
        output.write_text(json.dumps(attestation), encoding="utf-8")

        with pytest.raises(b.LegalReleaseGateError, match="does not match"):
            b.verify_legal_release_attestation(
                "1.3.0",
                installer,
                updater_sig,
                root=tmp_path,
            )

    def test_attestation_rejects_changed_updater_signature(self, tmp_path):
        _write_legal_headers(tmp_path)
        installer = tmp_path / "Whisperstream_1.3.0_x64-setup.exe"
        updater_sig = tmp_path / "Whisperstream_1.3.0_x64-setup.exe.sig"
        installer.write_bytes(b"installer")
        updater_sig.write_bytes(b"original")
        b.write_legal_release_attestation(
            "1.3.0", installer, updater_sig, root=tmp_path
        )
        updater_sig.write_bytes(b"replacement")

        with pytest.raises(b.LegalReleaseGateError, match="does not match"):
            b.verify_legal_release_attestation(
                "1.3.0",
                installer,
                updater_sig,
                root=tmp_path,
            )

    def test_attestation_rejects_changed_update_manifest(self, tmp_path):
        _write_legal_headers(tmp_path)
        installer = tmp_path / "Whisperstream_1.3.0_x64-setup.exe"
        updater_sig = tmp_path / "Whisperstream_1.3.0_x64-setup.exe.sig"
        installer.write_bytes(b"installer")
        updater_sig.write_bytes(b"updater")
        b.write_legal_release_attestation(
            "1.3.0", installer, updater_sig, root=tmp_path
        )
        (tmp_path / "dist" / "latest.json").write_text(
            '{"version":"tampered"}\n',
            encoding="utf-8",
        )

        with pytest.raises(b.LegalReleaseGateError, match="does not match"):
            b.verify_legal_release_attestation(
                "1.3.0",
                installer,
                updater_sig,
                root=tmp_path,
            )

    def test_attestation_rejects_changed_generated_licenses(self, tmp_path):
        _write_legal_headers(tmp_path)
        installer = tmp_path / "Whisperstream_1.3.0_x64-setup.exe"
        updater_sig = tmp_path / "Whisperstream_1.3.0_x64-setup.exe.sig"
        installer.write_bytes(b"installer")
        updater_sig.write_bytes(b"updater")
        b.write_legal_release_attestation(
            "1.3.0", installer, updater_sig, root=tmp_path
        )
        (tmp_path / "THIRD_PARTY_LICENSES.txt").write_text(
            "tampered\n",
            encoding="utf-8",
        )

        with pytest.raises(b.LegalReleaseGateError, match="does not match"):
            b.verify_legal_release_attestation(
                "1.3.0",
                installer,
                updater_sig,
                root=tmp_path,
            )

    def test_attestation_rejects_legacy_build_without_provenance(self, tmp_path):
        _write_legal_headers(tmp_path)
        installer = tmp_path / "Whisperstream_1.3.0_x64-setup.exe"
        updater_sig = tmp_path / "Whisperstream_1.3.0_x64-setup.exe.sig"
        installer.write_bytes(b"legacy")
        updater_sig.write_bytes(b"legacy updater")

        with pytest.raises(b.LegalReleaseGateError, match="rebuild"):
            b.verify_legal_release_attestation(
                "1.3.0",
                installer,
                updater_sig,
                root=tmp_path,
            )

    def test_attestation_write_rejects_missing_update_manifest(self, tmp_path):
        _write_legal_headers(tmp_path)
        (tmp_path / "dist" / "latest.json").unlink()
        installer = tmp_path / "Whisperstream_1.3.0_x64-setup.exe"
        updater_sig = tmp_path / "Whisperstream_1.3.0_x64-setup.exe.sig"
        installer.write_bytes(b"installer")
        updater_sig.write_bytes(b"updater")

        with pytest.raises(b.LegalReleaseGateError, match="cannot write"):
            b.write_legal_release_attestation(
                "1.3.0",
                installer,
                updater_sig,
                root=tmp_path,
            )


def test_eula_derivation_preserves_prior_versioned_resources(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parent.parent / "scripts" / "derive-eula-txt.py"
    spec = importlib.util.spec_from_file_location("derive_eula_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    resources = tmp_path / "resources"
    prior = resources / "eula" / "1.2.txt"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"prior agreement\r\n")
    monkeypatch.setattr(module, "DST", resources / "EULA.txt")
    monkeypatch.setattr(module, "VERSIONED_DST_DIR", resources / "eula")
    rendered = "Version 1.3\r\n\r\nEffective 2026-07-31\r\n"

    _, versioned = module.write_outputs(rendered)

    assert prior.read_bytes() == b"prior agreement\r\n"
    assert versioned == resources / "eula" / "1.3.txt"
    assert versioned.read_bytes() == rendered.encode("ascii")


# ---------------------------------------------------------------------------
# Fixture: a realistic CHANGELOG.md excerpt (Keep a Changelog format)
# ---------------------------------------------------------------------------

CHANGELOG_FIXTURE = """\
# Changelog

All notable changes to Whisperstream are documented here.

## [Unreleased]

- Some upcoming thing.

## [1.0.1] - 2026-06-24

- **Keystroke Input Mode:** Whisperstream can type your text out character by character instead of pasting it, so dictation works in remote sessions like Citrix and RDP and alongside screen readers.
- **Paste Without Enhancement:** set a shortcut to paste your most recent transcription before AI cleanup, so the original is always one keypress away.

## [1.0.0] - 2026-06-21

- **Automatic Profile Switching:** give each app or website its own AI cleanup style, and Whisperstream detects the active window and applies the right one.
- **Transcript History:** a searchable, private record of everything you dictate, with audio playback. It stays encrypted on your device.
- **File Import:** drop in existing audio files to transcribe them.
- Set separate keys for push-to-talk and toggle recording.
- Free trial is now unlimited use for 7 days.

## [0.9.0] - 2026-06-08

- AI Enhancement now runs a built-in local model on computers with a capable graphics card.
"""


# ---------------------------------------------------------------------------
# extract_changelog_notes
# ---------------------------------------------------------------------------

class TestExtractChangelogNotes:
    """Tests for extract_changelog_notes(version, changelog_text) -> str | None."""

    def test_notes_present_returns_stripped_body(self):
        """When the version has a matching section, return the stripped bullet body."""
        notes = b.extract_changelog_notes("1.0.1", CHANGELOG_FIXTURE)
        assert notes is not None
        # Should include the two bullets
        assert "Keystroke Input Mode" in notes
        assert "Paste Without Enhancement" in notes
        # Should NOT include the section heading itself
        assert "## [1.0.1]" not in notes
        # Should NOT include content from adjacent sections
        assert "Automatic Profile Switching" not in notes

    def test_section_missing_returns_none(self):
        """When no section matches the version, return None."""
        notes = b.extract_changelog_notes("9.9.9", CHANGELOG_FIXTURE)
        assert notes is None

    def test_multi_line_notes_preserved(self):
        """Multi-line sections return all lines, stripped of leading/trailing whitespace."""
        notes = b.extract_changelog_notes("1.0.0", CHANGELOG_FIXTURE)
        assert notes is not None
        # 1.0.0 has 5 bullets
        lines = [line for line in notes.splitlines() if line.strip()]
        assert len(lines) == 5
        assert "Transcript History" in notes
        assert "File Import" in notes

    def test_stripped_body_has_no_leading_trailing_blank_lines(self):
        """The returned string must be stripped (no leading/trailing whitespace)."""
        notes = b.extract_changelog_notes("1.0.1", CHANGELOG_FIXTURE)
        assert notes is not None
        assert notes == notes.strip()

    def test_empty_changelog_returns_none(self):
        """An empty changelog string produces None for any version."""
        assert b.extract_changelog_notes("1.0.0", "") is None

    def test_version_with_adjacent_section_boundary(self):
        """The extraction stops at the next ## [ header (does not bleed through)."""
        notes = b.extract_changelog_notes("0.9.0", CHANGELOG_FIXTURE)
        assert notes is not None
        assert "AI Enhancement now runs" in notes
        # Must not bleed into [Unreleased] content or [1.0.0]
        assert "Automatic Profile Switching" not in notes
        assert "upcoming" not in notes


PLATFORM_CHANGELOG = """\
## [2.0.1] - 2026-10-27

### Fixed

- Shared fix.
- [Mac] Mac fix.
- [Windows] Windows fix
  that wraps onto a second line.

### Mac

- [mac] Native window polish.
"""


class TestChangelogPlatformFilter:
    """Platform-tagged bullets reach only their own platform's update feed."""

    def test_no_platform_keeps_every_line(self):
        notes = b.extract_changelog_notes("2.0.1", PLATFORM_CHANGELOG)
        assert "[Mac] Mac fix." in notes and "[Windows] Windows fix" in notes

    def test_mac_keeps_shared_and_mac_lines_without_tags(self):
        notes = b.extract_changelog_notes("2.0.1", PLATFORM_CHANGELOG, platform="mac")
        assert notes == (
            "### Fixed\n\n- Shared fix.\n- Mac fix.\n\n### Mac\n\n- Native window polish."
        )

    def test_windows_drops_mac_lines_continuations_and_empty_headings(self):
        notes = b.extract_changelog_notes("2.0.1", PLATFORM_CHANGELOG, platform="windows")
        assert notes == (
            "### Fixed\n\n- Shared fix.\n- Windows fix\n  that wraps onto a second line."
        )

    def test_section_with_only_other_platform_lines_returns_none(self):
        text = "## [2.0.1]\n\n- [Mac] Only Mac.\n"
        assert b.extract_changelog_notes("2.0.1", text, platform="windows") is None

    def test_unknown_platform_is_rejected(self):
        with pytest.raises(ValueError):
            b.extract_changelog_notes("2.0.1", PLATFORM_CHANGELOG, platform="linux")


# ---------------------------------------------------------------------------
# build_latest_manifest
# ---------------------------------------------------------------------------

SAMPLE_SIGNATURE = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYy1rZXkgYWJjZA=="
SAMPLE_NOTES = "- **Keystroke Input Mode:** type character by character."
SAMPLE_PUB_DATE = "2026-06-24T12:00:00Z"
SAMPLE_VERSION = "1.0.1"
# A synthetic feed; the official one comes from the build's Tauri overlay.
SAMPLE_BASE = "https://updates.example.invalid/feed/"


class TestBuildLatestManifest:
    """Tests for build_latest_manifest(version, pub_date, signature, notes, *, download_base)."""

    def test_url_schema_shape(self):
        """The manifest dict must have the exact keys and URL pattern from build.sh."""
        manifest = b.build_latest_manifest(
            SAMPLE_VERSION, SAMPLE_PUB_DATE, SAMPLE_SIGNATURE, SAMPLE_NOTES, download_base=SAMPLE_BASE
        )
        assert manifest["version"] == SAMPLE_VERSION
        assert manifest["notes"] == SAMPLE_NOTES
        assert manifest["pub_date"] == SAMPLE_PUB_DATE
        assert "platforms" in manifest
        win = manifest["platforms"]["windows-x86_64"]
        assert win["signature"] == SAMPLE_SIGNATURE
        expected_url = f"{SAMPLE_BASE}{SAMPLE_VERSION}/Whisperstream_{SAMPLE_VERSION}_x64-setup.exe"
        assert win["url"] == expected_url

    def test_fallback_string_format_when_notes_is_none(self):
        """When notes is None, the manifest notes field becomes 'Whisperstream v{version}'."""
        manifest = b.build_latest_manifest(
            SAMPLE_VERSION, SAMPLE_PUB_DATE, SAMPLE_SIGNATURE, None, download_base=SAMPLE_BASE
        )
        assert manifest["notes"] == f"Whisperstream v{SAMPLE_VERSION}"

    def test_fallback_string_format_no_em_dash(self):
        """Fallback string must not contain an em dash (project style rule)."""
        manifest = b.build_latest_manifest(
            "0.9.0", SAMPLE_PUB_DATE, SAMPLE_SIGNATURE, None, download_base=SAMPLE_BASE
        )
        assert "—" not in manifest["notes"]

    def test_key_order_matches_build_sh(self):
        """Key order must match build.sh exactly: version, notes, pub_date, platforms."""
        manifest = b.build_latest_manifest(
            SAMPLE_VERSION, SAMPLE_PUB_DATE, SAMPLE_SIGNATURE, SAMPLE_NOTES, download_base=SAMPLE_BASE
        )
        keys = list(manifest.keys())
        assert keys == ["version", "notes", "pub_date", "platforms"]
        platform_keys = list(manifest["platforms"]["windows-x86_64"].keys())
        assert platform_keys == ["signature", "url"]

    def test_json_dumps_indent_2_byte_matches_build_sh(self):
        """json.dumps(..., indent=2) of the manifest must match what build.sh produces."""
        manifest = b.build_latest_manifest(
            "1.0.1", "2026-06-24T12:00:00Z", SAMPLE_SIGNATURE, SAMPLE_NOTES, download_base=SAMPLE_BASE
        )
        rendered = json.dumps(manifest, indent=2)
        # Verify the rendered JSON is valid and has the correct structure
        reparsed = json.loads(rendered)
        assert reparsed["version"] == "1.0.1"
        assert reparsed["platforms"]["windows-x86_64"]["url"] == (
            "https://updates.example.invalid/feed/1.0.1/Whisperstream_1.0.1_x64-setup.exe"
        )
        # Verify indent=2 produces 2-space indentation (not 4)
        lines = rendered.splitlines()
        # The "notes" key is at the second line (index 1) and should be 2-space indented
        assert any(line.startswith("  \"notes\"") for line in lines)
        assert not any(line.startswith("    \"notes\"") for line in lines)

    def test_notes_present_uses_provided_notes(self):
        """When notes is a non-empty string, use it verbatim (no override)."""
        manifest = b.build_latest_manifest(
            SAMPLE_VERSION, SAMPLE_PUB_DATE, SAMPLE_SIGNATURE, SAMPLE_NOTES, download_base=SAMPLE_BASE
        )
        assert manifest["notes"] == SAMPLE_NOTES

    def test_feed_names_only_the_given_targets(self):
        manifest = b.build_update_manifest(
            SAMPLE_VERSION, SAMPLE_PUB_DATE, None,
            {"darwin-aarch64": {"signature": "sig", "url": f"{SAMPLE_BASE}{SAMPLE_VERSION}/Whisperstream.app.tar.gz"}},
        )
        assert list(manifest["platforms"]) == ["darwin-aarch64"]
        assert manifest["platforms"]["darwin-aarch64"] == {
            "signature": "sig", "url": f"{SAMPLE_BASE}{SAMPLE_VERSION}/Whisperstream.app.tar.gz"}

    @pytest.mark.parametrize("version", ["", "1.0", "1.0.0/../x", "../1.0.0", "1.0.0 "])
    def test_versioned_url_rejects_unsafe_versions(self, version):
        with pytest.raises(b.LegalReleaseGateError):
            b.versioned_artifact_url(SAMPLE_BASE, version, "Whisperstream.app.tar.gz")

    @pytest.mark.parametrize("base,name", [("https://x.invalid/feed", "a"), (SAMPLE_BASE, "a/b"), (SAMPLE_BASE, "")])
    def test_versioned_url_rejects_unsafe_locations(self, base, name):
        with pytest.raises(b.LegalReleaseGateError):
            b.versioned_artifact_url(base, "1.0.0", name)


# ---------------------------------------------------------------------------
# iso_utc_now
# ---------------------------------------------------------------------------

class TestIsoUtcNow:
    """Tests for iso_utc_now() -> str."""

    def test_returns_string(self):
        ts = b.iso_utc_now()
        assert isinstance(ts, str)

    def test_format_matches_date_u(self):
        """Must match the ``date -u +%Y-%m-%dT%H:%M:%SZ`` format."""
        ts = b.iso_utc_now()
        # Pattern: YYYY-MM-DDTHH:MM:SSZ
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts), (
            f"Unexpected iso_utc_now format: {ts!r}"
        )

    def test_ends_with_z(self):
        """Must end with 'Z' (UTC indicator), not an offset like +00:00."""
        ts = b.iso_utc_now()
        assert ts.endswith("Z")

    def test_no_microseconds(self):
        """Must not include microseconds (build.sh uses %S, not fractional)."""
        ts = b.iso_utc_now()
        assert "." not in ts


class TestUnsignedNsisBuild:
    """Unsigned packaging must not request a signed updater artifact."""

    def test_disables_updater_artifacts_for_unsigned_nsis(self, monkeypatch):
        calls = []
        monkeypatch.delenv(build_script.b.TAURI_OVERLAY_ENV, raising=False)
        monkeypatch.setattr(build_script.b, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))

        build_script._step_tauri_nsis_unsigned(10, experimental=True)

        argv, kwargs = calls[0]
        assert argv[:3] == ["cargo", "tauri", "build"]
        config = json.loads(argv[argv.index("--config") + 1])
        assert config["bundle"]["createUpdaterArtifacts"] is False
        assert kwargs["cwd"] == build_script.b.REPO_ROOT / "src-tauri"
        assert kwargs["env"]["VITE_EXPERIMENTAL"] == "1"

    def test_unsigned_pipeline_does_not_generate_signed_manifest(self):
        source = inspect.getsource(build_script._run_build)
        unsigned_branch = source.split("if unsigned:", 1)[1].split(
            "        else:\n            # Default always-signed path",
            1,
        )[0]
        assert "_generate_latest_json" not in unsigned_branch


# ---------------------------------------------------------------------------
# Task 2.1: find_nsis_installer + format_build_time
# ---------------------------------------------------------------------------

import datetime
import tempfile


class TestFindNsisInstaller:
    """Tests for find_nsis_installer(version, base_dir=...) -> tuple[Path, ...]."""

    def _make_nsis_dir(self, tmp: Path, version: str) -> Path:
        """Create the NSIS bundle directory inside a temp base_dir."""
        nsis_dir = tmp / "src-tauri" / "target" / "release" / "bundle" / "nsis"
        nsis_dir.mkdir(parents=True)
        return nsis_dir

    def _write_pair(self, nsis_dir: Path, version: str) -> tuple[Path, Path]:
        """Write the installer and its updater signature and return their paths."""
        exe = nsis_dir / f"Whisperstream_{version}_x64-setup.exe"
        sig = nsis_dir / f"Whisperstream_{version}_x64-setup.exe.sig"
        exe.write_bytes(b"fake-exe")
        sig.write_text("fakesignature", encoding="utf-8")
        return exe, sig

    def test_discovery_returns_installer_and_signature(self, tmp_path):
        """The installer is the update payload; its .exe.sig sits beside it."""
        version = "1.2.3"
        nsis_dir = self._make_nsis_dir(tmp_path, version)
        exe, sig = self._write_pair(nsis_dir, version)

        assert b.find_nsis_installer(version, base_dir=tmp_path) == (exe, sig)

    def test_discovery_ignores_a_v1_updater_zip(self, tmp_path):
        """A v1Compatible .nsis.zip is not an artifact and cannot stand in for the signature."""
        version = "1.2.3"
        nsis_dir = self._make_nsis_dir(tmp_path, version)
        (nsis_dir / f"Whisperstream_{version}_x64-setup.exe").write_bytes(b"fake-exe")
        (nsis_dir / f"Whisperstream_{version}_x64-setup.nsis.zip").write_bytes(b"zip")
        (nsis_dir / f"Whisperstream_{version}_x64-setup.nsis.zip.sig").write_text("sig")

        with pytest.raises(FileNotFoundError, match=r"x64-setup\.exe\.sig"):
            b.find_nsis_installer(version, base_dir=tmp_path)

    @pytest.mark.parametrize("missing", ["exe", "exe.sig"])
    def test_discovery_names_the_missing_file(self, tmp_path, missing):
        version = "1.2.3"
        nsis_dir = self._make_nsis_dir(tmp_path, version)
        self._write_pair(nsis_dir, version)
        (nsis_dir / f"Whisperstream_{version}_x64-setup.{missing}").unlink()

        with pytest.raises(FileNotFoundError) as exc_info:
            b.find_nsis_installer(version, base_dir=tmp_path)

        assert f"Whisperstream_{version}_x64-setup.{missing}" in str(exc_info.value)

    def test_return_values_are_path_objects(self, tmp_path):
        """All returned items must be pathlib.Path instances."""
        version = "2.0.0"
        nsis_dir = self._make_nsis_dir(tmp_path, version)
        self._write_pair(nsis_dir, version)

        result = b.find_nsis_installer(version, base_dir=tmp_path)

        for p in result:
            assert isinstance(p, Path), f"Expected Path, got {type(p)}: {p!r}"

    def test_default_base_dir_is_repo_root(self):
        """When base_dir is not supplied, the function uses REPO_ROOT."""
        # We do not have real artifacts, so it must raise FileNotFoundError
        # pointing into the real REPO_ROOT path (not some tmp dir).
        import pytest
        version = "0.0.0-nonexistent"
        with pytest.raises(FileNotFoundError) as exc_info:
            b.find_nsis_installer(version)
        # Error should mention the NSIS directory under the real repo root
        assert "nsis" in str(exc_info.value).lower() or version in str(exc_info.value)


class TestFormatBuildTime:
    """Tests for format_build_time(path) -> str."""

    def test_returns_string(self, tmp_path):
        """format_build_time must return a str."""
        f = tmp_path / "test.exe"
        f.write_bytes(b"x")
        result = b.format_build_time(f)
        assert isinstance(result, str)

    def test_includes_local_and_utc(self, tmp_path):
        """The returned string must reference both local and UTC times."""
        f = tmp_path / "test.exe"
        f.write_bytes(b"x")
        result = b.format_build_time(f)
        # Must include both "local" and "UTC" (case-insensitive) as zone labels
        lower = result.lower()
        assert "local" in lower, f"Expected 'local' in: {result!r}"
        assert "utc" in lower, f"Expected 'utc' in: {result!r}"

    def test_parseable_datetimes(self, tmp_path):
        """The build-time string must contain at least one parseable ISO-like datetime."""
        f = tmp_path / "test.exe"
        f.write_bytes(b"x")
        result = b.format_build_time(f)
        # Expect at least one segment matching YYYY-MM-DD
        assert re.search(r"\d{4}-\d{2}-\d{2}", result), (
            f"No parseable date in: {result!r}"
        )

    def test_no_em_dash(self, tmp_path):
        """The result must not contain an em dash (project style rule)."""
        f = tmp_path / "test.exe"
        f.write_bytes(b"x")
        result = b.format_build_time(f)
        assert "—" not in result, f"Em dash found in: {result!r}"

    def test_reflects_file_mtime(self, tmp_path):
        """The timestamps in the result come from the file's mtime, not current time."""
        import os
        import time

        f = tmp_path / "artifact.exe"
        f.write_bytes(b"data")

        # Force mtime to a known value well in the past (2020-01-15 12:00:00 UTC)
        known_ts = datetime.datetime(2020, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
        known_posix = known_ts.timestamp()
        os.utime(f, (known_posix, known_posix))

        result = b.format_build_time(f)

        # The year 2020 must appear (ruling out "current time" being used)
        assert "2020" in result, f"Expected 2020 (known mtime year) in: {result!r}"


# ---------------------------------------------------------------------------
# Task 2.2: Authenticode signature verification (signtool /pa, cmdlet fallback)
# ---------------------------------------------------------------------------

# Captured real signtool output (from a signed installer, rc=0)
SIGNTOOL_SUCCESS_OUTPUT = """\
Verifying: C:\\build\\Whisperstream_1.0.1_x64-setup.exe

Signature Index: 0 (Primary Signature)
Hash of file (sha256): 05F64E2CC821041C8225E4B91F2651538AB2B7C46367F65C7430F92839647D6D

Signing Certificate Chain:
    Issued to: Lanreal Technologies Inc.
    Issued by: Microsoft ID Verified CS EOC CA 04
    Expires:   Fri Jun 26 21:40:40 2026
    SHA1 hash: C9EA021D5595BBDD49B1749AFE863946568077FA

Successfully verified: C:\\build\\Whisperstream_1.0.1_x64-setup.exe

Number of files successfully Verified: 1
Number of warnings: 0
Number of errors: 0
"""

# Captured real signtool output (unsigned PE file, rc=1)
SIGNTOOL_NO_SIGNATURE_OUTPUT = """\
Verifying: C:\\build\\unsigned.exe

Number of files successfully Verified: 0
Number of warnings: 0
Number of errors: 1
SignTool Error: No signature found.
"""

# Captured real signtool output (format not recognized, rc=1)
SIGNTOOL_UNRECOGNIZED_OUTPUT = """\
Verifying: C:\\build\\notanexe.bin

Number of files successfully Verified: 0
Number of warnings: 0
Number of errors: 1
SignTool Error: This file format cannot be verified because it is not recognized.
"""

# Captured PowerShell cmdlet status strings
CMDLET_VALID = "Valid"
CMDLET_NOT_SIGNED = "NotSigned"
CMDLET_UNKNOWN_ERROR = "UnknownError"
CMDLET_HASH_MISMATCH = "HashMismatch"


class TestParseSigntoolResult:
    """Tests for parse_signtool_result(returncode, stdout) -> bool (pure, no file I/O)."""

    def test_rc_zero_with_successfully_verified_returns_true(self):
        """rc=0 + 'Successfully verified' in output -> True."""
        assert b.parse_signtool_result(0, SIGNTOOL_SUCCESS_OUTPUT) is True

    def test_rc_nonzero_no_signature_returns_false(self):
        """rc=1 + 'No signature found' -> False."""
        assert b.parse_signtool_result(1, SIGNTOOL_NO_SIGNATURE_OUTPUT) is False

    def test_rc_nonzero_unrecognized_format_returns_false(self):
        """rc=1 + unrecognized format error -> False."""
        assert b.parse_signtool_result(1, SIGNTOOL_UNRECOGNIZED_OUTPUT) is False

    def test_rc_nonzero_empty_output_returns_false(self):
        """rc=1 with no output -> False."""
        assert b.parse_signtool_result(1, "") is False

    def test_rc_zero_without_verified_returns_false(self):
        """rc=0 but 'Successfully verified' absent -> False (defensive)."""
        assert b.parse_signtool_result(0, "Number of errors: 0\n") is False

    def test_rc_zero_verified_in_output_is_sufficient(self):
        """Minimal output: rc=0 and 'Successfully verified' in any position -> True."""
        minimal = "Successfully verified: C:\\foo.exe\n"
        assert b.parse_signtool_result(0, minimal) is True


class TestParseCmdletStatus:
    """Tests for parse_cmdlet_status(stdout) -> bool (pure, no process I/O)."""

    def test_valid_returns_true(self):
        """'Valid' status -> True."""
        assert b.parse_cmdlet_status(CMDLET_VALID) is True

    def test_not_signed_returns_false(self):
        """'NotSigned' status -> False."""
        assert b.parse_cmdlet_status(CMDLET_NOT_SIGNED) is False

    def test_unknown_error_returns_false(self):
        """'UnknownError' status -> False."""
        assert b.parse_cmdlet_status(CMDLET_UNKNOWN_ERROR) is False

    def test_hash_mismatch_returns_false(self):
        """'HashMismatch' status (tampered file) -> False."""
        assert b.parse_cmdlet_status(CMDLET_HASH_MISMATCH) is False

    def test_empty_returns_false(self):
        """Empty or whitespace-only output -> False (defensive)."""
        assert b.parse_cmdlet_status("") is False
        assert b.parse_cmdlet_status("   \n") is False

    def test_valid_with_whitespace_returns_true(self):
        """'Valid' with surrounding whitespace -> True (strip before compare)."""
        assert b.parse_cmdlet_status("  Valid  \n") is True


@pytest.mark.skipif(sys.platform != "win32", reason="Authenticode requires native Windows tools")
class TestVerifySignature:
    """Integration tests for verify_signature(path) -> SignatureResult.

    These tests call the real signtool or cmdlet on real files. They are marked
    with 'signature' so they can be run in isolation:
        uv run pytest tests/test_build_release_lib.py -q -k signature
    """

    def test_returns_signature_result_type(self, tmp_path):
        """verify_signature always returns a SignatureResult (even on unsigned files)."""
        f = tmp_path / "dummy.exe"
        f.write_bytes(b"\x4d\x5a")  # Minimal MZ bytes (not a real PE)
        result = b.verify_signature(f)
        assert hasattr(result, "trusted")
        assert hasattr(result, "tool")
        assert hasattr(result, "detail")

    def test_trusted_is_bool(self, tmp_path):
        """The 'trusted' field must be a Python bool."""
        f = tmp_path / "dummy.exe"
        f.write_bytes(b"\x4d\x5a")
        result = b.verify_signature(f)
        assert isinstance(result.trusted, bool)

    def test_tool_is_nonempty_string(self, tmp_path):
        """The 'tool' field must be a non-empty string identifying which tool was used."""
        f = tmp_path / "dummy.exe"
        f.write_bytes(b"\x4d\x5a")
        result = b.verify_signature(f)
        assert isinstance(result.tool, str)
        assert result.tool.strip() != ""

    def test_unsigned_file_is_not_trusted(self, tmp_path):
        """An unsigned binary must have trusted=False."""
        # Create a minimal PE-like file (not actually signed)
        f = tmp_path / "unsigned.exe"
        f.write_bytes(b"\x4d\x5a" + b"\x00" * 62)  # MZ stub
        result = b.verify_signature(f)
        assert result.trusted is False

    def test_no_em_dash_in_detail(self, tmp_path):
        """The 'detail' field must not contain an em dash (project style rule)."""
        f = tmp_path / "dummy.exe"
        f.write_bytes(b"\x4d\x5a")
        result = b.verify_signature(f)
        assert "—" not in result.detail, (
            f"Em dash found in detail: {result.detail!r}"
        )


class TestUpdaterProviderGate:
    def test_signed_builds_require_every_updater_hook(self, monkeypatch):
        complete = {name: "set" for name in b.UPDATER_HOOK_ENVS}
        b.require_updater_provider(complete)
        for name in b.UPDATER_HOOK_ENVS:
            with pytest.raises(b.LegalReleaseGateError, match=name):
                b.require_updater_provider({**complete, name: ""})

    def test_tauri_overlay_is_optional_and_must_be_a_checkout_file(self, tmp_path):
        assert b.tauri_overlay_args(tmp_path, {}) == []
        assert b.tauri_overlay_args(tmp_path, {b.TAURI_OVERLAY_ENV: ""}) == []
        overlay = tmp_path / "edition" / "tauri.json"
        overlay.parent.mkdir()
        overlay.write_text("{}", encoding="utf-8")
        expected = ["--config", str(overlay.resolve())]
        assert b.tauri_overlay_args(tmp_path, {b.TAURI_OVERLAY_ENV: "edition/tauri.json"}) == expected
        assert b.tauri_overlay_args(tmp_path, {b.TAURI_OVERLAY_ENV: str(overlay)}) == expected
        with pytest.raises(FileNotFoundError, match=b.TAURI_OVERLAY_ENV):
            b.tauri_overlay_args(tmp_path, {b.TAURI_OVERLAY_ENV: "edition/missing.json"})
        outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
        outside.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="inside the checkout"):
            b.tauri_overlay_args(tmp_path, {b.TAURI_OVERLAY_ENV: str(outside)})

    def test_policy_is_a_checkout_relative_release_document(self, monkeypatch, tmp_path):
        assert b.updater_policy_relative(tmp_path) == FAKE_UPDATER_POLICY
        monkeypatch.setenv(b.UPDATER_POLICY_ENV, str(tmp_path / FAKE_UPDATER_POLICY))
        assert b.updater_policy_relative(tmp_path) == FAKE_UPDATER_POLICY
        monkeypatch.setenv(b.UPDATER_POLICY_ENV, str(tmp_path.parent / "elsewhere.json"))
        with pytest.raises(b.LegalReleaseGateError, match="inside the checkout"):
            b.updater_policy_relative(tmp_path)
        monkeypatch.delenv(b.UPDATER_POLICY_ENV)
        with pytest.raises(b.LegalReleaseGateError, match="updater policy"):
            b.validate_updater_release_policy(tmp_path)


# ---------------------------------------------------------------------------
# update_download_base: the feed comes from the build's Tauri overlay
# ---------------------------------------------------------------------------


def _overlay(tmp_path, endpoints) -> dict[str, str]:
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"plugins": {"updater": {"endpoints": endpoints}}}), encoding="utf-8")
    return {b.TAURI_OVERLAY_ENV: str(overlay)}


def test_download_base_is_the_overlay_endpoint_directory(tmp_path):
    env = _overlay(tmp_path, ["https://updates.example.invalid/feed/latest.json"])
    assert b.update_download_base(tmp_path, env) == SAMPLE_BASE


def test_download_base_requires_the_overlay(tmp_path):
    with pytest.raises(b.LegalReleaseGateError, match="overlay"):
        b.update_download_base(tmp_path, {})


@pytest.mark.parametrize("endpoints", [
    [],
    ["https://a.example.invalid/latest.json", "https://b.example.invalid/latest.json"],
    ["http://updates.example.invalid/latest.json"],
    ["https://updates.example.invalid/feed.json"],
    ["https://latest.json"],
])
def test_download_base_refuses_an_unexpected_endpoint(tmp_path, endpoints):
    with pytest.raises(b.LegalReleaseGateError):
        b.update_download_base(tmp_path, _overlay(tmp_path, endpoints))


def test_shared_build_library_names_no_update_host():
    source = (Path(b.__file__)).read_text(encoding="utf-8")
    assert "releases.whisperstream" not in source
    assert "upload_to_r2" not in source and "purge_cloudflare" not in source
