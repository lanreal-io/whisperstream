"""Generated-output pins must match the committed outputs they describe.

Public's source policy accepts these generated files only at the hashes in
scripts/generated-assets.json. This catches a regenerated output whose pin was
not updated in the same change, before publication.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_generated_asset_pins_match_committed_outputs():
    pins = json.loads((ROOT / "scripts/generated-assets.json").read_text(encoding="utf-8"))
    assert pins["version"] == 1
    paths = [row["path"] for row in pins["assets"]]
    assert len(paths) == len(set(paths))
    for row in pins["assets"]:
        data = (ROOT / row["path"]).read_bytes()
        assert data.startswith(b"%PDF-"), row["path"]
        assert hashlib.sha256(data).hexdigest() == row["sha256"], f"Re-pin {row['path']} after regenerating it"
