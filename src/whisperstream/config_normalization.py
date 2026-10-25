"""Pure collection normalization used at the existing config migration points.

Word, offer and saved-model comparison rules intentionally remain distinct.
"""

import unicodedata
from typing import Any, Dict, List


def normalize_word_list(raw: Any, *, preserve_case: bool = False) -> List[str]:
    if not raw:
        return []
    seen: set[str] = set()
    result: List[str] = []
    for item in raw:
        word = str(item).strip()
        if not preserve_case:
            word = word.lower()
        # Match Dictionary's comparison key without changing the
        # saved spelling. Filters retain their prior lower-case key.
        key = " ".join(unicodedata.normalize("NFKC", word).split()).lower() if preserve_case else word
        if not word or key in seen:
            continue
        seen.add(key)
        result.append(word)
    return result

def normalize_offer_ids(raw: Any) -> List[str]:
    # Drop non-strings and blanks, trim, dedup preserving order. Offer ids
    # are case-sensitive (unlike word lists), so do NOT lowercase.
    if not raw:
        return []
    seen: set[str] = set()
    result: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        oid = item.strip()
        if not oid or oid in seen:
            continue
        seen.add(oid)
        result.append(oid)
    return result

def normalize_saved_models(data: Dict[str, Any]) -> Dict[str, List[str]]:
    raw_models = data.get("enhancement_saved_models", {})
    saved_models: Dict[str, List[str]] = {}
    for provider in ("gemini", "openai", "anthropic", "custom"):
        rows = raw_models.get(provider, []) if isinstance(raw_models, dict) else []
        if "enhancement_saved_models" not in data:
            rows = [data.get(f"enhancement_{provider}_model", "")]
        saved_models[provider] = list(dict.fromkeys(
            item.strip() for item in rows
            if isinstance(item, str) and 0 < len(item.strip()) <= 128
            and not any(ord(char) < 32 for char in item)
        )) if isinstance(rows, list) else []
    return saved_models
