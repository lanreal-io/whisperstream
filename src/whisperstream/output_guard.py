"""Deterministic hard safeguards for local enhancement output.

Production uses the basic whole-unit checks with structure checking disabled and
the post-repair digit-integrity fallback. The standalone semantic/structure
classifiers remain only for historical evaluation compatibility; they are not
production safeguards. It is
synchronous, standard-library-only, and fail-closed: an unexpected guard error returns
the complete raw enhancer unit. It never repairs, strips, generates, or splices prose.

DO NOT LOG transcript or cleaned content from this module. Guard logs are metadata
only (stable tag and check count).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from .digit_repair import analyze_digit_mismatches
from .text_guards import (
    has_refusal_not_in_input,
    has_sentence_repetition,
    is_length_explosion,
)

logger = logging.getLogger(__name__)

GuardAction = Literal["keep", "fallback"]
CodeFamily = Literal["gemma", "stream"]

_PROMPT_WRAPPER_TAG_RE = re.compile(
    r"<[ \t\r\n]{0,3}(?P<closing>/)?[ \t\r\n]{0,3}"
    r"(?P<name>transcript|input|output)[ \t\r\n]{0,3}>",
    re.IGNORECASE,
)
_BULLET_LIST_MARKER_RE = re.compile(r"(?m)^[ \t]*[-*+\N{BULLET}][ \t]+\S")
_FENCE_RE = re.compile(r"(?m)^\s*(?:```|~~~)")
_MARKDOWN_HEADING_RE = re.compile(
    r"(?m)^[ \t]{0,3}(?P<marker>#{1,6})[ \t]+"
)
_MARKDOWN_HORIZONTAL_RULE_RE = re.compile(
    r"(?m)^[ \t]{0,3}(?P<marker>"
    r"(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,}"
    r")[ \t]*$"
)
_META_PREAMBLES = (
    "sure, here's the cleaned text",
    "sure, here is the cleaned text",
    "here's the cleaned text",
    "here is the cleaned text",
)

_CHAT_TOKEN_RE = re.compile(
    r"\d{1,2}:\d{2}|\d+(?:[.,-]\d+)*|[^\W\d_]+(?:['’][^\W\d_]+)?|[$%]|[^\w\s]",
    re.UNICODE,
)
_REVIEWED_NGL_SURFACE_RE = re.compile(r"\bng l\.(?=$|\s)", re.IGNORECASE)
_NUMBER_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_NUMBER_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_WORD_EQUIVALENTS = {
    "colour": "color", "colours": "colors", "organise": "organize",
    "organised": "organized", "finalise": "finalize",
    "dollar": "currency:usd", "dollars": "currency:usd", "$": "currency:usd",
    "percent": "unit:percent", "%": "unit:percent",
    "kilometre": "unit:km", "kilometres": "unit:km",
    "kilometer": "unit:km", "kilometers": "unit:km", "km": "unit:km",
    "am": "timepart:am", "pm": "timepart:pm",
    "👍": "emoji:thumbs_up",
    "🔥": "emoji:fire",
}
_CONTRACTIONS = {
    "can't": ("can", "not"), "cannot": ("can", "not"),
    "it's": ("it", "is"), "i'm": ("i", "am"), "i'll": ("i", "will"),
}
_CHAT_FILLERS = frozenset({"um", "uh", "er"})
_INLINE_CORRECTION_RE = re.compile(
    r"\b(?:no\s+wait|no\s+make\s+it|scratch\s+that|make\s+that|i\s+mean|or\s+rather)\b",
    re.IGNORECASE,
)
_UNSTRUCTURED_CORRECTION_MARKERS = frozenset({"i mean", "or rather"})
_REVIEWED_RELATIONSHIP_POSSESSIVES = frozenset({"my"})
_REVIEWED_RELATIONSHIP_NOUNS = frozenset({"cousin", "sister"})
_UNSTRUCTURED_ENTITY_SLOT_CUES = frozenset({
    "ask", "at", "by", "call", "called", "contact", "email", "for", "from",
    "invite", "message", "named", "notify", "tell", "to", "use", "with",
})
_UNSTRUCTURED_NON_ENTITY_WORDS = frozenset({
    "a", "am", "an", "and", "are", "as", "ask", "at", "be", "because", "been",
    "being", "but", "by", "call", "called", "can", "contact", "could", "did",
    "do", "does", "email", "for", "from",
    "had", "has", "have", "he", "her", "hers", "him", "his", "how", "i", "if",
    "in", "invite", "is", "it", "its", "make", "made", "makes", "may", "me",
    "mean", "message",
    "means", "might", "mine", "must", "my", "named", "notify", "of", "on", "or",
    "our", "ours",
    "please", "rather", "say", "says", "shall", "she", "should", "than", "that",
    "tell", "the", "their", "theirs", "them", "then", "these", "they", "this", "those",
    "to", "us", "use", "was", "we", "were", "what", "when", "where", "which", "who",
    "whom", "whose", "why", "will", "with", "would", "you", "your", "yours",
})
_SORRY_CORRECTION_RE = re.compile(r"\bsorry\b", re.IGNORECASE)
_SORRY_EXPLICIT_CORRECTION_RE = re.compile(
    r"[\s,()\-]*(?:i\s+mean|make\s+that|or\s+rather)\b",
    re.IGNORECASE,
)
_OPENING_DISCOURSE_SO_RE = re.compile(
    r"^\s*(?:(?P<preface>okay|ok|hey)\s*,?\s+)?(?P<so>so)\b"
    r"(?=[\s,]+(?:um|uh|er|the\s+thing\s+is|here(?:'s|\s+is))\b)",
    re.IGNORECASE,
)
_DENVER_STYLE_CORRECTION_RE = re.compile(
    r"\bactually\s+not\s+(?P<old>[^\W\d_]+)\s*,?\s*(?:the\s+)?",
    re.IGNORECASE,
)
_BLOCK_RETRACTION_RE = re.compile(r"\bactually\s*,?\s*scrap\s+all\s+that\b", re.IGNORECASE)
_CODE_TOKEN_RE = re.compile(
    r"===|==|!=|=|/|\.|@|\d+(?:[.,]\d+)*|[^\W\d_]+(?:['’][^\W\d_]+)?",
    re.UNICODE,
)
_CODE_CORRECTION_RE = re.compile(
    r"\b(?:that\s+should\s+be|no\s+wait|i\s+mean)\b",
    re.IGNORECASE,
)
_CODE_ACRONYMS = frozenset({
    "api", "json", "rest", "http", "url", "sql", "css", "html", "id",
    "ui", "cli", "ide", "ci", "cd", "js",
})
_CASING_WORDS = frozenset({"camel", "pascal", "snake", "kebab"})
_EMAIL_ROUTING_RES = (
    re.compile(r"^\s*(?:email|message|ping)\s+(?P<recipient>[^\W\d_]+)(?:\s+saying)?\b", re.IGNORECASE),
    re.compile(r"^\s*tell\s+(?P<recipient>(?:the\s+)?[^\W\d_]+)\b", re.IGNORECASE),
    re.compile(r"^\s*let\s+(?P<recipient>[^\W\d_]+)\s+know\b", re.IGNORECASE),
    re.compile(r"^\s*(?:send|shoot|drop)\s+(?P<recipient>[^\W\d_]+)\s+(?:a\s+)?(?:note|message|line)\b", re.IGNORECASE),
)
_EMAIL_GREETING_RE = re.compile(
    r"^\s*(?P<word>good\s+(?:morning|afternoon|evening)|hi|hey|hiya|hello|morning|dear)\b",
    re.IGNORECASE,
)
_EMAIL_GREETING_ANY_RE = re.compile(
    r"\b(?P<word>good\s+(?:morning|afternoon|evening)|hi|hey|hiya|hello|morning|dear)\b",
    re.IGNORECASE,
)
_REVIEWED_STUTTER_WORDS = frozenset({"the", "to"})
# Finite, reviewed set of words whose ONLY function is to signal that a
# replacement supersedes a value the speaker retracted ("X, scratch that, Y
# instead"). Once the retracted value is deleted the word has no referent left,
# so a model that drops it has not lost user content. Kept deliberately small:
# every member must be vacuous on its own, never a carrier of meaning.
_RETRACTION_SCAFFOLD_WORDS = frozenset({"instead"})
# A scaffold word only stays vacuous inside the replacement clause. Anything
# past a sentence break belongs to a new statement, where "instead" is ordinary
# content and dropping it IS content loss.
_SENTENCE_BREAK_RE = re.compile(r"[.!?;\n]")
_COMPARATIVE_LIKE_NOUNS = frozenset({
    "design", "document", "email", "example", "file", "format", "layout",
    "message", "option", "photo", "picture", "report", "style", "template", "version",
})
_WEEKDAYS = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})
_MONTHS = frozenset({
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
})
_COLON_LIST_LEAD_RE = re.compile(
    r"^(?:shopping|packing|guest|task|todo|to-do|action|reading)\s+list$"
    r"|^(?:checklist|steps|items|the\s+following)$"
    r"|^here(?:'s|\s+is)\s+(?:the\s+)?(?:plan|agenda)$",
    re.IGNORECASE,
)
_ITEM_FRAGMENT_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
_ITEM_FRAGMENT_DETERMINERS = frozenset({
    "a", "an", "her", "his", "its", "my", "our", "the", "their", "your",
})
_ITEM_FRAGMENT_SUBORDINATORS = frozenset({
    "after", "although", "as", "because", "before", "if", "once", "since",
    "though", "unless", "until", "when", "whenever", "where", "whereas",
    "wherever", "while", "which", "who", "whom", "whose",
})
_ITEM_FRAGMENT_SUBJECT_HEADS = frozenset({
    "he", "i", "it", "she", "that", "they", "this", "those", "we", "what",
    "you",
})
_ITEM_FRAGMENT_DISCOURSE_HEADS = frozenset({
    "but", "however", "not", "otherwise", "rather", "unfortunately",
})


@dataclass(frozen=True, slots=True)
class GuardCheck:
    """One content-free guard outcome."""

    tag: str
    tripped: bool


@dataclass(frozen=True, slots=True)
class GuardResult:
    """Immutable whole-unit decision returned by the guard core."""

    final_text: str
    action: GuardAction
    guard: str | None
    checks: tuple[GuardCheck, ...]


@dataclass(frozen=True, slots=True)
class NormalizedAtom:
    """One comparison atom with lossless offsets into its original text."""

    value: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class GuardContract:
    """Finite prompt/guard contract resolved alongside the inference prompt."""

    name: str
    prompt_family: str
    check_structure: bool = True


def _number_phrase(tokens: list[tuple[str, int, int]], start: int) -> tuple[str, int] | None:
    """Parse one finite spoken-number span into its canonical digit string.

    Multiplier-free spans use the same grammar people dictate for phone numbers,
    years, and clock values: units remain individual digits, while a tens word may
    absorb one following unit. Magnitude spans retain arithmetic composition.
    A hyphen is only a connector when it sits between two number words.
    """
    items: list[tuple[str, int]] = []
    index = start
    while index < len(tokens):
        word = tokens[index][0].casefold()
        if word in _NUMBER_UNITS:
            items.append(("unit", _NUMBER_UNITS[word]))
        elif word in _NUMBER_TENS:
            items.append(("tens", _NUMBER_TENS[word]))
        elif word in _ORDINALS:
            items.append(("ordinal", _ORDINALS[word]))
        elif word in {"hundred", "thousand"} and items:
            items.append(("scale", 100 if word == "hundred" else 1000))
        elif word == "and" and any(kind == "scale" for kind, _ in items) and index + 1 < len(tokens):
            next_word = tokens[index + 1][0].casefold()
            if next_word not in _NUMBER_UNITS and next_word not in _NUMBER_TENS and next_word not in _ORDINALS:
                break
            index += 1
            continue
        elif word == "-" and items and index + 1 < len(tokens):
            next_word = tokens[index + 1][0].casefold()
            if next_word not in _NUMBER_UNITS and next_word not in _NUMBER_TENS and next_word not in _ORDINALS:
                break
            index += 1
            continue
        else:
            break
        index += 1
    if not items:
        return None

    if any(kind == "scale" for kind, _ in items):
        total = current = 0
        item_index = 0
        while item_index < len(items):
            kind, value = items[item_index]
            if kind == "scale":
                if current == 0:
                    return None
                if value == 100:
                    current *= value
                else:
                    total += current * value
                    current = 0
                item_index += 1
                continue
            if kind == "tens" and item_index + 1 < len(items):
                next_kind, next_value = items[item_index + 1]
                if next_kind in {"unit", "ordinal"} and 1 <= next_value <= 9:
                    current += value + next_value
                    item_index += 2
                    continue
            current += value
            item_index += 1
        return str(total + current), index

    groups: list[str] = []
    item_index = 0
    while item_index < len(items):
        kind, value = items[item_index]
        if kind == "tens" and item_index + 1 < len(items):
            next_kind, next_value = items[item_index + 1]
            if next_kind in {"unit", "ordinal"} and 1 <= next_value <= 9:
                groups.append(str(value + next_value))
                item_index += 2
                continue
        groups.append(str(value))
        item_index += 1
    return "".join(groups), index


def normalize_chat_atoms(
    text: str,
    *,
    literal_scale_words: frozenset[str] = frozenset(),
    allow_reviewed_ngl_surface: bool = False,
) -> tuple[NormalizedAtom, ...]:
    """Normalize only the finite, reviewed Chat rendering equivalences."""
    tokens = [(match.group(), match.start(), match.end()) for match in _CHAT_TOKEN_RE.finditer(text)]
    atoms: list[NormalizedAtom] = []
    index = 0
    while index < len(tokens):
        token, start, end = tokens[index]
        folded = token.casefold().replace("’", "'")

        if folded in {"a", "p"} and index + 2 < len(tokens):
            if tokens[index + 1][0] == "." and tokens[index + 2][0].casefold() == "m":
                atoms.append(NormalizedAtom(f"timepart:{folded}m", start, tokens[index + 2][2]))
                index += 3
                continue

        if index + 2 < len(tokens):
            phrase = tuple(item[0].casefold() for item in tokens[index : index + 3])
            if phrase == ("thumbs", "up", "emoji"):
                atoms.append(NormalizedAtom("emoji:thumbs_up", start, tokens[index + 2][2]))
                index += 3
                continue
            if phrase == ("in", "the", "morning"):
                atoms.append(NormalizedAtom("timepart:am", start, tokens[index + 2][2]))
                index += 3
                continue
            if phrase == ("in", "the", "afternoon"):
                atoms.append(NormalizedAtom("timepart:pm", start, tokens[index + 2][2]))
                index += 3
                continue

        if index + 1 < len(tokens):
            phrase = tuple(item[0].casefold() for item in tokens[index : index + 2])
            if allow_reviewed_ngl_surface and phrase == ("ng", "l"):
                if _REVIEWED_NGL_SURFACE_RE.match(text, start) is not None:
                    atoms.append(NormalizedAtom("ngl", start, tokens[index + 1][2]))
                    index += 2
                    continue
            if phrase == ("thumbs", "up"):
                atoms.append(NormalizedAtom("emoji:thumbs_up", start, tokens[index + 1][2]))
                index += 2
                continue
            if phrase == ("fire", "emoji"):
                atoms.append(NormalizedAtom("emoji:fire", start, tokens[index + 1][2]))
                index += 2
                continue

        if folded == "$" and index + 1 < len(tokens):
            numeric = tokens[index + 1][0].replace(",", "")
            if re.fullmatch(r"\d+(?:\.\d+)?", numeric):
                atoms.append(NormalizedAtom(f"num:{numeric}", start, tokens[index + 1][2]))
                atoms.append(NormalizedAtom("currency:usd", start, tokens[index + 1][2]))
                index += 2
                continue

        if len(folded) == 1 and folded.isalpha():
            last = index
            letters: list[str] = []
            while last < len(tokens) and len(tokens[last][0]) == 1 and tokens[last][0].isalpha():
                letters.append(tokens[last][0].casefold())
                last += 1
            joined = "".join(letters)
            if joined == "lmk":
                atoms.append(NormalizedAtom(joined, start, tokens[last - 1][2]))
                index = last
                continue

        if folded in _NUMBER_UNITS and 1 <= _NUMBER_UNITS[folded] <= 12 and index + 2 < len(tokens):
            minute_word = tokens[index + 1][0].casefold()
            timepart = tokens[index + 2][0].casefold()
            minute = _NUMBER_TENS.get(minute_word, _NUMBER_UNITS.get(minute_word))
            if minute is not None and timepart in {"am", "pm"}:
                atoms.append(NormalizedAtom(
                    f"time:{_NUMBER_UNITS[folded]}:{minute:02d}", start, tokens[index + 1][2]
                ))
                index += 2
                continue

        number = _number_phrase(tokens, index)
        if number is not None:
            value, last = number
            terminal_scale = tokens[last - 1][0].casefold()
            if terminal_scale in literal_scale_words and last - index > 1:
                prefix = tokens[index : last - 1]
                coefficient = _number_phrase(prefix, 0)
                if coefficient is not None and coefficient[1] == len(prefix):
                    atoms.append(NormalizedAtom(f"num:{coefficient[0]}", start, prefix[-1][2]))
                    atoms.append(NormalizedAtom(terminal_scale, tokens[last - 1][1], tokens[last - 1][2]))
                    index = last
                    continue
            atoms.append(NormalizedAtom(f"num:{value}", start, tokens[last - 1][2]))
            index = last
            continue

        if re.fullmatch(r"\d{1,2}:\d{2}", folded):
            hour, minute = folded.split(":")
            value = f"num:{int(hour)}" if minute == "00" else f"time:{int(hour)}:{minute}"
        elif re.fullmatch(r"\d+(?:[.,-]\d+)*", folded):
            value = f"num:{folded.replace(',', '').replace('-', '')}"
        elif folded in _CONTRACTIONS:
            atoms.extend(NormalizedAtom(value, start, end) for value in _CONTRACTIONS[folded])
            index += 1
            continue
        else:
            value = _WORD_EQUIVALENTS.get(folded, folded)

        if value.isalnum() or ":" in value or value in {"colors", "organized"}:
            atoms.append(NormalizedAtom(value, start, end))
        index += 1
    return tuple(atoms)


def _chat_alignment(raw_atoms: tuple[NormalizedAtom, ...], cleaned_atoms: tuple[NormalizedAtom, ...]):
    matcher = SequenceMatcher(
        a=[atom.value for atom in raw_atoms],
        b=[atom.value for atom in cleaned_atoms],
        autojunk=False,
    )
    pairs: list[tuple[int, int]] = []
    for block in matcher.get_matching_blocks():
        pairs.extend((block.a + offset, block.b + offset) for offset in range(block.size))
    return pairs


_SOURCE_LIST_CUE_RE = re.compile(
    r"\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)(?:ly)?\b"
    r"|\bnumber\s+(?:one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)
_SOURCE_ORDINAL_LIST_CUE_RE = re.compile(
    r"\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)(?:ly)?\b",
    re.IGNORECASE,
)
_LINE_NUMBER_MARKER_RE = re.compile(
    r"(?m)^[ \t]*(?P<number>\d{1,3})"
    r"(?P<separator>[.):][ \t]+|[ \t]*-[ \t]+)\S"
)
_NUMBERED_ITEM_LINE_RE = re.compile(
    r"^[ \t]*(?P<number>\d{1,3})"
    r"(?P<separator>[.):][ \t]+|[ \t]*-[ \t]+)"
    r"(?P<content>\S.*)$"
)
_LIST_ORDINAL_VALUES = {
    **{word: value for word, value in _ORDINALS.items()},
    **{f"{word}ly": value for word, value in _ORDINALS.items()},
}
_LIST_NUMBER_VALUES = {
    word: value for word, value in _NUMBER_UNITS.items() if 1 <= value <= 10
}


def _structural_number_markers(text: str) -> list[re.Match[str]]:
    matches = list(_LINE_NUMBER_MARKER_RE.finditer(text))
    if len(matches) == 1 and matches[0].group("separator").strip() == "-":
        return []
    return matches


def _has_inline_colon_list_rendering(text: str) -> bool:
    """Recognize a finite inline rendering without broadening source list proof."""
    if "\n" in text or "\r" in text:
        return False
    for colon_match in re.finditer(":", text):
        colon = colon_match.start()
        prefix = text[:colon].rstrip()
        lead_clause = re.split(r"[.!?;\n]", prefix)[-1].strip()
        if not _COLON_LIST_LEAD_RE.fullmatch(lead_clause):
            continue
        body = text[colon + 1 :].strip()
        if body.endswith((".", "?", "!")):
            body = body[:-1].rstrip()
        if not body or ";" in body or re.search(r"[.?!]", body):
            continue
        fragments = [fragment.strip() for fragment in body.split(",")]
        if len(fragments) < 2 or any(not fragment for fragment in fragments):
            continue
        fragment_words = [_colon_fragment_words(fragment) for fragment in fragments]
        if all(
            1 <= len(words) <= 8 and _has_lexical_colon_fragment_head(words)
            for words in fragment_words
        ):
            return True
    return False


def _list_presentation_atom_indices(
    raw: str,
    cleaned: str,
    raw_atoms: tuple[NormalizedAtom, ...],
    cleaned_atoms: tuple[NormalizedAtom, ...],
) -> tuple[set[int], set[int]]:
    """Return finite source/output atoms that are list presentation.

    This equivalence is available only when the source has an owner-locked list cue
    and the output either renders a numbered list or a proven inline colon list.
    The inline form makes only the reviewed ordinal cues optional; all item and
    lead-in atoms remain in the normal content comparison, and Stage 1 remains the
    sole authority for introduced structure. Ordinary numeric atoms remain lexical
    content everywhere else.
    """
    if not _has_ordered_list_cue(raw):
        return set(), set()
    marker_matches = _structural_number_markers(cleaned)
    inline_list_is_proven = _has_inline_colon_list_rendering(cleaned)
    if not marker_matches and not inline_list_is_proven:
        return set(), set()

    raw_cue_re = _SOURCE_LIST_CUE_RE if marker_matches else _SOURCE_ORDINAL_LIST_CUE_RE
    raw_ranges = [(match.start(), match.end()) for match in raw_cue_re.finditer(raw)]
    cleaned_ranges = [
        (match.start("number"), match.end("separator")) for match in marker_matches
    ]
    raw_indices = {
        index
        for index, atom in enumerate(raw_atoms)
        if any(atom.start < end and atom.end > start for start, end in raw_ranges)
    }
    cleaned_indices = {
        index
        for index, atom in enumerate(cleaned_atoms)
        if any(atom.start < end and atom.end > start for start, end in cleaned_ranges)
    }
    return raw_indices, cleaned_indices


def _bounded_replacement_target(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
    marker: re.Match[str],
) -> int | None:
    """Return an immediately-following target in the marker's clause, if any."""
    boundary = re.search(r"[.!?;\n]", raw[marker.end() :])
    region_end = marker.end() + boundary.start() if boundary else len(raw)
    for index, atom in enumerate(atoms):
        if atom.start < marker.end():
            continue
        if atom.start >= region_end or atom.start - marker.end() > 40:
            return None
        between = raw[marker.end() : atom.start]
        if not re.fullmatch(r"[\s,()\-]*", between):
            return None
        if atom.value in {"but", "for", "about", "that", "because"}:
            return None
        return index
    return None


def _bounded_atom_after(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
    start: int,
) -> int | None:
    """Return the first atom after ``start`` within the current correction clause."""
    boundary = re.search(r"[.!?;\n]", raw[start:])
    region_end = start + boundary.start() if boundary else len(raw)
    for index, atom in enumerate(atoms):
        if atom.start < start:
            continue
        if atom.start >= region_end or atom.start - start > 40:
            return None
        between = raw[start:atom.start]
        if not re.fullmatch(r"[\s,()\-]*", between):
            return None
        return index
    return None


def _replacement_slot(atom: NormalizedAtom) -> str | None:
    """Classify finite value kinds that provide evidence for bare ``sorry`` correction."""
    if atom.value.startswith("num:"):
        return "number"
    if atom.value.startswith(("time:", "timepart:")) or atom.value in {"noon", "midnight"}:
        return "time"
    if atom.value in _WEEKDAYS:
        return "weekday"
    if atom.value in _MONTHS:
        return "month"
    return None


def _structured_value_label(atom: NormalizedAtom) -> str | None:
    slot = _replacement_slot(atom)
    if slot is not None:
        return slot
    if atom.value == "at":
        return "connector:at"
    return None


def _structured_signature_family(signature: tuple[str, ...]) -> str | None:
    if "month" in signature:
        return "date"
    if "weekday" in signature:
        return "weekday"
    if "time" in signature or "connector:at" in signature:
        return "time"
    if signature == ("number",):
        return "number"
    return None


def _structured_signatures_are_compatible(
    old_signature: tuple[str, ...],
    target_signature: tuple[str, ...],
) -> bool:
    if old_signature == target_signature:
        return True
    if old_signature == ("number",) and target_signature == ("number", "time"):
        return True
    old_family = _structured_signature_family(old_signature)
    target_family = _structured_signature_family(target_signature)
    if old_family == "date":
        return target_signature[0] == "month" and target_family == "date"
    if old_family == "weekday":
        return target_signature[0] == "weekday" and target_family == "weekday"
    if old_family == "time":
        return target_family == "time" or target_signature == ("number",)
    return False


def _finite_correction_value_spans(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
    *,
    marker_start: int,
    marker_end: int,
    optional: set[int],
) -> tuple[set[int], set[int]] | None:
    """Return adjacent, signature-matched date/time/value spans around a marker."""
    before = [
        index
        for index, atom in enumerate(atoms)
        if atom.end <= marker_start and index not in optional
    ]
    after = [index for index, atom in enumerate(atoms) if atom.start >= marker_end]
    if not before or not after:
        return None
    old_end = before[-1]
    target_start = after[0]
    if not re.fullmatch(r"[.!?;\s,()\-]*", raw[atoms[old_end].end : marker_start]):
        return None
    if not re.fullmatch(r"[\s,()\-]*", raw[marker_end : atoms[target_start].start]):
        return None
    if _structured_value_label(atoms[old_end]) is None:
        return None
    if _structured_value_label(atoms[target_start]) is None:
        return None

    old_indices = [old_end]
    while len(old_indices) < 8 and old_indices[0] > 0:
        candidate = old_indices[0] - 1
        if candidate in optional or _structured_value_label(atoms[candidate]) is None:
            break
        if not re.fullmatch(r"[ \t]+", raw[atoms[candidate].end : atoms[old_indices[0]].start]):
            break
        old_indices.insert(0, candidate)

    target_indices = [target_start]
    while len(target_indices) < 8 and target_indices[-1] + 1 < len(atoms):
        candidate = target_indices[-1] + 1
        if _structured_value_label(atoms[candidate]) is None:
            break
        if not re.fullmatch(r"[ \t]+", raw[atoms[target_indices[-1]].end : atoms[candidate].start]):
            break
        target_indices.append(candidate)

    while old_indices and _structured_value_label(atoms[old_indices[0]]).startswith("connector:"):
        old_indices.pop(0)
    while target_indices and _structured_value_label(atoms[target_indices[-1]]).startswith("connector:"):
        target_indices.pop()
    if not old_indices or not target_indices:
        return None

    old_signature = tuple(_structured_value_label(atoms[index]) for index in old_indices)
    target_signature = tuple(_structured_value_label(atoms[index]) for index in target_indices)
    if not _structured_signatures_are_compatible(old_signature, target_signature):
        return None
    if any(
        label.startswith("connector:")
        and (position == 0 or position == len(target_signature) - 1)
        for position, label in enumerate(target_signature)
    ):
        return None
    return set(old_indices), set(target_indices)


def _replacement_region_is_terminal(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
    target_span: set[int],
) -> bool:
    end = max(atoms[index].end for index in target_span)
    boundary = re.search(r"[.!?;\n]", raw[end:])
    region_end = end + boundary.start() if boundary else len(raw)
    return bool(re.fullmatch(r"[\s,()\-]*", raw[end:region_end]))


def _replacement_region_is_terminal_or_proven_chain(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
    target_span: set[int],
    retracted: set[int],
) -> bool:
    if _replacement_region_is_terminal(raw, atoms, target_span):
        return True
    if not target_span.issubset(retracted):
        return False
    end = max(atoms[index].end for index in target_span)
    separator = re.match(r"[\s,()\-]*", raw[end:])
    if separator is None:
        return False
    next_marker = _INLINE_CORRECTION_RE.match(raw, end + separator.end())
    if next_marker is not None:
        return True
    return _SORRY_CORRECTION_RE.match(raw, end + separator.end()) is not None


def _sorry_correction_pair(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
    marker: re.Match[str],
    optional: set[int],
) -> tuple[set[int], set[int]] | None:
    """Return evidenced old/target spans for a ``sorry`` self-correction.

    A bare marker is accepted only for a same-kind number/date/day/time slot. An
    explicit correction phrase directly after ``sorry`` may replace another bounded
    atom kind. This keeps explanatory apologies such as ``sorry I was late`` lexical.
    """
    explicit = _SORRY_EXPLICIT_CORRECTION_RE.match(raw, marker.end())
    target = _bounded_atom_after(
        raw,
        atoms,
        explicit.end() if explicit else marker.end(),
    )
    if target is None:
        return None

    boundary_positions = [
        raw.rfind(delimiter, 0, marker.start()) for delimiter in (".", "!", "?", ";", "\n")
    ]
    region_start = max(boundary_positions) + 1
    before = [
        index
        for index, atom in enumerate(atoms)
        if atom.start >= region_start and atom.end <= marker.start() and index not in optional
    ]
    if not before:
        return None
    if explicit:
        return {before[-1]}, {target}

    slot = _replacement_slot(atoms[target])
    if slot is None:
        return None

    target_indices = [target]
    if target + 1 < len(atoms):
        next_slot = _replacement_slot(atoms[target + 1])
        signature = (slot, next_slot)
        adjacent = bool(re.fullmatch(r"\s+", raw[atoms[target].end : atoms[target + 1].start]))
        if adjacent and signature in {
            ("number", "time"),
            ("month", "number"),
            ("number", "month"),
        }:
            target_indices.append(target + 1)

    target_signature = tuple(_replacement_slot(atoms[index]) for index in target_indices)
    old_indices = before[-len(target_indices) :]
    if (
        len(old_indices) != len(target_indices)
        or any(right != left + 1 for left, right in zip(old_indices, old_indices[1:]))
        or tuple(_replacement_slot(atoms[index]) for index in old_indices) != target_signature
        or any(
            not re.fullmatch(r"\s+", raw[atoms[left].end : atoms[right].start])
            for left, right in zip(old_indices, old_indices[1:])
        )
    ):
        return None
    return set(old_indices), set(target_indices)


def _bounded_unstructured_correction_pair(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
    marker: re.Match[str],
    optional: set[int],
) -> tuple[set[int], set[int]] | None:
    """Return equal-width terminal entity spans across an explicit comma boundary."""
    before = [
        index
        for index, atom in enumerate(atoms)
        if atom.end <= marker.start() and index not in optional
    ]
    boundary = re.search(r"[.!?;\n]", raw[marker.end() :])
    region_end = marker.end() + boundary.start() if boundary else len(raw)
    after = [
        index
        for index, atom in enumerate(atoms)
        if atom.start >= marker.end() and atom.start < region_end
    ]
    if not before or not 1 <= len(after) <= 3:
        return None
    old_span = before[-len(after) :]
    if len(old_span) != len(after):
        return None
    if not re.fullmatch(r"\s*,\s*", raw[atoms[old_span[-1]].end : marker.start()]):
        return None
    if not re.fullmatch(r"[\s,()\-]*", raw[marker.end() : atoms[after[0]].start]):
        return None
    if not re.fullmatch(r"[\s,()\-]*", raw[atoms[after[-1]].end : region_end]):
        return None
    if any(
        not re.fullmatch(r"[ \t\-]+", raw[atoms[left].end : atoms[right].start])
        for span in (old_span, after)
        for left, right in zip(span, span[1:])
    ):
        return None

    def source_is_capitalized(index: int) -> bool:
        source = raw[atoms[index].start : atoms[index].end].lstrip()
        return bool(source and source[0].isupper())

    entity_indices = (*old_span, *after)
    if any(
        atoms[index].value in _UNSTRUCTURED_NON_ENTITY_WORDS
        and not (
            atoms[index].value in {"may", "will"}
            and source_is_capitalized(index)
        )
        for index in entity_indices
    ):
        return None
    if len(after) > 1 and not all(source_is_capitalized(index) for index in after):
        return None
    all_capitalized = all(source_is_capitalized(index) for index in entity_indices)
    prior = next(
        (
            atom
            for atom in reversed(atoms)
            if atom.end <= atoms[old_span[0]].start
            and re.fullmatch(r"[ \t]+", raw[atom.end : atoms[old_span[0]].start])
        ),
        None,
    )
    old_has_entity_cue = bool(
        prior is not None and prior.value in _UNSTRUCTURED_ENTITY_SLOT_CUES
    )
    if not all_capitalized and not old_has_entity_cue:
        return None
    return set(old_span), set(after)


def _reviewed_relationship_correction_pair(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
    marker: re.Match[str],
    optional: set[int],
) -> tuple[set[int], set[int]] | None:
    """Return the reviewed same-width possessive relationship correction."""
    before = [
        index
        for index, atom in enumerate(atoms)
        if atom.end <= marker.start() and index not in optional
    ]
    after = [index for index, atom in enumerate(atoms) if atom.start >= marker.end()]
    if len(before) < 2 or len(after) < 2:
        return None

    old_span = before[-2:]
    target_span = after[:2]
    if not re.fullmatch(r"[ \t]+", raw[atoms[old_span[0]].end : atoms[old_span[1]].start]):
        return None
    if not re.fullmatch(r"[ \t]+", raw[atoms[target_span[0]].end : atoms[target_span[1]].start]):
        return None
    if not re.fullmatch(r"\s*,\s*", raw[atoms[old_span[-1]].end : marker.start()]):
        return None
    if not re.fullmatch(r"[\s,()\-]*", raw[marker.end() : atoms[target_span[0]].start]):
        return None
    if re.match(r"\s*(?:[,.;!?\n]|$)", raw[atoms[target_span[-1]].end :]) is None:
        return None

    old_possessive, old_relationship = (atoms[index].value for index in old_span)
    target_possessive, target_relationship = (atoms[index].value for index in target_span)
    if (
        old_possessive != target_possessive
        or old_possessive not in _REVIEWED_RELATIONSHIP_POSSESSIVES
        or old_relationship not in _REVIEWED_RELATIONSHIP_NOUNS
        or target_relationship not in _REVIEWED_RELATIONSHIP_NOUNS
        or old_relationship == target_relationship
    ):
        return None
    return set(old_span), set(target_span)


def _chat_correction_policy(raw: str, atoms: tuple[NormalizedAtom, ...]):
    optional: set[int] = set()
    targets: set[int] = set()
    retracted: set[int] = set()
    block_markers: list[tuple[re.Match[str], set[int]]] = []

    opening_so = _OPENING_DISCOURSE_SO_RE.match(raw)
    if opening_so:
        optional.update(
            index
            for index, atom in enumerate(atoms)
            if atom.start < opening_so.end("so") and atom.end > opening_so.start("so")
        )
        if opening_so.group("preface") in {"okay", "ok"}:
            optional.update(
                index
                for index, atom in enumerate(atoms)
                if atom.start < opening_so.end("preface")
                and atom.end > opening_so.start("preface")
            )
    for match in re.finditer(r"\blike\s+the\s+one\b", raw, re.IGNORECASE):
        comparative_noun = next(
            (
                atom
                for atom in reversed(atoms)
                if atom.end <= match.start()
                and re.fullmatch(r"[ \t]+", raw[atom.end : match.start()])
            ),
            None,
        )
        if comparative_noun is None or comparative_noun.value not in _COMPARATIVE_LIKE_NOUNS:
            continue
        optional.update(
            i for i, atom in enumerate(atoms)
            if atom.start < match.end() and atom.end > match.start()
        )

    for regex in (_INLINE_CORRECTION_RE, _DENVER_STYLE_CORRECTION_RE):
        for match in regex.finditer(raw):
            marker_indices = {i for i, atom in enumerate(atoms) if atom.start < match.end() and atom.end > match.start()}
            span = _finite_correction_value_spans(
                raw,
                atoms,
                marker_start=match.start(),
                marker_end=match.end(),
                optional=optional,
            )
            if span is not None:
                old_span, target_span = span
                optional.update(marker_indices)
                optional.update(old_span)
                targets.update(target_span)
                retracted.update(old_span)
                continue

            before = [
                i
                for i, atom in enumerate(atoms)
                if atom.end <= match.start() and i not in optional
            ]
            after = [i for i, atom in enumerate(atoms) if atom.start >= match.end()]
            if regex is _DENVER_STYLE_CORRECTION_RE:
                old = match.group("old").casefold()
                previous_old = [i for i in before if atoms[i].value == old]
                target = next(
                    (i for i in after if atoms[i].value not in _CHAT_FILLERS),
                    None,
                )
                if not previous_old or target is None:
                    continue
                old_span = {previous_old[-1]}
                optional.update(marker_indices)
                optional.update(old_span)
                targets.add(target)
                retracted.update(old_span)
                continue

            if match.group().casefold().startswith("scratch"):
                replacement_anchor = next(
                    (i for i in after if atoms[i].value not in _CHAT_FILLERS),
                    None,
                )
                repeated_before = (
                    [i for i in before if atoms[i].value == atoms[replacement_anchor].value]
                    if replacement_anchor is not None
                    else []
                )
                if repeated_before:
                    old_anchor = repeated_before[-1]
                    old_span = {i for i in before if i >= old_anchor}
                    optional.update(marker_indices)
                    optional.update(old_span)
                    retracted.update(old_span)
                    targets.add(replacement_anchor)
                continue

            marker_text = " ".join(match.group().casefold().split())
            if marker_text not in _UNSTRUCTURED_CORRECTION_MARKERS:
                continue
            pair = _reviewed_relationship_correction_pair(
                raw,
                atoms,
                match,
                optional,
            )
            if pair is None:
                pair = _bounded_unstructured_correction_pair(
                    raw,
                    atoms,
                    match,
                    optional,
                )
            if pair is None:
                continue
            old_span, target_span = pair
            optional.update(marker_indices)
            optional.update(old_span)
            targets.update(target_span)
            retracted.update(old_span)

    for match in reversed(list(_SORRY_CORRECTION_RE.finditer(raw))):
        explicit = _SORRY_EXPLICIT_CORRECTION_RE.match(raw, match.end())
        span_end = explicit.end() if explicit else match.end()
        span = _finite_correction_value_spans(
            raw,
            atoms,
            marker_start=match.start(),
            marker_end=span_end,
            optional=optional,
        )
        if span is not None:
            old_span, target_span = span
            if explicit is None and not _replacement_region_is_terminal_or_proven_chain(
                raw, atoms, target_span, retracted
            ):
                continue
            optional.update(
                i
                for i, atom in enumerate(atoms)
                if atom.start < span_end and atom.end > match.start()
            )
            optional.update(old_span)
            targets.update(target_span)
            retracted.update(old_span)
            continue
        if explicit is None:
            continue
        pair = _sorry_correction_pair(raw, atoms, match, optional)
        if pair is None:
            continue
        old, target = pair
        optional.update(
            i for i, atom in enumerate(atoms)
            if atom.start < span_end and atom.end > match.start()
        )
        optional.update(old)
        targets.update(target)
        retracted.update(old)

    for match in _BLOCK_RETRACTION_RE.finditer(raw):
        marker_indices = {i for i, atom in enumerate(atoms) if atom.start < match.end() and atom.end > match.start()}
        optional.update(marker_indices)
        block_markers.append((match, marker_indices))
        after = [i for i, atom in enumerate(atoms) if atom.start >= match.end() and atom.value not in _CHAT_FILLERS]
        if after:
            targets.add(after[0])
    targets.difference_update(retracted)
    optional.update(_retraction_scaffold_indices(raw, atoms, targets, retracted))
    return optional, targets, retracted, block_markers


def _retraction_scaffold_indices(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
    targets: set[int],
    retracted: set[int],
) -> set[int]:
    """Return scaffold words left dangling by an accepted retraction.

    Only fires when this source actually retracts something, and only for the
    finite reviewed scaffold vocabulary sitting in the replacement clause that
    follows the retracted span. Everything else stays required content, so a
    model that silently drops real words still trips ``required_content_missing``.
    """
    if not retracted:
        return set()
    retraction_end = max(atoms[index].end for index in retracted)
    scaffold: set[int] = set()
    for index, atom in enumerate(atoms):
        if index in targets or atom.value not in _RETRACTION_SCAFFOLD_WORDS:
            continue
        if atom.start < retraction_end:
            continue
        if _SENTENCE_BREAK_RE.search(raw, retraction_end, atom.start):
            continue
        scaffold.add(index)
    return scaffold


def _source_proven_literal_scale_words(raw: str, cleaned: str) -> frozenset[str]:
    """Return finite full scale words rendered literally on both sides."""
    return frozenset(
        scale
        for scale in ("thousand",)
        if re.search(rf"\b{scale}\b", raw, re.IGNORECASE)
        and re.search(rf"\b\d+[ \t]+{scale}\b", cleaned, re.IGNORECASE)
    )


def run_chat_guards(raw: str, cleaned: str, *, emit_log: bool = True) -> GuardResult:
    """Enforce the finite built-in Chat anchor and correction invariants."""
    fallback_text = raw if isinstance(raw, str) else ""
    if not isinstance(raw, str) or not isinstance(cleaned, str):
        return _fallback(fallback_text, "unexpected_error", [])
    try:
        literal_scales = _source_proven_literal_scale_words(raw, cleaned)
        raw_atoms = normalize_chat_atoms(
            raw,
            literal_scale_words=literal_scales,
            allow_reviewed_ngl_surface=True,
        )
        cleaned_atoms = normalize_chat_atoms(
            cleaned,
            literal_scale_words=literal_scales,
            allow_reviewed_ngl_surface=(
                _REVIEWED_NGL_SURFACE_RE.search(raw) is not None
            ),
        )
        raw_presentation, cleaned_presentation = _list_presentation_atom_indices(
            raw, cleaned, raw_atoms, cleaned_atoms
        )
        optional, targets, retracted, block_markers = _chat_correction_policy(raw, raw_atoms)
        optional.update(raw_presentation)
        raw_projection = [
            (index, atom)
            for index, atom in enumerate(raw_atoms)
            if index not in raw_presentation and index not in retracted
        ]
        cleaned_projection = [
            (index, atom)
            for index, atom in enumerate(cleaned_atoms)
            if index not in cleaned_presentation
        ]
        projected_pairs = _chat_alignment(
            tuple(atom for _, atom in raw_projection),
            tuple(atom for _, atom in cleaned_projection),
        )
        matched_raw = {
            raw_projection[raw_index][0] for raw_index, _ in projected_pairs
        }
        matched_cleaned = {
            cleaned_projection[cleaned_index][0] for _, cleaned_index in projected_pairs
        }

        if any(target not in matched_raw for target in targets):
            return _fallback(raw, "correction_target_missing", [GuardCheck("correction_target_missing", True)], emit_log=emit_log)
        if retracted & matched_raw:
            return _fallback(
                raw,
                "correction_old_content_retained",
                [GuardCheck("correction_old_content_retained", True)],
                emit_log=emit_log,
            )

        for match, marker_indices in block_markers:
            prior_matches = [i for i in matched_raw if raw_atoms[i].end <= match.start()]
            scrap_indices = {i for i in marker_indices if raw_atoms[i].value == "scrap"}
            if len(prior_matches) >= 3 and not (scrap_indices & matched_raw):
                return _fallback(raw, "correction_marker_missing", [GuardCheck("correction_marker_missing", True)], emit_log=emit_log)

        missing = []
        for index, atom in enumerate(raw_atoms):
            if index in matched_raw or index in optional:
                continue
            reviewed_stutter_matched = atom.value in _REVIEWED_STUTTER_WORDS and any(
                other in matched_raw and raw_atoms[other].value == atom.value and abs(other - index) == 1
                for other in range(len(raw_atoms))
            )
            if not reviewed_stutter_matched:
                missing.append(index)
        if missing:
            return _fallback(raw, "required_content_missing", [GuardCheck("required_content_missing", True)], emit_log=emit_log)

        if any(
            index not in matched_cleaned and index not in cleaned_presentation
            for index in range(len(cleaned_atoms))
        ):
            return _fallback(raw, "unexpected_insertion", [GuardCheck("unexpected_insertion", True)], emit_log=emit_log)
        return _keep(cleaned, [GuardCheck("chat_invariants", False)])
    except Exception:
        return _fallback(raw, "unexpected_error", [GuardCheck("unexpected_error", True)], emit_log=emit_log)


def _code_tokens(text: str) -> list[tuple[str, int, int]]:
    return [(match.group(), match.start(), match.end()) for match in _CODE_TOKEN_RE.finditer(text)]


def normalize_code_atoms(text: str, *, family: CodeFamily) -> tuple[NormalizedAtom, ...]:
    """Normalize only the separately reviewed Gemma/Stream Code renderings."""
    tokens = _code_tokens(text)
    atoms: list[NormalizedAtom] = []
    index = 0
    while index < len(tokens):
        token, start, end = tokens[index]
        folded = token.casefold().replace("’", "'")

        if folded in {"double", "triple", "not"} and index + 1 < len(tokens):
            if tokens[index + 1][0].casefold() == "equals":
                operator = {"double": "==", "triple": "===", "not": "!="}[folded]
                atoms.append(NormalizedAtom(f"operator:{operator}", start, tokens[index + 1][2]))
                index += 2
                continue
        if folded in {"equals", "slash", "dot"}:
            operator = {"equals": "=", "slash": "/", "dot": "."}[folded]
            atoms.append(NormalizedAtom(f"operator:{operator}", start, end))
            index += 1
            continue
        if folded == "." and (end >= len(text) or not re.match(r"[^\W\d_]", text[end], re.UNICODE)):
            index += 1
            continue
        if folded in {"===", "==", "!=", "=", "/", ".", "@"}:
            atoms.append(NormalizedAtom(f"operator:{folded}", start, end))
            index += 1
            continue
        if folded == "at" and index + 1 < len(tokens):
            if tokens[index + 1][0].casefold() == "tanstack":
                atoms.append(NormalizedAtom("operator:@", start, end))
                index += 1
                continue

        identifier_parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+", token)
        if len(identifier_parts) > 1:
            atoms.extend(
                NormalizedAtom(part.casefold(), start, end) for part in identifier_parts
            )
            index += 1
            continue

        casing_length = 0
        if folded.endswith("case") and folded.removesuffix("case") in _CASING_WORDS:
            casing_length = 1
        elif folded in _CASING_WORDS and index + 1 < len(tokens) and tokens[index + 1][0].casefold() == "case":
            casing_length = 2
        if casing_length:
            if family == "stream":
                atoms.append(NormalizedAtom(folded.removesuffix("case"), start, end))
                if casing_length == 2:
                    atoms.append(NormalizedAtom("case", tokens[index + 1][1], tokens[index + 1][2]))
            index += casing_length
            continue

        if folded == "async" and index + 1 < len(tokens):
            following = tokens[index + 1][0].casefold()
            if following == "oight":
                atoms.extend((
                    NormalizedAtom("async", start, end),
                    NormalizedAtom("operator:/", start, tokens[index + 1][2]),
                    NormalizedAtom("await", tokens[index + 1][1], tokens[index + 1][2]),
                ))
                index += 2
                continue
            if following == "a" and index + 2 < len(tokens) and tokens[index + 2][0].casefold() == "wait":
                atoms.extend((
                    NormalizedAtom("async", start, end),
                    NormalizedAtom("operator:/", tokens[index + 1][1], tokens[index + 2][2]),
                    NormalizedAtom("await", tokens[index + 1][1], tokens[index + 2][2]),
                ))
                index += 3
                continue

        if folded in _NUMBER_UNITS or folded in _NUMBER_TENS:
            first_number = _number_phrase(tokens, index)
            if first_number is not None:
                value, last = first_number
                if last < len(tokens) and tokens[last][0].casefold() == "point":
                    decimal = _number_phrase(tokens, last + 1)
                    if decimal is not None:
                        decimal_value, decimal_last = decimal
                        atoms.append(NormalizedAtom(
                            f"num:{value}.{decimal_value}", start, tokens[decimal_last - 1][2]
                        ))
                        index = decimal_last
                        continue

        if index + 2 < len(tokens):
            digit_words = [tokens[index + offset][0].casefold() for offset in range(3)]
            if all(word in _NUMBER_UNITS or word == "oh" for word in digit_words) and "oh" in digit_words:
                digits = "".join(str(_NUMBER_UNITS.get(word, 0)) for word in digit_words)
                atoms.append(NormalizedAtom(f"num:{digits}", start, tokens[index + 2][2]))
                index += 3
                continue

        if len(folded) == 1 and folded.isalpha():
            last = index
            letters: list[str] = []
            while last < len(tokens) and len(tokens[last][0]) == 1 and tokens[last][0].isalpha():
                letters.append(tokens[last][0].casefold())
                last += 1
            joined = "".join(letters)
            if joined in _CODE_ACRONYMS:
                atoms.append(NormalizedAtom(joined, start, tokens[last - 1][2]))
                index = last
                continue

        number = _number_phrase(tokens, index)
        if number is not None:
            value, last = number
            atoms.append(NormalizedAtom(f"num:{value}", start, tokens[last - 1][2]))
            index = last
            continue
        if re.fullmatch(r"\d+(?:[.,]\d+)*", folded):
            atoms.append(NormalizedAtom(f"num:{folded.replace(',', '')}", start, end))
        else:
            atoms.append(NormalizedAtom(folded, start, end))
        index += 1
    return tuple(atoms)


def _code_correction_policy(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
) -> tuple[set[int], set[int]]:
    optional: set[int] = set()
    targets: set[int] = set()
    for match in _CODE_CORRECTION_RE.finditer(raw):
        target = _bounded_replacement_target(raw, atoms, match)
        before = [index for index, atom in enumerate(atoms) if atom.end <= match.start()]
        if target is None or not before:
            continue
        optional.update(
            index for index, atom in enumerate(atoms)
            if atom.start < match.end() and atom.end > match.start()
        )
        optional.add(before[-1])
        targets.add(target)
    return optional, targets


def _matched_atom_indices(
    raw_atoms: tuple[NormalizedAtom, ...],
    cleaned_atoms: tuple[NormalizedAtom, ...],
    *,
    excluded_raw: set[int] | None = None,
) -> tuple[set[int], set[int]]:
    excluded_raw = excluded_raw or set()
    raw_projection = [
        (index, atom)
        for index, atom in enumerate(raw_atoms)
        if index not in excluded_raw
    ]
    pairs = _chat_alignment(
        tuple(atom for _, atom in raw_projection),
        cleaned_atoms,
    )
    return (
        {raw_projection[raw_index][0] for raw_index, _ in pairs},
        {cleaned_index for _, cleaned_index in pairs},
    )


def _finite_atom_guard(
    raw: str,
    cleaned: str,
    raw_atoms: tuple[NormalizedAtom, ...],
    cleaned_atoms: tuple[NormalizedAtom, ...],
    *,
    optional: set[int] | None = None,
    required_targets: set[int] | None = None,
    retracted: set[int] | None = None,
    allow_reviewed_stutters: bool = False,
    emit_log: bool = True,
) -> GuardResult:
    optional = optional or set()
    required_targets = required_targets or set()
    retracted = retracted or set()
    matched_raw, matched_cleaned = _matched_atom_indices(
        raw_atoms,
        cleaned_atoms,
        excluded_raw=retracted,
    )
    if any(index not in matched_raw for index in required_targets):
        return _fallback(
            raw,
            "correction_target_missing",
            [GuardCheck("correction_target_missing", True)],
            emit_log=emit_log,
        )
    if retracted & matched_raw:
        return _fallback(
            raw,
            "correction_old_content_retained",
            [GuardCheck("correction_old_content_retained", True)],
            emit_log=emit_log,
        )
    if any(index not in matched_cleaned for index in range(len(cleaned_atoms))):
        return _fallback(
            raw,
            "unexpected_insertion",
            [GuardCheck("unexpected_insertion", True)],
            emit_log=emit_log,
        )
    for index, atom in enumerate(raw_atoms):
        if index in matched_raw or index in optional:
            continue
        if allow_reviewed_stutters and atom.value in _REVIEWED_STUTTER_WORDS and any(
            other in matched_raw and raw_atoms[other].value == atom.value and abs(other - index) == 1
            for other in range(len(raw_atoms))
        ):
            continue
        return _fallback(
            raw,
            "required_content_missing",
            [GuardCheck("required_content_missing", True)],
            emit_log=emit_log,
        )
    return _keep(cleaned, [GuardCheck("finite_invariants", False)])


def run_code_guards(
    raw: str,
    cleaned: str,
    *,
    family: CodeFamily,
    emit_log: bool = True,
) -> GuardResult:
    """Enforce only the reviewed equivalences for one local Code family."""
    fallback_text = raw if isinstance(raw, str) else ""
    if not isinstance(raw, str) or not isinstance(cleaned, str) or family not in {"gemma", "stream"}:
        return _fallback(fallback_text, "unexpected_error", [], emit_log=emit_log)
    try:
        raw_atoms = normalize_code_atoms(raw, family=family)
        optional, targets = _code_correction_policy(raw, raw_atoms)
        return _finite_atom_guard(
            raw,
            cleaned,
            raw_atoms,
            normalize_code_atoms(cleaned, family=family),
            optional=optional,
            required_targets=targets,
            emit_log=emit_log,
        )
    except Exception:
        return _fallback(
            raw, "unexpected_error", [GuardCheck("unexpected_error", True)], emit_log=emit_log
        )


def _email_route(raw: str) -> tuple[re.Match[str] | None, str | None]:
    for regex in _EMAIL_ROUTING_RES:
        match = regex.search(raw)
        if match:
            recipient = re.sub(r"\s+", " ", match.group("recipient").casefold()).removeprefix("the ")
            return match, recipient
    return None, None


def _email_has_fabricated_routing_greeting(raw: str, cleaned: str) -> bool:
    _route_match, recipient = _email_route(raw)
    if recipient is None or _EMAIL_GREETING_RE.search(raw):
        return False
    first_line = next((line.strip() for line in cleaned.splitlines() if line.strip()), "")
    if not first_line.endswith(",") or _EMAIL_GREETING_RE.search(first_line):
        return False
    return first_line[:-1].strip().casefold() == recipient


def run_email_guards(raw: str, cleaned: str, *, emit_log: bool = True) -> GuardResult:
    """Enforce conservative Email content and routing-greeting provenance."""
    fallback_text = raw if isinstance(raw, str) else ""
    if not isinstance(raw, str) or not isinstance(cleaned, str):
        return _fallback(fallback_text, "unexpected_error", [], emit_log=emit_log)
    try:
        if _email_has_fabricated_routing_greeting(raw, cleaned):
            return _fallback(
                raw, "routing_greeting", [GuardCheck("routing_greeting", True)], emit_log=emit_log
            )
        raw_atoms = tuple(atom for atom in normalize_chat_atoms(raw) if atom.value != ":")
        cleaned_atoms = tuple(atom for atom in normalize_chat_atoms(cleaned) if atom.value != ":")
        optional, targets, retracted, _blocks = _chat_correction_policy(raw, raw_atoms)
        route_match, recipient = _email_route(raw)
        if route_match:
            greeting_match = _EMAIL_GREETING_ANY_RE.search(raw, route_match.end())
            if greeting_match and not re.fullmatch(
                r"[\s,()\-]*", raw[route_match.end() : greeting_match.start()]
            ):
                greeting_match = None
            greeting_indices = [
                index for index, atom in enumerate(raw_atoms)
                if greeting_match is not None
                and atom.start < greeting_match.end()
                and atom.end > greeting_match.start()
            ]
            recipient_indices = [
                index for index, atom in enumerate(raw_atoms)
                if atom.start < route_match.end()
                and atom.end > route_match.start()
                and recipient is not None
                and atom.value in recipient.split()
            ]
            if greeting_indices and recipient_indices:
                route_indices = {
                    index for index, atom in enumerate(raw_atoms)
                    if atom.start < route_match.end() and atom.end > route_match.start()
                }
                prefix = [
                    atom for index, atom in enumerate(raw_atoms)
                    if index not in route_indices and index not in greeting_indices
                ]
                raw_atoms = (
                    *(raw_atoms[index] for index in greeting_indices),
                    *(raw_atoms[index] for index in recipient_indices),
                    *prefix,
                )
                optional, targets, retracted, _blocks = _chat_correction_policy(raw, raw_atoms)
            optional.update(
                index for index, atom in enumerate(raw_atoms)
                if atom.start < route_match.end() and atom.end > route_match.start()
                and not (greeting_indices and recipient is not None and atom.value in recipient.split())
            )
        for index, atom in enumerate(raw_atoms):
            if atom.value == "and" and index + 1 < len(raw_atoms) and raw_atoms[index + 1].value.startswith("num:"):
                optional.add(index)
        return _finite_atom_guard(
            raw,
            cleaned,
            raw_atoms,
            cleaned_atoms,
            optional=optional,
            required_targets=targets,
            retracted=retracted,
            allow_reviewed_stutters=True,
            emit_log=emit_log,
        )
    except Exception:
        return _fallback(
            raw, "unexpected_error", [GuardCheck("unexpected_error", True)], emit_log=emit_log
        )


def run_contract_guards(
    raw: str,
    cleaned: str,
    contract: GuardContract,
    *,
    emit_log: bool = True,
) -> GuardResult:
    """Dispatch only classified, calibrated discretionary contracts."""
    if contract.name in {"chat_stream", "chat_gemma"}:
        return run_chat_guards(raw, cleaned, emit_log=emit_log)
    if contract.name == "code_stream":
        return run_code_guards(raw, cleaned, family="stream", emit_log=emit_log)
    if contract.name == "code_gemma":
        return run_code_guards(raw, cleaned, family="gemma", emit_log=emit_log)
    if contract.name == "email":
        return run_email_guards(raw, cleaned, emit_log=emit_log)
    return _keep(cleaned, [GuardCheck("unsupported_contract", False)])


def _keep(cleaned: str, checks: list[GuardCheck]) -> GuardResult:
    return GuardResult(cleaned, "keep", None, tuple(checks))


def _fallback(
    raw: str,
    tag: str,
    checks: list[GuardCheck],
    *,
    emit_log: bool = True,
) -> GuardResult:
    if emit_log:
        logger.warning("[Guard] fired guard=%s checks=%d", tag, len(checks))
    return GuardResult(raw, "fallback", tag, tuple(checks))


def has_transcript_leak(cleaned: str, raw: str) -> bool:
    """Return whether output introduces a reviewed local prompt wrapper marker.

    The public name and ``transcript_leak`` guard tag remain stable for callers and
    logs. Internally, the finite prompt inventory also covers the Email few-shot
    ``INPUT``/``OUTPUT`` wrappers. Evidence is compared by wrapper name, direction,
    and count so faithfully dictated literals pass while duplicated markers do not.
    """
    output_markers = Counter(
        (
            match.group("name").casefold(),
            "close" if match.group("closing") else "open",
        )
        for match in _PROMPT_WRAPPER_TAG_RE.finditer(cleaned)
    )
    if not output_markers:
        return False
    source_markers = Counter(
        (
            match.group("name").casefold(),
            "close" if match.group("closing") else "open",
        )
        for match in _PROMPT_WRAPPER_TAG_RE.finditer(raw)
    )
    return any(
        count > source_markers[marker]
        for marker, count in output_markers.items()
    )


def has_refusal_or_meta_not_in_source(cleaned: str, raw: str) -> bool:
    """Return whether output introduces a known refusal or cleanup preamble."""
    if has_refusal_not_in_input(cleaned, raw):
        return True
    cleaned_folded = cleaned.casefold()
    raw_folded = raw.casefold()
    return any(
        marker in cleaned_folded and marker not in raw_folded
        for marker in _META_PREAMBLES
    )


def _colon_fragment_words(segment: str) -> list[str]:
    return [
        match.group().casefold()
        for match in _ITEM_FRAGMENT_WORD_RE.finditer(segment)
    ]


def _has_lexical_colon_fragment_head(words: list[str]) -> bool:
    if not words:
        return False
    head_index = 1 if words[0] in _ITEM_FRAGMENT_DETERMINERS else 0
    if head_index >= len(words):
        return False
    return words[head_index] not in (
        _ITEM_FRAGMENT_SUBORDINATORS
        | _ITEM_FRAGMENT_SUBJECT_HEADS
        | _ITEM_FRAGMENT_DISCOURSE_HEADS
    )


def _structural_colon_items(suffix: str) -> tuple[str, ...] | None:
    """Return source fragments proven by a finite comma/semicolon list shape."""
    if "\n" in suffix or "\r" in suffix:
        return None
    body = suffix.strip()
    if not body:
        return None
    if body[-1] in ".?!":
        body = body[:-1].rstrip()
    if not body or re.search(r"[.?!]", body):
        return None

    if ";" in body:
        if "," in body:
            return None
        segments = [segment.strip() for segment in body.split(";")]
        if len(segments) < 2 or any(not segment for segment in segments):
            return None
        fragment_words = [_colon_fragment_words(segment) for segment in segments]
        if not all(
            1 <= len(words) <= 8 and _has_lexical_colon_fragment_head(words)
            for words in fragment_words
        ):
            return None
        return tuple(segments)

    if "," not in body:
        return None
    segments = [segment.strip() for segment in body.split(",")]
    if len(segments) < 2 or any(not segment for segment in segments):
        return None
    fragment_words = [_colon_fragment_words(segment) for segment in segments]
    if not all(
        _has_lexical_colon_fragment_head(words)
        and 1
        <= len(words) - (1 if words[0] in _ITEM_FRAGMENT_DETERMINERS else 0)
        <= 2
        for words in fragment_words
    ):
        return None
    return tuple(segments)


def _explicit_ordered_sequence_cue(raw: str) -> bool:
    folded = raw.casefold()
    first = re.search(r"\bfirst(?:ly)?\b", folded)
    second = re.search(r"\bsecond(?:ly)?\b", folded)
    if first and second and first.start() < second.start():
        return True

    number_one = re.search(r"\bnumber\s+one\b", folded)
    number_two = re.search(r"\bnumber\s+two\b", folded)
    if number_one and number_two and number_one.start() < number_two.start():
        return True

    for colon_match in re.finditer(":", raw):
        values: list[int] = []
        for cue in _SOURCE_LIST_CUE_RE.finditer(raw[colon_match.end() :]):
            cue_text = cue.group().casefold()
            if cue_text.startswith("number "):
                values.append(_LIST_NUMBER_VALUES[cue_text.split()[-1]])
            else:
                values.append(_LIST_ORDINAL_VALUES[cue_text])
        if any(right == left + 1 for left, right in zip(values, values[1:])):
            return True
    return False


def _proven_colon_fragments(raw: str) -> tuple[str, ...] | None:
    for colon_match in re.finditer(":", raw):
        colon = colon_match.start()
        if (
            colon > 0
            and colon + 1 < len(raw)
            and raw[colon - 1].isdigit()
            and raw[colon + 1].isdigit()
        ):
            continue
        if raw[colon + 1 :].lstrip().startswith("//"):
            continue
        prefix = raw[:colon].rstrip()
        lead_clause = re.split(r"[.!?;\n]", prefix)[-1].strip()
        lead_clause = re.sub(r"^#{1,6}[ \t]+", "", lead_clause)
        if not _COLON_LIST_LEAD_RE.fullmatch(lead_clause):
            continue
        framing_starts = [
            match.start()
            for pattern in (_MARKDOWN_HEADING_RE, _MARKDOWN_HORIZONTAL_RULE_RE)
            for match in pattern.finditer(raw, colon + 1)
        ]
        body_end = min(framing_starts, default=len(raw))
        fragments = _structural_colon_items(
            raw[colon + 1 : body_end].rstrip("\r\n")
        )
        if fragments is not None:
            return fragments
    return None


def _numbered_output_items(cleaned: str) -> tuple[str, ...] | None:
    items: list[str] = []
    started = False
    for line in cleaned.splitlines():
        match = _NUMBERED_ITEM_LINE_RE.fullmatch(line)
        if match:
            started = True
            items.append(match.group("content"))
            continue
        if started and (
            _MARKDOWN_HEADING_RE.match(line)
            or _MARKDOWN_HORIZONTAL_RULE_RE.fullmatch(line)
        ):
            continue
        if started and line.strip():
            return None
    return tuple(items) if items else None


def _has_invented_punctuation_only_list_line(raw: str, cleaned: str) -> bool:
    source_lines = {
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not any(character.isalnum() for character in line)
    }
    for line in cleaned.splitlines():
        if _NUMBERED_ITEM_LINE_RE.fullmatch(line):
            continue
        stripped = line.strip()
        if (
            stripped
            and not any(character.isalnum() for character in stripped)
            and stripped not in source_lines
        ):
            return True
    return False


def _numbered_items_match_fragments(
    cleaned: str,
    fragments: tuple[str, ...],
) -> bool:
    output_items = _numbered_output_items(cleaned)
    if output_items is None or len(output_items) != len(fragments):
        return False
    source_atoms = [tuple(atom.value for atom in normalize_chat_atoms(item)) for item in fragments]
    output_atoms = [tuple(atom.value for atom in normalize_chat_atoms(item)) for item in output_items]
    return output_atoms == source_atoms


def _has_ordered_list_cue(raw: str) -> bool:
    """Recognize the three owner-locked cues for legitimate list rendering."""
    if _explicit_ordered_sequence_cue(raw):
        return True
    return _proven_colon_fragments(raw) is not None


def _is_json_container(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        return isinstance(json.loads(stripped), (dict, list))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _markdown_framing_signatures(
    text: str,
    atoms: tuple[NormalizedAtom, ...],
    *,
    excluded_atom_indices: set[int],
) -> tuple[tuple[str, str, int, str | None], ...]:
    """Return ordered, counted Markdown framing with lexical provenance.

    Character offsets are too brittle for cleanup reflow, so positions count the
    same canonical content atoms used by the guard's content comparison, excluding
    proven list presentation and source correction scrap. Headings also retain the
    first atom on their own line: this lets a dictated heading shed a list suffix
    while preventing its marker from being reassigned to another clause.
    """
    def content_position(offset: int) -> int:
        return sum(
            index not in excluded_atom_indices and atom.end <= offset
            for index, atom in enumerate(atoms)
        )

    occurrences: list[tuple[int, tuple[str, str, int, str | None]]] = []
    for match in _MARKDOWN_HEADING_RE.finditer(text):
        line_end = text.find("\n", match.end())
        if line_end == -1:
            line_end = len(text)
        anchor = next(
            (
                atom.value
                for atom in atoms
                if atom.start >= match.end() and atom.end <= line_end
            ),
            None,
        )
        occurrences.append(
            (
                match.start(),
                (
                    "heading",
                    match.group("marker"),
                    content_position(match.start()),
                    anchor,
                ),
            )
        )

    for match in _MARKDOWN_HORIZONTAL_RULE_RE.finditer(text):
        marker_shape = "".join(
            character
            for character in match.group("marker")
            if not character.isspace()
        )
        occurrences.append(
            (
                match.start(),
                (
                    "rule",
                    marker_shape,
                    content_position(match.start()),
                    None,
                ),
            )
        )

    occurrences.sort(key=lambda occurrence: occurrence[0])
    return tuple(signature for _, signature in occurrences)


def _source_correction_position_indices(
    raw: str,
    atoms: tuple[NormalizedAtom, ...],
) -> set[int]:
    """Return only confidently accepted correction scrap for position counts."""
    optional, _targets, retracted, _blocks = _chat_correction_policy(raw, atoms)
    marker_ranges = [
        (match.start(), match.end())
        for pattern in (
            _INLINE_CORRECTION_RE,
            _DENVER_STYLE_CORRECTION_RE,
            _SORRY_CORRECTION_RE,
            _SORRY_EXPLICIT_CORRECTION_RE,
            _BLOCK_RETRACTION_RE,
        )
        for match in pattern.finditer(raw)
    ]
    accepted_marker_indices = {
        index
        for index in optional
        if any(
            atoms[index].start < end and atoms[index].end > start
            for start, end in marker_ranges
        )
    }
    return retracted | accepted_marker_indices


def _has_introduced_markdown_framing(cleaned: str, raw: str) -> bool:
    framing_patterns = (_MARKDOWN_HEADING_RE, _MARKDOWN_HORIZONTAL_RULE_RE)
    if not any(pattern.search(cleaned) or pattern.search(raw) for pattern in framing_patterns):
        return False
    raw_atoms = normalize_chat_atoms(raw)
    cleaned_atoms = normalize_chat_atoms(cleaned)
    raw_presentation, cleaned_presentation = _list_presentation_atom_indices(
        raw,
        cleaned,
        raw_atoms,
        cleaned_atoms,
    )
    raw_correction_scrap = _source_correction_position_indices(
        raw,
        raw_atoms,
    )
    raw_excluded = raw_presentation | raw_correction_scrap
    return _markdown_framing_signatures(
        cleaned,
        cleaned_atoms,
        excluded_atom_indices=cleaned_presentation,
    ) != _markdown_framing_signatures(
        raw,
        raw_atoms,
        excluded_atom_indices=raw_excluded,
    )


def has_introduced_structure(cleaned: str, raw: str) -> bool:
    """Return whether a built-in cleanup output imposes new structural framing."""
    if _has_introduced_markdown_framing(cleaned, raw):
        return True
    if _FENCE_RE.search(cleaned) and not _FENCE_RE.search(raw):
        return True
    if _is_json_container(cleaned) and not _is_json_container(raw):
        return True

    output_number_markers = _structural_number_markers(cleaned)
    if output_number_markers:
        output_values = [int(match.group("number")) for match in output_number_markers]
        output_signatures = [
            (int(match.group("number")), match.group("separator").strip())
            for match in output_number_markers
        ]
        raw_number_markers = _structural_number_markers(raw)
        if raw_number_markers:
            raw_signatures = [
                (int(match.group("number")), match.group("separator").strip())
                for match in raw_number_markers
            ]
            if output_signatures != raw_signatures:
                return True
        expected_values = list(range(1, len(output_values) + 1))
        if output_values != expected_values and not raw_number_markers:
            return True

    output_has_list = bool(output_number_markers or _BULLET_LIST_MARKER_RE.search(cleaned))
    source_has_list = bool(
        _structural_number_markers(raw) or _BULLET_LIST_MARKER_RE.search(raw)
    )
    if not output_has_list or source_has_list:
        return False
    if output_number_markers and _has_invented_punctuation_only_list_line(raw, cleaned):
        return True
    if output_number_markers and not _explicit_ordered_sequence_cue(raw):
        fragments = _proven_colon_fragments(raw)
        if fragments is not None:
            return not _numbered_items_match_fragments(cleaned, fragments)
    return not _has_ordered_list_cue(raw)


def run_stage1_guards(
    raw: str,
    cleaned: str,
    *,
    check_structure: bool = True,
) -> GuardResult:
    """Run universal hard checks on untouched model output.

    ``check_structure`` must be false for an unclassified/custom contract that may
    intentionally request structured output. All other checks remain universal.
    """
    fallback_text = raw if isinstance(raw, str) else ""
    if not isinstance(raw, str) or not isinstance(cleaned, str):
        return _fallback(fallback_text, "unexpected_error", [])

    checks: list[GuardCheck] = []
    try:
        # Specific defects precede generic size/shape checks so reason tags remain
        # deterministic when one corrupt output exhibits more than one symptom.
        candidates = [
            ("transcript_leak", has_transcript_leak(cleaned, raw)),
            ("refusal", has_refusal_or_meta_not_in_source(cleaned, raw)),
            ("repetition", has_sentence_repetition(cleaned, raw)),
        ]
        if check_structure:
            candidates.append(("introduced_structure", has_introduced_structure(cleaned, raw)))
        candidates.append(("length_explosion", is_length_explosion(cleaned, raw)))

        for tag, tripped in candidates:
            check = GuardCheck(tag, tripped)
            checks.append(check)
            if tripped:
                return _fallback(raw, tag, checks)
        return _keep(cleaned, checks)
    except Exception:
        checks.append(GuardCheck("unexpected_error", True))
        return _fallback(raw, "unexpected_error", checks)


def guard_digit_integrity(raw: str, cleaned: str) -> GuardResult:
    """Fallback on an unresolved, confidently aligned post-repair digit mismatch."""
    fallback_text = raw if isinstance(raw, str) else ""
    if not isinstance(raw, str) or not isinstance(cleaned, str):
        return _fallback(fallback_text, "unexpected_error", [])
    try:
        analysis = analyze_digit_mismatches(cleaned, raw)
        tripped = analysis.confidently_aligned and analysis.unresolved_mismatches > 0
        check = GuardCheck("digit_mismatch", tripped)
        if tripped:
            return _fallback(raw, "digit_mismatch", [check])
        return _keep(cleaned, [check])
    except Exception:
        return _fallback(raw, "unexpected_error", [GuardCheck("unexpected_error", True)])


def guard_output(
    raw: str,
    cleaned: str,
    *,
    check_structure: bool = True,
) -> GuardResult:
    """Run Stage 1 then F1b over a value already passed through digit repair.

    Integration calls Stage 1 on untouched model output, performs
    ``repair_source_digits()``, then calls :func:`guard_digit_integrity`. This
    convenience function is for an already-repaired value and preserves the same
    whole-unit fallback contract.
    """
    stage1 = run_stage1_guards(raw, cleaned, check_structure=check_structure)
    if stage1.action == "fallback":
        return stage1
    return guard_digit_integrity(raw, cleaned)
