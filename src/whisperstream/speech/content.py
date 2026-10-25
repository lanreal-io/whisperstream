"""Local, bounded speech selection. Transcript content must never be logged."""

import math
import re

from whisperstream.agent_events import extract_spoken_summary

MAX_RESPONSE_CHARS = 32_000


def readable_response(value: object) -> str | None:
    """Normalize a complete response, never a truncated short-looking prefix."""
    if not isinstance(value, str) or len(value) > MAX_RESPONSE_CHARS:
        return None
    lines = value.rstrip().splitlines()
    if extract_spoken_summary(value):
        lines.pop()
    # Fenced code, including an unfinished fence, is never read aloud.
    prose = []
    fence = None
    for line in lines:
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            continue
        if fence is None and not line.startswith(("    ", "\t")):
            prose.append(line)
    text = "\n".join(prose)
    text = re.sub(r"!?\[([^\]]*)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"(?:https?://|www\.)\S+", "", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)", "", text)
    text = re.sub(r"(`+).*?\1", "", text)
    text = re.sub(r"[*_~|]", "", text)
    return " ".join(text.split()) or None


def select_speech_content(result: dict, config: object) -> str | None:
    summary = result.get("spoken_summary")
    summary = summary.strip() if isinstance(summary, str) else None
    policy = getattr(config, "agent_speech_content", "summary")
    if policy not in {"full", "short"}:
        return summary or None
    full = readable_response(result.get("text"))
    if not full:
        return summary or None
    if policy == "full":
        return full
    try:
        speed = float(getattr(config, "agent_speech_speed", 1.0))
        limit = float(getattr(config, "agent_speech_short_seconds", 20))
        if not math.isfinite(speed) or not math.isfinite(limit):
            return summary or None
    except (TypeError, ValueError):
        return summary or None
    # 150 words/minute, with a character floor for long tokens and unspaced text.
    words = max(len(full.split()), len(full) / 6)
    seconds = words / (2.5 * max(0.5, min(2.0, speed)))
    return full if seconds <= max(5, min(120, limit)) else summary or None
