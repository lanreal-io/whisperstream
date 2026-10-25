"""Pure recognition of dictated email sign-offs."""

# Default-email-signature sign-off heuristic. The email prompts (v6/v14) emit a
# sign-off ONLY when one was dictated, and it lands as a short trailing block
# after a blank line (e.g. "Thanks,\nAlex"). We treat the email as already
# signed off when, after right-stripping, the final blank-line-separated block
# is short in BOTH dimensions: at most _SIGNOFF_MAX_LINES lines, each at most
# _SIGNOFF_MAX_LINE_CHARS characters. A longer trailing paragraph (a real body
# paragraph) or a multi-line list does NOT match, so it still gets signed.
_SIGNOFF_MAX_LINES = 3
_SIGNOFF_MAX_LINE_CHARS = 40

# Closing phrases that, when they ARE the entire first line of a short trailing
# block, mark a dictated sign-off even without a greeting/body/sign-off three-
# block shape (the no-greeting 2-block case, e.g. "...looks off.\n\nThanks,\n
# Alex"). Matched EXACTLY (after trimming a trailing comma/period), so a short
# body line that merely starts with a closing word ("Thanks for the update.")
# is NOT mistaken for a sign-off.
_CLOSING_PHRASES = frozenset({
    "thanks", "thank you", "thanks so much", "thanks again", "many thanks",
    "best", "best regards", "regards", "kind regards", "warm regards",
    "warmly", "all the best", "cheers", "talk soon", "sincerely",
    "take care", "cordially", "respectfully", "yours truly", "yours",
})


def _is_closing_phrase_line(line: str) -> bool:
    """True when *line* is, on its own, a known closing phrase.

    Exact match after trimming surrounding whitespace and a trailing
    comma/period/exclamation. "Thanks," -> True; "Thanks for the update." ->
    False (the closing word is followed by body text, so it is not a sign-off).
    """
    normalized = line.strip().rstrip(".,!").strip().lower()
    return normalized in _CLOSING_PHRASES


def _is_signoff_name_block(block: str) -> bool:
    """True when *block* looks like a dictated bare-name sign-off (e.g. "Alex",
    "Alex Chen", "Alex\\nChen").

    Closes the 2-block double-sign gap: a no-greeting email ending in a bare name
    (not a known closing phrase) was not recognized as a dictated sign-off, so a
    default signature got appended on top of it. A name sign-off is short and is
    NOT a sentence: 1-2 non-empty lines, each at most 3 words, every word
    initial-capitalized (Title Case), and no line ending in sentence punctuation
    (. ! ? :). That distinguishes "Alex Chen" (sign-off) from a short body line
    like "call me back" (lowercase words) or "See you Monday." (trailing period).

    Deliberately conservative -> it errs toward NOT matching: a genuine short
    body block that happens to be Title Case merely misses the auto-append, which
    is far milder than emitting a visibly double-signed email.
    """
    lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
    if not lines or len(lines) > 2:
        return False
    for ln in lines:
        if ln[-1] in ".!?:":
            return False
        words = ln.split()
        if not words or len(words) > 3:
            return False
        if not all(w[0].isupper() for w in words):
            return False
    return True


def ends_with_signoff(text: str) -> bool:
    """True when *text* already ends in a short, dictated sign-off block.

    After right-stripping, split on blank lines and require the final block
    to be SHORT in both dimensions (at most _SIGNOFF_MAX_LINES lines, each at
    most _SIGNOFF_MAX_LINE_CHARS chars). A longer paragraph or a multi-line
    list never matches, so it still gets signed. Two recognized shapes:

    - **Greeting + body + sign-off (>= 3 blocks):** a short trailing block
      sitting after a real body is a sign-off regardless of its wording
      (e.g. "Hi Sam,\n\n...\n\nThanks,\nAlex", or a bare name).

    - **No greeting (2 blocks):** the trailing block counts as a sign-off
      ONLY when its first line is, on its own, a known closing phrase
      (e.g. "...looks off.\n\nThanks,\nAlex"). This closes the 2-block
      double-sign gap. A short body line that merely begins with a closing
      word ("Hi Sam,\n\nThanks for the update.") is NOT a closing phrase by
      exact match, so it still gets signed.

    A single block (no blank-line separator) is the body itself -> signed.
    """
    stripped = text.rstrip()
    blocks = stripped.split("\n\n")
    if len(blocks) < 2:
        return False
    final_block = blocks[-1]
    lines = final_block.split("\n")
    if len(lines) > _SIGNOFF_MAX_LINES:
        return False
    if not all(len(line.strip()) <= _SIGNOFF_MAX_LINE_CHARS for line in lines):
        return False
    # >= 3 blocks: greeting + body + short trailing block -> sign-off.
    if len(blocks) >= 3:
        return True
    # 2 blocks (no greeting): a sign-off is either a leading closing phrase
    # ("Thanks,\nAlex") OR a name-shaped trailing block (a bare dictated name
    # like "Alex" / "Alex Chen"). The name-shape check closes the bare-name
    # double-sign gap.
    return _is_closing_phrase_line(lines[0]) or _is_signoff_name_block(final_block)
