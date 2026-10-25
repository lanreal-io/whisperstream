"""Stream-1 Mini license files stay pinned to the artifact the app downloads."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from whisperstream.model_sources import MODEL_SOURCES

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "stream-1-mini"


def _provenance() -> dict:
    return json.loads((VENDOR / "provenance.json").read_text(encoding="utf-8"))


def test_provenance_matches_the_shipped_model_source():
    provenance = _provenance()
    source = MODEL_SOURCES["stream-1-mini"]
    assert provenance["repository"] == source["repo"]
    assert provenance["revision"] == source["revision"]
    assert list(source["files"]) == [provenance["file"]]
    pinned = source["files"][provenance["file"]]
    assert provenance["sha256"] == pinned["sha256"]
    assert provenance["size_bytes"] == pinned["size"]


def test_vendored_license_and_notice_match_recorded_bytes():
    provenance = _provenance()
    for name, key in (("LICENSE.txt", "license_sha256"), ("NOTICE.txt", "notice_sha256")):
        digest = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
        assert digest == provenance[key], name


def test_third_party_notices_name_the_license_and_shipped_artifact():
    provenance = _provenance()
    for path in (ROOT / "THIRD_PARTY_LICENSES.txt", ROOT / "src-tauri/resources/THIRD_PARTY_LICENSES.txt"):
        text = path.read_text(encoding="utf-8")
        assert "Stream-1 Mini License, Version 1.0" in text
        assert '"Stream-1 Mini" by\n"Whisperstream"' in text
        assert f"Revision  : {provenance['revision']}" in text
        assert f"Artifact  : {provenance['file']} ({provenance['size_bytes']} bytes)" in text
        assert f"SHA-256   : {provenance['sha256']}" in text
