"""Keep public provenance tied to the active model and generated notices."""
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import generate_licenses as licenses


def test_qwen_evidence_matches_active_pin_and_bundled_notices():
    pin = runpy.run_path(str(ROOT / "src/whisperstream/model_sources.py"))["MODEL_SOURCES"]["qwen3-asr-0.6b-int8"]
    text, rows = licenses.TxtBuilder(), licenses.Rows()
    licenses._section7_bundled(text, rows)
    output = text.text()
    start, end = "Component : Qwen3-ASR-0.6B", "Component : Cohere Transcribe"
    notice = output[output.index(start):output.index(end)]
    assert "Export license  : Apache-2.0" in notice
    assert "Wasser1462/Qwen3-ASR-onnx" in notice
    assert "Modification notice:" in notice
    assert licenses.APACHE_FILE.read_text(encoding="utf-8") in notice
    assert "issuecomment-6162677694" not in notice
    assert "No independent reproduction is claimed." not in notice
    assert "UNVERIFIED" not in notice
    assert any(row[1] == "Qwen3-ASR-0.6B" and row[3] == "Apache-2.0" and row[5] == "-" for row in rows.all_rows())
    for path in (licenses.OUT_TXT, licenses.OUT_STAGED):
        committed = path.read_text(encoding="utf-8")
        assert committed[committed.index(start):committed.index(end)] == notice
    evidence = (ROOT / "docs/qwen-provenance.md").read_text(encoding="utf-8")
    assert pin["revision"] in evidence
    assert "issuecomment-6162677694" in evidence
    assert "has not independently reproduced" in evidence
    for name, artifact in pin["files"].items():
        assert name in evidence
        assert str(artifact["size"]) in evidence
        assert artifact["sha256"] in evidence
    assert pin["github"]["sha256"] in evidence
