"""Generate frontend/public/hipaa-summary.pdf from the approved current source.

Source of truth: docs/security/lockdown-hipaa-summary.md.
The public Markdown source contains only the printable document.

Reproducible: re-run this script any time the source markdown changes
and re-commit the regenerated frontend/public/hipaa-summary.pdf.

    uv run python scripts/gen-hipaa-summary-pdf.py

Output is served at runtime as a static asset via Vite's `public/` copy
convention: reachable at "/hipaa-summary.pdf" in both `npm run dev` and
the built Tauri app (frontend/dist is Tauri's frontendDist).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "docs" / "security" / "lockdown-hipaa-summary.md"
DST = REPO / "frontend" / "public" / "hipaa-summary.pdf"

TITLE_MARKER = "## Lockdown Mode and HIPAA"
FOOTER_HEADING = "Footer"


def extract_body(md: str) -> tuple[str, list[str]]:
    """Pull the printed-page content out of the public Markdown source.

    Everything from the title through the end of the file goes on the page.
    """
    lines = md.splitlines()

    title_idx = next((i for i, l in enumerate(lines) if l.strip() == TITLE_MARKER), None)
    if title_idx is None:
        raise SystemExit(f"Could not find title heading {TITLE_MARKER!r} in {SRC}")

    title = TITLE_MARKER.removeprefix("## ").strip()
    body_lines = lines[title_idx + 1 :]

    # Trim trailing blank lines or a trailing Markdown rule.
    while body_lines and body_lines[-1].strip() in ("", "---"):
        body_lines.pop()

    return title, body_lines


def parse_blocks(body_lines: list[str]) -> list[tuple[str, object]]:
    """Parse the body into ('h2', text) / ('p', text) / ('bullets', [text, ...])."""
    blocks: list[tuple[str, object]] = []
    para_buf: list[str] = []
    bullet_buf: list[str] = []

    def flush_para() -> None:
        if para_buf:
            text = " ".join(para_buf).strip()
            if text:
                blocks.append(("p", text))
            para_buf.clear()

    def flush_bullets() -> None:
        if bullet_buf:
            blocks.append(("bullets", list(bullet_buf)))
            bullet_buf.clear()

    for raw in body_lines:
        line = raw.rstrip()

        if line.strip() == "":
            flush_para()
            flush_bullets()
            continue

        m_h3 = re.match(r"^###\s+(.*)$", line)
        if m_h3:
            flush_para()
            flush_bullets()
            blocks.append(("h2", m_h3.group(1).strip()))
            continue

        m_bullet = re.match(r"^-\s+(.*)$", line)
        if m_bullet:
            flush_para()
            bullet_buf.append(m_bullet.group(1).strip())
            continue

        # Regular paragraph line (continuation of the current paragraph).
        flush_bullets()
        para_buf.append(line.strip())

    flush_para()
    flush_bullets()
    return blocks


def to_markup(text: str) -> str:
    """Escape for reportlab's mini-XML and convert **bold** -> <b>bold</b>."""
    escaped = escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)


def assert_no_em_dash(md: str) -> None:
    if any(char in md for char in ("\u2011", "\u2013", "\u2014")):
        raise SystemExit(
            "Approved source contains a non-ASCII dash; use an ASCII hyphen "
            "in the source instead of changing it during generation."
        )


def build_pdf(title: str, blocks: list[tuple[str, object]]) -> None:
    styles = {
        "title": ParagraphStyle(
            "HipaaTitle",
            fontName="Helvetica-Bold",
            fontSize=18.5,
            leading=22,
            spaceAfter=9,
            textColor="#17324d",
        ),
        "h2": ParagraphStyle(
            "HipaaH2",
            fontName="Helvetica-Bold",
            fontSize=11.8,
            leading=14.2,
            spaceBefore=7,
            spaceAfter=3,
            textColor="#17324d",
        ),
        "body": ParagraphStyle(
            "HipaaBody",
            fontName="Helvetica",
            fontSize=9.6,
            leading=12.1,
            spaceAfter=4.5,
        ),
        "bullet": ParagraphStyle(
            "HipaaBullet",
            fontName="Helvetica",
            fontSize=9.6,
            leading=12.1,
            leftIndent=14,
            bulletIndent=3,
            spaceAfter=2.5,
        ),
        "footer": ParagraphStyle(
            "HipaaFooter",
            fontName="Helvetica-Oblique",
            fontSize=7.8,
            leading=9.8,
            spaceBefore=5,
            textColor="#555555",
        ),
    }

    story: list = [Paragraph(escape(title), styles["title"])]
    in_footer = False

    for kind, payload in blocks:
        if kind == "h2":
            heading = str(payload)
            in_footer = heading == FOOTER_HEADING
            if in_footer:
                # The "Footer" heading is an organizational label in the
                # source, not printed page text -- render only its body
                # (Version line + disclaimer) in a small muted style.
                story.append(Spacer(1, 0.1 * inch))
                continue
            story.append(Paragraph(escape(heading), styles["h2"]))
        elif kind == "p":
            style = styles["footer"] if in_footer else styles["body"]
            story.append(Paragraph(to_markup(str(payload)), style))
        elif kind == "bullets":
            for item in payload:  # type: ignore[union-attr]
                story.append(
                    Paragraph(to_markup(item), styles["bullet"], bulletText="-")
                )
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unknown block kind {kind!r}")

    doc = SimpleDocTemplate(
        str(DST),
        pagesize=LETTER,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        pageCompression=0,
        invariant=1,
        title="Lockdown Mode and HIPAA",
        author="Lanreal Technologies Inc.",
        subject="Whisperstream Lockdown Mode HIPAA summary",
    )
    DST.parent.mkdir(parents=True, exist_ok=True)
    doc.build(story)


def main() -> int:
    md = SRC.read_text(encoding="utf-8")
    assert_no_em_dash(md)

    title, body_lines = extract_body(md)
    blocks = parse_blocks(body_lines)
    build_pdf(title, blocks)

    n_bytes = DST.stat().st_size
    print(f"Wrote {DST}: {n_bytes} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
