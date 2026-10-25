"""Verify an official installer without rebuilding or publishing.

Run it with the same official build providers as the build: the legal
attestation binds the updater policy that provider names.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import _build_lib as b
from installer_profile import installer_profile


def verify(root: Path) -> dict:
    revision = b.require_clean_source_revision(root, generated_notices=True)
    with installer_profile(root, "official"):
        version = b.read_tauri_version(root)
        installer, updater_sig = b.find_nsis_installer(version, base_dir=root)
        b.verify_legal_release_attestation(version, installer, updater_sig, root=root)
        signature = b.verify_signature(installer)
        if not signature.trusted:
            raise ValueError("Installer Authenticode signature is not trusted")
        artifacts = [installer, updater_sig,
                     root / "dist/latest.json", root / "dist/legal-release.json"]
        return {
            "schema_version": 2, "profile": "official",
            "version": version, "source_revision": revision,
            "artifacts": [{"path": str(path.resolve()), "sha256": b.file_sha256(path)}
                          for path in artifacts],
        }


if __name__ == "__main__":
    try:
        report = verify(Path(__file__).resolve().parents[1])
    except (ValueError, OSError, b.LegalReleaseGateError) as exc:
        print(f"Installer verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(report, indent=2))
