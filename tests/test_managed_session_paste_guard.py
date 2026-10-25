"""Tests for the bracketed-paste payload guard (Task 2.7).

Bracketed paste behavior in Claude Code is undocumented. The spike proved a
payload containing ``/`` is delivered as one message, but Claude's own docs
note that text starting with ``!`` pasted into an empty prompt enters shell
mode, "matching typed ``!`` behavior". Leading ``/``, ``!``, ``@``, ``#``, and
``?`` are therefore hazardous first characters and must be refused rather than
silently escaped, prefixed, or quoted (scope decision 6: block rather than
guess).

An embedded ``ESC[201~`` (the bracketed-paste terminator) would end the paste
early and let a suffix leak in as though typed; an embedded C0 control byte
other than ``\n`` is similarly refused. A well-formed multi-line reply must
still pass untouched.
"""
from __future__ import annotations

import pytest

from whisperstream.managed_session_paste_guard import (
    BRACKETED_PASTE_TERMINATOR,
    HAZARDOUS_LEADING_CHARACTERS,
    guard_reply_payload,
)

HAZARDOUS_CHARS = ["/", "!", "@", "#", "?"]


@pytest.mark.parametrize("char", HAZARDOUS_CHARS)
def test_hazardous_leading_character_is_refused(char):
    assert guard_reply_payload(f"{char}some text") is False


@pytest.mark.parametrize("char", HAZARDOUS_CHARS)
def test_hazardous_leading_character_is_refused_after_leading_whitespace(char):
    assert guard_reply_payload(f"   \n\t {char}some text") is False


def test_hazardous_leading_characters_match_the_locked_set():
    assert HAZARDOUS_LEADING_CHARACTERS == frozenset(HAZARDOUS_CHARS)


def test_embedded_bracketed_paste_terminator_is_refused():
    payload = f"an ordinary reply{BRACKETED_PASTE_TERMINATOR}injected suffix"
    assert guard_reply_payload(payload) is False


def test_embedded_control_byte_is_refused():
    assert guard_reply_payload("hello\x07world") is False


@pytest.mark.parametrize("byte", [0x00, 0x01, 0x08, 0x1B, 0x1F])
def test_every_c0_control_other_than_newline_is_refused(byte):
    assert guard_reply_payload(f"hello{chr(byte)}world") is False


def test_a_multi_line_ordinary_reply_passes():
    payload = "line one\nline two\nline three"
    assert guard_reply_payload(payload) is True


def test_an_ordinary_single_line_reply_passes():
    assert guard_reply_payload("this is a normal dictated reply") is True


def test_a_reply_starting_with_ordinary_punctuation_passes():
    assert guard_reply_payload(".this starts with a period") is True


def test_leading_whitespace_before_an_ordinary_reply_passes():
    assert guard_reply_payload("   this has leading whitespace") is True


def test_the_guard_never_mutates_the_payload():
    """A refused payload is refused wholesale, never escaped or altered."""
    payload = "/help me with this"
    # The function returns a bare bool; there is no mutated string to inspect.
    result = guard_reply_payload(payload)
    assert isinstance(result, bool)
    assert payload == "/help me with this"
