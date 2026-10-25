"""Bracketed-paste payload guard for hazardous first characters (Task 2.7).

**Bracketed paste behavior in Claude Code is undocumented.** The spike proved a
payload containing ``/`` is delivered as one message when pasted, but Claude's
own docs note that text starting with ``!`` pasted into an empty prompt enters
shell mode, "matching typed ``!`` behavior". Leading ``/``, ``!``, ``@``, ``#``,
and ``?`` are therefore treated as hazardous first characters for any payload
this feature delivers over the PTY master (Task 3.3).

This module is a guard against an undocumented upstream behavior, not a
parser of it, and must be revisited if Claude documents bracketed-paste
handling authoritatively.

Behavior, applied to every outbound reply payload before it reaches the
wrapper: strip leading whitespace, inspect the first character, and when it is
one of the five hazardous characters, **refuse delivery**. Also refuse a
payload containing a raw ``ESC[201~`` (the bracketed-paste terminator, which
would end the paste early and let a suffix leak in as though typed) or any C0
control byte other than ``\n``.

Per scope decision 6 (block rather than guess), this function never mutates
the payload: no escaping, no prefixing a space, no quoting. A dictated reply
that arrives altered is worse than one that does not arrive at all. It returns
a bare bool, matching the reason-free ``can_reply`` precedent
(``managed_sessions.py``); the caller reports the refusal, this module does
not explain it.
"""
from __future__ import annotations

#: Leading characters that risk entering Claude's shell mode, slash-command
#: mode, or other special input handling instead of being read as plain text.
HAZARDOUS_LEADING_CHARACTERS = frozenset({"/", "!", "@", "#", "?"})

#: The bracketed-paste end sequence. Embedded mid-payload, it would close the
#: paste early and let whatever follows be interpreted as typed input.
BRACKETED_PASTE_TERMINATOR = "\x1b[201~"

#: C0 control range, excluding the one control byte an ordinary multi-line
#: reply legitimately contains.
_ALLOWED_CONTROL_CHAR = "\n"


def guard_reply_payload(payload: str) -> bool:
    """Return ``True`` only when *payload* is safe to deliver as-is.

    ``False`` means refuse delivery outright. There is no partial or escaped
    delivery path: the caller must drop the payload wholesale, never mutate
    and retry it.
    """
    if not isinstance(payload, str):
        return False

    if BRACKETED_PASTE_TERMINATOR in payload:
        return False

    for char in payload:
        if char == _ALLOWED_CONTROL_CHAR:
            continue
        if ord(char) < 0x20:
            return False

    stripped = payload.lstrip()
    if stripped and stripped[0] in HAZARDOUS_LEADING_CHARACTERS:
        return False

    return True
