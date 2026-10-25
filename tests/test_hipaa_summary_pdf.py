"""Canonical-generation checks for the bundled HIPAA summary PDF."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "gen-hipaa-summary-pdf.py"
SOURCE = REPO / "docs" / "security" / "lockdown-hipaa-summary.md"
PDF = REPO / "frontend" / "public" / "hipaa-summary.pdf"

APPROVED_SCOPE = (
    "Lockdown Mode keeps Whisperstream transcription, cleanup, and saved history "
    "on your device. It does not govern applications or services that receive "
    "your text, or other software running on the device. Evaluate those separately "
    "as part of your compliance program."
)
APPROVED_BAA_SCOPE = (
    "For this defined Whisperstream processing path, Lanreal does not receive your "
    "microphone audio, transcript content, or dictated text and does not act as a "
    "business associate. This conclusion does not extend to other applications or "
    "services you use, or support or hosting arrangements that give someone access to "
    "protected health information."
)
APPROVED_RECOVERY = (
    "If you lose both your password and recovery key, your history can't be "
    "restored. Reset deletes it instead."
)

FORBIDDEN_CLAIMS = (
    "runs entirely on your computer",
    "nothing you dictate leaves your machine",
    "nothing leaves your machine",
    "never transmitted off your device",
    "no ePHI in transit",
    "no third party handles your information",
    "blocks all cloud providers",
)
FORBIDDEN_PROVIDER_LIST = (
    "Agent Speak",
    "Claude Code",
    "Hugging Face",
    "Cloudflare",
)


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_hipaa_summary_pdf", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decode_pdf_literal(raw: bytes) -> str:
    """Decode the ASCII/WinAnsi literal strings emitted by ReportLab."""
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] != 0x5C:  # backslash
            out.append(raw[i])
            i += 1
            continue

        i += 1
        if i >= len(raw):
            break
        escaped = raw[i]
        replacements = {
            ord("n"): ord("\n"),
            ord("r"): ord("\r"),
            ord("t"): ord("\t"),
            ord("b"): 0x08,
            ord("f"): 0x0C,
        }
        if escaped in replacements:
            out.append(replacements[escaped])
            i += 1
        elif 0x30 <= escaped <= 0x37:
            end = i + 1
            while end < min(i + 3, len(raw)) and 0x30 <= raw[end] <= 0x37:
                end += 1
            out.append(int(raw[i:end], 8))
            i = end
        elif escaped in (ord("\n"), ord("\r")):
            if escaped == ord("\r") and i + 1 < len(raw) and raw[i + 1] == ord("\n"):
                i += 1
            i += 1
        else:
            out.append(escaped)
            i += 1
    return out.decode("cp1252")


def _extract_pdf_text(pdf: Path) -> str:
    data = pdf.read_bytes()
    assert b"/Filter" not in data, "canonical PDF must keep text streams uncompressed"
    literals = re.findall(rb"\(((?:\\.|[^()\\])*)\)\s*Tj", data)
    return " ".join(_decode_pdf_literal(item) for item in literals)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _printed_source_text(generator) -> str:
    title, body_lines = generator.extract_body(SOURCE.read_text(encoding="utf-8"))
    blocks = generator.parse_blocks(body_lines)
    parts = [title]
    for kind, payload in blocks:
        if kind == "h2":
            if payload != generator.FOOTER_HEADING:
                parts.append(str(payload))
        elif kind == "p":
            parts.append(str(payload))
        elif kind == "bullets":
            parts.extend(f"- {item}" for item in payload)
    return re.sub(r"\*\*(.+?)\*\*", r"\1", " ".join(parts))


def test_current_approved_source_exists_and_is_canonical() -> None:
    generator = _load_generator()

    assert SOURCE.is_file()
    assert generator.SRC == SOURCE


def test_shipped_pdf_is_one_page_and_matches_printed_source_text() -> None:
    generator = _load_generator()
    pdf_bytes = PDF.read_bytes()

    assert len(re.findall(rb"/Type\s*/Page(?!s)\b", pdf_bytes)) == 1
    assert _normalize_text(_extract_pdf_text(PDF)) == _normalize_text(
        _printed_source_text(generator)
    )


def test_generator_is_byte_stable(tmp_path: Path) -> None:
    generator = _load_generator()
    generator.DST = tmp_path / "hipaa-summary.pdf"

    generator.main()
    first = generator.DST.read_bytes()
    generator.main()

    assert generator.DST.read_bytes() == first


def test_approved_scope_baa_and_recovery_meaning_are_verbatim() -> None:
    source_text = SOURCE.read_text(encoding="utf-8")
    pdf_text = _extract_pdf_text(PDF)

    for approved in (APPROVED_SCOPE, APPROVED_BAA_SCOPE, APPROVED_RECOVERY):
        assert approved in source_text
        assert approved in _normalize_text(pdf_text)


def test_removed_whole_device_and_provider_list_claims_stay_removed() -> None:
    pdf_text = _extract_pdf_text(PDF)
    text = f"{SOURCE.read_text(encoding='utf-8')} {pdf_text}".lower()

    for forbidden in (*FORBIDDEN_CLAIMS, *FORBIDDEN_PROVIDER_LIST):
        assert forbidden.lower() not in text

    assert "\u2014" not in text
    assert "\u2013" not in text
    assert "compliance seal" not in pdf_text.lower()
