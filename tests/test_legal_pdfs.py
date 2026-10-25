import hashlib
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pdf_generation_uses_current_text_and_is_reproducible(tmp_path):
    generate = runpy.run_path(str(ROOT / "scripts/generate-legal-pdfs.py"))["generate"]
    source = tmp_path / "policy.txt"
    output = tmp_path / "policy.pdf"
    source.write_text("Policy\n======\n\nCurrent terms.", encoding="utf-8")
    generate(source, output, "Policy")
    first = output.read_bytes()
    assert first.startswith(b"%PDF-")
    assert hashlib.sha256(source.read_bytes()).hexdigest().encode() in first
    generate(source, output, "Policy")
    assert output.read_bytes() == first
    source.write_text("Policy\n======\n\nRevised terms.", encoding="utf-8")
    generate(source, output, "Policy")
    assert output.read_bytes() != first
    assert hashlib.sha256(source.read_bytes()).hexdigest().encode() in output.read_bytes()
