"""Acceptance is fixed after admission and reread only on backend restart."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from whisperstream import eula


def test_admitted_hot_path_never_reads_files_or_takes_ledger_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(eula, "_eula_dir", lambda: tmp_path)
    policy = eula.load_eula_policy_status()
    record = eula.write_acceptance(policy.terms_version, policy.terms_sha256, "test")
    grant = eula.get_acceptance_grant()

    def unexpected(*args, **kwargs):
        raise AssertionError("admitted session tried to reload evidence")

    monkeypatch.setattr(eula, "load_eula_policy_status", unexpected)
    monkeypatch.setattr(eula, "_read_acceptance_ledger_unlocked", unexpected)
    monkeypatch.setattr(eula, "_atomic_write_json", unexpected)
    # The old implementation would block every caller on this lock.
    with ThreadPoolExecutor(max_workers=4) as pool:
        with eula._acceptance_lock:
            futures = [pool.submit(eula.get_acceptance_grant) for _ in range(100)]
            assert all(f.result(timeout=5) is grant for f in futures)
    assert eula.acceptance_grant_is_current(grant)
    assert eula.write_acceptance(policy.terms_version, policy.terms_sha256, "retry") == record


def test_policy_update_is_seen_on_restart_only(tmp_path, monkeypatch):
    monkeypatch.setattr(eula, "_eula_dir", lambda: tmp_path)
    session = eula.get_acceptance_session()
    original = session.policy
    updated = replace(original, terms_version="next", terms_sha256="a" * 64)
    monkeypatch.setattr(eula, "load_eula_policy_status", lambda: updated)
    # Accept exactly what this session displayed, even if an updater has replaced
    # files. A different agreement cannot be substituted into the live session.
    eula.write_acceptance(original.terms_version, original.terms_sha256, "test")
    grant = eula.get_acceptance_grant()
    evidence = (tmp_path / "acceptance.json").read_bytes()
    with pytest.raises(eula.AcceptancePolicyError):
        eula.write_acceptance(updated.terms_version, updated.terms_sha256, "test")
    assert eula.get_acceptance_grant() is grant
    assert (tmp_path / "acceptance.json").read_bytes() == evidence
    monkeypatch.setattr(eula, "_acceptance_session", None)
    assert not eula.get_acceptance_grant().allowed
    assert eula.get_acceptance_session().policy == updated


def test_external_acceptance_cannot_open_a_blocked_session(tmp_path, monkeypatch):
    monkeypatch.setattr(eula, "_eula_dir", lambda: tmp_path)
    session = eula.get_acceptance_session()
    policy = session.policy
    (tmp_path / "acceptance.json").write_text(json.dumps({
        "eula_version": policy.terms_version, "eula_sha256": policy.terms_sha256,
        "eula_accepted_at": "2026-10-12T12:00:00Z", "software_version": "test",
    }), encoding="utf-8")
    assert not eula.get_acceptance_grant().allowed
    monkeypatch.setattr(eula, "_acceptance_session", None)
    assert eula.get_acceptance_grant().allowed
