"""Unit tests for whisperstream.eula — Phase 1 helpers.

Covers the five groups specified in tasks.md Task 1.3:
  (a) compute_eula_sha256 — known-vector check
  (b) parse_eula_version — extract version, raise on missing
  (c) is_major_version_bump — bump detection
  (d) utc_now_iso8601 — format constraints
  (e) read_bundled_eula_text — non-empty, valid ASCII
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone


def _active_identity() -> tuple[str, str]:
    from whisperstream.eula import load_eula_policy_status

    policy = load_eula_policy_status()
    assert policy.valid
    assert policy.terms_version is not None
    assert policy.terms_sha256 is not None
    return policy.terms_version, policy.terms_sha256


class TestComputeEulaSha256(unittest.TestCase):
    """(a) compute_eula_sha256 — known-vector check."""

    def test_known_vector(self):
        """SHA-256 of b'hello' must match the well-known digest."""
        from whisperstream.eula import compute_eula_sha256

        expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        self.assertEqual(compute_eula_sha256(b"hello"), expected)

    def test_empty_bytes(self):
        """SHA-256 of empty bytes is the SHA-256 of the empty string."""
        from whisperstream.eula import compute_eula_sha256

        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.assertEqual(compute_eula_sha256(b""), expected)

    def test_returns_lowercase_hex(self):
        """Returned digest must be 64 lowercase hex characters."""
        from whisperstream.eula import compute_eula_sha256

        digest = compute_eula_sha256(b"test")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")


class TestParseEulaVersion(unittest.TestCase):
    """(b) parse_eula_version — extract version, raise on missing."""

    def test_extracts_1_0_from_markdown_bold(self):
        """Should extract '1.0' from the markdown-bold header used in .md sources."""
        from whisperstream.eula import parse_eula_version

        data = b"...**Version 1.0**\n\n**Effective 2026-04-18**..."
        self.assertEqual(parse_eula_version(data), "1.0")

    def test_extracts_2_3_from_plaintext(self):
        """Should extract '2.3' from the plaintext header used in bundled .txt."""
        from whisperstream.eula import parse_eula_version

        data = b"Some preamble\nVersion 2.3\n\nEffective 2027-01-01\nMore text"
        self.assertEqual(parse_eula_version(data), "2.3")

    def test_raises_on_unversioned(self):
        """Should raise ValueError when no version header is present."""
        from whisperstream.eula import parse_eula_version

        with self.assertRaises(ValueError):
            parse_eula_version(b"This text has no version header at all.")

    def test_raises_on_empty_bytes(self):
        """Should raise ValueError on empty input."""
        from whisperstream.eula import parse_eula_version

        with self.assertRaises(ValueError):
            parse_eula_version(b"")

    def test_multi_component_version(self):
        """Should extract multi-part version strings like '1.2.3'."""
        from whisperstream.eula import parse_eula_version

        data = b"Version 1.2.3\n\nEffective 2026-06-01"
        self.assertEqual(parse_eula_version(data), "1.2.3")


class TestIsMajorVersionBump(unittest.TestCase):
    """(c) is_major_version_bump — bump detection."""

    def test_minor_bump_is_false(self):
        """1.0 -> 1.1 is a minor bump, not material."""
        from whisperstream.eula import is_major_version_bump

        self.assertFalse(is_major_version_bump("1.0", "1.1"))

    def test_major_bump_1_to_2_is_true(self):
        """1.0 -> 2.0 is a major bump, material."""
        from whisperstream.eula import is_major_version_bump

        self.assertTrue(is_major_version_bump("1.0", "2.0"))

    def test_major_bump_1_9_to_2_0_is_true(self):
        """1.9 -> 2.0 is a major bump, material."""
        from whisperstream.eula import is_major_version_bump

        self.assertTrue(is_major_version_bump("1.9", "2.0"))

    def test_same_version_is_false(self):
        """1.0 -> 1.0 (no change) is not a bump."""
        from whisperstream.eula import is_major_version_bump

        self.assertFalse(is_major_version_bump("1.0", "1.0"))

    def test_major_bump_0_to_1_is_true(self):
        """0.x -> 1.0 is a major bump."""
        from whisperstream.eula import is_major_version_bump

        self.assertTrue(is_major_version_bump("0.9", "1.0"))

    def test_major_bump_2_to_3_is_true(self):
        """2.5 -> 3.0 is a major bump."""
        from whisperstream.eula import is_major_version_bump

        self.assertTrue(is_major_version_bump("2.5", "3.0"))


class TestUtcNowIso8601(unittest.TestCase):
    """(d) utc_now_iso8601 — format constraints."""

    def test_ends_with_capital_z(self):
        """Timestamp must end with capital 'Z'."""
        from whisperstream.eula import utc_now_iso8601

        ts = utc_now_iso8601()
        self.assertTrue(ts.endswith("Z"), f"Expected Z suffix, got: {ts!r}")

    def test_no_plus_00_00_offset(self):
        """Timestamp must not contain '+00:00' (UTC offset notation)."""
        from whisperstream.eula import utc_now_iso8601

        ts = utc_now_iso8601()
        self.assertNotIn("+00:00", ts)

    def test_no_fractional_seconds(self):
        """Timestamp must not contain a fractional-seconds component."""
        from whisperstream.eula import utc_now_iso8601

        ts = utc_now_iso8601()
        self.assertNotIn(".", ts)

    def test_parses_back_via_fromisoformat(self):
        """Timestamp (with Z stripped) must parse via datetime.fromisoformat."""
        from whisperstream.eula import utc_now_iso8601

        ts = utc_now_iso8601()
        # fromisoformat does not accept the trailing 'Z' in Python < 3.11,
        # so strip it first (this is the documented usage pattern in tasks.md).
        stripped = ts.rstrip("Z")
        dt = datetime.fromisoformat(stripped)
        # Parsed datetime should be close to now (within 5 seconds).
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        delta = abs((now - dt).total_seconds())
        self.assertLess(delta, 5, f"Parsed timestamp is too far from now: {dt}")

    def test_matches_full_format_regex(self):
        """Timestamp must match YYYY-MM-DDTHH:MM:SSZ exactly."""
        from whisperstream.eula import utc_now_iso8601

        ts = utc_now_iso8601()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestReadBundledEulaText(unittest.TestCase):
    """(e) read_bundled_eula_text — non-empty, valid ASCII."""

    def test_returns_bytes(self):
        """read_bundled_eula_text() must return a bytes object."""
        from whisperstream.eula import read_bundled_eula_text

        data = read_bundled_eula_text()
        self.assertIsInstance(data, bytes)

    def test_non_empty(self):
        """Returned bytes must be non-empty."""
        from whisperstream.eula import read_bundled_eula_text

        data = read_bundled_eula_text()
        self.assertGreater(len(data), 0)

    def test_valid_ascii(self):
        """All bytes in the returned data must be valid ASCII (< 128)."""
        from whisperstream.eula import read_bundled_eula_text

        data = read_bundled_eula_text()
        self.assertTrue(
            data.isascii(),
            "EULA.txt contains non-ASCII bytes — run scripts/derive-eula-txt.py",
        )

    def test_contains_version_header(self):
        """Bundled EULA text must contain a parseable version header."""
        from whisperstream.eula import parse_eula_version, read_bundled_eula_text

        data = read_bundled_eula_text()
        version = parse_eula_version(data)
        # Version string should look like 'X.Y' or 'X.Y.Z'
        self.assertRegex(version, r"^\d+\.\d+")

    def test_markdown_source_contains_version_header(self):
        """legal/EULA.md (markdown source) must also be parseable.

        Independent of the derive script: if someone changes the markdown
        header in a way the parser can't handle, this catches it before
        derivation papers over the issue.
        """
        from pathlib import Path
        from whisperstream.eula import parse_eula_version

        md_path = Path(__file__).resolve().parent.parent / "legal" / "EULA.md"
        data = md_path.read_bytes()
        version = parse_eula_version(data)
        self.assertRegex(version, r"^\d+\.\d+")


class TestPolicySelectedEulaResources:
    def test_current_policy_pins_exact_versioned_terms(self):
        from whisperstream.eula import (
            parse_eula_version,
            read_bundled_eula_policy,
            read_bundled_eula_terms,
        )

        policy = json.loads(read_bundled_eula_policy())
        terms = read_bundled_eula_terms(policy["terms_path"])

        assert policy["schema_version"] == 1
        assert policy["notice_id"] == "open-source-2.1-gpl3"
        assert policy["requirement_kind"] == "substantive_update"
        assert policy["terms_version"] == "2.1"
        assert policy["terms_path"] == "eula/2.1.txt"
        assert len(policy["summary"]) >= 4
        assert "free" in " ".join(policy["summary"]).lower()
        assert "Hugging Face" in " ".join(policy["summary"])
        assert parse_eula_version(terms) == policy["terms_version"]
        assert hashlib.sha256(terms).hexdigest() == policy["terms_sha256"]

    def test_nuitka_lookup_resolves_nested_policy_terms(self, tmp_path, monkeypatch):
        import whisperstream.eula as eula_mod

        terms = tmp_path / "resources" / "eula" / "1.3.txt"
        terms.parent.mkdir(parents=True)
        terms.write_bytes(b"exact terms")
        compiled = type("Compiled", (), {"containing_dir": str(tmp_path)})()
        monkeypatch.setitem(vars(eula_mod), "__compiled__", compiled)

        assert eula_mod.read_bundled_eula_terms("eula/1.3.txt") == b"exact terms"

    def test_policy_terms_lookup_rejects_parent_traversal(self):
        from whisperstream.eula import read_bundled_eula_terms

        with unittest.TestCase().assertRaises(ValueError):
            read_bundled_eula_terms("../EULA.txt")

    def test_policy_status_fails_closed_when_terms_hash_disagrees(self, monkeypatch):
        import whisperstream.eula as eula_mod
        from whisperstream.eula import load_eula_policy_status

        monkeypatch.setattr(eula_mod, "read_bundled_eula_terms", lambda _path: b"wrong terms")
        status = load_eula_policy_status()
        assert status.valid is False
        assert status.reason == "terms_invalid"
        assert status.terms_text is None


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Task 1.5: acceptance / opt-out I/O helpers
# Uses pytest-style classes (tmp_path + monkeypatch fixtures) rather than
# unittest.TestCase so that pytest fixtures are injected natively.
# ---------------------------------------------------------------------------


class TestWriteReadAcceptance:
    """(a) write_acceptance + read_acceptance round-trip."""

    def test_round_trip_all_fields(self, tmp_path, monkeypatch):
        """write_acceptance then read_acceptance returns all four fields intact."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import read_acceptance, write_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        version, sha256 = _active_identity()
        written = write_acceptance(version, sha256, "0.2.0")
        read_back = read_acceptance()

        assert read_back is not None
        assert read_back["eula_version"] == version
        assert read_back["eula_sha256"] == sha256
        assert read_back["software_version"] == "0.2.0"
        assert read_back["eula_accepted_at"] == written["eula_accepted_at"]

    def test_accepted_at_format(self, tmp_path, monkeypatch):
        """eula_accepted_at must match YYYY-MM-DDTHH:MM:SSZ."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import write_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        version, sha256 = _active_identity()
        record = write_acceptance(version, sha256, "0.1.0")
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", record["eula_accepted_at"])


class TestReadAcceptanceMissing:
    """(b) read_acceptance returns None when file is missing."""

    def test_returns_none_when_missing(self, tmp_path, monkeypatch):
        """read_acceptance must return None when acceptance.json does not exist."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import read_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        # No file written — directory exists but acceptance.json is absent.
        assert read_acceptance() is None


class TestReadAcceptanceMalformed:
    """(c) read_acceptance returns None when file is malformed JSON."""

    def test_returns_none_on_malformed_json(self, tmp_path, monkeypatch):
        """read_acceptance must return None (not raise) on invalid JSON content."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import ACCEPTANCE_FILENAME, read_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        # Write deliberately malformed JSON to the acceptance file.
        (tmp_path / ACCEPTANCE_FILENAME).write_text("{not valid json}", encoding="utf-8")

        assert read_acceptance() is None


class TestWriteOptOutExpiry:
    """(d) write_opt_out expires_at is exactly 30 days after declined_at."""

    def test_expires_at_is_30_days_after_declined_at(self, tmp_path, monkeypatch):
        """expires_at must be exactly 30 days after declined_at (day delta == 30)."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import write_opt_out

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        # expires_at param is ignored — the implementation recomputes it from
        # declined_at (see eula.py write_opt_out implementation).
        record = write_opt_out(
            declined_version="2.0",
            pinned_version="1.0",
            software_version="0.3.0",
            expires_at="ignored",
        )

        declined_dt = datetime.fromisoformat(record["declined_at"].rstrip("Z"))
        expires_dt = datetime.fromisoformat(record["expires_at"].rstrip("Z"))
        delta = expires_dt - declined_dt
        assert delta.days == 30, (
            f"Expected 30-day window, got {delta.days} days "
            f"(declined_at={record['declined_at']!r}, expires_at={record['expires_at']!r})"
        )


class TestOptOutIsActive:
    """(e) opt_out_is_active — active, missing, and expired scenarios."""

    def test_active_when_before_expires_at(self, tmp_path, monkeypatch):
        """opt_out_is_active returns True when current time is before expires_at."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import opt_out_is_active, write_opt_out

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        write_opt_out(
            declined_version="2.0",
            pinned_version="1.0",
            software_version="0.3.0",
            expires_at="ignored",
        )

        # Use a "now" well in the past so the 30-day future window is definitely active.
        assert opt_out_is_active(now_iso="2000-01-01T00:00:00Z") is True

    def test_false_when_file_missing(self, tmp_path, monkeypatch):
        """opt_out_is_active returns False when update-opt-out.json does not exist."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import opt_out_is_active

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        assert opt_out_is_active() is False

    def test_false_when_now_past_expires_at(self, tmp_path, monkeypatch):
        """opt_out_is_active returns False when now_iso >= expires_at (window expired)."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import opt_out_is_active, write_opt_out

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        write_opt_out(
            declined_version="2.0",
            pinned_version="1.0",
            software_version="0.3.0",
            expires_at="ignored",
        )

        # Pass a far-future "now" that is definitely after the 30-day expires_at.
        assert opt_out_is_active(now_iso="2099-01-01T00:00:00Z") is False


# ---------------------------------------------------------------------------
# Task 3.2: detect_amendment_state + re-ask cadence
# ---------------------------------------------------------------------------

UTC = timezone.utc


class TestDetectAmendmentStateMinorBump:
    """(a) Minor bump (1.0 -> 1.1) is not a material amendment."""

    def test_minor_bump_no_amendment_needed(self, tmp_path, monkeypatch):
        """detect_amendment_state with bundled 1.1 and accepted 1.0 returns needs_amendment=False."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import detect_amendment_state

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        result = detect_amendment_state("1.1", {"eula_version": "1.0", "eula_accepted_at": "2026-01-01T00:00:00Z"})

        assert result["needs_amendment"] is False
        assert result["pinned_version"] is None


class TestDetectAmendmentStateMajorBumpNoOptOut:
    """(b) Major bump (1.0 -> 2.0) with no opt-out file requires re-acceptance."""

    def test_major_bump_needs_amendment(self, tmp_path, monkeypatch):
        """detect_amendment_state with bundled 2.0, accepted 1.0, no opt-out → needs_amendment=True."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import detect_amendment_state

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        result = detect_amendment_state("2.0", {"eula_version": "1.0", "eula_accepted_at": "2026-01-01T00:00:00Z"})

        assert result["needs_amendment"] is True
        assert result["pinned_version"] == "1.0"


class TestDetectAmendmentStateMajorBumpWithActiveOptOut:
    """(c) Major bump with an active (not-yet-expired) opt-out file suppresses the re-ask."""

    def test_active_opt_out_suppresses_amendment(self, tmp_path, monkeypatch):
        """detect_amendment_state with active opt-out returns needs_amendment=False."""
        import json
        import whisperstream.eula as eula_mod
        from whisperstream.eula import OPT_OUT_FILENAME, detect_amendment_state

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        # Write an opt-out file whose expires_at is 29 days in the future.
        future_expires = (datetime.now(UTC) + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")
        opt_out_record = {
            "declined_version": "2.0",
            "pinned_version": "1.0",
            "declined_at": "2026-04-01T00:00:00Z",
            "expires_at": future_expires,
            "software_version": "0.3.0",
        }
        (tmp_path / OPT_OUT_FILENAME).write_text(
            json.dumps(opt_out_record, indent=2) + "\n", encoding="utf-8"
        )

        result = detect_amendment_state("2.0", {"eula_version": "1.0", "eula_accepted_at": "2026-01-01T00:00:00Z"})

        assert result["needs_amendment"] is False
        assert result["pinned_version"] is None


class TestDetectAmendmentStateMajorBumpWithExpiredOptOut:
    """(d) Major bump with an expired opt-out file re-fires the amendment ask."""

    def test_expired_opt_out_re_asks_amendment(self, tmp_path, monkeypatch):
        """detect_amendment_state with expired opt-out returns needs_amendment=True again."""
        import json
        import whisperstream.eula as eula_mod
        from whisperstream.eula import OPT_OUT_FILENAME, detect_amendment_state

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        # Write an opt-out file whose expires_at is well in the past.
        past_expires = datetime(2020, 1, 1, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        opt_out_record = {
            "declined_version": "2.0",
            "pinned_version": "1.0",
            "declined_at": "2019-12-02T00:00:00Z",
            "expires_at": past_expires,
            "software_version": "0.3.0",
        }
        (tmp_path / OPT_OUT_FILENAME).write_text(
            json.dumps(opt_out_record, indent=2) + "\n", encoding="utf-8"
        )

        result = detect_amendment_state("2.0", {"eula_version": "1.0", "eula_accepted_at": "2026-01-01T00:00:00Z"})

        assert result["needs_amendment"] is True
        assert result["pinned_version"] == "1.0"


class TestWriteAcceptanceRemovesOptOut:
    """(e) write_acceptance removes an existing opt-out file from disk."""

    def test_write_acceptance_deletes_opt_out_file(self, tmp_path, monkeypatch):
        """After write_acceptance, update-opt-out.json must no longer exist."""
        import whisperstream.eula as eula_mod
        from whisperstream.eula import OPT_OUT_FILENAME, write_acceptance, write_opt_out

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)

        # First create an opt-out file.
        write_opt_out(
            declined_version="2.0",
            pinned_version="1.0",
            software_version="0.3.0",
            expires_at="ignored",
        )
        assert (tmp_path / OPT_OUT_FILENAME).exists(), "opt-out file should exist before write_acceptance"

        # Now accept the new EULA — this must remove the opt-out file.
        version, sha256 = _active_identity()
        write_acceptance(version, sha256, "0.3.0")

        assert not (tmp_path / OPT_OUT_FILENAME).exists(), (
            "opt-out file should have been removed by write_acceptance"
        )


# ---------------------------------------------------------------------------
# Re-acknowledgement floor: version_lt + detect_reacknowledgement_state
# ---------------------------------------------------------------------------


class TestVersionOrdering:
    """version_lt: component-wise numeric ordering, not string ordering."""

    def test_numeric_not_string_ordering(self):
        """1.2 < 1.10 numerically (a naive string compare would say the reverse)."""
        from whisperstream.eula import version_lt

        assert version_lt("1.2", "1.10") is True
        assert version_lt("1.10", "1.2") is False

    def test_equal_is_not_less_than(self):
        from whisperstream.eula import version_lt

        assert version_lt("1.2", "1.2") is False

    def test_older_is_less_than(self):
        from whisperstream.eula import version_lt

        assert version_lt("1.0", "1.2") is True
        assert version_lt("1.2", "1.0") is False

    def test_malformed_sorts_below_any_real_version(self):
        """An empty / unparseable version sorts as (0,), below any real version."""
        from whisperstream.eula import version_lt

        assert version_lt("", "1.2") is True
        assert version_lt("garbage", "1.2") is True


class TestDetectReacknowledgementState:
    """detect_reacknowledgement_state: the non-material affirmative re-consent path."""

    def test_below_floor_needs_reacknowledgement(self):
        """Accepted below the floor (and no major bump) requires re-acknowledgement."""
        from whisperstream.eula import REACK_FLOOR_VERSION, detect_reacknowledgement_state

        result = detect_reacknowledgement_state(REACK_FLOOR_VERSION, {"eula_version": "1.0"})
        assert result["needs_reacknowledgement"] is True

    def test_at_floor_no_reacknowledgement(self):
        """Accepting the floor version clears the prompt (one-shot per floor bump)."""
        from whisperstream.eula import REACK_FLOOR_VERSION, detect_reacknowledgement_state

        result = detect_reacknowledgement_state(
            REACK_FLOOR_VERSION, {"eula_version": REACK_FLOOR_VERSION}
        )
        assert result["needs_reacknowledgement"] is False

    def test_major_bump_defers_to_amendment_flow(self):
        """A material major bump is owned by detect_amendment_state; re-ack stands down."""
        from whisperstream.eula import detect_reacknowledgement_state

        result = detect_reacknowledgement_state("2.0", {"eula_version": "1.0"})
        assert result["needs_reacknowledgement"] is False

    def test_no_acceptance_no_reacknowledgement(self):
        """First-run (no acceptance record) is handled elsewhere, not here."""
        from whisperstream.eula import REACK_FLOOR_VERSION, detect_reacknowledgement_state

        result = detect_reacknowledgement_state(REACK_FLOOR_VERSION, None)
        assert result["needs_reacknowledgement"] is False

    def test_malformed_acceptance_no_reacknowledgement(self):
        """A record missing eula_version must not crash or prompt (crash-loop guard)."""
        from whisperstream.eula import REACK_FLOOR_VERSION, detect_reacknowledgement_state

        result = detect_reacknowledgement_state(REACK_FLOOR_VERSION, {"foo": "bar"})
        assert result["needs_reacknowledgement"] is False


class TestSchema2AcceptanceLedger:
    def test_first_accept_writes_schema2_and_keeps_four_field_projection(
        self, tmp_path, monkeypatch
    ):
        import whisperstream.eula as eula_mod
        from whisperstream.eula import read_acceptance, read_acceptance_ledger, write_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)
        version, sha256 = _active_identity()
        record = write_acceptance(version, sha256, "1.3.0")

        on_disk = json.loads((tmp_path / "acceptance.json").read_text(encoding="utf-8"))
        assert on_disk["schema_version"] == 2
        assert on_disk["records"] == [{**record, "event_type": "first_run_acceptance"}]
        assert read_acceptance() == record
        assert set(read_acceptance()) == {
            "eula_version",
            "eula_accepted_at",
            "software_version",
            "eula_sha256",
        }
        assert read_acceptance_ledger() == on_disk

    def test_legacy_normalization_is_non_inventive_idempotent_and_read_only(
        self, tmp_path, monkeypatch
    ):
        import whisperstream.eula as eula_mod
        from whisperstream.eula import read_acceptance_ledger, write_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)
        version, sha256 = _active_identity()
        legacy = {
            "eula_version": version,
            "eula_accepted_at": "2026-07-31T12:34:56Z",
            "software_version": "1.2.0",
            "eula_sha256": sha256,
        }
        path = tmp_path / "acceptance.json"
        original = json.dumps(legacy, indent=2) + "\n"
        path.write_text(original, encoding="utf-8")

        first = read_acceptance_ledger()
        second = read_acceptance_ledger()
        assert path.read_text(encoding="utf-8") == original
        assert first == second
        assert first["records"] == [{**legacy, "event_type": "legacy_migrated"}]

        current = write_acceptance(version, sha256, "1.3.0")
        ledger = read_acceptance_ledger()
        assert current == legacy
        assert path.read_text(encoding="utf-8") == original
        assert ledger["records"] == [{**legacy, "event_type": "legacy_migrated"}]
        assert sum(r["event_type"] == "legacy_migrated" for r in ledger["records"]) == 1

    def test_partial_scalar_future_and_mirror_mismatch_are_rejected(self, tmp_path, monkeypatch):
        import whisperstream.eula as eula_mod
        from whisperstream.eula import read_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)
        version, sha256 = _active_identity()
        event = {
            "eula_version": version,
            "eula_accepted_at": "2026-07-31T12:34:56Z",
            "software_version": "1.3.0",
            "eula_sha256": sha256,
            "event_type": "first_run_acceptance",
        }
        invalid_payloads = [
            [event],
            {"eula_version": version},
            {"schema_version": 3, **{k: event[k] for k in event if k != "event_type"}, "records": [event]},
            {
                "schema_version": 2,
                **{k: event[k] for k in event if k != "event_type"},
                "eula_sha256": "0" * 64,
                "records": [event],
            },
        ]
        for payload in invalid_payloads:
            (tmp_path / "acceptance.json").write_text(json.dumps(payload), encoding="utf-8")
            assert read_acceptance() is None


class TestExactAcceptanceAuthority:
    def test_valid_1_6_ledger_requires_1_7_and_is_preserved_after_acceptance(
        self, tmp_path, monkeypatch
    ):
        import whisperstream.eula as eula_mod
        from whisperstream.eula import (
            compute_eula_sha256,
            get_acceptance_grant,
            read_acceptance_ledger,
            write_acceptance,
        )

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)
        prior_bytes = b"Prior accepted terms 1.6\n"
        prior = {
            "eula_version": "1.6",
            "eula_accepted_at": "2026-08-24T12:00:00Z",
            "software_version": "1.6.0",
            "eula_sha256": compute_eula_sha256(prior_bytes),
            "event_type": "first_run_acceptance",
        }
        ledger = {
            "schema_version": 2,
            **{key: value for key, value in prior.items() if key != "event_type"},
            "records": [prior],
        }
        (tmp_path / "acceptance.json").write_text(
            json.dumps(ledger), encoding="utf-8"
        )

        grant = get_acceptance_grant()
        assert grant.allowed is False
        assert grant.reason == "acceptance_mismatch"

        version, sha256 = _active_identity()
        current = write_acceptance(version, sha256, "1.7.0")
        records = read_acceptance_ledger()["records"]
        assert records == [prior, {**current, "event_type": "updated_terms_acceptance"}]

    def test_exact_grant_is_fixed_until_restart_then_revalidated(
        self, tmp_path, monkeypatch
    ):
        import whisperstream.eula as eula_mod
        from whisperstream.eula import acceptance_grant_is_current, get_acceptance_grant, write_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)
        version, sha256 = _active_identity()
        assert get_acceptance_grant().allowed is False
        write_acceptance(version, sha256, "1.3.0")
        grant = get_acceptance_grant()
        assert grant.allowed is True
        assert grant.reason == "accepted"
        assert acceptance_grant_is_current(grant) is True

        ledger = json.loads((tmp_path / "acceptance.json").read_text(encoding="utf-8"))
        ledger["eula_sha256"] = "0" * 64
        ledger["records"][-1]["eula_sha256"] = "0" * 64
        (tmp_path / "acceptance.json").write_text(json.dumps(ledger), encoding="utf-8")
        assert get_acceptance_grant() is grant
        assert acceptance_grant_is_current(grant)
        monkeypatch.setattr(eula_mod, "_acceptance_session", None)  # Process restart.
        mismatch = get_acceptance_grant()
        assert mismatch.allowed is False
        assert mismatch.reason == "acceptance_mismatch"
        assert acceptance_grant_is_current(grant) is False

    def test_opt_out_never_authorizes_or_appends_acceptance(self, tmp_path, monkeypatch):
        import whisperstream.eula as eula_mod
        from whisperstream.eula import get_acceptance_grant, write_opt_out

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)
        write_opt_out("2.0", "1.3", "1.3.0", "ignored")
        assert not (tmp_path / "acceptance.json").exists()
        assert get_acceptance_grant().allowed is False

    def test_mismatched_request_and_corrupt_prior_evidence_are_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        import whisperstream.eula as eula_mod
        from whisperstream.eula import AcceptanceLedgerError, AcceptancePolicyError, write_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)
        version, sha256 = _active_identity()
        try:
            write_acceptance(version, "0" * 64, "1.3.0")
        except AcceptancePolicyError:
            pass
        else:
            raise AssertionError("mismatched target was accepted")
        assert not (tmp_path / "acceptance.json").exists()

        path = tmp_path / "acceptance.json"
        path.write_text("{broken", encoding="utf-8")
        try:
            write_acceptance(version, sha256, "1.3.0")
        except AcceptanceLedgerError:
            pass
        else:
            raise AssertionError("corrupt evidence was overwritten")
        assert path.read_text(encoding="utf-8") == "{broken"

    def test_atomic_write_failure_preserves_prior_evidence(self, tmp_path, monkeypatch):
        import whisperstream.eula as eula_mod
        from whisperstream.eula import write_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)
        version, sha256 = _active_identity()
        write_acceptance(version, sha256, "1.3.0")
        path = tmp_path / "acceptance.json"
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger["eula_sha256"] = "0" * 64
        ledger["records"][-1]["eula_sha256"] = "0" * 64
        path.write_text(json.dumps(ledger), encoding="utf-8")
        monkeypatch.setattr(eula_mod, "_acceptance_session", None)
        assert not eula_mod.get_acceptance_grant().allowed
        before = path.read_bytes()

        def fail_write(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(eula_mod, "_atomic_write_json", fail_write)
        try:
            write_acceptance(version, sha256, "1.3.1")
        except OSError:
            pass
        else:
            raise AssertionError("write failure did not propagate")
        assert path.read_bytes() == before

    def test_concurrent_accepts_admit_once(self, tmp_path, monkeypatch):
        import whisperstream.eula as eula_mod
        from whisperstream.eula import read_acceptance_ledger, write_acceptance

        monkeypatch.setattr(eula_mod, "_eula_dir", lambda: tmp_path)
        version, sha256 = _active_identity()
        software_versions = [f"1.3.{index}" for index in range(12)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda app_version: write_acceptance(version, sha256, app_version), software_versions))

        ledger = read_acceptance_ledger()
        assert len(ledger["records"]) == 1
        assert all(record == results[0] for record in results)
        assert ledger["records"][0]["software_version"] in software_versions
