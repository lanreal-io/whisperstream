"""Conservative source-anchored long-digit repair.

Only uniquely context-bound, multiplier-free long numbers are writable. Numeric
corrections, uncertain alignment, clocks, fractions and magnitudes are no-ops.
Both public APIs share eligibility; neither infers formats or correction targets.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DIGITS = frozenset("0123456789")

# --- Hand-rolled number-words lexicon (no third-party lib) --------------------
_UNITS = {
    "zero": 0, "oh": 0, "o": 0,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_MONTHS = frozenset({
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
})
_WEEKDAYS = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})
# Magnitude multipliers -> scale. A span containing one is parsed by the arithmetic
# state machine (running total); an ill-formed magnitude span becomes a WILDCARD
# (kept in the alignment, never repaired).
_MULTIPLIERS = {
    "hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000,
}

# Scale words that, when they IMMEDIATELY follow (or abut) an output digit run, mean the
# run is a RENDERED magnitude ("256 thousand", "2 million", "90k", "256 grand") whose digit
# string intentionally differs from the spoken source value. Repairing such a run would
# over-fire ("256 thousand" -> "256000 thousand"), so it is never touched.
_SCALE_WORDS = frozenset({
    "hundred", "thousand", "million", "billion", "k", "m", "b", "grand",
})

# Token classes.
_UNIT, _TEEN, _TEN, _LIT, _MULT = "unit", "teen", "ten", "lit", "mult"

# A span recognized as a real spoken number but not resolvable to confident digits
# (ill-formed magnitude, magnitude mixed with already-digit tokens). It consumes one
# output run in the positional alignment but is NEVER repaired — keeping the counts
# aligned so the OTHER numbers in the same utterance stay repairable instead of the whole
# alignment desyncing (the count-mismatch no-op that a dropped span would cause).
_WILDCARD = object()

# Split RAW into word / digit / punctuation / whitespace atoms so we can preserve
# adjacency: only whitespace continues a number span; any other punctuation breaks it
# (except a hyphen between two number-words, handled explicitly below).
_RAW_TOKEN_RE = re.compile(r"[A-Za-z']+|[0-9]+|[^A-Za-z0-9'\s]+|\s+", re.ASCII)

# A numeric OUTPUT run: an optional leading ``$`` then a digit, optionally followed by
# runs of (separator + digits). ASCII-only so CJK numerals are never matched. Anchored
# start/end on a digit, so a trailing sentence period is left outside the run.
_RUN_RE = re.compile(r"\$?[0-9](?:[0-9:.\-]*[0-9])?", re.ASCII)
_CORRECTION_MARKER_RE = re.compile(
    r"\b(?:sorry(?:[\s,]*(?:i\s+mean|make\s+that|or\s+rather))?"
    r"|no(?:\s+wait|\s+make\s+it)?|scratch\s+that|make\s+that|i\s+mean|or\s+rather)\b",
    re.IGNORECASE,
)
# Complete literal envelopes; unsupported forms occupy nonwritable slots.
_LITERAL_RE = re.compile(r"[+\-\u2212]?[$]?[0-9]+(?:[,.:/*+\-\u2013\u2212]+[0-9]+|[ \t]*/[ \t]*[0-9]+)*")
_GROUPED_RE = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+")
_DASHED_RE = re.compile(r"[0-9]+(?:-[0-9]+)+")
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


@dataclass(frozen=True, slots=True)
class DigitMismatchAnalysis:
    """Content-free summary of unresolved, confidently aligned digit corruption."""

    confidently_aligned: bool
    unresolved_mismatches: int


@dataclass(frozen=True, slots=True)
class _SourceSpan:
    """One source number span plus the finite evidence needed for alignment."""

    digits: str | object
    start: int
    end: int
    is_digit_sequence: bool
    terminal_scale: str | None = None
    coefficient_digits: str | None = None
    has_time_context: bool = False
    # True when every token in the span is a single spoken digit word ("five", "oh")
    # or a literal digit run — i.e. the speaker SPELLED the number out and no tens /
    # teens / multiplier compounding is involved. Tens compounds ("five forty five")
    # are exactly the shapes a clock can legitimately render, so they are excluded.
    is_flat_digit_dictation: bool = False


def _classify(word: str):
    """Classify a lowercased alphabetic token as a number-word, or return None."""
    if word in _UNITS:
        return (_UNIT, _UNITS[word])
    if word in _TEENS:
        return (_TEEN, _TEENS[word])
    if word in _TENS:
        return (_TEN, _TENS[word])
    if word in _ORDINALS:
        return (_UNIT, _ORDINALS[word])
    if word in _MULTIPLIERS:
        return (_MULT, _MULTIPLIERS[word])
    return None


def _concat_to_digits(items) -> str | None:
    """Digit-sequence grammar (multiplier-free): concatenate the spoken tokens.

      - a literal digit token contributes its digits verbatim;
      - a "ten" (20..90) optionally absorbs a following 1-9 unit ("forty five" -> 45);
      - a teen (10..19) and a bare unit each form their own group.
    ("ten nineteen" -> "1019", "twenty twenty six" -> "2026", "five forty five" -> "545").
    Returns None if the run has no resolvable groups.
    """
    groups: list[str] = []
    i = 0
    n = len(items)
    while i < n:
        kind, val = items[i]
        if kind == _LIT:
            groups.append(val)
            i += 1
        elif kind == _TEN:
            if i + 1 < n and items[i + 1][0] == _UNIT and 1 <= items[i + 1][1] <= 9:
                groups.append(str(val + items[i + 1][1]))
                i += 2
            else:
                groups.append(str(val))
                i += 1
        elif kind in (_TEEN, _UNIT):
            groups.append(str(val))
            i += 1
        else:  # pragma: no cover - defensive (multiplier spans route elsewhere)
            return None
    return "".join(groups) if groups else None


def _magnitude_to_digits(items) -> str | None:
    """Arithmetic grammar for a span containing >=1 multiplier: running total.

    "two hundred fifty six" -> "256", "two thousand twenty six" -> "2026",
    "one hundred and five" -> "105", "fifteen hundred" -> "1500".

    Returns None (-> WILDCARD, never repaired) on any ill-formed / ambiguous shape so the
    machine only ever fires on unambiguous arithmetic: a leading bare multiplier
    ("hundred five" is vague), a multiplier with nothing to scale, or a magnitude span
    mixed with already-digit tokens.
    """
    if not items or items[0][0] == _MULT:
        return None  # leading bare multiplier ("hundred five") is vague -> wildcard
    total = 0
    current = 0
    i = 0
    n = len(items)
    while i < n:
        kind, val = items[i]
        if kind == _LIT:
            return None  # spoken magnitude mixed with already-digit tokens -> ambiguous
        if kind == _MULT:
            if current == 0:
                return None  # multiplier with nothing to scale ("... thousand million")
            if val == 100:
                current *= 100
            else:  # thousand / million / billion
                total += current * val
                current = 0
            i += 1
        elif kind == _TEN:
            if i + 1 < n and items[i + 1][0] == _UNIT and 1 <= items[i + 1][1] <= 9:
                current += val + items[i + 1][1]
                i += 2
            else:
                current += val
                i += 1
        elif kind in (_TEEN, _UNIT):
            current += val
            i += 1
        else:  # pragma: no cover - defensive
            return None
    return str(total + current)


def _span_to_digits(items) -> str | None:
    """Resolve a contiguous run of classified number tokens to a digit string.

    A span containing a magnitude multiplier is arithmetic ("two hundred fifty six" ->
    256); a multiplier-free span is a digit sequence ("twenty twenty six" -> 2026). The
    two well-formed grammars barely overlap, and where they converge ("two thousand twenty
    six" == "twenty twenty six" == 2026) they agree. Returns None -> WILDCARD on ambiguity.
    """
    if any(kind == _MULT for kind, _ in items):
        return _magnitude_to_digits(items)
    return _concat_to_digits(items)


def _parse_raw_spans(raw: str) -> list[_SourceSpan]:
    """Parse RAW into an ordered list of source-number spans.

    Each element keeps the resolved digit string (or ``_WILDCARD``), source offsets,
    and narrowly reviewed provenance used by both repair and mismatch analysis:
    explicit time context and a terminal literal magnitude scale. Contiguous number
    tokens accumulate into one span; whitespace continues it, a hyphen between two
    number-words joins ("ninety-nine"), an "and" immediately after a multiplier joins
    ("one hundred and five"), and any other non-number word or punctuation breaks it.
    """
    spans: list[_SourceSpan] = []
    current: list[tuple[str, int | str]] = []
    current_start: int | None = None
    current_end = 0
    current_last_word: str | None = None

    def flush():
        nonlocal current_start, current_end, current_last_word
        if current:
            digits = _span_to_digits(current)
            terminal_scale = None
            coefficient_digits = None
            if current[-1][0] == _MULT and current_last_word in _MULTIPLIERS:
                terminal_scale = current_last_word
                coefficient_digits = _span_to_digits(current[:-1]) if len(current) > 1 else None
            spans.append(
                _SourceSpan(
                    digits if digits is not None else _WILDCARD,
                    current_start if current_start is not None else current_end,
                    current_end,
                    not any(kind == _MULT for kind, _value in current),
                    terminal_scale,
                    coefficient_digits,
                    False,
                    all(
                        kind == _LIT
                        or (kind == _UNIT and isinstance(value, int) and 0 <= value <= 9)
                        for kind, value in current
                    ),
                )
            )
            current.clear()
            current_start = None
            current_end = 0
            current_last_word = None

    for m in _RAW_TOKEN_RE.finditer(raw):
        tok = m.group(0)
        first = tok[0]
        if first.isspace():
            continue  # whitespace neither adds nor breaks a span
        if first.isalpha() or first == "'":
            w = tok.strip("'").lower()
            cls = _classify(w)
            if cls is not None:
                if current_start is None:
                    current_start = m.start()
                current.append(cls)
                current_end = m.end()
                current_last_word = w
            elif w == "and" and current and current[-1][0] == _MULT:
                continue  # "and" immediately after a multiplier is a connector
            else:
                flush()  # a non-number word breaks the span
        elif first in _DIGITS:
            if current_start is None:
                current_start = m.start()
            current.append((_LIT, tok))
            current_end = m.end()
            current_last_word = None
        elif tok == "-" and current:
            continue  # a hyphen between number-words joins them ("ninety-nine")
        else:
            flush()  # punctuation breaks the span
    flush()

    # Finite, reviewed clock-cue vocabulary. A meridiem or a part-of-day phrase proves
    # the speaker dictated a TIME, so an ``H:00`` rendering of a bare hour is the
    # source's own shape rather than an invented one. ``o'clock`` is the same evidence
    # by a different word — it names the clock outright — so it joins the set. Nothing
    # weaker ("tonight", "later") qualifies: an uncued bare hour must keep falling back.
    time_context_re = re.compile(
        r"[\s,()\-]*(?:a\.?\s*m\.?|p\.?\s*m\.?|o'?\s*clock|in\s+the\s+"
        r"(?:morning|afternoon|evening))\b",
        re.IGNORECASE,
    )
    return [
        _SourceSpan(
            span.digits,
            span.start,
            span.end,
            span.is_digit_sequence,
            span.terminal_scale,
            span.coefficient_digits,
            bool(time_context_re.match(raw, span.end)),
            span.is_flat_digit_dictation,
        )
        for span in spans
    ]


def _following_scale_word(enhanced: str, m: "re.Match") -> str | None:
    """Return a full scale word immediately following an output digit run."""
    j = m.end()
    while j < len(enhanced) and enhanced[j] in " \t":
        j += 1
    k = j
    while k < len(enhanced) and enhanced[k].isalpha():
        k += 1
    word = enhanced[j:k].lower()
    return word if word in _SCALE_WORDS else None


def _run_is_letter_abutting(enhanced: str, m: "re.Match") -> bool:
    start, end = m.start(), m.end()
    return bool(
        (start > 0 and enhanced[start - 1].isalpha())
        or (end < len(enhanced) and enhanced[end].isalpha())
    )


def _run_is_guarded(enhanced: str, m: "re.Match") -> bool:
    """True when an output run must never be repaired because it is a rendered magnitude
    or is glued to a letter.

    Magnitude numbers have rendering-dependent digit strings ("two hundred fifty six
    thousand" is 256000 OR "256 thousand" OR "256k"), so a run whose digits differ from the
    arithmetic source value must be left alone when it abuts a letter or a scale word —
    otherwise a flat-run replacement over-fires ("90k" -> "90000k").
    """
    return _run_is_letter_abutting(enhanced, m) or _following_scale_word(enhanced, m) is not None


def _overwrite_token(token: str, source_digits: str) -> str:
    """Rewrite ``token``'s digit slots with ``source_digits``, preserving separators.

    - Matching digits -> returned unchanged (no-op).
    - Pure flat digit run -> whole-token replacement (handles substitutions AND drops).
    - Separated skeleton -> refill digit slots in order ONLY when the slot count matches
      the source digit count; otherwise leave untouched (can't place a dropped digit
      inside a separated skeleton without guessing).
    Never adds, removes, or moves a separator.
    """
    token_digits = "".join(c for c in token if c in _DIGITS)
    if not token_digits or token_digits == source_digits:
        return token
    if all(c in _DIGITS for c in token):
        return source_digits  # flat run: unambiguous full replacement
    if len(source_digits) == len(token_digits):
        it = iter(source_digits)
        return "".join(next(it) if c in _DIGITS else c for c in token)
    return token  # separated skeleton with a size mismatch -> conservative no-op


def _literal_digits(token: str) -> str | None:
    if token.isascii() and token.isdigit():
        return token
    if _GROUPED_RE.fullmatch(token) or _DASHED_RE.fullmatch(token):
        return "".join(c for c in token if c in _DIGITS)
    return None


def _source_slots(raw: str) -> list[_SourceSpan]:
    """Protect complete literals before applying main's number-word grammar."""
    literals = list(_LITERAL_RE.finditer(raw))
    # The existing source grammar joins whitespace-separated dictated literal
    # groups. Only plain digit groups may join; unsupported envelopes stay atomic.
    groups = []
    for match in literals:
        if (groups and raw[groups[-1][0]:groups[-1][1]].replace(" ", "").replace("\t", "").isdigit()
                and match.group().isdigit()
                and re.fullmatch(r"[ \t]+", raw[groups[-1][1]:match.start()])):
            groups[-1] = (groups[-1][0], match.end())
        else:
            groups.append((match.start(), match.end()))
    masked = list(raw)
    for start, end in groups:
        masked[start:end] = "x" * (end - start)
    spans = _parse_raw_spans("".join(masked))
    for start, end in groups:
        token = raw[start:end]
        digits = _literal_digits(token)
        if re.fullmatch(r"[0-9]+(?:[ \t]+[0-9]+)+", token):
            digits = re.sub(r"[ \t]", "", token)
        spans.append(_SourceSpan(digits or _WILDCARD, start, end, True))
    return sorted(spans, key=lambda span: span.start)


def _anchors(text: str, slots: list) -> list[tuple[str, str] | None]:
    anchors = []
    previous_end = 0
    for slot in slots:
        start = slot.start() if isinstance(slot, re.Match) else slot.start
        end = slot.end() if isinstance(slot, re.Match) else slot.end
        words = [m.group().casefold() for m in _WORD_RE.finditer(text[previous_end:start])]
        words = [word for word in words if _classify(word) is None]
        anchors.append(tuple(words[-2:]) if len(words) >= 2 else None)
        previous_end = end
    return anchors


def _numeric_correction(raw: str, spans: list[_SourceSpan]) -> bool:
    for marker in _CORRECTION_MARKER_RE.finditer(raw):
        for span in spans:
            if span.end <= marker.start():
                gap = raw[span.end:marker.start()]
            elif marker.end() <= span.start:
                gap = raw[marker.end():span.start]
            else:
                continue
            if not any(char.isalnum() for char in gap):
                return True
    return False


def _source_is_writable(raw: str, span: _SourceSpan) -> bool:
    if not isinstance(span.digits, str) or len(span.digits) < 4:
        return False
    words = [m.group().casefold() for m in _WORD_RE.finditer(raw[span.start:span.end])]
    if not span.is_digit_sequence or span.has_time_context:
        return False
    if any(word in _ORDINALS or word in _MULTIPLIERS for word in words):
        return False
    if ((span.start > 0 and raw[span.start - 1].isalpha())
            or (span.end < len(raw) and raw[span.end].isalpha())):
        return False
    following = re.match(r"[ \t]*([A-Za-z]+)", raw[span.end:])
    if following and following.group(1).casefold() in _SCALE_WORDS:
        return False
    around = raw[max(0, span.start - 12):span.start] + " " + raw[span.end:span.end + 24]
    if re.search(r"\b(?:point|decimal|over|half|quarter|halves|quarters|am|pm|noon|midnight)\b", around, re.I):
        return False
    if not span.is_flat_digit_dictation and re.search(r"\bat\s*$", raw[:span.start], re.I):
        return False
    return True


def _eligible_pairs(enhanced: str, raw: str) -> list[tuple[str, re.Match]]:
    spans = _source_slots(raw)
    runs = list(_LITERAL_RE.finditer(enhanced))
    if not spans or len(spans) != len(runs) or _numeric_correction(raw, spans):
        return []
    source_anchors, output_anchors = _anchors(raw, spans), _anchors(enhanced, runs)
    source_counts, output_counts = Counter(source_anchors), Counter(output_anchors)
    pairs = []
    for span, match, anchor, out_anchor in zip(spans, runs, source_anchors, output_anchors):
        if (anchor is None or anchor != out_anchor
                or source_counts[anchor] != 1 or output_counts[anchor] != 1):
            continue
        if not _source_is_writable(raw, span) or _run_is_guarded(enhanced, match):
            continue
        if _literal_digits(match.group()) is None:
            continue
        # A neighboring number-word makes this a mixed rendering. Repairing just
        # the literal would duplicate part of the spoken value ("4 four").
        before = re.search(r"([A-Za-z']+)\s+$", enhanced[:match.start()])
        after = re.match(r"\s+([A-Za-z']+)", enhanced[match.end():])
        if any(word and _classify(word.group(1).casefold()) is not None
               for word in (before, after)):
            continue
        pairs.append((span.digits, match))
    return pairs


def analyze_digit_mismatches(enhanced: str, raw: str) -> DigitMismatchAnalysis:
    """Count eligible corrupt tokens whose separators prevent safe repair."""
    try:
        pairs = _eligible_pairs(enhanced, raw)
        unresolved = sum(
            _literal_digits(match.group()) != digits
            and _overwrite_token(match.group(), digits) == match.group()
            for digits, match in pairs
        )
        return DigitMismatchAnalysis(bool(pairs), unresolved)
    except Exception:
        logger.debug("[DigitRepair] analysis_skipped reason=unexpected_error")
        return DigitMismatchAnalysis(False, 0)


def repair_source_digits(enhanced: str, raw: str) -> str:
    """Replace digits only inside eligible existing tokens; errors preserve output."""
    try:
        pairs = _eligible_pairs(enhanced, raw)
        result = enhanced
        repaired = 0
        for digits, match in reversed(pairs):
            token = _overwrite_token(match.group(), digits)
            if token != match.group():
                result = result[:match.start()] + token + result[match.end():]
                repaired += 1
        if repaired:
            logger.debug("[DigitRepair] runs_repaired=%d", repaired)
        return result
    except Exception:
        logger.debug("[DigitRepair] skipped reason=unexpected_error")
        return enhanced
