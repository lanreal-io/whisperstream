"""Render the current packaged legal text into offline frontend PDF assets."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = {"EULA": "License agreement", "PRIVACY": "Privacy policy"}


def generate(source: Path, output: Path, title: str) -> None:
    raw = source.read_bytes()
    text = raw.decode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=8)
    heading = ParagraphStyle("heading", parent=body, fontName="Helvetica-Bold", fontSize=12, leading=16, spaceBefore=12, keepWithNext=True)
    story = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        is_heading = len(lines) > 1 and bool(re.fullmatch(r"[=\-]+", lines[-1]))
        if is_heading:
            lines.pop()
        paragraph = " ".join(lines)
        story.append(Paragraph(escape(paragraph), heading if is_heading else body))
    story.append(Spacer(1, 4))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#607080"))
        canvas.drawString(48, 28, title)
        canvas.drawRightString(A4[0] - 48, 28, str(doc.page))
        canvas.restoreState()

    SimpleDocTemplate(
        str(output), pagesize=A4, rightMargin=48, leftMargin=48,
        topMargin=42, bottomMargin=46, title=title, author="Whisperstream",
        subject=f"Source SHA256: {hashlib.sha256(raw).hexdigest()}",
        invariant=1,
    ).build(story, onFirstPage=footer, onLaterPages=footer)


if __name__ == "__main__":
    for name, title in DOCUMENTS.items():
        generate(ROOT / "src-tauri" / "resources" / f"{name}.txt",
                 ROOT / "frontend" / "src" / "assets" / "legal" / f"{name}.pdf", title)
    print("Generated offline legal PDFs from packaged text.")
