"""Tests for whisperstream.browser_domain.

Only the pure helper `extract_registrable_domain` is unit-tested here: it is the
omnibox guard the whole per-site-routing feature trusts, so it is table-driven.

The live UIA read (`read_active_browser_domain`) needs a real browser window and
is NOT unit-testable; it is verified end-to-end in the feature's Phase 3 manual
checklist. We only assert here that it never raises and degrades to None when UIA
is unavailable / the process is not a browser.

Privacy: the normalized host is in-memory only. The raw address-bar value does
not leave the UIA helper, and nothing from this module may be logged.
"""

from __future__ import annotations

import pytest

from types import SimpleNamespace
from unittest.mock import MagicMock

import whisperstream.browser_domain as browser_domain

from whisperstream.browser_domain import (
    extract_registrable_domain,
    read_active_browser_domain,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Full URLs -> registrable host.
        ("https://github.com/x/y", "github.com"),
        ("https://news.ycombinator.com/newest", "news.ycombinator.com"),
        # Subdomains are preserved as the host (www-strip + host only).
        # The spike's `to_domain` does NOT collapse subdomains, and the task's
        # algorithm spec is "(www-strip + host only)"; `news.ycombinator.com`
        # MUST keep its three labels, which rules out any last-two-labels or
        # eTLD+1 reduction (that would corrupt it to `ycombinator.com`). So
        # `mail.google.com` stays `mail.google.com`. The frontend mirror
        # (normalizeDomain) does the same. See module docstring.
        ("https://mail.google.com", "mail.google.com"),
        # www. is stripped.
        ("https://www.github.com/x", "github.com"),
        ("http://www.example.com", "example.com"),
        # Scheme-less input gets http:// prepended.
        ("github.com/anthropics/claude-code", "github.com"),
        ("news.ycombinator.com", "news.ycombinator.com"),
        # user@ and :port are stripped.
        ("https://user@example.com:8443/path", "example.com"),
        # Trailing/leading whitespace around a clean URL is fine.
        ("  https://github.com/x  ", "github.com"),
        # --- Guard: must return None ---
        # Search text (interior whitespace).
        ("how to center a div", None),
        ("how to ...", None),
        # Bare word, no dot.
        ("localhost", None),
        ("newtab", None),
        # Empty / None.
        ("", None),
        (None, None),
        ("   ", None),
        # New-tab style values with no host.
        ("about:newtab", None),
    ],
)
def test_extract_registrable_domain(raw, expected):
    assert extract_registrable_domain(raw) == expected


def test_read_active_browser_domain_never_raises_non_browser():
    """A non-browser process name returns None immediately, never raises."""
    assert read_active_browser_domain(1234, "notepad.exe") is None
    assert read_active_browser_domain(0, "explorer.exe") is None


def test_read_active_browser_domain_never_raises_bad_hwnd():
    """A bogus hwnd against a browser process must degrade to None, not raise.

    On a machine without UIA available this exercises the availability-flag
    short-circuit; on a live win32 machine it exercises the belt-and-suspenders
    COM error handling (no such window).
    """
    result = read_active_browser_domain(0, "chrome.exe")
    assert result is None or isinstance(result, str)


def _fake_uia_constants(monkeypatch):
    monkeypatch.setattr(
        browser_domain,
        "_UIA",
        SimpleNamespace(
            UIA_ControlTypePropertyId=1,
            UIA_EditControlTypeId=2,
            UIA_ComboBoxControlTypeId=3,
            TreeScope_Descendants=4,
            UIA_ValuePatternId=5,
            IUIAutomationValuePattern=object(),
        ),
        raising=False,
    )


def test_uia_worker_does_not_fall_back_to_arbitrary_edit_control(monkeypatch):
    _fake_uia_constants(monkeypatch)
    unrelated = MagicMock()
    unrelated.CurrentName = "Document text field"
    found = MagicMock(Length=1)
    found.GetElement.return_value = unrelated
    element = MagicMock()
    element.FindAll.return_value = found
    uia = MagicMock()

    assert browser_domain._read_domain(uia, element, "address") is None
    unrelated.GetCurrentPattern.assert_not_called()


def test_uia_worker_returns_only_normalized_host(monkeypatch):
    _fake_uia_constants(monkeypatch)
    unrelated = MagicMock()
    unrelated.CurrentName = "Document text field"
    address = MagicMock()
    address.CurrentName = "Address and search bar"
    value_pattern = MagicMock(CurrentValue="https://www.example.com/private?q=1")
    address.GetCurrentPattern.return_value.QueryInterface.return_value = value_pattern
    found = MagicMock(Length=2)
    found.GetElement.side_effect = [unrelated, address]
    element = MagicMock()
    element.FindAll.return_value = found
    uia = MagicMock()

    assert browser_domain._read_domain(uia, element, "address") == "example.com"
    unrelated.GetCurrentPattern.assert_not_called()
