"""Derive src-tauri/resources/PRIVACY.txt from legal/PRIVACY.md.

Output constraints:
- BOM-less UTF-8 (ASCII-only content means UTF-8 == ASCII)
- CRLF line endings
- ASCII only
- <= 80 columns (we target 68 for readability in NSIS license pane)

Not invoked by build.sh; run manually after editing PRIVACY.md.
"""

from __future__ import annotations

import re
import sys
import textwrap
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "legal" / "PRIVACY.md"
DST = REPO / "src-tauri" / "resources" / "PRIVACY.txt"

WIDTH = 68

ASCII_REPLACEMENTS = {
    "\u2014": "--",  # em dash
    "\u2013": "-",   # en dash
    "\u2212": "-",   # minus sign
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2026": "...",
    "\u00a0": " ",   # non-breaking space
    "\u00e9": "e",   # e-acute (exigé)
    "\u00e8": "e",
    "\u00ea": "e",
    "\u00e0": "a",
    "\u00e2": "a",
    "\u00ee": "i",
    "\u00ef": "i",
    "\u00f4": "o",
    "\u00fb": "u",
    "\u00fc": "u",
    "\u00e7": "c",
    "\u00ab": '"',
    "\u00bb": '"',
    "\u00b7": "*",
    "\u2022": "*",
}


def to_ascii(s: str) -> str:
    for k, v in ASCII_REPLACEMENTS.items():
        s = s.replace(k, v)
    # catch anything else via NFKD normalization
    normalized = unicodedata.normalize("NFKD", s)
    cleaned = "".join(ch for ch in normalized if ord(ch) < 128)
    return cleaned


def strip_md_inline(s: str) -> str:
    """Strip inline markdown: **bold**, *italic*, `code`, [text](url)."""
    # Bold/italic: **x** or __x__ or *x* or _x_
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", s)
    s = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", s)
    # Inline code: `code` -> code
    s = re.sub(r"`([^`]+)`", r"\1", s)
    # Markdown links: [text](url) -> text (url) when url is http(s), else text
    def _link(m: re.Match) -> str:
        text, url = m.group(1), m.group(2)
        if url.startswith(("http://", "https://")):
            return f"{text} ({url})"
        return text
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, s)
    return s


def wrap_paragraph(text: str, indent: str = "") -> list[str]:
    if not text.strip():
        return [""]
    lines = textwrap.wrap(
        text,
        width=WIDTH,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return lines or [indent]


def convert_heading(line: str) -> list[str]:
    """Convert markdown heading to UPPERCASE with ===== underline."""
    m = re.match(r"^(#{1,6})\s+(.*)$", line)
    if not m:
        return [line]
    level, title = len(m.group(1)), m.group(2).strip()
    title = strip_md_inline(title)
    title = to_ascii(title)
    if level == 1:
        up = title.upper()
        return [up, "=" * len(up)]
    if level == 2:
        up = title.upper()
        return [up, "=" * len(up)]
    if level == 3:
        return [title, "-" * len(title)]
    return [title]


def convert_md(md: str) -> str:
    # Extract the draft banner (first HTML comment at the top of the file)
    # before stripping comments. If present, emit as the first visible line
    # of the .txt so the installer license page still shows the ship-gate
    # warning.
    banner_text: str | None = None
    m_banner = re.match(r"\s*<!--\s*(.+?)\s*-->", md, flags=re.DOTALL)
    if m_banner:
        inner = to_ascii(m_banner.group(1).strip())
        # Collapse whitespace inside the banner
        banner_text = " ".join(inner.split())

    # Drop HTML comments (e.g. <!-- LEGAL REVIEW REQUIRED ... -->)
    md = re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL)

    lines = md.splitlines()
    out: list[str] = []
    if banner_text:
        out.append(banner_text)
        out.append("")
    i = 0
    n = len(lines)

    # Drop the first draft-banner blockquote (lines starting with ">")
    # Only the FIRST consecutive block of blockquote lines is dropped;
    # nested blockquotes in reviewer notes etc. are preserved but reviewer
    # notes section is dropped below anyway.
    first_blockquote_done = False

    in_reviewer_notes = False

    def flush_paragraph(buf: list[str]) -> None:
        if not buf:
            return
        text = " ".join(buf).strip()
        if not text:
            return
        text = strip_md_inline(text)
        text = to_ascii(text)
        for ln in wrap_paragraph(text):
            out.append(ln)
        out.append("")

    para: list[str] = []
    in_list = False

    while i < n:
        raw = lines[i]
        line = raw.rstrip()

        # Reviewer Notes section drop: stop output when we hit "## Reviewer Notes"
        if re.match(r"^##\s+Reviewer\s+Notes\s*$", line, re.IGNORECASE):
            in_reviewer_notes = True
            flush_paragraph(para); para = []
            break

        # Leading draft blockquote: skip
        if line.startswith(">") and not first_blockquote_done:
            # Consume the whole blockquote block
            while i < n and (lines[i].startswith(">") or lines[i].strip() == ""):
                # allow empty lines inside blockquote
                if lines[i].strip() == "":
                    # peek: if next non-empty line still blockquote, keep consuming
                    j = i + 1
                    while j < n and lines[j].strip() == "":
                        j += 1
                    if j < n and lines[j].startswith(">"):
                        i = j
                        continue
                    else:
                        break
                i += 1
            first_blockquote_done = True
            continue

        # Headings
        if re.match(r"^#{1,6}\s", line):
            flush_paragraph(para); para = []
            if out and out[-1] != "":
                out.append("")
            for h in convert_heading(line):
                out.append(h)
            out.append("")
            i += 1
            in_list = False
            continue

        # Horizontal rule
        if re.match(r"^-{3,}\s*$", line):
            flush_paragraph(para); para = []
            out.append("")
            i += 1
            continue

        # List item
        m_list = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        m_num = re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)
        # Only treat a line as a list item when we are NOT mid-paragraph. Real
        # lists in the source are always preceded by a blank line (which flushes
        # `para`), so `para` is empty at a genuine list start. Without this
        # guard, a paragraph that hard-wraps so a continuation line begins with
        # "N. " (e.g. "...within the meaning of Section\n5. This...") is misread
        # as an ordered-list item and the paragraph is garbled in the .txt.
        if (m_list or m_num) and not para:
            flush_paragraph(para); para = []
            if m_list:
                lead_ws = m_list.group(1)
                item_text = m_list.group(2)
                bullet = "- "
            else:
                lead_ws = m_num.group(1)
                num = m_num.group(2)
                item_text = m_num.group(3)
                bullet = f"{num}. "

            # Collect continuation lines (indented more than bullet)
            cont: list[str] = [item_text]
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "":
                    # Peek ahead: if next non-empty line is another list item
                    # at the same or deeper level, continue the list.
                    j = i + 1
                    while j < n and lines[j].strip() == "":
                        j += 1
                    if j < n and re.match(r"^\s+\S", lines[j]) and not re.match(
                        r"^\s*(?:[-*]|\d+\.)\s", lines[j]
                    ):
                        # Indented continuation of the same list item
                        i = j
                        nxt = lines[i]
                    else:
                        break
                # Continuation line must be indented relative to the bullet
                if nxt.startswith(lead_ws + "  ") or nxt.startswith(lead_ws + "\t"):
                    cont.append(nxt.strip())
                    i += 1
                else:
                    break

            text = " ".join(cont).strip()
            text = strip_md_inline(text)
            text = to_ascii(text)
            indent = lead_ws + " " * len(bullet)
            # First line uses bullet; subsequent lines use indent
            first_line = textwrap.wrap(
                text,
                width=WIDTH,
                initial_indent=lead_ws + bullet,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
            out.extend(first_line or [lead_ws + bullet])
            in_list = True
            continue

        # Blank line
        if line.strip() == "":
            flush_paragraph(para); para = []
            if out and out[-1] != "":
                out.append("")
            i += 1
            in_list = False
            continue

        # Regular paragraph line
        para.append(line.strip())
        i += 1

    flush_paragraph(para)

    # Collapse multiple blank lines
    cleaned: list[str] = []
    prev_blank = False
    for ln in out:
        if ln == "":
            if not prev_blank:
                cleaned.append(ln)
            prev_blank = True
        else:
            cleaned.append(ln)
            prev_blank = False

    # Trim trailing blank lines
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return "\r\n".join(cleaned) + "\r\n"


def main() -> int:
    md = SRC.read_text(encoding="utf-8")
    txt = convert_md(md)

    # Strict ASCII check
    for i, ln in enumerate(txt.split("\r\n"), 1):
        for c in ln:
            if ord(c) >= 128:
                print(f"Non-ASCII at line {i}: {c!r} (U+{ord(c):04X})", file=sys.stderr)
                return 1
        if len(ln) > 80:
            print(f"Line {i} > 80 cols ({len(ln)}): {ln[:60]}...", file=sys.stderr)
            return 1

    # Write without BOM
    DST.write_bytes(txt.encode("ascii"))
    n_lines = txt.count("\r\n")
    n_bytes = len(txt.encode("ascii"))
    print(f"Wrote {DST}: {n_bytes} bytes, {n_lines} CRLF lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
