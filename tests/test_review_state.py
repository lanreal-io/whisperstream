"""Unit tests for ReviewState persistence class."""

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from whisperstream.review_state import ReviewState


def make_review_state(tmp_path: Path) -> ReviewState:
    """Create a ReviewState backed by tmp_path."""
    with patch("whisperstream.review_state.get_app_dir", return_value=tmp_path):
        return ReviewState()


# ---------------------------------------------------------------------------
# 1. Initial state
# ---------------------------------------------------------------------------

def test_initial_state(tmp_path):
    rs = make_review_state(tmp_path)
    d = rs.to_dict()
    assert d["session_count"] == 0
    assert d["distinct_days"] == []
    assert d["permanently_dismissed"] is False
    assert d["prompt_count"] == 0
    assert d["last_prompt_date"] == ""
    assert d["last_dismiss_date"] == ""
    assert d["last_submitted_date"] == ""


# ---------------------------------------------------------------------------
# 2. record_session increments count and deduplicates dates
# ---------------------------------------------------------------------------

def test_record_session_increments(tmp_path):
    rs = make_review_state(tmp_path)
    rs.record_session()
    rs.record_session()
    rs.record_session()
    d = rs.to_dict()
    assert d["session_count"] == 3
    # All calls happen on the same day, so only one entry
    assert len(d["distinct_days"]) == 1
    assert d["distinct_days"][0] == date.today().isoformat()


# ---------------------------------------------------------------------------
# 3. should_show_prompt — not enough sessions
# ---------------------------------------------------------------------------

def test_should_show_prompt_not_enough_sessions(tmp_path):
    rs = make_review_state(tmp_path)
    # Add 5 distinct days but only 19 sessions
    for i in range(5):
        day = (date.today() - timedelta(days=i)).isoformat()
        if day not in rs._data["distinct_days"]:
            rs._data["distinct_days"].append(day)
    rs._data["session_count"] = 19
    assert rs.should_show_prompt() is False


# ---------------------------------------------------------------------------
# 4. should_show_prompt — not enough distinct days
# ---------------------------------------------------------------------------

def test_should_show_prompt_not_enough_days(tmp_path):
    rs = make_review_state(tmp_path)
    rs._data["session_count"] = 25
    # Only 4 distinct days
    for i in range(4):
        day = (date.today() - timedelta(days=i)).isoformat()
        rs._data["distinct_days"].append(day)
    assert rs.should_show_prompt() is False


# ---------------------------------------------------------------------------
# 5. should_show_prompt — entitlement is not consulted
# ---------------------------------------------------------------------------

def test_should_show_prompt_ignores_license_state(tmp_path):
    rs = make_review_state(tmp_path)
    rs._data["session_count"] = 25
    for i in range(6):
        rs._data["distinct_days"].append((date.today() - timedelta(days=i)).isoformat())
    assert rs.should_show_prompt() is True


# ---------------------------------------------------------------------------
# 6. should_show_prompt — all conditions met
# ---------------------------------------------------------------------------

def test_should_show_prompt_all_conditions_met(tmp_path):
    rs = make_review_state(tmp_path)
    rs._data["session_count"] = 20
    for i in range(5):
        rs._data["distinct_days"].append((date.today() - timedelta(days=i)).isoformat())
    assert rs.should_show_prompt() is True


# ---------------------------------------------------------------------------
# 7. Two dismissals permanently dismiss
# ---------------------------------------------------------------------------

def test_record_dismissal_twice_permanently_dismisses(tmp_path):
    rs = make_review_state(tmp_path)
    rs.record_dismissal()
    assert rs._data["permanently_dismissed"] is False
    rs.record_dismissal()
    assert rs._data["permanently_dismissed"] is True


def test_explicit_opt_out_permanently_dismisses_immediately(tmp_path):
    rs = make_review_state(tmp_path)

    rs.record_permanent_dismissal()

    assert rs._data["prompt_count"] == 1
    assert rs._data["last_dismiss_date"] == date.today().isoformat()
    assert rs._data["permanently_dismissed"] is True
    # should_show_prompt must respect this
    rs._data["session_count"] = 25
    for i in range(6):
        rs._data["distinct_days"].append((date.today() - timedelta(days=i)).isoformat())
    assert rs.should_show_prompt() is False


# ---------------------------------------------------------------------------
# 8. record_later restarts the interval but does NOT permanently dismiss
# ---------------------------------------------------------------------------

def test_record_later_sets_cooldown(tmp_path):
    rs = make_review_state(tmp_path)
    rs.record_later()
    assert rs._data["last_dismiss_date"] == date.today().isoformat()
    assert rs._data["permanently_dismissed"] is False
    assert rs._data["prompt_count"] == 0  # record_later does not increment prompt_count


# ---------------------------------------------------------------------------
# 9. Persistence survives reload
# ---------------------------------------------------------------------------

def test_persistence_survives_reload(tmp_path):
    rs = make_review_state(tmp_path)
    rs.record_session()
    rs.record_session()

    # Create a new instance backed by the same path
    rs2 = make_review_state(tmp_path)
    assert rs2._data["session_count"] == 2
    assert len(rs2._data["distinct_days"]) == 1


# ---------------------------------------------------------------------------
# 10. Corrupt file falls back to defaults without error
# ---------------------------------------------------------------------------

def test_corrupt_file_fallback(tmp_path):
    state_file = tmp_path / "review_state.json"
    state_file.write_text("this is not valid json }{{{", encoding="utf-8")

    # Should not raise; should load defaults
    rs = make_review_state(tmp_path)
    assert rs._data["session_count"] == 0
    assert rs._data["permanently_dismissed"] is False


# ---------------------------------------------------------------------------
# 11. Ask cadence: 30-day interval, at most three asks without a GitHub click
# ---------------------------------------------------------------------------

def _eligible(tmp_path):
    rs = make_review_state(tmp_path)
    rs._data["session_count"] = 20
    for i in range(5):
        rs._data["distinct_days"].append((date.today() - timedelta(days=i)).isoformat())
    return rs


def test_shown_prompt_waits_thirty_days(tmp_path):
    rs = _eligible(tmp_path)
    rs.record_prompt_shown()
    assert rs._data["prompt_count"] == 1
    assert rs.should_show_prompt() is False
    rs._data["last_prompt_date"] = (date.today() - timedelta(days=29)).isoformat()
    assert rs.should_show_prompt() is False
    rs._data["last_prompt_date"] = (date.today() - timedelta(days=30)).isoformat()
    assert rs.should_show_prompt() is True


def test_snooze_restarts_interval(tmp_path):
    rs = _eligible(tmp_path)
    rs._data["last_prompt_date"] = (date.today() - timedelta(days=40)).isoformat()
    rs.record_later()
    assert rs.should_show_prompt() is False


def test_stops_after_three_asks(tmp_path):
    rs = _eligible(tmp_path)
    for _ in range(3):
        rs.record_prompt_shown()
        rs._data["last_prompt_date"] = (date.today() - timedelta(days=31)).isoformat()
    assert rs._data["prompt_count"] == 3
    assert rs.should_show_prompt() is False


def test_github_handoff_ends_asks(tmp_path):
    rs = _eligible(tmp_path)
    rs.record_submission()
    assert rs.should_show_prompt() is False
